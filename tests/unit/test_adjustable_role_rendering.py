"""L1 tests pinning down the `[adj]` role-tag contract in scaffold output.

Regression coverage for the bug discovered in the 2026-05-27 variant B
trial: Swift's `snapshotAdjustable()` was setting `adjustable=True` on
focused text fields, causing the scaffold to render them as `[adj]`
in the observation — confusing both the agent (SYSTEM_PROMPT says
"NEVER TYPE into [adj]") and downstream replay heuristics (SCROLL
velocity, settle behavior, viewport carve-out).

The bug was fixed Swift-side. These tests pin the CONTRACT regardless
of who enforces it:
  - A text input with adjustable=False renders as `[input]`.
  - A text input with adjustable=True (the buggy state) WOULD render
    as `[adj]` (per the current effective_role override); this test
    documents the behavior so future refactors that change it are
    deliberate.
  - True adjustable elements (PICKER, SLIDER, etc.) render as `[adj]`.
"""
from __future__ import annotations
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))

from sibb_scaffold import (  # noqa: E402
    AXElement, AXFrame, AXTokenizer, AXTree, ElementRole,
)


def _make_el(ref: str, role: ElementRole, label: str,
             adjustable: bool = False,
             value: str = None) -> AXElement:
    return AXElement(
        ref=ref, label=label, role=role,
        value=value, frame=AXFrame(x=10, y=10, width=100, height=40),
        adjustable=adjustable,
    )


def _render(els):
    """Tokenize a list of elements into the observation-line format
    the agent sees. Returns one string per line."""
    tree = AXTree(elements=els, root=els[0] if els else None,
                  udid="test")
    tok = AXTokenizer()
    out = tok.tokenize(tree)
    return [l for l in out.split("\n") if l.strip()]


def test_textfield_without_adjustable_renders_as_input():
    """The healthy case: a plain text input renders as [input]."""
    el = _make_el("e001", ElementRole.TEXT_FIELD, "Last name",
                  adjustable=False)
    lines = _render([el])
    line = next(l for l in lines if "@e001" in l)
    assert "[input]" in line, f"expected [input], got: {line!r}"
    assert "[adj]" not in line, f"expected no [adj], got: {line!r}"


def test_textfield_with_adjustable_currently_renders_as_adj():
    """The buggy state (Swift over-flagging focused text fields):
    effective_role's adjustable override DOES turn TEXT_FIELD into
    [adj]. This test documents the current Python contract — the
    real fix lives in Swift (`snapshotAdjustable()` now excludes
    focused plain text inputs). If we later add a defensive Python
    filter, update this test."""
    el = _make_el("e002", ElementRole.TEXT_FIELD, "Last name",
                  adjustable=True)
    lines = _render([el])
    line = next(l for l in lines if "@e002" in l)
    assert "[adj]" in line, (
        f"expected [adj] per the effective_role override "
        f"(adjustable=True wins over role), got: {line!r}")


def test_real_picker_renders_as_adj():
    """A genuine adjustable element (PICKER role) renders as [adj]."""
    el = _make_el("e003", ElementRole.PICKER, "Year wheel",
                  adjustable=True, value="2024")
    lines = _render([el])
    line = next(l for l in lines if "@e003" in l)
    assert "[adj]" in line, f"expected [adj] on PICKER, got: {line!r}"


def test_slider_with_adjustable_renders_as_adj():
    """Slider — also a true adjustable. The scaffold's ROLE_MAP may
    or may not have an explicit SLIDER entry; the adjustable flag
    is the load-bearing signal. Once the Swift fix lands, sliders
    arrive with adjustable=True and effective_role promotes them."""
    el = _make_el("e004", ElementRole.OTHER, "Volume",
                  adjustable=True, value="0.5")
    lines = _render([el])
    line = next(l for l in lines if "@e004" in l)
    assert "[adj]" in line, (
        f"expected [adj] when adjustable=True (regardless of base "
        f"role), got: {line!r}")


def test_button_with_adjustable_false_does_not_render_as_adj():
    """Non-text non-adjustable elements: buttons stay as [btn]."""
    el = _make_el("e005", ElementRole.BUTTON, "Done",
                  adjustable=False)
    lines = _render([el])
    line = next(l for l in lines if "@e005" in l)
    assert "[btn]" in line, f"expected [btn], got: {line!r}"
    assert "[adj]" not in line, f"expected no [adj], got: {line!r}"


def test_mixed_tree_textfield_and_picker():
    """Multiple elements in one tree: a focused-style text input
    (adjustable=False, the correct post-fix state) renders as
    [input], a real picker as [adj] — sanity check that they
    don't interfere with each other."""
    text_field = _make_el("e010", ElementRole.TEXT_FIELD, "Name",
                          adjustable=False, value="Riley")
    picker = _make_el("e011", ElementRole.PICKER, "Month wheel",
                      adjustable=True, value="March")
    lines = _render([text_field, picker])
    name_line = next(l for l in lines if "@e010" in l)
    picker_line = next(l for l in lines if "@e011" in l)
    assert "[input]" in name_line and "[adj]" not in name_line
    assert "[adj]" in picker_line and "[input]" not in picker_line
