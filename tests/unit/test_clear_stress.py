"""L1.5 stress tests for CLEAR — edge cases the user flagged
2026-05-28:

  - Empty string (already covered by test_clear_empty_field_short_circuits
    in test_clear_executor.py; verified here for the multi-stage flow).
  - Long string (longer than visible screen width). iOS clips the
    rendered text but the AX `value` attribute is the FULL string.
    The CLEAR right-edge tap lands at the field's RIGHT BORDER, NOT
    the end of the (overflowing) text — iOS still positions the
    cursor at end-of-value when you tap past the visible content.
  - Multi-line string (UITextView). The right-edge tap lands at the
    end of LINE 1 (the line the tap-y intersects), NOT the end of the
    full multi-line value. Same backspace burst still works because
    we're sending N+5 backspaces and iOS deletes backward across line
    breaks.
  - Length-hint > 19 (so length_hint + 5 > 24) — Swift caps at 24,
    returns stopped_early=True; agent must re-issue CLEAR for residue.
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
    fake.set_observe_response(elements=elements)
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return fake, reader, asyncio.run(reader._read_xcuitest())


# ── empty value ──────────────────────────────────────────────────────────────

def test_clear_empty_value_no_swift_call():
    """Empty field: executor short-circuits WITHOUT calling Swift's
    clear_text. Verified by configuring Swift to fail — if we got
    there, the test would fail."""
    elements = [_el("e0042", "input", "Empty", value="")]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(ok=False, error="should NOT be called")
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["length_hint"] == 0
    assert result["deletes_sent"] == 0
    assert "already empty" in result["note"]


# ── long string (overflows visible width) ────────────────────────────────────

def test_clear_long_value_overflows_field_width():
    """Value is much longer than the rendered field width (e.g. a
    50-char URL in a 200px-wide field). iOS UITextField scrolls
    horizontally; the AX `value` is the full string. Length hint
    = len(value) = 50. Since 50 + 5 = 55 > 24 HARD_CAP, Swift sends
    24 and reports stopped_early=True. Agent should re-issue CLEAR
    to wipe the residue."""
    long_value = "https://www.example.com/some/long/path/abcdef"
    assert len(long_value) > 24
    elements = [_el("e0042", "input", "URL",
                     value=long_value, width=200)]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=24, stopped_early=True)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["length_hint"] == len(long_value)
    assert result["deletes_sent"] == 24
    assert result["stopped_early"] is True


# ── length_hint capping behavior at the boundary ─────────────────────────────

def test_clear_length_hint_exactly_19_does_not_stop_early():
    """N=19 → request 24 backspaces (19 + 5 padding). Swift caps at
    24 and reports stopped_early=False (request <= HARD_CAP)."""
    val = "x" * 19
    elements = [_el("e0042", "input", "F", value=val)]
    fake, reader, tree = _build_tree(elements)
    # Mirror what Swift would return: requested=19+5=24, n=24,
    # stopped_early=False.
    fake.set_clear_text_response(deletes_sent=24, stopped_early=False)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["deletes_sent"] == 24
    assert result["stopped_early"] is False


def test_clear_length_hint_20_triggers_stopped_early():
    """N=20 → request 25, exceeds HARD_CAP. Swift caps at 24 + stops
    early flag set."""
    val = "x" * 20
    elements = [_el("e0042", "input", "F", value=val)]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=24, stopped_early=True)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["deletes_sent"] == 24
    assert result["stopped_early"] is True


# ── multi-line value (UITextView) ────────────────────────────────────────────

def test_clear_multiline_value_textarea():
    """UITextView with a multi-line value. Length hint is the full
    string length including newlines. Right-edge tap puts the cursor
    at the end of LINE 1 (the line the tap-y intersects). Backspaces
    delete backward across line breaks — iOS treats \\n as a single
    deletable character. So N+5 backspaces still clear the whole
    value, AS LONG AS the request <= 24 cap."""
    val = "line one\nline two\nline three"  # 28 chars, exceeds cap
    assert len(val) > 24
    elements = [_el("e0042", "textarea", "Notes",
                     value=val, height=120)]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=24, stopped_early=True)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["length_hint"] == 28
    assert result["stopped_early"] is True


def test_clear_short_multiline_no_stop_early():
    """Short multi-line value (under 19 chars) clears in one CLEAR."""
    val = "ab\ncd\nef"  # 8 chars
    elements = [_el("e0042", "textarea", "Notes",
                     value=val, height=120)]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=13, stopped_early=False)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["length_hint"] == 8
    assert result["stopped_early"] is False


# ── unicode + emoji ──────────────────────────────────────────────────────────

def test_clear_value_with_emoji_uses_char_count():
    """Emoji are typically grapheme clusters of multiple UTF-16 code
    points. Python's len() counts code points; Swift's String.count
    counts grapheme clusters. They diverge for combined emoji
    (👨‍👩‍👧 etc.). The cap is on the COUNT we send, so as long as
    we're under HARD_CAP, divergence is harmless. This test confirms
    we propagate len() without crashing."""
    val = "Hi 👋 world"  # 10 grapheme clusters; 11 code points
    elements = [_el("e0042", "input", "F", value=val)]
    fake, reader, tree = _build_tree(elements)
    fake.set_clear_text_response(deletes_sent=len(val) + 5,
                                    stopped_early=False)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(f"CLEAR @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["length_hint"] == len(val)
