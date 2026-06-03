#!/usr/bin/env python3
"""Audit: how does the `visible` field arrive in raw XCUITest responses?

For each element on a representative screen (Springboard + Calendar
month view + Calendar Edit Event), report:
- how many elements have the `visible` key present
- of those, how many are True vs False
- how the scaffold's downstream filter interacts with this

This tells us whether `visible=False` is the default-when-missing
(which the AXElement constructor turns INTO True via .get(..., True))
or whether iOS reports it consistently.
"""
from __future__ import annotations
import asyncio, os, sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"


def audit_visible_field(label, els):
    print(f"\n=== {label} ===")
    n = len(els)
    has_key = sum(1 for e in els if "visible" in e)
    explicit_true = sum(1 for e in els if e.get("visible") is True)
    explicit_false = sum(1 for e in els if e.get("visible") is False)
    missing = n - has_key
    print(f"  Total elements: {n}")
    print(f"  'visible' key present:    {has_key}")
    print(f"  'visible': True  (explicit):  {explicit_true}")
    print(f"  'visible': False (explicit):  {explicit_false}")
    print(f"  'visible' key MISSING:     {missing}")

    # Show a sample of each category
    def sample(predicate, label, k=3):
        matches = [e for e in els if predicate(e)]
        if not matches: return
        print(f"  --- sample {label} ---")
        for e in matches[:k]:
            print(f"    role={e.get('role'):12s}  label={(e.get('label') or '')[:30]!r}  visible={e.get('visible', '<MISSING>')!r}")

    sample(lambda e: e.get("visible") is False, "visible=False")
    sample(lambda e: "visible" not in e, "key MISSING")
    sample(lambda e: e.get("visible") is True, "visible=True")


async def main():
    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()
    try:
        async with reader._lock:
            # 1. Springboard
            raw = await reader._send({"type": "observe",
                                       "bundleId": "com.apple.springboard"})
            audit_visible_field("SPRINGBOARD home", raw.get("elements") or [])

            # 2. Launch Calendar, give it time, observe month view
            await reader._send({"type": "launch", "bundleId": "com.apple.mobilecal"})
            await asyncio.sleep(2.5)
            raw2 = await reader._send({"type": "observe"})
            audit_visible_field("CALENDAR month view", raw2.get("elements") or [])

            # 3. Seed an event, navigate to it, open Edit screen
            target = (date.today() + timedelta(days=2)).isoformat()
            await reader._send({"type": "wipe_events"})
            await reader._send({
                "type": "create_event", "title": "Audit Event",
                "start_iso": f"{target}T15:00:00",
                "end_iso":   f"{target}T16:00:00",
            })

            # Restart Calendar so it picks up the event
            await reader._send({"type": "press", "button": "home"})
            await asyncio.sleep(0.5)
            await reader._send({"type": "launch", "bundleId": "com.apple.mobilecal"})
            await asyncio.sleep(2.5)

            # Find target date button
            raw3 = await reader._send({"type": "observe"})
            els3 = raw3.get("elements") or []
            target_dt = date.today() + timedelta(days=2)
            day_label = target_dt.strftime("%A, %B %-d")
            day_btn = None
            for e in els3:
                if (e.get("role") == "btn"
                        and day_label in (e.get("label") or "")):
                    day_btn = e; break
            if not day_btn:
                print(f"\n  Could not find day button matching {day_label!r}")
                return
            frm = day_btn.get("frame") or {}
            await reader._send({
                "type": "tap",
                "x": float(frm.get("x", 0) + frm.get("width", 0)/2),
                "y": float(frm.get("y", 0) + frm.get("height", 0)/2),
            })
            await asyncio.sleep(1.0)

            # Tap the event
            raw4 = await reader._send({"type": "observe"})
            event_btn = None
            for e in raw4.get("elements") or []:
                if "Audit Event" in (e.get("label") or ""):
                    event_btn = e; break
            if event_btn:
                frm = event_btn.get("frame") or {}
                await reader._send({
                    "type": "tap",
                    "x": float(frm.get("x", 0) + frm.get("width", 0)/2),
                    "y": float(frm.get("y", 0) + frm.get("height", 0)/2),
                })
                await asyncio.sleep(1.0)

            # Tap Edit
            raw5 = await reader._send({"type": "observe"})
            edit_btn = None
            for e in raw5.get("elements") or []:
                if (e.get("label") or "") == "Edit" and e.get("role") == "btn":
                    edit_btn = e; break
            if edit_btn:
                frm = edit_btn.get("frame") or {}
                await reader._send({
                    "type": "tap",
                    "x": float(frm.get("x", 0) + frm.get("width", 0)/2),
                    "y": float(frm.get("y", 0) + frm.get("height", 0)/2),
                })
                await asyncio.sleep(1.0)

            raw6 = await reader._send({"type": "observe"})
            audit_visible_field("CALENDAR Edit Event screen",
                                raw6.get("elements") or [])
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
