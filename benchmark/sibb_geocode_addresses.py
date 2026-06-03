#!/usr/bin/env python3
"""One-shot: geocode the curated address pools to lat/lon via in-sim
MKLocalSearch, so the verifier can range-check the agent's actual
Maps destination against the expected coord.

Why in-sim (not Mac CLGeocoder): MKLocalSearch on iOS-sim hits the
same Apple backend Maps.app uses at episode runtime. Geocoding here
eliminates SDK-build drift between macOS MapKit and iOS-sim MapKit,
so the design-time coord matches what Maps.app resolves when the
agent searches — variance shrinks to query-string fuzz only,
typically < 30 m for fully-qualified addresses.

Usage:
    /Library/Developer/CommandLineTools/usr/bin/python3 \
        sibb/benchmark/sibb_geocode_addresses.py <UDID>

Output: paste-ready Python source for the new 6-tuple
`_MESSAGE_ADDRESSES = [(street, city, state, postal, lat, lon), ...]`.
"""
from __future__ import annotations
import asyncio
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "simulator")))

from sibb_xcuitest_client import XCUITestReader  # noqa: E402

# The current 4-tuple corpus from sibb_task_generator_v3.py.
_ADDRESSES = [
    ("100 Market Street",          "San Francisco", "CA", "94105"),
    ("350 5th Avenue",             "New York",      "NY", "10118"),
    ("1100 Sunset Boulevard",      "Los Angeles",   "CA", "90026"),
    ("400 Lake Shore Drive",       "Chicago",       "IL", "60611"),
    ("700 Massachusetts Avenue",   "Boston",        "MA", "02118"),
    ("2200 Pike Place",            "Seattle",       "WA", "98101"),
    ("1500 Pennsylvania Avenue",   "Washington",    "DC", "20004"),
    ("250 Howard Street",          "San Francisco", "CA", "94105"),
    ("3300 Lakeshore Avenue",      "Oakland",       "CA", "94610"),
    ("1 Yawkey Way",               "Boston",        "MA", "02215"),
]


async def main(udid: str) -> int:
    reader = XCUITestReader(udid, bundle_id="com.apple.springboard")
    print(f"[geocode] starting SIBBHelper on {udid}...", file=sys.stderr)
    await reader.start()
    print("[geocode] connected", file=sys.stderr)

    results = []
    for street, city, state, postal in _ADDRESSES:
        query = f"{street}, {city}, {state} {postal}"
        resp = await reader._send({"type": "geocode_query", "query": query})
        if not resp.get("ok"):
            print(f"  ✗ {query!r}: {resp.get('error')!r}", file=sys.stderr)
            results.append((street, city, state, postal, None, None,
                            resp.get("error", "?")))
            continue
        lat = float(resp["lat"])
        lon = float(resp["lon"])
        formatted = resp.get("formatted_address", "")
        matches = resp.get("matches_returned", "?")
        print(f"  ✓ {query!r}", file=sys.stderr)
        print(f"      → ({lat:.6f}, {lon:.6f})  "
              f"[{matches} match(es); resolved: {formatted!r}]",
              file=sys.stderr)
        results.append((street, city, state, postal, lat, lon, formatted))

    # ── Pretty-print pasteable Python ────────────────────────────────────
    print()
    print("# Paste into sibb_task_generator_v3.py, replacing")
    print("# the current _MESSAGE_ADDRESSES list:")
    print("_MESSAGE_ADDRESSES = [")
    for street, city, state, postal, lat, lon, formatted in results:
        if lat is None:
            print(f"    # GEOCODE FAILED: {formatted!r}")
            print(f"    ({street!r:30}, {city!r:18}, "
                  f"{state!r}, {postal!r}, None, None),")
        else:
            print(f"    ({street!r:30}, {city!r:18}, "
                  f"{state!r}, {postal!r}, {lat:.6f}, {lon:.6f}),  "
                  f"# {formatted}")
    print("]")

    await reader.stop() if hasattr(reader, "stop") else None
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: sibb_geocode_addresses.py <UDID>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
