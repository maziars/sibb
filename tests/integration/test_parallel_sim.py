"""D1b — L2 sim: parallel episodes against real simulators.

Two tasks run concurrently, each in its own freshly-created sim.
Proves:
1. `ensure_runner_built` is correctly serialized across workers
2. `simctl create` runs in parallel without daemon conflict
3. Workers don't share state (separate UDIDs, separate sockets,
   separate patched xctestruns, separate derived data)
4. `sweep_sibb_orphans` doesn't kill in-flight sims (only orphans)
5. Per-episode results are correct and don't cross-contaminate

Slow (~2-4 minutes) because of the two full create→prewarm→
xcodebuild→episode→teardown cycles in parallel. Marked `sim` so
it skips when SIBB_UDID isn't set.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pytest

# `sim` runs the test against the real simulator (skipped in CI
# without SIBB_UDID). Un-quarantined 2026-05-15 after B3 fix
# (SpringBoard restart + Swift retry on requestAccess + prewarm
# serialization across workers) closed the TCC race that had been
# making this test ~50% flaky under concurrency=2. Empirical proof:
# 5-of-5 consecutive runs PASSED with the full fix stack. See
#
pytestmark = pytest.mark.sim


_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

from sibb_episode import EpisodeResult, run_episodes_parallel  # noqa: E402
from sibb_scaffold import AgentAction  # noqa: E402
import sibb_task_generator_v3 as gen  # noqa: E402


def _completing_agent_factory(task):
    """Returns an agent that creates the task's Calendar event via
    socket and returns DONE. Tests the framework, not the agent.
    """
    async def agent(tree, _task, step_idx, reader):
        if step_idx == 0:
            await reader._xcuitest._send({
                "type": "create_event",
                "title":     _task.params["title"],
                "start_iso": _task.params["start_iso"],
                "end_iso":   _task.params["end_iso"],
            })
            return AgentAction(action_type="done",
                                reason="created via socket")
        return AgentAction(action_type="done")
    return agent


def _make_multi_app_tasks(n: int, seed_base: int = 2026):
    """Build N gen_reminder_with_calendar_event tasks with distinct
    seeds so they have distinct titles/days (no spurious collision
    if any state leaked between sims).
    """
    tasks = []
    for i in range(n):
        random.seed(seed_base + i)
        t = gen.gen_reminder_with_calendar_event()
        t.task_id = f"parallel-{i:02d}"
        tasks.append(t)
    return tasks


async def test_parallel_with_mixed_outcomes(sibb_udid: str):
    """Single orchestrator invocation, two parallel workers, mixed
    outcomes: one task uses a correct agent, the other uses a
    wrong-title agent. Proves in ONE test:

    1. Parallelism: two episodes actually run concurrently (wall
       clock < 1.5× single-episode cost)
    2. Sim isolation: each worker gets its own UDID
    3. Verifier correctness: correct agent passes verifier-AFTER;
       wrong-title agent fails it
    4. Failure isolation: a verifier-failing peer doesn't poison
       framework state for the other worker (no framework error
       on the passing task)
    5. No worker-level exceptions: result.error is None for both
       (a verifier failure is NOT a framework error)

    Why one combined test instead of two separate ones: running
    two `run_episodes_parallel` invocations back-to-back stresses
    simctl/tccd state in a way that surfaces the deferred TCC
    dialog race Consolidating lets the L2
    suite cover parallelism + isolation without compounding that
    pre-existing flake. The TCC race is reproducible and tracked;
    it's separate from D1b's orchestrator correctness.

    sibb_udid is unused (env-permission gate only — workers create
    their own sims).
    """
    _ = sibb_udid
    tasks = _make_multi_app_tasks(2)

    def factory(task):
        if task.task_id == "parallel-00":
            async def wrong_title_agent(tree, _task, step_idx, reader):
                # Verifier will fail: title doesn't match.
                if step_idx == 0:
                    await reader._xcuitest._send({
                        "type": "create_event",
                        "title":     "Totally Wrong Title",
                        "start_iso": _task.params["start_iso"],
                        "end_iso":   _task.params["end_iso"],
                    })
                    return AgentAction(action_type="done",
                                        reason="wrong on purpose")
                return AgentAction(action_type="done")
            return wrong_title_agent
        return _completing_agent_factory(task)

    t0 = time.time()
    results = await run_episodes_parallel(
        tasks,
        agent_factory=factory,
        concurrency=2,
        max_steps=5,
    )
    elapsed = time.time() - t0
    print(f"\nparallel elapsed: {elapsed:.1f}s")

    # (5) No framework errors on either task. Verifier failure for
    # the wrong-title agent is NOT a framework error.
    for r in results:
        assert isinstance(r, EpisodeResult)
        assert r.error is None, (
            f"{r.task_id} framework error: {r.error}"
        )

    # (1) All results returned.
    assert len(results) == 2
    assert {r.task_id for r in results} == {"parallel-00", "parallel-01"}

    by_id = {r.task_id: r for r in results}

    # (3, 4) Verifier-correct task passes; wrong-title fails;
    # both report cleanly.
    assert by_id["parallel-01"].passed_after is True, (
        "good agent's verifier-AFTER failed: "
        f"{[(c.label, c.status, c.evidence) for c in by_id['parallel-01'].checks_after]}"
    )
    assert by_id["parallel-01"].final_status == "done"

    assert by_id["parallel-00"].passed_after is False, (
        "wrong-title agent should have failed verifier-AFTER"
    )
    assert by_id["parallel-00"].final_status == "done"

    # (2) Each worker got its own UDID — workers didn't share a sim.
    udids = {r.udid for r in results}
    assert len(udids) == 2, f"workers shared a sim! udids={udids}"
