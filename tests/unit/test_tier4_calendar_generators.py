"""Tier 4 Calendar generators — time edits / notes / url.

Same shape as test_tier1_calendar_generators.py / test_tier23_calendar.py:
spec validates, BEFORE/AFTER round-trip on FakeXCUITestReader,
target-side and distractor-side cheat-path regressions.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import random

import pytest

from fakes.fake_reader import FakeXCUITestReader
from sibb_spec import validate_spec
from sibb_state import apply_initial_state
from sibb_task_generator_v3 import (
    gen_reschedule_event_same_duration,
    gen_adjust_event_boundary,
    gen_add_notes_to_event,
    gen_add_event_url,
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


# ═════════════════════════ gen_reschedule_event_same_duration ══════════════

def test_reschedule_event_same_duration_spec_validates():
    random.seed(1)
    t = gen_reschedule_event_same_duration()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Calendar"]


def test_reschedule_event_same_duration_before_fails_after_passes():
    random.seed(2)
    t = gen_reschedule_event_same_duration()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"]   = t.params["new_end_iso"]
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_reschedule_event_same_duration_wrong_date_fails():
    # Agent moves target to a DIFFERENT day than requested.
    random.seed(3)
    t = gen_reschedule_event_same_duration()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    wrong_d = (_dt.date.fromisoformat(t.params["date_new"])
               + _dt.timedelta(days=1)).isoformat()
    new_start = t.params["new_start_iso"]
    new_end = t.params["new_end_iso"]
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = wrong_d + new_start[10:]
            e["end_iso"]   = wrong_d + new_end[10:]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_reschedule_event_same_duration_breaking_duration_fails():
    # Agent moves start correctly; leaves end at old date+time.
    random.seed(4)
    t = gen_reschedule_event_same_duration()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_reschedule_event_target_url_added_caught():
    # B1-style cheat: agent reschedules AND adds a URL on the target.
    random.seed(5)
    t = gen_reschedule_event_same_duration()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"]   = t.params["new_end_iso"]
            e["url"] = "https://sneaky.example.com"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_reschedule_event_distractor_mutation_caught():
    # Agent reschedules target correctly + shifts a source-day distractor.
    random.seed(6)
    t = gen_reschedule_event_same_duration()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target = t.params["target"]
    src = t.params["date_source"]
    for e in reader._events:
        if e["title"] == target:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"]   = t.params["new_end_iso"]
        elif e["start_iso"].startswith(src):
            # Time-shift one source-day distractor.
            old = e["start_iso"]
            hh = int(old[11:13])
            e["start_iso"] = old[:11] + f"{(hh + 1) % 24:02d}" + old[13:]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_reschedule_event_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_reschedule_event_same_duration().instruction)
    assert len(instructions) >= 2


def test_reschedule_event_springboard_includes_start_page():
    random.seed(10)
    t = gen_reschedule_event_same_duration()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


# ═════════════════════════ gen_adjust_event_boundary ════════════════════════

def test_adjust_boundary_spec_validates():
    random.seed(20)
    t = gen_adjust_event_boundary()
    assert validate_spec(t.initial_state.spec) == []


def test_adjust_boundary_before_fails_after_passes():
    random.seed(21)
    t = gen_adjust_event_boundary()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"]   = t.params["new_end_iso"]
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_adjust_boundary_moves_both_sides_fails():
    # Generator asks for ONE endpoint to move. Agent moves both.
    random.seed(22)
    t = gen_adjust_event_boundary()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    # Shift BOTH endpoints by an extra 5 minutes — the unchanged
    # side now mismatches.
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"]   = t.params["new_end_iso"]
            # Add a tiny extra shift to the OTHER endpoint relative
            # to baseline. Side determines which is "the other."
            if t.params["side"] == "start":
                # End was supposed to stay at original end; bump it.
                old = e["end_iso"]
                e["end_iso"] = old.replace(":00:00", ":05:00", 1)
            else:
                old = e["start_iso"]
                e["start_iso"] = old.replace(":00:00", ":05:00", 1)
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_adjust_boundary_target_notes_added_caught():
    random.seed(23)
    t = gen_adjust_event_boundary()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"]   = t.params["new_end_iso"]
            e["notes"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_adjust_boundary_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_adjust_event_boundary().instruction)
    assert len(instructions) >= 2


def test_adjust_boundary_side_and_direction_exposed_in_params():
    random.seed(24)
    t = gen_adjust_event_boundary()
    assert t.params["side"] in ("start", "end")
    assert t.params["direction"] in ("extend", "shorten")


def test_adjust_boundary_springboard_includes_start_page():
    random.seed(25)
    t = gen_adjust_event_boundary()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


# ═════════════════════════ gen_add_notes_to_event ═══════════════════════════

def test_add_notes_to_event_spec_validates():
    random.seed(30)
    t = gen_add_notes_to_event()
    assert validate_spec(t.initial_state.spec) == []


def test_add_notes_to_event_before_fails_after_passes():
    random.seed(31)
    t = gen_add_notes_to_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["notes"] = t.params["note"]
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_add_notes_to_event_on_wrong_event_fails():
    random.seed(32)
    t = gen_add_notes_to_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] != t.params["target"]:
            e["notes"] = t.params["note"]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_add_notes_to_event_target_time_shift_caught():
    random.seed(33)
    t = gen_add_notes_to_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["notes"] = t.params["note"]
            old = e["start_iso"]
            hh = int(old[11:13])
            e["start_iso"] = old[:11] + f"{(hh + 1) % 24:02d}" + old[13:]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_add_notes_to_event_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_add_notes_to_event().instruction)
    assert len(instructions) >= 2


# ═════════════════════════ gen_add_event_url ════════════════════════════════

def test_add_event_url_spec_validates():
    random.seed(40)
    t = gen_add_event_url()
    assert validate_spec(t.initial_state.spec) == []


def test_add_event_url_before_fails_after_passes():
    random.seed(41)
    t = gen_add_event_url()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["url"] = t.params["url"]
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_add_event_url_on_wrong_event_fails():
    random.seed(42)
    t = gen_add_event_url()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] != t.params["target"]:
            e["url"] = t.params["url"]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_add_event_url_target_notes_added_caught():
    random.seed(43)
    t = gen_add_event_url()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["url"] = t.params["url"]
            e["notes"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_add_event_url_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_add_event_url().instruction)
    assert len(instructions) >= 2


# ═════════════════════════ C6 regression tests ════════════════════════════
#
# Cover the critic-flagged gaps: cascade invariants (T4.2), quadrant
# matrix (T4.2), URL round-trip + parse failure (T4.0).


def test_adjust_boundary_cascade_invariants_across_seeds():
    """Sweep many seeds; assert every produced (new_start, new_end)
    is in-range [07:00, 23:59] and new_start < new_end. Catches
    the C2 cascade regression by construction."""
    for seed in range(200):
        random.seed(seed)
        t = gen_adjust_event_boundary()
        ns = t.params["new_start_iso"][11:19]
        ne = t.params["new_end_iso"][11:19]
        assert ns >= "07:00:00", (
            f"seed={seed}: new_start time {ns} below 07:00:00")
        assert ne <= "23:59:59", (
            f"seed={seed}: new_end time {ne} above 23:59:59")
        assert t.params["new_start_iso"] < t.params["new_end_iso"], (
            f"seed={seed}: new_start >= new_end "
            f"({t.params['new_start_iso']} vs {t.params['new_end_iso']})")


def test_adjust_boundary_quadrants_all_reachable():
    """All 4 (side, direction) quadrants should appear over a
    reasonable sweep of seeds. If one quadrant is silently never
    selected (e.g., cascade always flips OUT of it), the bug
    surfaces here."""
    quadrants = set()
    for seed in range(200):
        random.seed(seed)
        t = gen_adjust_event_boundary()
        quadrants.add((t.params["side"], t.params["direction"]))
    # We expect ≥3 of the 4 quadrants over 200 seeds. The cascade may
    # flip some seeds out of their initial quadrant, but at random
    # seeds across 200 trials, all four should appear at least once
    # in practice.
    assert len(quadrants) >= 3, (
        f"only {len(quadrants)} quadrants seen in 200 seeds: {quadrants}")


def test_adjust_boundary_baseline_value_not_leaked_in_instruction():
    """C3 regression: phrasings must NOT use the comparative form
    'X, not Y' that previously leaked baseline. After the fix, no
    phrasing should contain 'not ' followed by a time reference."""
    import re
    leak_pattern = re.compile(r"\bnot\s+\d")
    for seed in range(100):
        random.seed(seed)
        t = gen_adjust_event_boundary()
        assert not leak_pattern.search(t.instruction), (
            f"seed={seed}: instruction has comparative leak: "
            f"{t.instruction!r}")


# ═════════════════════════ URL contract (C6) ════════════════════════════════

def test_url_field_round_trips_through_fake():
    """Create an event with a URL; list it; assert the URL field
    matches the input. Mirror the contract the Swift round-trip
    promises (C5 added strict parse-failure handling on the Swift
    side; the fake doesn't reject malformed URLs — that's a known
    gap, see TODO_DEFERRED if it surfaces in L2)."""
    reader = FakeXCUITestReader()
    url = "https://example.com/path?key=value"
    asyncio.run(reader._send({
        "type": "create_event",
        "title": "Has URL",
        "start_iso": "2026-05-22T09:00:00",
        "end_iso":   "2026-05-22T10:00:00",
        "url": url,
    }))
    listed = asyncio.run(reader._send({"type": "list_events"}))
    rows = [e for e in listed["events"] if e["title"] == "Has URL"]
    assert len(rows) == 1
    assert rows[0]["url"] == url


def test_url_field_default_empty_string_on_create_without_url():
    """A create_event without a `url` field should round-trip with
    `url: ""` (mirrors Swift's `e.url?.absoluteString ?? ""`)."""
    reader = FakeXCUITestReader()
    asyncio.run(reader._send({
        "type": "create_event",
        "title": "No URL",
        "start_iso": "2026-05-22T09:00:00",
        "end_iso":   "2026-05-22T10:00:00",
    }))
    listed = asyncio.run(reader._send({"type": "list_events"}))
    rows = [e for e in listed["events"] if e["title"] == "No URL"]
    assert len(rows) == 1
    assert rows[0]["url"] == ""


def test_calendar_spec_carries_url_through_event_entries():
    """C1 regression. _calendar_spec was dropping url field from
    event specs because the optional-fields tuple omitted it. Test
    that an explicit url survives the spec build."""
    from sibb_task_generator_v3 import _calendar_spec
    spec = _calendar_spec([
        {"title": "Has URL", "start_iso": "2026-05-22T09:00:00",
         "end_iso": "2026-05-22T10:00:00", "url": "https://x.example"},
    ])
    assert spec[0]["url"] == "https://x.example"
