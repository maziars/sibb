"""L1 tests for the unified rstorage-backed active-route reader landed
in Phase A++ (`sibb_maps_reader.read_active_route_full` + helpers).

Covers:
  - `pick_activated_alt` pure function (8 disambiguation cases)
  - `_extract_response_uuid` positional path + regex fallback
  - `_rstorage_root_to_record` field projection
  - `_label_transport_type` enum mapping incl. unknown values
  - `_decode_disabled_transit_modes` bitfield decoder

All tests are pure-Python — no simulator needed. The picker is the
key piece because it encapsulates the disambiguation logic flagged
by the 5-critic review.
"""
from __future__ import annotations

import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

from sibb_maps_reader import (  # noqa: E402
    _decode_disabled_transit_modes,
    _extract_response_uuid,
    _label_transport_type,
    _rstorage_root_to_record,
    pick_activated_alt,
)
from sibb_verify import (  # noqa: E402
    BaselineSnapshot,
    ResourceFetchError,
    _expand_runtime_tokens,
)


# ── pick_activated_alt ───────────────────────────────────────────────────────

def test_picker_empty_returns_none():
    """0 candidates → None (caller upstream handles the no-rstorage case)."""
    assert pick_activated_alt([], None) is None
    assert pick_activated_alt([], []) is None


def test_picker_single_alt_with_graphdirs_is_activated():
    """1 candidate + GraphDirections present = activated with confirmation."""
    res = pick_activated_alt(
        [{"path": "a.rstorage", "mtime": 100.0, "root": {"x": "a"}}],
        [{"path": "g", "mtime": 101.0}],
    )
    assert res is not None
    assert res["disambiguation_reason"] == "graphdirs_confirmed"
    assert res["is_activated"] is True
    assert res["root"] == {"x": "a"}


def test_picker_single_alt_no_graphdirs_still_activated():
    """1 candidate but no GraphDirections is STILL activated — reaching
    this picker means the plist active-nav blob was present upstream,
    so the route IS active even without the GraphDirections telemetry
    marker (which may never write on sim without real GPS).
    """
    res = pick_activated_alt(
        [{"path": "a.rstorage", "mtime": 100.0, "root": {}}],
        [],
    )
    assert res is not None
    assert res["disambiguation_reason"] == "single"
    assert res["is_activated"] is True


def test_picker_multi_alt_with_graphdirs_uses_mtime_winner():
    """3 alts: mtime-latest wins; GraphDirections present upgrades the
    reason but doesn't change the pick."""
    res = pick_activated_alt(
        [{"path": "a", "mtime": 100.0, "root": {"x": "a"}},
         {"path": "b", "mtime": 200.0, "root": {"x": "b"}},
         {"path": "c", "mtime": 300.0, "root": {"x": "c"}}],
        [{"path": "g", "mtime": 301.0}],
    )
    assert res is not None
    assert res["disambiguation_reason"] == "graphdirs_confirmed"
    assert res["is_activated"] is True
    assert res["root"] == {"x": "c"}  # mtime=300 wins


def test_picker_multi_alt_no_graphdirs_uses_mtime():
    """Multiple alts without GraphDirections — still activated; pick
    mtime-latest. Reason=mtime_winner indicates GraphDirections wasn't
    available as confirmation."""
    res = pick_activated_alt(
        [{"path": "a", "mtime": 100.0, "root": {"x": "a"}},
         {"path": "b", "mtime": 200.0, "root": {"x": "b"}}],
        [],
    )
    assert res is not None
    assert res["disambiguation_reason"] == "mtime_winner"
    assert res["is_activated"] is True
    assert res["root"] == {"x": "b"}


def test_picker_does_not_mutate_input():
    """The picker returns a NEW dict — original candidate is not mutated.
    Important so the caller can re-use the candidates list."""
    original = {"path": "a", "mtime": 100.0, "root": {"x": "a"}}
    cands = [dict(original)]
    res = pick_activated_alt(cands, [{"path": "g", "mtime": 101.0}])
    assert "disambiguation_reason" not in cands[0]
    assert "is_activated" not in cands[0]
    assert res is not None
    assert res["disambiguation_reason"] == "graphdirs_confirmed"


def test_picker_with_none_graphdirs():
    """None graphdirs arg (not []) is treated same as empty —
    is_activated still True since candidates are present."""
    res = pick_activated_alt(
        [{"path": "a", "mtime": 100.0, "root": {}}],
        None,
    )
    assert res is not None
    assert res["disambiguation_reason"] == "single"
    assert res["is_activated"] is True


def test_picker_carries_through_root_keys():
    """Returned dict includes the original path/mtime/root plus the
    new disambiguation_reason and is_activated flags."""
    res = pick_activated_alt(
        [{"path": "/x/y.rstorage", "mtime": 12345.6, "root": {"_distance": 9999}}],
        [{"path": "g", "mtime": 12346.0}],
    )
    assert res is not None
    assert res["path"] == "/x/y.rstorage"
    assert res["mtime"] == 12345.6
    assert res["root"] == {"_distance": 9999}


# ── _extract_response_uuid ───────────────────────────────────────────────────

def _proto_varint(v: int) -> bytes:
    out = bytearray()
    while v > 0x7f:
        out.append((v & 0x7f) | 0x80)
        v >>= 7
    out.append(v & 0x7f)
    return bytes(out)


def _proto_length_delim(tag: int, payload: bytes) -> bytes:
    # tag with wire type 2 (length-delimited): (tag << 3) | 2
    return _proto_varint((tag << 3) | 2) + _proto_varint(len(payload)) + payload


def test_extract_response_uuid_positional_path():
    """Build a synthetic NavigationUserActivityDefault blob with the
    canonical iOS 26.3 path: top.7 → .1 → .3 → .1 = ASCII UUID."""
    uuid = "12345678-1234-1abc-9def-0123456789ab"
    inner_f1_inner_f3 = _proto_length_delim(1, uuid.encode())  # field 3.1
    inner_f1 = _proto_length_delim(3, inner_f1_inner_f3)        # field 1.3
    nav7 = _proto_length_delim(1, inner_f1)                     # field 7.1
    blob = _proto_length_delim(7, nav7)                         # field 7
    # Add some other top-level varint noise to make it more realistic
    blob = blob + _proto_varint((5 << 3) | 0) + _proto_varint(42)

    got = _extract_response_uuid(blob)
    assert got == uuid


def test_extract_response_uuid_regex_fallback_when_position_changes():
    """If Apple shuffles the tags so positional path fails, the regex
    fallback still pulls the UUID out — defends against iOS-version
    drift (critic 2's biggest concern)."""
    uuid = "abcdef01-2345-1bcd-9ef0-1234567890ab"
    # Wrap the UUID in a structurally different blob — tag 99, not the
    # nested 7.1.3.1 path. Positional decode will fail; regex won't.
    odd_blob = _proto_length_delim(99, uuid.encode()) + b"\x00\x01\x02"
    got = _extract_response_uuid(odd_blob)
    assert got == uuid


def test_extract_response_uuid_no_uuid_returns_none():
    """A blob with NO UUIDv1 pattern returns None — caller can then
    fall back to plist-only state."""
    got = _extract_response_uuid(b"\x08\x01\x10\x02\x18\x03this-is-not-a-uuid")
    assert got is None


def test_extract_response_uuid_rejects_v4_only_format():
    """UUIDv4 (version nibble = 4) is NOT matched — we want v1
    specifically because Apple emits v1 for these IDs (time+MAC)
    and a v4 false-positive elsewhere in the blob would mislead.

    Note: the regex requires version nibble == 1 in group 3.
    """
    v4 = "12345678-1234-4abc-9def-0123456789ab"  # version 4
    blob = v4.encode()
    got = _extract_response_uuid(blob)
    assert got is None


# ── _rstorage_root_to_record ──────────────────────────────────────────────────

def test_rstorage_projection_driving_route():
    """Projection of a typical driving GEOComposedRoute root dict."""
    root = {
        "_transportType": 0,
        "_distance": 5234.5,
        "_expectedTime": 600,
        "_avoidsHighways": True,
        "_avoidsTolls": False,
        "_avoidsTraffic": False,
        "_isWalkingOnlyTransitRoute": False,
        "_enrouteNotices": [],
        "_directionsResponseID": "11111111-2222-1333-8444-555555555555",
    }
    rec = _rstorage_root_to_record(root)
    assert rec["mode_raw"] == 0
    assert rec["mode"] == "driving"
    assert rec["distance_m"] == 5234.5
    assert rec["expected_time_s"] == 600
    assert rec["avoids_highways"] is True
    assert rec["avoids_tolls"] is False
    assert rec["is_walking_only_transit"] is False
    assert rec["notices"] == []
    assert rec["violations"] == []
    assert rec["directions_response_id"] == "11111111-2222-1333-8444-555555555555"


def test_rstorage_projection_walking_only_transit_subtype():
    """`_transportType=2` paired with `_isWalkingOnlyTransitRoute=True`
    is the "walk-only segment of a transit route" subtype. The
    projection surfaces it as `mode=walking` + an explicit flag, so
    verifiers can disambiguate if needed."""
    root = {
        "_transportType": 2,
        "_isWalkingOnlyTransitRoute": True,
        "_distance": 200.0,
        "_expectedTime": 240,
        "_avoidsHighways": False,
        "_avoidsTolls": False,
    }
    rec = _rstorage_root_to_record(root)
    assert rec["mode"] == "walking"
    assert rec["is_walking_only_transit"] is True


def test_rstorage_projection_unknown_transport_type():
    """Unknown enum values map to `unknown(N)` — never crashes,
    never silently coerces to a known mode (critic 2 concern)."""
    root = {"_transportType": 99}
    rec = _rstorage_root_to_record(root)
    assert rec["mode_raw"] == 99
    assert rec["mode"] == "unknown(99)"


def test_rstorage_projection_missing_transport_type():
    """Missing field → mode='unknown' (no raw value to label)."""
    root = {}
    rec = _rstorage_root_to_record(root)
    assert rec["mode_raw"] is None
    assert rec["mode"] == "unknown"


# ── _label_transport_type ─────────────────────────────────────────────────────

def test_label_transport_type_known_values():
    assert _label_transport_type(0) == "driving"
    assert _label_transport_type(1) == "transit"
    assert _label_transport_type(2) == "walking"


def test_label_transport_type_unknown_int():
    assert _label_transport_type(7) == "unknown(7)"


def test_label_transport_type_none():
    assert _label_transport_type(None) == "unknown"


def test_label_transport_type_non_int():
    """A non-int (e.g. a string or float) → 'unknown' rather than crash."""
    assert _label_transport_type("driving") == "unknown"
    assert _label_transport_type(1.5) == "unknown"


# ── _decode_disabled_transit_modes (bitfield) ─────────────────────────────────

def test_disabled_transit_modes_empty_bitfield():
    """0 = no modes disabled."""
    assert _decode_disabled_transit_modes(0) == []


def test_disabled_transit_modes_single_bits():
    """Empirically calibrated: bit 0=bus, 1=subway, 2=commuter, 3=ferry."""
    assert _decode_disabled_transit_modes(1) == ["bus"]
    assert _decode_disabled_transit_modes(2) == ["subway_light_rail"]
    assert _decode_disabled_transit_modes(4) == ["commuter_rail"]
    assert _decode_disabled_transit_modes(8) == ["ferry"]


def test_disabled_transit_modes_combined_bits():
    """5 = 1|4 = bus + commuter_rail."""
    got = set(_decode_disabled_transit_modes(5))
    assert got == {"bus", "commuter_rail"}
    # all four disabled
    got = set(_decode_disabled_transit_modes(15))
    assert got == {"bus", "subway_light_rail", "commuter_rail", "ferry"}


def test_disabled_transit_modes_none():
    """None (key missing in plist) → empty list, not crash."""
    assert _decode_disabled_transit_modes(None) == []


# ── $baseline_epoch runtime sentinel ──────────────────────────────────────────

def test_baseline_epoch_token_expands_to_float():
    """Selector value `$baseline_epoch` resolves to baseline.captured_at
    as a float. Used by `maps.active_route.min_mtime_epoch` to scope
    rstorage scans to the current episode."""
    bl = BaselineSnapshot(captured_at=1717000000.5, resources={})
    out = _expand_runtime_tokens(
        {"require_activated": True, "min_mtime_epoch": "$baseline_epoch"},
        bl,
    )
    assert out == {"require_activated": True, "min_mtime_epoch": 1717000000.5}
    assert isinstance(out["min_mtime_epoch"], float)


def test_baseline_epoch_token_requires_baseline():
    """If the check references `$baseline_epoch` but no baseline was
    captured, fail loudly — silent fallback to 0 would scan all
    stale rstorage cruft."""
    try:
        _expand_runtime_tokens(
            {"min_mtime_epoch": "$baseline_epoch"}, baseline=None)
    except ResourceFetchError as e:
        assert "baseline_epoch" in str(e)
        return
    assert False, "expected ResourceFetchError"


def test_baseline_epoch_and_iso_coexist():
    """Both `$baseline_epoch` (float) and `$baseline_iso` (string) can
    appear in the same selector. Used when a check needs both an
    mtime-floor AND an ISO floor (e.g. cross-referencing rstorage
    with ZHISTORYITEM)."""
    bl = BaselineSnapshot(captured_at=1717000000.0, resources={})
    out = _expand_runtime_tokens(
        {"min_mtime_epoch": "$baseline_epoch",
          "min_create_iso": "$baseline_iso"},
        bl,
    )
    assert isinstance(out["min_mtime_epoch"], float)
    assert isinstance(out["min_create_iso"], str)
    assert out["min_create_iso"].endswith("Z")
