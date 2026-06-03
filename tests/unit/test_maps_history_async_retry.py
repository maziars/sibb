"""L1 tests for the async-write retry in `_fetch_maps_history`.

iOS Maps writes ZHISTORYITEM 2-3s after the user commits a directions
action. The verifier runs immediately on episode end; without retry,
the row isn't flushed yet and the check false-fails. Variant D
2026-05-27 trial reproduced this — z_ent=16 rows existed on disk a
few seconds later but the verifier had already given up.

Fix design (constrained, bounded):
  - retry only when `min_create_iso` is in the selector (this is the
    "looking for new rows since baseline" signal — static-state
    checks don't pay the latency tax)
  - retry only when the initial read returned 0 matching rows
  - poll up to 5 seconds wallclock at 500ms intervals
  - bail as soon as a row appears

Tests cover the happy path, the retry-then-success case, the timeout
case, and the no-retry-by-default behavior.
"""
from __future__ import annotations
import asyncio
import os
import sys
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

from sibb_verify import _fetch_maps_history, _filter_maps_history  # noqa: E402


class _FakeReader:
    udid = "test-udid"


def _row(z_ent: int = 16, create_iso: str = "2026-05-28T07:00:00Z",
          query: str = "") -> dict:
    return {"z_ent": z_ent, "query": query,
            "location_display": "", "latitude": None, "longitude": None,
            "muid": None, "create_iso": create_iso,
            "modification_iso": create_iso}


class _FakeMapsHistory:
    """Replaces sibb_state._maps_history. Configurable to return
    different result sequences on successive calls — needed to test
    the retry-until-row-appears path.

    Usage:
        fake = _FakeMapsHistory(returns=[[], [], [row]])
        # 3rd call returns one row.
    """
    def __init__(self, returns):
        self.returns = list(returns)
        self.call_count = 0
        self.call_times = []  # monotonic time of each call

    def __call__(self, udid, limit=1000):
        self.call_times.append(time.monotonic())
        self.call_count += 1
        if not self.returns:
            return []
        # Return the next-scheduled response; if we run past the list,
        # keep returning the last one (simulates "still empty").
        if len(self.returns) > 1:
            return self.returns.pop(0)
        return list(self.returns[0])


def _patch_maps_history(fake) -> None:
    import sibb_state
    sibb_state._maps_history = fake


def _restore_maps_history() -> None:
    """Re-import to restore the real function reference."""
    import importlib
    import sibb_state
    importlib.reload(sibb_state)


# ── happy path: no retry needed ──────────────────────────────────────────────

def test_no_retry_when_first_read_has_matching_rows():
    """If the first read returns rows matching the selector, we
    return immediately — no sleep, no extra calls."""
    fake = _FakeMapsHistory(returns=[[_row(z_ent=16)]])
    _patch_maps_history(fake)
    try:
        t0 = time.monotonic()
        rows = asyncio.run(_fetch_maps_history(
            _FakeReader(),
            {"z_ent": 16, "min_create_iso": "2026-05-28T00:00:00Z"}))
        elapsed = time.monotonic() - t0
        assert len(rows) == 1
        assert fake.call_count == 1
        assert elapsed < 0.2, (
            f"unexpected delay {elapsed:.2f}s — should be <0.2s")
    finally:
        _restore_maps_history()


# ── retry path: empty then row appears ───────────────────────────────────────

def test_retry_until_row_appears_within_timeout():
    """Initial read empty + min_create_iso → poll. Row appears on
    the 2nd retry call. We return the row."""
    fake = _FakeMapsHistory(returns=[
        [],                       # call 1 (immediate)
        [],                       # call 2 (after 500ms)
        [_row(z_ent=16)],         # call 3 (after 1000ms) — row arrives
    ])
    _patch_maps_history(fake)
    try:
        t0 = time.monotonic()
        rows = asyncio.run(_fetch_maps_history(
            _FakeReader(),
            {"z_ent": 16, "min_create_iso": "2026-05-28T00:00:00Z"}))
        elapsed = time.monotonic() - t0
        assert len(rows) == 1
        assert fake.call_count == 3
        # ~1s wallclock (immediate + 500ms + 500ms). Generous bounds.
        assert 0.9 < elapsed < 1.8, (
            f"elapsed {elapsed:.2f}s outside expected 0.9-1.8s "
            f"(2 sleeps of 500ms)")
    finally:
        _restore_maps_history()


# ── timeout path: never appears ──────────────────────────────────────────────

def test_timeout_after_budget_returns_empty():
    """If the row never appears, the retry loop times out and
    returns []. Budget is 10s (was 5s; bumped after variant D
    2026-05-28 showed iOS Maps can take >5s for ZHISTORYITEM flush
    on a busy sim)."""
    fake = _FakeMapsHistory(returns=[[]])  # always empty
    _patch_maps_history(fake)
    try:
        t0 = time.monotonic()
        rows = asyncio.run(_fetch_maps_history(
            _FakeReader(),
            {"z_ent": 16, "min_create_iso": "2026-05-28T00:00:00Z"}))
        elapsed = time.monotonic() - t0
        assert rows == []
        # Cap is 10s + tolerance for the final read.
        assert 9.8 < elapsed < 11.5, (
            f"elapsed {elapsed:.2f}s outside expected 9.8-11.5s")
        # We should have polled ~20 times (10s / 500ms) + 1 initial.
        assert 18 <= fake.call_count <= 22, (
            f"call_count={fake.call_count}, expected ~20-21")
    finally:
        _restore_maps_history()


# ── no-retry-by-default: no min_create_iso means no async-write concern ──────

def test_no_retry_without_min_create_iso():
    """A selector that doesn't include min_create_iso is asking
    about static state — no async-write concern, no retry."""
    fake = _FakeMapsHistory(returns=[[]])  # would retry indefinitely
    _patch_maps_history(fake)
    try:
        t0 = time.monotonic()
        rows = asyncio.run(_fetch_maps_history(
            _FakeReader(),
            {"z_ent": 16}))  # no min_create_iso
        elapsed = time.monotonic() - t0
        assert rows == []
        assert fake.call_count == 1, (
            "should be exactly one call — no retry without "
            "min_create_iso")
        assert elapsed < 0.2
    finally:
        _restore_maps_history()


# ── selector-coherence: retry doesn't change filter behavior ──────────────────

def test_retry_re_applies_filter_on_each_read():
    """Confirms the retry calls `_filter_maps_history` per attempt,
    not a stale cache. Otherwise z_ent / min_create_iso etc. would
    silently mismatch on the row that finally arrives."""
    # First read: 1 row but z_ent=20 (doesn't match z_ent=16 selector)
    # Second read: now has z_ent=16 row too
    fake = _FakeMapsHistory(returns=[
        [_row(z_ent=20, create_iso="2026-05-28T07:00:00Z")],
        [_row(z_ent=20, create_iso="2026-05-28T07:00:00Z"),
         _row(z_ent=16, create_iso="2026-05-28T07:00:01Z")],
    ])
    _patch_maps_history(fake)
    try:
        rows = asyncio.run(_fetch_maps_history(
            _FakeReader(),
            {"z_ent": 16, "min_create_iso": "2026-05-28T00:00:00Z"}))
        assert len(rows) == 1
        assert rows[0]["z_ent"] == 16
        assert fake.call_count >= 2
    finally:
        _restore_maps_history()


# ── below-baseline row doesn't satisfy the wait ──────────────────────────────

def test_row_below_min_create_iso_does_not_end_retry():
    """A new row that's OLDER than baseline_iso shouldn't end the
    retry — it's stale data, not the agent's commit. Keep polling."""
    old_row = _row(z_ent=16, create_iso="2026-01-01T00:00:00Z")  # OLD
    fake = _FakeMapsHistory(returns=[
        [old_row],          # 1st: just the old row (filtered out)
        [old_row],          # 2nd
        [old_row, _row(z_ent=16,
                        create_iso="2026-05-28T07:00:00Z")],  # 3rd: new arrives
    ])
    _patch_maps_history(fake)
    try:
        rows = asyncio.run(_fetch_maps_history(
            _FakeReader(),
            {"z_ent": 16, "min_create_iso": "2026-05-28T00:00:00Z"}))
        assert len(rows) == 1
        assert rows[0]["create_iso"].startswith("2026-05-28")
        assert fake.call_count == 3
    finally:
        _restore_maps_history()


# ── filter helper standalone (sanity) ────────────────────────────────────────

def test_filter_maps_history_z_ent_passthrough():
    rows = [_row(z_ent=16), _row(z_ent=20), _row(z_ent=16)]
    out = _filter_maps_history(rows, {"z_ent": 16}, set())
    assert len(out) == 2
    assert all(r["z_ent"] == 16 for r in out)


def test_filter_maps_history_default_drops_directions():
    """No explicit z_ent / z_ent_in / include_directions → default
    scope is search-flavored only (20 + 22). z_ent=16 rows dropped."""
    rows = [_row(z_ent=16), _row(z_ent=20), _row(z_ent=22)]
    out = _filter_maps_history(rows, {}, search_flavored={20, 22})
    z_ents = sorted(r["z_ent"] for r in out)
    assert z_ents == [20, 22]
