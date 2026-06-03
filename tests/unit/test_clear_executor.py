"""L1.5 tests for the CLEAR action handler in `sibb_replay.execute`.

Uses FakeXCUITestReader to exercise the full pipeline:
  parse_action("CLEAR @e0042")
    → execute(reader, action, tree)
       → reader._xcuitest.clear_text(ref="e0042")
       → returns {"ok": True, "before_length": ..., "after_length": ...}

These tests run without a simulator. They confirm:
  - element resolution by ref works
  - missing element returns success=False
  - before/after lengths propagate to the executor result
  - the fake's clear_text response can be configured for failure-path tests
"""
from __future__ import annotations
import asyncio
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "fakes")))

from fake_reader import FakeXCUITestReader     # noqa: E402
from sibb_scaffold import SIBBScaffold, AXReader  # noqa: E402
from sibb_replay import execute                 # noqa: E402


def _el(ref_id, role, label, y=400, height=40, x=10, width=200,
        focused=True, value=None):
    return {
        "ref": ref_id, "role": role, "label": label,
        "value": value or "",
        "frame": {"x": x, "y": y, "width": width, "height": height},
        "enabled": True, "focused": focused, "adjustable": False,
    }


def _build_tree(elements):
    fake = FakeXCUITestReader()
    fake.set_observe_response(elements=elements, keyboard_visible=True)
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return fake, reader, asyncio.run(reader._read_xcuitest())


def test_clear_with_ref_taps_right_edge():
    """Tap coord must be (frame.x + width - margin, center_y) — NOT
    center. iOS positions the cursor closest to the tap point; right-
    edge tap anchors at end-of-text where backspaces actually clear
    the field. Margin = 8px."""
    elements = [
        _el("e0042", "input", "Street", value="350 5th AveNew York"),
    ]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=19)
    # Scaffold renumbers refs to sequential e0001+. Use the assigned
    # ref from the tokenized tree (matches what the LLM would see).
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["ref"] == ref
    # Frame is (x=10, y=400, w=200, h=40). Right edge minus 8 margin:
    #   tap_x = 10 + 200 - 8 = 202
    #   tap_y = 400 + 40/2 = 420
    assert result["coords"] == (202, 420)
    assert result["length_hint"] == 19
    assert result["deletes_sent"] == 19


def test_clear_empty_field_short_circuits():
    """An empty field is already cleared — no Swift round-trip, no
    spurious tap. Result indicates the no-op."""
    elements = [_el("e0042", "input", "Street", value="")]
    fake, reader, tree = _build_tree(elements)
    # Configure clear_text to fail; if the executor calls it, the test
    # fails (proving the short-circuit didn't fire).
    fake.set_clear_text_response(ok=False, error="should not be called")
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["length_hint"] == 0
    assert result["deletes_sent"] == 0
    assert "already empty" in result.get("note", "")


def test_clear_narrow_field_clamps_tap_x():
    """For a field narrower than 2*margin (e.g. 12px wide), right-
    edge-minus-margin would give x = frame.x + 12 - 8 = frame.x + 4.
    That's still inside the field, so OK. But a 4px-wide field would
    give x = frame.x - 4 (LEFT of the field). Clamp tap_x to at least
    frame.x + 1 so it stays inside."""
    elements = [_el("e0042", "input", "Tiny",
                     value="x", x=100, y=400, width=4, height=40)]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=1)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    # frame.x + 1 = 101 (the clamp). NOT frame.x + width - margin = 96.
    assert result["coords"] == (101, 420)


def test_clear_stopped_early_propagated():
    """If Swift reports stopped_early=True (because length_hint+5 > 24),
    the executor surfaces it so the agent can re-issue CLEAR."""
    elements = [_el("e0042", "input", "LongField",
                     value="a very long string with many characters here")]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=24, stopped_early=True)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["stopped_early"] is True
    assert result["deletes_sent"] == 24


def test_clear_unknown_ref_returns_error():
    elements = [_el("e0042", "input", "Street")]
    fake, reader, tree = _build_tree(elements)
    action = SIBBScaffold(AXReader("test-udid")).parse_action("CLEAR @e9999")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False
    assert "not found" in result["error"]


def test_clear_label_fallback():
    """`CLEAR "Street"` resolves via label match when no @ref given."""
    elements = [_el("e0042", "input", "Street", value="x")]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=1)
    action = SIBBScaffold(AXReader("test-udid")).parse_action('CLEAR "Street"')
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    # Scaffold-assigned ref is returned (not the input dict ref).
    assert result["ref"] == tree.elements[0].ref


def test_clear_length_hint_passed_to_swift():
    """The executor must pass the current value length as length_hint
    so Swift can bulk-delete residue after the triple-tap select."""
    elements = [_el("e0042", "input", "Street", value="hello world")]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=11)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["length_hint"] == 11  # len("hello world")


def test_clear_runtime_error_surfaces_as_failure():
    """If the Swift side reports ok=False, execute() returns a
    failed result with the error message — not an exception."""
    elements = [_el("e0042", "input", "Street", value="x")]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(ok=False, error="swift not_found error")
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False
    assert "not_found" in result["error"]
