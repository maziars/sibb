"""L1 tests for Step 5L-D: multi-action turn rejection.

When the LLM emits 2+ consecutive verb-lines in one response, the prior
"scan from end, pick last verb" behavior silently dropped the earlier
actions on the floor (e.g., seed=1 of the 5g-5k baseline emitted
CLEAR + TYPE + RETURN, parser ran only RETURN, RETURN hit the
no-keyboard guard and crashed Swift). The new behavior rejects with a
clear `fail` action whose `reason` lists the verbs the LLM emitted,
giving the agent actionable feedback.

Edge cases preserved (NOT rejected):
  * Reasoning prose between verb-lines (LLM "self-correction" pattern)
  * Inline verb after reasoning on the same line (no newline)
  * Single verb-line with prose before it (the common case)
"""
from __future__ import annotations
import os
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))

from sibb_scaffold import SIBBScaffold, AXReader  # noqa: E402

pytestmark = pytest.mark.fast


def _parser():
    return SIBBScaffold(AXReader("test-udid"))


# ─── rejection cases ──────────────────────────────────────────────────


def test_reject_two_consecutive_verb_lines():
    """The seed=1 baseline crash case: CLEAR + TYPE + RETURN emitted
    in one response with no prose between them. Old parser ran only
    RETURN. New parser rejects so the agent sees clear feedback."""
    msg = (
        'CLEAR @e0047\n'
        'TYPE @e0047 "http://127.0.0.1:55900/event"\n'
        'RETURN'
    )
    a = _parser().parse_action(msg)
    assert a.action_type == "fail"
    assert "Multi-action" in a.reason
    assert "CLEAR" in a.reason
    assert "TYPE" in a.reason
    assert "RETURN" in a.reason


def test_reject_two_simple_consecutive_verb_lines():
    msg = "TAP @e0042\nTYPE @e0058 \"hello\""
    a = _parser().parse_action(msg)
    assert a.action_type == "fail"
    assert "Multi-action" in a.reason


def test_reject_three_consecutive_verbs_picks_full_run_for_feedback():
    """When 3 are emitted consecutively, the failure message should
    list all three so the agent sees the full picture (not just the
    first two)."""
    msg = "TAP @e0001\nTYPE @e0002 \"a\"\nRETURN"
    a = _parser().parse_action(msg)
    assert a.action_type == "fail"
    # All three verbs should appear in the reason.
    assert "TAP" in a.reason
    assert "TYPE" in a.reason
    assert "RETURN" in a.reason


# ─── preserved (NOT rejected) cases ───────────────────────────────────


def test_self_correction_with_prose_between_picks_last():
    """The classic "thought, no wait, actual action" pattern:
       TAP @x
       Actually let me reconsider.
       ANSWER {...}
    The prose between disambiguates: this is self-correction, not
    sequential intent. Old behavior (pick last) is preserved.
    """
    msg = (
        'TAP @e0042\n'
        'Actually, let me reconsider.\n'
        'ANSWER {"value": "final"}'
    )
    a = _parser().parse_action(msg)
    assert a.action_type == "answer"
    assert a.answer_payload == {"value": "final"}


def test_single_verb_with_reasoning_before_picks_action():
    """The common case: reasoning followed by a single action line."""
    msg = (
        "I need to commit the URL now.\n"
        "RETURN"
    )
    a = _parser().parse_action(msg)
    assert a.action_type == "return"


def test_inline_verb_in_prose_picks_inline_action():
    """Verb appended to the end of a reasoning sentence (no newline)
    — the regex inline fallback should still work."""
    msg = "I've typed the URL, committing now. RETURN"
    a = _parser().parse_action(msg)
    assert a.action_type == "return"


def test_verb_line_then_prose_then_unrelated_verb_picks_last():
    """Wider self-correction window: prose-only lines between two
    verb-lines, with the second one being the actual final intent."""
    msg = (
        "DOUBLE_TAP (100, 200)\n"
        "Actually that's wrong; let me TAP the right element instead.\n"
        "TAP @e0099"
    )
    a = _parser().parse_action(msg)
    assert a.action_type == "tap"
    assert a.target_ref == "e0099"


# ─── edge cases ───────────────────────────────────────────────────────


def test_single_verb_line_unchanged():
    a = _parser().parse_action("RETURN")
    assert a.action_type == "return"


def test_empty_output_still_returns_fail_with_old_reason():
    a = _parser().parse_action("")
    assert a.action_type == "fail"
    # Old "Empty LLM output" reason should still fire.
    assert "Empty" in a.reason
