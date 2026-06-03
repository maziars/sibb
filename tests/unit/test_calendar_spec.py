"""Phase 2b — typed spec entry for Calendar events.

Adds `CalendarEvent` to the SPEC_TYPES registry. Round-trip +
validation invariants from A5 carry over via the parametrized
`test_spec_dataclasses.py` suite; this file adds Calendar-specific
assertions (default values, optional field handling).
"""

from __future__ import annotations

import pytest

from sibb_spec import CalendarEvent, SPEC_TYPES, validate_entry

pytestmark = pytest.mark.fast


def test_calendar_event_registered():
    assert ("Calendar", "event") in SPEC_TYPES
    assert SPEC_TYPES[("Calendar", "event")] is CalendarEvent


def test_calendar_event_required_fields():
    e = CalendarEvent(title="Lunch",
                       start_iso="2026-05-15T12:00:00",
                       end_iso="2026-05-15T13:00:00")
    assert e.title == "Lunch"
    assert e.start_iso == "2026-05-15T12:00:00"
    assert e.all_day is False
    assert e.calendar is None
    assert e.location is None


def test_calendar_event_to_dict_canonical_shape():
    e = CalendarEvent(title="Lunch",
                       start_iso="2026-05-15T12:00:00",
                       end_iso="2026-05-15T13:00:00",
                       calendar="Work", all_day=False,
                       location="Cafe", notes="bring laptop")
    assert e.to_dict() == {
        "app": "Calendar", "type": "event",
        "title": "Lunch",
        "start_iso": "2026-05-15T12:00:00",
        "end_iso":   "2026-05-15T13:00:00",
        "calendar": "Work", "all_day": False,
        "location": "Cafe", "notes": "bring laptop",
        # `url` defaults None and is included in to_dict()
        # since it's a dataclass field. Added 2026-05-21 as the
        # T4 prereq.
        "url": None,
        # `recurrence` defaults None. Added 2026-05-21 as the T4b prereq.
        "recurrence": None,
    }


def test_calendar_event_round_trip():
    original = CalendarEvent(
        title="X",
        start_iso="2026-05-15T12:00:00",
        end_iso="2026-05-15T13:00:00",
        all_day=True,
    )
    d = original.to_dict()
    back = CalendarEvent.from_dict(d)
    assert back == original


def test_validate_entry_accepts_calendar_event():
    typed, err = validate_entry({
        "app": "Calendar", "type": "event",
        "title": "Lunch",
        "start_iso": "2026-05-15T12:00:00",
        "end_iso":   "2026-05-15T13:00:00",
    })
    assert err is None
    assert isinstance(typed, CalendarEvent)


def test_validate_entry_rejects_missing_start_iso():
    typed, err = validate_entry({
        "app": "Calendar", "type": "event", "title": "X",
        "end_iso": "2026-05-15T13:00:00",
    })
    assert typed is None
    assert "CalendarEvent" in err
