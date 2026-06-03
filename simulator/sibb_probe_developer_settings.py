#!/usr/bin/env python3
"""Probe: Settings → Developer panel inventory.

Modeled on sibb_probe_calendar_inspect.py.

Goals:
  1. Launch Settings (com.apple.Preferences)
  2. Find the "Developer" row at the top level (scroll if needed)
  3. Dump the full Developer panel AX tree
  4. Drill into each sub-page that looks interesting (especially
     "CoreSpotlight Testing") and dump those trees
  5. Surface specific items: Reindex, Dark Mode, Memory*, Slow
     Animations, Show Touches, Reset Spotlight, Calendar*,
     Reminders*, Photos*.

Run:
    /Library/Developer/CommandLineTools/usr/bin/python3 \
        sibb/simulator/sibb_probe_developer_settings.py
"""
from __future__ import annotations
import asyncio, os, sys, re, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"

# Keywords for the "interesting" highlight pass + the auto-drill list.
INTEREST = [
    "spotlight", "reindex", "dark mode", "memory", "animation",
    "show touch", "reset spotlight", "calendar", "reminder",
    "photos", "network link", "background", "limit",
    "siri", "shortcuts", "appearance", "energy", "battery",
    "thermal", "logging", "diagnostic",
]

DRILL_HINTS = [
    "corespotlight testing",
    "reindex",
    "memory",
    "network link conditioner",
    "logging",
    "diagnostics",
    "spotlight",
    "show touches",
    "dark mode",
    "appearance",
]


def dump(els, label):
    print(f"\n=== {label} (n={len(els)}) ===")
    roles = {}
    for e in els:
        r = e.role or "?"
        roles[r] = roles.get(r, 0) + 1
    print("Role histogram:", ", ".join(f"{r}={n}" for r, n in
                                       sorted(roles.items(), key=lambda kv: -kv[1])))
    print("All labeled elements:")
    for i, e in enumerate(els):
        lab = (e.label or "").strip()
        if not lab:
            continue
        frm = e.frame
        cx = round(frm.center_x) if frm else 0
        cy = round(frm.center_y) if frm else 0
        w  = round(frm.width)    if frm else 0
        h  = round(frm.height)   if frm else 0
        val = f"  val={e.value!r}" if e.value else ""
        marker = " <<<" if any(k in lab.lower() for k in INTEREST) else ""
        print(f"  [{i:3d}] {e.role:14s} {lab!r:60s} @({cx},{cy}) {w}x{h}{val}{marker}")


def find_cell(els, *keywords):
    """Return first element whose label contains any keyword (case-insensitive)."""
    for kw in keywords:
        kwl = kw.lower()
        for e in els:
            if e.label and kwl in e.label.lower():
                return e
    return None


async def screenshot(name: str):
    subprocess.run(
        ["xcrun", "simctl", "io", UDID, "screenshot",
         f"/tmp/simcheck/dev_{name}.png"],
        capture_output=True,
    )
    print(f"  /tmp/simcheck/dev_{name}.png")


async def main():
    os.makedirs("/tmp/simcheck", exist_ok=True)
    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()
    try:
        # ─── 1. Launch Settings ─────────────────────────────────────────
        print("=== Launching Settings ===")
        await reader.launch("com.apple.Preferences")
        await asyncio.sleep(2.0)

        tree = await reader.observe()
        dump(tree.elements, "Settings ROOT (first observe)")
        await screenshot("settings_root")

        # ─── 2. Scroll down looking for "Developer" ─────────────────────
        dev = find_cell(tree.elements, "Developer")
        for attempt in range(8):
            if dev:
                break
            print(f"\n[scroll {attempt}] 'Developer' not in current view — swiping up")
            await reader.swipe("up")
            await asyncio.sleep(0.6)
            tree = await reader.observe()
            dev = find_cell(tree.elements, "Developer")

        if not dev:
            print("\n!!! Could not find 'Developer' cell at Settings top level.")
            print("    Dumping current view for forensics:")
            dump(tree.elements, "Settings after scrolling")
            return

        print(f"\n=== Found 'Developer' cell: {dev!r} ===")
        await reader.tap(ref=dev.ref)
        await asyncio.sleep(1.5)

        # ─── 3. Full Developer-panel dump ───────────────────────────────
        tree = await reader.observe()
        dump(tree.elements, "DEVELOPER PANEL — top of page")
        await screenshot("developer_top")

        # Collect all labels seen across scroll passes so we can drill
        # into sub-pages even if they're below the fold initially.
        seen_labels: dict[str, "AXElement"] = {}
        def absorb(els):
            for e in els:
                if e.label and e.role in ("cell", "btn", "switch", "link"):
                    key = e.label.strip()
                    if key and key not in seen_labels:
                        seen_labels[key] = e

        absorb(tree.elements)

        # Scroll all the way down dumping each frame.
        for attempt in range(12):
            await reader.swipe("up")
            await asyncio.sleep(0.5)
            tree2 = await reader.observe()
            new_count = sum(1 for e in tree2.elements
                            if e.label and e.label.strip() not in seen_labels)
            absorb(tree2.elements)
            print(f"[dev-scroll {attempt}] new labels this pass: {new_count}")
            if new_count == 0:
                break
            dump(tree2.elements, f"DEVELOPER PANEL — scroll pass {attempt}")

        await screenshot("developer_bottom")

        # ─── 4. Summary of all Developer cells we saw ───────────────────
        print(f"\n=== DEVELOPER PANEL — union of all labeled cells "
              f"(n={len(seen_labels)}) ===")
        for lab in sorted(seen_labels.keys()):
            e = seen_labels[lab]
            mark = " <<<" if any(k in lab.lower() for k in INTEREST) else ""
            print(f"  [{e.role:8s}] {lab}{mark}")

        # ─── 5. Auto-drill into anything that looks like a sub-page ─────
        # Walk DRILL_HINTS, tap into each, dump, and back out.
        # We rescroll each time to ensure target is on-screen.
        for hint in DRILL_HINTS:
            print(f"\n\n##### Drilling into hint '{hint}' #####")
            # Back to top of Developer panel first.
            for _ in range(20):
                await reader.swipe("down")
                await asyncio.sleep(0.25)

            tree3 = await reader.observe()
            target = find_cell(tree3.elements, hint)

            # Scroll up looking for the target.
            for attempt in range(12):
                if target and target.frame and target.frame.y < 800:
                    break
                await reader.swipe("up")
                await asyncio.sleep(0.4)
                tree3 = await reader.observe()
                target = find_cell(tree3.elements, hint)

            if not target:
                print(f"  (not found on screen — skipping)")
                continue

            print(f"  Tapping {target!r}")
            await reader.tap(ref=target.ref)
            await asyncio.sleep(1.2)

            sub = await reader.observe()
            dump(sub.elements, f"SUBPAGE — {hint!r}")
            await screenshot(f"sub_{re.sub(r'[^a-z0-9]+', '_', hint.lower())}")

            # Drill one more level if a CoreSpotlight Testing row had
            # the magic Reindex buttons inside another sub-cell.
            if "spotlight" in hint or "reindex" in hint:
                for kw in ("reindex all items",
                           "reindex search items",
                           "reindex"):
                    inner = find_cell(sub.elements, kw)
                    if inner and inner.role == "btn":
                        print(f"  >> Found likely Reindex button: {inner!r}")
                        # Don't actually tap it — we don't want to
                        # invalidate the simulator's index without an
                        # explicit user decision. Just record the AX
                        # surface.
                        break

            # Back to Developer root.
            back = find_cell(sub.elements, "Developer", "Settings", "Back")
            if back:
                try:
                    await reader.tap(ref=back.ref)
                except Exception:
                    await reader.press(button="back")
            else:
                await reader.press(button="back")
            await asyncio.sleep(0.8)

        print("\n=== DONE — full report above. Screenshots under /tmp/simcheck/dev_*.png ===")
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
