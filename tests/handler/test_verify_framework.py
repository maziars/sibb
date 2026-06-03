"""A6 — `run_checks` dispatcher end-to-end against FakeXCUITestReader.

Exercises selector matching, resource-fetcher routing, error
propagation, severity aggregation, and the legacy-format translator
together. Each check returns a structured `CheckResult` and
`blocking_pass` correctly aggregates only blocking severities.
"""

from __future__ import annotations

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_verify import (
    CheckResult,
    blocking_pass,
    legacy_format,
    run_check,
    run_checks,
)

pytestmark = pytest.mark.fake_reader


async def _seeded_reader():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "Personal"})
    await r._send({"type": "create_reminder",
                   "title": "Buy milk", "list": "Personal",
                   "priority": "medium"})
    await r._send({"type": "create_reminder",
                   "title": "Finish report", "list": "Personal",
                   "priority": "high"})
    await r._send({"type": "create_list", "name": "Work"})
    return r


# ─────────────────────────── dispatcher happy ─────────────────────────

async def test_exists_check_passes_for_existing_list():
    r = await _seeded_reader()
    result = await run_check(r, {
        "kind": "exists", "resource": "reminders.lists",
        "selector": {"name": "Personal"},
    })
    assert isinstance(result, CheckResult)
    assert result.status == "pass"
    assert result.severity == "blocking"
    assert result.evidence["count"] == 1


async def test_exists_check_is_case_insensitive():
    r = await _seeded_reader()
    result = await run_check(r, {
        "kind": "exists", "resource": "reminders.lists",
        "selector": {"name": "PERSONAL"},
    })
    assert result.status == "pass"


async def test_exists_fails_for_missing_list():
    r = await _seeded_reader()
    result = await run_check(r, {
        "kind": "exists", "resource": "reminders.lists",
        "selector": {"name": "NonExistent"},
    })
    assert result.status == "fail"
    assert result.evidence["count"] == 0


async def test_count_check_against_items():
    r = await _seeded_reader()
    result = await run_check(r, {
        "kind": "count", "resource": "reminders.items",
        "selector": {"list": "Personal"},
        "op": "eq", "n": 2,
    })
    assert result.status == "pass"


async def test_attribute_eq_priority():
    r = await _seeded_reader()
    result = await run_check(r, {
        "kind": "attribute_eq", "resource": "reminders.items",
        "selector": {"list": "Personal", "title": "Finish report"},
        "attr": "priority", "value": 1,    # EventKit "high"
    })
    assert result.status == "pass"


async def test_subset_check_items_in_list():
    r = await _seeded_reader()
    result = await run_check(r, {
        "kind": "subset", "resource": "reminders.items",
        "selector": {"list": "Personal"},
        "key": "title", "expected": ["Buy milk", "Finish report"],
    })
    assert result.status == "pass"


async def test_absent_check_passes_when_list_truly_absent():
    r = await _seeded_reader()
    result = await run_check(r, {
        "kind": "absent", "resource": "reminders.lists",
        "selector": {"name": "Imaginary"},
    })
    assert result.status == "pass"


# ─────────────────────────── error paths ──────────────────────────────

async def test_unknown_kind_returns_error_status():
    r = await _seeded_reader()
    result = await run_check(r, {"kind": "spaceship",
                                  "resource": "reminders.lists"})
    assert result.status == "error"
    assert "unknown check kind" in result.evidence["error"]


async def test_unknown_resource_returns_error_status():
    r = await _seeded_reader()
    result = await run_check(r, {"kind": "exists",
                                  "resource": "imaginary.things"})
    assert result.status == "error"
    assert "unknown resource" in result.evidence["error"]


async def test_malformed_check_param_returns_error_status():
    r = await _seeded_reader()
    result = await run_check(r, {
        "kind": "attribute_eq", "resource": "reminders.items",
        # missing `attr` and `value`
    })
    assert result.status == "error"


async def test_socket_failure_surfaces_as_error_status():
    # FakeXCUITestReader returns ok=false for unknown command types;
    # our resource fetcher reads `list_lists` so this case doesn't
    # naturally arise. Simulate by stubbing _send to return an
    # error from list_lists.
    class FailingReader:
        async def _send(self, cmd):
            return {"ok": False, "error": "simulated transport failure"}

    result = await run_check(FailingReader(), {
        "kind": "exists", "resource": "reminders.lists",
        "selector": {"name": "X"},
    })
    assert result.status == "error"
    assert "transport failure" in result.evidence["error"]


# ─────────────────── severity + aggregation ───────────────────────────

async def test_blocking_pass_true_when_all_blocking_pass():
    r = await _seeded_reader()
    results = await run_checks(r, [
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": "Personal"}, "severity": "blocking"},
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": "Work"}, "severity": "blocking"},
    ])
    assert blocking_pass(results) is True


async def test_blocking_pass_false_when_any_blocking_fails():
    r = await _seeded_reader()
    results = await run_checks(r, [
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": "Personal"}, "severity": "blocking"},
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": "Imaginary"}, "severity": "blocking"},
    ])
    assert blocking_pass(results) is False


async def test_informational_failures_do_not_gate_blocking_pass():
    r = await _seeded_reader()
    results = await run_checks(r, [
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": "Personal"}, "severity": "blocking"},
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": "NotThere"}, "severity": "informational"},
    ])
    assert blocking_pass(results) is True


async def test_blocking_error_counts_as_failure():
    r = await _seeded_reader()
    results = await run_checks(r, [
        {"kind": "spaceship", "resource": "reminders.lists",
         "severity": "blocking"},
    ])
    assert blocking_pass(results) is False


# ──────────────────────── legacy format ───────────────────────────────

def test_legacy_format_translation():
    results = [
        CheckResult(kind="exists", label="A", status="pass",
                     severity="blocking"),
        CheckResult(kind="exists", label="B", status="fail",
                     severity="blocking"),
        CheckResult(kind="exists", label="C", status="pass",
                     severity="informational"),
        CheckResult(kind="exists", label="D", status="error",
                     severity="blocking"),
    ]
    out = legacy_format(results)
    assert out == [("A", True), ("B", False), ("C", None), ("D", False)]
