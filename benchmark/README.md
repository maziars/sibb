# SIBB Benchmark Layer

Task generation, episode execution, AX enrichment, and verification.

## Files

| File | Purpose |
|---|---|
| `sibb_task_generator_v3.py` | Generates tasks with noise, constraints, CLARIFY/FAIL tools |
| `sibb_scaffold.py` | iOS ↔ LLM bridge: AXReader → AXEnricher → AXTokenizer |
| `sibb_inspect_screen.py` | Interactive inspector — see what LLM sees on any screen |
| `sibb_verify_reminders.py` | Reminders DB verifier (iOS 26 path confirmed) |
| `new_flows.py` | Additional flow generators (Search-then-act, Fetch, Update) |

## Scaffold Architecture

```
AXReader                     AXEnricher              AXTokenizer
────────                     ──────────              ───────────
Wraps XCUITestReader    →    SF symbol lookup   →    Compact text
Converts string roles        VLM enrichment          for LLM prompt
to ElementRole enums         for unlabeled els       ~40-130 tokens
```

### AXReader.start() / stop()

Must call `start(bundle_id)` before `read()`, `stop()` at episode end:

```python
reader = AXReader(udid)
await reader.start(bundle_id="com.apple.reminders")  # starts XCUITest server
tree   = await reader.read()                          # observe
await reader.stop()                                   # end of episode
```

### AXElement fields

```python
el.ref            # "@e0034" — unique per observation cycle
el.role           # ElementRole enum (ElementRole.BUTTON, etc.)
el.effective_role # same as role after enrichment
el.label          # "Title" — accessibility label
el.effective_label # label after enrichment (may differ if enriched)
el.value          # "Hello" — current field value
el.enabled        # True/False
el.focused           # True if keyboard focus
el.frame          # AXFrame(x, y, width, height)
el.frame.center_x # tap coordinate x
el.frame.center_y # tap coordinate y
el.enrichment_src # "ax_native" | "sf_symbol" | "vlm"
```

### AXTree fields

```python
tree.elements         # List[AXElement] — flat, viewport-filtered
tree.keyboard_visible # bool — is iOS keyboard on screen
tree.screen_width     # float — 402 for iPhone 17
tree.screen_height    # float — 874 for iPhone 17
tree.unlabeled()      # elements without label (candidates for VLM)
tree.find("Title")    # first element whose label contains "Title"
```

## Task Generator

### APP_REGISTRY

Single source of truth for app availability:
```python
APP_REGISTRY["Reminders"]["available"]  # True
APP_REGISTRY["Notes"]["available"]      # False
APP_REGISTRY["Notes"]["unavailable_reason"]  # "iOS 26 simulator missing"

check_app_available("Reminders")  # True
get_available_apps(tier="A")      # ["Reminders", "Calendar", ...]
```

### Re-enabling an app

When a future iOS simulator restores Notes/Clock/Music/Mail:
1. Set `"available": True` in `APP_REGISTRY`
2. Move generators from `GENERATORS_PENDING` into `ALL_GENERATORS`
3. Run `sibb_compatibility_audit.py` for that app

### Task structure

```python
task.instruction      # natural language task for agent
task.verify           # list of VerifyStep objects
task.initial_state    # InitialState with noise_records
task.apps             # list of app names involved
task.complexity       # float score
task.task_type        # "single" | "multi" | "constraint" | "impossible" | "ambiguous"
```

### Noise types

- `NoiseRecord(record_type="layout")` — home screen shuffle (70% probability)
- `NoiseRecord(record_type="list")` — pre-existing list in Reminders
- `NoiseRecord(record_type="event")` — decoy calendar event

## LLM Observation Format

What gets sent to the model:
```
@e0001 [btn] "Back" @(38,84)
@e0002 [btn] "More" @(308,84)
@e0003 [btn] "Done" @(364,84)
@e0004 [text] "Reminders" @(98,140)
@e0008 [input] "Title" @(198,190)
@e0009 [input] "Notes" @(220,214)
@e0010 [btn] "New Reminder" @(350,822)
```

Format: `@ref [role] "label" = value @(cx,cy) ✦ (focused) (disabled)`

## Running the Inspector

```bash
# Interactive mode (ENTER to capture, q to quit)
/Library/Developer/CommandLineTools/usr/bin/python3 sibb_inspect_screen.py \
  $SIBB_UDID --bundle com.apple.reminders

# All formats
python3 sibb_inspect_screen.py <UDID> --format all

# Watch mode (auto-refresh on screen change)
python3 sibb_inspect_screen.py <UDID> --watch
```

## Verifying a Task

Reminders DB path (iOS 26 confirmed):
```
~/Library/Developer/CoreSimulator/Devices/<UDID>/data/
  Containers/Shared/AppGroup/<group-uuid>/
    Container_v1/Stores/Data-<store-uuid>.sqlite
```

Table: `ZREMCDREMINDER`
Columns: `ZTITLE`, `ZPRIORITY` (1=high, 5=med, 9=low), `ZFLAGGED`, `ZCOMPLETED`

Note: `ZFLAGGED` always 0 without iCloud — exclude flag from tasks.
