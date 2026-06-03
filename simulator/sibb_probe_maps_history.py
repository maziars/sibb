#!/usr/bin/env python3
"""
Maps.app ZHISTORYITEM write-trigger probe.
==========================================

Question: which Maps.app interactions write rows into
`~/Library/.../Maps/MapsSync_0.0.1 → ZHISTORYITEM`?

The `_maps_history` fetcher in our plan reads that table. The
plan ASSUMED a search-only flow writes the row, but this was
never validated. We need to know:

  - Which openurl flavors (?q=, ?ll=, ?daddr=) write the row?
  - Does the row land at openurl time, or only after a UI tap?
  - What's the ZTYPE numerical value across flavors?
  - Are there sibling tables (ZSEARCHHISTORYITEM? ZUSERROUTE?
    ZVISIT?) that get hit instead/also?

Run BEFORE shipping `gen_maps_search_to_contact`. Keeps Maps
state intact between scenarios (snapshot diffs only).

Usage
-----
    /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb/simulator/sibb_probe_maps_history.py [<udid>]

Defaults to the SIBB-Demo UDID. No wipe — DOES NOT mutate
Maps state beyond what the probed interactions naturally do.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader  # noqa: E402

DEFAULT_UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"
HOME = os.path.expanduser("~")

# Tables that PLAUSIBLY get hit on user interactions. Even though
# the fetcher only reads ZHISTORYITEM, we want to know what ELSE
# gets written so the team can pick the right verifier surface.
WATCH_TABLES = [
    "ZHISTORYITEM",
    "ZUSERROUTE",
    "ZVISIT",
    "ZVISITEDLOCATION",
    "ZFAVORITEITEM",
    "ZREVIEWEDPLACE",
    "ZCACHEDMAPITEMSTORAGE",
    "ZMIXINMAPITEM",
    "ZCONTACTHANDLE",
    "ZCOLLECTIONITEM",
]


def db_path(udid: str) -> str:
    """Resolve Maps.app's *real* MapsSync DB inside its data container.

    NOTE: There is ALSO an outer ~/Library/.../data/Library/Maps/MapsSync_0.0.1
    copy. Empirically (2026-05-24) that copy stays at 0 rows; Maps.app
    writes only to its container DB. The `_maps_history` fetcher MUST
    point at this container-resolved path, not the outer one.
    """
    proc = subprocess.run(
        ["xcrun", "simctl", "get_app_container", udid, "com.apple.Maps", "data"],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"get_app_container com.apple.Maps failed: {proc.stderr.strip()}"
        )
    container = proc.stdout.strip()
    return os.path.join(container, "Library", "Maps", "MapsSync_0.0.1")


def open_db(udid: str) -> sqlite3.Connection:
    # Open without URI to avoid issues with path encoding. We don't
    # write — sqlite3.connect in default mode is fine.
    conn = sqlite3.connect(db_path(udid))
    conn.row_factory = sqlite3.Row
    return conn


def list_tables(conn: sqlite3.Connection) -> List[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [r[0] for r in cur.fetchall()]


def table_pk_set(conn: sqlite3.Connection, table: str) -> Set[int]:
    """Return set of Z_PK rowids currently in the table."""
    try:
        cur = conn.execute(f"SELECT Z_PK FROM {table}")
        return {r[0] for r in cur.fetchall()}
    except sqlite3.OperationalError:
        return set()


def column_names(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def fetch_rows(conn: sqlite3.Connection, table: str,
               pks: Set[int]) -> List[Dict[str, Any]]:
    if not pks:
        return []
    cols = column_names(conn, table)
    placeholders = ",".join(["?"] * len(pks))
    cur = conn.execute(
        f"SELECT * FROM {table} WHERE Z_PK IN ({placeholders})",
        list(pks),
    )
    out = []
    for row in cur.fetchall():
        d = {}
        for c in cols:
            v = row[c]
            if isinstance(v, bytes):
                d[c] = f"<BLOB len={len(v)}>"
            else:
                d[c] = v
        out.append(d)
    return out


def snapshot(udid: str) -> Dict[str, Set[int]]:
    """Snapshot Z_PK sets for all watched tables."""
    conn = open_db(udid)
    try:
        existing = set(list_tables(conn))
        snap = {}
        for t in WATCH_TABLES:
            if t in existing:
                snap[t] = table_pk_set(conn, t)
            else:
                snap[t] = set()
        return snap
    finally:
        conn.close()


def diff_snapshots(udid: str,
                    before: Dict[str, Set[int]],
                    after: Dict[str, Set[int]]) -> Dict[str, List[Dict]]:
    """Return per-table list of newly-added rows."""
    conn = open_db(udid)
    try:
        result: Dict[str, List[Dict]] = {}
        for t in WATCH_TABLES:
            new_pks = after.get(t, set()) - before.get(t, set())
            if new_pks:
                result[t] = fetch_rows(conn, t, new_pks)
        return result
    finally:
        conn.close()


def pp_row(row: Dict[str, Any], drop_zero: bool = True) -> str:
    """Pretty-print a row, dropping zero/empty columns by default."""
    parts = []
    for k, v in row.items():
        if drop_zero and (v == 0 or v == "" or v is None):
            continue
        parts.append(f"{k}={v!r}")
    return "  " + "\n  ".join(parts) if parts else "  <all zero/empty>"


def openurl(udid: str, url: str) -> None:
    proc = subprocess.run(
        ["xcrun", "simctl", "openurl", udid, url],
        capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        print(f"  [WARN] openurl failed: {proc.stderr.strip()}")


def go_home_sync(udid: str) -> None:
    """Press hardware home to back out of Maps WITHOUT terminating it.
    `simctl terminate com.apple.Maps` empirically kills the XCUITest
    runner's attached XCUIApplication and the socket dies with a
    broken pipe. Pressing home leaves Maps suspended, and the next
    openurl wakes it back up — which is what we want for diffing."""
    subprocess.run(
        ["xcrun", "simctl", "ui", udid, "appearance", "light"],
        capture_output=True, text=True, timeout=5,
    )


async def go_home(reader: XCUITestReader) -> None:
    try:
        await reader.press("home")
    except Exception:
        pass


def report_scenario(name: str,
                     before: Dict[str, Set[int]],
                     after: Dict[str, Set[int]],
                     udid: str) -> Dict[str, List[Dict]]:
    print(f"\n=== Scenario: {name} ===")
    diff = diff_snapshots(udid, before, after)
    if not diff:
        # Counts even if no diff — sanity-check.
        for t in WATCH_TABLES:
            b, a = len(before.get(t, set())), len(after.get(t, set()))
            if b or a:
                print(f"  {t}: {b} → {a} (no delta)")
        print("  >> NO new rows in any watched table.")
        return diff
    for t in WATCH_TABLES:
        b, a = len(before.get(t, set())), len(after.get(t, set()))
        d = len(diff.get(t, []))
        if d > 0:
            print(f"  {t}: {b} → {a}  (+{d} new)")
            for i, row in enumerate(diff[t]):
                print(f"  -- row {i+1} (Z_PK={row.get('Z_PK')}) --")
                print(pp_row(row))
        elif b != a:
            print(f"  {t}: {b} → {a} (delta {a-b}, no NEW pks?)")
    return diff


async def scenario_1_daddr(reader: XCUITestReader, udid: str) -> Dict:
    print("\n[S1] openurl ?daddr= (route)")
    await asyncio.sleep(2)
    before = snapshot(udid)
    openurl(udid, "http://maps.apple.com/?daddr=1+Apple+Park+Way,+Cupertino,+CA")
    await asyncio.sleep(8)
    after = snapshot(udid)
    return report_scenario("openurl ?daddr=", before, after, udid)


async def scenario_2_q(reader: XCUITestReader, udid: str) -> Dict:
    print("\n[S2] openurl ?q= (search query)")
    await asyncio.sleep(2)
    before = snapshot(udid)
    openurl(udid, "http://maps.apple.com/?q=Blue+Bottle+Coffee")
    await asyncio.sleep(8)
    after = snapshot(udid)
    return report_scenario("openurl ?q=", before, after, udid)


async def scenario_3_ll(reader: XCUITestReader, udid: str) -> Dict:
    print("\n[S3] openurl ?ll= (coordinate lookup)")
    await asyncio.sleep(2)
    before = snapshot(udid)
    openurl(udid, "http://maps.apple.com/?ll=37.4189,-122.0691")
    await asyncio.sleep(8)
    after = snapshot(udid)
    return report_scenario("openurl ?ll=", before, after, udid)


def dump_ax(tree, where: str, limit: int = 40) -> None:
    """Debug aid: dump a few useful AX elements when search-bar lookup
    fails so future runs can adapt."""
    print(f"  -- AX dump @ {where}, first {limit} labeled elements --")
    n = 0
    for e in tree.elements:
        lbl = (e.label or "").strip()
        role = e.role or ""
        if not lbl:
            continue
        print(f"    {role:18s} {lbl[:80]!r}  frame={e.frame.x:.0f},{e.frame.y:.0f}")
        n += 1
        if n >= limit:
            break


def find_searchbar(tree) -> Optional[Any]:
    """Find Maps search bar. iOS 26 Maps uses role='search' (not
    SearchField) and the bar's label is the localized app name —
    'Apple Maps' or 'Maps' — NOT 'Search Maps'. The image inside
    is labeled 'Search'."""
    candidates = []
    for e in tree.elements:
        role = (e.role or "").lower()
        lbl = (e.label or "").lower()
        if role == "search" or "searchfield" in role:
            candidates.append((0, e))
        elif role in ("textfield",) and "search" in lbl:
            candidates.append((1, e))
        elif "button" in role and (
                "search maps" in lbl or lbl == "search"
                or "apple maps" in lbl):
            candidates.append((2, e))
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1] if candidates else None


async def scenario_4_ui_search(reader: XCUITestReader, udid: str) -> Dict:
    print("\n[S4] UI flow — pure search (no Directions tap)")
    await asyncio.sleep(2)
    before = snapshot(udid)
    # Launch Maps cleanly.
    try:
        await reader.launch("com.apple.Maps")
    except Exception as exc:
        print(f"  [WARN] launch Maps: {exc}")
    await asyncio.sleep(4)
    # Observe to find search bar.
    try:
        tree = await reader.observe()
    except Exception as exc:
        print(f"  [WARN] observe failed: {exc}")
        return {}

    sb = find_searchbar(tree)
    if sb is None:
        dump_ax(tree, "after Maps launch")
        # Best-effort: tap near bottom-center where the search sheet
        # usually sits in iOS 26.
        print("  [HINT] no searchfield found via AX; tapping presumed sheet")
        try:
            await reader.tap(x=tree.screen_width / 2,
                              y=tree.screen_height * 0.85)
        except Exception as exc:
            print(f"  [WARN] fallback tap: {exc}")
        await asyncio.sleep(2)
        tree = await reader.observe()
        sb = find_searchbar(tree)

    if sb is None:
        dump_ax(tree, "after fallback tap")
        print("  [FAIL] could not locate search bar — skipping UI flow")
        after = snapshot(udid)
        return report_scenario("UI search (no searchbar found)",
                                before, after, udid)

    print(f"  Search bar @ ({sb.frame.center_x:.0f},{sb.frame.center_y:.0f}) "
          f"role={sb.role!r} label={sb.label!r}")
    try:
        await reader.tap(x=sb.frame.center_x, y=sb.frame.center_y)
    except Exception as exc:
        print(f"  [WARN] tap searchbar: {exc}")
    await asyncio.sleep(2)
    # Type a query.
    try:
        await reader.type_text("Apple Park")
    except Exception as exc:
        print(f"  [WARN] type: {exc}")
    await asyncio.sleep(3)
    # Find a result row in the autocomplete and tap it.
    try:
        tree = await reader.observe()
    except Exception as exc:
        print(f"  [WARN] observe after type: {exc}")
        tree = None

    tapped_result = False
    if tree:
        # iOS 26 Maps autocomplete rows are `text` elements (not Cells
        # or Buttons). The combined-label "Apple Park, 1 Apple Park
        # Way, Cupertino" is the first row — a sibling of the search
        # bar inside `table 'Search results'`. Tap on the combined
        # label's frame center.
        for e in tree.elements:
            role = (e.role or "").lower()
            lbl = (e.label or "")
            # Combined-summary text rows have commas and contain
            # the address — distinguish from the partial-label dupes.
            if role == "text" and "," in lbl and "Apple Park" in lbl:
                print(f"  Tapping result: {role!r} {lbl!r}")
                try:
                    await reader.tap(
                        x=e.frame.center_x, y=e.frame.center_y)
                    tapped_result = True
                except Exception as exc:
                    print(f"  [WARN] tap result: {exc}")
                break
        if not tapped_result:
            dump_ax(tree, "after type Apple Park", limit=25)

    # Either way wait for the place card and snapshot.
    await asyncio.sleep(5)
    after = snapshot(udid)
    return report_scenario("UI search → place card", before, after, udid)


async def scenario_5_ui_directions(reader: XCUITestReader, udid: str) -> Dict:
    print("\n[S5] UI flow — tap Directions on place card")
    before = snapshot(udid)
    try:
        tree = await reader.observe()
    except Exception as exc:
        print(f"  [WARN] observe failed: {exc}")
        return {}
    dirs = None
    for e in tree.elements:
        lbl = (e.label or "").lower()
        role = (e.role or "").lower()
        # Role can be 'btn' or 'button' depending on scaffold version.
        if role in ("btn", "button") and (lbl == "directions" or lbl.startswith("directions")):
            dirs = e
            break
    if dirs is None:
        dump_ax(tree, "Directions search", limit=40)
        print("  [WARN] no Directions button found.")
        after = snapshot(udid)
        return report_scenario("UI directions (button missing)",
                                before, after, udid)
    print(f"  Tapping Directions @ ({dirs.frame.center_x:.0f},"
          f"{dirs.frame.center_y:.0f})")
    try:
        await reader.tap(x=dirs.frame.center_x, y=dirs.frame.center_y)
    except Exception as exc:
        print(f"  [WARN] tap Directions: {exc}")
    await asyncio.sleep(5)
    after = snapshot(udid)
    return report_scenario("UI directions tap", before, after, udid)


async def scenario_7_tap_recent(reader: XCUITestReader, udid: str) -> Dict:
    """Tap a Recents row directly — this exercises the "user reopens
    a prior search" code path, which in iOS Maps usually re-stamps
    ZMODIFICATIONTIME and may upgrade ZTYPE / fill in coordinates.

    First back out to the Maps default sheet via Close button (the
    `'Close'` button next to the search bar when search is active).
    """
    print("\n[S7] UI flow — tap a Recents row (cached prior search)")
    # Close current search to surface Recents.
    try:
        tree = await reader.observe()
        for e in tree.elements:
            role = (e.role or "").lower()
            if role in ("btn", "button") and (e.label or "") == "Close":
                print(f"  Tapping Close to return to default sheet")
                await reader.tap(x=e.frame.center_x, y=e.frame.center_y)
                await asyncio.sleep(2)
                break
    except Exception as exc:
        print(f"  [WARN] could not Close: {exc}")
    await asyncio.sleep(1)
    try:
        tree = await reader.observe()
    except Exception as exc:
        print(f"  [WARN] observe: {exc}")
        return {}
    target = None
    for e in tree.elements:
        lbl = (e.label or "")
        role = (e.role or "").lower()
        if role in ("btn", "button") and "Blue Bottle" in lbl:
            target = e
            break
    if target is None:
        print("  [SKIP] no Blue Bottle row in Recents view")
        dump_ax(tree, "search Recents", limit=30)
        return {}
    before = snapshot(udid)
    print(f"  Tapping {target.label!r}")
    try:
        await reader.tap(x=target.frame.center_x, y=target.frame.center_y)
    except Exception as exc:
        print(f"  [WARN] tap: {exc}")
    await asyncio.sleep(5)
    after = snapshot(udid)
    return report_scenario("UI tap Recents row", before, after, udid)


async def scenario_6_recents(reader: XCUITestReader, udid: str) -> Dict:
    print("\n[S6] UI flow — Recents pane visibility")
    # Just observe and look for "Recents" section / cells whose
    # labels reflect prior scenarios.
    await asyncio.sleep(2)
    try:
        await reader.launch("com.apple.Maps")
    except Exception as exc:
        print(f"  [WARN] launch: {exc}")
    await asyncio.sleep(4)
    try:
        tree = await reader.observe()
    except Exception as exc:
        print(f"  [WARN] observe: {exc}")
        return {}
    # Tap the searchbar to expose the bottom sheet's Recents section.
    sb = find_searchbar(tree)
    if sb is not None:
        try:
            await reader.tap(x=sb.frame.center_x, y=sb.frame.center_y)
        except Exception:
            pass
        await asyncio.sleep(2)
        try:
            tree = await reader.observe()
        except Exception:
            pass
    # Look for cells with our prior queries.
    print("  Looking for Recents labels matching prior probe activity...")
    targets = [
        "Apple Park", "Blue Bottle", "Coffee",
        "1 Apple Park Way", "Cupertino",
        "37.41", "122.06",
    ]
    hits = []
    for e in tree.elements:
        lbl = (e.label or "")
        for t in targets:
            if t.lower() in lbl.lower():
                hits.append((e.role, lbl))
                break
    if hits:
        print(f"  Recents shows {len(hits)} matching cells:")
        for role, lbl in hits[:15]:
            print(f"    {role:18s} {lbl[:80]!r}")
    else:
        print("  No matching labels in current view.")
        dump_ax(tree, "Maps recents view", limit=30)
    return {}


def print_db_baseline(udid: str) -> None:
    print(f"\nDB path: {db_path(udid)}")
    print("Baseline row counts in watched tables:")
    conn = open_db(udid)
    try:
        existing = set(list_tables(conn))
        for t in WATCH_TABLES:
            if t in existing:
                cur = conn.execute(f"SELECT COUNT(*) FROM {t}")
                print(f"  {t}: {cur.fetchone()[0]}")
            else:
                print(f"  {t}: <MISSING>")
    finally:
        conn.close()


async def main() -> None:
    # Flags: --openurl-only (no XCUITest reader), --ui-only (skip openurl
    # scenarios). Useful when another probe is contending for the XCUITest
    # server on the same UDID.
    args = sys.argv[1:]
    openurl_only = "--openurl-only" in args
    ui_only = "--ui-only" in args
    udid = next((a for a in args if not a.startswith("--")), DEFAULT_UDID)
    print(f"=== Maps history probe — UDID {udid} ===")
    print(f"   openurl_only={openurl_only} ui_only={ui_only}")
    print_db_baseline(udid)

    ztypes: Dict[str, Optional[int]] = {}

    if openurl_only:
        # No XCUITest reader at all — pure openurl + diff.
        print("\n[openurl-only mode] skipping XCUITest reader")
        # Make reader-less proxies — scenarios 1/2/3 don't call reader.
        class _Noop:
            async def press(self, *a, **k): pass
        noop = _Noop()
        if not ui_only:
            for fn, label in [(scenario_1_daddr, "S1 ?daddr="),
                              (scenario_2_q,     "S2 ?q="),
                              (scenario_3_ll,    "S3 ?ll=")]:
                diff = await fn(noop, udid)  # type: ignore
                if "ZHISTORYITEM" in diff:
                    ztypes[label] = diff["ZHISTORYITEM"][0].get("ZTYPE")
    else:
        reader = XCUITestReader(udid, bundle_id="com.apple.Maps")
        print("\nStarting XCUITest reader (~10-20s first run)...")
        await reader.start()
        try:
            if not ui_only:
                diff = await scenario_1_daddr(reader, udid)
                if "ZHISTORYITEM" in diff:
                    ztypes["S1 ?daddr="] = diff["ZHISTORYITEM"][0].get("ZTYPE")
                diff = await scenario_2_q(reader, udid)
                if "ZHISTORYITEM" in diff:
                    ztypes["S2 ?q="] = diff["ZHISTORYITEM"][0].get("ZTYPE")
                diff = await scenario_3_ll(reader, udid)
                if "ZHISTORYITEM" in diff:
                    ztypes["S3 ?ll="] = diff["ZHISTORYITEM"][0].get("ZTYPE")
            diff = await scenario_4_ui_search(reader, udid)
            if "ZHISTORYITEM" in diff:
                ztypes["S4 UI search"] = diff["ZHISTORYITEM"][0].get("ZTYPE")
            diff = await scenario_5_ui_directions(reader, udid)
            if "ZHISTORYITEM" in diff:
                ztypes["S5 UI Directions"] = diff["ZHISTORYITEM"][0].get("ZTYPE")
            diff = await scenario_7_tap_recent(reader, udid)
            if "ZHISTORYITEM" in diff:
                ztypes["S7 tap Recents"] = diff["ZHISTORYITEM"][0].get("ZTYPE")
            await scenario_6_recents(reader, udid)
        finally:
            try:
                await reader.stop()
            except Exception:
                pass

    print("\n=== ZTYPE summary across scenarios ===")
    for k, v in ztypes.items():
        print(f"  {k:25s} → ZTYPE={v}")

    print("\nFinal row counts:")
    print_db_baseline(udid)


if __name__ == "__main__":
    asyncio.run(main())
