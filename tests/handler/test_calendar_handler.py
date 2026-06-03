"""Phase 2b — `CalendarHandler` against `FakeXCUITestReader`.

Mirrors `test_reminders_handler.py`. Same protocol contract: reset
wipes; apply realizes one typed spec entry; failure modes raise.
"""

from __future__ import annotations

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_state import CalendarHandler

pytestmark = pytest.mark.fake_reader


# ─────────────────── reset / apply happy path ─────────────────────────

async def test_reset_clears_events():
    reader = FakeXCUITestReader()
    await reader._send({"type": "create_event",
                        "title": "Stale", "start_iso": "2026-05-15T10:00:00",
                        "end_iso": "2026-05-15T11:00:00"})

    h = CalendarHandler(reader=reader)
    await h.reset()

    resp = await reader._send({"type": "list_events"})
    assert resp["events"] == []


async def test_apply_event_creates_event():
    reader = FakeXCUITestReader()
    h = CalendarHandler(reader=reader)

    await h.apply({
        "app": "Calendar", "type": "event",
        "title": "Lunch with Sam",
        "start_iso": "2026-05-15T12:00:00",
        "end_iso":   "2026-05-15T13:00:00",
    })

    resp = await reader._send({"type": "list_events"})
    assert len(resp["events"]) == 1
    assert resp["events"][0]["title"] == "Lunch with Sam"


async def test_apply_event_propagates_optional_fields():
    reader = FakeXCUITestReader()
    h = CalendarHandler(reader=reader)

    await h.apply({
        "app": "Calendar", "type": "event",
        "title": "All-day off",
        "start_iso": "2026-06-01T00:00:00",
        "end_iso":   "2026-06-02T00:00:00",
        "all_day": True,
        "location": "Beach",
        "notes": "Out of office",
    })

    resp = await reader._send({"type": "list_events"})
    ev = resp["events"][0]
    assert ev["all_day"] is True
    assert ev["location"] == "Beach"
    assert ev["notes"] == "Out of office"


# ─────────────────── error paths ──────────────────────────────────────

async def test_apply_unknown_type_raises_valueerror():
    h = CalendarHandler(reader=FakeXCUITestReader())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"app": "Calendar", "type": "spaceship"})


async def test_apply_missing_required_field_raises_runtimeerror():
    # Empty strings flow through Python to Swift (the handler trusts
    # validated input) and Swift's required-field check surfaces as
    # `RuntimeError(CalendarHandler.event failed: ... required)`.
    # Catches malformed inputs that slipped past validate_entry.
    h = CalendarHandler(reader=FakeXCUITestReader())
    with pytest.raises(RuntimeError, match="required"):
        await h.apply({"app": "Calendar", "type": "event",
                       "title": "broken",
                       "start_iso": "", "end_iso": ""})


# ─────────────────── class-attr invariants ────────────────────────────

def test_calendar_handler_metadata():
    assert CalendarHandler.bundle_id == "com.apple.mobilecal"
    assert CalendarHandler.tcc_services == ["calendar"]
    assert CalendarHandler.pre_runner is False
    assert CalendarHandler.pre_runner_kinds == []
    assert CalendarHandler.depends_on == []


def test_calendar_handler_registered():
    from sibb_state import HANDLERS, canonicalize_app
    assert canonicalize_app("Calendar") == "com.apple.mobilecal"
    assert HANDLERS["com.apple.mobilecal"] is CalendarHandler
