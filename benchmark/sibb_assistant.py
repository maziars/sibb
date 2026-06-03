#!/usr/bin/env python3
"""
SIBB LLM Driver — run a real model in the place of the human in sibb_replay.

Mirrors sibb_replay.py turn-for-turn:

  - same pre-runner setup (Springboard layout / dock / start_page)
  - same apply_initial_state (EventKit-backed Reminders/Calendar/...)
  - same BaselineSnapshot.capture for identity-kind verifiers
  - same BEFORE/AFTER verifier path
  - same XCUITest-backed execute() (TAP/TYPE/SCROLL/SWIPE/PRESS)
  - same parse_action() (handles ANSWER, multi-line reasoning, fences)

Differences:

  - prompts an LLM each turn instead of stdin
  - keeps a conversational history: system prompt + task instruction +
    (user observation, assistant action) pairs
  - logs every turn to JSONL for offline replay

Usage:

  /Library/Developer/CommandLineTools/usr/bin/python3 \\
    sibb/benchmark/sibb_assistant.py <UDID> \\
    --generator complete_specific_reminder \\
    --provider anthropic --model claude-haiku-4-5 \\
    --seed 42 --max-turns 30

Stops on terminal action (DONE/FAIL/ANSWER) or when --max-turns is hit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "simulator"))

from sibb_replay import GENERATORS, execute  # reuse the exact executor
from sibb_scaffold import (
    AXReader, AXEnricher, AXTokenizer, SIBBScaffold, AXTree,
)
from sibb_verify import BaselineSnapshot
from sibb_episode import _baseline_resources_for
from sibb_state import apply_initial_state, apply_pre_runner_setup
from sibb_llm import make_client, available_providers, default_model


# ── Terminal colours ────────────────────────────────────────────────────────
R  = "\033[0m";  B  = "\033[1m"
CY = "\033[36m"; GR = "\033[32m"; YE = "\033[33m"
RE = "\033[31m"; GY = "\033[90m"; BL = "\033[34m"


# ── System prompt ───────────────────────────────────────────────────────────
# Single canonical string. Mirrors the HELP table in sibb_replay so what the
# human sees during replay matches what the model sees in autonomous runs.
# When iterating on agent capabilities, add to this — don't fork.

SYSTEM_PROMPT = """\
You are an autonomous agent operating an iPhone simulator to complete a
single task. Each turn you receive the current accessibility (AX) tree
and must emit exactly ONE action.

== OBSERVATION FORMAT ==

Each line of the observation describes one on-screen element:

  @e042 [btn] "Add Alarm" @(335,822)
  @e017 [input] "Title" = "Team Standup" @(201,300)
  @e033 [switch] "Snooze" = on (disabled)

  @e<id>    element ref — use this to target the element in actions
  [role]    btn, input, txt, cell, switch, link, picker, sheet, adj, ...
  "label"   visible label
  = value   current value (text fields, switches, sliders)
  @(x,y)    centre of the element (you rarely need raw coords)
  ✦         element was enriched (no native AX label; VLM-derived)
  (disabled), (focused) — element state

[adj] is for ADJUSTABLE elements: date pickers, value pickers, sliders,
steppers, wheel-style controls. The interaction contract is TAP to focus,
then SCROLL or FLING @<ref> up/down to change. **NEVER TYPE into [adj]** —
typing into a date wheel does nothing and wastes the turn.

Compact date/time displays (e.g. `[adj] = "March 24"`) expand into
MULTIPLE INDEPENDENT [adj] columns when you TAP them — typically
Month / Day / Year. Each column has its own @ref and scrolls
INDEPENDENTLY. To set "March 24, 1964":
  1. TAP the compact [adj] to expand the wheels
  2. SCROLL/FLING @<month_ref> until it shows "March"
  3. SCROLL/FLING @<day_ref>   until it shows "24"
  4. SCROLL/FLING @<year_ref>  until it shows "1964"
The year column may render as "----" (year-omitted sentinel for
birthdays without a year). Scroll past it to a numeric year, or
land on it deliberately if the task wants no year.

Some wheels CYCLE (month, day, hour) — scrolling past the end wraps.
Others have ENDS (year, with "----" as a year-not-set sentinel) —
scrolling past the end does nothing.

The header above the list shows the current app and keyboard state, e.g.
"step 3   app=reminders   els=42   kb=False".

== ACTION GRAMMAR ==

Tap / type / scroll:
  TAP @e042                  tap element by ref
  TAP "Add Alarm"            tap element by label substring (case-sensitive)
  TAP (200, 400)             raw-coordinate tap (no AX lookup) — use sparingly
  TYPE @e017 "Hello world"   tap-to-focus then type into a text field.
                             NOTE: TYPE APPENDS to the current value;
                             it does NOT replace. `TYPE @e017 ""` is a
                             no-op, not a clear — use CLEAR first.
                             FAILURE: TYPE returns success=False when
                             the target can't receive focus within
                             1.5s. NO keystrokes are sent in that case
                             (they won't leak to the previously-focused
                             field). Common reasons:
                              * element is covered by the keyboard,
                                a modal, or another overlay
                              * element isn't actually a focus-receiver
                                (it's a label or image, not an input)
                              * element scrolled out of view between
                                observation and tap
                              * parent container intercepted the tap
                             Recovery: re-observe, scroll the field
                             into clear view, dismiss the keyboard if
                             blocking, or pick a different target.
  TYPE "text"                (no @ref) types to whatever's currently
                             focused. Use after you've already TAP'd
                             to focus, or after a CLEAR keeps the
                             same field focused.
  CLEAR @e017                wipe the current content of a text field
                             (triple-tap-select-all + delete on the iOS
                             side). Pair with TYPE to replace text:
                               CLEAR @e017
                               TYPE @e017 "new value"
  SCROLL down 2              whole-screen scroll (no element targeting).
                             For picker wheels, the @ref form is REQUIRED —
                             bare SCROLL moves the whole screen, not the
                             wheel underneath.
  SCROLL @e033 down 5        element-bounded scroll — PRECISE mode
                             (on picker wheels: ~1 tick per swipe;
                              on regular scroll views: ~one row per swipe).
                             MAX amount: 20 swipes/turn.
  FLING @e033 down 1         element-bounded FLING — FAST mode
                             (on picker wheels: ~20-30 ticks per fling;
                              on scroll views: ~screen-height per fling).
                             MAX amount: 3 flings/turn.
                             Use SCROLL for fine targeting (last 1-20
                             ticks); use FLING to close big gaps quickly.
                             Requires @ref — no whole-screen FLING.
  SWIPE left                 whole-screen swipe gesture
  SWIPE @e033 left           element-bounded swipe
  ADJUST @e044 up 3          (deprecated; use SCROLL @ref or FLING @ref)

Hardware buttons:
  PRESS home                 exit to home screen
  PRESS back                 left-edge in-app back gesture
  PRESS app_switcher         recent-apps carousel

Waiting for async UI:
  OBSERVE                    no-op — just re-observe on the next turn.
                             Use when you want to look again without
                             changing state.
  OBSERVE 5000               sleep 5 s (clamped to 0-10 s), then
                             re-observe. Use when an async UI process
                             is in flight (Maps route computation,
                             network spinners, animation settling)
                             and TAP/SCROLL would only interrupt the
                             background work. PREFER OBSERVE over
                             repeated SCROLL when waiting — scrolling
                             can slow or cancel routing.

Terminal actions (the episode ends after these):
  DONE "completed"           you believe the task is done (state-only tasks)
  FAIL "stuck"               you can't make progress
  ANSWER {"items": [...]}    reporting tasks — single-line JSON object;
                             the task instruction declares the exact keys.

== OUTPUT DISCIPLINE ==

- Emit exactly ONE action per turn.
- Brief reasoning before the action is fine, but the LAST line of your
  response MUST contain ONLY the action — the verb and its arguments,
  nothing else. Do NOT append the action to the end of a reasoning
  sentence (e.g. "...so I'm done.DONE \"completed\"" will FAIL to
  parse). Put a newline before the verb.
- Element refs (@e042) are valid ONLY for the current observation. After
  any non-terminal action, the next observation will assign fresh refs;
  do not reuse refs from a prior turn.
- For ANSWER: the JSON object MUST be on a single line, with no
  surrounding prose. Match the schema in the task instruction exactly —
  extra or missing keys cause an automatic failure.
- For DONE: emit it AS SOON AS the latest observation confirms the
  target state (e.g. a count or label changed to what the task asked
  for). Don't keep navigating after you're done — extra actions waste
  turns and can regress the state. If you're unsure, take ONE more
  OBSERVE-style turn (a benign TAP that returns you to a clear view),
  then DONE.
- Watch for futile actions. The observation header shows `els=N`
  (number of visible elements). If your last 2-3 actions left `els`
  unchanged AND the visible labels look the same, the screen isn't
  responding — CHANGE STRATEGY. Don't repeat the same action a fourth
  time. Try a different element, a different verb, navigate elsewhere
  (PRESS home, PRESS back, TAP "Cancel" / "Close"), or FAIL.

== SCROLL vs FLING — picker wheels, sliders, long lists ==

Two verbs for movement, picked by intent:

  SCROLL @<ref> <dir> [n]   PRECISE. On a picker wheel, 1 swipe ≈ 1 tick.
                            On a regular scroll, ≈ one row per swipe.
                            Max amount: 20 swipes per turn.
  FLING  @<ref> <dir> [n]   FAST/APPROXIMATE. On a picker wheel,
                            1 fling ≈ 20-30 ticks (with deceleration
                            variance). On a scroll view, ≈ one
                            screen-height per fling. Max amount: 3.

Strategy: when you need to span a big range (e.g. a year wheel from
2026 to 1964 = 62 ticks), use a FEW FLINGs to close most of the gap,
then SCROLLs for fine landing. Don't try to span 60 ticks with SCROLL
alone — you'd burn 3+ turns just on that.

WHAT YOU GET BACK FROM THE EXECUTOR:

  The PREVIOUS ACTION RESULT field on the next turn carries
  diagnostic flags. Read it.

  - `capped: true` means your requested `amount` exceeded the max.
    The `note` field tells you the actual amount executed and the
    cap. Example:
      "note": "SCROLL capped from 100 to 20. Max SCROLL is 20
       swipes/turn. For larger jumps use FLING."
    If you see this, DO NOT just re-emit the same big number — the
    cap will fire again. Switch to FLING for big jumps, or accept
    smaller per-turn moves.
  - `requested_swipes` / `swipes` (or `requested_flings` / `flings`):
    what you asked for vs. what executed. Useful for tracking your
    own intent against reality.

NUMBER vs TARGET-VALUE — common pitfall:

  The number in SCROLL / FLING is the COUNT OF SWIPES, NOT a target
  value. Concrete failure mode we've observed:
    `SCROLL down 2026`  does NOT navigate to year 2026 — it tries
    2026 whole-screen swipes (capped to 20). To set a year wheel
    from 2026 → 1964: TAP the date display to expand the wheels,
    pick the YEAR column's @ref, FLING down 2-3 times (~50 ticks),
    check the new value, then SCROLL to fine-tune.

PROBE DIRECTION FIRST:

  On a fresh wheel, scroll 1-2 ticks first to confirm which
  direction moves toward your target. Wheels don't always orient
  the way you'd guess — "down" might increase OR decrease depending
  on the wheel's value layout. Don't burn 10 ticks before checking.

== HOME-SCREEN NAVIGATION ==

The iOS home screen has MULTIPLE PAGES. The app you need may not be on
the page you start on. Pages are paged HORIZONTALLY — they do NOT
scroll vertically, so SCROLL down on the home screen does nothing
useful.

How to find an app:

1. **Search (preferred — fastest, always works).** Two equivalent ways
   to invoke Spotlight (iOS's global search):

   a) **`SWIPE down`** — from any regular home page, swipes down from
      the middle of the screen to open Spotlight. After Spotlight
      opens, TAP the search field to focus it (the keyboard will
      appear), then TYPE.
   b) **TAP a `"Search"` element on the home screen.** The Spotlight
      pill is at the BOTTOM of regular pages (high y, e.g. y > 600);
      App Library has its own search at the TOP. Either one opens a
      Spotlight-style search; TAPping it auto-focuses the field and
      brings up the keyboard immediately (one fewer step than SWIPE).

   Example sequences:

       # Option a (SWIPE):
       SWIPE down                # opens Spotlight overlay
       TAP @e041                 # tap the search field to focus
       TYPE @e041 "Reminders"
       TAP @e123                 # the row whose label is EXACTLY "Reminders"

       # Option b (TAP):
       TAP "Search"              # auto-focuses; keyboard appears
       TYPE @e041 "Reminders"
       TAP @e123

   IMPORTANT — section headers vs app icons: results are grouped into
   sections (Apps, Suggestions, etc.). Each section starts with a
   header row whose label lists every item in that section as one
   comma-separated string, e.g.
       [img] "Reminders, News, Calendar, Shortcuts"
   That header is NOT a tappable app launcher — it's a title for the
   row of icons that follows. The actual app you want is a separate
   row whose label is ONLY the app's name (no commas, no extra words).
   Prefer the row whose label is the EXACT app name.

   IMPORTANT — TWO result regions when searching:
   a) A vertical "Suggestions" list with `[cell] "AppName"` rows at
      properly-spaced y coordinates (e.g. y=198, 270, 342, ...).
      These are RELIABLE — tapping them launches the app.
   b) A horizontal row where multiple `[img] "AppName"` entries share
      IDENTICAL coordinates (e.g. all at `@(24,186)`). This row is
      UNRELIABLE — tapping one of these often hits whichever vertical
      cell happens to be at that y instead, so you may end up in the
      wrong app.
   Always prefer the `[cell]` results. If the app you want is NOT in
   the visible suggestions list, the list is alphabetical and
   truncated — SCROLL down inside the results to reveal more cells.

2. **Swipe between pages.** If Spotlight isn't an option:
       SWIPE left     advance to the next home-screen page
       SWIPE right    go back to the previous page
   The home screen can start on any page (not always page 0). If a
   SWIPE doesn't change what you see, you're at that end of the page
   list — try the OPPOSITE direction next.

3. **App Library.** Swiping left past the last home page opens the App
   Library (a searchable, auto-categorized view of every installed
   app). You can TAP the search field at the top and type the app
   name there too.

If you opened the wrong app, PRESS home to return, then try again.

== APP-SPECIFIC NOTES ==

- **Calendar.app**: the in-app Search bar (top-right corner of the
  month view) does NOT reliably find every event — many events that
  exist won't appear in search results. If your search returns zero
  result rows, TAP "Close" to dismiss the search, then navigate via
  the scrolling month view instead. Each day is a tappable button
  labeled with its event count, e.g.:

      [btn] "Tuesday, May 5" = 1 event @(86,334)
      [btn] "Sunday, May 10" = 1 event @(373,334)

  TAP a day with `= N events` (where N >= 1) to see its event list,
  then TAP the event you want.

- **Messages.app**: in inbox + thread views, message cells whose
  label starts with `"Your iMessage, ..."` are messages YOU sent;
  cells whose label starts with a phone number (e.g.
  `"+1 (555) 564-8583, ..."`) are messages you RECEIVED. The
  thread's title-bar shows the other party's phone number.

- **Maps.app — Directions screen**: after picking a transport mode
  (Drive / Walk / Transit / Cycle), iOS shows route alternatives.
  Each alternative renders as a row with a route-summary `[el]`
  carrying the ETA + distance + "Fastest" tag (e.g.
  `[el] "5 hr 33 min, 7:01 ETA · 380 mi, Fastest"`) and a `[btn]`
  labeled `"Steps"` next to it. The TOP row is iOS's
  fastest/recommended pick.

- **Maps.app — starting navigation (iOS sim quirk)**: in the iOS
  simulator the explicit GO / Start button you'd see on a real
  iPhone is NOT reliably present in the AX tree. **TAP the `[btn]
  "Steps"` next to the route you want — this BOTH starts turn-by-
  turn navigation AND shows the step-by-step list.** Despite the
  label, it's how route activation works in the sim. Don't try to
  hunt for a GO button — emit `TAP @<steps-ref>` on the chosen
  row.

- **Maps.app — "Loading…" on Directions**: while routes compute,
  the screen shows `[text] "Loading…"` plus `[el] "In progress" = 1`
  and NO route alternative buttons or GO button. Route computation
  takes 5-30 s for distant destinations (different city or state)
  because Maps has to fetch tile + routing data over the network.
  DO NOT TAP, SCROLL, or PRESS during this state — those actions
  can interrupt or cancel the route fetch. Emit `OBSERVE 5000`
  (or `OBSERVE 3000`) and wait. Repeat if Loading… is still up
  on the next turn. Route buttons + GO will appear when ready,
  then tap GO to start navigation.

== iOS QUIRKS ==

- The keyboard often covers the bottom of the screen; you may need to
  scroll a list — or scroll the FORM you're editing — before its lower
  entries become tappable. The scaffold filters out elements fully
  hidden behind the keyboard, so if a form field you expected isn't in
  the observation while typing, SCROLL the form up to bring it above
  the keyboard.
- Modal sheets dismiss by tapping outside, dragging the grabber down,
  or tapping a "Cancel"/"Done" button — there is no PRESS back from
  inside a sheet.
- PRESS home backgrounds the current app; it does not close it. You
  return to whichever home page you were last on, not necessarily
  page 0.
- After a TAP that opens a new screen, the AX tree may take ~200ms to
  settle. The system already waits for stability before observing.

Stay focused on the task. If you complete it, emit DONE (or ANSWER
for reporting tasks) immediately — don't keep exploring."""


# ── Helpers ─────────────────────────────────────────────────────────────────

def banner(text: str, color: str = B):
    line = "═" * 70
    print(f"\n{color}{line}{R}\n{color}  {text}{R}\n{color}{line}{R}")


def print_task(task):
    banner(f"Task: {task.task_id}", B + CY)
    print(f"\n{B}Instruction:{R}\n  {task.instruction}\n")
    print(f"{B}Apps:{R}        {', '.join(task.apps)}")
    print(f"{B}Steps:{R}       ~{task.steps}")
    print(f"{B}Complexity:{R}  {task.complexity}")


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


def fmt_observation(tree: AXTree, tokenizer: AXTokenizer, step: int,
                    *, max_elements: int = 150) -> str:
    flat = tokenizer.tokenize(tree, fmt="flat", max_elements=max_elements)
    kb   = getattr(tree, "keyboard_visible", False)
    bid  = getattr(tree, "bundle_id", "?") or "?"
    bid_short = bid.split(".")[-1]
    n    = len(tree.elements)
    header = (f"── step {step}   app={bid_short}   els={n}   "
              f"kb={kb} ──")
    return header + "\n" + flat


class TurnLog:
    """Append-only JSONL turn log + summary record at the end."""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "w", buffering=1)

    def append(self, record: Dict[str, Any]):
        self._f.write(json.dumps(record, default=str) + "\n")

    def close(self):
        self._f.close()


# ── Main driver ─────────────────────────────────────────────────────────────

async def verify_via(verifier_fn, task, xc, *, context=None, baseline=None):
    return await verifier_fn(task, xc, context=context, baseline=baseline)


async def run_episode(args) -> int:
    gen_fn, verifier_fn = GENERATORS[args.generator]
    random.seed(args.seed)
    task = gen_fn()
    task.task_id = f"assistant_{args.generator}_s{args.seed}"

    print_task(task)
    print(f"{B}Provider:{R}    {args.provider}")
    print(f"{B}Model:{R}       {args.model}")
    print(f"{B}Max turns:{R}   {args.max_turns}")
    if args.max_seconds != float("inf"):
        print(f"{B}Max seconds:{R} {args.max_seconds}")

    # LLM client (build now so a bad config fails before sim setup).
    llm = make_client(args.provider, model=args.model,
                      timeout=args.llm_timeout)

    reader    = AXReader(args.udid)
    tokenizer = AXTokenizer()
    enricher  = AXEnricher(vlm_client=None)
    parser_helper = SIBBScaffold(args.udid)

    # JSONL turn log next to the script. Filename: episode_<gen>_<provider>_<model>_<ts>.jsonl
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_name = f"episode_{args.generator}_{args.provider}_{args.model}_{ts}.jsonl"
    log_path = os.path.join(args.log_dir or SCRIPT_DIR, log_name)
    log = TurnLog(log_path)
    log.append({
        "type": "task",
        "task_id": task.task_id,
        "generator": args.generator,
        "seed": args.seed,
        "instruction": task.instruction,
        "apps": list(task.apps),
        "params": {k: str(v) for k, v in task.params.items()},
        "provider": args.provider,
        "model": args.model,
    })
    print(f"{B}Log:{R}         {log_path}")

    # Pre-runner setup (sim-shutdown-required plist edits — Springboard
    # layout/dock). No-op if the task has no such entries.
    pre_report = apply_pre_runner_setup(args.udid, task)
    if pre_report.get("applied"):
        print(f"\n{B}Pre-runner setup applied:{R}")
        for e in pre_report["applied"]:
            print(f"  {e}")
    if pre_report.get("errors"):
        print(f"\n{RE}{B}Pre-runner setup FAILED:{R}")
        for e in pre_report["errors"]:
            print(f"  {RE}✗ {e}{R}")
        log.append({"type": "abort", "reason": "pre_runner_failed",
                    "errors": pre_report["errors"]})
        log.close()
        return 1

    import subprocess as _sp
    _sp.run(["open", "-a", "Simulator",
             "--args", "-CurrentDeviceUDID", args.udid],
            capture_output=True)
    await asyncio.sleep(2)

    await reader.start(bundle_id=args.bundle)
    exit_code = 0
    try:
        # Apply initial state via the connected XCUITest reader.
        print(f"\n{B}Applying initial state…{R}")
        state_report = await apply_initial_state(reader._xcuitest, task)
        if state_report.get("reset"):
            print(f"  reset:   {state_report['reset']}")
        if state_report.get("applied"):
            print(f"  applied: {len(state_report['applied'])} spec entries")
        if state_report.get("errors"):
            for e in state_report["errors"]:
                print(f"  {RE}error: {e}{R}")
        log.append({"type": "initial_state", "report": state_report})

        # Capture baseline for identity-kind checks.
        verify_checks = getattr(task, "verify_checks", None) or []
        baseline_resources = _baseline_resources_for(verify_checks)
        baseline: Optional[BaselineSnapshot] = None
        if baseline_resources:
            try:
                baseline = await BaselineSnapshot.capture(
                    reader._xcuitest, sorted(baseline_resources))
                print(f"  baseline: captured {sorted(baseline_resources)}")
            except Exception as e:
                print(f"  {RE}baseline capture failed: "
                      f"{type(e).__name__}: {e}{R}")

        passed_before, checks_before = await verify_via(
            verifier_fn, task, reader._xcuitest, baseline=baseline)
        print_verify(passed_before, checks_before, "BEFORE")
        log.append({"type": "verify_before", "passed": passed_before,
                    "checks": [(c, bool(ok) if ok is not None else None)
                               for c, ok in checks_before]})
        if passed_before:
            print(f"  {YE}⚠ Verifier already passes — likely a setup "
                  f"misconfiguration; running anyway.{R}")

        # Conversation history — alternating user (observation+result) and
        # assistant (action text) messages. The task instruction goes in
        # the FIRST user turn so it's anchored in the conversation log.
        history: List[Dict[str, str]] = []
        first_user_prefix = (
            f"TASK INSTRUCTION:\n{task.instruction}\n\n"
            f"You are starting at: {args.bundle}\n"
            f"Emit exactly one action; the last line of your reply MUST be the action.\n\n"
        )

        step = 0
        terminal = None
        observed_bundles: set = set()
        answer_payload: Optional[dict] = None
        answer_parse_error: Optional[str] = None
        last_action_result: Optional[Dict[str, Any]] = None
        episode_start = time.monotonic()
        truncated_reason: Optional[str] = None

        for _ in range(args.max_turns):
            # Pre-turn budget check — catches the case where the previous
            # turn's LLM call + action pushed us past the budget. Without
            # this, we'd run one more full turn (LLM latency + up to ~43s
            # of action time) after the budget was crossed.
            elapsed_pre = time.monotonic() - episode_start
            if elapsed_pre >= args.max_seconds:
                truncated_reason = (
                    f"max-seconds ({args.max_seconds}) exceeded before "
                    f"turn {step + 1} ({elapsed_pre:.1f}s)")
                print(f"\n{YE}  ⚠ {truncated_reason}{R}")
                log.append({"type": "truncated",
                             "max_seconds": args.max_seconds,
                             "elapsed_s": elapsed_pre,
                             "step": step,
                             "phase": "pre_turn"})
                break
            step += 1
            tree = await reader.read()
            tree = await enricher.enrich(tree, screenshot=None)
            bid = getattr(tree, "bundle_id", None)
            if bid:
                observed_bundles.add(bid)

            obs_text = fmt_observation(tree, tokenizer, step,
                                       max_elements=args.max_elements)
            print(f"\n{B}{obs_text.splitlines()[0]}{R}")
            if args.verbose:
                print(obs_text)

            # Build the next user message. First turn includes the task
            # instruction; later turns prepend the previous action's result.
            user_chunks = []
            if not history:
                user_chunks.append(first_user_prefix)
            else:
                if last_action_result is not None:
                    result_one_line = json.dumps(
                        {k: last_action_result.get(k)
                         for k in ("success", "error", "note",
                                   "ref", "label", "coords", "button",
                                   "capped", "swipes",
                                   "requested_swipes",
                                   "flings", "requested_flings")
                         if last_action_result.get(k) is not None},
                        default=str,
                    )
                    user_chunks.append(
                        f"PREVIOUS ACTION RESULT: {result_one_line}\n\n"
                    )
            user_chunks.append("CURRENT OBSERVATION:\n")
            user_chunks.append(obs_text)
            user_msg = "".join(user_chunks)

            history.append({"role": "user", "content": user_msg})

            # ── LLM call ──
            t0 = time.time()
            try:
                llm_resp = await llm.chat(
                    history,
                    system=SYSTEM_PROMPT,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                )
            except Exception as e:
                print(f"{RE}  ✗ LLM call failed: {type(e).__name__}: {e}{R}")
                log.append({"type": "llm_error", "step": step,
                            "error": f"{type(e).__name__}: {e}"})
                exit_code = 2
                break
            llm_ms = round((time.time() - t0) * 1000)

            llm_text = llm_resp.text or ""
            # Show the model's reply (truncated for readability).
            shown = llm_text.strip()
            if len(shown) > 600 and not args.verbose:
                shown = shown[:600] + f"... [+{len(llm_text) - 600} chars]"
            print(f"{CY}{shown}{R}")
            print(f"{GY}  llm: {llm_resp.provider}/{llm_resp.model} "
                  f"{llm_resp.input_tokens}→{llm_resp.output_tokens} tok "
                  f"({llm_ms}ms){R}")

            history.append({"role": "assistant", "content": llm_text})
            log.append({
                "type": "turn",
                "step": step,
                "observation": obs_text,
                "llm_text": llm_text,
                "input_tokens": llm_resp.input_tokens,
                "output_tokens": llm_resp.output_tokens,
                "stop_reason": llm_resp.stop_reason,
                "llm_ms": llm_ms,
            })

            action = parser_helper.parse_action(llm_text)

            # Execute and record result.
            t1 = time.time()
            result = await execute(reader, action, tree)
            exec_ms = round((time.time() - t1) * 1000)
            last_action_result = result

            color = GR if result.get("success") else RE
            mark  = "✓" if result.get("success") else "✗"
            print(f"{color}  {mark} {action.action_type.upper()}{R}  "
                  f"{GY}{result}  ({exec_ms}ms){R}")
            log.append({
                "type": "action",
                "step": step,
                "action_type": action.action_type,
                "target_ref": action.target_ref,
                "target_label": action.target_label,
                "text": action.text,
                "direction": action.direction,
                "amount": action.amount,
                "reason": action.reason,
                "answer_payload": action.answer_payload,
                "parse_error": action.parse_error,
                "result": result,
                "exec_ms": exec_ms,
            })

            if result.get("terminal"):
                terminal = action.action_type
                if terminal == "answer":
                    answer_payload = result.get("answer_payload")
                    answer_parse_error = result.get("parse_error")
                break

            # Wall-clock timeout check (graceful — current action
            # already completed; we just don't start the next turn).
            elapsed = time.monotonic() - episode_start
            if elapsed >= args.max_seconds:
                truncated_reason = (
                    f"max-seconds ({args.max_seconds}) exceeded "
                    f"after {step} turns ({elapsed:.1f}s)")
                print(f"\n{YE}  ⚠ {truncated_reason}{R}")
                log.append({"type": "truncated",
                             "max_seconds": args.max_seconds,
                             "elapsed_s": elapsed,
                             "step": step,
                             "phase": "post_action"})
                break
        else:
            # for/else: loop fell through without hitting break.
            print(f"\n{YE}  ⚠ max-turns ({args.max_turns}) exhausted before "
                  f"any terminal action.{R}")
            log.append({"type": "exhausted",
                         "max_turns": args.max_turns,
                         "step": step,
                         "elapsed_s": time.monotonic() - episode_start})

        if terminal == "answer":
            print(f"\n{B}ANSWER captured:{R}")
            if answer_parse_error:
                print(f"  {RE}parse error: {answer_parse_error}{R}")
            else:
                print(f"  {CY}{json.dumps(answer_payload)}{R}")
            print(f"{B}Observed bundles this episode:{R} "
                  f"{sorted(observed_bundles)}")

        # AFTER verifier.
        verifier_context = {
            "agent_answer": answer_payload,
            "observed_bundles": sorted(observed_bundles),
        }
        passed_after, checks_after = await verify_via(
            verifier_fn, task, reader._xcuitest,
            context=verifier_context, baseline=baseline)
        print_verify(passed_after, checks_after, "AFTER")

        delta = passed_after and not passed_before
        if terminal == "done":
            agent_claim = "claimed DONE"
        elif terminal == "fail":
            agent_claim = "called FAIL"
        elif terminal == "answer":
            agent_claim = "emitted ANSWER"
        elif truncated_reason:
            agent_claim = "timed out (max-seconds)"
        else:
            agent_claim = "did not declare terminal (max-turns exhausted)"
        icon = "✅" if delta else ("⚠" if passed_after else "❌")
        color = GR if delta else (YE if passed_after else RE)
        print(f"\n  {color}{icon} Episode result: agent {agent_claim}; "
              f"verifier before={passed_before} after={passed_after}{R}")

        log.append({
            "type": "verify_after",
            "passed": passed_after,
            "checks": [(c, bool(ok) if ok is not None else None)
                       for c, ok in checks_after],
            "terminal": terminal,
            "delta": delta,
        })

        # Exit 0 if the verifier delta was a pass; 1 otherwise (lets
        # shell scripts gate on success).
        exit_code = 0 if delta else 1
    finally:
        await reader.stop()
        log.close()

    return exit_code


def parse_args():
    parser = argparse.ArgumentParser(
        description="SIBB LLM driver — run a model in the place of the human."
    )
    parser.add_argument("udid", help="Booted simulator UDID")
    parser.add_argument("--generator", default="complete_specific_reminder",
                        choices=list(GENERATORS.keys()),
                        help="Task generator (default: complete_specific_reminder)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    parser.add_argument("--bundle", default="com.apple.springboard",
                        help="Bundle to attach to (default springboard → home).")
    parser.add_argument("--provider", default="anthropic",
                        choices=available_providers(),
                        help="LLM provider")
    parser.add_argument("--model", default=None,
                        help="Model name (defaults to provider's default)")
    parser.add_argument("--max-turns", type=int, default=30,
                        help="Hard cap on action turns (default: 30)")
    parser.add_argument("--max-seconds", type=float, default=float("inf"),
                        help="Hard cap on total episode wall-clock "
                             "seconds (default: inf — no timeout). "
                             "Includes LLM calls + action execution. "
                             "Graceful — completes the current action "
                             "before checking, never kills mid-action.")
    parser.add_argument("--max-tokens", type=int, default=1024,
                        help="Max LLM output tokens per turn (default: 1024)")
    parser.add_argument("--max-elements", type=int, default=150,
                        help="Max AX elements per observation (default: 150)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="LLM temperature (default: provider default)")
    parser.add_argument("--llm-timeout", type=float, default=60.0,
                        help="Per-call LLM timeout seconds (default: 60)")
    parser.add_argument("--log-dir", default=None,
                        help="Directory for JSONL turn log "
                             "(default: alongside sibb_assistant.py)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print full observation each turn")
    args = parser.parse_args()
    if args.model is None:
        args.model = default_model(args.provider)
    return args


def main():
    args = parse_args()
    sys.exit(asyncio.run(run_episode(args)))


if __name__ == "__main__":
    main()
