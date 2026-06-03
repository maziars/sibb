"""A5 — typed spec dataclasses, round-trip + validation.

Each spec entry kind currently emitted by generators has a
dataclass in `sibb_spec`. Round-trip and validation tests catch
field-name drift before it reaches the dispatcher.
"""

from __future__ import annotations

import pytest

import sibb_spec
from sibb_spec import (
    SPEC_TYPES,
    RemindersItem,
    RemindersList,
    SpringboardDock,
    SpringboardLayout,
    SpringboardStartPage,
    validate_entry,
    validate_spec,
)

pytestmark = pytest.mark.fast


# ────────────────────────── Registry shape ────────────────────────────

def test_spec_types_registry_nonempty():
    assert SPEC_TYPES, "SPEC_TYPES is empty"


def test_spec_types_keys_match_class_attrs():
    for (app, ty), cls in SPEC_TYPES.items():
        assert cls.app == app
        assert cls.type == ty


def test_spec_types_covers_current_handlers():
    # Soft assertion: today we have entries for Reminders + Springboard.
    apps = {key[0] for key in SPEC_TYPES}
    assert "Reminders" in apps
    assert "Springboard" in apps


# ────────────────────────── Per-class round-trip ──────────────────────

@pytest.mark.parametrize("instance,expected", [
    (RemindersList(name="Personal"),
     {"app": "Reminders", "type": "list", "name": "Personal"}),
    (RemindersItem(list="Work", title="Finish report"),
     {"app": "Reminders", "type": "item", "list": "Work",
      "title": "Finish report", "priority": None, "completed": False,
      "due_iso": None, "notes": None, "url": None, "recurrence": None}),
    (RemindersItem(list="Work", title="Bug", priority="high", completed=True),
     {"app": "Reminders", "type": "item", "list": "Work",
      "title": "Bug", "priority": "high", "completed": True,
      "due_iso": None, "notes": None, "url": None, "recurrence": None}),
    (RemindersItem(list="Work", title="Pay rent",
                   due_iso="2026-05-20T09:00:00",
                   notes="Send Venmo to landlord",
                   url="https://venmo.com/u/landlord"),
     {"app": "Reminders", "type": "item", "list": "Work",
      "title": "Pay rent", "priority": None, "completed": False,
      "due_iso": "2026-05-20T09:00:00",
      "notes": "Send Venmo to landlord",
      "url": "https://venmo.com/u/landlord",
      "recurrence": None}),
    (RemindersItem(list="Work", title="Standup",
                   recurrence={"frequency": "weekly", "interval": 1}),
     {"app": "Reminders", "type": "item", "list": "Work",
      "title": "Standup", "priority": None, "completed": False,
      "due_iso": None, "notes": None, "url": None,
      "recurrence": {"frequency": "weekly", "interval": 1}}),
    (SpringboardLayout(seed=42, cross_page=True, distribute=True),
     {"app": "Springboard", "type": "layout",
      "seed": 42, "cross_page": True, "distribute": True, "n_pages": None}),
    (SpringboardLayout(seed=7, n_pages=3),
     {"app": "Springboard", "type": "layout",
      "seed": 7, "cross_page": False, "distribute": False, "n_pages": 3}),
    (SpringboardDock(seed=42, count=3),
     {"app": "Springboard", "type": "dock", "seed": 42, "count": 3}),
    (SpringboardDock(seed=0),
     {"app": "Springboard", "type": "dock", "seed": 0, "count": None}),
    (SpringboardStartPage(page=2),
     {"app": "Springboard", "type": "start_page", "page": 2}),
])
def test_to_dict_canonical_shape(instance, expected):
    assert instance.to_dict() == expected


@pytest.mark.parametrize("cls,kwargs", [
    (RemindersList, {"name": "Personal"}),
    (RemindersItem, {"list": "Work", "title": "X"}),
    (RemindersItem, {"list": "Work", "title": "Y",
                     "priority": "medium", "completed": True}),
    (RemindersItem, {"list": "Work", "title": "Z",
                     "due_iso": "2026-05-20T09:00:00",
                     "notes": "n", "url": "https://x.test/"}),
    (RemindersItem, {"list": "Work", "title": "Standup",
                     "recurrence": {"frequency": "weekly", "interval": 2,
                                    "end_count": 10}}),
    (SpringboardLayout, {"seed": 5, "cross_page": True}),
    (SpringboardLayout, {"seed": 5, "distribute": True, "n_pages": 4}),
    (SpringboardDock, {"seed": 1, "count": 3}),
    (SpringboardStartPage, {"page": 1}),
])
def test_round_trip_dataclass_to_dict_to_dataclass(cls, kwargs):
    instance = cls(**kwargs)
    d = instance.to_dict()
    reconstructed = cls.from_dict(d)
    assert reconstructed == instance


def test_from_dict_ignores_unknown_fields():
    # Permissive: dispatcher may add ephemeral fields (e.g. resolved
    # SymbolicRef metadata in Phase 2c C1); from_dict ignores them.
    d = {"app": "Reminders", "type": "list",
         "name": "X", "_resolved_ref_id": "abc"}
    r = RemindersList.from_dict(d)
    assert r.name == "X"


def test_from_dict_uses_defaults_for_missing_optional_fields():
    d = {"app": "Reminders", "type": "item",
         "list": "L", "title": "T"}
    r = RemindersItem.from_dict(d)
    assert r.priority is None
    assert r.completed is False


def test_from_dict_raises_typeerror_on_missing_required_field():
    # Missing `name` for RemindersList — TypeError from the dataclass
    # __init__. `validate_entry` translates this to a friendly message.
    with pytest.raises(TypeError):
        RemindersList.from_dict({"app": "Reminders", "type": "list"})


# ─────────────────────── validate_entry truth table ───────────────────

def test_validate_entry_accepts_known_well_formed():
    typed, err = validate_entry(
        {"app": "Reminders", "type": "list", "name": "Personal"}
    )
    assert err is None
    assert isinstance(typed, RemindersList)
    assert typed.name == "Personal"


def test_validate_entry_rejects_non_dict():
    typed, err = validate_entry(["not", "a", "dict"])
    assert typed is None
    assert err is not None
    assert "not a dict" in err


def test_validate_entry_rejects_unknown_app():
    typed, err = validate_entry(
        {"app": "Spaceship", "type": "list", "name": "X"}
    )
    assert typed is None
    assert "unknown spec entry kind" in err


def test_validate_entry_rejects_unknown_type():
    typed, err = validate_entry(
        {"app": "Reminders", "type": "spaceship", "name": "X"}
    )
    assert typed is None
    assert "unknown spec entry kind" in err


def test_validate_entry_rejects_missing_required_field():
    typed, err = validate_entry(
        {"app": "Reminders", "type": "list"}   # no `name`
    )
    assert typed is None
    assert "RemindersList" in err


def test_validate_entry_catches_historic_casing_bug():
    # `noise_layout` historically emitted "SpringBoard" (capital B).
    # A3 fixed the dispatcher; A5 deliberately keeps SPEC_TYPES
    # keyed by the canonical "Springboard" so a generator emitting
    # the typo is loudly rejected at validation time instead of
    # being canonicalized away (which would hide the bug).
    typed, err = validate_entry(
        {"app": "SpringBoard", "type": "layout", "seed": 1}
    )
    assert typed is None
    assert "unknown spec entry kind" in err


# ──────────────────── validate_spec aggregation ───────────────────────

def test_validate_spec_empty_returns_empty():
    assert validate_spec([]) == []
    assert validate_spec(None) == []


def test_validate_spec_collects_all_errors_with_index():
    spec = [
        {"app": "Reminders", "type": "list", "name": "OK"},
        {"app": "Spaceship", "type": "x"},
        {"app": "Reminders", "type": "item"},   # missing list+title
    ]
    errs = validate_spec(spec)
    assert len(errs) == 2
    assert "spec[1]" in errs[0]
    assert "spec[2]" in errs[1]


def test_validate_spec_all_pass_when_all_typed_round_trip():
    # Build a spec from typed instances → dicts; the dicts must
    # validate (this is the principal A5 invariant: typed entries
    # always survive serialization).
    instances = [
        RemindersList(name="Personal"),
        RemindersItem(list="Personal", title="X"),
        SpringboardLayout(seed=1, distribute=True),
        SpringboardDock(seed=1, count=3),
        SpringboardStartPage(page=0),
    ]
    spec = [i.to_dict() for i in instances]
    assert validate_spec(spec) == []
