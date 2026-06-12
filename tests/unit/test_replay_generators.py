"""L1 pin: the cross-app generator registration in sibb_replay.GENERATORS.

`gen_reminder_with_calendar_event` was added to `sibb_task_generator_v3`
and classified in `sibb/api_baseline/classification.yaml` before being
registered in `sibb_replay.GENERATORS`. The API runner's lookup raised
`SystemExit("unknown generator …")` mid-headline, silently killed the
batch (the runner's `except Exception` let SystemExit through).

These tests pin the registration so a future delete of the GENERATORS
entry surfaces as a fast-failing unit test instead of a sim-time crash.
"""

from __future__ import annotations

import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(REPO_ROOT / "sibb" / "simulator"))

import sibb_replay as R  # noqa: E402


def test_gen_reminder_with_calendar_event_registered_in_generators():
    """The 'reminder_with_calendar_event' key must resolve to a
    (generator, verifier) tuple where both halves are callable."""
    assert "reminder_with_calendar_event" in R.GENERATORS
    gen_fn, verifier_fn = R.GENERATORS["reminder_with_calendar_event"]
    assert callable(gen_fn)
    assert callable(verifier_fn)


def test_gen_reminder_with_calendar_event_uses_generic_verifier():
    """All Phase-3 cross-app generators share verify_generic_task_async;
    if a future commit re-routes this one to a bespoke verifier without
    updating the headline classification, surface that here."""
    _, verifier_fn = R.GENERATORS["reminder_with_calendar_event"]
    assert verifier_fn is R.verify_generic_task_async
