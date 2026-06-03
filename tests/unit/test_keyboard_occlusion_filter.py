"""L1.5 tests: keyboard-occlusion filter in `_read_xcuitest`.

Regression coverage for the 2026-05-27 probe finding: when the iOS
software keyboard is up, elements whose frame is fully below the
keyboard's top edge are unreachable by coordinate tap. The scaffold
filters them from the observation so the agent doesn't pick them as
TAP / TYPE targets only to have the action silently fail.

Tests use a synthetic AX tree via FakeXCUITestReader.observe() — no
simulator needed.
"""
from __future__ import annotations
import asyncio
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "fakes")))

from fake_reader import FakeXCUITestReader   # noqa: E402
from sibb_scaffold import AXReader            # noqa: E402


def _tree_with(elements, keyboard_frame=None):
    """Build a fake observe response and run the scaffold pipeline."""
    fake = FakeXCUITestReader()
    resp_kwargs = {"elements": elements}
    if keyboard_frame is not None:
        # FakeReader.set_observe_response doesn't take kb_frame; set
        # it on the response dict directly.
        fake.set_observe_response(**resp_kwargs)
        fake._observe_resp["keyboard_frame"] = keyboard_frame
    else:
        fake.set_observe_response(**resp_kwargs)
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return asyncio.run(reader._read_xcuitest())


def _el(ref_id, role, label, y, height=40, x=10, width=200,
        focused=False, value=None):
    return {
        "ref": ref_id, "role": role, "label": label,
        "value": value or "",
        "frame": {"x": x, "y": y, "width": width, "height": height},
        "enabled": True, "focused": focused, "adjustable": False,
    }


def test_no_keyboard_no_filtering():
    """When the keyboard isn't up (keyboard_frame=None), elements at
    any Y are preserved."""
    elements = [
        _el("a", "input", "First name", y=400),
        _el("b", "input", "Last name",  y=450),
        _el("c", "input", "Phone",      y=580),
        _el("d", "btn",   "Done",       y=820),
    ]
    tree = _tree_with(elements, keyboard_frame=None)
    labels = [e.label for e in tree.elements]
    assert "First name" in labels
    assert "Last name" in labels
    assert "Phone" in labels
    assert "Done" in labels


def test_keyboard_up_filters_fully_below_elements():
    """Keyboard at y=539. Elements at y >= 539 are filtered; elements
    at y < 539 are preserved."""
    elements = [
        _el("above1", "input", "First name", y=400),
        _el("above2", "input", "Last name",  y=450, focused=True),
        # y=540 — at keyboard top (below the 539 threshold)
        _el("below1", "input", "Phone",      y=540),
        _el("below2", "btn",   "KeyA",       y=600),
        _el("below3", "btn",   "KeyB",       y=700),
    ]
    kb_frame = {"x": 0, "y": 539, "width": 402, "height": 335}
    tree = _tree_with(elements, keyboard_frame=kb_frame)
    labels = [e.label for e in tree.elements]
    assert "First name" in labels, "above-keyboard elements preserved"
    assert "Last name" in labels
    assert "Phone" not in labels, (
        "phone field at y=540 should be filtered (below kb top 539)")
    assert "KeyA" not in labels
    assert "KeyB" not in labels


def test_focused_element_preserved_even_if_below_keyboard():
    """A focused element is preserved even if its frame is fully
    inside the keyboard region — the agent needs to see what's
    currently focused to reason about the next action (e.g., to clear
    a field they just typed into)."""
    elements = [
        _el("above", "input", "First name", y=400),
        # Hypothetical: a focused field at y=560 (below kb top 539).
        # Not realistic (focused fields normally lift above keyboard)
        # but tests the carve-out logic.
        _el("focused_below", "input", "Phone", y=560, focused=True),
    ]
    kb_frame = {"x": 0, "y": 539, "width": 402, "height": 335}
    tree = _tree_with(elements, keyboard_frame=kb_frame)
    labels = [e.label for e in tree.elements]
    assert "First name" in labels
    assert "Phone" in labels, (
        "focused element must NOT be filtered even when below kb top")


def test_keyboard_frame_propagates_to_tree():
    """The keyboard_frame from the raw response should land on the
    final AXTree as `.keyboard_frame` for downstream consumers
    (tokenizer header, executor pre-tap check)."""
    kb_frame = {"x": 0, "y": 500, "width": 402, "height": 374}
    tree = _tree_with([_el("a", "input", "Foo", y=100)],
                       keyboard_frame=kb_frame)
    assert tree.keyboard_frame == kb_frame, (
        f"keyboard_frame should pass through; got {tree.keyboard_frame!r}")


def test_kb_filtered_count_on_tree():
    """The tree exposes how many elements got filtered by the
    keyboard rule — useful for diagnostic logging."""
    kb_frame = {"x": 0, "y": 500, "width": 402, "height": 374}
    elements = [
        _el("a", "input", "Above1", y=100),
        _el("b", "input", "Above2", y=200),
        _el("c", "input", "Below1", y=550),
        _el("d", "input", "Below2", y=650),
        _el("e", "btn",   "Below3", y=750),
    ]
    tree = _tree_with(elements, keyboard_frame=kb_frame)
    assert tree.kb_filtered_count == 3, (
        f"expected 3 filtered, got {tree.kb_filtered_count}")


def test_partial_overlap_element_filtered():
    """An element whose TOP is above the keyboard but BOTTOM dips
    below is filtered — the tighter "fully visible" rule rejects any
    partial keyboard overlap. (Was previously preserved by the old
    fully-below-only filter; updated 2026-05-27 per user feedback that
    partial-visible elements create noise.)"""
    elements = [
        _el("partial", "input", "Half-covered", y=520, height=60),
        # y=520, h=60 → spans 520-580. kb_top=539 → top is above, bottom below.
    ]
    kb_frame = {"x": 0, "y": 539, "width": 402, "height": 335}
    tree = _tree_with(elements, keyboard_frame=kb_frame)
    labels = [e.label for e in tree.elements]
    assert "Half-covered" not in labels, (
        "partially-occluded element should be filtered "
        "(tighter fully-visible rule)")
    assert tree.kb_filtered_count == 1


def test_element_clipped_by_screen_bottom_filtered():
    """Element whose frame extends past screen bottom is filtered —
    cannot be fully seen or tapped, even with no keyboard up."""
    elements = [
        _el("above_fold",  "input", "Above",  y=400),
        _el("clipped",     "input", "Below",  y=850, height=60),
        # screen_height=874 → 850+60=910 > 874 + 1 tolerance → filtered
    ]
    tree = _tree_with(elements, keyboard_frame=None)
    labels = [e.label for e in tree.elements]
    assert "Above" in labels
    assert "Below" not in labels
    assert tree.viewport_filtered_count == 1


def test_element_clipped_by_screen_top_filtered():
    """Element whose frame starts above y=0 (negative y) is
    filtered — partially off-screen."""
    elements = [
        _el("normal",  "input", "Normal", y=100),
        _el("clipped", "input", "Above-screen", y=-10, height=40),
    ]
    tree = _tree_with(elements, keyboard_frame=None)
    labels = [e.label for e in tree.elements]
    assert "Normal" in labels
    assert "Above-screen" not in labels
    assert tree.viewport_filtered_count == 1


def test_element_clipped_by_right_edge_filtered():
    """Element extending past the right edge of the screen is
    filtered."""
    elements = [
        _el("normal",  "input", "Normal",   x=10, y=100, width=200),
        _el("clipped", "btn",   "Overflow", x=350, y=100, width=200),
        # screen_width=402 → 350+200=550 > 402 + 1 tolerance → filtered
    ]
    tree = _tree_with(elements, keyboard_frame=None)
    labels = [e.label for e in tree.elements]
    assert "Normal" in labels
    assert "Overflow" not in labels
    assert tree.viewport_filtered_count == 1


def test_focused_element_clipped_still_preserved():
    """Focused element bypasses the visibility filter — agent must
    see what they're typing into, even if iOS has it partially
    off-screen or under the keyboard."""
    elements = [
        _el("focused_below", "input", "Focused-but-occluded",
            y=560, focused=True),
    ]
    kb_frame = {"x": 0, "y": 539, "width": 402, "height": 335}
    tree = _tree_with(elements, keyboard_frame=kb_frame)
    labels = [e.label for e in tree.elements]
    assert "Focused-but-occluded" in labels


def test_within_tolerance_pixel_off_kept():
    """An element whose frame is within the 1px AX-rounding tolerance
    of the edge is KEPT — guards against false positives from sub-
    pixel frame geometry."""
    elements = [
        _el("edge", "input", "Edge", x=0, y=833, height=40),
        # bottom = 873 (just inside screen_h=874)
    ]
    tree = _tree_with(elements, keyboard_frame=None)
    labels = [e.label for e in tree.elements]
    assert "Edge" in labels
