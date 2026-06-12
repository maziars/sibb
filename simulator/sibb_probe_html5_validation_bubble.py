"""Probe (#245): is the iOS Safari HTML5 validation tooltip
("Please fill out this field") accessible to XCUITest?

Background
==========
During manual replay (2026-06-07) we observed that tapping the
form's Submit button with empty required fields produced an AX
element labeled `dismiss popup` (role=`el`) at @e0111 with no
text content. iOS Safari is rendering the standard HTML5
validation balloon ("Please fill out this field"), but its
message text was nowhere in the AX tree we could read.

iOS Safari announces the validation message to VoiceOver users.
The likely mechanism is `UIAccessibility.post(notification:
.announcement, argument: ...)` — accessibility ANNOUNCEMENTS are
transient runtime events delivered to AT clients, NOT stored in
the static AX tree. XCUITest's `app.snapshot()` reads the static
tree; it has no observation hook for announcements.

This probe systematizes that observation:
1. Loads the form, focuses an input, leaves all fields empty.
2. Taps the real Submit button.
3. Immediately (1s, 2s, 4s post-tap) captures the FULL raw AX
   snapshot AND the scaffold-filtered tree (what the agent sees).
4. For each capture, dumps every element with role/label/value/
   frame so we can search for the validation message text by hand.
5. Also screenshots each state so visual confirmation matches.

If the bubble's text appears NOWHERE in any capture, we have
concrete evidence: the message is invisible to XCUITest and the
agent cannot read it. We then document the "fallback in-AX alert"
that `submit_form` already emits server-side (the `role="alert"`
div on the response page) as the agent's only signal.

Usage
=====
    python3 sibb/simulator/sibb_probe_html5_validation_bubble.py <UDID>

Output
======
    /tmp/sibb_bubble_*.png    annotated screenshots
    Console: per-capture element listing
    Console verdict: text-found-in-AX vs not
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))


def _shot(udid, slug):
    out = f"/tmp/sibb_bubble_{slug}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", out],
        check=True, capture_output=True)
    return out


# Words / fragments that would appear in the validation balloon
# across iOS English locales. If we don't find any of these in the
# AX tree, the message is genuinely invisible to XCUITest.
_VALIDATION_NEEDLES = (
    "please fill", "fill out", "required",
    "fill in this field", "this field", "missing",
)


def _scan(snap, where) -> list:
    """Return a list of (role, label, value) tuples for every
    element in `snap` that contains a validation-message needle."""
    hits = []
    for el in snap.elements:
        label = (el.label or "")
        value = getattr(el, "value", "") or ""
        lower = (label + " " + str(value)).lower()
        for needle in _VALIDATION_NEEDLES:
            if needle in lower:
                hits.append((where, el.role, el.label, value))
                break
    return hits


def _dump(snap, where, limit=80):
    print(f"\n── {where}: {len(snap.elements)} elements ──")
    for i, el in enumerate(snap.elements):
        if i >= limit:
            print(f"  ... ({len(snap.elements) - limit} more elided)")
            break
        fr = el.frame
        loc = (f"@({fr.x:.0f},{fr.y:.0f},"
               f"{fr.width:.0f}x{fr.height:.0f})" if fr else "")
        focused = " (FOCUSED)" if getattr(el, "focused", False) else ""
        val = getattr(el, "value", "") or ""
        val_str = f" = {val!r}" if val else ""
        print(f"  [{el.role}] {el.label!r}{val_str}{loc}{focused}")


async def _capture(reader, ax_reader, udid, slug, note):
    raw = await reader.observe()
    filt = await ax_reader._read_xcuitest()
    print(f"\n══════════════════════════════════════════════════════")
    print(f"  {slug.upper()}  {note}")
    print(f"══════════════════════════════════════════════════════")
    _dump(raw, "raw (Swift envelope)")
    _dump(filt, "filtered (agent view)")
    _shot(udid, slug)
    return raw, filt


async def main(udid: str) -> int:
    import harness_pages  # noqa: F401
    from harness_layout import PAGE_REGISTRY
    from sibb_mock_site import MockSite, open_in_safari
    from sibb_xcuitest_client import XCUITestReader

    # seed=1 layout — the same page where the user's manual replay
    # observed the bare "dismiss popup" element.
    site = MockSite(
        site_id="bubble-probe",
        static_pages={"/event": PAGE_REGISTRY["rsvp_event"]},
    )
    site.page_seed = 506456970
    site.start()
    print(f"[probe] MockSite: {site.base_url}/event")

    import sibb_dns_resolver
    sibb_dns_resolver.start_if_needed()
    if sibb_dns_resolver.resolver_is_installed():
        url = f"http://rsvp.test:{site.port}/event"
    else:
        url = f"{site.base_url}/event"

    # Sanity: pull HTML, confirm font-size=13 (zoom triggers) — so
    # the post-tap state mirrors the user's manual-replay observation.
    html = urllib.request.urlopen(
        f"{site.base_url}/event", timeout=3).read().decode()
    assert "font-size:13px" in html or "font-size:14px" in html \
        or "font-size:15px" in html, \
        "expected a zoom-triggering font-size in head"
    print("  [verify] zoom-triggering font-size present")

    open_in_safari(udid, url)
    await asyncio.sleep(2.0)

    reader = XCUITestReader(udid, bundle_id="com.apple.mobilesafari")
    await reader.start()

    from sibb_scaffold import AXReader
    ax_reader = AXReader(udid)
    ax_reader._xcuitest = reader

    # Phase 0: baseline (no focus, no popup)
    raw0, filt0 = await _capture(
        reader, ax_reader, udid, "0_baseline", "fresh load, no focus")

    # Phase 1: focus an input (triggers zoom + raises kb) — DO NOT
    # type anything; all fields stay empty.
    inp = next(
        (e for e in raw0.elements if e.role == "input"
         and (e.label or "").lower() not in ("address",)),
        None)
    if inp is None:
        print("[probe] no input found; abort")
        site.stop()
        return 2
    print(f"\n[probe] focusing input '{inp.label}' "
          f"@({inp.frame.center_x:.0f},{inp.frame.center_y:.0f})")
    await reader.tap(x=inp.frame.center_x, y=inp.frame.center_y)
    await asyncio.sleep(1.2)
    raw1, filt1 = await _capture(
        reader, ax_reader, udid, "1_focused_empty",
        "input focused, all fields empty")

    # Phase 2: find Submit button, tap it. Expect the HTML5
    # validation popup to appear.
    submit = next(
        (e for e in raw1.elements if e.role == "btn"
         and "submit" in (e.label or "").lower()),
        None)
    if submit is None:
        print("[probe] no Submit button found; abort")
        site.stop()
        return 3
    print(f"\n[probe] tapping Submit '{submit.label}' "
          f"@({submit.frame.center_x:.0f},"
          f"{submit.frame.center_y:.0f})")
    await reader.tap(x=submit.frame.center_x, y=submit.frame.center_y)

    # Capture at 3 time offsets — the popup may need a beat to render
    # AND may be transient (UIAccessibility announcements can vanish
    # quickly).
    await asyncio.sleep(0.5)
    raw2a, filt2a = await _capture(
        reader, ax_reader, udid, "2a_post_submit_500ms",
        "0.5 s after Submit-tap")

    await asyncio.sleep(0.7)
    raw2b, filt2b = await _capture(
        reader, ax_reader, udid, "2b_post_submit_1200ms",
        "1.2 s after Submit-tap")

    await asyncio.sleep(2.0)
    raw2c, filt2c = await _capture(
        reader, ax_reader, udid, "2c_post_submit_3200ms",
        "3.2 s after Submit-tap (popup may have auto-dismissed)")

    # ── Verdict ───────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  VERDICT")
    print("═" * 70)
    all_captures = [
        ("baseline", raw0, filt0),
        ("focused", raw1, filt1),
        ("post-submit 500ms", raw2a, filt2a),
        ("post-submit 1200ms", raw2b, filt2b),
        ("post-submit 3200ms", raw2c, filt2c),
    ]
    text_hits = []
    popup_seen_in = []
    for tag, raw, filt in all_captures:
        text_hits.extend(_scan(raw, f"{tag} (raw)"))
        text_hits.extend(_scan(filt, f"{tag} (filtered)"))
        for el in raw.elements:
            if "popup" in (el.label or "").lower() or \
                    "dismiss" in (el.label or "").lower():
                popup_seen_in.append((tag, el.role, el.label))
    print(f"\n  Validation-message text matches "
          f"(any of {_VALIDATION_NEEDLES!r}):")
    if text_hits:
        for where, role, label, val in text_hits:
            print(f"    [{where}] role={role} label={label!r} "
                  f"value={val!r}")
    else:
        print("    NONE — message text does NOT appear in any "
              "AX snapshot.")
    print(f"\n  'popup' / 'dismiss' AX elements observed:")
    for tag, role, label in popup_seen_in:
        print(f"    [{tag}] role={role} label={label!r}")
    if not popup_seen_in:
        print("    NONE — no popup-related AX element either. "
              "Either the popup didn't fire (form validation may "
              "have been suppressed) or its AX hook lives elsewhere.")

    print()
    if not text_hits and popup_seen_in:
        print("  CONFIRMED: HTML5 validation popup IS present in the")
        print("  AX tree (visible as a 'dismiss popup' element) but")
        print("  its MESSAGE TEXT is not — iOS exposes the message")
        print("  via accessibility ANNOUNCEMENT (transient runtime")
        print("  event) which XCUITest's static snapshot cannot read.")
        print("  Agent can detect 'something popped up' but not")
        print("  'Field X is empty'.")
    elif text_hits:
        print("  SURPRISING: validation message text was found in")
        print("  the AX tree. Re-examine — may have been picked up")
        print("  by a generic label / placeholder match rather than")
        print("  the real validation tooltip.")
    else:
        print("  INCONCLUSIVE: neither popup nor message-text seen.")
        print("  Form may not have triggered HTML5 validation on this")
        print("  Submit tap; check screenshot /tmp/sibb_bubble_2*.png")
        print("  to see what actually happened on screen.")

    site.stop()
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_html5_validation_bubble.py <UDID>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
