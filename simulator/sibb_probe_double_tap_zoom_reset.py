"""Probe: does WebKit's double-tap recognizer reset Safari's auto-zoom?

Sequence:
  1. Open RSVP form (kb down, no zoom)            → A0
  2. TAP the email input → kb up + auto-zoom in   → A1
  3. TAP accessory `Done` → kb dismissed, page still zoomed → A2
  4. Identify a safe non-input coord on the page (a body
     text paragraph — non-interactive, no input focus risk)
  5. Issue TWO rapid `xc.tap()` calls ~120ms apart at that coord
     → DBLTAP
  6. Capture → C

Compare A0 (baseline, fit-to-page) vs C (post-double-tap).
If C visually matches A0 → WebKit's double-tap-to-zoom-out fired.
If C still shows the zoomed view → synthetic-gesture timing didn't
trigger the recognizer; we'd need a native Swift `coord.doubleTap()`.

Output:
  /tmp/sibb_dbltap_<step>.png
  /tmp/sibb_dbltap_log.jsonl
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))

LOG = "/tmp/sibb_dbltap_log.jsonl"


def _shot(udid: str, slug: str) -> str:
    out = f"/tmp/sibb_dbltap_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", out],
        check=True, capture_output=True)
    return out


def _log(rec: dict) -> None:
    with open(LOG, "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def _signals(snap) -> dict:
    sw = getattr(snap, "screen_width", 402)
    sh = getattr(snap, "screen_height", 874)
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
        "max_el_width": max_w,
        "screen_w": sw, "screen_h": sh,
        "zoom_scale_swift": getattr(snap, "zoom_scale", None),
        "n_els": len(snap.elements),
    }


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


def _find_body_text_coord(snap):
    """Identify a coordinate likely to land on non-interactive body
    text. Strategy: look for [other] elements with a long (>=20 char)
    label that aren't form-related (no 'form', 'input', 'rsvp' etc.
    in the label or nearby text). Returns (x, y) of the center."""
    BAD_TOKENS = ("form", "input", "rsvp", "button", "send",
                   "back", "reset", "address", "toolbar",
                   "typing predictions")
    sw = getattr(snap, "screen_width", 402)
    sh = getattr(snap, "screen_height", 874)
    candidates = []
    for el in snap.elements:
        if not el.frame:
            continue
        if not el.label or len(el.label) < 20:
            continue
        if el.role not in ("other",):
            continue
        lab = el.label.lower()
        if any(t in lab for t in BAD_TOKENS):
            continue
        cx = el.frame.x + el.frame.width / 2
        cy = el.frame.y + el.frame.height / 2
        # Require on-screen.
        if not (0 < cx < sw and 50 < cy < sh - 100):
            continue
        candidates.append((el, cx, cy))
    if not candidates:
        return None
    # Pick the one closest to vertical middle of the visible viewport.
    candidates.sort(key=lambda c: abs(c[2] - sh * 0.5))
    el, cx, cy = candidates[0]
    return (cx, cy, el.label[:60])


async def _capture(reader, udid, slug, note):
    snap = await reader.observe()
    sig = _signals(snap)
    png = _shot(udid, slug)
    _log({"slug": slug, "note": note, "signals": sig, "png": png})
    of = f"{sig['overflow']:.2f}" if sig['overflow'] else "?"
    print(f"\n── {slug} ── {note}")
    print(f"  kb={sig['kb']}  kb_y={sig['kb_y']}  overflow={of}  "
          f"max_w={sig['max_el_width']:.0f}  els={sig['n_els']}")
    return snap


async def main(udid: str) -> int:
    Path(LOG).unlink(missing_ok=True)
    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    site = MockSite(
        site_id="dbltap-probe",
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

    snap_a0 = await _capture(reader, udid, "A0_baseline",
                              "form loaded, no focus")

    # Find an input to focus.
    inp = (_find_input(snap_a0, ("email", "contact"))
           or _find_input(snap_a0, ("name", "guest"))
           or _find_input(snap_a0, ("attending",)))
    if inp is None:
        print("[probe] no input; abort"); site.stop(); return 2
    print(f"[probe] focusing input: {inp.label!r}")
    await reader.tap(x=inp.frame.center_x, y=inp.frame.center_y)
    await asyncio.sleep(1.0)
    snap_a1 = await _capture(reader, udid, "A1_focused_zoomed",
                              "kb up + auto-zoomed")

    # Tap Done to dismiss kb (page still zoomed per prior probe).
    done = _find_by_label(snap_a1, "done")
    if done and done.frame:
        print(f"[probe] tapping accessory 'Done'")
        await reader.tap(x=done.frame.center_x, y=done.frame.center_y)
        await asyncio.sleep(1.0)
    else:
        print("[probe] no Done found; falling back to outside-tap")
        await reader.tap(x=200, y=200)
        await asyncio.sleep(1.0)
    snap_a2 = await _capture(reader, udid, "A2_kb_dismissed_still_zoomed",
                              "kb gone; page expected still zoomed")

    # Hard-coded coord on body paragraph text (verified by inspecting
    # A2 screenshot — at this y the visible content is "should be
    # considered before proceeding" body paragraph, clearly NOT an
    # input or interactive element). This is more reliable than AX-
    # heuristic selection which picked a scroll-bar indicator in the
    # first run.
    cx, cy, label = 200, 500, "<hardcoded body paragraph y=500>"
    print(f"\n[probe] double-tap target: ({cx:.0f}, {cy:.0f}) "
           f"label={label!r}")

    # Approach #3: two rapid `xc.tap()` calls.
    # Try with tighter timing — 80ms inter-tap delay. iOS Safari's
    # WebKit recognizer typically wants both taps within 250ms.
    print(f"[probe] dispatching tap #1 at ({cx:.0f}, {cy:.0f})")
    await reader.tap(x=cx, y=cy)
    await asyncio.sleep(0.08)
    print(f"[probe] dispatching tap #2 at ({cx:.0f}, {cy:.0f})")
    await reader.tap(x=cx, y=cy)
    await asyncio.sleep(0.8)  # let WebKit recognizer settle

    snap_c = await _capture(reader, udid, "C_after_double_tap",
                             "after two rapid taps at body-text coord")

    # Also capture +500ms in case zoom-out is animated and the screenshot
    # caught mid-animation.
    await asyncio.sleep(0.7)
    snap_c2 = await _capture(reader, udid, "C2_after_500ms_more",
                              "additional 500ms settle")

    # Summary comparison.
    print("\n" + "═" * 70)
    print("  SUMMARY — overflow ratio across phases")
    print("═" * 70)
    for slug in ("A0_baseline", "A1_focused_zoomed",
                  "A2_kb_dismissed_still_zoomed",
                  "C_after_double_tap", "C2_after_500ms_more"):
        # Re-read log.
        with open(LOG) as fh:
            for line in fh:
                r = json.loads(line)
                if r["slug"] == slug:
                    s = r["signals"]
                    of = (f"{s['overflow']:.2f}"
                          if s['overflow'] else "?")
                    print(f"  {slug:<35} overflow={of}  "
                          f"max_w={s['max_el_width']:.0f}  "
                          f"kb={s['kb']}")
                    break

    site.stop()
    print("\n[probe] done — compare PNGs:")
    print("  /tmp/sibb_dbltap_A0_baseline.png  (fit-to-page)")
    print("  /tmp/sibb_dbltap_A1_focused_zoomed.png  (zoomed + kb)")
    print("  /tmp/sibb_dbltap_A2_kb_dismissed_still_zoomed.png  "
           "(zoomed, no kb)")
    print("  /tmp/sibb_dbltap_C_after_double_tap.png  (THE TEST)")
    print("  /tmp/sibb_dbltap_C2_after_500ms_more.png  (settle)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_double_tap_zoom_reset.py <UDID>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
