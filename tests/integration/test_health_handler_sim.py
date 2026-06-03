"""HealthHandler — L2 sim integration.

ALL TESTS IN THIS FILE ARE CURRENTLY SKIPPED.

Why: HealthKit on iOS simulator is partially supported by Apple
and we couldn't drive `HKHealthStore.requestAuthorization` through
its consent UI without writing new XCUITest infrastructure that
targets the test-runner app itself (the consent sheet appears
INSIDE the runner, not on SpringBoard, so our existing
`dismissPermissionDialogs` doesn't see it). Apple's own
documentation: "The simulator has no Health data and you should
always test on a real iPhone." See:
- https://developer.apple.com/documentation/healthkit/authorizing-access-to-health-data
- https://github.com/wix/AppleSimulatorUtils/issues/26
- `sibb/docs/IOS_SIM_QUIRKS.md` §10

The handler code, spec, fetcher, and 30/30 L1+L1.5 tests in
`test_health_handler.py` ARE all complete and pass — only the
real-sim integration is blocked. When iOS sim ships proper
HealthKit support (or we implement runner-side consent-UI
tap-through), removing `pytest.skip` here exercises the full
flow against the live store.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.sim,
    pytest.mark.skip(
        reason="HealthKit on iOS simulator requires in-app consent "
        "UI that our SpringBoard-targeted dismissal can't reach. "
        "See sibb/docs/IOS_SIM_QUIRKS.md §10 + module docstring."
    ),
]

_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
for p in (_SIM_DIR, _BENCHMARK_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sibb_scaffold import AXReader  # noqa: E402
from sibb_state import HealthHandler  # noqa: E402


@pytest_asyncio.fixture(scope="module")
async def reader(sibb_udid: str) -> AsyncIterator[AXReader]:
    r = AXReader(sibb_udid)
    await r.start(bundle_id="com.apple.springboard")
    try:
        await r._xcuitest._send({"type": "wipe_health_samples"})
        yield r
    finally:
        try:
            await r._xcuitest._send({"type": "wipe_health_samples"})
        except Exception:
            pass
        await r.stop()


# ────────────────────── Swift command shapes ─────────────────────

async def test_list_health_samples_empty_on_clean_baseline(reader):
    """Fresh clone has no samples written by our runner. HealthKit
    may have system-injected sample data (steps from "device
    motion") but only OUR runner's samples appear here because
    list_health_samples queries via our store reference — system
    samples have a different source."""
    resp = await reader._xcuitest._send({"type": "list_health_samples"})
    assert resp.get("ok") is True
    # Allow empty OR any pre-existing system samples we don't control.
    # Just confirm the response shape works.
    assert "samples" in resp


async def test_create_step_count_sample_round_trip(reader):
    await reader._xcuitest._send({"type": "wipe_health_samples"})
    resp = await reader._xcuitest._send({
        "type": "create_health_sample",
        "sample_type": "step_count",
        "value": 5000,
        "start_iso": "2026-05-16T08:00:00Z",
        "end_iso":   "2026-05-16T08:10:00Z",
    })
    assert resp.get("ok") is True, f"create failed: {resp}"
    assert resp.get("sample_type") == "step_count"
    assert resp.get("value") == 5000.0
    assert resp.get("unit") == "count"
    assert resp.get("identifier")

    resp = await reader._xcuitest._send({
        "type": "list_health_samples", "sample_type": "step_count"})
    assert resp.get("ok") is True
    ours = [s for s in resp.get("samples", [])
             if s["source"] == "com.sibb.tests.xctrunner"]
    assert len(ours) == 1
    assert ours[0]["value"] == 5000.0


async def test_create_heart_rate_sample_round_trip(reader):
    """Different unit (count/min) — verifies the HKUnit string
    parsing on the Swift side."""
    await reader._xcuitest._send({"type": "wipe_health_samples"})
    resp = await reader._xcuitest._send({
        "type": "create_health_sample",
        "sample_type": "heart_rate",
        "value": 72,
        "start_iso": "2026-05-16T08:00:00Z",
    })
    assert resp.get("ok") is True
    assert resp.get("unit") == "count/min"


async def test_create_body_mass_sample_round_trip(reader):
    """Body mass is the canonical 'instantaneous' sample —
    start_iso == end_iso (Swift defaults end to start if absent)."""
    await reader._xcuitest._send({"type": "wipe_health_samples"})
    resp = await reader._xcuitest._send({
        "type": "create_health_sample",
        "sample_type": "body_mass",
        "value": 70.5,
        "start_iso": "2026-05-16T07:00:00Z",
        # No end_iso — should default to start_iso.
    })
    assert resp.get("ok") is True

    resp = await reader._xcuitest._send({
        "type": "list_health_samples", "sample_type": "body_mass"})
    ours = [s for s in resp.get("samples", [])
             if s["source"] == "com.sibb.tests.xctrunner"]
    assert len(ours) == 1
    assert ours[0]["value"] == 70.5
    assert ours[0]["start_iso"] == ours[0]["end_iso"]


async def test_create_rejects_unknown_sample_type(reader):
    resp = await reader._xcuitest._send({
        "type": "create_health_sample",
        "sample_type": "blood_pressure",
        "value": 120,
        "start_iso": "2026-05-16T08:00:00Z",
    })
    assert resp.get("ok") is False
    assert "unknown sample_type" in resp.get("error", "")


async def test_wipe_health_samples_clears_runner_data(reader):
    """Seed multiple sample types, wipe, confirm our runner's
    samples are gone. System samples (if any) remain."""
    await reader._xcuitest._send({"type": "wipe_health_samples"})
    for kind, val in [("step_count", 100), ("heart_rate", 60),
                        ("body_mass", 70)]:
        await reader._xcuitest._send({
            "type": "create_health_sample",
            "sample_type": kind, "value": val,
            "start_iso": "2026-05-16T08:00:00Z",
        })
    resp = await reader._xcuitest._send({"type": "list_health_samples"})
    ours_before = [s for s in resp.get("samples", [])
                    if s["source"] == "com.sibb.tests.xctrunner"]
    assert len(ours_before) == 3

    resp = await reader._xcuitest._send({"type": "wipe_health_samples"})
    assert resp.get("ok") is True, f"wipe failed: {resp}"
    assert resp.get("removed_samples", 0) >= 3

    resp = await reader._xcuitest._send({"type": "list_health_samples"})
    ours_after = [s for s in resp.get("samples", [])
                   if s["source"] == "com.sibb.tests.xctrunner"]
    assert ours_after == []


async def test_list_pushes_time_window_predicate(reader):
    """NSPredicate-based time window narrows the underlying query.
    The shape Python sends must round-trip through Swift's
    HKQuery.predicateForSamples correctly."""
    await reader._xcuitest._send({"type": "wipe_health_samples"})
    for hour in (8, 10, 12):
        await reader._xcuitest._send({
            "type": "create_health_sample",
            "sample_type": "step_count", "value": 1000,
            "start_iso": f"2026-05-16T{hour:02d}:00:00Z",
            "end_iso":   f"2026-05-16T{hour:02d}:10:00Z",
        })
    resp = await reader._xcuitest._send({
        "type": "list_health_samples",
        "start_iso": "2026-05-16T09:00:00Z",
        "end_iso":   "2026-05-16T11:00:00Z",
    })
    ours = [s for s in resp.get("samples", [])
             if s["source"] == "com.sibb.tests.xctrunner"]
    assert len(ours) == 1
    assert ours[0]["start_iso"].startswith("2026-05-16T10")


# ────────────────────── HealthHandler integration ────────────────

async def test_handler_apply_then_fetcher_round_trip(reader):
    from sibb_verify import RESOURCE_FETCHERS

    await reader._xcuitest._send({"type": "wipe_health_samples"})
    h = HealthHandler(reader=reader._xcuitest)
    await h.apply({"type": "sample",
                    "sample_type": "step_count",
                    "value": 8500,
                    "start_iso": "2026-05-16T07:00:00Z",
                    "end_iso":   "2026-05-16T19:00:00Z"})

    fetcher = RESOURCE_FETCHERS["health.samples"]
    rows = await fetcher(reader._xcuitest, {"sample_type": "step_count"})
    ours = [r for r in rows
             if r["source"] == "com.sibb.tests.xctrunner"]
    assert len(ours) == 1
    assert ours[0]["value"] == 8500.0


async def test_handler_reset_clears_via_handler_api(reader):
    h = HealthHandler(reader=reader._xcuitest)
    await h.apply({"type": "sample", "sample_type": "step_count",
                    "value": 1000,
                    "start_iso": "2026-05-16T08:00:00Z"})
    await h.reset()
    resp = await reader._xcuitest._send({"type": "list_health_samples"})
    ours = [s for s in resp.get("samples", [])
             if s["source"] == "com.sibb.tests.xctrunner"]
    assert ours == []
