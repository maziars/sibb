"""A6 regression — `verify_reminders_list_task_async` legacy contract.

The refactor (A6) re-implements the verifier on top of `sibb_verify`
but must keep returning `(passed: bool, checks: List[Tuple[str, bool|None]])`
so `sibb_replay.py` and `sibb_episode_runner.py` keep working.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_verify_reminders import (
    _build_reminders_checks,
    verify_reminders_list_task_async,
)

pytestmark = pytest.mark.fake_reader


def _task(**params):
    return SimpleNamespace(params=params)


# ────────── _build_reminders_checks pure translation ─────────────────

def test_build_checks_minimal_list_only():
    checks = _build_reminders_checks(_task(list="L", items=[]))
    assert len(checks) == 1
    assert checks[0]["kind"] == "exists"
    assert checks[0]["resource"] == "reminders.lists"
    assert checks[0]["selector"] == {"name": "L"}


def test_build_checks_items_become_individual_exists_checks():
    checks = _build_reminders_checks(
        _task(list="L", items=["a", "b", "c"])
    )
    item_checks = [c for c in checks
                   if c.get("resource") == "reminders.items"]
    assert len(item_checks) == 3
    titles = [c["selector"]["title"] for c in item_checks]
    assert titles == ["a", "b", "c"]


def test_build_checks_priority_becomes_attribute_eq_with_ek_int():
    checks = _build_reminders_checks(_task(
        list="L", items=["x"],
        priority_item="x", priority_level="high",
    ))
    prio = [c for c in checks if c.get("kind") == "attribute_eq"]
    assert len(prio) == 1
    assert prio[0]["attr"] == "priority"
    assert prio[0]["value"] == 1   # EKReminder.priority for "high"


def test_build_checks_flag_is_informational():
    checks = _build_reminders_checks(_task(
        list="L", items=["x"], flag_item="x",
    ))
    flag = [c for c in checks if "flagged" in c.get("label", "")]
    assert len(flag) == 1
    assert flag[0]["severity"] == "informational"


# ───────────── end-to-end against FakeXCUITestReader ──────────────────

async def test_verifier_passes_after_correct_setup():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "Personal"})
    await r._send({"type": "create_reminder",
                   "title": "Buy milk", "list": "Personal"})
    await r._send({"type": "create_reminder",
                   "title": "Finish report", "list": "Personal",
                   "priority": "high"})

    task = _task(
        list="Personal",
        items=["Buy milk", "Finish report"],
        priority_item="Finish report",
        priority_level="high",
    )

    passed, checks = await verify_reminders_list_task_async(task, r)
    assert passed is True
    # Legacy shape: list of (label, status) tuples
    assert isinstance(checks, list)
    assert all(isinstance(c, tuple) and len(c) == 2 for c in checks)
    assert all(c[1] in (True, False, None) for c in checks)


async def test_verifier_fails_when_list_missing():
    r = FakeXCUITestReader()
    task = _task(list="Personal", items=[])

    passed, checks = await verify_reminders_list_task_async(task, r)
    assert passed is False
    assert any("Personal" in label and status is False
                for label, status in checks)


async def test_verifier_fails_when_item_missing():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "Personal"})

    task = _task(list="Personal", items=["Buy milk"])
    passed, checks = await verify_reminders_list_task_async(task, r)

    assert passed is False
    # "List 'Personal' created" → True, "'Buy milk' added ..." → False
    statuses = {label: ok for label, ok in checks}
    assert statuses[next(L for L in statuses if "List 'Personal'" in L)] is True
    assert statuses[next(L for L in statuses
                          if "'Buy milk' added" in L)] is False


async def test_flagged_item_is_informational_not_blocking():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "L"})
    await r._send({"type": "create_reminder", "title": "X", "list": "L"})

    task = _task(list="L", items=["X"], flag_item="X")
    passed, checks = await verify_reminders_list_task_async(task, r)

    # flag check uses informational severity → legacy_format emits None
    flag_entries = [(label, status) for label, status in checks
                     if "flagged" in label]
    assert len(flag_entries) == 1
    assert flag_entries[0][1] is None
    # Even though flag is informational, blocking checks pass
    assert passed is True
