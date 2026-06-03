"""A7 — `BaselineSnapshot` + `identity` check kind, unit-level.

Direct construction of a BaselineSnapshot (no reader) drives the
`_check_identity` handler through happy / fail / error paths.
End-to-end capture against FakeXCUITestReader is in the L1.5 file.
"""

from __future__ import annotations

import pytest

from sibb_verify import (
    BaselineSnapshot,
    CHECK_KINDS,
    _check_identity,
)

pytestmark = pytest.mark.fast


def _make_baseline(**resources):
    return BaselineSnapshot(captured_at=0.0, resources=dict(resources))


# ─────────────────── BaselineSnapshot shape ──────────────────────────

def test_baseline_snapshot_is_frozen():
    b = _make_baseline()
    with pytest.raises(Exception):
        b.captured_at = 1.0  # frozen dataclass


def test_baseline_snapshot_resources_keyed_by_resource_string():
    b = _make_baseline(**{
        "reminders.lists": [{"name": "Personal", "identifier": "id1"}],
        "reminders.items": [],
    })
    assert "reminders.lists" in b.resources
    assert b.resources["reminders.items"] == []


# ─────────────────────── identity kind registered ────────────────────

def test_identity_registered_in_check_kinds():
    assert "identity" in CHECK_KINDS


# ─────────────────────── identity check logic ────────────────────────

def test_identity_passes_when_identifiers_match():
    b = _make_baseline(**{
        "reminders.lists": [
            {"name": "A", "identifier": "id-1"},
            {"name": "B", "identifier": "id-2"},
        ],
    })
    current = [
        {"name": "B", "identifier": "id-2"},
        {"name": "A", "identifier": "id-1"},   # different order, same ids
    ]
    status, ev = _check_identity(
        current, {"resource": "reminders.lists"}, baseline=b
    )
    assert status == "pass"
    assert ev["method"] == "identifiers"


def test_identity_fails_when_record_added():
    b = _make_baseline(**{
        "reminders.lists": [
            {"name": "A", "identifier": "id-1"},
        ],
    })
    current = [
        {"name": "A", "identifier": "id-1"},
        {"name": "Leaked", "identifier": "id-leak"},
    ]
    status, ev = _check_identity(
        current, {"resource": "reminders.lists"}, baseline=b
    )
    assert status == "fail"
    assert "id-leak" in ev["added"]
    assert ev["current_count"] == 2
    assert ev["baseline_count"] == 1


def test_identity_fails_when_record_removed():
    b = _make_baseline(**{
        "reminders.items": [
            {"title": "A", "identifier": "id-a"},
            {"title": "B", "identifier": "id-b"},
        ],
    })
    current = [{"title": "A", "identifier": "id-a"}]
    status, ev = _check_identity(
        current, {"resource": "reminders.items"}, baseline=b
    )
    assert status == "fail"
    assert "id-b" in ev["removed"]


def test_identity_with_selector_filters_baseline_too():
    # Baseline has items in two lists; identity check filtered to one
    # list compares only those items on both sides.
    b = _make_baseline(**{
        "reminders.items": [
            {"title": "X", "list": "Personal", "identifier": "id-x"},
            {"title": "Y", "list": "Work", "identifier": "id-y"},
        ],
    })
    current = [
        {"title": "X", "list": "Personal", "identifier": "id-x"},
        # Work list got more items but Personal is unchanged
        {"title": "Y", "list": "Work", "identifier": "id-y"},
        {"title": "Z", "list": "Work", "identifier": "id-z"},
    ]
    status, _ = _check_identity(
        [r for r in current if r["list"] == "Personal"],
        {"resource": "reminders.items", "selector": {"list": "Personal"}},
        baseline=b,
    )
    assert status == "pass"


def test_identity_no_baseline_is_error():
    status, ev = _check_identity(
        [], {"resource": "reminders.lists"}, baseline=None
    )
    assert status == "error"
    assert "baseline" in ev["error"]


def test_identity_unknown_resource_in_baseline_is_error():
    b = _make_baseline(**{"reminders.lists": []})
    status, ev = _check_identity(
        [], {"resource": "photos.albums"}, baseline=b
    )
    assert status == "error"
    assert "photos.albums" in ev["error"]
    assert "reminders.lists" in ev["available"]


def test_identity_falls_back_to_count_when_no_identifiers():
    # Records with no `identifier` field — fall back to count-based
    # comparison. Less precise; method=count-only in evidence.
    b = _make_baseline(**{
        "anonymous.things": [{"x": 1}, {"x": 2}],
    })
    current_same_count = [{"x": 9}, {"x": 10}]
    status, ev = _check_identity(
        current_same_count, {"resource": "anonymous.things"}, baseline=b
    )
    assert status == "pass"
    assert ev["method"] == "count-only"


def test_identity_count_fallback_fails_on_count_mismatch():
    b = _make_baseline(**{
        "anonymous.things": [{"x": 1}],
    })
    status, _ = _check_identity(
        [{"x": 1}, {"x": 2}],
        {"resource": "anonymous.things"}, baseline=b,
    )
    assert status == "fail"


def test_identity_evidence_caps_added_removed_at_five():
    b = _make_baseline(**{
        "r": [{"identifier": f"old-{i}"} for i in range(10)],
    })
    current = [{"identifier": f"new-{i}"} for i in range(10)]
    _, ev = _check_identity(current, {"resource": "r"}, baseline=b)
    assert len(ev["added"]) == 5
    assert len(ev["removed"]) == 5


# ──────────── compare_fields (signature mode) ─────────────────────────
#
# Added 2026-05-20 for Calendar Tier 1 "no irrelevant edits" guards.
# When identity is given `compare_fields`, comparison is on per-record
# field-tuple sets — catches edits that preserve identifier but mutate
# fields (rename, time-shift, location set). See sibb_verify.py docstring.

def test_identity_compare_fields_passes_when_signatures_match():
    base_rec = {"identifier": "e1", "title": "Standup",
                "start_iso": "2026-05-20T09:00:00", "all_day": False}
    b = _make_baseline(**{"events": [base_rec]})
    current = [dict(base_rec)]
    status, _ = _check_identity(
        current,
        {"resource": "events",
         "compare_fields": ["title", "start_iso", "all_day"]},
        baseline=b,
    )
    assert status == "pass"


def test_identity_compare_fields_fails_when_title_mutated():
    # Same identifier, different title — pure-identifier identity would
    # pass, but signature-mode catches it.
    base_rec = {"identifier": "e1", "title": "Standup",
                "start_iso": "2026-05-20T09:00:00"}
    b = _make_baseline(**{"events": [base_rec]})
    current = [{"identifier": "e1", "title": "Rebranded",
                "start_iso": "2026-05-20T09:00:00"}]
    status, ev = _check_identity(
        current,
        {"resource": "events", "compare_fields": ["title", "start_iso"]},
        baseline=b,
    )
    assert status == "fail"
    assert ev["method"] == "signatures"
    # Each diff row is the tuple cast to list, in compare_fields order.
    assert ["Rebranded", "2026-05-20T09:00:00"] in ev["added"]
    assert ["Standup",   "2026-05-20T09:00:00"] in ev["removed"]


def test_identity_compare_fields_fails_when_time_shifted():
    base_rec = {"identifier": "e1", "title": "Lunch",
                "start_iso": "2026-05-20T12:00:00"}
    b = _make_baseline(**{"events": [base_rec]})
    current = [{"identifier": "e1", "title": "Lunch",
                "start_iso": "2026-05-20T13:00:00"}]
    status, _ = _check_identity(
        current,
        {"resource": "events", "compare_fields": ["title", "start_iso"]},
        baseline=b,
    )
    assert status == "fail"


def test_identity_compare_fields_order_insensitive():
    # Same set of (title, start) tuples, different list order.
    base_recs = [
        {"identifier": "a", "title": "A", "start_iso": "T1"},
        {"identifier": "b", "title": "B", "start_iso": "T2"},
    ]
    b = _make_baseline(**{"events": base_recs})
    current = [
        {"identifier": "b", "title": "B", "start_iso": "T2"},
        {"identifier": "a", "title": "A", "start_iso": "T1"},
    ]
    status, _ = _check_identity(
        current,
        {"resource": "events", "compare_fields": ["title", "start_iso"]},
        baseline=b,
    )
    assert status == "pass"


# ──────────── exclude_match (filter target out of both sides) ─────────

def test_exclude_match_drops_target_from_both_sides():
    # Baseline has target + 2 distractors. After agent: target is
    # renamed (identifier same, title new). With exclude_match scoping
    # the check to "not the target", identity passes — the 2 distractors
    # are byte-equal.
    base = [
        {"identifier": "t", "title": "Old Target",
         "start_iso": "T0", "all_day": False},
        {"identifier": "d1", "title": "Distractor 1",
         "start_iso": "T1", "all_day": False},
        {"identifier": "d2", "title": "Distractor 2",
         "start_iso": "T2", "all_day": False},
    ]
    b = _make_baseline(**{"events": base})
    # The agent renamed the target but left distractors alone.
    current = [
        {"identifier": "t", "title": "New Target",
         "start_iso": "T0", "all_day": False},
        {"identifier": "d1", "title": "Distractor 1",
         "start_iso": "T1", "all_day": False},
        {"identifier": "d2", "title": "Distractor 2",
         "start_iso": "T2", "all_day": False},
    ]
    status, _ = _check_identity(
        current,
        {"resource": "events",
         "compare_fields": ["title", "start_iso", "all_day"],
         # Exclude_match runs on BOTH sides, so the target's old title
         # in baseline and new title in current both need to be excluded.
         # Use identifier (stable across the edit) as the exclusion key.
         "exclude_match": {"identifier": "t"}},
        baseline=b,
    )
    assert status == "pass"


def test_exclude_match_catches_distractor_mutation():
    # Same setup as above, but agent ALSO mutated a distractor.
    # exclude_match drops only the target — distractor mutation surfaces.
    base = [
        {"identifier": "t",  "title": "Target",  "start_iso": "T0"},
        {"identifier": "d1", "title": "Distract", "start_iso": "T1"},
    ]
    b = _make_baseline(**{"events": base})
    current = [
        {"identifier": "t",  "title": "New T",   "start_iso": "T0"},
        # Distractor's start moved
        {"identifier": "d1", "title": "Distract", "start_iso": "T1-mutated"},
    ]
    status, _ = _check_identity(
        current,
        {"resource": "events",
         "compare_fields": ["title", "start_iso"],
         "exclude_match": {"identifier": "t"}},
        baseline=b,
    )
    assert status == "fail"


def test_exclude_match_alone_without_compare_fields_uses_identifier_set():
    # Exclude_match composes with the default identifier-set comparison.
    # Useful for "the rest of the set unchanged" without checking fields.
    base = [{"identifier": "t"}, {"identifier": "d1"}, {"identifier": "d2"}]
    b = _make_baseline(**{"events": base})
    # Agent kept all three identifiers — even though target's other
    # fields might have changed, identifier-set still matches.
    current = [{"identifier": "t"}, {"identifier": "d1"}, {"identifier": "d2"}]
    status, _ = _check_identity(
        current,
        {"resource": "events", "exclude_match": {"identifier": "t"}},
        baseline=b,
    )
    assert status == "pass"


def test_exclude_match_fails_when_distractor_disappears():
    base = [{"identifier": "t"}, {"identifier": "d1"}]
    b = _make_baseline(**{"events": base})
    # Agent deleted d1 too (collateral damage)
    current = [{"identifier": "t"}]
    status, _ = _check_identity(
        current,
        {"resource": "events", "exclude_match": {"identifier": "t"}},
        baseline=b,
    )
    assert status == "fail"


# ──── B3 — _signature_set missing vs None disambiguation ──────────────

def test_signature_set_treats_missing_field_as_distinct_from_none():
    # Record A: {title: "X"} — field omitted.
    # Record B: {title: "X", start_iso: None} — field explicitly None.
    # These must NOT compare equal under compare_fields=[title, start_iso].
    base = [{"identifier": "a", "title": "X", "start_iso": None}]
    b = _make_baseline(**{"events": base})
    current = [{"identifier": "a", "title": "X"}]  # start_iso MISSING
    status, _ = _check_identity(
        current,
        {"resource": "events",
         "compare_fields": ["title", "start_iso"]},
        baseline=b,
    )
    assert status == "fail"


# ──── B4 — exclude_match strict (case-sensitive) comparison ────────────

def test_exclude_match_is_case_sensitive():
    # Agent renamed "Lunch" → "LUNCH". exclude_match={"title":"Lunch"}.
    # Without strict comparison, the renamed event in current would be
    # silently dropped from the diff (lowercased match) and the agent's
    # mutation would not be caught. With strict comparison, only the
    # baseline "Lunch" is excluded — current's "LUNCH" stays in the
    # diff and the identity check fires "removed/added".
    base = [{"identifier": "t", "title": "Lunch",
             "start_iso": "T0"}]
    b = _make_baseline(**{"events": base})
    current = [{"identifier": "t", "title": "LUNCH",
                "start_iso": "T0"}]
    status, _ = _check_identity(
        current,
        {"resource": "events",
         "compare_fields": ["title", "start_iso"],
         "exclude_match": {"title": "Lunch"}},
        baseline=b,
    )
    # Baseline excludes "Lunch" target → []. Current keeps "LUNCH" → ["LUNCH"].
    # Sets differ → fail.
    assert status == "fail"


# ──── Windowed baseline filter for calendar.events ────────────────────

def test_identity_calendar_events_window_filters_baseline_correctly():
    """When `selector={start_iso, end_iso}` scopes an identity check to
    a date window, the baseline filter must apply window-OVERLAP
    semantics — not exact equality, which would silently match nothing
    and produce a spurious empty-set comparison."""
    # 3 events on tomorrow, 2 events on day-after-tomorrow.
    base = [
        {"identifier": "t1", "title": "A",
         "start_iso": "2026-05-22T09:00:00", "end_iso": "2026-05-22T10:00:00"},
        {"identifier": "t2", "title": "B",
         "start_iso": "2026-05-22T14:00:00", "end_iso": "2026-05-22T15:00:00"},
        {"identifier": "t3", "title": "C",
         "start_iso": "2026-05-22T16:00:00", "end_iso": "2026-05-22T17:00:00"},
        {"identifier": "o1", "title": "D",
         "start_iso": "2026-05-23T10:00:00", "end_iso": "2026-05-23T11:00:00"},
        {"identifier": "o2", "title": "E",
         "start_iso": "2026-05-23T13:00:00", "end_iso": "2026-05-23T14:00:00"},
    ]
    b = _make_baseline(**{"calendar.events": base})
    # Agent deleted all 3 events on 2026-05-22. Current has only o1, o2.
    current = [
        {"identifier": "o1", "title": "D",
         "start_iso": "2026-05-23T10:00:00", "end_iso": "2026-05-23T11:00:00"},
        {"identifier": "o2", "title": "E",
         "start_iso": "2026-05-23T13:00:00", "end_iso": "2026-05-23T14:00:00"},
    ]
    # Scope identity to 2026-05-23 events only — distractor preservation.
    status, _ = _check_identity(
        current,
        {"resource": "calendar.events",
         "selector": {"start_iso": "2026-05-23T00:00:00",
                       "end_iso":   "2026-05-23T23:59:59"},
         "compare_fields": ["title", "start_iso", "end_iso"]},
        baseline=b,
    )
    assert status == "pass"


def test_identity_calendar_events_window_includes_all_day_events():
    """B2 regression: date-only all-day events round-trip as
    "YYYY-MM-DD" (no T component). Naive lex compare with
    "YYYY-MM-DDT00:00:00" window bound silently drops them. The
    baseline filter must pad date-only forms before compare."""
    base = [
        # All-day event on 2026-05-22 — date-only ISO forms
        {"identifier": "ad1", "title": "Birthday",
         "start_iso": "2026-05-22", "end_iso": "2026-05-22",
         "all_day": True},
        # Timed event on same day
        {"identifier": "t1", "title": "Lunch",
         "start_iso": "2026-05-22T12:00:00",
         "end_iso": "2026-05-22T13:00:00",
         "all_day": False},
    ]
    b = _make_baseline(**{"calendar.events": base})
    # Agent didn't touch anything — current == baseline.
    current = [dict(r) for r in base]
    status, _ = _check_identity(
        current,
        {"resource": "calendar.events",
         "selector": {"start_iso": "2026-05-22T00:00:00",
                       "end_iso":   "2026-05-22T23:59:59"},
         "compare_fields": ["title", "start_iso", "end_iso", "all_day"]},
        baseline=b,
    )
    assert status == "pass"


def test_identity_calendar_events_window_catches_all_day_mutation():
    """B2 follow-up: if an agent mutates an all-day distractor, the
    window-scoped identity check must catch it (would silently pass
    if the baseline filter dropped the all-day event)."""
    base = [
        {"identifier": "ad1", "title": "Holiday",
         "start_iso": "2026-05-22", "end_iso": "2026-05-22",
         "all_day": True},
    ]
    b = _make_baseline(**{"calendar.events": base})
    # Agent renamed the all-day event
    current = [
        {"identifier": "ad1", "title": "Renamed Holiday",
         "start_iso": "2026-05-22", "end_iso": "2026-05-22",
         "all_day": True},
    ]
    status, _ = _check_identity(
        current,
        {"resource": "calendar.events",
         "selector": {"start_iso": "2026-05-22T00:00:00",
                       "end_iso":   "2026-05-22T23:59:59"},
         "compare_fields": ["title", "start_iso", "end_iso", "all_day"]},
        baseline=b,
    )
    assert status == "fail"


def test_identity_calendar_events_window_catches_other_day_mutation():
    # Same setup, but agent ALSO time-shifted an event on the other day.
    base = [
        {"identifier": "o1", "title": "D",
         "start_iso": "2026-05-23T10:00:00", "end_iso": "2026-05-23T11:00:00"},
        {"identifier": "o2", "title": "E",
         "start_iso": "2026-05-23T13:00:00", "end_iso": "2026-05-23T14:00:00"},
    ]
    b = _make_baseline(**{"calendar.events": base})
    current = [
        # o1's start moved
        {"identifier": "o1", "title": "D",
         "start_iso": "2026-05-23T15:00:00", "end_iso": "2026-05-23T16:00:00"},
        {"identifier": "o2", "title": "E",
         "start_iso": "2026-05-23T13:00:00", "end_iso": "2026-05-23T14:00:00"},
    ]
    status, _ = _check_identity(
        current,
        {"resource": "calendar.events",
         "selector": {"start_iso": "2026-05-23T00:00:00",
                       "end_iso":   "2026-05-23T23:59:59"},
         "compare_fields": ["title", "start_iso", "end_iso"]},
        baseline=b,
    )
    assert status == "fail"


def test_exclude_match_strict_does_not_drop_distractors():
    # Baseline + current both contain a distractor "lunch" (lowercase)
    # while exclude_match={"title": "Lunch"} (capital L). Strict match
    # should NOT exclude the distractor — the distractor IS the same in
    # both sides, so identity should pass.
    base = [
        {"identifier": "t",  "title": "Lunch", "start_iso": "T0"},
        {"identifier": "d1", "title": "lunch", "start_iso": "T1"},
    ]
    b = _make_baseline(**{"events": base})
    current = [
        # Agent only renamed the target (capital-L Lunch) to NewLunch
        {"identifier": "t",  "title": "NewLunch", "start_iso": "T0"},
        {"identifier": "d1", "title": "lunch",    "start_iso": "T1"},
    ]
    status, _ = _check_identity(
        current,
        {"resource": "events",
         "compare_fields": ["title", "start_iso"],
         "exclude_match": {"title": "Lunch"}},
        baseline=b,
    )
    # Baseline excludes "Lunch" → ["lunch"]. Current: target's new title
    # ("NewLunch") doesn't match "Lunch", so it STAYS in current.
    # Compare ["lunch"] vs ["lunch","NewLunch"] → fail.
    # (That's the intent: with strict semantics, the rename use case
    # must use a stable-key exclude like start_iso, not title.)
    assert status == "fail"
