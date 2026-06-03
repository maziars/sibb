#!/usr/bin/env python3
"""Empirical probe: investigate why the agent's TAP @phone_field
fails to transfer focus on the iOS Contacts new-contact sheet
(reached via Messages → "Create New Contact").

The probe is observation-only — does NOT modify SIBB code. Reproduces
the agent's state manually, captures screenshots + AX dumps at each
interesting moment, and tries several tap variations to characterize
the failure mode before we decide whether (and how) to fix.

Probe steps:
  1. Reset Contacts + Messages
  2. Seed an inbound iMessage via MessagesHandler (lands the agent
     inside the KB thread per our post-send navigation)
  3. From the KB thread, tap the sender phone → tap "Create New Contact"
  4. Type "Riley" into First name, "Jones" into Last name (mirroring
     the failed trial)
  5. **Pre-tap snapshot** — screenshot + AX dump. Note phone field's
     reported frame, keyboard's frame (if exposed), focused element
  6. **Variation A** — TAP at the phone field's reported center
     (the agent's path). Wait, observe. Did focus move?
  7. **Variation B** — TAP higher (y = phone_y - 40). Did focus move?
  8. **Variation C** — TAP empty area first to dismiss keyboard,
     then TAP phone field. Did focus move?
  9. **Variation D** — TAP the "Clear text" button next to the phone
     field's existing value. Did focus move?

Output goes to stdout + a per-step screenshot dir. No assertions —
just descriptive output for human review.

Run:
    SIBB_UDID=19B95A95-614A-4ECA-B943-44FDADFD7A9F \\
        /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb/simulator/sibb_probe_contacts_phone_focus.py
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

from sibb_xcuitest_client import XCUITestReader  # noqa
from sibb_state import MessagesHandler            # noqa

UDID = os.environ.get(
    "SIBB_UDID", "19B95A95-614A-4ECA-B943-44FDADFD7A9F")
MSGS = "com.apple.MobileSMS"
CONTACTS = "com.apple.MobileAddressBook"
OUT = os.path.join(SIBB, "..", "probes_out",
                    "contacts_phone_focus_2026-05-27")
os.makedirs(OUT, exist_ok=True)


def shell(cmd, timeout=10):
    return subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)


def screenshot(name):
    path = os.path.join(OUT, name)
    shell(f"xcrun simctl io {UDID} screenshot '{path}'", timeout=10)
    return path


def banner(msg):
    print("\n" + "=" * 70)
    print("  " + msg)
    print("=" * 70)


async def obs(r, bundle):
    raw = await r._send({"type": "observe", "bundleId": bundle})
    return raw.get("elements") or []


def find_focused(els):
    for e in els:
        if e.get("focused"):
            return e
    return None


def find_by_label(els, label_substr, role_in=None):
    s = label_substr.lower()
    for e in els:
        if s in (e.get("label") or "").lower():
            if role_in is None or e.get("role") in role_in:
                return e
    return None


def find_keyboard_top(els):
    """Find the topmost y of any element whose role/label suggests
    a keyboard. iOS doesn't expose 'keyboard' as a role directly via
    XCUITest, but key labels often appear as buttons in the bottom
    area, and there's sometimes a container with label='Keyboard'."""
    candidates_y = []
    for e in els:
        lbl = (e.get("label") or "").lower()
        if "keyboard" in lbl:
            fr = e.get("frame") or {}
            candidates_y.append(fr.get("y", 0))
    # Also look for the cluster of single-char button labels (keyboard
    # keys) — their top edge approximates the keyboard top.
    key_btn_ys = []
    for e in els:
        if e.get("role") != "btn":
            continue
        lbl = (e.get("label") or "")
        if 1 <= len(lbl) <= 2 and lbl.replace(".", "").isalnum():
            fr = e.get("frame") or {}
            key_btn_ys.append(fr.get("y", 0))
    if key_btn_ys:
        candidates_y.append(min(key_btn_ys))
    return min(candidates_y) if candidates_y else None


def dump_relevant(els):
    """Print the frame + role + label of the phone field, name fields,
    keyboard elements, focused element. Compact and human-readable."""
    print("  — Relevant elements —")
    for label_part in ["First name", "Last name", "Phone",
                        "mobile", "Cancel", "Done", "Clear text"]:
        for e in els:
            if label_part.lower() in (e.get("label") or "").lower():
                fr = e.get("frame") or {}
                fc = (e.get("focused"), e.get("adjustable"),
                       e.get("role"))
                print(f"    {label_part!r}: role={e.get('role')!r} "
                      f"frame=(x={fr.get('x', 0):.0f},"
                      f"y={fr.get('y', 0):.0f},"
                      f"w={fr.get('width', 0):.0f},"
                      f"h={fr.get('height', 0):.0f}) "
                      f"focused={e.get('focused')} "
                      f"adjustable={e.get('adjustable')} "
                      f"value={(e.get('value') or '')[:40]!r}")
                break
    foc = find_focused(els)
    if foc:
        fr = foc.get("frame") or {}
        print(f"  FOCUSED: role={foc.get('role')!r} "
              f"label={(foc.get('label') or '')[:30]!r} "
              f"frame=(x={fr.get('x', 0):.0f},"
              f"y={fr.get('y', 0):.0f},"
              f"w={fr.get('width', 0):.0f},"
              f"h={fr.get('height', 0):.0f})")
    kb_top = find_keyboard_top(els)
    if kb_top is not None:
        print(f"  KEYBOARD TOP (approx): y={kb_top:.0f}")
    else:
        print(f"  KEYBOARD TOP: not detected from AX")


async def main():
    r = XCUITestReader(UDID)
    await r.start()

    # ── Step 1: seed message + navigate to KB thread ────────────────
    banner("STEP 1: seed message via MessagesHandler")
    h = MessagesHandler(reader=r)
    # Wipe contacts so we have a clean slate
    await r._send({"type": "wipe_contacts"})
    # Send to JA → loopback inbound in KB → MessagesHandler
    # navigates back to inbox → deletes JA → PRESS home. So we
    # restart from springboard then re-open Messages and the KB
    # thread manually.
    await h.apply({"app": "Messages", "type": "send_in_thread",
                    "thread": "JA",
                    "text": "Forwarding Riley Jones's number "
                            "for you: 408-555-5422"})
    await asyncio.sleep(1.0)
    # Open Messages, tap the KB thread cell, then the sender phone.
    await r.launch(bundle_id=MSGS)
    await asyncio.sleep(2.5)
    els = await obs(r, MSGS)
    kb_cell = next((e for e in els
                    if e.get("role") == "cell"
                    and "(555)" in (e.get("label") or "")), None)
    if kb_cell is None:
        print("ERR: no KB cell — pre-runner may not have deleted JA")
        return
    fr = kb_cell["frame"]
    await r.tap(x=fr["x"] + fr["width"]/2, y=fr["y"] + fr["height"]/2)
    await asyncio.sleep(1.2)
    # Tap the sender phone in the title bar.
    els = await obs(r, MSGS)
    phone_btn = next((e for e in els
                      if e.get("role") == "btn"
                      and "+1" in (e.get("label") or "")), None)
    if phone_btn is None:
        print("ERR: no sender phone button")
        return
    fr = phone_btn["frame"]
    await r.tap(x=fr["x"] + fr["width"]/2, y=fr["y"] + fr["height"]/2)
    await asyncio.sleep(1.0)
    # Tap "Create New Contact".
    els = await obs(r, MSGS)
    create_btn = next((e for e in els
                       if (e.get("label") or "") == "Create New Contact"),
                      None)
    if create_btn is None:
        print("ERR: no Create New Contact button")
        return
    fr = create_btn["frame"]
    await r.tap(x=fr["x"] + fr["width"]/2, y=fr["y"] + fr["height"]/2)
    await asyncio.sleep(1.5)
    screenshot("01_new_contact_sheet.png")

    # ── Step 2: type Riley + Jones ──────────────────────────────────
    banner("STEP 2: type First name + Last name")
    els = await obs(r, MSGS)
    fn = find_by_label(els, "first name", role_in=("input", "adj"))
    if fn is None:
        print("ERR: no First name field")
        return
    fr = fn["frame"]
    await r.tap(x=fr["x"] + fr["width"]/2, y=fr["y"] + fr["height"]/2)
    await asyncio.sleep(0.6)
    await r.type_text("Riley")
    await asyncio.sleep(0.4)
    # Tab to Last name
    els = await obs(r, MSGS)
    ln = find_by_label(els, "last name", role_in=("input", "adj"))
    fr = ln["frame"]
    await r.tap(x=fr["x"] + fr["width"]/2, y=fr["y"] + fr["height"]/2)
    await asyncio.sleep(0.6)
    await r.type_text("Jones")
    await asyncio.sleep(0.4)
    screenshot("02_after_name_typed.png")

    # ── Step 3: pre-tap snapshot ────────────────────────────────────
    banner("STEP 3: pre-tap snapshot — find phone field + keyboard")
    els = await obs(r, MSGS)
    dump_relevant(els)
    # Capture phone field location for variation runs.
    phone_field = None
    for e in els:
        lbl = (e.get("label") or "")
        val = (e.get("value") or "")
        if "+1 (555)" in (lbl + val):
            if e.get("role") in ("input", "adj"):
                phone_field = e
                break
    if phone_field is None:
        # Fallback: find an input with role=='input' whose label is empty
        # and that's positioned below Last name.
        for e in els:
            if e.get("role") not in ("input", "adj"):
                continue
            fr = e.get("frame") or {}
            if fr.get("y", 0) > 540:  # below the name fields
                phone_field = e
                print(f"  (fallback) phone-like field: {phone_field}")
                break
    if phone_field is None:
        print("ERR: no phone field found at all")
        await r.stop()
        return
    pf_frame = phone_field["frame"]
    px = pf_frame["x"] + pf_frame["width"]/2
    py = pf_frame["y"] + pf_frame["height"]/2
    print(f"\n  Phone field center: ({px:.0f}, {py:.0f})")
    print(f"  Phone field frame:  x={pf_frame['x']:.0f}, "
          f"y={pf_frame['y']:.0f}, w={pf_frame['width']:.0f}, "
          f"h={pf_frame['height']:.0f}")

    async def tap_then_observe(x, y, label):
        banner(f"VARIATION: {label} — tap @({x:.0f}, {y:.0f})")
        await r.tap(x=x, y=y)
        await asyncio.sleep(0.8)
        els_after = await obs(r, MSGS)
        screenshot(f"after_{label.replace(' ', '_').lower()}.png")
        foc = find_focused(els_after)
        if foc:
            fr = foc.get("frame") or {}
            print(f"  FOCUSED post-tap: role={foc.get('role')!r} "
                  f"label={(foc.get('label') or '')[:30]!r} "
                  f"frame=(x={fr.get('x', 0):.0f},"
                  f"y={fr.get('y', 0):.0f},"
                  f"w={fr.get('width', 0):.0f},"
                  f"h={fr.get('height', 0):.0f})")
            in_phone = (fr.get("x", 0) <= x
                        <= fr.get("x", 0) + fr.get("width", 0)
                        and fr.get("y", 0) <= y
                        <= fr.get("y", 0) + fr.get("height", 0))
            print(f"  Focused frame contains tap point: {in_phone}")
        else:
            print(f"  FOCUSED post-tap: none")

    # ── Variation A: tap phone field's reported center ──────────────
    await tap_then_observe(px, py, "A_phone_center")

    # ── Variation B: tap slightly higher (above keyboard, maybe) ────
    await tap_then_observe(px, py - 40, "B_phone_minus40")

    # ── Variation C: dismiss keyboard first (tap form body) then phone
    # Tap an area near the top of the sheet — the title bar at y=140
    # should be safe.
    banner("VARIATION C: tap top sheet area to dismiss kb → then phone")
    await r.tap(x=200, y=140)
    await asyncio.sleep(0.7)
    screenshot("after_C_dismiss_attempt.png")
    els_mid = await obs(r, MSGS)
    foc = find_focused(els_mid)
    foc_label = repr(foc.get('label')) if foc else "none"
    print(f"  After dismiss attempt: focused={foc_label}")
    kb_top_now = find_keyboard_top(els_mid)
    print(f"  Keyboard top now: y={kb_top_now}")
    # Now tap the phone field again
    await tap_then_observe(px, py, "C_phone_after_dismiss")

    # ── Variation D: tap the Clear text button next to phone ────────
    els_d = await obs(r, MSGS)
    clear_btns = [e for e in els_d if e.get("role") == "btn"
                   and (e.get("label") or "") == "Clear text"]
    print(f"\n  Clear-text buttons found: {len(clear_btns)}")
    # Pick the one closest to the phone field's y
    if clear_btns and phone_field:
        target = min(clear_btns,
                     key=lambda e: abs((e.get('frame') or {}).get('y', 0) - py))
        fr = target["frame"]
        cx = fr["x"] + fr["width"]/2
        cy = fr["y"] + fr["height"]/2
        await tap_then_observe(cx, cy, "D_clear_text_btn")

    # ── Final: dump all elements at the end for debugging ───────────
    banner("FINAL: full AX dump")
    els_final = await obs(r, MSGS)
    for e in els_final:
        role = e.get("role", "?")
        lbl = (e.get("label") or "")[:40]
        val = (e.get("value") or "")[:30]
        fr = e.get("frame") or {}
        if fr.get("y", 0) > 400 or e.get("focused"):
            print(f"  {role:8s} y={fr.get('y', 0):.0f} "
                  f"focused={e.get('focused')} "
                  f"label={lbl!r} value={val!r}")
    print(f"\nScreenshots saved to: {OUT}")
    await r.stop()


if __name__ == "__main__":
    asyncio.run(main())
