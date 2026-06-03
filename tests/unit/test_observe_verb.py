"""L1 tests for the OBSERVE [ms] action verb (landed 2026-05-31 in
sibb_scaffold.py + sibb_replay.py).

OBSERVE is a pure no-op on the simulator: sleep for `ms` (clamped to
[0, 10000]), then return. The top-of-turn observation in the LLM
driver re-observes naturally on the next iteration, so no explicit
observe() call is needed inside the executor.

Motivating use case: Maps Directions screen shows `Loading…` while
routes compute (5-30 s for distant destinations). TAP/SCROLL
interrupt the route fetch — OBSERVE lets the agent wait without
perturbing state.
"""
from __future__ import annotations
import asyncio
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

# Match how sibb_assistant.py imports — scaffold's parse_action lives
# on the SIBBScaffold class.
from sibb_scaffold import SIBBScaffold  # noqa: E402


def _parse(text: str):
    """Run the scaffold parser on a raw LLM output string."""
    scaffold = SIBBScaffold.__new__(SIBBScaffold)
    return scaffold.parse_action(text)


# ── Parser: bare OBSERVE ────────────────────────────────────────────

def test_bare_observe_parses_with_zero_wait():
    a = _parse("OBSERVE")
    assert a.action_type == "observe"
    assert a.amount == 0.0


def test_observe_with_explicit_zero():
    a = _parse("OBSERVE 0")
    assert a.action_type == "observe"
    assert a.amount == 0.0


def test_observe_with_typical_wait():
    a = _parse("OBSERVE 5000")
    assert a.action_type == "observe"
    assert a.amount == 5000.0


def test_observe_with_max_wait():
    a = _parse("OBSERVE 10000")
    assert a.action_type == "observe"
    assert a.amount == 10000.0


# ── Parser: clamping ────────────────────────────────────────────────

def test_observe_clamps_negative_to_zero():
    a = _parse("OBSERVE -500")
    assert a.action_type == "observe"
    assert a.amount == 0.0


def test_observe_clamps_oversize_to_cap():
    """Argument over 10000 must be clamped to 10000 — a confused agent
    can't park for minutes by saying OBSERVE 600000."""
    a = _parse("OBSERVE 60000")
    assert a.action_type == "observe"
    assert a.amount == 10000.0


# ── Parser: tolerates extra text on the line ────────────────────────

def test_observe_with_trailing_reasoning_text():
    """LLMs sometimes append a brief reason after the action — the
    parser should still extract the verb and the numeric arg."""
    a = _parse("OBSERVE 3000   # waiting for Maps to compute routes")
    assert a.action_type == "observe"
    assert a.amount == 3000.0


def test_observe_appears_after_reasoning_prose():
    """OBSERVE on its own line after reasoning prose — the standard
    output shape for the LLM driver."""
    a = _parse("I see Loading… on the Directions screen.\n"
                "Routes haven't appeared yet — I'll wait.\n"
                "OBSERVE 5000")
    assert a.action_type == "observe"
    assert a.amount == 5000.0


def test_observe_lowercase_inline_recovered():
    """Action-verb regex fallback is case-insensitive — `observe`
    in the middle of a reasoning line should still parse."""
    a = _parse("I'll just observe 2000 and see what happens")
    assert a.action_type == "observe"
    assert a.amount == 2000.0


# ── Parser: malformed arg falls back to zero ────────────────────────

def test_observe_with_non_numeric_arg_defaults_to_zero():
    """OBSERVE foo → can't parse 'foo' as ms; default to zero rather
    than raise. Keeps the agent's turn alive on a typo."""
    a = _parse("OBSERVE soon")
    assert a.action_type == "observe"
    assert a.amount == 0.0


# ── Executor: no-op returns success ─────────────────────────────────

def test_executor_observe_returns_success_with_zero_wait():
    """The execute() handler with wait_ms=0 should return immediately
    with success=True. Doesn't touch the reader/socket at all."""
    from sibb_replay import execute
    from sibb_scaffold import AgentAction
    action = AgentAction(action_type="observe", amount=0.0)

    class _FakeXc:
        pass

    class _FakeReader:
        _xcuitest = _FakeXc()

    result = asyncio.run(execute(_FakeReader(), action, tree=None))
    assert result["success"] is True
    assert result["slept_ms"] == 0
    assert "OBSERVE" in result["note"]


def test_executor_observe_actually_sleeps():
    """For non-zero wait_ms, execute() must sleep approximately the
    requested duration. Use 50 ms to keep the test fast."""
    import time
    from sibb_replay import execute
    from sibb_scaffold import AgentAction
    action = AgentAction(action_type="observe", amount=50.0)

    class _FakeReader:
        _xcuitest = None

    start = time.monotonic()
    result = asyncio.run(execute(_FakeReader(), action, tree=None))
    elapsed_ms = (time.monotonic() - start) * 1000.0
    assert result["success"] is True
    assert result["slept_ms"] == 50
    # asyncio.sleep is not perfectly tight; allow generous slack but
    # require AT LEAST close to the requested sleep happened.
    assert elapsed_ms >= 40, f"observe should sleep ~50ms, slept {elapsed_ms:.0f}ms"
