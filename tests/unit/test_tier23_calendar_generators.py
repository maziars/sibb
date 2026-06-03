"""Tier 2/3 Calendar generators — bulk + structural mutations.

Same shape as test_tier1_calendar_generators.py: each generator gets
spec-validation, BEFORE/AFTER round-trip, and cheat-path regressions
against the FakeXCUITestReader.
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
    gen_delete_all_events_on_date,
    gen_duplicate_event_to_next_week,
    gen_delete_events_in_calendar,
    gen_move_event_between_calendars,
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


# ═════════════════════════ gen_delete_all_events_on_date ═══════════════════

def test_delete_all_events_on_date_spec_validates():
    random.seed(1)
    t = gen_delete_all_events_on_date()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Calendar"]


def test_delete_all_events_on_date_before_fails_after_passes():
    random.seed(2)
    t = gen_delete_all_events_on_date()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    # Agent deletes every event on the target date.
    target_date = t.params["date_target"]
    reader._events = [e for e in reader._events
                       if not e["start_iso"].startswith(target_date)]
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_delete_all_events_on_date_partial_delete_fails():
    # Agent only deletes SOME target-date events (forgot one).
    random.seed(3)
    t = gen_delete_all_events_on_date()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_date = t.params["date_target"]
    target_events = [e for e in reader._events
                      if e["start_iso"].startswith(target_date)]
    keep_one = target_events[0]["title"]
    reader._events = [e for e in reader._events
                       if not e["start_iso"].startswith(target_date)
                          or e["title"] == keep_one]
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_all_events_on_date_wrong_day_fails():
    # Agent deletes everything on the OTHER date instead.
    random.seed(4)
    t = gen_delete_all_events_on_date()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    other_date = t.params["date_other"]
    reader._events = [e for e in reader._events
                       if not e["start_iso"].startswith(other_date)]
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_all_events_on_date_other_day_mutation_caught():
    # Agent deletes target-date events correctly BUT also shifts an
    # other-date event's time. Identity check on the other-date window
    # must catch this.
    random.seed(5)
    t = gen_delete_all_events_on_date()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_date = t.params["date_target"]
    other_date = t.params["date_other"]
    # Delete target-date events.
    reader._events = [e for e in reader._events
                       if not e["start_iso"].startswith(target_date)]
    # Mutate one other-date event.
    for e in reader._events:
        if e["start_iso"].startswith(other_date):
            old = e["start_iso"]
            hh = int(old[11:13])
            e["start_iso"] = old[:11] + f"{(hh + 1) % 24:02d}" + old[13:]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_all_events_on_date_spurious_create_caught():
    # Agent deletes target-date events but ALSO creates a new event on
    # a THIRD day. Total count guard catches it.
    random.seed(6)
    t = gen_delete_all_events_on_date()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_date = t.params["date_target"]
    reader._events = [e for e in reader._events
                       if not e["start_iso"].startswith(target_date)]
    # Spurious creation 3 days from now.
    spur_d = (_dt.date.fromisoformat(target_date)
              + _dt.timedelta(days=3)).isoformat()
    asyncio.run(reader._send({
        "type": "create_event",
        "title": "Phantom",
        "start_iso": f"{spur_d}T09:00:00",
        "end_iso":   f"{spur_d}T10:00:00",
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_all_events_on_date_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_delete_all_events_on_date().instruction)
    assert len(instructions) >= 2


def test_delete_all_events_on_date_springboard_includes_start_page():
    random.seed(10)
    t = gen_delete_all_events_on_date()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


# ═════════════════════════ gen_duplicate_event_to_next_week ════════════════

def test_duplicate_event_spec_validates():
    random.seed(20)
    t = gen_duplicate_event_to_next_week()
    assert validate_spec(t.initial_state.spec) == []


def test_duplicate_event_before_fails_after_passes():
    random.seed(21)
    t = gen_duplicate_event_to_next_week()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    # Agent creates the duplicate on the next-week date.
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["target"],
        "start_iso": t.params["expected_start_iso"],
        "end_iso": t.params["expected_end_iso"],
    }))
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_duplicate_event_wrong_day_fails():
    # Agent duplicates to a different week (+14d instead of +7d).
    random.seed(22)
    t = gen_duplicate_event_to_next_week()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    wrong_date = (_dt.date.fromisoformat(t.params["date_next_week"])
                  + _dt.timedelta(days=7)).isoformat()
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["target"],
        "start_iso": f"{wrong_date}T{t.params['expected_start_iso'][11:]}",
        "end_iso":   f"{wrong_date}T{t.params['expected_end_iso'][11:]}",
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_duplicate_event_wrong_time_fails():
    # Agent duplicates to the right week but wrong hour.
    random.seed(23)
    t = gen_duplicate_event_to_next_week()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    # Off-by-one hour on the right next-week date.
    es = t.params["expected_start_iso"]
    ee = t.params["expected_end_iso"]
    s_hh = int(es[11:13])
    e_hh = int(ee[11:13])
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["target"],
        "start_iso": es[:11] + f"{(s_hh + 1) % 24:02d}" + es[13:],
        "end_iso":   ee[:11] + f"{(e_hh + 1) % 24:02d}" + ee[13:],
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_duplicate_event_breaks_duration_fails():
    # Duplicate to right time but wrong duration.
    random.seed(24)
    t = gen_duplicate_event_to_next_week()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    es = t.params["expected_start_iso"]
    ee = t.params["expected_end_iso"]
    e_hh = int(ee[11:13])
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["target"],
        "start_iso": es,
        "end_iso":   ee[:11] + f"{(e_hh + 1) % 24:02d}" + ee[13:],
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_duplicate_event_source_mutation_caught():
    # Agent duplicates correctly but ALSO modifies the source event.
    random.seed(25)
    t = gen_duplicate_event_to_next_week()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    # Correct duplicate.
    asyncio.run(reader._send({
        "type": "create_event",
        "title": t.params["target"],
        "start_iso": t.params["expected_start_iso"],
        "end_iso":   t.params["expected_end_iso"],
    }))
    # Mutate the source event.
    for e in reader._events:
        if (e["title"] == t.params["target"]
                and e["start_iso"].startswith(t.params["date_source"])):
            e["notes"] = "sneaky mutation"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_duplicate_event_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_duplicate_event_to_next_week().instruction)
    assert len(instructions) >= 2


def test_duplicate_event_springboard_includes_start_page():
    random.seed(30)
    t = gen_duplicate_event_to_next_week()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


# ═════════════════════════ gen_delete_events_in_calendar ═══════════════════

def test_delete_events_in_calendar_spec_validates():
    random.seed(40)
    t = gen_delete_events_in_calendar()
    assert validate_spec(t.initial_state.spec) == []
    # Spec must include calendar entries BEFORE event entries.
    spec_types = [e.get("type") for e in t.initial_state.spec
                   if e.get("app") == "Calendar"]
    first_event_idx = spec_types.index("event")
    cal_indices = [i for i, k in enumerate(spec_types) if k == "calendar"]
    assert all(i < first_event_idx for i in cal_indices), \
        "calendar entries must precede event entries"


def test_delete_events_in_calendar_before_fails_after_passes():
    random.seed(41)
    t = gen_delete_events_in_calendar()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    target_cal = t.params["target_calendar"]
    reader._events = [e for e in reader._events
                       if e["calendar"] != target_cal]
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_delete_events_in_calendar_wrong_calendar_fails():
    # Agent clears the OTHER calendar instead.
    random.seed(42)
    t = gen_delete_events_in_calendar()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    other_cal = t.params["other_calendar"]
    reader._events = [e for e in reader._events
                       if e["calendar"] != other_cal]
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_events_in_calendar_other_cal_mutation_caught():
    # Agent clears target correctly but ALSO modifies an event in
    # another calendar.
    random.seed(43)
    t = gen_delete_events_in_calendar()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_cal = t.params["target_calendar"]
    other_cal = t.params["other_calendar"]
    reader._events = [e for e in reader._events
                       if e["calendar"] != target_cal]
    for e in reader._events:
        if e["calendar"] == other_cal:
            e["notes"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_events_in_calendar_moves_event_caught():
    # Agent "deletes" by moving every target event to another calendar.
    # count(calendar=target_cal)==0 passes, but identity catches the
    # moved-event title appearing in the other-calendar set.
    random.seed(44)
    t = gen_delete_events_in_calendar()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_cal = t.params["target_calendar"]
    other_cal = t.params["other_calendar"]
    for e in reader._events:
        if e["calendar"] == target_cal:
            e["calendar"] = other_cal
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_events_in_calendar_deleting_calendar_itself_caught():
    # Agent deletes the calendar collection AND its events.
    # count(calendar.calendars where name=target) must be 1; if the
    # agent removed the calendar collection, count is 0 and the
    # check fires.
    random.seed(45)
    t = gen_delete_events_in_calendar()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_cal = t.params["target_calendar"]
    reader._events = [e for e in reader._events
                       if e["calendar"] != target_cal]
    reader._calendars = [c for c in reader._calendars
                          if c["name"] != target_cal]
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_events_in_calendar_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_delete_events_in_calendar().instruction)
    assert len(instructions) >= 2


def test_delete_events_in_calendar_springboard_includes_start_page():
    random.seed(50)
    t = gen_delete_events_in_calendar()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


# ═════════════════════════ gen_move_event_between_calendars ════════════════

def test_move_event_spec_validates():
    random.seed(60)
    t = gen_move_event_between_calendars()
    assert validate_spec(t.initial_state.spec) == []


def test_move_event_before_fails_after_passes():
    random.seed(61)
    t = gen_move_event_between_calendars()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    passed_before, _ = _verify(reader, t, baseline=baseline)
    assert passed_before is False

    # Agent moves the target by reassigning the calendar field.
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["calendar"] = t.params["dest_calendar"]
            break
    passed_after, _ = _verify(reader, t, baseline=baseline)
    assert passed_after is True


def test_move_event_wrong_event_moved_caught():
    # Agent moves a NON-target event from source to dest.
    random.seed(62)
    t = gen_move_event_between_calendars()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    source_cal = t.params["source_calendar"]
    dest_cal = t.params["dest_calendar"]
    target = t.params["target"]
    for e in reader._events:
        if e["calendar"] == source_cal and e["title"] != target:
            e["calendar"] = dest_cal
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_move_event_target_field_mutation_caught():
    # Agent moves the target correctly BUT also changes target's notes.
    random.seed(63)
    t = gen_move_event_between_calendars()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["calendar"] = t.params["dest_calendar"]
            e["notes"] = "sneaky"
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_move_event_distractor_calendar_swap_caught():
    # Agent moves target correctly but ALSO swaps a non-target event's
    # calendar in the same direction. Total counts shift; identity
    # compare_fields (including "calendar") catches it.
    random.seed(64)
    t = gen_move_event_between_calendars()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    source_cal = t.params["source_calendar"]
    dest_cal = t.params["dest_calendar"]
    target = t.params["target"]
    target_moved = False
    for e in reader._events:
        if e["title"] == target:
            e["calendar"] = dest_cal
            target_moved = True
        elif (e["calendar"] == source_cal and target_moved
              and e["title"] != target):
            e["calendar"] = dest_cal
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_move_event_delete_and_recreate_caught():
    # Agent deletes the target and creates a new event with the same
    # title in dest_cal but at a different time. attribute_eq(start_iso)
    # in _target_unchanged_checks catches the time mismatch.
    random.seed(65)
    t = gen_move_event_between_calendars()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target = t.params["target"]
    reader._events = [e for e in reader._events
                       if e["title"] != target]
    asyncio.run(reader._send({
        "type": "create_event",
        "title": target,
        "calendar": t.params["dest_calendar"],
        "start_iso": "2026-06-15T20:00:00",
        "end_iso":   "2026-06-15T21:00:00",
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_move_event_phrasing_variation():
    instructions = set()
    for seed in range(50):
        random.seed(seed)
        instructions.add(gen_move_event_between_calendars().instruction)
    assert len(instructions) >= 2


def test_move_event_springboard_includes_start_page():
    random.seed(70)
    t = gen_move_event_between_calendars()
    sb_kinds = {e["type"] for e in t.initial_state.spec
                if e.get("app") == "Springboard"}
    assert "start_page" in sb_kinds


# ═════════════════════════ B8 — additional cheat-path coverage ═══════════
#
# Tests recommended by T2/T3 critic 1 (2026-05-21). Closes gaps where
# a cheat was defensively caught by side-effect but not explicitly
# tested. Belt-and-suspenders against future corpora changes.


def test_delete_all_events_on_date_move_to_earlier_date_caught():
    """T2.1 spurious-create regression on the DATE BEFORE target.
    Symmetric to the existing _spurious_create_caught test, which
    only covers a date AFTER target."""
    random.seed(81)
    t = gen_delete_all_events_on_date()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_date = t.params["date_target"]
    reader._events = [e for e in reader._events
                       if not e["start_iso"].startswith(target_date)]
    earlier = (_dt.date.fromisoformat(target_date)
               - _dt.timedelta(days=2)).isoformat()
    asyncio.run(reader._send({
        "type": "create_event",
        "title": "Earlier Phantom",
        "start_iso": f"{earlier}T09:00:00",
        "end_iso":   f"{earlier}T10:00:00",
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_duplicate_event_source_moved_instead_caught():
    """T2.2: agent moves target FROM source to D+7 (no duplicate).
    count(source-date) drops; source-window count check fires."""
    random.seed(82)
    t = gen_duplicate_event_to_next_week()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    # Move target's date from source to next-week (not duplicate).
    for e in reader._events:
        if e["title"] == t.params["target"]:
            e["start_iso"] = t.params["expected_start_iso"]
            e["end_iso"]   = t.params["expected_end_iso"]
            break
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_events_in_calendar_partial_delete_fails():
    """T2.4: agent forgets to delete one target-cal event."""
    random.seed(83)
    t = gen_delete_events_in_calendar()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_cal = t.params["target_calendar"]
    target_events = [e for e in reader._events
                      if e["calendar"] == target_cal]
    keep_one_title = target_events[0]["title"]
    reader._events = [e for e in reader._events
                       if e["calendar"] != target_cal
                          or e["title"] == keep_one_title]
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_delete_events_in_calendar_moves_to_third_calendar_caught():
    """T2.4: agent creates a NEW user calendar and moves all
    target-cal events there. Total count guard fires."""
    random.seed(84)
    t = gen_delete_events_in_calendar()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target_cal = t.params["target_calendar"]
    # Spawn a new calendar and move target-cal events there.
    asyncio.run(reader._send({
        "type": "create_calendar", "name": "Sneaky"}))
    for e in reader._events:
        if e["calendar"] == target_cal:
            e["calendar"] = "Sneaky"
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_move_event_duplicate_in_dest_caught():
    """T2.5: agent leaves target in source AND creates a same-titled
    event in dest. Defensive count(title=target)==1 guard catches it
    (added in B6 fix)."""
    random.seed(85)
    t = gen_move_event_between_calendars()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    # Leave original in source; create a duplicate in dest.
    target = t.params["target"]
    original = next(e for e in reader._events if e["title"] == target)
    asyncio.run(reader._send({
        "type": "create_event",
        "title": target,
        "calendar": t.params["dest_calendar"],
        "start_iso": original["start_iso"],
        "end_iso":   original["end_iso"],
    }))
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False


def test_move_event_rotation_with_time_shift_caught():
    """T2.5: 3-cycle rotation — agent renames target→A and renames
    a distractor→target (with no time shift). Subset of titles or
    identity must catch the missing distractor original title."""
    random.seed(86)
    t = gen_move_event_between_calendars()
    reader = FakeXCUITestReader()
    _seed_initial_state(reader, t)
    baseline = _capture_baseline(reader)

    target = t.params["target"]
    source_cal = t.params["source_calendar"]
    dest_cal = t.params["dest_calendar"]
    # Pick a non-target distractor in source.
    distractor = next(e for e in reader._events
                       if e["title"] != target
                          and e["calendar"] == source_cal)
    distractor_old_title = distractor["title"]
    # Rename target to a brand-new label and move it to dest.
    for e in reader._events:
        if e["title"] == target:
            e["title"] = "Some Unrelated Label"
            e["calendar"] = dest_cal
            break
    # Rename distractor to target's old name (the rotation).
    distractor["title"] = target
    # Should fail — distractor_old_title now missing from subset.
    passed, _ = _verify(reader, t, baseline=baseline)
    assert passed is False
    # Use distractor_old_title to silence the "unused variable" lint —
    # the variable's value is the assertion ground truth.
    assert distractor_old_title != target
