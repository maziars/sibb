#!/usr/bin/env python3
"""
SIBB Compatibility Audit
=========================
Run once per iOS simulator version to:
  1. Test each (app, action) pair — pass / fail_silent / fail_error / unavailable
  2. Discover first-launch suppression keys for each app (calls sibb_discover_keys)
  3. Output compatibility_ios<VERSION>.json — consumed by task generator
  4. Output updated suppression key block for sibb_prewarm.sh

Usage:
    python3 sibb_compatibility_audit.py <UDID>
    python3 sibb_compatibility_audit.py <UDID> --app Reminders
    python3 sibb_compatibility_audit.py <UDID> --discover-keys-only
    python3 sibb_compatibility_audit.py <UDID> --output-dir ./audit_results

Schedule: run within 1 week of each new iOS simulator runtime release.
"""

import subprocess, json, sys, os, time, plistlib, glob, argparse, sqlite3
import shutil, tempfile
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
#  AUDIT ACTION DEFINITIONS
#  Each entry: (action_name, verify_fn)
#  verify_fn takes (udid, sim_data) and returns True/False
# ─────────────────────────────────────────────────────────────────────────────

AUDIT_ACTIONS = {

    "Reminders": [
        ("create_list",       "create a Reminders list named 'AuditList'"),
        ("add_item",          "add reminder 'AuditItem' to 'AuditList'"),
        ("set_priority_high", "set 'AuditItem' to high priority"),
        ("flag_item",         "flag 'AuditItem'"),
        ("set_due_date",      "set due date on 'AuditItem'"),
        ("add_tag",           "add tag 'audit' to list"),
    ],

    "Calendar": [
        ("create_event",      "create event 'AuditEvent' tomorrow at 9am"),
        ("set_alert",         "set 15-minute alert on 'AuditEvent'"),
        ("set_recurrence",    "set weekly recurrence on 'AuditEvent'"),
        ("add_location",      "add location to 'AuditEvent'"),
        ("create_calendar",   "create new calendar 'AuditCal'"),
    ],

    "Contacts": [
        ("create_contact",    "create contact 'Audit Person'"),
        ("add_phone",         "add phone number to 'Audit Person'"),
        ("add_email",         "add email to 'Audit Person'"),
        ("add_birthday",      "add birthday to 'Audit Person'"),
        ("create_group",      "create contact group 'AuditGroup'"),
    ],

    "Settings": [
        ("configure_focus",   "configure a Focus mode"),
        ("toggle_wifi",       "toggle WiFi off then on"),
        ("set_screen_time",   "set Screen Time limit for an app"),
        ("toggle_dark_mode",  "toggle dark mode"),
        ("notification_settings", "configure notification settings for an app"),
    ],

    "Files": [
        ("create_folder",     "create folder 'AuditFolder' in Files"),
        ("create_file",       "create text file 'audit.txt'"),
        ("move_file",         "move 'audit.txt' to 'AuditFolder'"),
        ("rename_file",       "rename 'audit.txt' to 'audit_renamed.txt'"),
        ("delete_file",       "delete 'audit_renamed.txt'"),
    ],

    "Health": [
        ("log_workout",       "log a 30-min walk workout"),
        ("log_water",         "log 250ml water intake"),
        ("add_medication",    "add medication 'AuditMed' to tracking"),
        ("set_sleep_goal",    "set sleep goal to 8 hours"),
    ],

    "Maps": [
        ("search_place",      "search for 'Golden Gate Park'"),
        ("save_pin",          "save a pin to Places"),
        ("get_directions",    "get driving directions to an address"),
    ],

    "Photos": [
        ("create_album",      "create album 'AuditAlbum'"),
        ("add_to_album",      "add a photo to 'AuditAlbum'"),
        ("share_photo",       "share a photo via Messages"),
    ],

    "Shortcuts": [
        ("create_shortcut",   "create a new shortcut 'AuditShortcut'"),
        ("add_action",        "add a Reminders action to the shortcut"),
        ("run_shortcut",      "run 'AuditShortcut'"),
        ("add_condition",     "add an If condition to the shortcut"),
    ],

    "Safari": [
        ("browse_url",        "navigate to apple.com"),
        ("save_reading_list", "add page to Reading List"),
        ("add_bookmark",      "bookmark the current page"),
        ("open_new_tab",      "open a new tab"),
    ],

    "Messages": [
        ("compose_message",   "compose a message (not send)"),
        ("share_content",     "share a Maps pin via Messages"),
    ],
}

# Permission types to grant per app
APP_PERMISSIONS = {
    "Reminders":  ["reminders"],
    "Calendar":   ["calendar"],
    "Contacts":   ["contacts"],
    "Settings":   [],
    "Files":      [],
    "Health":     ["health"],
    "Maps":       ["location", "location-always"],
    "Photos":     ["photos"],
    "Shortcuts":  [],
    "Safari":     [],
    "Messages":   ["contacts"],
}

BUNDLE_IDS = {
    "Reminders":  "com.apple.reminders",
    "Calendar":   "com.apple.mobilecal",
    "Contacts":   "com.apple.MobileAddressBook",
    "Settings":   "com.apple.Preferences",
    "Files":      "com.apple.DocumentsApp",
    "Health":     "com.apple.Health",
    "Maps":       "com.apple.Maps",
    "Photos":     "com.apple.mobileslideshow",
    "Shortcuts":  "com.apple.shortcuts",
    "Safari":     "com.apple.mobilesafari",
    "Messages":   "com.apple.MobileSMS",
}


# ─────────────────────────────────────────────────────────────────────────────
#  CORE UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def get_sim_info(udid: str) -> dict:
    result = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "--json"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    for runtime_key, devices in data.get("devices", {}).items():
        for d in devices:
            if d["udid"] == udid:
                # Extract iOS version from runtime key
                # e.g. com.apple.CoreSimulator.SimRuntime.iOS-26-3
                ios_ver = runtime_key.split("iOS-")[-1].replace("-", ".") \
                          if "iOS-" in runtime_key else "unknown"
                return {
                    "udid":     udid,
                    "name":     d.get("name"),
                    "state":    d.get("state"),
                    "ios":      ios_ver,
                    "runtime":  runtime_key,
                }
    return {"udid": udid, "ios": "unknown"}


def get_sim_data_path(udid: str) -> str:
    return os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}/data"
    )


def run_simctl(udid: str, *args) -> tuple[bool, str]:
    result = subprocess.run(
        ["xcrun", "simctl"] + list(args),
        capture_output=True, text=True
    )
    return result.returncode == 0, result.stdout + result.stderr


def grant_permissions(udid: str, app: str):
    bundle = BUNDLE_IDS[app]
    perms  = APP_PERMISSIONS.get(app, [])
    for perm in perms:
        subprocess.run(
            ["xcrun", "simctl", "privacy", udid, "grant", perm, bundle],
            capture_output=True
        )
    # Always grant 'all' as a catch-all
    subprocess.run(
        ["xcrun", "simctl", "privacy", udid, "grant", "all", bundle],
        capture_output=True
    )


def launch_app(udid: str, app: str) -> bool:
    ok, _ = run_simctl(udid, "launch", udid, BUNDLE_IDS[app])
    return ok


def terminate_app(udid: str, app: str):
    run_simctl(udid, "terminate", udid, BUNDLE_IDS[app])


# ─────────────────────────────────────────────────────────────────────────────
#  KEY DISCOVERY  (from sibb_discover_keys.py, integrated inline)
# ─────────────────────────────────────────────────────────────────────────────

def read_all_prefs(sim_data: str) -> dict:
    prefs = {}
    pref_dir = os.path.join(sim_data, "Library", "Preferences")
    for path in glob.glob(f"{pref_dir}/*.plist"):
        domain = os.path.basename(path).replace(".plist", "")
        try:
            with open(path, "rb") as f:
                prefs[domain] = plistlib.load(f)
        except Exception:
            prefs[domain] = {}
    return prefs


def diff_prefs(before: dict, after: dict) -> dict:
    changes = {}
    for domain in set(before) | set(after):
        b = before.get(domain, {})
        a = after.get(domain, {})
        domain_changes = {}
        for key in set(b) | set(a):
            bv, av = b.get(key), a.get(key)
            if bv != av:
                domain_changes[key] = {"before": bv, "after": av}
        if domain_changes:
            changes[domain] = domain_changes
    return changes


LAUNCH_KEY_WORDS = [
    "welcome", "launch", "onboard", "shown", "setup",
    "first", "intro", "tutorial", "icloud", "sync",
    "agreed", "seen", "complete", "did", "have",
    "migration", "asked", "request", "prompt",
]

def discover_keys_for_app(udid: str, app: str, sim_data: str) -> dict:
    """
    Launch app, diff prefs before/after, return suppression key commands.
    """
    print(f"    Discovering keys for {app}...")
    before = read_all_prefs(sim_data)

    grant_permissions(udid, app)
    launch_app(udid, app)
    time.sleep(5)   # wait for first-launch logic

    after = read_all_prefs(sim_data)
    terminate_app(udid, app)

    changes = diff_prefs(before, after)

    # Filter to likely suppression keys
    suppression_commands = []
    all_changes = {}

    for domain, keys in sorted(changes.items()):
        for key, vals in sorted(keys.items()):
            av = vals["after"]
            all_changes[f"{domain}.{key}"] = av

            if any(w in key.lower() for w in LAUNCH_KEY_WORDS):
                if isinstance(av, bool):
                    t, v = "-bool", "YES" if av else "NO"
                elif isinstance(av, int):
                    t, v = "-integer", str(av)
                elif isinstance(av, float):
                    t, v = "-float", str(av)
                elif isinstance(av, str):
                    t, v = "-string", f'"{av}"'
                else:
                    continue
                suppression_commands.append(
                    f"sim_defaults {domain} {key} {t} {v}"
                )

    return {
        "all_changes":           all_changes,
        "suppression_commands":  suppression_commands,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ACTION VERIFICATION
#  Each action is tested manually by the user and rated:
#  "pass" | "fail_silent" | "fail_error" | "unavailable" | "requires_icloud"
# ─────────────────────────────────────────────────────────────────────────────

def audit_app_actions(udid: str, app: str, sim_data: str) -> dict:
    """
    For each action in the app, prompt the user to test it and record result.
    This is intentionally manual — automated DB verification would duplicate
    the task generator's verifier logic. The audit is about capability detection,
    not full task execution.
    """
    bundle  = BUNDLE_IDS[app]
    actions = AUDIT_ACTIONS.get(app, [])
    results = {}

    print(f"\n  {'─'*50}")
    print(f"  Auditing: {app} ({bundle})")
    print(f"  {'─'*50}")

    # Launch the app
    ok = launch_app(udid, app)
    if not ok:
        print(f"  ✗ Could not launch {app} — marking all actions 'unavailable'")
        for action_name, _ in actions:
            results[action_name] = "unavailable"
        return results

    print(f"  ✓ {app} launched. The app is now open in the simulator.")
    print(f"  For each action below, try it in the simulator and report the result.")
    print()

    for action_name, description in actions:
        print(f"  Action: {action_name}")
        print(f"  Task:   {description}")
        print()
        print(f"  Result? [p=pass / s=fail_silent / e=fail_error / u=unavailable / c=requires_icloud / q=quit app]")
        rating = input(f"  > ").strip().lower()

        result_map = {
            "p": "pass",
            "s": "fail_silent",
            "e": "fail_error",
            "u": "unavailable",
            "c": "requires_icloud",
            "q": "quit",
        }
        result = result_map.get(rating, "unknown")

        if result == "quit":
            terminate_app(udid, app)
            break

        results[action_name] = result
        print(f"  Recorded: {result}")
        print()

    return results


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SIBB compatibility audit — tests actions and discovers suppression keys."
    )
    parser.add_argument("udid", help="Simulator UDID")
    parser.add_argument("--app", default=None,
                        help="Audit only this app (default: all SIBB-11 apps)")
    parser.add_argument("--discover-keys-only", action="store_true",
                        help="Only run key discovery, skip manual action audit")
    parser.add_argument("--audit-only", action="store_true",
                        help="Only run action audit, skip key discovery")
    parser.add_argument("--output-dir", default=".",
                        help="Directory to write output files (default: current dir)")
    args = parser.parse_args()

    udid     = args.udid
    sim_info = get_sim_info(udid)
    sim_data = get_sim_data_path(udid)
    ios_ver  = sim_info.get("ios", "unknown").replace(".", "_")

    print()
    print("═" * 60)
    print("  SIBB Compatibility Audit")
    print(f"  Simulator: {sim_info.get('name')} ({udid})")
    print(f"  iOS:       {sim_info.get('ios')}")
    print(f"  State:     {sim_info.get('state')}")
    print("═" * 60)

    if sim_info.get("state") != "Booted":
        print(f"\n  ERROR: Simulator is not booted.")
        print(f"  Run: xcrun simctl boot {udid}")
        sys.exit(1)

    apps_to_audit = [args.app] if args.app else list(AUDIT_ACTIONS.keys())
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    results = {
        "metadata": {
            "udid":       udid,
            "ios":        sim_info.get("ios"),
            "runtime":    sim_info.get("runtime"),
            "name":       sim_info.get("name"),
            "audit_date": timestamp,
        },
        "apps": {},
    }

    prewarm_commands = []

    for app in apps_to_audit:
        if app not in BUNDLE_IDS:
            print(f"  Unknown app: {app} — skipping")
            continue

        results["apps"][app] = {
            "bundle_id":            BUNDLE_IDS[app],
            "actions":              {},
            "suppression_commands": [],
            "all_pref_changes":     {},
        }

        # Key discovery
        if not args.audit_only:
            print(f"\n  Discovering keys for {app}...")
            key_data = discover_keys_for_app(udid, app, sim_data)
            results["apps"][app]["suppression_commands"] = \
                key_data["suppression_commands"]
            results["apps"][app]["all_pref_changes"] = \
                key_data["all_changes"]
            prewarm_commands.extend(key_data["suppression_commands"])

            if key_data["suppression_commands"]:
                print(f"    Found {len(key_data['suppression_commands'])} suppression keys")
                for cmd in key_data["suppression_commands"]:
                    print(f"      {cmd}")
            else:
                print(f"    No suppression keys found for {app}")

        # Manual action audit
        if not args.discover_keys_only:
            action_results = audit_app_actions(udid, app, sim_data)
            results["apps"][app]["actions"] = action_results

            # Flag actions that need updating in task generator
            broken = [a for a, r in action_results.items()
                      if r in ("fail_silent", "fail_error", "unavailable",
                               "requires_icloud")]
            if broken:
                print(f"  ⚠  Broken actions in {app}: {', '.join(broken)}")
                results["apps"][app]["broken_actions"] = broken

    # ── Write compatibility JSON ──────────────────────────────────────────────
    compat_path = os.path.join(output_dir, f"compatibility_ios{ios_ver}.json")
    with open(compat_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Compatibility matrix: {compat_path}")

    # ── Write updated prewarm suppression block ───────────────────────────────
    prewarm_path = os.path.join(output_dir, f"prewarm_keys_ios{ios_ver}.sh")
    unique_cmds = list(dict.fromkeys(prewarm_commands))   # deduplicate

    with open(prewarm_path, "w") as f:
        f.write(f"# SIBB suppression keys discovered by audit on iOS {sim_info.get('ios')}\n")
        f.write(f"# Generated: {timestamp}\n")
        f.write(f"# Copy this block into sibb_prewarm.sh Step 2\n\n")
        for app in apps_to_audit:
            app_cmds = results["apps"].get(app, {}).get("suppression_commands", [])
            if app_cmds:
                f.write(f"# {app}\n")
                for cmd in app_cmds:
                    f.write(f"{cmd}\n")
                f.write("\n")
    print(f"  Updated prewarm keys:  {prewarm_path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    if not args.discover_keys_only:
        print()
        print("═" * 60)
        print("  ACTION AUDIT SUMMARY")
        print("═" * 60)

        all_actions = 0
        broken_actions = 0
        for app, data in results["apps"].items():
            for action, status in data.get("actions", {}).items():
                all_actions += 1
                if status not in ("pass",):
                    broken_actions += 1
                    print(f"  ✗ {app}.{action}: {status}")

        passing = all_actions - broken_actions
        print()
        print(f"  Passing: {passing}/{all_actions}")
        print(f"  Broken:  {broken_actions}/{all_actions}")
        print()
        print("  Update sibb_task_generator_v3.py APP_REGISTRY:")
        print("  Set include_prob=0.0 for broken actions in OptionalParam.")

    print()
    print("  Done.")


if __name__ == "__main__":
    main()
