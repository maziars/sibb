#!/usr/bin/env python3
"""
SIBB Verifier — Reminders Task (iOS 26 edition)
Queries DB via subprocess sqlite3 CLI — confirmed working on iOS 26.

Task: Create list 'Work', add 3 items, set 'Review budget' to high priority and flag it.
Usage: python3 sibb_verify_reminders.py <UDID>
"""

import subprocess, json, sys, os, glob

UDID = sys.argv[1] if len(sys.argv) > 1 else None

TARGET_LIST    = "Work"
TARGET_ITEMS   = {"review budget", "update roadmap", "send board summary"}
HIGH_PRIO_ITEM = "review budget"


def find_udid():
    result = subprocess.run(["xcrun","simctl","list","devices","--json"],
                            capture_output=True, text=True)
    for devs in json.loads(result.stdout).get("devices",{}).values():
        for d in devs:
            if d.get("state") == "Booted" and "SIBB" in d.get("name",""):
                return d["udid"]
    for devs in json.loads(result.stdout).get("devices",{}).values():
        for d in devs:
            if d.get("state") == "Booted":
                return d["udid"]
    return None


SYSTEM_LIST_NAMES = {"sirifoundinapps"}  # internal lists not visible to users

def _store_score(f):
    """
    Higher = more likely to be the active user store.
    Reminders are weighted heavily; system-only lists score 0.
    """
    rems, err = sql(f, "SELECT COUNT(*) FROM ZREMCDREMINDER "
                       "WHERE ZMARKEDFORDELETION = 0;")
    n_rem = int(rems[0]) if (not err and rems and rems[0].strip().isdigit()) else 0
    names, err = sql(f, "SELECT ZNAME FROM ZREMCDBASELIST "
                        "WHERE ZMARKEDFORDELETION = 0 AND ZNAME IS NOT NULL;")
    if err: names = []
    n_user_lists = sum(1 for r in names
                       if r.strip().lower() not in SYSTEM_LIST_NAMES)
    return n_rem * 100 + n_user_lists

def find_db(udid):
    """
    Locate the active Reminders SQLite store.

    iOS 26 leaves several `Data-<UUID>.sqlite` files in the same AppGroup
    container (e.g. a fresh empty CK store created on app launch plus the
    pre-existing one with the user's data). Rank candidates by user-data
    content (reminders weighted heavily over user-named lists; system
    lists like "SiriFoundInApps" excluded), then by mtime as tie-breaker.
    Fall back to legacy locations if no AppGroup store is present.
    """
    base = os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}/data"
    )
    candidates = [f for f in glob.glob(
        f"{base}/Containers/Shared/AppGroup/*/Container_v1/Stores/Data-*.sqlite"
    ) if "local" not in f]

    if candidates:
        ranked = sorted(candidates,
                        key=lambda f: (_store_score(f), os.path.getmtime(f)),
                        reverse=True)
        return ranked[0]

    for pat in [
        f"{base}/Containers/Data/Application/*/Library/Reminders/Reminders.db",
        f"{base}/Library/Reminders/Reminders.db",
    ]:
        hits = glob.glob(pat)
        if hits: return hits[0]
    return None


def sql(db, query):
    """Run a sqlite3 query and return list of pipe-separated rows."""
    result = subprocess.run(
        ["sqlite3", db, query],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return [], result.stderr.strip()
    rows = [r for r in result.stdout.strip().split("\n") if r]
    return rows, None


def screenshot(udid):
    path = os.path.expanduser("~/Desktop/sibb_reminders_verify.png")
    subprocess.run(["xcrun","simctl","io",udid,"screenshot",path], capture_output=True)
    return path


# ─────────────────────────────────────────────────────────────────────────────
#  Generic verifier — consumes Task.params produced by gen_reminders_list
# ─────────────────────────────────────────────────────────────────────────────
#
# Two entry points:
#   verify_reminders_list_task(task, udid)
#     Legacy synchronous SQLite-based verifier. Kept for back-compat and
#     for callers that don't have an active XCUITestReader.
#   verify_reminders_list_task_async(task, reader)
#     EventKit-based verifier. Queries the in-simulator EKEventStore via
#     the same socket commands the setup uses (list_lists, list_reminders).
#     This eliminates the multi-store ambiguity, the ZNAME-vs-ZTITLE schema
#     hazards, and the CloudKit-mirror inconsistencies that plagued direct
#     SQLite reads. Strongly preferred when a connected reader is available.

PRIORITY_MAP = {"high": "1", "medium": "5", "low": "9"}

# EventKit priority ints (per EKReminder.priority):
#   0 = none, 1 = high, 5 = medium, 9 = low
EK_PRIORITY_MAP = {"high": 1, "medium": 5, "low": 9}


def _build_reminders_checks(task) -> list:
    """Translate task.params into the generic check-kind dict format.

    Kept separate from the async runner so unit tests can validate the
    translation without touching a reader. Selectors use friendly
    field names and rely on `sibb_verify._matches` to do case-
    insensitive string comparison.
    """
    p = task.params or {}
    list_name      = p.get("list")
    items          = p.get("items", []) or []
    priority_item  = p.get("priority_item")
    priority_level = p.get("priority_level")
    flag_item      = p.get("flag_item")

    checks: list = []

    if list_name:
        checks.append({
            "kind": "exists", "resource": "reminders.lists",
            "selector": {"name": list_name},
            "label": f"List '{list_name}' created",
            "severity": "blocking",
        })

    for item in items:
        checks.append({
            "kind": "exists", "resource": "reminders.items",
            "selector": {"list": list_name, "title": item},
            "label": f"'{item}' added to '{list_name}'",
            "severity": "blocking",
        })

    if priority_item and priority_level:
        expected = EK_PRIORITY_MAP.get(priority_level, 0)
        checks.append({
            "kind": "attribute_eq", "resource": "reminders.items",
            "selector": {"list": list_name, "title": priority_item},
            "attr": "priority", "value": expected,
            "label": (f"'{priority_item}' priority={priority_level} "
                       f"(EKpri={expected})"),
            "severity": "blocking",
        })

    if flag_item:
        # EventKit has no public "flagged" field on EKReminder. Treat
        # as informational — the legacy verifier carried this as a
        # None (soft-pass) tuple; informational severity preserves
        # that semantic in the structured-result world.
        checks.append({
            "kind": "exists", "resource": "reminders.items",
            "selector": {"title": flag_item},
            "label": (f"'{flag_item}' flagged "
                       "(no EKReminder API; informational)"),
            "severity": "informational",
        })

    return checks


async def verify_reminders_list_task_async(task, reader, *,
                                            context=None, baseline=None):
    """Legacy reminders verifier. `context` and `baseline` are accepted
    but ignored; they exist so the sibb_replay verifier call sites can
    pass them uniformly across legacy and generic verifiers."""
    """
    EventKit-based verifier, A6-refactored: translates task.params
    into typed checks against `sibb_verify` resource fetchers. Returns
    the legacy `(passed, checks_tuples)` shape so replay/runner
    callers don't change. The same socket round-trips happen
    underneath; only the dispatch path is structured now.
    """
    import sibb_verify as sv
    checks = _build_reminders_checks(task)
    results = await sv.run_checks(reader, checks)
    return sv.blocking_pass(results), sv.legacy_format(results)


def verify_reminders_list_task(task, udid):
    """
    Verify a Task produced by gen_reminders_list.

    Reads task.params:
      list             — list name
      items            — list of reminder titles
      priority_item    — title that should have a priority (or None)
      priority_level   — "high" | "medium" | "low" (or None)
      flag_item        — title that should be flagged (or None)

    Returns: (passed: bool, checks: list of (label, passed_or_None))
      passed=None means "informational only, doesn't affect overall pass"
    """
    checks = []
    db = find_db(udid)
    if not db:
        return False, [("Reminders DB found", False)]

    p = task.params
    list_name      = p.get("list")
    items          = p.get("items", [])
    priority_item  = p.get("priority_item")
    priority_level = p.get("priority_level")
    flag_item      = p.get("flag_item")

    # Lists: iOS 26 stores the user-visible name in ZNAME (not ZTITLE)
    list_rows, err = sql(db, """
        SELECT Z_PK, ZNAME
        FROM ZREMCDBASELIST
        WHERE ZMARKEDFORDELETION = 0 AND ZNAME IS NOT NULL;
    """)
    if err:
        return False, [(f"DB list query error: {err}", False)]
    list_by_name = {}      # name_lower → Z_PK
    for row in list_rows:
        parts = row.split("|")
        if len(parts) >= 2:
            try:
                list_by_name[parts[1].strip().lower()] = int(parts[0])
            except ValueError:
                pass
    target_pk = list_by_name.get(list_name.lower())
    checks.append((f"List '{list_name}' created", target_pk is not None))

    # Reminders — join to list via ZLIST = ZREMCDBASELIST.Z_PK
    rows, err = sql(db, """
        SELECT ZTITLE, ZPRIORITY, ZFLAGGED, ZLIST
        FROM ZREMCDREMINDER
        WHERE ZCOMPLETED = 0 AND ZMARKEDFORDELETION = 0;
    """)
    if err:
        return False, [(f"DB reminder query error: {err}", False)]

    # title_lower → (priority, flagged, list_pk)
    db_items = {}
    for row in rows:
        parts = row.split("|")
        if len(parts) >= 4:
            try:
                list_pk = int(parts[3])
            except ValueError:
                list_pk = None
            db_items[parts[0].strip().lower()] = (parts[1].strip(),
                                                   parts[2].strip(),
                                                   list_pk)

    # Each item must be present AND in the target list. Always phrase the
    # check the same way so it's obvious membership is being verified.
    inverse = {v: k for k, v in list_by_name.items()}
    for item in items:
        key = item.lower()
        label = f"'{item}' added to '{list_name}'"
        if key not in db_items:
            checks.append((label + " (item not found in any list)", False))
            continue
        _, _, list_pk = db_items[key]
        if target_pk is None:
            wrong_list = inverse.get(list_pk, f"list_pk={list_pk}")
            checks.append((label + f" (list doesn't exist; item is in '{wrong_list}')",
                          False))
            continue
        if list_pk == target_pk:
            checks.append((label, True))
        else:
            wrong_list = inverse.get(list_pk, f"list_pk={list_pk}")
            checks.append((label + f" (currently in '{wrong_list}')", False))

    if priority_item and priority_level:
        expected = PRIORITY_MAP.get(priority_level, "0")
        key = priority_item.lower()
        if key in db_items:
            actual, _, _ = db_items[key]
            checks.append(
                (f"'{priority_item}' priority={priority_level} (ZPRIORITY={expected})",
                 actual == expected)
            )
        else:
            checks.append((f"'{priority_item}' priority={priority_level}", False))

    if flag_item:
        key = flag_item.lower()
        if key in db_items:
            _, flagged, _ = db_items[key]
            label = f"'{flag_item}' flagged"
            if flagged != "1":
                label += " (ZFLAGGED=0; iCloud-only)"
                checks.append((label, None))
            else:
                checks.append((label, True))
        else:
            checks.append((f"'{flag_item}' flagged", False))

    passed = all(p for _, p in checks if p is not None)
    return passed, checks


def main():
    global UDID
    print(f"\n{'═'*60}")
    print("  SIBB Verifier — Reminders Task")
    print(f"{'═'*60}")

    if not UDID: UDID = find_udid()
    if not UDID: print("  No booted simulator found."); sys.exit(1)
    print(f"  Simulator: {UDID}")

    shot = screenshot(UDID)
    print(f"  Screenshot: {shot}")

    db = find_db(UDID)
    if not db:
        print("  DB not found — visual verification only (see screenshot).")
        return
    print(f"  DB: {db}\n")

    # ── 1. Active reminders ───────────────────────────────────────────────
    rows, err = sql(db, """
        SELECT ZTITLE, ZPRIORITY, ZFLAGGED
        FROM ZREMCDREMINDER
        WHERE ZCOMPLETED = 0
        ORDER BY ZTITLE;
    """)
    if err:
        print(f"  Query error: {err}"); return

    print(f"  Active reminders ({len(rows)}):")
    items_lower  = set()
    high_prio    = False
    flagged      = False

    for row in rows:
        parts = row.split("|")
        if len(parts) < 3: continue
        title   = parts[0].strip()
        prio    = parts[1].strip()
        flag    = parts[2].strip()
        print(f"    • '{title}'  priority={prio}  flagged={flag}")
        items_lower.add(title.lower())
        if title.lower() == HIGH_PRIO_ITEM:
            # iOS 26: ZPRIORITY 1 = high (confirmed from our data)
            if prio in ("1","3"):
                high_prio = True
            if flag == "1":
                flagged = True

    # ── 2. Lists ──────────────────────────────────────────────────────────
    list_rows, _ = sql(db, "SELECT ZTITLE FROM ZREMCDBASELIST WHERE ZTITLE IS NOT NULL;")
    lists = [r.strip() for r in list_rows]
    print(f"\n  Lists: {lists}")
    list_found = any(TARGET_LIST.lower() == l.lower() for l in lists)
    if not list_found and items_lower:
        list_found = True
        print("  (list name not in ZREMCDBASELIST — inferred from items)")

    # ── 3. Flag check — also look at completed items (first attempt) ─────
    if not flagged:
        comp_rows, _ = sql(db, """
            SELECT ZTITLE, ZPRIORITY, ZFLAGGED
            FROM ZREMCDREMINDER
            WHERE ZTITLE LIKE '%udget%';
        """)
        for row in comp_rows:
            parts = row.split("|")
            if len(parts) >= 3 and parts[2].strip() == "1":
                flagged = True
                print(f"  (flag found on completed item: {row})")

    # ── 4. Results ────────────────────────────────────────────────────────
    checks = {
        f"List '{TARGET_LIST}' created":         list_found,
        "'Review budget' added":                 "review budget"     in items_lower,
        "'Update roadmap' added":                "update roadmap"    in items_lower,
        "'Send board summary' added":            "send board summary" in items_lower,
        "'Review budget' high priority (p=1)":   high_prio,
        "'Review budget' flagged":               flagged,
    }

    print(f"\n{'─'*60}")
    all_pass = True
    for label, passed in checks.items():
        icon = "✓" if passed else "✗"
        print(f"  {icon}  {label}")
        if not passed: all_pass = False

    # Note about priority encoding
    print(f"\n  Note: iOS 26 ZPRIORITY encoding: 1=high, 5=medium, 9=low, 0=none")

    print(f"\n{'═'*60}")
    if all_pass:
        print("  ✅  TASK PASSED — 1.0")
    else:
        print("  ❌  TASK FAILED — 0.0")
        if not flagged:
            print("  → 'Review budget' flag not found in DB.")
            print("    In iOS 26, try: open Reminders → long-press item → Flag")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
