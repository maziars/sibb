"""FitnessHandler — L1 tests.

v1 ships registry-only (no apply primitive — workouts come with the
first Fitness task). The real value-add is the
`fitness.activity_summary` resource fetcher, which reads the
host-side `healthdb_secure.sqlite` directly to bypass the HealthKit
auth dialog that blocks Swift writes on the simulator
(see `IOS_SIM_QUIRKS.md` §10).

Tests use an in-memory `activity_caches` stand-in mirroring the real
schema; the schema was confirmed empirically against an iOS 26.3 sim
on 2026-05-17 (see the `sibb_state.py` docstring above
`_fitness_activity_summary`).
"""

from __future__ import annotations

import sqlite3

import pytest

import sibb_state
from sibb_state import (
    HANDLERS,
    FitnessHandler,
    _APPLE_REFERENCE_EPOCH,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────


def test_fitness_handler_registered_by_bundle_id():
    assert FitnessHandler.bundle_id == "com.apple.Fitness"
    assert HANDLERS[FitnessHandler.bundle_id] is FitnessHandler


def test_fitness_handler_declares_healthkit_tcc_services():
    """Shares HealthHandler's authorization surface — same TCC
    grant requests, even though writes don't go through reliably
    yet. Lints against accidental drift."""
    assert FitnessHandler.tcc_services == ["health-share", "health-update"]


def test_fitness_handler_is_not_a_pre_runner():
    assert FitnessHandler.pre_runner is False
    assert FitnessHandler.pre_runner_kinds == []


def test_fitness_handler_no_depends_on():
    """Even though Fitness and Health share an HKHealthStore, there's
    no formal ordering constraint between their reset/apply paths."""
    assert FitnessHandler.depends_on == []


def test_canonicalize_fitness_friendly_name():
    assert canonicalize_app("Fitness") == "com.apple.Fitness"
    assert canonicalize_app("fitness") == "com.apple.Fitness"
    assert canonicalize_app("FITNESS") == "com.apple.Fitness"


def test_fitness_contributes_tcc_services_to_runner_permissions():
    """`ensure_runner_permissions` iterates HANDLERS for tcc_services.
    Fitness must contribute health-share and health-update so the
    runner can later read activity data via HealthKit (when writes
    land)."""
    services = collect_tcc_services()
    assert "health-share" in services
    assert "health-update" in services


# ─────────────────────────── apply / reset ───────────────────────────


async def test_reset_is_noop():
    h = FitnessHandler(reader=None)
    await h.reset()


async def test_apply_raises_clear_v1_error():
    """v1 has no apply primitive; the error message must point at
    the read path (`fitness.activity_summary` + healthdb) so a
    confused engineer doesn't think Fitness is just broken."""
    h = FitnessHandler(reader=None)
    with pytest.raises(ValueError, match="no apply primitive"):
        await h.apply({"type": "workout",
                        "activity_type": "running",
                        "duration_min": 30})


# ─────────────────────────── healthdb helpers ────────────────────────
#
# Direct sqlite reads against a stand-in DB. Real healthdb is at
# `data/Library/Health/healthdb_secure.sqlite` per simulator UDID.

class _UdidStub:
    def __init__(self, udid="FAKE"):
        self.udid = udid


def _make_test_healthdb(path) -> None:
    """Create a stand-in healthdb_secure.sqlite with just the two
    tables `_fitness_activity_summary` JOINs: `samples` and
    `activity_caches`. Mirrors the real iOS 26.3 schema closely
    enough for the helper to run."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE samples (
            data_id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_date REAL NOT NULL,
            end_date REAL NOT NULL,
            data_type INTEGER
        );
        CREATE TABLE activity_caches (
            data_id INTEGER PRIMARY KEY,
            cache_index INTEGER,
            sequence INTEGER NOT NULL DEFAULT 0,
            activity_mode INTEGER,
            paused INTEGER,
            wheelchair_use INTEGER,
            energy_burned REAL,
            energy_burned_goal REAL,
            brisk_minutes REAL,
            brisk_minutes_goal REAL,
            active_hours REAL,
            active_hours_goal REAL,
            steps REAL,
            version INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


def _seed_day(db_path, *, day_iso: str, energy: float,
              energy_goal: float, steps: float,
              brisk: float = None, brisk_goal: float = None,
              active: float = None, active_goal: float = None) -> None:
    """Insert one day's worth of (samples + activity_caches) data.
    `day_iso` is a YYYY-MM-DD string treated as midnight UTC for
    the start_date; end_date is +24h."""
    import datetime as _dt
    dt = _dt.datetime.strptime(day_iso, "%Y-%m-%d")
    start_unix = dt.timestamp()  # rough — local tz vs UTC isn't strict here
    # Convert unix → Apple reference epoch
    start_apple = start_unix - _APPLE_REFERENCE_EPOCH
    end_apple = start_apple + 86400.0

    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO samples (start_date, end_date, data_type) "
        "VALUES (?, ?, ?);",
        (start_apple, end_apple, 0))
    data_id = cur.lastrowid
    conn.execute(
        "INSERT INTO activity_caches "
        "(data_id, energy_burned, energy_burned_goal, "
        " brisk_minutes, brisk_minutes_goal, "
        " active_hours, active_hours_goal, steps) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
        (data_id, energy, energy_goal, brisk, brisk_goal,
         active, active_goal, steps))
    conn.commit()
    conn.close()


def test_activity_summary_returns_empty_when_db_missing(
        monkeypatch, tmp_path):
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(tmp_path / "nope.sqlite"))
    assert sibb_state._fitness_activity_summary("FAKE") == []


def test_activity_summary_returns_empty_when_no_activity_rows(
        monkeypatch, tmp_path):
    """DB exists but no activity_caches rows — valid state for a
    sim where Fitness has launched but no synthetic data has been
    seeded yet. Must return [], not raise."""
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))
    assert sibb_state._fitness_activity_summary("FAKE") == []


def test_activity_summary_returns_one_row_per_day(
        monkeypatch, tmp_path):
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    _seed_day(db, day_iso="2026-05-15",
              energy=500.0, energy_goal=600.0, steps=8000.0)
    _seed_day(db, day_iso="2026-05-16",
              energy=720.0, energy_goal=600.0, steps=12000.0)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))

    rows = sibb_state._fitness_activity_summary("FAKE")
    assert len(rows) == 2
    # Ordered DESC by start_date — newest first.
    assert rows[0]["start_iso"].startswith("2026-05-16")
    assert rows[1]["start_iso"].startswith("2026-05-15")


def test_activity_summary_ring_columns_round_trip(
        monkeypatch, tmp_path):
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    _seed_day(db, day_iso="2026-05-17",
              energy=2229.5, energy_goal=120.0, steps=105593.0)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))

    rows = sibb_state._fitness_activity_summary("FAKE")
    assert len(rows) == 1
    r = rows[0]
    assert r["energy_burned"] == pytest.approx(2229.5)
    assert r["energy_burned_goal"] == pytest.approx(120.0)
    assert r["steps"] == pytest.approx(105593.0)


def test_activity_summary_iphone_only_nulls_pass_through(
        monkeypatch, tmp_path):
    """Brisk/active columns are NULL on iPhone-only sims (Apple
    Watch input drives those rings). The fetcher must surface
    NULLs as Python `None`, not as `0.0` or missing keys —
    distinguishing "0 minutes of exercise" from "no data"."""
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    _seed_day(db, day_iso="2026-05-17",
              energy=2229.5, energy_goal=120.0, steps=105593.0,
              brisk=None, brisk_goal=None,
              active=None, active_goal=None)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))

    r = sibb_state._fitness_activity_summary("FAKE")[0]
    assert r["brisk_minutes"] is None
    assert r["brisk_minutes_goal"] is None
    assert r["active_hours"] is None
    assert r["active_hours_goal"] is None


def test_activity_summary_apple_epoch_conversion():
    """The Apple reference date is 2001-01-01 00:00:00 UTC; adding
    `_APPLE_REFERENCE_EPOCH` to a sample's stored timestamp gives
    a Unix timestamp. Lints the constant against drift — if Apple
    ever changes their reference date, this fails fast."""
    import datetime as _dt
    # 2001-01-01 00:00:00 UTC
    expected = _dt.datetime(2001, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
    derived = _dt.datetime.fromtimestamp(
        _APPLE_REFERENCE_EPOCH, tz=_dt.timezone.utc)
    assert derived == expected


# ─────────────────────────── resource fetcher ────────────────────────


def test_fitness_activity_summary_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "fitness.activity_summary" in RESOURCE_FETCHERS


async def test_fetcher_requires_udid():
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError

    class _NoUdid:
        pass
    fetcher = RESOURCE_FETCHERS["fitness.activity_summary"]
    with pytest.raises(ResourceFetchError, match=".udid"):
        await fetcher(_NoUdid(), {})


async def test_fetcher_returns_all_rows_by_default(
        monkeypatch, tmp_path):
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    _seed_day(db, day_iso="2026-05-15",
              energy=500.0, energy_goal=600.0, steps=8000.0)
    _seed_day(db, day_iso="2026-05-16",
              energy=720.0, energy_goal=600.0, steps=12000.0)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))

    rows = await RESOURCE_FETCHERS["fitness.activity_summary"](
        _UdidStub(), {})
    assert len(rows) == 2


async def test_fetcher_date_filter_narrows_to_iso_prefix(
        monkeypatch, tmp_path):
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    _seed_day(db, day_iso="2026-05-15",
              energy=500.0, energy_goal=600.0, steps=8000.0)
    _seed_day(db, day_iso="2026-05-16",
              energy=720.0, energy_goal=600.0, steps=12000.0)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))

    rows = await RESOURCE_FETCHERS["fitness.activity_summary"](
        _UdidStub(), {"date": "2026-05-16"})
    assert len(rows) == 1
    assert rows[0]["steps"] == pytest.approx(12000.0)


async def test_fetcher_date_must_be_string(monkeypatch, tmp_path):
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))

    with pytest.raises(ResourceFetchError, match="YYYY-MM-DD"):
        await RESOURCE_FETCHERS["fitness.activity_summary"](
            _UdidStub(), {"date": 20260516})


async def test_fetcher_latest_returns_one_row(
        monkeypatch, tmp_path):
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    _seed_day(db, day_iso="2026-05-15",
              energy=500.0, energy_goal=600.0, steps=8000.0)
    _seed_day(db, day_iso="2026-05-16",
              energy=720.0, energy_goal=600.0, steps=12000.0)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))

    rows = await RESOURCE_FETCHERS["fitness.activity_summary"](
        _UdidStub(), {"latest": True})
    assert len(rows) == 1
    # `latest` = newest = the 2026-05-16 row.
    assert rows[0]["start_iso"].startswith("2026-05-16")


async def test_fetcher_passes_through_value_filters(
        monkeypatch, tmp_path):
    """`_filter_records` does exact-match key/value filtering — pass
    `steps=12000.0` and only the matching day comes back. Verifies
    arbitrary selectors don't get accidentally consumed."""
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "healthdb_secure.sqlite"
    _make_test_healthdb(db)
    _seed_day(db, day_iso="2026-05-15",
              energy=500.0, energy_goal=600.0, steps=8000.0)
    _seed_day(db, day_iso="2026-05-16",
              energy=720.0, energy_goal=600.0, steps=12000.0)
    monkeypatch.setattr(sibb_state, "_healthdb_path",
                         lambda udid: str(db))

    rows = await RESOURCE_FETCHERS["fitness.activity_summary"](
        _UdidStub(), {"steps": 12000.0})
    assert len(rows) == 1
    assert rows[0]["start_iso"].startswith("2026-05-16")
