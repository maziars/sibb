#!/usr/bin/env python3
"""
SIBB XCUITest Client
=====================
Persistent AX server using XCUITest — reads the FULL iOS accessibility tree.
Scales to N concurrent simulators — each gets its own socket and derived data.

Setup (once per Mac):
    ./sibb_xcuitest_setup.sh <UDID>

Usage:
    from sibb_xcuitest_client import XCUITestReader
    reader = XCUITestReader(udid, bundle_id="com.apple.reminders")
    await reader.start()           # ~10-20s first run, ~2s subsequent
    tree   = await reader.observe()  # ~30ms
    await reader.tap(x=335, y=831)   # ~50ms
    await reader.stop()

Scaling:
    N concurrent simulators = N independent reader instances.
    Each uses:
      - Socket:       /tmp/sibb_xcuitest_<UDID>.sock   (no collision)
      - Derived data: /tmp/sibb_dd_<UDID>/              (no lock contention)
      - Env var:      SIBB_UDID=<UDID>                  (passed to Swift server)
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections import defaultdict
from typing import Any, Optional, List, Dict, Tuple

HOME      = os.path.expanduser("~")
PROJ_DIR  = os.path.join(HOME, "SIBBHelper")
BUILD_DIR = os.path.join(PROJ_DIR, "build")


def find_xctestrun() -> Optional[str]:
    """Return path to the MASTER (unpatched) xctestrun, or None.

    Per-UDID patched copies named `sibb_<UDID>.xctestrun` live in the
    same directory; they MUST be skipped here. Returning a patched
    copy as the "master" would cause N parallel workers to walk over
    each other's `patch_xctestrun` output, racing on the file the
    next worker's `os.walk` happens to enumerate first. The result is
    intermittent socket-not-created errors and cross-UDID contamination
    that is essentially impossible to reproduce on demand.

    Mirrors the filter in `sibb_simctl.find_xctestrun_path`.
    """
    for root, dirs, files in os.walk(BUILD_DIR):
        for f in files:
            if not f.endswith(".xctestrun"):
                continue
            if f.startswith("sibb_"):
                continue
            return os.path.join(root, f)
    return None


def socket_path(udid: str) -> str:
    return f"/tmp/sibb_xcuitest_{udid}.sock"


def derived_data_path(udid: str) -> str:
    return f"/tmp/sibb_dd_{udid}"


def list_foreground_candidates(udid: str) -> List[str]:
    """
    Bundle IDs scanned when auto-detecting the foreground app on `observe`.

    Order matters: Springboard reports `.runningForeground` even while
    another app is on top, so we scan every installed app FIRST and only
    fall back to Springboard if none matched. That fallback is what
    captures the home screen, Spotlight, App Switcher, Control Center,
    and the lock screen (all owned by Springboard, not listed by
    `simctl listapps`).
    """
    candidates: List[str] = []
    try:
        proc = subprocess.run(
            f"xcrun simctl listapps {udid} | plutil -convert json -o - -",
            shell=True, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            candidates = list(data.keys())
    except Exception:
        pass
    candidates.append("com.apple.springboard")
    return candidates


def build_pid_to_bundle_map(udid: str) -> Dict[int, str]:
    """
    Map iOS-side PIDs to bundle IDs via `simctl spawn launchctl list`.
    Covers both user apps (UIKitApplication:<bundle>) and the
    com.apple.SpringBoard system daemon (which owns home, Spotlight,
    App Switcher, Control Center, lock screen).
    """
    m: Dict[int, str] = {}
    try:
        r = subprocess.run(
            ["xcrun", "simctl", "spawn", udid, "launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return m
    for line in r.stdout.splitlines():
        mo = re.match(r"(\d+)\s+\S+\s+UIKitApplication:([a-zA-Z0-9._-]+)\[", line)
        if mo:
            m[int(mo.group(1))] = mo.group(2)
            continue
        mo = re.match(r"(\d+)\s+\S+\s+com\.apple\.SpringBoard$", line)
        if mo:
            m[int(mo.group(1))] = "com.apple.springboard"
    return m


XCTRUNNER_BUNDLE = "com.sibb.tests.xctrunner"

async def ensure_runner_permissions(udid: str):
    """
    Pre-grant TCC permissions to the XCUITest runner bundle BEFORE
    xcodebuild launches it. Granting EventKit-style access after the
    runner is already running can still produce a user-facing
    permission prompt on the first call, even though simctl writes
    auth_value=2 to TCC.db. Granting up-front avoids the prompt.

    Which services to grant is sourced from `sibb_state.HANDLERS` —
    each handler declares `tcc_services: List[str]`. Lazy import so
    the simulator package doesn't take a module-load dependency on
    the benchmark package (the reverse direction is the natural one).
    If sibb_state isn't importable (smoke-only runs, packaging in
    flux), we no-op gracefully — a missing grant just means the
    iOS-17 transparency dialog fires on first use, which Swift's
    `dismissPermissionDialogs()` cleans up.

    Async (D1.5): under N parallel workers in `asyncio.gather`, the
    sync `subprocess.run` version blocked the event loop for the
    full grant burst (~5s × per service), stalling every other
    worker's coroutines. Async subprocess yields between calls.
    """
    try:
        from sibb_state import collect_tcc_services  # type: ignore
        services = collect_tcc_services()
    except Exception:
        services = []
    grants: List[Tuple[str, str]] = [
        (service, XCTRUNNER_BUNDLE) for service in services
    ]
    # External-app grants. The runner bundle owns its own TCC.db rows,
    # but other apps (Maps, etc.) have separate rows that the runner's
    # grants don't cover. Specifically: Maps' directions flow requires
    # `location` permission AND the iOS master Location Services
    # switch — without these, agents trying to take directions hit a
    # "Location Services is Off" prompt at the Maps directions step
    # (variant D 2026-05-27 trial).
    #
    # We grant `location` to com.apple.Maps unconditionally. It's
    # idempotent and only relevant for Maps-using tasks; harmless
    # otherwise. Same pattern is open for other system apps that
    # require user-facing TCC prompts to unblock task flows.
    grants.append(("location", "com.apple.Maps"))
    for service, bundle in grants:
        try:
            proc = await asyncio.create_subprocess_exec(
                "xcrun", "simctl", "privacy", udid, "grant",
                service, bundle,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except Exception:
                pass
        except Exception:
            pass


def ensure_software_keyboard():
    """
    Disconnect the iOS Simulator's hardware keyboard so the on-screen
    keyboard appears whenever a text field is focused. Required for
    realistic typing — without this, host keystrokes are forwarded as
    HID events and the on-screen keyboard never shows up.

    The setting is a Simulator.app preference, so it takes effect on
    Simulator.app launch. If Simulator.app is already running when this
    is called, you may need to restart it once for the change to apply;
    after that it persists.
    """
    try:
        subprocess.run(
            ["defaults", "write", "com.apple.iphonesimulator",
             "ConnectHardwareKeyboard", "-bool", "false"],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Data model (compatible with sibb_scaffold.py AXElement interface)
# ─────────────────────────────────────────────────────────────────────────────

class Frame:
    def __init__(self, x, y, width, height):
        self.x = x;      self.y = y
        self.width = width; self.height = height

    @property
    def center_x(self): return self.x + self.width / 2
    @property
    def center_y(self): return self.y + self.height / 2

    @classmethod
    def from_dict(cls, d):
        if not d: return None
        return cls(float(d.get("x",0)), float(d.get("y",0)),
                   float(d.get("width",0)), float(d.get("height",0)))


class AXElement:
    def __init__(self, raw: dict):
        self.ref            = raw.get("ref", "")
        self.role           = raw.get("role", "other")
        self.label          = raw.get("label", "") or None
        self.value          = raw.get("value", "") or None
        self.enabled        = raw.get("enabled", True)
        self.exists         = raw.get("exists", True)
        self.hittable       = raw.get("hittable", True)
        self.focused        = raw.get("focused", False)
        self.adjustable     = raw.get("adjustable", False)
        self.frame          = Frame.from_dict(raw.get("frame"))
        self.raw_label      = self.label
        self.enrichment_src = "xcuitest"
        self.inferred_label = None

    @property
    def effective_label(self): return self.label
    @property
    def effective_role(self):  return self.role

    def is_actionable(self):
        return self.role in ("btn","input","textarea","switch","slider",
                             "picker","pickerWheel","cell","link","tab","search")

    def __repr__(self):
        cx = round(self.frame.center_x) if self.frame else 0
        cy = round(self.frame.center_y) if self.frame else 0
        return f"@{self.ref[:8]} [{self.role}] \"{self.label}\" @({cx},{cy})"


class AXTree:
    def __init__(self, elements: List[AXElement], udid: str,
                 keyboard_visible: bool = False,
                 screen_width: float = 402,
                 screen_height: float = 874,
                 method: str = "snapshot",
                 bundle_id: str = "",
                 keyboard_frame: Optional[Dict[str, float]] = None):
        self.elements         = elements
        self.udid             = udid
        self.keyboard_visible = keyboard_visible
        self.screen_width     = screen_width
        self.screen_height    = screen_height
        self.method           = method  # "snapshot" | "fallback" | "none"
        self.bundle_id        = bundle_id  # foreground app's bundle ID at observe time
        # Bounding rect of the iOS software keyboard when visible
        # (from `XCUIApplication.keyboards.firstMatch.frame` server-side).
        # `None` when no keyboard is on screen. Used by the scaffold to
        # filter or annotate elements occluded by the keyboard so the
        # agent doesn't try to tap unreachable targets.
        self.keyboard_frame   = keyboard_frame

    def unlabeled(self):
        return [e for e in self.elements
                if not e.label and e.role not in
                ("other","app","window","scroll","nav","toolbar","tabbar")]

    def find(self, label: str) -> Optional[AXElement]:
        for e in self.elements:
            if e.label and label.lower() in e.label.lower():
                return e
        return None

    def find_all_role(self, role: str) -> List[AXElement]:
        return [e for e in self.elements if e.role == role]


# ─────────────────────────────────────────────────────────────────────────────
#  XCUITest persistent client
# ─────────────────────────────────────────────────────────────────────────────


def patch_xctestrun(master_path: str, udid: str,
                    candidates: Optional[List[str]] = None) -> str:
    """
    Create a per-UDID copy of the xctestrun plist with SIBB_UDID injected.
    The copy MUST sit in the same directory as the master xctestrun so that
    __TESTROOT__ resolves correctly to the build Products directory.

    `candidates` is the comma-separated list of bundle IDs the Swift server
    will iterate when auto-detecting the foreground app on each observe.
    """
    import plistlib

    with open(master_path, "rb") as f:
        data = plistlib.load(f)

    candidates_str = ",".join(candidates or [])

    # xctestrun v2 format: inject into TestConfigurations[0].TestTargets[0]
    # This is the key that the XCUITest runner process actually inherits.
    injected = False
    for cfg in data.get("TestConfigurations", []):
        for target in cfg.get("TestTargets", []):
            env = target.get("TestingEnvironmentVariables", {})
            env["SIBB_UDID"] = udid
            env["SIBB_FOREGROUND_CANDIDATES"] = candidates_str
            target["TestingEnvironmentVariables"] = env
            injected = True

    if not injected:
        # Fallback: v1 format — inject into top-level target dicts
        for target_name, target_config in data.items():
            if not isinstance(target_config, dict):
                continue
            env = target_config.get("TestingEnvironmentVariables", {})
            env["SIBB_UDID"] = udid
            env["SIBB_FOREGROUND_CANDIDATES"] = candidates_str
            target_config["TestingEnvironmentVariables"] = env

    # Write patched copy NEXT TO the original (same dir = same __TESTROOT__)
    master_dir = os.path.dirname(master_path)
    out_path   = os.path.join(master_dir, f"sibb_{udid}.xctestrun")
    with open(out_path, "wb") as f:
        plistlib.dump(data, f)

    return out_path


class XCUITestReader:
    """
    One instance per simulator. Start once per episode, use for the duration.

    Communicates via Unix domain socket — bypasses xcodebuild's stdout capture.
    N instances run concurrently with zero contention (separate sockets + dd paths).
    """

    def __init__(self, udid: str, bundle_id: str = "com.apple.reminders"):
        self.udid      = udid
        self.bundle_id = bundle_id
        self._proc     = None
        self._sock     = None
        self._conn     = None
        self._lock     = asyncio.Lock()
        self._buf      = b""
        # PID → bundle ID cache for live frontmost-app resolution.
        self._pid_map: Dict[int, str] = {}

    async def _kill_proc_group(self) -> None:
        """SIGTERM the xcodebuild PROCESS GROUP, escalate to SIGKILL.

        `_proc.kill()` only signals the xcodebuild parent — its child
        `xctest`/`testmanagerd` processes survive as launchd-reparented
        orphans. `os.killpg(pgid, ...)` signals the whole tree because
        the parent was launched with `start_new_session=True` (so its
        PID equals its PGID).
        """
        import os
        import signal

        proc = self._proc
        if proc is None:
            return
        pid = proc.pid
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            # Already gone or owned by someone else; fall back to kill().
            try:
                proc.kill()
            except Exception:
                pass
        # Give the tree ~3s to terminate cleanly. If anything's still
        # alive, escalate to SIGKILL on the whole group.
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass

    async def start(self):
        """Launch the XCUITest server. Call once per episode (~10-20s first run)."""
        ensure_software_keyboard()
        # Pre-authorize TCC permissions BEFORE xcodebuild launches the
        # runner — otherwise the first EKEventStore call can still pop
        # a user-facing permission dialog on iOS 17+.
        await ensure_runner_permissions(self.udid)

        xctestrun = find_xctestrun()
        if not xctestrun:
            raise RuntimeError(
                "XCUITest bundle not found.\n"
                f"Run: ./sibb_xcuitest_setup.sh {self.udid}"
            )

        sock   = socket_path(self.udid)
        ddpath = derived_data_path(self.udid)
        os.makedirs(ddpath, exist_ok=True)

        # Clean up stale socket
        if os.path.exists(sock):
            os.remove(sock)

        print(f"  Starting XCUITest server [{self.udid[:8]}...]")

        # Patch xctestrun: inject SIBB_UDID into TestingEnvironmentVariables
        # This is the reliable way to pass env vars into the XCUITest runner.
        # Each concurrent instance gets its own patched copy → no collision.
        candidates = list_foreground_candidates(self.udid)
        patched_xctestrun = patch_xctestrun(xctestrun, self.udid, candidates)

        self._proc = await asyncio.create_subprocess_exec(
            "xcodebuild", "test-without-building",
            "-xctestrun",        patched_xctestrun,
            "-destination",      f"id={self.udid}",
            "-derivedDataPath",  ddpath,
            # Disable the 600s default execution-time allowance so long
            # manual / agent sessions don't get the XCTest target killed
            # mid-run, which would close the Unix socket from the server side.
            "-test-timeouts-enabled", "NO",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Place xcodebuild + its children (xctest, testmanagerd) in
            # their own session group so `stop()` can `killpg` the whole
            # tree. SIGKILL on just the xcodebuild parent re-parents the
            # children to launchd and leaks them — empirically ~50-100 MB
            # per orphaned xctest, which adds up fast in long parallel
            # runs (D1.5 critic #1).
            start_new_session=True,
        )
        # Drain xcodebuild stdout continuously in the background so the test
        # target never blocks on stdout writes (Swift print() output, runtime
        # logs). Mirror everything to a log file for post-mortem debugging.
        # `_ready_evt` flips when we see SIBB_READY; start() awaits it.
        self._stdout_log = f"/tmp/sibb_server_{self.udid}.log"
        self._ready_evt  = asyncio.Event()
        self._stdout_drainer = asyncio.create_task(self._drain_stdout())

        # Ready timeout — 120s rather than 60s because under parallel
        # workers (D1b), N xcodebuild test processes share one Mac's
        # CPU/IO. xcodebuild's per-process setup time grows with N
        # (build artifact reads, sim install, runner launch). 60s
        # was sufficient single-threaded but fired prematurely with
        # concurrency=2 (caught by D1b parallel L2 test).
        print(f"  Waiting for test runner (20-30s warm, up to 120s under load)...")
        try:
            await asyncio.wait_for(self._ready_evt.wait(), timeout=120)
        except asyncio.TimeoutError:
            raise RuntimeError("XCUITest server did not signal ready in time.")

        # Wait for socket file to appear (UDID-specific via xctestrun patch)
        default_sock = "/tmp/sibb_xcuitest_default.sock"
        found_sock   = None
        for _ in range(30):
            if os.path.exists(sock):
                found_sock = sock
                break
            if os.path.exists(default_sock):   # fallback if patch failed
                found_sock = default_sock
                print(f"  WARNING: using default socket (UDID injection may have failed)")
                break
            await asyncio.sleep(0.5)
        if not found_sock:
            raise RuntimeError(
                f"Socket not created at {sock}\n"
                f"Check that SIBBHelper built successfully and simulator is booted."
            )
        sock = found_sock

        # Connect
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(sock)
        self._sock.setblocking(False)
        print(f"  Connected to XCUITest server [{self.udid[:8]}...]")

        # Attach to target app
        resp = await self._send({"type": "attach", "bundleId": self.bundle_id})
        if not resp.get("ok"):
            raise RuntimeError(f"Attach failed: {resp.get('error')}")
        print(f"  Attached to {self.bundle_id}")

    async def _drain_stdout(self):
        """
        Background reader for xcodebuild stdout. Writes every line to
        the per-UDID log file and flips `_ready_evt` when SIBB_READY
        appears. Critical: without this, the test target eventually
        blocks on a stdout write when the OS pipe buffer fills (a few
        KB of Swift `print()` output), causing observe to hang forever.
        """
        try:
            with open(self._stdout_log, "w") as f:
                while True:
                    line = await self._proc.stdout.readline()
                    if not line:
                        break
                    f.write(line.decode(errors="replace"))
                    f.flush()
                    if b"SIBB_READY" in line:
                        self._ready_evt.set()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def stop(self):
        """Shut down the server and clean up."""
        try:
            await self._send({"type": "quit"})
        except Exception:
            pass
        if self._sock:
            self._sock.close()
            self._sock = None
        if self._proc:
            await self._kill_proc_group()
        if hasattr(self, "_stdout_drainer") and self._stdout_drainer:
            self._stdout_drainer.cancel()
            try:
                await self._stdout_drainer
            except Exception:
                pass
        sock = socket_path(self.udid)
        if os.path.exists(sock):
            os.remove(sock)
        # Clean up patched xctestrun copy
        xctestrun = find_xctestrun()
        if xctestrun:
            patched = os.path.join(os.path.dirname(xctestrun),
                                   f"sibb_{self.udid}.xctestrun")
            if os.path.exists(patched):
                os.remove(patched)

    # ── Public API ────────────────────────────────────────────────────────────

    async def _resolve_frontmost(self) -> Optional[str]:
        """
        Ask the Swift server for the frontmost app's PID (via the private
        XCUIDevice.accessibilityInterface.activeApplications API), then
        translate it to a bundle ID using our launchctl-derived cache.
        Refreshes the cache on cache miss so newly-launched apps are picked
        up. Returns None if resolution fails (caller falls back to the
        currently-attached app).
        """
        try:
            resp = await self._send({"type": "frontmost"})
        except Exception:
            return None
        pid = resp.get("pid", 0)
        if not isinstance(pid, int) or pid <= 0:
            return None
        bundle = self._pid_map.get(pid)
        if bundle is None:
            # Cache miss — refresh from launchctl
            self._pid_map.update(build_pid_to_bundle_map(self.udid))
            bundle = self._pid_map.get(pid)
        return bundle

    async def observe(self) -> AXTree:
        """
        Read full AX tree of whatever is *actually* on screen.

        Flow per call:
          1. Resolve the frontmost app via XCAXClient_iOS (private API) +
             launchctl PID→bundle mapping.
          2. Tell the Swift server which bundle to observe.
          3. Swift dumps that app's tree and returns it.

        If resolution fails for any reason, falls back to the bundle the
        reader was started with.
        """
        async with self._lock:
            bundle = await self._resolve_frontmost()
            cmd = {"type": "observe"}
            if bundle:
                cmd["bundleId"] = bundle
            resp = await self._send(cmd)
            if not resp.get("ok"):
                raise RuntimeError(f"Observe failed: {resp.get('error')}")
            elements = [AXElement(e) for e in resp.get("elements", [])]
            tree = AXTree(
                elements,
                self.udid,
                keyboard_visible=resp.get("keyboard_visible", False),
                screen_width=resp.get("screen_width", 402),
                screen_height=resp.get("screen_height", 874),
                method=resp.get("method", "snapshot"),
                bundle_id=resp.get("bundle_id", "") or (bundle or ""),
                keyboard_frame=resp.get("keyboard_frame"),
            )
            # Fields added 2026-06-05 — gracefully default to None so
            # this client still works against an unbuilt SIBBHelper
            # (Swift may not emit zoom_scale / accessory_bar_frame yet).
            # See sibb_scaffold._read_xcuitest for consumers.
            tree.zoom_scale = resp.get("zoom_scale")
            tree.accessory_bar_frame = resp.get("accessory_bar_frame")
            return tree

    async def tap(self, x: float = None, y: float = None, ref: str = None):
        """Tap by coordinate or element identifier."""
        async with self._lock:
            cmd = {"type": "tap"}
            if x is not None: cmd["x"] = x
            if y is not None: cmd["y"] = y
            if ref:           cmd["ref"] = ref
            resp = await self._send(cmd)
            if not resp.get("ok"):
                raise RuntimeError(f"Tap failed: {resp.get('error')}")

    async def double_tap(self, x: float = None, y: float = None,
                          ref: str = None):
        """Double-tap by coordinate or element identifier. Dispatches
        through `XCUICoordinate.doubleTap()` (native gesture path).

        Primary use: reset Safari's WKWebView auto-zoom — `xc.tap()`
        twice in succession does NOT fire WebKit's double-tap-to-zoom
        recognizer, but this native API does. Verified empirically
        (see IOS_SIM_QUIRKS §21).
        """
        async with self._lock:
            cmd = {"type": "double_tap"}
            if x is not None: cmd["x"] = x
            if y is not None: cmd["y"] = y
            if ref:           cmd["ref"] = ref
            resp = await self._send(cmd)
            if not resp.get("ok"):
                raise RuntimeError(
                    f"double_tap failed: {resp.get('error')}")

    async def type_text(self, text: str):
        async with self._lock:
            resp = await self._send({"type": "type", "text": text})
            if not resp.get("ok"):
                raise RuntimeError(f"Type failed: {resp.get('error')}")

    async def tap_then_type(self, x: float, y: float, text: str,
                             focus_timeout_ms: int = 1500
                             ) -> Dict[str, Any]:
        """Atomic tap-to-focus + typeText with focus verification on
        the Swift side. Returns the full response dict (caller checks
        `ok` field). On focus-not-acquired, raises RuntimeError with
        the diagnostic; on success, returns the response with
        acquired_ms and typed fields.

        Policy A (fail-fast): if focus doesn't transfer to an element
        containing (x, y) within `focus_timeout_ms`, typeText is NOT
        called — keystrokes won't leak to the previously-focused
        field. Caller (sibb_replay.execute) translates the failure
        into an action result the agent can react to.
        """
        async with self._lock:
            cmd: Dict[str, Any] = {"type": "tap_then_type",
                                    "x": x, "y": y, "text": text,
                                    "focus_timeout_ms": focus_timeout_ms}
            return await self._send(cmd)

    async def clear_text(self, x: float, y: float,
                          length_hint: Optional[int] = None) -> Dict[str, Any]:
        """Clear a text field's content via the Swift-side triple-tap-
        at-coords + delete-key strategy. `length_hint` (optional) is
        the current value's character count — the Swift side uses it
        for a bounded bulk-delete fallback after the triple-tap-and-
        delete (covers fields where triple-tap selected only a word).
        """
        async with self._lock:
            cmd: Dict[str, Any] = {"type": "clear_text", "x": x, "y": y}
            if length_hint is not None:
                cmd["length_hint"] = int(length_hint)
            resp = await self._send(cmd)
            if not resp.get("ok"):
                raise RuntimeError(f"Clear failed: {resp.get('error')}")
            return resp

    async def swipe(self, direction: str = "up"):
        async with self._lock:
            resp = await self._send({"type": "swipe", "direction": direction})
            if not resp.get("ok"):
                raise RuntimeError(f"Swipe failed: {resp.get('error')}")

    async def swipe_at(self, x1: float, y1: float,
                        x2: float, y2: float,
                        duration_s: float = 0.05,
                        settle: bool = True,
                        velocity_pps: Optional[float] = None):
        """Element-targeted swipe between two screen coordinates.

        Maps to Swift's `swipe_at` command (XCUITest
        `coordinate.press(forDuration:thenDragTo:)`). Used by
        scroll/swipe actions in sibb_replay.execute() when an
        AgentAction targets a specific element — coordinates are
        derived from the element's frame so the gesture stays
        bounded by the element (carousels, picker wheels, nested
        scroll views, map panning).

        `settle=False` skips the Swift-side `waitForSettle` after the
        press-drag, returning as soon as the gesture is dispatched.
        Callers issuing batched swipes (picker-wheel cascades) should
        pass settle=False for swipes 1..N-1 and settle=True for the
        Nth — the wait-for-settle on each swipe empirically hangs for
        seconds because descendants-count keeps changing during the
        wheel's deceleration animation.
        """
        async with self._lock:
            cmd: Dict[str, Any] = {
                "type": "swipe_at",
                "x1": float(x1), "y1": float(y1),
                "x2": float(x2), "y2": float(y2),
                "duration_s": float(duration_s),
                "settle": bool(settle),
            }
            if velocity_pps is not None:
                cmd["velocity_pps"] = float(velocity_pps)
            resp = await self._send(cmd)
            if not resp.get("ok"):
                raise RuntimeError(
                    f"swipe_at failed: {resp.get('error')}")

    async def dismiss_app_onboarding(self, bundle: str) -> int:
        """Launch an app and tap through any onboarding/upgrade dialogs.

        Returns the count of dismiss-taps the runner performed. Used
        during baseline build to ensure the SIBB-11 apps don't show
        welcome/iCloud/etc prompts on the cloned sims that come from
        the baseline.

        Conservative — only taps buttons whose labels match a fixed
        allow-list (Continue, Not Now, Skip, Done, OK, Get Started,
        Maybe Later, Cancel, No Thanks, Later, Dismiss). New apps
        with different labels need their labels added on the Swift
        side (sibb_xcuitest_setup.sh).
        """
        async with self._lock:
            resp = await self._send({
                "type": "dismiss_app_onboarding",
                "bundle": bundle,
            })
            if not resp.get("ok"):
                raise RuntimeError(
                    f"dismiss_app_onboarding({bundle}) failed: "
                    f"{resp.get('error')}")
            return int(resp.get("taps", 0))

    async def press(self, button: str = "home"):
        """
        Hardware-button / system-gesture press.
          home         → XCUIDevice.press(.home), exits to home screen
          back         → left-edge swipe (in-app pop gesture)
          app_switcher → swipe-up-and-hold from bottom (recent-apps carousel)
        """
        async with self._lock:
            resp = await self._send({"type": "press", "button": button})
            if not resp.get("ok"):
                raise RuntimeError(f"Press failed: {resp.get('error')}")

    async def pinch(self, scale: float = 0.5,
                     velocity: float = 1.0) -> Dict[str, Any]:
        """Two-finger pinch gesture on the whole-app frame.

        Args:
            scale: pinch factor. >1 zooms IN, <1 zooms OUT. Default 0.5
                   (zoom out by half — the canonical Safari auto-zoom
                   recovery).
            velocity: gesture speed in scale-per-second. Default 1.0
                      (matches Apple's documented sane default).

        Primary motivation: iOS Safari auto-zooms when focusing an
        input whose `font-size < 16px`. The page can stay zoomed even
        after the agent dismisses the keyboard or leaves the app.
        `PINCH out` is the only reliable reset confirmed empirically
        on iOS 26 sim (TAP URL bar, PRESS home, app-switcher trips —
        all observed not to reset the WebView's stuck zoom).
        """
        async with self._lock:
            resp = await self._send({
                "type": "pinch",
                "scale": float(scale),
                "velocity": float(velocity),
            })
            if not resp.get("ok"):
                raise RuntimeError(f"Pinch failed: {resp.get('error')}")
            return resp

    async def launch(self, bundle_id: str = None):
        async with self._lock:
            resp = await self._send({
                "type": "launch",
                "bundleId": bundle_id or self.bundle_id
            })
            if not resp.get("ok"):
                raise RuntimeError(f"Launch failed: {resp.get('error')}")

    async def ping(self) -> bool:
        try:
            resp = await asyncio.wait_for(
                self._send({"type": "ping"}), timeout=2.0
            )
            return resp.get("ok", False)
        except Exception:
            return False

    # Drop-in replacement for AXReader.read()
    async def read(self) -> AXTree:
        return await self.observe()

    # ── Socket I/O ────────────────────────────────────────────────────────────

    async def _send(self, cmd: dict) -> dict:
        """Send JSON command over Unix socket, read JSON response."""
        if not self._sock:
            raise RuntimeError("Not connected. Call start() first.")

        # Write command
        line = json.dumps(cmd) + "\n"
        loop = asyncio.get_event_loop()
        await loop.sock_sendall(self._sock, line.encode())

        # Read response (may arrive in chunks)
        while True:
            if b"\n" in self._buf:
                idx  = self._buf.index(b"\n")
                line = self._buf[:idx]
                self._buf = self._buf[idx+1:]
                text = line.decode().strip()
                if text.startswith("{") or text.startswith("["):
                    return json.loads(text)
                # Skip non-JSON lines
                continue
            try:
                # Per-command socket-recv budget. 60s was the original
                # value; tightened to 30s on 2026-06-11 because the
                # only commands that legitimately take >30s are batched
                # ones (e.g. CLEAR with many backspaces) and even those
                # rarely cross 20s. Empirically the >30s cases were
                # iOS Calendar / Safari tapping into a heavy view that
                # hung the synthetic-touch system — failing fast lets
                # the runner-level healthcheck + recycle pattern
                # recover ~30s sooner per fault.
                chunk = await asyncio.wait_for(
                    loop.sock_recv(self._sock, 65536), timeout=30.0
                )
                if not chunk:
                    raise RuntimeError("Socket closed by server.")
                self._buf += chunk
            except asyncio.TimeoutError:
                raise TimeoutError(f"No response for command: {cmd['type']} (server may have exited — restart inspector)")


# ─────────────────────────────────────────────────────────────────────────────
#  Quick test
# ─────────────────────────────────────────────────────────────────────────────

async def quick_test(udid: str, bundle_id: str):
    reader = XCUITestReader(udid, bundle_id)
    try:
        await reader.start()

        t0   = time.time()
        tree = await reader.observe()
        ms   = round((time.time() - t0) * 1000)

        print(f"\nObserve time: {ms}ms")
        print(f"Total elements: {len(tree.elements)}")
        print(f"Unlabeled:      {len(tree.unlabeled())}")
        print()

        by_role = defaultdict(list)
        for el in tree.elements:
            by_role[el.role].append(el)

        for role in sorted(by_role):
            labeled = [e for e in by_role[role] if e.label]
            if not labeled: continue
            print(f"  [{role}] ({len(labeled)})")
            for el in labeled[:5]:
                cx  = round(el.frame.center_x) if el.frame else 0
                cy  = round(el.frame.center_y) if el.frame else 0
                val = f" = \"{el.value}\"" if el.value else ""
                dis = " (disabled)" if not el.enabled else ""
                print(f"    \"{el.label}\"{val}{dis}  @({cx},{cy})")

    finally:
        await reader.stop()


async def bench_concurrent(udids: List[str], bundle_id: str):
    """Benchmark N concurrent readers."""
    readers = [XCUITestReader(u, bundle_id) for u in udids]
    print(f"\nStarting {len(readers)} concurrent readers...")
    t0 = time.time()
    await asyncio.gather(*[r.start() for r in readers])
    print(f"All started in {round((time.time()-t0)*1000)}ms")

    t0 = time.time()
    trees = await asyncio.gather(*[r.observe() for r in readers])
    ms = round((time.time()-t0)*1000)
    print(f"Concurrent observe: {ms}ms  "
          f"({[len(t.elements) for t in trees]} elements)")

    await asyncio.gather(*[r.stop() for r in readers])


if __name__ == "__main__":
    udid      = sys.argv[1] if len(sys.argv) > 1 else None
    bundle_id = sys.argv[2] if len(sys.argv) > 2 else "com.apple.reminders"

    if not udid:
        r = subprocess.run(["xcrun","simctl","list","devices","--json"],
                           capture_output=True, text=True)
        for devs in json.loads(r.stdout).get("devices",{}).values():
            for d in devs:
                if d.get("state") == "Booted":
                    udid = d["udid"]; break
            if udid: break

    if not udid:
        print("No booted simulator found.")
        sys.exit(1)

    print(f"SIBB XCUITest Client  —  {udid}")
    print(f"Bundle: {bundle_id}")
    asyncio.run(quick_test(udid, bundle_id))
