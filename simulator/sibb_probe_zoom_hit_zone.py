"""Probe: do iOS Safari's hit-zones drift consistently downward (or
upward) for closely-stacked buttons under auto-zoom?

Hypothesis under test (user-proposed, 2026-06-07):
    When tappable centers are stacked <44 pt apart (Apple's HIG
    minimum), iOS's hit-test snaps the tap toward one consistent
    neighbor — i.e. tapping at AX-center y=647 for "Preview"
    consistently lands on "Remind Me Later" (AX-center y=676)
    because iOS biases hit-resolution toward one direction when
    targets are tightly stacked. The bias may be zoom-induced or
    just stacking-induced.

How this probe tests it
=======================
1. Renders the seed=1 form (font-size=13 px → auto-zoom triggers)
   AND the seed=3 form (font-size=16 px → no zoom) back-to-back.
2. For each condition, opens Safari, focuses an input (to put the
   keyboard up so the layout matches what an agent would actually
   see), takes one AX snapshot, identifies the four distractor
   buttons (submit / discard / preview / remind), and SCANS y-values
   in 4 pt steps across the union of their AX frames.
3. At each y-step, taps (x_common, y) and reads which `action=`
   fired from MockSite's submission log. Then taps Back to return.
4. Builds a hit-zone map per condition:

       y=540  → submit
       y=544  → submit
       ...
       y=580  → submit
       y=584  → discard
       ...

5. Compares each button's empirical hit-zone center to its
   AX-reported center. If the empirical center is offset
   consistently downward under zoom but not under no-zoom →
   (2) hit-test bias is confirmed AND zoom-specific.
   If both conditions show offset → just stacking.
   If neither → re-flow / something else.

Caveats
=======
- 30+ taps per condition × 2 conditions × ~3 s/tap ≈ 3 minutes.
- Each tap navigates to /rsvp success page; we tap Back between.
- Re-focuses an input each iteration so zoom state persists (Safari
  may release zoom after chrome taps; the input refocus restores it).
- The success-page URL has `?action=submit_response&...` for the
  real button, `?action=discard|preview|remind_me_later` for decoys —
  used to disambiguate.

Usage
=====
    python3 sibb/simulator/sibb_probe_zoom_hit_zone.py <UDID>
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))


_BUTTON_KEYS = ("submit", "discard", "preview", "remind")


def _shot(udid: str, slug: str) -> str:
    path = f"/tmp/sibb_hitzone_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", path],
        check=True, capture_output=True,
    )
    return path


def _pick_buttons(snap) -> Dict[str, object]:
    """Return {key: element} for each of the 4 buttons, by label match."""
    out: Dict[str, object] = {}
    for el in snap.elements:
        if el.role != "btn":
            continue
        lbl = (el.label or "").lower()
        for key in _BUTTON_KEYS:
            if key in lbl and key not in out:
                out[key] = el
    return out


def _action_of_last_post(site_id: str) -> Optional[str]:
    from sibb_mock_site import get_site
    site = get_site(site_id)
    if site is None:
        return None
    subs = site.submissions(include_decoys=True)
    if not subs:
        return None
    last = subs[-1]
    # MockSite returns each submission as a dict with keys: path,
    # fields, fields_raw, is_decoy. Real submit POSTs to /rsvp (no
    # `action` field); decoys POST to DECOY_PATH with
    # `action=discard|preview|remind_me_later`.
    return last.get("fields", {}).get("action") or last.get("path")


def _classify(action_or_path: Optional[str]) -> str:
    """Map the action= or path back to a button key."""
    if action_or_path is None:
        return "—"
    s = action_or_path.lower()
    if s == "/rsvp":
        return "submit"
    if "discard" in s:
        return "discard"
    if "preview" in s:
        return "preview"
    if "remind" in s:
        return "remind"
    return f"?{action_or_path}"


async def _scan_condition(
        udid: str, page_seed: int, condition_tag: str
        ) -> Tuple[Dict[str, object], List[Tuple[int, str]], float]:
    """Open MockSite + Safari for `page_seed`, focus an input to keep
    zoom (or not) consistent, scan y-coords, return:
      (ax_buttons_dict, [(y, fired_key), ...], font_size_px)
    """
    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    site_id = f"hitzone-{condition_tag}"
    site = MockSite(
        site_id=site_id,
        static_pages={"/event": PAGE_REGISTRY["rsvp_event"]},
    )
    site.page_seed = page_seed
    site.start()

    # Pull rendered HTML so we can extract the font-size for the report.
    html = urllib.request.urlopen(
        f"{site.base_url}/event", timeout=3).read().decode()
    # Find the inline style block; cheap regex avoidance.
    px = 0.0
    if "font-size:" in html:
        seg = html.split("font-size:", 1)[1]
        try:
            px = float(seg.split("px")[0])
        except ValueError:
            px = -1.0
    print(f"\n══════════════════════════════════════════════════════════")
    print(f"  CONDITION: {condition_tag}  "
          f"page_seed={page_seed}  font-size={px:.0f}px")
    print(f"  expected zoom: {'YES' if px < 16 else 'NO'}")
    print(f"══════════════════════════════════════════════════════════")

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

    # Focus an input so zoom (or not) is established and the layout
    # matches the agent's normal observe-time state.
    snap0 = await reader.observe()
    inp = next(
        (e for e in snap0.elements if e.role == "input"
         and (e.label or "").lower() not in ("address",)),
        None)
    if inp is None:
        print("  [scan] no form input found; abort")
        site.stop()
        return ({}, [], px)
    print(f"  [scan] focusing input '{inp.label}' at "
          f"({inp.frame.center_x:.0f},{inp.frame.center_y:.0f})")
    await reader.tap(x=inp.frame.center_x, y=inp.frame.center_y)
    await asyncio.sleep(1.2)

    snap = await reader.observe()
    btns = _pick_buttons(snap)
    print("  [scan] AX-reported button positions (post-focus):")
    for key in _BUTTON_KEYS:
        el = btns.get(key)
        if el is None:
            print(f"    {key}: NOT IN AX TREE")
        else:
            fr = el.frame
            print(f"    {key:8s} y={fr.y:.0f} center_y={fr.center_y:.0f} "
                  f"h={fr.height:.0f} x={fr.x:.0f}-{fr.x + fr.width:.0f}")

    if len(btns) < 2:
        print("  [scan] fewer than 2 buttons visible — abort scan")
        _shot(udid, f"{condition_tag}_abort")
        site.stop()
        return (btns, [], px)

    # Pick the x-band common to all buttons. Use min(right_edge) on the
    # left and max(left_edge) on the right; tap the midpoint of that
    # intersection. Falls back to the first button's center_x if the
    # intersection is empty.
    lefts = [btns[k].frame.x for k in btns]
    rights = [btns[k].frame.x + btns[k].frame.width for k in btns]
    common_left = max(lefts)
    common_right = min(rights)
    if common_right > common_left:
        x_scan = (common_left + common_right) / 2
    else:
        x_scan = btns[_BUTTON_KEYS[0]].frame.center_x
    print(f"  [scan] x_scan = {x_scan:.0f}")

    # Y range: 8 pt above topmost button to 8 pt below bottommost.
    y_top = min(btns[k].frame.y for k in btns) - 8
    y_bottom = max(btns[k].frame.y + btns[k].frame.height for k in btns) + 8
    y_top = max(0, int(y_top))
    y_bottom = int(y_bottom)
    step = 4
    print(f"  [scan] y range [{y_top}..{y_bottom}] step {step}")
    print(f"  [scan] {(y_bottom - y_top) // step + 1} taps incoming…")

    annotated = _shot(udid, f"{condition_tag}_pre_scan")
    print(f"  [scan] pre-scan screenshot: {annotated}")

    fired: List[Tuple[int, str]] = []
    last_n = 0
    for y in range(y_top, y_bottom + 1, step):
        # Tap.
        await reader.tap(x=x_scan, y=y)
        await asyncio.sleep(1.3)  # let nav settle
        action = _action_of_last_post(site_id)
        # If no new POST, fired is "miss" (tap landed on non-button
        # whitespace or the form alert).
        from sibb_mock_site import get_site
        sub_n = len(get_site(site_id).submissions(include_decoys=True))
        if sub_n > last_n:
            key = _classify(action)
            last_n = sub_n
        else:
            key = "miss"
        fired.append((y, key))
        print(f"  y={y:3d} → {key}")

        # Navigate back to the form so the next iteration has buttons
        # again. The success page has a Safari Back button in the
        # chrome.
        if sub_n > last_n - 1:  # only if nav happened
            snap_post = await reader.observe()
            back = next(
                (e for e in snap_post.elements if e.role == "btn"
                 and (e.label or "").lower() == "back" and e.frame),
                None)
            if back is not None:
                await reader.tap(x=back.frame.center_x,
                                  y=back.frame.center_y)
                await asyncio.sleep(1.2)
                # Re-focus an input to restore zoom condition.
                snap_post = await reader.observe()
                inp2 = next(
                    (e for e in snap_post.elements if e.role == "input"
                     and (e.label or "").lower() not in ("address",)),
                    None)
                if inp2 is not None:
                    await reader.tap(x=inp2.frame.center_x,
                                      y=inp2.frame.center_y)
                    await asyncio.sleep(1.0)

    site.stop()
    return (btns, fired, px)


def _summarize(condition_tag: str, btns: Dict[str, object],
                fired: List[Tuple[int, str]], px: float) -> None:
    """Print zone boundaries + empirical-center vs AX-center per button."""
    print(f"\n──────────────────────────────────────────────────────")
    print(f"  HIT-ZONE SUMMARY: {condition_tag}  (font={px:.0f}px)")
    print(f"──────────────────────────────────────────────────────")
    # Group consecutive y's by which button they fired.
    zones: Dict[str, List[int]] = {}
    for y, key in fired:
        zones.setdefault(key, []).append(y)
    for key in _BUTTON_KEYS + ("miss", "—"):
        ys = zones.get(key, [])
        if not ys:
            continue
        ax = btns.get(key)
        ax_cy = ax.frame.center_y if ax is not None else None
        emp_min = min(ys)
        emp_max = max(ys)
        emp_cy = (emp_min + emp_max) / 2
        ax_str = f"{ax_cy:.0f}" if ax_cy is not None else "—"
        if ax_cy is not None:
            delta = emp_cy - ax_cy
            sign = "+" if delta >= 0 else ""
            delta_str = f"Δ={sign}{delta:+.1f}"
        else:
            delta_str = ""
        print(f"  {key:8s} AX_cy={ax_str:>4s}  empirical=[{emp_min}..{emp_max}] "
              f"emp_cy={emp_cy:.1f}  {delta_str}")
    if "miss" in zones:
        print(f"  ({len(zones['miss'])} taps landed on no button — "
              f"likely whitespace or alert overlay)")


async def main(udid: str, only: Optional[str] = None) -> int:
    # CONDITION 1: zoomed (seed=1 → page_seed 506456970 → font-size 13)
    if only in (None, "zoom"):
        btns_z, fired_z, px_z = await _scan_condition(
            udid, page_seed=506456970, condition_tag="zoomed_13px")
        _summarize("zoomed_13px", btns_z, fired_z, px_z)
    else:
        btns_z, fired_z, px_z = {}, [], 0.0
    if only == "zoom":
        return 0

    # CONDITION 2: SAME page_seed (identical labels, filler, distractor
    # order) but font_size_px FORCED to 16 → no zoom. This is the
    # tightest possible control: layout differs only in input text-size
    # (and thus zoom-on-focus behavior). Monkeypatch
    # `rsvp_event_choices` so the template's downstream code uses 16.
    import harness_pages
    _orig_choices = harness_pages.rsvp_event_choices

    def _force16(rng):
        cfg = _orig_choices(rng)
        cfg = dict(cfg)
        cfg["font_size_px"] = 16
        return cfg
    harness_pages.rsvp_event_choices = _force16
    try:
        btns_n, fired_n, px_n = await _scan_condition(
            udid, page_seed=506456970,
            condition_tag="nozoom_force16_same_layout")
    finally:
        harness_pages.rsvp_event_choices = _orig_choices
    _summarize("nozoom_force16_same_layout", btns_n, fired_n, px_n)

    # COMPARISON
    print("\n══════════════════════════════════════════════════════════")
    print("  VERDICT")
    print("══════════════════════════════════════════════════════════")
    drift_z: Dict[str, float] = {}
    drift_n: Dict[str, float] = {}
    for key in _BUTTON_KEYS:
        for btns, fired, drifts in [(btns_z, fired_z, drift_z),
                                      (btns_n, fired_n, drift_n)]:
            ax = btns.get(key)
            if ax is None:
                continue
            ys = [y for y, k in fired if k == key]
            if not ys:
                continue
            emp_cy = (min(ys) + max(ys)) / 2
            drifts[key] = emp_cy - ax.frame.center_y

    print(f"  AX-center → empirical-center drift (positive = DOWN):")
    print(f"  {'button':10s} {'zoomed':>10s} {'no-zoom':>10s}")
    for key in _BUTTON_KEYS:
        z = drift_z.get(key)
        n = drift_n.get(key)
        zs = f"{z:+.1f}" if z is not None else "—"
        ns = f"{n:+.1f}" if n is not None else "—"
        print(f"  {key:10s} {zs:>10s} {ns:>10s}")

    if drift_z and drift_n:
        z_mean = sum(drift_z.values()) / len(drift_z)
        n_mean = sum(drift_n.values()) / len(drift_n)
        print(f"  {'mean':10s} {z_mean:+10.1f} {n_mean:+10.1f}")
        if abs(z_mean) > 6 and abs(n_mean) < 4:
            print("\n  → Zoom-specific drift confirmed. Hit-test bias under")
            print("    auto-zoom shifts taps {0} by ~{1:.0f} pt.".format(
                "down" if z_mean > 0 else "up", abs(z_mean)))
            print("    Fix: either reject taps on AX-spacings <44 pt under")
            print("    zoom, or PINCH-out before tapping decoy/control")
            print("    buttons (PINCH unreliable in Safari — DOUBLE_TAP).")
        elif abs(z_mean) > 6 and abs(n_mean) > 4:
            print("\n  → Drift in BOTH conditions. Bias is from close")
            print("    stacking (<44pt centers), NOT zoom. Same fix:")
            print("    reject too-close tap targets at the scaffold layer.")
        else:
            print("\n  → No consistent drift. The wrong-button-fire in")
            print("    the live agent run was caused by SOMETHING ELSE")
            print("    (re-flow between observe and tap, popup overlay,")
            print("    stale frame). Investigate re-flow next.")

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_zoom_hit_zone.py <UDID> "
              "[--only zoom|nozoom]",
              file=sys.stderr)
        sys.exit(2)
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
        assert only in ("zoom", "nozoom"), only
    sys.exit(asyncio.run(main(sys.argv[1], only=only)))
