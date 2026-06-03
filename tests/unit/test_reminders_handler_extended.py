"""Reminders handler — due_iso / notes / url pass-through.

Phase-B prerequisite A: extends the Reminders apply surface so reporting
tasks can ask "what's due tomorrow", "what reminder has note containing
X", etc. The Swift command (create_reminder) and the fetcher
(reminders.items) are both authoritative for the new fields; this test
walks the round-trip end-to-end against the FakeXCUITestReader (no sim
required) so a Swift contract drift is caught at L1.5.
"""

from __future__ import annotations

import asyncio

import pytest

from sibb_state import RemindersHandler
from sibb_verify import RESOURCE_FETCHERS
from fakes.fake_reader import FakeXCUITestReader

pytestmark = pytest.mark.fast


def _setup_handler() -> tuple:
    reader = FakeXCUITestReader()
    # System list exists by default in the fake; we add one user list.
    asyncio.run(reader._send({"type": "create_list", "name": "Work"}))
    h = RemindersHandler(reader=reader)
    return h, reader


def test_apply_passes_due_iso_to_swift():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Pay rent",
        "due_iso": "2026-05-20T09:00:00",
    }))
    # The fake records the create_reminder command verbatim.
    sent = [h_ for h_ in reader.history
             if h_["request"].get("type") == "create_reminder"][-1]["request"]
    assert sent["due_iso"] == "2026-05-20T09:00:00"
    assert "notes" not in sent
    assert "url" not in sent


def test_apply_passes_notes_to_swift():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Call mom",
        "notes": "Wish happy birthday and ask about the trip",
    }))
    sent = [h_ for h_ in reader.history
             if h_["request"].get("type") == "create_reminder"][-1]["request"]
    assert sent["notes"] == "Wish happy birthday and ask about the trip"


def test_apply_passes_url_to_swift():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Read Anthropic blog",
        "url": "https://www.anthropic.com/news",
    }))
    sent = [h_ for h_ in reader.history
             if h_["request"].get("type") == "create_reminder"][-1]["request"]
    assert sent["url"] == "https://www.anthropic.com/news"


def test_apply_omits_optional_fields_when_none():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Plain"}))
    sent = [h_ for h_ in reader.history
             if h_["request"].get("type") == "create_reminder"][-1]["request"]
    assert "due_iso" not in sent
    assert "notes" not in sent
    assert "url" not in sent


def test_apply_passes_all_three_together():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Plan vacation",
        "due_iso": "2026-06-01T18:00:00",
        "notes": "Look at flights to Tokyo",
        "url": "https://flights.example.com/tokyo",
    }))
    sent = [h_ for h_ in reader.history
             if h_["request"].get("type") == "create_reminder"][-1]["request"]
    assert sent["due_iso"] == "2026-06-01T18:00:00"
    assert sent["notes"] == "Look at flights to Tokyo"
    assert sent["url"] == "https://flights.example.com/tokyo"


# ────────────────── fetcher round-trip via reminders.items ─────────────

def test_fetcher_surfaces_due_field():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Pay rent",
        "due_iso": "2026-05-20T09:00:00",
    }))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert len(rows) == 1
    assert rows[0]["due"] == "2026-05-20T09:00:00"


def test_fetcher_omits_due_when_not_set():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Plain"}))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert "due" not in rows[0]


def test_fetcher_surfaces_notes_and_url():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Call",
        "notes": "ABC", "url": "https://x.test/",
    }))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert rows[0]["notes"] == "ABC"
    assert rows[0]["url"] == "https://x.test/"


# ─────────────────────── recurrence round-trip ────────────────────────

def test_apply_passes_recurrence_to_swift():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Standup",
        "recurrence": {"frequency": "weekly", "interval": 1},
    }))
    sent = [h_ for h_ in reader.history
             if h_["request"].get("type") == "create_reminder"][-1]["request"]
    assert sent["recurrence"] == {"frequency": "weekly", "interval": 1}


def test_apply_omits_recurrence_when_absent():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "One-off"}))
    sent = [h_ for h_ in reader.history
             if h_["request"].get("type") == "create_reminder"][-1]["request"]
    assert "recurrence" not in sent


def test_fetcher_surfaces_recurrence_field():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Standup",
        "due_iso": "2026-05-21T09:00:00",
        "recurrence": {"frequency": "weekly", "interval": 1},
    }))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert rows[0]["recurrence"] == {"frequency": "weekly", "interval": 1}


def test_fetcher_omits_recurrence_when_not_set():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "One-off"}))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert "recurrence" not in rows[0]


def test_fetcher_surfaces_recurrence_with_end_iso():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Monthly review",
        "due_iso": "2026-06-01",
        "recurrence": {"frequency": "monthly", "interval": 1,
                        "end_iso": "2027-01-01"},
    }))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert rows[0]["recurrence"]["frequency"] == "monthly"
    assert rows[0]["recurrence"]["interval"] == 1
    # Date-only end_iso normalizes to end-of-day local — see the
    # validation tests for the rationale.
    assert rows[0]["recurrence"]["end_iso"] == "2027-01-01T23:59:59"


def test_fetcher_surfaces_recurrence_with_end_count():
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Workout",
        "due_iso": "2026-06-01T07:00:00",
        "recurrence": {"frequency": "daily", "interval": 2,
                        "end_count": 30},
    }))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert rows[0]["recurrence"]["frequency"] == "daily"
    assert rows[0]["recurrence"]["interval"] == 2
    assert rows[0]["recurrence"]["end_count"] == 30


# ─────── recurrence validation mirroring real Swift+EventKit ─────────

def test_recurrence_without_due_is_silently_dropped():
    # EventKit drops the rule on save without dueDateComponents; the
    # fake mirrors that — the row simply has no `recurrence` key.
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "No due",
        "recurrence": {"frequency": "weekly"},
    }))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert "recurrence" not in rows[0]


def test_recurrence_frequency_is_lowercased():
    # Swift's create_reminder calls .lowercased() on the frequency
    # string. The fake matches.
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Standup",
        "due_iso": "2026-05-21T09:00:00",
        "recurrence": {"frequency": "Weekly", "interval": 1},
    }))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert rows[0]["recurrence"]["frequency"] == "weekly"


def test_recurrence_date_only_end_iso_normalizes_to_end_of_day():
    # EKRecurrenceEnd stores a point-in-time Date; UNTIL is RFC-5545
    # inclusive. Date-only end_iso parses as 23:59:59 local.
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "Monthly review",
        "due_iso": "2026-06-01",
        "recurrence": {"frequency": "monthly", "interval": 1,
                        "end_iso": "2027-01-01"},
    }))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(reader, {}))
    assert rows[0]["recurrence"]["end_iso"] == "2027-01-01T23:59:59"


def test_recurrence_end_iso_xor_end_count_validated():
    h, reader = _setup_handler()
    resp = asyncio.run(reader._send({
        "type": "create_reminder",
        "list": "Work", "title": "Bad",
        "due_iso": "2026-06-01",
        "recurrence": {"frequency": "daily", "interval": 1,
                        "end_iso": "2027-01-01", "end_count": 10},
    }))
    assert resp["ok"] is False
    assert "mutually exclusive" in resp["error"]


def test_recurrence_interval_zero_rejected():
    h, reader = _setup_handler()
    resp = asyncio.run(reader._send({
        "type": "create_reminder",
        "list": "Work", "title": "Bad",
        "due_iso": "2026-06-01",
        "recurrence": {"frequency": "daily", "interval": 0},
    }))
    assert resp["ok"] is False
    assert ">= 1" in resp["error"]


def test_recurrence_unknown_frequency_rejected():
    h, reader = _setup_handler()
    resp = asyncio.run(reader._send({
        "type": "create_reminder",
        "list": "Work", "title": "Bad",
        "due_iso": "2026-06-01",
        "recurrence": {"frequency": "biweekly"},
    }))
    assert resp["ok"] is False
    assert "daily/weekly/monthly/yearly" in resp["error"]


def test_selector_filters_on_due():
    # Exact-string match (case-insensitive) — the same filter rule
    # used by other selector fields. Range filters ("due before X")
    # aren't supported by the verifier today; phase-2.x can add a
    # custom check kind if a task needs them.
    h, reader = _setup_handler()
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "A",
        "due_iso": "2026-05-20T09:00:00"}))
    asyncio.run(h.apply({
        "type": "item", "list": "Work", "title": "B",
        "due_iso": "2026-05-21T09:00:00"}))
    fetcher = RESOURCE_FETCHERS["reminders.items"]
    rows = asyncio.run(fetcher(
        reader, {"due": "2026-05-20T09:00:00"}))
    assert [r["title"] for r in rows] == ["A"]
