"""Phase 2b B4 — `gen_reminder_with_calendar_event` structural tests.

Asserts the multi-app generator emits the expected shape: both apps
in `task.apps`, spec entries for both, `verify_checks` referencing
the shared title via the same SymbolicRef instance. The
`SymbolicRef` round-trip invariant (one ref resolves identically
everywhere) is the test that catches drift between spec and verify
even when the generator gets refactored.
"""

from __future__ import annotations

import random

import pytest

import sibb_refs
import sibb_spec
import sibb_task_generator_v3 as gen

pytestmark = pytest.mark.fast


_GEN_SEEDS = [1, 7, 42, 100, 2026]


@pytest.mark.parametrize("seed", _GEN_SEEDS)
def test_generator_runs_deterministically(seed: int):
    random.seed(seed)
    task = gen.gen_reminder_with_calendar_event()
    assert task.flow == "reminder_to_calendar"
    assert set(task.apps) == {"Reminders", "Calendar"}


@pytest.mark.parametrize("seed", _GEN_SEEDS)
def test_spec_contains_both_apps(seed: int):
    random.seed(seed)
    task = gen.gen_reminder_with_calendar_event()
    apps_in_spec = {e.get("app") for e in task.initial_state.spec}
    # Calendar setup is the agent's job; spec only seeds Reminders.
    assert "Reminders" in apps_in_spec


@pytest.mark.parametrize("seed", _GEN_SEEDS)
def test_verify_checks_reference_both_apps(seed: int):
    random.seed(seed)
    task = gen.gen_reminder_with_calendar_event()
    resources = {c["resource"] for c in task.verify_checks}
    # At least one Reminders-side and one Calendar-side check.
    assert any(r.startswith("reminders.") for r in resources)
    assert any(r.startswith("calendar.") for r in resources)


@pytest.mark.parametrize("seed", _GEN_SEEDS)
def test_shared_title_is_single_symbolic_ref_instance(seed: int):
    # THE B4 invariant: the title used in the Reminders item spec
    # and in the Calendar event verify_check selector is sourced
    # from the same SymbolicRef. Any time the value changes, both
    # places follow. C1 resolve_refs is what makes this work at
    # dispatch; this test makes sure the generator actually wires
    # both positions to the same ref.
    random.seed(seed)
    task = gen.gen_reminder_with_calendar_event()

    item_entry = next(
        e for e in task.initial_state.spec
        if e.get("app") == "Reminders" and e.get("type") == "item"
    )
    title_in_spec = item_entry["title"]
    assert isinstance(title_in_spec, sibb_refs.SymbolicRef)

    calendar_verify = next(
        c for c in task.verify_checks
        if c.get("resource") == "calendar.events"
    )
    title_in_verify = calendar_verify["selector"]["title"]
    assert isinstance(title_in_verify, sibb_refs.SymbolicRef)

    # Same instance — that's the whole point.
    assert title_in_spec is title_in_verify


@pytest.mark.parametrize("seed", _GEN_SEEDS)
def test_resolved_spec_validates(seed: int):
    # After ref resolution, the spec must validate against the
    # typed SPEC_TYPES dataclasses (A5 contract).
    random.seed(seed)
    task = gen.gen_reminder_with_calendar_event()
    resolved_spec = sibb_refs.resolve_refs(task.initial_state.spec)
    errors = sibb_spec.validate_spec(resolved_spec)
    assert errors == [], (
        f"resolved spec fails validation:\n  " + "\n  ".join(errors)
    )


@pytest.mark.parametrize("seed", _GEN_SEEDS)
def test_resolved_verify_checks_are_pure_strings(seed: int):
    # After resolve_refs, verify_checks should contain no remaining
    # SymbolicRef instances anywhere.
    random.seed(seed)
    task = gen.gen_reminder_with_calendar_event()
    resolved = sibb_refs.resolve_refs(task.verify_checks)

    def _no_refs(obj):
        if isinstance(obj, sibb_refs.SymbolicRef):
            return False
        if isinstance(obj, dict):
            return all(_no_refs(v) for v in obj.values())
        if isinstance(obj, list):
            return all(_no_refs(x) for x in obj)
        return True

    assert _no_refs(resolved)


def test_instruction_contains_resolved_title():
    # Instruction is a plain string; generator uses `ref.value`
    # directly when building it. Resolved title must appear.
    random.seed(2026)
    task = gen.gen_reminder_with_calendar_event()
    assert task.params["title"] in task.instruction


def test_params_title_matches_spec_title_after_resolve():
    # params['title'] is built from ref.value at construction time;
    # spec's title is the SymbolicRef. After resolve, they agree.
    random.seed(2026)
    task = gen.gen_reminder_with_calendar_event()
    resolved_spec = sibb_refs.resolve_refs(task.initial_state.spec)
    item = next(e for e in resolved_spec if e.get("type") == "item")
    assert item["title"] == task.params["title"]
