"""CalendarHandler extension — `type=calendar` (Calendar dataclass)
and the create/list/wipe round-trip on the fake reader.

Mirrors the structure of test_reminders_handler_extended.py: type
validators, dispatcher coverage, fake round-trip, edge cases.

The Swift side has its own contract tests at L2 via
sibb_probe_calendar.py (Q5) — these L1 tests guard the Python /
fake-reader half of the contract.
"""

from __future__ import annotations

import asyncio

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import Calendar, CalendarEvent, SPEC_TYPES, validate_entry
from sibb_state import CalendarHandler, HANDLERS

pytestmark = pytest.mark.fast


# ─────────────────────────── Spec registration ────────────────────────

def test_calendar_dataclass_registered():
    assert ("Calendar", "calendar") in SPEC_TYPES
    cls = SPEC_TYPES[("Calendar", "calendar")]
    assert cls is Calendar


def test_calendar_event_still_registered():
    # Regression: don't accidentally drop CalendarEvent when adding Calendar.
    assert ("Calendar", "event") in SPEC_TYPES
    assert SPEC_TYPES[("Calendar", "event")] is CalendarEvent


# ─────────────────────────── Spec validation ──────────────────────────

def test_validate_entry_calendar_with_name_passes():
    typed, err = validate_entry({"app": "Calendar", "type": "calendar",
                                  "name": "Work"})
    assert err is None
    assert isinstance(typed, Calendar)
    assert typed.name == "Work"
    assert typed.color is None


def test_validate_entry_calendar_with_color_passes():
    typed, err = validate_entry({"app": "Calendar", "type": "calendar",
                                  "name": "Work", "color": "#FF0000"})
    assert err is None
    assert typed.color == "#FF0000"


def test_validate_entry_calendar_missing_name_fails():
    typed, err = validate_entry({"app": "Calendar", "type": "calendar"})
    assert err is not None
    assert typed is None
    # error mentions the missing field
    assert "name" in err


# ─────────────────────────── Handler.apply dispatch ───────────────────

def test_apply_calendar_type_sends_create_calendar():
    reader = FakeXCUITestReader()
    handler = CalendarHandler(reader=reader)
    asyncio.run(handler.apply({"app": "Calendar", "type": "calendar",
                                 "name": "Work"}))
    rows = asyncio.run(reader._send({"type": "list_calendars"}))
    names = [c["name"] for c in rows["calendars"]]
    assert "Work" in names
    # Default 'Calendar' still present.
    assert "Calendar" in names


def test_apply_calendar_type_with_color_passes_through():
    reader = FakeXCUITestReader()
    handler = CalendarHandler(reader=reader)
    asyncio.run(handler.apply({"app": "Calendar", "type": "calendar",
                                 "name": "Personal", "color": "#00FF00"}))
    rows = asyncio.run(reader._send({"type": "list_calendars"}))
    names = [c["name"] for c in rows["calendars"]]
    assert "Personal" in names


def test_apply_unknown_type_raises():
    reader = FakeXCUITestReader()
    handler = CalendarHandler(reader=reader)
    with pytest.raises(ValueError):
        asyncio.run(handler.apply({"app": "Calendar",
                                     "type": "bogus_kind"}))


# ─────────────────────────── Handler.reset wipes calendars too ────────

def test_reset_wipes_user_calendars_but_preserves_default():
    reader = FakeXCUITestReader()
    handler = CalendarHandler(reader=reader)
    # Seed two user-created calendars.
    asyncio.run(handler.apply({"app": "Calendar", "type": "calendar",
                                 "name": "Work"}))
    asyncio.run(handler.apply({"app": "Calendar", "type": "calendar",
                                 "name": "Personal"}))
    # And an event for good measure.
    asyncio.run(handler.apply({
        "app": "Calendar", "type": "event",
        "title": "Standup",
        "start_iso": "2026-05-22T09:00:00",
        "end_iso": "2026-05-22T09:30:00",
    }))

    asyncio.run(handler.reset())

    rows = asyncio.run(reader._send({"type": "list_calendars"}))
    names = [c["name"] for c in rows["calendars"]]
    assert names == ["Calendar"]  # default survives, Work/Personal gone
    rows = asyncio.run(reader._send({"type": "list_events"}))
    assert rows["events"] == []


def test_reset_wipes_events_before_calendars():
    """If wipe_events ran AFTER wipe_calendars, deleting a non-empty
    user calendar would fail and leave residual state. Order is
    documented; this test pins it."""
    reader = FakeXCUITestReader()
    handler = CalendarHandler(reader=reader)
    asyncio.run(handler.apply({"app": "Calendar", "type": "calendar",
                                 "name": "Work"}))
    asyncio.run(handler.apply({
        "app": "Calendar", "type": "event",
        "title": "Sync", "calendar": "Work",
        "start_iso": "2026-05-22T09:00:00",
        "end_iso": "2026-05-22T09:30:00",
    }))
    asyncio.run(handler.reset())  # must not raise
    rows = asyncio.run(reader._send({"type": "list_calendars"}))
    assert [c["name"] for c in rows["calendars"]] == ["Calendar"]


# ─────────────────────────── Fake reader contract ─────────────────────

def test_fake_create_calendar_rejects_duplicate():
    reader = FakeXCUITestReader()
    asyncio.run(reader._send({"type": "create_calendar", "name": "Work"}))
    resp = asyncio.run(
        reader._send({"type": "create_calendar", "name": "Work"}))
    assert resp["ok"] is False
    assert "already exists" in resp["error"]


def test_fake_create_calendar_rejects_default_shadow():
    """Trying to create another 'Calendar' calendar fails because the
    default already exists with that name."""
    reader = FakeXCUITestReader()
    resp = asyncio.run(
        reader._send({"type": "create_calendar", "name": "Calendar"}))
    assert resp["ok"] is False
    assert "already exists" in resp["error"]


def test_fake_create_calendar_case_insensitive_duplicate():
    """Mirror Swift: comparison is case-insensitive."""
    reader = FakeXCUITestReader()
    asyncio.run(reader._send({"type": "create_calendar", "name": "Work"}))
    resp = asyncio.run(
        reader._send({"type": "create_calendar", "name": "WORK"}))
    assert resp["ok"] is False


def test_fake_create_event_in_unknown_calendar_fails():
    """Mirror Swift error 'no writable calendar available' when an
    event is created for a calendar name that doesn't exist."""
    reader = FakeXCUITestReader()
    resp = asyncio.run(reader._send({
        "type": "create_event",
        "title": "Meeting",
        "start_iso": "2026-05-22T09:00:00",
        "end_iso": "2026-05-22T10:00:00",
        "calendar": "Nonexistent",
    }))
    assert resp["ok"] is False
    assert "no writable calendar" in resp["error"]


def test_fake_create_event_in_user_calendar_succeeds():
    reader = FakeXCUITestReader()
    asyncio.run(reader._send({"type": "create_calendar", "name": "Work"}))
    resp = asyncio.run(reader._send({
        "type": "create_event",
        "title": "Standup",
        "start_iso": "2026-05-22T09:00:00",
        "end_iso": "2026-05-22T09:30:00",
        "calendar": "Work",
    }))
    assert resp["ok"] is True
    listed = asyncio.run(reader._send({"type": "list_events"}))
    assert any(e["calendar"] == "Work" for e in listed["events"])


def test_fake_list_calendars_includes_default():
    reader = FakeXCUITestReader()
    listed = asyncio.run(reader._send({"type": "list_calendars"}))
    names = [c["name"] for c in listed["calendars"]]
    assert names == ["Calendar"]


def test_fake_wipe_calendars_preserves_default():
    reader = FakeXCUITestReader()
    asyncio.run(reader._send({"type": "create_calendar", "name": "Work"}))
    asyncio.run(reader._send({"type": "create_calendar",
                                 "name": "Personal"}))
    resp = asyncio.run(reader._send({"type": "wipe_calendars"}))
    assert resp["ok"] is True
    assert resp["removed_calendars"] == 2
    listed = asyncio.run(reader._send({"type": "list_calendars"}))
    names = [c["name"] for c in listed["calendars"]]
    assert names == ["Calendar"]


# ─────────────────────────── Resource fetcher ─────────────────────────

def test_calendar_calendars_resource_registered():
    from sibb_verify import RESOURCE_FETCHERS
    assert "calendar.calendars" in RESOURCE_FETCHERS


# ─────────────────────────── Two-phase apply order ────────────────────

def test_calendar_handler_declares_apply_order_calendar_before_event():
    """Multi-calendar generators (T2.4 / T2.5) put calendar entries
    before event entries in their spec. The dispatcher honors this
    via `apply_order_by_type` so generators don't need to hand-order."""
    order = CalendarHandler.apply_order_by_type
    assert order["calendar"] < order["event"]


def test_dispatcher_sorts_calendar_entries_first():
    """End-to-end: feed apply_initial_state a spec with event BEFORE
    calendar entries (reversed). Dispatcher should apply calendar
    first; the subsequent event creation succeeds."""
    from sibb_state import apply_initial_state

    reader = FakeXCUITestReader()
    # Build a minimal fake-task with reversed spec ordering.
    class FakeTask:
        apps = ["Calendar"]
        initial_state = type("S", (), {"spec": [
            # Event references "Work" — but "Work" entry comes AFTER.
            {"app": "Calendar", "type": "event",
             "title": "Meeting",
             "calendar": "Work",
             "start_iso": "2026-05-22T09:00:00",
             "end_iso":   "2026-05-22T10:00:00"},
            {"app": "Calendar", "type": "calendar", "name": "Work"},
        ]})()

    report = asyncio.run(apply_initial_state(reader, FakeTask()))
    assert not report["errors"], (
        f"two-phase apply should have created Work calendar first "
        f"so the event could be assigned to it: {report['errors']}"
    )
    # Confirm the event ended up in Work, not in default Calendar.
    listed = asyncio.run(reader._send({"type": "list_events"}))
    rows = [e for e in listed["events"] if e["title"] == "Meeting"]
    assert len(rows) == 1
    assert rows[0]["calendar"] == "Work"
