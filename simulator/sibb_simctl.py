"""
Async simctl wrappers + XCUITest runner-build helper.

Layer between Python orchestration and `xcrun simctl`. Provides:
- One-shot wrappers (create, boot, shutdown, delete, wait_booted)
- Runtime / device-type discovery (latest iOS, named device)
- `ensure_runner_built()` — idempotent guard that compiles the
  SIBB XCUITest runner if `~/SIBBHelper/build/` is missing or
  stale. Guarded by an asyncio.Lock so parallel workers can call
  it without racing on the build.

Why a dedicated module: D1b parallel orchestrator spawns N
workers, each of which creates+destroys its own sim per
episode. The runner BUILD must happen exactly once before any
worker spawns; without a centralized helper, every worker would
race on `xcodebuild build-for-testing`.

Per-sim runtime artifacts (`/tmp/sibb_xcuitest_<UDID>.sock`,
`/tmp/sibb_dd_<UDID>`, `/tmp/sibb_server_<UDID>.log`, and
`~/SIBBHelper/build/Build/Products/sibb_<UDID>.xctestrun`) are
already designed to be per-UDID by `sibb_xcuitest_client.py` —
this module assumes that contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_SCRIPT_DIR = Path(__file__).resolve().parent
SETUP_SCRIPT = _SCRIPT_DIR / "sibb_xcuitest_setup.sh"
BUILD_PRODUCTS_DIR = (
    Path.home() / "SIBBHelper" / "build" / "Build" / "Products"
)


# ────────────────────────── Subprocess helper ─────────────────────────

async def _run_simctl(*args: str, timeout: float = 30.0,
                       check: bool = False) -> Tuple[int, str, str]:
    """Run `xcrun simctl <args>` async. Returns (rc, stdout, stderr).

    `check=True` raises RuntimeError on nonzero return code.
    `timeout` is total wallclock; raises RuntimeError on hit.
    """
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"simctl {' '.join(args)} timed out after {timeout}s")
    rc = proc.returncode or 0
    stdout = out.decode("utf-8", errors="replace").strip()
    stderr = err.decode("utf-8", errors="replace").strip()
    if check and rc != 0:
        raise RuntimeError(
            f"simctl {' '.join(args)} failed (rc={rc}): {stderr or stdout}")
    return rc, stdout, stderr


# ──────────────────────────── Lifecycle ───────────────────────────────

async def simctl_create(name: str, device_type: str,
                         runtime: str) -> str:
    """Create a new simulator. Returns its UDID.

    `name` need not be unique — simctl distinguishes by UDID. We
    use timestamp-prefixed names so orphans (failed deletes) are
    easy to identify when sweeping.
    """
    _, udid, _ = await _run_simctl(
        "create", name, device_type, runtime,
        check=True, timeout=30.0,
    )
    return udid


async def simctl_boot(udid: str, timeout: float = 120.0) -> None:
    """Boot a simulator. Idempotent — no-op if already booted.

    Timeout raised from 60 → 120s on 2026-06-11 after the hybrid v3b
    run hit a `simctl boot timed out after 60.0s` on the third recycle
    of the slate. Cold boots take 20-30s on a warm Mac but the time
    grows after several shutdown/boot cycles because CoreSimulatorService
    accumulates state. 120s gives one degraded boot enough headroom
    before we declare the sim un-recoverable; further failures will
    bubble up as `RuntimeError` for the runner's recycle-failed path.
    """
    rc, _, err = await _run_simctl("boot", udid, timeout=timeout)
    if rc == 0:
        return
    lower = err.lower()
    if "already booted" in lower or "current state: booted" in lower:
        return
    raise RuntimeError(f"simctl boot {udid} failed: {err}")


async def simctl_quit_simulator_app(timeout: float = 5.0) -> None:
    """Kill the Simulator.app process tree. Use between recycles to
    drop UI-side state CoreSimulatorService accumulates and to let
    the next `simctl boot` start from a clean place.

    Documented in Apple Developer Forum #713921 + Maestro #3318 as a
    workaround for CoreSimulator state-leak across long batches. Best-
    effort — pkill returns non-zero when nothing matched.
    """
    import asyncio as _asyncio
    proc = await _asyncio.create_subprocess_exec(
        "pkill", "-x", "Simulator",
        stdout=_asyncio.subprocess.DEVNULL,
        stderr=_asyncio.subprocess.DEVNULL,
    )
    try:
        await _asyncio.wait_for(proc.wait(), timeout=timeout)
    except _asyncio.TimeoutError:
        proc.kill()


async def simctl_clone(src_udid: str, name: str, *,
                        retries: int = 3,
                        timeout: float = 60.0) -> str:
    """Clone an existing simulator. Returns the new clone's UDID.

    `simctl clone` is the Apple-blessed primitive for fast test
    isolation (WWDC 2019 session 418): copy a pre-prepared baseline
    sim instead of paying create+boot+prewarm per worker. A boot of
    a fresh clone takes ~10-15s vs ~150-300s for create+prewarm.

    Source must be `Shutdown` — simctl refuses to clone a `Booted`
    sim. Caller's responsibility to shutdown the baseline first.

    Flake handling: cloning the same source from multiple processes
    in quick succession occasionally returns `Failed to clone device`
    on the same machine (Apple Developer Forum #713921). We retry up
    to `retries` times with linear backoff. The retry is essentially
    free — each attempt is ~5-10s, so 3 attempts caps the worst case
    at ~30s before we give up entirely.
    """
    last_err = ""
    for attempt in range(1, retries + 1):
        rc, udid, err = await _run_simctl(
            "clone", src_udid, name, timeout=timeout,
        )
        if rc == 0 and udid:
            return udid
        last_err = err or f"clone returned rc={rc}, stdout={udid!r}"
        if attempt < retries:
            await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(
        f"simctl clone {src_udid} -> {name!r} failed after "
        f"{retries} attempts: {last_err}"
    )


async def simctl_shutdown(udid: str, timeout: float = 30.0) -> None:
    """Shutdown a simulator. Best-effort (idempotent on already-shutdown)."""
    rc, _, err = await _run_simctl("shutdown", udid, timeout=timeout)
    if rc == 0:
        return
    lower = err.lower()
    if "current state: shutdown" in lower or "already shutdown" in lower:
        return
    # Don't raise — shutdown failures during teardown shouldn't mask the
    # underlying episode error. Caller can inspect via `simctl list`
    # if it needs to confirm.


async def simctl_delete(udid: str, timeout: float = 30.0) -> None:
    """Delete a simulator's UDID + filesystem. Best-effort.

    Tolerant of already-deleted / not-found errors so cleanup
    paths can call this unconditionally. Failed deletes leak a
    directory under `~/Library/Developer/CoreSimulator/Devices/`
    — orchestrators that care should `simctl list devices` and
    sweep periodically.
    """
    await _run_simctl("delete", udid, timeout=timeout)


async def restart_springboard(udid: str, settle: float = 2.0) -> None:
    """Restart SpringBoard inside the simulator so TCC grants take effect.

    Why this exists: `simctl privacy grant` writes to the sim's TCC.db
    correctly (verified via sqlite3 against the sim's TCC.db), but
    EventKit's `requestAccess` still returns granted=false until
    SpringBoard re-reads its per-bundle cache. Under serial execution
    this race almost never bites — SpringBoard has time to refresh
    between grant and the test runner's first EventKit call. Under
    parallel xcodebuild load, the window shrinks below SpringBoard's
    natural refresh cadence and ~50% of workers see granted=false.

    Mechanism: `launchctl kickstart -k` SIGKILLs SpringBoard and lets
    launchd respawn it. On respawn it re-reads TCC.db fresh. Empirically
    proven via wix/AppleSimulatorUtils (used by Detox) — search.

    Idempotent — safe to call on a booted sim at any point. Takes
    ~2-3s end-to-end (SIGKILL + respawn + the settle wait below).
    """
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "spawn", udid,
        "launchctl", "kickstart", "-k", "system/com.apple.SpringBoard",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"restart_springboard: kickstart timed out for {udid}")
    # SpringBoard takes a moment to come back up + reload its caches.
    # Without this, an immediate xcodebuild test launch can race the
    # restart and see SpringBoard in a half-up state.
    await asyncio.sleep(settle)


async def simctl_wait_booted(udid: str, timeout: float = 60.0) -> None:
    """Poll `simctl list devices` until `Booted` appears for this UDID."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        _, out, _ = await _run_simctl(
            "list", "devices", udid, timeout=10.0)
        if "Booted" in out:
            return
        await asyncio.sleep(1.0)
    raise RuntimeError(
        f"simctl_wait_booted: {udid} did not reach Booted within "
        f"{timeout}s")


# ──────────────────────── Discovery ───────────────────────────────────

def list_runtimes() -> List[Dict[str, Any]]:
    """Sync list of available simulator runtimes. Used at orchestrator
    startup; not on the per-episode hot path."""
    try:
        r = subprocess.run(
            ["xcrun", "simctl", "list", "runtimes", "-j"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return []
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return [d for d in data.get("runtimes", []) if d.get("isAvailable")]


def list_device_types() -> List[Dict[str, Any]]:
    """Sync list of available simulator device types."""
    try:
        r = subprocess.run(
            ["xcrun", "simctl", "list", "devicetypes", "-j"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return []
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return data.get("devicetypes", [])


def find_ios_runtime_id(version: Optional[str] = None) -> Optional[str]:
    """Resolve to a runtime identifier (e.g. `com.apple...SimRuntime.iOS-26-3`).

    `version=None` picks the highest version available. A non-None
    `version` is matched as a substring of either the runtime's
    `version` or `name` (so "26" or "26.3" or "iOS 26.3" all work).
    """
    runtimes = [r for r in list_runtimes() if "iOS" in r.get("name", "")]
    if not runtimes:
        return None
    if version is None:
        runtimes.sort(key=lambda r: r.get("version", ""), reverse=True)
        return runtimes[0].get("identifier")
    v = str(version)
    for r in runtimes:
        if v in r.get("version", "") or v in r.get("name", ""):
            return r.get("identifier")
    return None


def find_device_type_id(name_substring: str = "iPhone 17") -> Optional[str]:
    """Resolve a device type id by substring match on its display name."""
    needle = name_substring.lower()
    for dt in list_device_types():
        if needle in dt.get("name", "").lower():
            return dt.get("identifier")
    return None


# ──────────────────────── Runner build ────────────────────────────────

def find_xctestrun_path() -> Optional[Path]:
    """Return the path to the MASTER (unpatched) xctestrun if the
    build is present, else None.

    Per-UDID patched copies named `sibb_<UDID>.xctestrun` live in
    the same directory; they are deliberately skipped here.
    """
    if not BUILD_PRODUCTS_DIR.exists():
        return None
    for p in BUILD_PRODUCTS_DIR.glob("*.xctestrun"):
        if p.name.startswith("sibb_"):
            continue
        return p
    return None


# ──────────────────────────── Orphan sweeper ──────────────────────────

# Names matching this prefix were created by SIBB and are safe to
# reap at orchestrator startup. Anything else is left alone (could
# be a user-created sim or another project's).
_SIBB_ORPHAN_NAME_PREFIXES = ("SIBB-Episode-", "SIBB-Build-Temp",
                                "SIBB-D1c-")


async def sweep_sibb_orphans() -> Dict[str, int]:
    """Delete any leaked SIBB simulators + tmp artifacts.

    Called at orchestrator startup (and optionally at exit). After a
    crash, the following can leak:
    - Simulator devices named `SIBB-Episode-*`, `SIBB-Build-Temp*`,
      `SIBB-D1c-*` (uses ~500 MB disk per sim)
    - `/tmp/sibb_xcuitest_<UDID>.sock` (cheap but pollutes /tmp)
    - `/tmp/sibb_dd_<UDID>` (derived data; ~50-200 MB)
    - `/tmp/sibb_server_<UDID>.log`
    - `~/SIBBHelper/build/Build/Products/sibb_<UDID>.xctestrun` patched
       copies — these confuse `find_xctestrun` in the next run

    Returns a count-by-category report for logging.
    """
    import glob
    import shutil

    report = {"sims_deleted": 0, "sockets_removed": 0,
              "dd_dirs_removed": 0, "logs_removed": 0,
              "patched_xctestruns_removed": 0}

    # 1. Sim devices — JSON output identifies orphans by name prefix.
    try:
        rc, out, _ = await _run_simctl(
            "list", "devices", "-j", timeout=15.0)
    except Exception:
        rc, out = 1, ""
    orphan_udids: List[str] = []
    if rc == 0 and out:
        try:
            data = json.loads(out)
            for runtime_devices in data.get("devices", {}).values():
                for d in runtime_devices:
                    name = d.get("name", "")
                    if any(name.startswith(p)
                           for p in _SIBB_ORPHAN_NAME_PREFIXES):
                        u = d.get("udid")
                        if u:
                            orphan_udids.append(u)
        except json.JSONDecodeError:
            pass
    for u in orphan_udids:
        # Shutdown first (delete fails on Booted sims with simctl on
        # some Xcode versions; best-effort either way).
        try:
            await simctl_shutdown(u, timeout=15.0)
        except Exception:
            pass
        try:
            await simctl_delete(u, timeout=15.0)
            report["sims_deleted"] += 1
        except Exception:
            pass

    # 2. /tmp/sibb_xcuitest_*.sock — these are stale Unix sockets from
    # killed runners. Safe to remove unconditionally.
    for p in glob.glob("/tmp/sibb_xcuitest_*.sock"):
        try:
            os.remove(p)
            report["sockets_removed"] += 1
        except OSError:
            pass

    # 3. /tmp/sibb_dd_* — derived data dirs.
    for p in glob.glob("/tmp/sibb_dd_*"):
        try:
            shutil.rmtree(p, ignore_errors=True)
            report["dd_dirs_removed"] += 1
        except OSError:
            pass

    # 4. /tmp/sibb_server_*.log — stdout drain files.
    for p in glob.glob("/tmp/sibb_server_*.log"):
        try:
            os.remove(p)
            report["logs_removed"] += 1
        except OSError:
            pass

    # 5. Patched xctestrun copies. The MASTER name does NOT start with
    # sibb_; per-UDID copies named sibb_<UDID>.xctestrun do. Safe to
    # remove all of the latter — every active worker writes a fresh
    # patched copy when it calls XCUITestReader.start().
    if BUILD_PRODUCTS_DIR.exists():
        for p in BUILD_PRODUCTS_DIR.glob("sibb_*.xctestrun"):
            try:
                p.unlink()
                report["patched_xctestruns_removed"] += 1
            except OSError:
                pass

    return report


_BUILD_LOCK: Optional[asyncio.Lock] = None
_BUILD_LOCK_LOOP: Optional[Any] = None


def _get_build_lock() -> asyncio.Lock:
    """Lazy per-loop creation of the build lock.

    Module-load `asyncio.Lock()` binds to whatever loop is current
    at import time (often None in Python 3.9), then fails with
    "Future attached to a different loop" when used from a test's
    or worker's running loop. Creating the lock on first use within
    a running loop avoids that — and recreating it when the loop
    changes keeps cross-loop misuse from masquerading as deadlock.
    """
    global _BUILD_LOCK, _BUILD_LOCK_LOOP
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if _BUILD_LOCK is None or _BUILD_LOCK_LOOP is not current:
        _BUILD_LOCK = asyncio.Lock()
        _BUILD_LOCK_LOOP = current
    return _BUILD_LOCK


async def ensure_runner_built(
    device_type_substring: str = "iPhone 17",
    runtime_version: Optional[str] = None,
    setup_timeout: float = 300.0,
) -> None:
    """Compile the XCUITest runner once if it isn't already.

    Idempotent. Guarded by an asyncio.Lock so N parallel workers
    can each call it at startup without racing on the underlying
    `xcodebuild build-for-testing`.

    `setup.sh` requires a real simulator UDID to target for the
    build (xcodebuild needs `-destination`). We create a temp sim
    just for the build, then delete it. Build artifacts live under
    `~/SIBBHelper/build/` and are reusable by every other UDID
    against the same iOS runtime.
    """
    async with _get_build_lock():
        if find_xctestrun_path() is not None:
            return

        runtime_id = find_ios_runtime_id(runtime_version)
        if runtime_id is None:
            raise RuntimeError(
                f"No iOS runtime found for version={runtime_version!r}; "
                f"available: {[r.get('name') for r in list_runtimes()]}"
            )
        device_type_id = find_device_type_id(device_type_substring)
        if device_type_id is None:
            raise RuntimeError(
                f"No device type matching {device_type_substring!r}; "
                f"try: list_device_types() for available names"
            )

        temp_udid = await simctl_create(
            "SIBB-Build-Temp", device_type_id, runtime_id)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(SETUP_SCRIPT), temp_udid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out_bytes, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=setup_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise RuntimeError(
                    f"setup.sh timed out after {setup_timeout}s "
                    f"(temp UDID {temp_udid} left behind for inspection)"
                )
            if proc.returncode != 0:
                tail = out_bytes.decode(
                    "utf-8", errors="replace")[-2000:]
                raise RuntimeError(
                    f"setup.sh failed (rc={proc.returncode}):\n{tail}")
        finally:
            await simctl_shutdown(temp_udid)
            await simctl_delete(temp_udid)

        if find_xctestrun_path() is None:
            raise RuntimeError(
                "setup.sh completed but no .xctestrun found under "
                f"{BUILD_PRODUCTS_DIR}"
            )
