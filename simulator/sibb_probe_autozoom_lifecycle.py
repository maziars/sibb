"""Probe: Safari auto-zoom lifecycle + accessory bar usefulness.

Empirical answers to two design questions that gate ALT-1 (zoom-latch
state shape) and ALT-2 (accessory-bar detection necessity):

Phase A — auto-zoom lifecycle:
  - When kb dismisses, does the zoom signal go away too?
  - Does the page visually re-zoom to 1.0 when kb closes? (zoom is
    a WebView property, not a kb property — easy to assume wrong)
  - Are zoom signals STABLE across consecutive observations during
    a single zoom state, or do they flicker?
  - Does re-focusing the SAME input re-zoom identically? A
    DIFFERENT input?

  These decide ALT-1: if signals are stable and the zoom STATE always
  matches the kb state, the latch is solving a problem that doesn't
  exist and we should use ALT-1b (stateless).

Phase B — accessory bar usefulness:
  - With kb up, what AX elements live in the
    `[kb_y_top - 80, kb_y_top]` slab?
  - Are their labels descriptive (literal "Done", "Next", "Previous")?
  - Does tapping `Done` actually dismiss the kb?
  - Does tapping `Next` advance focus to the next form input?

  These decide ALT-2: if the bar contains genuinely useful interactive
  elements with descriptive labels, the current design (filter the
  whole strip via `accessory_bar_frame` → `keyboard_y_min`) is hiding
  features from the agent. We should keep the bar visible and only
  filter elements BELOW kb_frame (truly unreachable).

Output:
  - /tmp/sibb_probe_autozoom_<step>.png  — annotated screenshots
  - /tmp/sibb_probe_autozoom_log.jsonl   — every observation captured

Usage:
  python3 sibb/simulator/sibb_probe_autozoom_lifecycle.py <UDID>
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

LOG_PATH = "/tmp/sibb_probe_autozoom_log.jsonl"


def _log(record: dict) -> None:
    """Append one JSONL row to the probe log."""
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _screenshot(udid: str, slug: str) -> str:
    """Capture the sim's current screen to /tmp."""
    out = f"/tmp/sibb_probe_autozoom_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", out],
        check=True, capture_output=True)
    return out


def _zoom_signals(snap) -> dict:
    """Compute the same 3 zoom signals our scaffold uses. Logged at
    every observation so we can see whether they flicker or stay
    stable across consecutive snapshots in the same zoom state."""
    sw = getattr(snap, "screen_width", 402)
    sh = getattr(snap, "screen_height", 874)
    zoom_scale = getattr(snap, "zoom_scale", None)
    kb_frame = getattr(snap, "keyboard_frame", None)
    kb_y_top = kb_frame.get("y") if kb_frame else None
    max_width = 0.0
    for el in snap.elements:
        if el.frame and el.frame.width > max_width:
            max_width = el.frame.width
    return {
        "screen_w": sw,
        "screen_h": sh,
        "kb_visible": getattr(snap, "keyboard_visible", False),
        "kb_y_top": kb_y_top,
        "kb_above_screen": (kb_y_top is not None and kb_y_top > sh + 1),
        "zoom_scale_swift": zoom_scale,
        "max_element_width": max_width,
        "overflow_ratio": (max_width / sw) if sw else None,
        "overflow_detected": (max_width > sw * 1.10),
        "n_elements": len(snap.elements),
    }


def _accessory_slab_elements(snap) -> list:
    """Return all elements whose frame intersects the slab
    `[kb_y_top - 80, kb_y_top]` — the accessory-bar region. With kb
    down, returns []."""
    kb_frame = getattr(snap, "keyboard_frame", None)
    if not kb_frame:
        return []
    kb_y_top = kb_frame.get("y")
    if kb_y_top is None:
        return []
    slab_lo = kb_y_top - 80
    slab_hi = kb_y_top + 5  # a bit below to catch frames straddling
    out = []
    for el in snap.elements:
        if not el.frame:
            continue
        # Intersects the slab if any vertical overlap.
        if (el.frame.y + el.frame.height >= slab_lo
                and el.frame.y <= slab_hi):
            out.append({
                "ref": el.ref[:8],
                "role": el.role,
                "label": el.label,
                "value": el.value,
                "frame": (round(el.frame.x), round(el.frame.y),
                          round(el.frame.width), round(el.frame.height)),
                "hittable": getattr(el, "hittable", None),
                "focused": getattr(el, "focused", False),
            })
    return out


def _find_input(snap, label_keys):
    for el in snap.elements:
        lbl = (el.label or "").lower()
        if el.role == "input" and any(k in lbl for k in label_keys):
            return el
    return None


def _find_by_label(snap, label_substring):
    """Find first element whose label contains `label_substring`
    (case-insensitive). Used to locate Done / Next / Previous on the
    accessory bar."""
    key = label_substring.lower()
    for el in snap.elements:
        if el.label and key in el.label.lower():
            return el
    return None


async def _capture(reader, udid, slug, phase, note=""):
    """Take a screenshot + AX snapshot. Log everything."""
    snap = await reader.observe()
    sig = _zoom_signals(snap)
    bar = _accessory_slab_elements(snap)
    png_path = _screenshot(udid, slug)
    rec = {
        "phase": phase,
        "slug": slug,
        "note": note,
        "signals": sig,
        "accessory_slab": bar,
        "png": png_path,
    }
    _log(rec)
    overflow_pretty = (f"{sig['overflow_ratio']:.2f}"
                        if sig['overflow_ratio'] is not None else "?")
    print(f"\n── {phase} :: {slug} ── {note}")
    print(f"  kb={sig['kb_visible']} kb_y={sig['kb_y_top']} "
           f"kb_above={sig['kb_above_screen']} "
           f"overflow={overflow_pretty} "
           f"swift_zoom={sig['zoom_scale_swift']}")
    print(f"  els={sig['n_elements']}  bar_slab_count={len(bar)}")
    for el in bar:
        print(f"    @{el['ref']} [{el['role']}] {el['label']!r} "
               f"@{el['frame']} hittable={el['hittable']} "
               f"focused={el['focused']}")
    return snap


async def main(udid: str) -> int:
    # Reset the log file.
    Path(LOG_PATH).unlink(missing_ok=True)

    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    site = MockSite(
        site_id="autozoom-probe",
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

    # ─── PHASE A: auto-zoom lifecycle ─────────────────────────────────
    print("\n" + "═" * 60)
    print(" PHASE A — auto-zoom lifecycle")
    print("═" * 60)

    # A0 — baseline: form loaded, no focus, no zoom.
    snap = await _capture(reader, udid, "A0_baseline", "A",
                          "form loaded, no input focused")

    # Find a text input to focus.
    input_el = (_find_input(snap, ("name", "registrant", "guest"))
                or _find_input(snap, ("email", "contact"))
                or _find_input(snap, ("attending", "rsvp")))
    if input_el is None:
        print("[probe] no input found; aborting")
        site.stop()
        return 2
    print(f"[probe] target input: {input_el.label!r} "
           f"@({input_el.frame.center_x:.0f},{input_el.frame.center_y:.0f})")

    # A1 — TAP input → focus + auto-zoom.
    await reader.tap(x=input_el.frame.center_x, y=input_el.frame.center_y)
    await asyncio.sleep(1.0)

    # A1a/A1b/A1c — sample every 250ms for 750ms. Are signals stable?
    await _capture(reader, udid, "A1a_focused_t0", "A",
                   "just focused; first observation")
    await asyncio.sleep(0.25)
    await _capture(reader, udid, "A1b_focused_t250", "A",
                   "250ms after focus")
    await asyncio.sleep(0.25)
    await _capture(reader, udid, "A1c_focused_t500", "A",
                   "500ms after focus")

    # A2 — dismiss kb. Try TAP outside (a safe spot near y=200).
    print("\n[probe] tapping outside input to dismiss kb")
    await reader.tap(x=200, y=200)
    await asyncio.sleep(1.0)

    # A2a/A2b/A2c — sample 3x post-dismiss. Did zoom go away?
    await _capture(reader, udid, "A2a_kb_dismissed_t0", "A",
                   "post-kb-dismiss; first observation")
    await asyncio.sleep(0.5)
    await _capture(reader, udid, "A2b_kb_dismissed_t500", "A",
                   "500ms post-dismiss")
    await asyncio.sleep(0.5)
    await _capture(reader, udid, "A2c_kb_dismissed_t1000", "A",
                   "1000ms post-dismiss")

    # A3 — re-focus same input. Does zoom return identically?
    print("\n[probe] re-focusing same input")
    await reader.tap(x=input_el.frame.center_x, y=input_el.frame.center_y)
    await asyncio.sleep(1.0)
    snap_a3 = await _capture(reader, udid, "A3_refocused_same", "A",
                              "re-focused same input")

    # A4 — focus DIFFERENT input. Does zoom change?
    other = _find_input(snap_a3, ("email", "contact"))
    if other and other.ref != input_el.ref:
        print(f"\n[probe] focusing different input: {other.label!r}")
        await reader.tap(x=other.frame.center_x, y=other.frame.center_y)
        await asyncio.sleep(1.0)
        await _capture(reader, udid, "A4_other_input", "A",
                       f"focused different input ({other.label!r})")

    # ─── PHASE B: accessory bar usefulness ────────────────────────────
    print("\n" + "═" * 60)
    print(" PHASE B — accessory bar usefulness")
    print("═" * 60)

    # B0 — current state: kb up with bar. Capture is already done as A4
    # (or A3). Re-capture for clarity under the B phase tag.
    snap_b = await _capture(reader, udid, "B0_kb_up_bar_visible", "B",
                             "kb up; survey bar contents")

    # Did the bar elements show up in the accessory slab?
    bar_elements = _accessory_slab_elements(snap_b)
    done_el = _find_by_label(snap_b, "done")
    next_el = _find_by_label(snap_b, "next")
    prev_el = _find_by_label(snap_b, "previous")

    # B1 — TAP Next, verify focus advances.
    if next_el and next_el.frame:
        print(f"\n[probe] tapping accessory 'Next' "
               f"@({next_el.frame.center_x:.0f},"
               f"{next_el.frame.center_y:.0f})")
        await reader.tap(x=next_el.frame.center_x,
                          y=next_el.frame.center_y)
        await asyncio.sleep(0.8)
        await _capture(reader, udid, "B1_after_next", "B",
                       "after tapping accessory 'Next'")
    else:
        print("[probe] no 'Next' element found in accessory slab")

    # B2 — TAP Previous, verify focus goes back.
    snap_b1 = await reader.observe()
    prev_el = _find_by_label(snap_b1, "previous")
    if prev_el and prev_el.frame:
        print(f"\n[probe] tapping accessory 'Previous'")
        await reader.tap(x=prev_el.frame.center_x,
                          y=prev_el.frame.center_y)
        await asyncio.sleep(0.8)
        await _capture(reader, udid, "B2_after_previous", "B",
                       "after tapping accessory 'Previous'")
    else:
        print("[probe] no 'Previous' element found")

    # B3 — TAP Done, verify kb dismisses.
    snap_b2 = await reader.observe()
    done_el = _find_by_label(snap_b2, "done")
    if done_el and done_el.frame:
        print(f"\n[probe] tapping accessory 'Done'")
        await reader.tap(x=done_el.frame.center_x,
                          y=done_el.frame.center_y)
        await asyncio.sleep(1.0)
        await _capture(reader, udid, "B3_after_done", "B",
                       "after tapping accessory 'Done' (kb should go down)")
    else:
        print("[probe] no 'Done' element found")

    site.stop()
    print("\n[probe] done")
    print(f"  log: {LOG_PATH}")
    print(f"  pngs: /tmp/sibb_probe_autozoom_*.png")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_autozoom_lifecycle.py <UDID>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
