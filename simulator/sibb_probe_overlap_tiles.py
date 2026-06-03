#!/usr/bin/env python3
"""One-off probe: seed two overlapping events on today's date and
prompt the user to manually inspect Calendar.app's Day view AX tree.

Goal: confirm whether iOS Calendar's Day view exposes both
overlapping events as distinct AX nodes (good — agent can read both)
or stacks/collapses them (bad — agent may see only the front tile).

Usage:
    /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb_probe_overlap_tiles.py

Then on the iOS sim:
    1. Open Calendar.app from the home screen.
    2. Tap "Today" (or scroll to today's date).
    3. Tap the Day view button (calendar icon in the bottom-left,
       or swipe up on the date header). You should see two
       overlapping tiles at 10:00 and 10:30.
    4. In another terminal, run:
           cd sibb/benchmark
           /Library/Developer/CommandLineTools/usr/bin/python3 \\
               sibb_inspect_screen.py \\
               19B95A95-614A-4ECA-B943-44FDADFD7A9F \\
               --bundle com.apple.mobilecal --once

    5. Look for two `[btn]` lines mentioning "Standup" and "Lunch".
       Both present  → AX tree exposes overlapping events cleanly.
       Only one present → iOS collapses stacked tiles; would
       penalize agents on gen_list_conflicting_events unfairly.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader  # noqa: E402

DEFAULT_UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"


async def main(udid: str) -> int:
    today = date.today()
    print(f"Seeding 2 overlapping events on {today.isoformat()}...")

    reader = XCUITestReader(udid, bundle_id="com.apple.mobilecal")
    await reader.start()
    try:
        async with reader._lock:
            await reader._send({"type": "wipe_events"})
            r1 = await reader._send({
                "type": "create_event",
                "title": "Standup",
                "start_iso": f"{today.isoformat()}T10:00:00",
                "end_iso":   f"{today.isoformat()}T11:00:00",
            })
            r2 = await reader._send({
                "type": "create_event",
                "title": "Lunch",
                "start_iso": f"{today.isoformat()}T10:30:00",
                "end_iso":   f"{today.isoformat()}T11:30:00",
            })
            if not r1.get("ok") or not r2.get("ok"):
                print(f"  ERROR: create failed: {r1.get('error') or r2.get('error')}")
                return 1
            print(f"  ✓ 'Standup' 10:00-11:00 created.")
            print(f"  ✓ 'Lunch' 10:30-11:30 created.")
    finally:
        await reader.stop()

    print()
    print("Next steps:")
    print("  1. On the iOS sim, open Calendar.app from the home screen.")
    print(f"  2. Tap on today's date ({today.strftime('%A, %B %-d')}).")
    print("  3. Tap the Day view (the < icon in top-left bar, or")
    print("     swipe down on the date header to expand the day grid).")
    print("  4. You should see 'Standup' and 'Lunch' as overlapping tiles")
    print("     around 10am-11:30am.")
    print()
    print("  5. In another terminal, run:")
    print(f"       cd \"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/benchmark\"")
    print(f"       /Library/Developer/CommandLineTools/usr/bin/python3 \\")
    print(f"           sibb_inspect_screen.py {udid} \\")
    print(f"           --bundle com.apple.mobilecal --once")
    print()
    print("Look for two [btn] lines with both event titles. Report back.")
    return 0


if __name__ == "__main__":
    udid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_UDID
    sys.exit(asyncio.run(main(udid)))
