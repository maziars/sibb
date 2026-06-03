#!/usr/bin/env python3
"""
SIBB state framework — reset and apply per-app state for a task.
================================================================

Per-app handlers implement two operations:

  reset(udid)        wipe the app's user-visible data back to a known
                     baseline (default lists, no items, no events, …)
  apply(udid, entry) realize one entry from a task's
                     `initial_state.spec` list

`apply_initial_state(udid, task)` is the single entry point used by the
runner and the manual-replay tool. It dispatches:

  1. Reset every app referenced in the task (via task.apps or via the
     spec entries' "app" field).
  2. Apply each spec entry to its target app's handler.

Adding a new app is a small isolated chunk of work: write one handler
class that conforms to `AppStateHandler` and register it in `HANDLERS`.

Strategy per handler:

  - Schema-aware (direct SQLite / plist edits) — preferred where the
    layout is well-understood. Fast (~100 ms per entry), deterministic.
  - UI-driven (drive the app through the scaffold) — fallback for apps
    whose stores are too risky to edit directly. Slow (~10–30 s/entry).

The framework doesn't care which strategy a handler picks.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from typing import Any, ClassVar, Dict, List, Optional, Protocol


# ─────────────────────────────────────────────────────────────────────────────
#  Handler protocol
# ─────────────────────────────────────────────────────────────────────────────
#
# Async-first by design: all post-boot state changes flow through the
# XCUITest Unix socket, which is async. Handlers that do shut-down-time
# work (Springboard plist edits) additionally declare `pre_runner=True`
# and implement `apply_pre_runner(udid, entry)` — that path is sync
# because there's no live socket yet, only `simctl`/file edits.
#
# Class attributes declared here are CONSUMED by:
#   - `tcc_services`  → A2 will make `ensure_runner_permissions` iterate
#                       HANDLERS for these instead of hardcoding.
#   - `pre_runner`    → A2 will replace the external `PRE_RUNNER_KINDS`
#                       denylist by asking each handler instead.
#   - `depends_on`    → A4 will topo-sort `apply_initial_state` by these.
#   - `bundle_id`     → A3 will key the registry by bundle id and
#                       canonicalize `task.apps` strings on lookup.
# Today (A1) the attributes are declared and populated but most are not
# yet read — each item lights up the corresponding consumer.

class AppStateHandler(Protocol):
    bundle_id: ClassVar[str]
    tcc_services: ClassVar[List[str]]
    pre_runner: ClassVar[bool]
    pre_runner_kinds: ClassVar[List[str]]
    depends_on: ClassVar[List[str]]

    def __init__(self, reader: Optional[Any] = None) -> None: ...

    async def reset(self) -> None:
        """Wipe user-visible data; the next launch sees a known baseline."""
        ...

    async def apply(self, entry: Dict[str, Any]) -> None:
        """Realize one post-boot state-descriptor entry."""
        ...

    # Optional. Only handlers with non-empty `pre_runner_kinds` implement
    # this. Called by `apply_pre_runner_setup` while the sim is shut down.
    # def apply_pre_runner(self, udid: str, entry: Dict[str, Any]) -> None: ...

# Invariant: `pre_runner == bool(pre_runner_kinds)`. The boolean is kept
# as an explicit declarative flag (mistakes get caught by the protocol
# test); the list enumerates which entry `type` values route to the
# sync `apply_pre_runner` path rather than the async `apply` path.


# ─────────────────────────────────────────────────────────────────────────────
#  Reminders handler — EventKit via the XCUITest socket
# ─────────────────────────────────────────────────────────────────────────────
#
# Why not direct SQLite: Reminders maintains the row in ZREMCDBASELIST /
# ZREMCDREMINDER *and* an account-level manifest (ZREMCDACCOUNTLISTDATA.
# ZORDEREDIDENTIFIERMAP is an NSKeyedArchiver-encoded REMOrderedIdentifierMap),
# plus a per-row CRDT replica state (ZRESOLUTIONTOKENMAP_V3_JSONDATA), plus
# a CK mirror (ZCKDIRTYFLAGS / ZCKCLOUDSTATE). A row inserted directly via
# SQL is silently ignored by the app because the manifest doesn't reference
# its identifier. EventKit (`EKEventStore`) is Apple's official API and
# maintains every piece of that state atomically. The XCUITest runner runs
# inside the simulator process with full UIKit + EventKit access, so we
# route writes through the socket: Python sends a one-line JSON command;
# Swift calls EKEventStore on Apple's terms.
#
# Required wiring (already handled by `XCUITestReader.start()`):
#   - `simctl privacy grant reminders com.sibb.tests.xctrunner` BEFORE
#     xcodebuild launches the runner. iOS 17+ still shows a transparency
#     dialog after the first EventKit call even with TCC=Allowed; the
#     Swift server auto-dismisses it via `dismissPermissionDialogs()`.
#   - `NSRemindersUsageDescription` is auto-injected into the test
#     runner's Info.plist by Xcode (no manual setup needed).


class RemindersHandler:
    """
    Reminders state setup via EventKit.

    Holds an XCUITestReader reference so it can talk to the in-simulator
    EventKit-backed socket commands defined in `sibb_xcuitest_setup.sh`.
    The reader is passed at construction time by the dispatcher.
    """

    bundle_id: ClassVar[str] = "com.apple.reminders"
    tcc_services: ClassVar[List[str]] = ["reminders"]
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        resp = await self.reader._send({"type": "wipe_reminders"})
        if not resp.get("ok"):
            raise RuntimeError(f"wipe_reminders failed: {resp.get('error')}")

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind == "list":
            resp = await self.reader._send({
                "type": "create_list",
                "name": entry["name"],
            })
        elif kind == "item":
            cmd: Dict[str, Any] = {
                "type": "create_reminder",
                "title":    entry["title"],
                "list":     entry["list"],
                "priority": entry.get("priority"),
                "completed": bool(entry.get("completed", False)),
            }
            for opt in ("due_iso", "notes", "url", "recurrence"):
                if entry.get(opt) is not None:
                    cmd[opt] = entry[opt]
            resp = await self.reader._send(cmd)
        else:
            raise ValueError(f"RemindersHandler: unknown entry type {kind!r}")
        if not resp.get("ok"):
            raise RuntimeError(
                f"RemindersHandler.{kind} failed: {resp.get('error')}")


# ─────────────────────────────────────────────────────────────────────────────
#  Calendar handler — EventKit (.event entity) via the XCUITest socket
# ─────────────────────────────────────────────────────────────────────────────
#
# Mirrors RemindersHandler — same socket, different EKEntityType.
# Swift's create_event/list_events/wipe_events take an ISO8601 start/end
# pair. Generators emit those strings (or a `SymbolicRef` resolves to
# them in Phase 2c C1).
#
# `tcc_services=["calendar"]` flows through `collect_tcc_services` to
# `ensure_runner_permissions` automatically. No `simctl privacy grant`
# edits required when adding this handler.

class CalendarHandler:
    """Calendar state setup via EventKit `.event` entity. Supports two
    entry types:
      • `event` — creates an EKEvent (CalendarEvent spec)
      • `calendar` — creates an EKCalendar of type .event on the
                     `.local` source (Calendar spec dataclass)

    Reset wipes both events AND user-created calendars (preserves the
    iOS default `"Calendar"`). Order matters: events must be wiped
    BEFORE calendars, since removing a non-empty calendar fails.

    Apply ordering: `calendar` entries must be applied before `event`
    entries (events reference calendars by name). The dispatcher in
    `apply_initial_state` reads `apply_order_by_type` and sorts
    per-handler entries before the apply loop — so generators no
    longer need to hand-order their spec lists."""

    bundle_id: ClassVar[str] = "com.apple.mobilecal"
    tcc_services: ClassVar[List[str]] = ["calendar"]
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []
    # Two-phase apply: calendars before events. Unranked types sort to
    # the end (default 99) — fine for future extensions like alarms.
    apply_order_by_type: ClassVar[Dict[str, int]] = {
        "calendar": 0,
        "event":    1,
    }

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # Wipe events first — removing a non-empty calendar fails.
        resp = await self.reader._send({"type": "wipe_events"})
        if not resp.get("ok"):
            raise RuntimeError(f"wipe_events failed: {resp.get('error')}")
        # Then wipe user-created calendars (default "Calendar" survives).
        resp = await self.reader._send({"type": "wipe_calendars"})
        if not resp.get("ok"):
            raise RuntimeError(
                f"wipe_calendars failed: {resp.get('error')}")

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind == "event":
            cmd: Dict[str, Any] = {
                "type":      "create_event",
                "title":     entry["title"],
                "start_iso": entry["start_iso"],
                "end_iso":   entry["end_iso"],
            }
            for opt in ("calendar", "all_day", "location", "notes",
                         "url", "recurrence"):
                if entry.get(opt) is not None:
                    cmd[opt] = entry[opt]
            resp = await self.reader._send(cmd)
        elif kind == "calendar":
            cmd = {"type": "create_calendar", "name": entry["name"]}
            if entry.get("color") is not None:
                cmd["color"] = entry["color"]
            resp = await self.reader._send(cmd)
        else:
            raise ValueError(f"CalendarHandler: unknown entry type {kind!r}")
        if not resp.get("ok"):
            raise RuntimeError(
                f"CalendarHandler.{kind} failed: {resp.get('error')}")


# ─────────────────────────────────────────────────────────────────────────────
#  Contacts handler — Contacts.framework (CNContact) via XCUITest socket
# ─────────────────────────────────────────────────────────────────────────────
#
# Direct analog to RemindersHandler/CalendarHandler — same socket, same
# shape, different framework (Contacts.framework instead of EventKit).
# Swift's create_contact/list_contacts/wipe_contacts back this handler.
#
# tcc_services=["contacts"] flows through collect_tcc_services to
# ensure_runner_permissions automatically — no simctl privacy grant
# edits required when adding this handler.

class ContactsHandler:
    """Contacts state setup via Contacts.framework (CNContactStore)."""

    bundle_id: ClassVar[str] = "com.apple.MobileAddressBook"
    tcc_services: ClassVar[List[str]] = ["contacts"]
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        resp = await self.reader._send({"type": "wipe_contacts"})
        if not resp.get("ok"):
            raise RuntimeError(
                f"wipe_contacts failed: {resp.get('error')}")

    # Simple scalar fields the Swift side accepts on create_contact /
    # update_contact. New v2 fields land in this list to keep
    # apply() concise.
    _SCALAR_FIELDS: ClassVar[List[str]] = [
        "given_name", "family_name", "middle_name", "nickname",
        "phonetic_given_name", "phonetic_family_name", "phonetic_middle_name",
        "phone", "email",  # legacy single-value
        "organization", "job_title", "department",
        "birthday",  # "YYYY-MM-DD" or "--MM-DD"
    ]
    # Multi-value labeled fields. Each is a list of dicts on the wire;
    # Swift maps labels through canonicalPhoneLabel/etc.
    _MULTI_FIELDS: ClassVar[List[str]] = [
        "phones",           # [{label, value}]
        "emails",           # [{label, value}]
        "postal_addresses", # [{label, street, city, state, postal_code, country}]
        "urls",             # [{label, value}]
        "dates",            # [{label, iso}]
    ]

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind == "contact":
            # Create: at least one of given_name / family_name is
            # required (Swift enforces).
            cmd: Dict[str, Any] = {"type": "create_contact"}
            for opt in self._SCALAR_FIELDS + self._MULTI_FIELDS:
                if entry.get(opt) is not None:
                    cmd[opt] = entry[opt]
            resp = await self.reader._send(cmd)
        elif kind == "update_contact":
            # Update an existing contact by `identifier`. Multi-value
            # arrays REPLACE the existing array on the target contact;
            # omit a key to leave it unchanged.
            ident = entry.get("identifier")
            if not ident:
                raise ValueError(
                    "ContactsHandler.update_contact requires identifier")
            cmd = {"type": "update_contact", "identifier": ident}
            for opt in self._SCALAR_FIELDS + self._MULTI_FIELDS:
                if opt in entry:  # presence (not None) — allows "" to clear
                    cmd[opt] = entry[opt]
            resp = await self.reader._send(cmd)
        else:
            raise ValueError(
                f"ContactsHandler: unknown entry type {kind!r}")
        if not resp.get("ok"):
            raise RuntimeError(
                f"ContactsHandler.{kind} failed: {resp.get('error')}")


# ─────────────────────────────────────────────────────────────────────────────
#  Files handler — FileManager-backed workspace inside the runner sandbox
# ─────────────────────────────────────────────────────────────────────────────
#
# Different shape from the Reminders/Calendar/Contacts handlers: no
# framework store, no permission grant. Swift's create_file /
# list_files / wipe_files use FileManager directly. All paths are
# scoped to the runner's `Documents/SIBBWorkspace/` directory — see
# sibbWorkspaceRoot() in sibb_xcuitest_setup.sh.
#
# Visibility caveat (documented in setup.sh too): files written here
# live in the SIBBTests-Runner sandbox and are NOT visible in the
# Files UI unless `UIFileSharingEnabled` is set on the runner's
# Info.plist. v1 doesn't flip that — handler is correct for state
# setup and verifier reads; UI navigation is a follow-up.

class FilesHandler:
    """Files state setup via FileManager (SIBB workspace directory)."""

    bundle_id: ClassVar[str] = "com.apple.DocumentsApp"
    tcc_services: ClassVar[List[str]] = []
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        resp = await self.reader._send({"type": "wipe_files"})
        if not resp.get("ok"):
            raise RuntimeError(f"wipe_files failed: {resp.get('error')}")

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind == "file":
            cmd: Dict[str, Any] = {
                "type": "create_file",
                "path": entry["path"],
                "content": entry.get("content", ""),
            }
            if entry.get("encoding") is not None:
                cmd["encoding"] = entry["encoding"]
            resp = await self.reader._send(cmd)
        else:
            raise ValueError(
                f"FilesHandler: unknown entry type {kind!r}")
        if not resp.get("ok"):
            raise RuntimeError(
                f"FilesHandler.{kind} failed: {resp.get('error')}")


# ─────────────────────────────────────────────────────────────────────────────
#  Settings handler — host-side `simctl spawn defaults` shellout
# ─────────────────────────────────────────────────────────────────────────────
#
# Different shape from the runner-backed handlers above (Reminders /
# Calendar / Contacts / Files): Settings state lives in per-domain
# preferences plists at
#   /Library/Developer/CoreSimulator/Devices/<UDID>/data/Library/Preferences/
# and per-app sandbox preferences. iOS sandboxing means the XCUITest
# runner can only access its OWN UserDefaults — it can't reach
# `com.apple.Preferences` or any other app's domain. So the handler
# can't go through the XCUITest socket.
#
# Instead it shells out to `xcrun simctl spawn <udid> defaults
# write/read/delete`. The handler only needs `reader.udid` from the
# injected reader (everything else is host-side subprocess).
#
# Reset semantics: v1 is a no-op. Between-episode isolation comes
# from clone-from-baseline (F1); within-episode rollback isn't yet
# needed by any task generator. When we add that, we'll snapshot
# the touched (domain, key) pairs at apply time and restore at
# reset time.

# Module-level subprocess helpers so tests can monkeypatch them
# (same pattern as restart_springboard / simctl_clone in sibb_simctl).

_VALUE_FLAG_BY_TYPE = {
    "bool":   "-bool",
    "int":    "-int",
    "string": "-string",
    "float":  "-float",
}


async def _simctl_defaults_write(udid: str, domain: str, key: str,
                                  value: Any, value_type: str) -> None:
    """`xcrun simctl spawn <udid> defaults write <domain> <key> -<type> <value>`.
    Raises RuntimeError on nonzero return code."""
    flag = _VALUE_FLAG_BY_TYPE.get(value_type)
    if flag is None:
        raise ValueError(
            f"Settings: unsupported value_type {value_type!r}. "
            f"Valid: {sorted(_VALUE_FLAG_BY_TYPE)}"
        )
    # `defaults` expects bool values as "YES" / "NO" strings, not Python
    # True/False. simctl spawn shells the args through launchctl, which
    # bools-as-strings via `defaults` syntax accept fine.
    if value_type == "bool":
        value_str = "YES" if value else "NO"
    else:
        value_str = str(value)
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "spawn", udid,
        "defaults", "write", domain, key, flag, value_str,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"simctl spawn defaults write timed out for "
            f"{domain}:{key} on {udid}")
    if proc.returncode != 0:
        msg = (err or out).decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"simctl spawn defaults write {domain} {key} failed "
            f"(rc={proc.returncode}): {msg}")


async def _simctl_defaults_read(udid: str, domain: str,
                                 key: Optional[str] = None) -> str:
    """`xcrun simctl spawn <udid> defaults read <domain> [<key>]`.

    Returns the raw stdout string. Caller is responsible for parsing
    (bool/int/string roundtrip through `defaults` is type-lossy on
    read — `defaults read` always returns text).

    Returns the empty string when the domain or key doesn't exist
    (`defaults read` exits nonzero in that case; we swallow that
    specific failure mode for read-only callers like verifier fetchers
    that need "absent" to surface as an empty value rather than an
    exception).
    """
    args = ["xcrun", "simctl", "spawn", udid,
            "defaults", "read", domain]
    if key is not None:
        args.append(key)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"simctl spawn defaults read timed out for "
            f"{domain}:{key!r} on {udid}")
    if proc.returncode != 0:
        return ""
    return out.decode("utf-8", errors="replace").strip()


class SettingsHandler:
    """Settings state via host-side `simctl spawn defaults`."""

    bundle_id: ClassVar[str] = "com.apple.Preferences"
    tcc_services: ClassVar[List[str]] = []
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # v1: no-op. Between-episode isolation comes from clone-from-
        # baseline. Within-episode rollback is deferred.
        pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind != "default":
            raise ValueError(
                f"SettingsHandler: unknown entry type {kind!r}")
        if not self.reader or not getattr(self.reader, "udid", None):
            raise RuntimeError(
                "SettingsHandler requires a reader with a .udid "
                "attribute — the handler shells out to "
                "`simctl spawn defaults write` per-key.")
        await _simctl_defaults_write(
            self.reader.udid,
            entry["domain"], entry["key"],
            entry["value"],
            entry.get("value_type", "string"),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Photos handler — host-side `simctl addmedia` + runner-side PhotoKit
# ─────────────────────────────────────────────────────────────────────────────
#
# Asymmetric transport: apply shells out from Python (the path-to-
# image file is on the HOST, not the sim, and `simctl addmedia` is
# the cleanest way to inject it). Reset + list go through the
# XCUITest socket because PhotoKit is the only API that can
# enumerate / delete assets, and PhotoKit lives in the runner
# sandbox.
#
# TCC: `photos` (legacy / readWrite on iOS 14+). The runner needs
# both READ (to list) and WRITE (to delete) access.

async def _simctl_addmedia(udid: str, host_path: str) -> None:
    """`xcrun simctl addmedia <udid> <host_path>`.

    Apple's documented path for injecting an image/video into the
    sim photo library from a host file. Works without booting the
    Photos app. Raises RuntimeError on nonzero return code.
    """
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "addmedia", udid, host_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"simctl addmedia timed out for {host_path} on {udid}")
    if proc.returncode != 0:
        msg = (err or out).decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"simctl addmedia {host_path} failed "
            f"(rc={proc.returncode}): {msg}")


class PhotosHandler:
    """Photos state via PhotoKit (read/delete) + simctl addmedia (write)."""

    bundle_id: ClassVar[str] = "com.apple.mobileslideshow"
    tcc_services: ClassVar[List[str]] = ["photos"]
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        resp = await self.reader._send({"type": "wipe_photos"})
        if not resp.get("ok"):
            raise RuntimeError(
                f"wipe_photos failed: {resp.get('error')}")

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind != "media":
            raise ValueError(
                f"PhotosHandler: unknown entry type {kind!r}")
        host_path = entry.get("host_path")
        if not host_path:
            raise ValueError(
                "PhotosHandler.media: host_path is required "
                "(absolute path to an image/video file on the host)")
        if not getattr(self.reader, "udid", None):
            raise RuntimeError(
                "PhotosHandler requires a reader with a .udid "
                "attribute — apply shells out to `simctl addmedia`.")
        await _simctl_addmedia(self.reader.udid, host_path)


# ─────────────────────────────────────────────────────────────────────────────
#  Health handler — HealthKit (HKHealthStore) via the XCUITest socket
# ─────────────────────────────────────────────────────────────────────────────
#
# Different from Reminders/Calendar/Contacts in two ways:
# 1. HealthKit's authorization is per-sample-type AND split into
#    share (read) and update (write). The runner declares both
#    tcc_services so ensure_runner_permissions grants both ahead
#    of time.
# 2. Wipe semantics are scoped to "samples this app wrote". Apple's
#    cross-app deletion ban means we can't accidentally clobber
#    user data — exactly the reset behavior we want.
#
# Supported sample types in v1 (mirror sibb_xcuitest_setup.sh):
#   step_count, heart_rate, body_mass
# Adding a new type means:
#   1. Add to HEALTH_QUANTITY_TYPES in setup.sh
#   2. Add to HEALTH_VALID_TYPES below
#   3. Rebuild the runner (setup.sh fingerprint flip auto-rebuilds
#      the baseline)

HEALTH_VALID_TYPES: List[str] = [
    "step_count",
    "heart_rate",
    "body_mass",
]


class HealthHandler:
    """Health state setup via HealthKit (HKHealthStore).

    ⚠ Currently sim-limited. Apple's documentation: "The simulator
    has no Health data and you should always test on a real iPhone."
    Empirically: `HKHealthStore.requestAuthorization` either returns
    silently denied or shows an in-runner consent sheet our
    SpringBoard-targeted dismissal can't reach. The handler /
    spec / fetcher / fake-reader / L1+L1.5 tests are all complete
    and pass; only the live HealthKit integration is blocked.
    See `sibb/docs/IOS_SIM_QUIRKS.md` §10 and
    `sibb/tests/integration/test_health_handler_sim.py` module
    docstring for the workaround design.
    """

    bundle_id: ClassVar[str] = "com.apple.Health"
    tcc_services: ClassVar[List[str]] = ["health-share", "health-update"]
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        resp = await self.reader._send({"type": "wipe_health_samples"})
        if not resp.get("ok"):
            raise RuntimeError(
                f"wipe_health_samples failed: {resp.get('error')}")

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind != "sample":
            raise ValueError(
                f"HealthHandler: unknown entry type {kind!r}")
        sample_type = entry.get("sample_type")
        if sample_type not in HEALTH_VALID_TYPES:
            raise ValueError(
                f"HealthHandler.sample: unknown sample_type "
                f"{sample_type!r}; valid: {HEALTH_VALID_TYPES}")
        value = entry.get("value")
        if value is None:
            raise ValueError(
                "HealthHandler.sample: value is required")
        start_iso = entry.get("start_iso")
        if not start_iso:
            raise ValueError(
                "HealthHandler.sample: start_iso is required")
        cmd: Dict[str, Any] = {
            "type": "create_health_sample",
            "sample_type": sample_type,
            "value": value,
            "start_iso": start_iso,
        }
        if entry.get("end_iso") is not None:
            cmd["end_iso"] = entry["end_iso"]
        resp = await self.reader._send(cmd)
        if not resp.get("ok"):
            raise RuntimeError(
                f"HealthHandler.sample failed: {resp.get('error')}")


# ─────────────────────────────────────────────────────────────────────────────
#  Fitness handler — workouts (TBD) + activity-ring reads via healthdb
# ─────────────────────────────────────────────────────────────────────────────
#
# Fitness shares the HealthKit store with HealthHandler. Sample types
# (steps, heart rate, body mass) belong to HealthHandler; Fitness owns
# the workout-shaped writes (HKWorkout — TBD) and the activity-ring /
# workout-list READ surfaces via host-side sqlite against
# `data/Library/Health/healthdb_secure.sqlite`.
#
# Why host-side sqlite for reads: the HealthKit auth dialog blocks
# write-side access from the test runner on simulator
# (IOS_SIM_QUIRKS §10) — but the DB on disk is unencrypted and
# readable via `sqlite3` from the host. We use the same trick for
# `keychain-2-debug.db` (Passwords) and `Bookmarks.db` (Safari):
# bypass the on-device authorization layer entirely. Writes still
# need a real Swift command path.
#
# Empirical findings 2026-05-17 (iOS 26.3 sim):
#   - `samples` table has 14k rows (mostly step_count, data_type=5);
#     joined with `quantity_samples` for values, or `activity_caches`
#     for ring data.
#   - `activity_caches` is the ring source: `energy_burned`,
#     `energy_burned_goal` (Move ring), `brisk_minutes`,
#     `brisk_minutes_goal` (Exercise ring), `active_hours`,
#     `active_hours_goal` (Stand ring), `steps`.
#   - On iPhone-only sim only Move/steps are populated; the
#     Exercise + Stand columns are NULL (those need Apple Watch
#     to drive them).
#   - `workouts` table exists with the right schema but is empty —
#     no HKWorkout writes yet. v1 ships read-only; writes land
#     when the first Fitness task that needs a workout is built.
#
# v1 scope:
#   - registry + canonicalization (bundle id, TCC services,
#     friendly-name alias)
#   - resource fetcher `fitness.activity_summary` reads
#     `activity_caches` rows for a date or range
#   - no apply primitive (workouts deferred)
#   - reset is a noop (clone-from-baseline gives isolation)


def _healthdb_path(udid: str) -> str:
    """Path to the simulator's HealthKit private DB on the host
    filesystem. Same schema iOS 17+ — `IOS_SIM_QUIRKS.md` §10 has
    the Apple Developer link for the broader limitation."""
    return os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}"
        f"/data/Library/Health/healthdb_secure.sqlite"
    )


# Apple's reference epoch is 2001-01-01 00:00:00 UTC (Cocoa "reference
# date"). HealthKit stores `start_date` / `end_date` as seconds since
# that point; add this constant to convert to a Unix timestamp.
_APPLE_REFERENCE_EPOCH: int = 978307200


def _mapsdb_path(udid: str) -> Optional[str]:
    """Path to Maps' live MapsSync sqlite. **Maps writes to its
    app-container copy**, NOT the canonical `~/Library/.../data/Library/
    Maps/MapsSync_0.0.1` (which is a stale 0-row copy iOS keeps for
    other reasons).

    Empirically verified 2026-05-24 — see `sibb/simulator/sibb_probe_
    maps_history.py`. ZHISTORYITEM rows land in the container copy
    only; the data-dir copy stays empty.

    Resolution strategy:
      1. Try `simctl get_app_container` — fast, accurate. Requires
         the sim to be BOOTED.
      2. Fallback: filesystem walk under the device's Containers/Data/
         Application/<UUID>/Library/Maps/. Required because the
         verifier may run after the agent declares DONE and the
         sim has been shut down — simctl returns "Unable to lookup
         in current state: Shutdown" then, but the DB file itself
         is still on disk and readable. Discovered 2026-05-28 during
         variant D validation: the agent's commit row was on disk
         (pk=28 z_ent=16) but the verifier returned [] because
         `_mapsdb_path` returned None.

    Returns None only if Maps was never installed/launched on this
    sim (no container directory exists at all).
    """
    import subprocess
    container: Optional[str] = None
    try:
        out = subprocess.run(
            ["xcrun", "simctl", "get_app_container",
             udid, "com.apple.Maps", "data"],
            capture_output=True, text=True, timeout=5.0,
        )
        if out.returncode == 0:
            container = out.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if container:
        db = os.path.join(container, "Library", "Maps", "MapsSync_0.0.1")
        if os.path.exists(db):
            return db

    # Filesystem-walk fallback. Each app gets a UUID-named container
    # under Containers/Data/Application/. Maps' container is the one
    # whose subdir contains Library/Maps/MapsSync_0.0.1. Walk and
    # return the first match (only one Maps container per sim).
    devices_root = os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}/data/"
        f"Containers/Data/Application")
    if not os.path.isdir(devices_root):
        return None
    try:
        for app_uuid in os.listdir(devices_root):
            candidate = os.path.join(devices_root, app_uuid,
                                       "Library", "Maps", "MapsSync_0.0.1")
            if os.path.exists(candidate):
                return candidate
    except OSError:
        return None
    return None


# Maps Core Data entity IDs (resolved via `Z_PRIMARYKEY`). Probe-confirmed
# 2026-05-24. `ZTYPE` column is ALWAYS NULL on iOS 26 — `Z_ENT` is the
# real discriminator.
MAPS_Z_ENT_HISTORY_PLACE = 20      # Tap search result / tap Directions
MAPS_Z_ENT_HISTORY_DIRECTIONS = 16  # Committed route (GO)
MAPS_Z_ENT_HISTORY_SEARCH = 22     # `?q=…` openurl committed without picking
MAPS_Z_ENT_SEARCH_FLAVORED = {MAPS_Z_ENT_HISTORY_PLACE,
                               MAPS_Z_ENT_HISTORY_SEARCH}


def _maps_history(udid: str, limit: int = 1000) -> List[Dict[str, Any]]:
    """Read ZHISTORYITEM rows — Maps' recents/history store.

    `limit` caps rows (default 1000). Real users accumulate ~10K
    history entries over a year; on a sim reused across episodes
    without periodic wipe, ZHISTORYITEM grows monotonically. Pass
    limit=0 to disable the cap.

    Returns rows shaped like (probe-verified column population):
        {"z_ent":             20,                          # 20=Place, 22=Search, 16=Directions
         "query":             "Blue Bottle Coffee" | "",  # ZQUERY (populated for Z_ENT=22)
         "location_display":  "Palo Alto" | "",           # ZLOCATIONDISPLAY (region-dependent)
         "latitude":          37.3348863 | None,          # ZLATITUDE1 (populated for Z_ENT=20)
         "longitude":         -122.0089878 | None,        # ZLONGITUDE1
         "muid":              559098170073364042 | None,  # ZMUID1 (place id, Z_ENT=20)
         "create_iso":        "2026-05-24T21:30:00Z",     # ZCREATETIME
         "modification_iso":  "2026-05-24T21:30:00Z"}

    `ZTYPE` is intentionally NOT projected — empirically always NULL.

    Returns [] if the container DB doesn't exist (Maps never launched
    on this sim).
    """
    db = _mapsdb_path(udid)
    if db is None:
        return []
    import datetime as _dt
    import sqlite3 as _sqlite3_maps
    # Open with `immutable=1` URI so we bypass the WAL coordination
    # with Maps' writer process. Without this, our read can MISS rows
    # that Maps has committed but whose pages aren't yet in our
    # private mmap view — file mtime can lag the actual row commit
    # by 5-15+ seconds while WAL checkpointing catches up. The
    # immutable read sees the file as a snapshot, including committed
    # rows in the WAL.
    #
    # Per 2026-05-28 research: ZHISTORYITEM rows ARE committed
    # synchronously when Maps shows the directions UI — the apparent
    # async delay was a SQLite read artifact, not a Maps write delay.
    conn = _sqlite3_maps.connect(
        f"file:{db}?immutable=1&mode=ro", uri=True, timeout=2.0)
    try:
        sql = ("SELECT Z_ENT, ZQUERY, ZLOCATIONDISPLAY, "
               "       ZLATITUDE1, ZLONGITUDE1, ZMUID1, "
               "       ZCREATETIME, ZMODIFICATIONTIME "
               "FROM ZHISTORYITEM "
               "ORDER BY ZCREATETIME DESC")
        try:
            if limit and limit > 0:
                rows = conn.execute(sql + " LIMIT ?;",
                                      (limit,)).fetchall()
            else:
                rows = conn.execute(sql + ";").fetchall()
        except _sqlite3_maps.OperationalError:
            # ZHISTORYITEM table doesn't exist yet — possible on a
            # freshly-prewarmed sim where Maps installed but never
            # exercised history-write code paths. Return empty.
            return []
    finally:
        conn.close()

    def _iso(apple_ts: Optional[float]) -> Optional[str]:
        if apple_ts is None:
            return None
        return _dt.datetime.utcfromtimestamp(
            float(apple_ts) + _APPLE_REFERENCE_EPOCH
        ).isoformat() + "Z"

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "z_ent":            r[0],
            "query":            r[1] or "",
            "location_display": r[2] or "",
            "latitude":         r[3],
            "longitude":        r[4],
            "muid":             r[5],
            "create_iso":       _iso(r[6]),
            "modification_iso": _iso(r[7]),
        })
    return out


def _fitness_activity_summary(udid: str) -> List[Dict[str, Any]]:
    """Read activity_caches rows joined with samples for date info.

    Each row corresponds to one calendar day of activity data —
    the daily "rings" Fitness renders on its Summary tab. Columns
    that depend on Apple Watch input (brisk_minutes, active_hours)
    are NULL on iPhone-only sims.

    Returns rows shaped like:
        {"start_iso": "2026-05-17T07:00:00Z",
         "end_iso":   "2026-05-18T07:00:00Z",
         "energy_burned": 2229.55,
         "energy_burned_goal": 120.0,
         "brisk_minutes": None,
         "brisk_minutes_goal": None,
         "active_hours": None,
         "active_hours_goal": None,
         "steps": 105593.0}
    """
    db = _healthdb_path(udid)
    if not os.path.exists(db):
        return []
    import datetime as _dt
    import sqlite3 as _sqlite3_fit
    conn = _sqlite3_fit.connect(db, timeout=2.0)
    try:
        rows = conn.execute(
            "SELECT s.start_date, s.end_date, "
            "       ac.energy_burned, ac.energy_burned_goal, "
            "       ac.brisk_minutes, ac.brisk_minutes_goal, "
            "       ac.active_hours, ac.active_hours_goal, "
            "       ac.steps "
            "FROM activity_caches ac "
            "JOIN samples s ON s.data_id = ac.data_id "
            "ORDER BY s.start_date DESC;"
        ).fetchall()
    finally:
        conn.close()

    def _iso(apple_ts: Optional[float]) -> Optional[str]:
        if apple_ts is None:
            return None
        return _dt.datetime.utcfromtimestamp(
            float(apple_ts) + _APPLE_REFERENCE_EPOCH
        ).isoformat() + "Z"

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "start_iso":          _iso(r[0]),
            "end_iso":            _iso(r[1]),
            "energy_burned":      r[2],
            "energy_burned_goal": r[3],
            "brisk_minutes":      r[4],
            "brisk_minutes_goal": r[5],
            "active_hours":       r[6],
            "active_hours_goal":  r[7],
            "steps":              r[8],
        })
    return out


class FitnessHandler:
    """Fitness state setup — workouts (TBD) + activity-ring reads.

    Shares the HealthKit store with `HealthHandler`. Sample types
    live there; Fitness owns workout-shaped entries and the
    Summary-page ring reads.

    v1 scope: registry only. Workout writes (HKWorkout, via a new
    Swift command path) land with the first Fitness task that
    needs them. Reads work today via host-side sqlite — see the
    `fitness.activity_summary` resource fetcher.
    """

    bundle_id: ClassVar[str] = "com.apple.Fitness"
    tcc_services: ClassVar[List[str]] = ["health-share", "health-update"]
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    # No formal `depends_on` even though we share a HealthKit store
    # with HealthHandler — handler dispatch is independent per app
    # and the shared store doesn't impose ordering constraints
    # (HKHealthStore is thread-safe and our reads/writes don't
    # interlock).
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # Clone-from-baseline handles between-episode isolation.
        # When workout writes land, this will gain a per-workout
        # wipe via a new Swift command.
        pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        raise ValueError(
            f"FitnessHandler: v1 has no apply primitive. Workout "
            f"entries (HKWorkout) deferred until the first Fitness "
            f"task ships. Got entry type {entry.get('type')!r}; "
            f"see IOS_SIM_QUIRKS §10 + `_healthdb_path()` for the "
            f"read path that does work today.")


# ─────────────────────────────────────────────────────────────────────────────
#  Maps handler — minimal (no data inject in v1)
# ─────────────────────────────────────────────────────────────────────────────
#
# Maps is a content app — the agent searches a real-world place
# index, gets routes, etc. There's no SQLite store we can inject
# arbitrary "places" into; favorites/recents live in opaque
# binary state files (`Library/Safari/...savedState/data.data`
# style) we don't reverse-engineer. v1 ships a stub handler that
# just registers Maps' bundle id and TCC needs so:
#
#   1. canonicalize_app("Maps") works for task generators
#   2. ensure_runner_permissions grants location TCC ahead of time
#   3. Multi-app tasks with apps=["Maps", ...] don't error on
#      dispatcher canonicalization
#
# Reset/apply are no-ops. When a task generator wants to seed
# Maps-side state (favorites, etc.), v2 of this handler will
# reverse-engineer the plist storage and add an apply primitive.

# ─────────────────────────────────────────────────────────────────────────────
#  Safari handler — Bookmarks.db (host-side SQLite)
# ─────────────────────────────────────────────────────────────────────────────
#
# Safari stores bookmarks in `data/Library/Safari/Bookmarks.db` at
# the system level (not inside the app sandbox). We have host-side
# filesystem access, so the handler shells out to sqlite3 via
# Python's stdlib — same transport pattern as SettingsHandler.
#
# Unlike Messages (multi-store, filtered), Safari is single-store:
# bookmarks injected into Bookmarks.db surface in the Safari UI on
# next launch. Verified empirically (2026-05-16). See
# `sibb/docs/IOS_SIM_QUIRKS.md` §11 for the Messages contrast.
#
# v1 scope: bookmark inject + read + wipe. History.db and
# SafariTabs.db deferred until first task needs them.
#
# Reset semantics: same as PhotosHandler — between-episode isolation
# comes from clone-from-baseline. v1 reset() is a no-op; if a future
# task needs within-episode reset, we'll track inserted IDs and
# DELETE them targetedly to avoid wiping the default bookmarks
# (Apple / Bing / Google / Yahoo) that ship with Safari.

import sqlite3 as _sqlite3


def _safari_bookmarks_db_path(udid: str) -> str:
    """Path to Safari's Bookmarks.db inside the simulator filesystem."""
    return os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}"
        f"/data/Library/Safari/Bookmarks.db"
    )


async def _safari_terminate(udid: str) -> None:
    """Terminate Safari to avoid concurrent-write contention with
    our SQL inject. Best-effort — returns silently on failure."""
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "terminate", udid, "com.apple.mobilesafari",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()


def _safari_clear_tab_state(udid: str) -> None:
    """Wipe Safari's tab/session restoration state so the next
    launch shows the Start Page instead of restoring whatever URL
    the previous run left in the active tab.

    Safari on iOS 18+ keeps tab state in `SafariTabs.db` and
    `BrowserState.db` (plus their `-shm`/`-wal` companions). Both
    are recreated empty on next launch when missing. Bookmarks
    live in a different file (`Bookmarks.db`) — explicitly NOT
    touched here so SafariHandler.apply(bookmark) state survives.

    Caller is responsible for terminating Safari first; deleting
    these files while Safari is running corrupts SQLite WAL state.
    """
    safari_dir = os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}"
        f"/data/Library/Safari")
    for name in ("SafariTabs.db", "SafariTabs.db-shm", "SafariTabs.db-wal",
                  "BrowserState.db", "BrowserState.db-shm",
                  "BrowserState.db-wal"):
        path = os.path.join(safari_dir, name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _safari_bookmarks_bar_parent_id(conn) -> Optional[int]:
    """The BookmarksBar folder ID — parent for top-level user
    bookmarks. Identified by special_id=1 (folder type).

    NOTE: in iOS Safari this folder is presented to the user under the
    label "Favorites" — `special_id=1` is the DB-level name, "Favorites"
    is the UI label."""
    row = conn.execute(
        "SELECT id FROM bookmarks WHERE special_id=1 LIMIT 1;"
    ).fetchone()
    return row[0] if row else None


def _safari_reading_list_parent_id(conn) -> Optional[int]:
    """The Reading List folder ID. Reading List entries live in the
    same `bookmarks` table under a separate special-id parent
    (typically special_id=4 on iOS). Returns None if the row isn't
    present yet (e.g. fresh sim that never opened Reading List)."""
    row = conn.execute(
        "SELECT id FROM bookmarks WHERE special_id=4 LIMIT 1;"
    ).fetchone()
    return row[0] if row else None


def _safari_bookmark_path(conn, row_id: int) -> List[str]:
    """Walk up from a bookmark row to the root, returning the folder
    titles encountered. Used to surface `folder_path` so verifiers can
    distinguish "in Favorites" vs "in Favorites > Recipes" vs root."""
    path: List[str] = []
    cur = row_id
    seen: Set[int] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = conn.execute(
            "SELECT parent, title, special_id FROM bookmarks WHERE id=?;",
            (cur,)).fetchone()
        if row is None:
            break
        parent, title, special_id = row
        # Root folder has parent=NULL; stop there. Don't include the
        # bookmark's own title — just folder ancestry.
        if cur != row_id:
            label = title or (f"<special:{special_id}>" if special_id else "")
            if label:
                path.append(label)
        if parent is None:
            break
        cur = parent
    return list(reversed(path))


async def _safari_insert_bookmark(
    udid: str, title: str, url: str, *,
    folder: Optional[str] = None,
) -> int:
    """Insert a bookmark under BookmarksBar (or under the named
    subfolder of BookmarksBar if `folder` is provided). The subfolder
    is created on demand if missing.

    Returns the new bookmark's row id. Caller is responsible for the
    DB existing (Safari has been launched at least once to create it).
    """
    await _safari_terminate(udid)
    db_path = _safari_bookmarks_db_path(udid)
    if not os.path.exists(db_path):
        raise RuntimeError(
            f"Bookmarks.db doesn't exist at {db_path}. Safari must "
            f"be launched at least once (during baseline prewarm) "
            f"before bookmark inject works.")
    conn = _sqlite3.connect(db_path, isolation_level=None)
    try:
        bookmarks_bar = _safari_bookmarks_bar_parent_id(conn)
        if bookmarks_bar is None:
            raise RuntimeError(
                "Bookmarks.db missing BookmarksBar folder "
                "(special_id=1) — schema may have changed")

        if folder:
            # Look up an existing subfolder with this title; create one
            # on demand if absent. Match case-sensitively against the
            # user-provided title (folder titles are user data).
            row = conn.execute(
                "SELECT id FROM bookmarks "
                "WHERE parent=? AND type=1 AND title=? AND deleted=0 "
                "LIMIT 1;",
                (bookmarks_bar, folder)).fetchone()
            if row is not None:
                parent = row[0]
            else:
                folder_idx = conn.execute(
                    "SELECT COALESCE(MAX(order_index), -1) + 1 "
                    "FROM bookmarks WHERE parent=?;",
                    (bookmarks_bar,)).fetchone()[0]
                folder_uuid = str(uuid.uuid4()).upper()
                conn.execute(
                    "INSERT INTO bookmarks "
                    "(parent, type, title, order_index, external_uuid, "
                    " editable, deletable, hidden, num_children, "
                    " added, deleted) "
                    "VALUES (?, 1, ?, ?, ?, 1, 1, 0, 0, 1, 0);",
                    (bookmarks_bar, folder, folder_idx, folder_uuid))
                parent = conn.execute(
                    "SELECT id FROM bookmarks WHERE external_uuid=?;",
                    (folder_uuid,)).fetchone()[0]
        else:
            parent = bookmarks_bar

        next_idx = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) + 1 FROM bookmarks "
            "WHERE parent=?;", (parent,)).fetchone()[0]
        new_uuid = str(uuid.uuid4()).upper()
        conn.execute(
            "INSERT INTO bookmarks "
            "(parent, type, title, url, order_index, external_uuid, "
            " editable, deletable, hidden, num_children, added, deleted) "
            "VALUES (?, 0, ?, ?, ?, ?, 1, 1, 0, 0, 1, 0);",
            (parent, title, url, next_idx, new_uuid))
        return conn.execute(
            "SELECT id FROM bookmarks WHERE external_uuid=?;",
            (new_uuid,)).fetchone()[0]
    finally:
        conn.close()


async def _safari_list_bookmarks(
    udid: str,
    parent_filter: Optional[str] = None,
    *,
    include_subfolders: bool = True,
    include_reading_list: bool = False,
) -> List[Dict[str, Any]]:
    """Read leaf bookmarks (type=0) from Safari's Bookmarks.db.

    By default returns every leaf bookmark anywhere under the
    BookmarksBar (= "Favorites" in UI), recursively walking subfolders.
    The 2026-06-01 fix replaced the prior parent=BookmarksBar-only
    query — bookmarks the user creates in a subfolder were previously
    invisible, which silently broke any mutation generator where iOS's
    "Add Bookmark" UI defaulted to a folder other than the BookmarksBar
    root.

    Args:
      parent_filter: if set, restrict results to bookmarks DIRECTLY
        under a folder with this title (case-insensitive). E.g.
        "Favorites" restricts to BookmarksBar root only.
      include_subfolders: if True (default), recursively include
        bookmarks in subfolders of the BookmarksBar tree.
      include_reading_list: if True, also include Reading List entries
        (special_id=4 parent). Reading List rows are tagged with
        `kind="reading_list"`; regular bookmarks with `kind="bookmark"`.

    Returns rows with: id, title, url, parent_id, parent_title,
    folder_path (list of folder titles from root → leaf, excluding the
    bookmark's own title), kind."""
    db_path = _safari_bookmarks_db_path(udid)
    if not os.path.exists(db_path):
        return []
    conn = _sqlite3.connect(db_path, isolation_level=None)
    try:
        bookmarks_bar = _safari_bookmarks_bar_parent_id(conn)
        if bookmarks_bar is None:
            return []

        # Collect all folder IDs under the BookmarksBar tree.
        if include_subfolders:
            tree_ids = {bookmarks_bar}
            frontier = [bookmarks_bar]
            while frontier:
                children = conn.execute(
                    "SELECT id FROM bookmarks "
                    "WHERE parent=? AND type=1 AND deleted=0;",
                    (frontier.pop(),)).fetchall()
                for (cid,) in children:
                    if cid not in tree_ids:
                        tree_ids.add(cid)
                        frontier.append(cid)
        else:
            tree_ids = {bookmarks_bar}

        # Optionally pull in Reading List as a parallel tree.
        rl_root = (_safari_reading_list_parent_id(conn)
                    if include_reading_list else None)
        rl_ids: Set[int] = set()
        if rl_root is not None:
            rl_ids.add(rl_root)
            frontier = [rl_root]
            while frontier:
                children = conn.execute(
                    "SELECT id FROM bookmarks "
                    "WHERE parent=? AND type=1 AND deleted=0;",
                    (frontier.pop(),)).fetchall()
                for (cid,) in children:
                    if cid not in rl_ids:
                        rl_ids.add(cid)
                        frontier.append(cid)

        # Pull leaves under any of the collected folders.
        all_ids = tree_ids | rl_ids
        if not all_ids:
            return []
        placeholders = ",".join("?" * len(all_ids))
        rows = conn.execute(
            f"SELECT id, title, url, parent FROM bookmarks "
            f"WHERE parent IN ({placeholders}) "
            f"AND type=0 AND deleted=0 "
            f"ORDER BY parent, order_index;",
            tuple(all_ids)).fetchall()

        # Build a parent_id → title cache for folder_path resolution.
        parent_titles: Dict[int, str] = {}
        for pid in all_ids:
            t = conn.execute(
                "SELECT title, special_id FROM bookmarks WHERE id=?;",
                (pid,)).fetchone()
            if t is None:
                continue
            title, special_id = t
            if title:
                parent_titles[pid] = title
            elif special_id == 1:
                parent_titles[pid] = "Favorites"
            elif special_id == 4:
                parent_titles[pid] = "Reading List"
            else:
                parent_titles[pid] = f"<special:{special_id}>" if special_id else ""

        results: List[Dict[str, Any]] = []
        pfilter_norm = (parent_filter or "").strip().lower()
        for r in rows:
            row_id, title, url, parent_id = r
            kind = "reading_list" if parent_id in rl_ids else "bookmark"
            parent_title = parent_titles.get(parent_id, "")
            path = _safari_bookmark_path(conn, row_id)
            if pfilter_norm:
                # Match against immediate parent OR any folder in path.
                hay = [parent_title.lower()] + [p.lower() for p in path]
                if pfilter_norm not in hay:
                    continue
            results.append({
                "id": row_id,
                "title": title or "",
                "url": url or "",
                "parent_id": parent_id,
                "parent_title": parent_title,
                "folder_path": path,
                "kind": kind,
            })
        return results
    finally:
        conn.close()


class SafariHandler:
    """Safari state setup: bookmark inject + mock-site fixtures.

    Two entry kinds:
      `bookmark`  — host-side SQL insert into `Bookmarks.db`.
                    Surfaces in the Safari start page.
      `mock_site` — spin up a host-side HTTP login fixture (see
                    `sibb_mock_site.MockSite`) and optionally
                    navigate Safari to its login URL. The fixture
                    is the verification surface for password-value
                    tasks (the keychain encrypts the password
                    column; we can only hash-match the username/
                    server attributes).
    """

    bundle_id: ClassVar[str] = "com.apple.mobilesafari"
    tcc_services: ClassVar[List[str]] = []
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader
        # MockSite fixtures spawned by this episode. Per-instance
        # ownership lets reset() stop them and clear the process-
        # global registry without leaks between episodes.
        self._mock_sites: List[Any] = []

    async def reset(self) -> None:
        # No mock-site spawns → keep v1's "don't touch bookmarks"
        # contract intact. With sites present, terminate Safari
        # first so keep-alive sockets close cleanly before we shut
        # the HTTP fixtures down.
        if not self._mock_sites:
            return
        udid = getattr(self.reader, "udid", None)
        if udid:
            await _safari_terminate(udid)
        while self._mock_sites:
            site = self._mock_sites.pop()
            try:
                site.stop()
            except Exception:
                # stop() is idempotent in practice; swallow so a
                # failing teardown doesn't poison subsequent
                # handler resets.
                pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind == "bookmark":
            await self._apply_bookmark(entry)
        elif kind == "mock_site":
            await self._apply_mock_site(entry)
        else:
            raise ValueError(
                f"SafariHandler: unknown entry type {kind!r}")

    async def _apply_bookmark(self, entry: Dict[str, Any]) -> None:
        if not getattr(self.reader, "udid", None):
            raise RuntimeError(
                "SafariHandler requires a reader with a .udid "
                "attribute — apply uses host-side simctl + sqlite3.")
        title = entry.get("title")
        url = entry.get("url")
        folder = entry.get("folder")
        if not title:
            raise ValueError("SafariHandler.bookmark: title required")
        if not url:
            raise ValueError("SafariHandler.bookmark: url required")
        await _safari_insert_bookmark(self.reader.udid, title, url,
                                        folder=folder)

    async def _apply_mock_site(self, entry: Dict[str, Any]) -> None:
        from sibb_mock_site import MockSite, open_in_safari

        site_id = entry.get("site_id")
        if not isinstance(site_id, str) or not site_id:
            raise ValueError(
                "SafariHandler.mock_site: site_id required (non-empty str)")

        credentials = entry.get("credentials") or {}
        if not isinstance(credentials, dict):
            raise ValueError(
                f"SafariHandler.mock_site.credentials must be a dict, "
                f"got {type(credentials).__name__}")
        for k, v in credentials.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(
                    "SafariHandler.mock_site.credentials: keys and "
                    "values must be str")

        site_kwargs: Dict[str, Any] = {
            "site_id": site_id,
            "credentials": credentials,
        }
        for opt in ("sign_in_path", "sign_up_path"):
            if entry.get(opt) is not None:
                site_kwargs[opt] = entry[opt]

        site = MockSite(**site_kwargs)
        site.start()
        # Track before navigating so an open_in_safari failure still
        # leaves the fixture visible to reset() for cleanup.
        self._mock_sites.append(site)

        if entry.get("open_at_start", True):
            udid = getattr(self.reader, "udid", None)
            if not udid:
                raise RuntimeError(
                    "SafariHandler.mock_site.open_at_start=True "
                    "requires a reader with a .udid attribute")
            # Kill any prior Safari before navigating; otherwise iOS
            # may surface a "Restore N tabs?" prompt that obscures
            # the login form on first observation.
            await _safari_terminate(udid)
            await asyncio.sleep(0.5)
            open_in_safari(udid, site.login_url)


# ─────────────────────────────────────────────────────────────────────────────
#  News handler — minimal (read-only via AX, no inject)
# ─────────────────────────────────────────────────────────────────────────────
#
# Capability assessment from 2026-05-16 investigation:
#
# What works:
#   - News on fresh baseline shows REAL cached headlines (Today
#     feed, with source attribution like "The Wall Street Journal").
#     Visible in the AX tree as `Other` elements with comma-
#     separated labels of the form `"<source>, <title>"`.
#   - Tabs (Today, News+, Audio, Following, Search) are navigable.
#   - Search bar accepts queries (lookup against Apple's index
#     may or may not return results depending on connectivity).
#
# What's unreliable:
#   - Tapping a feed cell to open article detail often fails with
#     "Cannot Connect / Retry" — could be paywall (News+), missing
#     iCloud sign-in, or network state. Some free articles may
#     work; many don't.
#
# What's blocked:
#   - Programmatically saving articles. The `reading-list` binary
#     plist has a 6-byte header wrapper (00 07 08 03 1a 2a) of
#     unknown semantics that prepends a standard bplist00. Even
#     if we wrote a wrapped plist, we'd need valid Apple News
#     article IDs (server-issued) — we have no way to mint them
#     on the simulator.
#   - "Save this article via UI" tasks because the article-detail
#     view is unreachable.
#
# Implication for the v1 handler: register the app, provide a
# news.headlines resource fetcher that scrapes the AX tree's
# Today-feed cells. No apply primitive (nothing to seed). No reset
# (no persistent state to wipe within an episode).
#
# Tasks that work with v1:
#   - "Find an article about <topic> in the Today feed" — verify
#     via AX tree text matching.
#   - "Open the News app and switch to the <X> tab" — UI navigation
#     verification.
#
# Tasks that need future work:
#   - "Save the article about X" — needs reading-list write or
#     UI article-detail path working.
#   - "Subscribe to <channel>" — needs Following + iCloud sync.

# ─────────────────────────────────────────────────────────────────────────────
#  Passwords handler — UI-driven; keychain SQL for verification
# ─────────────────────────────────────────────────────────────────────────────
#
# iOS 18+ Passwords.app (com.apple.Passwords) is a UI on top of
# the system Keychain. Investigation 2026-05-17 mapped the
# capability surface:
#
# What works:
#   - App launches on simulator (auto-unlocks; no Face ID flow).
#   - Categories: All / Passkeys / Codes / Wi-Fi (3 pre-seeded) /
#     Security / Deleted.
#   - "New Password" form has TextFields for Title, User Name,
#     Password, Website. Tap Save persists.
#   - Adding a password via UI creates rows in
#     `data/Library/Keychains/keychain-2-debug.db`:
#     - 3 new rows in `inet` table across access groups
#       `com.apple.cfnetwork`, `com.apple.password-manager`,
#       `com.apple.password-manager.password-evaluations`
#     - 7 new metadata rows in `genp` table under `apple` group
#
# What's blocked for direct programmatic write:
#   - Apple's `kSecAttrAccessGroup = "com.apple.password-manager"`
#     is reserved for system apps via entitlement. Our test runner
#     (`com.sibb.tests.xctrunner`) lacks that entitlement, so
#     `SecItemAdd` from the runner cannot create
#     Passwords-app-visible entries.
#   - Password values are AES-encrypted in the `data` BLOB column.
#     Decryption needs the system key derived from the device
#     passcode — we don't have that.
#
# Verification surface (programmatic):
#   - **Row count by access group**: query
#     `SELECT COUNT(*) FROM inet WHERE agrp='com.apple.password-manager'`
#     before vs after an action. Doesn't need decryption.
#   - **AX-tree titles** in Passwords-list view: after the agent
#     has navigated to the All / specific-category view, the
#     visible password cells expose the username (`sibbuser`)
#     and an auto-shortened display title (e.g. iOS rewrites
#     "sibb-test.example.com" → "Example"). Username is the more
#     stable identifier.
#
# v1 scope:
#   - Bundle registration + canonicalize.
#   - No apply primitive (writes are agent-UI-only).
#   - No reset in v1 (clone-from-baseline; documented gap).
#   - `passwords.entry_count` resource fetcher reads the
#     keychain DB via host-side sqlite to count entries by
#     access group.

import sqlite3 as _sqlite3_pw  # alias since SafariHandler already binds _sqlite3


def _keychain_db_path(udid: str) -> str:
    return os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}"
        f"/data/Library/Keychains/keychain-2-debug.db"
    )


def _passwords_entry_count(udid: str,
                            agrp: str = "com.apple.password-manager"
                            ) -> int:
    """Count `inet` rows in the keychain DB for a given access
    group. The Passwords app surfaces entries under
    `com.apple.password-manager`; pass other access groups to
    introspect specific keychain partitions.
    """
    db = _keychain_db_path(udid)
    if not os.path.exists(db):
        return 0
    conn = _sqlite3_pw.connect(db, timeout=2.0)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM inet WHERE agrp=?;",
            (agrp,)).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _passwords_entry_exists(
    udid: str,
    service: str,
    account: str,
    agrp: str = "com.apple.password-manager",
) -> bool:
    # acct/srvr are 20-byte SHA-1 hashes of the plaintext; see
    # IOS_SIM_QUIRKS.md §13 "Hash-equality verification" for the
    # empirical proof. tomb=0 excludes soft-deleted rows.
    import hashlib
    db = _keychain_db_path(udid)
    if not os.path.exists(db):
        return False
    s = hashlib.sha1(service.encode("utf-8")).digest()
    a = hashlib.sha1(account.encode("utf-8")).digest()
    conn = _sqlite3_pw.connect(db, timeout=2.0)
    try:
        row = conn.execute(
            "SELECT 1 FROM inet "
            "WHERE agrp=? AND srvr=? AND acct=? AND tomb=0 LIMIT 1;",
            (agrp, s, a)).fetchone()
        return row is not None
    finally:
        conn.close()


class PasswordsHandler:
    """Passwords state setup — UI-driven, keychain-readable for verify."""

    bundle_id: ClassVar[str] = "com.apple.Passwords"
    tcc_services: ClassVar[List[str]] = []
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # v1: no-op. Direct DELETE FROM inet would orphan the
        # encrypted blobs and confuse the Passwords app's state.
        # Clone-from-baseline handles between-episode isolation.
        pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        raise ValueError(
            f"PasswordsHandler: v1 has no apply primitive. "
            f"`com.apple.password-manager` access group is reserved "
            f"by Apple; SecItemAdd from the test runner cannot "
            f"create Passwords-app-visible entries. Agent must use "
            f"the New Password UI. Got entry type "
            f"{entry.get('type')!r}; nothing to do.")


class NewsHandler:
    """News state setup — minimal v1, AX-tree-based reads only."""

    bundle_id: ClassVar[str] = "com.apple.news"
    tcc_services: ClassVar[List[str]] = []
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # No SIBB-writable persistent News state in v1.
        # Clone-from-baseline gives a fresh feed snapshot each
        # episode.
        pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        raise ValueError(
            f"NewsHandler: v1 has no apply primitive — feed content "
            f"is server-curated and reading-list writes need an "
            f"unwrapped binary-plist format we haven't reversed. "
            f"Got entry type {entry.get('type')!r}; nothing to do.")


class MessagesHandler:
    """Messages pre-runner: send a marker iMessage in a phantom thread
    so the simulator's no-account IDS loopback echoes the text back as
    a gray-bubble INBOUND on the other phantom thread.

    Pre-runner contract:
        spec entry  {"app": "Messages", "type": "send_in_thread",
                     "thread": "JA"|"KB", "text": "..."}
        thread="JA" (default) → send to the 888-prefixed cell;
                                loopback appears as inbound in KB.
        thread="KB"           → send to the 555-prefixed cell;
                                loopback appears as inbound in JA.

    Hard constraint — see IOS_SIM_QUIRKS.md §11: NEVER call
    `simctl terminate com.apple.MobileSMS` between this apply() and
    the verifier. The loopback bubble lives in iMessage's in-memory
    state (no `sms.db` row). The single terminate inside _reset_app()
    happens BEFORE the send — that's the only one allowed.

    The send logic mirrors `sibb/simulator/sibb_probe_messages_lifetime.py`
    (the regression probe). If iOS later changes Messages' AX surface,
    update both in lock-step.
    """

    bundle_id: ClassVar[str] = "com.apple.MobileSMS"
    tcc_services: ClassVar[List[str]] = []
    # NOT a `pre_runner` handler. The Messages send needs the live
    # XCUITest socket (UI driving) — it runs via the standard async
    # `apply()` path, AFTER the sim boots and the runner attaches.
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    # Phantom inbox-cell discriminators on iOS 26.3 sim. JA = Kate Bell's
    # +1 (888) 555-1212; KB = Anna Haro's +1 (555) 564-8583. These are
    # contact-suggestion rows, NOT real conversations — see §11.
    #
    # Use the area-code-in-parens form because "555" substring matches
    # BOTH numbers (it appears as the second triplet in the JA number
    # "+1 (888) 555-1212"). "(888)" appears only in JA; "(555)" appears
    # only in KB (the JA number doesn't have "(555)" — its parens
    # contain 888).
    _CELL_TAG = {"JA": "(888)", "KB": "(555)"}

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # No persistent state to wipe — clone-from-baseline gives a
        # fresh sms.db each episode. The bubble is in-memory.
        pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind == "send_in_thread":
            await self._send_in_thread(entry)
        else:
            raise ValueError(
                f"MessagesHandler: unknown entry type {kind!r}")

    async def _send_in_thread(self, entry: Dict[str, Any]) -> None:
        thread = entry.get("thread", "JA")
        # Two spec shapes accepted:
        #   text:  "single bubble string"           (legacy single-bubble)
        #   texts: ["bubble 1", "bubble 2", ...]    (multi-bubble — N
        #                                             separate iMessages,
        #                                             each appears as its
        #                                             own inbound bubble
        #                                             in the OPPOSITE
        #                                             thread via IDS
        #                                             loopback)
        # Multi-bubble mirrors real-world chat where someone types
        # fragments rather than a paragraph. The agent has to read
        # multiple bubbles to reconstruct the full message.
        texts: List[str] = []
        if "texts" in entry and entry["texts"] is not None:
            texts = [str(t) for t in entry["texts"] if t]
        elif entry.get("text"):
            texts = [str(entry["text"])]
        if not texts:
            raise ValueError(
                "MessagesHandler.send_in_thread: `text` (str) or "
                "`texts` (list[str]) required")
        if thread not in self._CELL_TAG:
            raise ValueError(
                f"MessagesHandler.send_in_thread: thread must be 'JA' "
                f"or 'KB', got {thread!r}")
        udid = getattr(self.reader, "udid", None)
        if not udid:
            raise RuntimeError(
                "MessagesHandler requires a reader with a .udid attr")

        await self._reset_messages_app(udid)
        await self._dismiss_popups()
        await self._navigate_back_to_inbox()
        if not await self._open_thread(thread):
            raise RuntimeError(
                f"MessagesHandler: couldn't open {thread} thread cell")
        # Send each bubble in turn. Short wait between sends lets iOS'
        # send animation finish AND ensures the loopback IDS echo
        # arrives in order on the receiving thread.
        for i, t in enumerate(texts):
            await self._send_in_compose(t)
            if i < len(texts) - 1:
                await asyncio.sleep(0.6)
        # Post-send cleanup so the agent encounters a clean,
        # realistic-looking inbox:
        #   1. Navigate back to inbox.
        #   2. DELETE the thread we just sent to (swipe-left → tap
        #      Delete). The loopback inbound bubble in the OPPOSITE
        #      thread (where the agent must read from) is preserved
        #      because it's stored separately by IDS.
        #   3. PRESS home so the agent starts on the home screen
        #      rather than inside Messages — they must find their
        #      way to Messages.app themselves.
        # Mirrors real-world UX: the user receives a message,
        # background apps to springboard, then opens Messages later.
        await self._navigate_back_to_inbox()
        await asyncio.sleep(0.5)
        try:
            await self._delete_thread(thread)
        except Exception as exc:
            import sys as _sys
            print(f"MessagesHandler: post-send delete of {thread!r} "
                  f"thread failed ({type(exc).__name__}: {exc}); "
                  f"agent will see both inbox cells", file=_sys.stderr)
        # Return to springboard.
        await self.reader.press("home")
        await asyncio.sleep(0.8)

    async def _delete_thread(self, thread: str) -> None:
        """Swipe-left on the named inbox cell, then tap the Delete
        button that appears. iOS Messages presents a Delete button
        (and on some iOS versions a confirmation sheet — handled
        by retrying the Delete tap if one appears)."""
        tag = self._CELL_TAG[thread]
        els = await self._observe_raw()
        cell = None
        for e in els:
            if e.get("role") == "cell" and tag in (e.get("label") or ""):
                cell = e
                break
        if cell is None:
            raise RuntimeError(
                f"_delete_thread: no inbox cell containing {tag!r}")
        fr = cell.get("frame") or {}
        y = fr.get("y", 0) + fr.get("height", 0) / 2
        # Swipe-left across the cell.
        await self.reader.swipe_at(
            x1=fr.get("x", 0) + fr.get("width", 0) - 30, y1=y,
            x2=fr.get("x", 0) + 30, y2=y,
            duration_s=0.15, settle=False)
        await asyncio.sleep(0.7)
        # Tap Delete (may need 1 or 2 taps if a confirmation sheet
        # appears).
        for _attempt in range(2):
            els = await self._observe_raw()
            delete_btn = None
            for e in els:
                if (e.get("role") == "btn"
                        and (e.get("label") or "").strip().lower()
                        == "delete"):
                    delete_btn = e
                    break
            if delete_btn is None:
                break
            await self._tap_element(delete_btn)
            await asyncio.sleep(0.5)

    async def _reset_messages_app(self, udid: str) -> None:
        """The ONLY allowed terminate. Wipes the in-memory bubble from
        any prior episode + clears stale UI state, then relaunches via
        the XCUITest socket (which also re-attaches the server to
        Messages so subsequent observes return Messages' AX tree)."""
        import subprocess as _sp
        _sp.run(["xcrun", "simctl", "terminate", udid, self.bundle_id],
                capture_output=True, timeout=15)
        await asyncio.sleep(1.5)
        # Launch via XCUITest, not subprocess — the server's attached-
        # app state otherwise stays stuck on the previous bundle and
        # `_send({"type": "observe"})` returns that app's tree instead
        # of Messages.
        await self.reader.launch(bundle_id=self.bundle_id)
        await asyncio.sleep(2.5)

    async def _observe_raw(self) -> List[Dict[str, Any]]:
        # Explicit bundleId so we ALWAYS read Messages' tree, never
        # whatever the server was last attached to. Belt-and-suspenders
        # with _reset_messages_app's XCUITest-side launch.
        raw = await self.reader._send({
            "type": "observe", "bundleId": self.bundle_id,
        })
        return raw.get("elements") or []

    async def _tap_element(self, el: Dict[str, Any]) -> None:
        fr = el.get("frame") or {}
        cx = fr.get("x", 0) + fr.get("width", 0) / 2
        cy = fr.get("y", 0) + fr.get("height", 0) / 2
        await self.reader.tap(x=cx, y=cy)

    async def _dismiss_popups(self) -> None:
        """Click through any TCC / welcome / continue dialogs."""
        for _ in range(3):
            els = await self._observe_raw()
            dismissed = False
            for e in els:
                lbl = (e.get("label") or "").strip().lower()
                if (e.get("role") == "btn"
                        and lbl in ("continue", "not now", "ok",
                                     "skip", "done")):
                    await self._tap_element(e)
                    await asyncio.sleep(0.7)
                    dismissed = True
                    break
            if not dismissed:
                return

    async def _navigate_back_to_inbox(self) -> bool:
        for _ in range(3):
            els = await self._observe_raw()
            if self._view_kind(els) == "inbox":
                return True
            back = None
            for e in els:
                if (e.get("role") == "btn"
                        and (e.get("label") or "") in ("Messages", "Back")):
                    back = e
                    break
            if not back:
                return False
            await self._tap_element(back)
            await asyncio.sleep(0.6)
        return False

    @staticmethod
    def _view_kind(els: List[Dict[str, Any]]) -> str:
        has_compose = any(
            e.get("role") == "input"
            and (e.get("label") or "") == "Message" for e in els)
        has_imessage_text = any(
            (e.get("label") or "") == "iMessage"
            and e.get("role") == "text" for e in els)
        has_conversations = any(
            (e.get("label") or "") == "Conversations"
            and e.get("role") == "collection" for e in els)
        has_create_contact = any(
            (e.get("label") or "") == "Create New Contact" for e in els)
        if has_create_contact and not has_compose:
            return "contact_details"
        if has_compose or has_imessage_text:
            return "thread"
        if has_conversations:
            return "inbox"
        return "other"

    async def _open_thread(self, thread: str) -> bool:
        tag = self._CELL_TAG[thread]
        els = await self._observe_raw()
        for e in els:
            if e.get("role") == "cell" and tag in (e.get("label") or ""):
                await self._tap_element(e)
                await asyncio.sleep(1.0)
                return True
        return False

    async def _send_in_compose(self, text: str) -> None:
        els = await self._observe_raw()
        compose = None
        for e in els:
            if (e.get("role") == "input"
                    and (e.get("label") or "") == "Message"):
                compose = e
                break
        if not compose:
            raise RuntimeError(
                "MessagesHandler: no compose input found in thread view")
        await self._tap_element(compose)
        await asyncio.sleep(0.7)
        await self.reader.type_text(text)
        await asyncio.sleep(0.5)
        # SendButton appears after typing. iOS 26 sim emits role='btn'
        # label='SendButton' or 'Send'; fall back to the unlabeled
        # send-arrow near (367, 499) on the compose row.
        els = await self._observe_raw()
        send_btn = None
        for e in els:
            if (e.get("role") == "btn"
                    and (e.get("label") or "") in ("SendButton", "Send")):
                send_btn = e
                break
        if not send_btn:
            best = None
            best_d = 1e9
            for e in els:
                if e.get("role") == "btn" and not (e.get("label") or ""):
                    fr = e.get("frame") or {}
                    cx = fr.get("x", 0) + fr.get("width", 0) / 2
                    cy = fr.get("y", 0) + fr.get("height", 0) / 2
                    if cx < 300 or cy < 400 or cy > 600:
                        continue
                    d = (cx - 367) ** 2 + (cy - 499) ** 2
                    if d < best_d:
                        best_d = d
                        best = e
            send_btn = best
        if not send_btn:
            raise RuntimeError(
                "MessagesHandler: no Send button found after typing")
        await self._tap_element(send_btn)
        await asyncio.sleep(0.8)


class MapsHandler:
    """Maps state setup — minimal v1, no data inject yet."""

    bundle_id: ClassVar[str] = "com.apple.Maps"
    tcc_services: ClassVar[List[str]] = ["location"]
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # No persistent Maps state to wipe in v1. Clone-from-baseline
        # guarantees a fresh disk image per episode.
        pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        raise ValueError(
            f"MapsHandler: v1 has no apply primitive — Maps content "
            f"is the real-world place index, not a seedable store. "
            f"Got entry type {entry.get('type')!r}; nothing to do.")


# ─────────────────────────────────────────────────────────────────────────────
#  Springboard handler — wraps the existing sibb_randomize_layout.py
# ─────────────────────────────────────────────────────────────────────────────

class SpringboardHandler:
    """
    SpringBoard state: home-screen layout, dock contents, starting page.

    `layout` and `dock` entries edit the IconState.plist while the sim
    is SHUT DOWN — they cannot be applied through a running XCUITest
    socket. The runner shuts down the sim, runs the script, and boots
    the sim back up.

    `start_page` is post-boot navigation: from page 0 (where SpringBoard
    boots) the runner swipes left N times to land on page N. This one
    uses the reader (sim must be up).

    `reader` is optional: only needed for start_page.
    """

    bundle_id: ClassVar[str] = "com.apple.springboard"
    tcc_services: ClassVar[List[str]] = []
    pre_runner: ClassVar[bool] = True
    pre_runner_kinds: ClassVar[List[str]] = ["layout", "dock"]
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # Restoring the IconState plist to a known baseline is a Phase 3
        # concern; we'd need to checkpoint a clean copy at prewarm time
        # and copy it back here. For now, layout changes persist across
        # episodes (the seed determines the layout, so a re-run with the
        # same seed reproduces the same state).
        pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind == "start_page":
            await self._apply_start_page(entry)
        else:
            # layout/dock are pre_runner=True entries — they should have
            # been routed to apply_pre_runner_setup before the reader
            # even started. Reaching here means the dispatcher missed.
            raise RuntimeError(
                f"SpringboardHandler.{kind!r} is a pre-runner entry; "
                "should have been applied via apply_pre_runner_setup "
                "before reader.start().")

    def apply_pre_runner(self, udid: str, entry: Dict[str, Any]) -> None:
        """Sync path used by `apply_pre_runner_setup` while the sim is shut down."""
        kind = entry.get("type")
        if kind == "layout":
            self._apply_layout(udid, entry)
        elif kind == "dock":
            self._apply_dock(udid, entry)
        else:
            raise ValueError(
                f"SpringboardHandler.apply_pre_runner: unknown entry type {kind!r}")

    def _apply_layout(self, udid: str, entry: Dict[str, Any]) -> None:
        self._run_script(udid, [
            "--seed", str(entry.get("seed", 0)),
            *(["--cross-page"] if entry.get("cross_page") else []),
            *(["--distribute"] if entry.get("distribute") else []),
            *(["--n-pages", str(entry["n_pages"])] if "n_pages" in entry else []),
        ])

    def _apply_dock(self, udid: str, entry: Dict[str, Any]) -> None:
        self._run_script(udid, [
            "--seed", str(entry.get("seed", 0)),
            "--randomize-dock",
            *(["--dock-count", str(entry["count"])] if "count" in entry else []),
        ])

    def _run_script(self, udid: str, extra_args: list) -> None:
        """
        Run sibb_randomize_layout.py. The script expects the sim to be
        shut down so iOS doesn't overwrite our plist edits on its next
        save. Caller is responsible for sim lifecycle (shutdown before,
        boot after).
        """
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "simulator", "sibb_randomize_layout.py",
        )
        if not os.path.exists(script):
            raise RuntimeError(f"randomize-layout script not found at {script}")
        cmd = ["/Library/Developer/CommandLineTools/usr/bin/python3", script,
               udid] + extra_args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"randomize_layout failed: {result.stderr or result.stdout}")

    async def _apply_start_page(self, entry: Dict[str, Any]) -> None:
        """
        Navigate to a specific home-screen page by swiping.

        SpringBoard boots showing page 0. To reach page N (0-indexed),
        we send N left-swipes (swipe-left moves forward to the next
        page; swipe-right moves back). If `page < 0` we no-op.
        """
        page = int(entry.get("page", 0))
        if page <= 0 or not self.reader:
            return
        # Ensure we're on SpringBoard. Don't activate — just swipe.
        for _ in range(page):
            await self.reader._send({"type": "swipe", "direction": "left"})
            await asyncio.sleep(0.25)   # small gap between swipes


# ─────────────────────────────────────────────────────────────────────────────
#  Shortcuts handler — run-by-name via URL scheme + AX-read library
# ─────────────────────────────────────────────────────────────────────────────
#
# v1 capabilities:
#   - apply(type="run", name, input) — invoke a Library shortcut by
#     name via `simctl openurl shortcuts://run-shortcut?...`. The URL
#     scheme accepts ONE input slot (the `text` query param), which
#     becomes the "Shortcut Input" magic variable inside the shortcut.
#     For multi-parameter shortcuts, pass a `dict` — we JSON-encode it
#     and the shortcut uses `Get Dictionary from Input` to parse.
#
# v1 NON-capabilities (Apple constraints, not ours):
#   - No create / edit / delete. Apple has no public API for these;
#     the Shortcuts Core Data store is opaque, located in a Group
#     container we haven't reverse-engineered. See TODO_DEFERRED §G1
#     for the full design rationale.
#   - No run-by-name for trigger-based Automations. URL scheme is
#     name-addressable for Library shortcuts only. Automations need
#     UI drive: Automation tab → tap → "Run Immediately".
#   - No success/failure feedback from openurl itself. iOS silently
#     no-ops if the name doesn't match a Library shortcut. Verify via
#     side effect (the shortcut's actions write to Reminders/Files/
#     etc. — read those back).
#
# AX-read library: the `shortcuts.installed` resource fetcher
# launches Shortcuts, attaches an AXReader, parses Cells whose labels
# match `<name>, <N> action[s]`. The action-count suffix is what
# distinguishes user shortcuts from Apple's app-grouped suggestion
# cells (Scan Document, Recents, Places — those lack the annotation).


def _build_run_shortcut_url(
    name: str, input_value: Any = None,
) -> str:
    """Build a `shortcuts://run-shortcut?...` URL.

    Encoding rules:
      - `name` is URL-encoded via `quote_plus` (spaces → `+`, special
        chars %-escaped). Must match the shortcut's display name
        verbatim (case-sensitive on iOS).
      - `input_value=None` produces `?name=X` with no input clause.
      - `str` is passed as `&input=text&text=<encoded>`.
      - `dict` is JSON-encoded with sorted keys for stability, then
        passed as `&input=text&text=<encoded>`. Recipient shortcut
        uses `Get Dictionary from Input`.
      - Anything else raises ValueError.
    """
    import json
    import urllib.parse

    if not isinstance(name, str) or not name:
        raise ValueError(
            "_build_run_shortcut_url: name must be a non-empty string")
    url = "shortcuts://run-shortcut?name=" + urllib.parse.quote_plus(name)
    if input_value is None:
        return url
    if isinstance(input_value, dict):
        payload = json.dumps(input_value, sort_keys=True)
    elif isinstance(input_value, str):
        payload = input_value
    else:
        raise ValueError(
            f"_build_run_shortcut_url: input must be str, dict, or None; "
            f"got {type(input_value).__name__}")
    url += "&input=text&text=" + urllib.parse.quote_plus(payload)
    return url


async def _shortcuts_openurl(
    udid: str, url: str, *, timeout: float = 10.0,
) -> None:
    """Drive `xcrun simctl openurl <udid> <url>` and surface stderr
    on failure. Returns when openurl exits (the shortcut may still
    be running asynchronously on the sim — verify via side effect)."""
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "openurl", udid, url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"simctl openurl timed out after {timeout}s: {url}")
    if proc.returncode != 0:
        stderr = (err or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"simctl openurl failed (exit {proc.returncode}): "
            f"{stderr or '<no stderr>'}")


# Cell-label regex for the iOS 26 Library tab. User shortcuts render
# as `<name>, <N> action[s]`; Apple's app-grouped suggestions don't
# get this annotation, so the regex doubles as a discriminator.
import re as _re_shortcuts  # noqa: E402

_SHORTCUTS_LIBRARY_CELL_RE = _re_shortcuts.compile(
    r"^(?P<name>.+?), (?P<count>\d+) actions?$")


def _parse_shortcuts_library_tree(elements) -> List[Dict[str, Any]]:
    """Pure function: scan an AX element list (from the Shortcuts
    Library tab) and return one row per user-installed shortcut.

    Each row: `{"name": str, "action_count": int}`. Order follows
    the AX tree order (typically newest first in iOS 26 Library).
    Apple's app-grouped suggestion cells (Scan Document, Recents,
    Places, …) lack the `, N action[s]` annotation and are skipped.
    """
    # Local import so this module imports fine on machines without
    # the simulator scaffold installed (e.g. unit-test CI).
    from sibb_scaffold import ElementRole as _ElementRole

    rows: List[Dict[str, Any]] = []
    for e in elements:
        if e.effective_role != _ElementRole.CELL:
            continue
        label = (e.effective_label or "").strip()
        m = _SHORTCUTS_LIBRARY_CELL_RE.match(label)
        if m:
            rows.append({
                "name": m.group("name"),
                "action_count": int(m.group("count")),
            })
    return rows


class ShortcutsHandler:
    """Shortcuts state setup — run-by-name + AX-read library.

    v1 scope: `apply(type="run", name, input)` invokes a Library
    shortcut via the `shortcuts://run-shortcut` URL scheme. The
    `shortcuts.installed` resource fetcher reads the Library tab via
    AX. No create / edit / delete — Apple doesn't expose APIs for
    those (TODO_DEFERRED §G1).

    Side-effect verification is the primary path: the shortcut's
    actions write to Reminders / Files / etc.; read those back via
    the target app's resource fetcher.
    """

    bundle_id: ClassVar[str] = "com.apple.shortcuts"
    tcc_services: ClassVar[List[str]] = []
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []

    def __init__(self, reader: Optional[Any] = None):
        self.reader = reader

    async def reset(self) -> None:
        # Clone-from-baseline handles isolation. No per-episode
        # shortcut wipe (we can't programmatically delete a shortcut
        # anyway — Core Data store is opaque).
        pass

    async def apply(self, entry: Dict[str, Any]) -> None:
        kind = entry.get("type")
        if kind == "run":
            await self._apply_run(entry)
        elif kind in ("create", "edit", "delete"):
            raise ValueError(
                f"ShortcutsHandler: type={kind!r} not supported in v1. "
                f"Apple has no public API to create/edit/delete user "
                f"shortcuts — the Core Data store is opaque (see "
                f"TODO_DEFERRED §G1). Build shortcuts manually in the "
                f"Shortcuts UI during episode setup if pre-seeded ones "
                f"are needed.")
        elif kind == "run_automation":
            raise ValueError(
                f"ShortcutsHandler: type='run_automation' not supported "
                f"in v1. URL scheme is name-addressable for Library "
                f"shortcuts only. Automations need UI drive: open the "
                f"Automation tab → tap the automation → 'Run Immediately'.")
        else:
            raise ValueError(
                f"ShortcutsHandler: unknown entry type {kind!r}. "
                f"Supported: 'run'.")

    async def _apply_run(self, entry: Dict[str, Any]) -> None:
        udid = getattr(self.reader, "udid", None)
        if not udid:
            raise RuntimeError(
                "ShortcutsHandler.run requires a reader with a .udid "
                "attribute — apply uses host-side `simctl openurl`.")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(
                "ShortcutsHandler.run: `name` required (non-empty str)")
        url = _build_run_shortcut_url(name, entry.get("input"))
        await _shortcuts_openurl(udid, url)


# ─────────────────────────────────────────────────────────────────────────────
#  Registry + dispatcher
# ─────────────────────────────────────────────────────────────────────────────

# Keyed by bundle id — the unambiguous Apple identifier. Friendly names
# from generators ("Reminders", "Springboard", and the historical typo
# "SpringBoard") are resolved to bundle ids via `canonicalize_app()`
# below, so the dispatcher never depends on a particular casing or
# spelling of the friendly name.
HANDLERS: Dict[str, type] = {
    RemindersHandler.bundle_id:    RemindersHandler,
    CalendarHandler.bundle_id:     CalendarHandler,
    ContactsHandler.bundle_id:     ContactsHandler,
    FilesHandler.bundle_id:        FilesHandler,
    SettingsHandler.bundle_id:     SettingsHandler,
    PhotosHandler.bundle_id:       PhotosHandler,
    HealthHandler.bundle_id:       HealthHandler,
    FitnessHandler.bundle_id:      FitnessHandler,
    SafariHandler.bundle_id:       SafariHandler,
    MessagesHandler.bundle_id:     MessagesHandler,
    MapsHandler.bundle_id:         MapsHandler,
    NewsHandler.bundle_id:         NewsHandler,
    PasswordsHandler.bundle_id:    PasswordsHandler,
    ShortcutsHandler.bundle_id:    ShortcutsHandler,
    SpringboardHandler.bundle_id:  SpringboardHandler,
}


def _build_app_aliases() -> Dict[str, str]:
    """Map lowercased-friendly-name and lowercased-bundle-id -> bundle id.

    Friendly name is derived from the class name (`RemindersHandler` ->
    `Reminders`). Bundle id is taken from the handler's `bundle_id`
    attribute. Both sources contribute to the alias table; lookup is
    case-insensitive via `.lower()` on the input.
    """
    aliases: Dict[str, str] = {}
    for cls in HANDLERS.values():
        bid = cls.bundle_id
        aliases[bid.lower()] = bid
        cls_name = cls.__name__
        if cls_name.endswith("Handler"):
            friendly = cls_name[: -len("Handler")]
            aliases[friendly.lower()] = bid
    return aliases


_APP_ALIASES: Dict[str, str] = _build_app_aliases()


def canonicalize_app(name: Optional[str]) -> Optional[str]:
    """Resolve a friendly name (any case) or bundle id to the bundle id.

    Returns `None` if the name doesn't match any registered handler.
    This is the single seam through which generator-emitted casing
    drift ("SpringBoard" vs "Springboard") becomes harmless.
    """
    if not isinstance(name, str) or not name:
        return None
    return _APP_ALIASES.get(name.lower())


def _topo_sort_apps(
    bundle_ids,
    deps_fn=None,
) -> List[str]:
    """Topologically sort a set of bundle ids by handler `depends_on`.

    `deps_fn(bid) -> List[str]` returns the bundle ids that `bid`
    depends on. The default reads each registered handler's
    `depends_on` and canonicalizes entries via `canonicalize_app`.
    Tests pass a custom `deps_fn` to exercise synthetic graphs
    without mutating `HANDLERS`.

    Dependencies referencing apps outside `bundle_ids` are ignored —
    a task that touches only A doesn't pull in A's deps for reset.

    Returns a stable order: among nodes that are simultaneously
    available (no remaining incoming edges), the alphabetically
    first wins. A cycle raises `ValueError` naming the participants.
    """
    if deps_fn is None:
        def deps_fn(bid: str) -> List[str]:
            cls = HANDLERS.get(bid)
            if cls is None:
                return []
            out: List[str] = []
            for dep_raw in cls.depends_on:
                dep_bid = canonicalize_app(dep_raw)
                if dep_bid is not None:
                    out.append(dep_bid)
            return out

    from bisect import insort

    nodes = set(bundle_ids)
    in_degree: Dict[str, int] = {n: 0 for n in nodes}
    children: Dict[str, List[str]] = {n: [] for n in nodes}
    for n in nodes:
        for dep in deps_fn(n):
            if dep in nodes:
                in_degree[n] += 1
                children[dep].append(n)

    ready: List[str] = sorted(n for n, d in in_degree.items() if d == 0)
    result: List[str] = []
    while ready:
        n = ready.pop(0)
        result.append(n)
        for child in children[n]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                insort(ready, child)

    if len(result) != len(nodes):
        remaining = sorted(nodes - set(result))
        raise ValueError(
            f"depends_on cycle among {remaining!r}; "
            "check each handler's `depends_on` for a cycle."
        )
    return result


def is_pre_runner_entry(entry: Dict[str, Any]) -> bool:
    """True iff this spec entry must be applied while the sim is shut down.

    Replaces the external `PRE_RUNNER_KINDS` denylist: each handler
    owns its `pre_runner_kinds` declaration, and the dispatcher asks
    the handler instead of consulting a module-level set.
    """
    bid = canonicalize_app(entry.get("app"))
    cls = HANDLERS.get(bid) if bid is not None else None
    if cls is None:
        return False
    return entry.get("type") in cls.pre_runner_kinds


def collect_tcc_services() -> List[str]:
    """Sorted union of every registered handler's `tcc_services`.

    Consumed by `sibb_xcuitest_client.ensure_runner_permissions` to
    drive `simctl privacy grant` BEFORE xcodebuild launches the
    runner. The simulator layer imports this lazily so we don't
    invert the natural benchmark → simulator dependency.
    """
    services = set()
    for cls in HANDLERS.values():
        services.update(cls.tcc_services)
    return sorted(services)


def _device_state(udid: str) -> str:
    """Return the trailing state string from `simctl list devices <udid>`.
    Values include "Booted", "Shutdown", "Shutting Down", "Booting".
    Empty string if the device can't be found or simctl errors."""
    try:
        r = subprocess.run(["xcrun", "simctl", "list", "devices", udid],
                            capture_output=True, text=True, timeout=5)
    except subprocess.SubprocessError:
        return ""
    for line in r.stdout.splitlines():
        if udid in line:
            # Match the LAST parenthesized state token on the line.
            # "(Shutting Down)" comes after "(<UDID>)" so we need the last.
            tail = line.rsplit(")", 1)[0]
            if "(" in tail:
                return tail.rsplit("(", 1)[1]
    return ""


def _wait_for_state(udid: str, target: str, timeout: float = 30.0) -> bool:
    """Poll device state until it matches `target` (e.g. "Shutdown" or
    "Booted") or the timeout elapses. Returns True iff target reached."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _device_state(udid) == target:
            return True
        time.sleep(0.5)
    return False


def _kill_orphan_launchd_sim(udid: str) -> int:
    """SIGKILL any orphan `launchd_sim` for this device. Returns count killed.

    CoreSimulator periodically loses track of `launchd_sim` during a
    shutdown; the device sticks in "Shutting Down" forever. Killing the
    orphan + restarting CoreSimulatorService clears the tracker.
    """
    killed = 0
    try:
        ps = subprocess.run(["pgrep", "-f", f"launchd_sim.*{udid}"],
                             capture_output=True, text=True, timeout=5)
        for pid in ps.stdout.split():
            try:
                subprocess.run(["kill", "-KILL", pid], timeout=5)
                killed += 1
            except subprocess.SubprocessError:
                pass
    except subprocess.SubprocessError:
        pass
    return killed


def _kickstart_coresimulatorservice() -> None:
    """Restart the user-domain CoreSimulatorService (no sudo needed)."""
    try:
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "kickstart", "-k",
             f"user/{uid}/com.apple.CoreSimulator.CoreSimulatorService"],
            capture_output=True, timeout=15)
    except subprocess.SubprocessError:
        pass


def _shutdown_sim(udid: str, quiesce: float = 2.0,
                  *, _retry: bool = True) -> None:
    """Bring a sim to a quiet, fully-stopped state ready for plist edits.

    The historical implementation did `simctl shutdown` + `sleep 2`, which:
      (a) raced SpringBoard's persist-on-shutdown writes, so IconState.plist
          edits could be silently clobbered, and
      (b) hung for the full 30 s timeout when CoreSimulator orphaned the
          `launchd_sim` (the "Shutting Down" stuck state).

    Fix:
      • Tell simctl to shutdown (10 s timeout).
      • Poll device state until "Shutdown" (not "Shutting Down").
      • Sleep `quiesce` seconds to let SpringBoard finish writing.
      • If simctl times out OR polling never reaches "Shutdown":
          - SIGKILL any orphan launchd_sim for this device
          - kickstart user-domain CoreSimulatorService
          - retry once
    """
    if _device_state(udid) == "Shutdown":
        # Already there — just quiesce so any in-flight plist writes
        # finish before the caller writes IconState.plist.
        time.sleep(quiesce)
        return

    try:
        subprocess.run(["xcrun", "simctl", "shutdown", udid],
                        capture_output=True, timeout=10)
    except subprocess.TimeoutExpired:
        if not _retry:
            raise
        # Recover & retry. Don't return here — fall through to the
        # post-shutdown wait so the retry path also confirms Shutdown.

    # Poll for Shutdown. If we never get there, attempt recovery.
    if not _wait_for_state(udid, "Shutdown", timeout=20.0):
        if not _retry:
            raise RuntimeError(
                f"sim {udid} did not reach 'Shutdown' state "
                f"(currently {_device_state(udid)!r})")
        _kill_orphan_launchd_sim(udid)
        _kickstart_coresimulatorservice()
        # Service restart momentarily blocks simctl; wait for it to
        # answer queries again before retrying.
        retry_deadline = time.time() + 10.0
        while time.time() < retry_deadline:
            if _device_state(udid) in ("Shutdown", "Booted",
                                         "Shutting Down", "Booting"):
                break
            time.sleep(0.5)
        _shutdown_sim(udid, quiesce=quiesce, _retry=False)
        return

    time.sleep(quiesce)


def _boot_sim(udid: str, timeout: float = 120.0) -> None:
    """Boot a sim and wait for ALL critical daemons to be ready.

    `simctl boot` returns as soon as launchd starts, but the sim's
    `testmanagerd` (which xcodebuild talks to) and SpringBoard need
    longer. The historical implementation polled for the "Booted"
    state string + 2 s sleep — empirically not enough after a fresh
    layout plist edit, leading to xcodebuild hangs with no log output.

    Use `simctl bootstatus -b` which blocks until the device emits the
    boot-complete event. Falls back to the legacy poll+sleep if the
    bootstatus subcommand isn't available (older Xcode).
    """
    subprocess.run(["xcrun", "simctl", "boot", udid],
                    capture_output=True, timeout=30)
    try:
        # -b: wait for BootCompleteEvent (full daemon startup).
        # -d: print device-ready time to stdout (we ignore it).
        subprocess.run(["xcrun", "simctl", "bootstatus", udid, "-b"],
                        capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    except subprocess.SubprocessError:
        # Fall through to the legacy wait — bootstatus may not exist on
        # very old Xcode. The poll-then-sleep approach is the next-best.
        if not _wait_for_state(udid, "Booted", timeout=timeout):
            return
        time.sleep(5)
        return
    # Even with bootstatus, give SpringBoard another moment to render
    # — empirically helps subsequent xcodebuild launches under load.
    time.sleep(3)


def apply_pre_runner_setup(udid: str, task) -> Dict[str, Any]:
    """
    Apply spec entries that require the simulator to be shut down.

    Must be called BEFORE `XCUITestReader.start()`. The reader's start()
    will boot the sim back up (xcodebuild test launches the sim if it's
    not booted) and connect to the AX server.

    If the spec has no pre-runner entries, this is a no-op — no shutdown,
    no reboot, no wasted ~15 seconds.

    Returns a report dict logging what was done.
    """
    spec = list(task.initial_state.spec or [])
    pre_entries = [e for e in spec if is_pre_runner_entry(e)]
    report: Dict[str, Any] = {"applied": [], "errors": []}
    if not pre_entries:
        return report

    _shutdown_sim(udid)

    handler = SpringboardHandler(reader=None)
    for entry in pre_entries:
        try:
            handler.apply_pre_runner(udid, entry)
            report["applied"].append(entry)
        except Exception as e:
            report["errors"].append(
                f"Springboard.{entry.get('type')} failed: {e}")

    _boot_sim(udid)
    return report


async def apply_initial_state(reader, task) -> Dict[str, Any]:
    """
    Reset apps referenced by the task, then apply each spec entry.

    `reader` is a connected XCUITestReader — it's the bridge to the
    in-simulator Swift server that hosts the EventKit-backed commands.
    Handlers that need socket access (Reminders) take it; handlers that
    shell out (Springboard) ignore it.

    Returns a report dict logging what was done and any errors.
    """
    # Skip entries that needed shutdown — those should already have been
    # applied by `apply_pre_runner_setup` before the reader was started.
    spec = [
        e for e in (task.initial_state.spec or [])
        if not is_pre_runner_entry(e)
    ]

    # Canonicalize every app reference (task.apps + spec entries' "app")
    # to its bundle id before the set union so duplicates like
    # {"Reminders", "reminders"} resolve to one handler invocation.
    apps_in_spec = {canonicalize_app(e.get("app")) for e in spec}
    apps_in_task = {canonicalize_app(a) for a in (task.apps or [])}
    apps_to_process = {a for a in (apps_in_spec | apps_in_task) if a is not None}

    report: Dict[str, Any] = {"reset": [], "applied": [], "errors": []}

    # Surface unknown-app references early so a typo in task.apps doesn't
    # silently no-op an entire app's reset. The original (pre-canonical)
    # strings appear in the error so the typo is obvious.
    raw_unknown = {
        a for a in (set(task.apps or []) | {e.get("app") for e in spec})
        if a and canonicalize_app(a) is None
    }
    for raw in sorted(raw_unknown):
        report["errors"].append(f"unknown app {raw!r}")

    # Topo-sort by handler `depends_on`. A cycle is a programmer error
    # in handler declarations; surface it loudly without aborting other
    # framework work that already succeeded.
    try:
        ordered = _topo_sort_apps(apps_to_process)
    except ValueError as e:
        report["errors"].append(str(e))
        return report

    # Group spec entries by canonical app for the per-app apply phase.
    entries_by_app: Dict[str, List[Dict[str, Any]]] = {}
    for entry in spec:
        bid = canonicalize_app(entry.get("app"))
        if bid is not None:
            entries_by_app.setdefault(bid, []).append(entry)

    # Per-app pipeline: reset → apply(this app's entries). The previous
    # "reset everything, then apply everything in spec order" loop
    # interleaved apps and made cross-app dependency ordering invisible.
    # Per-app pipelines honor `depends_on` and isolate failure modes
    # (e.g. CalendarAgent shared-daemon races between Reminders and
    # Calendar reset — see PHASE2_PROGRESS.md "Multi-app episode lifecycle").
    for bid in ordered:
        cls = HANDLERS.get(bid)
        if cls is None:
            report["errors"].append(f"no handler for app {bid!r}")
            continue
        h = cls(reader=reader)
        try:
            await h.reset()
            report["reset"].append(bid)
        except Exception as e:
            report["errors"].append(f"{bid}.reset failed: {e}")
            # Continue to apply — current behavior tolerates partial reset
            # failures so a clean spec can still seed state. Callers
            # treating reset-failure as fatal can inspect report["errors"].
        # Per-handler apply order: handlers can declare
        # `apply_order_by_type: {type: rank}` to two-phase their entries
        # (e.g. CalendarHandler: calendar before event). Python's sort
        # is stable, so unranked types preserve spec order within their
        # rank bucket. Default rank 99 means "apply last."
        entries = entries_by_app.get(bid, [])
        order_map = getattr(cls, "apply_order_by_type", None) or {}
        if order_map:
            entries = sorted(
                entries,
                key=lambda e: order_map.get(e.get("type"), 99))
        for entry in entries:
            try:
                await h.apply(entry)
                report["applied"].append(entry)
            except Exception as e:
                report["errors"].append(
                    f"{bid}.apply({entry.get('type')}) failed: {e}")

    return report


if __name__ == "__main__":
    # Smoke test: end-to-end against a booted sim with the XCUITest server.
    import argparse
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "simulator"))
    from sibb_xcuitest_client import XCUITestReader

    parser = argparse.ArgumentParser(description="SIBB state framework smoke test")
    parser.add_argument("udid")
    parser.add_argument("--list", default="TestList")
    parser.add_argument("--items", nargs="*", default=["alpha", "beta"])
    args = parser.parse_args()

    class _T:
        pass
    from sibb_task_generator_v3 import InitialState
    t = _T()
    t.apps = ["Reminders"]
    t.initial_state = InitialState(
        spec=(
            [{"app": "Reminders", "type": "list", "name": args.list}]
            + [{"app": "Reminders", "type": "item", "list": args.list,
                "title": title} for title in args.items]
        )
    )
    print("Applying:")
    for e in t.initial_state.spec:
        print(f"  {e}")

    async def main():
        r = XCUITestReader(args.udid, bundle_id="com.apple.reminders")
        await r.start()
        try:
            report = await apply_initial_state(r, t)
            print("\nReport:")
            for k, v in report.items():
                print(f"  {k}: {v}")
        finally:
            await r.stop()
    asyncio.run(main())
