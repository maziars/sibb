#!/usr/bin/env python3
"""Empirical probe: which `swipe_at` origin/end pairs actually invoke
iOS system gestures (Spotlight, page-flip, etc.) on Springboard?

Uses the existing `swipe_at` Swift command (no rebuild needed) to send
explicit-coordinate swipes from Python. After each gesture, reads the
AX tree and reports a one-line characterization of what UI appeared.

Run while the SIBB-Demo simulator is booted; assumes XCUITestHelper is
already built. Returns to a clean Springboard between gestures via
PRESS home + a wait.

    /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb/simulator/sibb_probe_swipe_origins.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader  # noqa: E402

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"

# iPhone 17 Pro logical size (points).
W, H = 393, 852

# ── Gestures to test ────────────────────────────────────────────────────────
# Each entry: (name, x1, y1, x2, y2, hypothesis)
GESTURES = [
    ("mid_down_to_lower_mid",    W//2, int(H*0.50), W//2, int(H*0.85),
     "swipe from mid → lower-mid: hypothesized to invoke Spotlight"),
    ("upper_third_down",         W//2, int(H*0.33), W//2, int(H*0.66),
     "swipe from upper-third → lower-third"),
    ("top_edge_down",            W//2, int(H*0.02), W//2, int(H*0.40),
     "swipe from VERY TOP → mid: should be Notification Center"),
    ("right_to_left_mid",        int(W*0.85), H//2, int(W*0.15), H//2,
     "horizontal swipe across mid-y (might hit widgets): page-advance?"),
    ("right_to_left_lower",      int(W*0.85), int(H*0.70), int(W*0.15), int(H*0.70),
     "horizontal swipe at dy=0.70 (below widgets, above dock)"),
    ("right_to_left_upper",      int(W*0.85), int(H*0.20), int(W*0.15), int(H*0.20),
     "horizontal swipe at dy=0.20 (above main grid)"),
]


def characterize_tree(elements: List[Dict], bundle: str, kb: bool) -> str:
    """One-line summary of what UI is showing."""
    labels = [e.get("label") or "" for e in elements]
    # Quick heuristics
    has_cancel_at_top = any("Cancel" in l for l in labels[:5])
    has_search_at_low_y = any(
        l == "Search" and (e.get("frame") or {}).get("y", 0) > 600
        for l, e in zip(labels, elements)
    )
    has_search_at_high_y = any(
        l == "Search" and 0 < (e.get("frame") or {}).get("y", 999) < 200
        for l, e in zip(labels, elements)
    )
    # Spotlight overlay signature: Cancel button at top + Search field + apps list
    # App Library search signature: Close button at top right + Search field
    has_close = any(l == "Close" for l in labels)
    n_cells = sum(1 for e in elements if e.get("role") == "AXCell")
    n_imgs = sum(1 for e in elements if e.get("role") == "AXImage")

    flags = []
    if kb: flags.append("kb")
    if has_cancel_at_top: flags.append("Cancel-top")
    if has_close: flags.append("Close-btn")
    if has_search_at_low_y: flags.append("Search@bottom")
    if has_search_at_high_y: flags.append("Search@top")

    return (f"bundle={bundle}  els={len(elements)}  cells={n_cells}  "
            f"imgs={n_imgs}  [{','.join(flags) or 'no-flags'}]")


async def settle_at_home(reader: XCUITestReader, label: str):
    """Return to a clean Springboard before the next test."""
    # PRESS home up to thrice to escape any modal/Spotlight/App Library
    for _ in range(3):
        await reader._send({"type": "press", "button": "home"})
        await asyncio.sleep(0.4)
    await asyncio.sleep(0.6)
    raw = await reader._send({"type": "observe",
                               "bundleId": "com.apple.springboard"})
    if not raw.get("ok"):
        print(f"  [{label} ← reset] OBSERVE FAILED: {raw.get('error')}")
        return
    bundle = raw.get("bundle_id") or "?"
    elements = raw.get("elements") or []
    kb = bool(raw.get("keyboard_visible"))
    print(f"  [{label} ← reset] {characterize_tree(elements, bundle, kb)}")


async def run_gesture(reader: XCUITestReader, name: str,
                      x1: int, y1: int, x2: int, y2: int) -> Dict:
    print(f"\n── {name} ───────────────────────────────────────────────────")
    print(f"    swipe_at ({x1},{y1}) → ({x2},{y2})")

    # BEFORE
    raw_b = await reader._send({"type": "observe",
                                 "bundleId": "com.apple.springboard"})
    bundle_b = raw_b.get("bundle_id") or "?"
    els_b = raw_b.get("elements") or []
    kb_b = bool(raw_b.get("keyboard_visible"))
    print(f"  before: {characterize_tree(els_b, bundle_b, kb_b)}")

    # GESTURE
    t0 = time.time()
    resp = await reader._send({
        "type": "swipe_at",
        "x1": float(x1), "y1": float(y1),
        "x2": float(x2), "y2": float(y2),
        "duration_s": 0.05,
    })
    dt = round((time.time() - t0) * 1000)
    if not resp.get("ok"):
        print(f"  ERROR: {resp.get('error')}")
        return {"name": name, "error": resp.get("error")}
    await asyncio.sleep(0.5)  # let UI settle

    # AFTER — observe whatever app is now frontmost (Spotlight is a
    # separate process from Springboard, so don't pin the bundle).
    raw_a = await reader._send({"type": "observe"})
    bundle_a = raw_a.get("bundle_id") or "?"
    els_a = raw_a.get("elements") or []
    kb_a = bool(raw_a.get("keyboard_visible"))
    print(f"  after : {characterize_tree(els_a, bundle_a, kb_a)}  ({dt}ms)")
    # Show first 8 LABELED elements at non-zero coords for sanity
    labeled = []
    for e in els_a:
        lab = (e.get("label") or "").strip()
        frm = e.get("frame") or {}
        x, y = frm.get("x", 0), frm.get("y", 0)
        if lab and (x or y):
            labeled.append(f'{lab[:30]!r}@({x:.0f},{y:.0f})')
            if len(labeled) >= 8:
                break
    print(f"  visible: {', '.join(labeled) or '(none labeled)'}")

    # Verdict
    changed_bundle = bundle_a != bundle_b
    changed_kb = kb_a != kb_b
    changed_count = len(els_a) - len(els_b)
    verdict = []
    if changed_bundle: verdict.append(f"bundle:{bundle_b}→{bundle_a}")
    if changed_kb: verdict.append(f"kb:{kb_b}→{kb_a}")
    if abs(changed_count) > 3: verdict.append(f"els:{len(els_b)}→{len(els_a)}")
    if not verdict: verdict.append("no observable change")
    print(f"  verdict: {' | '.join(verdict)}")

    return {
        "name": name,
        "before": {"bundle": bundle_b, "els": len(els_b), "kb": kb_b},
        "after":  {"bundle": bundle_a, "els": len(els_a), "kb": kb_a},
    }


async def main():
    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()
    try:
        print(f"Probing swipe origins on iPhone 17 Pro ({W}×{H} points)")
        print(f"Each gesture is preceded by PRESS home ×3 to reset state.")

        async with reader._lock:
            for name, x1, y1, x2, y2, hypothesis in GESTURES:
                await settle_at_home(reader, name)
                print(f"    hypothesis: {hypothesis}")
                await run_gesture(reader, name, x1, y1, x2, y2)
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
