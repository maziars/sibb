"""Verify the new DOUBLE_TAP verb resets Safari's WebView zoom via
the native Swift `XCUICoordinate.doubleTap()` path.

Sequence:
  1. Open RSVP form (kb down, no zoom)            → A0
  2. TAP the email input → kb up + auto-zoom in   → A1
  3. TAP accessory `Done` → kb dismissed, page still zoomed → A2
  4. Issue `xc.double_tap(x=200, y=100)`          → expect zoom reset
  5. Sample AX + screenshots                       → B
  6. Compare A0 vs B — should match (both fit-to-page)

If B's screenshot matches A0 (page fit-to-page), the new DOUBLE_TAP
verb works through the native gesture pipeline and the synthetic
two-rapid-taps issue is resolved end-to-end.

If B still shows the zoomed page, the native `coord.doubleTap()` API
also doesn't fire the recognizer — would mean the user's manual
trackpad gesture used a different code path (e.g. AppKit event
forwarding) than XCUITest can reach.
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


def _shot(udid: str, slug: str) -> str:
    out = f"/tmp/sibb_dbltap_verify_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", out],
        check=True, capture_output=True)
    return out


def _signals(snap):
    sw = getattr(snap, "screen_width", 402)
    kb_frame = getattr(snap, "keyboard_frame", None)
    kb_y = kb_frame.get("y") if kb_frame else None
    max_w = 0.0
    for el in snap.elements:
        if el.frame and el.frame.width > max_w:
            max_w = el.frame.width
    return {
        "kb": getattr(snap, "keyboard_visible", False),
        "kb_y": kb_y,
        "overflow": (max_w / sw) if sw else None,
        "max_w": max_w,
        "n_els": len(snap.elements),
    }


async def _capture(reader, udid, slug, note):
    snap = await reader.observe()
    sig = _signals(snap)
    png = _shot(udid, slug)
    of = f"{sig['overflow']:.2f}" if sig['overflow'] else "?"
    print(f"\n── {slug} ── {note}")
    print(f"  kb={sig['kb']}  kb_y={sig['kb_y']}  "
          f"overflow={of}  max_w={sig['max_w']:.0f}  els={sig['n_els']}")
    print(f"  png: {png}")
    return snap, sig


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


async def main(udid: str) -> int:
    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    site = MockSite(
        site_id="dbltap-verify",
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
    print("  STEP 1: BASELINE (fit-to-page, no kb)")
    print("═" * 70)
    snap_a0, sig_a0 = await _capture(reader, udid, "A0_baseline",
                                       "page loaded; no input focused")

    print("\n" + "═" * 70)
    print("  STEP 2: FOCUS INPUT (kb up + auto-zoom)")
    print("═" * 70)
    inp = (_find_input(snap_a0, ("email", "contact"))
           or _find_input(snap_a0, ("name", "guest")))
    if inp is None:
        print("[probe] no input found; abort")
        site.stop()
        return 2
    print(f"  focusing: {inp.label!r}")
    await reader.tap(x=inp.frame.center_x, y=inp.frame.center_y)
    await asyncio.sleep(1.0)
    snap_a1, sig_a1 = await _capture(reader, udid, "A1_focused_zoomed",
                                       "after TAP input")

    print("\n" + "═" * 70)
    print("  STEP 3: DISMISS KB via accessory Done")
    print("═" * 70)
    done = _find_by_label(snap_a1, "done")
    if done and done.frame:
        await reader.tap(x=done.frame.center_x, y=done.frame.center_y)
        await asyncio.sleep(1.0)
    snap_a2, sig_a2 = await _capture(reader, udid, "A2_kb_gone_still_zoomed",
                                      "kb gone; page expected still zoomed")

    print("\n" + "═" * 70)
    print("  STEP 4: DOUBLE_TAP (200, 100) via native Swift")
    print("═" * 70)
    print("  Dispatching xc.double_tap(x=200, y=100)...")
    try:
        await reader.double_tap(x=200, y=100)
        print("  ✓ Swift double_tap returned ok")
    except RuntimeError as e:
        print(f"  ✗ Swift double_tap raised: {e}")
        print("  (Likely: SIBBHelper not rebuilt. Run "
               "./sibb_xcuitest_setup.sh <UDID> to rebuild.)")
        site.stop()
        return 3
    # Wait for WebKit's zoom-fit animation (~250ms) plus settle.
    await asyncio.sleep(1.0)

    snap_b, sig_b = await _capture(reader, udid, "B_after_double_tap",
                                     "after DOUBLE_TAP (200, 100)")

    # And one more sample 500ms later to confirm steady state.
    await asyncio.sleep(0.5)
    snap_b2, sig_b2 = await _capture(reader, udid, "B2_after_settle",
                                       "500ms settle")

    print("\n" + "═" * 70)
    print("  SUMMARY")
    print("═" * 70)
    print(f"  A0 baseline:           "
          f"kb={sig_a0['kb']}  overflow={sig_a0['overflow']:.2f}  "
          f"max_w={sig_a0['max_w']:.0f}")
    print(f"  A1 focused (zoomed):   "
          f"kb={sig_a1['kb']}  overflow={sig_a1['overflow']:.2f}  "
          f"max_w={sig_a1['max_w']:.0f}")
    print(f"  A2 kb gone, zoomed:    "
          f"kb={sig_a2['kb']}  overflow={sig_a2['overflow']:.2f}  "
          f"max_w={sig_a2['max_w']:.0f}")
    print(f"  B  after DOUBLE_TAP:   "
          f"kb={sig_b['kb']}  overflow={sig_b['overflow']:.2f}  "
          f"max_w={sig_b['max_w']:.0f}")
    print(f"  B2 settle:             "
          f"kb={sig_b2['kb']}  overflow={sig_b2['overflow']:.2f}  "
          f"max_w={sig_b2['max_w']:.0f}")
    print()
    print("  Visual comparison:")
    print("    A0_baseline.png       ← expected target (fit-to-page)")
    print("    A1_focused_zoomed.png ← zoomed + kb up")
    print("    A2_kb_gone_still_zoomed.png ← zoomed, no kb")
    print("    B_after_double_tap.png  ← THE TEST")
    print("    B2_after_settle.png   ← B + 500ms")
    print()
    print("  If B looks like A0 (full form visible, fit-to-page),")
    print("  DOUBLE_TAP via native Swift works. Verdict: ALT-1b is")
    print("  the correct design + DOUBLE_TAP is the agent's recovery.")
    print()
    print("  If B looks like A2 (still zoomed), the native gesture")
    print("  also doesn't fire WebKit's recognizer — would mean the")
    print("  user's trackpad double-tap uses a different code path.")

    site.stop()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_double_tap_verify.py <UDID>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
