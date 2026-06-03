#!/usr/bin/env python3
"""Empirical probe: investigate why the scaffold's "fully-visible"
filter (sibb_scaffold._read_xcuitest) leaks elements that aren't on
screen.

User-reported 2026-05-27: on iOS Contacts new-contact form with the
keyboard up, the agent saw many elements that were not visible (off
screen below the keyboard, scrolled past, etc.). Expected behavior:
filter removes any element whose frame isn't fully within the screen
viewport AND non-keyboard region (1px tolerance, focused element
exempt).

This probe is observation-only — does NOT modify SIBB code. Drives
the sim to the Contacts new-contact form, takes ONE raw observation,
and prints:

  - screen dimensions reported by Swift
  - keyboard_visible flag + keyboard_frame
  - for every element: ref, role, label, frame, focused
  - the would-be filter verdict (KEEP / FILTER) with the deciding rule

If the agent's observation contains elements the filter SHOULD have
caught, the deciding-rule output tells us exactly which check let
them through.

Run:
    SIBB_UDID=19B95A95-614A-4ECA-B943-44FDADFD7A9F \\
        /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb/simulator/sibb_probe_visibility_filter.py
"""
from __future__ import annotations
import asyncio
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIBB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(SIBB, "benchmark"))

from sibb_xcuitest_client import XCUITestReader  # noqa: E402
from sibb_state import MessagesHandler           # noqa: E402

UDID = os.environ.get(
    "SIBB_UDID", "19B95A95-614A-4ECA-B943-44FDADFD7A9F")
MSGS = "com.apple.MobileSMS"
CONTACTS = "com.apple.MobileAddressBook"

TOL = 1.0   # mirrors the constant in sibb_scaffold._read_xcuitest


def shell(cmd, timeout=15):
    return subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)


def visibility_verdict(frame, focused, kb_y_top, screen_w, screen_h):
    """Mirror of sibb_scaffold._is_fully_visible. Returns
    (keep: bool, reason: str). KEEP=True iff the element passes
    every check OR is the focused element (which bypasses)."""
    if focused:
        return True, "focused-exempt"
    if frame is None:
        return False, "no-frame"
    x = frame.get("x", 0); y = frame.get("y", 0)
    w = frame.get("width", 0); h = frame.get("height", 0)
    if x < -TOL:
        return False, f"clipped-left (x={x:.0f} < 0)"
    if y < -TOL:
        return False, f"clipped-top (y={y:.0f} < 0)"
    if x + w > screen_w + TOL:
        return False, (f"clipped-right "
                       f"(x+w={x+w:.0f} > screen_w={screen_w})")
    if y + h > screen_h + TOL:
        return False, (f"clipped-bottom "
                       f"(y+h={y+h:.0f} > screen_h={screen_h})")
    if kb_y_top is not None and y + h > kb_y_top + TOL:
        return False, (f"kb-occluded "
                       f"(y+h={y+h:.0f} > kb_top={kb_y_top})")
    return True, "fully-visible"


async def main():
    # Pre-warm: launch fresh Messages so the inbound message is fresh.
    print(f"UDID: {UDID}")
    print("Resetting Messages so we can navigate to a known thread…")
    shell(f"xcrun simctl terminate {UDID} {MSGS}")
    shell(f"xcrun simctl terminate {UDID} {CONTACTS}")
    await asyncio.sleep(0.5)

    reader = XCUITestReader(UDID)
    await reader.start()
    print("XCUITest server connected.")

    # Use MessagesHandler to seed an inbound iMessage so we land in
    # the same state the agent encounters.
    handler = MessagesHandler(reader)
    seed_text = ("Riley Taylor's home is 350 5th Avenue, "
                 "New York")
    spec = {"app": "Messages", "type": "send_in_thread",
             "thread": "JA", "text": seed_text}
    print("Seeding message via MessagesHandler…")
    await handler.apply(spec)
    print("Message seeded. Agent would now see the inbound bubble in KB.")
    await asyncio.sleep(1.0)

    # Manually drive to the Contacts new-contact sheet via Messages.
    # 1. Re-open the KB thread (handler pressed home post-send).
    # 2. Tap the sender phone → "Create New Contact".
    print("Opening Messages KB thread + Create New Contact sheet…")
    await reader.launch(bundle_id=MSGS)
    await asyncio.sleep(1.0)
    # Tap the KB thread cell (look for one containing "(555)" — KB number).
    raw = await reader._send({"type": "observe", "bundleId": MSGS})
    cells = [e for e in (raw.get("elements") or [])
              if e.get("role") == "cell" and "(555)" in (e.get("label") or "")]
    if not cells:
        print("Couldn't find KB inbox cell. Bailing.")
        await reader.stop()
        return
    fr = cells[0]["frame"]
    cx = fr["x"] + fr["width"]/2
    cy = fr["y"] + fr["height"]/2
    await reader.tap(x=cx, y=cy)
    await asyncio.sleep(1.0)
    # Tap the phone number at top of thread.
    raw = await reader._send({"type": "observe", "bundleId": MSGS})
    phone_btns = [e for e in (raw.get("elements") or [])
                   if "+1 (555)" in (e.get("label") or "")]
    if not phone_btns:
        print("Couldn't find sender phone button. Bailing.")
        await reader.stop()
        return
    fr = phone_btns[0]["frame"]
    await reader.tap(x=fr["x"] + fr["width"]/2,
                     y=fr["y"] + fr["height"]/2)
    await asyncio.sleep(1.0)
    # Tap "Create New Contact".
    raw = await reader._send({"type": "observe", "bundleId": MSGS})
    btns = [e for e in (raw.get("elements") or [])
             if "Create New Contact" in (e.get("label") or "")]
    if not btns:
        print("Couldn't find Create New Contact button. Bailing.")
        await reader.stop()
        return
    fr = btns[0]["frame"]
    await reader.tap(x=fr["x"] + fr["width"]/2,
                     y=fr["y"] + fr["height"]/2)
    await asyncio.sleep(2.0)
    print("Should now be on the new-contact form (First name focused).")

    # Mirror the agent's path: type into First, type into Last,
    # tap "add address" — that's where the agent sees the off-screen
    # leak.
    await reader.type_text("Riley")
    await asyncio.sleep(0.5)
    # Tab to Last name via a controlled tap (look for the "Last name"
    # input in the current AX tree). Don't swipe — we want kb up.
    raw = await reader._send({"type": "observe", "bundleId": MSGS})
    last_inputs = [e for e in (raw.get("elements") or [])
                    if "Last name" in (e.get("label") or "")
                    and e.get("role") == "input"]
    if last_inputs:
        fr = last_inputs[0]["frame"]
        await reader.tap(x=fr["x"] + fr["width"]/2,
                          y=fr["y"] + fr["height"]/2)
        await asyncio.sleep(0.4)
    await reader.type_text("Taylor")
    await asyncio.sleep(0.5)
    # Now scroll the FORM (not whole-app) by tapping the body region.
    # Actually we want to tap "add address" if visible — but it may be
    # below the fold. Issue a SCROLL via swipe but TARGET a coord NOT
    # below the keyboard top, so keyboard stays up.
    # Easier: just observe in the current state (kb up, Last focused).
    await asyncio.sleep(0.5)

    # THE PROBE: capture the raw observation as the scaffold would see it.
    print("\n" + "="*70)
    print("PROBE OBSERVATION (form in keyboard-up state)")
    print("="*70)
    raw = await reader._send({"type": "observe", "bundleId": MSGS})
    kb_frame = raw.get("keyboard_frame")
    screen_w = raw.get("screen_width", 402)
    screen_h = raw.get("screen_height", 874)
    kb_y_top = kb_frame.get("y") if kb_frame else None
    print(f"screen_width:  {screen_w}")
    print(f"screen_height: {screen_h}")
    print(f"keyboard_visible: {raw.get('keyboard_visible')}")
    print(f"keyboard_frame:   {kb_frame}")
    print(f"kb_y_top:         {kb_y_top}")
    print(f"element count:    {len(raw.get('elements') or [])}")

    elements = raw.get("elements") or []
    print("\n%-4s  %-12s  %-30s  %-30s  %s" %
          ("KEEP", "ROLE", "LABEL", "FRAME", "REASON"))
    print("-" * 130)
    kept = 0; filtered = 0
    for e in elements:
        role = e.get("role") or "?"
        lbl = (e.get("label") or "")[:28]
        fr = e.get("frame") or {}
        focused = bool(e.get("focused"))
        fstr = f"({fr.get('x', 0):.0f},{fr.get('y', 0):.0f}) " \
               f"{fr.get('width', 0):.0f}x{fr.get('height', 0):.0f}"
        keep, reason = visibility_verdict(fr, focused, kb_y_top,
                                           screen_w, screen_h)
        flag = "KEEP" if keep else "DROP"
        focus_mark = " *" if focused else ""
        print(f"{flag}  {role:12s}  {lbl:30s}  {fstr:30s}  {reason}{focus_mark}")
        if keep:
            kept += 1
        else:
            filtered += 1
    print("-" * 130)
    print(f"\nSUMMARY: {kept} kept, {filtered} filtered, "
          f"{len(elements)} total")

    await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
