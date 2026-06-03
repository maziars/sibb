#!/usr/bin/env python3
"""
SIBB Plist Key Discoverer
==========================
Discovers unknown first-launch suppression keys for iOS 26 by:
  1. Taking a snapshot of all simulator preferences BEFORE launching an app
  2. Launching the app and waiting
  3. Taking a snapshot AFTER
  4. Diffing to find which keys were written

This tells you exactly which defaults keys to write in sibb_prewarm.sh
to suppress first-launch dialogs for any app.

Usage:
    python3 sibb_discover_keys.py <UDID> <bundle_id>
    python3 sibb_discover_keys.py 19B95A95-... com.apple.reminders
"""

import subprocess, json, sys, os, time, plistlib, glob

UDID      = sys.argv[1] if len(sys.argv) > 1 else "booted"
BUNDLE_ID = sys.argv[2] if len(sys.argv) > 2 else None

if not BUNDLE_ID:
    print("Usage: python3 sibb_discover_keys.py <UDID> <bundle_id>")
    sys.exit(1)


def get_sim_data_path(udid):
    if udid == "booted":
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "--json"],
            capture_output=True, text=True
        )
        for devs in json.loads(result.stdout).get("devices", {}).values():
            for d in devs:
                if d.get("state") == "Booted":
                    udid = d["udid"]
                    break
    return os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}/data"
    )


def read_all_prefs(sim_data: str) -> dict:
    """Read all plist files in Library/Preferences."""
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
    """Find all keys that were added or changed."""
    changes = {}
    all_domains = set(before) | set(after)
    for domain in all_domains:
        b = before.get(domain, {})
        a = after.get(domain, {})
        domain_changes = {}
        for key in set(b) | set(a):
            bv = b.get(key)
            av = a.get(key)
            if bv != av:
                domain_changes[key] = {"before": bv, "after": av}
        if domain_changes:
            changes[domain] = domain_changes
    return changes


def launch_app(udid, bundle_id):
    subprocess.run(
        ["xcrun", "simctl", "launch", udid, bundle_id],
        capture_output=True
    )


def terminate_app(udid, bundle_id):
    subprocess.run(
        ["xcrun", "simctl", "terminate", udid, bundle_id],
        capture_output=True
    )


def main():
    sim_data = get_sim_data_path(UDID)
    print(f"Simulator data: {sim_data}")
    print(f"App to profile: {BUNDLE_ID}")
    print()

    # Before snapshot
    print("Taking pre-launch snapshot...")
    before = read_all_prefs(sim_data)

    # Launch app and wait
    print(f"Launching {BUNDLE_ID}...")
    launch_app(UDID, BUNDLE_ID)
    print("Waiting 5 seconds for first-launch logic to complete...")
    time.sleep(5)

    # After snapshot
    print("Taking post-launch snapshot...")
    after = read_all_prefs(sim_data)

    # Terminate
    terminate_app(UDID, BUNDLE_ID)
    print(f"Terminated {BUNDLE_ID}.")

    # Diff
    changes = diff_prefs(before, after)

    print()
    print("═" * 60)
    print(f"  Keys written during first launch of {BUNDLE_ID}")
    print("═" * 60)

    if not changes:
        print("  No plist changes detected.")
        print("  App may not write first-launch keys, or they're in a")
        print("  different location (e.g., app container, not shared prefs).")
        return

    # Show all changes
    for domain, keys in sorted(changes.items()):
        print(f"\n  {domain}:")
        for key, vals in sorted(keys.items()):
            before_v = vals["before"]
            after_v  = vals["after"]
            if before_v is None:
                print(f"    + {key} = {after_v!r}  (NEW)")
            else:
                print(f"    ~ {key}: {before_v!r} → {after_v!r}")

    # Generate the suppression commands
    print()
    print("═" * 60)
    print("  Generated suppression commands for sibb_prewarm.sh:")
    print("═" * 60)

    for domain, keys in sorted(changes.items()):
        # Only show keys that look like first-launch indicators
        launch_keys = {
            k: v for k, v in keys.items()
            if any(word in k.lower() for word in [
                "welcome", "launch", "onboard", "shown", "setup",
                "first", "intro", "tutorial", "icloud", "sync",
                "agreed", "seen", "complete", "did", "have",
            ])
        }
        if launch_keys:
            for key, vals in launch_keys.items():
                av = vals["after"]
                if isinstance(av, bool):
                    t, v = "-bool", "YES" if av else "NO"
                elif isinstance(av, int):
                    t, v = "-integer", str(av)
                elif isinstance(av, float):
                    t, v = "-float", str(av)
                elif isinstance(av, str):
                    t, v = "-string", f'"{av}"'
                else:
                    t, v = "-string", f'"{av}"'
                print(f'  sim_defaults {domain} {key} {t} {v}')


if __name__ == "__main__":
    main()
