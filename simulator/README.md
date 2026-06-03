# SIBB Simulator Layer

Controls the iOS 26.3 simulator and reads the accessibility tree.

## Files

| File | Purpose | Run when |
|---|---|---|
| `sibb_xcuitest_setup.sh` | Builds XCUITest server in `~/SIBBHelper/` | Once per Mac, or after changes |
| `sibb_xcuitest_client.py` | Persistent AX server client via Unix socket | Imported by scaffold |
| `sibb_prewarm.sh` | Grants permissions, suppresses first-launch dialogs | Once per baseline simulator |
| `sibb_randomize_layout.py` | Shuffles home screen app positions | Per episode (70% probability) |
| `sibb_compatibility_audit.py` | Audits app availability and action support | Per iOS version |
| `sibb_discover_keys.py` | Discovers first-launch suppression keys per app | Per app per iOS version |
| `sibb_ax_helper.swift` | macOS AX API fallback (not primary path) | Not in use |
| `sibb_ax_reader_native.py` | macOS AX reader using Swift helper (not primary) | Not in use |

## Primary AX Reading Path

```
xcodebuild test-without-building
    → SIBBTests.xctest (Swift, runs inside simulator)
    → Unix socket /tmp/sibb_xcuitest_<UDID>.sock
    → sibb_xcuitest_client.py (Python)
    → sibb_scaffold.py AXReader
```

## XCUITest Server Protocol

Commands sent as JSON over Unix socket, one per line:

```json
{"type": "ping"}
{"type": "attach", "bundleId": "com.apple.reminders"}
{"type": "launch", "bundleId": "com.apple.reminders"}
{"type": "observe"}
{"type": "tap", "x": 335, "y": 831}
{"type": "tap", "ref": "BackButton"}
{"type": "type", "text": "Hello"}
{"type": "swipe", "direction": "up"}
{"type": "quit"}
```

Responses:
```json
{"ok": true, "elements": [...], "keyboard_visible": false, "screen_width": 402, "screen_height": 874}
{"ok": false, "error": "no_app"}
```

## Element Format

Each element in the `elements` array:

```json
{
  "ref": "BackButton",
  "role": "btn",
  "label": "Back",
  "value": "",
  "enabled": true,
  "hittable": true,
  "focused": false,
  "frame": {"x": 16, "y": 62, "width": 44, "height": 44}
}
```

## Filtering Rules (in Swift, inside dumpTree)

1. `KEYBOARD_TYPES` (`.key`, `.keyboard`) — always excluded
2. Zero-size elements — excluded
3. Elements below keyboard top — excluded (when keyboard visible)
4. Interactive elements with `hittable: false` — excluded
5. Non-app windows (callout bar) — excluded via largest-window selection

## Scaling

N concurrent simulators = N independent instances, no collision:
- Socket: `/tmp/sibb_xcuitest_<UDID>.sock`
- Derived data: `/tmp/sibb_dd_<UDID>/`
- xctestrun: patched copy next to original with `SIBB_UDID` in `TestingEnvironmentVariables`

## Focus & Speed

`dumpTree` uses `window.snapshot()` as the primary path (~200ms) — this is the
only place `hasFocus` reads correctly on iOS 26. The live `XCUIElement.hasFocus`
property and NSPredicate-based queries return false even on focused text fields.

If `snapshot()` returns fewer than 5 elements (suspicious), the server falls
back to `allElementsBoundByIndex` (~17-50s) and uses the snapshot only to
locate the focused frame, then matches that frame on the slow traversal.

The observe response includes `"method": "snapshot" | "fallback"` so callers
can tell which path was used.
