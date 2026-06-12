"""D1.5 hardening — regression tests for the bugs caught by the
5-critic review of D1c/D1a.

Each test pins a specific class of bug surfaced by the review.
Failures here mean the parallel-readiness guarantee D1.5 was
supposed to add has regressed.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.fast


# ─────────────────────── #6 find_xctestrun filter ────────────────────

def test_find_xctestrun_skips_per_udid_patched_copies(tmp_path):
    """find_xctestrun must NOT return `sibb_<UDID>.xctestrun` copies.

    Under N parallel workers, returning a patched copy as the
    "master" causes patch_xctestrun to overwrite another worker's
    UDID injection — manifests as intermittent socket-not-created
    failures and cross-UDID contamination. This was Critic #1's
    TOP RISK.
    """
    import sibb_xcuitest_client as xcc

    # Layout under tmp_path:
    #   master.xctestrun                ← should be returned
    #   sibb_FAKE-UDID-A.xctestrun     ← patched copy, MUST be skipped
    #   sibb_FAKE-UDID-B.xctestrun     ← patched copy, MUST be skipped
    (tmp_path / "master.xctestrun").write_text("master")
    (tmp_path / "sibb_FAKE-UDID-A.xctestrun").write_text("A")
    (tmp_path / "sibb_FAKE-UDID-B.xctestrun").write_text("B")

    with patch.object(xcc, "BUILD_DIR", str(tmp_path)):
        found = xcc.find_xctestrun()
    assert found is not None
    assert found.endswith("master.xctestrun"), (
        f"find_xctestrun returned a patched copy: {found!r}"
    )


def test_find_xctestrun_returns_none_when_only_patched_copies(tmp_path):
    import sibb_xcuitest_client as xcc
    (tmp_path / "sibb_X.xctestrun").write_text("")
    with patch.object(xcc, "BUILD_DIR", str(tmp_path)):
        assert xcc.find_xctestrun() is None


# ─────────────── #7 ensure_runner_permissions async ─────────────────

def test_ensure_runner_permissions_is_coroutine():
    import inspect
    import sibb_xcuitest_client as xcc
    assert inspect.iscoroutinefunction(xcc.ensure_runner_permissions), (
        "ensure_runner_permissions must be async — sync subprocess "
        "blocks the event loop and stalls parallel workers"
    )


async def test_ensure_runner_permissions_calls_simctl_per_service():
    import sibb_xcuitest_client as xcc

    spawned = []

    async def fake_exec(*args, **kwargs):
        spawned.append(args)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.kill = MagicMock()
        return proc

    with patch.object(asyncio, "create_subprocess_exec",
                       AsyncMock(side_effect=fake_exec)):
        # Fake collect_tcc_services to return a known set.
        with patch("sibb_state.collect_tcc_services",
                    return_value=["reminders", "calendar"]):
            await xcc.ensure_runner_permissions("FAKE-UDID")

    # One simctl invocation per service that the runner needs, PLUS
    # one unconditional `location → com.apple.Maps` grant added at
    # the end of `ensure_runner_permissions` so Maps' directions flow
    # doesn't hit the "Location Services is Off" prompt (added
    # 2026-05-27 for Phase 3 messaging variant D). So with 2 mocked
    # services we expect exactly 3 spawns total.
    assert len(spawned) == 3
    runner_grants = [a for a in spawned
                      if "com.sibb.tests.xctrunner" in " ".join(map(str, a))]
    maps_grants = [a for a in spawned
                    if "com.apple.Maps" in " ".join(map(str, a))]
    assert len(runner_grants) == 2, (
        "expected one runner-bundle grant per mocked service "
        "(reminders + calendar)")
    assert len(maps_grants) == 1, (
        "expected the unconditional location → com.apple.Maps grant")
    for call_args in spawned:
        flat = " ".join(str(a) for a in call_args)
        assert "simctl" in flat
        assert "privacy" in flat
        assert "grant" in flat
        assert "FAKE-UDID" in flat
    # Verify the Maps grant targets `location`.
    maps_flat = " ".join(str(a) for a in maps_grants[0])
    assert "location" in maps_flat


# ──────────────── #9 process-group cleanup ──────────────────────────

def test_xcuitest_reader_start_uses_start_new_session():
    """xcodebuild must be in its own session group so killpg works."""
    import sibb_xcuitest_client as xcc
    src = pathlib.Path(xcc.__file__).read_text()
    # Find the create_subprocess_exec for xcodebuild and assert the
    # keyword is present somewhere in the kwargs block. The closing
    # `)` of create_subprocess_exec ends the kwargs; bound the search
    # there rather than guessing a char count.
    idx = src.find('"xcodebuild", "test-without-building"')
    assert idx > 0, "xcodebuild test-without-building invocation moved"
    # Find the matching close-paren that ends create_subprocess_exec.
    end = src.find("\n        )", idx)
    assert end > idx
    window = src[idx:end]
    assert "start_new_session=True" in window, (
        "xcodebuild must be spawned with start_new_session=True; "
        "without it, killpg on stop() can't reap the xctest tree"
    )


def test_xcuitest_reader_has_kill_proc_group():
    """`stop()` must delegate to `_kill_proc_group`, not raw `kill()`."""
    import sibb_xcuitest_client as xcc
    assert hasattr(xcc.XCUITestReader, "_kill_proc_group")
    import inspect
    assert inspect.iscoroutinefunction(
        xcc.XCUITestReader._kill_proc_group)
    src = pathlib.Path(xcc.__file__).read_text()
    # Find the stop() method and assert it references _kill_proc_group.
    stop_idx = src.find("async def stop(self):")
    assert stop_idx > 0
    stop_body = src[stop_idx:stop_idx + 1000]
    assert "_kill_proc_group" in stop_body, (
        "stop() must call self._kill_proc_group() so xctest children "
        "get reaped along with the xcodebuild parent"
    )


# ──────────────── #10 orphan sweeper ────────────────────────────────

async def test_sweep_returns_count_report():
    import sibb_simctl

    async def fake_simctl(*args, **kwargs):
        return (0, '{"devices": {}}', "")

    with patch.object(sibb_simctl, "_run_simctl",
                       AsyncMock(side_effect=fake_simctl)), \
         patch.object(sibb_simctl, "simctl_shutdown", AsyncMock()), \
         patch.object(sibb_simctl, "simctl_delete", AsyncMock()):
        report = await sibb_simctl.sweep_sibb_orphans()
    assert isinstance(report, dict)
    for key in ("sims_deleted", "sockets_removed", "dd_dirs_removed",
                 "logs_removed", "patched_xctestruns_removed"):
        assert key in report
        assert isinstance(report[key], int)


async def test_sweep_deletes_only_sibb_named_sims():
    import sibb_simctl

    sim_json = {
        "devices": {
            "com.apple.x.iOS-26-3": [
                {"name": "iPhone (user)",
                 "udid": "USER-UDID", "state": "Shutdown"},
                {"name": "SIBB-Episode-task1",
                 "udid": "EP-UDID-1", "state": "Shutdown"},
                {"name": "SIBB-Build-Temp",
                 "udid": "BUILD-UDID", "state": "Shutdown"},
                {"name": "Something Else",
                 "udid": "OTHER-UDID", "state": "Shutdown"},
            ]
        }
    }

    deleted_udids = []

    async def fake_simctl(*args, **kwargs):
        if args[0] == "list":
            import json as _json
            return (0, _json.dumps(sim_json), "")
        return (0, "", "")

    async def fake_delete(udid, **kwargs):
        deleted_udids.append(udid)

    with patch.object(sibb_simctl, "_run_simctl",
                       AsyncMock(side_effect=fake_simctl)), \
         patch.object(sibb_simctl, "simctl_shutdown", AsyncMock()), \
         patch.object(sibb_simctl, "simctl_delete",
                       AsyncMock(side_effect=fake_delete)):
        await sibb_simctl.sweep_sibb_orphans()

    assert "EP-UDID-1" in deleted_udids
    assert "BUILD-UDID" in deleted_udids
    assert "USER-UDID" not in deleted_udids
    assert "OTHER-UDID" not in deleted_udids


async def test_sweep_removes_stale_patched_xctestruns(tmp_path):
    import sibb_simctl
    # Two patched copies + one master, all in tmp_path:
    (tmp_path / "sibb_LEAK-A.xctestrun").write_text("")
    (tmp_path / "sibb_LEAK-B.xctestrun").write_text("")
    (tmp_path / "master.xctestrun").write_text("")

    async def fake_simctl(*args, **kwargs):
        return (0, '{"devices": {}}', "")

    with patch.object(sibb_simctl, "BUILD_PRODUCTS_DIR", tmp_path), \
         patch.object(sibb_simctl, "_run_simctl",
                       AsyncMock(side_effect=fake_simctl)):
        report = await sibb_simctl.sweep_sibb_orphans()

    assert report["patched_xctestruns_removed"] == 2
    # Master MUST survive.
    assert (tmp_path / "master.xctestrun").exists()
    # Leaked copies gone.
    assert not (tmp_path / "sibb_LEAK-A.xctestrun").exists()
    assert not (tmp_path / "sibb_LEAK-B.xctestrun").exists()


# ──────────────── #11 asyncio.get_running_loop ──────────────────────

def test_simctl_wait_booted_uses_running_loop():
    """No `asyncio.get_event_loop()` — fails on Python 3.12+."""
    import pathlib
    src = pathlib.Path("sibb/simulator/sibb_simctl.py").read_text()
    # Find simctl_wait_booted body.
    idx = src.find("async def simctl_wait_booted")
    assert idx > 0
    end = src.find("\nasync def ", idx + 1)
    body = src[idx:end if end > 0 else idx + 2000]
    assert "asyncio.get_event_loop()" not in body, (
        "simctl_wait_booted uses deprecated asyncio.get_event_loop()"
    )
    assert "get_running_loop" in body


# ──────────────── #12 baseline+clone gate (post-F1) ──────────────────

def test_episode_runner_acquires_clone_on_fresh_sim_unconditional():
    """Post-F1: own_sim must acquire a clone of the prewarmed baseline,
    not pay per-episode prewarm. The original D1.5 contract (#12) was
    "prewarm runs for every fresh sim regardless of pre_runner state"
    — F1 preserves the property by baking prewarm into the baseline
    and cloning from it unconditionally.

    Critic #5 (unchanged motivation): a fresh-sim task with Springboard
    layout/dock entries must still arrive at the agent loop with TCC
    granted + first-run dialogs dismissed. With F1 those preconditions
    live in the baseline, so `acquire_clone` is the right thing to
    assert here.
    """
    import pathlib
    src = pathlib.Path("sibb/benchmark/sibb_episode.py").read_text()
    idx = src.find("if own_sim:")
    assert idx > 0
    end = src.find("\n    result = EpisodeResult(", idx)
    window = src[idx:end if end > 0 else idx + 4000]
    assert "acquire_clone(" in window, (
        "acquire_clone must be called inside the `if own_sim:` block "
        "(F1 contract: clone from baseline instead of per-episode prewarm)"
    )
    assert "ensure_baseline_sim(" in window, (
        "ensure_baseline_sim must be called inside `if own_sim:` so "
        "the baseline is guaranteed to exist before acquire_clone"
    )
    # No pre_report gate on the clone acquisition. Find the call and
    # check no `pre_report` appears between `if own_sim:` and it.
    clone_idx = window.find("acquire_clone(")
    preceding = window[:clone_idx]
    last_pre_report = preceding.rfind("pre_report")
    last_own_sim = preceding.rfind("if own_sim:")
    assert last_pre_report < last_own_sim, (
        "acquire_clone appears gated on pre_report — should be "
        "unconditional inside the own_sim branch"
    )


# ──────────────── #13 BaselineSnapshot wiring ───────────────────────

def test_baseline_resources_extracts_identity_resources():
    from sibb_episode import _baseline_resources_for

    checks = [
        {"kind": "exists", "resource": "reminders.lists",
         "selector": {"name": "X"}},
        {"kind": "identity", "resource": "reminders.lists"},
        {"kind": "identity", "resource": "calendar.events"},
        {"kind": "count", "resource": "reminders.items",
         "op": "eq", "n": 0},
    ]
    out = _baseline_resources_for(checks)
    assert out == {"reminders.lists", "calendar.events"}


def test_baseline_resources_ignores_unknown_resources():
    from sibb_episode import _baseline_resources_for
    checks = [
        {"kind": "identity", "resource": "imaginary.thing"},
        {"kind": "identity", "resource": "reminders.lists"},
    ]
    out = _baseline_resources_for(checks)
    # Unknown resource is silently dropped so the runner doesn't
    # try to capture from a non-existent fetcher.
    assert out == {"reminders.lists"}


def test_baseline_resources_empty_when_no_identity_checks():
    from sibb_episode import _baseline_resources_for
    checks = [{"kind": "exists", "resource": "reminders.lists"}]
    assert _baseline_resources_for(checks) == set()


def test_baseline_resources_empty_when_no_checks():
    from sibb_episode import _baseline_resources_for
    assert _baseline_resources_for([]) == set()
    assert _baseline_resources_for(None) == set()


# ──────────────── #14 AbortEpisode for connection failures ──────────

def test_abort_episode_is_exception_subclass():
    from sibb_episode import AbortEpisode
    assert issubclass(AbortEpisode, Exception)


def test_abort_episode_distinct_from_runtimeerror():
    """AbortEpisode must not be confused with generic RuntimeError —
    the runner branches on it to set final_status='connection_lost'.
    """
    from sibb_episode import AbortEpisode
    assert not issubclass(AbortEpisode, RuntimeError)


def test_connection_failure_exc_includes_brokenpipe():
    from sibb_episode import _CONNECTION_FAILURE_EXC
    assert BrokenPipeError in _CONNECTION_FAILURE_EXC
    assert ConnectionResetError in _CONNECTION_FAILURE_EXC


async def test_agent_loop_raises_abortepisode_on_connection_failure():
    """When reader.read() raises BrokenPipeError, the loop must
    raise AbortEpisode instead of grinding through max_steps.
    """
    from sibb_episode import (
        AbortEpisode,
        EpisodeResult,
        _run_agent_loop,
    )
    from sibb_scaffold import AgentAction

    reader = MagicMock()

    async def dead_read():
        raise BrokenPipeError("simulated socket death")

    reader.read = dead_read

    async def agent(*_a, **_k):
        return AgentAction(action_type="tap", target_label="X")

    result = EpisodeResult(task_id="t", apps=[], udid="X")
    with pytest.raises(AbortEpisode, match="reader.read"):
        await _run_agent_loop(reader, MagicMock(), agent,
                               result, max_steps=50)


async def test_agent_loop_swallows_recoverable_execute_failure():
    """Generic action failure (element not hittable, etc.) must NOT
    end the episode — only connection-level errors do.
    """
    from sibb_episode import EpisodeResult, _run_agent_loop
    from sibb_scaffold import AgentAction

    reader = MagicMock()
    reader.read = AsyncMock(return_value=MagicMock())
    step_count = {"n": 0}

    async def agent(*_a, **_k):
        step_count["n"] += 1
        if step_count["n"] >= 3:
            return AgentAction(action_type="done", reason="ok")
        return AgentAction(action_type="tap", target_label="X")

    # Patch the lazy-imported execute to always raise a generic
    # exception (NOT a connection error).
    with patch("sibb_replay.execute",
                AsyncMock(side_effect=ValueError("element disabled"))):
        result = EpisodeResult(task_id="t", apps=[], udid="X")
        await _run_agent_loop(reader, MagicMock(), agent,
                               result, max_steps=10)
    assert result.final_status == "done"
    assert result.steps_taken == 3
