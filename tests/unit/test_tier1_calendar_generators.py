"""Tier 1 Calendar generators — full BEFORE/AFTER round-trip on the
FakeXCUITestReader.

Layered tests per generator:
  1. Spec validates (typed-spec shape).
  2. Apply spec via dispatcher to seed the fake.
  3. Capture baseline (identity checks require it).
  4. Verifier BEFORE — must FAIL (blocking).
  5. Simulate agent action against fake.
  6. Verifier AFTER — must PASS (blocking).
  7. Cheat-path tests: edit-wrong-event / partial mutation must FAIL.

L1.5 in spirit but lives under unit/ since no simulator boot. Catches
generator/verifier drift in ms.
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
    gen_create_event_with_title_time,
    gen_delete_specific_event,
    gen_change_event_title,
    gen_set_event_location,
    gen_change_event_time,
    gen_toggle_event_all_day,
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


# ═════════════════════════ gen_create_event_with_title_time ═════════════════

def test_create_event_spec_validates():
    random.seed(1)
    t = gen_create_event_with_title_time()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Calendar"]
    assert all(c["severity"] == "blocking" for c in t.verify_checks)


def test_create_event_before_fails_after_passes():
    random.seed(2)
    t = gen_create_event_with_title_time()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    # BEFORE — no event yet, verifier should fail
    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    # Agent creates the requested event in the fake.
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["title"],
        "start_iso": t.params["start_iso"],
        "end_iso": t.params["end_iso"],
    }))

    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_create_event_with_extra_event_fails():
    # Agent creates the right event AND a spurious one.
    random.seed(3)
    t = gen_create_event_with_title_time()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["title"],
        "start_iso": t.params["start_iso"],
        "end_iso": t.params["end_iso"],
    }))
    asyncio.run(reader._send({
        "type": "create_event",
        "title": "Spurious",
        "start_iso": "2026-06-01T20:00:00",
        "end_iso":   "2026-06-01T20:30:00",
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_create_event_wrong_time_fails():
    random.seed(4)
    t = gen_create_event_with_title_time()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    # Off by one hour. Use explicit substring slicing rather than
    # str.replace because the hour digit can be anywhere in 7..20 now
    # that _calendar_anchor_date randomizes the day (and indirectly the
    # picked hour). start_iso is "YYYY-MM-DDTHH:MM:SS" — index 11..13
    # is HH.
    good_start = t.params["start_iso"]
    hh = int(good_start[11:13])
    bad_hh = (hh + 1) % 24
    bad_start = good_start[:11] + f"{bad_hh:02d}" + good_start[13:]
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["title"],
        "start_iso": bad_start,
        "end_iso": t.params["end_iso"],
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


# ═════════════════════════ gen_delete_specific_event ════════════════════════

def test_delete_specific_event_spec_validates():
    random.seed(10)
    t = gen_delete_specific_event()
    assert validate_spec(t.initial_state.spec) == []
    # 7-10 distractor count chosen by generator
    n = len(t.params["titles"])
    assert 7 <= n <= 10


def test_delete_specific_event_before_fails_after_passes():
    random.seed(11)
    t = gen_delete_specific_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    # Agent deletes the target by mutating the fake store.
    reader._events = [e for e in reader._events
                       if e["title"] != t.params["target"]]
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_delete_specific_event_wrong_target_fails():
    # Agent deletes a distractor instead of the target.
    random.seed(12)
    t = gen_delete_specific_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    distractor = t.params["survivors"][0]
    reader._events = [e for e in reader._events if e["title"] != distractor]
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_specific_event_renaming_distractor_caught():
    # Agent deletes the target but also renames a distractor —
    # identity check must catch the second mutation.
    random.seed(13)
    t = gen_delete_specific_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target = t.params["target"]
    distractor = t.params["survivors"][0]
    new = list(reader._events)
    new = [e for e in new if e["title"] != target]
    for e in new:
        if e["title"] == distractor:
            e["title"] = "Sneaky Rename"
            break
    reader._events = new
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_specific_event_distractor_time_shift_caught():
    # Agent deletes target but ALSO moves a distractor's start time —
    # identity check via compare_fields must catch it.
    random.seed(14)
    t = gen_delete_specific_event()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target = t.params["target"]
    distractor = t.params["survivors"][0]
    new = [e for e in reader._events if e["title"] != target]
    for e in new:
        if e["title"] == distractor:
            # Move start by 1 hour
            old = e["start_iso"]
            e["start_iso"] = old.replace("T0", "T2", 1) if "T0" in old \
                              else old.replace("T1", "T2", 1)
            break
    reader._events = new
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


# ═════════════════════════ gen_change_event_title ═══════════════════════════

def test_change_event_title_spec_validates():
    random.seed(20)
    t = gen_change_event_title()
    assert validate_spec(t.initial_state.spec) == []


def test_change_event_title_before_fails_after_passes():
    random.seed(21)
    t = gen_change_event_title()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["old_title"]:
            e["title"] = t.params["new_title"]
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_change_event_title_renames_wrong_event_fails():
    random.seed(22)
    t = gen_change_event_title()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    # Rename a distractor instead of the target
    for e in reader._events:
        if e["title"] != t.params["old_title"]:
            e["title"] = t.params["new_title"]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_change_event_title_rename_plus_time_shift_caught():
    # Agent renames target correctly AND shifts target's start time —
    # the rename-target's attribute_eq(start_iso=baseline) should catch
    # this even though the distractor identity check excludes the target.
    random.seed(23)
    t = gen_change_event_title()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    for e in reader._events:
        if e["title"] == t.params["old_title"]:
            e["title"] = t.params["new_title"]
            old = e["start_iso"]
            e["start_iso"] = old.replace(":00:00", ":30:00", 1)
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


# ═════════════════════════ gen_set_event_location ═══════════════════════════

def test_set_event_location_spec_validates():
    random.seed(30)
    t = gen_set_event_location()
    assert validate_spec(t.initial_state.spec) == []


def test_set_event_location_before_fails_after_passes():
    random.seed(31)
    t = gen_set_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["location"] = t.params["location"]
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_set_event_location_on_wrong_event_fails():
    random.seed(32)
    t = gen_set_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] != t.params["target"]:
            e["location"] = t.params["location"]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


# ═════════════════════════ gen_change_event_time ════════════════════════════

def test_change_event_time_spec_validates():
    random.seed(40)
    t = gen_change_event_time()
    assert validate_spec(t.initial_state.spec) == []
    # Duration preserved between old and new.
    new_start = t.params["new_start_iso"]
    new_end = t.params["new_end_iso"]
    s_h = int(new_start[11:13])
    e_h = int(new_end[11:13])
    dur = (e_h - s_h) * 60 + int(new_end[14:16]) - int(new_start[14:16])
    assert dur == t.params["duration_minutes"]


def test_change_event_time_before_fails_after_passes():
    random.seed(41)
    t = gen_change_event_time()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"] = t.params["new_end_iso"]
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_change_event_time_breaking_duration_fails():
    # Agent moves the start correctly but the end stays at old value —
    # duration is now different. attribute_eq(end_iso) catches it.
    random.seed(42)
    t = gen_change_event_time()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            # end_iso NOT updated
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


# ═════════════════════════ gen_toggle_event_all_day ═════════════════════════

def test_toggle_event_all_day_spec_validates():
    random.seed(50)
    t = gen_toggle_event_all_day()
    assert validate_spec(t.initial_state.spec) == []


def test_toggle_event_all_day_before_fails_after_passes():
    random.seed(51)
    t = gen_toggle_event_all_day()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    # Agent toggles target to all-day. iOS rewrites start/end to
    # date-only — emulate that on the fake.
    target_date = t.params["date"]
    # Real iOS all-day: end_iso == start_iso for single-day events
    # (see IOS_SIM_QUIRKS §16, probed 2026-05-21).
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["all_day"] = True
            e["start_iso"] = target_date
            e["end_iso"] = target_date
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_toggle_event_all_day_wrong_event_fails():
    random.seed(52)
    t = gen_toggle_event_all_day()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    target_date = t.params["date"]
    # iOS all-day: single-day events have end_iso == start_iso.
    next_day = target_date
    for e in reader._events:
        if e["title"] != t.params["target"]:
            e["all_day"] = True
            e["start_iso"] = target_date
            e["end_iso"] = next_day
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


# ═════════════════════════ all generators — phrasing + noise ════════════════

@pytest.mark.parametrize("gen", [
    gen_create_event_with_title_time,
    gen_delete_specific_event,
    gen_change_event_title,
    gen_set_event_location,
    gen_change_event_time,
    gen_toggle_event_all_day,
])
def test_calendar_t1_has_phrasing_variation(gen):
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen().instruction)
    # 3 phrasings per generator — at least 2 distinct in 50 seeds.
    assert len(instructions) >= 2


@pytest.mark.parametrize("gen", [
    gen_create_event_with_title_time,
    gen_delete_specific_event,
    gen_change_event_title,
    gen_set_event_location,
    gen_change_event_time,
    gen_toggle_event_all_day,
])
def test_calendar_t1_springboard_includes_start_page(gen):
    random.seed(100)
    t = gen()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


# ═════════════════════════ B1 cheat-path regressions ═══════════════════════
#
# The `_event_distractor_identity_check` excludes the target from BOTH
# sides of the comparison, so target-side field mutations would slip
# through unless `_target_unchanged_checks` emits explicit attribute_eq
# guards. These tests pin the "agent does right thing + side-effect on
# target" cases and assert they fail.

def test_change_event_title_target_notes_added_caught():
    random.seed(60)
    t = gen_change_event_title()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["old_title"]:
            e["title"] = t.params["new_title"]
            e["notes"] = "side-effect notes"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_change_event_title_target_location_added_caught():
    random.seed(61)
    t = gen_change_event_title()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["old_title"]:
            e["title"] = t.params["new_title"]
            e["location"] = "Sneaky Cafe"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_set_event_location_target_time_shift_caught():
    # Critical cheat: agent sets correct location AND moves target's time.
    # Without target_unchanged guards on start_iso/end_iso, exclude_match
    # drops target from distractor identity → silent pass. This test
    # pins it as a FAIL.
    random.seed(62)
    t = gen_set_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["location"] = t.params["location"]
            # Shift start by 2h
            old = e["start_iso"]
            hh = int(old[11:13])
            new_hh = (hh + 2) % 24
            e["start_iso"] = old[:11] + f"{new_hh:02d}" + old[13:]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_set_event_location_target_notes_added_caught():
    random.seed(63)
    t = gen_set_event_location()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["location"] = t.params["location"]
            e["notes"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_change_event_time_target_notes_added_caught():
    random.seed(64)
    t = gen_change_event_time()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"] = t.params["new_end_iso"]
            e["notes"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_change_event_time_target_location_added_caught():
    random.seed(65)
    t = gen_change_event_time()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["new_start_iso"]
            e["end_iso"] = t.params["new_end_iso"]
            e["location"] = "Sneaky Cafe"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_toggle_event_all_day_target_notes_added_caught():
    random.seed(66)
    t = gen_toggle_event_all_day()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    target_date = t.params["date"]
    # iOS all-day: single-day events have end_iso == start_iso.
    next_day = target_date
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["all_day"] = True
            e["start_iso"] = target_date
            e["end_iso"] = next_day
            e["notes"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_toggle_event_all_day_target_location_added_caught():
    random.seed(67)
    t = gen_toggle_event_all_day()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    target_date = t.params["date"]
    # iOS all-day: single-day events have end_iso == start_iso.
    next_day = target_date
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["all_day"] = True
            e["start_iso"] = target_date
            e["end_iso"] = next_day
            e["location"] = "Sneaky Cafe"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_create_event_with_extra_notes_payload_caught():
    # T1.1 doesn't use _target_unchanged_checks (creates from empty),
    # but the count(notes="") and count(location="") guards must catch
    # an agent that creates the right event AND sets spurious notes/loc.
    random.seed(68)
    t = gen_create_event_with_title_time()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["title"],
        "start_iso": t.params["start_iso"],
        "end_iso": t.params["end_iso"],
        "notes": "leaked",
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


# ═════════════════════════ helpers / parametrized ═══════════════════════════

@pytest.mark.parametrize("gen", [
    gen_create_event_with_title_time,
    gen_delete_specific_event,
    gen_change_event_title,
    gen_set_event_location,
    gen_change_event_time,
    gen_toggle_event_all_day,
])
def test_calendar_t1_includes_distractor_identity_check(gen):
    random.seed(101)
    t = gen()
    # Every Calendar T1 generator ships an identity check using
    # compare_fields + exclude_match — the critic-recommended
    # "no irrelevant edits" guard. create_event_with_title_time is the
    # one exception (no distractors to preserve at apply time).
    if gen is gen_create_event_with_title_time:
        pytest.skip("create-from-empty has no baseline distractors")
    identity_checks = [c for c in t.verify_checks
                        if c["kind"] == "identity"]
    assert len(identity_checks) >= 1
    ic = identity_checks[0]
    assert "compare_fields" in ic
    assert "exclude_match" in ic
