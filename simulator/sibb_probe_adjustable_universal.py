#!/usr/bin/env python3
"""Universal adjustable-trait probe — validate the Phase 2 wheel fix
across multiple iOS adjustable controls.

Two validation goals:

1. The Swift `snapshotAdjustable()` helper flags the `adjustable` bit
   correctly on every kind of wheel/picker/slider/stepper iOS exposes,
   not just UIDatePicker compact (which we already proved on the
   earlier trial). Test 3 surfaces:
     a. Calendar → New Event → time row (UIDatePicker)
     b. Settings → Display & Brightness → brightness slider (UISlider)
     c. Contacts → Edit → birthday picker (UIDatePicker — sanity re-check)

2. The settle-skip optimization (settle=False for swipes 1..N-1,
   settle=True only on the LAST) is materially faster than the old
   behavior. Microbenchmark a 10-swipe batch on a real wheel and
   report old vs new wall-clock time.

Navigation patterns are copied from `sibb_probe_contacts_validations.py`
(force-terminate before launch, dismiss-onboarding fallback, generous
waits, coord-based tap to avoid iOS 26 snapshot-path crashes).
"""
from __future__ import annotations
import asyncio, json, os, subprocess, sys, time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"


def green(s): return f"\033[32m{s}\033[0m"
def red(s):   return f"\033[31m{s}\033[0m"
def yellow(s):return f"\033[33m{s}\033[0m"


def simctl(*args):
    return subprocess.run(["xcrun", "simctl", *args],
                            capture_output=True, text=True, timeout=20)


def check(label, cond, evidence=""):
    mark = green("✓") if cond else red("✗")
    print(f"  {mark} {label}" + (f" — {evidence}" if evidence else ""))
    return 0 if cond else 1


async def observe_raw(reader) -> Dict[str, Any]:
    """Send a raw observe (don't go through the AXReader filter)."""
    resp = await reader._send({"type": "observe"})
    return resp if resp.get("ok") else {"elements": []}


def find_in_elements(elements, *, role=None, label_contains=None,
                      label_exact=None, hittable=True):
    """Return the first element matching the filters, or None."""
    for e in elements:
        if hittable and not e.get("hittable", True):
            continue
        if role and e.get("role") != role:
            continue
        lbl = e.get("label") or ""
        if label_exact is not None and lbl != label_exact:
            continue
        if label_contains is not None and label_contains not in lbl:
            continue
        return e
    return None


def adjustable_elements(elements) -> List[Dict[str, Any]]:
    return [e for e in elements if e.get("adjustable")]


async def tap_frame(reader, e):
    """Tap the center of an element's frame."""
    f = e["frame"]
    await reader._send({"type": "tap",
                          "x": f["x"] + f["width"]/2,
                          "y": f["y"] + f["height"]/2})


async def launch_clean(reader, bundle: str, wait_s: float = 3.0):
    """Force-terminate via simctl then socket-level launch — the latter
    updates the Swift runner's `currentApp` pointer that `observe`
    requires. Using `simctl launch` alone leaves currentApp stale and
    observe returns empty element lists."""
    simctl("terminate", UDID, bundle)
    await asyncio.sleep(0.8)
    # Socket-level launch — sets currentApp = XCUIApplication(bundle)
    # and waits for runningForeground.
    await reader._send({"type": "launch", "bundleId": bundle})
    await asyncio.sleep(wait_s)


async def dump_first_n_adj(elements, n=10, label=""):
    """Pretty-print the first n adjustable elements."""
    adj = adjustable_elements(elements)
    print(f"  adjustable elements ({label}): {len(adj)}")
    for a in adj[:n]:
        f = a.get("frame", {})
        print(f"    role={a.get('role'):>12}  "
              f"label={(a.get('label') or '')[:24]!r:>26}  "
              f"value={(a.get('value') or '')[:18]!r:>20}  "
              f"frame=({f.get('x'):.0f},{f.get('y'):.0f},"
              f"{f.get('width'):.0f}×{f.get('height'):.0f})")
    return adj


# ──────────────────────────────────────────────────────────────────────────
# Scenario 1 — UIDatePicker via Calendar New Event time row
# ──────────────────────────────────────────────────────────────────────────

async def scenario_calendar_time(reader):
    print()
    print("=" * 70)
    print("Scenario 1: UIDatePicker (Calendar → New Event → Start time)")
    print("=" * 70)
    fails = 0
    await reader._send({"type": "wipe_events"})
    await launch_clean(reader, "com.apple.mobilecal")

    # Find the Add (+) button — Calendar's "New Event" affordance
    obs = await observe_raw(reader)
    add = find_in_elements(obs["elements"], role="btn",
                            label_contains="Add")
    if not add:
        # Try other common labels
        for c in ("New Event", "+", "Create"):
            add = find_in_elements(obs["elements"], role="btn",
                                    label_contains=c)
            if add:
                break
    if not add:
        print(red("  FAIL: no Add/+ button found in Calendar"))
        return 1
    print(f"  tapping Add: label={add.get('label')!r} @"
          f"({add['frame']['x']:.0f},{add['frame']['y']:.0f})")
    await tap_frame(reader, add)
    await asyncio.sleep(2.0)

    # Now we're on the New Event sheet. Find the "Start" row.
    obs = await observe_raw(reader)
    start_row = find_in_elements(obs["elements"], role="cell",
                                   label_contains="Start")
    if not start_row:
        # Sheet may need a Title first — type a title to enable the rows
        first_input = find_in_elements(obs["elements"], role="input")
        if first_input:
            await tap_frame(reader, first_input)
            await asyncio.sleep(0.8)
            await reader._send({"type": "type_text", "text": "ProbeEvent"})
            await asyncio.sleep(0.5)
            obs = await observe_raw(reader)
            start_row = find_in_elements(obs["elements"], role="cell",
                                           label_contains="Start")
    if not start_row:
        print(red("  FAIL: no Start row found on New Event sheet"))
        return 1
    print(f"  tapping Start: label={start_row.get('label')!r}")
    await tap_frame(reader, start_row)
    await asyncio.sleep(1.5)

    # The time picker should be inline now
    obs = await observe_raw(reader)
    adj = await dump_first_n_adj(obs["elements"], n=8,
                                    label="after Start tap")

    fails += check("≥1 adjustable element on Calendar Start-time view",
                    len(adj) >= 1, f"got {len(adj)}")
    if adj:
        # Look for elements with time-shaped values (HH:MM, AM/PM)
        time_shaped = [a for a in adj
                        if any(c in (a.get("value") or "")
                                for c in [":", "AM", "PM", "00"])]
        fails += check("≥1 adjustable has time-shaped value",
                        len(time_shaped) >= 1,
                        f"got {len(time_shaped)}")
    return fails


# ──────────────────────────────────────────────────────────────────────────
# Scenario 2 — UISlider via Settings → Display & Brightness
# ──────────────────────────────────────────────────────────────────────────

async def scenario_settings_brightness(reader):
    print()
    print("=" * 70)
    print("Scenario 2: UISlider (Settings → Display & Brightness)")
    print("=" * 70)
    fails = 0
    await launch_clean(reader, "com.apple.Preferences", wait_s=3.5)

    # Iterate scroll-and-look for "Display & Brightness"
    obs = await observe_raw(reader)
    target = find_in_elements(obs["elements"], role="cell",
                                label_contains="Display & Brightness")
    if not target:
        for _ in range(6):
            await reader._send({"type": "swipe", "direction": "up"})
            await asyncio.sleep(0.6)
            obs = await observe_raw(reader)
            target = find_in_elements(obs["elements"], role="cell",
                                        label_contains="Display & Brightness")
            if target:
                break
    if not target:
        # Try shorter substring
        target = find_in_elements(obs["elements"], role="cell",
                                    label_contains="Brightness")
    if not target:
        print(red("  FAIL: 'Display & Brightness' cell not found in Settings"))
        # Diagnostic: dump first 20 cells
        cells = [e for e in obs["elements"] if e.get("role") == "cell"][:20]
        print("  Available cells (first 20):")
        for c in cells:
            print(f"    {(c.get('label') or '')[:60]!r}")
        return 1
    print(f"  tapping Display & Brightness")
    await tap_frame(reader, target)
    await asyncio.sleep(1.5)

    # Brightness slider should be on this screen
    obs = await observe_raw(reader)
    adj = await dump_first_n_adj(obs["elements"], n=8,
                                    label="Display & Brightness screen")

    fails += check("≥1 adjustable element on Brightness screen",
                    len(adj) >= 1, f"got {len(adj)}")
    has_slider = any(a.get("role") == "slider" for a in adj)
    fails += check("at least one is role=slider",
                    has_slider,
                    f"roles found: {sorted(set(a.get('role') for a in adj))}")
    return fails


# ──────────────────────────────────────────────────────────────────────────
# Scenario 3 — Contacts birthday (re-validate the original case)
# ──────────────────────────────────────────────────────────────────────────

async def scenario_contacts_birthday(reader):
    print()
    print("=" * 70)
    print("Scenario 3: UIDatePicker compact + expanded (Contacts birthday)")
    print("=" * 70)
    fails = 0

    # Seed
    await reader._send({"type": "wipe_contacts"})
    r = await reader._send({"type": "create_contact",
                              "given_name": "Probe",
                              "family_name": "Adjustable",
                              "phone": "650-555-0001"})
    print(f"  seeded contact: {r.get('identifier','?')[:36]}...")

    await launch_clean(reader, "com.apple.MobileAddressBook")

    # Find the contact row — try several label fallbacks
    obs = await observe_raw(reader)
    contact_cell = (
        find_in_elements(obs["elements"], role="cell",
                          label_contains="Probe Adjustable")
        or find_in_elements(obs["elements"], role="cell",
                              label_contains="Adjustable")
        or find_in_elements(obs["elements"], role="cell",
                              label_contains="Probe")
    )
    if not contact_cell:
        # Diagnostic: what cells DO exist?
        cells = [e for e in obs["elements"] if e.get("role") == "cell"][:10]
        print("  Available cells (first 10):")
        for c in cells:
            print(f"    {(c.get('label') or '')[:60]!r}")
        print(red("  FAIL: Probe contact row not visible"))
        return 1
    print(f"  tapping contact: {contact_cell.get('label')!r}")
    await tap_frame(reader, contact_cell)
    await asyncio.sleep(1.5)

    # Tap Edit
    obs = await observe_raw(reader)
    edit_btn = find_in_elements(obs["elements"], role="btn",
                                  label_exact="Edit")
    if not edit_btn:
        print(red("  FAIL: Edit button not found"))
        return 1
    await tap_frame(reader, edit_btn)
    await asyncio.sleep(1.2)

    # Scroll until "add birthday" is in view
    for _ in range(6):
        obs = await observe_raw(reader)
        bday_cell = find_in_elements(obs["elements"], role="cell",
                                       label_contains="add birthday")
        if bday_cell:
            break
        await reader._send({"type": "swipe", "direction": "up"})
        await asyncio.sleep(0.5)
    else:
        print(red("  FAIL: 'add birthday' cell not visible"))
        return 1

    await tap_frame(reader, bday_cell)
    await asyncio.sleep(1.5)

    # The picker should now be expanded — multiple [adj] elements
    obs = await observe_raw(reader)
    adj = await dump_first_n_adj(obs["elements"], n=10,
                                    label="after tap 'add birthday'")

    fails += check("≥3 adjustable elements (compact + 3-4 wheel columns)",
                    len(adj) >= 3, f"got {len(adj)}")

    # Date-shaped values (month names or "----")
    date_values = [a for a in adj
                    if any(c in (a.get("value") or "")
                            for c in ["May", "Jan", "Feb", "Mar", "Apr",
                                       "Jun", "Jul", "Aug", "Sep", "Oct",
                                       "Nov", "Dec", "----"])]
    fails += check("at least one [adj] has date-shaped value",
                    len(date_values) >= 1,
                    f"got {len(date_values)}")
    return fails, adj  # Return adj for the timing scenario


# ──────────────────────────────────────────────────────────────────────────
# Scenario 4 — settle-skip timing benchmark on the year wheel
# ──────────────────────────────────────────────────────────────────────────

async def scenario_settle_timing(reader, adj_from_birthday):
    print()
    print("=" * 70)
    print("Scenario 4: settle-skip timing — 10-swipe batch comparison")
    print("=" * 70)
    fails = 0
    if not adj_from_birthday:
        print(yellow("  SKIP: no adjustable elements available from prior scenario"))
        return 0

    # Pick the last [adj] (usually the year wheel) — it's a vertical
    # column wheel which is the closest analog to a real picker
    wheel = adj_from_birthday[-1]
    f = wheel["frame"]
    if f.get("height", 0) < 10:
        print(yellow("  SKIP: chosen wheel frame too small for swipe"))
        return 0
    cx = f["x"] + f["width"]/2
    top = f["y"] + 5
    bot = f["y"] + f["height"] - 5
    print(f"  using wheel ref={wheel.get('ref')} role={wheel.get('role')} "
          f"frame=({f['x']:.0f},{f['y']:.0f},"
          f"{f['width']:.0f}×{f['height']:.0f})")

    # OLD behavior — settle=True on every swipe
    t0 = time.time()
    for _ in range(10):
        await reader.swipe_at(cx, top, cx, bot, settle=True)
    t_old = time.time() - t0
    print(f"  OLD (settle=True per swipe):  {t_old:.2f}s for 10 swipes")

    await asyncio.sleep(1.0)  # let wheel settle naturally

    # NEW behavior — settle=False except last
    t0 = time.time()
    for i in range(10):
        is_last = (i == 9)
        await reader.swipe_at(cx, top, cx, bot, settle=is_last)
    t_new = time.time() - t0
    print(f"  NEW (settle on last only):    {t_new:.2f}s for 10 swipes")

    speedup = t_old / t_new if t_new > 0 else float("inf")
    print(f"  speedup: {speedup:.1f}×")
    fails += check("NEW is at least 2× faster",
                    speedup >= 2.0,
                    f"old={t_old:.1f}s new={t_new:.1f}s")
    return fails


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

async def main():
    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()
    total = 0
    summary = {}
    try:
        async with reader._lock:
            try:
                f = await scenario_calendar_time(reader)
                summary["Calendar time"] = f
                total += f
            except Exception as e:
                print(red(f"  Exception: {e}"))
                summary["Calendar time"] = 99; total += 1

            try:
                f = await scenario_settings_brightness(reader)
                summary["Settings brightness"] = f
                total += f
            except Exception as e:
                print(red(f"  Exception: {e}"))
                summary["Settings brightness"] = 99; total += 1

            adj_from_birthday = []
            try:
                result = await scenario_contacts_birthday(reader)
                if isinstance(result, tuple):
                    f, adj_from_birthday = result
                else:
                    f = result
                summary["Contacts birthday"] = f
                total += f
            except Exception as e:
                print(red(f"  Exception: {e}"))
                summary["Contacts birthday"] = 99; total += 1

            try:
                f = await scenario_settle_timing(reader, adj_from_birthday)
                summary["Settle timing"] = f
                total += f
            except Exception as e:
                print(red(f"  Exception: {e}"))
                summary["Settle timing"] = 99; total += 1
    finally:
        await reader.stop()

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for k, v in summary.items():
        mark = green("PASS") if v == 0 else red(f"FAIL ({v})")
        print(f"  {k:>22}: {mark}")
    print()
    if total == 0:
        print(green("All scenarios passed."))
    else:
        print(red(f"{total} failure(s) total."))
    sys.exit(0 if total == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
