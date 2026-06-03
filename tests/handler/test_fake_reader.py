"""Self-tests for the FakeXCUITestReader. These run in the L1.5
fake-reader layer because they exercise the fake itself — when a
handler test fails, we want to know whether it's the fake or the
handler that's wrong.
"""

from __future__ import annotations

import pytest

from fakes.fake_reader import FakeXCUITestReader

pytestmark = pytest.mark.fake_reader


async def test_wipe_then_create_then_list_reminder_round_trip():
    r = FakeXCUITestReader()

    resp = await r._send({"type": "wipe_reminders"})
    assert resp["ok"] is True

    resp = await r._send({"type": "list_lists"})
    assert resp["ok"] is True
    user_lists = [L for L in resp["lists"] if not L["immutable"]]
    assert user_lists == []

    resp = await r._send({"type": "create_list", "name": "Work"})
    assert resp["ok"] is True
    assert resp["name"] == "Work"
    assert resp["identifier"].startswith("fake-list-")

    resp = await r._send({"type": "create_reminder",
                          "title": "Finish report",
                          "list": "Work",
                          "priority": "medium"})
    assert resp["ok"] is True
    assert resp["title"] == "Finish report"
    assert resp["list"] == "Work"

    resp = await r._send({"type": "list_reminders", "list": "Work"})
    assert resp["ok"] is True
    items = resp["reminders"]
    assert len(items) == 1
    assert items[0]["title"] == "Finish report"
    # iOS EventKit medium == 5; if this drifts, contract test
    # against the live sim will tell us first.
    assert items[0]["priority"] == 5


async def test_create_reminder_into_missing_list_fails():
    r = FakeXCUITestReader()
    resp = await r._send({"type": "create_reminder",
                          "title": "X", "list": "Nope"})
    assert resp["ok"] is False
    assert "Nope" in resp["error"]


async def test_wipe_preserves_system_list_clears_user_lists():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "A"})
    await r._send({"type": "create_list", "name": "B"})
    await r._send({"type": "create_reminder", "title": "x", "list": "A"})

    resp = await r._send({"type": "wipe_reminders"})
    assert resp["ok"] is True
    assert resp["removed_reminders"] == 1
    assert resp["removed_lists"] == 2

    resp = await r._send({"type": "list_lists"})
    user = [L for L in resp["lists"] if not L["immutable"]]
    system = [L for L in resp["lists"] if L["immutable"]]
    assert user == []
    assert len(system) == 1
    assert system[0]["name"] == "Reminders"


async def test_list_reminders_filter_is_case_insensitive():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "Work"})
    await r._send({"type": "create_reminder",
                   "title": "A", "list": "Work"})
    resp = await r._send({"type": "list_reminders", "list": "WORK"})
    assert resp["ok"] is True
    assert len(resp["reminders"]) == 1


async def test_completed_reminders_excluded_unless_requested():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "L"})
    await r._send({"type": "create_reminder",
                   "title": "open", "list": "L"})
    await r._send({"type": "create_reminder", "title": "done",
                   "list": "L", "completed": True})

    resp = await r._send({"type": "list_reminders", "list": "L"})
    titles = [x["title"] for x in resp["reminders"]]
    assert titles == ["open"]

    resp = await r._send({"type": "list_reminders", "list": "L",
                          "include_completed": True})
    titles = sorted(x["title"] for x in resp["reminders"])
    assert titles == ["done", "open"]


async def test_unknown_command_returns_structured_error():
    r = FakeXCUITestReader()
    resp = await r._send({"type": "make_sandwich"})
    assert resp["ok"] is False
    assert "make_sandwich" in resp["error"]


async def test_history_records_round_trips_in_order():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "X"})
    await r._send({"type": "list_lists"})
    assert len(r.history) == 2
    assert r.history[0]["request"]["type"] == "create_list"
    assert r.history[1]["response"]["ok"] is True
    assert any(L["name"] == "X"
               for L in r.history[1]["response"]["lists"])
