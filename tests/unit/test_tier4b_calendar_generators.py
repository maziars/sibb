"""Tier 4b Calendar generators — recurrence rule attach / detach /
change-frequency / create-recurring.

Same shape as test_tier4_calendar_generators.py + critic-flagged
cheats specific to recurrence:
  • per-distractor attribute_absent(recurrence) (no rule leak)
  • _signature_set never sees `recurrence` (would be unhashable)
  • baseline captured with master_only=True (default) so recurring
    masters are 1 row not 52.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import validate_spec
from sibb_state import apply_initial_state
from sibb_task_generator_v3 import (
    gen_make_event_recurring,
    gen_stop_event_recurrence,
    gen_change_event_recurrence_frequency,
    gen_create_recurring_event,
)
from sibb_verify import BaselineSnapshot, run_checks, blocking_pass

pytestmark = pytest.mark.fast


def _seed_initial_state(reader, task):
    report = asyncio.run(apply_initial_state(reader, task))
    assert not report.get("errors"), \
        f"state setup failed: {report['errors']}"
    return report


def _capture_baseline(reader):
    return asyncio.run(
        BaselineSnapshot.capture(reader, ["calendar.events"]))


def _verify(reader, task, *, baseline=None):
    results = asyncio.run(
        run_checks(reader, task.verify_checks, baseline=baseline))
    return blocking_pass(results), results


# ═════════════════════════ gen_make_event_recurring ═══════════════════════

def test_make_event_recurring_spec_validates():
    random.seed(1)
    t = gen_make_event_recurring()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Calendar"]


def test_make_event_recurring_before_fails_after_passes():
    random.seed(2)
    t = gen_make_event_recurring()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    # Agent attaches a weekly recurrence to the target.
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["recurrence"] = {
                "frequency": "weekly",
                "interval": 1,
                "end_count": t.params["end_count"],
            }
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_make_event_recurring_wrong_frequency_fails():
    random.seed(3)
    t = gen_make_event_recurring()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["recurrence"] = {
                "frequency": "daily",   # wrong
                "interval": 1,
                "end_count": t.params["end_count"],
            }
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_make_event_recurring_wrong_end_count_fails():
    random.seed(4)
    t = gen_make_event_recurring()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["recurrence"] = {
                "frequency": "weekly", "interval": 1,
                "end_count": t.params["end_count"] + 1,   # off by one
            }
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_make_event_recurring_rule_on_wrong_event_caught():
    # Agent attaches recurrence to a DISTRACTOR. The per-distractor
    # attribute_absent(recurrence) checks fire.
    random.seed(5)
    t = gen_make_event_recurring()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] != t.params["target"]:
            e["recurrence"] = {
                "frequency": "weekly", "interval": 1,
                "end_count": t.params["end_count"],
            }
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_make_event_recurring_target_notes_added_caught():
    # Agent attaches the right rule AND adds spurious notes on target.
    random.seed(6)
    t = gen_make_event_recurring()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["recurrence"] = {
                "frequency": "weekly", "interval": 1,
                "end_count": t.params["end_count"],
            }
            e["notes"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_make_event_recurring_target_time_shift_caught():
    random.seed(7)
    t = gen_make_event_recurring()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["recurrence"] = {
                "frequency": "weekly", "interval": 1,
                "end_count": t.params["end_count"],
            }
            old = e["start_iso"]
            hh = int(old[11:13])
            e["start_iso"] = old[:11] + f"{(hh + 1) % 24:02d}" + old[13:]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_make_event_recurring_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_make_event_recurring().instruction)
    assert len(instructions) >= 2


# ═════════════════════════ gen_stop_event_recurrence ═════════════════════════

def test_stop_event_recurrence_spec_validates():
    random.seed(20)
    t = gen_stop_event_recurrence()
    assert validate_spec(t.initial_state.spec) == []


def test_stop_event_recurrence_seeds_recurring_target():
    """Sanity: the seeded spec must put a recurrence rule on the target,
    else the BEFORE verifier passes trivially."""
    random.seed(21)
    t = gen_stop_event_recurrence()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            assert e.get("recurrence") is not None, \
                "spec failed to seed target with a recurrence rule"
            return
    pytest.fail(f"target {t.params['target']!r} not found in seeded events")


def test_stop_event_recurrence_before_fails_after_passes():
    random.seed(22)
    t = gen_stop_event_recurrence()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e.pop("recurrence", None)
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_stop_event_recurrence_delete_target_fails():
    # Agent removes the entire event instead of just the rule.
    random.seed(23)
    t = gen_stop_event_recurrence()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    reader._events = [e for e in reader._events
                       if e["title"] != t.params["target"]]
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_stop_event_recurrence_keep_partial_rule_fails():
    # Agent changes frequency to yearly but doesn't remove the rule.
    random.seed(24)
    t = gen_stop_event_recurrence()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["recurrence"] = {
                "frequency": "yearly", "interval": 1,
                "end_count": 2,
            }
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_stop_event_recurrence_target_field_mutation_caught():
    random.seed(25)
    t = gen_stop_event_recurrence()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e.pop("recurrence", None)
            e["location"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_stop_event_recurrence_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_stop_event_recurrence().instruction)
    assert len(instructions) >= 2


# ═════════════════════════ Shared T4b invariants ════════════════════════════

@pytest.mark.parametrize("gen", [
    gen_make_event_recurring, gen_stop_event_recurrence,
])
def test_t4b_does_not_put_recurrence_in_distractor_compare_fields(gen):
    """Critic 3's blocker: recurrence is a dict; adding it to
    _CAL_DISTRACTOR_FIELDS would cause _signature_set to build tuples
    containing dicts, which TypeError on set() at the cheat path
    (the worst failure mode). Verify the helper field list excludes
    recurrence."""
    from sibb_task_generator_v3 import _CAL_DISTRACTOR_FIELDS
    assert "recurrence" not in _CAL_DISTRACTOR_FIELDS


@pytest.mark.parametrize("gen", [
    gen_make_event_recurring, gen_stop_event_recurrence,
])
def test_t4b_emits_attribute_absent_recurrence_per_distractor(gen):
    """Each T4b generator must emit one attribute_absent(recurrence)
    check per non-target event, since recurrence can't live in
    compare_fields."""
    random.seed(101)
    t = gen()
    abs_checks = [c for c in t.verify_checks
                   if c.get("kind") == "attribute_absent"
                      and c.get("attr") == "recurrence"]
    n_titles = len(t.params["titles"])
    # n_titles - 1 non-target events; gen_stop_event_recurrence also
    # asserts target's recurrence-absent via attribute_absent — that
    # check appears alongside the per-distractor ones, totaling n.
    if gen is gen_stop_event_recurrence:
        assert len(abs_checks) == n_titles
    else:
        assert len(abs_checks) == n_titles - 1


# ═════════════════════════ gen_change_event_recurrence_frequency ══════════

def test_change_event_recurrence_frequency_spec_validates():
    random.seed(30)
    t = gen_change_event_recurrence_frequency()
    assert validate_spec(t.initial_state.spec) == []
    assert t.params["old_frequency"] != t.params["new_frequency"]


def test_change_event_recurrence_frequency_before_fails_after_passes():
    random.seed(31)
    t = gen_change_event_recurrence_frequency()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["recurrence"] = {
                "frequency": t.params["new_frequency"],
                "interval":  1,
                "end_count": t.params["end_count"],
            }
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_change_event_recurrence_frequency_changes_interval_caught():
    # Critic 3 cheat: agent changes interval=7 (effectively daily-with-7)
    # rather than the requested frequency.
    random.seed(32)
    t = gen_change_event_recurrence_frequency()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["recurrence"] = {
                "frequency": t.params["old_frequency"],
                "interval":  7,  # NOT 1
                "end_count": t.params["end_count"],
            }
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_change_event_recurrence_frequency_phrasing_no_old_freq_leak():
    """Critic 3 directive: instruction must not leak the OLD
    frequency. Agent should observe via Calendar UI."""
    for seed in range(50):
        random.seed(seed)
        t = gen_change_event_recurrence_frequency()
        assert t.params["old_frequency"] not in t.instruction, (
            f"seed={seed}: instruction leaks old_frequency "
            f"{t.params['old_frequency']!r}: {t.instruction!r}")


def test_change_event_recurrence_frequency_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(
            gen_change_event_recurrence_frequency().instruction)
    assert len(instructions) >= 2


# ═════════════════════════ gen_create_recurring_event ═══════════════════════

def test_create_recurring_event_spec_validates():
    random.seed(40)
    t = gen_create_recurring_event()
    assert validate_spec(t.initial_state.spec) == []


def test_create_recurring_event_before_fails_after_passes():
    random.seed(41)
    t = gen_create_recurring_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["title"],
        "start_iso": t.params["start_iso"],
        "end_iso":   t.params["end_iso"],
        "recurrence": {
            "frequency": "weekly", "interval": 1,
            "end_count": t.params["end_count"],
        },
    }))
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_create_recurring_event_one_off_fails():
    # Agent creates the event without a recurrence rule.
    random.seed(42)
    t = gen_create_recurring_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["title"],
        "start_iso": t.params["start_iso"],
        "end_iso":   t.params["end_iso"],
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_create_recurring_event_wrong_end_count_fails():
    random.seed(43)
    t = gen_create_recurring_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["title"],
        "start_iso": t.params["start_iso"],
        "end_iso":   t.params["end_iso"],
        "recurrence": {
            "frequency": "weekly", "interval": 1,
            "end_count": t.params["end_count"] + 1,
        },
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_create_recurring_event_with_spurious_notes_fails():
    random.seed(44)
    t = gen_create_recurring_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["title"],
        "start_iso": t.params["start_iso"],
        "end_iso":   t.params["end_iso"],
        "notes":     "sneaky",
        "recurrence": {
            "frequency": "weekly", "interval": 1,
            "end_count": t.params["end_count"],
        },
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_create_recurring_event_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_create_recurring_event().instruction)
    assert len(instructions) >= 2
