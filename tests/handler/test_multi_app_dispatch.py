"""Phase 2b B4 — multi-app task end-to-end through `apply_initial_state`.

The generator emits SymbolicRef-bearing dicts. `resolve_refs` runs
between generation and dispatch, producing pure-string dicts the
dispatcher consumes. Tests verify the full pipeline against the
FakeXCUITestReader: refs resolved → handlers apply both apps →
state lands correctly.
"""

from __future__ import annotations

import random

import pytest

import sibb_refs
import sibb_state
import sibb_task_generator_v3 as gen
from fakes.fake_reader import FakeXCUITestReader

pytestmark = pytest.mark.fake_reader


def _resolved_task(seed: int):
    random.seed(seed)
    task = gen.gen_reminder_with_calendar_event()
    # Materialize refs into the structures the dispatcher consumes.
    task.initial_state.spec = sibb_refs.resolve_refs(
        task.initial_state.spec)
    task.verify_checks = sibb_refs.resolve_refs(task.verify_checks)
    return task


async def test_apply_seeds_reminders_state_for_multi_app_task():
    task = _resolved_task(seed=42)
    reader = FakeXCUITestReader()
    report = await sibb_state.apply_initial_state(reader, task)

    assert report["errors"] == [], (
        f"apply_initial_state errors: {report['errors']}"
    )
    assert set(report["reset"]) == {
        sibb_state.RemindersHandler.bundle_id,
        sibb_state.CalendarHandler.bundle_id,
    }


async def test_seeded_reminder_visible_after_apply():
    task = _resolved_task(seed=42)
    reader = FakeXCUITestReader()
    await sibb_state.apply_initial_state(reader, task)

    list_name = task.params["list"]
    title     = task.params["title"]

    resp = await reader._send({"type": "list_lists"})
    assert list_name in [L["name"] for L in resp["lists"]]

    resp = await reader._send({"type": "list_reminders",
                                "list": list_name})
    titles = [r["title"] for r in resp["reminders"]]
    assert title in titles


async def test_calendar_state_is_empty_after_apply():
    # Calendar is the agent's job — setup leaves it empty. The
    # spec emits Reminders entries only.
    task = _resolved_task(seed=42)
    reader = FakeXCUITestReader()
    await sibb_state.apply_initial_state(reader, task)

    resp = await reader._send({"type": "list_events"})
    assert resp["events"] == []


async def test_verifier_before_fails_calendar_check():
    # Before the agent acts, Reminders-side checks pass but the
    # Calendar event check fails — exactly the "pre_completed"
    #.
    import sibb_verify
    task = _resolved_task(seed=42)
    reader = FakeXCUITestReader()
    await sibb_state.apply_initial_state(reader, task)

    results = await sibb_verify.run_checks(reader, task.verify_checks)
    # Inspect per-resource pass/fail rather than label substring,
    # which would be brittle to label-string changes.
    by_resource = {}
    for r, check in zip(results, task.verify_checks):
        by_resource[check["resource"]] = r.status

    # Reminders-side passes (we seeded those).
    assert by_resource["reminders.lists"] == "pass"
    assert by_resource["reminders.items"] == "pass"
    # Calendar-side fails (agent hasn't acted).
    assert by_resource["calendar.events"] == "fail"
    assert sibb_verify.blocking_pass(results) is False


async def test_verifier_after_simulated_agent_action_passes():
    # Simulate the "agent did the task" path by manually creating
    # the calendar event through the handler. The whole verifier
    # then passes.
    import sibb_verify
    task = _resolved_task(seed=42)
    reader = FakeXCUITestReader()
    await sibb_state.apply_initial_state(reader, task)

    title = task.params["title"]
    start = task.params["start_iso"]
    end   = task.params["end_iso"]
    cal_h = sibb_state.CalendarHandler(reader=reader)
    await cal_h.apply({"app": "Calendar", "type": "event",
                       "title": title, "start_iso": start,
                       "end_iso": end})

    results = await sibb_verify.run_checks(reader, task.verify_checks)
    assert sibb_verify.blocking_pass(results) is True, (
        "post-action blocking checks did not all pass: "
        f"{[(r.label, r.status) for r in results]}"
    )
