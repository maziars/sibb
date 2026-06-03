"""A8 — L2 sim integration smoke for RemindersHandler.

Runs against a booted iOS 26.3 simulator with the SIBB XCUITest runner
already built (~/SIBBHelper/). Exercises the full reset → apply →
verify → reset → verify-absent lifecycle through real EventKit,
proving the Swift socket + Python handler + verifier stack
end-to-end. This is the test that catches Swift schema drift,
EventKit API changes, and (eventually) cross-handler reset races.

Run:
    SIBB_UDID=19B95A95-614A-4ECA-B943-44FDADFD7A9F \
        python3 -m pytest -m sim sibb/tests/integration/test_reminders_handler_sim.py -v

Skips automatically if SIBB_UDID is not set or the runner build is
missing. First run takes ~30-60s for xcodebuild launch; subsequent
runs in the same session are fast (session-scoped fixture reuses
one reader across all tests).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim


_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

# Imported lazily to avoid sys.path issues when conftest hasn't run.
from sibb_xcuitest_client import XCUITestReader  # noqa: E402

import sibb_state  # noqa: E402
import sibb_verify  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def reader(sibb_udid: str) -> AsyncIterator[XCUITestReader]:
    """Module-scoped XCUITestReader. One xcodebuild launch per file.

    Yields the reader after `start()` succeeds; tears down with
    `stop()` after all tests in the module run.
    """
    r = XCUITestReader(sibb_udid, bundle_id="com.apple.reminders")
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


async def _wipe(reader: XCUITestReader) -> None:
    resp = await reader._send({"type": "wipe_reminders"})
    assert resp.get("ok"), f"wipe_reminders failed: {resp}"


@pytest_asyncio.fixture
async def clean_reader(reader: XCUITestReader) -> AsyncIterator[XCUITestReader]:
    """Wipes Reminders state before AND after each test so tests can
    run in any order without polluting each other.
    """
    await _wipe(reader)
    yield reader
    await _wipe(reader)


# ─────────────────── Lifecycle smoke ──────────────────────────────────

async def test_reset_clears_user_lists(clean_reader: XCUITestReader):
    # Seed some state, then reset, then assert clean.
    await clean_reader._send({
        "type": "create_list", "name": "ToBeWiped",
    })
    await clean_reader._send({
        "type": "create_reminder",
        "title": "stale item", "list": "ToBeWiped",
    })

    handler = sibb_state.RemindersHandler(reader=clean_reader)
    await handler.reset()

    resp = await clean_reader._send({"type": "list_lists"})
    user_lists = [L for L in resp["lists"] if not L["immutable"]]
    assert user_lists == [], f"reset left user lists behind: {user_lists}"


async def test_apply_list_creates_real_calendar(clean_reader: XCUITestReader):
    handler = sibb_state.RemindersHandler(reader=clean_reader)

    await handler.apply({
        "app": "Reminders", "type": "list",
        "name": "SIBBIntegrationTest",
    })

    resp = await clean_reader._send({"type": "list_lists"})
    names = [L["name"] for L in resp["lists"]]
    assert "SIBBIntegrationTest" in names


async def test_apply_item_creates_real_reminder(clean_reader: XCUITestReader):
    handler = sibb_state.RemindersHandler(reader=clean_reader)
    await handler.apply({
        "app": "Reminders", "type": "list",
        "name": "SIBBIntegrationTest",
    })
    await handler.apply({
        "app": "Reminders", "type": "item",
        "list": "SIBBIntegrationTest",
        "title": "Smoke item", "priority": "high",
    })

    resp = await clean_reader._send({
        "type": "list_reminders", "list": "SIBBIntegrationTest",
    })
    titles = [r["title"] for r in resp["reminders"]]
    assert "Smoke item" in titles
    # iOS EventKit "high" -> integer priority 1
    smoke = next(r for r in resp["reminders"] if r["title"] == "Smoke item")
    assert smoke["priority"] == 1


# ────────────── Generic VERIFIERS framework against real sim ───────────

async def test_verify_framework_exists_check_against_real_state(
    clean_reader: XCUITestReader,
):
    # Drive setup via the handler (the path Phase 2b Calendar will follow).
    handler = sibb_state.RemindersHandler(reader=clean_reader)
    await handler.apply({"app": "Reminders", "type": "list",
                          "name": "VFrame"})
    await handler.apply({"app": "Reminders", "type": "item",
                          "list": "VFrame", "title": "Alpha"})
    await handler.apply({"app": "Reminders", "type": "item",
                          "list": "VFrame", "title": "Beta"})

    results = await sibb_verify.run_checks(clean_reader, [
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": "VFrame"},
         "label": "List VFrame created", "severity": "blocking"},
        {"kind": "count", "resource": "reminders.items",
         "selector": {"list": "VFrame"},
         "op": "eq", "n": 2,
         "label": "VFrame has 2 items", "severity": "blocking"},
        {"kind": "subset", "resource": "reminders.items",
         "selector": {"list": "VFrame"},
         "key": "title", "expected": ["Alpha", "Beta"],
         "label": "Alpha + Beta present", "severity": "blocking"},
    ])
    assert sibb_verify.blocking_pass(results), (
        f"unexpected verifier failure: "
        f"{[(r.label, r.status, r.evidence) for r in results]}"
    )


async def test_baseline_snapshot_catches_real_drift(
    clean_reader: XCUITestReader,
):
    # Seed, capture baseline, mutate, run identity check via the
    # real socket — proves BaselineSnapshot works end-to-end.
    await clean_reader._send({"type": "create_list", "name": "BaseA"})

    baseline = await sibb_verify.BaselineSnapshot.capture(
        clean_reader, ["reminders.lists"]
    )

    # Add a list AFTER baseline — identity should flag it.
    await clean_reader._send({"type": "create_list", "name": "AddedAfter"})

    result = await sibb_verify.run_check(clean_reader, {
        "kind": "identity", "resource": "reminders.lists",
        "label": "lists unchanged",
    }, baseline=baseline)
    assert result.status == "fail"
    assert result.evidence["current_count"] > result.evidence["baseline_count"]


# ───────────── Reminders verifier (legacy contract preserved) ─────────

async def test_legacy_verifier_passes_after_correct_setup(
    clean_reader: XCUITestReader,
):
    from sibb_verify_reminders import verify_reminders_list_task_async
    from types import SimpleNamespace

    handler = sibb_state.RemindersHandler(reader=clean_reader)
    await handler.apply({"app": "Reminders", "type": "list",
                          "name": "LegacyVerify"})
    await handler.apply({"app": "Reminders", "type": "item",
                          "list": "LegacyVerify", "title": "Item1"})
    await handler.apply({"app": "Reminders", "type": "item",
                          "list": "LegacyVerify", "title": "Item2",
                          "priority": "high"})

    task = SimpleNamespace(params={
        "list": "LegacyVerify",
        "items": ["Item1", "Item2"],
        "priority_item": "Item2",
        "priority_level": "high",
    })

    passed, checks = await verify_reminders_list_task_async(task, clean_reader)
    assert passed is True, (
        f"legacy verifier failed against good state: {checks}"
    )
