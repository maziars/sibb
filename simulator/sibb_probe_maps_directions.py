#!/usr/bin/env python3
"""Empirical probe: characterize iOS 26.3 Maps directions DB state
for verifier design.

Goal — answer these questions with DB-state evidence (no trajectory
inspection):
  1. Which UI action creates a ZHISTORYITEM z_ent=16 row?
  2. Does switching transport mode (Drive / Walk / Transit / Cycle /
     Ride) write distinguishable bytes in ZROUTEREQUESTSTORAGE?
  3. Does toggling Avoid Tolls / Avoid Highways write distinguishable
     bytes?
  4. Does tapping a non-default route (vs the "Fastest" / top one)
     write a different route_selection index?
  5. Whether ZUSERROUTE (separate table) ever populates.

Method: vary ONE dimension at a time, snapshot the rrs bytes, then
diff byte-by-byte across scenarios. Small enum/flag changes flip
1-2 bytes — we can identify them empirically without the .proto
file.

Output:
  - per-scenario z_ent counts + rrs hex
  - byte-diff matrix between scenarios
  - written summary at the bottom telling us exactly which bytes
    encode mode / avoid / route-selection

Run:
    SIBB_UDID=19B95A95-614A-4ECA-B943-44FDADFD7A9F \\
        /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb/simulator/sibb_probe_maps_directions.py
"""
from __future__ import annotations
import asyncio
import os
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
SIBB = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(SIBB, "benchmark"))

from sibb_xcuitest_client import XCUITestReader  # noqa
from sibb_state import _mapsdb_path              # noqa

UDID = os.environ.get(
    "SIBB_UDID", "19B95A95-614A-4ECA-B943-44FDADFD7A9F")
MAPS = "com.apple.Maps"
TARGET_QUERY = "Salk Institute"


def shell(cmd, timeout=15):
    return subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)


def db_latest_history_row() -> Optional[Dict[str, Any]]:
    """Return the most recent ZHISTORYITEM row + its ZROUTEREQUESTSTORAGE
    full bytes (hex). Returns None if no DB or no rows."""
    dbpath = _mapsdb_path(UDID)
    if dbpath is None:
        return None
    conn = sqlite3.connect(dbpath, timeout=2.0)
    try:
        r = conn.execute(
            "SELECT Z_PK, Z_ENT, ZCREATETIME, HEX(ZROUTEREQUESTSTORAGE) "
            "FROM ZHISTORYITEM ORDER BY ZCREATETIME DESC LIMIT 1;"
        ).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return {"pk": r[0], "z_ent": r[1], "create_ts": r[2],
            "rrs_hex": r[3] or ""}


def db_counts() -> Dict[str, Any]:
    dbpath = _mapsdb_path(UDID)
    if dbpath is None:
        return {"z_ent_counts": {}, "user_route": 0}
    conn = sqlite3.connect(dbpath, timeout=2.0)
    try:
        rows = conn.execute(
            "SELECT Z_ENT, COUNT(*) FROM ZHISTORYITEM "
            "GROUP BY Z_ENT;").fetchall()
        user_route = conn.execute(
            "SELECT COUNT(*) FROM ZUSERROUTE;").fetchone()[0]
    finally:
        conn.close()
    return {"z_ent_counts": {int(z): int(c) for z, c in rows},
            "user_route": int(user_route)}


async def get_els(r) -> List[Dict[str, Any]]:
    raw = await r._send({"type": "observe", "bundleId": MAPS})
    return raw.get("elements") or []


def find_label(els, label_substr, role_in=None) -> Optional[Dict[str, Any]]:
    s = label_substr.lower()
    for e in els:
        if role_in and e.get("role") not in role_in:
            continue
        lbl = (e.get("label") or "").lower()
        if s in lbl:
            return e
    return None


def find_exact_label(els, label, role_in=None) -> Optional[Dict[str, Any]]:
    """Find a button/cell whose label matches exactly (case-insensitive)."""
    target = label.lower()
    for e in els:
        if role_in and e.get("role") not in role_in:
            continue
        if (e.get("label") or "").lower() == target:
            return e
    return None


async def tap_el(r, e: Dict[str, Any], settle: float = 1.5) -> bool:
    if e is None or not e.get("frame"):
        return False
    fr = e["frame"]
    await r.tap(x=fr["x"] + fr["width"]/2,
                 y=fr["y"] + fr["height"]/2)
    await asyncio.sleep(settle)
    return True


async def wait_for_db_settle(seconds: float = 4.0) -> None:
    """iOS Maps writes ZHISTORYITEM asynchronously; sleep so the
    write completes before we snapshot. Empirically 2-3s is enough;
    use 4s for safety."""
    await asyncio.sleep(seconds)


def byte_diff_segments(a_hex: str, b_hex: str) -> List[Tuple[int, str, str]]:
    """Return [(byte_offset, a_byte, b_byte), ...] for positions
    where the two hex strings differ. Hex strings are uppercase
    contiguous (no spaces). 2 hex chars per byte."""
    n = min(len(a_hex), len(b_hex))
    diffs = []
    i = 0
    while i + 1 < n:
        a = a_hex[i:i+2]
        b = b_hex[i:i+2]
        if a != b:
            diffs.append((i // 2, a, b))
        i += 2
    # Length mismatch suffix
    if len(a_hex) != len(b_hex):
        diffs.append((-1, f"len={len(a_hex)//2}",
                      f"len={len(b_hex)//2}"))
    return diffs


def banner(s):
    print("\n" + "="*72)
    print("  " + s)
    print("="*72)


def scenario_print(name: str, rrs_hex: str, counts: Dict[str, Any]):
    """Print a scenario's recorded state."""
    rrs_len = len(rrs_hex) // 2
    print(f"  [{name}]")
    print(f"     z_ent_counts={counts.get('z_ent_counts', {})}")
    print(f"     ZUSERROUTE  ={counts.get('user_route', 0)}")
    print(f"     rrs_bytes   ={rrs_len}")
    print(f"     rrs_head    ={rrs_hex[:60]}...")
    print(f"     rrs_tail    =...{rrs_hex[-40:]}")


async def commit_route_via_duration_button(r) -> bool:
    """Find the duration button on the place card. The label always
    contains a time (`X min, ...` or `X hr Y min, ...`) followed by
    a mode word. Tapping it commits a z_ent=16 row.

    DON'T match elements whose label is JUST a mode word (Drive /
    Walk / Transit etc) — those are tabs on the route options
    screen, not the place-card duration button."""
    import re as _re
    els = await get_els(r)
    # Require: a digit followed by "min" or "hr" AND a mode word
    pat = _re.compile(r"\d+\s*(?:min|hr).*(driving|walking|transit|cycling|riding)",
                       _re.IGNORECASE)
    for e in els:
        lbl = (e.get("label") or "")
        if e.get("role") == "btn" and pat.search(lbl):
            print(f"     duration btn: {lbl!r}")
            return await tap_el(r, e, settle=4.5)
    return False


async def switch_mode_on_directions_screen(r, mode_label: str) -> bool:
    """We're on the directions/route-options screen. Tap the mode
    button (Drive/Walk/Transit/Cycle/Ride). Returns True iff found+tapped."""
    els = await get_els(r)
    btn = find_exact_label(els, mode_label, role_in=("btn",))
    if btn:
        print(f"     mode btn: {btn.get('label')!r}")
        ok = await tap_el(r, btn, settle=4.0)
        return ok
    return False


async def main():
    print(f"UDID: {UDID}")
    # Idempotent grant.
    shell(f"xcrun simctl privacy {UDID} grant location {MAPS}")
    shell(f"xcrun simctl terminate {UDID} {MAPS}")
    await asyncio.sleep(1.0)

    reader = XCUITestReader(UDID)
    await reader.start()
    print("XCUITest server connected.")

    baseline_row = db_latest_history_row()
    baseline_counts = db_counts()
    print(f"\nBaseline: counts={baseline_counts['z_ent_counts']}, "
          f"latest_pk={(baseline_row or {}).get('pk')}")

    scenarios: List[Tuple[str, str, Dict[str, Any]]] = []

    # ── Step 1: launch Maps + openurl q= (lands on place card) ──────────────
    banner("STEP 1: launch Maps + openurl q= (lands on place card)")
    await reader.launch(bundle_id=MAPS)
    await asyncio.sleep(3.0)
    shell(f"xcrun simctl openurl {UDID} 'maps://?q={TARGET_QUERY.replace(' ', '%20')}'")
    await asyncio.sleep(5.0)
    await wait_for_db_settle()
    # Diagnostic: what's on screen?
    els = await get_els(reader)
    print(f"  After openurl: {len(els)} elements")
    # Find anything that looks like a place card duration button or
    # a result cell containing the query
    import re as _re
    duration_pat = _re.compile(r"\d+\s*(?:min|hr).*driv|walk|transit",
                                _re.IGNORECASE)
    print("  Relevant elements (place names + duration buttons + nav):")
    for e in els:
        lbl = (e.get("label") or "")
        if not lbl: continue
        role = e.get("role", "?")
        if (TARGET_QUERY.lower() in lbl.lower()
                or duration_pat.search(lbl)
                or "directions" in lbl.lower()
                or role == "btn"):
            print(f"    {role:8s} {lbl[:60]!r} "
                  f"@({e['frame']['x']:.0f},{e['frame']['y']:.0f})")
    # If we see a result row for the query, tap it.
    result = None
    for e in els:
        lbl = (e.get("label") or "").lower()
        if (TARGET_QUERY.lower() in lbl
                and e.get("role") in ("cell", "btn")):
            result = e
            break
    if result:
        print(f"  tapping result row: {result.get('label')!r}")
        await tap_el(reader, result, settle=4.0)
        await wait_for_db_settle()

    # ── Step 3: scenario DRIVING — tap "X min, driving" on place card ───────
    banner("STEP 3 — SCENARIO 'DRIVING': tap duration button on place card")
    if not await commit_route_via_duration_button(reader):
        print("  ! couldn't find duration button; bailing")
        await reader.stop()
        return
    await wait_for_db_settle()
    row = db_latest_history_row()
    counts = db_counts()
    scenario_print("DRIVING (default)", row.get("rrs_hex", ""), counts)
    scenarios.append(("DRIVING", row.get("rrs_hex", ""), counts))

    # We should now be on the route options screen. The mode switcher
    # (Drive/Walk/Transit/Cycle/Ride) is at the top.

    # ── Step 4: scenario WALK — tap Walk mode → tap duration ────────────────
    banner("STEP 4 — SCENARIO 'WALK': switch mode → re-commit")
    if await switch_mode_on_directions_screen(reader, "Walk"):
        # After mode switch, Maps reloads routes; the duration button
        # MAY be re-rendered on a Walk-specific options sheet, or the
        # original duration card on the place sheet may now read
        # "X mins, walking". Wait + re-snap.
        await wait_for_db_settle()
        # The mode switch itself MAY create a row. Snapshot now.
        row = db_latest_history_row()
        counts = db_counts()
        scenario_print("WALK (mode switched, no re-commit)",
                       row.get("rrs_hex", ""), counts)
        scenarios.append(("WALK_SWITCH_ONLY", row.get("rrs_hex", ""), counts))
        # Now try to commit by tapping the duration if visible
        if await commit_route_via_duration_button(reader):
            await wait_for_db_settle()
            row = db_latest_history_row()
            counts = db_counts()
            scenario_print("WALK (re-committed)", row.get("rrs_hex", ""),
                           counts)
            scenarios.append(("WALK_COMMITTED",
                               row.get("rrs_hex", ""), counts))
    else:
        print("  ! Walk button not found")

    # ── Step 5: scenario TRANSIT ────────────────────────────────────────────
    banner("STEP 5 — SCENARIO 'TRANSIT': switch mode → re-commit")
    if await switch_mode_on_directions_screen(reader, "Transit"):
        await wait_for_db_settle()
        row = db_latest_history_row()
        counts = db_counts()
        scenario_print("TRANSIT (mode switched)",
                       row.get("rrs_hex", ""), counts)
        scenarios.append(("TRANSIT", row.get("rrs_hex", ""), counts))
    else:
        print("  ! Transit button not found")

    # ── Step 6: scenario CYCLE ──────────────────────────────────────────────
    banner("STEP 6 — SCENARIO 'CYCLE': switch mode")
    if await switch_mode_on_directions_screen(reader, "Cycle"):
        await wait_for_db_settle()
        row = db_latest_history_row()
        counts = db_counts()
        scenario_print("CYCLE", row.get("rrs_hex", ""), counts)
        scenarios.append(("CYCLE", row.get("rrs_hex", ""), counts))
    else:
        print("  ! Cycle button not found")

    # ── Step 7: scenario DRIVING again + try Avoid Tolls ─────────────────────
    banner("STEP 7 — SCENARIO 'DRIVING + AVOID': toggle Avoid options")
    if await switch_mode_on_directions_screen(reader, "Drive"):
        await wait_for_db_settle()
        # Find and tap "Avoid" button
        els = await get_els(reader)
        avoid_btn = find_exact_label(els, "Avoid", role_in=("btn",))
        if avoid_btn:
            print(f"     tapping Avoid: {avoid_btn.get('label')!r}")
            await tap_el(reader, avoid_btn, settle=2.0)
            els = await get_els(reader)
            # In the Avoid sheet, look for Tolls switch.
            tolls = find_label(els, "Tolls",
                                role_in=("switch", "btn", "cell"))
            if tolls:
                print(f"     tapping Tolls: {tolls.get('label')!r}")
                await tap_el(reader, tolls, settle=2.0)
                # Dismiss the sheet — look for Done or close
                els = await get_els(reader)
                done = (find_exact_label(els, "Done", role_in=("btn",))
                        or find_label(els, "Close", role_in=("btn",)))
                if done:
                    await tap_el(reader, done, settle=2.0)
            # Now commit by tapping a duration button
            if await commit_route_via_duration_button(reader):
                await wait_for_db_settle()
                row = db_latest_history_row()
                counts = db_counts()
                scenario_print("DRIVING + AVOID_TOLLS",
                               row.get("rrs_hex", ""), counts)
                scenarios.append(("DRIVING_AVOID_TOLLS",
                                   row.get("rrs_hex", ""), counts))
        else:
            print("  ! Avoid button not found")

    # ── SUMMARY: pairwise byte diffs ─────────────────────────────────────────
    banner("BYTE DIFF MATRIX — which bytes encode each dimension?")
    for i, (n1, h1, _) in enumerate(scenarios):
        for j, (n2, h2, _) in enumerate(scenarios):
            if j <= i:
                continue
            print(f"\n  diff [{n1}] vs [{n2}]:")
            diffs = byte_diff_segments(h1, h2)
            if not diffs:
                print("    (no byte differences)")
            else:
                print(f"    {len(diffs)} byte positions differ "
                      f"(showing first 20):")
                for off, a, b in diffs[:20]:
                    if off < 0:
                        print(f"      length mismatch: {a} vs {b}")
                    else:
                        print(f"      offset {off:>4}: {a} → {b}")

    banner("PROBE COMPLETE")
    print("Findings written to stdout. Use the byte-diff matrix")
    print("to map dimensions to ZROUTEREQUESTSTORAGE byte offsets.")
    await reader.stop()


if __name__ == "__main__":
    asyncio.run(main())
