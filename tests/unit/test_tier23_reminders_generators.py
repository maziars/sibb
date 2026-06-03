"""Tier 2/3 Reminders generators — bulk + structural tasks.

For each generator: spec validates, BEFORE fails, AFTER passes after
the simulated agent action, and representative cheat-path tests for
the strict checks. Uses FakeXCUITestReader; no sim required.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import validate_spec
from sibb_state import apply_initial_state
from sibb_task_generator_v3 import (
    gen_complete_all_in_list,
    gen_delete_specific_reminder,
    gen_delete_entire_list,
    gen_move_reminder_between_lists,
    gen_rename_reminder,
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


# ───────────────────── gen_complete_all_in_list ──────────────────────

def test_complete_all_spec_validates():
    random.seed(1)
    t = gen_complete_all_in_list()
    assert validate_spec(t.initial_state.spec) == []
    assert t.params["target_list"] != t.params["distractor_list"]


def test_complete_all_before_fails_after_passes():
    random.seed(1)
    t = gen_complete_all_in_list()
    target_list = t.params["target_list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["list"] == target_list:
            r["completed"] = True

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_complete_all_touching_distractor_list_fails():
    random.seed(2)
    t = gen_complete_all_in_list()
    target_list = t.params["target_list"]
    distractor_list = t.params["distractor_list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    for r in reader._reminders:
        if r["list"] == target_list:
            r["completed"] = True
    # Agent also touched a distractor item.
    for r in reader._reminders:
        if r["list"] == distractor_list:
            r["completed"] = True
            break

    passed, _ = _verify(reader, t)
    assert passed is False


def test_complete_all_partial_completion_fails():
    random.seed(3)
    t = gen_complete_all_in_list()
    target_list = t.params["target_list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Complete all but one.
    targets = [r for r in reader._reminders if r["list"] == target_list]
    for r in targets[:-1]:
        r["completed"] = True

    passed, _ = _verify(reader, t)
    assert passed is False


# ─────────────────── gen_delete_specific_reminder ────────────────────

def test_delete_specific_spec_validates():
    random.seed(10)
    t = gen_delete_specific_reminder()
    assert validate_spec(t.initial_state.spec) == []


def test_delete_specific_before_fails_after_passes():
    random.seed(10)
    t = gen_delete_specific_reminder()
    list_name = t.params["list"]; target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    reader._reminders = [
        r for r in reader._reminders
        if not (r["list"] == list_name and r["title"] == target)
    ]

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_delete_specific_deleting_wrong_item_fails():
    random.seed(11)
    t = gen_delete_specific_reminder()
    list_name = t.params["list"]; target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Agent deletes a sibling, not the target.
    sibling = next(r for r in reader._reminders
                     if r["list"] == list_name and r["title"] != target)
    reader._reminders.remove(sibling)

    passed, _ = _verify(reader, t)
    assert passed is False


def test_delete_specific_deleting_target_AND_sibling_fails():
    random.seed(12)
    t = gen_delete_specific_reminder()
    list_name = t.params["list"]; target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Agent overshoots — deletes target + one extra.
    to_delete = [r for r in reader._reminders if r["list"] == list_name][:2]
    if target not in [r["title"] for r in to_delete]:
        # Make sure target is in the delete set.
        for r in reader._reminders:
            if r["list"] == list_name and r["title"] == target:
                to_delete[0] = r; break
    for r in to_delete:
        if r in reader._reminders:
            reader._reminders.remove(r)
    passed, _ = _verify(reader, t)
    assert passed is False


# ───────────────────── gen_delete_entire_list ────────────────────────

def test_delete_entire_list_spec_validates():
    random.seed(20)
    t = gen_delete_entire_list()
    assert validate_spec(t.initial_state.spec) == []


def test_delete_entire_list_before_fails_after_passes():
    random.seed(20)
    t = gen_delete_entire_list()
    target_list = t.params["target_list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    # Simulate iOS cascade: delete the list AND all its items.
    reader._lists = [L for L in reader._lists if L["name"] != target_list]
    reader._reminders = [r for r in reader._reminders
                          if r["list"] != target_list]

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_delete_entire_list_deleting_distractor_too_fails():
    random.seed(21)
    t = gen_delete_entire_list()
    target_list = t.params["target_list"]
    distractor_list = t.params["distractor_list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    reader._lists = [L for L in reader._lists
                      if L["name"] not in (target_list, distractor_list)]
    reader._reminders = [r for r in reader._reminders
                          if r["list"] not in (target_list, distractor_list)]

    passed, _ = _verify(reader, t)
    assert passed is False


def test_delete_entire_list_only_emptying_target_fails():
    # Agent deletes all items but leaves the list itself.
    random.seed(22)
    t = gen_delete_entire_list()
    target_list = t.params["target_list"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    reader._reminders = [r for r in reader._reminders
                          if r["list"] != target_list]
    # Note: list still exists.

    passed, _ = _verify(reader, t)
    assert passed is False


# ───────────────── gen_move_reminder_between_lists ───────────────────

def test_move_reminder_spec_validates():
    random.seed(30)
    t = gen_move_reminder_between_lists()
    assert validate_spec(t.initial_state.spec) == []
    assert t.params["source"] != t.params["dest"]
    assert t.params["target"] in t.params["source_titles"]


def test_move_reminder_before_fails_after_passes():
    random.seed(30)
    t = gen_move_reminder_between_lists()
    source = t.params["source"]; dest = t.params["dest"]
    target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    # Simulate move: remove from source, add to dest.
    for r in reader._reminders:
        if r["list"] == source and r["title"] == target:
            r["list"] = dest
            break

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_move_reminder_copy_instead_of_move_fails():
    # Agent copies (creates new in dest) but leaves the original in source.
    random.seed(31)
    t = gen_move_reminder_between_lists()
    source = t.params["source"]; dest = t.params["dest"]
    target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    # Add to dest, don't remove from source.
    asyncio.run(reader._send({
        "type": "create_reminder",
        "list": dest, "title": target, "completed": False,
    }))

    passed, _ = _verify(reader, t)
    assert passed is False


# ────────────────────── gen_rename_reminder ──────────────────────────

def test_rename_spec_validates():
    random.seed(40)
    t = gen_rename_reminder()
    assert validate_spec(t.initial_state.spec) == []
    assert t.params["old_title"] != t.params["new_title"]
    assert t.params["new_title"] not in t.params["items"]


def test_rename_before_fails_after_passes():
    random.seed(40)
    t = gen_rename_reminder()
    list_name = t.params["list"]
    old_title = t.params["old_title"]; new_title = t.params["new_title"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == old_title:
            r["title"] = new_title
            break

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_rename_creating_new_without_deleting_old_fails():
    # Lazy path: agent creates a new item with the new title but
    # doesn't rename the old. count guard catches this (n+1 != n).
    random.seed(41)
    t = gen_rename_reminder()
    list_name = t.params["list"]; new_title = t.params["new_title"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    asyncio.run(reader._send({
        "type": "create_reminder",
        "list": list_name, "title": new_title, "completed": False,
    }))

    passed, _ = _verify(reader, t)
    assert passed is False


def test_rename_deleting_old_without_creating_new_fails():
    # Mirror: agent deletes the old but doesn't create the new.
    random.seed(42)
    t = gen_rename_reminder()
    list_name = t.params["list"]; old_title = t.params["old_title"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    reader._reminders = [
        r for r in reader._reminders
        if not (r["list"] == list_name and r["title"] == old_title)
    ]

    passed, _ = _verify(reader, t)
    assert passed is False


# ─────────── all Tier 2/3 generators ship Springboard noise ──────────

@pytest.mark.parametrize("gen", [
    gen_complete_all_in_list,
    gen_delete_specific_reminder,
    gen_delete_entire_list,
    gen_move_reminder_between_lists,
    gen_rename_reminder,
])
def test_tier23_springboard_noise_included(gen):
    random.seed(100)
    t = gen()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "layout" in sb_kinds
    assert "start_page" in sb_kinds
