"""Phase-B verifier — `agent_answer` check kind.

Pure-function tests on `_check_agent_answer` and its path / match-kind
machinery, plus the end-to-end `run_check` dispatch with
`resource="agent.answer"` and a synthetic context. Strict-by-design
behaviors get explicit coverage: case_sensitive=True default,
trim_strings=False default, extra-key rejection, no numeric coercion,
observation-gate refusal.
"""

from __future__ import annotations

import asyncio

import pytest

from sibb_verify import (
    _check_agent_answer,
    _walk_path,
    run_check,
)

pytestmark = pytest.mark.fast


# ───────────────────────────── path walker ───────────────────────────

def test_walk_path_root_returns_payload():
    assert _walk_path({"a": 1}, "$") == {"a": 1}


def test_walk_path_single_key():
    assert _walk_path({"items": [1, 2]}, "$.items") == [1, 2]


def test_walk_path_nested():
    assert _walk_path({"a": {"b": {"c": 7}}}, "$.a.b.c") == 7


def test_walk_path_missing_raises_keyerror():
    with pytest.raises(KeyError):
        _walk_path({"a": 1}, "$.b")


def test_walk_path_descend_into_non_dict_raises_type_error():
    with pytest.raises(TypeError):
        _walk_path({"items": [1, 2]}, "$.items.x")


# ───────────────────────────── no-answer ─────────────────────────────

def test_check_agent_answer_no_records_returns_no_answer():
    check = {"match": "number_eq", "expected": 5, "path": "$.count"}
    status, ev = _check_agent_answer([], check)
    assert status == "fail"
    assert ev["failure_kind"] == "no_answer"


# ───────────────────────────── set_equals ────────────────────────────

def _set_equals_check(expected, *, item_keys=None, path="$.items",
                      case_sensitive=True, trim_strings=False):
    c = {"match": "set_equals", "expected": expected, "path": path,
         "case_sensitive": case_sensitive, "trim_strings": trim_strings}
    if item_keys is not None:
        c["item_keys"] = item_keys
    return c


def test_set_equals_pass_order_insensitive():
    check = _set_equals_check(
        [{"title": "A"}, {"title": "B"}], item_keys=["title"])
    payload = {"items": [{"title": "B"}, {"title": "A"}]}
    status, ev = _check_agent_answer([payload], check)
    assert status == "pass"
    assert ev["matched"] == 2


def test_set_equals_fail_missing_item():
    check = _set_equals_check(
        [{"title": "A"}, {"title": "B"}], item_keys=["title"])
    payload = {"items": [{"title": "A"}]}
    status, ev = _check_agent_answer([payload], check)
    assert status == "fail"
    assert ev["failure_kind"] == "value_mismatch"


def test_set_equals_strict_rejects_extra_key():
    # User decision: extra keys in agent items must fail.
    check = _set_equals_check(
        [{"title": "Buy milk"}], item_keys=["title"])
    payload = {"items": [{"title": "Buy milk", "due": "tomorrow"}]}
    status, ev = _check_agent_answer([payload], check)
    assert status == "fail"
    assert ev["failure_kind"] == "extra_key"
    assert "due" in ev["extra"]


def test_set_equals_strict_rejects_missing_required_key():
    # Pure missing-key case: required ["title","list"], item has only
    # "title". A swap (item has only "name") would also be missing
    # "title" *and* extra "name" — that's schema_violation, covered
    # by its own test.
    check = _set_equals_check(
        [{"title": "Buy milk", "list": "Groceries"}],
        item_keys=["title", "list"])
    payload = {"items": [{"title": "Buy milk"}]}
    status, ev = _check_agent_answer([payload], check)
    assert status == "fail"
    assert ev["failure_kind"] == "missing_required_key"


def test_set_equals_swapped_keys_is_schema_violation():
    # Both an extra key AND a missing key — the combined case gets
    # the umbrella `schema_violation` failure_kind so it's
    # distinguishable from pure missing or pure extra in evidence.
    check = _set_equals_check(
        [{"title": "Buy milk"}], item_keys=["title"])
    payload = {"items": [{"name": "Buy milk"}]}
    status, ev = _check_agent_answer([payload], check)
    assert status == "fail"
    assert ev["failure_kind"] == "schema_violation"
    assert "name" in ev["extra"]
    assert "title" in ev["missing"]


def test_set_equals_scalar_items_when_no_item_keys():
    # If item_keys is omitted, comparison is over scalar items.
    check = _set_equals_check(["A", "B", "C"])
    payload = {"items": ["C", "A", "B"]}
    status, _ = _check_agent_answer([payload], check)
    assert status == "pass"


def test_set_equals_case_sensitive_by_default_fails_on_case_mismatch():
    check = _set_equals_check([{"title": "Buy Milk"}], item_keys=["title"])
    payload = {"items": [{"title": "buy milk"}]}
    status, ev = _check_agent_answer([payload], check)
    assert status == "fail"
    assert ev["failure_kind"] == "value_mismatch"


def test_set_equals_case_insensitive_opt_in_passes():
    check = _set_equals_check([{"title": "Buy Milk"}],
                                item_keys=["title"],
                                case_sensitive=False)
    payload = {"items": [{"title": "buy milk"}]}
    status, _ = _check_agent_answer([payload], check)
    assert status == "pass"


def test_set_equals_trim_strings_off_by_default_fails_on_whitespace():
    check = _set_equals_check([{"title": "Buy Milk"}], item_keys=["title"])
    payload = {"items": [{"title": "Buy Milk "}]}
    status, ev = _check_agent_answer([payload], check)
    assert status == "fail"


def test_set_equals_trim_strings_opt_in_passes():
    check = _set_equals_check([{"title": "Buy Milk"}],
                                item_keys=["title"],
                                trim_strings=True)
    payload = {"items": [{"title": " Buy Milk\t"}]}
    status, _ = _check_agent_answer([payload], check)
    assert status == "pass"


def test_set_equals_empty_expected_requires_empty_value():
    check = _set_equals_check([], item_keys=["title"])
    status, _ = _check_agent_answer([{"items": []}], check)
    assert status == "pass"
    status, _ = _check_agent_answer(
        [{"items": [{"title": "X"}]}], check)
    assert status == "fail"


def test_set_equals_non_list_value_fails_type():
    check = _set_equals_check([{"title": "A"}], item_keys=["title"])
    status, ev = _check_agent_answer([{"items": {"title": "A"}}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "type_mismatch"


# ─────────────────────────── ordered_match ───────────────────────────

def test_ordered_match_pass_exact_sequence():
    check = {"match": "ordered_match",
             "expected": ["A", "B", "C"],
             "path": "$.items",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer([{"items": ["A", "B", "C"]}], check)
    assert status == "pass"


def test_ordered_match_fail_wrong_order():
    check = {"match": "ordered_match",
             "expected": ["A", "B", "C"],
             "path": "$.items",
             "case_sensitive": True, "trim_strings": False}
    status, ev = _check_agent_answer([{"items": ["B", "A", "C"]}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "value_mismatch"
    assert ev["ordered"] is True


# ───────────────────────────── number_eq ─────────────────────────────

def test_number_eq_pass():
    check = {"match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer([{"count": 5}], check)
    assert status == "pass"


def test_number_eq_int_float_compatible():
    check = {"match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer([{"count": 5.0}], check)
    assert status == "pass"


def test_number_eq_rejects_string_coercion():
    # User decision: no numeric string coercion.
    check = {"match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False}
    status, ev = _check_agent_answer([{"count": "5"}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "type_mismatch"


def test_number_eq_rejects_bool_lookalike():
    # bool is a Python int subclass; the comparator excludes it so
    # True doesn't accidentally pass number_eq when expected is 1.
    check = {"match": "number_eq", "expected": 1, "path": "$.count",
             "case_sensitive": True, "trim_strings": False}
    status, ev = _check_agent_answer([{"count": True}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "type_mismatch"


# ─────────────────────────── number_close ────────────────────────────

def test_number_close_pass_within_tolerance():
    check = {"match": "number_close", "expected": 8500,
             "tolerance": 100, "path": "$.count",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer([{"count": 8450}], check)
    assert status == "pass"


def test_number_close_fail_outside_tolerance():
    check = {"match": "number_close", "expected": 8500,
             "tolerance": 100, "path": "$.count",
             "case_sensitive": True, "trim_strings": False}
    status, ev = _check_agent_answer([{"count": 9000}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "value_mismatch"


# ───────────────────────────── string_eq ─────────────────────────────

def test_string_eq_pass_exact():
    check = {"match": "string_eq", "expected": "Buy milk",
             "path": "$.value",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer([{"value": "Buy milk"}], check)
    assert status == "pass"


def test_string_eq_case_sensitive_default_fails_on_case():
    check = {"match": "string_eq", "expected": "Buy milk",
             "path": "$.value",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer([{"value": "buy milk"}], check)
    assert status == "fail"


def test_string_eq_rejects_non_string_value():
    check = {"match": "string_eq", "expected": "5", "path": "$.value",
             "case_sensitive": True, "trim_strings": False}
    status, ev = _check_agent_answer([{"value": 5}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "type_mismatch"


# ────────────────────────── string_contains ──────────────────────────

def test_string_contains_pass():
    check = {"match": "string_contains", "expected": "Paris",
             "path": "$.value",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer(
        [{"value": "Eiffel Tower in Paris, France"}], check)
    assert status == "pass"


def test_string_contains_fail_substring_missing():
    check = {"match": "string_contains", "expected": "Berlin",
             "path": "$.value",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer(
        [{"value": "Eiffel Tower in Paris, France"}], check)
    assert status == "fail"


# ─────────────────────────── string_regex ────────────────────────────

def test_string_regex_pass():
    check = {"match": "string_regex", "expected": r"\bParis\b",
             "path": "$.value",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer(
        [{"value": "Eiffel Tower in Paris, France"}], check)
    assert status == "pass"


def test_string_regex_invalid_pattern_raises():
    check = {"match": "string_regex", "expected": "[",
             "path": "$.value",
             "case_sensitive": True, "trim_strings": False}
    with pytest.raises(ValueError):
        _check_agent_answer([{"value": "x"}], check)


# ───────────────────────────── boolean ───────────────────────────────

def test_boolean_pass_true():
    check = {"match": "boolean", "expected": True, "path": "$.answer",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer([{"answer": True}], check)
    assert status == "pass"


def test_boolean_pass_false():
    check = {"match": "boolean", "expected": False, "path": "$.answer",
             "case_sensitive": True, "trim_strings": False}
    status, _ = _check_agent_answer([{"answer": False}], check)
    assert status == "pass"


def test_boolean_rejects_int_lookalike():
    check = {"match": "boolean", "expected": True, "path": "$.answer",
             "case_sensitive": True, "trim_strings": False}
    status, ev = _check_agent_answer([{"answer": 1}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "type_mismatch"


# ─────────────────────────── path failures ───────────────────────────

def test_path_miss_returns_failure_kind():
    check = {"match": "number_eq", "expected": 5, "path": "$.missing",
             "case_sensitive": True, "trim_strings": False}
    status, ev = _check_agent_answer([{"count": 5}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "path_miss"


def test_path_descent_into_non_dict_returns_path_miss():
    check = {"match": "number_eq", "expected": 5, "path": "$.items.x",
             "case_sensitive": True, "trim_strings": False}
    status, ev = _check_agent_answer([{"items": [1, 2]}], check)
    assert status == "fail"
    assert ev["failure_kind"] == "path_miss"


# ───────────────────────── observation gate ──────────────────────────

def test_observation_gate_missing_context_fails():
    check = {"match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False,
             "observation_required": ["com.apple.reminders"]}
    status, ev = _check_agent_answer(
        [{"count": 5}], check, observed_bundles=None)
    assert status == "fail"
    assert ev["failure_kind"] == "observation_data_missing"


def test_observation_gate_unmet_returns_no_evidence():
    check = {"match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False,
             "observation_required": ["com.apple.reminders"]}
    status, ev = _check_agent_answer(
        [{"count": 5}], check, observed_bundles=["com.apple.mobilecal"])
    assert status == "fail"
    assert ev["failure_kind"] == "no_evidence"
    assert ev["unmet"] == ["com.apple.reminders"]


def test_observation_gate_met_proceeds_to_match():
    check = {"match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False,
             "observation_required": ["com.apple.reminders"]}
    status, _ = _check_agent_answer(
        [{"count": 5}], check,
        observed_bundles=["com.apple.reminders", "com.apple.springboard"])
    assert status == "pass"


# ──────────────────────── run_check integration ──────────────────────

def test_run_check_routes_agent_answer_via_context():
    check = {"kind": "agent_answer", "resource": "agent.answer",
             "match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False}
    result = asyncio.run(run_check(
        None, check, context={"agent_answer": {"count": 5}}))
    assert result.status == "pass"


def test_run_check_with_no_answer_in_context_fails():
    check = {"kind": "agent_answer", "resource": "agent.answer",
             "match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False}
    result = asyncio.run(run_check(None, check, context={}))
    assert result.status == "fail"
    assert result.evidence["failure_kind"] == "no_answer"


def test_run_check_threads_observed_bundles():
    check = {"kind": "agent_answer", "resource": "agent.answer",
             "match": "number_eq", "expected": 5, "path": "$.count",
             "case_sensitive": True, "trim_strings": False,
             "observation_required": ["com.apple.reminders"]}
    result = asyncio.run(run_check(
        None, check,
        context={"agent_answer": {"count": 5},
                  "observed_bundles": ["com.apple.reminders"]}))
    assert result.status == "pass"


# ────────────────────── invalid check dispatch ───────────────────────

def test_invalid_match_kind_raises():
    check = {"match": "magic", "expected": 1, "path": "$.x",
             "case_sensitive": True, "trim_strings": False}
    with pytest.raises(ValueError):
        _check_agent_answer([{"x": 1}], check)


def test_missing_match_raises():
    check = {"expected": 1, "path": "$.x",
             "case_sensitive": True, "trim_strings": False}
    with pytest.raises(ValueError):
        _check_agent_answer([{"x": 1}], check)


def test_missing_expected_raises():
    check = {"match": "number_eq", "path": "$.x",
             "case_sensitive": True, "trim_strings": False}
    with pytest.raises(ValueError):
        _check_agent_answer([{"x": 1}], check)
