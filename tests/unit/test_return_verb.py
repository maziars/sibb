"""L1 tests for the RETURN verb (Step 5k, 2026-06-07).

The keyboard's Return key is NOT surfaced in the AX tree, so the agent
has no AX-visible way to commit a typed URL in Safari's URL bar or
submit an in-app search. RETURN fires `\\n` via Swift's
`app.typeText("\\n")` — iOS dispatches it against the focused field's
keyboard configuration, so the same verb covers `Go` / `Search` /
`Done` / `Next` / plain `Return` semantics.

Coverage:
  - parser: bare `RETURN` parses to action_type="return"
  - parser: case-insensitivity (lowercase / mixed-case RETURN)
  - parser: trailing garbage after RETURN is ignored (no args)
  - parser: scan-from-end picks the last RETURN even with prose before
  - executor: dispatches `{"type": "return"}` through xc._send
  - executor: returns success=True with a sensible note
  - regression: TAP / TYPE / OBSERVE parsing still work after
    introducing RETURN
"""
from __future__ import annotations
import asyncio
import os
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "fakes")))

from fake_reader import FakeXCUITestReader  # noqa: E402
from sibb_scaffold import SIBBScaffold, AXReader  # noqa: E402
from sibb_replay import execute  # noqa: E402

pytestmark = pytest.mark.fast


def _parser():
    return SIBBScaffold(AXReader("test-udid"))


def _empty_tree():
    """Minimal AX tree with no elements — RETURN is argless and
    doesn't need an element. Returns (fake, reader, tree)."""
    fake = FakeXCUITestReader()
    fake.set_observe_response(elements=[])
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    tree = asyncio.run(reader._read_xcuitest())
    return fake, reader, tree


# ─── parser ───────────────────────────────────────────────────────────


def test_parser_return_bare():
    r = _parser().parse_action("RETURN")
    assert r.action_type == "return"
    # No targets — RETURN is argless.
    assert r.target_ref is None
    assert r.target_label is None
    assert r.target_x is None
    assert r.target_y is None


def test_parser_return_lowercase():
    """LLMs sometimes emit verbs lowercase mid-sentence. Action verbs
    are case-insensitive (the ambiguous DONE/FAIL/ANSWER/CLARIFY are
    not — RETURN is action-only)."""
    r = _parser().parse_action("return")
    assert r.action_type == "return"


def test_parser_return_trailing_garbage_ignored():
    """RETURN takes no arguments — anything after it on the same line
    is ignored. Doesn't accidentally pick up @refs or quoted strings."""
    r = _parser().parse_action('RETURN @e0042 "foo"')
    assert r.action_type == "return"
    assert r.target_ref is None
    assert r.target_label is None


def test_parser_return_picks_last_verb_with_prose():
    """Scan-from-end: if the LLM writes reasoning then issues RETURN,
    the parser finds RETURN on the last line and uses it."""
    r = _parser().parse_action(
        "I typed the URL, now I need to commit it.\nRETURN")
    assert r.action_type == "return"


def test_parser_return_inline_after_prose():
    """Inline fallback: if RETURN is at the END of a reasoning line
    (no preceding newline), the regex fallback still picks it up."""
    r = _parser().parse_action(
        "URL is typed, committing now. RETURN")
    assert r.action_type == "return"


# ─── executor ─────────────────────────────────────────────────────────


def test_executor_return_calls_send_with_return_type():
    """The executor must dispatch `{"type": "return"}` to Swift, NOT
    fall through to type/tap. The fake's return_call_count counter
    pins this so a regression where someone routes RETURN through
    `xc.type` (or skips dispatch entirely) would be caught."""
    fake, reader, tree = _empty_tree()
    action = _parser().parse_action("RETURN")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True, f"expected success, got {result}"
    assert fake.return_call_count == 1, (
        f"executor must call _send with type=return; "
        f"got return_call_count={getattr(fake, 'return_call_count', 0)}")


def test_executor_return_does_not_call_type_or_tap():
    """Sanity: RETURN must not accidentally route through `type` or
    `tap` paths. Empty text would still register as a `type` call in
    the fake and silently no-op."""
    fake, reader, tree = _empty_tree()
    action = _parser().parse_action("RETURN")
    asyncio.run(execute(reader, action, tree))
    assert getattr(fake, "tap_call_count", 0) == 0
    # type counter doesn't exist in fake — just confirm no spurious
    # call recorded by checking history for any non-return commands.
    cmd_types = [h["request"].get("type") for h in fake.history]
    assert "type" not in cmd_types
    assert "tap" not in cmd_types
    assert "tap_then_type" not in cmd_types


def test_executor_return_result_shape():
    """The returned result dict must include success=True and a
    non-empty note explaining what fired (for replay log readability
    + JSONL diagnostics)."""
    fake, reader, tree = _empty_tree()
    action = _parser().parse_action("RETURN")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert "note" in result
    assert isinstance(result["note"], str) and result["note"].strip()


def test_executor_return_no_keyboard_returns_clean_error():
    """Step 5L-C (2026-06-08): when Swift signals `no_keyboard`, the
    executor MUST surface a clean structured error rather than passing
    through `success=True`. The agent reading the result needs to learn
    that RETURN requires a focused text input — and the runner's JSONL
    log captures the failure mode for offline debugging."""
    fake, reader, tree = _empty_tree()
    fake.return_simulate_no_keyboard = True
    action = _parser().parse_action("RETURN")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False, (
        f"expected success=False when no keyboard is up; got {result}")
    assert "error" in result
    assert "no_keyboard" in result["error"]
    # Hint helps the agent diagnose (must include "focused" or "input").
    hint = result.get("hint", "")
    assert "focused" in hint or "input" in hint or "TAP" in hint, (
        f"hint should suggest TAP-to-focus; got {hint!r}")


def test_executor_return_no_keyboard_still_called_send():
    """Even on the no-keyboard error path, the executor MUST have
    called `_send({type: "return"})`. Otherwise the fake's counter
    wouldn't have ticked, which would mean we silently short-circuited
    on the Python side (and the Swift guard would never see the
    request)."""
    fake, reader, tree = _empty_tree()
    fake.return_simulate_no_keyboard = True
    action = _parser().parse_action("RETURN")
    asyncio.run(execute(reader, action, tree))
    assert fake.return_call_count == 1


# ─── regression: other verbs still parse ──────────────────────────────


def test_regression_tap_still_parses_after_return_added():
    r = _parser().parse_action("TAP @e0042")
    assert r.action_type == "tap"
    assert r.target_ref == "e0042"


def test_regression_observe_still_parses_after_return_added():
    r = _parser().parse_action("OBSERVE 5000")
    assert r.action_type == "observe"
    assert r.amount == 5000.0


def test_regression_type_still_parses_after_return_added():
    r = _parser().parse_action('TYPE @e0042 "hello"')
    assert r.action_type == "type"
    assert r.text == "hello"
