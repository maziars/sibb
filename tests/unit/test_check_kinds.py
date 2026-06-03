"""A6 — check-kind handlers in isolation.

Each handler is a pure function `(records, check) -> (status, evidence)`.
Tested here against synthetic record lists; integration with the
resource fetchers (the actual sibb_verify dispatcher) is covered in
the L1.5 verifier tests.
"""

from __future__ import annotations

import pytest

from sibb_verify import (
    CHECK_KINDS,
    _check_absent,
    _check_attribute_eq,
    _check_count,
    _check_exists,
    _check_subset,
)

pytestmark = pytest.mark.fast


# ─────────────────────────────── exists ────────────────────────────────

def test_exists_pass_when_records_present():
    status, ev = _check_exists([{"name": "A"}], {})
    assert status == "pass"
    assert ev["count"] == 1


def test_exists_fail_when_no_records():
    status, ev = _check_exists([], {"selector": {"name": "X"}})
    assert status == "fail"
    assert ev["count"] == 0
    assert ev["selector"] == {"name": "X"}


# ─────────────────────────────── absent ────────────────────────────────

def test_absent_pass_when_records_empty():
    status, ev = _check_absent([], {})
    assert status == "pass"


def test_absent_fail_includes_found_evidence():
    records = [{"name": "Leaked", "identifier": "id-1"}]
    status, ev = _check_absent(records, {})
    assert status == "fail"
    assert ev["count"] == 1
    assert ev["found"][0]["name"] == "Leaked"


def test_absent_evidence_caps_at_three_records():
    records = [{"title": f"r{i}", "identifier": f"id-{i}"} for i in range(10)]
    status, ev = _check_absent(records, {})
    assert status == "fail"
    assert len(ev["found"]) == 3


# ─────────────────────────────── count ─────────────────────────────────

@pytest.mark.parametrize("op,n,actual,expected_status", [
    ("eq", 3, 3, "pass"),
    ("eq", 3, 2, "fail"),
    ("eq", 0, 0, "pass"),
    ("ge", 2, 5, "pass"),
    ("ge", 2, 2, "pass"),
    ("ge", 2, 1, "fail"),
    ("le", 2, 1, "pass"),
    ("le", 2, 2, "pass"),
    ("le", 2, 3, "fail"),
])
def test_count_truth_table(op, n, actual, expected_status):
    records = [{"i": i} for i in range(actual)]
    status, _ = _check_count(records, {"op": op, "n": n})
    assert status == expected_status


def test_count_missing_n_raises():
    with pytest.raises(ValueError, match="`n`"):
        _check_count([], {"op": "eq"})


def test_count_invalid_op_raises():
    with pytest.raises(ValueError, match="op"):
        _check_count([], {"op": "spaceship", "n": 1})


# ────────────────────────── attribute_eq ──────────────────────────────

def test_attribute_eq_pass_when_all_match():
    records = [{"priority": 5}, {"priority": 5}]
    status, ev = _check_attribute_eq(records,
                                      {"attr": "priority", "value": 5})
    assert status == "pass"
    assert ev["checked_count"] == 2


def test_attribute_eq_fail_lists_mismatches():
    records = [{"priority": 5, "title": "ok"},
               {"priority": 1, "title": "bad"}]
    status, ev = _check_attribute_eq(records,
                                      {"attr": "priority", "value": 5})
    assert status == "fail"
    assert len(ev["mismatches"]) == 1
    assert ev["mismatches"][0]["actual"] == 1


def test_attribute_eq_fail_when_no_records():
    status, ev = _check_attribute_eq([],
                                      {"attr": "x", "value": 1})
    assert status == "fail"
    assert "no records match" in ev["error"]


def test_attribute_eq_missing_attr_raises():
    with pytest.raises(ValueError):
        _check_attribute_eq([{"x": 1}], {"value": 1})


def test_attribute_eq_missing_value_raises():
    with pytest.raises(ValueError):
        _check_attribute_eq([{"x": 1}], {"attr": "x"})


# ─────────── attribute_eq with dot-path attr (2026-05-20) ─────────────
# Lets generators express "this reminder has frequency=weekly" without
# locking down the whole `recurrence` dict shape.

def test_attribute_eq_dot_path_passes_on_leaf_match():
    records = [{"title": "Standup",
                "recurrence": {"frequency": "weekly", "interval": 1}}]
    status, _ = _check_attribute_eq(
        records,
        {"attr": "recurrence.frequency", "value": "weekly"})
    assert status == "pass"


def test_attribute_eq_dot_path_fails_on_leaf_mismatch():
    records = [{"title": "Standup",
                "recurrence": {"frequency": "monthly", "interval": 1}}]
    status, ev = _check_attribute_eq(
        records,
        {"attr": "recurrence.frequency", "value": "weekly"})
    assert status == "fail"
    assert ev["mismatches"][0]["actual"] == "monthly"


def test_attribute_eq_dot_path_missing_intermediate_is_none():
    # No `recurrence` key at all → dot-path resolves to None.
    records = [{"title": "One-off"}]
    status, _ = _check_attribute_eq(
        records,
        {"attr": "recurrence.frequency", "value": None})
    assert status == "pass"


def test_attribute_eq_dot_path_non_dict_intermediate_is_none():
    records = [{"title": "X", "recurrence": "not-a-dict"}]
    status, _ = _check_attribute_eq(
        records,
        {"attr": "recurrence.frequency", "value": None})
    assert status == "pass"


def test_attribute_eq_flat_attr_unchanged_by_dot_path_path():
    # Sanity: pre-existing flat-attr behavior still works.
    records = [{"title": "X", "completed": True}]
    status, _ = _check_attribute_eq(
        records, {"attr": "completed", "value": True})
    assert status == "pass"


# ─────────────────────────── attribute_exists ─────────────────────────

def _check_attribute_exists_imported():
    """Lazy import so the existing test module doesn't break if the
    new symbol is missing during a partial rollback."""
    from sibb_verify import _check_attribute_exists
    return _check_attribute_exists


def test_attribute_exists_pass_when_set():
    records = [{"title": "Standup",
                "recurrence": {"frequency": "weekly", "interval": 1}}]
    fn = _check_attribute_exists_imported()
    status, _ = fn(records, {"attr": "recurrence"})
    assert status == "pass"


def test_attribute_exists_fail_when_missing():
    records = [{"title": "One-off"}]
    fn = _check_attribute_exists_imported()
    status, ev = fn(records, {"attr": "recurrence"})
    assert status == "fail"
    assert ev["missing_on"][0]["identity"] == "One-off"


def test_attribute_exists_fail_when_value_is_none():
    records = [{"title": "Half-empty", "recurrence": None}]
    fn = _check_attribute_exists_imported()
    status, _ = fn(records, {"attr": "recurrence"})
    assert status == "fail"


def test_attribute_exists_dot_path():
    records = [{"title": "Standup",
                "recurrence": {"frequency": "weekly"}}]
    fn = _check_attribute_exists_imported()
    status, _ = fn(records, {"attr": "recurrence.frequency"})
    assert status == "pass"
    status, _ = fn(records, {"attr": "recurrence.end_iso"})
    assert status == "fail"


def test_attribute_exists_no_records_fails():
    fn = _check_attribute_exists_imported()
    status, _ = fn([], {"attr": "recurrence"})
    assert status == "fail"


def test_attribute_exists_missing_attr_raises():
    fn = _check_attribute_exists_imported()
    with pytest.raises(ValueError):
        fn([{"x": 1}], {})


# ─────────────────────────── attribute_absent ─────────────────────────

def _check_attribute_absent_imported():
    from sibb_verify import _check_attribute_absent
    return _check_attribute_absent


def test_attribute_absent_pass_when_missing():
    records = [{"title": "One-off"}]
    fn = _check_attribute_absent_imported()
    status, _ = fn(records, {"attr": "recurrence"})
    assert status == "pass"


def test_attribute_absent_fail_when_present():
    records = [{"title": "Standup",
                "recurrence": {"frequency": "weekly"}}]
    fn = _check_attribute_absent_imported()
    status, ev = fn(records, {"attr": "recurrence"})
    assert status == "fail"
    assert ev["present_on"][0]["identity"] == "Standup"


def test_attribute_absent_dot_path():
    # Recurrence is set but has no end_iso — absent passes.
    records = [{"title": "Standup",
                "recurrence": {"frequency": "weekly"}}]
    fn = _check_attribute_absent_imported()
    status, _ = fn(records, {"attr": "recurrence.end_iso"})
    assert status == "pass"


def test_attribute_absent_vacuously_passes_on_empty_records():
    fn = _check_attribute_absent_imported()
    status, _ = fn([], {"attr": "recurrence"})
    assert status == "pass"


def test_attribute_absent_missing_attr_raises():
    fn = _check_attribute_absent_imported()
    with pytest.raises(ValueError):
        fn([{"x": 1}], {})


# ─────────────────────────────── subset ────────────────────────────────

def test_subset_pass_when_all_expected_present():
    records = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    status, _ = _check_subset(records,
                               {"expected": ["a", "B"], "key": "title"})
    assert status == "pass"


def test_subset_case_insensitive():
    records = [{"title": "personal"}]
    status, _ = _check_subset(records,
                               {"expected": ["Personal"], "key": "title"})
    assert status == "pass"


def test_subset_fail_lists_missing():
    records = [{"title": "A"}]
    status, ev = _check_subset(records,
                                {"expected": ["A", "B", "C"], "key": "title"})
    assert status == "fail"
    assert set(ev["missing"]) == {"b", "c"}
    assert ev["expected_count"] == 3


def test_subset_missing_key_raises():
    with pytest.raises(ValueError):
        _check_subset([], {"expected": ["a"]})


# ────────────────────────── registry membership ───────────────────────

def test_all_kinds_registered():
    expected = {"exists", "absent", "count", "attribute_eq", "subset"}
    assert expected.issubset(set(CHECK_KINDS))


def test_no_check_kind_handler_is_async():
    # Handlers must be synchronous — the dispatcher calls them after
    # awaiting the resource fetcher. If a handler ever needs async
    # work it should go via a new resource fetcher, not by making
    # the kind handler async.
    import inspect
    for name, fn in CHECK_KINDS.items():
        assert not inspect.iscoroutinefunction(fn), (
            f"CHECK_KINDS[{name!r}] must be a sync function"
        )
