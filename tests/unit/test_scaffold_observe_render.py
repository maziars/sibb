"""L1.5 tests: synthetic AX trees → scaffold tokenizer.

Uses FakeXCUITestReader.set_observe_response() to inject canned AX
JSON, then exercises the full scaffold observation pipeline
(_read_xcuitest → AXTokenizer.tokenize). Pins behavior for cases the
real simulator surfaced as bugs, without needing the simulator
running.

Regression: a TEXT_FIELD coming back with `adjustable=True` (the
Swift-side `snapshotAdjustable()` over-flagging bug) must NOT
silently render as `[adj]` and lose the typing affordance. This test
documents what the scaffold currently does so a future Python-side
defensive filter is a deliberate behavior change, not a stealth one.
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
    """Build a fake reader pre-loaded with `elements` as its next
    observe() response, then run the scaffold's _read_xcuitest path
    (which is the entry point used by SIBBScaffold.observe()) and
    return the resulting AXTree.

    Uses `asyncio.run()` rather than `get_event_loop().run_until_complete()`
    — pytest-asyncio in `auto` mode (see pytest.ini) manages its own
    event loops, and `get_event_loop()` from a sync wrapper can return
    a closed/stale loop when sibling async tests have run first. The
    failure mode is non-deterministic across pytest's collection
    ordering; `asyncio.run()` always creates a fresh isolated loop."""
    fake = FakeXCUITestReader()
    fake.set_observe_response(elements)
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return asyncio.run(reader._read_xcuitest())


def _format(tree):
    return AXTokenizer().tokenize(tree)


def test_observe_textfield_with_adjustable_false():
    """The healthy state: post-Swift-fix, a focused text input arrives
    with adjustable=False and renders as `[input]`."""
    elements = [
        {"ref": "raw1", "role": "input", "label": "Last name",
         "value": "Brown", "frame": {"x": 10, "y": 10,
                                       "width": 100, "height": 40},
         "enabled": True, "adjustable": False, "focused": True},
    ]
    tree = _tree_with(elements)
    out = _format(tree)
    assert "[input]" in out, f"expected [input], got: {out!r}"
    assert "[adj]" not in out, f"expected no [adj], got: {out!r}"


def test_observe_textfield_with_adjustable_true_documents_current_bug():
    """The buggy state (pre-Swift-fix): if the Swift handler sends a
    text input with adjustable=True, the scaffold's effective_role
    override turns it into `[adj]`. We document this so future
    defensive Python filters are deliberate.

    Real fix is Swift-side (`snapshotAdjustable()` now excludes
    focused plain text inputs)."""
    elements = [
        {"ref": "raw2", "role": "input", "label": "Last name",
         "value": "Brown", "frame": {"x": 10, "y": 10,
                                       "width": 100, "height": 40},
         "enabled": True, "adjustable": True, "focused": True},
    ]
    tree = _tree_with(elements)
    out = _format(tree)
    assert "[adj]" in out, (
        "scaffold currently honors adjustable=True over base role "
        "(effective_role override). If you add a defensive Python "
        f"filter, update this test. Got: {out!r}")


def test_observe_real_picker_renders_as_adj():
    """A real picker — Swift sends adjustable=True legitimately, and
    the scaffold renders [adj]. This is the wanted behavior."""
    elements = [
        {"ref": "raw3", "role": "picker", "label": "Month wheel",
         "value": "March", "frame": {"x": 10, "y": 10,
                                       "width": 100, "height": 200},
         "enabled": True, "adjustable": True, "focused": False},
    ]
    tree = _tree_with(elements)
    out = _format(tree)
    assert "[adj]" in out


def test_observe_mixed_realistic_scene():
    """A small contact-form-like scene: two text inputs (post-fix
    state, adjustable=False) + one real picker (adjustable=True).
    Verify each role tag lines up correctly."""
    elements = [
        {"ref": "raw_fn", "role": "input", "label": "First name",
         "value": "Riley",
         "frame": {"x": 10, "y": 10, "width": 200, "height": 40},
         "enabled": True, "adjustable": False, "focused": False},
        {"ref": "raw_ln", "role": "input", "label": "Last name",
         "value": "",
         "frame": {"x": 10, "y": 60, "width": 200, "height": 40},
         "enabled": True, "adjustable": False, "focused": True},
        {"ref": "raw_bd", "role": "picker", "label": "Birthday",
         "value": "March 24",
         "frame": {"x": 10, "y": 110, "width": 200, "height": 100},
         "enabled": True, "adjustable": True, "focused": False},
    ]
    tree = _tree_with(elements)
    out = _format(tree)
    # First name line
    fn_line = next(l for l in out.split("\n") if "First name" in l)
    assert "[input]" in fn_line and "[adj]" not in fn_line, (
        f"first name should be [input] when adjustable=False, "
        f"got: {fn_line!r}")
    # Last name line (focused, but adjustable=False per post-fix)
    ln_line = next(l for l in out.split("\n") if "Last name" in l)
    assert "[input]" in ln_line and "[adj]" not in ln_line, (
        f"focused last name should STILL be [input] when "
        f"adjustable=False (the Swift fix). got: {ln_line!r}")
    # Birthday picker (real adjustable)
    bd_line = next(l for l in out.split("\n") if "Birthday" in l)
    assert "[adj]" in bd_line and "[input]" not in bd_line, (
        f"birthday picker should be [adj], got: {bd_line!r}")
