"""Probe: verify PINCH out actually resets Safari auto-zoom on the sim
AND visualize whether the AX-reported coords match the on-screen
positions.

What it does
============
1. Spawns a MockSite serving the RSVP form at /event.
2. Opens Safari to it.
3. At three points (baseline / post-focus / post-pinch), takes both
   an AX snapshot AND a screen capture, then renders an annotated
   image showing where the AX says the submit button and focused
   input are. Crosshairs at the AX-reported centers; rectangles
   around the AX-reported frames; labels with the raw numbers.
4. Saves the annotated images to /tmp/sibb_probe_*.png so we can
   compare visually whether AX coords actually match the painted
   positions.

The annotated images directly answer the question: "are the AX
coords correct screen coords, or is there a coord-system mismatch?"

Usage
=====
    python3 sibb/simulator/sibb_probe_pinch_recovery.py <UDID>

After running, open the saved PNGs:
    open /tmp/sibb_probe_*.png
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))


def _describe(snap, tag):
    print(f"\n── {tag} ──")
    elements = getattr(snap, "elements", [])
    print(f"  elements: {len(elements)}")
    kb = getattr(snap, "keyboard_visible", None)
    print(f"  keyboard_visible: {kb}")
    kbf = getattr(snap, "keyboard_frame", None)
    if kbf:
        print(f"  keyboard_frame: y={kbf.get('y')} height={kbf.get('height')}")
    print(f"  screen: {getattr(snap, 'screen_width', '?')}x"
          f"{getattr(snap, 'screen_height', '?')}")
    # Print form container + submit button if visible.
    for el in elements:
        lbl = (el.label or "").lower()
        if ("rsvp form" in lbl or "send" in lbl or "submit" in lbl
                or "confirm" in lbl):
            fr = el.frame
            if fr:
                print(f"    [{el.role}] {el.label!r} "
                      f"x={fr.x:.0f} y={fr.y:.0f} "
                      f"w={fr.width:.0f} h={fr.height:.0f}")


def _annotate(udid, snap, tag, slug):
    """Take a sim screenshot and overlay the AX-reported frames + centers
    for the focused input, submit button, and form container. Saves to
    /tmp/sibb_probe_<slug>.png. The visual answer to "are AX coords
    correct screen coords?"."""
    import subprocess
    from PIL import Image, ImageDraw, ImageFont
    png_path = f"/tmp/sibb_probe_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", png_path],
        check=True, capture_output=True)
    img = Image.open(png_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # iOS sim screenshots are at @3x or @2x device-pixel density; AX
    # frames are in iOS POINTS. iPhone 16 = 402x874 points. Read the
    # image's actual pixel size and compute the scale factor.
    img_w, img_h = img.size
    screen_w = getattr(snap, "screen_width", 402) or 402
    screen_h = getattr(snap, "screen_height", 874) or 874
    sx = img_w / screen_w
    sy = img_h / screen_h
    # Use the same scale for both axes (assume square pixels).
    sf = sx
    print(f"  image size: {img_w}x{img_h}, "
          f"AX screen: {screen_w}x{screen_h}, scale: {sf:.2f}")

    def _draw_rect(x, y, w, h, color, label):
        x0, y0 = x * sf, y * sf
        x1, y1 = (x + w) * sf, (y + h) * sf
        draw.rectangle([x0, y0, x1, y1], outline=color, width=5)
        # Center crosshair
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        r = 18
        draw.line([cx - r, cy, cx + r, cy], fill=color, width=4)
        draw.line([cx, cy - r, cx, cy + r], fill=color, width=4)
        # Big label near the top-left of the frame
        draw.text((x0 + 6, max(0, y0 - 26)), label, fill=color)

    targets = []
    for el in snap.elements:
        lbl = (el.label or "").lower()
        if "rsvp form" in lbl:
            targets.append((el, (255, 200, 0, 255), "FORM"))
        elif "send" in lbl or "submit" in lbl or "confirm rsvp" in lbl:
            targets.append((el, (255, 0, 0, 255), "SUBMIT"))
        elif getattr(el, "focused", False) and el.role == "input":
            targets.append((el, (0, 200, 255, 255), "FOCUSED"))
    for el, color, name in targets:
        fr = el.frame
        if fr is None:
            continue
        info = (f"{name} AX=({fr.x:.0f},{fr.y:.0f}) "
                f"{fr.width:.0f}x{fr.height:.0f}")
        print(f"  overlay: {info}")
        _draw_rect(fr.x, fr.y, fr.width, fr.height, color, info)

    # Compose overlay on the screenshot
    out = Image.alpha_composite(img, overlay)
    out.convert("RGB").save(png_path, "PNG")
    print(f"  annotated: {png_path}")
    return png_path


def _annotate_filtered(udid, tree, tag, slug):
    """Overlay EVERY visible element from the scaffold-filtered tree
    (what the agent actually sees) onto a fresh screenshot. Any
    element whose box is OUTSIDE the visible viewport is a
    filter-bug — the screenshot will show the box at a position with
    no actual content there. Any element that IS visible but not in
    the filtered tree is a false-negative — visible-but-hidden-from-
    agent. Manual inspection of the saved image answers both."""
    import subprocess
    from PIL import Image, ImageDraw
    png_path = f"/tmp/sibb_probe_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", png_path],
        check=True, capture_output=True)
    img = Image.open(png_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    img_w, img_h = img.size
    screen_w = getattr(tree, "screen_width", 402) or 402
    sf = img_w / screen_w
    print(f"\n── {tag} ──")
    print(f"  elements (agent sees): {len(tree.elements)}")
    for el in tree.elements:
        fr = el.frame
        if fr is None:
            continue
        # Lime for interactive / input / button; gray for text/other.
        if el.role in ("BUTTON", "Button", "INPUT", "TEXT_FIELD",
                        "TextField"):
            color = (60, 220, 60, 255)
        else:
            color = (140, 140, 140, 180)
        x0 = fr.x * sf
        y0 = fr.y * sf
        x1 = (fr.x + fr.width) * sf
        y1 = (fr.y + fr.height) * sf
        draw.rectangle([x0, y0, x1, y1], outline=color, width=3)
    out = Image.alpha_composite(img, overlay)
    out.convert("RGB").save(png_path, "PNG")
    print(f"  annotated: {png_path}")
    return png_path


async def main(udid: str) -> int:
    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    site = MockSite(
        site_id="pinch-probe",
        static_pages={"/event": PAGE_REGISTRY["rsvp_event"]},
    )
    site.page_seed = 42
    site.start()
    print(f"[probe] MockSite: {site.base_url}/event")

    import sibb_dns_resolver
    sibb_dns_resolver.start_if_needed()
    if sibb_dns_resolver.resolver_is_installed():
        url = f"http://rsvp.test:{site.port}/event"
    else:
        url = f"{site.base_url}/event"
    open_in_safari(udid, url)
    await asyncio.sleep(2.0)

    reader = XCUITestReader(udid, bundle_id="com.apple.mobilesafari")
    await reader.start()

    # The scaffold-filtered tree is what the AGENT sees. Wire up an
    # AXReader pointing at the same XCUITest client so we can call
    # _read_xcuitest() to get the post-filter tree. Compare the raw
    # snap to the filtered snap to verify the existing
    # `_is_fully_visible` filter correctly drops off-screen elements
    # and keeps the rest at their real screen coords.
    from sibb_scaffold import AXReader
    ax_reader = AXReader(udid)
    ax_reader._xcuitest = reader

    # 1. baseline
    snap0 = await reader.observe()
    filt0 = await ax_reader._read_xcuitest()
    _describe(snap0, "baseline raw (post-load, before any focus)")
    _annotate(udid, snap0, "baseline raw", "1a_baseline_raw")
    _annotate_filtered(udid, filt0, "baseline filtered (agent view)",
                        "1b_baseline_filtered")

    # 2. tap the first form input — triggers auto-zoom
    # Find an input labeled with one of our form-field labels.
    target = None
    for el in snap0.elements:
        lbl = (el.label or "").lower()
        if el.role == "input" and any(
                k in lbl for k in
                ("email", "name", "guest", "attending", "rsvp",
                 "contact", "registrant", "going")):
            target = el
            break
    if target is None:
        print("[probe] no form input found to focus — aborting")
        site.stop()
        return 2
    print(f"\n[probe] tapping input @{target.ref} '{target.label}' "
          f"at ({target.frame.center_x:.0f}, "
          f"{target.frame.center_y:.0f})")
    await reader.tap(x=target.frame.center_x, y=target.frame.center_y)
    await asyncio.sleep(1.0)  # let auto-zoom + kb animation settle

    snap1 = await reader.observe()
    filt1 = await ax_reader._read_xcuitest()
    _describe(snap1, "post-focus raw (zoom expected)")
    _annotate(udid, snap1, "post-focus raw", "2a_post_focus_raw")
    _annotate_filtered(udid, filt1, "post-focus filtered (agent view)",
                        "2b_post_focus_filtered")

    # 3. PINCH out
    print("\n[probe] issuing PINCH out (scale=0.3, velocity=5.0)")
    try:
        resp = await reader.pinch(scale=0.3, velocity=5.0)
        print(f"  → {resp}")
    except RuntimeError as e:
        print(f"[probe] PINCH failed: {e}")
        print("[probe] Likely cause: SIBBHelper not rebuilt. Run "
              "`./sibb_xcuitest_setup.sh <UDID>` and try again.")
        site.stop()
        return 3
    await asyncio.sleep(1.0)

    snap2 = await reader.observe()
    filt2 = await ax_reader._read_xcuitest()
    _describe(snap2, "post-pinch raw")
    _annotate(udid, snap2, "post-pinch raw", "3a_post_pinch_raw")
    _annotate_filtered(udid, filt2, "post-pinch filtered (agent view)",
                        "3b_post_pinch_filtered")
    site.stop()
    print("\n[probe] done — compare the snapshots above and confirm:")
    print("  * post-focus had kb_y > screen_h OR form_width > screen_w")
    print("  * post-pinch-out frames look like screen coords again")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_pinch_recovery.py <UDID>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
