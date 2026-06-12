"""Probe: does the Swift `value(forKey:"zoomScale")` proxy on
WKWebView's scrollView actually return a usable value on iOS 26.3?

Step 5 (2026-06-07) replaced the snapshot-based placeholder with a
live-element query. This probe verifies the wiring end-to-end:

  A0  open MockSite RSVP form, no focus     → expect zoom_scale ~1.0
  A1  TAP an input → kb up + auto-zoom      → expect zoom_scale > 1.0
  A2  TAP accessory Done → kb gone          → expect zoom_scale STILL > 1.0
  A3  DOUBLE_TAP on heading                 → expect zoom_scale ~1.0

If the values track the visible state, KVC works and we can wire it
into Python detection + reinstate AUTO-ZOOMED tag with a minimal latch.
If `zoom_scale` is None / 1.0 throughout despite visible zoom, the
proxy doesn't surface scrollView KVC — fall back to the
container-disappearance heuristic.

REQUIRES SIBBHelper rebuild before running:
  rm -rf ~/SIBBHelper && ./sibb_xcuitest_setup.sh <UDID>
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))


def _shot(udid, slug):
    out = f"/tmp/sibb_zoom_kvc_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", out],
        check=True, capture_output=True)
    return out


def _find_input(snap, keys):
    for el in snap.elements:
        lbl = (el.label or "").lower()
        if el.role == "input" and any(k in lbl for k in keys):
            return el
    return None


def _find_by_label(snap, sub):
    sub = sub.lower()
    for el in snap.elements:
        if el.label and sub in el.label.lower():
            return el
    return None


async def _capture(reader, udid, slug, note):
    snap = await reader.observe()
    zoom = getattr(snap, "zoom_scale", None)
    kb = getattr(snap, "keyboard_visible", False)
    sw = getattr(snap, "screen_width", 402)
    sh = getattr(snap, "screen_height", 874)
    png = _shot(udid, slug)
    print(f"\n── {slug} ── {note}")
    print(f"  zoom_scale = {zoom!r}  kb={kb}  screen={sw}x{sh}")
    print(f"  png: {png}")
    return snap, zoom


async def main(udid):
    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    site = MockSite(
        site_id="zoom-kvc",
        static_pages={"/event": PAGE_REGISTRY["rsvp_event"]},
    )
    site.page_seed = 42
    site.start()
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

    print("\n" + "═" * 70)
    print("  PHASE A: BASELINE (page loaded, no focus)")
    print("═" * 70)
    snap_a0, zoom_a0 = await _capture(reader, udid, "A0_baseline",
                                        "no focus, no kb")

    print("\n" + "═" * 70)
    print("  PHASE A1: FOCUS INPUT (kb up + auto-zoom triggers)")
    print("═" * 70)
    inp = (_find_input(snap_a0, ("email", "contact"))
           or _find_input(snap_a0, ("name", "guest")))
    if inp is None:
        print("[probe] no input found; abort")
        site.stop()
        return 2
    print(f"  focusing input: {inp.label!r} "
           f"@({inp.frame.center_x:.0f},{inp.frame.center_y:.0f})")
    await reader.tap(x=inp.frame.center_x, y=inp.frame.center_y)
    await asyncio.sleep(1.2)
    snap_a1, zoom_a1 = await _capture(reader, udid, "A1_focused_zoomed",
                                        "kb up; page should be auto-zoomed")

    print("\n" + "═" * 70)
    print("  PHASE A2: DISMISS KB via accessory Done")
    print("═" * 70)
    done = _find_by_label(snap_a1, "done")
    if done and done.frame:
        await reader.tap(x=done.frame.center_x, y=done.frame.center_y)
        await asyncio.sleep(1.2)
    snap_a2, zoom_a2 = await _capture(reader, udid, "A2_kb_gone",
                                        "kb dismissed; page STILL zoomed (per probe)")

    print("\n" + "═" * 70)
    print("  PHASE A3: DOUBLE_TAP at (200, 100) to reset zoom")
    print("═" * 70)
    try:
        await reader.double_tap(x=200, y=100)
        print("  ✓ Swift double_tap returned ok")
    except RuntimeError as e:
        print(f"  ✗ {e}")
    await asyncio.sleep(1.0)
    snap_a3, zoom_a3 = await _capture(reader, udid, "A3_after_double_tap",
                                        "should be back to ~1.0x")

    print("\n" + "═" * 70)
    print("  RESULT")
    print("═" * 70)
    def fmt(z):
        if z is None: return "None (KVC not populated)"
        return f"{z:.3f}"
    print(f"  A0 baseline      : zoom_scale = {fmt(zoom_a0)}")
    print(f"  A1 focused       : zoom_scale = {fmt(zoom_a1)}")
    print(f"  A2 kb gone       : zoom_scale = {fmt(zoom_a2)}")
    print(f"  A3 post-double-tap: zoom_scale = {fmt(zoom_a3)}")
    print()
    if all(z is None for z in (zoom_a0, zoom_a1, zoom_a2, zoom_a3)):
        print("  VERDICT: KVC proxy did NOT surface zoomScale. Need")
        print("           to fall back to container-disappearance heuristic.")
    elif zoom_a1 and zoom_a1 > 1.05:
        print("  VERDICT: KVC WORKS. Wire into Python detection + restore latch.")
        if zoom_a2 and zoom_a2 > 1.05:
            print("           Confirmed: zoom persists past kb dismissal.")
        if zoom_a3 and zoom_a3 <= 1.05:
            print("           Confirmed: DOUBLE_TAP releases zoom in KVC reading.")
    else:
        print("  VERDICT: KVC populated but didn't show zoom in A1. Either")
        print("           Safari didn't auto-zoom this run, or KVC reads a")
        print("           different scale than expected. Inspect screenshots.")

    site.stop()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_zoom_scale_kvc.py <UDID>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
