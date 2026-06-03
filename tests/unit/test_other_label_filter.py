"""L1 tests for the labeled-[other] surface + UIKit noise denylist
landed 2026-05-30 in sibb_scaffold.py.

Before this change: every [other]-roled element was dropped, even
when labeled — meaning Maps' per-route summary cells
("21 min, 10:22 ETA · 6.9 mi, Fastest") never reached the agent.

After this change:
  - Labeled [other] cells flow through to the agent's observation
    (rendered as `[el] "label"`).
  - A small UIKit-chrome denylist (Vertical/Horizontal scroll bar,
    Loading, Dimming View) drops noise that leaks through.
  - Unlabeled [other] continues to be dropped by SKIP_IF_UNLABELED.

The denylist is anchored (`^…$`) so legitimate labels containing
"Loading" or "scroll bar" as substrings are NOT dropped. See
IOS_SIM_QUIRKS §20 for the rationale + locale dependency.
"""
from __future__ import annotations
import asyncio
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "fakes")))

from fake_reader import FakeXCUITestReader  # noqa: E402
from sibb_scaffold import AXReader, AXTokenizer  # noqa: E402


def _tree_with(elements):
    fake = FakeXCUITestReader()
    fake.set_observe_response(elements)
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return asyncio.run(reader._read_xcuitest())


def _format(tree):
    return AXTokenizer().tokenize(tree)


def _other(label, y=100, x=200, width=200, height=40):
    """Build a fake-reader element dict for a labeled [other]."""
    return {
        "ref": f"raw_{y}", "role": "other", "label": label,
        "value": None,
        "frame": {"x": x, "y": y, "width": width, "height": height},
        "enabled": True, "adjustable": False, "focused": False,
    }


def _btn(label, y=200, x=300):
    return {
        "ref": f"btn_{y}", "role": "btn", "label": label,
        "value": None,
        "frame": {"x": x, "y": y, "width": 60, "height": 44},
        "enabled": True, "adjustable": False, "focused": False,
    }


# ── Labeled [other] surfaces to the agent (the motivating fix) ──────────────

def test_labeled_other_surfaces_as_el():
    """Maps' per-route summary cell ('21 min, 10:22 ETA · 6.9 mi,
    Fastest') is labeled [other]. Pre-fix: dropped. Post-fix: agent
    sees it as `[el] "21 min, ..."`. Without this, the agent can't
    differentiate route 1 from route 2."""
    tree = _tree_with([
        _other("21 min, 10:22 ETA · 6.9 mi, Fastest", y=487),
        _btn("Steps", y=487),
    ])
    out = _format(tree)
    assert '[el] "21 min, 10:22 ETA · 6.9 mi, Fastest"' in out, out
    assert '[btn] "Steps"' in out, out


def test_labeled_other_with_short_useful_label_surfaces():
    """Generic short labels also surface — Apple uses [other] for
    things like 'My Location' callouts and 'Active route' annotations."""
    tree = _tree_with([_other("Active route, 21 min", y=300)])
    out = _format(tree)
    assert '[el] "Active route, 21 min"' in out, out


# ── UIKit noise denylist drops scrollbar/loading/dimming ────────────────────

def test_vertical_scroll_bar_dropped():
    """UIScrollView's vertical indicator is labeled
    'Vertical scroll bar, 1 page'. Drop it."""
    tree = _tree_with([
        _other("Vertical scroll bar, 1 page", y=400),
        _btn("Save", y=500),  # control that should survive
    ])
    out = _format(tree)
    assert "scroll bar" not in out, out
    assert '[btn] "Save"' in out, out


def test_horizontal_scroll_bar_dropped():
    tree = _tree_with([
        _other("Horizontal scroll bar, 1 page", y=856),
    ])
    out = _format(tree)
    assert out == "" or "Horizontal scroll bar" not in out, out


def test_loading_indicator_dropped():
    """UIActivityIndicatorView's default AX label is 'Loading' or
    'Loading…'. Both should drop."""
    for label in ("Loading", "Loading…"):
        tree = _tree_with([_other(label, y=300)])
        out = _format(tree)
        assert label not in out, f"label {label!r} leaked: {out!r}"


def test_dimming_view_dropped():
    """UIPresentationController's backdrop dimming view is labeled
    'Dimming View' — purely decorative, never tappable."""
    tree = _tree_with([_other("Dimming View", y=200)])
    out = _format(tree)
    assert "Dimming View" not in out, out


# ── Anchored regex avoids false positives ───────────────────────────────────

def test_anchored_regex_keeps_loading_dock():
    """The regex is anchored (`^Loading$`/`^Loading…$`). A legitimate
    label containing 'Loading' as a substring (e.g. 'Loading dock
    instructions') must NOT be dropped."""
    tree = _tree_with([_other("Loading dock instructions", y=300)])
    out = _format(tree)
    assert "Loading dock instructions" in out, out


def test_anchored_regex_keeps_scroll_bar_substring():
    """A label that happens to mention 'scroll bar' inside a longer
    sentence (e.g. an app's setting description) is preserved."""
    tree = _tree_with([
        _other("Show the scroll bar always", y=300),
    ])
    out = _format(tree)
    assert "Show the scroll bar always" in out, out


# ── Regression guards — pre-existing behavior unchanged ─────────────────────

def test_unlabeled_other_still_dropped():
    """SKIP_IF_UNLABELED still drops empty [other] containers."""
    tree = _tree_with([
        {"ref": "empty", "role": "other", "label": None, "value": None,
         "frame": {"x": 0, "y": 100, "width": 100, "height": 40},
         "enabled": True, "adjustable": False, "focused": False},
        _btn("Done", y=200),
    ])
    out = _format(tree)
    assert "[el]" not in out, out
    assert '[btn] "Done"' in out, out


def test_button_label_unchanged_by_filter():
    """Non-OTHER elements (BTN, CELL, INPUT) pass through unchanged
    — the filter only affects the OTHER role."""
    tree = _tree_with([
        _btn("Save", y=100),
        {"ref": "cell1", "role": "cell", "label": "Row 1", "value": None,
         "frame": {"x": 0, "y": 200, "width": 390, "height": 60},
         "enabled": True, "adjustable": False, "focused": False},
    ])
    out = _format(tree)
    assert '[btn] "Save"' in out, out
    assert '[cell] "Row 1"' in out, out
