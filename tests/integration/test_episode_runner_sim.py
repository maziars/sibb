"""D1a — L2 sim: programmatic episode runner full lifecycle.

Two paths:
1. udid provided (uses existing SIBB_UDID sim) — fast (~5s)
2. udid=None (full create→boot→runner→...→delete cycle) — slow
   (~30-60s) but tests the sim lifecycle path the parallel
   orchestrator (D1b) will use.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim


_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

from sibb_episode import EpisodeResult, run_episode_scripted  # noqa: E402
from sibb_scaffold import AXReader, AgentAction  # noqa: E402
import sibb_task_generator_v3 as gen  # noqa: E402


@pytest_asyncio.fixture(scope="session")
async def episode_reader(sibb_udid: str):
    """Session-scoped AXReader injected into run_episode_scripted.

    Sharing one reader across tests in this file avoids re-spinning
    a new XCUITest server process per test, which empirically
    triggers per-bundle `requestAccess` denials when restarted
    rapidly on the same sim.
    """
    r = AXReader(sibb_udid)
    await r.start(bundle_id="com.apple.reminders")
    try:
        yield r
    finally:
        await r.stop()


def _scripted_agent_completes_via_socket():
    """An 'agent' that bypasses the action grammar — it calls the
    XCUITest socket directly to create the Calendar event the task
    asks for, then returns DONE. Tests the framework, not the agent.
    """
    async def agent(tree, task, step_idx, reader):
        if step_idx == 0:
            # Direct socket call — exactly what a real agent's
            # TAP/TYPE sequence would eventually produce via the
            # Calendar UI, just compressed for the test.
            await reader._xcuitest._send({
                "type": "create_event",
                "title":     task.params["title"],
                "start_iso": task.params["start_iso"],
                "end_iso":   task.params["end_iso"],
            })
            return AgentAction(
                action_type="done",
                reason="created via direct socket (test shortcut)",
            )
        return AgentAction(action_type="done", reason="already done")
    return agent


def _scripted_agent_immediately_done():
    """No-op agent — returns DONE on first step. Verifier-AFTER
    should fail because the agent didn't actually do anything."""
    async def agent(tree, task, step_idx, reader):
        return AgentAction(action_type="done", reason="lazy")
    return agent


@pytest.fixture
def multi_app_task():
    random.seed(2026)
    task = gen.gen_reminder_with_calendar_event()
    task.task_id = "d1a-sim-test"
    return task


# ───────────────── happy path with existing UDID ──────────────────────

async def test_episode_runs_end_to_end_with_existing_udid(
    episode_reader, multi_app_task,
):
    # Re-roll task_id per test so sim names don't collide (cosmetic).
    multi_app_task.task_id = "episode-completes"
    result = await run_episode_scripted(
        multi_app_task,
        _scripted_agent_completes_via_socket(),
        reader=episode_reader,
        max_steps=5,
    )
    assert isinstance(result, EpisodeResult)
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.final_status == "done", (
        f"expected done, got {result.final_status}")
    # Before: Calendar event doesn't exist yet → fail.
    assert result.passed_before is False, (
        "verifier-BEFORE passed unexpectedly — task pre-completed footgun")
    # After: agent created event → pass.
    assert result.passed_after is True, (
        f"verifier-AFTER did not pass: "
        f"{[(c.label, c.status, c.evidence) for c in result.checks_after]}"
    )
    assert result.steps_taken == 1
    assert result.udid == episode_reader.udid


async def test_lazy_agent_does_not_pass_verifier(
    episode_reader, multi_app_task,
):
    multi_app_task.task_id = "lazy-agent"
    result = await run_episode_scripted(
        multi_app_task,
        _scripted_agent_immediately_done(),
        reader=episode_reader,
        max_steps=5,
    )
    assert result.final_status == "done"
    assert result.passed_after is False, (
        "lazy agent (no Calendar action) should fail verifier-AFTER"
    )


async def test_no_agent_mode_runs_verifier_twice(
    episode_reader, multi_app_task,
):
    multi_app_task.task_id = "no-agent"
    result = await run_episode_scripted(
        multi_app_task,
        agent_fn=None,
        reader=episode_reader,
    )
    assert result.final_status == "no_agent"
    # Both verifier passes ran; both should produce the same blocking
    # result (no agent action between them).
    assert result.passed_before == result.passed_after
    assert len(result.checks_before) == len(result.checks_after)
    assert result.steps_taken == 0
    assert result.agent_actions == []


# ─────────────── full create-destroy lifecycle (slow) ────────────────

async def test_runner_owns_sim_lifecycle(sibb_udid: str, multi_app_task):
    """The OTHER path — udid=None means we create+delete our own sim.

    Slow (~30-90s) because we go through full simctl create → boot →
    xcodebuild → install runner → episode → delete. This is the path
    the parallel orchestrator (D1b) will use for every worker.

    We pass sibb_udid only as the env-permission gate (not used).
    """
    _ = sibb_udid
    result = await run_episode_scripted(
        multi_app_task,
        _scripted_agent_completes_via_socket(),
        udid=None,    # own the sim
        max_steps=5,
    )
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.final_status == "done"
    assert result.passed_after is True
    # The UDID was created by the runner; we don't know it ahead of
    # time, but it should be set in the result.
    assert result.udid and len(result.udid) >= 36
    # By now the sim has been deleted. Sanity-check via simctl list
    # — the udid should NOT appear in the booted list anymore.
    # (Skipping that explicit check to keep the test simple; the
    # delete is in a `finally` block and best-effort by design.)
