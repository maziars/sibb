"""Structured reader for iOS Maps active-route and directions-history state.

What this gives the verifier
----------------------------
Four entry points:

  read_active_route(udid)    -> dict | None
      Reads ~/Library/.../com.apple.Maps.plist for
      NavigationUserActivityDefault. Returns the parsed active route
      (origin, destination, mode_group, raw-fields) or None when no
      route is active.

  read_directions_history(udid, since=None, limit=20)
      Reads MapsSync_0.0.1's ZHISTORYITEM where Z_ENT=16. Each row is a
      "directions" history entry — what Maps recorded when the user
      requested a route. Returns a list ordered newest-first.

  read_route_violations(udid, max_age_seconds=600)
      Scans recent rstorage files in Caches/com.apple.navd/tmp/planning/
      and returns the route alternatives Maps generated, including any
      "enroute notices" (Steep Climb, Steep Descent, etc.) that indicate
      a stated avoid-preference couldn't be honored. Lets the verifier
      distinguish "agent forgot the preference" from "iOS couldn't
      satisfy the preference at this destination."

  read_maps_user_defaults(udid)
      Reads the labeled user-preference keys from com.apple.Maps.plist
      (MapsDefaultAvoid*Key, MapsTransit*Key, DefaultDisabledTransit-
      ModesKey). These are simpler and more reliable than the request-
      blob protobuf for preference checks — Maps mirrors options-panel
      state to these keys instantly, so the verifier reads them to
      validate "did the agent set avoid_hills?", "did they disable Bus
      for transit?", etc.

Why this exists
---------------
ZNAVIGATIONINTERRUPTED is NULL on iOS 26 sim, so we can't use it to
distinguish "directions requested" from "navigation activated". The
plist NavigationUserActivityDefault is the canonical "is something
active right now?" signal.

Field mapping (iOS 26.3 simulator, empirically)
-----------------------------------------------
The blobs are Apple-internal protobuf (not Apple's public Geo proto).
Field meanings reverse-engineered from controlled captures:

ZROUTEREQUESTSTORAGE (per directions-history row):
  field 1   waypoints (repeated, length-delimited; first=origin,
            last=destination). Inside each:
              5 = display name (e.g., "Transamerica Pyramid")
              6 = address lines (repeated)
              3.1 / 3.2 = lat / lon (double)
  field 2   transport mode-group (varint):
              0 = driving family (driving, possibly transit)
              2 = non-vehicle family (walking, cycling)
  field 3   request UUID (length-delimited; first field = ASCII UUID)
  field 4   driving-family options (length-delimited message):
              1 = leave-now flag (1=now)
              2 = alternatives requested (e.g., 3)
              6.1 = avoid highways (0/1)
              6.2 = avoid tolls (0/1)
  field 6   walk/cycle-family options (length-delimited message):
              3.1, 3.2, 3.3 = three avoid flags (per-mode meaning
              still TBD — calibrate via controlled probe)

NavigationUserActivityDefault (active nav, top-level wrapper):
  field 7   payload. Inside:
              1    waypoints (same shape as request.field 1)
              5    activation timestamp (Apple epoch, double)
              8    transport mode-group (same enum as request.field 2)
              17   request-creation timestamp (Apple epoch, double)

Per-mode option semantics (calibrated against iOS 26.3 sim)
-----------------------------------------------------------
WALKING (field 6.3 inner):
    flag_1 = avoid hills
    flag_2 = avoid busy roads
    flag_3 = avoid stairs
CYCLING (field 6.3 inner — only 2 toggles surface in the UI):
    flag_1 = avoid hills
    flag_2 = avoid busy roads
    (flag_3 sometimes present but not exposed in the UI)
TRANSIT (field 6.3 inner — interpretation differs from walk/cycle):
    flag_1 = leave-at / arrive-by mode
    flag_2 = prefer (bus / subway-or-lightrail / commuter-rail / ferry — bitfield)
    flag_3 = transit-card fares / cash fares

The raw mode_group at field 2 doesn't distinguish walk vs cycle vs
transit on its own — they all share group=2. Distinguishing them
requires either:
  (a) checking active-nav blob's deeper transport-type fields, or
  (b) the verifier passing a `mode_hint` so labels resolve correctly.

The reader returns raw flags + a label-resolver helper
(`label_avoids(blob_avoid_dict, mode_hint)`) the caller can use.

Edges still TBD
---------------
- precise location of leave-at / arrive-by timestamp (only the
  binary flag at 6.3.1 is mapped today; the wall-clock timestamp
  lives elsewhere)
- transit prefer-bitfield decoding (bus / subway / rail / ferry
  mapping to bit positions)

iOS-version stability
---------------------
This schema is iOS-private protobuf, not a public API. What stays
stable across iOS versions:
  - the plist key names (NSUserActivity contract, public)
  - established field numbers (driving avoids — stable since iOS 12+;
    cycling/walking — stable since iOS 14+/15+)
  - protobuf forwards-compat: new fields don't break existing
    parsers (this module ignores unknown fields)
What can change:
  - newer transit / fare / accessibility fields (Apple iterates these)
  - top-level wrapper structure if Apple swaps the NSUserActivity
    payload codec (last did this in iOS 13 — not since)
This reader is calibrated for iOS 26.3. If iOS bumps to 27+ and a
verifier mismatch surfaces, re-run the calibration probe before
trusting the labels.

Returns
-------
read_active_route:
    {
      "is_active": True,
      "destination": {"name": str, "address_lines": [str, ...],
                      "lat": float, "lon": float},
      "origin":      {"name": str, "address_lines": [str, ...],
                      "lat": float, "lon": float},
      "mode_group_raw": int,            # 0 or 2 (so far)
      "mode_group_label": str,          # "driving_family" / "walk_cycle_family" / "unknown"
      "activated_iso": str,             # UTC ISO from Apple epoch
      "raw": dict                       # full parsed-field dict
    }

read_directions_history:
    list of {
      "pk": int, "uuid": str | None, "created_iso": str,
      "destination": ..., "origin": ...,
      "mode_group_raw": int, "mode_group_label": str,
      "avoids_raw": dict,               # field 4 or field 6 parsed dict
      "is_active": bool,                # matches active nav if any
      "raw": dict
    }
"""
from __future__ import annotations

import datetime
import os
import plistlib
import sqlite3
import struct
from typing import Optional


APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01


# Protobuf wire types
_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_LENGTH = 2
_WIRE_FIXED32 = 5


def _varint(b: bytes, i: int):
    v = 0
    shift = 0
    while i < len(b):
        x = b[i]
        i += 1
        v |= (x & 0x7f) << shift
        if not (x & 0x80):
            return v, i
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _parse_message(b: bytes) -> dict:
    """Return {field_number: [list of (wire, value)]}.

    `value` is:
      - int for varint
      - float for fixed64
      - bytes for length-delimited (caller decides whether to recurse)
      - int for fixed32
    """
    out: dict = {}
    i = 0
    while i < len(b):
        try:
            tag, i = _varint(b, i)
        except ValueError:
            break
        field = tag >> 3
        wire = tag & 7
        if wire == _WIRE_VARINT:
            try:
                v, i = _varint(b, i)
            except ValueError:
                break
            out.setdefault(field, []).append((wire, v))
        elif wire == _WIRE_FIXED64:
            if i + 8 > len(b):
                break
            v = struct.unpack("<d", b[i:i+8])[0]
            i += 8
            out.setdefault(field, []).append((wire, v))
        elif wire == _WIRE_LENGTH:
            try:
                l, i = _varint(b, i)
            except ValueError:
                break
            if i + l > len(b):
                break
            v = b[i:i+l]
            i += l
            out.setdefault(field, []).append((wire, v))
        elif wire == _WIRE_FIXED32:
            if i + 4 > len(b):
                break
            v = struct.unpack("<I", b[i:i+4])[0]
            i += 4
            out.setdefault(field, []).append((wire, v))
        else:
            break
    return out


def _is_printable(b: bytes, threshold: float = 0.85) -> bool:
    if not b:
        return False
    n = sum(1 for c in b if 0x20 <= c < 0x7f or c in (9, 10, 13))
    return (n / len(b)) >= threshold


def _decode_string(b: bytes) -> Optional[str]:
    if _is_printable(b):
        try:
            return b.decode("utf-8", "replace")
        except Exception:
            return None
    return None


def _parse_waypoint(blob: bytes) -> dict:
    """Extract name + address lines + lat/lon from a waypoint blob.

    Waypoint shape (iOS 26):
        wp.3.{1,2}   = lat / lon (double)        — the coords block
        wp.1.{1,2}   = also lat / lon            — duplicate, sometimes only origin
        wp.1.2.5     = display name              — destination name
        wp.1.2.6     = address lines (repeated)  — destination addr
        wp.1.2.4     = postal-code blob          — structured address
    """
    msg = _parse_message(blob)
    out: dict = {"name": None, "address_lines": [], "lat": None, "lon": None}

    # ── name + address: descend wp -> 1 -> 2, then look for fields 5/6 ──
    for w1, v1 in msg.get(1, []):
        if w1 != _WIRE_LENGTH:
            continue
        env = _parse_message(v1)
        for w2, v2 in env.get(2, []):
            if w2 != _WIRE_LENGTH:
                continue
            poi = _parse_message(v2)
            # name at field 5
            for sw, sv in poi.get(5, []):
                if sw == _WIRE_LENGTH and out["name"] is None:
                    s = _decode_string(sv)
                    if s:
                        out["name"] = s.strip()
            # address lines at field 6 (repeated)
            for sw, sv in poi.get(6, []):
                if sw == _WIRE_LENGTH:
                    s = _decode_string(sv)
                    if s and s not in out["address_lines"]:
                        out["address_lines"].append(s.strip())

    # ── lat/lon: prefer wp.3.{1,2}, fall back to wp.1.{1,2} ───────────
    for sub_field in (3, 1):
        if out["lat"] is not None:
            break
        for w, v in msg.get(sub_field, []):
            if w == _WIRE_LENGTH:
                inner = _parse_message(v)
                lat = next((iv for iw, iv in inner.get(1, [])
                            if iw == _WIRE_FIXED64), None)
                lon = next((iv for iw, iv in inner.get(2, [])
                            if iw == _WIRE_FIXED64), None)
                if lat is not None and lon is not None:
                    out["lat"] = lat
                    out["lon"] = lon
                    break
    return out


def _label_mode_group(raw):
    """Legacy stub. Field 2 alone is NOT a reliable mode indicator —
    use `_detect_mode` instead. Kept only so existing call sites don't
    crash; returns a best-effort label that the verifier should not
    rely on. Pending replacement after schema research."""
    return f"field2={raw}"


def _detect_mode(msg: dict) -> str:
    """Multi-signal transport-mode detector for ZROUTEREQUESTSTORAGE.

    `field 2` (top-level varint) is NOT a reliable mode indicator —
    empirically it can be 2 for both walking *and* transit-with-default-
    options. Use these signals instead, in priority order:

      1. field 4 present  → driving (carries avoid hwy/tolls)
      2. field 5 present  → transit with explicit options (arrive-by,
                            mode bitfield, fare)
      3. field 7 present  → cycling (avoid hills, busy_roads)
      4. field 6 present  → walking (avoid hills, busy_roads, stairs)
      5. field 3 > 200 B  → transit with default options (Apple stores
                            transit schedule data inline; UUID-only
                            field 3 is ~38 bytes, so a bloated one means
                            the row carries cached schedule data)
      6. otherwise        → "unknown" (could be walking with all-default
                            avoids — fall back to the plist defaults
                            via read_maps_user_defaults)
    """
    if 4 in msg:
        return "driving"
    if 5 in msg:
        return "transit"
    if 7 in msg:
        return "cycling"
    if 6 in msg:
        return "walking"
    f3_entries = msg.get(3, [])
    if f3_entries:
        for w, v in f3_entries:
            if w == _WIRE_LENGTH and len(v) > 200:
                return "transit"
    return "unknown"


# Per-mode label maps. Two physical schemas exist:
#
#   Legacy (mode_group=2): single sub-message at 6.3 with flags 1/2/3.
#       For walking, flag indices 1/2/3 = hills / busy_roads / stairs.
#       For cycling, only 1/2 are exposed in the UI.
#       For transit, 1/2/3 carry leave-or-arrive / prefer / fare info.
#
#   Current (mode_group=3): split into 7.1 and 7.2 messages.
#       7.1.2 = avoid_hills (walk+cycle)
#       7.1.3 = avoid_busy_roads (walk+cycle)
#       7.2.1 = avoid_stairs (walking-only)
#       Transit-specific fields under field 7 still TBD (calibrate
#       once a transit route lands in our sample).
_AVOID_LABELS_LEGACY = {
    "walking": {1: "avoid_hills", 2: "avoid_busy_roads", 3: "avoid_stairs"},
    "cycling": {1: "avoid_hills", 2: "avoid_busy_roads"},
    "transit": {1: "leave_or_arrive", 2: "transit_prefer",
                 3: "fare_card_or_cash"},
}
_AVOID_LABELS_CURRENT = {
    # current schema: keys are tuples (subfield_in_7, flag_in_subfield)
    # — e.g. (1, 2) means 7.1.2.
    "walking": {(1, 2): "avoid_hills", (1, 3): "avoid_busy_roads",
                 (2, 1): "avoid_stairs"},
    "cycling": {(1, 2): "avoid_hills", (1, 3): "avoid_busy_roads"},
    # transit TBD — needs a calibration capture
}


def label_avoids(avoid_record: dict, mode_hint: Optional[str]) -> dict:
    """Resolve raw avoid flags into human-readable labels.

    `avoid_record` is the dict returned by _parse_avoid_options — it
    carries either `{kind:'walk_cycle', avoid:{1:[v], 2:[v], 3:[v]}}`
    (legacy) or `{kind:'walk_cycle_v2', sub1:{...}, sub2:{...}}`
    (current).
    """
    if not avoid_record:
        return {}
    kind = avoid_record.get("kind")
    if kind == "driving":
        return {"avoid_highways": avoid_record.get("highways"),
                "avoid_tolls":    avoid_record.get("tolls")}
    if kind == "walk_cycle":
        # legacy schema
        if mode_hint not in _AVOID_LABELS_LEGACY:
            return {f"flag_{k}": (vs[0] if vs else None)
                    for k, vs in (avoid_record.get("avoid") or {}).items()}
        out = {}
        avoid_dict = avoid_record.get("avoid") or {}
        for k, label in _AVOID_LABELS_LEGACY[mode_hint].items():
            vs = avoid_dict.get(k) or []
            out[label] = vs[0] if vs else 0
        return out
    if kind == "walk_cycle_v2":
        if mode_hint not in _AVOID_LABELS_CURRENT:
            # No labels yet for this mode — emit raw structurally.
            out = {}
            for sub_name in ("sub1", "sub2"):
                for k, vs in (avoid_record.get(sub_name) or {}).items():
                    out[f"{sub_name}.{k}"] = vs[0] if vs else None
            return out
        out = {}
        for (sub, k), label in _AVOID_LABELS_CURRENT[mode_hint].items():
            sub_dict = avoid_record.get(f"sub{sub}") or {}
            vs = sub_dict.get(k) or []
            out[label] = vs[0] if vs else 0
        return out
    return {}


def _apple_epoch_to_iso(secs: Optional[float]) -> Optional[str]:
    if secs is None:
        return None
    try:
        dt = datetime.datetime.fromtimestamp(secs + APPLE_EPOCH_OFFSET,
                                              tz=datetime.timezone.utc)
        return dt.isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_avoid_options(req_msg: dict, mode_label: str) -> dict:
    """Decode the mode-specific option fields. Dispatch on the
    structural mode (driving / walking / cycling / transit) detected
    via `_detect_mode`, not the unreliable top-level field 2.
    """
    if mode_label == "driving":
        # driving family — options live in field 4
        for w, v in req_msg.get(4, []):
            if w == _WIRE_LENGTH:
                inner = _parse_message(v)
                # Decode the avoid sub-message at 4.6
                avoid = {}
                for sw, sv in inner.get(6, []):
                    if sw == _WIRE_LENGTH:
                        avoid_msg = _parse_message(sv)
                        for k, vals in avoid_msg.items():
                            avoid[k] = [vv for _w, vv in vals
                                         if _w == _WIRE_VARINT]
                return {"raw": _serializable(inner), "kind": "driving",
                        "avoid": avoid,
                        "highways": _flag(avoid, 1),
                        "tolls":    _flag(avoid, 2)}
        return {"raw": None, "kind": "driving", "avoid": None,
                "highways": None, "tolls": None}
    if mode_label == "transit":
        # transit — options at field 5 (NOT at 4/6/7).
        # Default-options transit rows have NO field 5; in that case we
        # return a minimal record so the caller knows it's transit.
        # Calibrated against iOS 26.3 probe (arrive-by 5pm, Bus unchecked):
        #   5.6 (repeated varint)  = transit mode preference list (semantics TBD —
        #                             the mode-ID enum hasn't been pinned down)
        #   5.8 (varint)           = likely "leave_now" flag (off if arrive-by set)
        #   5.10.2 (varint)        = fare option (cash vs transit card)
        #   5.11 (varint)          = likely "arrive_by" flag (on if arrive-by set)
        # The arrive-by/leave-at TIMESTAMP itself is in the active-nav
        # plist's `nav.7.5`, not in the request blob.
        for w, v in req_msg.get(5, []):
            if w == _WIRE_LENGTH:
                inner = _parse_message(v)
                modes_list = [vv for w2, vv in inner.get(6, [])
                                if w2 == _WIRE_VARINT]
                f8 = next((vv for w2, vv in inner.get(8, [])
                            if w2 == _WIRE_VARINT), None)
                f11 = next((vv for w2, vv in inner.get(11, [])
                             if w2 == _WIRE_VARINT), None)
                # nested 10.2
                fare = None
                for w2, v2 in inner.get(10, []):
                    if w2 == _WIRE_LENGTH:
                        sub = _parse_message(v2)
                        fare = next((vv for w3, vv in sub.get(2, [])
                                      if w3 == _WIRE_VARINT), None)
                        break
                return {"raw": _serializable(inner), "kind": "transit",
                        "prefer_modes": modes_list,
                        "leave_now_flag": f8,
                        "arrive_by_flag": f11,
                        "fare_setting": fare}
        return {"raw": None, "kind": "transit",
                "prefer_modes": None, "leave_now_flag": None,
                "arrive_by_flag": None, "fare_setting": None}
    if mode_label == "walking":
        # walking — avoids at field 6.3.{1,2,3}: hills / busy / stairs
        for w, v in req_msg.get(6, []):
            if w == _WIRE_LENGTH:
                inner = _parse_message(v)
                avoid_msg_outer = inner.get(3, [])
                avoid: dict = {}
                if avoid_msg_outer:
                    for sw, sv in avoid_msg_outer:
                        if sw == _WIRE_LENGTH:
                            avoid_msg = _parse_message(sv)
                            for k, vals in avoid_msg.items():
                                avoid[k] = [vv for _w, vv in vals
                                             if _w == _WIRE_VARINT]
                return {"raw": _serializable(inner), "kind": "walking",
                        "avoid": avoid,
                        "avoid_hills":      _flag(avoid, 1),
                        "avoid_busy_roads": _flag(avoid, 2),
                        "avoid_stairs":     _flag(avoid, 3)}
        return {"raw": None, "kind": "walking", "avoid": None,
                "avoid_hills": None, "avoid_busy_roads": None,
                "avoid_stairs": None}
    if mode_label == "cycling":
        # cycling — options at field 7 (sub1 = hills/busy, sub2 = stairs)
        for w, v in req_msg.get(7, []):
            if w == _WIRE_LENGTH:
                inner = _parse_message(v)
                sub1: dict = {}
                sub2: dict = {}
                for sw, sv in inner.get(1, []):
                    if sw == _WIRE_LENGTH:
                        msg = _parse_message(sv)
                        for k, vals in msg.items():
                            sub1[k] = [vv for _w, vv in vals
                                        if _w == _WIRE_VARINT]
                for sw, sv in inner.get(2, []):
                    if sw == _WIRE_LENGTH:
                        msg = _parse_message(sv)
                        for k, vals in msg.items():
                            sub2[k] = [vv for _w, vv in vals
                                        if _w == _WIRE_VARINT]
                # Cycling exposes only hills + busy_roads in the UI.
                # 7.2.1 (avoid_stairs slot) is always 0 for cycling.
                return {"raw": _serializable(inner),
                        "kind": "cycling",
                        "sub1": sub1, "sub2": sub2,
                        "avoid_hills":      _flag(sub1, 2),
                        "avoid_busy_roads": _flag(sub1, 3)}
        return {"raw": None, "kind": "cycling",
                "sub1": None, "sub2": None,
                "avoid_hills": None, "avoid_busy_roads": None}
    return {"raw": None, "kind": "unknown", "avoid": None}


def _flag(avoid: dict, k: int) -> Optional[int]:
    """Return 0/1 (or first int) for an avoid flag, None if absent."""
    if k not in avoid or not avoid[k]:
        return None
    return avoid[k][0]


def _serializable(msg: dict) -> dict:
    """Drop bytes values from the parsed-message dict so it's JSON-safe."""
    out: dict = {}
    for k, items in msg.items():
        bucket = []
        for w, v in items:
            if isinstance(v, bytes):
                bucket.append((w, "bytes", len(v)))
            else:
                bucket.append((w, v))
        out[k] = bucket
    return out


def _parse_route_request(blob: bytes) -> dict:
    """Decode a ZROUTEREQUESTSTORAGE blob into a structured record."""
    msg = _parse_message(blob)

    # Waypoints (field 1 repeated, first=origin, last=destination)
    waypoints = []
    for w, v in msg.get(1, []):
        if w == _WIRE_LENGTH:
            waypoints.append(_parse_waypoint(v))

    origin = waypoints[0] if waypoints else None
    dest = waypoints[-1] if len(waypoints) > 1 else None

    # Top-level field 2 — kept as diagnostic; NOT used to determine mode.
    field2_raw = None
    for w, v in msg.get(2, []):
        if w == _WIRE_VARINT:
            field2_raw = v
            break

    mode_label = _detect_mode(msg)

    # UUID (field 3, length-delimited, ASCII inside an inner field 1)
    uuid_str = None
    for w, v in msg.get(3, []):
        if w == _WIRE_LENGTH:
            inner = _parse_message(v)
            for iw, iv in inner.get(1, []):
                if iw == _WIRE_LENGTH:
                    s = _decode_string(iv)
                    if s:
                        uuid_str = s
                        break

    avoids = _parse_avoid_options(msg, mode_label)

    return {
        "origin": origin,
        "destination": dest,
        "mode": mode_label,
        "mode_field2_raw": field2_raw,
        "uuid": uuid_str,
        "avoids": avoids,
        "raw": _serializable(msg),
    }


# ─── plist active-route ──────────────────────────────────────────────────────

def _maps_sandbox_dir(udid: str) -> Optional[str]:
    """Find the com.apple.Maps app-sandbox Data container by walking
    the simulator's Application directory and looking for the Maps plist."""
    home = os.path.expanduser("~")
    base = os.path.join(home, "Library", "Developer", "CoreSimulator",
                         "Devices", udid, "data", "Containers", "Data",
                         "Application")
    if not os.path.isdir(base):
        return None
    for d in os.listdir(base):
        plist = os.path.join(base, d, "Library", "Preferences",
                              "com.apple.Maps.plist")
        if os.path.exists(plist):
            return os.path.join(base, d)
    return None


def _maps_mapssync_path(udid: str) -> Optional[str]:
    sandbox = _maps_sandbox_dir(udid)
    if not sandbox:
        return None
    db = os.path.join(sandbox, "Library", "Maps", "MapsSync_0.0.1")
    return db if os.path.exists(db) else None


import re as _re

# UUIDv1 pattern: 8-4-4-4-12 hex with version nibble = 1 in the 3rd group.
# Matches the format Apple uses for _directionsResponseID.
_UUIDV1_RE = _re.compile(
    rb"([0-9a-f]{8}-[0-9a-f]{4}-1[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def _extract_response_uuid(nav_blob: bytes) -> Optional[str]:
    """Pull the `_directionsResponseID` UUID out of a
    NavigationUserActivityDefault blob.

    Strategy:
      1. Try the known iOS 26.3 positional path: top.7.1.3.1 (an ASCII
         UUID inside a length-delimited length-delimited length-delimited
         field-1 string).
      2. If that fails (Apple shuffled a tag), regex-scan the whole
         blob for the first UUIDv1 pattern. iOS uses v1 (time+MAC)
         exclusively for these IDs, so a v4-pattern false-positive is
         vanishingly unlikely.

    Returns the UUID string, or None if no UUID is found anywhere in
    the blob (signals broken or unsupported schema).
    """
    try:
        top = _parse_message(nav_blob)
        payloads = [v for w, v in top.get(7, []) if w == _WIRE_LENGTH]
        if payloads:
            inner = _parse_message(payloads[0])
            for w, v in inner.get(1, []):
                if w != _WIRE_LENGTH:
                    continue
                outer = _parse_message(v)
                for sw, sv in outer.get(3, []):
                    if sw != _WIRE_LENGTH:
                        continue
                    f3 = _parse_message(sv)
                    for sw2, sv2 in f3.get(1, []):
                        if sw2 == _WIRE_LENGTH:
                            s = _decode_string(sv2)
                            if s and _UUIDV1_RE.match(s.encode()):
                                return s.strip()
    except Exception:
        pass

    # Fallback: regex-scan whole blob. Slower but iOS-version-tolerant.
    m = _UUIDV1_RE.search(nav_blob)
    return m.group(1).decode() if m else None


def _parse_active_nav_blob(blob: bytes) -> Optional[dict]:
    """Parse the NavigationUserActivityDefault value.

    Top-level is a single field 7 wrapping the actual record.
    """
    top = _parse_message(blob)
    payloads = [v for w, v in top.get(7, []) if w == _WIRE_LENGTH]
    if not payloads:
        return None
    inner = _parse_message(payloads[0])

    # Mode group at field 8
    mode_group_raw = None
    for w, v in inner.get(8, []):
        if w == _WIRE_VARINT:
            mode_group_raw = v
            break

    # nav.7.5 (Apple epoch double): the user-set arrive-by timestamp
    # for transit; absent/meaningless for other modes. We surface it
    # ONLY for the transit case to avoid confusion (it persists
    # across mode changes in the same session).
    arrive_by_secs = None
    for w, v in inner.get(5, []):
        if w == _WIRE_FIXED64:
            arrive_by_secs = v
            break

    # Request creation timestamp at field 17 (Apple epoch double)
    created_secs = None
    for w, v in inner.get(17, []):
        if w == _WIRE_FIXED64:
            created_secs = v
            break

    # Waypoints live inside field 1 (length-delimited), which itself
    # contains repeated field 1 (each a waypoint).
    waypoints = []
    for w, v in inner.get(1, []):
        if w == _WIRE_LENGTH:
            outer = _parse_message(v)
            for sw, sv in outer.get(1, []):
                if sw == _WIRE_LENGTH:
                    waypoints.append(_parse_waypoint(sv))
            break

    origin = waypoints[0] if waypoints else None
    dest = waypoints[-1] if len(waypoints) > 1 else None

    return {
        "origin": origin,
        "destination": dest,
        "mode_group_raw": mode_group_raw,
        "mode_group_label": _label_mode_group(mode_group_raw),
        "arrive_by_iso": _apple_epoch_to_iso(arrive_by_secs),
        "created_iso": _apple_epoch_to_iso(created_secs),
        "response_uuid": _extract_response_uuid(blob),
        "raw": _serializable(inner),
    }


# Transit-mode bitfield (DefaultDisabledTransitModesKey).
# Calibrated against iOS 26.3 sim via single-toggle A/B captures.
_TRANSIT_MODE_BITS = {
    0: "bus",
    1: "subway_light_rail",
    2: "commuter_rail",
    3: "ferry",
}


def _decode_disabled_transit_modes(bits: Optional[int]) -> list:
    """Decode the DefaultDisabledTransitModesKey bitfield to a list of
    disabled mode names (empty list = all modes enabled)."""
    if not isinstance(bits, int):
        return []
    return [name for bit, name in _TRANSIT_MODE_BITS.items()
              if bits & (1 << bit)]


def read_maps_user_defaults(udid: str) -> dict:
    """Read the persistent user-preference keys from com.apple.Maps.plist.

    These reflect the user's current settings panel state — independent
    of any active route. The agent can read or write these via Maps'
    options UI, and the verifier can validate "agent set X preference"
    by checking the plist key directly.

    Returns labels keyed by the plist key family. Returns {} if the
    plist isn't readable.
    """
    sandbox = _maps_sandbox_dir(udid)
    if not sandbox:
        return {}
    plist_path = os.path.join(sandbox, "Library", "Preferences",
                               "com.apple.Maps.plist")
    try:
        with open(plist_path, "rb") as f:
            pl = plistlib.load(f)
    except (plistlib.InvalidFileException, OSError):
        return {}

    disabled_bits = pl.get("DefaultDisabledTransitModesKey")
    return {
        # Driving defaults
        "driving_avoid_highways": bool(pl.get("MapsDefaultAvoidHighwaysKey")),
        "driving_avoid_tolls":    bool(pl.get("MapsDefaultAvoidTollsKey")),
        # Cycling defaults
        "cycling_avoid_hills":      bool(pl.get("MapsDefaultAvoidHillsKey")),
        "cycling_avoid_busy_roads": bool(pl.get("MapsDefaultAvoidBusyRoadsKey")),
        "cycling_use_ebike":        bool(pl.get("MapsDefaultUseEbikeKey")),
        # Walking defaults
        "walking_avoid_hills":      bool(pl.get("MapsDefaultWalkingAvoidHillsKey")),
        "walking_avoid_busy_roads": bool(pl.get("MapsDefaultWalkingAvoidBusyRoadsKey")),
        "walking_avoid_stairs":     bool(pl.get("MapsDefaultWalkingAvoidStairsKey")),
        # Transit
        "transit_disabled_modes_bits": disabled_bits,
        "transit_disabled_modes":      _decode_disabled_transit_modes(disabled_bits),
        "transit_show_ic_card_fares":  bool(pl.get("MapsTransitShowICFaresKey")),
        "transit_sort_option":         pl.get("MapsTransitSortOption"),
    }


def read_active_route(udid: str) -> Optional[dict]:
    """Returns the active route (from plist), or None if no active route."""
    sandbox = _maps_sandbox_dir(udid)
    if not sandbox:
        return None
    plist_path = os.path.join(sandbox, "Library", "Preferences",
                               "com.apple.Maps.plist")
    if not os.path.exists(plist_path):
        return None
    try:
        with open(plist_path, "rb") as f:
            pl = plistlib.load(f)
    except (plistlib.InvalidFileException, OSError):
        return None

    pending = pl.get("NavigationUserActivityPendingDeletion")
    if pending:
        return None
    blob = pl.get("NavigationUserActivityDefault")
    if not isinstance(blob, bytes):
        return None

    parsed = _parse_active_nav_blob(blob)
    if not parsed:
        return None
    parsed["is_active"] = True
    return parsed


# ─── ZHISTORYITEM directions history ────────────────────────────────────────

def read_directions_history(udid: str,
                              since_apple_epoch: Optional[float] = None,
                              limit: int = 20) -> list:
    """Return recent direction-request rows (Z_ENT=16) newest-first.

    `since_apple_epoch` filters on ZCREATETIME > since (Apple epoch).
    """
    db_path = _maps_mapssync_path(udid)
    if not db_path:
        return []

    # Open read-only WITHOUT immutable=1 — Maps writes new rows to the
    # WAL file first, and `immutable=1` makes SQLite skip the WAL,
    # which causes fresh rows to be invisible until checkpoint (can
    # be minutes). `mode=ro` alone still respects WAL.
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []

    active = read_active_route(udid)
    active_dest = active and active.get("destination")
    active_origin = active and active.get("origin")

    where = ["Z_ENT=16", "ZROUTEREQUESTSTORAGE IS NOT NULL"]
    params: list = []
    if since_apple_epoch is not None:
        where.append("ZCREATETIME > ?")
        params.append(since_apple_epoch)
    sql = (f"SELECT Z_PK, ZCREATETIME, ZMODIFICATIONTIME, "
            f"ZROUTEREQUESTSTORAGE FROM ZHISTORYITEM "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY Z_PK DESC LIMIT ?")
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    db.close()

    out = []
    for pk, ct, mt, blob in rows:
        if not blob:
            continue
        parsed = _parse_route_request(bytes(blob))
        is_active = False
        if active_dest and parsed["destination"]:
            ad = active_dest
            pd = parsed["destination"]
            # Match by destination lat/lon proximity (within 50 m at SF
            # latitude ≈ 0.0005°). We deliberately do NOT cross-check
            # mode_group because the active-nav plist uses a different
            # mode-group enum than the request blob (e.g. nav.7.8=2
            # corresponds to request.field2=3 for the same route on
            # iOS 26.3). The destination match alone is reliable
            # because Maps doesn't keep multiple active routes.
            if (ad.get("lat") is not None and pd.get("lat") is not None
                and ad.get("lon") is not None and pd.get("lon") is not None):
                if (abs(ad["lat"] - pd["lat"]) < 0.0005
                    and abs(ad["lon"] - pd["lon"]) < 0.0005):
                    is_active = True
        out.append({
            "pk": pk,
            "uuid": parsed["uuid"],
            "created_iso": _apple_epoch_to_iso(ct),
            "modified_iso": _apple_epoch_to_iso(mt),
            "destination": parsed["destination"],
            "origin": parsed["origin"],
            "mode_group_raw": parsed["mode_group_raw"],
            "mode_group_label": parsed["mode_group_label"],
            "avoids": parsed["avoids"],
            "is_active": is_active,
            "raw": parsed["raw"],
        })
    return out


# ─── rstorage-based violation detection ─────────────────────────────────────

# Maps stores each planned route alternative in an .rstorage file under
# `Library/Caches/com.apple.navd/tmp/planning/`. It's a NSKeyedArchiver
# bplist with `$top._route` pointing at the root MapsRoute object.
# Inside, `_enrouteNotices` is an array of notice entries — each one
# wraps a protobuf-encoded message under `_annotation.data.NS.data`
# (and a duplicate at `_enrouteNotice.data.NS.data`). The protobuf
# carries: severity, category enum, and i18n-rendered (label, description)
# string pair. We extract those.

# Known notice categories observed on iOS 26.3 (more will surface as
# we encounter routes that hit them):
_NOTICE_CATEGORY_LABELS = {
    "Steep Climb":   "hill",
    "Steep Descent": "hill",
    "TrafficLight":  "traffic_signal",
    "Stop Sign":     "stop_sign",
    "Toll Road":     "toll",
    "Tunnel":        "tunnel",
    "Bridge":        "bridge",
    "Ferry":         "ferry",
    "Unpaved Road":  "unpaved",
    "Highway":       "highway",
    "Stairs":        "stairs",
    "Busy Road":     "busy_road",
}

# A subset that represent VIOLATIONS of an avoid-preference (vs neutral
# enroute info like traffic signals). The verifier scopes its violation
# checks to this set.
_VIOLATION_CATEGORIES = {
    "hill", "stairs", "busy_road", "toll", "highway", "ferry", "unpaved",
}


def _parse_notice_bytes(blob: bytes) -> Optional[dict]:
    """Decode the protobuf payload inside an enroute-notice entry.

    Two payload formats observed on iOS 26.3:

      (a) Outer wrapper (from `_enrouteNotice.data.NS.data`):
          field 1 (varint) = severity  (1=info, 2=traffic, 3=warning)
          field 6 (varint) = category enum int
          field 7 (length) = inner message

      (b) Inner only (from `_annotation.data.NS.data`):
          (same shape as field 7's value above — no severity / enum wrapper)

    The inner message contains:
          field 3 (length) = strings group with:
              field 1.3 = category label ("Steep Climb")
              field 2.3 = description ("There may be a steep hill ...")
    """
    msg = _parse_message(blob)
    severity = next((v for w, v in msg.get(1, [])
                       if w == _WIRE_VARINT), None)
    category_enum = next((v for w, v in msg.get(6, [])
                            if w == _WIRE_VARINT), None)

    # If field 7 is present → outer wrapper, descend. Else → blob IS the
    # inner message; use it directly.
    inner_candidates = [v for w, v in msg.get(7, [])
                          if w == _WIRE_LENGTH]
    if not inner_candidates:
        inner_candidates = [blob]

    label = None
    description = None
    for inner_blob in inner_candidates:
        inner_msg = _parse_message(inner_blob)
        for sw, sv in inner_msg.get(3, []):
            if sw != _WIRE_LENGTH:
                continue
            strings_grp = _parse_message(sv)
            # label at 3.1.3
            for sw1, sv1 in strings_grp.get(1, []):
                if sw1 == _WIRE_LENGTH:
                    inner = _parse_message(sv1)
                    for sw2, sv2 in inner.get(3, []):
                        if sw2 == _WIRE_LENGTH:
                            s = _decode_string(sv2)
                            if s and label is None:
                                label = s.strip()
            # description at 3.2.3
            for sw1, sv1 in strings_grp.get(2, []):
                if sw1 == _WIRE_LENGTH:
                    inner = _parse_message(sv1)
                    for sw2, sv2 in inner.get(3, []):
                        if sw2 == _WIRE_LENGTH:
                            s = _decode_string(sv2)
                            if s and description is None:
                                description = s.strip()

    if label is None and description is None:
        return None
    category = _NOTICE_CATEGORY_LABELS.get(label, "other")
    return {
        "category": category,
        "label": label,
        "description": description,
        "severity": severity,
        "category_enum": category_enum,
        "is_violation": category in _VIOLATION_CATEGORIES,
    }


def _deref_nskeyed(uid, objects, seen=None):
    """Walk an NSKeyedArchiver $objects array, dereferencing UIDs."""
    import plistlib as _pl
    if seen is None:
        seen = set()
    if isinstance(uid, _pl.UID):
        if uid.data in seen:
            return None
        seen = seen | {uid.data}
        return _deref_nskeyed(objects[uid.data], objects, seen)
    if isinstance(uid, dict):
        # NSDictionary / NSArray wrapper?
        if uid.get("$class") is not None:
            if "NS.objects" in uid:
                items = [_deref_nskeyed(x, objects, seen)
                          for x in uid["NS.objects"]]
                if "NS.keys" in uid:
                    keys_l = [_deref_nskeyed(x, objects, seen)
                                for x in uid["NS.keys"]]
                    return dict(zip(keys_l, items))
                return items
            if "NS.string" in uid:
                return _deref_nskeyed(uid["NS.string"], objects, seen)
            if "NS.data" in uid:
                return uid["NS.data"]
        return {k: _deref_nskeyed(v, objects, seen)
                for k, v in uid.items() if not k.startswith("$")}
    if isinstance(uid, list):
        return [_deref_nskeyed(x, objects, seen) for x in uid]
    return uid


def _extract_notice_bytes(notice_dict: dict) -> Optional[bytes]:
    """Walk a deref'd notice entry to find the encoded protobuf payload.

    `_enrouteNotice` has the outer wrapper (severity + category_enum +
    inner message). `_annotation` has only the inner message. We try
    the wrapped form first because it carries more info; the parser
    handles both.
    """
    if not isinstance(notice_dict, dict):
        return None
    for key in ("_enrouteNotice", "_annotation"):
        v = notice_dict.get(key)
        if isinstance(v, dict):
            data = v.get("data")
            if isinstance(data, dict):
                raw = data.get("NS.data")
                if isinstance(raw, (bytes, bytearray)):
                    return bytes(raw)
            elif isinstance(data, (bytes, bytearray)):
                return bytes(data)
        elif isinstance(v, (bytes, bytearray)):
            return bytes(v)
    return None


def read_route_violations(udid: str,
                            max_age_seconds: float = 600) -> list:
    """Return recent route alternatives + their enroute notices.

    Each entry:
      {
        "rstorage_file": str,
        "mtime_iso": str,
        "transport_type": int,         # Apple enum: 0=driving-segment,
                                       # 2=walking, etc. (not the same
                                       # enum as request.field 2 — see
                                       # below)
        "distance_m": float,
        "expected_time_s": int,
        "avoids_highways": bool,
        "avoids_tolls": bool,
        "notices": [
          {"category": "hill"/"stairs"/...,
           "label": "Steep Climb",
           "description": "There may be a steep hill on 29th St ...",
           "is_violation": bool,
           "severity": int}
        ],
        "violations": [...],           # filtered to is_violation=True
      }

    `transport_type` here is Apple's internal route-segment enum (used
    in rstorage), not the request blob's mode-group enum. Map roughly:
        0 = vehicle/segment (driving)
        2 = walking
        (3, 4, ... TBD)
    """
    import plistlib as _pl
    sandbox = _maps_sandbox_dir(udid)
    if not sandbox:
        return []
    planning = os.path.join(sandbox, "Library", "Caches",
                              "com.apple.navd", "tmp", "planning")
    if not os.path.isdir(planning):
        return []

    import time
    now = time.time()
    files = []
    for fn in os.listdir(planning):
        if not fn.endswith(".rstorage"):
            continue
        p = os.path.join(planning, fn)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if now - mt > max_age_seconds:
            continue
        files.append((mt, p, fn))
    files.sort(reverse=True)

    out = []
    for mt, path, fn in files:
        try:
            with open(path, "rb") as f:
                pl = _pl.load(f)
        except (OSError, _pl.InvalidFileException):
            continue
        objects = pl.get("$objects")
        top = pl.get("$top")
        if not isinstance(objects, list) or not isinstance(top, dict):
            continue
        root_uid = top.get("_route")
        if root_uid is None:
            continue
        root = _deref_nskeyed(root_uid, objects)
        if not isinstance(root, dict):
            continue

        notices_out = []
        for entry in (root.get("_enrouteNotices") or []):
            blob = _extract_notice_bytes(entry)
            if not blob:
                continue
            parsed = _parse_notice_bytes(blob)
            if parsed:
                notices_out.append(parsed)

        violations = [n for n in notices_out if n.get("is_violation")]
        out.append({
            "rstorage_file": fn,
            "mtime_iso": _apple_epoch_to_iso(mt - APPLE_EPOCH_OFFSET),
            "transport_type": root.get("_transportType"),
            "distance_m": root.get("_distance"),
            "expected_time_s": root.get("_expectedTime"),
            "avoids_highways": bool(root.get("_avoidsHighways")),
            "avoids_tolls": bool(root.get("_avoidsTolls")),
            "notices": notices_out,
            "violations": violations,
        })
    return out


# ─── CLI: pretty-print current Maps state ───────────────────────────────────

def _format_route(r: dict, indent: str = "  ",
                    mode_hint: Optional[str] = None) -> str:
    lines = []
    if r.get("destination"):
        d = r["destination"]
        addr = " · ".join(d.get("address_lines") or [])
        lines.append(f"{indent}destination: {d.get('name') or '?'}  ({addr})")
        if d.get("lat") is not None:
            lines.append(f"{indent}             lat={d['lat']:.5f} lon={d['lon']:.5f}")
    if r.get("origin"):
        o = r["origin"]
        addr = " · ".join(o.get("address_lines") or [])
        lines.append(f"{indent}origin:      {o.get('name') or '?'}  ({addr})")
        if o.get("lat") is not None:
            lines.append(f"{indent}             lat={o['lat']:.5f} lon={o['lon']:.5f}")
    lines.append(f"{indent}mode_group:  {r.get('mode_group_raw')} ({r.get('mode_group_label')})")
    av = r.get("avoids") or {}
    if av and av.get("kind"):
        kind = av["kind"]
        if kind == "driving":
            lines.append(f"{indent}avoids:      "
                          f"highways={av.get('highways')} tolls={av.get('tolls')}")
        elif kind == "walking":
            lines.append(f"{indent}avoids:      "
                          f"hills={av.get('avoid_hills')} "
                          f"busy_roads={av.get('avoid_busy_roads')} "
                          f"stairs={av.get('avoid_stairs')}")
        elif kind == "cycling":
            lines.append(f"{indent}avoids:      "
                          f"hills={av.get('avoid_hills')} "
                          f"busy_roads={av.get('avoid_busy_roads')}")
        elif kind == "transit":
            lines.append(f"{indent}transit:     "
                          f"prefer_modes={av.get('prefer_modes')} "
                          f"arrive_by_flag={av.get('arrive_by_flag')} "
                          f"leave_now_flag={av.get('leave_now_flag')} "
                          f"fare={av.get('fare_setting')}")
        else:
            lines.append(f"{indent}avoids_raw:  {av}")
    return "\n".join(lines)


# ─── unified rstorage-backed active-route reader (Phase A++) ─────────────────
#
# Goal: one labeled source — `_transportType`, `_distance`, `_expectedTime`,
# `_avoidsHighways`, `_avoidsTolls`, `_enrouteNotices` — for the route the
# user actually activated.
#
# Pipeline:
#   1. read_active_route(udid) gets `response_uuid` from the plist's
#      NavigationUserActivityDefault (via _extract_response_uuid which
#      has a regex fallback if the protobuf positional path drifts).
#   2. _find_alternatives_for_response walks the planning dir, peeks
#      each rstorage's `_directionsResponseID`, returns the matching
#      group. Two-stage filter: stat() first (cheap mtime check),
#      then peek (cheaper than full deref).
#   3. pick_activated_alt (pure function, fully unit-testable) picks
#      the chosen alternative from the group using rstorage + optional
#      GraphDirections activation proof.
#   4. read_active_route_full composes the above and returns ONE dict
#      with every labeled field the verifier needs.
#
# Critic-driven safety:
#   - cfprefsd freshness handled by caller (verifier polls; backstopped
#     by PendingDeletion check + min_mtime selector)
#   - GraphDirections presence is the activation proof; absence ⇒
#     reason=preview_only (return None or marked record per caller)
#   - Unknown _transportType → label "unknown" (don't fail)
#   - Returns None on absent state, NEVER raises (errors logged in
#     diagnostic field)


# Labeled transport-mode mapping for rstorage's `_transportType` enum.
# Calibrated against iOS 26.3 sim. Unknown values map to "unknown".
_RSTORAGE_TRANSPORT_TYPES = {
    0: "driving",
    1: "transit",
    2: "walking",
    # 3 = cycling (verified empirically in earlier captures); leave
    # commented until re-verified post-Phase B audit
    # 3: "cycling",
}


def _label_transport_type(raw) -> str:
    if not isinstance(raw, int):
        return "unknown"
    return _RSTORAGE_TRANSPORT_TYPES.get(raw, f"unknown({raw})")


def _peek_response_id(path: str) -> Optional[str]:
    """Cheap check: open rstorage just enough to read `_directionsResponseID`
    from the root `GEOComposedRoute`. Returns None on parse failure."""
    try:
        with open(path, "rb") as f:
            pl = plistlib.load(f)
    except (plistlib.InvalidFileException, OSError):
        return None
    objects = pl.get("$objects")
    top = pl.get("$top")
    if not isinstance(objects, list) or not isinstance(top, dict):
        return None
    root_uid = top.get("_route")
    if root_uid is None:
        return None
    try:
        root = _deref_nskeyed(root_uid, objects)
    except Exception:
        return None
    if not isinstance(root, dict):
        return None
    rid = root.get("_directionsResponseID")
    if isinstance(rid, str):
        return rid
    # Could also be NSData (bytes) — extract ASCII UUID inside
    if isinstance(rid, (bytes, bytearray)):
        m = _UUIDV1_RE.search(bytes(rid))
        return m.group(1).decode() if m else None
    return None


def _find_alternatives_for_response(
        udid: str,
        response_uuid: str,
        min_mtime: float = 0.0,
        max_age_seconds: Optional[float] = None,
        ) -> list:
    """Find all rstorage files in the Maps planning dir whose
    `_directionsResponseID` matches `response_uuid`.

    `min_mtime`: epoch-seconds floor. Files with mtime < min_mtime are
    skipped (use baseline.captured_at to scope to the current episode).
    `max_age_seconds`: convenience upper bound; if set, files older
    than `now - max_age_seconds` are skipped too.

    Returns list of dicts: `{path, mtime, root}` — `root` is the
    fully-deref'd `GEOComposedRoute` dict. Sorted by mtime ASC.

    Two-stage filter (per code-quality critic): stat() all files,
    drop by mtime range FIRST, peek `_directionsResponseID` SECOND,
    fully deref ONLY for matches.
    """
    sandbox = _maps_sandbox_dir(udid)
    if not sandbox:
        return []
    planning = os.path.join(sandbox, "Library", "Caches",
                             "com.apple.navd", "tmp", "planning")
    if not os.path.isdir(planning):
        return []

    import time as _time
    now = _time.time()
    floor = max(min_mtime, 0.0)
    ceil_age = max_age_seconds

    # Stage 1: stat + mtime window
    candidates = []
    for fn in os.listdir(planning):
        if not fn.endswith(".rstorage"):
            continue
        p = os.path.join(planning, fn)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt < floor:
            continue
        if ceil_age is not None and (now - mt) > ceil_age:
            continue
        candidates.append((mt, p))

    # Stage 2: peek _directionsResponseID
    matched_paths = []
    for mt, p in candidates:
        rid = _peek_response_id(p)
        if rid == response_uuid:
            matched_paths.append((mt, p))

    # Stage 3: full deref for matches only
    out = []
    for mt, p in matched_paths:
        try:
            with open(p, "rb") as f:
                pl = plistlib.load(f)
        except (plistlib.InvalidFileException, OSError):
            continue
        objects = pl.get("$objects")
        top = pl.get("$top")
        if not isinstance(objects, list) or not isinstance(top, dict):
            continue
        root_uid = top.get("_route")
        if root_uid is None:
            continue
        try:
            root = _deref_nskeyed(root_uid, objects)
        except Exception:
            continue
        if not isinstance(root, dict):
            continue
        out.append({"path": p, "mtime": mt, "root": root})

    out.sort(key=lambda d: d["mtime"])
    return out


def _find_graphdirs_files(udid: str,
                            response_uuid: Optional[str] = None,
                            min_mtime: float = 0.0,
                            ) -> list:
    """Find GraphDirections activation-marker files matching
    `response_uuid` (if given). These are written ONLY when the user
    actually starts navigation (taps Go), so their presence is the
    one-and-only proof-of-activation signal.

    Returns list of {path, mtime}.
    """
    sandbox = _maps_sandbox_dir(udid)
    if not sandbox:
        return []
    gdir = os.path.join(sandbox, "Library", "Maps", "ReportAProblem",
                         "GraphDirections")
    if not os.path.isdir(gdir):
        return []
    out = []
    for fn in os.listdir(gdir):
        p = os.path.join(gdir, fn)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt < min_mtime:
            continue
        if response_uuid is None:
            out.append({"path": p, "mtime": mt})
            continue
        # Match: first embedded UUIDv1 in the protobuf == response_uuid
        try:
            with open(p, "rb") as f:
                data = f.read(4096)  # UUID is in the first KB
        except OSError:
            continue
        m = _UUIDV1_RE.search(data)
        if m and m.group(1).decode() == response_uuid:
            out.append({"path": p, "mtime": mt})
    out.sort(key=lambda d: d["mtime"])
    return out


def pick_activated_alt(candidates: list,
                        graph_dirs: Optional[list] = None,
                        ) -> Optional[dict]:
    """Pure function: pick the activated alternative from a list of
    rstorage candidates sharing a `_directionsResponseID`.

    PRIMARY signal of "user activated a route" is the **caller** having
    successfully read `NavigationUserActivityDefault` from the plist
    (PendingDeletion=False, blob present). That's what `read_active_route_full`
    does upstream. Reaching this picker at all means the route is active.

    GraphDirections file is a SECONDARY confirmation: present only when
    `navd` actually started turn-by-turn nav (real GPS movement may be
    required). On the iOS sim with no real GPS, the file may never
    appear even though the route is "active" from the plist's POV and
    the user sees Maps' nav UI.

    Args:
      candidates: list of {path, mtime, root} dicts. Pre-filtered to
        the matching response group. Pre-sorted by mtime ASC.
      graph_dirs: optional list of {path, mtime} for matching
        GraphDirections files — used as a tiebreaker for alt selection
        and to upgrade the disambiguation reason. NOT a precondition
        for is_activated.

    Returns one of the candidate dicts, augmented with:
      - `disambiguation_reason`: "single" | "graphdirs_confirmed" |
        "mtime_winner"
      - `is_activated`: True whenever candidates are present
        (route is in plist AND we have at least one matching rstorage)
    None if `candidates` is empty.
    """
    if not candidates:
        return None

    has_graphdirs = bool(graph_dirs)

    if len(candidates) == 1:
        winner = dict(candidates[0])
        winner["disambiguation_reason"] = (
            "graphdirs_confirmed" if has_graphdirs else "single")
        winner["is_activated"] = True
        return winner

    # N candidates → pick by mtime-latest (candidates are sorted ASC).
    # On real iOS, Apple touches the chosen alt LAST when the user
    # taps Go; mtime-latest within the group is the best heuristic
    # available without protobuf-RE'ing the GraphDirections payload.
    winner = dict(candidates[-1])
    winner["disambiguation_reason"] = (
        "graphdirs_confirmed" if has_graphdirs else "mtime_winner")
    winner["is_activated"] = True
    return winner


def _rstorage_root_to_record(root: dict) -> dict:
    """Project the relevant labeled fields out of a GEOComposedRoute
    NSKeyedArchiver root into the verifier-facing record."""
    transport_raw = root.get("_transportType")
    # _enrouteNotices may be a list of dicts; reuse the violation
    # parser from `read_route_violations` if present.
    notices = []
    violations = []
    for entry in (root.get("_enrouteNotices") or []):
        blob = _extract_notice_bytes(entry)
        if blob:
            parsed = _parse_notice_bytes(blob)
            if parsed:
                notices.append(parsed)
                if parsed.get("is_violation"):
                    violations.append(parsed)
    return {
        "mode_raw": transport_raw,
        "mode": _label_transport_type(transport_raw),
        "is_walking_only_transit": bool(root.get("_isWalkingOnlyTransitRoute")),
        "distance_m": root.get("_distance"),
        "expected_time_s": root.get("_expectedTime"),
        "avoids_highways": bool(root.get("_avoidsHighways")),
        "avoids_tolls":    bool(root.get("_avoidsTolls")),
        "avoids_traffic":  bool(root.get("_avoidsTraffic")),
        "notices":  notices,
        "violations": violations,
        "directions_response_id": root.get("_directionsResponseID")
            if isinstance(root.get("_directionsResponseID"), str) else None,
    }


def read_active_route_full(udid: str,
                             min_mtime: Optional[float] = None,
                             max_age_seconds: float = 1800.0,
                             ) -> Optional[dict]:
    """Unified labeled-source reader for the currently-active route.

    Composes:
      1. read_active_route(udid) for plist-level destination/origin/
         arrive_by/response_uuid.
      2. _find_alternatives_for_response to get the rstorage group.
      3. _find_graphdirs_files to find the activation marker.
      4. pick_activated_alt to choose the activated alt.
      5. _rstorage_root_to_record to project labeled fields.

    Returns ONE dict combining plist + rstorage fields, OR None if
    no active route exists / no rstorage group matches.

    Args:
      min_mtime: epoch-seconds floor for both rstorage and GraphDirections
        file scans. Pass baseline.captured_at to scope to the current
        episode (defends against stale-state per critic 4).
      max_age_seconds: ceiling for rstorage scan (skip files older
        than this). Default 1800s (30min) catches any plausible
        same-session route while pruning 284-file cruft.

    The returned dict carries `disambiguation_reason` and
    `is_activated` for verifier-side decisions. Never raises;
    returns None on any absent/broken state.
    """
    base = read_active_route(udid)
    if not base:
        return None
    response_uuid = base.get("response_uuid")
    if not response_uuid:
        # Plist had NavigationUserActivityDefault but we couldn't
        # extract a UUID — schema drift signal. Return the partial
        # so verifier can use destination/origin/arrive_by even
        # without rstorage cross-ref.
        base["disambiguation_reason"] = "no_response_uuid"
        base["is_activated"] = False
        return base

    floor = float(min_mtime or 0.0)
    candidates = _find_alternatives_for_response(
        udid, response_uuid,
        min_mtime=floor, max_age_seconds=max_age_seconds)
    graph_dirs = _find_graphdirs_files(
        udid, response_uuid=response_uuid, min_mtime=floor)

    chosen = pick_activated_alt(candidates, graph_dirs)
    if not chosen:
        # No rstorage matched — possibly evicted from cache.
        base["disambiguation_reason"] = "no_matching_rstorage"
        base["is_activated"] = False
        base["alternatives_count"] = 0
        return base

    projection = _rstorage_root_to_record(chosen["root"])
    out = {**base, **projection}
    out["disambiguation_reason"] = chosen.get("disambiguation_reason")
    out["is_activated"] = chosen.get("is_activated", False)
    out["alternatives_count"] = len(candidates)
    out["rstorage_file"] = os.path.basename(chosen.get("path") or "")
    return out


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    mode_hint = None
    if "--mode" in args:
        i = args.index("--mode")
        mode_hint = args[i+1]
        args = args[:i] + args[i+2:]
    udid = args[0] if args else "19B95A95-614A-4ECA-B943-44FDADFD7A9F"

    print(f"=== MAPS USER DEFAULTS (udid={udid}) ===")
    defaults = read_maps_user_defaults(udid)
    if defaults:
        for k, v in defaults.items():
            print(f"  {k}: {v}")

    print(f"\n=== ACTIVE ROUTE (udid={udid}) ===")
    active = read_active_route(udid)
    if not active:
        print("  (no active route)")
    else:
        print(_format_route(active, mode_hint=mode_hint))
        if active.get("arrive_by_iso"):
            print(f"  arrive_by:   {active['arrive_by_iso']}")
        print(f"  created:     {active.get('created_iso')}")

    print(f"\n=== RECENT DIRECTIONS HISTORY (top 5) ===")
    for h in read_directions_history(udid, limit=5):
        flag = " ← ACTIVE" if h["is_active"] else ""
        print(f"\npk={h['pk']}  uuid={h['uuid']}  created={h['created_iso']}{flag}")
        print(_format_route(h, mode_hint=mode_hint))

    print(f"\n=== ROUTE PLANS (last 10 min) ===")
    plans = read_route_violations(udid, max_age_seconds=600)
    if not plans:
        print("  (no recent route plans)")
    for p in plans[:8]:
        vc = len(p["violations"])
        vtag = f"  ⚠ {vc} violation(s)" if vc else ""
        dist = p["distance_m"]
        eta = p["expected_time_s"]
        dist_str = f"{dist/1000:.2f} km" if dist else "?"
        eta_str = f"{eta // 60} min" if eta else "?"
        print(f"\n  {p['rstorage_file'][:30]}  ttype={p['transport_type']} "
                f"dist={dist_str} eta={eta_str}{vtag}")
        if p["violations"]:
            # Group by category and dedupe by label
            by_label = {}
            for v in p["violations"]:
                by_label.setdefault(v["label"], []).append(v)
            for label, vs in by_label.items():
                print(f"    [{vs[0]['category']}] {label}: ×{len(vs)}")
                # show first description
                if vs[0].get("description"):
                    desc = vs[0]["description"]
                    if len(desc) > 100: desc = desc[:97] + "..."
                    print(f"      └─ {desc}")
