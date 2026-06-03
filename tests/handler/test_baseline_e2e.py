"""A7 — `BaselineSnapshot.capture` + `identity` end-to-end.

Drives the whole flow against FakeXCUITestReader: capture baseline →
mutate state (or not) → run identity check → assert it caught (or
missed) the change.
"""

from __future__ import annotations

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_verify import (
    BaselineSnapshot,
    blocking_pass,
    run_check,
    run_checks,
)

pytestmark = pytest.mark.fake_reader


async def _seeded():
    r = FakeXCUITestReader()
    await r._send({"type": "create_list", "name": "Personal"})
    await r._send({"type": "create_list", "name": "Work"})
    await r._send({"type": "create_reminder",
                   "title": "Buy milk", "list": "Personal"})
    return r


# ──────────────────── capture happy paths ────────────────────────────

async def test_capture_records_unfiltered_resource_state():
    r = await _seeded()
    b = await BaselineSnapshot.capture(
        r, ["reminders.lists", "reminders.items"])

    assert "reminders.lists" in b.resources
    assert "reminders.items" in b.resources
    names = {L["name"] for L in b.resources["reminders.lists"]}
    assert "Personal" in names
    assert "Work" in names
    # System "Reminders" list is also captured (it's part of state).
    assert "Reminders" in names

    titles = {x["title"] for x in b.resources["reminders.items"]}
    assert "Buy milk" in titles


async def test_capture_unknown_resource_raises_valueerror():
    r = await _seeded()
    with pytest.raises(ValueError, match="unknown resource"):
        await BaselineSnapshot.capture(r, ["photos.albums"])


async def test_capture_records_timestamp():
    import time
    r = await _seeded()
    before = time.time()
    b = await BaselineSnapshot.capture(r, ["reminders.lists"])
    after = time.time()
    assert before <= b.captured_at <= after


# ───────────────── identity end-to-end through dispatcher ────────────

async def test_identity_passes_when_state_unchanged():
    r = await _seeded()
    b = await BaselineSnapshot.capture(r, ["reminders.lists"])

    result = await run_check(r, {
        "kind": "identity", "resource": "reminders.lists",
    }, baseline=b)
    assert result.status == "pass"
    assert result.evidence["method"] == "identifiers"


async def test_identity_fails_when_list_added_after_baseline():
    r = await _seeded()
    b = await BaselineSnapshot.capture(r, ["reminders.lists"])

    await r._send({"type": "create_list", "name": "Sneaky"})

    result = await run_check(r, {
        "kind": "identity", "resource": "reminders.lists",
    }, baseline=b)
    assert result.status == "fail"
    assert result.evidence["current_count"] > result.evidence["baseline_count"]


async def test_identity_fails_when_list_removed_after_baseline():
    r = await _seeded()
    b = await BaselineSnapshot.capture(r, ["reminders.lists"])

    await r._send({"type": "wipe_reminders"})

    result = await run_check(r, {
        "kind": "identity", "resource": "reminders.lists",
    }, baseline=b)
    assert result.status == "fail"
    # wipe_reminders removes user lists; baseline had Personal+Work
    assert result.evidence["current_count"] < result.evidence["baseline_count"]


async def test_identity_via_run_checks_thread_baseline():
    r = await _seeded()
    b = await BaselineSnapshot.capture(
        r, ["reminders.lists", "reminders.items"])
    # Mutate items but not lists; identity on lists passes,
    # on items fails.
    await r._send({"type": "create_reminder",
                    "title": "After baseline", "list": "Personal"})

    results = await run_checks(r, [
        {"kind": "identity", "resource": "reminders.lists",
         "severity": "informational",
         "label": "lists unchanged"},
        {"kind": "identity", "resource": "reminders.items",
         "severity": "blocking",
         "label": "items unchanged"},
    ], baseline=b)

    by_label = {res.label: res for res in results}
    assert by_label["lists unchanged"].status == "pass"
    assert by_label["items unchanged"].status == "fail"
    assert blocking_pass(results) is False


async def test_identity_without_baseline_returns_error_status():
    r = await _seeded()
    result = await run_check(r, {
        "kind": "identity", "resource": "reminders.lists",
    })   # no baseline=
    assert result.status == "error"
    assert "baseline" in result.evidence["error"]


async def test_non_identity_checks_ignore_baseline():
    # Sanity that existing kinds still work when baseline=None
    # (no regression in A6 dispatch).
    r = await _seeded()
    b = await BaselineSnapshot.capture(r, ["reminders.lists"])

    # exists should pass with or without baseline.
    result_no_baseline = await run_check(r, {
        "kind": "exists", "resource": "reminders.lists",
        "selector": {"name": "Personal"},
    })
    result_with_baseline = await run_check(r, {
        "kind": "exists", "resource": "reminders.lists",
        "selector": {"name": "Personal"},
    }, baseline=b)
    assert result_no_baseline.status == "pass"
    assert result_with_baseline.status == "pass"
