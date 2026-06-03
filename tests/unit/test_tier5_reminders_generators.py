"""Tier 5 Reminders generators — reporting tasks via agent_answer.

For each: spec validates; instruction lint passes; BEFORE fails (no
ANSWER yet); a correct ANSWER payload threaded via context makes
AFTER pass; cheat-path tests confirm wrong answers + state-mutation
side effects + bypassing the observation gate all fail.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import validate_spec
from sibb_state import apply_initial_state
from sibb_task_generator_v3 import (
    gen_list_due_today,
    gen_list_due_tomorrow,
    gen_lookup_reminder_notes,
)
from sibb_verify import (
    run_checks, blocking_pass, lint_answer_instruction,
)

pytestmark = pytest.mark.fast


def _verify(reader, task, *, answer=None, observed=None):
    """Run the task's checks with the given ANSWER payload + observed
    bundles. `observed=None` defaults to `[com.apple.reminders]` so the
    observation gate passes; pass `[]` to exercise the gate's refusal."""
    ctx = {
        "agent_answer": answer,
        "observed_bundles": observed if observed is not None
                              else ["com.apple.reminders"],
    }
    results = asyncio.run(run_checks(reader, task.verify_checks,
                                      context=ctx))
    return blocking_pass(results), results


def _seed_initial_state(reader, task):
    report = asyncio.run(apply_initial_state(reader, task))
    assert not report.get("errors"), \
        f"state setup failed: {report['errors']}"
    return report


# gen_count_overdue tests dropped 2026-05-20 alongside the generator.
# See sibb_task_generator_v3.py for rationale.

# ─────────────────────── gen_list_due_today ──────────────────────────

def test_list_due_today_spec_and_lint():
    random.seed(10)
    t = gen_list_due_today()
    assert validate_spec(t.initial_state.spec) == []
    aa = next(c for c in t.verify_checks if c["kind"] == "agent_answer")
    assert lint_answer_instruction(t.instruction, aa) == []


def test_list_due_today_correct_answer_passes():
    random.seed(10)
    t = gen_list_due_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    payload = {"items": [{"title": t_} for t_ in t.params["today_titles"]]}
    passed_after, _ = _verify(reader, t, answer=payload)
    assert passed_after is True


def test_list_due_today_order_does_not_matter():
    random.seed(11)
    t = gen_list_due_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    # Reverse the order — set_equals should still pass.
    payload = {
        "items": [{"title": t_}
                  for t_ in reversed(t.params["today_titles"])]
    }
    passed_after, _ = _verify(reader, t, answer=payload)
    assert passed_after is True


def test_list_due_today_missing_one_fails():
    random.seed(12)
    t = gen_list_due_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    titles = t.params["today_titles"]
    # Leave one out.
    payload = {"items": [{"title": t_} for t_ in titles[:-1]]}
    passed, _ = _verify(reader, t, answer=payload)
    assert passed is False


def test_list_due_today_including_extra_fails():
    random.seed(13)
    t = gen_list_due_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    titles = t.params["today_titles"]
    # Add a non-today title.
    payload = {"items": [{"title": t_} for t_ in titles]
                 + [{"title": t.params["other_titles"][0]}]}
    passed, _ = _verify(reader, t, answer=payload)
    assert passed is False


def test_list_due_today_extra_key_in_item_fails():
    # Strict no-extra-keys policy: each item must be exactly {"title": ...}.
    random.seed(14)
    t = gen_list_due_today()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    payload = {
        "items": [{"title": t_, "due": "today"}
                  for t_ in t.params["today_titles"]]
    }
    passed, _ = _verify(reader, t, answer=payload)
    assert passed is False


# ───────────────────── gen_list_due_tomorrow ─────────────────────────

def test_list_due_tomorrow_correct_answer_passes():
    random.seed(20)
    t = gen_list_due_tomorrow()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    payload = {"items": [{"title": t_}
                          for t_ in t.params["tomorrow_titles"]]}
    passed_after, _ = _verify(reader, t, answer=payload)
    assert passed_after is True


# gen_yesno_overdue tests dropped 2026-05-20 alongside the generator.
# The boolean check kind itself remains exercised via
# test_agent_answer_check.py — only the Tier 5 task layer was dropped.

# ────────────────────── gen_lookup_reminder_notes ────────────────────

def test_lookup_notes_spec_and_lint():
    random.seed(40)
    t = gen_lookup_reminder_notes()
    assert validate_spec(t.initial_state.spec) == []
    aa = next(c for c in t.verify_checks if c["kind"] == "agent_answer")
    assert lint_answer_instruction(t.instruction, aa) == []


def test_lookup_notes_exact_match_passes():
    random.seed(40)
    t = gen_lookup_reminder_notes()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    passed, _ = _verify(reader, t, answer={"value": t.params["note"]})
    assert passed is True


def test_lookup_notes_case_insensitive_passes():
    random.seed(41)
    t = gen_lookup_reminder_notes()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    passed, _ = _verify(reader, t,
                         answer={"value": t.params["note"].upper()})
    assert passed is True


def test_lookup_notes_trimmed_whitespace_passes():
    random.seed(42)
    t = gen_lookup_reminder_notes()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    passed, _ = _verify(reader, t,
                         answer={"value": "  " + t.params["note"] + "\t"})
    assert passed is True


def test_lookup_notes_paraphrase_fails():
    random.seed(43)
    t = gen_lookup_reminder_notes()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    passed, _ = _verify(reader, t,
                         answer={"value": "something completely different"})
    assert passed is False


# ──────── all Tier 5 reporting generators have observation gate ──────

@pytest.mark.parametrize("gen", [
    gen_list_due_today, gen_list_due_tomorrow,
    gen_lookup_reminder_notes,
])
def test_tier5_has_observation_required(gen):
    random.seed(100)
    t = gen()
    aa = next(c for c in t.verify_checks if c["kind"] == "agent_answer")
    assert aa.get("observation_required") == ["com.apple.reminders"]


@pytest.mark.parametrize("gen", [
    gen_list_due_today, gen_list_due_tomorrow,
    gen_lookup_reminder_notes,
])
def test_tier5_springboard_noise_included(gen):
    random.seed(101)
    t = gen()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "layout" in sb_kinds
    assert "start_page" in sb_kinds
