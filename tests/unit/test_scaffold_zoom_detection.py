"""L1 tests for the Safari auto-zoom detection + chrome derivation
pipeline added 2026-06-05 / hardened 2026-06-06 / simplified 2026-06-07.

Covers (as of Step 4):
  * Per-frame stateless zoom detection: Swift `zoom_scale` (when
    populated) or `kb_above_screen` heuristic. No latch, no hysteresis.
  * Accessory bar plumbed onto tree.accessory_bar_frame as diagnostic-
    only (Step 3 dropped the kb_y_top union; bar elements survive
    the visibility filter so the agent can tap Done/Next/Previous).
  * Runtime chrome bounds: derived from AX, not hardcoded.
  * Orientation derivation (portrait vs landscape) from screen dims.
  * Landscape sanity: form-field y-coords above what the OLD fixed-
    threshold filter would have called "chrome".
  * AXReader has no cross-snapshot state — invariant pinned by an
    allowlist test.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "fakes"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "benchmark"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "simulator"))

from fake_reader import FakeXCUITestReader  # noqa: E402
from sibb_scaffold import (  # noqa: E402
    AXReader, _derive_chrome_bounds,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── helpers ──────────────────────────────────


def _read(reader, fake, elements, **observe_kwargs):
    fake.set_observe_response(elements, **observe_kwargs)
    return asyncio.run(reader._read_xcuitest())


def _frame(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def _form_element(label, y, w=155, h=21, focused=False, role="input",
                   value=None):
    return {
        "ref": f"raw_{label[:6]}",
        "role": role,
        "label": label,
        "value": value,
        "frame": _frame(8, y, w, h),
        "enabled": True,
        "focused": focused,
        "adjustable": False,
    }


# ────────────────────── _derive_chrome_bounds ─────────────────────────


def test_derive_chrome_defaults_when_no_signal():
    """No AX elements → safe defaults (50 / screen_h-100)."""
    top, bot = _derive_chrome_bounds([], None, 402, 874)
    assert top == 50.0
    assert bot == 774.0


def test_derive_chrome_bottom_pulled_in_by_url_bar():
    """An element labeled 'Address' at y=600 is the URL bar; bottom
    chrome should shrink to start there (minus 5px slack)."""
    class _E:
        def __init__(self, label, y, h=22, w=69):
            self.label = label
            self.frame = type("F", (), {"x": 100, "y": y, "width": w,
                                          "height": h})()

    url_bar = _E("Address", 600, h=22)
    _top, bot = _derive_chrome_bounds([url_bar], None, 402, 874)
    assert bot <= 595.0   # ≤ 600 - 5px slack
    assert bot >= 500.0   # not pulled in absurdly far


def test_derive_chrome_top_pulled_down_by_status_bar():
    """A small text element near y=0 ("12:00 PM") pulls the top
    chrome boundary down to cover it."""
    class _E:
        def __init__(self, label, y, h=22, w=80):
            self.label = label
            self.frame = type("F", (), {"x": 100, "y": y, "width": w,
                                          "height": h})()

    time_lbl = _E("10:22 PM", 32, h=18)
    top, _bot = _derive_chrome_bounds([time_lbl], None, 402, 874)
    assert top >= 55.0   # 32 + 18 + 5 = 55
    assert top < 100.0


def test_derive_chrome_landscape_does_not_invert():
    """Landscape iPhone 16 has screen_h=402. With no chrome signals,
    the defaults (50 / screen_h-100 = 302) must not invert or collapse
    to where they sweep mid-screen form content."""
    top, bot = _derive_chrome_bounds([], None, 874, 402)
    assert top < bot
    # The middle must remain usable — at least 50% of the screen
    # should not be in either chrome region.
    middle_size = bot - top
    assert middle_size >= screen_h_for_calc(402) * 0.4

def screen_h_for_calc(h):
    return h


# ─────────────────────── zoom detection paths ─────────────────────────


def _safari_form_elements_unzoomed():
    """Synthetic AX matching the probe data we captured pre-zoom
    on iPhone 16 (portrait, screen 402x874). All elements fit within
    screen bounds and represent a happy-path Safari RSVP form."""
    return [
        _form_element("Email for confirmation", y=494),
        _form_element("Attending — yes or no", y=530),
        _form_element("Guest name", y=568),
        {"ref": "raw_btn", "role": "btn", "label": "Send RSVP",
         "frame": _frame(8, 594, 83, 21), "enabled": True,
         "focused": False, "adjustable": False},
        # iOS chrome — URL bar
        {"ref": "raw_url", "role": "input", "label": "Address",
         "value": "rsvp.test",
         "frame": _frame(167, 805, 69, 22), "enabled": True,
         "focused": False, "adjustable": False},
    ]


def _safari_form_elements_overflow_viewport():
    """Synthetic AX captured from probe data post-focus on iPhone 16
    portrait. Form CONTAINER reports 563 wide on a 402-wide screen.
    Used to verify that the visibility filter drops oversized
    containers while keeping the inner inputs that DO fit.

    Renamed 2026-06-07 (Step 4): previously called
    `_safari_form_elements_zoomed` on the (now-disproved) theory that
    AX coords for web-content were in zoomed-doc space. The
    `sibb_probe_pinch_recovery.py` overlay proved AX frames ARE real
    screen coords even when auto-zoomed — this fixture is just an
    overflow-container Safari snapshot, not a zoom-state snapshot."""
    return [
        # Form container — overflow signal.
        {"ref": "raw_container", "role": "other",
         "label": "RSVP form, form",
         "frame": _frame(0, 401, 563, 194),
         "enabled": True, "focused": False, "adjustable": False},
        _form_element("Email for confirmation", y=418, w=226, h=32,
                       focused=True, value="riley@example.com"),
        _form_element("Confirming attendance", y=472, w=227, h=32),
        _form_element("Guest name", y=526, w=227, h=31),
        # Submit button — small enough to fit screen, but coord-system
        # zoomed.
        {"ref": "raw_btn", "role": "btn", "label": "Send RSVP",
         "frame": _frame(0, 564, 122, 31), "enabled": True,
         "focused": False, "adjustable": False},
        # URL bar — still in screen coords.
        {"ref": "raw_url", "role": "input", "label": "Address",
         "value": "rsvp.test",
         "frame": _frame(175, 782, 51, 16), "enabled": True,
         "focused": False, "adjustable": False},
    ]


def _new_reader():
    fake = FakeXCUITestReader()
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return reader, fake


def test_zoom_signal_swift_zoom_scale_authoritative():
    """When Swift reports zoom_scale > 1.0, that's the source — no
    need for heuristics."""
    reader, fake = _new_reader()
    tree = _read(reader, fake, _safari_form_elements_unzoomed(),
                  zoom_scale=1.5)
    assert tree.coord_system_zoomed is True
    assert tree.zoom_source == "swift"
    assert tree.zoom_factor == pytest.approx(1.5)


def test_zoom_signal_swift_zoom_scale_normal_means_unzoomed():
    """zoom_scale exactly 1.0 → no zoom, period."""
    reader, fake = _new_reader()
    tree = _read(reader, fake, _safari_form_elements_unzoomed(),
                  zoom_scale=1.0)
    assert tree.coord_system_zoomed is False
    assert tree.zoom_factor == pytest.approx(1.0)


def test_zoom_signal_kb_above_screen_heuristic():
    """kb_top > screen_h is the symptom-of-symptom signal — the kb
    frame got reported in zoomed-doc coords. Use it as last-resort
    detection."""
    reader, fake = _new_reader()
    elements = _safari_form_elements_unzoomed()
    tree = _read(reader, fake, elements,
                  keyboard_visible=True,
                  keyboard_frame=_frame(0, 891, 402, 282))
    assert tree.coord_system_zoomed is True
    assert tree.zoom_source == "kb_above_screen"


def test_zoom_signal_stateless_per_frame():
    """Step 4 (2026-06-07): zoom detection is per-frame, no latch.
    A snapshot with no signal returns coord_system_zoomed=False even
    if the prior snapshot was zoomed (previous design latched for 2
    frames). Empirical probe showed signals are stable so the latch
    was solving a problem that didn't exist."""
    reader, fake = _new_reader()
    # Frame 1: zoom on (Swift signal).
    tree1 = _read(reader, fake, _safari_form_elements_unzoomed(),
                   zoom_scale=1.5)
    assert tree1.coord_system_zoomed is True
    # Frame 2: signal gone → immediately False, no hysteresis.
    tree2 = _read(reader, fake, _safari_form_elements_unzoomed())
    assert tree2.coord_system_zoomed is False
    # Frame 3: still gone → still False.
    tree3 = _read(reader, fake, _safari_form_elements_unzoomed())
    assert tree3.coord_system_zoomed is False


# ─────────────────────── filter behavior ──────────────────────────────


def test_safari_form_keeps_submit_button_visible():
    """The submit button at center (61, 580) fits on a 402-wide
    screen — must NOT be filtered. Validates the visibility filter
    doesn't over-aggressively drop form elements."""
    reader, fake = _new_reader()
    tree = _read(reader, fake, _safari_form_elements_overflow_viewport())
    labels = [e.label for e in tree.elements if e.label]
    assert "Send RSVP" in labels, (
        f"Send RSVP must NOT be filtered; got: {labels}")


def test_safari_form_keeps_url_bar():
    reader, fake = _new_reader()
    tree = _read(reader, fake, _safari_form_elements_overflow_viewport())
    labels = [e.label for e in tree.elements if e.label]
    assert "Address" in labels


def test_safari_form_keeps_focused_field():
    reader, fake = _new_reader()
    tree = _read(reader, fake, _safari_form_elements_overflow_viewport())
    focused = [e for e in tree.elements if e.focused]
    assert len(focused) >= 1
    assert "Email for confirmation" in (focused[0].label or "")


def test_viewport_filter_drops_oversized_container():
    """A form-container element with width=563 on a 402-wide screen
    doesn't fit — the `_is_fully_visible` filter must drop it."""
    reader, fake = _new_reader()
    tree = _read(reader, fake, _safari_form_elements_overflow_viewport())
    labels = [e.label for e in tree.elements if e.label]
    assert "RSVP form, form" not in labels, (
        f"form container is 563-wide on a 402 screen — should be "
        f"filtered by viewport check. Got: {labels}")


# ─────────────────────── orientation derivation ───────────────────────


def test_orientation_portrait_default():
    reader, fake = _new_reader()
    tree = _read(reader, fake, _safari_form_elements_unzoomed())
    assert tree.orientation == "portrait"


def test_orientation_landscape_when_wide():
    reader, fake = _new_reader()
    tree = _read(reader, fake, [], screen_width=874, screen_height=402)
    assert tree.orientation == "landscape"


# ─────────────────────── keyboard_y_min plumbing ──────────────────────
#
# Semantics (2026-06-06): keyboard_y_min is now just kb_frame.y. The
# previous design unioned it with `accessory_bar_frame.y` so the
# occlusion filter rejected the bar's elements (Done/Next/Previous/
# Typing Predictions). Empirical probe
# (`sibb_probe_autozoom_lifecycle.py`) proved those elements are
# first-class labeled buttons with descriptive labels — the agent
# should SEE and USE them. The accessory_bar_frame field is still
# kept on the tree for diagnostic logging, just not for occlusion.


def test_keyboard_y_min_equals_kb_frame_y():
    """keyboard_y_min is just kb_frame.y, unionless.

    Even when an accessory bar IS reported above the kb top, we no
    longer hide its elements. The bar is useful UI."""
    reader, fake = _new_reader()
    tree = _read(
        reader, fake, _safari_form_elements_unzoomed(),
        keyboard_visible=True,
        keyboard_frame=_frame(0, 660, 402, 280),
        accessory_bar_frame=_frame(0, 600, 402, 50),
    )
    # Used to be 600.0 (accessory top); now bare kb top.
    assert tree.keyboard_y_min == pytest.approx(660.0)


def test_keyboard_y_min_falls_back_to_kb_y_when_no_accessory():
    """Unchanged behavior — no accessory means kb_frame.y wins."""
    reader, fake = _new_reader()
    tree = _read(
        reader, fake, _safari_form_elements_unzoomed(),
        keyboard_visible=True,
        keyboard_frame=_frame(0, 660, 402, 280),
    )
    assert tree.keyboard_y_min == pytest.approx(660.0)


def test_accessory_bar_frame_kept_for_diagnostics():
    """The Swift `accessory_bar_frame` field is still surfaced on the
    tree (post-2026-06-06: informational only, no longer load-bearing
    for filtering). Useful for JSONL post-hoc analysis."""
    reader, fake = _new_reader()
    tree = _read(
        reader, fake, _safari_form_elements_unzoomed(),
        keyboard_visible=True,
        keyboard_frame=_frame(0, 660, 402, 280),
        accessory_bar_frame=_frame(0, 600, 402, 50),
    )
    assert tree.accessory_bar_frame == {"x": 0, "y": 600,
                                          "width": 402, "height": 50}


def test_accessory_bar_elements_survive_visibility_filter():
    """The load-bearing payoff for the design change: with kb at y=660
    and a Done button at y=600 (height 38, bottom 638 < kb 661), the
    element survives. Pre-fix it would have been dropped (kb_y_min
    = min(660, 600) = 600 → bottom 638 > 600 → filtered).

    Probe `sibb_probe_autozoom_lifecycle.py` showed iOS Safari emits
    `Previous` / `Next` / `Done` as fully-labeled [btn] elements;
    hiding them was a bug."""
    done_button = {
        "ref": "raw_done", "role": "btn", "label": "Done",
        "frame": _frame(341, 600, 40, 38),
        "enabled": True, "focused": False, "adjustable": False,
    }
    reader, fake = _new_reader()
    tree = _read(
        reader, fake, [done_button],
        keyboard_visible=True,
        keyboard_frame=_frame(0, 660, 402, 280),
        accessory_bar_frame=_frame(0, 600, 402, 50),
    )
    labels = [e.label for e in tree.elements if e.label]
    assert "Done" in labels, (
        f"accessory bar 'Done' must reach the agent; got: {labels}")


def test_bar_survives_while_form_field_below_kb_is_filtered():
    """The load-bearing invariant of Step 3 in a SINGLE fixture: the
    bar's Done button (y=600 bottom=638) survives because its bottom
    is above kb_y_top=660. A form field at y=665 bottom=705 — clearly
    BELOW the kb — gets filtered out. This proves the bare-kb cutoff
    still does its kb-occlusion job while no longer hiding the bar."""
    done_button = {
        "ref": "raw_done", "role": "btn", "label": "Done",
        "frame": _frame(341, 600, 40, 38),
        "enabled": True, "focused": False, "adjustable": False,
    }
    below_kb_field = {
        "ref": "raw_phone", "role": "input", "label": "Phone",
        "frame": _frame(10, 665, 350, 40),
        "enabled": True, "focused": False, "adjustable": False,
    }
    reader, fake = _new_reader()
    tree = _read(
        reader, fake, [done_button, below_kb_field],
        keyboard_visible=True,
        keyboard_frame=_frame(0, 660, 402, 280),
        accessory_bar_frame=_frame(0, 600, 402, 50),
    )
    labels = [e.label for e in tree.elements if e.label]
    assert "Done" in labels, f"bar Done must survive; got: {labels}"
    assert "Phone" not in labels, (
        f"form field below kb must be filtered; got: {labels}")


def test_bar_element_at_exact_kb_top_boundary_survives():
    """Brittle 1px-boundary case (adversarial critic findings): a
    bar element whose `bottom == kb_y_top` exactly. The filter uses
    `frame.y + frame.height > kb_y_top + TOL` with TOL=1, so a
    bar at y=583 h=44 (bottom=583) survives.

    Pin this so a future TOL change doesn't silently drop predictions.
    Real-world: iOS emits predictions at y=539 h=44 → bottom=583;
    matches kb_y_top=583 exactly in the probe data."""
    predictions_strip = {
        "ref": "raw_pred", "role": "other", "label": "Typing Predictions",
        "frame": _frame(0, 539, 402, 44),
        "enabled": True, "focused": False, "adjustable": False,
    }
    reader, fake = _new_reader()
    tree = _read(
        reader, fake, [predictions_strip],
        keyboard_visible=True,
        keyboard_frame=_frame(0, 583, 402, 280),
    )
    labels = [e.label for e in tree.elements if e.label]
    assert "Typing Predictions" in labels, (
        f"prediction strip at exactly kb_y_top must survive (TOL=1); "
        f"got: {labels}")


# ─────────────────────── landscape regression (R6) ────────────────────


def test_landscape_form_fields_not_dropped_as_chrome():
    """Regression: with the old fixed CHROME_BOTTOM_Y = screen_h-100,
    a landscape iPhone 16 (screen_h=402) would compute 302 as the
    cutoff. A form field at y=320 would be classified as "chrome"
    in zoomed state — that's wrong. With runtime-derived bounds it
    must NOT happen unless there's positive evidence of chrome
    (URL bar / kb / toolbar at that y)."""
    elements = [
        # Landscape form field at y=320 — well below the OLD fixed
        # 302 cutoff but legitimately in the page body.
        _form_element("Your name", y=320, h=24, focused=True),
        _form_element("Email", y=350, h=24),
        {"ref": "raw_btn", "role": "btn", "label": "Submit",
         "frame": _frame(8, 380, 100, 21), "enabled": True,
         "focused": False, "adjustable": False},
    ]
    reader, fake = _new_reader()
    # Landscape, no zoom signal. All fields should be visible.
    tree = _read(reader, fake, elements,
                  screen_width=874, screen_height=402)
    labels = {e.label for e in tree.elements if e.label}
    assert tree.orientation == "landscape"
    assert tree.coord_system_zoomed is False
    assert "Your name" in labels
    assert "Email" in labels
    assert "Submit" in labels


# ─────────────────────── header rendering smoke ───────────────────────


def test_header_shows_landscape_tag():
    """Smoke: the assistant's observation header tags landscape so
    the agent knows the layout."""
    from sibb_assistant import fmt_observation
    from sibb_scaffold import AXTokenizer

    reader, fake = _new_reader()
    tree = _read(reader, fake, [], screen_width=874, screen_height=402)
    out = fmt_observation(tree, AXTokenizer(), step=1)
    assert "LANDSCAPE" in out


def test_header_shows_auto_zoom_with_factor():
    """Header shows the detected zoom factor + source."""
    from sibb_assistant import fmt_observation
    from sibb_scaffold import AXTokenizer

    reader, fake = _new_reader()
    tree = _read(reader, fake, _safari_form_elements_unzoomed(),
                  zoom_scale=1.5)
    out = fmt_observation(tree, AXTokenizer(), step=1)
    assert "AUTO-ZOOMED" in out
    assert "1.50x" in out
    assert "(swift)" in out


# ────── no-latch invariant (Step 4, 2026-06-07) ───────────────────────


def test_axreader_has_no_zoom_latch_state():
    """Step 4 dropped the per-reader latch entirely. Confirm AXReader
    doesn't hold latch state that could leak across episodes (the
    original motivation for `reset_episode_state()`)."""
    reader = AXReader("test-udid")
    assert not hasattr(reader, "_zoom_latch_active")
    assert not hasattr(reader, "_zoom_unset_streak")
    assert not hasattr(reader, "reset_episode_state")


def test_axreader_init_state_is_the_allowlisted_set():
    """Step 4 removed cross-snapshot state from AXReader. Pin the
    exact instance-attribute set so any future regression that adds
    a per-reader latch (the very class of bug Step 4 fixed) fails
    loudly — not just literal `_zoom_*` field names but ANY new
    instance field."""
    reader = AXReader("test-udid")
    expected = frozenset({"udid", "_xcuitest",
                            "_using_xctest", "_ref_counter"})
    actual = frozenset(vars(reader).keys())
    assert actual == expected, (
        f"AXReader.__init__ allowlist drift; "
        f"unexpected={actual - expected}, "
        f"missing={expected - actual}")


def test_axreader_ref_counter_monotonic_across_reads():
    """The previously-deleted `test_reset_episode_state_does_not_clobber
    _ref_counter` was the only test pinning ref stability. Step 4
    deleted it along with the method. Restore the underlying invariant:
    refs are session-monotonic; sibb_episode_runner.py reuses one
    AXReader across N tasks and depends on ref stability across the
    multi-task loop."""
    reader, fake = _new_reader()
    # Pass non-empty elements so the counter actually increments.
    elements = _safari_form_elements_unzoomed()
    fake.set_observe_response(elements)
    asyncio.run(reader._read_xcuitest())
    first_counter = reader._ref_counter
    assert first_counter > 0
    fake.set_observe_response(elements)
    asyncio.run(reader._read_xcuitest())
    second_counter = reader._ref_counter
    assert second_counter > first_counter, (
        f"_ref_counter must increase across reads; got "
        f"{first_counter} → {second_counter}")


# ──────────── _derive_chrome_bounds role+geometry gate (task #212) ────


class _MockElement:
    """Light AX element stand-in: supports the label + frame + role
    interface that _derive_chrome_bounds expects."""

    def __init__(self, label, y, h=22, w=80, x=100, role=None):
        self.label = label
        self.frame = type("F", (), {"x": x, "y": y, "width": w,
                                      "height": h})()
        # _derive_chrome_bounds reads `effective_role`.
        self.effective_role = role


def test_chrome_label_rejected_when_role_is_static_text():
    """A 'Done' label rendered as STATIC_TEXT (decorative) must not
    pull the bottom chrome up. Only role-button/text-field/etc count."""
    from sibb_scaffold import ElementRole

    # Plain text element with "Done" sitting at y=600 (bottom half).
    deco = _MockElement("Done", y=600, h=22, role=ElementRole.STATIC_TEXT)
    _top, bot = _derive_chrome_bounds([deco], None, 402, 874)
    assert bot == pytest.approx(774.0), (
        f"text-only 'Done' should not shrink chrome; got bot={bot}")


def test_chrome_label_rejected_when_too_tall():
    """A 'Toolbar' element 300px tall is NOT the keyboard accessory
    bar — it's a full-screen container that happens to share a label.
    Must not pull chrome up."""
    from sibb_scaffold import ElementRole

    tall = _MockElement("Toolbar", y=500, h=300, w=402,
                          role=ElementRole.TOOLBAR)
    _top, bot = _derive_chrome_bounds([tall], None, 402, 874)
    assert bot == pytest.approx(774.0), (
        f"tall element shouldn't be chrome; got bot={bot}")


def test_chrome_label_rejected_when_in_upper_half_no_kb():
    """A 'Done' button at y=120 with no kb known is a nav-bar / sheet
    confirmation, NOT the keyboard's Done. Must not pull chrome up."""
    from sibb_scaffold import ElementRole

    upper = _MockElement("Done", y=120, h=30,
                          role=ElementRole.BUTTON)
    _top, bot = _derive_chrome_bounds([upper], None, 402, 874)
    assert bot == pytest.approx(774.0), (
        f"upper-half 'Done' shouldn't be chrome; got bot={bot}")


def test_chrome_label_rejected_when_at_or_below_kb_top():
    """When kb_y_top is known, a labeled element AT or BELOW kb_y_top
    is irrelevant chrome (it's behind the kb) — must not anchor
    bottom_chrome_top."""
    from sibb_scaffold import ElementRole

    below_kb = _MockElement("Done", y=540, h=30,
                              role=ElementRole.BUTTON)
    # kb_y_top = 500 — the "Done" sits at 540 which is BELOW kb top.
    _top, bot = _derive_chrome_bounds([below_kb], 500.0, 402, 874)
    # bot should come from kb_y_top - 5 = 495, NOT 535 (the bogus Done).
    assert bot == pytest.approx(495.0), (
        f"below-kb 'Done' must not anchor chrome; got bot={bot}")


def test_chrome_label_accepted_when_role_textfield_in_bottom_short():
    """The URL bar ('Address') of TextField role in the bottom half IS
    Safari chrome. Bottom chrome should shrink to its top.

    Updated 2026-06-06: previously this test used 'Done' but Step 3
    pruned `done`/`next`/`previous`/`typing predictions` from
    `_SAFARI_BOTTOM_CHROME_LABELS` (they're agent-visible interactive
    UI, not chrome). The URL bar is the remaining label-anchor.

    Position chosen at y=700 so the URL-bar's contribution genuinely
    pulls `bot` (default is `screen_h - 100 = 774`; URL bar at y=805
    would NOT pull because 800 > 774 — the test would pass trivially
    even if the label gate were broken)."""
    from sibb_scaffold import ElementRole

    addr = _MockElement("Address", y=700, h=22,
                          role=ElementRole.TEXT_FIELD)
    _top, bot = _derive_chrome_bounds([addr], None, 402, 874)
    # Anchored at 700 - 5 = 695, clamped only if < default 774. Want
    # a tight assertion that proves the label-gate ran.
    assert bot == pytest.approx(695.0), (
        f"URL bar at y=700 should anchor bot at 695; got {bot}")


def test_chrome_label_accepted_with_raw_xcuitest_role_string():
    """Critical regression (task #215): the elements `_derive_chrome_bounds`
    sees in PRODUCTION carry raw lowercase XCUITest role strings
    (`'btn'`, `'input'`, `'toolbar'`), NOT mapped ElementRole enums —
    the chrome derivation runs BEFORE role mapping. Earlier versions
    of the gate compared against the enum set and rejected every
    match silently. This test pins behavior for the production path.

    Updated 2026-06-06: the second sub-assertion (Done as btn) was
    removed when Step 3 dropped `done` from the chrome-label set.
    URL bar moved to y=700 so the label-anchored bot actually pulls
    (default 774 would otherwise eclipse y=805's contribution)."""
    addr = _MockElement("Address", y=700, h=22, role="search")  # URL bar
    _top, bot = _derive_chrome_bounds([addr], None, 402, 874)
    assert bot == pytest.approx(695.0), (
        f"raw-string 'search' role must pass gate and pull bot "
        f"to 695; got {bot}")


def test_chrome_label_rejected_with_raw_xcuitest_wrong_role():
    """Mirror of above: a raw-string role that's NOT in the chrome set
    (`'text'`, `'cell'`, `'img'`) must still be rejected."""
    # Use `Address` (still in the label set after Step 3); `Done` was
    # pruned, so a label="Done" check short-circuits on the LABEL gate
    # before the role gate is reached — the test would pass by accident.
    fake_addr = _MockElement("Address", y=700, h=22, role="text")
    _top, bot = _derive_chrome_bounds([fake_addr], None, 402, 874)
    assert bot == pytest.approx(774.0), (
        f"raw 'text' role must NOT pass; got {bot}")


def test_chrome_label_accepted_when_no_role_metadata_falls_through():
    """Pass-through documented contract: when effective_role is None
    (very old test fixtures, never the production path), the role
    check is skipped entirely and only geometry decides. This test
    PINS that the bypass exists — flip it to assert rejection if the
    contract ever changes."""
    addr = _MockElement("Address", y=805, h=22)
    addr.effective_role = None
    _top, bot = _derive_chrome_bounds([addr], None, 402, 874)
    assert bot <= 800.0, "None-role fixtures must fall through to geometry"


def test_chrome_clamp_does_not_inflate_bottom_above_kb_top():
    """Regression for task #223: the previous `middle = screen_h * 0.5`
    floor forced `bottom_chrome_top >= screen_h * 0.5 + 1`. In landscape
    iPhone 16 with kb up (screen_h=402, kb_y_top=170), the kb genuinely
    covers >50% of the screen and the chrome region must extend ABOVE
    the half-screen line. The old clamp would have pushed
    bottom_chrome_top to 202, mis-classifying y=170-202 as 'middle'.

    With the relaxed clamp, when the kb signal is present, the bound
    is allowed to sit anywhere as long as it's above top_chrome_bottom."""
    from sibb_scaffold import ElementRole

    # Landscape iPhone 16: screen_w=874, screen_h=402, kb_y_top=170.
    # No labels other than kb signal.
    _top, bot = _derive_chrome_bounds([], 170.0, 874, 402)
    # bottom_chrome_top should follow kb signal: min(170-5, 302) = 165.
    # Old clamp would force it to 202. New clamp lets it sit at 165.
    assert bot == pytest.approx(165.0), (
        f"clamp must not inflate bot above kb_y_top - 5; got bot={bot}")


def test_chrome_gate_uses_kb_y_top_not_bottom_half():
    """B2 regression (task #216): when kb_y_top is provided, the gate
    must accept a label match whose `y < kb_y_top` regardless of
    whether it's in the bottom half of the screen. Earlier code
    required `fr.y > screen_h * 0.5`, which silently rejected
    legitimate chrome anchors in landscape (small screen_h × kb top).

    Updated 2026-06-06: uses "Address" (URL bar — still in the chrome
    label set) instead of "Typing Predictions" (pruned by Step 3 —
    accessory bar is now agent-visible UI, not chrome).

    Test by comparing two configurations on a TALL screen (so the
    `middle = screen_h * 0.5` clamp doesn't eat the result): with the
    URL bar present vs without. If the gate accepts it, the bottom-
    chrome anchor shifts."""
    from sibb_scaffold import ElementRole

    # Tall portrait-like config (screen_h=900) so the clamp threshold
    # is 450 and the URL-bar contribution at y=550 isn't masked.
    url = _MockElement("Address", y=550, h=22,
                        role=ElementRole.TEXT_FIELD)
    _top, bot_with = _derive_chrome_bounds([url], 600.0, 402, 900)
    _top, bot_without = _derive_chrome_bounds([], 600.0, 402, 900)
    # bot_with anchored on min(550, 600) - 5 = 545.
    # bot_without anchored on kb_y_top - 5 = 595.
    assert bot_with < bot_without, (
        f"URL bar must pull chrome IN: with={bot_with}, "
        f"without={bot_without}")
    assert bot_with == pytest.approx(545.0)

    # Orientation-agnostic case: kb_y_top very small, URL bar above
    # it but in the UPPER half of the screen. Old gate would reject
    # (`y < screen_h * 0.5`). New gate must accept.
    upper_url = _MockElement("Address", y=200, h=22,
                              role=ElementRole.TEXT_FIELD)
    _top, bot_upper = _derive_chrome_bounds(
        [upper_url], 260.0, 402, 900)
    upper_bad = _MockElement("Address", y=200, h=22,
                              role="text")  # rejected by role gate
    _top, bot_bad = _derive_chrome_bounds(
        [upper_bad], 260.0, 402, 900)
    # Both clamp to >= 451, but the upper_bar case anchors on 200-5=195
    # (clamped to 451), while bot_bad anchors on kb_y_top-5=255 (also
    # clamped to 451). The clamp masks the difference here, so we
    # instead verify by mutating the screen to escape the clamp:
    # screen_h=400 → middle=200 → clamp threshold=201.
    _top, bot_upper_2 = _derive_chrome_bounds(
        [upper_url], 260.0, 402, 400)
    _top, bot_bad_2 = _derive_chrome_bounds(
        [upper_bad], 260.0, 402, 400)
    # bot_upper_2 anchored on min(200, 260) - 5 = 195, clamped to 201.
    # bot_bad_2 anchored on kb_y_top - 5 = 255, no clamp needed (255>201).
    assert bot_upper_2 < bot_bad_2, (
        f"upper URL bar should pass new gate; got upper={bot_upper_2}, "
        f"bad_role={bot_bad_2}")
