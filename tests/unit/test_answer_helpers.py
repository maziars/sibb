"""Phase-B generator helpers — make_answer_check / describe_answer_format
/ lint_answer_instruction.

These keep the agent-facing instruction sentence and the verifier-side
check dict aligned. The lint is the safety net that fails loud at task
construction time if a generator forgets to splice the schema sentence
into its instruction.
"""

from __future__ import annotations

import pytest

from sibb_verify import (
    make_answer_check,
    describe_answer_format,
    lint_answer_instruction,
)

pytestmark = pytest.mark.fast


# ───────────────────────── make_answer_check ─────────────────────────

def test_make_answer_check_set_equals_returns_dict_and_sentence():
    check, schema = make_answer_check(
        match="set_equals",
        expected=[{"title": "Buy milk"}],
        path="$.items",
        item_keys=["title"],
    )
    assert check["kind"] == "agent_answer"
    assert check["resource"] == "agent.answer"
    assert check["match"] == "set_equals"
    assert check["item_keys"] == ["title"]
    assert check["severity"] == "blocking"
    assert "ANSWER" in schema
    assert '"items"' in schema


def test_make_answer_check_passes_observation_required():
    check, _ = make_answer_check(
        match="number_eq", expected=5, path="$.count",
        observation_required=["com.apple.reminders"],
    )
    assert check["observation_required"] == ["com.apple.reminders"]


def test_make_answer_check_rejects_unknown_match():
    with pytest.raises(ValueError, match="match must be one of"):
        make_answer_check(match="magic", expected=1, path="$.x")


def test_make_answer_check_number_close_requires_tolerance():
    with pytest.raises(ValueError, match="number_close requires `tolerance`"):
        make_answer_check(match="number_close", expected=5, path="$.count")


def test_make_answer_check_set_equals_requires_list_expected():
    with pytest.raises(ValueError, match="expected must be a list"):
        make_answer_check(match="set_equals", expected={"title": "X"},
                            path="$.items")


def test_make_answer_check_boolean_requires_bool_expected():
    with pytest.raises(ValueError, match="boolean expected must be a bool"):
        make_answer_check(match="boolean", expected=1, path="$.answer")


def test_make_answer_check_strict_defaults():
    # case_sensitive defaults to True, trim_strings to False — the
    # strict-by-default policy. Generators opt into leniency.
    check, _ = make_answer_check(match="string_eq", expected="X",
                                   path="$.value")
    assert check["case_sensitive"] is True
    assert check["trim_strings"] is False


# ────────────────────── describe_answer_format ───────────────────────

def test_describe_set_equals_mentions_no_order():
    check, _ = make_answer_check(
        match="set_equals",
        expected=[{"title": "X"}],
        path="$.items", item_keys=["title"])
    s = describe_answer_format(check)
    assert "order" in s.lower()
    assert "does NOT matter" in s


def test_describe_ordered_match_mentions_order():
    check, _ = make_answer_check(
        match="ordered_match",
        expected=["A", "B"],
        path="$.items")
    s = describe_answer_format(check)
    assert "order of items matters" in s.lower()


def test_describe_set_equals_warns_about_extra_keys():
    check, _ = make_answer_check(
        match="set_equals",
        expected=[{"title": "X"}],
        path="$.items", item_keys=["title"])
    s = describe_answer_format(check)
    # 2026-05-20: phrasing replaced "Extra keys ... rejected" with
    # the positive form "Use ONLY these keys" — semantically the same
    # constraint, more LLM-friendly. The lint that pairs instruction
    # ↔ schema still substring-matches the canonical sentence; this
    # test just confirms the no-extras intent is communicated.
    assert "Use ONLY these keys" in s


def test_describe_number_says_emit_number_not_string():
    check, _ = make_answer_check(
        match="number_eq", expected=5, path="$.count")
    s = describe_answer_format(check)
    assert "JSON number" in s
    # 2026-05-20: phrasing now reads "NOT a string in quotes" — the
    # earlier "not a string" substring is gone but the intent is the
    # same. Test against the new canonical text.
    assert "NOT a string" in s


def test_describe_boolean_shows_both_values():
    check, _ = make_answer_check(
        match="boolean", expected=True, path="$.answer")
    s = describe_answer_format(check)
    assert "true" in s
    assert "false" in s


def test_describe_unknown_match_raises():
    bad_check = {"kind": "agent_answer", "match": "magic", "path": "$"}
    with pytest.raises(ValueError):
        describe_answer_format(bad_check)


# ────────────────────── lint_answer_instruction ──────────────────────

def test_lint_passes_when_schema_spliced():
    check, schema = make_answer_check(
        match="number_eq", expected=5, path="$.count")
    instr = "How many reminders are overdue? " + schema
    assert lint_answer_instruction(instr, check) == []


def test_lint_fails_when_answer_token_missing():
    check, _ = make_answer_check(
        match="number_eq", expected=5, path="$.count")
    errors = lint_answer_instruction("Tell me the count.", check)
    assert any("ANSWER" in e for e in errors)


def test_lint_fails_when_schema_sentence_not_spliced():
    check, _ = make_answer_check(
        match="number_eq", expected=5, path="$.count")
    # Instruction mentions ANSWER but not the canonical schema sentence.
    errors = lint_answer_instruction(
        "Tell me the count. ANSWER {count: number}", check)
    assert any("schema sentence" in e for e in errors)


def test_lint_inert_for_non_answer_checks():
    # The lint runs over agent_answer checks only. A state-check dict
    # (e.g., `exists`) passes through silently.
    check = {"kind": "exists", "resource": "reminders.items"}
    assert lint_answer_instruction("Anything", check) == []
