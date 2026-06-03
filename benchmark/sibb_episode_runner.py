#!/usr/bin/env python3
"""
SIBB Episode Runner — Phase 1 (Reminders only)
==============================================
Walks the user through N benchmark tasks one at a time.
For each task:
  1. Print task instruction + intended initial state / noise.
  2. Run verifier BEFORE → expect FAIL (nothing done yet).
  3. Start an auto-capture watcher in the background.
  4. User completes the task manually in the simulator GUI.
  5. User presses ENTER to stop the watcher.
  6. Run verifier AFTER → expect PASS.

Captures are written to /tmp/sibb_captures_<task_id>/cap_<n>.txt,
one file per unique AX observation (same pipeline as the inspector
and the scaffold: AXReader → AXEnricher → AXTokenizer).

NOTE (Phase 1): Noise application is NOT yet implemented — the task
generator emits placeholder setup_cmds. This runner only prints what
the noise WOULD be. Verifier-before will therefore reflect the live
DB state, not the noise-injected one. Phase 2 will wire up the actual
noise injector and per-app verifier dispatcher.

Usage:
  /Library/Developer/CommandLineTools/usr/bin/python3 \\
    sibb_episode_runner.py <UDID> [n_tasks=5] [seed=42]
"""

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

from sibb_task_generator_v3 import gen_reminders_list
from sibb_scaffold import AXReader, AXEnricher, AXTokenizer
from sibb_verify_reminders import (
    verify_reminders_list_task,
    verify_reminders_list_task_async,
)
from sibb_state import apply_initial_state, apply_pre_runner_setup

# ── Terminal colours ─────────────────────────────────────────────────────────
R  = "\033[0m";  B  = "\033[1m"
CY = "\033[36m"; GR = "\033[32m"; YE = "\033[33m"
RE = "\033[31m"; GY = "\033[90m"; WH = "\033[97m"
BL = "\033[34m"


def banner(text: str, color: str = B):
    line = "═" * 70
    print(f"\n{color}{line}{R}")
    print(f"{color}  {text}{R}")
    print(f"{color}{line}{R}")


def print_task(task, idx: int, total: int):
    banner(f"Task {idx}/{total}   id={task.task_id}", B)
    print(f"\n{B}Instruction:{R}\n  {task.instruction}\n")
    print(f"{B}Apps:{R}        {', '.join(task.apps)}")
    print(f"{B}Steps:{R}       ~{task.steps}")
    print(f"{B}Complexity:{R}  {task.complexity}")
    print(f"{B}Detail:{R}      {task.detail_level}")

    init = task.initial_state
    if init.present or init.absent:
        print(f"\n{B}Expected initial state (not auto-applied — Phase 2):{R}")
        for p in init.present: print(f"  {GR}+{R} {p}")
        for a in init.absent:  print(f"  {RE}-{R} {a}")

    if init.noise_records:
        print(f"\n{B}Noise ({len(init.noise_records)} record(s)):{R}")
        for n in init.noise_records:
            print(f"  • {n.similarity}  [{n.record_type}]")
            for line in n.setup_cmd.splitlines():
                if line.strip():
                    print(f"    {GY}{line[:90]}{R}")

    print(f"\n{B}Params (verifier-relevant):{R}")
    for k in ("list", "list_state", "priority_item", "priority_level",
              "flag_item", "due_day", "tag"):
        v = task.params.get(k)
        if v is not None:
            print(f"  {k}: {v}")
    print(f"  items: {task.params.get('items', [])}")

    print(f"\n{B}Verify spec (description, not executable):{R}")
    print(f"  {GY}{task.verify[:200]}{R}")


def print_verify(passed_overall: bool, checks, when: str):
    color = GR if passed_overall else RE
    icon  = "✅" if passed_overall else "❌"
    label = "PASS (1.0)" if passed_overall else "FAIL (0.0)"
    print(f"\n{color}{B}  {icon} Verifier {when}: {label}{R}")
    for chk_label, p in checks:
        if p is None:
            print(f"     {GY}–  {chk_label}{R}")
        elif p:
            print(f"     {GR}✓  {chk_label}{R}")
        else:
            print(f"     {RE}✗  {chk_label}{R}")


_DEAD_SERVER_MARKERS = ("socket closed", "broken pipe", "not connected",
                        "connection reset")


def _stable_signature(tree):
    """
    Content-only signature for change detection — ignores @e#### ref churn.
    AXReader regenerates refs each observation, so hashing the tokenized
    text would treat every poll as a change. Hash structural fields,
    plus bundle_id so app switches always trigger a capture.
    """
    parts = []
    for el in tree.elements:
        f = el.frame
        cx = round(f.center_x) if f else 0
        cy = round(f.center_y) if f else 0
        parts.append((
            getattr(el.effective_role, "value", str(el.effective_role)),
            el.effective_label or "",
            el.value or "",
            cx, cy,
            bool(getattr(el, "focused", False)),
            bool(el.enabled),
        ))
    parts.append(("__kb__", bool(getattr(tree, "keyboard_visible", False))))
    parts.append(("__app__", getattr(tree, "bundle_id", "") or ""))
    return hash(tuple(parts))

async def watcher(reader: AXReader, output_dir: str, stop_event: asyncio.Event):
    """
    Background task: read AX tree on a tight loop, write a new
    capture file every time the LLM-formatted observation changes.
    Uses the exact scaffold pipeline so what gets dumped is what the
    LLM would see.

    Exits cleanly if the XCUITest server disconnects (the test target
    hits its default 600 s execution-time allowance during long manual
    sessions). Verifier-after still works since it reads SQLite directly.
    """
    tokenizer = AXTokenizer()
    enricher  = AXEnricher(vlm_client=None)
    os.makedirs(output_dir, exist_ok=True)

    last_sig = None
    counter  = 0
    print(f"  {GY}watcher → {output_dir}{R}")

    while not stop_event.is_set():
        try:
            tree = await reader.read()
            tree = await enricher.enrich(tree, screenshot=None)
            sig  = _stable_signature(tree)
            if sig != last_sig:
                flat = tokenizer.tokenize(tree, fmt="flat", max_elements=150)
                counter += 1
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                path = os.path.join(output_dir, f"cap_{counter:03d}.txt")
                focused = [e.effective_label for e in tree.elements
                           if getattr(e, "focused", False)]
                kb = getattr(tree, "keyboard_visible", False)
                method = getattr(tree, "method", "?")
                bundle = getattr(tree, "bundle_id", "") or "?"
                header = (f"# Captured at {ts}\n"
                          f"# App: {bundle}  Elements: {len(tree.elements)}  "
                          f"keyboard_visible: {kb}  method: {method}\n"
                          f"# Focused: {focused}\n\n")
                with open(path, "w") as f:
                    f.write(header)
                    f.write(flat)
                print(f"  {CY}📸 #{counter:03d}{R} {GY}{ts}{R}  "
                      f"app={bundle.split('.')[-1] if bundle else '?':<12}  "
                      f"els={len(tree.elements):3}  "
                      f"kb={str(kb):5}  "
                      f"focused={focused}")
                last_sig = sig
            try:
                # 0.8s poll — frequent enough to catch screen changes, slow
                # enough that the XCUITest server isn't hammered while the
                # user is doing rapid UI transitions (taps, Done, dismissals).
                await asyncio.wait_for(stop_event.wait(), timeout=0.8)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            break
        except (BrokenPipeError, ConnectionError):
            print(f"  {RE}watcher: server disconnected — stopping{R}")
            return
        except Exception as e:
            msg = str(e).lower()
            if any(m in msg for m in _DEAD_SERVER_MARKERS):
                print(f"  {RE}watcher: server disconnected ({e}) — stopping{R}")
                return
            print(f"  {YE}watcher error: {e}{R}")
            await asyncio.sleep(1.0)


async def ainput(prompt: str) -> str:
    """asyncio-friendly input() that doesn't block the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, input, prompt)


async def run_episode(reader: AXReader, task, idx: int, total: int, udid: str):
    print_task(task, idx, total)

    # Apply task's per-episode state spec (Reminders state, start_page, …).
    # Note: any Springboard layout/dock entries needed sim shutdown and
    # must have been applied via apply_pre_runner_setup BEFORE reader
    # started. The runner does that once at top-level for the whole
    # task batch — see main(). apply_initial_state skips those entries.
    state_report = await apply_initial_state(reader._xcuitest, task)
    if state_report["applied"]:
        print(f"\n  {GY}state applied: {len(state_report['applied'])} entries{R}")
    if state_report["errors"]:
        for e in state_report["errors"]:
            print(f"  {RE}state error: {e}{R}")

    # Verifier BEFORE — EventKit-backed, same code path as setup.
    passed_before, checks_before = await verify_reminders_list_task_async(
        task, reader._xcuitest)
    print_verify(passed_before, checks_before, "BEFORE")
    if passed_before:
        print(f"\n  {YE}⚠ Verifier already passes — task state already satisfied.{R}")
        print(f"  {YE}  (Either a generator quirk or a leftover from a prior run.){R}")

    # Start the watcher and let the user act.
    print(f"\n{B}Now complete the task manually on the simulator.{R}")
    output_dir = f"/tmp/sibb_captures_{task.task_id}"
    stop = asyncio.Event()
    watch_task = asyncio.create_task(watcher(reader, output_dir, stop))

    await ainput(f"{B}>>> Press ENTER when finished with the task <<<{R}\n")

    stop.set()
    await watch_task

    # Verifier AFTER — same EventKit path as BEFORE.
    passed_after, checks_after = await verify_reminders_list_task_async(
        task, reader._xcuitest)
    print_verify(passed_after, checks_after, "AFTER")

    delta_pass = passed_after and not passed_before
    summary_color = GR if delta_pass else (YE if passed_after else RE)
    icon = "✅" if delta_pass else ("⚠" if passed_after else "❌")
    print(f"\n  {summary_color}{icon} Episode {idx}: before={passed_before} after={passed_after}{R}")
    print(f"  {GY}captures: {output_dir}{R}\n")
    sys.stdout.flush()
    return passed_before, passed_after


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    udid = sys.argv[1]
    n    = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 42

    random.seed(seed)
    tasks = []
    for i in range(n):
        t = gen_reminders_list()
        t.task_id = f"phase1_{i+1:02d}"
        tasks.append(t)

    banner(f"SIBB Phase 1 Runner — {n} Reminders tasks, seed={seed}", B + CY)
    print(f"{B}UDID:{R} {udid}")
    print(f"{B}Backend:{R} XCUITest persistent server (snapshot path)")
    print(f"{B}Verifier:{R} verify_reminders_list_task_async (EventKit)")

    # Pre-runner setup: any Springboard layout/dock entries from the
    # FIRST task in the batch are applied before the runner starts,
    # because they require the sim to be shut down. Subsequent tasks in
    # the same session share that layout (we don't tear down + restart
    # the reader per episode). If per-episode layout randomization is
    # needed, run the replay tool one task at a time instead.
    if tasks:
        pre_report = apply_pre_runner_setup(udid, tasks[0])
        if pre_report["applied"]:
            print(f"\n{B}Pre-runner setup (from task 1):{R}")
            for e in pre_report["applied"]:
                print(f"  {e}")
        if pre_report["errors"]:
            for e in pre_report["errors"]:
                print(f"  {RE}error: {e}{R}")

    reader = AXReader(udid)
    await reader.start(bundle_id="com.apple.reminders")
    try:
        results = []
        for i, task in enumerate(tasks, 1):
            before, after = await run_episode(reader, task, i, n, udid)
            results.append((task.task_id, before, after))
            if i < n:
                cmd = (await ainput(
                    f"{B}Press ENTER for next task, or 'q' to quit: {R}")).strip().lower()
                if cmd == "q":
                    break

        banner("SIBB Phase 1 Summary", B + CY)
        for tid, before, after in results:
            icon = "✅" if (after and not before) else ("⚠" if after else "❌")
            print(f"  {icon}  {tid}: before={before} → after={after}")
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
