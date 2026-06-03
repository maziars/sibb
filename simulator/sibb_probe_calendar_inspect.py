#!/usr/bin/env python3
"""Quick diagnostic: seed a Calendar event, launch Calendar, dump
EVERY element with its role distribution. No filtering. No navigation.

This tells us what state Calendar is in after launch and what the
raw AX tree actually looks like.
"""
from __future__ import annotations
import asyncio, os, sys, json
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"


async def main():
    target_date = date.today() + timedelta(days=2)
    target_iso = target_date.isoformat()
    title = "Probe Date Night"

    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()
    try:
        async with reader._lock:
            print(f"=== Seeding '{title}' on {target_iso} 14:00-15:00 ===")
            await reader._send({"type": "wipe_events"})
            resp = await reader._send({
                "type": "create_event",
                "title": title,
                "start_iso": f"{target_iso}T14:00:00",
                "end_iso":   f"{target_iso}T15:00:00",
            })
            print(f"  ok={resp.get('ok')} err={resp.get('error')}")

            print("\n=== Launching Calendar (with longer settle) ===")
            await reader._send({"type": "launch",
                                 "bundleId": "com.apple.mobilecal"})
            await asyncio.sleep(4.0)  # longer wait for post-erase first-launch

            print("\n=== First observe ===")
            raw = await reader._send({"type": "observe",
                                       "bundleId": "com.apple.mobilecal"})
            els = raw.get("elements") or []
            print(f"  bundle={raw.get('bundle_id')}  els={len(els)}  kb={raw.get('keyboard_visible')}")

            print("\n=== Role histogram ===")
            roles = {}
            for e in els:
                r = e.get("role") or "?"
                roles[r] = roles.get(r, 0) + 1
            for r, n in sorted(roles.items(), key=lambda kv: -kv[1]):
                print(f"  {r:30s}  {n}")

            print("\n=== All elements with non-empty labels ===")
            for i, e in enumerate(els):
                lab = (e.get("label") or "").strip()
                if lab:
                    frm = e.get("frame") or {}
                    print(f"  [{i:3d}] {e.get('role'):25s}  {lab!r:50s}  "
                          f"@({frm.get('x',0):.0f},{frm.get('y',0):.0f}) "
                          f"{frm.get('width',0):.0f}x{frm.get('height',0):.0f}"
                          + (f"  val={e.get('value')!r}" if e.get('value') else ""))

            print("\n=== Saving screenshot ===")
            # Trigger a screenshot from the host
            import subprocess
            subprocess.run(["xcrun", "simctl", "io", UDID, "screenshot",
                            "/tmp/simcheck/calendar_launched.png"],
                           capture_output=True)
            print("  /tmp/simcheck/calendar_launched.png")
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
