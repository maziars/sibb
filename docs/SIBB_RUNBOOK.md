# SIBB Simulator Runbook

Complete guide for setting up a new iOS simulator version for SIBB benchmark
episodes. Run this end-to-end whenever a new Xcode or iOS simulator runtime
is released.

---

## Files referenced in this runbook

| File | Purpose |
|---|---|
| `sibb_compatibility_audit.py` | Discovers suppression keys + audits action availability |
| `sibb_prewarm.sh` | Grants permissions, writes suppression keys, launches each app |
| `sibb_randomize_layout.py` | Shuffles home screen layout for episode noise |
| `sibb_task_generator_v3.py` | Task generator — reads APP_REGISTRY and compatibility JSON |
| `sibb_scaffold.py` | iOS ↔ LLM bridge for episode execution |
| `sibb_verify_reminders.py` | Example verifier (Reminders DB, iOS 26 confirmed) |

---

## SIBB-11: The 11 target apps

These are the apps confirmed available in the iOS 26.3 simulator. Check
availability again on each new runtime using Step 3 below.

| # | App | Bundle ID | Tier | Notes |
|---|---|---|---|---|
| 1 | Reminders | com.apple.reminders | A | Core benchmark app |
| 2 | Calendar | com.apple.mobilecal | A | Core benchmark app |
| 3 | Contacts | com.apple.MobileAddressBook | A | Core benchmark app |
| 4 | Settings | com.apple.Preferences | A | No API — AX only |
| 5 | Files | com.apple.DocumentsApp | A | No API — AX only |
| 6 | Health | com.apple.Health | A | HealthKit partial |
| 7 | Maps | com.apple.Maps | A | MapKit partial |
| 8 | Photos | com.apple.mobileslideshow | A | PhotoKit partial |
| 9 | Shortcuts | com.apple.shortcuts | A | No API — AX only |
| 10 | Safari | com.apple.mobilesafari | B | No API — AX only |
| 11 | Messages | com.apple.MobileSMS | B | Cross-app target |

**Apps unavailable in iOS 26.3 simulator** (monitor for return in future builds):
Notes, Clock, Music, Podcasts, Books, Mail — moved to App Store downloadable
model; simulator has no App Store.

**Apps not in simulator runtime** (device-only):
Phone, Voice Memos, Keynote, Pages, Numbers.

---

## Part 0: Python test toolchain (one-time)

The system Python 3.9 at `/Library/Developer/CommandLineTools/usr/bin/python3`
does NOT ship pytest. Install user-scope before running any tests:

```bash
/Library/Developer/CommandLineTools/usr/bin/python3 -m pip install --user \
    pytest pytest-asyncio pytest-rerunfailures
```

Always invoke as `python3 -m pytest`, never bare `pytest` — bare `pytest`
on this Mac resolves to the Anaconda 3.7 install, which doesn't see SIBB's
Python 3.9-targeted code.

Layered run instructions live in
[`../tests/README.md`](../tests/README.md). Test-layer rationale
(why L1 / L1.5 / L2 / L3 / L4) is in
[`PHASE2_PROGRESS.md`](./PHASE2_PROGRESS.md) → "Testing strategy".

---

## Part 1: New iOS Runtime Setup

### When to run
- New Xcode version released
- New iOS simulator runtime available
- First-time setup on a new Mac

### Step 1.1 — Check available runtimes

```bash
xcrun simctl list runtimes
```

If the runtime you want is missing:

```bash
# Download latest iOS simulator runtime
xcodebuild -downloadPlatform iOS

# Verify it installed
xcrun simctl list runtimes
```

### Step 1.2 — Check available device types

```bash
xcrun simctl list devicetypes | grep "iPhone 1"
```

Use the most recent iPhone available (iPhone 17 for iOS 26).

### Step 1.3 — Check which target apps are present in the runtime

```bash
RUNTIME_PATH=$(xcrun simctl list runtimes -j | \
  python3 -c "import sys,json; \
  rts=json.load(sys.stdin)['runtimes']; \
  print([r for r in rts if r.get('isAvailable')][-1]['runtimeIdentifier']")

# Find the runtime root
ls /Library/Developer/CoreSimulator/Volumes/*/Library/Developer/CoreSimulator/Profiles/Runtimes/*.simruntime/Contents/Resources/RuntimeRoot/Applications/ \
  | grep -iE "Reminders|Calendar|MobileAddress|Preferences|Documents|Health|Maps|Photos|Shortcuts|MobileSafari|MobileSMS|MobileNotes|MobileTimer|Music|Podcasts|Books|MobileMail"
```

Update `APP_REGISTRY` in `sibb_task_generator_v3.py` for any changes:
- Set `"available": True` for newly available apps
- Set `"available": False` for newly missing apps
- Update `"unavailable_reason"` accordingly

### Step 1.4 — Create the fresh simulator

```bash
# Get the exact runtime identifier
xcrun simctl list runtimes

# Create simulator (replace runtime identifier as needed)
FRESH_UDID=$(xcrun simctl create "SIBB-Fresh-$(date +%Y%m%d)" \
  "iPhone 17" \
  "com.apple.CoreSimulator.SimRuntime.iOS-26-3")

echo "Fresh simulator UDID: $FRESH_UDID"

# Boot it
xcrun simctl boot $FRESH_UDID
open -a Simulator
```

Wait for the simulator to fully boot (home screen visible) before continuing.

---

## Part 2: Compatibility Audit

### When to run
- After creating a fresh simulator (as part of baseline setup)
- After any Xcode update that changes iOS version
- After discovering a benchmark verifier is returning wrong results

### Step 2.1 — Run key discovery (automated, ~60 seconds)

This launches each of the 11 apps, watches for plist changes, and generates
the suppression key commands that prevent first-launch dialogs.

```bash
python3 sibb_compatibility_audit.py $FRESH_UDID \
  --discover-keys-only \
  --output-dir ./audit_ios26_3/
```

**Output files:**
- `audit_ios26_3/compatibility_ios26_3.json` — full results
- `audit_ios26_3/prewarm_keys_ios26_3.sh` — suppression commands to add to prewarm script

### Step 2.2 — Update sibb_prewarm.sh with discovered keys

Open `audit_ios26_3/prewarm_keys_ios26_3.sh` and copy the generated
`sim_defaults` commands into `sibb_prewarm.sh` under Step 2
("Write first-launch suppression keys"), replacing the existing block.

### Step 2.3 — Run full action audit (manual, ~30 minutes)

For each of the 11 apps, you manually test each action in the simulator
and rate it. This catches things like:
- `ZFLAGGED` unavailable without iCloud (iOS 26 Reminders)
- Feature moved behind a different UI flow
- Action silently failing (appears to work, DB not updated)

```bash
python3 sibb_compatibility_audit.py $FRESH_UDID \
  --output-dir ./audit_ios26_3/
```

For each action you'll be prompted:
```
Action: flag_item
Task:   flag 'AuditItem'

Result? [p=pass / s=fail_silent / e=fail_error / u=unavailable / c=requires_icloud / q=quit app]
> c
Recorded: requires_icloud
```

### Step 2.4 — Update task generator based on audit results

Open `audit_ios26_3/compatibility_ios26_3.json` and check for broken actions.
For each broken action, update `sibb_task_generator_v3.py`:

```python
# Example: flag is broken in iOS 26 without iCloud
# In gen_reminders_list():
flag = OptionalParam("flag", True,
    include_prob=0.0,   # was 0.5 — set to 0.0 to disable
    step_cost=1).sample(detail_level)
```

Also update the `AUDIT_ACTIONS` dict in `sibb_compatibility_audit.py` if
any actions were discovered to need new test descriptions for this iOS version.

---

## Part 3: Baseline Simulator Setup

### Step 3.1 — Run prewarm script

```bash
chmod +x sibb_prewarm.sh
./sibb_prewarm.sh $FRESH_UDID
```

The script:
1. Grants all privacy permissions to SIBB-11 apps
2. Writes suppression keys (discovered in Step 2.2)
3. Launches each app for 3 seconds to trigger first-run logic

### Step 3.2 — Manual dialog cleanup

Open Simulator.app and manually verify each app opens cleanly:

```
□ Reminders  — no iCloud sync dialog
□ Calendar   — no iCloud dialog
□ Contacts   — opens to contact list
□ Settings   — opens to main settings
□ Files      — opens to browse view
□ Health     — no onboarding carousel
□ Maps       — no location permission dialog (already granted)
□ Photos     — no iCloud photos dialog
□ Shortcuts  — opens to shortcut list
□ Safari     — opens to start page
□ Messages   — no iMessage setup dialog
```

For any app that still shows a dialog: dismiss it manually, then note the
dialog type so a suppression key can be added to `sibb_prewarm.sh`.

### Step 3.3 — Clone the baseline

```bash
xcrun simctl shutdown $FRESH_UDID

BASELINE_UDID=$(xcrun simctl clone $FRESH_UDID "SIBB-Baseline-$(date +%Y%m%d)")
echo "Baseline UDID: $BASELINE_UDID"
```

Write this UDID down — it is the source for all episode clones.

### Step 3.4 — Verify the baseline

Boot the baseline clone and do a quick smoke test:

```bash
xcrun simctl boot $BASELINE_UDID
open -a Simulator

# Test: create a Reminders task and verify programmatically
# (follow the demo task steps from the SIBB demo README)
python3 sibb_verify_reminders.py $BASELINE_UDID
```

Expected result: all checks pass, no dialogs appeared.

---

## Part 4: Episode Reset Loop

### Per-episode workflow

```bash
BASELINE_UDID="<your-baseline-udid>"
EPISODE_NUM=1

# 1. Clone baseline for this episode
EPISODE_UDID=$(xcrun simctl clone $BASELINE_UDID "SIBB-Episode-$EPISODE_NUM")

# 2. Randomize home screen layout (70% probability in task generator)
xcrun simctl shutdown $EPISODE_UDID  # must be shut down for layout change
python3 sibb_randomize_layout.py $EPISODE_UDID --seed $EPISODE_NUM
xcrun simctl boot $EPISODE_UDID

# 3. Inject DB noise records for the task
# (task.initial_state.setup_commands contains the sqlite3 commands)
for cmd in "${TASK_SETUP_COMMANDS[@]}"; do
    eval "$cmd"
done

# 4. Run the episode (agent interacts with simulator)
python3 -c "
from sibb_scaffold import SIBBScaffold
import asyncio

async def run():
    scaffold = SIBBScaffold(udid='$EPISODE_UDID')
    # ... episode loop ...

asyncio.run(run())
"

# 5. Verify and record reward
python3 sibb_verify_reminders.py $EPISODE_UDID  # or appropriate verifier

# 6. Clean up
xcrun simctl shutdown $EPISODE_UDID
xcrun simctl delete $EPISODE_UDID
```

### Parallelism

For N parallel episodes, create N clones simultaneously:

```bash
BASELINE_UDID="<baseline>"
N=4

for i in $(seq 1 $N); do
    xcrun simctl clone $BASELINE_UDID "SIBB-Episode-$i" &
done
wait
```

Clone time: 3–8 seconds on M3 Ultra. N=40 parallel episodes takes ~30 seconds
to clone.

---

## Part 5: Known Issues and Fixes

### iOS 26.3 (Xcode 26.3, build 17C529)

| Issue | Affected App | Fix |
|---|---|---|
| `ZFLAGGED` always 0 | Reminders | Set `include_prob=0.0` for flag OptionalParam |
| `simctl snapshot` removed | All | Use `simctl clone` instead |
| Clock not launchable | Clock | App unavailable in this runtime |
| Notes not on home screen | Notes | App unavailable in this runtime |
| `ZREMCDBASELIST` empty | Reminders | List name inferred from items; not a bug |
| DB path changed | Reminders | Use `AppGroup/<UUID>/Container_v1/Stores/Data-<UUID>.sqlite` |

### Reminders DB path (iOS 26 confirmed)

```
~/Library/Developer/CoreSimulator/Devices/<UDID>/data/
  Containers/Shared/AppGroup/
    <group-uuid>/
      Container_v1/Stores/
        Data-<store-uuid>.sqlite    ← use this (not Data-local.sqlite)
```

Find the group UUID:
```bash
find ~/Library/Developer/CoreSimulator/Devices/<UDID> \
  -path "*group.com.apple.reminders*" 2>/dev/null
```

### Priority encoding (iOS 26 confirmed)

`ZPRIORITY` in `ZREMCDREMINDER`: 1=high, 5=medium, 9=low, 0=none

---

## Part 6: Checklist for New iOS Version

Run through this checklist when a new simulator runtime is released:

```
SETUP
□ Download new runtime: xcodebuild -downloadPlatform iOS
□ Create fresh simulator: xcrun simctl create ...
□ Check which SIBB apps are present in runtime Applications/
□ Update APP_REGISTRY in sibb_task_generator_v3.py

AUDIT
□ Run key discovery: python3 sibb_compatibility_audit.py <UDID> --discover-keys-only
□ Update sibb_prewarm.sh with discovered suppression keys
□ Run full action audit: python3 sibb_compatibility_audit.py <UDID>
□ Update OptionalParam include_prob for broken actions

BASELINE
□ Run sibb_prewarm.sh
□ Manually verify all 11 apps open without dialogs
□ Clone baseline: xcrun simctl clone <FRESH_UDID> "SIBB-Baseline-<date>"
□ Record baseline UDID
□ Smoke test: run one Reminders task and verify

DB PATHS
□ Confirm Reminders DB path (may change between iOS versions)
□ Confirm Calendar DB path
□ Confirm Contacts DB path
□ Update sibb_verify_reminders.py and other verifiers if paths changed

TASK GENERATOR
□ Run: python3 sibb_task_generator_v3.py (check for errors)
□ Generate 20 sample tasks and spot-check instructions
□ Confirm complexity scores are in expected range

DOCUMENTATION
□ Update research_summary.md Part 8 with findings
□ Commit all changes with iOS version tag: git tag ios-26.3-audit
```

---

## Part 7: File Locations Quick Reference

```
sibb_task_generator_v3.py   Task generation and APP_REGISTRY
sibb_scaffold.py            iOS ↔ LLM bridge (AXReader, enricher, executor)
sibb_compatibility_audit.py Audit + key discovery — run per iOS version
sibb_prewarm.sh             Baseline setup — run once per runtime
sibb_randomize_layout.py    Home screen shuffle — run per episode
sibb_discover_keys.py       Standalone key discovery for single app
sibb_verify_reminders.py    Reminders verifier (iOS 26 DB path confirmed)
research_summary.md         Full design document
```
