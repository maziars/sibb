"""A5 regression — current generator outputs validate against typed spec.

If a generator emits a spec entry that no `SPEC_TYPES` dataclass
covers (typo in app name, unrecognized type, missing required
field), this test fails loudly at the seam — long before the
episode actually runs.
"""

from __future__ import annotations

import random

import pytest

import sibb_spec

pytestmark = pytest.mark.fast


# Fixed seeds covering both branches (list_state in {"exists","absent"})
# AND triggering layout/dock noise paths. Each seed picked empirically
# to exercise different shapes; if the generator changes structurally,
# add new seeds rather than removing old ones (no false sense of
# coverage if we drop checks).
_GEN_SEEDS = [1, 7, 42, 100, 2026]


@pytest.mark.parametrize("seed", _GEN_SEEDS)
def test_gen_reminders_list_emits_validating_spec(seed: int):
    random.seed(seed)
    import sibb_task_generator_v3 as gen
    task = gen.gen_reminders_list()
    errors = sibb_spec.validate_spec(task.initial_state.spec)
    assert errors == [], (
        f"gen_reminders_list(seed={seed}) emitted invalid spec:\n  "
        + "\n  ".join(errors)
        + f"\n\nFull spec:\n  {task.initial_state.spec!r}"
    )


def test_collected_gen_outputs_cover_all_currently_emitted_kinds():
    # Sanity check that the test seeds exercise the kinds we care about
    # so a future generator regression in a seldom-hit branch (e.g.
    # a dock-noise path) doesn't slip through. We don't require ALL
    # kinds — just at least one Reminders entry and at least one
    # Springboard entry across the seed set.
    import sibb_task_generator_v3 as gen
    seen_apps = set()
    for seed in _GEN_SEEDS:
        random.seed(seed)
        task = gen.gen_reminders_list()
        for entry in task.initial_state.spec:
            seen_apps.add(entry.get("app"))
    assert "Springboard" in seen_apps, (
        "no seed in _GEN_SEEDS produces a Springboard noise entry; "
        "test coverage of Springboard spec validation is empty"
    )
