#!/usr/bin/env python3
"""SIBB Phase 1 Contacts validations — 4 sequential probes.

Runs four empirical probes on the iOS 26.3 simulator to confirm
infrastructure assumptions about CNContactStore cross-process refresh,
the iOS Birthdays calendar auto-population behaviour, Spotlight
indexing of runner-created contacts, and `wipe_contacts` safety wrt
system-seeded entries.

Sequential by design — they mutate Contacts state.

Run:
  /Library/Developer/CommandLineTools/usr/bin/python3 \\
      sibb/simulator/sibb_probe_contacts_validations.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader  # noqa: E402

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"
OUT_DIR = f"/tmp/sibb_contacts_validations_{int(time.time())}"


# ── helpers ─────────────────────────────────────────────────────────────

async def _send(reader: XCUITestReader, cmd: Dict[str, Any],
                use_lock: bool = True) -> Dict[str, Any]:
    if use_lock:
        async with reader._lock:
            return await reader._send(cmd)
    return await reader._send(cmd)


async def list_contacts(reader: XCUITestReader) -> List[Dict[str, Any]]:
    r = await _send(reader, {"type": "list_contacts"})
    if not r.get("ok"):
        return []
    return r.get("contacts", []) or []


async def wipe_contacts(reader: XCUITestReader) -> int:
    r = await _send(reader, {"type": "wipe_contacts"})
    return int(r.get("removed_contacts", -1)) if r.get("ok") else -1


async def observe_app(reader: XCUITestReader, retries: int = 2) -> Any:
    """Observe via the public path so we get an AXTree.

    iOS 26 occasionally raises NSException inside XCUITest snapshot
    calls when a transient modal/popover is animating. Retry once
    after a short delay before giving up.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            return await reader.observe()
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(1.0)
    raise last_exc  # type: ignore[misc]


def dump_observation(tree: Any, fname: str) -> str:
    path = os.path.join(OUT_DIR, fname)
    os.makedirs(OUT_DIR, exist_ok=True)
    rows = []
    for e in tree.elements:
        if e.frame:
            cx = round(e.frame.center_x); cy = round(e.frame.center_y)
        else:
            cx = cy = 0
        rows.append({
            "ref": e.ref, "role": e.role,
            "label": e.label, "value": e.value,
            "enabled": e.enabled, "hittable": e.hittable,
            "focused": e.focused, "center": [cx, cy],
        })
    with open(path, "w") as f:
        json.dump({
            "bundle_id": tree.bundle_id,
            "keyboard_visible": tree.keyboard_visible,
            "screen": [tree.screen_width, tree.screen_height],
            "method": tree.method,
            "elements": rows,
        }, f, indent=2, default=str)
    return path


def find_first(tree: Any, *,
               role: Optional[str] = None,
               label_contains: Optional[str] = None,
               value_contains: Optional[str] = None) -> Any:
    needle_l = (label_contains or "").lower()
    needle_v = (value_contains or "").lower()
    for e in tree.elements:
        if role is not None and e.role != role:
            continue
        if label_contains is not None:
            if not e.label or needle_l not in e.label.lower():
                continue
        if value_contains is not None:
            if not e.value or needle_v not in e.value.lower():
                continue
        return e
    return None


def simctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["xcrun", "simctl", *args],
        capture_output=True, text=True, timeout=30,
    )


# ════════════════════════════════════════════════════════════════════════
#  Probe A — Cross-process refresh validity
# ════════════════════════════════════════════════════════════════════════

async def probe_a(reader: XCUITestReader) -> Dict[str, Any]:
    print("\n" + "=" * 72)
    print("Probe A — Cross-process refresh validity")
    print("=" * 72)

    out: Dict[str, Any] = {"verdict": "INCONCLUSIVE", "evidence": []}

    print("[A.1] wipe_contacts")
    removed = await wipe_contacts(reader)
    out["evidence"].append(f"wipe removed {removed} contacts")
    print(f"      removed={removed}")

    print("[A.2] list_contacts (should be 0 or close to it)")
    rows = await list_contacts(reader)
    out["evidence"].append(f"post-wipe list count = {len(rows)}")
    print(f"      list count={len(rows)}")

    print("[A.3] Launch Contacts.app, drive 'Add Contact' via XCUITest UI")
    # Force-terminate first so we land on the main list view, not on
    # a stale new-contact sheet from a prior probe interrupt.
    simctl("terminate", UDID, "com.apple.MobileAddressBook")
    await asyncio.sleep(1.0)
    p = simctl("launch", UDID, "com.apple.MobileAddressBook")
    if p.returncode != 0:
        out["verdict"] = "INCONCLUSIVE"
        out["evidence"].append(f"launch failed: {p.stderr.strip()}")
        return out
    await asyncio.sleep(3.0)

    # First, observe what we have.
    tree = await observe_app(reader)
    dump_observation(tree, "A_after_launch.json")
    print(f"      bundle now: {tree.bundle_id}")

    # Locate the "+" Add button. It's usually a btn labelled "Add" or "+".
    add_btn = (
        find_first(tree, role="btn", label_contains="Add")
        or find_first(tree, role="btn", label_contains="+")
    )
    if add_btn is None:
        # Some Contacts builds open with onboarding — dismiss-tap pass.
        try:
            await reader.dismiss_app_onboarding("com.apple.MobileAddressBook")
        except Exception:
            pass
        await asyncio.sleep(1.5)
        tree = await observe_app(reader)
        dump_observation(tree, "A_after_onboarding.json")
        add_btn = (
            find_first(tree, role="btn", label_contains="Add")
            or find_first(tree, role="btn", label_contains="+")
        )

    if add_btn is None or add_btn.frame is None:
        out["verdict"] = "INCONCLUSIVE"
        out["evidence"].append("could not locate Add button in Contacts.app")
        return out

    print(f"      Add button: ref={add_btn.ref[:8]} label={add_btn.label!r}")
    await reader.tap(x=add_btn.frame.center_x, y=add_btn.frame.center_y)
    await asyncio.sleep(2.0)

    tree = await observe_app(reader)
    dump_observation(tree, "A_after_tap_add.json")

    # iOS 26 Contacts new-contact sheet exposes the First/Last name
    # fields as UNLABELED `input` elements (the placeholder text is
    # not surfaced through AX). The companion `cell` rows are the
    # hit-targets that reliably focus the input (tapping the input
    # element directly can crash the XCUITest runner under iOS 26's
    # automation snapshot path).
    inputs = [e for e in tree.elements
              if e.role == "input" and e.frame is not None
              and e.hittable]
    inputs.sort(key=lambda e: (e.frame.center_y, e.frame.center_x))
    if len(inputs) < 2:
        out["verdict"] = "INCONCLUSIVE"
        out["evidence"].append(
            f"new-contact sheet exposed only {len(inputs)} input fields")
        return out

    fname_field = inputs[0]
    lname_field = inputs[1]
    out["evidence"].append(
        f"first-name input at y={round(fname_field.frame.center_y)} "
        f"(unlabeled — taking spatially-first input)")

    # Tap by raw coords directly (skip the snapshot path that crashes)
    fx = fname_field.frame.center_x; fy = fname_field.frame.center_y
    lx = lname_field.frame.center_x; ly = lname_field.frame.center_y

    await reader.tap(x=fx, y=fy)
    await asyncio.sleep(1.0)
    await reader.type_text("XProbe")
    await asyncio.sleep(0.5)

    # Skip the re-observe step — tap by the previously-resolved
    # last-name coord. (Y position is stable; the cell array doesn't
    # reflow when typing in the first row.)
    await reader.tap(x=lx, y=ly)
    await asyncio.sleep(0.6)
    await reader.type_text("ContactDaemon")
    await asyncio.sleep(0.5)

    # Save with the navigation-bar Done button.
    tree = await observe_app(reader)
    dump_observation(tree, "A_pre_done.json")
    done_btn = find_first(tree, role="btn", label_contains="Done")
    if done_btn is None or done_btn.frame is None:
        out["verdict"] = "INCONCLUSIVE"
        out["evidence"].append("Done button not found after typing names")
        return out

    print(f"      Tapping Done @({round(done_btn.frame.center_x)},"
          f"{round(done_btn.frame.center_y)})")
    save_time = time.time()
    await reader.tap(x=done_btn.frame.center_x, y=done_btn.frame.center_y)
    await asyncio.sleep(1.2)
    await reader.press("home")
    await asyncio.sleep(2.0)

    # THE KEY TEST: socket-side visibility, attempt #1
    timings: List[float] = []
    found_at: Optional[int] = None
    for attempt in range(4):
        elapsed = time.time() - save_time
        rows = await list_contacts(reader)
        found = any(c.get("given_name") == "XProbe" for c in rows)
        names = ", ".join(
            f"{c.get('given_name','')} {c.get('family_name','')}".strip()
            for c in rows
        ) or "<empty>"
        timings.append(elapsed)
        print(f"      attempt {attempt+1}: t+{elapsed:5.1f}s → "
              f"found={found}  contacts=[{names}]")
        if found:
            if found_at is None:
                found_at = attempt
            break
        await asyncio.sleep(2.0)

    out["evidence"].append(f"save→list timings: {[round(t,2) for t in timings]}")
    out["evidence"].append(f"found_at_attempt: {found_at}")

    if found_at == 0:
        # Re-attempt with a FRESH reader process to confirm socket-side
        # determinism (load-bearing for parallel-worker setup). Best-
        # effort — XCUITest cold-start latency varies and a second
        # back-to-back start often takes ~3 min, longer than our
        # 120s ready timeout. The primary cross-process test is the
        # in-process re-read above; this is gravy.
        print("[A.4] Restarting reader process for second-process check")
        try:
            await reader.stop()
            reader2 = XCUITestReader(UDID, bundle_id="com.apple.springboard")
            await reader2.start()
            try:
                t0 = time.time()
                rows2 = await list_contacts(reader2)
                found2 = any(c.get("given_name") == "XProbe" for c in rows2)
                t1 = time.time() - t0
                out["evidence"].append(
                    f"second-process list ({t1:.2f}s post-restart): "
                    f"found={found2} contact_count={len(rows2)}")
                print(f"      second-process found={found2}")
            finally:
                await reader2.stop()
        except Exception as exc:
            out["evidence"].append(
                f"second-process restart check skipped: {exc}")
            print(f"      second-process restart skipped: {exc}")

        out["verdict"] = "PASS"
        out["timing_summary"] = (
            "saw new contact on first list attempt — "
            "cross-process refresh works as-is"
        )
        out["m4_recommendation"] = "NOT NEEDED"
    elif found_at is not None:
        out["verdict"] = "PASS_DELAYED"
        out["timing_summary"] = (
            f"contact surfaced on attempt #{found_at+1} "
            f"({timings[found_at]:.1f}s after save)"
        )
        out["m4_recommendation"] = "TRY-AND-SEE"
    else:
        out["verdict"] = "FAIL"
        out["timing_summary"] = (
            "contact NOT visible after 4 attempts spanning "
            f"{timings[-1]:.1f}s — cross-process refresh is broken"
        )
        out["m4_recommendation"] = "NEEDED"

    return out


# ════════════════════════════════════════════════════════════════════════
#  Probe B — Birthdays calendar auto-population
# ════════════════════════════════════════════════════════════════════════

async def probe_b(reader: XCUITestReader) -> Dict[str, Any]:
    print("\n" + "=" * 72)
    print("Probe B — Birthdays calendar auto-population")
    print("=" * 72)

    out: Dict[str, Any] = {"verdict": "INCONCLUSIVE", "evidence": []}

    # wipe contacts + events
    n_c = await wipe_contacts(reader)
    r = await _send(reader, {"type": "wipe_events"})
    n_e = int(r.get("removed_events", -1)) if r.get("ok") else -1
    print(f"[B.1] wiped contacts={n_c} events={n_e}")
    out["evidence"].append(f"wipe contacts={n_c} events={n_e}")

    # Survey calendars BOTH writable and all (some args differ across kits).
    r = await _send(reader, {"type": "list_calendars"})
    writable_cals = r.get("calendars", []) if r.get("ok") else []
    print(f"[B.2] writable calendars: "
          f"{[c.get('name') for c in writable_cals]}")
    out["evidence"].append(
        f"writable_calendars={[c.get('name') for c in writable_cals]}")

    # Look at full event surface (writable_only=false) — this is where
    # Birthdays / subscribed events would show up.
    r = await _send(reader, {
        "type": "list_events",
        "writable_only": False,
        "start_iso": "2025-01-01T00:00:00",
        "end_iso":   "2027-12-31T00:00:00",
    })
    pre_events = r.get("events", []) if r.get("ok") else []
    pre_cals = sorted({e.get("calendar") for e in pre_events})
    print(f"[B.2b] pre-create events across all calendars: "
          f"{len(pre_events)}  calendars seen: {pre_cals}")
    out["evidence"].append(
        f"pre_event_count={len(pre_events)} calendars_seen={pre_cals}")

    # Create the bday contact
    r = await _send(reader, {
        "type": "create_contact",
        "given_name": "BdayProbe",
        "family_name": "Tester",
        "birthday": "1990-09-15",
    })
    if not r.get("ok"):
        out["verdict"] = "INCONCLUSIVE"
        out["evidence"].append(f"create_contact failed: {r}")
        return out
    ident = r.get("identifier")
    print(f"[B.3] created BdayProbe (id={(ident or '')[:8]}) bday=1990-09-15")

    # Poll every 5s for 60s. Look for Sept 15 birthday events.
    first_seen_at: Optional[float] = None
    last_count = 0
    poll_t0 = time.time()
    for i in range(13):  # 13 × 5s = 65s
        elapsed = time.time() - poll_t0
        r = await _send(reader, {
            "type": "list_events",
            "writable_only": False,
            "start_iso": "2026-09-01T00:00:00",
            "end_iso":   "2026-09-30T00:00:00",
        })
        events = r.get("events", []) if r.get("ok") else []
        # Look for any event whose title mentions "BdayProbe"
        matches = [
            e for e in events
            if "bdayprobe" in (e.get("title") or "").lower()
        ]
        last_count = len(matches)
        print(f"      t+{elapsed:5.1f}s → window events={len(events)}  "
              f"BdayProbe matches={len(matches)}  "
              f"calendars={sorted({e.get('calendar') for e in events})}")
        if matches and first_seen_at is None:
            first_seen_at = elapsed
            out["evidence"].append(
                f"first match at t+{elapsed:.1f}s  example={matches[0]}")
            break
        await asyncio.sleep(5.0)

    out["evidence"].append(f"final match count={last_count}")

    if first_seen_at is not None:
        out["verdict"] = "PASS"
        out["timing"] = f"birthday event appeared at t+{first_seen_at:.1f}s"
        out["shippability"] = "NEEDS-WAIT-POLL"
    else:
        # Did we even have a Birthdays calendar to write to?
        any_bday_cal = any(
            "birthday" in (c.get("name") or "").lower()
            for c in writable_cals
        )
        any_bday_cal_seen = any(
            "birthday" in (c or "").lower() for c in pre_cals
        )
        out["evidence"].append(
            f"birthdays_writable_cal_present={any_bday_cal} "
            f"birthdays_cal_seen_via_events={any_bday_cal_seen}")
        out["verdict"] = "FAIL"
        out["shippability"] = "NOT-SHIPPABLE"

    return out


# ════════════════════════════════════════════════════════════════════════
#  Probe D — Spotlight indexing of runner-created contacts
# ════════════════════════════════════════════════════════════════════════

async def probe_d(reader: XCUITestReader) -> Dict[str, Any]:
    print("\n" + "=" * 72)
    print("Probe D — Spotlight indexing of runner-created contacts")
    print("=" * 72)

    out: Dict[str, Any] = {"verdict": "INCONCLUSIVE", "evidence": []}

    n = await wipe_contacts(reader)
    print(f"[D.1] wiped {n} contacts")

    r = await _send(reader, {
        "type": "create_contact",
        "given_name": "UniqueSpotlightName",
        "family_name": "Probe",
    })
    if not r.get("ok"):
        out["evidence"].append(f"create failed: {r}")
        return out
    print(f"[D.2] created UniqueSpotlightName Probe")

    print("[D.3] sleep 5s for indexing")
    await asyncio.sleep(5.0)

    # Land on home screen.
    try:
        await reader.press("home")
    except Exception:
        pass
    await asyncio.sleep(1.0)

    # Open Spotlight via swipe-down gesture from middle of screen.
    tree = await observe_app(reader)
    w = tree.screen_width; h = tree.screen_height
    print(f"[D.4] invoking Spotlight via swipe down (screen={w}x{h})")
    try:
        await reader.swipe_at(x1=w * 0.5, y1=h * 0.5,
                              x2=w * 0.5, y2=h * 0.9,
                              duration_s=0.05)
    except Exception as exc:
        out["evidence"].append(f"swipe_at failed: {exc}")
        try:
            await reader.swipe(direction="down")
        except Exception as exc2:
            out["evidence"].append(f"swipe down failed: {exc2}")
    await asyncio.sleep(1.5)

    tree = await observe_app(reader)
    dump_observation(tree, "D_after_swipe.json")
    print(f"      bundle after swipe: {tree.bundle_id}")

    # iOS 26 Spotlight surfaces the search bar as an UNLABELED
    # `input` role (label=None, value=None). Locate by role + the
    # adjacent `img` with label="Search" sitting just left of it, or
    # fall back to first hittable input on the Spotlight overlay.
    search_field = (
        find_first(tree, role="search")
        or find_first(tree, role="input", label_contains="Search")
    )
    if search_field is None:
        candidates = [
            e for e in tree.elements
            if e.role == "input" and e.frame is not None and e.hittable
        ]
        if candidates:
            search_field = candidates[0]
            out["evidence"].append(
                f"using unlabeled spotlight input at "
                f"y={round(search_field.frame.center_y)}")
    if search_field is None or search_field.frame is None:
        out["verdict"] = "INCONCLUSIVE"
        out["evidence"].append(
            "no search field after swipe — Spotlight may not have opened "
            f"(bundle={tree.bundle_id})")
        return out

    print(f"      search field: ref={search_field.ref[:8]} "
          f"role={search_field.role}")
    await reader.tap(x=search_field.frame.center_x,
                     y=search_field.frame.center_y)
    await asyncio.sleep(0.8)
    await reader.type_text("UniqueSpotlightName")
    await asyncio.sleep(2.5)

    tree = await observe_app(reader)
    path = dump_observation(tree, "D_after_query.json")
    print(f"      dumped result tree → {path}")

    matches: List[Dict[str, Any]] = []
    for e in tree.elements:
        lab = (e.label or "") + " " + (e.value or "")
        if "uniquespotlightname" in lab.lower() and e.role in (
                "cell", "btn", "link", "text"):
            matches.append({
                "role": e.role, "label": e.label, "value": e.value,
            })

    out["evidence"].append(f"matching_elements={matches[:10]}")
    print(f"      AX matches for query: {len(matches)}")

    if any(m["role"] == "cell" for m in matches):
        out["verdict"] = "PASS"
        out["implication"] = (
            "Spotlight indexes runner-created contacts; T5 generators "
            "may use Spotlight as a primary navigation path")
    elif matches:
        # Search field echoes the query in its value — that doesn't count.
        # Filter those out by removing matches in the search field itself.
        non_self = [
            m for m in matches
            if (m["role"] != "search") and (
                m["label"] is None or
                "search" not in (m["label"] or "").lower()
            )
        ]
        if non_self:
            out["verdict"] = "PARTIAL"
            out["implication"] = (
                "query echo appears but no `cell` row — Spotlight may not "
                "have surfaced the contact as a typed result")
        else:
            out["verdict"] = "FAIL"
            out["implication"] = (
                "only the search field itself echoes the query; "
                "Spotlight did not surface the contact")
    else:
        out["verdict"] = "FAIL"
        out["implication"] = (
            "Spotlight returned no AX-visible reference to the contact; "
            "do not rely on Spotlight for runner-created contacts in T5")

    # Reset to home.
    try:
        await reader.press("home")
    except Exception:
        pass
    await asyncio.sleep(1.0)

    return out


# ════════════════════════════════════════════════════════════════════════
#  Probe E — wipe_contacts safety
# ════════════════════════════════════════════════════════════════════════

async def probe_e(reader: XCUITestReader) -> Dict[str, Any]:
    print("\n" + "=" * 72)
    print("Probe E — wipe_contacts safety (system-seeded survivors)")
    print("=" * 72)

    out: Dict[str, Any] = {"verdict": "INCONCLUSIVE", "evidence": []}

    # First: make sure we have a "clean-ish" state — but DON'T wipe yet.
    # We want to know what the runner sees on a freshly-baselined sim.
    # However, since previous probes wiped, do a baseline reseed: we
    # simply list whatever's currently there.

    print("[E.1] Pre-wipe inventory (whatever survived earlier probes)")
    pre_rows = await list_contacts(reader)
    pre_inventory = []
    for c in pre_rows:
        pre_inventory.append({
            "id": (c.get("identifier") or "")[:36],
            "given": c.get("given_name") or "",
            "family": c.get("family_name") or "",
            "org": c.get("organization") or "",
            "phone": c.get("phone") or "",
            "birthday": c.get("birthday") or "",
        })
    print(f"      pre-wipe contact count: {len(pre_inventory)}")
    for c in pre_inventory[:20]:
        print(f"        - {c}")
    out["evidence"].append(f"pre_wipe_count={len(pre_inventory)}")
    out["evidence"].append(f"pre_wipe_inventory={pre_inventory}")

    print("[E.2] Run wipe_contacts")
    r = await _send(reader, {"type": "wipe_contacts"})
    removed = int(r.get("removed_contacts", -1)) if r.get("ok") else -1
    print(f"      removed_contacts reported = {removed}")
    out["evidence"].append(f"removed_reported={removed}")

    print("[E.3] Post-wipe inventory")
    post_rows = await list_contacts(reader)
    post_inventory = []
    for c in post_rows:
        post_inventory.append({
            "id": (c.get("identifier") or "")[:36],
            "given": c.get("given_name") or "",
            "family": c.get("family_name") or "",
            "org": c.get("organization") or "",
        })
    print(f"      post-wipe contact count: {len(post_inventory)}")
    for c in post_inventory:
        print(f"        SURVIVED — {c}")
    out["evidence"].append(f"post_wipe_count={len(post_inventory)}")
    out["evidence"].append(f"post_wipe_survivors={post_inventory}")

    # Diff
    pre_ids = {c["id"] for c in pre_inventory}
    post_ids = {c["id"] for c in post_inventory}
    survived = pre_ids & post_ids
    deleted = pre_ids - post_ids

    out["evidence"].append(f"survived_count={len(survived)}")
    out["evidence"].append(f"deleted_count={len(deleted)}")

    if not post_inventory:
        out["verdict"] = "PASS"
        out["recommended_exclusions"] = (
            "none — wipe_contacts cleared everything including any "
            "system-seeded entries; no exclusion list needed")
    else:
        out["verdict"] = "PARTIAL"
        out["recommended_exclusions"] = (
            f"{len(post_inventory)} contacts survived wipe (likely "
            "system-immutable). Consider preserving these in any "
            "snapshot-diff verifier and listing them as a known baseline.")

    return out


# ════════════════════════════════════════════════════════════════════════
#  Report
# ════════════════════════════════════════════════════════════════════════

def render_report(results: Dict[str, Dict[str, Any]]) -> str:
    A = results["A"]; B = results["B"]; D = results["D"]; E = results["E"]
    lines: List[str] = []

    lines.append("## Probe A: Cross-process refresh")
    lines.append("- Procedure executed: wipe_contacts via socket; drive "
                 "Contacts.app via XCUITest to add `XProbe ContactDaemon`; "
                 "list_contacts via socket; restart reader process to "
                 "double-check.")
    lines.append(f"- Result: {A.get('verdict','?')}")
    lines.append(f"- Timing: {A.get('timing_summary','')}")
    lines.append(f"- Verdict for M4 fix: {A.get('m4_recommendation','?')}")
    lines.append("- Evidence:")
    for ev in A.get("evidence", []):
        lines.append(f"    - {ev}")
    lines.append("")

    lines.append("## Probe B: Birthdays calendar")
    lines.append(f"- Result: {B.get('verdict','?')}")
    lines.append(f"- Timing: {B.get('timing','')}")
    lines.append(
        f"- Verdict for gen_birthday_with_calendar_check: "
        f"{B.get('shippability','?')}"
    )
    lines.append("- Evidence:")
    for ev in B.get("evidence", []):
        lines.append(f"    - {ev}")
    lines.append("")

    lines.append("## Probe D: Spotlight indexing")
    lines.append(f"- Result: {D.get('verdict','?')}")
    lines.append(f"- Implication for T5 generators: "
                 f"{D.get('implication','')}")
    lines.append("- Evidence:")
    for ev in D.get("evidence", []):
        lines.append(f"    - {ev}")
    lines.append("")

    lines.append("## Probe E: wipe_contacts safety")
    pre = next((ev for ev in E.get("evidence", [])
                if ev.startswith("pre_wipe_count=")), "")
    post = next((ev for ev in E.get("evidence", [])
                 if ev.startswith("post_wipe_count=")), "")
    surv = next((ev for ev in E.get("evidence", [])
                 if ev.startswith("post_wipe_survivors=")), "")
    lines.append(f"- Pre-wipe contact inventory: {pre}")
    lines.append(f"- Post-wipe survivors: {post}")
    lines.append(f"- Survivor detail: {surv}")
    lines.append(f"- Result: {E.get('verdict','?')}")
    lines.append(f"- Recommended exclusions: "
                 f"{E.get('recommended_exclusions','')}")
    lines.append("")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════
#  Main
# ════════════════════════════════════════════════════════════════════════

async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Output directory: {OUT_DIR}")
    print(f"UDID: {UDID}")
    print(f"Start time: {datetime.now().isoformat(timespec='seconds')}")

    results: Dict[str, Dict[str, Any]] = {}

    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()

    try:
        # Probe A — may stop()+restart the reader. We re-build the reader
        # if needed.
        results["A"] = await probe_a(reader)

        # If reader was stopped during A (PASS path restarts process),
        # rebuild it for B/D/E.
        if reader._sock is None:
            reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
            await reader.start()

        results["B"] = await probe_b(reader)
        results["D"] = await probe_d(reader)
        results["E"] = await probe_e(reader)
    finally:
        try:
            await reader.stop()
        except Exception:
            pass

    # Write outputs
    json_path = os.path.join(OUT_DIR, "results.json")
    md_path = os.path.join(OUT_DIR, "report.md")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    report = render_report(results)
    with open(md_path, "w") as f:
        f.write(report)

    print("\n" + "=" * 72)
    print("FINAL REPORT")
    print("=" * 72)
    print(report)
    print(f"\nArtifacts:\n  {json_path}\n  {md_path}\n  {OUT_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
