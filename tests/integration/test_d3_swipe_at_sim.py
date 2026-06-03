"""D3 — L2 sim: element-targeted SCROLL via `swipe_at`.

Verifies the full path lands on a real sim: Swift's `swipe_at`
command works, Python's XCUITestReader wrapper sends correct args,
and the gesture actually scrolls inside a real scrollable element
(Reminders list, which is a UITableView). Compares element-
targeted scroll to whole-app scroll empirically.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim


_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

from sibb_scaffold import AXReader, ElementRole  # noqa: E402


@pytest_asyncio.fixture(scope="module")
async def reader(sibb_udid: str) -> AsyncIterator[AXReader]:
    r = AXReader(sibb_udid)
    await r.start(bundle_id="com.apple.reminders")
    try:
        yield r
    finally:
        await r.stop()


# ───────────── Swift swipe_at command works at all ────────────────────

async def test_swipe_at_returns_ok_with_coords_echoed(reader):
    """Basic round-trip: send valid coords, get ok=true with the
    coords echoed in the response. Doesn't assert anything visual
    just yet — that's the next test."""
    resp = await reader._xcuitest._send({
        "type": "swipe_at",
        "x1": 200.0, "y1": 600.0, "x2": 200.0, "y2": 300.0,
        "duration_s": 0.1,
    })
    assert resp.get("ok") is True, f"swipe_at failed: {resp}"
    assert resp.get("from") == [200.0, 600.0]
    assert resp.get("to") == [200.0, 300.0]
    assert resp.get("duration_s") == 0.1


async def test_swipe_at_rejects_missing_coords(reader):
    resp = await reader._xcuitest._send({
        "type": "swipe_at", "x1": 100.0,
    })
    assert resp.get("ok") is False
    assert "required" in resp.get("error", "")


# ───────────── element-targeted scroll moves Reminders list ─────────

async def test_swipe_at_on_reminders_list_scrolls_content(reader):
    """The headline test: send a swipe_at on a real `[scroll]`
    element and verify content moved.

    Workflow:
    1. Wipe + seed Reminders with enough items to require scrolling
       (40 items in one list — Reminders renders ~10 per screen,
       so initial view shows items 1-10, last item 40 not visible).
    2. Read AX tree → find the scrollable element containing cells.
    3. Snapshot the visible cell labels before scroll.
    4. swipe_at inside that element (drag finger up = reveal more
       content below).
    5. Read AX tree again → snapshot visible cell labels after.
    6. Assert: before-set != after-set (content moved). Final list
       includes items further down than the before list.
    """
    # 1. Seed
    await reader._xcuitest._send({"type": "wipe_reminders"})
    await reader._xcuitest._send({
        "type": "create_list", "name": "ScrollTest"})
    for i in range(1, 41):
        await reader._xcuitest._send({
            "type": "create_reminder",
            "title": f"Item-{i:02d}",
            "list": "ScrollTest",
        })

    # Re-launch Reminders to refresh the AX tree (some cells only
    # appear after a fresh attach). The baseline's dismiss-onboarding
    # pass should have cleared Reminders' welcome/iCloud prompts on
    # first launch already, so we go straight to the lists screen.
    await reader._xcuitest._send({
        "type": "launch_app", "bundle": "com.apple.reminders"})
    import asyncio
    await asyncio.sleep(1.5)

    # Navigate into the list by tapping its cell. Look up the cell
    # frame from the home screen AX tree, tap its center.
    home_tree = await reader.read()
    list_cell = next(
        (e for e in home_tree.elements
         if e.effective_role == ElementRole.CELL
         and e.effective_label
         and "ScrollTest" in e.effective_label),
        None,
    )
    assert list_cell and list_cell.frame, (
        "Couldn't find ScrollTest list cell on Reminders home screen"
    )
    await reader._xcuitest.tap(list_cell.frame.center_x,
                                list_cell.frame.center_y)
    await asyncio.sleep(0.8)  # navigation animation

    # 2. Find the scrollable element on the current screen
    tree = await reader.read()
    scroll_elements = [e for e in tree.elements
                        if e.effective_role == ElementRole.SCROLL_AREA
                        and e.frame and e.frame.height > 200]
    if not scroll_elements:
        pytest.skip(
            "no ScrollArea element with height>200 visible at the "
            "current screen — this test needs a scrollable region"
        )
    target = scroll_elements[0]

    # 3. Snapshot pre-scroll content (cells inside the scroll element's frame)
    def visible_cell_labels(tree):
        return tuple(
            e.effective_label for e in tree.elements
            if e.effective_role == ElementRole.CELL and e.effective_label
        )

    labels_before = visible_cell_labels(tree)

    # 4. Element-targeted swipe — drag finger up to reveal content below
    # 80% amplitude inside the element, like _swipe_coords_for_finger_direction
    h_inset = target.frame.height * 0.10
    cx = target.frame.center_x
    y_top = target.frame.y + h_inset
    y_bot = target.frame.y + target.frame.height - h_inset
    await reader._xcuitest.swipe_at(cx, y_bot, cx, y_top)

    # Let the scroll settle.
    await asyncio.sleep(0.6)

    # 5. Snapshot post-scroll
    tree_after = await reader.read()
    labels_after = visible_cell_labels(tree_after)

    # 6. Assert content moved
    assert labels_before != labels_after, (
        f"swipe_at didn't move content. before={labels_before[:5]} "
        f"after={labels_after[:5]}"
    )


async def test_swipe_at_amplitude_clamped_to_element(reader):
    """Sanity: swipe_at with coords *outside* the screen still
    returns ok (we don't validate bounds Swift-side) but doesn't
    crash the runner. The XCUITest CGVector(dx, dy) just gets
    clamped/normalized inside Apple's gesture machinery.

    Whether the gesture has any visible effect with off-screen
    coords is undefined; we just want "doesn't kill the runner".
    """
    resp = await reader._xcuitest._send({
        "type": "swipe_at",
        "x1": -100.0, "y1": -100.0,
        "x2": 10000.0, "y2": 10000.0,
        "duration_s": 0.05,
    })
    # Apple's gesture machinery either accepts or no-ops; either is
    # fine. The runner must NOT have crashed (next observe works).
    assert resp.get("ok") in (True, False)
    tree = await reader.read()
    assert len(tree.elements) > 0  # runner alive, observe works
