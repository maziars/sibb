#!/usr/bin/env python3
"""Probe: dump the full AX tree of Calendar.app's Edit Event screen.

Why: the LLM agent reaches the Edit Event screen but cannot find a
tappable title input field. The standard observation filtering (only
labelled+visible+actionable) may be hiding the field, OR iOS Calendar
genuinely doesn't expose the title field as a standard AXTextField in
the AX tree. This probe seeds one event, drives the UI to the Edit
screen, and dumps EVERY element (no filtering) so we can see what
the title field actually looks like.

Run:
    /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb/simulator/sibb_probe_calendar_edit.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader  # noqa: E402

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"


def fmt_element(e: dict, indent: int = 0) -> str:
    role = e.get("role") or "?"
    label = (e.get("label") or "").replace("\n", " ")
    value = e.get("value")
    frm = e.get("frame") or {}
    x, y = frm.get("x", 0), frm.get("y", 0)
    w, h = frm.get("width", 0), frm.get("height", 0)
    enabled = e.get("enabled")
    visible = e.get("visible")
    focused = e.get("focused")
    identifier = e.get("identifier")
    flags = []
    if not enabled: flags.append("disabled")
    if not visible: flags.append("invisible")
    if focused: flags.append("focused")
    flag_str = f" [{','.join(flags)}]" if flags else ""
    pad = " " * indent
    parts = [
        f"{pad}{role:20s} label={label!r:40s}",
        f" frame=({x:.0f},{y:.0f},{w:.0f},{h:.0f})",
    ]
    if value is not None and value != "":
        parts.append(f" value={value!r}")
    if identifier:
        parts.append(f" id={identifier!r}")
    parts.append(flag_str)
    return "".join(parts)


async def main():
    today = date.today()
    target_date = today + timedelta(days=2)  # day-after-tomorrow to avoid "Today" view confusion
    target_iso = target_date.isoformat()
    title = "Probe Date Night"

    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()
    try:
        async with reader._lock:
            # 1. Clean state
            print("=== Cleaning Calendar ===")
            await reader._send({"type": "wipe_events"})

            # 2. Seed one event
            print(f"=== Seeding '{title}' on {target_iso} 14:00-15:00 ===")
            resp = await reader._send({
                "type": "create_event",
                "title": title,
                "start_iso": f"{target_iso}T14:00:00",
                "end_iso":   f"{target_iso}T15:00:00",
            })
            if not resp.get("ok"):
                print(f"  ERROR: {resp.get('error')}")
                return 1

            # 3. Launch Calendar
            print("\n=== Launching Calendar.app ===")
            await reader._send({
                "type": "launch", "bundleId": "com.apple.mobilecal",
            })
            await asyncio.sleep(2.0)

            # 4. Dump initial calendar view + diagnostic labels
            print(f"\n=== Calendar view (after launch) ===")
            raw = await reader._send({"type": "observe",
                                       "bundleId": "com.apple.mobilecal"})
            els = raw.get("elements") or []
            print(f"  bundle={raw.get('bundle_id')}  els={len(els)}  kb={raw.get('keyboard_visible')}")
            print(f"  First 30 labelled buttons/cells:")
            shown = 0
            for e in els:
                if e.get("role") in ("AXButton", "AXCell") and (e.get("label") or "").strip():
                    frm = e.get("frame") or {}
                    print(f"    {e.get('role'):12s} {e['label']!r}  @({frm.get('x',0):.0f},{frm.get('y',0):.0f})")
                    shown += 1
                    if shown >= 30: break

            # 5. Look for seeded event ANYWHERE in tree (skip day-button hop)
            print(f"\n=== Searching tree for '{title}' ===")
            event_btn = None
            for e in els:
                if title in (e.get("label") or ""):
                    event_btn = e
                    break

            # 5a. If not visible at initial view, try tapping the day button by date
            if not event_btn:
                target_day_short = target_date.strftime("%A, %B %-d")
                print(f"  Event not visible; trying day-button hop to {target_day_short!r}")
                for e in els:
                    lab = e.get("label") or ""
                    if (e.get("role") == "btn"
                            and target_day_short in lab):
                        frm = e.get("frame") or {}
                        cx = frm.get("x", 0) + frm.get("width", 0) / 2
                        cy = frm.get("y", 0) + frm.get("height", 0) / 2
                        print(f"    Tapping {lab!r} at ({cx:.0f},{cy:.0f})")
                        await reader._send({"type": "tap", "x": float(cx), "y": float(cy)})
                        await asyncio.sleep(1.5)
                        break
                # Re-observe
                raw = await reader._send({"type": "observe"})
                els = raw.get("elements") or []
                print(f"    Post-hop els={len(els)}")
                for e in els:
                    if title in (e.get("label") or ""):
                        event_btn = e
                        break

            if not event_btn:
                print(f"\n  STILL no event found. All AXButton labels in tree:")
                for e in els:
                    if e.get("role") == "btn":
                        lab = (e.get("label") or "").strip()
                        if lab:
                            print(f"    {lab!r}")
                return 1

            els2 = els  # use the latest observation for event-tap step
            print(f"\n=== Found event: {event_btn['label']!r} ===")

            frm = event_btn.get("frame") or {}
            cx, cy = frm.get("x",0)+frm.get("width",0)/2, frm.get("y",0)+frm.get("height",0)/2
            print(f"  Tapping event {event_btn['label']!r} at ({cx:.0f},{cy:.0f})")
            await reader._send({"type": "tap", "x": float(cx), "y": float(cy)})
            await asyncio.sleep(1.5)

            # 7. Now on event detail view — tap Edit
            raw3 = await reader._send({"type": "observe"})
            els3 = raw3.get("elements") or []
            print(f"\n=== Event detail view  els={len(els3)} ===")
            edit_btn = None
            for e in els3:
                if (e.get("label") or "") == "Edit" and e.get("role") == "btn":
                    edit_btn = e
                    break
            if not edit_btn:
                print("  No 'Edit' button found! Buttons visible:")
                for e in els3:
                    if e.get("role") == "btn":
                        print(f"    {(e.get('label') or '')!r} @ {e.get('frame')}")
                return 1

            frm = edit_btn.get("frame") or {}
            cx, cy = frm.get("x",0)+frm.get("width",0)/2, frm.get("y",0)+frm.get("height",0)/2
            print(f"  Tapping Edit at ({cx:.0f},{cy:.0f})")
            await reader._send({"type": "tap", "x": float(cx), "y": float(cy)})
            await asyncio.sleep(1.5)

            # 8. FULL DUMP of Edit Event screen
            print("\n" + "=" * 78)
            print("  EDIT EVENT SCREEN — FULL AX TREE (no filtering)")
            print("=" * 78)
            raw4 = await reader._send({"type": "observe"})
            els4 = raw4.get("elements") or []
            kb = raw4.get("keyboard_visible")
            bundle = raw4.get("bundle_id")
            print(f"  bundle={bundle}  els={len(els4)}  kb={kb}")
            print()
            for e in els4:
                print(fmt_element(e))
            print()
            print("=" * 78)
            print("  ROLE HISTOGRAM (so we can see what types of elements exist)")
            print("=" * 78)
            roles = {}
            for e in els4:
                r = e.get("role") or "?"
                roles[r] = roles.get(r, 0) + 1
            for r, n in sorted(roles.items(), key=lambda kv: -kv[1]):
                print(f"  {r:30s} {n}")

            # 9. Now TRY tapping where 'Probe Date Night' text is, then re-observe
            print("\n=== Attempting TAP on the title input + post-tap observe ===")
            title_el = None
            # First look for an input whose VALUE matches the title
            for e in els4:
                val = e.get("value") or ""
                if title in val and e.get("role") == "input":
                    title_el = e
                    break
            # Fallback: any element whose label contains the title
            if not title_el:
                for e in els4:
                    lab = e.get("label") or ""
                    if title in lab and e.get("role") in (
                        "input", "text", "cell", "other"
                    ):
                        title_el = e
                        break
            if title_el:
                frm = title_el.get("frame") or {}
                cx, cy = frm.get("x",0)+frm.get("width",0)/2, frm.get("y",0)+frm.get("height",0)/2
                print(f"  Found title-bearing element: role={title_el.get('role')} "
                      f"label={title_el.get('label')!r} frame={frm}")
                print(f"  TAPping at ({cx:.0f},{cy:.0f})")
                await reader._send({"type": "tap", "x": float(cx), "y": float(cy)})
                await asyncio.sleep(1.5)
                raw5 = await reader._send({"type": "observe"})
                els5 = raw5.get("elements") or []
                kb5 = raw5.get("keyboard_visible")
                print(f"\n  After tap:  els={len(els5)}  kb={kb5}")
                if kb5:
                    print("  ✓ Keyboard came up — title field IS focusable")
                    # Find any AXTextField or AXTextView in the post-tap tree
                    fields = [e for e in els5 if e.get("role") in ("input", "text", "cell")]
                    for f in fields[:10]:
                        print(f"    {fmt_element(f)}")
                else:
                    print("  ✗ Keyboard did NOT come up — investigating further")
                    # 9a. Screenshot the current state
                    import subprocess
                    subprocess.run(["xcrun", "simctl", "io", UDID, "screenshot",
                                    "/tmp/simcheck/edit_after_tap.png"],
                                   capture_output=True)
                    print("  screenshot → /tmp/simcheck/edit_after_tap.png")
                    # 9b. Find input element in the post-tap tree and check its focused state
                    for f in els5:
                        if f.get("role") == "input":
                            print(f"    POST-TAP input: {fmt_element(f)}")
                    # 9c. Try TYPE directly — maybe focus is set even without visible keyboard
                    print("\n  Trying TYPE 'Vet Visit' to see if focus is silently set...")
                    type_resp = await reader._send({"type": "type", "text": "Vet Visit"})
                    print(f"    type response: ok={type_resp.get('ok')} err={type_resp.get('error')}")
                    await asyncio.sleep(1.0)
                    raw6 = await reader._send({"type": "observe"})
                    els6 = raw6.get("elements") or []
                    print(f"    After TYPE: els={len(els6)}  kb={raw6.get('keyboard_visible')}")
                    for e in els6:
                        if e.get("role") == "input":
                            print(f"    POST-TYPE input: {fmt_element(e)}")
                    # 9d. Take another screenshot
                    subprocess.run(["xcrun", "simctl", "io", UDID, "screenshot",
                                    "/tmp/simcheck/edit_after_type.png"],
                                   capture_output=True)
                    print("  screenshot → /tmp/simcheck/edit_after_type.png")
            else:
                print("  No title-bearing element found in the unfiltered tree.")

            # 10. NEW: probe what happens to the [input] AFTER tapping
            # the "Clear text" button. Does the input become invisible,
            # disappear from the tree, or just have value=''?
            print("\n" + "=" * 78)
            print("  POST-CLEAR PROBE: tap Clear text, then dump input state")
            print("=" * 78)
            # Re-observe (in case we already typed something during the
            # previous step)
            raw_pre = await reader._send({"type": "observe"})
            els_pre = raw_pre.get("elements") or []
            clear_btn = None
            for e in els_pre:
                if (e.get("label") or "") == "Clear text":
                    clear_btn = e
                    break
            if not clear_btn:
                print("  No 'Clear text' button visible. Maybe the field is "
                      "already empty?")
            else:
                frm = clear_btn.get("frame") or {}
                cx = frm.get("x", 0) + frm.get("width", 0) / 2
                cy = frm.get("y", 0) + frm.get("height", 0) / 2
                print(f"  Tapping Clear text at ({cx:.0f},{cy:.0f})")
                await reader._send({"type": "tap", "x": float(cx), "y": float(cy)})
                await asyncio.sleep(1.0)

            raw_post = await reader._send({"type": "observe"})
            els_post = raw_post.get("elements") or []
            print(f"  After clear: els={len(els_post)}  kb={raw_post.get('keyboard_visible')}")
            print(f"  All [input]/[textarea] elements in unfiltered tree:")
            found = False
            for e in els_post:
                if e.get("role") in ("input", "textarea"):
                    found = True
                    print(f"    {fmt_element(e)}")
            if not found:
                print(f"    (NONE — the input element is genuinely gone from "
                      f"the AX tree)")

            # Save screenshot for visual confirmation
            import subprocess
            subprocess.run(["xcrun", "simctl", "io", UDID, "screenshot",
                            "/tmp/simcheck/edit_after_clear.png"],
                           capture_output=True)
            print(f"  screenshot → /tmp/simcheck/edit_after_clear.png")
    finally:
        await reader.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
