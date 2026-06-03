#!/usr/bin/env python3
"""Verify the new normalized-coord `swipe` handler invokes the right
iOS system gestures end-to-end via the Python `xc.swipe(direction)`
call (NOT swipe_at — that already worked; we want to confirm the
normal SWIPE path now does the right thing too).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader  # noqa: E402

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"


def characterize(elements, bundle, kb):
    n = len(elements)
    labeled = []
    for e in elements:
        lab = (e.get("label") or "").strip()
        frm = e.get("frame") or {}
        x, y = frm.get("x", 0), frm.get("y", 0)
        if lab and (x or y):
            labeled.append(f"{lab[:25]!r}@({x:.0f},{y:.0f})")
            if len(labeled) >= 6:
                break
    return f"bundle={bundle} els={n} kb={kb} | " + ", ".join(labeled)


async def reset(reader, label):
    for _ in range(3):
        await reader._send({"type": "press", "button": "home"})
        await asyncio.sleep(0.4)
    await asyncio.sleep(0.5)
    raw = await reader._send({"type": "observe",
                               "bundleId": "com.apple.springboard"})
    elements = raw.get("elements") or []
    bundle = raw.get("bundle_id") or "?"
    print(f"  [{label} reset] {characterize(elements, bundle, raw.get('keyboard_visible'))}")


async def test(reader, direction, hypothesis):
    print(f"\n── SWIPE {direction} ───────────────────────────────────────────")
    print(f"    hypothesis: {hypothesis}")

    raw_b = await reader._send({"type": "observe",
                                 "bundleId": "com.apple.springboard"})
    print(f"  before: {characterize(raw_b.get('elements') or [], raw_b.get('bundle_id'), raw_b.get('keyboard_visible'))}")

    t0 = time.time()
    resp = await reader._send({"type": "swipe", "direction": direction})
    dt = round((time.time() - t0) * 1000)
    if not resp.get("ok"):
        print(f"  ERROR: {resp.get('error')}")
        return
    await asyncio.sleep(0.5)

    raw_a = await reader._send({"type": "observe"})
    print(f"  after : {characterize(raw_a.get('elements') or [], raw_a.get('bundle_id'), raw_a.get('keyboard_visible'))} ({dt}ms)")


async def main():
    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()
    try:
        async with reader._lock:
            await reset(reader, "down")
            await test(reader, "down", "should invoke Spotlight (els drops dramatically)")
            await reset(reader, "up")
            await test(reader, "up", "should be a no-op or dismiss Spotlight if open")
            await reset(reader, "left")
            await test(reader, "left", "should advance to next home page (icons change)")
            await reset(reader, "right")
            await test(reader, "right", "should go back to previous home page")
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
