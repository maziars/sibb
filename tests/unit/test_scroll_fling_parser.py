"""L1 tests for SCROLL + FLING parser branches in `sibb_scaffold.py`.

Regression coverage for the failure modes observed in trial runs:
- `SCROLL down 2026` (agent confused amount with target value) parses
  cleanly to amount=2026 without crashing — the cap downstream is what
  protects the agent.
- `FLING @e042 down` with no amount defaults to 1.
- Lowercase `fling` / `scroll` inline-emitted by the LLM mid-sentence
  gets recovered by the case-insensitive regex.
"""
from __future__ import annotations
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))

from sibb_scaffold import SIBBScaffold, AXReader  # noqa: E402


def _parser():
    # SIBBScaffold needs a reader; pass a stub UDID — we never use it
    # for parsing.
    return SIBBScaffold(AXReader("test-udid"))


def test_scroll_with_ref_and_amount():
    r = _parser().parse_action("SCROLL @e0350 down 5")
    assert r.action_type == "scroll"
    assert r.target_ref == "e0350"
    assert r.direction == "down"
    assert r.amount == 5.0


def test_scroll_year_misinterpretation_regression():
    """Yesterday's failure: agent emitted `SCROLL down 2026` thinking
    2026 was the target year. Parser should accept it cleanly; the
    cap in the executor (SCROLL_MAX_AMOUNT=20) clamps the actual
    swipe count and the result `note` field carries cap feedback to
    the agent."""
    r = _parser().parse_action("SCROLL down 2026")
    assert r.action_type == "scroll"
    assert r.target_ref is None       # bare form, no element
    assert r.direction == "down"
    assert r.amount == 2026.0          # parser accepts as-is


def test_fling_with_ref_and_amount():
    r = _parser().parse_action("FLING @e0350 down 2")
    assert r.action_type == "fling"
    assert r.target_ref == "e0350"
    assert r.direction == "down"
    assert r.amount == 2.0


def test_fling_with_ref_no_amount_defaults_to_1():
    r = _parser().parse_action("FLING @e0042 up")
    assert r.action_type == "fling"
    assert r.target_ref == "e0042"
    assert r.direction == "up"
    assert r.amount == 1.0


def test_fling_no_ref_parses_to_action():
    """Parser accepts it; executor's job to reject with a clear
    error pointing at SWIPE for whole-screen gestures."""
    r = _parser().parse_action("FLING down 5")
    assert r.action_type == "fling"
    assert r.target_ref is None
    assert r.direction == "down"
    assert r.amount == 5.0


def test_lowercase_fling_inline_recovered():
    """LLM mid-sentence: '... so I will fling @e042 down 2'. The
    inline-verb regex was previously case-sensitive — lowercase
    `fling` would not match, the parser would fall through to
    'Empty LLM output' or first-line fallback. Now matches via
    re.IGNORECASE."""
    text = "Reasoning is correct so I'll fling @e042 down 2"
    r = _parser().parse_action(text)
    assert r.action_type == "fling", (
        f"expected fling, got {r.action_type!r}: parse_error="
        f"{getattr(r, 'parse_error', None)}")
    assert r.target_ref == "e042"
    assert r.direction == "down"
    assert r.amount == 2.0


def test_lowercase_scroll_inline_recovered():
    """Same as above for scroll."""
    text = "I think I should scroll @e0350 down 5 to get there"
    r = _parser().parse_action(text)
    assert r.action_type == "scroll"
    assert r.target_ref == "e0350"


def test_leading_fling_uppercased():
    """Leading-position verb is uppercased before dispatch; agent
    emitting lowercase `fling @e042 up` (against grammar) still works."""
    r = _parser().parse_action("fling @e042 up")
    assert r.action_type == "fling"
    assert r.target_ref == "e042"


def test_lowercase_done_in_prose_not_matched():
    """`I am done with this` must NOT parse to DONE — `done` is a common
    English word, the agent isn't declaring task completion. Ambiguous
    verbs (DONE / FAIL / ANSWER / CLARIFY) require exact uppercase."""
    r = _parser().parse_action("I am done with this challenge.")
    assert r.action_type != "done", (
        f"lowercase 'done' in prose should NOT parse to DONE action; "
        f"got action_type={r.action_type!r}")


def test_lowercase_fail_in_prose_not_matched():
    """`the fail case` must NOT parse to a true FAIL agent declaration.
    Parser may still return action_type='fail' as its parse-failed
    sentinel, but the reason should reflect a parse failure rather than
    a successful FAIL parse."""
    r = _parser().parse_action("Considering the fail case here.")
    # Either NOT a fail action, OR a fail action whose reason is a
    # parse-error (not the agent's actual FAIL declaration).
    if r.action_type == "fail":
        assert r.reason and "Unrecognized" in r.reason, (
            f"lowercase 'fail' in prose recovered as a FAIL action; "
            f"reason={r.reason!r}")


def test_uppercase_done_bare_matches():
    """Bare uppercase DONE on its own line still parses correctly."""
    r = _parser().parse_action("DONE")
    assert r.action_type == "done"


def test_uppercase_done_inline_matches():
    """Inline uppercase DONE in prose still recovered (LLM emits action
    at end of reasoning paragraph)."""
    r = _parser().parse_action("All looks good. DONE")
    assert r.action_type == "done"
