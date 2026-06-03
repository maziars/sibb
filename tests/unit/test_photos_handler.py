"""PhotosHandler — L1 + L1.5 tests.

First content-type handler with asymmetric transport: apply goes
host-side (simctl addmedia), reset + list go through the runner
socket (PhotoKit). Tests cover both legs:

- Handler-protocol attribute lints + canonicalization
- PhotoMedia typed spec round-trip
- apply() shells out to `simctl addmedia` with the correct argv
- apply() rejects malformed entries (missing host_path, wrong type)
- reset() calls wipe_photos through the socket
- Resource fetcher routes through list_photos with optional
  client-side filtering
- FakeXCUITestReader handles _inject_photo + list_photos + wipe_photos
  in-memory
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

import sibb_state
from sibb_spec import PhotoMedia, SPEC_TYPES, validate_entry
from sibb_state import (
    HANDLERS,
    PhotosHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


class _UdidStub:
    def __init__(self, udid: str = "FAKE-UDID"):
        self.udid = udid


# ─────────────────────────── handler-protocol lints ──────────────────

def test_photos_handler_registered_by_bundle_id():
    assert PhotosHandler.bundle_id == "com.apple.mobileslideshow"
    assert HANDLERS[PhotosHandler.bundle_id] is PhotosHandler


def test_photos_handler_declares_photos_tcc_service():
    """`photos` TCC service → readWrite access on iOS 14+. Read-only
    or add-only would leave the runner unable to delete during
    wipe_photos. Lock the full-access grant."""
    assert PhotosHandler.tcc_services == ["photos"]


def test_photos_handler_is_not_a_pre_runner():
    assert PhotosHandler.pre_runner is False
    assert PhotosHandler.pre_runner_kinds == []


def test_photos_in_collect_tcc_services_union():
    services = collect_tcc_services()
    assert "photos" in services


def test_canonicalize_photos_friendly_name():
    assert canonicalize_app("Photos") == "com.apple.mobileslideshow"
    assert canonicalize_app("photos") == "com.apple.mobileslideshow"


# ─────────────────────────── PhotoMedia spec dataclass ───────────────

def test_photo_media_spec_registered():
    assert ("Photos", "media") in SPEC_TYPES
    assert SPEC_TYPES[("Photos", "media")] is PhotoMedia


def test_photo_media_minimal_construction():
    m = PhotoMedia(host_path="/tmp/test.png")
    assert m.host_path == "/tmp/test.png"


def test_photo_media_to_dict_canonical_shape():
    m = PhotoMedia(host_path="/tmp/test.jpg")
    assert m.to_dict() == {
        "app": "Photos", "type": "media",
        "host_path": "/tmp/test.jpg",
    }


def test_photo_media_round_trip():
    original = PhotoMedia(host_path="/tmp/x.png")
    back = PhotoMedia.from_dict(original.to_dict())
    assert back == original


def test_validate_entry_accepts_photo_media():
    typed, err = validate_entry({
        "app": "Photos", "type": "media",
        "host_path": "/tmp/test.png",
    })
    assert err is None
    assert isinstance(typed, PhotoMedia)


# ─────────────────────────── handler.apply (simctl addmedia) ─────────

async def _record_addmedia_calls(monkeypatch):
    calls: List[Tuple[str, str]] = []

    async def fake_addmedia(udid, host_path):
        calls.append((udid, host_path))

    monkeypatch.setattr(sibb_state, "_simctl_addmedia", fake_addmedia)
    return calls


async def test_apply_media_shells_to_simctl_addmedia(monkeypatch):
    calls = await _record_addmedia_calls(monkeypatch)
    h = PhotosHandler(reader=_UdidStub("UDID-A"))
    await h.apply({"type": "media", "host_path": "/tmp/img.png"})
    assert calls == [("UDID-A", "/tmp/img.png")]


async def test_apply_raises_when_host_path_missing(monkeypatch):
    """Without host_path, simctl addmedia would crash with a
    confusing error. We validate up-front so the failure message
    points at the entry, not at simctl."""
    await _record_addmedia_calls(monkeypatch)
    h = PhotosHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="host_path"):
        await h.apply({"type": "media"})


async def test_apply_raises_when_reader_has_no_udid():
    h = PhotosHandler(reader=None)
    with pytest.raises(RuntimeError, match="requires a reader"):
        await h.apply({"type": "media", "host_path": "/tmp/x.png"})

    class _NoUdid:
        pass
    with pytest.raises(RuntimeError, match="requires a reader"):
        await PhotosHandler(reader=_NoUdid()).apply(
            {"type": "media", "host_path": "/tmp/x.png"})


async def test_apply_rejects_unknown_entry_kind(monkeypatch):
    await _record_addmedia_calls(monkeypatch)
    h = PhotosHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"type": "album", "name": "Vacation"})


# ─────────────────────────── _simctl_addmedia argv ───────────────────

async def test_addmedia_assembles_correct_subprocess_args(monkeypatch):
    captured: Dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec",
                         fake_create_subprocess_exec)
    await sibb_state._simctl_addmedia("UDID-X", "/tmp/photo.png")
    assert captured["args"] == (
        "xcrun", "simctl", "addmedia", "UDID-X", "/tmp/photo.png",
    )


async def test_addmedia_propagates_subprocess_error(monkeypatch):
    """A bad image file (corrupt JPEG, missing path) makes simctl
    return nonzero. The handler must surface that as RuntimeError
    rather than continuing silently — otherwise verifier-AFTER
    would fail against a missing asset with a misleading
    "photo not found" error instead of the real root cause."""
    async def fake_create_subprocess_exec(*args, **kwargs):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"", b"corrupt media file"
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec",
                         fake_create_subprocess_exec)
    with pytest.raises(RuntimeError, match="corrupt media file"):
        await sibb_state._simctl_addmedia(
            "UDID", "/tmp/broken.jpg")


# ─────────────────────────── handler.reset (socket) ──────────────────

async def test_reset_calls_wipe_photos_via_socket():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    # Seed via the test escape hatch.
    await r._send({"type": "_inject_photo", "media_type": "image"})
    await r._send({"type": "_inject_photo", "media_type": "video"})
    h = PhotosHandler(reader=r)
    await h.reset()
    resp = await r._send({"type": "list_photos"})
    assert resp["photos"] == []


async def test_reset_raises_on_socket_error():
    class FailingReader:
        udid = "FAKE"
        async def _send(self, cmd):
            return {"ok": False, "error": "no photos permission"}
    h = PhotosHandler(reader=FailingReader())
    with pytest.raises(RuntimeError, match="no photos permission"):
        await h.reset()


# ─────────────────────────── fake reader photo ops ───────────────────

async def test_fake_reader_inject_photo_appears_in_list():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "_inject_photo",
                            "media_type": "image",
                            "pixel_width": 100, "pixel_height": 200,
                            "creation_date": "2026-05-16T10:00:00Z"})
    assert resp["ok"] is True
    assert resp["identifier"].startswith("fake-photo-")
    resp = await r._send({"type": "list_photos"})
    assert resp["ok"] is True
    assert len(resp["photos"]) == 1
    p = resp["photos"][0]
    assert p["media_type"] == "image"
    assert p["pixel_width"] == 100
    assert p["creation_date"] == "2026-05-16T10:00:00Z"


async def test_fake_reader_wipe_photos_clears_list():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    for _ in range(3):
        await r._send({"type": "_inject_photo"})
    resp = await r._send({"type": "wipe_photos"})
    assert resp["ok"] is True
    assert resp["removed_photos"] == 3
    resp = await r._send({"type": "list_photos"})
    assert resp["photos"] == []


# ─────────────────────────── resource fetcher ────────────────────────

def test_photos_assets_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "photos.assets" in RESOURCE_FETCHERS


async def test_photos_assets_fetcher_returns_socket_rows():
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    await r._send({"type": "_inject_photo", "media_type": "image"})
    fetcher = RESOURCE_FETCHERS["photos.assets"]
    rows = await fetcher(r, {})
    assert len(rows) == 1
    assert rows[0]["media_type"] == "image"


async def test_photos_assets_fetcher_filters_by_media_type():
    """Selector `media_type=video` should pass through the
    client-side filter and return only video rows."""
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    await r._send({"type": "_inject_photo", "media_type": "image"})
    await r._send({"type": "_inject_photo", "media_type": "video"})
    await r._send({"type": "_inject_photo", "media_type": "image"})
    fetcher = RESOURCE_FETCHERS["photos.assets"]
    rows = await fetcher(r, {"media_type": "video"})
    assert len(rows) == 1
    assert rows[0]["media_type"] == "video"
