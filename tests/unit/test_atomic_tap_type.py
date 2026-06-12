"""L1.5 tests for the atomic tap_then_type executor path.

`TYPE @ref "text"` now routes through Swift's atomic
`tap_then_type` command: tap → poll-focus → type. Policy A
(fail-fast): if focus doesn't transfer, no keystrokes are sent
and the executor returns success=False with a diagnostic the
agent can act on.

Tests cover:
  - happy path (focus acquired, text typed)
  - focus_not_acquired (Policy A fail-fast — no keystrokes leak)
  - pre-check: target off-screen → fails before Swift call
  - pre-check: target below keyboard top + already focused →
    routes to raw type_text (no tap, no focus poll)
  - empty text → no-op
  - no @ref → raw typeText to currently-focused element
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

from fake_reader import FakeXCUITestReader     # noqa: E402
from sibb_scaffold import SIBBScaffold, AXReader  # noqa: E402
from sibb_replay import execute                 # noqa: E402


def _el(ref_id, role, label, y=400, height=40, x=10, width=200,
        focused=False, value=None):
    return {
        "ref": ref_id, "role": role, "label": label,
        "value": value or "",
        "frame": {"x": x, "y": y, "width": width, "height": height},
        "enabled": True, "focused": focused, "adjustable": False,
    }


def _build_tree(elements, *, keyboard_frame=None):
    fake = FakeXCUITestReader()
    fake.set_observe_response(elements=elements,
                                keyboard_visible=keyboard_frame is not None)
    if keyboard_frame is not None:
        fake._observe_resp["keyboard_frame"] = keyboard_frame
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return fake, reader, asyncio.run(reader._read_xcuitest())


def test_type_with_ref_happy_path():
    """Focus acquired in <1.5s, text typed, success=True."""
    elements = [_el("e0042", "input", "First name")]
    fake, reader, tree = _build_tree(elements)
    fake.set_tap_then_type_response(ok=True, acquired_ms=180)
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(
        f'TYPE @{ref} "Sarah"')
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["typed"] == "Sarah"
    assert result["focus_acquired_ms"] == 180


def test_type_focus_not_acquired_fails_clean():
    """Policy A — when Swift returns ok=False because focus didn't
    transfer, executor returns success=False with a recovery hint.
    No keystrokes are sent."""
    elements = [_el("e0042", "input", "First name")]
    fake, reader, tree = _build_tree(elements)
    fake.set_tap_then_type_response(
        ok=False, error="focus_not_acquired",
        polled_ms=1500,
        focused_frame={"x": 0, "y": 0, "width": 0, "height": 0})
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(
        f'TYPE @{ref} "Sarah"')
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False
    assert "focus_not_acquired" in result["error"]
    assert "Recovery" in result["error"]
    assert result["polled_ms"] == 1500
    # Critically: no "typed" key — agent must know nothing was sent.
    assert "typed" not in result


def test_type_off_screen_target_fails_before_swift():
    """If the target element's center is off-screen, the executor
    fails immediately without involving Swift. We bypass the
    visibility filter by injecting a focused element (focused exempt
    from the filter) whose frame extends past screen bottom — its
    center then lands at y > screen_height."""
    elements = [_el("e0042", "input", "Off",
                     x=10, y=860, width=200, height=40,
                     focused=True)]  # bottom=900, focused-exempt
    fake, reader, tree = _build_tree(elements)
    # Configure Swift to FAIL the test if reached — proves we
    # short-circuited the pre-check.
    fake.set_tap_then_type_response(ok=False,
                                       error="should not be called")
    assert len(tree.elements) >= 1, (
        "focused-exempt element should bypass the filter")
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(
        f'TYPE @{ref} "x"')
    result = asyncio.run(execute(reader, action, tree))
    # tap_y = 860 + 20 = 880 > 874 → off-screen → pre-check fails
    assert result["success"] is False
    assert "off-screen" in result["error"].lower()
    # Critical: the pre-check must short-circuit BEFORE Swift is reached.
    # Without this assertion the test passes even if tap_then_type ran.
    assert getattr(fake, "tap_then_type_call_count", 0) == 0


def test_type_below_keyboard_focused_routes_to_type_text():
    """A focused element below kb top (only path that reaches the
    executor — visibility filter drops below-kb fields unless they're
    focused) routes to raw type_text instead of being rejected. The
    keyboard already has the field's responder; tap-then-type would
    just hit the keyboard. Task #219."""
    # Field at y=600 (center y=620), kb_top=539 → tap_y=620 > 539
    elements = [_el("e0042", "input", "Phone",
                     y=600, height=40, focused=True)]
    kb_frame = {"x": 0, "y": 539, "width": 402, "height": 335}
    fake, reader, tree = _build_tree(elements, keyboard_frame=kb_frame)
    # Configure tap_then_type to FAIL so we can prove it wasn't called.
    fake.set_tap_then_type_response(
        ok=False, error="should not be called — focused field skips tap")
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(
        f'TYPE @{ref} "x"')
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["typed"] == "x"
    assert "already-focused" in result.get("note", "")
    assert result["kb_y_min_used"] == pytest.approx(539.0)
    # Critical: the focused-routing branch must SKIP the tap entirely.
    assert getattr(fake, "tap_then_type_call_count", 0) == 0


def test_type_empty_text_with_ref_is_noop():
    """TYPE @ref "" is documented as a no-op. The executor must NOT
    tap or send Swift — that would steal focus from whatever the
    agent had focused."""
    elements = [_el("e0042", "input", "First name")]
    fake, reader, tree = _build_tree(elements)
    fake.set_tap_then_type_response(ok=False, error="should not be called")
    ref = tree.elements[0].ref
    # Note: parse_action() returns text=None for the empty-quote form;
    # we mimic that explicitly.
    from sibb_scaffold import AgentAction
    action = AgentAction(action_type="type", target_ref=ref, text=None)
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["typed"] == ""


def test_type_no_ref_routes_to_raw_typetext():
    """When TYPE has no @ref, the executor calls raw type_text on
    whatever's currently focused — no atomic tap, no focus poll."""
    elements = [_el("e0042", "input", "Field", focused=True)]
    fake, reader, tree = _build_tree(elements)
    fake.set_tap_then_type_response(ok=False,
                                       error="should not be called")
    from sibb_scaffold import AgentAction
    action = AgentAction(action_type="type", text="raw")
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["typed"] == "raw"
    # "no @ref provided" diagnostic
    assert "currently-focused" in result.get("note", "")


def _build_tree_with_accessory(elements, *, keyboard_frame,
                                  accessory_bar_frame):
    """Variant of _build_tree that also injects an accessory_bar_frame
    (predictive bar / inputAccessoryView). As of Step 3 (2026-06-06)
    the Python side keeps `accessory_bar_frame` as diagnostic-only;
    `keyboard_y_min` is bare `kb_frame.y` regardless."""
    fake = FakeXCUITestReader()
    fake.set_observe_response(
        elements=elements,
        keyboard_visible=True,
        keyboard_frame=keyboard_frame,
        accessory_bar_frame=accessory_bar_frame,
    )
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return fake, reader, asyncio.run(reader._read_xcuitest())


def test_type_focused_routing_aborts_on_focus_drift():
    """Bug 2 fix (task #230): the agent's tree was captured at observe
    time; between observe and TYPE, focus may have moved. The routing
    branch now re-observes immediately before type_text and aborts if
    the live focused element no longer contains the original tap point.
    Without this guard, keystrokes would leak into whatever's currently
    focused (search bar, address bar, message composer)."""
    elements = [_el("e0042", "input", "Phone",
                     y=600, height=40, focused=True)]
    kb_frame = {"x": 0, "y": 539, "width": 402, "height": 335}
    fake, reader, tree = _build_tree(elements, keyboard_frame=kb_frame)
    # Simulate focus drift: by the time we re-observe, NO element is
    # focused (modal pop, sheet auto-dismiss, prior Return press, etc.).
    fake.set_observe_response(
        elements=[_el("e0042", "input", "Phone",
                       y=600, height=40, focused=False)],
        keyboard_frame=kb_frame)
    fake.set_tap_then_type_response(
        ok=False, error="should not be called — guard must abort first")
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(
        f'TYPE @{ref} "x"')
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is False
    assert result.get("focus_moved") is True
    assert "focus moved" in result["error"].lower()
    # The guard must short-circuit BEFORE the type_text dispatch — and
    # tap_then_type was never an option in this branch.
    assert getattr(fake, "tap_then_type_call_count", 0) == 0


def test_type_kb_y_min_is_bare_kb_top_even_with_accessory_bar():
    """Updated 2026-06-06: the executor's `kb_y_min` is now just
    `kb_frame.y` even when an `accessory_bar_frame` is reported on the
    tree. Pre-fix, kb_y_min was the union (= min(kb_top, acc_top))
    which hid the bar's elements (`Done`/`Next`/`Previous`/prediction
    words) from the agent. Empirical probe proved those elements are
    useful interactive UI.

    Pin the new behavior: a focused field below the kb top still
    routes to type_text (focused-routing); the kb_y_min consumed is
    the bare kb_frame.y, not the bar top."""
    # Focused field WAY below both the bar AND the kb so it survives
    # the visibility filter (focused exemption) and reaches the
    # focused-routing branch.
    elements = [_el("e0042", "input", "Phone",
                     y=700, height=40, focused=True)]
    kb_frame = {"x": 0, "y": 660, "width": 402, "height": 280}
    acc_frame = {"x": 0, "y": 600, "width": 402, "height": 50}
    fake, reader, tree = _build_tree_with_accessory(
        elements, keyboard_frame=kb_frame,
        accessory_bar_frame=acc_frame)
    fake.set_tap_then_type_response(
        ok=False, error="should not be called — focused below kb")
    ref = tree.elements[0].ref
    action = SIBBScaffold(AXReader("test-udid")).parse_action(
        f'TYPE @{ref} "x"')
    result = asyncio.run(execute(reader, action, tree))
    assert result["success"] is True
    assert result["typed"] == "x"
    # New: bare kb_frame.y (660), NOT the accessory union (600).
    assert result["kb_y_min_used"] == pytest.approx(660.0)
    assert getattr(fake, "tap_then_type_call_count", 0) == 0
