"""
Baseline simulator lifecycle — F1.

A long-lived `SIBB-Baseline-<ios>-<fingerprint>` simulator that
captures the post-prewarm state (TCC populated, first-launch
dialogs dismissed, suppression keys written). Episode workers
`simctl clone` this baseline instead of paying create+prewarm
costs themselves.

Workflow per episode:

  baseline_udid = await ensure_baseline_sim()         # ~50ms after first call
  clone_udid    = await acquire_clone(baseline_udid)  # ~15-25s (clone + boot)
  # ... episode runs against clone ...
  await release_clone(clone_udid)                     # ~5s (shutdown + delete)

Compared to the pre-F1 flow (create → boot → prewarm → shutdown
→ boot per episode, with prewarm serialized across workers), this
cuts the per-episode prelude from ~150-300s to ~50-100s AND
removes the prewarm-serialization bottleneck.

Fingerprint = sha256 of `sibb_prewarm.sh` + iOS runtime id. If
prewarm.sh changes (new app added, new suppression key written,
etc.) the fingerprint flips, the old baseline is deleted, and a
new one is built on the next `ensure_baseline_sim()` call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Optional, Tuple
from uuid import uuid4

from sibb_simctl import (
    _run_simctl,
    find_device_type_id,
    find_ios_runtime_id,
    simctl_boot,
    simctl_clone,
    simctl_create,
    simctl_delete,
    simctl_shutdown,
    simctl_wait_booted,
)


_SCRIPT_DIR = Path(__file__).resolve().parent
PREWARM_SCRIPT = _SCRIPT_DIR / "sibb_prewarm.sh"
SETUP_SCRIPT = _SCRIPT_DIR / "sibb_xcuitest_setup.sh"


# ────────────────────────── Fingerprint ───────────────────────────────

def baseline_fingerprint(runtime_id: str) -> str:
    """8-char sha256 prefix over inputs that affect baseline state.

    Inputs:
    - `sibb_prewarm.sh` — writes TCC grants, suppression keys
    - `sibb_xcuitest_setup.sh` — contains the Swift
      `dismiss_app_onboarding` logic invoked during baseline build
    - `runtime_id` — so different iOS runtimes get distinct baselines

    Why setup.sh is in here: the baseline build calls
    `dismiss_app_onboarding` for each SIBB-11 app to clear in-app
    welcome/iCloud prompts (which prewarm.sh can't reach via plist
    keys alone). When setup.sh changes — e.g. new dismiss labels
    added, dispatch logic altered — the baseline's dismissal state
    is stale and must be rebuilt.
    """
    h = hashlib.sha256()
    for script in (PREWARM_SCRIPT, SETUP_SCRIPT):
        try:
            h.update(script.read_bytes())
        except OSError:
            # If a script is unreadable, fall back to a stable
            # placeholder so the fingerprint stays deterministic
            # — the downstream build will surface the real error.
            h.update(f"<{script.name} unreadable>".encode("utf-8"))
    h.update(runtime_id.encode("utf-8"))
    return h.hexdigest()[:8]


def baseline_name(runtime_id: str) -> str:
    """`SIBB-Baseline-<runtime>-<fingerprint>` — the canonical name.

    Runtime is shortened (`iOS-26-3` → `26.3`) so the name reads
    naturally in `simctl list devices`.
    """
    fp = baseline_fingerprint(runtime_id)
    short_rt = runtime_id.rsplit(".", 1)[-1].replace("iOS-", "").replace("-", ".")
    return f"SIBB-Baseline-{short_rt}-{fp}"


# ────────────────────────── Discovery ─────────────────────────────────

async def find_sim_by_name(name: str) -> Optional[str]:
    """Returns UDID of the first sim matching `name`, else None.

    Matches against `name` exactly (not substring) — caller passes
    the fully-formed baseline name from `baseline_name()`. Stops at
    the first match because there should never be more than one
    (`simctl create` doesn't enforce unique names, but the baseline
    lifecycle here only ever creates one per fingerprint).
    """
    rc, out, _ = await _run_simctl("list", "devices", "-j", timeout=15.0)
    if rc != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    for devices in data.get("devices", {}).values():
        for d in devices:
            if d.get("name") == name:
                return d.get("udid")
    return None


async def list_baseline_orphans(current_runtime_id: str,
                                  current_fingerprint: str) -> list:
    """Returns UDIDs of stale `SIBB-Baseline-*` sims that don't match
    the current fingerprint. Used at `ensure_baseline_sim()` time to
    sweep old baselines whose prewarm.sh has changed.

    Doesn't touch `SIBB-Episode-*` clones — those are per-worker
    and reaped by `release_clone`.
    """
    rc, out, _ = await _run_simctl("list", "devices", "-j", timeout=15.0)
    if rc != 0 or not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    current_name = baseline_name(current_runtime_id)
    orphans = []
    for devices in data.get("devices", {}).values():
        for d in devices:
            name = d.get("name", "")
            if not name.startswith("SIBB-Baseline-"):
                continue
            if name == current_name:
                continue
            udid = d.get("udid")
            if udid:
                orphans.append(udid)
    return orphans


# ────────────────────────── Baseline build ────────────────────────────

_BASELINE_LOCK: Optional[asyncio.Lock] = None
_BASELINE_LOCK_LOOP = None


def _get_baseline_lock() -> asyncio.Lock:
    """Lazy-per-loop pattern (same as `_get_build_lock` in sibb_simctl).

    If two `ensure_baseline_sim()` calls land concurrently on a
    fresh dev machine, only one should actually build — the second
    should observe the in-progress one and wait.
    """
    global _BASELINE_LOCK, _BASELINE_LOCK_LOOP
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if _BASELINE_LOCK is None or _BASELINE_LOCK_LOOP is not current:
        _BASELINE_LOCK = asyncio.Lock()
        _BASELINE_LOCK_LOOP = current
    return _BASELINE_LOCK


async def _dismiss_onboardings(udid: str) -> None:
    """Start the XCUITest runner against `udid` and have it dismiss
    every SIBB-11 app's in-app onboarding flow.

    Why this isn't in `sibb_prewarm.sh`: shell scripts can't read
    AX trees. Dismissal needs the Swift runner — which means the
    baseline build pays a one-time ~30s xcodebuild startup cost on
    top of the prewarm itself. That cost is fine because baseline
    is a one-shot per fingerprint.

    Implementation: instantiate an `AXReader` against the baseline
    UDID, attach to SpringBoard (cheapest activation), then call
    `dismiss_app_onboarding(bundle)` for each SIBB-11 bundle. The
    runner's `dismiss_app_onboarding` activates the bundle, walks
    the AX snapshot for known dismiss-button labels, and taps any
    it finds — up to 6 chained dialogs per app.
    """
    # Lazy import — keeps `sibb_baseline` importable in tests that
    # mock out the heavyweight client.
    import sys
    sim_dir = str(Path(__file__).resolve().parent)
    if sim_dir not in sys.path:
        sys.path.insert(0, sim_dir)
    benchmark_dir = str(
        Path(__file__).resolve().parent.parent / "benchmark")
    if benchmark_dir not in sys.path:
        sys.path.insert(0, benchmark_dir)
    from sibb_scaffold import AXReader

    # SIBB-11 bundle ids. Kept inline rather than imported because
    # the canonical list lives in sibb_prewarm.sh (shell array) —
    # duplicating it here matches the existing pattern and avoids
    # tying baseline lifecycle to handler-registry import order.
    bundles = [
        "com.apple.reminders",
        "com.apple.mobilecal",
        "com.apple.MobileAddressBook",
        "com.apple.Preferences",
        "com.apple.DocumentsApp",
        "com.apple.Health",
        "com.apple.Maps",
        "com.apple.mobileslideshow",
        "com.apple.shortcuts",
        "com.apple.mobilesafari",
        "com.apple.MobileSMS",
    ]

    reader = AXReader(udid)
    await reader.start(bundle_id="com.apple.springboard")
    try:
        for bundle in bundles:
            try:
                taps = await reader._xcuitest.dismiss_app_onboarding(
                    bundle)
                if taps:
                    print(f"  [baseline] dismissed {taps} dialog(s) "
                          f"on {bundle}")
            except Exception as e:
                # Don't fail the whole baseline build over a single
                # app's onboarding quirk. Log and continue.
                print(f"  [baseline] dismiss_onboarding({bundle}) "
                      f"failed: {type(e).__name__}: {e}")
    finally:
        await reader.stop()


async def _run_prewarm(udid: str, timeout: float = 300.0) -> None:
    """Run `sibb_prewarm.sh <udid>` against a booted sim.

    Mirror of the helper previously in `sibb_episode.py` (which is
    now deleted). The old prewarm-serialization lock isn't needed
    here — baseline build only ever happens ONCE per fingerprint
    per machine, and is guarded by `_get_baseline_lock()`.
    """
    proc = await asyncio.create_subprocess_exec(
        "bash", str(PREWARM_SCRIPT), udid,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"sibb_prewarm.sh timed out after {timeout}s on {udid}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"sibb_prewarm.sh failed (rc={proc.returncode}) on {udid}")


async def ensure_baseline_sim(
    device_type_substring: str = "iPhone 17",
    runtime_version: Optional[str] = None,
    *,
    sweep_orphans: bool = True,
) -> Tuple[str, str]:
    """Returns `(baseline_udid, runtime_id)` for the current fingerprint.

    Idempotent — returns immediately if the baseline already exists.
    First call on a fresh machine pays the full create + boot +
    prewarm cost (~3 min). Subsequent calls are ~50ms (JSON listing
    parse).

    Behavior:
    1. Resolve runtime + device type.
    2. Compute fingerprint, derive baseline name.
    3. If a sim with that name exists → return it.
    4. Else: optionally sweep stale baselines, then create + boot +
       ensure_runner_permissions + prewarm + shutdown a new one.

    `sweep_orphans=False` is for tests that want to verify the
    baseline-build path without touching unrelated state.
    """
    runtime_id = find_ios_runtime_id(runtime_version)
    if runtime_id is None:
        raise RuntimeError(
            f"No iOS runtime found for version={runtime_version!r}"
        )
    device_type_id = find_device_type_id(device_type_substring)
    if device_type_id is None:
        raise RuntimeError(
            f"No device type matching {device_type_substring!r}"
        )

    name = baseline_name(runtime_id)

    async with _get_baseline_lock():
        # Re-check inside the lock — a peer may have built it
        # while we waited.
        existing = await find_sim_by_name(name)
        if existing:
            return existing, runtime_id

        if sweep_orphans:
            fp = baseline_fingerprint(runtime_id)
            for stale in await list_baseline_orphans(runtime_id, fp):
                try:
                    await simctl_shutdown(stale, timeout=15.0)
                except Exception:
                    pass
                try:
                    await simctl_delete(stale, timeout=15.0)
                except Exception:
                    pass

        # Build it. Late-imported so the test layer that mocks
        # ensure_runner_permissions doesn't pull in xcuitest_client's
        # full surface area.
        from sibb_xcuitest_client import ensure_runner_permissions

        baseline_udid = await simctl_create(
            name, device_type_id, runtime_id)
        try:
            await simctl_boot(baseline_udid)
            await simctl_wait_booted(baseline_udid)
            await ensure_runner_permissions(baseline_udid)
            await _run_prewarm(baseline_udid)
            # Programmatically dismiss in-app onboarding flows that
            # prewarm.sh can't reach via plist suppression keys
            # (Reminders' "Welcome" → "Enable iCloud Syncing?" chain,
            # similar for Calendar/Health/etc). Without this the
            # first clone-and-launch of each app shows the
            # onboarding, which breaks any UI test that doesn't
            # itself dismiss it.
            await _dismiss_onboardings(baseline_udid)
        except Exception:
            # If anything fails mid-build, the partial baseline is
            # useless — delete it so the next call rebuilds cleanly
            # instead of returning a half-baked sim.
            try:
                await simctl_shutdown(baseline_udid, timeout=15.0)
            except Exception:
                pass
            try:
                await simctl_delete(baseline_udid, timeout=15.0)
            except Exception:
                pass
            raise
        # Baseline must be Shutdown to be cloneable.
        await simctl_shutdown(baseline_udid)
        return baseline_udid, runtime_id


# ────────────────────────── Clone lifecycle ───────────────────────────

async def acquire_clone(baseline_udid: str, *,
                         label: str = "anon") -> str:
    """Clone the baseline + boot the clone. Returns the clone's UDID.

    `label` is folded into the clone's name for log readability
    (`SIBB-Episode-<label>-<short-uuid>`). Caller is responsible for
    `release_clone(udid)` after use — orphan sims leak ~500 MB disk.

    Returns AFTER boot completes, so the caller can immediately
    proceed to `restart_springboard` + reader.start without waiting.

    Re-grants TCC services on the clone. Most TCC entries survive
    `simctl clone`, but some don't — Photos (`kTCCServicePhotos`)
    notably resets to `auth_value=0` on the clone, even though it's
    `=2` on the baseline. Empirically verified 2026-05-16: cloning a
    baseline with all granted yields a clone where Photos returns
    `.notDetermined` from `PHPhotoLibrary.requestAuthorization`.
    Re-running `ensure_runner_permissions` on the clone is cheap
    (~1-2s) and idempotent for the services that did survive.
    """
    clone_name = f"SIBB-Episode-{label}-{uuid4().hex[:6]}"
    clone_udid = await simctl_clone(baseline_udid, clone_name)
    await simctl_boot(clone_udid)
    await simctl_wait_booted(clone_udid)
    # Late import keeps sibb_baseline standalone-importable for tests
    # that don't need the xcuitest_client surface.
    from sibb_xcuitest_client import ensure_runner_permissions
    await ensure_runner_permissions(clone_udid)
    return clone_udid


async def release_clone(udid: str) -> None:
    """Shutdown + delete a clone. Best-effort."""
    try:
        await simctl_shutdown(udid, timeout=15.0)
    except Exception:
        pass
    try:
        await simctl_delete(udid, timeout=15.0)
    except Exception:
        pass
