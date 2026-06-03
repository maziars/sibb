"""PhotosHandler — L2 sim integration.

Verifies the asymmetric transport: host-side `simctl addmedia`
actually puts a PHAsset where runner-side PhotoKit can see and
delete it. Covers:
1. Swift list_photos shape matches Python's expectations
2. simctl addmedia → PHAsset is enumerable
3. Wipe semantics actually clear the photo library (and the iOS
   confirmation-dialog tap-through works)
4. Resource fetcher round-trip
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim

_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
for p in (_SIM_DIR, _BENCHMARK_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sibb_scaffold import AXReader  # noqa: E402
from sibb_state import PhotosHandler, _simctl_addmedia  # noqa: E402


_FIXTURE_PNG = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_pixel.png"


@pytest_asyncio.fixture(scope="module")
async def reader(sibb_udid: str) -> AsyncIterator[AXReader]:
    r = AXReader(sibb_udid)
    await r.start(bundle_id="com.apple.springboard")
    try:
        # Photo wipe is slow + may dialog-tap-through; do it once at
        # entry so each test starts clean.
        await r._xcuitest._send({"type": "wipe_photos"})
        yield r
    finally:
        # Best-effort teardown — leaving a photo behind is annoying
        # but not fatal; the clone is deleted by the harness anyway.
        try:
            await r._xcuitest._send({"type": "wipe_photos"})
        except Exception:
            pass
        await r.stop()


# ────────────────────── Swift command shapes ────────────────────────

async def test_list_photos_empty_on_clean_library(reader):
    """Baseline clone has zero photos — list_photos returns []
    rather than failing or returning system-injected media."""
    resp = await reader._xcuitest._send({"type": "list_photos"})
    assert resp.get("ok") is True
    assert resp.get("photos") == []


async def test_addmedia_makes_photo_visible_to_photokit(reader, sibb_udid):
    """End-to-end: host shellout puts a PHAsset where the runner's
    PHAsset.fetchAssets can find it."""
    assert _FIXTURE_PNG.exists(), f"fixture missing: {_FIXTURE_PNG}"
    await _simctl_addmedia(sibb_udid, str(_FIXTURE_PNG))

    resp = await reader._xcuitest._send({"type": "list_photos"})
    assert resp.get("ok") is True
    photos = resp.get("photos", [])
    assert len(photos) == 1, (
        f"expected one photo after addmedia, got {photos}"
    )
    p = photos[0]
    assert p["media_type"] == "image"
    assert p["pixel_width"] == 1
    assert p["pixel_height"] == 1
    assert p["identifier"]  # PhotoKit assigns local identifier


async def test_addmedia_via_handler_apply(reader, sibb_udid):
    """The handler.apply path should produce the same outcome as
    raw _simctl_addmedia — verify by running addmedia twice (once
    via handler, once direct) and counting."""
    await reader._xcuitest._send({"type": "wipe_photos"})
    h = PhotosHandler(reader=reader._xcuitest)
    # Reader needs udid for the shellout — wire it up.
    h.reader.udid = sibb_udid
    await h.apply({"type": "media", "host_path": str(_FIXTURE_PNG)})

    resp = await reader._xcuitest._send({"type": "list_photos"})
    assert len(resp.get("photos", [])) == 1


async def test_wipe_photos_clears_library(reader, sibb_udid):
    """Seed two photos, wipe, verify empty. Exercises the
    PHAssetChangeRequest path + the SpringBoard confirmation-dialog
    tap-through that wipe_photos performs internally."""
    await reader._xcuitest._send({"type": "wipe_photos"})
    await _simctl_addmedia(sibb_udid, str(_FIXTURE_PNG))
    await _simctl_addmedia(sibb_udid, str(_FIXTURE_PNG))

    resp = await reader._xcuitest._send({"type": "list_photos"})
    assert len(resp.get("photos", [])) == 2

    resp = await reader._xcuitest._send({"type": "wipe_photos"})
    assert resp.get("ok") is True, f"wipe failed: {resp}"
    assert resp.get("removed_photos", 0) >= 2

    resp = await reader._xcuitest._send({"type": "list_photos"})
    assert resp.get("photos", []) == []


# ────────────────────── handler + fetcher round-trip ────────────────

async def test_handler_apply_then_fetcher_round_trip(reader, sibb_udid):
    """The verifier-AFTER loop end-to-end."""
    from sibb_verify import RESOURCE_FETCHERS

    await reader._xcuitest._send({"type": "wipe_photos"})
    h = PhotosHandler(reader=reader._xcuitest)
    h.reader.udid = sibb_udid
    await h.apply({"type": "media", "host_path": str(_FIXTURE_PNG)})

    fetcher = RESOURCE_FETCHERS["photos.assets"]
    rows = await fetcher(reader._xcuitest, {})
    assert len(rows) == 1


async def test_handler_reset_clears_via_handler_api(reader, sibb_udid):
    h = PhotosHandler(reader=reader._xcuitest)
    h.reader.udid = sibb_udid
    await h.apply({"type": "media", "host_path": str(_FIXTURE_PNG)})
    await h.reset()
    resp = await reader._xcuitest._send({"type": "list_photos"})
    assert resp.get("photos") == []
