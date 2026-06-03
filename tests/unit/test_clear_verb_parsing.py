"""L1 parser tests for the CLEAR @ref verb.

The CLEAR verb wipes a text field's current content via the Swift-side
triple-tap-select-all + delete-key gesture. Tests cover:
  - basic `CLEAR @ref` parsing
  - case-insensitive variant on the action verb (LLMs lowercase mid-sentence)
  - mid-sentence recovery via the regex fallback
  - that empty `TYPE @ref ""` is NOT silently treated as CLEAR
    (the parser remains intentionally separate)
"""
from __future__ import annotations
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))

from sibb_scaffold import SIBBScaffold, AXReader  # noqa: E402


def _parse(output: str):
    return SIBBScaffold(AXReader("test-udid")).parse_action(output)


def test_clear_with_ref_uppercase():
    a = _parse("CLEAR @e0042")
    assert a.action_type == "clear"
    assert a.target_ref == "e0042"
    assert a.text is None


def test_clear_with_ref_lowercase_inline():
    """LLMs sometimes emit the action mid-sentence in lowercase. The
    regex fallback should still pull it out as a clear action."""
    a = _parse("The street has wrong text — I'll clear @e0099 first.")
    assert a.action_type == "clear"
    assert a.target_ref == "e0099"


def test_clear_with_label_quoted():
    """`CLEAR "Street"` should resolve via label fallback when no
    @ref is provided. (Less common but supported for symmetry with
    TAP / TYPE.)"""
    a = _parse('CLEAR "Street"')
    assert a.action_type == "clear"
    assert a.target_ref is None
    assert a.target_label == "Street"


def test_clear_at_end_of_reasoning():
    a = _parse(
        "I see the Street field has stale 'New York' appended. "
        "I will clear the field and re-type the street address.\n"
        "CLEAR @e0143"
    )
    assert a.action_type == "clear"
    assert a.target_ref == "e0143"


def test_type_empty_string_is_still_type_not_clear():
    """`TYPE @e0042 ""` should parse as a type (no-op at runtime),
    NOT silently rewritten as CLEAR. The agent should learn to use
    CLEAR explicitly. (This guards against a 'forgiving' parser that
    masks the actual behavior from the agent.)"""
    a = _parse('TYPE @e0042 ""')
    assert a.action_type == "type"
    # text is None because the regex extracts content between quotes;
    # an empty pair gives no match → None. Either way: NOT clear.
    assert a.action_type != "clear"


def test_clear_does_not_consume_type_lines():
    """A line starting with TYPE should not be misclassified as CLEAR
    even if 'clear' appears in the reasoning."""
    a = _parse(
        "The field is clear of content now. Let me type the new value.\n"
        'TYPE @e0042 "Hello"'
    )
    assert a.action_type == "type"
    assert a.target_ref == "e0042"
    assert a.text == "Hello"
