"""D1c — async simctl wrappers + ensure_runner_built guard.

L1 unit tests with mocked subprocess so the suite stays fast and
runs without xcrun. The actual simctl behavior is covered by the
L2 sim test (which IS gated on the env having Xcode + simulators).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sibb_simctl

pytestmark = pytest.mark.fast


async def _fake_proc(rc: int, stdout: bytes = b"", stderr: bytes = b""):
    """Return a MagicMock that imitates an asyncio subprocess."""
    proc = MagicMock()
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


# ─────────────────────────── simctl_create ────────────────────────────

async def test_simctl_create_returns_udid_from_stdout():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(0, b"UDID-123\n")),
    ):
        udid = await sibb_simctl.simctl_create(
            "Test", "iPhone 17", "iOS 26-3")
    assert udid == "UDID-123"


async def test_simctl_create_raises_on_nonzero_rc():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(1, b"", b"bad type")),
    ):
        with pytest.raises(RuntimeError, match="bad type"):
            await sibb_simctl.simctl_create(
                "Test", "Bogus", "iOS 26-3")


# ─────────────────────────── simctl_boot ──────────────────────────────

async def test_simctl_boot_succeeds_on_rc_zero():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(0)),
    ):
        await sibb_simctl.simctl_boot("UDID-X")


async def test_simctl_boot_idempotent_on_already_booted():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(
            149, b"", b"Unable to boot; current state: Booted")),
    ):
        await sibb_simctl.simctl_boot("UDID-X")   # no raise


async def test_simctl_boot_raises_on_real_failure():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(
            1, b"", b"some real error")),
    ):
        with pytest.raises(RuntimeError, match="some real error"):
            await sibb_simctl.simctl_boot("UDID-X")


# ─────────────────────────── simctl_shutdown ──────────────────────────

async def test_simctl_shutdown_succeeds_on_rc_zero():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(0)),
    ):
        await sibb_simctl.simctl_shutdown("UDID-X")


async def test_simctl_shutdown_swallows_already_shutdown():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(
            149, b"", b"Unable to shutdown; current state: Shutdown")),
    ):
        await sibb_simctl.simctl_shutdown("UDID-X")   # no raise


async def test_simctl_shutdown_swallows_unknown_errors_too():
    # Shutdown during teardown shouldn't mask the underlying error.
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(1, b"", b"whatever")),
    ):
        await sibb_simctl.simctl_shutdown("UDID-X")   # still no raise


# ─────────────────────────── simctl_delete ────────────────────────────

async def test_simctl_delete_is_best_effort():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(1, b"", b"not found")),
    ):
        await sibb_simctl.simctl_delete("UDID-X")   # no raise


# ─────────────────────────── timeouts ────────────────────────────────

async def test_simctl_timeout_raises_runtimeerror():
    # _run_simctl's timeout handler calls proc.kill() then awaits
    # proc.communicate() one more time to drain. The mock must let
    # the second communicate return promptly once kill fires; otherwise
    # we re-hang and the test times out at the test-suite level.
    killed = asyncio.Event()

    async def _hang(*args, **kwargs):
        if killed.is_set():
            return (b"", b"")
        await asyncio.wait_for(killed.wait(), timeout=10.0)
        return (b"", b"")

    slow_proc = MagicMock()
    slow_proc.returncode = 0
    slow_proc.communicate = _hang
    slow_proc.kill = MagicMock(side_effect=lambda: killed.set())
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=slow_proc),
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            await sibb_simctl._run_simctl(
                "foo", timeout=0.1, check=False)


# ─────────────────────────── wait_booted ─────────────────────────────

async def test_simctl_wait_booted_returns_when_booted():
    # Return "Booted" on first poll.
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(
            0, b"iPhone 17 (UDID) (Booted)")),
    ):
        await sibb_simctl.simctl_wait_booted("UDID-X", timeout=2.0)


async def test_simctl_wait_booted_times_out_if_never_booted():
    with patch.object(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=await _fake_proc(
            0, b"iPhone 17 (UDID) (Shutdown)")),
    ):
        with pytest.raises(RuntimeError, match="did not reach Booted"):
            await sibb_simctl.simctl_wait_booted("UDID-X", timeout=0.5)


# ─────────────────────────── discovery ───────────────────────────────

def test_list_runtimes_returns_list():
    # Live call — succeeds in any environment with simctl, returns
    # whatever runtimes are available. We just assert the shape.
    out = sibb_simctl.list_runtimes()
    assert isinstance(out, list)
    if out:
        assert all(isinstance(r, dict) for r in out)


def test_find_ios_runtime_id_picks_a_runtime_when_available():
    # Skip if no iOS runtime is installed (CI without simulator).
    runtimes = [r for r in sibb_simctl.list_runtimes()
                 if "iOS" in r.get("name", "")]
    if not runtimes:
        pytest.skip("no iOS runtime installed on this host")
    rid = sibb_simctl.find_ios_runtime_id()
    assert rid is not None
    assert "iOS" in rid


def test_find_ios_runtime_id_returns_none_for_missing_version():
    rid = sibb_simctl.find_ios_runtime_id("999")
    assert rid is None


def test_find_device_type_id_returns_none_for_missing():
    assert sibb_simctl.find_device_type_id("WidgetThatDoesNotExist") is None


# ─────────────────────────── runner build guard ──────────────────────

def test_find_xctestrun_path_returns_none_when_build_missing(tmp_path):
    # Point at an empty dir → no .xctestrun present → None.
    with patch.object(sibb_simctl, "BUILD_PRODUCTS_DIR", tmp_path):
        assert sibb_simctl.find_xctestrun_path() is None


def test_find_xctestrun_path_skips_per_udid_patched_copies(tmp_path):
    # Place only patched copies; finder should NOT return them.
    (tmp_path / "sibb_FAKE-UDID.xctestrun").write_text("")
    with patch.object(sibb_simctl, "BUILD_PRODUCTS_DIR", tmp_path):
        assert sibb_simctl.find_xctestrun_path() is None


def test_find_xctestrun_path_finds_master(tmp_path):
    master = tmp_path / "SIBBTests_master.xctestrun"
    master.write_text("")
    (tmp_path / "sibb_FAKE-UDID.xctestrun").write_text("")  # patched copy
    with patch.object(sibb_simctl, "BUILD_PRODUCTS_DIR", tmp_path):
        found = sibb_simctl.find_xctestrun_path()
    assert found == master


async def test_ensure_runner_built_no_op_when_already_built(tmp_path):
    master = tmp_path / "master.xctestrun"
    master.write_text("")
    with patch.object(sibb_simctl, "BUILD_PRODUCTS_DIR", tmp_path):
        # subprocess should NEVER be called when build is present.
        with patch.object(asyncio, "create_subprocess_exec",
                           AsyncMock(side_effect=AssertionError(
                               "ensure_runner_built called subprocess "
                               "when build was already present"))):
            await sibb_simctl.ensure_runner_built()


async def test_ensure_runner_built_serialized_under_lock(tmp_path):
    # When 5 callers race on a missing build, only ONE setup.sh
    # invocation should happen. The other four should see the build
    # has materialized (we simulate that by toggling find_xctestrun
    # to return a path after the first call).
    master = tmp_path / "master.xctestrun"
    call_count = {"n": 0}

    async def fake_subproc(*args, **kwargs):
        call_count["n"] += 1
        # Simulate setup.sh writing the build artifact.
        master.write_text("")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.kill = MagicMock()
        return proc

    # Patch BOTH find_ios_runtime_id and find_device_type_id to
    # return real-looking strings so ensure_runner_built proceeds
    # past the discovery step.
    with patch.object(sibb_simctl, "BUILD_PRODUCTS_DIR", tmp_path), \
         patch.object(sibb_simctl, "find_ios_runtime_id",
                       return_value="com.apple.x.iOS-26-3"), \
         patch.object(sibb_simctl, "find_device_type_id",
                       return_value="com.apple.x.iPhone-17"), \
         patch.object(sibb_simctl, "simctl_create",
                       AsyncMock(return_value="TMP-UDID")), \
         patch.object(sibb_simctl, "simctl_shutdown",
                       AsyncMock()), \
         patch.object(sibb_simctl, "simctl_delete",
                       AsyncMock()), \
         patch.object(asyncio, "create_subprocess_exec",
                       AsyncMock(side_effect=fake_subproc)):
        await asyncio.gather(*[
            sibb_simctl.ensure_runner_built() for _ in range(5)
        ])
    # Exactly one setup.sh invocation despite 5 concurrent callers.
    assert call_count["n"] == 1
