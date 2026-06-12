"""L1 pin: instruction prose for anchor-date generators matches the
anchor date.

The 2026-06-11 bug: `_calendar_anchor_date()` returns a random date in
[today+1, today+30], but three Calendar generators
(gen_create_event_with_title_time, gen_change_event_time,
gen_create_recurring_event) hardcoded the word "tomorrow" in every
instruction template. That meant for any day_offset > 1, the prose
said "tomorrow" but the verifier expected a date 2-30 days out.

The bug was masked for ~3 weeks because agents kept failing these
tasks for OTHER reasons (date hallucination from training cutoff; UI
default behavior; etc.). v3b sim run finally surfaced it once the
agent had a working clock + correctly used system.now.

The fix: a `_day_reference(d)` helper that returns "tomorrow" when
`d == today + 1` and the explicit day label otherwise.

This file pins that contract: every generator using
`_calendar_anchor_date` must NOT hardcode "tomorrow"/"today" in
instruction prose. The test instantiates each generator at multiple
seeds; if any seed produces an anchor offset > 1 AND the instruction
says "tomorrow", we trip the pin.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import random
import re
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(REPO_ROOT / "sibb" / "simulator"))

import sibb_task_generator_v3 as G  # noqa: E402


# Generators known to use _calendar_anchor_date and have instruction
# prose that includes a day reference. Audit this list when adding new
# Calendar generators.
ANCHOR_GENERATORS = [
    "gen_create_event_with_title_time",
    "gen_change_event_time",
    "gen_create_recurring_event",
]

# Seeds to scan. With seed=0 + today=2026-06-11, day_offset is 28 days
# (verified empirically), which lands well outside "tomorrow" territory.
# The other seeds give a spread so we catch the bug in different ways.
SEEDS = (0, 1, 7, 42, 100)


def _instr_says_tomorrow(text: str) -> bool:
    return bool(re.search(r"\btomorrow\b", text, re.IGNORECASE))


def test_day_reference_helper_returns_tomorrow_for_today_plus_one():
    today = _dt.date.today()
    assert G._day_reference(today + _dt.timedelta(days=1)) == "tomorrow"


def test_day_reference_helper_returns_explicit_label_for_offsets_above_one():
    today = _dt.date.today()
    ref = G._day_reference(today + _dt.timedelta(days=5))
    assert ref.startswith("on "), (
        f"day_reference at offset 5 should start with 'on '; got {ref!r}")
    assert "tomorrow" not in ref.lower()


def test_anchor_generators_never_say_tomorrow_when_anchor_is_not_tomorrow():
    """For each generator that uses _calendar_anchor_date, scan multiple
    seeds and verify: if the anchor date is NOT today+1, the instruction
    must NOT contain the word 'tomorrow'."""
    today = _dt.date.today()
    failures = []
    for name in ANCHOR_GENERATORS:
        gen = getattr(G, name, None)
        assert gen is not None, (
            f"generator {name} missing from sibb_task_generator_v3 — "
            f"either it was deleted (drop from ANCHOR_GENERATORS) or "
            f"renamed (update ANCHOR_GENERATORS)")
        for seed in SEEDS:
            random.seed(seed)
            task = gen()
            # Re-derive the anchor date from task.params if exposed,
            # otherwise look at the start_iso from the verifier.
            # Simplest: parse the start_iso check.
            anchor_iso = None
            for c in task.verify_checks:
                if c.get("attr") == "start_iso" and c.get("value"):
                    anchor_iso = c["value"][:10]  # YYYY-MM-DD
                    break
            if anchor_iso is None:
                # Generator doesn't expose a start_iso check; skip.
                continue
            anchor_date = _dt.date.fromisoformat(anchor_iso)
            days = (anchor_date - today).days
            says_tom = _instr_says_tomorrow(task.instruction)
            if days != 1 and says_tom:
                failures.append(
                    f"{name}@seed={seed}: anchor_date={anchor_iso} "
                    f"(today+{days}) but instruction says 'tomorrow':\n"
                    f"    {task.instruction!r}")
    assert not failures, (
        f"{len(failures)} prose-vs-anchor mismatch(es) — see "
        f"`_day_reference` helper in sibb_task_generator_v3.py for the "
        f"fix pattern:\n\n" + "\n\n".join(failures))


def test_anchor_generators_say_tomorrow_when_anchor_is_today_plus_one():
    """The Option C dual: when the anchor DOES land on today+1, the
    prose SHOULD say 'tomorrow' (most natural). This pins the dual
    direction of the contract — if the helper degrades to always
    using day labels, this trips."""
    today = _dt.date.today()
    # Manually trigger an anchor of today+1 by patching the anchor
    # function for one call. (Easier than scanning seeds for one
    # that happens to land at offset 1.)
    import unittest.mock as mock
    tomorrow = today + _dt.timedelta(days=1)
    with mock.patch.object(G, "_calendar_anchor_date",
                            return_value=tomorrow):
        for name in ANCHOR_GENERATORS:
            gen = getattr(G, name)
            random.seed(0)
            task = gen()
            assert _instr_says_tomorrow(task.instruction), (
                f"{name}: anchor==today+1 but instruction did NOT say "
                f"'tomorrow' — _day_reference may have regressed.\n"
                f"  instruction: {task.instruction!r}")
