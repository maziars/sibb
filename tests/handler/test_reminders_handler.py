"""A1 — RemindersHandler behavior against the in-memory fake reader.

Exercises the async `reset` / `apply` contract end-to-end through
the same JSON command shapes the real Swift server emits. Drift in
either side surfaces here first.
"""

from __future__ import annotations

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_state import RemindersHandler

pytestmark = pytest.mark.fake_reader


async def test_reset_wipes_user_lists_and_reminders():
    reader = FakeXCUITestReader()
    await reader._send({"type": "create_list", "name": "Old"})
    await reader._send({"type": "create_reminder",
                        "title": "stale", "list": "Old"})

    h = RemindersHandler(reader=reader)
    await h.reset()

    resp = await reader._send({"type": "list_lists"})
    user = [L for L in resp["lists"] if not L["immutable"]]
    assert user == []
    resp = await reader._send({"type": "list_reminders"})
    assert resp["reminders"] == []


async def test_apply_list_creates_user_list():
    reader = FakeXCUITestReader()
    h = RemindersHandler(reader=reader)

    await h.apply({"app": "Reminders", "type": "list", "name": "Work"})

    resp = await reader._send({"type": "list_lists"})
    names = [L["name"] for L in resp["lists"]]
    assert "Work" in names


async def test_apply_item_creates_reminder_in_list():
    reader = FakeXCUITestReader()
    h = RemindersHandler(reader=reader)

    await h.apply({"app": "Reminders", "type": "list", "name": "Work"})
    await h.apply({"app": "Reminders", "type": "item",
                   "list": "Work", "title": "Finish report",
                   "priority": "high"})

    resp = await reader._send({"type": "list_reminders", "list": "Work"})
    assert len(resp["reminders"]) == 1
    assert resp["reminders"][0]["title"] == "Finish report"
    assert resp["reminders"][0]["priority"] == 1   # EventKit "high"


async def test_apply_unknown_type_raises_valueerror():
    h = RemindersHandler(reader=FakeXCUITestReader())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"app": "Reminders", "type": "spaceship"})


async def test_apply_item_into_missing_list_raises_runtimeerror():
    h = RemindersHandler(reader=FakeXCUITestReader())
    with pytest.raises(RuntimeError, match="not found"):
        await h.apply({"app": "Reminders", "type": "item",
                       "list": "Nope", "title": "X"})


async def test_apply_completed_flag_is_propagated():
    reader = FakeXCUITestReader()
    h = RemindersHandler(reader=reader)
    await h.apply({"app": "Reminders", "type": "list", "name": "L"})
    await h.apply({"app": "Reminders", "type": "item",
                   "list": "L", "title": "done", "completed": True})

    resp = await reader._send({"type": "list_reminders", "list": "L",
                               "include_completed": True})
    assert resp["reminders"][0]["completed"] is True


async def test_handler_metadata_drives_no_runtime_state():
    # Class attrs are class-level — instantiation must not perturb them.
    a = RemindersHandler(reader=None)
    b = RemindersHandler(reader=FakeXCUITestReader())
    assert RemindersHandler.bundle_id == "com.apple.reminders"
    assert RemindersHandler.tcc_services == ["reminders"]
    assert RemindersHandler.pre_runner is False
    assert RemindersHandler.depends_on == []
    assert a.bundle_id == b.bundle_id
