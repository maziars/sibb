"""B3 fix — `restart_springboard` after TCC grant.

Closes the TCC race under parallel `run_episodes_parallel`. Wix's
AppleSimulatorUtils (production-proven via Detox) is the source for
this pattern: `simctl privacy grant` writes TCC.db correctly but
SpringBoard caches per-bundle state and needs to be restarted before
the grant is visible to a launching app's `requestAccess` call.

The L1 tests here pin the structural pieces. The actual proof that
the race is closed is the L2 parallel test going from quarantined
→ green; that's verified manually after this lands.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sibb_episode
import sibb_simctl

pytestmark = pytest.mark.fast


# ───────────────── restart_springboard implementation ────────────────

def test_restart_springboard_exists_and_is_async():
    import inspect
    assert hasattr(sibb_simctl, "restart_springboard")
    assert inspect.iscoroutinefunction(sibb_simctl.restart_springboard)


async def test_restart_springboard_invokes_launchctl_kickstart():
    """The exact invocation must be:
        xcrun simctl spawn <UDID> launchctl kickstart -k
            system/com.apple.SpringBoard

    `kickstart -k` SIGKILLs the service so launchd respawns it
    cleanly; the alternative `kickstart` without -k is a no-op when
    the service is already running.
    """
    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.append(args)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.kill = MagicMock()
        return proc

    with patch.object(asyncio, "create_subprocess_exec",
                       AsyncMock(side_effect=fake_exec)), \
         patch.object(asyncio, "sleep", AsyncMock()):
        await sibb_simctl.restart_springboard("UDID-X", settle=0.0)

    assert len(captured_args) == 1
    args = captured_args[0]
    assert args[0] == "xcrun"
    assert args[1] == "simctl"
    assert args[2] == "spawn"
    assert args[3] == "UDID-X"
    assert args[4] == "launchctl"
    assert args[5] == "kickstart"
    assert args[6] == "-k", "missing -k flag; without it kickstart is a no-op for a running service"
    assert args[7] == "system/com.apple.SpringBoard"


async def test_restart_springboard_settle_sleeps_after_spawn():
    """A settle wait after kickstart is required — SpringBoard
    takes ~2s to come back up after SIGKILL. Without it, the next
    operation can race with SpringBoard's restart.
    """
    sleep_calls: list = []

    async def fake_sleep(t):
        sleep_calls.append(t)

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.kill = MagicMock()
        return proc

    with patch.object(asyncio, "create_subprocess_exec",
                       AsyncMock(side_effect=fake_exec)), \
         patch.object(asyncio, "sleep",
                       AsyncMock(side_effect=fake_sleep)):
        await sibb_simctl.restart_springboard("UDID-X", settle=2.5)

    assert 2.5 in sleep_calls


async def test_restart_springboard_raises_on_kickstart_timeout():
    """Kickstart hanging means SpringBoard is stuck; surface as
    RuntimeError rather than silently sleeping on a dead daemon.

    The mock must let `kill()` interrupt the second `communicate()`
    in the timeout-drain path (same pattern as test_simctl_wrappers
    timeout test) — otherwise we re-hang and the suite never returns.
    """
    killed = asyncio.Event()

    async def hang(*args, **kwargs):
        if killed.is_set():
            return (b"", b"")
        try:
            await asyncio.wait_for(killed.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            return (b"", b"")
        return (b"", b"")

    # Patch the function's `wait_for` to use a tiny timeout for the
    # main communicate call so the test stays fast.
    slow_proc = MagicMock()
    slow_proc.returncode = 0
    slow_proc.communicate = hang
    slow_proc.kill = MagicMock(side_effect=lambda: killed.set())

    # Inject a tiny timeout via patching the asyncio.wait_for call
    # path inside restart_springboard. Simpler: use the function's
    # default 10s but make the mock return faster — actually no,
    # we WANT to test the timeout fires. Trim by patching
    # asyncio.wait_for to use a tiny timeout.
    original_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout=None):
        # Force a small timeout regardless of caller's value.
        return await original_wait_for(awaitable, timeout=0.1)

    with patch.object(asyncio, "create_subprocess_exec",
                       AsyncMock(return_value=slow_proc)), \
         patch.object(asyncio, "wait_for", fast_wait_for):
        with pytest.raises(RuntimeError, match="kickstart timed out"):
            await sibb_simctl.restart_springboard("UDID-X")


# ──────── Wired into run_episode_scripted own_sim path ───────────────

def test_run_episode_scripted_calls_restart_springboard():
    """run_episode_scripted must call restart_springboard exactly
    once, in the LAST step before reader.start.

    Placement matters: restart_springboard SIGKILLs SpringBoard and
    waits ~2s for respawn. Placing it BEFORE the clone boot settles
    risks the runner connecting to a half-up SpringBoard. Placing
    it AFTER the boot but BEFORE reader.start is correct: boot has
    settled, runner's first EventKit call is next and benefits from
    a fresh TCC cache.

    Post-F1 contract: own_sim acquires a clone of the prewarmed
    baseline (no per-episode prewarm). The B3 SpringBoard restart
    is still present as cheap insurance — the clone inherits the
    baseline's TCC.db but SpringBoard caches may differ.
    """
    src = pathlib.Path(sibb_episode.__file__).read_text()
    func_idx = src.find("async def run_episode_scripted(")
    assert func_idx > 0
    func_end = src.find("\nasync def ", func_idx + 1)
    if func_end < 0:
        func_end = src.find("\ndef _", func_idx + 1)
    func_body = src[func_idx:func_end if func_end > 0 else func_idx + 8000]

    restart_calls = func_body.count("restart_springboard(udid)")
    assert restart_calls == 1, (
        f"expected exactly 1 restart_springboard call in "
        f"run_episode_scripted, found {restart_calls}"
    )

    # Ordering contract: acquire_clone → restart_springboard → reader.start.
    # acquire_clone replaces the old grant + prewarm sequence (which
    # now lives inside ensure_baseline_sim, not the per-episode path).
    restart_idx = func_body.find("restart_springboard(udid)")
    reader_start_idx = func_body.find("await reader.start(")
    clone_idx = func_body.find("acquire_clone(")

    assert clone_idx > 0, "acquire_clone must be called in own_sim path"
    assert restart_idx > 0, "restart_springboard must be called"
    assert reader_start_idx > 0, "reader.start must be called"
    assert clone_idx < restart_idx < reader_start_idx, (
        f"wrong ordering: acquire_clone={clone_idx} "
        f"restart={restart_idx} reader.start={reader_start_idx}"
    )


def test_restart_springboard_imported_in_sibb_episode():
    """The import must be at module level (not lazy) so the
    runner-level test that asserts the call-site can resolve.
    """
    assert sibb_episode.restart_springboard is sibb_simctl.restart_springboard, (
        "restart_springboard must be imported into sibb_episode "
        "from sibb_simctl (module-level), not lazily inside a function"
    )
