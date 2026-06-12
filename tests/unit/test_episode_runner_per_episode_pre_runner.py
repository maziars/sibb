"""Phase 2 item #5 — per-episode baseline reset.

`sibb_episode_runner.py` runs a batch of N tasks in one sim session.
The historical bug: `apply_pre_runner_setup` (Springboard layout/dock,
which requires the sim to be shut down) was called only for the FIRST
task in the batch. Tasks #2..N inherited task #1's layout instead of
getting their own seed-determined randomization.

These tests pin the per-task contract:
  * Multi-task batch: every task with a pre-runner entry triggers
    `apply_pre_runner_setup` with that task's seed.
  * Single-task batch: behavior unchanged (regression guard).
  * Tasks without pre-runner entries are no-ops (cheap path).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sibb_episode_runner
from sibb_task_generator_v3 import InitialState, Task

pytestmark = pytest.mark.fast


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


def _make_task(task_id: str, spec: Optional[List[Dict[str, Any]]] = None) -> Task:
    """Build a minimal Task with the given initial-state spec."""
    return Task(
        task_id=task_id,
        flow="test_flow",
        apps=["Reminders"],
        instruction="noop",
        verify="noop",
        verify_mode="db_query",
        initial_state=InitialState(spec=list(spec or [])),
        steps=1,
        complexity=0.0,
        detail_level=0.0,
        params={},
    )


def _layout_entry(seed: int) -> Dict[str, Any]:
    """Springboard layout entry — pre-runner per HANDLERS registry."""
    return {"app": "Springboard", "type": "layout", "seed": seed}


def _start_page_entry() -> Dict[str, Any]:
    """Springboard start_page entry — NOT pre-runner (runtime apply)."""
    return {"app": "Springboard", "type": "start_page", "page": 0}


# ──────────────────────────────────────────────────────────────────────
#  _task_has_pre_runner — pure function, no I/O
# ──────────────────────────────────────────────────────────────────────


def test_task_has_pre_runner_true_for_layout():
    task = _make_task("t1", [_layout_entry(seed=7)])
    assert sibb_episode_runner._task_has_pre_runner(task) is True


def test_task_has_pre_runner_false_for_runtime_entry():
    task = _make_task("t1", [_start_page_entry()])
    assert sibb_episode_runner._task_has_pre_runner(task) is False


def test_task_has_pre_runner_false_for_empty_spec():
    task = _make_task("t1", [])
    assert sibb_episode_runner._task_has_pre_runner(task) is False


# ──────────────────────────────────────────────────────────────────────
#  _prepare_task_pre_runner — the core fix
# ──────────────────────────────────────────────────────────────────────


def _patch_runner_io():
    """Patch every I/O-touching call _prepare_task_pre_runner uses.

    Returns a context manager exposing:
      patches.apply_pre_runner  — MagicMock recording (udid, task) calls
      patches.reader_instances  — list of AXReader fakes constructed
    """
    class _Patches:
        apply_pre_runner: MagicMock
        reader_instances: List[MagicMock]

    holder = _Patches()
    holder.apply_pre_runner = MagicMock(
        return_value={"applied": [], "errors": []})
    holder.reader_instances = []

    def _fake_reader_ctor(udid):
        r = MagicMock()
        r.udid = udid
        r.start = AsyncMock()
        r.stop = AsyncMock()
        holder.reader_instances.append(r)
        return r

    p1 = patch.object(
        sibb_episode_runner, "apply_pre_runner_setup",
        holder.apply_pre_runner)
    p2 = patch.object(
        sibb_episode_runner, "AXReader", side_effect=_fake_reader_ctor)

    class _Ctx:
        def __enter__(self):
            p1.start()
            p2.start()
            return holder

        def __exit__(self, *exc):
            p2.stop()
            p1.stop()
            return False

    return _Ctx()


async def test_multi_task_batch_each_task_invokes_pre_runner_with_own_seed():
    """The regression we're fixing: a batch of 3 tasks where every task
    carries a Springboard layout entry should call
    `apply_pre_runner_setup` THREE times — once per task — and each
    call must see the per-task seed.
    """
    tasks = [
        _make_task("t1", [_layout_entry(seed=11)]),
        _make_task("t2", [_layout_entry(seed=22)]),
        _make_task("t3", [_layout_entry(seed=33)]),
    ]

    with _patch_runner_io() as P:
        reader: Optional[Any] = None
        for i, task in enumerate(tasks, 1):
            reader = await sibb_episode_runner._prepare_task_pre_runner(
                "UDID-X", task, reader, i)

    # apply_pre_runner_setup called exactly once per task.
    assert P.apply_pre_runner.call_count == 3

    # Each call must have been with the matching task object — i.e.
    # the seed that flows through is per-task, not inherited from t1.
    seeds_seen = []
    for call in P.apply_pre_runner.call_args_list:
        args, _ = call
        assert args[0] == "UDID-X"
        passed_task = args[1]
        # The spec carries the seed; that's what downstream code reads.
        spec = passed_task.initial_state.spec
        assert len(spec) == 1
        seeds_seen.append(spec[0]["seed"])

    assert seeds_seen == [11, 22, 33], (
        f"each task's seed must reach apply_pre_runner_setup; got {seeds_seen}")


async def test_multi_task_batch_reader_stopped_between_pre_runner_tasks():
    """A task with pre-runner entries needs the sim shut down. The
    runner must stop the prior reader before applying pre-runner
    setup (otherwise its socket dies mid-shutdown and the next
    observe hangs).
    """
    tasks = [
        _make_task("t1", [_layout_entry(seed=1)]),
        _make_task("t2", [_layout_entry(seed=2)]),
    ]

    with _patch_runner_io() as P:
        r1 = await sibb_episode_runner._prepare_task_pre_runner(
            "UDID-X", tasks[0], None, 1)
        r2 = await sibb_episode_runner._prepare_task_pre_runner(
            "UDID-X", tasks[1], r1, 2)

    # Two distinct reader instances should have been constructed.
    assert len(P.reader_instances) == 2
    assert r1 is P.reader_instances[0]
    assert r2 is P.reader_instances[1]

    # The first reader must have been stopped before the second one
    # was started (sim shutdown contract).
    r1.stop.assert_awaited_once()
    r1.start.assert_awaited_once()
    r2.start.assert_awaited_once()


async def test_task_without_pre_runner_reuses_existing_reader():
    """A task with no pre-runner entries (only runtime spec) must NOT
    trigger a sim shutdown — the existing reader is reused. This is
    the cheap path that lets a batch avoid ~15s of shutdown/boot
    cycles when no Springboard randomization is needed.
    """
    tasks = [
        _make_task("t1", [_layout_entry(seed=1)]),     # forces fresh reader
        _make_task("t2", [_start_page_entry()]),       # runtime entry only
        _make_task("t3", []),                          # empty spec
    ]

    with _patch_runner_io() as P:
        reader: Optional[Any] = None
        r_after_t1 = await sibb_episode_runner._prepare_task_pre_runner(
            "UDID-X", tasks[0], reader, 1)
        r_after_t2 = await sibb_episode_runner._prepare_task_pre_runner(
            "UDID-X", tasks[1], r_after_t1, 2)
        r_after_t3 = await sibb_episode_runner._prepare_task_pre_runner(
            "UDID-X", tasks[2], r_after_t2, 3)

    # Only task 1 needed the pre-runner shutdown/apply/boot cycle.
    assert P.apply_pre_runner.call_count == 1
    # Only one reader instance — t2/t3 reused it.
    assert len(P.reader_instances) == 1
    # The single reader was started ONCE, never stopped.
    P.reader_instances[0].start.assert_awaited_once()
    P.reader_instances[0].stop.assert_not_awaited()
    # And the same reader object propagated through every prepare call.
    assert r_after_t1 is r_after_t2 is r_after_t3


# ──────────────────────────────────────────────────────────────────────
#  Single-task batch regression guard
# ──────────────────────────────────────────────────────────────────────


async def test_single_task_batch_calls_pre_runner_once():
    """Single-task batch behaviour must be unchanged: exactly one
    `apply_pre_runner_setup` call, exactly one reader started.
    """
    tasks = [_make_task("t1", [_layout_entry(seed=99)])]

    with _patch_runner_io() as P:
        reader: Optional[Any] = None
        reader = await sibb_episode_runner._prepare_task_pre_runner(
            "UDID-X", tasks[0], reader, 1)

    assert P.apply_pre_runner.call_count == 1
    args, _ = P.apply_pre_runner.call_args
    assert args[0] == "UDID-X"
    assert args[1].initial_state.spec[0]["seed"] == 99
    assert len(P.reader_instances) == 1
    P.reader_instances[0].start.assert_awaited_once()
    P.reader_instances[0].stop.assert_not_awaited()


async def test_single_task_batch_with_no_pre_runner_still_starts_reader():
    """First-task path with NO pre-runner entries must still start a
    reader (the loop has nothing to reuse). `apply_pre_runner_setup`
    is still called once to keep reporting uniform, but it's a no-op.
    """
    task = _make_task("t1", [_start_page_entry()])

    with _patch_runner_io() as P:
        reader = await sibb_episode_runner._prepare_task_pre_runner(
            "UDID-X", task, None, 1)

    assert P.apply_pre_runner.call_count == 1
    assert len(P.reader_instances) == 1
    assert reader is P.reader_instances[0]
    P.reader_instances[0].start.assert_awaited_once()
