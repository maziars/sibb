"""L1 tests for the SCROLL_PAGE verb (task #228).

SCROLL_PAGE is a content-direction synonym for SWIPE: the agent says
"show me lower content" and the parser emits SWIPE with the direction
that iOS interprets as "finger moves up" (which is what scrolls the
page down). The verb exists because the clipped-button benchmark
showed agents emit `SWIPE down` when they mean "scroll the page down"
and then loop because iOS does the opposite (`SWIPE down` = finger
down = content goes down with finger = page scrolls UP).
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


# ─── inversion table ──────────────────────────────────────────────────


@pytest.mark.parametrize("content_dir,expected_finger_dir", [
    ("down",  "up"),
    ("up",    "down"),
    ("right", "left"),
    ("left",  "right"),
])
def test_scroll_page_inverts_direction(content_dir, expected_finger_dir):
    """SCROLL_PAGE <content_dir> → SWIPE <finger_dir>."""
    r = _parser().parse_action(f"SCROLL_PAGE {content_dir}")
    assert r.action_type == "swipe", (
        f"SCROLL_PAGE must dispatch as swipe; got {r.action_type}")
    assert r.direction == expected_finger_dir, (
        f"SCROLL_PAGE {content_dir} must emit SWIPE {expected_finger_dir}; "
        f"got SWIPE {r.direction}")


def test_scroll_page_default_is_down():
    """Bare `SCROLL_PAGE` (no direction) defaults to content-down — the
    most common case (agent wants to reveal more content below the
    fold). Emits SWIPE up."""
    r = _parser().parse_action("SCROLL_PAGE")
    assert r.action_type == "swipe"
    assert r.direction == "up"


def test_scroll_page_with_ref():
    """SCROLL_PAGE on an element ref still inverts direction; the
    element-bounded swipe executes inside the ref's frame."""
    r = _parser().parse_action("SCROLL_PAGE @e0042 down")
    assert r.action_type == "swipe"
    assert r.target_ref == "e0042"
    assert r.direction == "up"


def test_scroll_page_case_insensitive():
    """Inline-emitted lowercase form also routes correctly through the
    regex-based fallback recovery path."""
    r = _parser().parse_action(
        "I want to see more content, so scroll_page down")
    assert r.action_type == "swipe"
    assert r.direction == "up"


def test_scroll_page_beats_scroll_in_regex_alternation():
    """Regex alternation picks the FIRST match, not the longest. The
    parser sorts by length descending so `SCROLL_PAGE` beats `SCROLL`
    in the inline-recovery regex. Without that sort, the line below
    would parse as a bare SCROLL and lose the direction-inversion."""
    r = _parser().parse_action(
        "The page seems short; I'll scroll_page up to verify")
    assert r.action_type == "swipe"
    assert r.direction == "down", (
        f"SCROLL_PAGE up must emit SWIPE down (regex must prefer "
        f"SCROLL_PAGE over SCROLL); got direction={r.direction}")


def test_scroll_page_does_not_break_swipe_parsing():
    """Adding SCROLL_PAGE to the verb set must not regress SWIPE."""
    r = _parser().parse_action("SWIPE down")
    assert r.action_type == "swipe"
    assert r.direction == "down"  # finger direction, NOT inverted


def test_scroll_page_does_not_break_scroll_parsing():
    """Adding SCROLL_PAGE to the verb set must not regress SCROLL."""
    r = _parser().parse_action("SCROLL @e0042 down 5")
    assert r.action_type == "scroll"
    assert r.target_ref == "e0042"
    assert r.direction == "down"
    assert r.amount == 5.0


def test_scroll_page_accepts_amount():
    """SCROLL_PAGE down 3 should repeat the swipe 3 times — parity
    with SCROLL. Fixes Bug 3 from the 6-critic review: previously
    SCROLL_PAGE silently discarded the amount."""
    r = _parser().parse_action("SCROLL_PAGE down 3")
    assert r.action_type == "swipe"
    assert r.direction == "up"  # inverted content-down → finger-up
    assert r.amount == 3.0


def test_scroll_page_default_amount_is_one():
    r = _parser().parse_action("SCROLL_PAGE down")
    assert r.amount == 1.0


def test_scroll_page_garbage_direction_falls_back_to_default():
    """Bug 3 also asked for KeyError-safe inversion. An unknown
    direction (`SCROLL_PAGE diagonal`) must NOT crash the parser;
    it should fall through to the content-down default."""
    r = _parser().parse_action("SCROLL_PAGE diagonal")
    assert r.action_type == "swipe"
    # No direction token recognized → defaults to content-down → SWIPE up.
    assert r.direction == "up"


def test_scroll_page_uppercase_leading_verb_preserves_amount():
    """Leading-verb path (`SCROLL_PAGE DOWN 3`) — pins case-insensitive
    parsing AND that the amount survives the case-shift. The previous
    case-only test covered the inline-recovery path."""
    r = _parser().parse_action("SCROLL_PAGE DOWN 3")
    assert r.action_type == "swipe"
    assert r.direction == "up"
    assert r.amount == 3.0


def test_scroll_page_records_raw_verb():
    """Bug 4 fix: post-translation action_type=swipe loses provenance.
    raw_verb preserves the literal verb the agent emitted."""
    r = _parser().parse_action("SCROLL_PAGE down")
    assert r.raw_verb == "SCROLL_PAGE", (
        f"expected raw_verb=SCROLL_PAGE; got {r.raw_verb}")


def test_swipe_records_raw_verb():
    r = _parser().parse_action("SWIPE down")
    assert r.raw_verb == "SWIPE"


def test_tap_records_raw_verb():
    """raw_verb should be set even for non-aliased verbs so the analyst
    field is universally populated."""
    r = _parser().parse_action("TAP @e0042")
    assert r.raw_verb == "TAP"
