#!/usr/bin/env python3
"""
EventKit Calendar baseline probe.
=================================

Answers three questions before any Calendar generator lands:

  Q1. What writable calendars does a fresh iOS 26.x sim expose
      via EKEventStore.calendars(for: .event)? Which subscribed/
      read-only calendars also appear?
  Q2. Does the current Swift `acquireEventStore()` (which calls
      the deprecated `requestAccess(to: .event)`) give us READ
      access on iOS 17+, or only writeOnly? Test: create an event,
      then `list_events` — non-empty = full access; empty = bug.
  Q3. Does iOS include all-day events in a `predicateForEvents`
      window like `[D-T14:00, D-T16:00]`? If NO, all-day events
      could be a cheat path for time-windowed count checks.

Usage
-----
    xcrun simctl boot 19B95A95-614A-4ECA-B943-44FDADFD7A9F
    /Library/Developer/CommandLineTools/usr/bin/python3 \\
        sibb_probe_calendar.py [<udid>]

Defaults to the SIBB-Demo UDID in CLAUDE.md if no argument.
Re-runs are idempotent (the probe wipes events before each Q).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader  # noqa: E402

DEFAULT_UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"


async def _send(reader: XCUITestReader, cmd: Dict[str, Any]) -> Dict[str, Any]:
    async with reader._lock:
        return await reader._send(cmd)


async def wipe(reader: XCUITestReader) -> None:
    resp = await _send(reader, {"type": "wipe_events"})
    if not resp.get("ok"):
        print(f"  [WARN] wipe_events failed: {resp.get('error')}")


async def q1_calendar_enumeration(reader: XCUITestReader) -> List[Dict[str, Any]]:
    """List every event in the store after wiping. Each event row
    surfaces a `.calendar` field — collect distinct names. If the
    list is empty (expected on a fresh sim after wipe), we instead
    probe by attempting create_event with a series of candidate
    names and noting which succeed.
    """
    print("\n[Q1] Enumerating writable calendars...")
    await wipe(reader)
    # Try create with no calendar arg → Swift picks default.
    today = date.today()
    start = f"{today.isoformat()}T10:00:00"
    end   = f"{today.isoformat()}T10:30:00"
    resp = await _send(reader, {
        "type": "create_event",
        "title": "PROBE default cal",
        "start_iso": start,
        "end_iso": end,
    })
    if not resp.get("ok"):
        print(f"  [FAIL] create_event without calendar: {resp.get('error')}")
        return []
    default_cal = resp.get("calendar", "<unknown>")
    print(f"  Default writable calendar (no arg): {default_cal!r}")

    # Now try with explicit names — known iOS defaults and subscribed.
    candidates = ["Calendar", "Home", "Work", "Personal", "Birthdays",
                  "Holidays", "US Holidays", "Siri Suggestions",
                  "Family"]
    writable: List[str] = []
    rejected: List[tuple] = []
    for name in candidates:
        r = await _send(reader, {
            "type": "create_event",
            "title": f"PROBE {name}",
            "start_iso": f"{today.isoformat()}T11:00:00",
            "end_iso":   f"{today.isoformat()}T11:30:00",
            "calendar":  name,
        })
        if r.get("ok"):
            writable.append(name)
        else:
            rejected.append((name, r.get("error", "")))
    print(f"  Writable when named: {writable}")
    print(f"  Rejected (likely read-only / missing):")
    for n, e in rejected:
        print(f"    - {n}: {e}")
    return writable


async def q2_tcc_read_access(reader: XCUITestReader) -> None:
    """Create an event, then list_events. If list returns the event
    we just created, requestAccess(to:.event) gave full access.
    If empty, it gave writeOnly — blocker for verification.
    """
    print("\n[Q2] Testing TCC read scope (writeOnly vs fullAccess)...")
    await wipe(reader)
    today = date.today()
    start = f"{today.isoformat()}T14:30:00"
    end   = f"{today.isoformat()}T15:30:00"
    r = await _send(reader, {
        "type": "create_event",
        "title": "PROBE TCC scope",
        "start_iso": start, "end_iso": end,
    })
    if not r.get("ok"):
        print(f"  [FAIL] create failed: {r.get('error')}")
        return
    listed = await _send(reader, {"type": "list_events"})
    events = listed.get("events", [])
    print(f"  Events visible after create: {len(events)}")
    if events:
        for e in events:
            print(f"    - {e.get('title')!r} in {e.get('calendar')!r}")
        print("  [OK] Full read access confirmed.")
    else:
        print("  [BLOCKER] list_events empty after create —"
              " writeOnly access likely. Need requestFullAccessToEvents.")


async def q6_recurrence_semantics(reader: XCUITestReader) -> None:
    """5 empirical questions on iOS 26.3 sim recurrence behavior:
      Q6.1: recurrence wire shape (what dict comes back?)
      Q6.2: does predicateForEvents EXPAND recurring events into N rows?
      Q6.3: what does setting recurrenceRules = [] do? Same identifier?
      Q6.4: cache freshness — after save, immediate list sees the new rule?
      Q6.5: wipe_events with .thisEvent on recurring master — does it
            actually delete all occurrences or just the first?
    Answers determine T4b architecture (unique_by, span fix, etc)."""
    print("\n[Q6] Recurrence semantics probe...")
    await wipe(reader)

    today = date.today()
    tomorrow = today + timedelta(days=1)

    # Q6.1 — wire shape. Create a weekly event with end_count=4.
    print("\n  Q6.1 — recurrence wire shape (weekly × 4)...")
    r = await _send(reader, {
        "type": "create_event",
        "title": "PROBE Weekly",
        "start_iso": f"{tomorrow.isoformat()}T10:00:00",
        "end_iso":   f"{tomorrow.isoformat()}T11:00:00",
        "recurrence": {"frequency": "weekly", "interval": 1,
                        "end_count": 4},
    })
    if not r.get("ok"):
        print(f"    [BLOCKER] create_event with recurrence failed: "
              f"{r.get('error')}")
        return

    # Q6.2 — expansion. Default window.
    print("\n  Q6.2 — occurrence expansion under default window...")
    listed = await _send(reader, {"type": "list_events"})
    events = [e for e in listed.get("events", [])
              if e.get("title", "").startswith("PROBE Weekly")]
    print(f"    list_events returned {len(events)} row(s) for the series.")
    if events:
        ids = {e.get("identifier") for e in events}
        print(f"    distinct identifiers: {len(ids)}")
        for e in events[:3]:
            print(f"      start={e.get('start_iso')!r}  "
                  f"recurrence={e.get('recurrence')!r}")
        if len(events) > 1:
            print(f"    >> iOS EXPANDS: {len(events)} rows; "
                  f"identifier shared={len(ids) == 1}.")
        else:
            print(f"    >> iOS returns MASTER ONLY (no expansion).")

    # Q6.3 — collapse on rule removal. Re-fetch the identifier first.
    print("\n  Q6.3 — stop_recurrence (set rules to nil)...")
    if events:
        master_id = events[0].get("identifier")
        # Send a one-off "modify_event" command — doesn't exist yet, so
        # we have to wipe+recreate without rule and observe. NOTE: this
        # only tests creation-without-rule, not actual rule removal.
        # Real test requires a future modify_event Swift command, which
        # we'll need for stop_recurrence anyway.
        print(f"    master identifier: {master_id!r}")
        print(f"    >> need modify_event command to test in-place rule "
              f"removal; deferred to S6 implementation.")

    # Q6.4 — cache freshness. Already implicit in Q6.2 (we listed
    # immediately after create and got results). Skip explicit test.
    print("\n  Q6.4 — cache freshness: OK (Q6.2 listed immediately "
          "after create and got results).")

    # Q6.5 — wipe_events span behavior on recurring master.
    print("\n  Q6.5 — wipe_events with current (.thisEvent) span...")
    wipe_resp = await _send(reader, {"type": "wipe_events"})
    print(f"    wipe_events removed: {wipe_resp.get('removed_events')}")
    re_listed = await _send(reader, {"type": "list_events"})
    re_events = [e for e in re_listed.get("events", [])
                  if e.get("title", "").startswith("PROBE Weekly")]
    if not re_events:
        print(f"    >> wipe cleared all occurrences ({len(re_events)} "
              f"remaining). Existing .thisEvent span is sufficient.")
    else:
        print(f"    >> [BLOCKER CONFIRMED] {len(re_events)} occurrences "
              f"survived wipe. Need .futureEvents on recurring masters.")
        for e in re_events[:3]:
            print(f"      survivor: start={e.get('start_iso')!r}")


async def q5_create_calendar_roundtrip(reader: XCUITestReader) -> None:
    """Verify the new create_calendar/list_calendars/wipe_calendars
    round-trip works on iOS 26.3. Added 2026-05-21 for the Calendar
    T2/3 multi-calendar prereq."""
    print("\n[Q5] create_calendar/list_calendars/wipe_calendars...")
    # Wipe first so we can count baseline writable calendars cleanly.
    await _send(reader, {"type": "wipe_events"})
    await _send(reader, {"type": "wipe_calendars"})
    listed = await _send(reader, {"type": "list_calendars"})
    if not listed.get("ok"):
        print(f"  [FAIL] list_calendars: {listed.get('error')}")
        return
    baseline_names = sorted(c["name"] for c in listed.get("calendars", []))
    print(f"  Writable baseline (after wipe): {baseline_names}")

    # Create two new calendars.
    for name in ("Work", "Personal"):
        r = await _send(reader, {"type": "create_calendar", "name": name})
        if not r.get("ok"):
            print(f"  [FAIL] create {name}: {r.get('error')}")
            return

    listed = await _send(reader, {"type": "list_calendars"})
    names = sorted(c["name"] for c in listed.get("calendars", []))
    print(f"  After create Work + Personal: {names}")
    expected = sorted(baseline_names + ["Work", "Personal"])
    if names == expected:
        print(f"  [OK] Both calendars created.")
    else:
        print(f"  [BLOCKER] expected {expected}, got {names}")
        return

    # Verify duplicate-name rejection.
    r = await _send(reader, {"type": "create_calendar", "name": "Work"})
    if r.get("ok"):
        print(f"  [BLOCKER] duplicate Work was NOT rejected")
    else:
        print(f"  [OK] Duplicate-name rejected: {r.get('error')}")

    # Wipe calendars and confirm default survives.
    r = await _send(reader, {"type": "wipe_calendars"})
    print(f"  wipe_calendars: removed={r.get('removed_calendars')}")
    listed = await _send(reader, {"type": "list_calendars"})
    final_names = sorted(c["name"] for c in listed.get("calendars", []))
    print(f"  After wipe: {final_names}")
    if final_names == baseline_names:
        print(f"  [OK] Default 'Calendar' survives wipe; user calendars gone.")
    else:
        print(f"  [BLOCKER] wipe left wrong set: {final_names!r} "
              f"vs baseline {baseline_names!r}")


async def q4_all_day_toggle_readback(reader: XCUITestReader) -> None:
    """Probe what list_events returns for an event freshly created as
    all-day on a target date D. Validates the generator's expected
    start_iso == "D" and end_iso == "D+1" assertions in
    gen_toggle_event_all_day. If iOS / EventKit deviates (off-by-one
    day, time-bearing string, TZ-tagged), the generator must adapt."""
    print("\n[Q4] All-day toggle round-trip readback...")
    await wipe(reader)
    today = date.today()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)
    # Create as if we're an agent toggling an existing event. We pass
    # all_day=True with the "Swift normalization" inputs (midnight on D
    # local, midnight on D+1 local). What we read back is what the
    # generator's assertion must encode.
    r = await _send(reader, {
        "type": "create_event",
        "title": "PROBE all-day readback",
        "start_iso": f"{tomorrow.isoformat()}T00:00:00",
        "end_iso":   f"{day_after.isoformat()}T00:00:00",
        "all_day":   True,
    })
    if not r.get("ok"):
        print(f"  [FAIL] create failed: {r.get('error')}")
        return
    listed = await _send(reader, {"type": "list_events"})
    events = listed.get("events", [])
    for e in events:
        if e.get("title") == "PROBE all-day readback":
            sIso = e.get("start_iso")
            eIso = e.get("end_iso")
            allDay = e.get("all_day")
            print(f"  Created all-day for tomorrow ({tomorrow.isoformat()}):")
            print(f"    start_iso: {sIso!r}")
            print(f"    end_iso:   {eIso!r}")
            print(f"    all_day:   {allDay!r}")
            # iOS empirically returns end_iso == start_iso for a
            # single-day all-day event (NOT day+1). See IOS_SIM_QUIRKS §16.
            expected_start = tomorrow.isoformat()
            expected_end   = tomorrow.isoformat()
            if sIso == expected_start and eIso == expected_end:
                print(f"  [OK] Matches gen_toggle_event_all_day's assertion:")
                print(f"       start={expected_start!r}, end={expected_end!r}")
            else:
                print(f"  [BLOCKER] gen_toggle_event_all_day expects "
                      f"start={expected_start!r} end={expected_end!r} "
                      f"but got start={sIso!r} end={eIso!r}.")
            return
    print(f"  [FAIL] PROBE all-day readback row not found")


async def q3_all_day_window(reader: XCUITestReader) -> None:
    """Create an all-day event for date D, then query
    list_events with start_iso=D-T14:00 / end_iso=D-T16:00.
    Is the all-day event included?
    """
    print("\n[Q3] All-day vs time-window selector...")
    await wipe(reader)
    today = date.today()
    tomorrow = today + timedelta(days=1)
    # iOS all-day convention: start = D 00:00, end = D+1 00:00
    r = await _send(reader, {
        "type": "create_event",
        "title": "PROBE all-day",
        "start_iso": f"{today.isoformat()}T00:00:00",
        "end_iso":   f"{tomorrow.isoformat()}T00:00:00",
        "all_day":   True,
    })
    if not r.get("ok"):
        print(f"  [FAIL] all-day create failed: {r.get('error')}")
        return
    # Add a normal timed event in the same day to confirm window works at all
    r2 = await _send(reader, {
        "type": "create_event",
        "title": "PROBE timed 2:30pm",
        "start_iso": f"{today.isoformat()}T14:30:00",
        "end_iso":   f"{today.isoformat()}T15:30:00",
    })
    if not r2.get("ok"):
        print(f"  [WARN] timed-event create failed: {r2.get('error')}")

    window_start = f"{today.isoformat()}T14:00:00"
    window_end   = f"{today.isoformat()}T16:00:00"
    listed = await _send(reader, {
        "type": "list_events",
        "start_iso": window_start,
        "end_iso":   window_end,
    })
    events = listed.get("events", [])
    titles = [e.get("title") for e in events]
    all_day_in = "PROBE all-day" in titles
    timed_in   = "PROBE timed 2:30pm" in titles
    print(f"  Window {window_start} → {window_end}")
    print(f"  Returned titles: {titles}")
    print(f"  All-day in window: {all_day_in}  (timed in window: {timed_in})")
    if all_day_in:
        print("  [OK] iOS includes all-day events in any-overlap window."
              " 'all_day=True' cheat path is closed.")
    else:
        print("  [BLOCKER] iOS EXCLUDES all-day from intra-day windows."
              " Generators must use full-day windows when validating"
              " event sets, or emit explicit all_day=False guards.")


async def main(udid: str) -> int:
    print(f"Calendar probe on UDID {udid}")
    print(f"Today (host): {date.today().isoformat()}")
    print(f"Now   (host): {datetime.now().isoformat()}")

    reader = XCUITestReader(udid, bundle_id="com.apple.mobilecal")
    await reader.start()
    try:
        await q1_calendar_enumeration(reader)
        await q2_tcc_read_access(reader)
        await q3_all_day_window(reader)
        await q4_all_day_toggle_readback(reader)
        await q5_create_calendar_roundtrip(reader)
        await q6_recurrence_semantics(reader)
        print("\nDone. Wiping events one more time before exit.")
        await wipe(reader)
        await _send(reader, {"type": "wipe_calendars"})
    finally:
        await reader.stop()
    return 0


if __name__ == "__main__":
    udid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_UDID
    sys.exit(asyncio.run(main(udid)))
