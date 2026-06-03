"""D1a — `EpisodeResult` shape + agent-loop import sanity (no sim)."""

from __future__ import annotations

import inspect

import pytest

from sibb_episode import (
    AgentFn,
    EpisodeResult,
    _run_agent_loop,
    run_episode_scripted,
)

pytestmark = pytest.mark.fast


def test_episode_result_defaults():
    r = EpisodeResult(task_id="t1", apps=["Reminders"], udid="X")
    assert r.task_id == "t1"
    assert r.apps == ["Reminders"]
    assert r.udid == "X"
    assert r.passed_before is False
    assert r.passed_after is False
    assert r.checks_before == []
    assert r.checks_after == []
    assert r.agent_actions == []
    assert r.steps_taken == 0
    assert r.final_status == "error"
    assert r.error is None


def test_episode_result_field_mutation_allowed():
    r = EpisodeResult(task_id="t1", apps=[], udid="X")
    r.passed_after = True
    r.steps_taken = 5
    r.final_status = "done"
    assert r.passed_after is True
    assert r.steps_taken == 5
    assert r.final_status == "done"


def test_run_episode_scripted_signature():
    sig = inspect.signature(run_episode_scripted)
    params = sig.parameters
    assert "task" in params
    assert "agent_fn" in params
    assert "udid" in params
    assert "device_type_substring" in params
    assert "runtime_version" in params
    assert "max_steps" in params
    # All keyword-only args after task and agent_fn are kwonly:
    assert params["udid"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["max_steps"].kind == inspect.Parameter.KEYWORD_ONLY


def test_run_episode_scripted_is_coroutine():
    assert inspect.iscoroutinefunction(run_episode_scripted)


def test_run_agent_loop_is_coroutine():
    assert inspect.iscoroutinefunction(_run_agent_loop)


def test_agent_fn_type_is_callable_alias():
    # AgentFn is a type alias; just confirm it's importable and
    # that the call signature is what callers will rely on. No
    # runtime enforcement at the type-alias level.
    assert AgentFn is not None
