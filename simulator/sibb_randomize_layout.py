#!/usr/bin/env python3
"""
SIBB SpringBoard Layout Randomizer
====================================
Randomizes the position of apps on the iOS Simulator home screen
by shuffling bundle ID entries in IconState.plist.

Rules:
  - Only plain app entries (strings) are shuffled — widgets and folders stay put
  - Shuffles within each page independently OR across all pages (configurable)
  - Dock (buttonBar) can optionally be shuffled too
  - Simulator must be SHUT DOWN before running, then rebooted after

Usage:
    # Shut down simulator first
    xcrun simctl shutdown <UDID>

    # Randomize layout
    python3 sibb_randomize_layout.py <UDID> [--seed 42] [--cross-page] [--dock]

    # Reboot
    xcrun simctl boot <UDID>
    open -a Simulator

Example (in episode reset loop):
    xcrun simctl shutdown $EPISODE_UDID
    python3 sibb_randomize_layout.py $EPISODE_UDID --seed $EPISODE_NUMBER
    xcrun simctl boot $EPISODE_UDID
"""

import subprocess
import plistlib
import random
import shutil
import sys
import os
import argparse
import json
from datetime import datetime
from typing import Optional


SIBB_APPS = {
    # SIBB-11 available apps — these get shuffled
    "com.apple.reminders",
    "com.apple.mobilecal",
    "com.apple.MobileAddressBook",
    "com.apple.Preferences",
    "com.apple.DocumentsApp",
    "com.apple.Health",
    "com.apple.Maps",
    "com.apple.mobileslideshow",
    "com.apple.shortcuts",
    "com.apple.mobilesafari",
    "com.apple.MobileSMS",
}

# Hook for excluding specific bundles from the shuffle pool. Currently
# empty — even our own xcodebuild-installed test runner participates in
# the randomization, on the principle that the agent should see the
# device's *real* state including any artifacts that happen to be there.
# Add bundle IDs here if a future need surfaces (e.g. a system-critical
# app that breaks when moved).
EXCLUDE_APPS: set = set()


def _is_shufflable_app(item, sibb_only: bool) -> bool:
    """Single source of truth for which apps participate in shuffles."""
    if not is_app_entry(item):
        return False
    if item in EXCLUDE_APPS:
        return False
    if sibb_only and item not in SIBB_APPS:
        return False
    return True


def find_icon_state(udid: str) -> str:
    path = os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}"
        f"/data/Library/SpringBoard/IconState.plist"
    )
    if not os.path.exists(path):
        # Fresh / erased sims don't have IconState.plist until
        # SpringBoard launches at least once. Hint the user.
        raise FileNotFoundError(
            f"IconState.plist not found at {path}.\n"
            f"  This sim has never booted SpringBoard, or was just erased.\n"
            f"  Recover with:\n"
            f"    xcrun simctl boot {udid}\n"
            f"    sleep 5     # let SpringBoard launch + persist its state\n"
            f"    xcrun simctl shutdown {udid}\n"
            f"  Then retry the replay command."
        )
    return path


def load_plist(path: str) -> dict:
    with open(path, "rb") as f:
        return plistlib.load(f)


def save_plist(data: dict, path: str):
    with open(path, "wb") as f:
        plistlib.dump(data, f, fmt=plistlib.FMT_BINARY)


def backup_plist(path: str, keep: int = 5) -> str:
    """Snapshot IconState.plist before edit. Prunes older backups beyond
    `keep` so the SpringBoard directory doesn't accumulate clutter
    across many randomize runs."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path + f".backup_{ts}"
    shutil.copy2(path, backup)
    directory = os.path.dirname(path)
    base = os.path.basename(path) + ".backup_"
    older = sorted(
        [os.path.join(directory, f) for f in os.listdir(directory)
         if f.startswith(base)],
        reverse=True,
    )
    for old in older[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return backup


def is_app_entry(item) -> bool:
    """True if item is a plain app bundle ID string."""
    return isinstance(item, str)


def is_widget_or_folder(item) -> bool:
    """True if item is a widget dict or folder dict — leave these alone."""
    return isinstance(item, dict)


def extract_apps_from_page(page: list) -> list:
    """Get all plain app bundle IDs from a page."""
    return [item for item in page if is_app_entry(item)]


def shuffle_page(page: list, rng: random.Random,
                 sibb_only: bool = False) -> list:
    """
    Shuffle app positions within a single page.
    Widgets and folders stay in their original positions.
    Apps fill the remaining slots in shuffled order.
    """
    # Separate apps from non-apps, preserving non-app positions
    non_app_positions = {}   # index → item
    apps = []
    app_positions = []

    for i, item in enumerate(page):
        if _is_shufflable_app(item, sibb_only):
            apps.append(item)
            app_positions.append(i)
        else:
            non_app_positions[i] = item   # widget/folder OR excluded/non-SIBB app

    # Shuffle the apps
    shuffled_apps = apps.copy()
    rng.shuffle(shuffled_apps)

    # Reconstruct page
    result = [None] * len(page)
    for idx, item in non_app_positions.items():
        result[idx] = item
    for pos, app in zip(app_positions, shuffled_apps):
        result[pos] = app

    return result


def redistribute_pages(icon_lists: list, rng: random.Random,
                       sibb_only: bool = True,
                       n_pages: Optional[int] = None,
                       min_per_page: int = 1) -> list:
    """
    Re-distribute SIBB apps across pages with random per-page counts.

    Unlike `shuffle_page` / `shuffle_cross_page` (which preserve each
    page's app *count* and just move apps around), this function changes
    *how many* SIBB apps live on each page. e.g. starting from 11 SIBB
    apps spread evenly over 2 pages (5 + 6), one randomization might
    produce 3 + 2 + 6 over 3 pages, the next 11 + 0 over 2.

    Non-SIBB apps and widgets/folders stay on their original pages — we
    only re-shuffle the SIBB cohort. Result is a list of pages, each a
    list of bundle-ID strings (apps) interleaved with widgets/folders.

    Constraints:
      - At least `min_per_page` SIBB apps per page used (default 1).
      - Total SIBB apps preserved.
      - Existing page count is the cap unless `n_pages` is specified.
    """
    # Pull SIBB apps out of every page; keep everything else where it is.
    sibb_pool: list = []
    page_fixed: list = []   # one list per page of (slot_idx, item) tuples
    for page in icon_lists:
        fixed = []
        for slot_idx, item in enumerate(page):
            if _is_shufflable_app(item, sibb_only):
                sibb_pool.append(item)
            else:
                fixed.append((slot_idx, item))
        page_fixed.append(fixed)

    rng.shuffle(sibb_pool)

    target_n_pages = n_pages if n_pages is not None else len(icon_lists)
    # Can't have more pages than fixed structures + SIBB apps available.
    target_n_pages = max(1, min(target_n_pages, len(sibb_pool)))

    # Pick random per-page counts summing to len(sibb_pool), each ≥ min_per_page.
    counts = _random_partition(len(sibb_pool), target_n_pages,
                               minimum=min_per_page, rng=rng)

    new_pages: list = []
    cursor = 0
    for page_idx in range(target_n_pages):
        n = counts[page_idx]
        sibb_chunk = sibb_pool[cursor:cursor + n]
        cursor += n
        # Reconstruct the page: start with the SIBB apps in their new order,
        # then append any fixed items that were on this page originally.
        # We don't try to preserve slot indices precisely — iOS re-flows on
        # boot; preserving relative order is enough.
        page_items = list(sibb_chunk)
        if page_idx < len(page_fixed):
            page_items.extend(item for _, item in page_fixed[page_idx])
        new_pages.append(page_items)

    return new_pages


def _random_partition(total: int, n: int, minimum: int,
                       rng: random.Random) -> list:
    """
    Return `n` random positive integers ≥ `minimum` summing exactly to `total`.
    Uses the "bars and stars" approach with shuffled gaps. Returns at most
    `total - n*minimum + 1` distinct values per slot.
    """
    if n <= 0:
        return []
    if n * minimum > total:
        # Not enough to give everyone the minimum — distribute as evenly as we can.
        base = total // n
        remainder = total - base * n
        out = [base] * n
        for i in range(remainder):
            out[i] += 1
        return out
    remaining = total - n * minimum
    # Pick n-1 cut points uniformly in [0, remaining], producing n bins.
    cuts = sorted(rng.randint(0, remaining) for _ in range(n - 1))
    cuts = [0] + cuts + [remaining]
    sizes = [cuts[i+1] - cuts[i] for i in range(n)]
    return [s + minimum for s in sizes]


def randomize_dock_contents(data: dict, rng: random.Random,
                             count: Optional[int] = None,
                             sibb_only: bool = True) -> None:
    """
    Replace the dock with a randomly-chosen subset of apps.

    Unlike the older path that only shuffled the order of the existing
    dock entries, this picks BOTH the count (1–4 on iPhone) and the
    actual apps. Widgets/folders inside the dock are preserved at their
    original slot positions.

    `count`:
      None → pick uniformly from [1, 4]
      int  → use that exact count

    `sibb_only`:
      True  → pick from `SIBB_APPS` (so dock apps overlap our task set)
      False → pick from the union of dock+page apps currently visible
    """
    dock = data.get("buttonBar", [])
    fixed = []
    pool_existing: list = []
    for slot_idx, item in enumerate(dock):
        if is_app_entry(item):
            pool_existing.append(item)
        else:
            fixed.append((slot_idx, item))

    if count is None:
        count = rng.randint(1, max(1, min(4, len(SIBB_APPS))))
    count = max(1, min(count, 4))

    # Build the candidate pool. Start from SIBB apps; fall back to existing
    # dock apps if SIBB pool is too small.
    if sibb_only:
        pool = list(SIBB_APPS)
    else:
        pool = list(SIBB_APPS) + [p for p in pool_existing if p not in SIBB_APPS]
    rng.shuffle(pool)
    chosen = pool[:count]

    # Replace the entire dock with chosen apps, preserving any non-app items.
    new_dock = list(chosen)
    for _, item in fixed:
        new_dock.append(item)
    data["buttonBar"] = new_dock


def shuffle_cross_page(icon_lists: list, rng: random.Random,
                       sibb_only: bool = False) -> list:
    """
    Collect all app entries across all pages, shuffle globally,
    then redistribute back — maintaining page sizes and non-app positions.
    """
    # Collect all apps across pages with their page indices
    all_apps = []
    page_app_positions = []   # list of (page_idx, slot_idx) for each app

    for page_idx, page in enumerate(icon_lists):
        positions = []
        for slot_idx, item in enumerate(page):
            if _is_shufflable_app(item, sibb_only):
                all_apps.append(item)
                positions.append(slot_idx)
        page_app_positions.append(positions)

    # Shuffle all apps globally
    rng.shuffle(all_apps)

    # Rebuild icon_lists
    new_icon_lists = [list(page) for page in icon_lists]
    app_iter = iter(all_apps)

    for page_idx, positions in enumerate(page_app_positions):
        for slot_idx in positions:
            try:
                new_icon_lists[page_idx][slot_idx] = next(app_iter)
            except StopIteration:
                break

    return new_icon_lists


def randomize_layout(udid: str,
                     seed: int = None,
                     cross_page: bool = False,
                     shuffle_dock: bool = False,
                     sibb_only: bool = False,   # changed from True → False
                     dry_run: bool = False,
                     distribute: bool = False,
                     n_pages: Optional[int] = None,
                     randomize_dock: bool = False,
                     dock_count: Optional[int] = None) -> dict:
    """
    Main function. Randomizes the SpringBoard layout.

    Variability modes (combine freely; all gated by `seed` for reproducibility):
      cross_page=True    shuffle apps across all pages (otherwise within-page only)
      distribute=True    randomize the *count* of apps per page (in addition to
                          placement). n_pages caps the page count; default uses
                          the existing page count.
      shuffle_dock=True  shuffle existing dock contents (preserve count)
      randomize_dock=True  pick a new dock count (1-4) and new contents
      dock_count=N       force a specific dock count (1-4) — implies randomize_dock

    Returns dict with backup_path, seed, before/after states.
    """
    plist_path = find_icon_state(udid)

    if seed is None:
        seed = random.randint(0, 999999)
    rng = random.Random(seed)

    print(f"  Randomizing layout for {udid}")
    print(f"  Seed: {seed}  cross_page={cross_page}  distribute={distribute}")
    print(f"  shuffle_dock={shuffle_dock}  randomize_dock={randomize_dock}"
          f"  dock_count={dock_count}  sibb_only={sibb_only}")

    # Load
    data = load_plist(plist_path)
    icon_lists = data.get("iconLists", [])

    # Capture before state
    before = {
        f"page_{i}": [item if is_app_entry(item) else "<widget/folder>"
                      for item in page]
        for i, page in enumerate(icon_lists)
    }
    before["dock"] = [item if is_app_entry(item) else "<widget/folder>"
                      for item in data.get("buttonBar", [])]

    # Page-level changes: distribute counts first if requested, otherwise
    # just shuffle in place (cross-page or within-page).
    if distribute:
        new_icon_lists = redistribute_pages(icon_lists, rng, sibb_only,
                                            n_pages=n_pages)
    elif cross_page:
        new_icon_lists = shuffle_cross_page(icon_lists, rng, sibb_only)
    else:
        new_icon_lists = [
            shuffle_page(page, rng, sibb_only)
            for page in icon_lists
        ]

    # Dock changes: prefer the new "pick count + contents" path when
    # `randomize_dock` or an explicit `dock_count` is set. Fall back to
    # the legacy in-place shuffle for back-compat.
    if randomize_dock or dock_count is not None:
        randomize_dock_contents(data, rng, count=dock_count,
                                sibb_only=sibb_only)
    elif shuffle_dock and "buttonBar" in data:
        dock = data["buttonBar"]
        dock_apps = [item for item in dock if is_app_entry(item)]
        rng.shuffle(dock_apps)
        new_dock = []
        app_iter = iter(dock_apps)
        for item in dock:
            if is_app_entry(item):
                new_dock.append(next(app_iter))
            else:
                new_dock.append(item)
        data["buttonBar"] = new_dock

    # iOS silently de-duplicates apps that appear in BOTH the dock and
    # a page — and when it does, the dock entries get hidden visually
    # even though the plist looks correct. To make the dock actually
    # render, strip any app from the pages that we just placed in the
    # dock. (Without this fix, the user sees an empty dock and the
    # apps still appear on a page; with it, the dock renders and the
    # apps are removed from the page they would otherwise duplicate to.)
    dock_apps = {
        item for item in data.get("buttonBar", [])
        if is_app_entry(item)
    }
    if dock_apps:
        new_icon_lists = [
            [item for item in page
             if not (is_app_entry(item) and item in dock_apps)]
            for page in new_icon_lists
        ]

    data["iconLists"] = new_icon_lists

    # Capture after state
    after = {
        f"page_{i}": [item if is_app_entry(item) else "<widget/folder>"
                      for item in page]
        for i, page in enumerate(new_icon_lists)
    }
    after["dock"] = [item if is_app_entry(item) else "<widget/folder>"
                     for item in data.get("buttonBar", [])]

    if dry_run:
        print("  DRY RUN — not writing to disk")
        _print_diff(before, after)
        return {"seed": seed, "dry_run": True}

    # Backup and save
    backup_path = backup_plist(plist_path)
    save_plist(data, plist_path)

    print(f"  Backup: {backup_path}")
    print(f"  Written: {plist_path}")
    _print_diff(before, after)

    return {
        "backup_path": backup_path,
        "seed": seed,
        "before": before,
        "after": after,
    }


def restore_layout(udid: str, backup_path: str):
    """Restore IconState.plist from a backup."""
    plist_path = find_icon_state(udid)
    shutil.copy2(backup_path, plist_path)
    print(f"  Restored from {backup_path}")


def _print_diff(before: dict, after: dict):
    """Print a human-readable diff of page layouts."""
    for page_key in before:
        b = before[page_key]
        a = after.get(page_key, [])
        moved = [(i, b[i], a[i]) for i in range(min(len(b), len(a)))
                 if b[i] != a[i] and b[i] != "<widget/folder>"]
        if moved:
            print(f"\n  {page_key} — {len(moved)} apps moved:")
            for idx, old, new in moved[:6]:
                old_s = old.split(".")[-1] if "." in old else old
                new_s = new.split(".")[-1] if "." in new else new
                print(f"    slot {idx}: {old_s} → {new_s}")
            if len(moved) > 6:
                print(f"    ... and {len(moved)-6} more")


def get_current_layout(udid: str) -> dict:
    """Return the current app layout as a dict for logging/debugging."""
    plist_path = find_icon_state(udid)
    data = load_plist(plist_path)
    result = {}
    for i, page in enumerate(data.get("iconLists", [])):
        result[f"page_{i}"] = [
            item if is_app_entry(item) else f"<{item.get('elementType','?')}>"
            for item in page
        ]
    result["dock"] = data.get("buttonBar", [])
    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Randomize iOS Simulator home screen layout for SIBB episodes."
    )
    parser.add_argument("udid", help="Simulator UDID")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed (default: random)")
    parser.add_argument("--cross-page", action="store_true",
                        help="Shuffle apps across pages (not just within each page)")
    parser.add_argument("--distribute", action="store_true",
                        help="Randomize the COUNT of apps per page in addition "
                             "to their placement (implies cross-page-style "
                             "redistribution).")
    parser.add_argument("--n-pages", type=int, default=None,
                        help="Target number of pages when --distribute is set "
                             "(default: keep current count).")
    parser.add_argument("--dock", action="store_true",
                        help="Shuffle existing dock apps (preserves dock count).")
    parser.add_argument("--randomize-dock", action="store_true",
                        help="Pick a NEW dock count (1-4) and new contents.")
    parser.add_argument("--dock-count", type=int, default=None,
                        help="Force a specific dock count (1-4). "
                             "Implies --randomize-dock.")
    parser.add_argument("--sibb-only", action="store_true",
                        help="Only shuffle the SIBB-11 apps; keep non-SIBB "
                             "apps (Fitness, news, Passwords, …) in their "
                             "original positions. Default is to shuffle ALL "
                             "non-widget/non-folder apps — more realistic "
                             "because non-SIBB position cues can't be used "
                             "to deduce SIBB positions.")
    parser.add_argument("--all-apps", action="store_true",
                        help="Deprecated alias — all-apps is now the default. "
                             "Pass --sibb-only to restore the old behavior.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    parser.add_argument("--show", action="store_true",
                        help="Show current layout and exit")
    parser.add_argument("--restore", type=str, default=None,
                        help="Restore from a backup plist path")

    args = parser.parse_args()

    if args.show:
        layout = get_current_layout(args.udid)
        print(json.dumps(layout, indent=2))
        sys.exit(0)

    if args.restore:
        restore_layout(args.udid, args.restore)
        sys.exit(0)

    result = randomize_layout(
        udid=args.udid,
        seed=args.seed,
        cross_page=args.cross_page,
        shuffle_dock=args.dock,
        sibb_only=args.sibb_only,
        dry_run=args.dry_run,
        distribute=args.distribute,
        n_pages=args.n_pages,
        randomize_dock=args.randomize_dock,
        dock_count=args.dock_count,
    )

    if not args.dry_run:
        print(f"\n  Done. Seed was: {result['seed']}")
        print(f"  Reboot the simulator to apply:")
        print(f"    xcrun simctl boot {args.udid}")
