"""Tier 4b Reminders generators — recurrence-based tasks.

For each: spec validates, BEFORE fails, AFTER passes after the
simulated agent action, plus representative cheat-path tests for the
strict guards. Uses FakeXCUITestReader; no sim required.

NOT covered (per the 2026-05-20 critic synthesis): completing a
recurring reminder. iOS spawns the next occurrence on completion and
the fake doesn't model that — those tests must be L2.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import validate_spec
from sibb_state import apply_initial_state
from sibb_task_generator_v3 import (
    gen_make_reminder_recurring,
    gen_change_recurrence_frequency,
    gen_stop_recurrence,
    gen_create_recurring_with_due,
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


# ───────────────────── gen_make_reminder_recurring ───────────────────

def test_make_recurring_spec_validates():
    random.seed(1)
    t = gen_make_reminder_recurring()
    assert validate_spec(t.initial_state.spec) == []
    assert t.params["frequency"] in ("daily", "weekly", "monthly", "yearly")


def test_make_recurring_before_fails_after_passes():
    random.seed(1)
    t = gen_make_reminder_recurring()
    list_name = t.params["list"]; target = t.params["target"]
    freq = t.params["frequency"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["recurrence"] = {"frequency": freq, "interval": 1}
            break

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_make_recurring_setting_wrong_frequency_fails():
    random.seed(2)
    t = gen_make_reminder_recurring()
    list_name = t.params["list"]; target = t.params["target"]
    freq = t.params["frequency"]
    # Pick any other frequency.
    wrong = next(f for f in ("daily", "weekly", "monthly", "yearly")
                   if f != freq)
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["recurrence"] = {"frequency": wrong, "interval": 1}
            break
    passed, _ = _verify(reader, t)
    assert passed is False


def test_make_recurring_attaching_rule_to_sibling_fails():
    random.seed(3)
    t = gen_make_reminder_recurring()
    list_name = t.params["list"]; target = t.params["target"]
    freq = t.params["frequency"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    # Correctly set rule on target.
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["recurrence"] = {"frequency": freq, "interval": 1}
            break
    # But also accidentally rule a sibling.
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] != target:
            r["recurrence"] = {"frequency": freq, "interval": 1}
            # siblings have no due — but the agent might also bolt one
            # on. Cover the worst case.
            r["due"] = t.params["due_iso"]
            break
    passed, _ = _verify(reader, t)
    assert passed is False


def test_make_recurring_clearing_due_fails():
    random.seed(4)
    t = gen_make_reminder_recurring()
    list_name = t.params["list"]; target = t.params["target"]
    freq = t.params["frequency"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Agent sets recurrence but accidentally clears the due date.
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["recurrence"] = {"frequency": freq, "interval": 1}
            r.pop("due", None)
            break
    passed, _ = _verify(reader, t)
    assert passed is False


# ───────────────────── gen_change_recurrence_frequency ───────────────

def test_change_recurrence_spec_seeds_target_with_old_frequency():
    random.seed(10)
    t = gen_change_recurrence_frequency()
    target = t.params["target"]
    old_freq = t.params["old_frequency"]
    seeded = {e["title"]: e for e in t.initial_state.spec
              if e["type"] == "item"}
    rec = seeded[target].get("recurrence")
    assert rec is not None and rec["frequency"] == old_freq


def test_change_recurrence_before_fails_after_passes():
    random.seed(10)
    t = gen_change_recurrence_frequency()
    list_name = t.params["list"]; target = t.params["target"]
    new_freq = t.params["new_frequency"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r["recurrence"]["frequency"] = new_freq
            break

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_change_recurrence_leaving_old_frequency_fails():
    random.seed(11)
    t = gen_change_recurrence_frequency()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Agent does nothing — old frequency still in place.
    passed, _ = _verify(reader, t)
    assert passed is False


def test_change_recurrence_removing_rule_entirely_fails():
    # Agent over-corrects: removes the rule instead of changing it.
    random.seed(12)
    t = gen_change_recurrence_frequency()
    list_name = t.params["list"]; target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r.pop("recurrence", None)
            break
    passed, _ = _verify(reader, t)
    assert passed is False


# ─────────────────────── gen_stop_recurrence ─────────────────────────

def test_stop_recurrence_spec_seeds_target_recurring():
    random.seed(20)
    t = gen_stop_recurrence()
    target = t.params["target"]
    seeded = {e["title"]: e for e in t.initial_state.spec
              if e["type"] == "item"}
    assert seeded[target].get("recurrence") is not None


def test_stop_recurrence_before_fails_after_passes():
    random.seed(20)
    t = gen_stop_recurrence()
    list_name = t.params["list"]; target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r.pop("recurrence", None)
            break

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_stop_recurrence_clearing_due_too_fails():
    # Agent removes rule AND the due date (over-deletion).
    random.seed(21)
    t = gen_stop_recurrence()
    list_name = t.params["list"]; target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    for r in reader._reminders:
        if r["list"] == list_name and r["title"] == target:
            r.pop("recurrence", None)
            r.pop("due", None)
            break
    passed, _ = _verify(reader, t)
    assert passed is False


def test_stop_recurrence_deleting_target_entirely_fails():
    random.seed(22)
    t = gen_stop_recurrence()
    list_name = t.params["list"]; target = t.params["target"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    reader._reminders = [
        r for r in reader._reminders
        if not (r["list"] == list_name and r["title"] == target)
    ]
    passed, _ = _verify(reader, t)
    assert passed is False


# ───────────────────── gen_create_recurring_with_due ─────────────────

def test_create_recurring_spec_validates():
    random.seed(30)
    t = gen_create_recurring_with_due()
    assert validate_spec(t.initial_state.spec) == []
    assert t.params["new_title"]
    assert t.params["due_iso"]
    assert t.params["frequency"] in ("daily", "weekly", "monthly", "yearly")


def test_create_recurring_before_fails_after_passes():
    random.seed(30)
    t = gen_create_recurring_with_due()
    list_name = t.params["list"]; new_title = t.params["new_title"]
    due_iso = t.params["due_iso"]; freq = t.params["frequency"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)

    passed_before, _ = _verify(reader, t)
    assert passed_before is False

    asyncio.run(reader._send({
        "type": "create_reminder",
        "list": list_name, "title": new_title,
        "due_iso": due_iso,
        "recurrence": {"frequency": freq, "interval": 1},
    }))

    passed_after, _ = _verify(reader, t)
    assert passed_after is True


def test_create_recurring_missing_recurrence_fails():
    random.seed(31)
    t = gen_create_recurring_with_due()
    list_name = t.params["list"]; new_title = t.params["new_title"]
    due_iso = t.params["due_iso"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Agent creates the reminder + due but forgets the recurrence.
    asyncio.run(reader._send({
        "type": "create_reminder",
        "list": list_name, "title": new_title, "due_iso": due_iso,
    }))
    passed, _ = _verify(reader, t)
    assert passed is False


def test_create_recurring_missing_due_fails():
    # The fake silently drops recurrence without due → the row has no
    # recurrence, and our attribute_eq on recurrence.frequency fails.
    random.seed(32)
    t = gen_create_recurring_with_due()
    list_name = t.params["list"]; new_title = t.params["new_title"]
    freq = t.params["frequency"]
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    asyncio.run(reader._send({
        "type": "create_reminder",
        "list": list_name, "title": new_title,
        "recurrence": {"frequency": freq, "interval": 1},
    }))
    passed, _ = _verify(reader, t)
    assert passed is False


def test_create_recurring_wrong_frequency_fails():
    random.seed(33)
    t = gen_create_recurring_with_due()
    list_name = t.params["list"]; new_title = t.params["new_title"]
    due_iso = t.params["due_iso"]; freq = t.params["frequency"]
    wrong = next(f for f in ("daily", "weekly", "monthly", "yearly")
                   if f != freq)
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    asyncio.run(reader._send({
        "type": "create_reminder",
        "list": list_name, "title": new_title, "due_iso": due_iso,
        "recurrence": {"frequency": wrong, "interval": 1},
    }))
    passed, _ = _verify(reader, t)
    assert passed is False


# ─────────── all Tier 4b generators ship Springboard noise ───────────

@pytest.mark.parametrize("gen", [
    gen_make_reminder_recurring,
    gen_change_recurrence_frequency,
    gen_stop_recurrence,
    gen_create_recurring_with_due,
])
def test_tier4b_springboard_noise_included(gen):
    random.seed(100)
    t = gen()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "layout" in sb_kinds
    assert "start_page" in sb_kinds
