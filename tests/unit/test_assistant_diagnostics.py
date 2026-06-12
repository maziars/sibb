"""L1 tests for `_tree_diagnostics` — the helper that snapshots the
AX pipeline's per-observation state into the assistant's JSONL turn
log (task #226).

The fields exposed here are the contract between the AX pipeline
(producer in `sibb_scaffold.py`) and post-hoc analysis tools (consumer
of the JSONL). Renames here cascade — pin them with these tests.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "fakes")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "simulator")))

from fake_reader import FakeXCUITestReader            # noqa: E402
from sibb_assistant import _tree_diagnostics            # noqa: E402
from sibb_scaffold import AXReader                      # noqa: E402

pytestmark = pytest.mark.fast


_EXPECTED_KEYS = frozenset({
    "ax_backend",
    "orientation",
    "coord_system_zoomed",
    "zoom_factor",
    "zoom_source",
    "keyboard_visible",
    "keyboard_y_min",
    "accessory_bar_frame",
    "top_chrome_bottom",
    "bottom_chrome_top",
    "kb_filtered_count",
    "viewport_filtered_count",
    "screen_width",
    "screen_height",
})


def _new_reader_with(elements=None, **observe_kwargs):
    fake = FakeXCUITestReader()
    fake.set_observe_response(elements=elements or [], **observe_kwargs)
    reader = AXReader("test-udid")
    reader._xcuitest = fake
    return reader, fake


def test_diagnostics_keyset_is_stable():
    """Adding/removing fields changes the JSONL contract — surface
    drift as a test failure rather than a silent log change."""
    reader, _ = _new_reader_with()
    tree = asyncio.run(reader._read_xcuitest())
    got = _tree_diagnostics(tree)
    assert frozenset(got.keys()) == _EXPECTED_KEYS, (
        f"diagnostics keys drifted; "
        f"missing={_EXPECTED_KEYS - got.keys()}, "
        f"extra={got.keys() - _EXPECTED_KEYS}"
    )


def test_diagnostics_unzoomed_portrait_defaults():
    reader, _ = _new_reader_with()
    tree = asyncio.run(reader._read_xcuitest())
    d = _tree_diagnostics(tree)
    assert d["orientation"] == "portrait"
    assert d["coord_system_zoomed"] is False
    # zoom_factor falls through to None when no signal at all.
    assert d["zoom_source"] is None
    assert d["screen_width"] == 402
    assert d["screen_height"] == 874


def test_diagnostics_landscape_when_wide():
    reader, _ = _new_reader_with(screen_width=874, screen_height=402)
    tree = asyncio.run(reader._read_xcuitest())
    d = _tree_diagnostics(tree)
    assert d["orientation"] == "landscape"
    assert d["screen_width"] == 874


def test_diagnostics_zoom_signal_carries_source_and_factor():
    reader, _ = _new_reader_with(zoom_scale=1.5)
    tree = asyncio.run(reader._read_xcuitest())
    d = _tree_diagnostics(tree)
    assert d["coord_system_zoomed"] is True
    assert d["zoom_source"] == "swift"
    assert d["zoom_factor"] == pytest.approx(1.5)


def test_diagnostics_filter_counts_initialized():
    """kb_filtered_count and viewport_filtered_count must be present
    even when zero — None would mask 'no elements filtered' vs 'field
    absent', a meaningful distinction post-hoc."""
    reader, _ = _new_reader_with()
    tree = asyncio.run(reader._read_xcuitest())
    d = _tree_diagnostics(tree)
    assert d["kb_filtered_count"] == 0
    assert d["viewport_filtered_count"] == 0


def test_diagnostics_chrome_bounds_present():
    """Even with no chrome signals, the derived bounds should reach
    the JSONL so post-hoc 'why did X get filtered' is debuggable."""
    reader, _ = _new_reader_with()
    tree = asyncio.run(reader._read_xcuitest())
    d = _tree_diagnostics(tree)
    assert d["top_chrome_bottom"] is not None
    assert d["bottom_chrome_top"] is not None
    assert d["bottom_chrome_top"] > d["top_chrome_bottom"]


def test_diagnostics_backend_marker_xcuitest():
    """Closes bug-1 from the 6-critic review: without a backend
    marker, an IDB-backed turn looks identical to a Safari turn
    with no zoom signal — both are 12 fields of None. Diagnose
    by emitting ax_backend so the analyst can branch."""
    reader, _ = _new_reader_with()
    tree = asyncio.run(reader._read_xcuitest())
    d = _tree_diagnostics(tree)
    assert d["ax_backend"] == "xcuitest"


def test_diagnostics_backend_marker_none_when_attribute_missing():
    """Defensive: if a future refactor produces a tree without
    ax_backend (e.g. a stubbed tree in some L1.5 fixture), the
    diagnostics should report None for that field rather than
    raising AttributeError. getattr(... None) covers this."""
    from sibb_scaffold import AXTree
    tree = AXTree(elements=[], root=None, udid="x")
    d = _tree_diagnostics(tree)
    assert d["ax_backend"] is None


# ────────────────── SYSTEM_PROMPT header documentation (task #227) ────


def test_system_prompt_documents_landscape_and_auto_zoomed_tags():
    """The agent's reference for the observation header must match
    what `fmt_observation` actually emits. Stale examples confuse the
    agent into treating real tags as noise. This test fails the day
    someone adds a new header tag without updating the prompt."""
    from sibb_assistant import SYSTEM_PROMPT
    assert "LANDSCAPE" in SYSTEM_PROMPT, (
        "SYSTEM_PROMPT must document the LANDSCAPE header tag")
    assert "AUTO-ZOOMED" in SYSTEM_PROMPT, (
        "SYSTEM_PROMPT must document the AUTO-ZOOMED header tag")


def test_system_prompt_header_example_matches_fmt_observation_shape():
    """Sanity: the example string in SYSTEM_PROMPT uses the same
    field names as fmt_observation. Pinned by token, not exact text,
    so cosmetic edits to either side don't desync."""
    from sibb_assistant import SYSTEM_PROMPT
    for token in ("step", "app=", "els=", "kb="):
        assert token in SYSTEM_PROMPT, (
            f"prompt example must include the `{token}` field")
