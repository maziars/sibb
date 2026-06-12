"""L1 tests for the PINCH verb (added 2026-06-06).

Covers:
  * Parser: `PINCH out` / `PINCH in` / `PINCH <scale>` / bare `PINCH`.
  * Executor: dispatches the right scale + velocity to the XCUITest
    client (via FakeXCUITestReader.pinch_history).
  * Inline-verb fallback: agent emits the verb mid-sentence.
  * Default behavior: bare PINCH defaults to zoom OUT (the canonical
    Safari auto-zoom recovery).

Live sim verification of the actual gesture dispatch is in
`sibb/simulator/sibb_probe_pinch_recovery.py`.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "fakes"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "benchmark"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "simulator"))

from fake_reader import FakeXCUITestReader  # noqa: E402
from sibb_scaffold import (  # noqa: E402
    AXFrame, AXReader, AXTree, SIBBScaffold,
)
from sibb_replay import execute  # noqa: E402

pytestmark = pytest.mark.fast


# ─────────────────────────── parser ──────────────────────────────────


def _parse(text: str):
    """Run the scaffold's grammar parser on a single line of LLM
    output. Returns the AgentAction."""
    sc = SIBBScaffold(udid="test-udid")
    return sc.parse_action(text)


def test_parse_pinch_out():
    action = _parse("PINCH out")
    assert action.action_type == "pinch"
    assert action.direction == "out"


def test_parse_pinch_in():
    action = _parse("PINCH in")
    assert action.action_type == "pinch"
    assert action.direction == "in"


def test_parse_pinch_explicit_scale():
    action = _parse("PINCH 0.6")
    assert action.action_type == "pinch"
    # No direction word — scale carries the intent.
    assert action.amount == pytest.approx(0.6)


def test_parse_bare_pinch_defaults_to_out():
    """A bare `PINCH` (no args) is the canonical Safari auto-zoom
    recovery. Default to zoom out so the agent's "recover from
    AUTO-ZOOMED" recipe works without remembering the direction word."""
    action = _parse("PINCH")
    assert action.action_type == "pinch"
    assert action.direction == "out"


def test_parse_pinch_inline_in_reasoning():
    """The scaffold's grammar tolerates the verb appearing inside the
    agent's reasoning prose (not just on its own first line). Make
    sure PINCH joins the verb set."""
    txt = ("Safari is auto-zoomed; the form fields are unreachable.\n"
           "I'll reset zoom now.\nPINCH out")
    action = _parse(txt)
    assert action.action_type == "pinch"
    assert action.direction == "out"


def test_parse_pinch_rejects_negative_scale():
    """Negative or zero scale is nonsensical — the parser should skip
    it and fall back to the direction default."""
    action = _parse("PINCH -1.0")
    assert action.action_type == "pinch"
    # Negative wasn't accepted as a scale; default to out.
    assert action.direction == "out"


# ─────────────────────── executor (replay) ───────────────────────────


def _exec(action_text: str):
    """Parse a single LLM-output line and run it through the live
    execute() path against a fake reader. Returns (result_dict,
    pinch_history)."""
    sc = SIBBScaffold(udid="test-udid")
    action = sc.parse_action(action_text)
    fake = FakeXCUITestReader()
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    tree = AXTree(elements=[], root=None, udid="test-udid")
    result = asyncio.run(execute(reader, action, tree))
    return result, getattr(fake, "pinch_history", [])


def test_execute_pinch_out_dispatches_scale_half():
    result, history = _exec("PINCH out")
    assert result["success"] is True
    assert result["scale"] == pytest.approx(0.5)
    assert result["direction"] == "out"
    assert len(history) == 1
    assert history[0]["scale"] == pytest.approx(0.5)
    assert history[0]["velocity"] == pytest.approx(1.0)


def test_execute_pinch_in_dispatches_scale_double():
    result, history = _exec("PINCH in")
    assert result["success"] is True
    assert result["scale"] == pytest.approx(2.0)
    assert result["direction"] == "in"
    assert history[0]["scale"] == pytest.approx(2.0)


def test_execute_pinch_explicit_scale_06():
    result, history = _exec("PINCH 0.6")
    assert result["success"] is True
    assert result["scale"] == pytest.approx(0.6)
    # Derived direction follows scale<1 → "out"
    assert result["direction"] == "out"
    assert history[0]["scale"] == pytest.approx(0.6)


def test_execute_pinch_explicit_scale_25():
    result, history = _exec("PINCH 2.5")
    assert result["success"] is True
    assert result["scale"] == pytest.approx(2.5)
    assert result["direction"] == "in"
    assert history[0]["scale"] == pytest.approx(2.5)


def test_execute_bare_pinch_zooms_out():
    """Bare PINCH (no direction) is the canonical recovery — zooms
    out. End-to-end through the executor."""
    result, history = _exec("PINCH")
    assert result["success"] is True
    assert result["scale"] == pytest.approx(0.5)
    assert history[0]["scale"] == pytest.approx(0.5)


# ─────────────────────── grammar inventory ───────────────────────────


def test_pinch_listed_in_verb_set():
    """The verb is enumerated in `_VERBS` (the grammar's known-verb
    list) so a stray "PINCH" in the LLM's prose is parsed as the
    verb rather than swallowed as noise."""
    from sibb_scaffold import SIBBScaffold
    sc = SIBBScaffold(udid="test-udid")
    out = sc.parse_action("I'm going to PINCH out\nPINCH out")
    assert out.action_type == "pinch"


def test_pinch_does_not_require_ref():
    """A `@ref` token in the line should not be required for PINCH —
    we pinch the whole app, not an element. (Future enhancement could
    accept an `@ref` to anchor the gesture; today we don't.)"""
    action = _parse("PINCH out")
    assert action.target_ref is None
    assert action.target_label is None


def test_zoom_recovery_header_mentions_pinch():
    """The AUTO-ZOOMED header note must give the agent a concrete
    recovery action — PINCH out — not just hand-wave."""
    from sibb_assistant import fmt_observation
    from sibb_scaffold import AXTokenizer

    # Build a synthetic zoomed tree by stamping flags directly.
    tree = AXTree(elements=[], root=None, udid="test")
    tree.keyboard_visible = True
    tree.coord_system_zoomed = True
    tree.zoom_factor = 1.5
    tree.zoom_source = "swift"
    tree.orientation = "portrait"
    out = fmt_observation(tree, AXTokenizer(), step=1)
    assert "AUTO-ZOOMED" in out
    assert "PINCH out" in out
