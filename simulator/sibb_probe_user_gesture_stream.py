"""User-driven gesture probe: stream the AX while you interact with
the sim manually.

Flow:
  1. Probe opens the RSVP form, focuses an input, dismisses kb via
     Done → page in still-zoomed state (kb=False, page visibly zoomed).
  2. Probe streams AX every 500ms. For each frame: logs the signals,
     prints one line. When any signal changes from the prior frame,
     captures a screenshot + timestamps the change.
  3. YOU interact with the sim manually — try double-tap, pinch,
     tap-the-URL-bar, whatever. Each gesture's effect is logged.
  4. Press Ctrl+C when done. Final summary printed.

For double-tap on the macOS Simulator:
  - With a trackpad: tap the trackpad twice quickly (don't click).
  - With a mouse: click twice quickly.
  - On the sim's window: macOS forwards your tap to iOS as a single-
    finger touch, so a real double-click registers as a real double-tap
    in iOS.

Output:
  /tmp/sibb_userstream_<timestamp>_<change_id>.png
  /tmp/sibb_userstream_log.jsonl
  stdout: one line per snapshot, "* CHANGED *" markers on transitions
"""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))

LOG = "/tmp/sibb_userstream_log.jsonl"


def _shot(udid: str, slug: str) -> str:
    out = f"/tmp/sibb_userstream_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", out],
        check=True, capture_output=True)
    return out


def _log(rec: dict) -> None:
    with open(LOG, "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def _signals(snap) -> dict:
    sw = getattr(snap, "screen_width", 402)
    kb_frame = getattr(snap, "keyboard_frame", None)
    kb_y = kb_frame.get("y") if kb_frame else None
    max_w = 0.0
    max_label = None
    for el in snap.elements:
        if el.frame and el.frame.width > max_w:
            max_w = el.frame.width
            max_label = el.label
    # Build a stable signature for change detection.
    sig = {
        "kb": getattr(snap, "keyboard_visible", False),
        "kb_y": int(kb_y) if kb_y else None,
        "overflow": round(max_w / sw, 2) if sw else None,
        "max_w": int(max_w),
        "max_w_label": (max_label[:40] if max_label else None),
        "n_els": len(snap.elements),
    }
    return sig


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


_STOP = False


def _on_sigint(*_):
    global _STOP
    _STOP = True


async def main(udid: str) -> int:
    Path(LOG).unlink(missing_ok=True)
    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    site = MockSite(
        site_id="userstream-probe",
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

    # Setup state: focus input → kb up + zoom → tap Done → still zoomed
    snap = await reader.observe()
    inp = (_find_input(snap, ("email", "contact"))
           or _find_input(snap, ("name", "guest")))
    if inp is None:
        print("[probe] no input; abort"); site.stop(); return 2
    print(f"\n[probe] Setting up state...")
    print(f"  1. Focus input: {inp.label!r} "
           f"@({inp.frame.center_x:.0f},{inp.frame.center_y:.0f})")
    await reader.tap(x=inp.frame.center_x, y=inp.frame.center_y)
    await asyncio.sleep(1.0)
    snap = await reader.observe()
    done = _find_by_label(snap, "done")
    if done and done.frame:
        print(f"  2. Tap accessory 'Done' to dismiss kb")
        await reader.tap(x=done.frame.center_x, y=done.frame.center_y)
        await asyncio.sleep(1.0)

    sig0 = _signals(await reader.observe())
    png_initial = _shot(udid, "00_initial_state")
    _log({"ts": 0.0, "signals": sig0, "png": png_initial,
          "note": "initial state (kb dismissed, page should be zoomed)"})

    print("\n" + "═" * 72)
    print(f" READY — sim is at: kb={sig0['kb']}  "
           f"overflow={sig0['overflow']}  max_w={sig0['max_w']}")
    print(f" Initial screenshot: {png_initial}")
    print("═" * 72)
    print("\nNow MANUALLY interact with the sim window. Try:")
    print("  • Double-tap somewhere on the page (NOT on an input)")
    print("  • Pinch out / in (Option + drag on trackpad)")
    print("  • Tap the URL bar")
    print("  • Anything else you want to test")
    print("\nThis probe will stream the AX every 500ms and capture")
    print("a screenshot whenever a signal changes.")
    print(f"\nLog file: {LOG}")
    print("\nPress Ctrl+C when done.\n")

    signal.signal(signal.SIGINT, _on_sigint)

    prev_sig = sig0
    change_id = 1
    frame_id = 0
    t_start = time.time()
    while not _STOP:
        await asyncio.sleep(0.5)
        try:
            snap = await reader.observe()
        except Exception as e:
            print(f"[probe] observe failed: {e}; retrying")
            continue
        sig = _signals(snap)
        ts = time.time() - t_start
        frame_id += 1

        # Significant change detection: kb, overflow, or n_els.
        changed_keys = [k for k in ("kb", "overflow", "max_w",
                                       "max_w_label", "kb_y")
                        if sig[k] != prev_sig[k]]
        if changed_keys:
            slug = f"{frame_id:03d}_change_{change_id}"
            png = _shot(udid, slug)
            change_id += 1
            print(f"\n* CHANGED * @ t={ts:6.2f}s  diff: {changed_keys}")
            print(f"  prev: kb={prev_sig['kb']} "
                   f"overflow={prev_sig['overflow']} "
                   f"max_w={prev_sig['max_w']} "
                   f"label={prev_sig['max_w_label']!r}")
            print(f"  now:  kb={sig['kb']} "
                   f"overflow={sig['overflow']} "
                   f"max_w={sig['max_w']} "
                   f"label={sig['max_w_label']!r}")
            print(f"  → {png}")
            _log({"ts": round(ts, 2),
                  "frame_id": frame_id,
                  "change_id": change_id - 1,
                  "diff": changed_keys,
                  "prev": prev_sig,
                  "now": sig,
                  "png": png})
            prev_sig = sig
        else:
            # Quiet frame — print one-line status every 4s.
            if int(ts) % 4 == 0 and int(ts * 2) % 8 == 0:
                of = (f"{sig['overflow']:.2f}"
                      if sig['overflow'] else "?")
                print(f"  ...t={ts:6.2f}s  kb={sig['kb']} "
                       f"overflow={of} "
                       f"max_w={sig['max_w']}")

    # Final summary.
    print("\n" + "═" * 72)
    print(f" Done. {change_id - 1} signal changes captured "
           f"over {time.time() - t_start:.1f}s.")
    print(f" Log: {LOG}")
    print(f" Screenshots: /tmp/sibb_userstream_*.png")
    print("═" * 72)

    site.stop()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_user_gesture_stream.py <UDID>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
