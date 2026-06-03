"""Phase 2b B4 — multi-app generator end-to-end against real sim.

Generator emits `gen_reminder_with_calendar_event` → `resolve_refs`
→ `apply_initial_state` to real EventKit → simulated agent creates
the Calendar event → `run_checks` blocking_pass against live state.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim


_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

from sibb_xcuitest_client import XCUITestReader  # noqa: E402

import sibb_refs
import sibb_state
import sibb_task_generator_v3 as gen
import sibb_verify


@pytest_asyncio.fixture(scope="session")
async def reader(sibb_udid: str) -> AsyncIterator[XCUITestReader]:
    r = XCUITestReader(sibb_udid, bundle_id="com.apple.reminders")
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


@pytest_asyncio.fixture
async def clean(reader: XCUITestReader) -> AsyncIterator[XCUITestReader]:
    for cmd in ("wipe_reminders", "wipe_events"):
        resp = await reader._send({"type": cmd})
        assert resp.get("ok"), f"{cmd} failed: {resp}"
    yield reader
    for cmd in ("wipe_reminders", "wipe_events"):
        await reader._send({"type": cmd})


def _resolved_task():
    random.seed(2026)
    task = gen.gen_reminder_with_calendar_event()
    task.initial_state.spec = sibb_refs.resolve_refs(
        task.initial_state.spec)
    task.verify_checks = sibb_refs.resolve_refs(task.verify_checks)
    return task


async def test_multi_app_task_setup_then_agent_completes(
    clean: XCUITestReader,
):
    task = _resolved_task()
    report = await sibb_state.apply_initial_state(clean, task)
    assert report["errors"] == [], (
        f"apply_initial_state errors: {report['errors']}"
    )

    # Verifier-BEFORE: Reminders-side passes, Calendar-side fails.
    results_before = await sibb_verify.run_checks(
        clean, task.verify_checks)
    assert sibb_verify.blocking_pass(results_before) is False, (
        "verifier-BEFORE unexpectedly passed: task is pre-completed "
        "(footgun PHASE2_PROGRESS.md flags) — re-roll the seed."
    )

    # Simulate the agent: create the calendar event via the handler.
    cal_h = sibb_state.CalendarHandler(reader=clean)
    await cal_h.apply({
        "app": "Calendar", "type": "event",
        "title":     task.params["title"],
        "start_iso": task.params["start_iso"],
        "end_iso":   task.params["end_iso"],
    })

    # Verifier-AFTER: everything blocking passes.
    results_after = await sibb_verify.run_checks(
        clean, task.verify_checks)
    assert sibb_verify.blocking_pass(results_after) is True, (
        "verifier-AFTER did not pass: "
        f"{[(r.label, r.status, r.evidence) for r in results_after]}"
    )


async def test_multi_app_task_failure_when_agent_skips_calendar(
    clean: XCUITestReader,
):
    # Agent never creates the Calendar event → verifier-AFTER fails.
    task = _resolved_task()
    await sibb_state.apply_initial_state(clean, task)

    results = await sibb_verify.run_checks(clean, task.verify_checks)
    assert sibb_verify.blocking_pass(results) is False
    # The specific failure: calendar.events check.
    cal_results = [r for r in results
                    if "Calendar" in r.label or "calendar" in r.label]
    assert any(r.status == "fail" for r in cal_results)


async def test_multi_app_task_failure_when_agent_uses_wrong_title(
    clean: XCUITestReader,
):
    # Agent creates a calendar event but with WRONG title.
    # Verifier catches the mismatch.
    task = _resolved_task()
    await sibb_state.apply_initial_state(clean, task)

    cal_h = sibb_state.CalendarHandler(reader=clean)
    await cal_h.apply({
        "app": "Calendar", "type": "event",
        "title":     "Totally Wrong Title",
        "start_iso": task.params["start_iso"],
        "end_iso":   task.params["end_iso"],
    })

    results = await sibb_verify.run_checks(clean, task.verify_checks)
    assert sibb_verify.blocking_pass(results) is False
