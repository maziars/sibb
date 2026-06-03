"""Phase 2b — L2 sim integration smoke for `CalendarHandler`.

Mirrors `test_reminders_handler_sim.py`. Plus the Reminders ×
Calendar reset-adjacency smoke that Critic #2 specifically flagged
as the test the A4 design depends on — confirms resetting one
EventKit-backed app doesn't trigger CalendarAgent shared-daemon
races that corrupt the other.

Run:
    SIBB_UDID=19B95A95-614A-4ECA-B943-44FDADFD7A9F \
        python3 -m pytest -m sim sibb/tests/integration/test_calendar_handler_sim.py -v
"""

from __future__ import annotations

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

import sibb_state  # noqa: E402
import sibb_verify  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def reader(sibb_udid: str) -> AsyncIterator[XCUITestReader]:
    r = XCUITestReader(sibb_udid, bundle_id="com.apple.reminders")
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


async def _wipe_all(reader: XCUITestReader) -> None:
    resp = await reader._send({"type": "wipe_reminders"})
    assert resp.get("ok"), f"wipe_reminders failed: {resp}"
    resp = await reader._send({"type": "wipe_events"})
    assert resp.get("ok"), f"wipe_events failed: {resp}"


@pytest_asyncio.fixture
async def clean(reader: XCUITestReader) -> AsyncIterator[XCUITestReader]:
    await _wipe_all(reader)
    yield reader
    await _wipe_all(reader)


# ─────────────────── Calendar lifecycle ──────────────────────────────

async def test_reset_clears_user_events(clean: XCUITestReader):
    # Seed via socket, reset via handler, verify clean.
    await clean._send({
        "type": "create_event",
        "title": "ToBeWiped",
        "start_iso": "2026-05-15T10:00:00",
        "end_iso":   "2026-05-15T11:00:00",
    })
    handler = sibb_state.CalendarHandler(reader=clean)
    await handler.reset()
    resp = await clean._send({"type": "list_events"})
    titles = [e["title"] for e in resp["events"]]
    assert "ToBeWiped" not in titles


async def test_apply_event_creates_real_event(clean: XCUITestReader):
    handler = sibb_state.CalendarHandler(reader=clean)
    await handler.apply({
        "app": "Calendar", "type": "event",
        "title": "SIBBIntegrationEvent",
        "start_iso": "2026-05-15T12:00:00",
        "end_iso":   "2026-05-15T13:00:00",
    })
    resp = await clean._send({
        "type": "list_events",
        "start_iso": "2026-05-15T00:00:00",
        "end_iso":   "2026-05-16T00:00:00",
    })
    titles = [e["title"] for e in resp["events"]]
    assert "SIBBIntegrationEvent" in titles


async def test_calendar_events_fetcher_against_real_state(
    clean: XCUITestReader,
):
    handler = sibb_state.CalendarHandler(reader=clean)
    await handler.apply({"app": "Calendar", "type": "event",
                          "title": "First",
                          "start_iso": "2026-06-01T10:00:00",
                          "end_iso":   "2026-06-01T11:00:00"})
    await handler.apply({"app": "Calendar", "type": "event",
                          "title": "Second",
                          "start_iso": "2026-06-02T10:00:00",
                          "end_iso":   "2026-06-02T11:00:00"})

    # Exists check via the generic VERIFIERS framework
    result = await sibb_verify.run_check(clean, {
        "kind": "exists", "resource": "calendar.events",
        "selector": {"title": "First"},
    })
    assert result.status == "pass"

    # Window-filter pushdown: only return events in the June 1 window
    result = await sibb_verify.run_check(clean, {
        "kind": "count", "resource": "calendar.events",
        "selector": {"start_iso": "2026-06-01T00:00:00",
                     "end_iso":   "2026-06-01T23:59:59"},
        "op": "eq", "n": 1,
    })
    assert result.status == "pass"


# ─────────── Reminders × Calendar reset-adjacency ─────────────────────
# THE test Critic #2 said the A4 design hinges on. Resetting one
# EventKit-backed app must not corrupt the other (no CalendarAgent
# shared-daemon race). If this ever fails, the topo-sort in
# apply_initial_state needs to add an explicit `depends_on` edge
# OR we need to serialize reset calls across handlers.

async def test_reminders_reset_preserves_calendar_state(
    clean: XCUITestReader,
):
    # Seed Calendar, then reset Reminders, then assert Calendar
    # state survived intact.
    cal_h = sibb_state.CalendarHandler(reader=clean)
    rem_h = sibb_state.RemindersHandler(reader=clean)

    await cal_h.apply({"app": "Calendar", "type": "event",
                       "title": "SurvivesReminderReset",
                       "start_iso": "2026-05-15T12:00:00",
                       "end_iso":   "2026-05-15T13:00:00"})

    baseline = await sibb_verify.BaselineSnapshot.capture(
        clean, ["calendar.events"]
    )

    await rem_h.reset()

    result = await sibb_verify.run_check(clean, {
        "kind": "identity", "resource": "calendar.events",
        "label": "calendar events survived reminder reset",
    }, baseline=baseline)
    assert result.status == "pass", (
        f"CalendarAgent shared-daemon race: Reminders.reset "
        f"corrupted calendar.events. Evidence: {result.evidence}"
    )


async def test_calendar_reset_preserves_reminders_state(
    clean: XCUITestReader,
):
    rem_h = sibb_state.RemindersHandler(reader=clean)
    cal_h = sibb_state.CalendarHandler(reader=clean)

    await rem_h.apply({"app": "Reminders", "type": "list",
                       "name": "SurvivesCalendarReset"})
    await rem_h.apply({"app": "Reminders", "type": "item",
                       "list": "SurvivesCalendarReset",
                       "title": "Item that should remain"})

    baseline = await sibb_verify.BaselineSnapshot.capture(
        clean, ["reminders.lists", "reminders.items"]
    )

    await cal_h.reset()

    for resource in ("reminders.lists", "reminders.items"):
        result = await sibb_verify.run_check(clean, {
            "kind": "identity", "resource": resource,
            "label": f"{resource} survived calendar reset",
        }, baseline=baseline)
        assert result.status == "pass", (
            f"CalendarAgent shared-daemon race: Calendar.reset "
            f"corrupted {resource}. Evidence: {result.evidence}"
        )


# ─────────────────── Multi-app pipeline ───────────────────────────────

async def test_apply_initial_state_runs_both_handlers(
    clean: XCUITestReader,
):
    from types import SimpleNamespace

    task = SimpleNamespace(
        apps=["Reminders", "Calendar"],
        initial_state=SimpleNamespace(spec=[
            {"app": "Reminders", "type": "list", "name": "MultiAppList"},
            {"app": "Reminders", "type": "item",
             "list": "MultiAppList", "title": "Reminder task"},
            {"app": "Calendar", "type": "event",
             "title": "MultiAppEvent",
             "start_iso": "2026-05-20T14:00:00",
             "end_iso":   "2026-05-20T15:00:00"},
        ]),
    )

    report = await sibb_state.apply_initial_state(clean, task)

    assert report["errors"] == [], f"unexpected errors: {report['errors']}"
    assert set(report["reset"]) == {
        "com.apple.reminders", "com.apple.mobilecal",
    }

    # Verify both apps received their state.
    lists_resp = await clean._send({"type": "list_lists"})
    list_names = [L["name"] for L in lists_resp["lists"]]
    assert "MultiAppList" in list_names

    rems_resp = await clean._send({"type": "list_reminders",
                                    "list": "MultiAppList"})
    rem_titles = [r["title"] for r in rems_resp["reminders"]]
    assert "Reminder task" in rem_titles

    events_resp = await clean._send({"type": "list_events"})
    event_titles = [e["title"] for e in events_resp["events"]]
    assert "MultiAppEvent" in event_titles
