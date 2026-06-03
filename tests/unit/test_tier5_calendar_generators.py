"""Tier 5 Calendar generators — reporting via agent_answer.

Mirrors test_tier5_reminders_generators.py: each generator's spec
validates, the BEFORE verifier fails without an answer, a correct
ANSWER payload threaded via context makes AFTER pass, and various
wrong-answer / state-mutation cheats fail.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import validate_spec
from sibb_state import apply_initial_state
from sibb_task_generator_v3 import (
    gen_lookup_event_location,
    gen_list_events_today,
    gen_list_conflicting_events,
    gen_next_event_lookup,
)
from sibb_verify import (
    BaselineSnapshot, run_checks, blocking_pass, lint_answer_instruction,
)

pytestmark = pytest.mark.fast


def _verify(reader, task, *, answer=None, observed=None, baseline=None):
    ctx = {
        "agent_answer": answer,
        "observed_bundles": observed if observed is not None
                              else ["com.apple.mobilecal"],
    }
    results = asyncio.run(
        run_checks(reader, task.verify_checks,
                    context=ctx, baseline=baseline))
    return blocking_pass(results), results


def _seed_initial_state(reader, task):
    report = asyncio.run(apply_initial_state(reader, task))
    assert not report.get("errors"), \
        f"state setup failed: {report['errors']}"
    return report


def _capture_baseline(reader):
    return asyncio.run(
        BaselineSnapshot.capture(reader, ["calendar.events"]))


# ═════════════════════════ gen_lookup_event_location ═══════════════════════

def test_lookup_event_location_spec_and_lint():
    random.seed(1)
    t = gen_lookup_event_location()
    assert validate_spec(t.initial_state.spec) == []
    aa = next(c for c in t.verify_checks if c["kind"] == "agent_answer")
    assert lint_answer_instruction(t.instruction, aa) == []


def test_lookup_event_location_exact_answer_passes():
    random.seed(2)
    t = gen_lookup_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    passed, _ = _verify(reader, t,
                          answer={"value": t.params["location"]},
                          baseline=baseline)
    assert passed is True


def test_lookup_event_location_case_insensitive_passes():
    random.seed(3)
    t = gen_lookup_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    passed, _ = _verify(reader, t,
                          answer={"value": t.params["location"].upper()},
                          baseline=baseline)
    assert passed is True


def test_lookup_event_location_trimmed_whitespace_passes():
    random.seed(4)
    t = gen_lookup_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    passed, _ = _verify(reader, t,
                          answer={"value": "  " + t.params["location"]
                                            + "\t"},
                          baseline=baseline)
    assert passed is True


def test_lookup_event_location_wrong_value_fails():
    random.seed(5)
    t = gen_lookup_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    passed, _ = _verify(reader, t,
                          answer={"value": "Somewhere else entirely"},
                          baseline=baseline)
    assert passed is False


def test_lookup_event_location_observation_gate_blocks_without_calendar():
    # Agent didn't observe Calendar — answer must fail.
    random.seed(6)
    t = gen_lookup_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    passed, _ = _verify(reader, t,
                          answer={"value": t.params["location"]},
                          observed=[],
                          baseline=baseline)
    assert passed is False


def test_lookup_event_location_state_mutation_caught():
    # Agent answers correctly but ALSO mutates target's location.
    random.seed(7)
    t = gen_lookup_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["location"] = "Mutated cafe"
            break
    passed, _ = _verify(reader, t,
                          answer={"value": t.params["location"]},
                          baseline=baseline)
    assert passed is False


def test_lookup_event_location_no_answer_fails():
    random.seed(8)
    t = gen_lookup_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    passed, _ = _verify(reader, t, answer=None, baseline=baseline)
    assert passed is False


def test_lookup_event_location_includes_observation_required():
    random.seed(10)
    t = gen_lookup_event_location()
    aa = next(c for c in t.verify_checks if c["kind"] == "agent_answer")
    assert aa.get("observation_required") == ["com.apple.mobilecal"]


def test_lookup_event_location_springboard_includes_start_page():
    random.seed(11)
    t = gen_lookup_event_location()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


# ═════════════════════════ gen_list_events_today ═══════════════════════════

def test_list_events_today_spec_and_lint():
    random.seed(20)
    t = gen_list_events_today()
    assert validate_spec(t.initial_state.spec) == []
    aa = next(c for c in t.verify_checks if c["kind"] == "agent_answer")
    assert lint_answer_instruction(t.instruction, aa) == []


def test_list_events_today_correct_set_passes():
    random.seed(21)
    t = gen_list_events_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{"title": ti}
                          for ti in t.params["today_titles"]]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is True


def test_list_events_today_order_does_not_matter():
    random.seed(22)
    t = gen_list_events_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{"title": ti}
                          for ti in reversed(t.params["today_titles"])]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is True


def test_list_events_today_missing_one_fails():
    random.seed(23)
    t = gen_list_events_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{"title": ti}
                          for ti in t.params["today_titles"][:-1]]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_list_events_today_extra_fails():
    # Agent includes a non-today event in the answer.
    random.seed(24)
    t = gen_list_events_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{"title": ti}
                          for ti in t.params["today_titles"]]
                + [{"title": t.params["other_titles"][0]}]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_list_events_today_observation_gate_fails_without_calendar():
    random.seed(25)
    t = gen_list_events_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{"title": ti}
                          for ti in t.params["today_titles"]]}
    passed, _ = _verify(reader, t, answer=payload,
                          observed=[], baseline=baseline)
    assert passed is False


def test_list_events_today_state_mutation_caught():
    # Agent answers correctly but ALSO deletes a today event.
    random.seed(26)
    t = gen_list_events_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    one_today_title = t.params["today_titles"][0]
    reader._events = [e for e in reader._events
                       if e["title"] != one_today_title]
    payload = {"items": [{"title": ti}
                          for ti in t.params["today_titles"]]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_list_events_today_gates_at_least_two_today_events():
    """T5 critic 1: n_today must be ≥2 so 'report everything' or
    'report nothing' cheats don't trivially pass."""
    for seed in range(50):
        random.seed(seed)
        t = gen_list_events_today()
        assert len(t.params["today_titles"]) >= 2


def test_list_events_today_observation_required_set_correctly():
    random.seed(30)
    t = gen_list_events_today()
    aa = next(c for c in t.verify_checks if c["kind"] == "agent_answer")
    assert aa.get("observation_required") == ["com.apple.mobilecal"]


def test_list_events_today_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_list_events_today().instruction)
    assert len(instructions) >= 2


# ═════════════════════════ gen_list_conflicting_events ═════════════════════

def test_list_conflicting_events_spec_and_invariants():
    """Critic-pinned invariants: exactly 1 overlapping pair + ≥3
    non-conflicting distractors. Sweep seeds to ensure these hold."""
    for seed in range(50):
        random.seed(seed)
        t = gen_list_conflicting_events()
        assert validate_spec(t.initial_state.spec) == []
        all_titles = t.params["all_titles"]
        assert len(all_titles) >= 5  # 2 conflict + ≥3 distractors
        distractors = t.params["distractor_titles"]
        assert len(distractors) >= 3


def test_list_conflicting_events_correct_pair_passes():
    random.seed(1)
    t = gen_list_conflicting_events()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [
        {"title": t.params["conflict_a"]},
        {"title": t.params["conflict_b"]},
    ]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is True


def test_list_conflicting_events_order_does_not_matter():
    random.seed(2)
    t = gen_list_conflicting_events()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [
        {"title": t.params["conflict_b"]},
        {"title": t.params["conflict_a"]},
    ]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is True


def test_list_conflicting_events_only_one_title_fails():
    # Agent reports only one of the conflicting events.
    random.seed(3)
    t = gen_list_conflicting_events()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{"title": t.params["conflict_a"]}]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_list_conflicting_events_dump_all_fails():
    # Cheat path: agent reports ALL event titles. set_equals catches
    # because the answer includes non-conflicting distractors.
    random.seed(4)
    t = gen_list_conflicting_events()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{"title": ti} for ti in t.params["all_titles"]]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_list_conflicting_events_wrong_pair_fails():
    # Agent reports the wrong pair (a distractor instead of one of
    # the conflict pair).
    random.seed(5)
    t = gen_list_conflicting_events()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [
        {"title": t.params["conflict_a"]},
        {"title": t.params["distractor_titles"][0]},
    ]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_list_conflicting_events_state_mutation_caught():
    # Agent answers correctly but ALSO moves one of the conflicting
    # events to "resolve" the overlap. attribute_eq on conflict_b's
    # start_iso catches the mutation.
    random.seed(6)
    t = gen_list_conflicting_events()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["conflict_b"]:
            # Shift by 2 hours
            old = e["start_iso"]
            hh = int(old[11:13])
            e["start_iso"] = old[:11] + f"{(hh + 2) % 24:02d}" + old[13:]
            break
    payload = {"items": [
        {"title": t.params["conflict_a"]},
        {"title": t.params["conflict_b"]},
    ]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_list_conflicting_events_observation_gate():
    random.seed(7)
    t = gen_list_conflicting_events()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [
        {"title": t.params["conflict_a"]},
        {"title": t.params["conflict_b"]},
    ]}
    passed, _ = _verify(reader, t, answer=payload,
                          observed=[], baseline=baseline)
    assert passed is False


def test_list_conflicting_events_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_list_conflicting_events().instruction)
    assert len(instructions) >= 2


def test_list_conflicting_events_end_shrink_caught():
    """C4 regression: agent reports the right pair AND shrinks B's
    end_iso to remove the actual overlap. attribute_eq on end_iso
    must catch this."""
    random.seed(60)
    t = gen_list_conflicting_events()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    # Shrink B's end_iso to before A's end → no overlap.
    for e in reader._events:
        if e["title"] == t.params["conflict_b"]:
            # Original end is conflict_hour+1:30; shrink to conflict_hour+0:45
            e["end_iso"] = e["end_iso"].replace(":30:00", ":00:00", 1)
            break
    payload = {"items": [
        {"title": t.params["conflict_a"]},
        {"title": t.params["conflict_b"]},
    ]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


# ═════════════════════════ gen_next_event_lookup ═══════════════════════════

def test_next_event_lookup_spec_and_lint():
    random.seed(1)
    t = gen_next_event_lookup()
    assert validate_spec(t.initial_state.spec) == []
    aa = next(c for c in t.verify_checks if c["kind"] == "agent_answer")
    assert lint_answer_instruction(t.instruction, aa) == []


def test_next_event_lookup_correct_answer_passes():
    random.seed(2)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{
        "title": t.params["target_title"],
        "start_local": t.params["target_start_local"],
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is True


def test_next_event_lookup_wrong_title_fails():
    random.seed(3)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{
        "title": "Some Other Event",
        "start_local": t.params["target_start_local"],
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_next_event_lookup_wrong_time_fails():
    random.seed(4)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    bad_hour = (t.params["target_hour"] + 1) % 24
    payload = {"items": [{
        "title": t.params["target_title"],
        "start_local": f"{bad_hour:02d}:00",
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_next_event_lookup_case_insensitive_title_passes():
    random.seed(5)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{
        "title": t.params["target_title"].upper(),
        "start_local": t.params["target_start_local"],
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is True


def test_next_event_lookup_pre_anchor_event_fails():
    # Cheat path: agent reports a pre-anchor event as "next".
    random.seed(6)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    pre_anchor_title = t.params["titles"][0]  # first hour = before anchor
    payload = {"items": [{
        "title": pre_anchor_title,
        "start_local": "09:00",   # arbitrary pre-anchor time
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_next_event_lookup_state_mutation_caught():
    # Agent moves an event to make it the "next" answer.
    random.seed(7)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    # Move the target event to a different hour.
    for e in reader._events:
        if e["title"] == t.params["target_title"]:
            old = e["start_iso"]
            hh = int(old[11:13])
            e["start_iso"] = old[:11] + f"{(hh + 3) % 24:02d}" + old[13:]
            break
    payload = {"items": [{
        "title": t.params["target_title"],
        "start_local": t.params["target_start_local"],
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False


def test_next_event_lookup_observation_gate():
    random.seed(8)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{
        "title": t.params["target_title"],
        "start_local": t.params["target_start_local"],
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload,
                          observed=[], baseline=baseline)
    assert passed is False


def test_next_event_lookup_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_next_event_lookup().instruction)
    assert len(instructions) >= 2


def test_next_event_lookup_target_is_smallest_after_anchor():
    """The target hour MUST be > anchor_hour, and SMALLEST among
    after-anchor hours seeded."""
    for seed in range(50):
        random.seed(seed)
        t = gen_next_event_lookup()
        assert t.params["target_hour"] > t.params["anchor_hour"]


def test_next_event_lookup_accepts_12h_pm_format():
    """C3: agent reads iOS Calendar AX showing '2:00 PM'. The
    time_keys canonicalization should accept both 12-hour and
    24-hour forms."""
    random.seed(50)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    hh = int(t.params["target_start_local"][:2])
    ampm = "AM" if hh < 12 else "PM"
    display_hh = hh % 12 or 12
    twelve_hour = f"{display_hh}:00 {ampm}"
    payload = {"items": [{
        "title": t.params["target_title"],
        "start_local": twelve_hour,
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is True


def test_next_event_lookup_accepts_narrow_space():
    """iOS AX uses U+202F narrow no-break space between time and AM/PM.
    Verifier normalizes to regular whitespace."""
    random.seed(51)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    hh = int(t.params["target_start_local"][:2])
    ampm = "AM" if hh < 12 else "PM"
    display_hh = hh % 12 or 12
    narrow_space = " "
    twelve_hour = f"{display_hh}:00{narrow_space}{ampm}"
    payload = {"items": [{
        "title": t.params["target_title"],
        "start_local": twelve_hour,
        "date_iso":    t.params["target_date_iso"],
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is True


def test_next_event_lookup_wrong_date_fails():
    """C2: agent reports a coincidentally-matching event on a
    DIFFERENT date. The date_iso item_key catches it."""
    random.seed(52)
    t = gen_next_event_lookup()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    payload = {"items": [{
        "title": t.params["target_title"],
        "start_local": t.params["target_start_local"],
        "date_iso":    "2099-12-31",
    }]}
    passed, _ = _verify(reader, t, answer=payload, baseline=baseline)
    assert passed is False
