"""L1 tests for the DOUBLE_TAP verb.

DOUBLE_TAP exists because two rapid `xc.tap()` calls don't fire
WebKit's double-tap-to-zoom recognizer (empirically verified — see
IOS_SIM_QUIRKS §21). The native `XCUICoordinate.doubleTap()` API
does, so Swift exposes a `double_tap` command and the agent gets a
DOUBLE_TAP verb.

Coverage:
  - parser: by-coord / by-ref / by-label / case-insensitive
  - executor: dispatches through `xc.double_tap`, NOT `xc.tap`
  - executor: raw coord, ref, label paths return correct result dict
  - executor: error paths (element not found, no frame)
  - regression: does not break TAP / SCROLL_PAGE / SCROLL parsing
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


def _el(ref_id, role, label, y=400, height=40, x=10, width=200,
        focused=False, value=None):
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


# ─── parser ───────────────────────────────────────────────────────────


def test_parser_double_tap_by_coord():
    r = _parser().parse_action("DOUBLE_TAP (200, 100)")
    assert r.action_type == "double_tap"
    assert r.target_x == 200.0
    assert r.target_y == 100.0
    assert r.target_ref is None
    assert r.target_label is None


def test_parser_double_tap_by_ref():
    r = _parser().parse_action("DOUBLE_TAP @e0042")
    assert r.action_type == "double_tap"
    assert r.target_ref == "e0042"
    assert r.target_x is None
    assert r.target_y is None


def test_parser_double_tap_by_label():
    r = _parser().parse_action('DOUBLE_TAP "Heading"')
    assert r.action_type == "double_tap"
    assert r.target_label == "Heading"


def test_parser_double_tap_case_insensitive():
    """Inline-recovery path: LLM emits lowercase mid-sentence."""
    r = _parser().parse_action(
        "I'll reset the zoom with double_tap (200, 100)")
    assert r.action_type == "double_tap"
    assert r.target_x == 200.0


def test_parser_double_tap_records_raw_verb():
    """raw_verb is the literal agent-emitted verb (task #232)."""
    r = _parser().parse_action("DOUBLE_TAP (200, 100)")
    assert r.raw_verb == "DOUBLE_TAP"


# ─── executor — raw coord ────────────────────────────────────────────


def test_executor_double_tap_raw_coord_dispatches_double_tap_not_tap():
    """The critical contract: DOUBLE_TAP must dispatch through Swift's
    `double_tap` command, NOT two `tap` commands. The fake's
    `double_tap_call_count` proves the right pipeline was hit."""
    elements = [_el("e0042", "input", "Field")]
    fake, reader, tree = _build_tree(elements)
    action = _parser().parse_action("DOUBLE_TAP (200, 100)")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["coords"] == (200, 100)
    assert getattr(fake, "double_tap_call_count", 0) == 1
    assert fake.double_tap_calls[0] == {"x": 200.0, "y": 100.0,
                                         "ref": None}


def test_executor_double_tap_raw_coord_does_not_call_tap():
    """Defensive: assert we didn't accidentally route to single tap.
    The fake's `tap` dispatch is now instrumented (mirrors
    `double_tap_call_count`), so this assertion has real teeth: a
    regression that called `xc.tap()` twice instead of
    `xc.double_tap()` once would bump `tap_call_count` and fail."""
    elements = [_el("e0042", "input", "Field")]
    fake, reader, tree = _build_tree(elements)
    action = _parser().parse_action("DOUBLE_TAP (200, 100)")
    asyncio.run(execute(reader, action, tree))
    assert getattr(fake, "tap_call_count", 0) == 0, (
        f"DOUBLE_TAP must NOT route to single tap; got "
        f"tap_call_count={fake.tap_call_count}")
    assert getattr(fake, "double_tap_call_count", 0) == 1


# ─── executor — by ref ────────────────────────────────────────────────


def test_executor_double_tap_by_ref():
    elements = [_el("e0042", "btn", "Heading",
                    x=50, y=80, width=100, height=40)]
    fake, reader, tree = _build_tree(elements)
    ref = tree.elements[0].ref
    action = _parser().parse_action(f"DOUBLE_TAP @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    # center_x = 50 + 50 = 100; center_y = 80 + 20 = 100
    assert result["coords"] == (100, 100)
    assert result["ref"] == ref
    assert fake.double_tap_call_count == 1


def test_executor_double_tap_ref_not_found():
    """Bad ref → success=False, NO Swift dispatch."""
    elements = [_el("e0042", "btn", "Heading")]
    fake, reader, tree = _build_tree(elements)
    action = _parser().parse_action("DOUBLE_TAP @e9999")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False
    assert "not found" in result["error"].lower()
    assert getattr(fake, "double_tap_call_count", 0) == 0


# ─── regressions — sibling verbs ──────────────────────────────────────


def test_double_tap_does_not_break_tap_parsing():
    r = _parser().parse_action("TAP @e0042")
    assert r.action_type == "tap"
    assert r.target_ref == "e0042"


def test_double_tap_does_not_break_scroll_page_parsing():
    r = _parser().parse_action("SCROLL_PAGE down")
    assert r.action_type == "swipe"  # SCROLL_PAGE dispatches as swipe
    assert r.direction == "up"  # content-down → finger-up


def test_double_tap_does_not_break_scroll_parsing():
    r = _parser().parse_action("SCROLL @e0042 down 5")
    assert r.action_type == "scroll"
    assert r.amount == 5.0


def test_double_tap_beats_tap_in_regex_alternation():
    """Same alternation-ordering concern as SCROLL_PAGE vs SCROLL:
    DOUBLE_TAP is longer than TAP and must beat it in the inline-
    recovery regex. The parser sorts verbs by length descending."""
    r = _parser().parse_action(
        "the page is zoomed — let me double_tap (200, 100)")
    assert r.action_type == "double_tap"
    assert r.target_x == 200.0


# ─── executor — label-dispatch path (was untested) ───────────────────


def test_executor_double_tap_by_label():
    """Label-substring match through the executor. The parser test
    only verified target_label was set; this exercises the full
    dispatch path."""
    elements = [_el("e0042", "btn", "Confirm",
                    x=50, y=80, width=100, height=40)]
    fake, reader, tree = _build_tree(elements)
    action = _parser().parse_action('DOUBLE_TAP "Confirm"')
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["label"] == "Confirm"
    assert fake.double_tap_call_count == 1


# ─── executor — divergence-with-TAP guards ───────────────────────────


def test_executor_double_tap_rejects_disabled_element():
    """Aligns with TAP: a disabled element returns success=False with
    no Swift dispatch. This was a critic-flagged inconsistency where
    DOUBLE_TAP would silently dispatch on a disabled control."""
    elements = [_el("e0042", "btn", "Submit",
                    x=50, y=80, width=100, height=40)]
    elements[0]["enabled"] = False
    fake, reader, tree = _build_tree(elements)
    ref = tree.elements[0].ref
    action = _parser().parse_action(f"DOUBLE_TAP @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False
    assert "disabled" in result["error"].lower()
    assert getattr(fake, "double_tap_call_count", 0) == 0


def test_executor_double_tap_no_frame_returns_error():
    """Element resolves by ref but has no frame → clean error.
    Mirrors TAP's check."""
    # Build the element WITHOUT a frame by directly mutating
    # post-construction.
    elements = [_el("e0042", "btn", "Test")]
    fake, reader, tree = _build_tree(elements)
    tree.elements[0].frame = None
    ref = tree.elements[0].ref
    action = _parser().parse_action(f"DOUBLE_TAP @{ref}")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False
    assert "no frame" in result["error"].lower()
    assert getattr(fake, "double_tap_call_count", 0) == 0


def test_executor_double_tap_raw_coord_takes_precedence_over_ref():
    """`DOUBLE_TAP @e042 (200, 100)` — coord wins (mirrors TAP). Pins
    the documented contract."""
    elements = [_el("e0042", "btn", "Test",
                    x=50, y=80, width=100, height=40)]
    fake, reader, tree = _build_tree(elements)
    # parse_action accepts both forms in one line.
    action = _parser().parse_action("DOUBLE_TAP @e0042 (200, 100)")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    # The coord branch returns "raw coordinate double-tap" note; the
    # ref branch returns ref + label. Use that to discriminate.
    assert result.get("note") == "raw coordinate double-tap"
    assert result["coords"] == (200, 100)
    assert fake.double_tap_calls[0]["x"] == 200.0


# ─── old-helper backwards compat ─────────────────────────────────────


def test_executor_double_tap_catches_runtime_error_for_old_helper():
    """Old SIBBHelper builds (pre-2026-06-06) lack `case "double_tap":`
    and return `unknown:double_tap`. The PRODUCTION client raises
    `RuntimeError("double_tap failed: ...")`; the executor must catch
    and return a structured failure so the turn loop doesn't abort
    the episode.

    Simulates by patching the fake's `double_tap` method to raise."""
    elements = []
    fake, reader, tree = _build_tree(elements)
    async def _raise(x=None, y=None, ref=None):
        raise RuntimeError("double_tap failed: unknown:double_tap")
    fake.double_tap = _raise
    action = _parser().parse_action("DOUBLE_TAP (200, 100)")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False
    assert "dispatch failed" in result["error"].lower()
    # Diagnostic hint: should point at the rebuild path.
    assert "sibb_xcuitest_setup.sh" in result["error"]
