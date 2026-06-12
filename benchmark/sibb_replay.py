#!/usr/bin/env python3
"""
SIBB Manual Replay — play the role of the LLM, one task at a time.
===================================================================

The scaffold sets up the environment, prints the same task instruction
+ AX observation an LLM would receive, and waits for you to type one
action at a time using the same grammar an LLM would emit:

    TAP @e042                tap element by ref
    TAP "Add Alarm"          tap element by label substring
    TYPE @e017 "Hello"       tap-to-focus then type (auto-focus)
    SCROLL @e033 down 1.5    scroll inside an element
    ADJUST @e044 up 3        step a slider/picker/stepper
    SWIPE @e021 left         swipe an element (finger direction)
    SCROLL_PAGE down         CONTENT-direction page scroll (inverts
                             internally to the iOS-correct SWIPE)
    DONE "all set"           terminal — agent claims success
    FAIL "can't do it"       terminal — agent gives up
    ANSWER {"items": [...]}  terminal — reporting tasks: single-line JSON
                             payload checked by the agent_answer
                             verifier kind. The per-task instruction
                             declares the exact shape expected.

    OBSERVE                  re-read screen without acting
    HELP                     show this list
    QUIT                     bail out without running the AFTER verifier

Action execution goes through `XCUITestReader.tap / type_text / swipe`
(same backend as the watcher), not the legacy idb path baked into
`ActionExecutor`. SCROLL falls back to whole-app directional swipe.
ADJUST is not yet supported on XCUITest path.

Usage:
    /Library/Developer/CommandLineTools/usr/bin/python3 \\
      sibb/benchmark/sibb_replay.py <UDID> [--seed N] [--bundle <bid>]

Currently only the Reminders task generator (gen_reminders_list) is
wired up — that's the one with a working verifier.
"""

import argparse
import asyncio
import os
import random
import sys
import time
from datetime import datetime
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "simulator"))

from sibb_task_generator_v3 import (
    gen_reminders_list,
    gen_complete_specific_reminder,
    gen_uncomplete_reminder,
    gen_add_reminder_to_existing_list,
    gen_set_priority,
    gen_set_due_date_on_reminder,
    gen_change_due_date,
    gen_complete_all_overdue,
    gen_add_notes_to_reminder,
    gen_clear_completed_only,
    gen_complete_all_in_list,
    gen_delete_specific_reminder,
    gen_delete_entire_list,
    gen_move_reminder_between_lists,
    gen_rename_reminder,
    gen_make_reminder_recurring,
    gen_change_recurrence_frequency,
    gen_stop_recurrence,
    gen_create_recurring_with_due,
    gen_list_due_today,
    gen_list_due_tomorrow,
    gen_lookup_reminder_notes,
    # Calendar Tier 1
    gen_create_event_with_title_time,
    gen_delete_specific_event,
    gen_change_event_title,
    gen_set_event_location,
    gen_change_event_time,
    gen_toggle_event_all_day,
    # Calendar Tier 2/3
    gen_delete_all_events_on_date,
    gen_duplicate_event_to_next_week,
    gen_delete_events_in_calendar,
    gen_move_event_between_calendars,
    # Calendar Tier 4
    gen_reschedule_event_same_duration,
    gen_adjust_event_boundary,
    gen_add_notes_to_event,
    gen_add_event_url,
    # Calendar Tier 4b
    gen_make_event_recurring,
    gen_stop_event_recurrence,
    gen_change_event_recurrence_frequency,
    gen_create_recurring_event,
    # Calendar Tier 5
    gen_lookup_event_location,
    gen_list_events_today,
    gen_list_conflicting_events,
    gen_next_event_lookup,
    # Contacts Phase 2 v1 (2026-05-24)
    gen_set_contact_birthday,
    gen_set_contact_birthday_no_year,
    gen_add_second_phone_label,
    gen_create_contact_with_address,
    gen_full_business_card,
    gen_lookup_phone_by_name,
    # Phase 3 cross-app
    gen_reminder_with_calendar_event,
    gen_maps_search_to_contact,
    gen_message_save_sender,
    gen_message_save_sender_with_address,
    gen_message_save_body,
    gen_message_save_address,
    gen_message_to_contact_to_maps,
    gen_message_to_new_contact_to_maps,
    # Phase 4 Safari Tier-1
    gen_safari_bookmark_specific_url,
    gen_safari_rsvp_form,
    gen_safari_rsvp_form_clipped,
    gen_safari_shop_pick_by_attrs,
    gen_safari_shop_filter_and_sort,
)
from sibb_scaffold import (
    AXReader, AXEnricher, AXTokenizer, SIBBScaffold, AXTree, AXElement
)
from sibb_verify_reminders import (
    verify_reminders_list_task,
    verify_reminders_list_task_async,
)
from sibb_verify import (
    BaselineSnapshot, RESOURCE_FETCHERS,
    blocking_pass, legacy_format, run_checks,
)
from sibb_episode import _baseline_resources_for
from sibb_state import apply_initial_state, apply_pre_runner_setup


async def verify_generic_task_async(task, xcuitest_reader, *,
                                      context=None, baseline=None):
    """Generic verifier wrapper for any task that emits a populated
    `verify_checks` list. Drives `sibb_verify.run_checks` and translates
    the results back to the legacy `(passed, checks)` tuple shape used
    by `print_verify`. `context` carries per-episode runtime data
    (parsed ANSWER payload, observed bundles) when the task includes
    `agent_answer` checks; Tier 1 tasks don't need it. `baseline` is
    threaded to `identity`-kind checks (e.g. Calendar Tier 1's
    distractor-signature guards)."""
    results = await run_checks(xcuitest_reader, task.verify_checks,
                                 context=context, baseline=baseline)
    return blocking_pass(results), legacy_format(results)

# ── Terminal colours ─────────────────────────────────────────────────────────
R  = "\033[0m";  B  = "\033[1m"
CY = "\033[36m"; GR = "\033[32m"; YE = "\033[33m"
RE = "\033[31m"; GY = "\033[90m"; BL = "\033[34m"; WH = "\033[97m"


# Max repeats per SCROLL / SWIPE / SCROLL_PAGE action — the agent can't
# see intermediate state during a batch, so an over-large amount commits
# N gestures blindly before the next observation. Cap = 20 keeps each
# action bounded to ~20 wheel ticks of overshoot, ~screen-height of pan.
# Lifted to module scope so the SWIPE / SCROLL_PAGE executor can share
# the same ceiling as SCROLL.
SCROLL_MAX_AMOUNT = 20


GENERATORS = {
    "reminders_list":              (gen_reminders_list, verify_reminders_list_task_async),
    # Tier 1 — single-action Reminders tasks, generic verifier.
    "complete_specific_reminder":  (gen_complete_specific_reminder,
                                    verify_generic_task_async),
    "uncomplete_reminder":         (gen_uncomplete_reminder,
                                    verify_generic_task_async),
    "add_reminder_to_existing_list": (gen_add_reminder_to_existing_list,
                                      verify_generic_task_async),
    "set_priority":                (gen_set_priority,
                                    verify_generic_task_async),
    # Tier 4 — due-date / notes / mixed-state Reminders tasks.
    "set_due_date_on_reminder":    (gen_set_due_date_on_reminder,
                                    verify_generic_task_async),
    "change_due_date":             (gen_change_due_date,
                                    verify_generic_task_async),
    "complete_all_overdue":        (gen_complete_all_overdue,
                                    verify_generic_task_async),
    "add_notes_to_reminder":       (gen_add_notes_to_reminder,
                                    verify_generic_task_async),
    "clear_completed_only":        (gen_clear_completed_only,
                                    verify_generic_task_async),
    # Tier 2/3 — bulk + structural Reminders tasks.
    "complete_all_in_list":        (gen_complete_all_in_list,
                                    verify_generic_task_async),
    "delete_specific_reminder":    (gen_delete_specific_reminder,
                                    verify_generic_task_async),
    "delete_entire_list":          (gen_delete_entire_list,
                                    verify_generic_task_async),
    "move_reminder_between_lists": (gen_move_reminder_between_lists,
                                    verify_generic_task_async),
    "rename_reminder":             (gen_rename_reminder,
                                    verify_generic_task_async),
    # Tier 4b — recurrence-based Reminders tasks.
    "make_reminder_recurring":     (gen_make_reminder_recurring,
                                    verify_generic_task_async),
    "change_recurrence_frequency": (gen_change_recurrence_frequency,
                                    verify_generic_task_async),
    "stop_recurrence":             (gen_stop_recurrence,
                                    verify_generic_task_async),
    "create_recurring_with_due":   (gen_create_recurring_with_due,
                                    verify_generic_task_async),
    # Tier 5 — reporting Reminders tasks (agent_answer).
    "list_due_today":              (gen_list_due_today,
                                    verify_generic_task_async),
    "list_due_tomorrow":           (gen_list_due_tomorrow,
                                    verify_generic_task_async),
    "lookup_reminder_notes":       (gen_lookup_reminder_notes,
                                    verify_generic_task_async),
    # Calendar Tier 1 — single-action calendar tasks (Phase 2c).
    "create_event_with_title_time":(gen_create_event_with_title_time,
                                    verify_generic_task_async),
    "delete_specific_event":       (gen_delete_specific_event,
                                    verify_generic_task_async),
    "change_event_title":          (gen_change_event_title,
                                    verify_generic_task_async),
    "set_event_location":          (gen_set_event_location,
                                    verify_generic_task_async),
    "change_event_time":           (gen_change_event_time,
                                    verify_generic_task_async),
    "toggle_event_all_day":        (gen_toggle_event_all_day,
                                    verify_generic_task_async),
    # Calendar Tier 2/3 — bulk + structural (Phase 2c).
    "delete_all_events_on_date":   (gen_delete_all_events_on_date,
                                    verify_generic_task_async),
    "duplicate_event_to_next_week":(gen_duplicate_event_to_next_week,
                                    verify_generic_task_async),
    "delete_events_in_calendar":   (gen_delete_events_in_calendar,
                                    verify_generic_task_async),
    "move_event_between_calendars":(gen_move_event_between_calendars,
                                    verify_generic_task_async),
    # Calendar Tier 4 — time edits / notes / url (Phase 2c).
    "reschedule_event_same_duration":(gen_reschedule_event_same_duration,
                                     verify_generic_task_async),
    "adjust_event_boundary":       (gen_adjust_event_boundary,
                                    verify_generic_task_async),
    "add_notes_to_event":          (gen_add_notes_to_event,
                                    verify_generic_task_async),
    "add_event_url":               (gen_add_event_url,
                                    verify_generic_task_async),
    # Calendar Tier 4b — recurrence (Phase 2c).
    "make_event_recurring":        (gen_make_event_recurring,
                                    verify_generic_task_async),
    # Note: Reminders has a `stop_recurrence` key for its T4b stop
    # generator; Calendar uses `stop_event_recurrence` to disambiguate.
    "stop_event_recurrence":       (gen_stop_event_recurrence,
                                    verify_generic_task_async),
    "change_event_recurrence_frequency": (gen_change_event_recurrence_frequency,
                                          verify_generic_task_async),
    "create_recurring_event":      (gen_create_recurring_event,
                                    verify_generic_task_async),
    # Calendar Tier 5 — reporting via agent_answer (Phase 2c).
    "lookup_event_location":       (gen_lookup_event_location,
                                    verify_generic_task_async),
    "list_events_today":           (gen_list_events_today,
                                    verify_generic_task_async),
    "list_conflicting_events":     (gen_list_conflicting_events,
                                    verify_generic_task_async),
    "next_event_lookup":           (gen_next_event_lookup,
                                    verify_generic_task_async),
    # Contacts Phase 2 v1 (2026-05-24).
    "set_contact_birthday":        (gen_set_contact_birthday,
                                    verify_generic_task_async),
    "set_contact_birthday_no_year": (gen_set_contact_birthday_no_year,
                                     verify_generic_task_async),
    "add_second_phone_label":      (gen_add_second_phone_label,
                                    verify_generic_task_async),
    "create_contact_with_address": (gen_create_contact_with_address,
                                    verify_generic_task_async),
    "full_business_card":          (gen_full_business_card,
                                    verify_generic_task_async),
    "lookup_phone_by_name":        (gen_lookup_phone_by_name,
                                    verify_generic_task_async),
    # Phase 3 cross-app
    "reminder_with_calendar_event":
                                   (gen_reminder_with_calendar_event,
                                    verify_generic_task_async),
    "maps_search_to_contact":      (gen_maps_search_to_contact,
                                    verify_generic_task_async),
    "message_save_sender":         (gen_message_save_sender,
                                    verify_generic_task_async),
    "message_save_body":           (gen_message_save_body,
                                    verify_generic_task_async),
    "message_save_address":        (gen_message_save_address,
                                    verify_generic_task_async),
    "message_to_contact_to_maps":  (gen_message_to_contact_to_maps,
                                    verify_generic_task_async),
    "message_to_new_contact_to_maps":
                                   (gen_message_to_new_contact_to_maps,
                                    verify_generic_task_async),
    "message_save_sender_with_address":
                                   (gen_message_save_sender_with_address,
                                    verify_generic_task_async),
    # Safari Tier 1 — single-app bookmark create (Phase 4)
    "safari_bookmark_specific_url":
                                   (gen_safari_bookmark_specific_url,
                                    verify_generic_task_async),
    # Safari Tier 1 — first harness-served form-fill (Phase 4)
    "safari_rsvp_form":            (gen_safari_rsvp_form,
                                    verify_generic_task_async),
    # Adversarial: submit button positioned past viewport edges.
    "safari_rsvp_form_clipped":    (gen_safari_rsvp_form_clipped,
                                    verify_generic_task_async),
    # Step 5M (2026-06-08) — shop flow V0: search → pick → checkout.
    "safari_shop_pick_by_attrs":   (gen_safari_shop_pick_by_attrs,
                                    verify_generic_task_async),
    # Step 5P (2026-06-09) — Q4 shop flow: filter + sort cascade.
    "safari_shop_filter_and_sort": (gen_safari_shop_filter_and_sort,
                                    verify_generic_task_async),
}


def banner(text: str, color: str = B):
    line = "═" * 70
    print(f"\n{color}{line}{R}\n{color}  {text}{R}\n{color}{line}{R}")


def print_task(task):
    banner(f"Task: {task.task_id}", B + CY)
    print(f"\n{B}Instruction:{R}\n  {task.instruction}\n")
    print(f"{B}Apps:{R}        {', '.join(task.apps)}")
    print(f"{B}Steps:{R}       ~{task.steps}")
    print(f"{B}Complexity:{R}  {task.complexity}")
    print(f"{B}Params:{R}")
    for k, v in task.params.items():
        if v is not None and v != []:
            print(f"  {k}: {v}")


def print_verify(passed, checks, when: str):
    color = GR if passed else RE
    icon  = "✅" if passed else "❌"
    label = "PASS (1.0)" if passed else "FAIL (0.0)"
    print(f"\n{color}{B}  {icon} Verifier {when}: {label}{R}")
    for chk_label, ok in checks:
        if ok is None:
            print(f"     {GY}–  {chk_label}{R}")
        elif ok:
            print(f"     {GR}✓  {chk_label}{R}")
        else:
            print(f"     {RE}✗  {chk_label}{R}")


HELP = f"""
{B}Action grammar{R}
  {GR}TAP{R} @e042                  tap element by ref
  {GR}TAP{R} "Add Alarm"            tap element by label substring
  {GR}TAP{R} (200, 400)             raw-coordinate tap (no AX lookup)
  {GR}DOUBLE_TAP{R} (200, 100)       coordinate double-tap — primary
                              use is resetting Safari's auto-zoom on
                              a non-input page region. Also accepts
                              @ref / "label" like TAP.
  {GR}TYPE{R} @e017 "Hello world"   tap-to-focus then type
  {GR}SCROLL{R} @e042 down 2        pan a scrollable element (table/scroll/
                              web). @ref is REQUIRED — bare SCROLL
                              errors. Use SWIPE for whole-screen gestures.
  {GR}FLING{R} @e042 down 1          fast-velocity element-bounded gesture
  {GR}SWIPE{R} left                 whole-screen system gesture (page-flip,
                              Spotlight, Control Center, app switcher).
                              `direction` = FINGER direction.
  {GR}SWIPE{R} @e042 left           element-bounded swipe (finger direction)
  {GR}SCROLL_PAGE{R} down           CONTENT-direction page scroll. SCROLL_PAGE
                              down = reveal lower content (emits SWIPE up
                              internally). Use this when you want to think
                              in content terms.
  {GR}PRESS{R} home                 exit to home screen
  {GR}PRESS{R} back                 in-app back gesture (left-edge swipe)
  {GR}PRESS{R} app_switcher         recent-apps carousel (swipe-up-and-hold)
  {GR}DONE{R} "completed"           terminal success (state-only tasks)
  {GR}FAIL{R} "stuck"               terminal failure
  {GR}ANSWER{R} {{"key": ...}}        terminal — reporting tasks; payload is
                              single-line JSON. The task instruction
                              tells you the exact shape.

{B}Meta commands{R}
  {CY}OBSERVE{R}                     re-read without acting
  {CY}MAPSDB{R}                      dump last 8 Maps ZHISTORYITEM rows
                              (look for z_ent=16=DIRECTIONS before DONE
                              — Maps writes async, can lag 5-15s after
                              the UI action)
  {CY}CONTACTSDB{R}                  dump all Contacts + addresses
                              (verify your address-saving actions
                              reached the AddressBook DB)
  {CY}HELP{R}                        this list
  {CY}QUIT{R}                        bail out (skip AFTER verifier)
"""


def _swipe_coords_for_finger_direction(frame, finger_direction: str,
                                          short_distance: Optional[float] = None):
    """Compute (x1, y1, x2, y2) for a swipe-inside-element gesture.

    `frame` is an AXElement-style frame with `.center_x`, `.center_y`,
    `.x`, `.y`, `.width`, `.height` attributes (matches sibb_scaffold's
    AXFrame). `finger_direction` is the literal direction the finger
    moves: "up" = drag from near-bottom to near-top, etc.

    Default behavior: 80% of element's height/width as the drag
    amplitude. Suitable for ScrollViews and carousels.

    When `short_distance` is set (in pixels), the gesture is exactly
    that long, centered on the element. Used for picker wheels — a
    full-element drag at 0.05s reads as a fling and spins the wheel
    many ticks; a one-row drag (~36px) at 0.3-0.4s settles to ~1 tick.

    SCROLL's content-vs-finger inversion is handled in the caller
    (`execute()` maps SCROLL down → finger up before calling here).
    """
    cx = frame.center_x
    cy = frame.center_y
    left = frame.x
    top = frame.y
    right = left + frame.width
    bottom = top + frame.height

    if short_distance is not None:
        half = short_distance / 2.0
        if finger_direction == "up":
            return cx, cy + half, cx, cy - half
        if finger_direction == "down":
            return cx, cy - half, cx, cy + half
        if finger_direction == "left":
            return cx + half, cy, cx - half, cy
        if finger_direction == "right":
            return cx - half, cy, cx + half, cy
        return cx, cy, cx, cy

    # 10% inset from each edge → 80% amplitude (default for scroll views).
    h_inset = frame.height * 0.10
    w_inset = frame.width * 0.10

    if finger_direction == "up":
        return cx, bottom - h_inset, cx, top + h_inset
    if finger_direction == "down":
        return cx, top + h_inset, cx, bottom - h_inset
    if finger_direction == "left":
        return right - w_inset, cy, left + w_inset, cy
    if finger_direction == "right":
        return left + w_inset, cy, right - w_inset, cy
    return cx, cy, cx, cy


def find_element(action, tree: AXTree) -> Optional[AXElement]:
    if action.target_ref:
        el = tree.find_by_ref(action.target_ref)
        if el:
            return el
    if action.target_label:
        candidates = tree.find_by_label(action.target_label)
        if candidates:
            enabled = [c for c in candidates if c.enabled]
            pool = enabled or candidates
            return _disambiguate_by_label(pool, action.target_label, tree)
    return None


# Roles that are interactive surfaces — preferred over decorative/text
# elements when multiple labels collide (Step 3 fallout: prediction
# words now reach the tree as StaticText/Other and collide on short
# substrings). Include BOTH the lowercased ElementRole enum values
# (production path post-role-mapping) AND raw XCUITest lowercase
# strings (in case a caller hits us pre-mapping).
_INTERACTIVE_ROLES_FOR_DISAMBIG = frozenset({
    # ElementRole enum values, lowercased
    "button", "textfield", "textview", "cell", "switch", "tab",
    "picker", "adjustable",
    # raw XCUITest strings
    "btn", "input", "textarea", "link", "search",
    "pickerwheel",
})


def _disambiguate_by_label(candidates, label, tree):
    """Pick the best candidate when label-match returns multiple.

    Two ambiguities Step 3 introduced (the accessory bar now reaches
    the agent):
      A. Multi-`Done`: sheet/nav confirmation Done at top of sheet
         (small y, role=btn) vs accessory-bar Done at kb top.
      B. Common-word predictions: prediction words like "I" / "The" /
         "and" come through as `[other]` / `[StaticText]`. They can
         poison short-substring searches.

    Rules:
      1. If kb is up AND `target_label` matches an accessory-bar
         button label exactly (case-insensitive), prefer the candidate
         OUTSIDE the accessory bar (frame.y + height ≤ accessory top).
         Falls back to the bar Done if no non-bar match.
      2. Prefer candidates whose role is in `_INTERACTIVE_ROLES_FOR_DISAMBIG`
         (button/input/cell/etc.) over StaticText/Other — handles the
         prediction-word collision.
      3. Otherwise return the first candidate (legacy behavior).

    Pre-Step-3 there was no ambiguity to resolve because the bar's
    elements were filtered out before reaching the tree. Keeping
    behavior identical for single-match cases avoids regressing
    existing tasks."""
    if len(candidates) <= 1:
        return candidates[0] if candidates else None

    target = (label or "").strip().lower()
    acc_frame = getattr(tree, "accessory_bar_frame", None)

    # Rule 1 — multi-Done / Next / Previous when kb is up.
    if (acc_frame and target in {"done", "next", "previous"}):
        acc_top = acc_frame.get("y")
        if acc_top is not None:
            # Prefer candidates whose entire frame sits ABOVE the
            # accessory bar (= sheet/nav button, not the bar's button).
            non_bar = [c for c in candidates
                       if c.frame and c.frame.y + c.frame.height
                            <= acc_top]
            if non_bar:
                return non_bar[0]
            # All candidates are at the bar. Use the bar's Done.

    # Rule 2 — prefer interactive roles for short / common queries
    # (prediction words usually come through as StaticText / Other).
    if len(target) <= 3:
        interactive = [c for c in candidates
                       if (getattr(c, "role", None)
                            and (c.role.value.lower()
                                 if hasattr(c.role, "value")
                                 else str(c.role).lower())
                                in _INTERACTIVE_ROLES_FOR_DISAMBIG)]
        if interactive:
            return interactive[0]

    return candidates[0]


async def _wait_for_focus_at(xc, tap_x: float, tap_y: float,
                               timeout_s: float = 1.5,
                               poll_interval_s: float = 0.12) -> bool:
    """Poll until the focused element's frame contains the given tap
    point, or `timeout_s` elapses. Returns True if focus was confirmed
    (the next typeText will land on the targeted element), False on
    timeout (caller types anyway as a fallback).

    Cheap when focus settles fast (poll loop exits on first hit;
    typical 100-300ms for in-app navigation). Bounded when slow
    (~1.5s ceiling for stubborn iOS sheets). Bypassable: if the AX
    snapshot doesn't expose the focused element (some apps don't),
    we fall through to the timeout — same outcome as the previous
    flat-sleep approach."""
    import time as _time
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        await asyncio.sleep(poll_interval_s)
        try:
            tree = await xc.observe()
        except Exception:
            continue
        for e in tree.elements:
            if not getattr(e, "focused", False):
                continue
            fr = getattr(e, "frame", None)
            if fr is None:
                continue
            if (fr.x <= tap_x <= fr.x + fr.width
                    and fr.y <= tap_y <= fr.y + fr.height):
                return True
    return False


async def execute(reader: AXReader, action, tree: AXTree) -> dict:
    """
    XCUITest-backed action executor — bypasses the idb-only ActionExecutor.
    Uses reader._xcuitest (the XCUITestReader) for tap / type / swipe.
    """
    xc = reader._xcuitest
    a = action.action_type

    if a in ("done", "fail"):
        return {"success": True, "terminal": True, "reason": action.reason}

    if a == "observe":
        # Pure no-op: sleep for the requested ms (parser clamps to
        # [0, 10000]) and return. The top-of-turn loop in the driver
        # re-observes naturally on the next iteration — no need to
        # call observe() ourselves. The agent uses this when an async
        # UI process is mid-flight (Maps "Loading…" while routes
        # compute, network spinners) and TAP/SCROLL would interrupt
        # background work.
        wait_ms = float(action.amount or 0.0)
        if wait_ms > 0:
            import asyncio
            await asyncio.sleep(wait_ms / 1000.0)
        return {"success": True, "slept_ms": int(wait_ms),
                "note": "OBSERVE — pure no-op, fresh AX on next turn"}

    if a == "return":
        # Fire the Return key into the currently keyboard-focused
        # element via Swift's `app.typeText("\n")`. iOS dispatches the
        # event against the focused field's keyboard configuration,
        # firing whatever the Return key is contextually labeled —
        # Go / Search / Done / Next / plain Return. We don't need to
        # know which; the field does the right thing on receiving "\n".
        #
        # PRIMARY USE: commit a typed URL in Safari's URL bar (the
        # keyboard's Return key isn't surfaced in AX, so the agent has
        # no other way to commit — tapping a suggestion submits as
        # search instead).
        #
        # Step 5L-C (2026-06-08) — Swift returns a structured
        # `{ok: false, error: "no_keyboard", hint: "..."}` when the
        # keyboard isn't visible. Surface that to the agent so it
        # learns to TAP-to-focus first.
        resp = await xc._send({"type": "return"})
        if not resp.get("ok", True):
            err = resp.get("error", "unknown")
            hint = resp.get("hint", "")
            return {"success": False,
                    "error": f"RETURN failed: {err}",
                    "hint": hint,
                    "note": "RETURN requires a focused text input"}
        return {"success": True,
                "note": "RETURN fired \\n into focused element"}

    if a == "answer":
        # ANSWER is terminal — payload is on the action itself (parsed
        # by SIBBScaffold.parse_action). Captured by the run-task loop
        # below and threaded into the verifier via the agent.answer
        # resource. Surfaces parse_error so the demo can see when the
        # JSON was malformed.
        return {
            "success": action.parse_error is None,
            "terminal": True,
            "answer_payload": action.answer_payload,
            "parse_error": action.parse_error,
        }

    if a == "tap":
        # Raw-coordinate tap takes precedence — bypasses element lookup
        # and the enabled/frame checks entirely.
        if action.target_x is not None and action.target_y is not None:
            x, y = action.target_x, action.target_y
            await xc.tap(x=x, y=y)
            return {"success": True, "coords": (round(x), round(y)),
                    "note": "raw coordinate tap (no AX lookup)"}
        el = find_element(action, tree)
        if not el:
            return {"success": False, "error": f"element not found "
                    f"(ref={action.target_ref!r} label={action.target_label!r})"}
        if not el.enabled:
            return {"success": False, "error": f"@{el.ref} '{el.effective_label}' is disabled"}
        if not el.frame:
            return {"success": False, "error": f"@{el.ref} has no frame"}
        x, y = el.frame.center_x, el.frame.center_y
        await xc.tap(x=x, y=y)
        return {"success": True, "coords": (round(x), round(y)),
                "ref": el.ref, "label": el.effective_label}

    if a == "double_tap":
        # Coordinate-based double-tap. Dispatches via Swift's native
        # `XCUICoordinate.doubleTap()` — the gesture path that fires
        # WebKit's double-tap-to-zoom recognizer on Safari (the agent's
        # only reliable auto-zoom reset). Same target-resolution
        # semantics as TAP: raw coord > @ref > label match.
        #
        # The xc.double_tap() raises RuntimeError on `ok:false` (older
        # SIBBHelper builds return `unknown:double_tap` from the
        # default case). We catch and surface as a structured result
        # so the turn loop doesn't abort the episode.
        if action.target_x is not None and action.target_y is not None:
            x, y = action.target_x, action.target_y
            try:
                await xc.double_tap(x=x, y=y)
            except RuntimeError as e:
                return {"success": False,
                         "error": (f"DOUBLE_TAP dispatch failed: {e}. "
                                   f"If the SIBBHelper build is older "
                                   f"than 2026-06-06, rebuild via "
                                   f"./sibb_xcuitest_setup.sh <UDID>.")}
            return {"success": True, "coords": (round(x), round(y)),
                    "note": "raw coordinate double-tap"}
        el = find_element(action, tree)
        if not el:
            return {"success": False,
                     "error": f"element not found "
                              f"(ref={action.target_ref!r} "
                              f"label={action.target_label!r})"}
        if not el.frame:
            return {"success": False,
                     "error": f"@{el.ref} has no frame"}
        # Consistency with TAP: refuse disabled elements. For the
        # PRIMARY USE (Safari zoom reset by coord), this branch is
        # not reached. For @ref / label dispatch on a button/link,
        # disabled means "no action would occur" and surfacing the
        # failure is preferable to silent no-op.
        if not el.enabled:
            return {"success": False,
                     "error": (f"@{el.ref} '{el.effective_label}' is "
                               f"disabled")}
        x, y = el.frame.center_x, el.frame.center_y
        try:
            await xc.double_tap(x=x, y=y)
        except RuntimeError as e:
            return {"success": False,
                     "error": (f"DOUBLE_TAP dispatch failed: {e}. "
                               f"If the SIBBHelper build is older "
                               f"than 2026-06-06, rebuild via "
                               f"./sibb_xcuitest_setup.sh <UDID>.")}
        return {"success": True, "coords": (round(x), round(y)),
                 "ref": el.ref, "label": el.effective_label}

    if a == "type":
        # Two paths:
        # 1. TYPE @ref "text" (auto-focus): use the atomic Swift
        #    tap_then_type command. Swift taps, polls until focus
        #    transfers, THEN types. If focus never transfers (target
        #    is invisible, off-screen, occluded by keyboard, or just
        #    not focusable), Swift returns ok=false without typing —
        #    no keystroke leak.
        # 2. TYPE "text" (no @ref / no label): legacy raw typeText,
        #    goes to whatever's currently focused. For when the agent
        #    explicitly already has focus and just wants to type.
        if action.target_ref or action.target_label:
            el = find_element(action, tree)
            if not el:
                return {"success": False, "error": "target field not found"}
            if not el.frame:
                return {"success": False, "error": "target field has no frame"}
            tap_x, tap_y = el.frame.center_x, el.frame.center_y
            # Pre-check: tap coord must not be under the keyboard or
            # off-screen. The Swift side would still register the tap
            # but no input would receive focus.
            kb_frame = getattr(tree, "keyboard_frame", None)
            screen_w = getattr(tree, "screen_width", 402)
            screen_h = getattr(tree, "screen_height", 874)
            # `keyboard_y_min` is just kb_frame.y as of 2026-06-06.
            # Used to be a union with `accessory_bar_frame.y` so the
            # check also rejected taps onto the predictive bar / kb
            # accessory toolbar. We dropped the union when an empirical
            # probe proved the bar elements (`Done`/`Next`/`Previous`/
            # prediction words) are useful labeled buttons the agent
            # SHOULD be able to tap. They appear in the AX tree as
            # first-class elements; replay routes a TAP/DOUBLE_TAP at
            # the bar's coords to that element directly.
            kb_y_min = getattr(tree, "keyboard_y_min", None)
            if kb_y_min is None and kb_frame is not None:
                kb_y_min = kb_frame.get("y")
            el_focused = bool(getattr(el, "focused", False))
            if tap_x < 0 or tap_x > screen_w or tap_y < 0 or tap_y > screen_h:
                return {"success": False,
                         "error": (f"target field's center "
                                   f"({tap_x:.0f},{tap_y:.0f}) is "
                                   f"off-screen — scroll into view first"),
                         "kb_y_min_used": kb_y_min}
            # Focused-element exemption: the scaffold exempts focused
            # fields from the visibility filter so the agent always
            # sees "what am I typing into". The pre-tap occlusion check
            # must be symmetric — but tapping below-kb coords would
            # still route the tap into the keyboard. So when the field
            # is already focused, skip the tap entirely and route to
            # raw type_text (the kb already has the field's responder).
            if kb_y_min is not None and tap_y >= kb_y_min:
                if not el_focused:
                    return {"success": False,
                             "error": (f"target field's center is below "
                                       f"the keyboard (y={tap_y:.0f} >= "
                                       f"kb_top={kb_y_min:.0f}); "
                                       f"scroll the form or dismiss the "
                                       f"keyboard first"),
                             "kb_y_min_used": kb_y_min}
                # Focused + below-kb: type into the live responder.
                if not action.text:
                    return {"success": True, "typed": "",
                             "note": "empty text; no-op (focused)"}
                # TOCTOU guard: the agent's tree was captured at
                # observation time; between observe and TYPE, the
                # simulator can drop / move focus (animation, modal,
                # auto-Return-press on a prior turn). type_text dispatches
                # to whatever's currently focused — without this check,
                # we could leak keystrokes into the wrong field. Re-
                # observe and verify the live focused element's frame
                # still contains the originally-targeted tap point. If
                # it doesn't, refuse to type and tell the agent.
                live = await xc.observe()
                live_focused = next(
                    (e for e in live.elements
                     if getattr(e, "focused", False) and e.frame), None)
                # `live.elements[].frame` is the xcuitest_client Frame
                # type (no .contains method) — inline the bbox check.
                def _frame_contains(fr, x, y):
                    return (fr.x <= x <= fr.x + fr.width
                            and fr.y <= y <= fr.y + fr.height)
                if (live_focused is None
                        or not _frame_contains(
                            live_focused.frame, tap_x, tap_y)):
                    live_focused_label = (
                        getattr(live_focused, "label", None)
                        if live_focused is not None else None)
                    return {
                        "success": False,
                        "error": (
                            f"TYPE @{el.ref} aborted: between observation "
                            f"and TYPE, focus moved off the target field. "
                            f"Re-observe and re-tap. Current focused "
                            f"element: {live_focused_label!r}."),
                        "kb_y_min_used": kb_y_min,
                        "focus_moved": True,
                    }
                await xc.type_text(text=action.text)
                return {"success": True, "typed": action.text,
                         "note": ("typed into already-focused field; "
                                  "tap omitted because field is below "
                                  "the keyboard (this is success)"),
                         "kb_y_min_used": kb_y_min}
            if not action.text:
                return {"success": True, "typed": "",
                         "note": "empty text; no-op"}
            resp = await xc.tap_then_type(x=tap_x, y=tap_y,
                                            text=action.text)
            if not resp.get("ok"):
                # Policy A: focus didn't transfer → don't type, return
                # a clean failure so the agent knows to recover (re-
                # tap, scroll, dismiss kb).
                err_kind = resp.get("error", "tap_then_type failed")
                msg = (f"TYPE @{el.ref} failed: {err_kind}. "
                       f"The target element couldn't receive focus "
                       f"within 1.5s. Common causes: the element is "
                       f"covered by another view (keyboard, modal, "
                       f"overlay); the element isn't actually a focus-"
                       f"receiver (it's a label or image, not an "
                       f"input); the element scrolled out of view "
                       f"between observation and tap; the parent view "
                       f"intercepted the tap. Recovery: re-observe, "
                       f"scroll the field into clear view, dismiss "
                       f"the keyboard if blocking, or pick a different "
                       f"target.")
                return {"success": False,
                         "error": msg,
                         "polled_ms": resp.get("polled_ms"),
                         "focused_frame": resp.get("focused_frame"),
                         "ref": el.ref, "label": el.effective_label}
            return {"success": True, "ref": el.ref,
                     "label": el.effective_label,
                     "coords": (round(tap_x), round(tap_y)),
                     "typed": action.text,
                     "focus_acquired_ms": resp.get("acquired_ms")}
        # No @ref / label → raw typeText to currently-focused element.
        if action.text:
            await xc.type_text(action.text)
        return {"success": True, "typed": action.text,
                 "note": ("raw typeText to currently-focused element "
                           "(no @ref provided)")}

    if a == "clear":
        # CLEAR @ref — wipe the current value of a text field.
        #
        # Strategy (research-validated against Appium WebDriverAgent
        # PR #248, Hyperwallet/Blackjacx Swift gists, Appium issues
        # #13046 and #20088):
        #
        # 1. Tap at the field's RIGHT EDGE (frame.x + width - margin,
        #    center_y), not the center. iOS positions the cursor at
        #    the character closest to the tap; tapping the center of
        #    a populated field puts the cursor mid-text, so N backward
        #    deletes would only clear the LEFT half. Tapping the right
        #    edge anchors the cursor at the end of the text, where
        #    backward deletes wipe everything.
        #
        # 2. Send min(N + 5, 24) backspaces where N = current value
        #    length. The +5 covers race-condition residue. The 24 cap
        #    is per Appium #20088: on iOS 17+ Simulator, sending more
        #    than ~25 delete keys to an already-empty TextField can
        #    crash the keyboard service.
        #
        # 3. Short-circuit if the field is already empty — saves the
        #    round-trip and avoids the unnecessary tap.
        #
        # 4. For long fields (N > 19, so N + 5 > 24), Swift returns
        #    stopped_early=True; the agent sees it in the result and
        #    can issue another CLEAR.
        el = find_element(action, tree)
        if not el:
            return {"success": False,
                     "error": f"element not found "
                              f"(ref={action.target_ref!r} "
                              f"label={action.target_label!r})"}
        if not el.frame:
            return {"success": False,
                     "error": f"@{el.ref} has no frame"}
        # Short-circuit: empty field is already cleared.
        current = el.value or ""
        if not current:
            return {"success": True, "ref": el.ref,
                    "label": el.effective_label,
                    "length_hint": 0, "deletes_sent": 0,
                    "note": "field was already empty; no-op"}
        # Right-edge tap. Clamp `width - margin` so the tap point
        # never goes to the LEFT of the field's left edge on narrow
        # fields (e.g., a 30px-wide input would give right-edge tap
        # at x=22 with margin=8, well to the right of center).
        margin = 8.0
        tap_x = max(el.frame.x + 1.0,
                     el.frame.x + el.frame.width - margin)
        tap_y = el.frame.center_y
        length_hint = len(current)
        try:
            resp = await xc.clear_text(x=tap_x, y=tap_y,
                                         length_hint=length_hint)
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "ref": el.ref,
                "label": el.effective_label,
                "coords": (round(tap_x), round(tap_y)),
                "length_hint": length_hint,
                "deletes_sent": resp.get("deletes_sent"),
                "stopped_early": resp.get("stopped_early", False)}

    if a == "swipe":
        # If a target element is specified, swipe BETWEEN coordinates
        # bounded by that element (uses Swift swipe_at). Otherwise
        # fall back to whole-app swipe.
        # SWIPE direction is finger direction (drag direction), not
        # content direction.
        direction = (action.direction or "left").lower()
        # SWIPE/SCROLL_PAGE may carry an `amount` (number of repeats),
        # cap matching SCROLL to keep the agent's blind-batch bounded.
        amount = int(action.amount or 1)
        capped = False
        if amount > SCROLL_MAX_AMOUNT:
            amount = SCROLL_MAX_AMOUNT
            capped = True
        amount = max(1, amount)
        el = find_element(action, tree) if (
            action.target_ref or action.target_label) else None
        if el and el.frame:
            x1, y1, x2, y2 = _swipe_coords_for_finger_direction(
                el.frame, direction)
            for _ in range(amount):
                await xc.swipe_at(x1, y1, x2, y2)
            result = {"success": True, "direction": direction,
                       "ref": el.ref, "label": el.effective_label,
                       "from": (round(x1), round(y1)),
                       "to": (round(x2), round(y2)),
                       "swipes": amount}
            if capped:
                result["capped"] = True
                result["requested_swipes"] = int(action.amount or 1)
            return result
        for _ in range(amount):
            await xc.swipe(direction=direction)
        result = {"success": True, "direction": direction,
                   "swipes": amount,
                   "note": "whole-app swipe (no element ref)"}
        if capped:
            result["capped"] = True
            result["requested_swipes"] = int(action.amount or 1)
        return result

    if a == "pinch":
        # Two-finger pinch on the whole-app frame. Used primarily as
        # the recovery for Safari's auto-zoom on input focus — see
        # `IOS_SIM_QUIRKS §21`. The parser leaves either:
        #   * `direction in ("out", "in")` — we map to canonical
        #     scale (0.5 for out, 2.0 for in); OR
        #   * `amount` carrying an explicit scale (e.g. 0.6).
        # `amount=1.0` is the parser's default sentinel; when there's
        # no direction AND amount is exactly 1.0, treat as `out`.
        direction = (action.direction or "").lower()
        scale: float
        if action.amount is not None and action.amount != 1.0:
            scale = float(action.amount)
        elif direction == "in":
            scale = 2.0
        else:
            scale = 0.5  # default & explicit "out"
        # Velocity 1.0 scale/second is iOS' documented sane default.
        resp = await xc.pinch(scale=scale, velocity=1.0)
        return {
            "success": True,
            "scale": scale,
            "direction": direction or ("out" if scale < 1.0 else "in"),
            "note": ("whole-app pinch; iOS routes to focused scroll-"
                     "view (WKWebView for Safari)"),
        }

    if a == "scroll":
        # SCROLL is content-direction (down = see more content below),
        # which is the OPPOSITE finger gesture (drag finger upward).
        # amount = number of swipes (capped at SCROLL_MAX_AMOUNT to
        # prevent runaway batch actions — the agent can't see intermediate
        # state during a batch, so an over-large amount commits N swipes
        # blindly before the next observation. Cap = 10 keeps each action
        # bounded to ~10 wheel ticks of overshoot).
        #
        # SCROLL REQUIRES @ref (2026-06-03 design).
        # Bare SCROLL was previously a whole-screen swipe — but that's
        # what SWIPE is for. SCROLL is for panning a scrollable element
        # (UIScrollView / UITableView / UICollectionView / WKWebView).
        # The agent must name the scrollable element so iOS treats the
        # gesture as a content pan, not a chrome interaction (URL bar,
        # tab strip, system gesture). For whole-screen gestures
        # (Spotlight, Control Center, Notification Center, app switcher,
        # page-flip) use SWIPE direction.
        if not (action.target_ref or action.target_label):
            return {
                "success": False,
                "error": (
                    "SCROLL requires an element reference. Use "
                    "`SCROLL @<ref> <direction>` to pan a scrollable "
                    "element (UIScrollView / table / WebView). "
                    "For whole-screen gestures (Spotlight, Control "
                    "Center, page-flip, app switcher) use "
                    "`SWIPE <direction>` instead."
                ),
                "scroll_dir": (action.direction or "down").lower(),
            }
        SCROLL_TO_FINGER = {
            "down":  "up",     # see content below → drag finger up
            "up":    "down",
            "left":  "right",  # see content to the right → drag right
            "right": "left",
        }
        # Unified cap for SCROLL: 20 swipes/action regardless of element
        # type. Adjustable swipes are slow (settle each, ~2.16s) →
        # ~43s worst case. Non-adjustable batches are fast (settle
        # only on last, ~0.05s each) → ~3s worst case. The asymmetric
        # wall-clock falls out of the gesture mode, not the cap.
        #
        # Per-swipe socket recv timeout is 60s; each swipe_at is its
        # own round-trip, so the cap bounds the agent's per-turn
        # budget, not the socket. For larger jumps the agent should
        # use FLING (max 3, each ~20-30 ticks). SCROLL_MAX_AMOUNT is
        # module-level so SWIPE / SCROLL_PAGE share the same ceiling.
        scroll_dir = (action.direction or "down").lower()
        finger_dir = SCROLL_TO_FINGER.get(scroll_dir, "up")
        n_req = max(1, int(action.amount or 1))
        # Resolve the target element BEFORE capping for use later.
        target_el = find_element(action, tree) if (
            action.target_ref or action.target_label) else None
        target_is_adj = bool(target_el
                              and getattr(target_el, "adjustable", False))
        n = min(n_req, SCROLL_MAX_AMOUNT)
        capped = n != n_req

        # Element-targeted scroll if action references an element.
        # We already resolved `target_el` above for the cap decision;
        # reuse it here.
        el = target_el
        if el and el.frame:
            # Adjustable elements (picker wheels, sliders, steppers) need
            # a DRAG, not a fling. iOS interprets a fast 80%-frame swipe
            # as a fling → wheel spins many ticks. A slow short drag
            # (~one row, ~0.4s) settles to ~1 tick per swipe. Use
            # settle=True on every swipe so the wheel decelerates
            # cleanly between swipes (the deceleration IS the per-tick
            # discretization).
            is_adjustable = target_is_adj
            if is_adjustable:
                # Picker wheels need:
                #   - Short drag distance (~one row, ~40px) so the
                #     gesture spans ~1 tick of the wheel
                #   - SLOW velocity (250 px/s = .slow per Apple's
                #     XCUIGestureVelocity enum) so iOS does NOT
                #     interpret the gesture as a fling — flings spin
                #     the wheel through many ticks with deceleration.
                # XCUITest's vanilla press(forDuration:thenDragTo:)
                # ignores duration_s for drag SPEED (it's pre-hold time
                # only) and drags at ~1000 px/s. We need the
                # withVelocity: variant — plumbed via velocity_pps.
                x1, y1, x2, y2 = _swipe_coords_for_finger_direction(
                    el.frame, finger_dir, short_distance=28.0)
                for _ in range(n):
                    await xc.swipe_at(x1, y1, x2, y2,
                                        duration_s=0.0, settle=True,
                                        velocity_pps=180.0)
            else:
                x1, y1, x2, y2 = _swipe_coords_for_finger_direction(
                    el.frame, finger_dir)
                # Batch swipes: settle=False for swipes 1..N-1,
                # settle=True on the LAST. Wheel-deceleration
                # animations cause per-swipe waitForSettle to hang
                # for 2s each — an N=10 batch would burn 20s+. With
                # this: ~50ms per intermediate swipe + 2s settle at end.
                for i in range(n):
                    is_last = (i == n - 1)
                    await xc.swipe_at(x1, y1, x2, y2, settle=is_last)
            return {"success": True, "scroll_dir": scroll_dir,
                    "finger_dir": finger_dir, "swipes": n,
                    "requested_swipes": n_req,
                    "capped": capped,
                    "ref": el.ref, "label": el.effective_label,
                    "from": (round(x1), round(y1)),
                    "to": (round(x2), round(y2)),
                    "note": (
                        "element-targeted scroll" if not capped else
                        f"SCROLL capped from {n_req} to "
                        f"{SCROLL_MAX_AMOUNT}. Max SCROLL is "
                        f"{SCROLL_MAX_AMOUNT} swipes/turn. For larger "
                        f"jumps use FLING (each fling ≈ 20-30 ticks)."
                    )}

        # If we got here, the action had a ref/label but the element
        # couldn't be resolved (off-screen, stale snapshot) or it has
        # no frame. We deliberately do NOT fall back to a whole-screen
        # swipe — that's SWIPE's job. Return a clear error so the agent
        # re-observes and picks a fresh scrollable element ref.
        return {
            "success": False,
            "error": (
                f"SCROLL target {action.target_ref or action.target_label!r} "
                "couldn't be resolved in the current snapshot, or has no "
                "frame. Re-observe and pick a visible scrollable element "
                "(role: scroll / table / collection / web). For "
                "whole-screen gestures use SWIPE instead."
            ),
            "scroll_dir": scroll_dir,
            "finger_dir": finger_dir,
            "requested_ref": action.target_ref,
            "requested_label": action.target_label,
        }

    if a == "fling":
        # FLING: high-velocity, larger-distance gesture for big jumps.
        # Unlike SCROLL, FLING ignores the adjustable flag — always
        # uses one fast gesture pattern. Effect varies by element type
        # naturally:
        #   - Adjustable (wheel): 1 fling ≈ 20-30 ticks (iOS picker
        #     deceleration physics)
        #   - Non-adjustable (scroll view, list): 1 fling ≈ screen-
        #     height of scroll content
        # Cap of 3 keeps wall-clock bounded (~7.5s worst case) and
        # variance manageable (3 flings = ~60-90 ticks, predictable).
        FLING_TO_FINGER = {
            "down":  "up",     # see content below → drag finger up
            "up":    "down",
            "left":  "right",
            "right": "left",
        }
        FLING_MAX_AMOUNT = 3
        fling_dir = (action.direction or "down").lower()
        finger_dir = FLING_TO_FINGER.get(fling_dir, "up")
        n_req = max(1, int(action.amount or 1))
        n = min(n_req, FLING_MAX_AMOUNT)
        capped = n != n_req
        target_el = find_element(action, tree) if (
            action.target_ref or action.target_label) else None
        if target_el and target_el.frame:
            # 120px drag at 1500 px/s — deliberately above iOS's fling
            # threshold so the gesture compounds with the element's
            # natural momentum/deceleration. Settle=True between
            # flings so each lands a discrete amount and the agent
            # can reason about the result.
            x1, y1, x2, y2 = _swipe_coords_for_finger_direction(
                target_el.frame, finger_dir, short_distance=120.0)
            for _ in range(n):
                await xc.swipe_at(x1, y1, x2, y2,
                                    duration_s=0.0, settle=True,
                                    velocity_pps=1500.0)
            return {"success": True, "fling_dir": fling_dir,
                    "finger_dir": finger_dir, "flings": n,
                    "requested_flings": n_req,
                    "capped": capped,
                    "ref": target_el.ref,
                    "label": target_el.effective_label,
                    "note": (
                        "element-targeted fling (1 fling ≈ 20-30 ticks "
                        "on a wheel; ≈ screen-height on a scroll view)"
                        if not capped else
                        f"FLING capped from {n_req} to "
                        f"{FLING_MAX_AMOUNT}. Max FLING is "
                        f"{FLING_MAX_AMOUNT} per turn (each ≈ 20-30 "
                        f"ticks on a wheel). For more, observe and "
                        f"FLING again."
                    )}
        # Disambiguate the three failure modes:
        #   (a) no ref was provided at all → use SWIPE for whole-screen
        #   (b) ref was provided but didn't resolve → re-observe (refs
        #       are per-observation; stale refs from a prior turn fail)
        #   (c) ref resolved but element has no usable frame → pick a
        #       different element (rare, but happens with some
        #       zero-area UI elements)
        # Always include a `note` field so JSON consumers don't KeyError.
        if not (action.target_ref or action.target_label):
            return {"success": False,
                    "error": "FLING requires an element ref (@e<id>). "
                              "Use SWIPE for whole-screen gestures.",
                    "note": "FLING needs an @ref",
                    "fling_dir": fling_dir}
        if target_el is None:
            return {"success": False,
                    "error": (f"FLING @{action.target_ref} not found in "
                               f"current AX tree — refs are per-observation, "
                               f"stale refs from prior turns won't resolve."),
                    "note": "FLING ref not found; re-observe and retry",
                    "fling_dir": fling_dir}
        return {"success": False,
                "error": (f"FLING target @{target_el.ref} has no usable "
                           f"frame (frame={target_el.frame}). Pick a "
                           f"different element."),
                "note": "FLING target has no frame",
                "fling_dir": fling_dir}

    if a == "adjust":
        return {"success": False,
                "error": "ADJUST not supported on XCUITest path yet"}

    if a == "press":
        button = (action.direction or "home").lower()
        if button not in ("home", "back", "app_switcher"):
            return {"success": False,
                    "error": f"unknown button: {button} "
                             "(expected: home | back | app_switcher)"}
        await xc.press(button=button)
        return {"success": True, "button": button}

    return {"success": False, "error": f"unknown action type: {a}"}


def parse_meta(line: str) -> Optional[str]:
    s = line.strip().lower()
    if s in ("quit", "q", "exit"):     return "quit"
    if s in ("help", "?", "h"):        return "help"
    if s in ("observe", "obs", "o"):   return "observe"
    if s in ("mapsdb", "maps_db", "mdb"): return "mapsdb"
    if s in ("contactsdb", "contacts_db", "cdb"): return "contactsdb"
    return None


def _meta_mapsdb(udid: str, baseline=None) -> None:
    """Dump the last 8 ZHISTORYITEM rows from Maps' live container.
    Useful for confirming directions-committed state before issuing
    DONE — the verifier's z_ent=16 check needs a row whose
    create_iso > baseline.captured_at.

    `baseline` is the BaselineSnapshot captured at session start;
    we print its iso so you can see exactly which rows the
    verifier will consider new."""
    import sys as _sys
    sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "benchmark"))
    from sibb_state import _mapsdb_path, _maps_history
    path = _mapsdb_path(udid)
    if not path:
        print(f"  {RE}MapsSync_0.0.1 not found (Maps never installed?).{R}")
        return
    rows = _maps_history(udid, limit=8)
    print(f"  {CY}MapsSync_0.0.1{R}: {path}")
    # Show the verifier's baseline cutoff — rows OLDER than this
    # don't count as "new" for the directions check.
    baseline_iso = None
    if baseline is not None:
        import datetime as _dt
        baseline_iso = _dt.datetime.utcfromtimestamp(
            baseline.captured_at).isoformat(timespec="seconds") + "Z"
        print(f"  {CY}baseline cutoff{R}: {baseline_iso}  "
              f"(rows must be after this to count)")
    if not rows:
        print(f"  (no rows in ZHISTORYITEM)")
        return
    print(f"  {len(rows)} recent rows:")
    label = {16: "DIRECTIONS", 20: "PLACE     ", 22: "SEARCH    "}
    for r in rows:
        ze = r.get("z_ent", "?")
        kind = label.get(ze, f"z_ent={ze}")
        q = (r.get("query") or "")[:40]
        loc = (r.get("location_display") or "")[:30]
        ts = r.get("create_iso", "")
        # Annotate rows that fall AFTER the baseline (= new this session)
        marker = ""
        if baseline_iso and ts > baseline_iso:
            marker = f" {GR}← NEW{R}"
        print(f"    {GR}{kind}{R}  {ts}  q={q!r}  loc={loc!r}{marker}")


def _meta_contactsdb(udid: str) -> None:
    """Dump the current Contacts state — names + postal_addresses.
    Helps verify the agent's address-saving actions reached the DB
    before issuing DONE."""
    import sqlite3 as _sqlite3, os as _os
    home = _os.path.expanduser("~")
    db = (f"{home}/Library/Developer/CoreSimulator/Devices/{udid}/"
          f"data/Library/AddressBook/AddressBook.sqlitedb")
    if not _os.path.exists(db):
        print(f"  {RE}AddressBook.sqlitedb not found.{R}")
        return
    print(f"  {CY}AddressBook{R}: {db}")
    conn = _sqlite3.connect(db, timeout=2.0)
    try:
        people = conn.execute(
            "SELECT ROWID, First, Last FROM ABPerson "
            "ORDER BY First, Last;").fetchall()
        print(f"  {len(people)} contacts:")
        for rowid, first, last in people:
            multivals = conn.execute(
                "SELECT property, value, label FROM ABMultiValue "
                "WHERE record_id=?;", (rowid,)).fetchall()
            print(f"    {GR}{(first or '')} "
                   f"{(last or '')}{R}  (#{rowid})")
            for prop, val, lbl in multivals:
                kind = {3: "phone   ", 4: "email   ",
                         5: "address "}.get(prop, f"prop={prop}")
                # Address: read sub-fields from ABMultiValueEntry
                if prop == 5:
                    subs = conn.execute(
                        "SELECT key, value FROM ABMultiValueEntry "
                        "WHERE parent_id=(SELECT rowid FROM ABMultiValue "
                        "WHERE record_id=? AND property=5 AND "
                        "value IS NULL LIMIT 1);", (rowid,)).fetchall()
                    sub_str = ", ".join(f"{k}={v!r}" for k, v in subs)
                    print(f"      {kind} label={lbl} fields={{{sub_str}}}")
                else:
                    print(f"      {kind} label={lbl} value={val!r}")
    finally:
        conn.close()


async def ainput(prompt: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, input, prompt)


def fmt_observation(tree: AXTree, tokenizer: AXTokenizer, step: int) -> str:
    flat = tokenizer.tokenize(tree, fmt="flat", max_elements=150)
    kb   = getattr(tree, "keyboard_visible", False)
    bid  = getattr(tree, "bundle_id", "?") or "?"
    bid_short = bid.split(".")[-1]
    n    = len(tree.elements)
    header = (f"\n{B}── step {step}   app={bid_short}   els={n}   "
              f"kb={kb}   {datetime.now().strftime('%H:%M:%S')} ──{R}\n")
    return header + flat


async def main():
    parser = argparse.ArgumentParser(
        description="SIBB Manual Replay — play the LLM, one task at a time.",
    )
    parser.add_argument("udid", help="Booted simulator UDID")
    parser.add_argument("--generator", default="reminders_list",
                        choices=list(GENERATORS.keys()),
                        help="Task generator (default: reminders_list)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    parser.add_argument("--bundle", default="com.apple.springboard",
                        help="Bundle the runner activates on attach. Default "
                             "is com.apple.springboard so the agent starts at "
                             "the home screen (and has to navigate to the app "
                             "the task requires) — that's the realistic agent "
                             "scenario. Pass com.apple.reminders or similar "
                             "to skip the navigation and drop the agent "
                             "directly into the target app.")
    args = parser.parse_args()

    gen_fn, verifier_fn = GENERATORS[args.generator]
    random.seed(args.seed)
    task = gen_fn()
    task.task_id = f"replay_{args.generator}_s{args.seed}"

    print_task(task)

    reader    = AXReader(args.udid)
    tokenizer = AXTokenizer()
    enricher  = AXEnricher(vlm_client=None)
    # Use SIBBScaffold purely for its parse_action() method — it doesn't
    # start its own reader unless we call setup() / observe() / act() on it.
    parser_helper = SIBBScaffold(args.udid)

    # Manual replay needs the Simulator.app GUI visible so a human can
    # tap/type. simctl boot only spins up the headless device process
    # — it doesn't open the window. We open the GUI here (no-op if
    # already running), AFTER pre-runner setup (which reboots the sim
    # for layout edits) — see below.

    # Pre-runner setup (Springboard layout/dock plist edits — these
    # require the sim to be shut down). No-op if the task has no
    # layout/dock entries; otherwise shuts down the sim, edits the
    # IconState plist, and reboots before the runner attaches.
    pre_report = apply_pre_runner_setup(args.udid, task)
    if pre_report["applied"]:
        print(f"\n{B}Pre-runner setup applied:{R}")
        for e in pre_report["applied"]:
            print(f"  {e}")
    if pre_report["errors"]:
        # Springboard pre-runner failures break the demo's premise (the
        # agent must navigate a randomized home screen). Print loudly
        # and abort instead of continuing into a half-set-up state.
        print(f"\n{RE}{B}Pre-runner setup FAILED:{R}")
        for e in pre_report["errors"]:
            print(f"  {RE}✗ {e}{R}")
        print(f"\n{YE}Aborting replay. Common recoveries:")
        print(f"  • If sim is stuck 'Shutting Down': "
              f"launchctl kickstart -k user/$UID/com.apple.CoreSimulator.CoreSimulatorService")
        print(f"  • If IconState.plist missing: boot the sim once to "
              f"materialize it, then re-run.{R}")
        sys.exit(1)

    # Open the Simulator.app GUI for this device so the user can see and
    # interact with the screen during manual replay. `open -a Simulator`
    # is a no-op if the app is already running; the `--args` flag
    # focuses the GUI on this specific UDID even if multiple devices
    # are booted.
    import subprocess as _sp
    _sp.run(["open", "-a", "Simulator",
              "--args", "-CurrentDeviceUDID", args.udid],
             capture_output=True)
    # Brief settle so the window is rendered before xcodebuild attaches.
    await asyncio.sleep(2)

    await reader.start(bundle_id=args.bundle)
    try:
        # Apply the rest of the spec (Reminders state, start_page, etc.)
        # via the now-connected reader.
        print(f"\n{B}Applying initial state…{R}")
        state_report = await apply_initial_state(reader._xcuitest, task)
        if state_report["reset"]:
            print(f"  reset:   {state_report['reset']}")
        if state_report["applied"]:
            print(f"  applied: {len(state_report['applied'])} spec entries")
        if state_report["errors"]:
            for e in state_report["errors"]:
                print(f"  {RE}error: {e}{R}")

        # Capture baseline for any identity-kind verify_checks (mirrors
        # the episode runner). Without this, identity checks error out
        # with "baseline required" — see sibb_verify._check_identity.
        verify_checks = getattr(task, "verify_checks", None) or []
        baseline_resources = _baseline_resources_for(verify_checks)
        baseline: Optional[BaselineSnapshot] = None
        if baseline_resources:
            try:
                baseline = await BaselineSnapshot.capture(
                    reader._xcuitest, sorted(baseline_resources))
                print(f"  baseline: captured "
                      f"{sorted(baseline_resources)}")
            except Exception as e:
                print(f"  {RE}baseline capture failed: "
                      f"{type(e).__name__}: {e}{R}")

        passed_before, checks_before = await verifier_fn(
            task, reader._xcuitest, baseline=baseline)
        print_verify(passed_before, checks_before, "BEFORE")
        if passed_before:
            print(f"\n  {YE}⚠ Verifier already passes — baseline state already "
                  f"satisfies the task. Likely a setup misconfiguration.{R}")

        print(HELP)

        step = 0
        last_tree: Optional[AXTree] = None
        terminal = None
        # Observation gate accounting. We track every distinct bundle
        # id seen in a tree across this episode; the agent_answer
        # verifier rejects ANSWERs whose `observation_required` bundles
        # weren't in this set.
        observed_bundles: set = set()
        # ANSWER payload captured from the terminal action, if any.
        answer_payload: Optional[dict] = None
        answer_parse_error: Optional[str] = None
        while True:
            step += 1
            tree = await reader.read()
            tree = await enricher.enrich(tree, screenshot=None)
            last_tree = tree
            bid = getattr(tree, "bundle_id", None)
            if bid:
                observed_bundles.add(bid)
            print(fmt_observation(tree, tokenizer, step))

            line = await ainput(f"{B}>>> {R}")
            line = line.strip()
            if not line:
                step -= 1   # don't count empty input as a step
                continue

            meta = parse_meta(line)
            if meta == "quit":
                print(f"{YE}Quitting without running AFTER verifier.{R}")
                return
            if meta == "help":
                print(HELP)
                step -= 1
                continue
            if meta == "observe":
                step -= 1   # next loop will re-observe
                continue
            if meta == "mapsdb":
                _meta_mapsdb(args.udid, baseline=baseline)
                step -= 1
                continue
            if meta == "contactsdb":
                _meta_contactsdb(args.udid)
                step -= 1
                continue

            action = parser_helper.parse_action(line)
            # Interactive-replay convenience: an unrecognized verb
            # (typo like "DOME" instead of "DONE") would otherwise
            # parse to FAIL and terminate the episode immediately —
            # confusing because the user can't recover from a typo.
            # For LLM-driver agents this still treats malformed
            # output as a hard fail (no scaffolding around bad model
            # behavior); this re-prompt only fires when reason starts
            # with "Unrecognized action".
            if (action.action_type == "fail"
                    and (action.reason or "").startswith(
                        "Unrecognized action")):
                print(f"{RE}  ✗ {action.reason} — "
                      f"type HELP for the grammar or try again.{R}")
                step -= 1
                continue
            t0 = time.time()
            result = await execute(reader, action, tree)
            ms = round((time.time() - t0) * 1000)

            color = GR if result.get("success") else RE
            mark  = "✓" if result.get("success") else "✗"
            print(f"{color}  {mark} {action.action_type.upper()}{R}  "
                  f"{GY}{result}  ({ms}ms){R}")

            if result.get("terminal"):
                terminal = action.action_type
                if terminal == "answer":
                    answer_payload = result.get("answer_payload")
                    answer_parse_error = result.get("parse_error")
                break

        # If the agent emitted ANSWER, show what we captured for the
        # demo viewer.
        if terminal == "answer":
            print(f"\n{B}ANSWER captured:{R}")
            if answer_parse_error:
                print(f"  {RE}parse error: {answer_parse_error}{R}")
            else:
                import json as _json
                print(f"  {CY}{_json.dumps(answer_payload)}{R}")
            print(f"{B}Observed bundles this episode:{R} "
                  f"{sorted(observed_bundles)}")

        # AFTER verifier. Thread the captured payload + observed
        # bundles through; both legacy and generic verifiers accept
        # `context=` (legacy ignores it).
        verifier_context = {
            "agent_answer": answer_payload,
            "observed_bundles": sorted(observed_bundles),
        }
        passed_after, checks_after = await verifier_fn(
            task, reader._xcuitest, context=verifier_context,
            baseline=baseline)
        print_verify(passed_after, checks_after, "AFTER")

        delta = passed_after and not passed_before
        if terminal == "done":
            agent_claim = "claimed DONE"
        elif terminal == "fail":
            agent_claim = "called FAIL"
        else:
            agent_claim = "did not declare terminal"
        icon = "✅" if delta else ("⚠" if passed_after else "❌")
        color = GR if delta else (YE if passed_after else RE)
        print(f"\n  {color}{icon} Episode result: agent {agent_claim}; "
              f"verifier before={passed_before} after={passed_after}{R}")
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
