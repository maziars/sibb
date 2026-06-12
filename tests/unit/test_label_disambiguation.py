"""L1 tests for `_disambiguate_by_label` in `sibb_replay.py`.

Step 3 made Safari's keyboard accessory bar elements (Previous/Next/
Done/Typing Predictions/prediction words) reach the agent's AX tree.
Pre-Step-3 they were filtered. Two ambiguity classes emerged:

  A. Multi-`Done`: both the sheet/nav confirmation `Done` (top of
     sheet, role=btn) AND the accessory bar `Done` (kb top) are
     visible. `TAP "Done"` previously routed to the sheet Done (the
     only one); now it would non-deterministically route to either.

  B. Common-word prediction collision: prediction words like 'I',
     'The', 'and' come through as `[other]` / `[StaticText]`. A short
     query like `TAP "I"` substring-matches every label containing
     'i' — Visit, Insert, City, etc.

These tests pin the disambiguation rules. Rule 1 fixes A by
preferring candidates outside the accessory_bar_frame. Rule 2 fixes B
by preferring interactive roles for short queries.
"""
from __future__ import annotations

import os
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))

from sibb_scaffold import AXElement, AXFrame, AXTree, ElementRole  # noqa: E402
from sibb_replay import _disambiguate_by_label  # noqa: E402

pytestmark = pytest.mark.fast


def _el(ref, label, y, *, role=ElementRole.BUTTON, x=10, w=40, h=38,
         enabled=True):
    return AXElement(
        ref=ref, label=label,
        role=role,
        frame=AXFrame(x=x, y=y, width=w, height=h),
        enabled=enabled,
    )


def _tree(elements, *, accessory_bar_frame=None,
            keyboard_frame=None):
    t = AXTree(elements=elements, root=None, udid="x")
    t.accessory_bar_frame = accessory_bar_frame
    t.keyboard_frame = keyboard_frame
    return t


# ─── Rule 1 — multi-Done ──────────────────────────────────────────────


def test_multi_done_prefers_sheet_over_accessory_bar():
    """Sheet confirmation Done at top of sheet, accessory bar Done at
    kb top. With kb up, `TAP "Done"` should select the SHEET Done
    (the user's intent — confirming the sheet's action)."""
    sheet_done = _el("e0001", "Done", y=70, role=ElementRole.BUTTON)
    accessory_done = _el("e0099", "Done", y=486, role=ElementRole.BUTTON)
    tree = _tree([sheet_done, accessory_done],
                  accessory_bar_frame={"x": 0, "y": 481,
                                         "width": 402, "height": 48})
    pick = _disambiguate_by_label(
        [sheet_done, accessory_done], "Done", tree)
    assert pick.ref == "e0001", (
        f"Sheet Done should win over accessory Done; got {pick.ref}")


def test_multi_done_falls_back_to_accessory_when_no_sheet_done():
    """When the only `Done` IS the accessory bar Done, return it."""
    accessory_done = _el("e0099", "Done", y=486, role=ElementRole.BUTTON)
    tree = _tree([accessory_done],
                  accessory_bar_frame={"x": 0, "y": 481,
                                         "width": 402, "height": 48})
    # When list length is 1, disambiguate just returns it.
    pick = _disambiguate_by_label([accessory_done], "Done", tree)
    assert pick.ref == "e0099"


def test_multi_done_no_kb_returns_first_candidate():
    """When kb is down (no accessory_bar_frame), behavior is legacy:
    first match wins. Pre-Step-3 behavior preserved."""
    first = _el("e0001", "Done", y=70, role=ElementRole.BUTTON)
    second = _el("e0002", "Done", y=600, role=ElementRole.BUTTON)
    tree = _tree([first, second], accessory_bar_frame=None)
    pick = _disambiguate_by_label([first, second], "Done", tree)
    assert pick.ref == "e0001"


def test_multi_next_uses_same_rule():
    """`Next` is also an accessory bar label that can collide with
    sheet/wizard Next buttons. Same disambiguation."""
    wizard_next = _el("e0001", "Next", y=80, role=ElementRole.BUTTON)
    acc_next = _el("e0099", "Next", y=486, role=ElementRole.BUTTON)
    tree = _tree([wizard_next, acc_next],
                  accessory_bar_frame={"x": 0, "y": 481,
                                         "width": 402, "height": 48})
    pick = _disambiguate_by_label(
        [wizard_next, acc_next], "Next", tree)
    assert pick.ref == "e0001"


# ─── Rule 2 — prediction-word collision ──────────────────────────────


def test_short_query_prefers_interactive_role():
    """`TAP "I"` should select the City input field, NOT the
    prediction word 'I' (role=OTHER) that happens to substring-match.
    Pre-Step-3, the prediction word was filtered out so the conflict
    didn't exist. Post-Step-3 it can poison short queries."""
    prediction = _el("e0099", "I", y=539,
                      role=ElementRole.OTHER, w=134, h=44)
    city_input = _el("e0042", "City",
                      y=300, role=ElementRole.TEXT_FIELD,
                      w=200, h=40)
    tree = _tree([prediction, city_input])
    pick = _disambiguate_by_label(
        [prediction, city_input], "I", tree)
    assert pick.ref == "e0042", (
        f"interactive City input must beat prediction word 'I'; "
        f"got {pick.ref}")


def test_short_query_falls_back_when_no_interactive():
    """If NO candidate has an interactive role, the first wins
    (legacy behavior)."""
    pred_a = _el("e0001", "I", y=539, role=ElementRole.OTHER)
    pred_b = _el("e0002", "I'm", y=539, role=ElementRole.OTHER)
    tree = _tree([pred_a, pred_b])
    pick = _disambiguate_by_label([pred_a, pred_b], "I", tree)
    assert pick.ref == "e0001"


def test_long_query_does_not_trigger_rule_2():
    """Rule 2 only applies to short (<=3 char) queries. A longer
    target like `TAP "Submit"` should NOT reorder candidates."""
    # Static text "Submit" at the top, button "Submit" below.
    text_submit = _el("e0001", "Submit", y=70,
                       role=ElementRole.STATIC_TEXT)
    btn_submit = _el("e0002", "Submit", y=400,
                      role=ElementRole.BUTTON)
    tree = _tree([text_submit, btn_submit])
    pick = _disambiguate_by_label(
        [text_submit, btn_submit], "Submit", tree)
    assert pick.ref == "e0001", (
        "long queries should NOT trigger role-preference rule; "
        "legacy first-match wins")


# ─── Single-candidate and empty cases ────────────────────────────────


def test_single_candidate_returns_it():
    el = _el("e0001", "Done", y=70)
    tree = _tree([el])
    pick = _disambiguate_by_label([el], "Done", tree)
    assert pick is el


def test_empty_candidates_returns_none():
    tree = _tree([])
    pick = _disambiguate_by_label([], "Done", tree)
    assert pick is None
