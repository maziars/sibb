"""Probe: drive the RSVP form through the same sequence the agent
just took (TAP name, TYPE name, TAP email, TYPE email, TAP attending,
TYPE yes), THEN at the very moment the agent would tap Send response,
capture a screenshot + the AX snapshot and overlay them so we can
verify visually whether the AX-reported button position matches the
on-screen button position.

Output: /tmp/sibb_form_state_*.png
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


def _annotate(udid, snap, tag, slug):
    from PIL import Image, ImageDraw
    png_path = f"/tmp/sibb_form_state_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", png_path],
        check=True, capture_output=True)
    img = Image.open(png_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    img_w, _ = img.size
    screen_w = getattr(snap, "screen_width", 402) or 402
    sf = img_w / screen_w
    print(f"\n── {tag} ──")
    print(f"  elements: {len(snap.elements)} screen={screen_w}xH"
           f" scale={sf:.2f}")
    for el in snap.elements:
        lbl = (el.label or "").lower()
        fr = el.frame
        if fr is None:
            continue
        color = None
        if "send" in lbl or "submit" in lbl or "confirm" in lbl:
            color = (255, 0, 0, 255); name = "SUBMIT"
        elif "remind me later" in lbl:
            color = (255, 140, 0, 255); name = "DECOY1"
        elif "save for later" in lbl:
            color = (200, 100, 0, 255); name = "DECOY2"
        elif "help" in lbl and el.role == "btn":
            color = (160, 80, 0, 255); name = "DECOY3"
        elif getattr(el, "focused", False) and el.role == "input":
            color = (0, 200, 255, 255); name = "FOCUSED"
        elif el.role == "input":
            color = (0, 140, 255, 180); name = "INPUT"
        if color is None:
            continue
        x0, y0 = fr.x * sf, fr.y * sf
        x1, y1 = (fr.x + fr.width) * sf, (fr.y + fr.height) * sf
        draw.rectangle([x0, y0, x1, y1], outline=color, width=5)
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        r = 20
        draw.line([cx - r, cy, cx + r, cy], fill=color, width=4)
        draw.line([cx, cy - r, cx, cy + r], fill=color, width=4)
        info = (f"{name} AX=({fr.x:.0f},{fr.y:.0f}) "
                f"w={fr.width:.0f} h={fr.height:.0f}")
        print(f"  {info}")
        draw.text((x0 + 6, max(0, y0 - 28)), info, fill=color)
    out = Image.alpha_composite(img, overlay)
    out.convert("RGB").save(png_path, "PNG")
    print(f"  → {png_path}")
    return png_path


async def main(udid: str) -> int:
    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    site = MockSite(
        site_id="form-state",
        static_pages={"/event": PAGE_REGISTRY["rsvp_event"]},
    )
    # Same page_seed as the failing agent run (use the same generator
    # seed → same page_seed; for now hardcode something close).
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

    # 0. baseline (form just loaded)
    snap = await reader.observe()
    _annotate(udid, snap, "0_just_loaded", "0_just_loaded")

    # Drive the form. Find each input by label and TAP+TYPE.
    async def find_and_fill(label_keys, value):
        snap = await reader.observe()
        target = None
        for el in snap.elements:
            lbl = (el.label or "").lower()
            if el.role == "input" and any(k in lbl for k in label_keys):
                target = el
                break
        if target is None:
            print(f"[probe] no input matching {label_keys}; skipping")
            return False
        print(f"\n[probe] TAP+TYPE {target.label!r} → {value!r}")
        await reader.tap(x=target.frame.center_x,
                          y=target.frame.center_y)
        await asyncio.sleep(0.5)
        await reader.type_text(value)
        await asyncio.sleep(0.5)
        return True

    await find_and_fill(("name", "registrant", "guest"), "Riley Brooks")
    await find_and_fill(("email", "contact", "confirmation",
                          "registration"), "riley.b@example.org")
    await find_and_fill(("attending", "rsvp", "going",
                          "confirming"), "yes")

    # Take a snapshot + screenshot IMMEDIATELY before the submit tap.
    snap = await reader.observe()
    _annotate(udid, snap, "1_pre_submit", "1_pre_submit_filled")

    # Find the submit button.
    submit = None
    for el in snap.elements:
        lbl = (el.label or "").lower()
        if el.role == "btn" and (
                "send" in lbl or "submit" in lbl or "confirm" in lbl):
            submit = el
            break
    if submit is None:
        print("[probe] no submit button found; aborting")
        site.stop()
        return 2
    print(f"\n[probe] submit @ AX=({submit.frame.x:.0f},"
          f"{submit.frame.y:.0f}) w={submit.frame.width:.0f} "
          f"h={submit.frame.height:.0f} center=("
          f"{submit.frame.center_x:.0f},{submit.frame.center_y:.0f})")

    # TAP the button — does iOS actually receive it on the visible
    # portion? The "1_pre_submit" image already has the AX overlay
    # so we can compare visually.
    print(f"[probe] TAP at AX center ({submit.frame.center_x},"
          f"{submit.frame.center_y})")
    await reader.tap(x=submit.frame.center_x,
                      y=submit.frame.center_y)
    await asyncio.sleep(1.5)

    snap = await reader.observe()
    _annotate(udid, snap, "2_post_submit_tap", "2_post_submit_tap")

    site.stop()
    print("\n[probe] done — open the PNGs to compare.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_form_state.py <UDID>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
