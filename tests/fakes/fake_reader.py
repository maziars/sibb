"""
In-memory stand-in for `sibb_xcuitest_client.XCUITestReader._send`.

Why this exists: handler logic (`sibb_state.RemindersHandler` and the
ones to follow) is worth testing without paying ~30 s/test for an
xcodebuild + boot + apply cycle. A naive Mock returning `{"ok": True}`
for everything tests the handler against an oracle of the bug — we'd
never catch a handler sending `{"list_name": ...}` when Swift expects
`{"list": ...}`.

This fake keeps in-memory state with the same response shapes Swift
emits. Shapes are derived from `sibb_xcuitest_setup.sh`'s case arms
(`create_list`, `create_reminder`, `list_lists`, `list_reminders`,
`wipe_reminders`). If Swift changes shape, the L4 contract goldens
(captured by `sibb/tests/scripts/record_socket_fixture.py`) flag the
drift on the nightly job and this file is updated to match.

What this is NOT: a complete EventKit simulator. Coverage is exactly
what current SIBB tasks exercise (list/create/wipe for reminders).
Anything richer — notifications, alarms, full-text search — is out
of scope; the L2 sim integration tests are the floor for those.

Recurrence — limitations to be aware of (2026-05-20):

  • Static round-trip only. The fake stores the rule as a dict and
    returns it on read. It does NOT simulate iOS's recurrence engine
    (occurrence expansion, due-date advance on completion, exception
    dates, etc.).
  • **Completion of a recurring reminder is NOT modeled.** On real
    iOS, completing a recurring `EKReminder` mutates the SAME row:
    `dueDateComponents` advances to the next occurrence and
    `isCompleted` is reset to `false`. The fake just flips
    `completed=True`. Tests exercising completion semantics of a
    recurring reminder MUST be L2 (sim integration), not L1.5.
  • Validations mirrored from Swift: recurrence without `due_iso` is
    silently dropped (matches EKReminder); `frequency` is lowercased;
    `end_iso`+`end_count` together is rejected; date-only `end_iso`
    is normalized to `"YYYY-MM-DDT23:59:59"` (end-of-day local,
    RFC-5545 UNTIL inclusivity).
  • `daysOfTheWeek` / `daysOfTheMonth` / by-position rules are NOT
    supported in v1 (Swift doesn't model them either). The reminder
    repeats every N units of frequency, end stop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional


_PRIORITY_STR_TO_INT = {"high": 1, "medium": 5, "low": 9}

_SYSTEM_LIST_NAME = "Reminders"
# Default writable calendar name on a fresh iOS 26.3 sim (per
# IOS_SIM_QUIRKS §16, probed 2026-05-20). Was "Home" prior to Phase
# 2c — corrected to match the real sim.
_DEFAULT_CALENDAR_NAME = "Calendar"


class FakeXCUITestReader:
    def __init__(self, udid: str = "FAKE-UDID-0000-0000-0000-000000000000"):
        self.udid = udid
        self._counter = 0
        self._lists: List[Dict[str, Any]] = [
            {"name": _SYSTEM_LIST_NAME,
             "identifier": "system-reminders-default",
             "immutable": True},
        ]
        self._reminders: List[Dict[str, Any]] = []
        # Calendar collections — the iOS default "Calendar" is always
        # present; user-created ones come and go via create_calendar /
        # wipe_calendars (mirrors EKCalendar semantics).
        self._calendars: List[Dict[str, Any]] = [
            {"name": _DEFAULT_CALENDAR_NAME,
             "identifier": "system-calendar-default",
             "source": "Local",
             "system": True},
        ]
        # Calendar events.
        self._events: List[Dict[str, Any]] = []
        # Contacts live in a single default container; the fake doesn't
        # model CN containers/groups. Identifier = "fake-contact-<hex>".
        self._contacts: List[Dict[str, Any]] = []
        # In-memory filesystem for the Files handler. Keys are
        # workspace-relative paths; values are utf-8 string content
        # (base64-decoded if the source encoding was base64). The
        # fake doesn't model directories as first-class objects —
        # they're synthesized from the set of file paths' parents.
        self._files: Dict[str, str] = {}
        # In-memory PHAsset stand-ins. Each entry is a dict shaped
        # like Swift's list_photos row output. The fake doesn't
        # support `addmedia` (that's a host-side simctl call, not a
        # socket command) — tests that want to seed photos via the
        # fake should call _send({"type": "_inject_photo", ...}) or
        # poke `_photos` directly.
        self._photos: List[Dict[str, Any]] = []
        # In-memory HealthKit samples. Each entry mirrors what Swift's
        # list_health_samples returns: sample_type, value, unit,
        # start_iso, end_iso, identifier, source.
        self._health_samples: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"fake-{prefix}-{self._counter:08x}"

    async def _send(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        await asyncio.sleep(0)
        resp = self._dispatch(cmd)
        self.history.append({"request": dict(cmd), "response": dict(resp)})
        return resp

    def _dispatch(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        t = cmd.get("type")
        if t == "create_list":
            return self._create_list(cmd)
        if t == "create_reminder":
            return self._create_reminder(cmd)
        if t == "list_lists":
            return self._list_lists()
        if t == "list_reminders":
            return self._list_reminders(cmd)
        if t == "wipe_reminders":
            return self._wipe_reminders()
        if t == "create_event":
            return self._create_event(cmd)
        if t == "list_events":
            return self._list_events(cmd)
        if t == "wipe_events":
            return self._wipe_events()
        if t == "create_calendar":
            return self._create_calendar(cmd)
        if t == "list_calendars":
            return self._list_calendars(cmd)
        if t == "wipe_calendars":
            return self._wipe_calendars()
        if t == "create_contact":
            return self._create_contact(cmd)
        if t == "list_contacts":
            return self._list_contacts(cmd)
        if t == "wipe_contacts":
            return self._wipe_contacts()
        if t == "create_file":
            return self._create_file(cmd)
        if t == "list_files":
            return self._list_files(cmd)
        if t == "read_file":
            return self._read_file(cmd)
        if t == "wipe_files":
            return self._wipe_files()
        if t == "list_photos":
            return self._list_photos()
        if t == "wipe_photos":
            return self._wipe_photos()
        if t == "_inject_photo":
            return self._inject_photo(cmd)
        if t == "create_health_sample":
            return self._create_health_sample(cmd)
        if t == "list_health_samples":
            return self._list_health_samples(cmd)
        if t == "wipe_health_samples":
            return self._wipe_health_samples()
        if t == "swipe_at":
            return self._swipe_at(cmd)
        if t == "swipe":
            return {"ok": True, "direction": cmd.get("direction", "up")}
        if t == "pinch":
            return {"ok": True,
                     "scale": cmd.get("scale", 0.5),
                     "velocity": cmd.get("velocity", 1.0)}
        if t == "observe":
            return self._observe(cmd)
        if t == "tap":
            # Instrumented for tests asserting "didn't accidentally
            # route to single-tap" (notably DOUBLE_TAP regressions).
            # Same pattern as `tap_then_type_call_count` and
            # `double_tap_call_count` below.
            if not hasattr(self, "tap_call_count"):
                self.tap_call_count = 0
            self.tap_call_count += 1
            if not hasattr(self, "tap_calls"):
                self.tap_calls = []
            self.tap_calls.append(
                {"x": cmd.get("x"), "y": cmd.get("y"),
                 "ref": cmd.get("ref")})
            return {"ok": True}
        if t == "double_tap":
            # Instrumented for tests asserting the executor reached
            # Swift. Mirrors `tap_then_type_call_count` pattern.
            if not hasattr(self, "double_tap_call_count"):
                self.double_tap_call_count = 0
            self.double_tap_call_count += 1
            if not hasattr(self, "double_tap_calls"):
                self.double_tap_calls = []
            self.double_tap_calls.append(
                {"x": cmd.get("x"), "y": cmd.get("y"),
                 "ref": cmd.get("ref")})
            return {"ok": True}
        if t == "type":
            return {"ok": True}
        if t == "return":
            # Instrumented so RETURN-verb L1 tests can assert the
            # executor reached Swift (same pattern as double_tap +
            # tap_then_type counters above). Step 5L-C also lets the
            # test simulate Swift's no-keyboard error path by setting
            # `return_simulate_no_keyboard = True` on the fake.
            if not hasattr(self, "return_call_count"):
                self.return_call_count = 0
            self.return_call_count += 1
            if getattr(self, "return_simulate_no_keyboard", False):
                return {"ok": False, "error": "no_keyboard",
                         "hint": ("No soft keyboard is up — RETURN "
                                  "requires a focused text input.")}
            return {"ok": True}
        if t == "clear_text":
            return self._clear_text(cmd)
        if t == "tap_then_type":
            return self._tap_then_type(cmd)
        if t == "launch":
            return {"ok": True}
        if t == "attach":
            return {"ok": True}
        return {"ok": False,
                "error": f"FakeXCUITestReader: unknown command {t!r}"}

    async def observe(self):
        """High-level observe matching XCUITestReader.observe()'s
        contract: returns an AXTree populated from the canned response
        configured via set_observe_response(). Used by scaffold tests
        that exercise the full read pipeline."""
        # Lazy import — the fakes module shouldn't pull simulator
        # imports unless this code path is hit.
        from sibb_xcuitest_client import AXElement, AXTree  # type: ignore
        resp = self._observe({"type": "observe"})
        if not resp.get("ok"):
            raise RuntimeError(f"observe failed: {resp.get('error')}")
        elements = [AXElement(e) for e in resp.get("elements", [])]
        tree = AXTree(
            elements,
            self.udid,
            keyboard_visible=resp.get("keyboard_visible", False),
            screen_width=resp.get("screen_width", 402),
            screen_height=resp.get("screen_height", 874),
            method=resp.get("method", "snapshot"),
            bundle_id=resp.get("bundle_id", ""),
            keyboard_frame=resp.get("keyboard_frame"),
        )
        # Mirror the live client: gracefully pass-through new fields.
        tree.zoom_scale = resp.get("zoom_scale")
        tree.accessory_bar_frame = resp.get("accessory_bar_frame")
        return tree

    async def type_text(self, text: str) -> None:
        """Mirror of XCUITestReader.type_text (raw typeText to
        currently-focused element). Records the text in
        self.typed_history for tests that need to assert what was
        sent."""
        if not hasattr(self, "typed_history"):
            self.typed_history: List[str] = []
        self.typed_history.append(text)
        await self._send({"type": "type", "text": text})

    async def tap(self, x: float = None, y: float = None,
                   ref: str = None) -> None:
        """Mirror of XCUITestReader.tap — no-op success on the fake."""
        cmd: Dict[str, Any] = {"type": "tap"}
        if x is not None: cmd["x"] = x
        if y is not None: cmd["y"] = y
        if ref: cmd["ref"] = ref
        await self._send(cmd)

    async def double_tap(self, x: float = None, y: float = None,
                          ref: str = None) -> None:
        """Mirror of XCUITestReader.double_tap. The dispatched command
        increments `double_tap_call_count` and appends to
        `double_tap_calls` so tests can assert what coords/refs were
        sent."""
        cmd: Dict[str, Any] = {"type": "double_tap"}
        if x is not None: cmd["x"] = x
        if y is not None: cmd["y"] = y
        if ref: cmd["ref"] = ref
        await self._send(cmd)

    async def pinch(self, scale: float = 0.5,
                     velocity: float = 1.0) -> Dict[str, Any]:
        """Mirror of XCUITestReader.pinch — records the call in
        `self.pinch_history` for tests, returns the canonical OK
        envelope."""
        if not hasattr(self, "pinch_history"):
            self.pinch_history: List[Dict[str, float]] = []
        self.pinch_history.append({"scale": float(scale),
                                     "velocity": float(velocity)})
        return await self._send({
            "type": "pinch",
            "scale": float(scale),
            "velocity": float(velocity),
        })

    async def tap_then_type(self, x: float, y: float, text: str,
                              focus_timeout_ms: int = 1500
                              ) -> Dict[str, Any]:
        """Mirror of XCUITestReader.tap_then_type. Routes through
        _send so test responses can be configured via
        set_tap_then_type_response()."""
        return await self._send({"type": "tap_then_type",
                                  "x": x, "y": y, "text": text,
                                  "focus_timeout_ms": focus_timeout_ms})

    async def clear_text(self, x: float, y: float,
                          length_hint: Optional[int] = None
                          ) -> Dict[str, Any]:
        """Mirror of XCUITestReader.clear_text(x, y, length_hint).
        Routes through _send so test responses can be configured via
        set_clear_text_response()."""
        cmd: Dict[str, Any] = {"type": "clear_text", "x": x, "y": y}
        if length_hint is not None:
            cmd["length_hint"] = int(length_hint)
        resp = await self._send(cmd)
        if not resp.get("ok"):
            raise RuntimeError(f"Clear failed: {resp.get('error')}")
        return resp

    def set_observe_response(self, elements: List[Dict[str, Any]],
                              bundle_id: str = "fake.bundle",
                              keyboard_visible: bool = False,
                              screen_width: int = 402,
                              screen_height: int = 874,
                              keyboard_frame: Optional[Dict[str, float]] = None,
                              zoom_scale: Optional[float] = None,
                              accessory_bar_frame: Optional[Dict[str, float]] = None,
                              ) -> None:
        """Set the canned response for the next `observe` call. Tests
        construct synthetic AX trees here to exercise the scaffold's
        observation pipeline without a real simulator.

        New optional kwargs (added 2026-06-05) for the Safari auto-zoom
        robustness work:
          * keyboard_frame — {x, y, width, height} for the on-screen kb
          * zoom_scale     — WKWebView zoom factor (1.0 = unzoomed)
          * accessory_bar_frame — predictive bar / inputAccessoryView
        These are passed through to scaffold._read_xcuitest as a real
        Swift response would.
        """
        self._observe_resp = {
            "ok": True,
            "elements": elements,
            "bundle_id": bundle_id,
            "keyboard_visible": keyboard_visible,
            "screen_width": screen_width,
            "screen_height": screen_height,
            "method": "snapshot",
        }
        if keyboard_frame is not None:
            self._observe_resp["keyboard_frame"] = keyboard_frame
        if zoom_scale is not None:
            self._observe_resp["zoom_scale"] = zoom_scale
        if accessory_bar_frame is not None:
            self._observe_resp["accessory_bar_frame"] = accessory_bar_frame

    def _observe(self, _cmd: Dict[str, Any]) -> Dict[str, Any]:
        resp = getattr(self, "_observe_resp", None)
        if resp is None:
            return {"ok": True, "elements": [], "bundle_id": "",
                     "keyboard_visible": False,
                     "screen_width": 402, "screen_height": 874,
                     "method": "snapshot"}
        return dict(resp)

    def _tap_then_type(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Stub for the Swift `tap_then_type` command. Tests configure
        the response via set_tap_then_type_response(); default is
        success with focus acquired in 100ms.

        Instrumented for tests: increments `tap_then_type_call_count`
        on every call. Tests using `set_tap_then_type_response(
        error="should not be called")` should also assert
        `fake.tap_then_type_call_count == 0` to actually pin the
        short-circuit. Without that, the "should not be called" error
        merely propagates through the result dict and a regression
        that DID call Swift would still pass."""
        if not hasattr(self, "tap_then_type_call_count"):
            self.tap_then_type_call_count = 0
        self.tap_then_type_call_count += 1
        x = cmd.get("x"); y = cmd.get("y")
        text = cmd.get("text")
        if (not isinstance(x, (int, float)) or
                not isinstance(y, (int, float)) or
                not isinstance(text, str)):
            return {"ok": False,
                    "error": "tap_then_type requires `x`, `y` (Double), `text` (str)"}
        resp = getattr(self, "_tap_type_resp", None)
        if resp is None:
            return {"ok": True, "focus_acquired": True,
                    "acquired_ms": 100, "typed": text}
        # If the configured response uses `ok=True` and the text isn't
        # present, fill it in for diagnostic completeness.
        out = dict(resp)
        if out.get("ok") and "typed" not in out:
            out["typed"] = text
        return out

    def set_tap_then_type_response(self, *,
                                     ok: bool = True,
                                     acquired_ms: int = 100,
                                     error: Optional[str] = None,
                                     polled_ms: int = 1500,
                                     focused_frame: Optional[Dict[str, float]] = None
                                     ) -> None:
        """Configure the next tap_then_type response. For Policy A
        fail-fast tests, pass ok=False + error='focus_not_acquired' +
        the focused_frame the agent should see."""
        if ok:
            self._tap_type_resp = {"ok": True, "focus_acquired": True,
                                    "acquired_ms": acquired_ms}
        else:
            resp: Dict[str, Any] = {"ok": False,
                                     "error": error or "focus_not_acquired",
                                     "polled_ms": polled_ms}
            if focused_frame is not None:
                resp["focused_frame"] = focused_frame
            self._tap_type_resp = resp

    def _clear_text(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Stub for the Swift `clear_text` command. Tests configure
        the response via set_clear_text_response(); default returns
        success with the length_hint echoed as deletes_sent."""
        x = cmd.get("x")
        y = cmd.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return {"ok": False,
                    "error": "clear_text requires `x` and `y` (Double)"}
        resp = getattr(self, "_clear_resp", None)
        if resp is None:
            return {"ok": True,
                    "coords": [x, y],
                    "deletes_sent": cmd.get("length_hint", 0),
                    "stopped_early": False}
        return dict(resp)

    def set_clear_text_response(self, *,
                                  deletes_sent: int = 0,
                                  stopped_early: bool = False,
                                  ok: bool = True,
                                  error: Optional[str] = None) -> None:
        """Configure the next clear_text response. `deletes_sent` is
        how many backspaces Swift sent; `stopped_early` indicates the
        24-key cap fired (length_hint > 19 + 5 padding). Tests that
        want the fail path pass ok=False + error."""
        if ok:
            self._clear_resp = {"ok": True,
                                 "deletes_sent": deletes_sent,
                                 "stopped_early": stopped_early}
        else:
            self._clear_resp = {"ok": False,
                                 "error": error or "clear failed"}

    def _swipe_at(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        for field in ("x1", "y1", "x2", "y2"):
            if not isinstance(cmd.get(field), (int, float)):
                return {"ok": False,
                        "error": "x1, y1, x2, y2 (Double) required"}
        return {"ok": True,
                "from": [cmd["x1"], cmd["y1"]],
                "to":   [cmd["x2"], cmd["y2"]],
                "duration_s": cmd.get("duration_s", 0.05)}

    def _find_list(self, name: str) -> Optional[Dict[str, Any]]:
        for L in self._lists:
            if L["name"] == name:
                return L
        return None

    def _create_list(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        name = cmd.get("name", "")
        if not name:
            return {"ok": False, "error": "name required"}
        ident = self._next_id("list")
        self._lists.append(
            {"name": name, "identifier": ident, "immutable": False}
        )
        return {"ok": True, "name": name, "identifier": ident}

    def _create_reminder(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        title = cmd.get("title", "")
        list_name = cmd.get("list", "")
        if not title or not list_name:
            return {"ok": False, "error": "title and list required"}
        if self._find_list(list_name) is None:
            return {"ok": False, "error": f"list {list_name} not found"}
        priority_str = cmd.get("priority")
        priority = _PRIORITY_STR_TO_INT.get(
            (priority_str or "").lower(), 0
        )
        ident = self._next_id("reminder")
        row: Dict[str, Any] = {
            "title": title,
            "list": list_name,
            "priority": priority,
            "completed": bool(cmd.get("completed", False)),
            "identifier": ident,
        }
        # due_iso / notes / url are only surfaced in list_reminders rows
        # when set — match the Swift contract exactly. Storage key is
        # `due` (matches Swift output), preserving the canonical Swift
        # round-trip:
        #   "YYYY-MM-DD"          → kept verbatim (date-only).
        #   "YYYY-MM-DDTHH:MM:SS" → kept verbatim (local-time, no Z).
        #   "YYYY-MM-DDTHH:MM:SSZ"→ Swift parses + re-emits without Z;
        #                            the fake matches by stripping Z so
        #                            comparison stays exact-string.
        for opt in ("due_iso", "notes", "url"):
            v = cmd.get(opt)
            if v is None or (isinstance(v, str) and v == ""):
                continue
            if opt == "due_iso":
                if isinstance(v, str) and v.endswith("Z"):
                    v = v[:-1]
                row["due"] = v
            else:
                row[opt] = v
        # Recurrence: mirror the real Swift+EventKit normalization so
        # L1.5 tests don't drift from L2. Specifically (2026-05-20
        # critic pass):
        #   • Drop recurrence if no due_iso — EKReminder silently
        #     discards the rule on save without dueDateComponents.
        #   • Lowercase frequency on store — Swift does `.lowercased()`.
        #   • Reject end_iso+end_count together — EKRecurrenceEnd is
        #     a sum type.
        #   • Normalize date-only end_iso to "YYYY-MM-DDT23:59:59"
        #     — EKRecurrenceEnd stores a Date (point-in-time), and
        #     UNTIL is RFC-5545 inclusive so we use end-of-day.
        # Tests exercising the **completion** lifecycle of a recurring
        # reminder MUST go through L2 — see class docstring.
        rec = cmd.get("recurrence")
        if isinstance(rec, dict) and rec:
            if cmd.get("due_iso") in (None, ""):
                # Mirror EventKit's silent-discard; the row simply
                # has no `recurrence` key.
                pass
            else:
                freq_raw = rec.get("frequency")
                if not isinstance(freq_raw, str):
                    return {"ok": False,
                             "error": "recurrence requires frequency"}
                freq = freq_raw.lower()
                if freq not in ("daily", "weekly", "monthly", "yearly"):
                    return {"ok": False,
                             "error": ("recurrence frequency must be one "
                                       "of daily/weekly/monthly/yearly, "
                                       f"got {freq_raw}")}
                interval_raw = rec.get("interval", 1)
                # Don't use `or 1` — explicit 0 (or negative) must
                # reach the validation below, not get silently clamped.
                interval = int(interval_raw) if interval_raw is not None else 1
                if interval < 1:
                    return {"ok": False,
                             "error": ("recurrence interval must be >= 1, "
                                       f"got {interval}")}
                has_end_iso   = "end_iso"   in rec and rec["end_iso"] is not None
                has_end_count = "end_count" in rec and rec["end_count"] is not None
                if has_end_iso and has_end_count:
                    return {"ok": False,
                             "error": ("recurrence end_iso and end_count "
                                       "are mutually exclusive")}
                normalized: Dict[str, Any] = {
                    "frequency": freq, "interval": interval,
                }
                if has_end_iso:
                    end_iso = rec["end_iso"]
                    if isinstance(end_iso, str) and "T" not in end_iso:
                        # Date-only → end-of-day local.
                        end_iso = f"{end_iso}T23:59:59"
                    normalized["end_iso"] = end_iso
                elif has_end_count:
                    normalized["end_count"] = int(rec["end_count"])
                row["recurrence"] = normalized
        self._reminders.append(row)
        return {"ok": True, "title": title,
                "list": list_name, "identifier": ident}

    def _list_lists(self) -> Dict[str, Any]:
        return {"ok": True, "lists": [dict(L) for L in self._lists]}

    def _list_reminders(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        list_filter = cmd.get("list")
        include_completed = bool(cmd.get("include_completed", False))
        out: List[Dict[str, Any]] = []
        for r in self._reminders:
            if not include_completed and r["completed"]:
                continue
            if list_filter is not None and \
               r["list"].lower() != list_filter.lower():
                continue
            out.append(dict(r))
        return {"ok": True, "reminders": out}

    def _wipe_reminders(self) -> Dict[str, Any]:
        removed_reminders = len(self._reminders)
        self._reminders = []
        kept: List[Dict[str, Any]] = []
        removed_lists = 0
        for L in self._lists:
            if L.get("immutable"):
                kept.append(L)
            else:
                removed_lists += 1
        self._lists = kept
        return {"ok": True,
                "removed_reminders": removed_reminders,
                "removed_lists": removed_lists}

    # ───────────────────────── Calendar events ────────────────────────

    def _create_event(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        title = cmd.get("title", "")
        start_iso = cmd.get("start_iso", "")
        end_iso = cmd.get("end_iso", "")
        if not title or not start_iso or not end_iso:
            return {"ok": False,
                    "error": "title, start_iso, end_iso required"}
        requested_cal = cmd.get("calendar")
        if requested_cal is not None:
            # Mirror Swift: lookup by name case-insensitively; error if
            # absent. Generators that target a user-created calendar
            # must seed it via create_calendar first (spec order
            # enforces this).
            match = next(
                (c for c in self._calendars
                 if c["name"].lower() == requested_cal.lower()),
                None)
            if match is None:
                return {"ok": False,
                        "error": "no writable calendar available"}
            cal_name = match["name"]
        else:
            cal_name = _DEFAULT_CALENDAR_NAME
        all_day = bool(cmd.get("all_day", False))
        # Mirror Swift's round-trip canonicalization (sibb_xcuitest_setup.sh
        # list_events) plus the empirically-verified all-day endDate quirk
        # (IOS_SIM_QUIRKS §16, probed 2026-05-21):
        #   • timed events round-trip as "YYYY-MM-DDTHH:MM:SS" (local, no Z)
        #   • all-day events round-trip as date-only ("YYYY-MM-DD")
        #   • all-day end_iso is the LAST INCLUSIVE day, NOT day-after.
        #     iOS stores endDate as "last second of inclusive last day";
        #     when an "exclusive end" input like "T00:00:00" of day-after
        #     is parsed and date-formatted, it falls back to the day
        #     before. Mirror this by decrementing end_iso when input
        #     uses the exclusive-end convention.
        import datetime as _dt_local
        if all_day:
            if "T" in start_iso:
                start_iso = start_iso[:10]
            if "T" in end_iso:
                # Input was "YYYY-MM-DDTHH:MM:SS" with time component.
                # Interpret as exclusive end; subtract one day so the
                # read-back matches what real iOS Calendar emits.
                ed = _dt_local.date.fromisoformat(end_iso[:10])
                end_iso = (ed - _dt_local.timedelta(days=1)).isoformat()
        ident = self._next_id("event")
        # Recurrence parsing: mirror Swift validators (sibb_xcuitest_setup.sh
        # create_event's recurrence block). Same shape as RemindersItem.
        # No "requires due_iso" check (events always have start/end).
        rec = cmd.get("recurrence")
        normalized_rec: Optional[Dict[str, Any]] = None
        if isinstance(rec, dict) and rec:
            freq_raw = rec.get("frequency")
            if not isinstance(freq_raw, str):
                return {"ok": False,
                         "error": "recurrence requires frequency"}
            freq = freq_raw.lower()
            if freq not in ("daily", "weekly", "monthly", "yearly"):
                return {"ok": False,
                         "error": ("recurrence frequency must be one "
                                   "of daily/weekly/monthly/yearly, "
                                   f"got {freq_raw}")}
            interval_raw = rec.get("interval", 1)
            interval = (int(interval_raw) if interval_raw is not None
                        else 1)
            if interval < 1:
                return {"ok": False,
                         "error": ("recurrence interval must be >= 1, "
                                   f"got {interval}")}
            has_end_iso   = "end_iso"   in rec and rec["end_iso"] is not None
            has_end_count = "end_count" in rec and rec["end_count"] is not None
            if has_end_iso and has_end_count:
                return {"ok": False,
                         "error": ("recurrence end_iso and end_count "
                                   "are mutually exclusive")}
            normalized_rec = {"frequency": freq, "interval": interval}
            if has_end_iso:
                end_iso = rec["end_iso"]
                if isinstance(end_iso, str) and "T" not in end_iso:
                    # Date-only → end-of-day local (RFC 5545 inclusive).
                    end_iso = f"{end_iso}T23:59:59"
                normalized_rec["end_iso"] = end_iso
            elif has_end_count:
                normalized_rec["end_count"] = int(rec["end_count"])
        event_row = {
            "title":      title,
            "calendar":   cal_name,
            "start_iso":  start_iso,
            "end_iso":    end_iso,
            "all_day":    all_day,
            "location":   cmd.get("location", "") or "",
            "notes":      cmd.get("notes", "") or "",
            "url":        cmd.get("url", "") or "",
            "identifier": ident,
        }
        if normalized_rec is not None:
            event_row["recurrence"] = normalized_rec
        self._events.append(event_row)
        return {"ok": True, "title": title,
                "calendar": cal_name, "identifier": ident}

    def _list_events(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        cal_filter = cmd.get("calendar")
        start = cmd.get("start_iso")
        end = cmd.get("end_iso")
        # writable_only is accepted for API parity with the real Swift
        # command (see IOS_SIM_QUIRKS §16). The fake doesn't model
        # read-only subscribed calendars (US Holidays etc.), so the
        # filter is a no-op here — every event in self._events is
        # considered writable. Tests that want to validate the filter's
        # behavior should run on a real sim, not the fake.
        _ = cmd.get("writable_only", True)
        # master_only default ON — same as Swift, returns one row per
        # series (master's startDate). When False, expand recurring
        # events into N occurrence rows matching iOS's predicateForEvents
        # semantics (per probe Q6.2 / IOS_SIM_QUIRKS §16).
        master_only = bool(cmd.get("master_only", True))
        out: List[Dict[str, Any]] = []
        for e in self._events:
            if cal_filter is not None and \
               e["calendar"].lower() != cal_filter.lower():
                continue
            expansions = self._expand_event_occurrences(
                e, master_only=master_only,
                window_start=start, window_end=end)
            for occ in expansions:
                # Window-overlap filter on each expanded occurrence
                # (master_only=True hits this once with the master row).
                if start is not None and occ["end_iso"] < start:
                    continue
                if end is not None and occ["start_iso"] > end:
                    continue
                out.append(occ)
        return {"ok": True, "events": out}

    def _expand_event_occurrences(self, event: Dict[str, Any],
                                    *, master_only: bool,
                                    window_start: Optional[str],
                                    window_end: Optional[str]
                                    ) -> List[Dict[str, Any]]:
        """For a recurring event, generate occurrence rows (master's
        identifier; each occurrence's own start/end). When master_only
        or no recurrence, emit one row (the master/event as-is)."""
        if master_only or not event.get("recurrence"):
            return [dict(event)]
        rec = event["recurrence"]
        freq = rec.get("frequency")
        interval = int(rec.get("interval", 1)) or 1
        # Determine occurrence step in days. Only daily/weekly handled
        # exactly; monthly/yearly approximated for now (fake fidelity
        # gap — real iOS uses calendar arithmetic).
        step_days = {"daily": 1, "weekly": 7,
                      "monthly": 30, "yearly": 365}.get(freq)
        if step_days is None:
            return [dict(event)]
        step = step_days * interval
        # Compute occurrence cap.
        end_count = rec.get("end_count")
        end_iso = rec.get("end_iso")
        max_iters = 200  # safety cap; iOS won't materialize forever either
        if end_count is not None:
            max_iters = int(end_count)
        import datetime as _dtl
        # Parse master's start_iso (timed) or treat date-only for all-day.
        start_str = event["start_iso"]
        end_str = event["end_iso"]
        try:
            if "T" in start_str:
                master_start = _dtl.datetime.fromisoformat(start_str)
                master_end = _dtl.datetime.fromisoformat(end_str)
            else:
                master_start = _dtl.datetime.fromisoformat(start_str + "T00:00:00")
                master_end = _dtl.datetime.fromisoformat(end_str + "T00:00:00")
        except ValueError:
            return [dict(event)]
        end_dt = None
        if end_iso is not None:
            try:
                end_dt = _dtl.datetime.fromisoformat(
                    end_iso if "T" in end_iso else end_iso + "T23:59:59")
            except ValueError:
                end_dt = None
        out: List[Dict[str, Any]] = []
        for i in range(max_iters):
            occ_start = master_start + _dtl.timedelta(days=step * i)
            occ_end   = master_end   + _dtl.timedelta(days=step * i)
            if end_dt is not None and occ_start > end_dt:
                break
            row = dict(event)
            row["start_iso"] = occ_start.isoformat() if "T" in start_str \
                                else occ_start.date().isoformat()
            row["end_iso"]   = occ_end.isoformat() if "T" in end_str \
                                else occ_end.date().isoformat()
            out.append(row)
        return out

    def _wipe_events(self) -> Dict[str, Any]:
        removed = len(self._events)
        self._events = []
        return {"ok": True, "removed_events": removed}

    def _create_calendar(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        name = cmd.get("name", "")
        if not name:
            return {"ok": False, "error": "name required"}
        # Mirror Swift: reject duplicates (case-insensitive) and reject
        # shadowing the default "Calendar".
        for c in self._calendars:
            if c["name"].lower() == name.lower():
                return {"ok": False,
                        "error": f"calendar with name {name} already exists"}
        ident = self._next_id("calendar")
        self._calendars.append({
            "name": name,
            "identifier": ident,
            "source": "Local",
            "system": False,
        })
        return {"ok": True, "name": name, "identifier": ident}

    def _list_calendars(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        # Swift's list_calendars excludes non-writable subscribed
        # calendars; the fake doesn't model those so the system default
        # "Calendar" + any user-created ones are returned, sans the
        # "system" flag (which Swift doesn't surface either).
        rows: List[Dict[str, Any]] = []
        for c in self._calendars:
            rows.append({
                "name": c["name"],
                "identifier": c["identifier"],
                "source": c["source"],
            })
        return {"ok": True, "calendars": rows}

    def _wipe_calendars(self) -> Dict[str, Any]:
        # Preserve the default "Calendar" (system==True); remove all
        # user-created calendars. Matches Swift wipe_calendars semantics.
        removed = 0
        kept: List[Dict[str, Any]] = []
        for c in self._calendars:
            if c.get("system"):
                kept.append(c)
            else:
                removed += 1
        self._calendars = kept
        return {"ok": True, "removed_calendars": removed}

    def _create_contact(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        given  = cmd.get("given_name", "") or ""
        family = cmd.get("family_name", "") or ""
        if not given and not family:
            return {"ok": False,
                    "error": "given_name or family_name required"}
        ident = self._next_id("contact")
        self._contacts.append({
            "given_name":   given,
            "family_name":  family,
            "phone":        cmd.get("phone", "") or "",
            "email":        cmd.get("email", "") or "",
            "organization": cmd.get("organization", "") or "",
            "identifier":   ident,
        })
        return {"ok": True, "given_name": given,
                "family_name": family, "identifier": ident}

    def _list_contacts(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        name_filter = cmd.get("name_filter")
        nf = name_filter.lower() if isinstance(name_filter, str) else None
        out: List[Dict[str, Any]] = []
        for c in self._contacts:
            if nf is not None:
                g = c["given_name"].lower()
                f = c["family_name"].lower()
                full = f"{g} {f}"
                if nf not in g and nf not in f and nf not in full:
                    continue
            out.append(dict(c))
        return {"ok": True, "contacts": out}

    def _wipe_contacts(self) -> Dict[str, Any]:
        removed = len(self._contacts)
        self._contacts = []
        return {"ok": True, "removed_contacts": removed}

    # ── Files (FileManager-backed in real Swift; in-memory here) ────

    @staticmethod
    def _validate_file_path(path: str) -> Optional[str]:
        """Returns None if `path` is workspace-safe, else an error
        string mirroring the Swift-side rejection cases."""
        if not path:
            return "path required"
        if path.startswith("/"):
            return "path must be relative and not contain .."
        if ".." in path.split("/"):
            return "path must be relative and not contain .."
        return None

    def _create_file(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        path = cmd.get("path", "")
        err = self._validate_file_path(path)
        if err:
            return {"ok": False, "error": err}
        encoding = cmd.get("encoding", "utf-8")
        content = cmd.get("content", "")
        if encoding == "base64":
            import base64 as _b64
            try:
                decoded = _b64.b64decode(content).decode(
                    "utf-8", errors="replace")
            except Exception:
                return {"ok": False,
                        "error": f"content decode failed (encoding={encoding})"}
            stored = decoded
            size = len(_b64.b64decode(content))
        else:
            stored = content
            size = len(content.encode("utf-8"))
        self._files[path] = stored
        return {"ok": True, "path": path, "size": size}

    def _list_files(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        directory = cmd.get("directory", "")
        recursive = bool(cmd.get("recursive", True))
        if directory:
            err = self._validate_file_path(directory)
            if err:
                return {"ok": False, "error": err}
            prefix = directory.rstrip("/") + "/"
        else:
            prefix = ""
        # Collect files matching the scope, then synthesize parent
        # directory entries (Swift's listing returns both file and
        # dir rows; the fake mirrors that shape).
        scoped = {p: c for p, c in self._files.items() if p.startswith(prefix)}
        if not recursive and prefix == "":
            # Only direct children of root.
            scoped = {p: c for p, c in scoped.items() if "/" not in p}
        elif not recursive:
            scoped = {p: c for p, c in scoped.items()
                       if "/" not in p[len(prefix):]}
        rows: List[Dict[str, Any]] = []
        seen_dirs = set()
        for p, c in scoped.items():
            rows.append({"path": p, "type": "file",
                          "size": len(c.encode("utf-8"))})
            # Walk parents and emit dir rows once each.
            parts = p.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d and d not in seen_dirs:
                    seen_dirs.add(d)
                    rows.append({"path": d, "type": "dir", "size": 0})
        return {"ok": True, "files": rows}

    def _read_file(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        path = cmd.get("path", "")
        err = self._validate_file_path(path)
        if err:
            return {"ok": False, "error": err}
        if path not in self._files:
            return {"ok": False, "error": "not found"}
        encoding = cmd.get("encoding", "utf-8")
        content = self._files[path]
        if encoding == "base64":
            import base64 as _b64
            return {"ok": True, "path": path,
                    "size": len(content.encode("utf-8")),
                    "content": _b64.b64encode(
                        content.encode("utf-8")).decode("ascii")}
        return {"ok": True, "path": path,
                "size": len(content.encode("utf-8")),
                "content": content}

    def _wipe_files(self) -> Dict[str, Any]:
        removed = len(self._files)
        self._files = {}
        return {"ok": True, "removed": removed}

    # ── Photos (PHAsset-shaped rows in memory) ─────────────────────

    def _inject_photo(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Test-only escape hatch — simctl addmedia in the real
        world is host-side and bypasses the socket. The fake gets
        an explicit injection command so L1.5 tests can seed
        `list_photos` without monkeypatching subprocess."""
        ident = self._next_id("photo")
        row = {
            "identifier":    ident,
            "media_type":    cmd.get("media_type", "image"),
            "pixel_width":   cmd.get("pixel_width", 1),
            "pixel_height":  cmd.get("pixel_height", 1),
            "duration":      cmd.get("duration", 0.0),
            "creation_date": cmd.get("creation_date", ""),
            "is_favorite":   cmd.get("is_favorite", False),
            "is_hidden":     cmd.get("is_hidden", False),
        }
        self._photos.append(row)
        return {"ok": True, "identifier": ident}

    def _list_photos(self) -> Dict[str, Any]:
        return {"ok": True,
                "photos": [dict(p) for p in self._photos]}

    def _wipe_photos(self) -> Dict[str, Any]:
        removed = len(self._photos)
        self._photos = []
        return {"ok": True, "removed_photos": removed}

    # ── Health (HKHealthStore) ─────────────────────────────────────

    _HEALTH_UNITS = {
        "step_count": "count",
        "heart_rate": "count/min",
        "body_mass":  "kg",
    }

    def _create_health_sample(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        sample_type = cmd.get("sample_type", "")
        unit = self._HEALTH_UNITS.get(sample_type)
        if unit is None:
            return {"ok": False,
                    "error": f"unknown sample_type {sample_type}; "
                             f"valid: {sorted(self._HEALTH_UNITS)}"}
        value = cmd.get("value")
        if not isinstance(value, (int, float)):
            return {"ok": False,
                    "error": "value (Double or Int) required"}
        start_iso = cmd.get("start_iso", "")
        if not start_iso:
            return {"ok": False, "error": "start_iso required"}
        end_iso = cmd.get("end_iso", start_iso)
        ident = self._next_id("health")
        self._health_samples.append({
            "sample_type": sample_type,
            "value":       float(value),
            "unit":        unit,
            "start_iso":   start_iso,
            "end_iso":     end_iso,
            "identifier":  ident,
            "source":      "com.sibb.tests.xctrunner",
        })
        return {"ok": True, "sample_type": sample_type,
                "value": float(value), "unit": unit,
                "identifier": ident}

    def _list_health_samples(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        type_filter = cmd.get("sample_type")
        start = cmd.get("start_iso")
        end = cmd.get("end_iso")
        out: List[Dict[str, Any]] = []
        for s in self._health_samples:
            if type_filter is not None and s["sample_type"] != type_filter:
                continue
            if start is not None and s["end_iso"] < start:
                continue
            if end is not None and s["start_iso"] > end:
                continue
            out.append(dict(s))
        return {"ok": True, "samples": out}

    def _wipe_health_samples(self) -> Dict[str, Any]:
        removed = len(self._health_samples)
        self._health_samples = []
        return {"ok": True, "removed_samples": removed}
