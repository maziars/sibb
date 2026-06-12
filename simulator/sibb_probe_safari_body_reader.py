#!/usr/bin/env python3
"""Safari AX body-text exposure probe.
=====================================

Settles three questions raised when designing Safari generators:

  Q1. Is the "body summarized to fragments" finding in
      ``IOS_SIM_QUIRKS.md §14`` a VIEWPORT artifact (only the visible
      slice is in the snapshot) or a TRUE iOS truncation (body
      permanently absent regardless of scrolling)?

  Q2. Does toggling Safari's Reader Mode change the body exposure?

  Q3. If Reader Mode helps, by how much, and does scrolling inside
      Reader Mode reveal additional text?

The probe loads the same Wikipedia article §14 used (``/wiki/IOS``)
plus one denser article (``/wiki/Pluto``), snapshots, scrolls,
re-snapshots, then attempts to toggle Reader Mode, and re-snapshots.

It dumps:

  * cumulative unique substantive StaticText labels at each step,
  * delta vs. previous step,
  * a small sample of "newly revealed" labels per step.

A substantive label = any AX StaticText with ``len(label) >= 40``
(filters out nav chrome like "Edit", "Tools", "Languages").

Usage:

    SIBB_UDID=<udid> /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb/simulator/sibb_probe_safari_body_reader.py

Sim must be booted, XCUITestHelper already built.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "benchmark"))
from sibb_xcuitest_client import XCUITestReader  # noqa: E402
from sibb_state import _safari_clear_tab_state  # noqa: E402

SAFARI_BUNDLE = "com.apple.mobilesafari"

UDID = (os.environ.get("SIBB_UDID")
        or "19B95A95-614A-4ECA-B943-44FDADFD7A9F")

# iPhone 17 Pro logical viewport — same as the swipe-origins probe.
W, H = 393, 852

# Probe targets — same /wiki/IOS that §14 measured plus /wiki/Pluto
# (longer body, more sections; tests whether we hit a per-article
# ceiling or whether it's the same underlying truncation regardless
# of article length).
PROBES: List[Tuple[str, str]] = [
    ("https://en.wikipedia.org/wiki/IOS",   "wiki-IOS (§14 baseline)"),
    ("https://en.wikipedia.org/wiki/Pluto", "wiki-Pluto (dense body)"),
]

# Page-load + JS-settle.
LOAD_SETTLE_S = 8.0
# Time between a gesture and the next observe; needs to be enough
# for fling-deceleration + WebKit's accessibility-tree rebuild.
GESTURE_SETTLE_S = 1.2
# Time for Reader Mode chrome to render after we tap the toggle.
READER_TOGGLE_S = 2.0

# A "substantive" StaticText is anything 40+ chars after trimming —
# nav chrome (Edit / Tools / View source / Languages / etc.) is all
# under 25 chars, infobox cell values are 10-20 chars. Setting at 40
# captures sentence fragments and full sentences without false-
# positives from labeled metadata.
SUBSTANTIVE_MIN_CHARS = 40


# ── helpers ──────────────────────────────────────────────────────────


def substantive_labels(elements: List[Dict]) -> List[str]:
    """Returns trimmed labels of all elements long enough to plausibly
    be body content (regardless of role — iOS 26 Safari exposes web
    text under roles other than StaticText, including Other/Button)."""
    out: List[str] = []
    for e in elements:
        lab = (e.get("label") or "").strip()
        if len(lab) >= SUBSTANTIVE_MIN_CHARS:
            out.append(lab)
    return out


def role_dist_for_long_labels(elements: List[Dict]) -> Dict[str, int]:
    """Role counts restricted to elements with long labels — tells us
    WHICH role iOS 26 Safari is using to expose body content."""
    out: Dict[str, int] = {}
    for e in elements:
        lab = (e.get("label") or "").strip()
        if len(lab) >= SUBSTANTIVE_MIN_CHARS:
            r = e.get("role") or "?"
            out[r] = out.get(r, 0) + 1
    return out


def all_role_count(elements: List[Dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in elements:
        r = e.get("role") or "?"
        out[r] = out.get(r, 0) + 1
    return out


def find_url_bar_y(elements: List[Dict]) -> Optional[float]:
    """Return the y of the URL bar so we can swipe BELOW it (so the
    swipe is treated as a WebView pan, not a chrome interaction).

    The Swift XCUITest server normalizes element roles: textField /
    secureTextField → "input", searchField → "search". See
    sibb_xcuitest_setup.sh ROLE_NAMES.
    """
    for e in elements:
        if (e.get("role") or "") in ("input", "search"):
            lab = (e.get("label") or "").lower()
            if ("address" in lab or "url" in lab or "search" in lab
                    or "tabgroup" in lab or "google" in lab):
                fr = e.get("frame") or {}
                if fr.get("y") is not None:
                    # Server emits "width"/"height", not "w"/"h".
                    return float(fr["y"]) + float(fr.get("height", 36))
    # Fallback: top 10% of screen
    return H * 0.10


def find_reader_toggle(elements: List[Dict]) -> Optional[Dict]:
    """The 'aA' page-settings button in the URL bar opens a menu that
    contains 'Show Reader'. iOS 26 labels it inconsistently across
    states — discover by walking buttons in the URL-bar row.

    Returns the AX element dict for the button, or None.
    """
    candidates: List[Tuple[float, Dict]] = []
    for e in elements:
        # Swift server normalizes button → "btn"; "Button" never appears.
        if (e.get("role") or "") != "btn":
            continue
        lab = (e.get("label") or "").lower()
        ident = (e.get("identifier") or "").lower()
        # Known iOS labels for the page-settings affordance across
        # versions: "Website Settings", "Page Settings", "Show Reader",
        # "Reader", "Page Menu", "Show Reader View", "AA", "Format".
        if any(tok in lab for tok in
                ("page menu", "website settings", "page settings",
                 "show reader", "reader", "format menu", "format options",
                 "aa")):
            fr = e.get("frame") or {}
            y = float(fr.get("y", 9999))
            candidates.append((y, e))
        elif any(tok in ident for tok in
                  ("readerbutton", "formatmenu", "websitemenu",
                   "pagecontroller", "pagemenu")):
            fr = e.get("frame") or {}
            y = float(fr.get("y", 9999))
            candidates.append((y, e))
    if not candidates:
        return None
    # Prefer an exact "Page Menu" / "AA" match over generic "reader"
    # mentions (which can match content links on a Wikipedia article).
    def specificity(e: Dict) -> int:
        lab = (e.get("label") or "").lower()
        if "page menu" in lab:
            return 0
        if lab in ("aa", "format menu", "format options"):
            return 1
        if "website settings" in lab or "page settings" in lab:
            return 2
        return 5
    candidates.sort(key=lambda t: (specificity(t[1]), t[0]))
    return candidates[0][1]


def _find_topmost_popover_bounds(
        elements: List[Dict]) -> Optional[Tuple[float, float, float, float]]:
    """Find the smallest container whose role is "popover" / "sheet" /
    "ALERT" / "SHEET", returning (x, y, x+width, y+height). Returns
    None if no popover container is in the tree."""
    candidates: List[Tuple[float, Tuple[float, float, float, float]]] = []
    for e in elements:
        role = (e.get("role") or "").lower()
        if role in ("popover", "sheet", "alert"):
            fr = e.get("frame") or {}
            x = fr.get("x")
            y = fr.get("y")
            w = fr.get("width")
            h = fr.get("height")
            if x is None or y is None or w is None or h is None:
                continue
            area = float(w) * float(h)
            if area > 0:
                candidates.append(
                    (area, (float(x), float(y),
                            float(x) + float(w), float(y) + float(h))))
    if not candidates:
        return None
    # Smallest area = topmost / most-specific popover.
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def _frame_inside(elem: Dict,
                   bounds: Tuple[float, float, float, float]) -> bool:
    fr = elem.get("frame") or {}
    if fr.get("x") is None or fr.get("y") is None:
        return False
    cx = float(fr["x"]) + float(fr.get("width", 0)) / 2.0
    cy = float(fr["y"]) + float(fr.get("height", 0)) / 2.0
    x0, y0, x1, y1 = bounds
    return x0 <= cx <= x1 and y0 <= cy <= y1


def find_show_reader_in_popover(elements: List[Dict]) -> Optional[Dict]:
    """After tapping the page menu, look for the 'Show Reader' row
    INSIDE THE TOPMOST POPOVER. Label varies across iOS revs.

    Scoping to the popover is load-bearing: an unscoped regex match
    can hit any element whose label contains "reader" anywhere in the
    AX tree — including Wikipedia article links that reference "Safari
    Reader" or screen-reader documentation. The 2026-06-01 follow-up
    probe hit exactly this false-positive."""
    bounds = _find_topmost_popover_bounds(elements)
    pattern = re.compile(
        r"\bshow\s+reader\b|^reader$|\breader view\b", re.IGNORECASE)
    for e in elements:
        if bounds is not None and not _frame_inside(e, bounds):
            continue
        lab = (e.get("label") or "").strip()
        if not lab:
            continue
        if pattern.search(lab):
            return e
    return None


def assert_in_reader_mode(elements: List[Dict]) -> bool:
    """After tapping 'Show Reader', verify Reader Mode actually
    engaged. Reader mode adds a distinctive dismiss control:
    'Hide Reader' or 'Reader View Settings' or a 'AA done' button.
    The page itself is reformatted, but checking the chrome control
    is more reliable than text-counting."""
    for e in elements:
        lab = (e.get("label") or "").strip().lower()
        if not lab:
            continue
        if ("hide reader" in lab
                or "reader view settings" in lab
                or "exit reader" in lab):
            return True
    return False


def fmt_sample(labels: List[str], n: int = 3) -> str:
    out = []
    for s in labels[:n]:
        s = s.replace("\n", " ").strip()
        if len(s) > 90:
            s = s[:87] + "..."
        out.append(f'    "{s}"')
    return "\n".join(out)


# ── one stage of the probe ───────────────────────────────────────────


async def snap(reader: XCUITestReader) -> Tuple[List[Dict], Set[str]]:
    """Snapshot Safari's AX tree as raw dicts.

    Uses the private `_send` rather than `reader.observe()` because we
    need the raw response (role strings like "btn"/"text", frame keys
    "width"/"height") — the public `observe()` returns parsed
    AXElement/AXTree objects with a different shape.
    """
    raw = await reader._send({"type": "observe",
                                "bundleId": SAFARI_BUNDLE})
    if not raw.get("ok"):
        raise RuntimeError(f"observe failed: {raw.get('error')}")
    elements = raw.get("elements") or []
    subs = set(substantive_labels(elements))
    return elements, subs


async def scroll_in_scrollable(reader: XCUITestReader,
                                elements: List[Dict],
                                direction: str) -> bool:
    """Find the largest scrollable element and pan it.

    `direction` = "down" / "up" (content direction). Inverts to finger
    direction internally (down = finger UP). Returns True if a
    scrollable region was found and a swipe issued; False if none
    found. 80% amplitude swipe bounded within the element frame.
    """
    bounds = _find_scrollable_frame(elements)
    if bounds is None:
        return False
    x0, y0, x1, y1 = bounds
    cx = (x0 + x1) / 2.0
    h = y1 - y0
    if direction == "down":
        y_start, y_end = y0 + h * 0.85, y0 + h * 0.15
    elif direction == "up":
        y_start, y_end = y0 + h * 0.15, y0 + h * 0.85
    else:
        return False
    await reader.swipe_at(cx, y_start, cx, y_end,
                           duration_s=0.05,
                           velocity_pps=1500.0)
    await asyncio.sleep(GESTURE_SETTLE_S)
    return True


def _find_scrollable_frame(
        elements: List[Dict]) -> Optional[Tuple[float, float, float, float]]:
    """Find the largest scrollable element's frame. Returns
    (x, y, x+width, y+height) or None.

    iOS Safari exposes the WKWebView under role `web`. Other apps use
    `scroll`, `table`, or `collection`. The same selection logic works
    for all of them — pick the largest by area. This is the per-app
    independence the 2026-06-03 refactor of SCROLL is built on: we
    SCROLL inside the largest scrollable region rather than guessing
    coordinates that happen to work for one Safari chrome layout.
    """
    candidates: List[Tuple[float, Tuple[float, float, float, float]]] = []
    for e in elements:
        role = (e.get("role") or "").lower()
        if role not in ("web", "scroll", "table", "collection"):
            continue
        fr = e.get("frame") or {}
        x = fr.get("x")
        y = fr.get("y")
        w = fr.get("width")
        h = fr.get("height")
        if x is None or y is None or w is None or h is None:
            continue
        area = float(w) * float(h)
        if area <= 0:
            continue
        candidates.append(
            (area, (float(x), float(y),
                    float(x) + float(w), float(y) + float(h))))
    if not candidates:
        return None
    candidates.sort(key=lambda t: -t[0])  # largest first
    return candidates[0][1]


async def run_one_url(reader: XCUITestReader,
                       url: str, label: str) -> None:
    print(f"\n══════════════════════════════════════════════════════════════")
    print(f"  {label}")
    print(f"  {url}")
    print(f"══════════════════════════════════════════════════════════════")

    # Cold-open Safari at the URL with a clean tab state.
    #
    # 2026-06-03 finding: leftover tabs from a prior probe run made
    # the next run's `simctl openurl` land in Safari's TAB SWITCHER
    # view (visible by tile buttons "<page-title> - Wikipedia" and
    # "Private, Tab Group" chrome). Our scroll gestures then panned
    # the tab carousel, not the article — every snapshot returned
    # IDENTICAL element counts. Wiping SafariTabs.db + BrowserState.db
    # forces a fresh Start Page so openurl lands in a single-tab view.
    subprocess.run(
        ["xcrun", "simctl", "terminate", UDID, SAFARI_BUNDLE],
        capture_output=True, timeout=5)
    await asyncio.sleep(0.6)
    _safari_clear_tab_state(UDID)
    await asyncio.sleep(0.3)
    subprocess.run(
        ["xcrun", "simctl", "openurl", UDID, url],
        capture_output=True, timeout=5)
    await asyncio.sleep(LOAD_SETTLE_S)

    # One-time orientation dump: what's actually in the AX tree?
    try:
        elements0, _ = await snap(reader)
    except Exception as e:
        print(f"!! orientation snap failed: {e!r}")
        return
    print(f"\n── ORIENTATION (baseline element sample) ────────────────")
    print(f"  total elements: {len(elements0)}")
    roles0 = all_role_count(elements0)
    print(f"  role distribution: {dict(sorted(roles0.items(), key=lambda t: -t[1]))}")
    # Sample first 12 elements with non-empty labels
    print("  sample labeled elements (first 12 below URL bar):")
    url_bar_y_0 = find_url_bar_y(elements0) or (H * 0.10)
    shown = 0
    for e in elements0:
        if shown >= 12:
            break
        lab = (e.get("label") or "").strip()
        if not lab:
            continue
        fr = e.get("frame") or {}
        y = fr.get("y", 0)
        if y < url_bar_y_0:
            continue
        role = e.get("role") or "?"
        ident = (e.get("identifier") or "")[:20]
        short = lab[:80].replace("\n", " ")
        print(f"    [{role:11s}] y={y:>5.1f}  ident={ident!r:22s}  lab={short!r}")
        shown += 1

    # ── Phase A: viewport-only baseline + scroll walk ────────────────
    cumulative: Set[str] = set()
    # Track per-step on-screen substantive labels so we can do the
    # retention test (does step-1's label set still appear after the
    # agent scrolls past it?).
    per_step_subs: List[Set[str]] = []
    truncated_walk = False
    steps_completed = 0
    for step in range(5):  # baseline + 4 scrolls
        try:
            elements, subs = await snap(reader)
        except Exception as e:
            print(f"\n!! snap at step {step} FAILED: {e!r}")
            truncated_walk = True
            break
        per_step_subs.append(subs)
        new = subs - cumulative
        cumulative |= subs
        steps_completed = step + 1
        n_total = len(elements)
        # Swift server emits role "text" (XCUIElementTypeStaticText).
        roles = all_role_count(elements)
        n_text = roles.get("text", 0)

        stage = "baseline" if step == 0 else f"after scroll #{step}"
        print(f"\n── {stage} ──────────────────────────────────────────────")
        print(f"  elements total      : {n_total}")
        # Top 5 roles by count
        top_roles = sorted(roles.items(), key=lambda t: -t[1])[:5]
        print(f"  top roles           : {top_roles}")
        print(f"  text-role total     : {n_text}")
        long_roles = role_dist_for_long_labels(elements)
        print(f"  long-label roles    : {dict(long_roles)}")
        print(f"  substantive (>={SUBSTANTIVE_MIN_CHARS}c) on screen : {len(subs)}")
        print(f"  substantive cumulative : {len(cumulative)}")
        print(f"  NEW substantive this step : {len(new)}")
        if new:
            print("  sample new labels:")
            print(fmt_sample(sorted(new)))

        if step < 4:
            try:
                ok = await scroll_in_scrollable(reader, elements, "down")
                if not ok:
                    print(f"  scroll #{step+1} FAILED: no scrollable element "
                          "found in AX tree (no role:web/scroll/table/"
                          "collection with non-zero frame)")
                    truncated_walk = True
                    break
            except Exception as e:
                print(f"  scroll #{step+1} FAILED: {e!r}")
                truncated_walk = True
                break

    if truncated_walk:
        print(f"  ⚠ Phase A walk truncated at step {steps_completed} "
              "— cumulative count understates true viewport coverage.")

    # ── Phase A retention probe: scroll back UP and check whether the
    # baseline's substantive labels reappear in the tree. Answers:
    # is the AX tree a sliding window (labels evicted when scrolled
    # past), or is it cumulative (labels persist)?
    if len(per_step_subs) >= 2 and not truncated_walk:
        print(f"\n── RETENTION PROBE (scroll back up to baseline) ──────────")
        try:
            elements_pre, _ = await snap(reader)
            # Scroll up the same number of times we scrolled down.
            for i in range(4):
                ok = await scroll_in_scrollable(reader, elements_pre, "up")
                if not ok:
                    break
                # Snapshot for the next scroll so we re-find the
                # scrollable element (its frame may shift as the URL bar
                # collapses/expands).
                elements_pre, _ = await snap(reader)
            elements_after, subs_after = await snap(reader)
        except Exception as e:
            print(f"  retention probe FAILED: {e!r}")
        else:
            baseline_set = per_step_subs[0]
            retained = baseline_set & subs_after
            lost = baseline_set - subs_after
            print(f"  baseline substantive count : {len(baseline_set)}")
            print(f"  after scroll-back  count   : {len(subs_after)}")
            print(f"  baseline labels retained   : {len(retained)} / {len(baseline_set)}")
            print(f"  baseline labels lost       : {len(lost)}")
            if lost:
                print("  sample lost labels (would have been evicted):")
                print(fmt_sample(sorted(lost)))
            if len(baseline_set) > 0:
                ratio = len(retained) / len(baseline_set)
                if ratio >= 0.7:
                    verdict = "HIGH retention — AX tree behaves cumulatively (or scroll-back restores viewport)"
                elif ratio >= 0.3:
                    verdict = "PARTIAL retention — mixed behavior"
                else:
                    verdict = "LOW retention — labels appear to be evicted (sliding-window model)"
                print(f"  → {verdict}")

    # iOS 26 Safari auto-collapses the bottom URL bar after scrolling
    # down. Wake it with a small upward swipe in the middle of the
    # screen (gentle — not enough to scroll content meaningfully).
    print("\n── waking URL bar + searching for Reader Mode toggle ─────")
    await reader.swipe_at(float(W // 2), float(H * 0.50),
                           float(W // 2), float(H * 0.65),
                           duration_s=0.20,
                           velocity_pps=200.0)
    await asyncio.sleep(1.0)
    try:
        elements, _ = await snap(reader)
    except Exception as e:
        print(f"  snap failed: {e!r}")
        return

    toggle = find_reader_toggle(elements)
    if toggle is None:
        print("  NO toggle candidate found via label/identifier match.")
        print("  Dumping ALL buttons + the URL field for discovery:")
        for e in elements:
            # Swift server roles: btn / input / search (NOT "Button" etc).
            if (e.get("role") or "") not in ("btn", "input", "search"):
                continue
            fr = e.get("frame") or {}
            y = fr.get("y", -1)
            x = fr.get("x", -1)
            lab = (e.get("label") or "")[:50]
            ident = (e.get("identifier") or "")[:30]
            role = e.get("role") or ""
            print(f"    [{role:6s}] @({x:>5.1f},{y:>5.1f})  "
                  f"label={lab!r:52s} ident={ident!r}")
        print("  Skipping Reader Mode phase for this URL.")
        return

    fr = toggle.get("frame") or {}
    # Swift server emits "width"/"height", not "w"/"h" — without this fix
    # tap centers collapsed to the element's top-left corner.
    tx = float(fr.get("x", 0)) + float(fr.get("width", 0)) / 2.0
    ty = float(fr.get("y", 0)) + float(fr.get("height", 0)) / 2.0
    print(f"  toggle candidate: label={(toggle.get('label') or '')!r:32s} "
          f"@({tx:.0f},{ty:.0f})")

    await reader.tap(tx, ty)
    await asyncio.sleep(READER_TOGGLE_S)

    elements, _ = await snap(reader)
    show_reader = find_show_reader_in_popover(elements)
    if show_reader is None:
        print("  page-menu popover opened but NO 'Show Reader' row "
              "found inside the topmost popover.")
        print("  Dumping popover rows for discovery (popover-scoped):")
        bounds = _find_topmost_popover_bounds(elements)
        if bounds is None:
            print("    (no popover container found — tap may have missed)")
        for e in elements[:60]:
            if bounds is not None and not _frame_inside(e, bounds):
                continue
            lab = (e.get("label") or "").strip()
            if lab and len(lab) < 80:
                role = e.get("role") or ""
                print(f"    [{role:11s}] {lab!r}")
        return

    fr = show_reader.get("frame") or {}
    sx = float(fr.get("x", 0)) + float(fr.get("width", 0)) / 2.0
    sy = float(fr.get("y", 0)) + float(fr.get("height", 0)) / 2.0
    print(f"  tapping Show Reader at ({sx:.0f},{sy:.0f})")
    await reader.tap(sx, sy)
    await asyncio.sleep(READER_TOGGLE_S * 1.5)

    # Verify Reader Mode actually engaged before measuring Phase B.
    elements_after, _ = await snap(reader)
    if not assert_in_reader_mode(elements_after):
        print("  READER MODE NOT ENGAGED — no 'Hide Reader' / 'Reader View "
              "Settings' control found after tap. Skipping Phase B.")
        return
    print("  ✓ Reader Mode engaged (found dismiss control).")

    # ── Phase B: Reader Mode baseline + scroll walk ──────────────────
    reader_cumulative: Set[str] = set()
    for step in range(4):
        try:
            elements, subs = await snap(reader)
        except Exception as e:
            print(f"\n!! reader-snap at step {step} FAILED: {e!r}")
            return
        new = subs - reader_cumulative
        reader_cumulative |= subs
        # Swift server emits role "text", not "StaticText".
        n_text = all_role_count(elements).get("text", 0)

        stage = "READER baseline" if step == 0 else f"READER after scroll #{step}"
        print(f"\n── {stage} ────────────────────────────────────────")
        print(f"  text-role total : {n_text}")
        print(f"  substantive on screen : {len(subs)}")
        print(f"  substantive cumulative : {len(reader_cumulative)}")
        print(f"  NEW substantive this step : {len(new)}")
        if new:
            print("  sample new labels:")
            print(fmt_sample(sorted(new)))

        if step < 3:
            try:
                ok = await scroll_in_scrollable(reader, elements, "down")
                if not ok:
                    print(f"  scroll #{step+1} FAILED: no scrollable element "
                          "in Reader Mode AX tree")
                    break
            except Exception as e:
                print(f"  scroll #{step+1} FAILED: {e!r}")
                break

    # ── Summary for this URL ─────────────────────────────────────────
    print(f"\n── SUMMARY: {label} ────────────────────────────────────")
    print(f"  Normal-mode cumulative substantive labels : {len(cumulative)}")
    print(f"  Reader-mode cumulative substantive labels : {len(reader_cumulative)}")
    overlap = cumulative & reader_cumulative
    print(f"  overlap                                   : {len(overlap)}")
    print(f"  reader-only                               : "
          f"{len(reader_cumulative - cumulative)}")
    print(f"  normal-only                               : "
          f"{len(cumulative - reader_cumulative)}")


# ── main ─────────────────────────────────────────────────────────────


async def main() -> None:
    reader = XCUITestReader(UDID, bundle_id=SAFARI_BUNDLE)
    await reader.start()
    await asyncio.sleep(1.0)
    try:
        for url, label in PROBES:
            try:
                await run_one_url(reader, url, label)
            except Exception as e:  # pragma: no cover — diagnostic probe
                print(f"\n!! {label} FAILED: {e!r}")
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
