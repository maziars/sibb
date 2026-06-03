"""Phase 2b — calendar.events resource fetcher + check kinds.

Through the existing generic VERIFIERS framework: no new check
kinds; just the `calendar.events` fetcher registered alongside
`reminders.lists` / `reminders.items`.
"""

from __future__ import annotations

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_state import CalendarHandler
from sibb_verify import RESOURCE_FETCHERS, run_check

pytestmark = pytest.mark.fake_reader


async def _seeded():
    r = FakeXCUITestReader()
    h = CalendarHandler(reader=r)
    await h.apply({"app": "Calendar", "type": "event",
                   "title": "Lunch with Sam",
                   "start_iso": "2026-05-15T12:00:00",
                   "end_iso":   "2026-05-15T13:00:00"})
    await h.apply({"app": "Calendar", "type": "event",
                   "title": "Dentist",
                   "start_iso": "2026-05-16T14:00:00",
                   "end_iso":   "2026-05-16T15:00:00"})
    return r


def test_calendar_events_fetcher_registered():
    assert "calendar.events" in RESOURCE_FETCHERS


async def test_exists_check_calendar_event_by_title():
    r = await _seeded()
    result = await run_check(r, {
        "kind": "exists", "resource": "calendar.events",
        "selector": {"title": "Lunch with Sam"},
    })
    assert result.status == "pass"
    assert result.evidence["count"] == 1


async def test_exists_check_is_case_insensitive():
    r = await _seeded()
    result = await run_check(r, {
        "kind": "exists", "resource": "calendar.events",
        "selector": {"title": "DENTIST"},
    })
    assert result.status == "pass"


async def test_count_check_calendar_events():
    r = await _seeded()
    result = await run_check(r, {
        "kind": "count", "resource": "calendar.events",
        "selector": {},
        "op": "eq", "n": 2,
    })
    assert result.status == "pass"


async def test_absent_check_passes_when_no_match():
    r = await _seeded()
    result = await run_check(r, {
        "kind": "absent", "resource": "calendar.events",
        "selector": {"title": "Imaginary"},
    })
    assert result.status == "pass"


async def test_calendar_events_window_filter_pushdown():
    r = await _seeded()
    # Window covers only the May 15 event.
    result = await run_check(r, {
        "kind": "count", "resource": "calendar.events",
        "selector": {"start_iso": "2026-05-15T00:00:00",
                     "end_iso":   "2026-05-15T23:59:59"},
        "op": "eq", "n": 1,
    })
    assert result.status == "pass"
