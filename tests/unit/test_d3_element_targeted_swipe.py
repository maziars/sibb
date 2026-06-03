"""D3 — element-targeted SCROLL/SWIPE via `swipe_at`.

Pure unit tests for the coordinate-computation helper and a smoke
test that the fake reader accepts `swipe_at`. Routing through
`execute()` is exercised in the L1.5 layer below where AgentAction
+ AXTree are easy to construct; full integration is the L2 sim test.
"""

from __future__ import annotations

import pytest

from sibb_replay import _swipe_coords_for_finger_direction

pytestmark = pytest.mark.fast


# ────────────────────── Frame fixture ─────────────────────────────────

class _F:
    """Minimal frame stand-in matching the attrs `_swipe_coords_for_finger_direction`
    reads. Mirrors `sibb_scaffold.AXFrame`'s public surface."""
    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.center_x = x + width / 2
        self.center_y = y + height / 2


# ────────────────────── coordinate helper ────────────────────────────

def test_finger_up_drag_starts_near_bottom_ends_near_top():
    f = _F(x=100, y=200, width=300, height=400)
    x1, y1, x2, y2 = _swipe_coords_for_finger_direction(f, "up")
    # X stays centered.
    assert x1 == f.center_x
    assert x2 == f.center_x
    # Y: start near bottom (large y), end near top (small y).
    assert y1 > y2
    # 10% inset on each side.
    assert y1 == pytest.approx(200 + 400 * 0.90)
    assert y2 == pytest.approx(200 + 400 * 0.10)


def test_finger_down_drag_starts_near_top_ends_near_bottom():
    f = _F(x=100, y=200, width=300, height=400)
    x1, y1, x2, y2 = _swipe_coords_for_finger_direction(f, "down")
    assert x1 == f.center_x
    assert x2 == f.center_x
    assert y1 < y2
    assert y1 == pytest.approx(200 + 400 * 0.10)
    assert y2 == pytest.approx(200 + 400 * 0.90)


def test_finger_left_drag_starts_right_ends_left():
    f = _F(x=100, y=200, width=300, height=400)
    x1, y1, x2, y2 = _swipe_coords_for_finger_direction(f, "left")
    # Y stays centered.
    assert y1 == f.center_y
    assert y2 == f.center_y
    assert x1 > x2
    assert x1 == pytest.approx(100 + 300 * 0.90)
    assert x2 == pytest.approx(100 + 300 * 0.10)


def test_finger_right_drag_starts_left_ends_right():
    f = _F(x=100, y=200, width=300, height=400)
    x1, y1, x2, y2 = _swipe_coords_for_finger_direction(f, "right")
    assert y1 == f.center_y
    assert y2 == f.center_y
    assert x1 < x2
    assert x1 == pytest.approx(100 + 300 * 0.10)
    assert x2 == pytest.approx(100 + 300 * 0.90)


def test_invalid_direction_returns_no_movement():
    f = _F(x=100, y=200, width=300, height=400)
    x1, y1, x2, y2 = _swipe_coords_for_finger_direction(f, "spaceship")
    assert (x1, y1) == (x2, y2)


def test_small_element_amplitude_scales_proportionally():
    # Tiny element (e.g. a picker wheel row) — drag amplitude stays
    # inside the element, not bigger than 80% of its dimension.
    f = _F(x=0, y=0, width=50, height=20)
    x1, y1, x2, y2 = _swipe_coords_for_finger_direction(f, "up")
    # Movement in Y between 2 and 18 (10% to 90% of 20).
    assert 2 <= y2 <= 18
    assert 2 <= y1 <= 18
    assert abs(y1 - y2) == pytest.approx(20 * 0.80)


# ────────────────────── FakeXCUITestReader swipe_at ──────────────────

async def test_fake_reader_accepts_swipe_at_command():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({
        "type": "swipe_at",
        "x1": 100.0, "y1": 600.0, "x2": 100.0, "y2": 200.0,
    })
    assert resp["ok"] is True
    assert resp["from"] == [100.0, 600.0]
    assert resp["to"] == [100.0, 200.0]


async def test_fake_reader_swipe_at_rejects_missing_coords():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "swipe_at", "x1": 1.0})
    assert resp["ok"] is False
    assert "required" in resp["error"]


# ────────────────────── ScrollArea unfiltering ────────────────────────

def test_scroll_area_no_longer_in_skip_if_unlabeled():
    """D3 unfilters unlabeled ScrollArea so agents can target it.

    Source-level check rather than constructing a full AX tree —
    SKIP_IF_UNLABELED is a constant inside `_read_xcuitest`.
    """
    import pathlib
    src = pathlib.Path("sibb/benchmark/sibb_scaffold.py").read_text()
    # Find the SKIP_IF_UNLABELED definition.
    idx = src.find("SKIP_IF_UNLABELED = {")
    assert idx > 0
    end = src.find("}", idx)
    block = src[idx:end + 1]
    assert "SCROLL_AREA" not in block, (
        "ElementRole.SCROLL_AREA is back in SKIP_IF_UNLABELED — "
        "agents can no longer see unlabeled scroll containers, "
        "defeating D3's element-targeted SCROLL routing"
    )


# ────────────────────── XCUITestReader.swipe_at exists ────────────────

def test_xcuitest_reader_has_swipe_at():
    import inspect
    import sibb_xcuitest_client as xcc
    assert hasattr(xcc.XCUITestReader, "swipe_at"), (
        "XCUITestReader.swipe_at must be defined for execute() to "
        "route element-targeted swipes"
    )
    assert inspect.iscoroutinefunction(xcc.XCUITestReader.swipe_at)


# ────────────────────── execute() routing source-lint ─────────────────

def test_execute_routes_swipe_through_swipe_at_when_ref_present():
    """The execute() body should call xc.swipe_at when an element is
    resolved, and fall back to xc.swipe when not.
    """
    import pathlib
    src = pathlib.Path("sibb/benchmark/sibb_replay.py").read_text()
    # The swipe and scroll branches must reference swipe_at AND
    # the whole-app fallback.
    swipe_idx = src.find('if a == "swipe":')
    scroll_idx = src.find('if a == "scroll":')
    assert swipe_idx > 0
    assert scroll_idx > 0
    swipe_block = src[swipe_idx:src.find('if a == "scroll":', swipe_idx)]
    scroll_block = src[scroll_idx:src.find('if a == "adjust":', scroll_idx)]
    assert "xc.swipe_at" in swipe_block, (
        "swipe branch should route through xc.swipe_at when element "
        "ref resolves"
    )
    assert "xc.swipe(direction=" in swipe_block, (
        "swipe branch should still fall back to whole-app xc.swipe "
        "when no element ref"
    )
    assert "xc.swipe_at" in scroll_block, (
        "scroll branch should route through xc.swipe_at when element "
        "ref resolves"
    )
    assert "xc.swipe(direction=" in scroll_block, (
        "scroll branch should still fall back to whole-app xc.swipe "
        "when no element ref"
    )
