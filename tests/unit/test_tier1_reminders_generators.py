"""Tier 1 Reminders generators — full BEFORE/AFTER round-trip on the
FakeXCUITestReader.

For each generator:
  1. Validate the produced spec (typed-spec shape).
  2. Apply the spec via the dispatcher to seed the fake.
  3. Run the verifier BEFORE the agent acts — must FAIL (blocking).
  4. Simulate the agent's action against the fake reader.
  5. Run the verifier AFTER — must PASS (blocking).

L1.5 in spirit (uses the fake), but lives under unit/ since it doesn't
boot a simulator. Catches generator/verifier drift early — a typo'd
selector that wouldn't fire until L2 lights up here in ms.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import validate_spec
from sibb_state import apply_initial_state
from sibb_task_generator_v3 import (
    gen_complete_specific_reminder,
    gen_uncomplete_reminder,
    gen_add_reminder_to_existing_list,
    gen_set_priority,
    _PRIORITY_STR_TO_INT,
)
from sibb_verify import run_checks, blocking_pass

pytestmark = pytest.mark.fast


def _verify(reader, task):
    """Convenience: blocking_pass over the task's check list."""
    results = asyncio.run(run_checks(reader, task.verify_checks))
    return blocking_pass(results), results


def _seed_initial_state(reader, task):
    """Apply the typed initial-state spec on the fake reader via the
    dispatcher. Returns a state-application report; raises on errors."""
    report = asyncio.run(apply_initial_state(reader, task))
    assert not report.get("errors"), \
        f"state setup failed: {report['errors']}"
    return report


# ─────────────────── gen_complete_specific_reminder ──────────────────

def test_complete_specific_reminder_spec_validates():
    random.seed(1)
    t = gen_complete_specific_reminder()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Reminders"]
    # Strict: attribute_eq (target) + count(list)==n + subset(titles)
    # + 6 "no irrelevant edits" checks (completed, priority=0, notes
    # null, due null, url null, recurrence null counts) = 9 blocking.
    assert len(t.verify_checks) == 9
    assert all(c["severity"] == "blocking" for c in t.verify_checks)
    assert t.verify_checks[0]["kind"] == "attribute_eq"
    assert t.verify_checks[0]["attr"] == "completed"
    assert t.verify_checks[0]["value"] is True


def test_complete_specific_reminder_instruction_has_phrasing_variation():
    # Across many seeds we should see at least 2 distinct instruction
    # texts (3 phrasings exist, so this catches a regression where
    # random.choice gets pinned).
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_complete_specific_reminder().instruction)
    assert len(instructions) >= 2


def test_complete_specific_reminder_springboard_includes_start_page():
    # Random start_page is part of the home-screen randomization so
    # episodes don't always land on page 0.
    random.seed(1)
    t = gen_complete_specific_reminder()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


def test_complete_specific_reminder_adding_notes_to_target_fails():
    # Agent completes target AND adds notes to it. The "no irrelevant
    # edits" block must catch the notes side-effect.
    random.seed(20)
    t = gen_complete_specific_reminder()
    list_name = t.params["list"]
    target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["completed"] = True
            r["notes"] = "Side note the agent shouldn't have written"
            break

    passed, _ = _verify(reader, t)
    assert passed is False


def test_complete_specific_reminder_attaching_recurrence_to_target_fails():
    # Agent completes target AND silently attaches a recurrence rule.
    # The "no irrelevant edits" block must catch the recurrence
    # side-effect via count(recurrence=None) == n_items.
    random.seed(22)
    t = gen_complete_specific_reminder()
    list_name = t.params["list"]
    target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["completed"] = True
            r["recurrence"] = {"frequency": "weekly", "interval": 1}
            r["due"] = "2026-06-01T09:00:00"
            break

    passed, _ = _verify(reader, t)
    assert passed is False


def test_complete_specific_reminder_bumping_sibling_priority_fails():
    # Agent completes target correctly but also changes a sibling's
    # priority. count(priority=0)==n must catch this.
    random.seed(21)
    t = gen_complete_specific_reminder()
    list_name = t.params["list"]
    target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["completed"] = True
        elif r["list"] == list_name:
            r["priority"] = 1   # high — sibling shouldn't have changed
            break

    passed, _ = _verify(reader, t)
    assert passed is False


def test_complete_specific_reminder_springboard_noise_included():
    # Every Tier 1 task ships home-screen randomization so the agent
    # must find Reminders by app label. The spec should have at least
    # one Springboard entry.
    random.seed(1)
    t = gen_complete_specific_reminder()
    sb = [e for e in t.initial_state.spec if e.get("app") == "Springboard"]
    assert sb, "expected at least one Springboard layout entry in the spec"
    assert any(e["type"] == "layout" for e in sb)


def test_complete_specific_reminder_completing_target_AND_sibling_fails():
    # Agent does the right thing on target but also accidentally
    # toggles a sibling. Strict count(completed=True)==1 must block.
    random.seed(7)
    t = gen_complete_specific_reminder()
    list_name = t.params["list"]
    target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    # Mark target completed.
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["completed"] = True
            break
    # Mark one sibling completed too.
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] != target:
            r["completed"] = True
            break

    passed, _ = _verify(reader, t)
    assert passed is False


def test_complete_specific_reminder_completing_and_renaming_fails():
    # Agent flips completed correctly but also renames the target.
    # subset(title) must catch this.
    random.seed(8)
    t = gen_complete_specific_reminder()
    list_name = t.params["list"]
    target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["completed"] = True
            r["title"] = "Renamed by agent"

    passed, _ = _verify(reader, t)
    assert passed is False


def test_complete_specific_reminder_completing_and_deleting_sibling_fails():
    # Agent completes target but deletes a sibling. Strict count(list)==n
    # catches the deletion.
    random.seed(9)
    t = gen_complete_specific_reminder()
    list_name = t.params["list"]
    target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    # Complete target.
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["completed"] = True
            break
    # Delete a sibling.
    victim = next(r for r in reader._reminders
                    if r["list"] == list_name and r["title"] != target)
    reader._reminders.remove(victim)

    passed, _ = _verify(reader, t)
    assert passed is False


def test_complete_specific_reminder_before_fails_after_passes():
    random.seed(1)
    t = gen_complete_specific_reminder()
    target = t.params["target"]
    list_name = t.params["list"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    # BEFORE — nothing completed yet → blocking check fails.
    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    # Simulate the user-action: mark target completed in the fake.
    for r in reader._reminders:
        if r["title"] == target and r["list"] == list_name:
            r["completed"] = True

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_complete_specific_reminder_completing_the_wrong_item_fails():
    random.seed(2)
    t = gen_complete_specific_reminder()
    list_name = t.params["list"]
    target = t.params["target"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    # Mark a NON-target item complete → blocking attribute_eq must fail.
    wrong = next(r for r in reader._reminders
                   if r["title"] != target and r["list"] == list_name)
    wrong["completed"] = True

    passed, _ = _verify(reader, t)
    assert passed is False


# ───────────────────── gen_uncomplete_reminder ───────────────────────

def test_uncomplete_reminder_spec_validates_and_seeds_target_completed():
    random.seed(3)
    t = gen_uncomplete_reminder()
    assert validate_spec(t.initial_state.spec) == []
    target = t.params["target"]
    seeded = {e["title"]: e.get("completed", False)
              for e in t.initial_state.spec
              if e["type"] == "item"}
    # Only the target starts completed.
    assert seeded[target] is True
    assert all(v is False for k, v in seeded.items() if k != target)


def test_uncomplete_reminder_before_fails_after_passes():
    random.seed(3)
    t = gen_uncomplete_reminder()
    target = t.params["target"]
    list_name = t.params["list"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["title"] == target and r["list"] == list_name:
            r["completed"] = False

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


# ──────────────── gen_add_reminder_to_existing_list ──────────────────

def test_add_reminder_spec_validates():
    random.seed(4)
    t = gen_add_reminder_to_existing_list()
    assert validate_spec(t.initial_state.spec) == []
    # The new title is not in the seeded items.
    seeded_titles = {e["title"] for e in t.initial_state.spec
                     if e["type"] == "item"}
    assert t.params["new_title"] not in seeded_titles


def test_add_reminder_before_fails_after_passes():
    random.seed(4)
    t = gen_add_reminder_to_existing_list()
    new_title = t.params["new_title"]
    list_name = t.params["list"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    # Simulate the agent: append the new reminder.
    asyncio.run(reader._send({
        "type": "create_reminder",
        "title": new_title, "list": list_name,
        "completed": False,
    }))

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_add_reminder_duplicate_now_blocks_pass():
    # Tightened verifier (2026-05-18): count==3 is blocking, so adding
    # the same title twice trips the count guard and fails the task.
    random.seed(4)
    t = gen_add_reminder_to_existing_list()
    new_title = t.params["new_title"]
    list_name = t.params["list"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    for _ in range(2):
        asyncio.run(reader._send({
            "type": "create_reminder",
            "title": new_title, "list": list_name,
            "completed": False,
        }))

    passed, _ = _verify(reader, t)
    assert passed is False


def test_add_reminder_deleting_existing_then_adding_new_fails():
    # Agent adds the new reminder but also deletes an existing one.
    # count==3 still passes (2 existing - 1 deleted + 1 new = 2... no
    # wait, 2 - 1 + 1 = 2, count==3 fails). Let's set up clearer: agent
    # deletes ONE existing item and adds the new one + a junk one to keep
    # count at 3. Then subset(title) catches the missing original.
    random.seed(10)
    t = gen_add_reminder_to_existing_list()
    new_title = t.params["new_title"]
    list_name = t.params["list"]
    existing = t.params["existing"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    # Delete one of the existing items.
    victim = next(r for r in reader._reminders
                    if r["list"] == list_name and r["title"] == existing[0])
    reader._reminders.remove(victim)
    # Add the new one + a junk one (so count==3 still passes).
    for title in (new_title, "Some junk"):
        asyncio.run(reader._send({
            "type": "create_reminder",
            "title": title, "list": list_name, "completed": False,
        }))

    passed, _ = _verify(reader, t)
    # subset(title) must catch that existing[0] is missing.
    assert passed is False


def test_add_reminder_auto_completing_new_item_fails():
    # Agent adds the reminder but marks it completed in the same step.
    # Strict count(completed=True)==0 catches this.
    random.seed(11)
    t = gen_add_reminder_to_existing_list()
    new_title = t.params["new_title"]
    list_name = t.params["list"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    asyncio.run(reader._send({
        "type": "create_reminder",
        "title": new_title, "list": list_name, "completed": True,
    }))

    passed, _ = _verify(reader, t)
    assert passed is False


# ─────────────────────── gen_set_priority ────────────────────────────

def test_set_priority_spec_validates():
    random.seed(5)
    t = gen_set_priority()
    assert validate_spec(t.initial_state.spec) == []
    # Verifier check uses the EventKit integer.
    blk = t.verify_checks[0]
    assert blk["attr"] == "priority"
    assert blk["value"] == _PRIORITY_STR_TO_INT[t.params["level"]]


def test_set_priority_before_fails_after_passes():
    random.seed(5)
    t = gen_set_priority()
    target = t.params["target"]
    list_name = t.params["list"]
    level_int = t.params["level_int"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["title"] == target and r["list"] == list_name:
            r["priority"] = level_int

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_set_priority_wrong_level_fails():
    random.seed(6)
    t = gen_set_priority()
    target = t.params["target"]
    list_name = t.params["list"]
    correct_int = t.params["level_int"]

    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    # Pick any int that isn't the right one.
    wrong = 1 if correct_int != 1 else 5
    for r in reader._reminders:
        if r["title"] == target and r["list"] == list_name:
            r["priority"] = wrong

    passed, _ = _verify(reader, t)
    assert passed is False
