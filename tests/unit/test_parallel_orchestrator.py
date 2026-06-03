"""D1b — `run_episodes_parallel` L1 tests.

Mocks `run_episode_scripted` + `sweep_sibb_orphans` + `ensure_runner_built`
so the orchestration logic is tested without spinning sims. The actual
parallel-against-real-sims path is covered by the L2 test.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import sibb_episode
from sibb_episode import EpisodeResult, run_episodes_parallel

pytestmark = pytest.mark.fast


def _make_task(task_id: str, apps=("Reminders",)):
    return SimpleNamespace(task_id=task_id, apps=list(apps),
                            initial_state=SimpleNamespace(spec=[]),
                            verify_checks=[], params={})


def _ok_result(task_id: str) -> EpisodeResult:
    return EpisodeResult(
        task_id=task_id, apps=[], udid="UDID-" + task_id,
        final_status="done", passed_after=True,
    )


# ────────────────────────── empty / trivial paths ────────────────────

async def test_empty_task_list_returns_empty():
    out = await run_episodes_parallel([])
    assert out == []


async def test_no_setup_when_task_list_empty():
    # sweep + ensure_runner_built should not even be called.
    sweep_calls = []
    build_calls = []

    async def fake_sweep():
        sweep_calls.append(True)
        return {}

    async def fake_build(*a, **kw):
        build_calls.append(True)

    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(side_effect=fake_sweep)), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock(side_effect=fake_build)):
        await run_episodes_parallel([])
    assert sweep_calls == []
    assert build_calls == []


# ────────────── startup sequence: sweep then build then workers ──────

async def test_calls_sweep_then_build_then_workers():
    """sweep must complete before ensure_runner_built; both must
    complete before any worker calls run_episode_scripted.
    """
    order: list = []

    async def fake_sweep():
        order.append("sweep")
        return {}

    async def fake_build(*a, **kw):
        order.append("build")

    async def fake_episode(task, agent_fn, **kw):
        order.append(("episode", task.task_id))
        return _ok_result(task.task_id)

    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(side_effect=fake_sweep)), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock(side_effect=fake_build)), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=fake_episode)):
        await run_episodes_parallel([_make_task("a"), _make_task("b")])

    assert order[0] == "sweep"
    assert order[1] == "build"
    # Worker calls follow. Two episode calls total.
    assert len([x for x in order if isinstance(x, tuple)
                and x[0] == "episode"]) == 2


async def test_sweep_at_start_false_skips_sweep():
    sweep_calls = []

    async def fake_sweep():
        sweep_calls.append(True)
        return {}

    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(side_effect=fake_sweep)), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(return_value=_ok_result("t"))):
        await run_episodes_parallel([_make_task("t")],
                                     sweep_at_start=False)
    assert sweep_calls == []


# ────────────────── task distribution + ordering ──────────────────────

async def test_all_tasks_produce_a_result():
    async def fake_episode(task, agent_fn, **kw):
        return _ok_result(task.task_id)

    tasks = [_make_task(f"t{i}") for i in range(5)]
    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(return_value={})), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=fake_episode)):
        results = await run_episodes_parallel(tasks, concurrency=2)
    assert len(results) == 5
    assert {r.task_id for r in results} == {"t0", "t1", "t2", "t3", "t4"}


async def test_concurrency_caps_at_task_count():
    """N tasks with concurrency=8 should NOT spawn 8 workers idling
    on an empty queue. The orchestrator caps at len(tasks).
    """
    spawned_worker_ids: set = set()

    async def fake_episode(task, agent_fn, **kw):
        return _ok_result(task.task_id)

    # Spy on worker IDs by patching to capture them — done via
    # asyncio.current_task name. Simpler: just trust that the
    # `n_workers = max(1, min(concurrency, len(task_list)))` invariant
    # is testable via an indirect signal: spawn count.
    # Instead, monkeypatch asyncio.create_task to record.
    original_create_task = asyncio.create_task
    created = []

    def spy_create_task(coro):
        created.append(coro)
        return original_create_task(coro)

    tasks = [_make_task(f"t{i}") for i in range(3)]
    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(return_value={})), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=fake_episode)), \
         patch.object(asyncio, "create_task", side_effect=spy_create_task):
        await run_episodes_parallel(tasks, concurrency=10)
    # Only 3 workers, even though concurrency=10.
    assert len(created) == 3


# ──────────────────── agent factory plumbing ──────────────────────────

async def test_agent_factory_called_per_task():
    seen_task_ids: list = []

    def factory(task):
        seen_task_ids.append(task.task_id)
        async def agent(*_a, **_k):
            from sibb_scaffold import AgentAction
            return AgentAction(action_type="done")
        return agent

    async def fake_episode(task, agent_fn, **kw):
        assert agent_fn is not None
        return _ok_result(task.task_id)

    tasks = [_make_task(f"t{i}") for i in range(3)]
    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(return_value={})), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=fake_episode)):
        await run_episodes_parallel(tasks, agent_factory=factory,
                                     concurrency=2)
    assert set(seen_task_ids) == {"t0", "t1", "t2"}


async def test_no_agent_factory_means_no_agent_episode():
    captured_agent_fns: list = []

    async def fake_episode(task, agent_fn, **kw):
        captured_agent_fns.append(agent_fn)
        return _ok_result(task.task_id)

    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(return_value={})), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=fake_episode)):
        await run_episodes_parallel(
            [_make_task("t1"), _make_task("t2")],
            agent_factory=None,
        )
    # All agent_fns passed to run_episode_scripted should be None.
    assert all(fn is None for fn in captured_agent_fns)


# ────────────────────── worker exception handling ─────────────────────

async def test_worker_exception_captured_as_error_result():
    """A bare exception from run_episode_scripted (not via its
    normal error-result path) must be captured as a synthesized
    EpisodeResult, not silently dropped.
    """
    async def fake_episode(task, agent_fn, **kw):
        if task.task_id == "bad":
            raise RuntimeError("simulated worker explosion")
        return _ok_result(task.task_id)

    tasks = [_make_task("good"), _make_task("bad"), _make_task("also-good")]
    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(return_value={})), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=fake_episode)):
        results = await run_episodes_parallel(tasks, concurrency=2)

    assert len(results) == 3
    by_id = {r.task_id: r for r in results}
    assert by_id["good"].final_status == "done"
    assert by_id["also-good"].final_status == "done"
    assert by_id["bad"].final_status == "error"
    assert "simulated worker explosion" in by_id["bad"].error


async def test_worker_exception_doesnt_kill_other_workers():
    # One exploding task in the middle of a batch; rest complete.
    async def fake_episode(task, agent_fn, **kw):
        if task.task_id == "explode":
            raise RuntimeError("boom")
        return _ok_result(task.task_id)

    tasks = [_make_task(f"t{i}") for i in range(10)]
    tasks[4] = _make_task("explode")
    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(return_value={})), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=fake_episode)):
        results = await run_episodes_parallel(tasks, concurrency=3)

    assert len(results) == 10
    statuses = [r.final_status for r in results]
    assert statuses.count("done") == 9
    assert statuses.count("error") == 1


# ───────────────────── true parallelism check ─────────────────────────

async def test_workers_run_concurrently():
    """concurrency=N must actually run N episodes concurrently —
    not serialize them. Use a barrier to detect serial execution.
    """
    # Each episode signals "I started" then waits for the barrier
    # to be reached by all workers. If they're serial, the second
    # episode never sees the first's start signal before the
    # timeout fires.
    started = asyncio.Event()
    started_count = {"n": 0}

    async def slow_episode(task, agent_fn, **kw):
        started_count["n"] += 1
        if started_count["n"] >= 2:
            started.set()
        try:
            await asyncio.wait_for(started.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            return EpisodeResult(
                task_id=task.task_id, apps=[], udid="",
                final_status="error",
                error="other worker never started — serialized",
            )
        return _ok_result(task.task_id)

    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(return_value={})), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=slow_episode)):
        results = await run_episodes_parallel(
            [_make_task("a"), _make_task("b")], concurrency=2)
    assert all(r.final_status == "done" for r in results), (
        f"workers serialized: {[(r.task_id, r.final_status, r.error) for r in results]}"
    )


async def test_concurrency_one_runs_serially():
    """concurrency=1 with a synchronization signal — second task
    must wait for first to complete.
    """
    finished_ids: list = []

    async def episode(task, agent_fn, **kw):
        await asyncio.sleep(0.05)  # tiny work
        finished_ids.append(task.task_id)
        return _ok_result(task.task_id)

    with patch.object(sibb_episode, "sweep_sibb_orphans",
                       AsyncMock(return_value={})), \
         patch.object(sibb_episode, "ensure_runner_built",
                       AsyncMock()), \
         patch.object(sibb_episode, "run_episode_scripted",
                       AsyncMock(side_effect=episode)):
        await run_episodes_parallel(
            [_make_task("first"), _make_task("second"),
             _make_task("third")],
            concurrency=1,
        )
    # Single worker → strict completion order matches queue order.
    assert finished_ids == ["first", "second", "third"]
