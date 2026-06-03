#!/usr/bin/env python3
"""
Safari AX-readability probe across website archetypes.
======================================================

Walks Safari on the simulator through a fixed set of representative
URLs (plain HTML, search engine, link-heavy aggregator, modern SPA,
canvas-rendered map, video site, etc.) and dumps the AX tree for
each. The result populates the empirical table in
`IOS_SIM_QUIRKS.md` §14.

When to re-run
--------------
Every iOS major version bump. WebKit accessibility has shifted
between iOS 17 → 18 → 26; the body-text summarization behavior
and the exposure of dynamic widgets may tighten or loosen. If
§14 starts feeling stale, re-run this and update the table.

Usage
-----
    SIBB_UDID=<udid> ./sibb_probe_safari_ax.py
    # or
    /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb_probe_safari_ax.py <udid>

The sim must be booted. No keychain / TCC dependencies — Safari
just loads each URL and we read the AX tree.

Output goes to stdout in a format suitable for pasting into a
table or diff'ing against the docs.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections import Counter
from typing import List, Tuple

# Co-located with the rest of the benchmark code; sibb_mock_site
# provides the `open_in_safari` shim and sibb_scaffold the AXReader.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "simulator"))

from sibb_mock_site import open_in_safari  # noqa: E402
from sibb_scaffold import AXReader  # noqa: E402


# The probe set. Each entry is (url, archetype label). Keep it
# small enough to run in ~2 minutes — 8 sites at ~8 s each.
# Cover: plain HTML, content article, search, link-heavy news,
# modern SPA, canvas (map), paywalled news, video.
PROBES: List[Tuple[str, str]] = [
    ("http://example.com",                  "plain-html baseline"),
    ("https://en.wikipedia.org/wiki/IOS",   "content article"),
    ("https://duckduckgo.com",              "search homepage"),
    ("https://news.ycombinator.com",        "minimal news (link-heavy)"),
    ("https://github.com/torvalds/linux",   "modern SPA"),
    ("https://www.google.com/maps",         "canvas / map archetype"),
    ("https://www.nytimes.com",             "news with paywall banner"),
    ("https://www.youtube.com",             "video widget"),
]

# Time to wait for a page to load + JS to settle before reading
# the AX tree. 7-8 s is enough for everything in the probe set on
# a warm sim; bump to 12 s on a cold sim or on slow networks.
SETTLE_SECONDS = 7.5

SAFARI_BUNDLE = "com.apple.mobilesafari"


async def probe(reader: AXReader, udid: str,
                 url: str, label: str) -> None:
    subprocess.run(
        ["xcrun", "simctl", "terminate", udid, SAFARI_BUNDLE],
        capture_output=True, timeout=5)
    await asyncio.sleep(0.8)
    open_in_safari(udid, url)
    await asyncio.sleep(SETTLE_SECONDS)
    tree = await reader.read()

    elems = tree.elements
    role_count = Counter(e.effective_role.value for e in elems)
    by_role = {}
    for e in elems:
        by_role.setdefault(e.effective_role.value, []).append(e)

    print(f"\n────────────── {label}: {url}")
    print(f"  total elements: {len(elems)}")
    print(f"  roles: {dict(role_count.most_common())}")

    def sample(role: str, n: int = 4) -> None:
        for e in by_role.get(role, [])[:n]:
            lab = (e.effective_label or "")[:55].replace("\n", " ")
            val = (getattr(e, "value", None) or "")[:25].replace("\n", " ")
            print(f"    [{role:11s}] label={lab!r:57s} val={val!r}")

    for role in ["StaticText", "Link", "Button", "TextField",
                  "SearchField", "Image", "Other"]:
        if role in role_count:
            sample(role)

    has_text_inputs = any(
        e.effective_role.value
        in ("TextField", "SecureTextField", "SearchField")
        for e in elems)
    print(f"  has_form_inputs: {has_text_inputs}")


async def main() -> None:
    udid = os.environ.get("SIBB_UDID") or (
        sys.argv[1] if len(sys.argv) > 1 else None)
    if not udid:
        print("usage: SIBB_UDID=<udid> sibb_probe_safari_ax.py", file=sys.stderr)
        print("   or: sibb_probe_safari_ax.py <udid>", file=sys.stderr)
        sys.exit(2)

    reader = AXReader(udid)
    await reader.start(bundle_id=SAFARI_BUNDLE)
    await asyncio.sleep(1.0)
    try:
        for url, label in PROBES:
            try:
                await probe(reader, udid, url, label)
            except Exception as e:  # pragma: no cover — diagnostic probe
                print(f"\n!! {label} FAILED: {e!r}")
    finally:
        await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
