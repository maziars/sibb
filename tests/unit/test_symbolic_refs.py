"""Phase 2c C1 — SymbolicRef + resolve_refs invariants."""

from __future__ import annotations

import pytest

from sibb_refs import SymbolicRef, resolve_refs
from sibb_spec import RemindersItem, CalendarEvent

pytestmark = pytest.mark.fast


# ─────────────────────────────── construction ─────────────────────────

def test_symbolic_ref_is_frozen():
    r = SymbolicRef("a", "b")
    with pytest.raises(Exception):
        r.value = "changed"  # type: ignore[misc]


def test_symbolic_ref_requires_nonempty_name():
    with pytest.raises(ValueError):
        SymbolicRef(name="", value="x")


def test_symbolic_ref_rejects_non_string_name():
    with pytest.raises(ValueError):
        SymbolicRef(name=123, value="x")  # type: ignore[arg-type]


def test_symbolic_ref_value_accepts_any_type():
    assert SymbolicRef("count", 42).value == 42
    assert SymbolicRef("times", ["09:00"]).value == ["09:00"]
    assert SymbolicRef("flag", True).value is True


# ─────────────────────────────── resolve leaves ───────────────────────

@pytest.mark.parametrize("obj", [
    "hello", 42, True, False, None, 3.14, b"bytes",
])
def test_resolve_primitives_pass_through(obj):
    assert resolve_refs(obj) == obj


def test_resolve_replaces_leaf_ref():
    assert resolve_refs(SymbolicRef("title", "Lunch")) == "Lunch"


def test_resolve_replaces_ref_with_complex_value():
    r = SymbolicRef("times", ["09:00", "10:00"])
    assert resolve_refs(r) == ["09:00", "10:00"]


# ─────────────────────────────── containers ───────────────────────────

def test_resolve_dict_with_ref_value():
    r = SymbolicRef("title", "Lunch")
    obj = {"name": "X", "title": r, "count": 3}
    assert resolve_refs(obj) == {"name": "X", "title": "Lunch", "count": 3}


def test_resolve_list_with_refs():
    r = SymbolicRef("x", "value")
    assert resolve_refs([1, r, "stay", r]) == [1, "value", "stay", "value"]


def test_resolve_tuple_with_refs():
    r = SymbolicRef("x", "v")
    out = resolve_refs((1, r, "stay"))
    assert out == (1, "v", "stay")
    assert isinstance(out, tuple)


def test_resolve_recurses_into_nested_dict():
    r = SymbolicRef("title", "Lunch")
    obj = {"outer": {"inner": {"title": r}}}
    assert resolve_refs(obj) == {"outer": {"inner": {"title": "Lunch"}}}


def test_resolve_recurses_into_list_of_dicts():
    r = SymbolicRef("title", "Lunch")
    obj = [{"title": r}, {"title": r}]
    assert resolve_refs(obj) == [{"title": "Lunch"}, {"title": "Lunch"}]


# ─────────────────────────────── core invariant ───────────────────────

def test_same_ref_resolves_identically_everywhere():
    # THE invariant C1 exists to guarantee.
    ref = SymbolicRef("title", "Lunch with Sam")
    obj = {
        "spec": [{"title": ref}, {"title": ref}],
        "params": {"title": ref},
        "verify_checks": [{"selector": {"title": ref}}],
    }
    resolved = resolve_refs(obj)
    assert resolved["spec"][0]["title"] == "Lunch with Sam"
    assert resolved["spec"][1]["title"] == "Lunch with Sam"
    assert resolved["params"]["title"] == "Lunch with Sam"
    assert resolved["verify_checks"][0]["selector"]["title"] == "Lunch with Sam"


# ─────────────────────────────── typed spec ───────────────────────────

def test_resolve_typed_spec_entry_with_ref():
    ref = SymbolicRef("title", "Resolved")
    # type: ignore — passing SymbolicRef where str is expected; the
    # field's runtime type is permissive.
    item = RemindersItem(list="L", title=ref)  # type: ignore[arg-type]
    assert resolve_refs(item) == {
        "app": "Reminders", "type": "item",
        "list": "L", "title": "Resolved",
        "priority": None, "completed": False,
        "due_iso": None, "notes": None, "url": None,
        "recurrence": None,
    }


def test_resolve_typed_spec_entry_without_refs():
    # No-ref typed entry still resolves to its dict form.
    item = RemindersItem(list="L", title="x")
    assert resolve_refs(item) == item.to_dict()


def test_resolve_calendar_event_with_ref_in_title():
    ref = SymbolicRef("title", "Lunch")
    ev = CalendarEvent(  # type: ignore[arg-type]
        title=ref,
        start_iso="2026-05-15T12:00:00",
        end_iso="2026-05-15T13:00:00",
    )
    out = resolve_refs(ev)
    assert out["title"] == "Lunch"
    assert out["app"] == "Calendar"


# ─────────────────────────────── immutability ─────────────────────────

def test_resolve_does_not_mutate_input_dict():
    ref = SymbolicRef("title", "X")
    obj = {"title": ref, "items": [{"title": ref}]}
    out = resolve_refs(obj)
    assert obj["title"] is ref
    assert obj["items"][0]["title"] is ref
    assert out["title"] == "X"
    assert out is not obj


def test_resolve_does_not_mutate_input_list():
    ref = SymbolicRef("x", "v")
    original = [1, ref, "stay"]
    snapshot = list(original)
    out = resolve_refs(original)
    assert original == snapshot
    assert out is not original


# ─────────────────────────────── edge cases ───────────────────────────

def test_resolve_empty_structures_unchanged():
    assert resolve_refs({}) == {}
    assert resolve_refs([]) == []
    assert resolve_refs(()) == ()


def test_resolve_dict_with_no_refs_returns_equal_copy():
    obj = {"a": 1, "b": [2, 3], "c": {"d": 4}}
    out = resolve_refs(obj)
    assert out == obj
    assert out is not obj
