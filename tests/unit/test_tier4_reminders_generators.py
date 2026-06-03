"""Tier 4 Reminders generators — due-date / notes / mixed-state tasks.

For each generator: spec validates, BEFORE fails, AFTER passes on the
right action, and the "no irrelevant edits" guards block at least one
representative cheat path. Pure-Python tests using FakeXCUITestReader.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import validate_spec
from sibb_state import apply_initial_state
from sibb_task_generator_v3 import (
    gen_set_due_date_on_reminder,
    gen_change_due_date,
    gen_complete_all_overdue,
    gen_add_notes_to_reminder,
    gen_clear_completed_only,
    _resolve_due,
    _past_iso,
)
from sibb_verify import run_checks, blocking_pass

pytestmark = pytest.mark.fast


def _verify(reader, task):
    results = asyncio.run(run_checks(reader, task.verify_checks))
    return blocking_pass(results), results


def _seed_initial_state(reader, task):
    report = asyncio.run(apply_initial_state(reader, task))
    assert not report.get("errors"), \
        f"state setup failed: {report['errors']}"
    return report


# ────────────────────────── _resolve_due helper ─────────────────────

def test_resolve_due_returns_iso_and_phrasing():
    random.seed(0)
    iso, phr = _resolve_due()
    assert iso and phr
    # ISO is either YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, no trailing Z.
    assert not iso.endswith("Z")
    if "T" in iso:
        # date+time form
        date_part, time_part = iso.split("T")
        assert len(date_part) == 10  # YYYY-MM-DD
        assert len(time_part) == 8   # HH:MM:SS
    else:
        assert len(iso) == 10


def test_resolve_due_mixes_date_only_and_date_with_time():
    # Across 100 calls, both forms should appear (50/50 coin flip).
    random.seed(42)
    forms = {"date_only": 0, "date_time": 0}
    for _ in range(100):
        iso, _ = _resolve_due()
        forms["date_time" if "T" in iso else "date_only"] += 1
    assert forms["date_only"] > 10 and forms["date_time"] > 10


# ───────────────────── gen_set_due_date_on_reminder ──────────────────

def test_set_due_date_spec_validates():
    random.seed(1)
    t = gen_set_due_date_on_reminder()
    assert validate_spec(t.initial_state.spec) == []
    assert all(c["severity"] == "blocking" for c in t.verify_checks)
    assert t.verify_checks[0]["attr"] == "due"


def test_set_due_date_before_fails_after_passes():
    random.seed(1)
    t = gen_set_due_date_on_reminder()
    target = t.params["target"]; list_name = t.params["list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["due"] = t.params["due_iso"]
            break

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_set_due_date_adding_due_to_wrong_item_fails():
    random.seed(2)
    t = gen_set_due_date_on_reminder()
    target = t.params["target"]; list_name = t.params["list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Agent picks the wrong item.
    wrong = next(r for r in reader._reminders
                   if r["list"] == list_name and r["title"] != target)
    wrong["due"] = t.params["due_iso"]
    passed, _ = _verify(reader, t)
    assert passed is False


def test_set_due_date_attaching_notes_side_effect_fails():
    random.seed(3)
    t = gen_set_due_date_on_reminder()
    target = t.params["target"]; list_name = t.params["list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["due"] = t.params["due_iso"]
            r["notes"] = "side-effect text"
            break
    passed, _ = _verify(reader, t)
    assert passed is False


# ─────────────────────── gen_change_due_date ─────────────────────────

def test_change_due_date_spec_seeds_target_with_old_iso():
    random.seed(10)
    t = gen_change_due_date()
    target = t.params["target"]
    old_iso = t.params["old_iso"]
    seeded = {e["title"]: e for e in t.initial_state.spec
               if e["type"] == "item"}
    assert seeded[target].get("due_iso") == old_iso
    # Siblings don't get a due.
    for title, entry in seeded.items():
        if title != target:
            assert "due_iso" not in entry


def test_change_due_date_after_passes_when_target_moves():
    random.seed(10)
    t = gen_change_due_date()
    target = t.params["target"]; list_name = t.params["list"]
    new_iso = t.params["new_iso"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False   # target still on old_iso

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["due"] = new_iso
            break

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_change_due_date_creating_duplicate_with_new_date_fails():
    # Agent adds a new item with the new date instead of moving.
    random.seed(11)
    t = gen_change_due_date()
    list_name = t.params["list"]; new_iso = t.params["new_iso"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    asyncio.run(reader._send({
        "type": "create_reminder",
        "list": list_name, "title": "Duplicate", "due_iso": new_iso,
    }))
    passed, _ = _verify(reader, t)
    assert passed is False


# ─────────────────────── gen_complete_all_overdue ────────────────────

def test_complete_all_overdue_spec_validates():
    random.seed(20)
    t = gen_complete_all_overdue()
    assert validate_spec(t.initial_state.spec) == []
    assert len(t.params["overdue"]) == 2
    assert len(t.params["non_overdue"]) == 3


def test_complete_all_overdue_before_fails_after_passes():
    random.seed(20)
    t = gen_complete_all_overdue()
    list_name = t.params["list"]
    overdue = set(t.params["overdue"])
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] in overdue:
            r["completed"] = True

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_complete_all_overdue_completing_a_non_overdue_fails():
    random.seed(21)
    t = gen_complete_all_overdue()
    list_name = t.params["list"]
    overdue = set(t.params["overdue"])
    non_overdue = set(t.params["non_overdue"])
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Complete the overdue ones (correct) PLUS one non-overdue (wrong).
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] in overdue:
            r["completed"] = True
    one_non_overdue = next(iter(non_overdue))
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == one_non_overdue:
            r["completed"] = True
            break
    passed, _ = _verify(reader, t)
    assert passed is False


# ───────────────────── gen_add_notes_to_reminder ─────────────────────

def test_add_notes_spec_validates():
    random.seed(30)
    t = gen_add_notes_to_reminder()
    assert validate_spec(t.initial_state.spec) == []
    assert t.params["note"]


def test_add_notes_before_fails_after_passes():
    random.seed(30)
    t = gen_add_notes_to_reminder()
    target = t.params["target"]; list_name = t.params["list"]
    note = t.params["note"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["notes"] = note
            break

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_add_notes_wrong_text_fails():
    random.seed(31)
    t = gen_add_notes_to_reminder()
    target = t.params["target"]; list_name = t.params["list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["notes"] = "different text the agent wrote"
            break
    passed, _ = _verify(reader, t)
    assert passed is False


# ─────────────────────── gen_clear_completed_only ────────────────────

def test_clear_completed_spec_validates():
    random.seed(40)
    t = gen_clear_completed_only()
    assert validate_spec(t.initial_state.spec) == []
    assert len(t.params["completed"]) == 3
    assert len(t.params["remaining"]) == 2


def test_clear_completed_before_fails_after_passes():
    random.seed(40)
    t = gen_clear_completed_only()
    list_name = t.params["list"]
    completed = set(t.params["completed"])
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    reader._reminders = [
        r for r in reader._reminders
        if not (r["list"] == list_name and r["title"] in completed)
    ]

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_clear_completed_deleting_a_non_completed_item_fails():
    random.seed(41)
    t = gen_clear_completed_only()
    list_name = t.params["list"]
    completed = set(t.params["completed"])
    remaining = set(t.params["remaining"])
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Delete the completed ones + one of the unfinished ones (wrong).
    one_unfinished = next(iter(remaining))
    reader._reminders = [
        r for r in reader._reminders
        if not (r["list"] == list_name
                and (r["title"] in completed
                     or r["title"] == one_unfinished))
    ]
    passed, _ = _verify(reader, t)
    assert passed is False


def test_clear_completed_leaving_a_completed_item_fails():
    random.seed(42)
    t = gen_clear_completed_only()
    list_name = t.params["list"]
    completed = list(t.params["completed"])
    keep_one = completed[0]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Delete all completed EXCEPT one — agent missed it.
    reader._reminders = [
        r for r in reader._reminders
        if not (r["list"] == list_name
                and r["title"] in completed
                and r["title"] != keep_one)
    ]
    passed, _ = _verify(reader, t)
    assert passed is False


# ───────── all Tier 4 generators ship Springboard randomization ──────

@pytest.mark.parametrize("gen", [
    gen_set_due_date_on_reminder,
    gen_change_due_date,
    gen_complete_all_overdue,
    gen_add_notes_to_reminder,
    gen_clear_completed_only,
])
def test_tier4_springboard_noise_included(gen):
    random.seed(100)
    t = gen()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "layout" in sb_kinds
    assert "start_page" in sb_kinds
