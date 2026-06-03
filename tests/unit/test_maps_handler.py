"""MapsHandler — L1 tests.

Maps is a content app (real-world place index) without a SIBB-
writable data store. v1 handler is intentionally minimal: it
registers Maps' bundle id, declares its TCC needs, and stubs
apply/reset. These tests pin that contract so future expansion
(v2 favorites/recents inject) can extend without breaking the
canonicalization + registry behavior.
"""

from __future__ import annotations

import pytest

from sibb_state import (
    HANDLERS,
    MapsHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────

def test_maps_handler_registered_by_bundle_id():
    assert MapsHandler.bundle_id == "com.apple.Maps"
    assert HANDLERS[MapsHandler.bundle_id] is MapsHandler


def test_maps_handler_declares_location_tcc_service():
    """Maps needs location TCC for "where am I" / "directions from
    here" features. Without it, every Maps-using task hits the
    "Allow While Using App" dialog mid-episode."""
    assert MapsHandler.tcc_services == ["location"]


def test_maps_handler_is_not_a_pre_runner():
    assert MapsHandler.pre_runner is False
    assert MapsHandler.pre_runner_kinds == []


def test_location_in_collect_tcc_services_union():
    services = collect_tcc_services()
    assert "location" in services


def test_canonicalize_maps_friendly_name():
    assert canonicalize_app("Maps") == "com.apple.Maps"
    assert canonicalize_app("maps") == "com.apple.Maps"


# ─────────────────────────── apply/reset stubs ───────────────────────

async def test_reset_is_noop():
    """No persistent Maps state to wipe; reset is a no-op.
    Future-proof: if v2 adds an apply primitive AND a reset that
    actually wipes state, the no-op test should be replaced.
    """
    h = MapsHandler(reader=None)
    # Calling reset on a None reader must not crash — it shouldn't
    # touch the reader at all in v1.
    await h.reset()


async def test_apply_raises_clear_error_in_v1():
    """v1 has no apply primitive. If a generator tries to seed
    a Maps entry, the handler must raise a clear ValueError
    pointing at the v1 limitation, not silently no-op.
    """
    h = MapsHandler(reader=None)
    with pytest.raises(ValueError, match="no apply primitive"):
        await h.apply({"type": "favorite", "name": "Home"})
