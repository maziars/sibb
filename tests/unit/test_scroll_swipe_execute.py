"""SCROLL / SWIPE execute()-path L1 tests (2026-06-03).

The 2026-06-03 commit changed `sibb_replay.execute()`'s SCROLL handler
to REQUIRE an `@ref` — bare `SCROLL down` now returns
`{success: False, error: ...}` instead of falling back to a whole-app
swipe. SWIPE was left unchanged (it IS the whole-screen gesture verb).

Until now, the only coverage was `test_d3_element_targeted_swipe.py`
which **string-greps** `sibb_replay.py` for `"SCROLL requires an
element reference"` — that breaks the moment the error message is
reformatted, silently restoring the regression.

These tests dispatch a parsed `AgentAction` through the real
`execute()` against a fake `xc` whose method calls are recorded.
They lock in the behavior contract at the actual execution layer.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import pytest

# Make sibb/benchmark importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmark"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "simulator"))

from sibb_replay import execute  # noqa: E402
from sibb_scaffold import (  # noqa: E402
    AgentAction, AXElement, AXFrame, AXTree, ElementRole,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── fakes ────────────────────────────────────


@dataclass
class _Call:
    method: str
    args: tuple
    kwargs: dict


class FakeXc:
    """Records every method call. Returns minimal-ok responses where
    the real handler reads from the response."""

    def __init__(self) -> None:
        self.calls: List[_Call] = []

    def _rec(self, method: str, *args, **kwargs) -> None:
        self.calls.append(_Call(method, args, kwargs))

    async def swipe(self, direction: str = "left"):
        self._rec("swipe", direction=direction)

    async def swipe_at(self, x1: float, y1: float,
                        x2: float, y2: float,
                        duration_s: float = 0.05,
                        settle: bool = True,
                        velocity_pps: Optional[float] = None):
        self._rec("swipe_at", x1, y1, x2, y2,
                   duration_s=duration_s, settle=settle,
                   velocity_pps=velocity_pps)

    # Stubs for handlers that may incidentally be invoked (none of the
    # tests below dispatch a non-scroll/non-swipe action, but keep the
    # surface complete for safety).
    async def observe(self):  # pragma: no cover
        return AXTree(elements=[], root=None)


class FakeReader:
    """Minimal AXReader stand-in. `execute()` reads only `_xcuitest`."""

    def __init__(self, xc: FakeXc) -> None:
        self._xcuitest = xc


def _tree_with(elements: List[AXElement]) -> AXTree:
    return AXTree(elements=elements, root=None)


def _scroll_element(ref: str = "e0042",
                     x: float = 100, y: float = 200,
                     width: float = 200, height: float = 400,
                     role: ElementRole = ElementRole.SCROLL_AREA
                     ) -> AXElement:
    return AXElement(
        ref=ref, label=None, role=role,
        frame=AXFrame(x=x, y=y, width=width, height=height),
        enabled=True, visible=True)


# ─────────────────────────── SCROLL: bare → error ─────────────────────


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if asyncio.get_event_loop().is_running() is False else \
        asyncio.run(coro)


def test_scroll_bare_returns_error_and_does_not_swipe():
    """Bare `SCROLL down` (no @ref, no @label) must return
    success=False, point the agent at SWIPE, and NEVER call into the
    XCUITest socket (no whole-app swipe fallback)."""
    xc = FakeXc()
    reader = FakeReader(xc)
    action = AgentAction(action_type="scroll", direction="down")
    tree = _tree_with([])

    resp = asyncio.run(execute(reader, action, tree))

    assert resp["success"] is False
    assert "element reference" in resp["error"].lower() \
        or "@ref" in resp["error"] \
        or "requires" in resp["error"].lower()
    # The agent must be steered to SWIPE for whole-screen gestures.
    assert "swipe" in resp["error"].lower()
    # No swipe of either flavor should have been dispatched.
    assert xc.calls == []


def test_scroll_unresolvable_ref_returns_error_and_does_not_swipe():
    """SCROLL with an @ref that doesn't resolve in the snapshot must
    error out cleanly, NOT fall through to whole-app or to a default-
    coords swipe."""
    xc = FakeXc()
    reader = FakeReader(xc)
    action = AgentAction(action_type="scroll",
                          direction="down",
                          target_ref="e9999")
    tree = _tree_with([])  # no element with this ref

    resp = asyncio.run(execute(reader, action, tree))

    assert resp["success"] is False
    assert ("couldn't be resolved" in resp["error"]
            or "could not be resolved" in resp["error"]
            or "not found" in resp["error"])
    assert xc.calls == []


def test_scroll_unresolvable_label_returns_error():
    """SCROLL by label is the same contract — if no element with the
    label exists, error out."""
    xc = FakeXc()
    reader = FakeReader(xc)
    action = AgentAction(action_type="scroll",
                          direction="up",
                          target_label="Nonexistent List")
    tree = _tree_with([])

    resp = asyncio.run(execute(reader, action, tree))

    assert resp["success"] is False
    assert xc.calls == []


# ─────────────────────────── SCROLL: resolvable → swipe_at ────────────


def test_scroll_with_resolvable_ref_calls_swipe_at_inside_frame():
    """SCROLL with a resolvable @ref must invoke `xc.swipe_at` once,
    with coordinates entirely INSIDE the element's frame. This is the
    "stays inside the WebView" contract — the whole reason for the
    refactor. A bug that uses screen bounds instead of element bounds
    would still scroll on a phone-with-one-scrollable, but fail in
    Maps' nested scrolls, sheets, picker wheels."""
    xc = FakeXc()
    reader = FakeReader(xc)
    scroll = _scroll_element(ref="e0042",
                              x=100, y=200,
                              width=200, height=400)
    action = AgentAction(action_type="scroll",
                          direction="down",
                          target_ref="e0042",
                          amount=1.0)
    tree = _tree_with([scroll])

    resp = asyncio.run(execute(reader, action, tree))

    assert resp["success"] is True
    # Exactly one swipe_at; no whole-app fallback.
    assert any(c.method == "swipe_at" for c in xc.calls)
    assert all(c.method != "swipe" for c in xc.calls)

    swipe_calls = [c for c in xc.calls if c.method == "swipe_at"]
    assert len(swipe_calls) == 1
    x1, y1, x2, y2 = swipe_calls[0].args
    # Frame bounds: x in [100, 300], y in [200, 600].
    assert 100 <= x1 <= 300, f"x1={x1} outside element frame"
    assert 100 <= x2 <= 300, f"x2={x2} outside element frame"
    assert 200 <= y1 <= 600, f"y1={y1} outside element frame"
    assert 200 <= y2 <= 600, f"y2={y2} outside element frame"

    # Direction: SCROLL down ⇒ finger UP ⇒ y1 > y2 (drag bottom→top).
    assert y1 > y2, (
        f"SCROLL down should drag finger UPWARD (y1>y2); got y1={y1}, y2={y2}")
    # x stays at center for vertical scroll.
    assert abs(x1 - x2) < 1e-6, "vertical SCROLL should keep x constant"


def test_scroll_with_resolvable_ref_amount_n_dispatches_n_swipes():
    """`SCROLL @e042 down 3` issues 3 swipe_at calls (each one
    swipe). Locks the amount semantics in."""
    xc = FakeXc()
    reader = FakeReader(xc)
    scroll = _scroll_element(ref="e0042")
    action = AgentAction(action_type="scroll",
                          direction="down",
                          target_ref="e0042",
                          amount=3.0)
    tree = _tree_with([scroll])

    resp = asyncio.run(execute(reader, action, tree))

    assert resp["success"] is True
    swipe_calls = [c for c in xc.calls if c.method == "swipe_at"]
    assert len(swipe_calls) == 3
    assert resp["swipes"] == 3
    assert resp["requested_swipes"] == 3
    assert resp.get("capped") is False


def test_scroll_with_resolvable_ref_amount_above_cap_is_capped():
    """SCROLL_MAX_AMOUNT = 20. Asking for 100 swipes lands 20 and
    reports capped=True. Lock the cap in."""
    xc = FakeXc()
    reader = FakeReader(xc)
    scroll = _scroll_element(ref="e0042")
    action = AgentAction(action_type="scroll",
                          direction="down",
                          target_ref="e0042",
                          amount=100.0)
    tree = _tree_with([scroll])

    resp = asyncio.run(execute(reader, action, tree))

    assert resp["success"] is True
    swipe_calls = [c for c in xc.calls if c.method == "swipe_at"]
    assert len(swipe_calls) == 20
    assert resp["swipes"] == 20
    assert resp["requested_swipes"] == 100
    assert resp["capped"] is True


# ─────────────────────────── SWIPE: contract preservation ─────────────


def test_swipe_bare_falls_back_to_whole_app_swipe():
    """SWIPE was deliberately LEFT WITH the whole-app fallback —
    SWIPE is the whole-screen gesture verb (page-flip, Spotlight,
    Control Center, app switcher). Bare `SWIPE left` must call
    `xc.swipe(direction="left")` — NOT error like SCROLL does."""
    xc = FakeXc()
    reader = FakeReader(xc)
    action = AgentAction(action_type="swipe", direction="left")
    tree = _tree_with([])

    resp = asyncio.run(execute(reader, action, tree))

    assert resp["success"] is True
    # Whole-app swipe, NOT swipe_at.
    swipes = [c for c in xc.calls if c.method == "swipe"]
    assert len(swipes) == 1
    assert swipes[0].kwargs.get("direction") == "left"
    assert all(c.method != "swipe_at" for c in xc.calls)


def test_swipe_with_resolvable_ref_uses_swipe_at_inside_frame():
    """SWIPE with @ref still routes through swipe_at — element-
    targeted swipes (carousel pages, sheet grabbers) keep working."""
    xc = FakeXc()
    reader = FakeReader(xc)
    el = _scroll_element(ref="e0042",
                          x=50, y=100, width=300, height=200)
    action = AgentAction(action_type="swipe",
                          direction="left",
                          target_ref="e0042")
    tree = _tree_with([el])

    resp = asyncio.run(execute(reader, action, tree))

    assert resp["success"] is True
    swipe_at_calls = [c for c in xc.calls if c.method == "swipe_at"]
    assert len(swipe_at_calls) == 1
    x1, y1, x2, y2 = swipe_at_calls[0].args
    # Frame bounds: x in [50, 350], y in [100, 300].
    assert 50 <= x1 <= 350 and 50 <= x2 <= 350
    assert 100 <= y1 <= 300 and 100 <= y2 <= 300
    # SWIPE direction is finger direction — "left" means dragging
    # from right edge to left edge.
    assert x1 > x2
    # No whole-app fallback when ref resolved.
    assert all(c.method != "swipe" for c in xc.calls)
