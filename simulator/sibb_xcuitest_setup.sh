#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  SIBB XCUITest Server — One-time setup
#  Creates an Xcode project with a persistent AX server that reads
#  the full iOS accessibility tree from inside the simulator.
#
#  Run once:
#    chmod +x sibb_xcuitest_setup.sh
#    ./sibb_xcuitest_setup.sh
#
#  After setup, use sibb_xcuitest_client.py in place of AXReader.
# ═══════════════════════════════════════════════════════════════════
set -e

PROJ_DIR="$HOME/SIBBHelper"
echo "Creating XCUITest project at $PROJ_DIR..."
mkdir -p "$PROJ_DIR/SIBBApp"
mkdir -p "$PROJ_DIR/SIBBTests"

# ── Minimal host app ─────────────────────────────────────────────
cat > "$PROJ_DIR/SIBBApp/AppDelegate.swift" << 'SWIFT'
import UIKit
@main
class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    func application(_ application: UIApplication,
        didFinishLaunchingWithOptions opts: [UIApplication.LaunchOptionsKey:Any]?) -> Bool {
        window = UIWindow(frame: UIScreen.main.bounds)
        window?.rootViewController = UIViewController()
        window?.makeKeyAndVisible()
        return true
    }
}
SWIFT

cat > "$PROJ_DIR/SIBBApp/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>com.sibb.helper</string>
    <key>CFBundleName</key><string>SIBBHelper</string>
    <key>CFBundleExecutable</key><string>$(EXECUTABLE_NAME)</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>UILaunchScreen</key><dict/>
    <key>UIApplicationSceneManifest</key><dict/>
</dict>
</plist>
PLIST

# ── XCUITest server ───────────────────────────────────────────────
cat > "$PROJ_DIR/SIBBTests/SIBBServer.swift" << 'SWIFT'
import XCTest
import Foundation
import Network
import EventKit
import Contacts
import Photos
import HealthKit
import MapKit

// ── Element type mapping ─────────────────────────────────────────
let ROLE_NAMES: [XCUIElement.ElementType: String] = [
    .button: "btn", .textField: "input", .secureTextField: "input",
    .textView: "textarea", .staticText: "text", .image: "img",
    .cell: "cell", .table: "table", .collectionView: "collection",
    .scrollView: "scroll", .switch: "switch", .slider: "slider",
    .picker: "picker", .pickerWheel: "pickerWheel",
    .segmentedControl: "segmented", .tab: "tab", .tabBar: "tabbar",
    .navigationBar: "nav", .toolbar: "toolbar",
    .alert: "ALERT", .sheet: "SHEET", .link: "link",
    .other: "other", .window: "window", .application: "app",
    .searchField: "search", .webView: "web",
    .activityIndicator: "spinner", .pageIndicator: "pageIndicator",
    .icon: "icon", .toggle: "switch",
]

func elementTypeToRole(_ type: XCUIElement.ElementType) -> String {
    return ROLE_NAMES[type] ?? "other"
}

func serializeElement(_ el: XCUIElement) -> [String: Any] {
    let frame = el.frame
    return [
        "ref":      el.identifier,
        "role":     elementTypeToRole(el.elementType),
        "label":    el.label,
        "value":    el.value as? String ?? "",
        "enabled":  el.isEnabled,
        "exists":   el.exists,
        "hittable": el.isHittable,
        "focused":  el.hasFocus,
        "frame": [
            "x": Double(frame.origin.x), "y": Double(frame.origin.y),
            "width": Double(frame.width), "height": Double(frame.height),
        ]
    ]
}

// Recursively find the first descendant matching the given element type
// in a snapshot tree. Used to locate the main window and the keyboard
// from a single app.snapshot() call — avoids hitting XCUIElementQuery
// (app.windows / app.keyboards) which can fail XCTest's snapshot-retry
// machinery when a window index doesn't exist (tears down the test).
func findFirst(_ root: XCUIElementSnapshot,
               ofType type: XCUIElement.ElementType) -> XCUIElementSnapshot? {
    if root.elementType == type { return root }
    for child in root.children {
        if let found = findFirst(child, ofType: type) { return found }
    }
    return nil
}

// Element types that belong to the system keyboard — always excluded
let KEYBOARD_TYPES: Set<XCUIElement.ElementType> = [.key, .keyboard]

// Interactive types — filtered to hittable only when we have hittability info
let INTERACTIVE_TYPES: Set<XCUIElement.ElementType> = [
    .button, .textField, .textView, .secureTextField,
    .switch, .slider, .segmentedControl, .picker,
    .pickerWheel, .link, .toggle
]

// Text-input element types — the only ones where focus matters for the agent.
let FOCUSABLE_TYPES: Set<XCUIElement.ElementType> = [
    .textField, .textView, .secureTextField, .searchField
]

// Private KVC keys that may carry keyboard focus on XCElementSnapshot
// (the internal class behind XCUIElementSnapshot). The public hasFocus
// property and "hasFocus == YES" NSPredicate are broken on iOS 26.
//
// We only call value(forKey:) when the getter selector exists on the
// object — naked value(forKey:) on a missing key raises NSUndefinedKey-
// Exception, which is an Objective-C exception Swift cannot catch, and
// would kill the XCTest process mid-session.
let FOCUS_KVC_KEYS = ["hasKeyboardFocus", "isFocused"]

func snapshotFocused(_ snap: XCUIElementSnapshot) -> Bool {
    if snap.hasFocus { return true }
    let obj = snap as AnyObject
    for key in FOCUS_KVC_KEYS {
        let sel = NSSelectorFromString(key)
        guard obj.responds(to: sel) else { continue }
        if let n = obj.value(forKey: key) as? NSNumber, n.boolValue { return true }
        if let b = obj.value(forKey: key) as? Bool, b { return true }
    }
    return false
}

// UIAccessibilityTraits.adjustable bit. Set on:
//   - UIDatePicker (compact AND expanded styles)
//   - UIPickerView (and its child UIPickerView columns)
//   - UIStepper
//   - Any custom view that overrides accessibilityTraits with .adjustable
//
// The interaction contract for adjustable elements is TAP-then-SCROLL
// (or accessibilityIncrement/Decrement on the AX layer) — NOT type.
// Without this signal, our scaffold misclassifies compact date pickers
// as text inputs (their AX role is .textField, value is the displayed
// date), and agents waste turns typing into them.
//
// XCUIElementSnapshot's underlying class (XCElementSnapshot) exposes
// the raw UIAccessibilityTraits bitmask via KVC under "traits". Per
// Apple's UIAccessibility header, the .adjustable bit is 0x200000.
let TRAIT_ADJUSTABLE: UInt64 = 0x00200000
let TRAITS_KVC_KEYS = ["traits", "accessibilityTraits"]

func snapshotAdjustable(_ snap: XCUIElementSnapshot) -> Bool {
    // Element types XCUITest already classifies as adjustable —
    // we set the bit even if KVC probe fails on these.
    let knownAdjustable: Set<XCUIElement.ElementType> = [
        .datePicker, .picker, .pickerWheel, .slider, .stepper
    ]
    if knownAdjustable.contains(snap.elementType) { return true }

    // KVC fallback for adjustable elements wrapped in other element
    // types (notably the iOS compact date picker, which surfaces as
    // role=.textField but has the .adjustable trait set so tapping it
    // opens a wheel sheet).
    //
    // EXCLUSION: iOS sets the .adjustable bit on the keyboard-focused
    // element's UIAccessibilityTraits bitmask — apparently because the
    // VoiceOver rotor uses the adjustable gesture to position the text
    // cursor. We must filter that out, otherwise plain text inputs
    // flip to `adjustable=true` whenever they have focus (confirmed in
    // 2026-05-27 trial logs: First/Last name fields in Contacts'
    // new-contact sheet swapped between [input] and [adj] purely on
    // focus). Filter: if the element is a plain text input AND has
    // keyboard focus, skip the KVC probe — anything that's truly
    // adjustable should be in the whitelist above.
    let isTextInput = (snap.elementType == .textField
                        || snap.elementType == .secureTextField
                        || snap.elementType == .textView)
    if isTextInput && snapshotFocused(snap) {
        return false
    }

    let obj = snap as AnyObject
    for key in TRAITS_KVC_KEYS {
        let sel = NSSelectorFromString(key)
        guard obj.responds(to: sel) else { continue }
        if let n = obj.value(forKey: key) as? NSNumber {
            return (n.uint64Value & TRAIT_ADJUSTABLE) != 0
        }
    }
    return false
}

// Find the focused text field in a snapshot tree. Two-pass:
//   1) explicit: snap.hasFocus or a guarded private-KVC probe
//   2) conservative heuristic: when keyboard is visible AND exactly one
//      focusable element sits above the keyboard, mark that one focused.
//      Multiple candidates → leave unmarked (better than guessing wrong).
func findFocusedFrame(_ root: XCUIElementSnapshot, keyboardTop: CGFloat,
                      screenBounds: CGRect) -> CGRect? {
    func explicitSearch(_ snap: XCUIElementSnapshot) -> CGRect? {
        if FOCUSABLE_TYPES.contains(snap.elementType) && snapshotFocused(snap) {
            return snap.frame
        }
        for child in snap.children {
            if let f = explicitSearch(child) { return f }
        }
        return nil
    }
    if let f = explicitSearch(root) { return f }

    guard keyboardTop < screenBounds.maxY - 1 else { return nil }
    var candidates: [CGRect] = []
    func gather(_ snap: XCUIElementSnapshot) {
        if FOCUSABLE_TYPES.contains(snap.elementType) {
            let f = snap.frame
            if f.width > 0 && f.height > 0 &&
               f.maxY <= keyboardTop && screenBounds.intersects(f) {
                candidates.append(f)
            }
        }
        for child in snap.children { gather(child) }
    }
    gather(root)
    return candidates.count == 1 ? candidates[0] : nil
}

// Returns the fraction (0.0–1.0) of `frame`'s area that lies inside the
// visible viewport — the region above the keyboard within screenBounds.
// Used to drop elements that are off-screen above/below the viewport,
// which iOS still exposes in the snapshot tree (scroll views, sheets
// with overflow content).
//
// The `min(screenBounds.maxY, keyboardTop)` is defensive — keyboardTop
// defaults to `screenBounds.maxY` when no keyboard is visible, but a
// future kb-detection regression returning an off-screen sentinel
// would otherwise blow up the viewport math.
func visibleAreaFraction(_ frame: CGRect, screenBounds: CGRect,
                          keyboardTop: CGFloat) -> CGFloat {
    let viewportBottom = min(screenBounds.maxY, keyboardTop)
    let viewport = CGRect(
        x: screenBounds.minX, y: screenBounds.minY,
        width: screenBounds.width,
        height: max(0, viewportBottom - screenBounds.minY))
    let totalArea = frame.width * frame.height
    if totalArea <= 0 { return 0 }
    let inter = frame.intersection(viewport)
    let interArea = max(0, inter.width) * max(0, inter.height)
    return interArea / totalArea
}

// Minimum fraction of an element's area that must be inside the
// visible viewport for it to ship to the agent. Empirically chosen
// at 0.2 — high enough to drop mostly-off-screen scroll content,
// low enough to keep partial-visible "next-item-peeking" cells the
// agent can legitimately tap (e.g. a 60pt cell with 15pt visible at
// the viewport edge = 0.25, just above threshold).
//
// Override via `SIBB_VIS_FRAC` env var for empirical A/B without
// rebuilding the runner.
let VISIBILITY_THRESHOLD: CGFloat = {
    if let s = ProcessInfo.processInfo.environment["SIBB_VIS_FRAC"],
       let v = Double(s) { return CGFloat(v) }
    return 0.2
}()

func walkSnapshot(_ snap: XCUIElementSnapshot,
                  parentCellFrame: CGRect?,
                  focusedFrame: CGRect?,
                  screenBounds: CGRect,
                  keyboardTop: CGFloat,
                  results: inout [[String: Any]]) {
    // Track the containing UITableViewCell frame as we descend. iOS
    // table-view virtualization can return STALE local-coord frames
    // (e.g. (0, 44)) for descendants of a cell whose layout shifted
    // after a sibling cell expanded. Detection: if the descendant's
    // frame is NON-EMPTY but lies entirely OUTSIDE its parent cell's
    // frame, the descendant has a stale frame — drop it.
    //
    // This catches the Contacts new-contact "add X" cluster from the
    // 2026-05-27 probe: 30+ children at (0, 44) inside cells whose
    // real frames are at (20, 500+) or off-screen.
    let isCell = (snap.elementType == .cell)
    let nextParentCellFrame: CGRect? = isCell ? snap.frame : parentCellFrame

    if !KEYBOARD_TYPES.contains(snap.elementType) {
        let f = snap.frame

        // Stale-frame detection: descendant of a cell whose frame
        // doesn't intersect the cell's. iOS virtualization bug.
        var staleFrame = false
        if !isCell, let cellFrame = parentCellFrame,
                f.width > 0 && f.height > 0,
                cellFrame.width > 0 && cellFrame.height > 0,
                !cellFrame.intersects(f) {
            staleFrame = true
        }

        if !staleFrame {
            // Require a minimum fraction of the element to be in the
            // visible viewport. Pure intersects-check (1px overlap counts)
            // was shipping off-screen scroll content to the agent. The
            // threshold (default 0.2) keeps "next-item-peeking" cells the
            // agent might legitimately want to tap while dropping mostly-
            // off-screen items. See `VISIBILITY_THRESHOLD` above for the
            // env-var override knob.
            let fracVisible = visibleAreaFraction(
                f, screenBounds: screenBounds, keyboardTop: keyboardTop)
            let visible = f.width > 0 && f.height > 0
                           && fracVisible >= VISIBILITY_THRESHOLD
            if visible {
                let isFocused = (focusedFrame != nil && f == focusedFrame!)
                let dict: [String: Any] = [
                    "ref":      snap.identifier,
                    "role":     elementTypeToRole(snap.elementType),
                    "label":    snap.label,
                    "value":    snap.value as? String ?? "",
                    "enabled":  snap.isEnabled,
                    "exists":   true,
                    "hittable": true,
                    "focused":  isFocused,
                    "adjustable": snapshotAdjustable(snap),
                    "frame": [
                        "x": Double(f.origin.x), "y": Double(f.origin.y),
                        "width": Double(f.width), "height": Double(f.height),
                    ]
                ]
                results.append(dict)
            }
        }
    }
    for child in snap.children {
        walkSnapshot(child,
                     parentCellFrame: nextParentCellFrame,
                     focusedFrame: focusedFrame,
                     screenBounds: screenBounds,
                     keyboardTop: keyboardTop, results: &results)
    }
}

func dumpTree(_ app: XCUIApplication) -> [String: Any] {
    let screenBounds = app.frame

    // Take ONE snapshot of the whole app and derive everything from it:
    // main window (largest top-level), keyboard (if any), focused field.
    //
    // Critical: we deliberately AVOID `app.windows.allElementsBoundByIndex`
    // and `app.keyboards.count`. Those are XCUIElementQuery resolutions
    // that XCTest retries up to 3× over ~2 s on failure and then tears
    // the test down with "Failed to get matching snapshot: No matches
    // found for Element at index N" — killing our socket server. Walking
    // the snapshot tree never has that failure mode.
    var appSnap: XCUIElementSnapshot? = nil
    var snapErr: NSString?
    SIBBSafeRun(&snapErr) {
        appSnap = try? app.snapshot()
    }
    if let e = snapErr {
        print("dumpTree: app.snapshot threw: \(e)")
    }

    guard let topSnap = appSnap else {
        return ["elements": [], "keyboard_visible": false,
                "screen_width":  Double(screenBounds.width),
                "screen_height": Double(screenBounds.height),
                "method": "snapshot_failed"]
    }

    // Keyboard detection. The iOS software keyboard's container
    // window has `elementType == .keyboard`, but on iOS Simulator
    // it lives in a process-level window stack that `app.snapshot()`
    // doesn't always include in the snapshot tree. `findFirst(topSnap,
    // ofType: .keyboard)` was returning nil even when the keyboard
    // was clearly visible (probe 2026-05-27).
    //
    // Switching to the individual `.key` elements as the source of
    // truth: a visible keyboard always renders its keys (the things
    // the user taps to type), and those DO appear in the app's
    // snapshot tree. The keyboard's bounding box is the union of all
    // key frames; the keyboard top is the minimum y across keys.
    //
    // Belt-and-suspenders: if we find a `.keyboard` element (older
    // iOS versions, or future fix), use its frame directly — the
    // .key-aggregation path is the fallback.
    var keyFrames: [CGRect] = []
    func gatherKeys(_ snap: XCUIElementSnapshot) {
        if snap.elementType == .key {
            let f = snap.frame
            if f.width > 0 && f.height > 0 {
                keyFrames.append(f)
            }
        }
        for child in snap.children { gatherKeys(child) }
    }
    gatherKeys(topSnap)

    let kbSnap = findFirst(topSnap, ofType: .keyboard)
    var keyboardVisible = false
    var keyboardTop = screenBounds.maxY
    var keyboardFrameDict: [String: Double]? = nil

    if let kbFrame = kbSnap?.frame, kbFrame.height > 0 {
        keyboardVisible = true
        keyboardTop = kbFrame.origin.y
        keyboardFrameDict = [
            "x":      Double(kbFrame.origin.x),
            "y":      Double(kbFrame.origin.y),
            "width":  Double(kbFrame.width),
            "height": Double(kbFrame.height),
        ]
    } else if !keyFrames.isEmpty {
        // Fallback: bounding box of .key elements.
        let minX = keyFrames.map { $0.minX }.min()!
        let minY = keyFrames.map { $0.minY }.min()!
        let maxX = keyFrames.map { $0.maxX }.max()!
        let maxY = keyFrames.map { $0.maxY }.max()!
        keyboardVisible = true
        keyboardTop = minY
        keyboardFrameDict = [
            "x":      Double(minX),
            "y":      Double(minY),
            "width":  Double(maxX - minX),
            "height": Double(maxY - minY),
        ]
    }

    // Include EVERY substantial top-level window (>= 20% of screen
    // area) — not just the largest. In-app alerts, action sheets,
    // share sheets, modals, and date pickers are presented as
    // additional UIWindows in the same process; "largest only" would
    // hide them. Tiny callout bars (Paste/AutoFill — UIEditMenuInteraction
    // windows, usually < 200×80) fall below the threshold and are
    // correctly excluded.
    //
    // System alerts owned by SpringBoard live in a different process;
    // those are captured because PID-based foreground detection switches
    // the whole observe to com.apple.springboard.
    let screenArea = screenBounds.width * screenBounds.height
    let minArea = screenArea * 0.20
    var significantWindows: [XCUIElementSnapshot] = []
    for child in topSnap.children where child.elementType == .window {
        let f = child.frame
        if f.width * f.height >= minArea {
            significantWindows.append(child)
        }
    }

    var elements: [[String: Any]] = []
    SIBBSafeRun(nil) {
        for window in significantWindows {
            let focusedFrame = findFocusedFrame(window,
                                                keyboardTop: keyboardTop,
                                                screenBounds: screenBounds)
            walkSnapshot(window,
                         parentCellFrame: nil,
                         focusedFrame: focusedFrame,
                         screenBounds: screenBounds,
                         keyboardTop: keyboardTop, results: &elements)
        }
    }

    var responseDict: [String: Any] = [
        "elements":         elements,
        "keyboard_visible": keyboardVisible,
        "screen_width":     Double(screenBounds.width),
        "screen_height":    Double(screenBounds.height),
        "method":           significantWindows.isEmpty ? "no_windows" : "snapshot",
        "window_count":     significantWindows.count,
    ]
    if let kf = keyboardFrameDict {
        responseDict["keyboard_frame"] = kf
    }
    return responseDict
}

// ── Private API: frontmost app via accessibility interface ───────
//
// XCUITest's public XCUIApplication.state is a cached internal flag —
// it only updates when XCUITest itself activates an app, so when the
// user navigates manually via the simulator GUI we can't see what's
// actually frontmost. The accessibility-server side exposes a real-
// time query that returns the list of running apps with the frontmost
// first; this is what Appium's WebDriverAgent uses for `activeAppInfo`.
//
// Modern path (Xcode 10.2+): XCUIDevice.shared.accessibilityInterface
//   returns an XCAXClient_iOS-like object whose -activeApplications
//   returns [XCAccessibilityElement]; firstObject.processIdentifier
//   is the frontmost app's PID. (+[XCAXClient_iOS sharedClient] was
//   removed in Xcode 10.2.) We map PID → bundle ID in Python via
//   `simctl spawn launchctl list`.
@objc protocol _AccessibilityElement {
    var processIdentifier: pid_t { get }
}

// Apply a short AX query timeout so individual accessibility calls fail
// fast instead of hanging the entire test process indefinitely. WDA does
// the same. Wrapped in SIBBSafeRun to survive missing-selector cases.
func configureAXTimeout(_ seconds: Double) {
    var err: NSString?
    SIBBSafeRun(&err) {
        let device: NSObject = XCUIDevice.shared
        let ifaceSel = NSSelectorFromString("accessibilityInterface")
        guard device.responds(to: ifaceSel),
              let u = device.perform(ifaceSel),
              let iface = u.takeUnretainedValue() as? NSObject
        else { return }
        let setSel = NSSelectorFromString("setAXTimeout:")
        if iface.responds(to: setSel) {
            iface.setValue(seconds, forKey: "AXTimeout")
            print("SIBB_AXTIMEOUT set=\(seconds)")
        }
    }
    if let e = err { print("AXTimeout: \(e)") }
}

func getFrontmostPID() -> pid_t {
    var pid: pid_t = 0
    var err: NSString?
    SIBBSafeRun(&err) {
        let device: NSObject = XCUIDevice.shared
        let interfaceSel = NSSelectorFromString("accessibilityInterface")
        guard device.responds(to: interfaceSel) else { pid = -1; return }
        guard let interfaceUnmgd = device.perform(interfaceSel),
              let interface = interfaceUnmgd.takeUnretainedValue() as? NSObject
        else { pid = -2; return }
        let activeSel = NSSelectorFromString("activeApplications")
        guard interface.responds(to: activeSel) else { pid = -4; return }
        guard let activeUnmgd = interface.perform(activeSel),
              let apps = activeUnmgd.takeUnretainedValue() as? [AnyObject],
              !apps.isEmpty
        else { pid = -5; return }
        let first = apps[0] as AnyObject
        let pidSel = NSSelectorFromString("processIdentifier")
        guard first.responds(to: pidSel) else { pid = -6; return }
        let element = unsafeBitCast(apps[0], to: _AccessibilityElement.self)
        pid = element.processIdentifier
    }
    if let e = err {
        print("FM: caught NSException: \(e)")
        return 0
    }
    return pid
}

// ── Foreground-app auto-detection ────────────────────────────────
//
// XCUITest queries an app's AX tree by bundle ID. To capture "whatever
// the user is interacting with" — home screen, Spotlight, any installed
// app — we maintain a list of bundle IDs and pick whichever reports
// state == .runningForeground on each observe. The candidate list is
// passed in via SIBB_FOREGROUND_CANDIDATES (comma-separated), built
// from `simctl listapps` on the Python side and always including
// com.apple.springboard (which is the home screen, App Switcher,
// Control Center, Spotlight Search, and the lock screen).
func parseForegroundCandidates() -> [String] {
    let raw = ProcessInfo.processInfo.environment["SIBB_FOREGROUND_CANDIDATES"] ?? ""
    let parts = raw.split(separator: ",").map { String($0) }
    if parts.isEmpty {
        return ["com.apple.springboard"]
    }
    return parts
}

// Cache: don't reallocate XCUIApplication objects on every observe.
// Each `XCUIApplication(bundleIdentifier:)` creates a new query handle
// that talks to the AX server; reusing them keeps memory pressure down
// during long sessions and matches XCTest's intended usage pattern.
var APP_CACHE: [String: XCUIApplication] = [:]

func appFor(_ bid: String) -> XCUIApplication {
    if let a = APP_CACHE[bid] { return a }
    let a = XCUIApplication(bundleIdentifier: bid)
    APP_CACHE[bid] = a
    return a
}

func detectForegroundApp(candidates: [String],
                         current: XCUIApplication?,
                         currentBID: String?) -> (XCUIApplication, String)? {
    // Fast path: if the cached current app is still foreground, reuse it.
    if let app = current, let bid = currentBID,
       app.state == .runningForeground {
        return (app, bid)
    }
    // Scan all candidates. The list is ordered so installed apps come
    // before Springboard — Springboard reports .runningForeground even
    // when another app is on top of it, so we'd never see that other
    // app if Springboard came first.
    for bid in candidates {
        let app = appFor(bid)
        if app.state == .runningForeground {
            return (app, bid)
        }
    }
    return nil
}

func waitForSettle(_ app: XCUIApplication, timeout: TimeInterval = 2.0) {
    var prev = app.descendants(matching: .any).count
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        Thread.sleep(forTimeInterval: 0.15)
        let current = app.descendants(matching: .any).count
        if current == prev { break }
        prev = current
    }
}

// Stronger settle for the launch path: `wait(for: .runningForeground)`
// returns when the app is foregrounded, but iOS Contacts and similar
// apps have a multi-stage launch (window → hierarchy load → initial AX
// populated). The plain waitForSettle has a 2-second cap which is too
// short for that — we ship an observation captured mid-launch, with
// few/wrong elements.
//
// Two-tier exit:
//   - If count ≥ minElements: exit after 2 stable polls (~0.4s of
//     stability) — the loaded-app case.
//   - If count < minElements:  exit after 4 stable polls (~0.8s of
//     stability) — sparse-launch cases like a TCC permission dialog
//     hosted by SpringBoard (the LAUNCHED app's descendants count
//     stays at ~0-3) or a single-CTA Welcome sheet. Without this
//     branch, these flows would eat the full timeout (6s) on every
//     launch.
//
// `minElements = 5` is conservative — the lowest-count post-launch
// state I've measured in the SIBB-11 apps is empty Reminders at ~8-12
// elements, so 5 leaves slack.
//
// Note on polling rate: `descendants(matching: .any).count` is itself
// ~150ms on iOS 26 sim, so the sleep(0.2) + count call → effective
// ~0.35s per poll (~3 Hz), not the 5 Hz the sleep suggests.
func waitForLaunchSettle(_ app: XCUIApplication,
                          minElements: Int = 5,
                          timeout: TimeInterval = 6.0) {
    var prev = app.descendants(matching: .any).count
    let deadline = Date().addingTimeInterval(timeout)
    var stableCount = 0
    while Date() < deadline {
        Thread.sleep(forTimeInterval: 0.2)
        let current = app.descendants(matching: .any).count
        if current == prev {
            stableCount += 1
            let needed = current >= minElements ? 2 : 4
            if stableCount >= needed { break }
        } else {
            stableCount = 0
        }
        prev = current
    }
}

func sendResponse(_ connection: FileHandle, _ dict: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: dict),
          var str = String(data: data, encoding: .utf8) else { return }
    str += "\n"
    connection.write(str.data(using: .utf8)!)
}

// ── EventKit-backed state setup ───────────────────────────────────
//
// Direct SQLite writes to Reminders' CoreData store are unreliable:
// Reminders also maintains an account-level NSKeyedArchiver manifest
// (ZREMCDACCOUNTLISTDATA.ZORDEREDIDENTIFIERMAP), a CRDT replica state
// blob (ZRESOLUTIONTOKENMAP_V3_JSONDATA), a CK mirror flag (ZCKDIRTYFLAGS)
// and other metadata in parallel with the row. Inserting a row without
// updating those is treated as garbage and ignored by the app.
//
// EventKit (EKEventStore) is Apple's official API for Reminders and
// Calendar — it maintains all that state atomically. Since the XCUITest
// runner runs inside the simulator, it can use EventKit directly.

let EVENTKIT_PRIORITY: [String: Int] = [
    "high": 1, "medium": 5, "low": 9, "none": 0,
]

// Cached EKEventStore — created on first use, reused thereafter.
//
// CRITICAL: do NOT create a fresh EKEventStore + requestAccess() per
// command. iOS rate-limits requestAccess and starts returning
// granted=false after ~10 calls in quick succession (caught by L2
// sim test 2026-05-15: cycle 3 of wipe→create_list→create_reminder
// failed with "no reminders permission" even though the first 2
// cycles succeeded). Caching the store across calls keeps the
// runner stable for hundreds of EventKit operations per session.
//
// iOS 17 split EKEventStore.requestAccess into requestFullAccessToReminders
// (needs NSRemindersFullAccessUsageDescription, which Xcode's auto-injection
// doesn't include) and requestWriteOnlyAccessToEvents (no Reminders
// equivalent). The deprecated requestAccess(to: .reminder) still works
// on iOS 17+ and only needs NSRemindersUsageDescription which IS auto-
// injected by Xcode's test runner.
class ReminderStoreCache {
    static var instance: EKEventStore?
}

// Belt-and-suspenders retry on top of the host-side SpringBoard
// restart (D1.5 B3 fix). The restart closes ~75% of the TCC-race
// failures empirically observed under parallel load; this retry
// closes the residual ~25% where SpringBoard's per-bundle cache
// hasn't fully refreshed by the time the runner's first
// requestAccess fires. Cost: <100ms on the success path (single
// attempt), bounded ~5s in the rare retry case.
//
// Between attempts we dismiss any transparency dialog that may
// have just appeared (iOS 17+ posts these on first requestAccess
// even when TCC.db says granted=true). After dismissal, the next
// requestAccess sees a consistent permission state.
func acquireReminderStore() -> EKEventStore? {
    if let cached = ReminderStoreCache.instance { return cached }
    for attempt in 1...3 {
        let store = EKEventStore()
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        store.requestAccess(to: .reminder) { granted, _ in
            ok = granted; sem.signal()
        }
        _ = sem.wait(timeout: .now() + 5.0)
        if ok {
            ReminderStoreCache.instance = store
            return store
        }
        if attempt < 3 {
            // First-call dialog may still be settling; dismiss any
            // pending TCC dialog then try a fresh store.
            Thread.sleep(forTimeInterval: 1.0)
            dismissPermissionDialogs()
            Thread.sleep(forTimeInterval: 0.5)
        }
    }
    return nil
}

// Same caching discipline as Reminders, distinct cache for Calendar
// events: EKEventStore is one class but `.reminder` and `.event`
// have separate access grants on iOS 17+. Two caches × at most 1
// requestAccess each = 2 grant calls per runner session, well under
// the iOS rate-limit threshold the L2 test established.
class EventStoreCache {
    static var instance: EKEventStore?
}

// Same retry pattern as acquireReminderStore — see comment above.
func acquireEventStore() -> EKEventStore? {
    if let cached = EventStoreCache.instance { return cached }
    for attempt in 1...3 {
        let store = EKEventStore()
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        store.requestAccess(to: .event) { granted, _ in
            ok = granted; sem.signal()
        }
        _ = sem.wait(timeout: .now() + 5.0)
        if ok {
            EventStoreCache.instance = store
            return store
        }
        if attempt < 3 {
            Thread.sleep(forTimeInterval: 1.0)
            dismissPermissionDialogs()
            Thread.sleep(forTimeInterval: 0.5)
        }
    }
    return nil
}

// (freshReadStore — removed 2026-05-21 after introducing non-
// deterministic same-process reads in the toggle_all_day demo:
// consecutive freshReadStore() reads returned different counts
// even with no intervening mutation. Cross-process cache staleness
// is now addressed via `store.reset()` + `refreshSourcesIfNecessary()`
// at the top of list_events / list_calendars on the cached store.)

// ──────────────────────── Photos authorization ────────────────────
//
// Same caching discipline as EventKit/Contacts: PhotoKit's
// requestAuthorization succeeds once per runner session.
//
// iOS 14+ split the permission into .readWrite (full library) and
// .addOnly (write-only). The Photos handler needs read AND delete,
// so we ask for .readWrite. The legacy `simctl privacy grant photos`
// maps to .readWrite on iOS 17+.

class PhotoLibraryAuthCache {
    static var status: PHAuthorizationStatus?
}

func ensurePhotoLibraryAccess() -> Bool {
    if let cached = PhotoLibraryAuthCache.status,
        cached == .authorized {
        return true
    }
    for attempt in 1...3 {
        let sem = DispatchSemaphore(value: 0)
        var got: PHAuthorizationStatus = .notDetermined
        PHPhotoLibrary.requestAuthorization(for: .readWrite) { s in
            got = s; sem.signal()
        }
        _ = sem.wait(timeout: .now() + 5.0)
        if got == .authorized {
            PhotoLibraryAuthCache.status = got
            return true
        }
        if attempt < 3 {
            Thread.sleep(forTimeInterval: 1.0)
            dismissPermissionDialogs()
            Thread.sleep(forTimeInterval: 0.5)
        }
    }
    return false
}


// ──────────────────────── HealthKit authorization ──────────────────
//
// HealthKit's authorization is per-sample-type and split into
// read (share) and write (update) per type. SIBB-v1 supports a
// curated set of 3 quantity sample types:
//
//   step_count  → HKQuantityTypeIdentifier.stepCount, count
//   heart_rate  → HKQuantityTypeIdentifier.heartRate, count/min
//   body_mass   → HKQuantityTypeIdentifier.bodyMass,  kg
//
// Adding a new type means: register its identifier here, declare
// its unit, and ensure both NSHealthShareUsageDescription and
// NSHealthUpdateUsageDescription are present in the runner's
// Info.plist (the latter pair are auto-injected by Xcode but we
// set them explicitly in project.yml as belt-and-suspenders).

class HealthStoreCache {
    static var store: HKHealthStore?
}

/// Map from SIBB sample-type name to (HKQuantityTypeIdentifier raw, unit string).
/// Kept as a flat table so the Python side can validate names
/// before the socket call without coordinating with Swift.
let HEALTH_QUANTITY_TYPES: [String: (HKQuantityTypeIdentifier, String)] = [
    "step_count": (.stepCount,  "count"),
    "heart_rate": (.heartRate,  "count/min"),
    "body_mass":  (.bodyMass,   "kg"),
]

func healthSampleTypes() -> Set<HKSampleType> {
    var s: Set<HKSampleType> = []
    for (_, (id, _)) in HEALTH_QUANTITY_TYPES {
        if let t = HKObjectType.quantityType(forIdentifier: id) {
            s.insert(t)
        }
    }
    return s
}

func acquireHealthStore() -> HKHealthStore? {
    if let cached = HealthStoreCache.store { return cached }
    guard HKHealthStore.isHealthDataAvailable() else { return nil }
    let store = HKHealthStore()
    let types = healthSampleTypes()
    // requestAuthorization for HealthKit ALWAYS returns success=true
    // — even when the user denies. The actual grant state has to be
    // checked per-type via authorizationStatus(for:). On the sim
    // with TCC granted via simctl + Info.plist usage descriptions,
    // the request typically resolves silently to sharingAuthorized.
    for attempt in 1...3 {
        let sem = DispatchSemaphore(value: 0)
        var requestOk = false
        store.requestAuthorization(toShare: types, read: types) { ok, _ in
            requestOk = ok; sem.signal()
        }
        _ = sem.wait(timeout: .now() + 5.0)
        // Verify at least one type is authorized for sharing.
        var anyAuthorized = false
        for t in types {
            if store.authorizationStatus(for: t) == .sharingAuthorized {
                anyAuthorized = true; break
            }
        }
        if requestOk && anyAuthorized {
            HealthStoreCache.store = store
            return store
        }
        if attempt < 3 {
            Thread.sleep(forTimeInterval: 1.0)
            dismissPermissionDialogs()
            Thread.sleep(forTimeInterval: 0.5)
        }
    }
    return nil
}

// ──────────────────────── Files workspace helpers ─────────────────
//
// All Files-handler operations are scoped to a single workspace
// directory inside the runner's sandbox: ~/Documents/SIBBWorkspace/.
// Scoping prevents tests from clobbering arbitrary runner-side
// state and makes wipe_files a single-directory `rm -rf` rather
// than a discriminating sweep.
//
// Visibility caveat: this workspace is in the SIBBTests-Runner.app's
// sandbox, not "On My iPhone" or iCloud Drive. The Files UI won't
// see it unless the runner's Info.plist declares UIFileSharingEnabled
// (not currently set). The handler is still correct for filesystem
// state setup + verification; tasks that need the agent to browse
// to a path via the Files UI need that capability flipped on.

func sibbWorkspaceRoot() -> URL {
    let docs = FileManager.default.urls(
        for: .documentDirectory, in: .userDomainMask).first!
    let root = docs.appendingPathComponent("SIBBWorkspace",
                                            isDirectory: true)
    if !FileManager.default.fileExists(atPath: root.path) {
        try? FileManager.default.createDirectory(
            at: root, withIntermediateDirectories: true)
    }
    return root
}

func sibbWorkspaceURL(_ relativePath: String) -> URL? {
    // Reject absolute paths and `..` traversal — keep file ops
    // bounded to the workspace tree even with adversarial inputs.
    if relativePath.hasPrefix("/") { return nil }
    if relativePath.contains("..") { return nil }
    return sibbWorkspaceRoot()
        .appendingPathComponent(relativePath, isDirectory: false)
}


// Cross-process refresh: CNContactStore has no .reset() like EKEventStore.
// To see writes made by Contacts.app (the agent's UI mutations) from this
// test runner process, we MUST instantiate a fresh CNContactStore per call.
// Cache only the "permission was granted" flag so we don't redo the
// requestAccess dance on every call.
class ContactPermissionCache {
    static var granted: Bool = false
}

// Returns a fresh CNContactStore on every call. First call performs the
// requestAccess dance; subsequent calls reuse the granted-flag and skip
// straight to a new store instance.
func acquireContactStore() -> CNContactStore? {
    let store = CNContactStore()
    if ContactPermissionCache.granted { return store }
    for attempt in 1...3 {
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        store.requestAccess(for: .contacts) { granted, _ in
            ok = granted; sem.signal()
        }
        _ = sem.wait(timeout: .now() + 5.0)
        if ok {
            ContactPermissionCache.granted = true
            return store
        }
        if attempt < 3 {
            Thread.sleep(forTimeInterval: 1.0)
            dismissPermissionDialogs()
            Thread.sleep(forTimeInterval: 0.5)
        }
    }
    return nil
}

// ── Label canonicalization helpers for CNLabeledValue arrays ───────────
//
// SIBB JSON wire format uses lowercase friendly names ("mobile", "home",
// "work"); CN APIs require constant strings like CNLabelPhoneNumberMobile.
// These helpers map the JSON form → constant, defaulting to CNLabelOther
// when no match.

// All canonical*Label helpers preserve unknown / custom labels verbatim.
// This lets generators specify user-defined labels like "Beach House"
// or "Cabin" and round-trip them losslessly through list_contacts.
// Empty / "" / "other" maps to CNLabelOther explicitly.
func canonicalPhoneLabel(_ s: String?) -> String {
    let raw = s ?? ""
    switch raw.lowercased() {
    case "mobile", "cell":  return CNLabelPhoneNumberMobile
    case "iphone":          return CNLabelPhoneNumberiPhone
    case "main":            return CNLabelPhoneNumberMain
    case "home":            return CNLabelHome
    case "work":            return CNLabelWork
    case "home fax":        return CNLabelPhoneNumberHomeFax
    case "work fax":        return CNLabelPhoneNumberWorkFax
    case "other fax":       return CNLabelPhoneNumberOtherFax
    case "pager":           return CNLabelPhoneNumberPager
    case "", "other":       return CNLabelOther
    default:                return raw
    }
}

func canonicalEmailLabel(_ s: String?) -> String {
    let raw = s ?? ""
    switch raw.lowercased() {
    case "home":    return CNLabelHome
    case "work":    return CNLabelWork
    case "icloud":  return CNLabelEmailiCloud
    case "", "other": return CNLabelOther
    default:        return raw
    }
}

func canonicalAddressLabel(_ s: String?) -> String {
    let raw = s ?? ""
    switch raw.lowercased() {
    case "home":    return CNLabelHome
    case "work":    return CNLabelWork
    case "", "other": return CNLabelOther
    default:        return raw
    }
}

func canonicalURLLabel(_ s: String?) -> String {
    let raw = s ?? ""
    switch raw.lowercased() {
    case "homepage": return CNLabelURLAddressHomePage
    case "home":     return CNLabelHome
    case "work":     return CNLabelWork
    case "", "other": return CNLabelOther
    default:         return raw
    }
}

func canonicalDateLabel(_ s: String?) -> String {
    let raw = s ?? ""
    switch raw.lowercased() {
    case "anniversary": return CNLabelDateAnniversary
    case "", "other":   return CNLabelOther
    default:            return raw
    }
}

// Inverse: map CN constant back to lowercase friendly name for list_*
// output. Verifier-side compares against lowercase JSON labels.
func friendlyLabel(_ raw: String?) -> String {
    guard let r = raw else { return "" }
    switch r {
    case CNLabelPhoneNumberMobile:  return "mobile"
    case CNLabelPhoneNumberiPhone:  return "iphone"
    case CNLabelPhoneNumberMain:    return "main"
    case CNLabelPhoneNumberHomeFax: return "home fax"
    case CNLabelPhoneNumberWorkFax: return "work fax"
    case CNLabelPhoneNumberOtherFax:return "other fax"
    case CNLabelPhoneNumberPager:   return "pager"
    case CNLabelEmailiCloud:        return "icloud"
    case CNLabelURLAddressHomePage: return "homepage"
    case CNLabelDateAnniversary:    return "anniversary"
    case CNLabelHome:               return "home"
    case CNLabelWork:               return "work"
    case CNLabelOther:              return "other"
    default:                        return r
    }
}

// Parse "YYYY-MM-DD" (full date) or "--MM-DD" (year-omitted, iOS Contacts
// convention for birthdays whose year is unknown) into NSDateComponents.
// Returns nil for malformed strings AND for out-of-range month/day/year
// (rejects "1990-13-45" silent garbage). Does not validate leap years —
// CN accepts a "Feb 29 1991" NSDateComponents and renders nothing,
// which is the same UX a calendar UI would produce.
func parseContactDateComponents(_ s: String) -> NSDateComponents? {
    let comps = NSDateComponents()
    if s.hasPrefix("--") {
        let stripped = String(s.dropFirst(2))
        let parts = stripped.split(separator: "-")
        guard parts.count == 2,
              let m = Int(parts[0]), let d = Int(parts[1]),
              (1...12).contains(m), (1...31).contains(d) else {
            return nil
        }
        comps.month = m
        comps.day = d
        return comps
    }
    let parts = s.split(separator: "-")
    guard parts.count == 3,
          let y = Int(parts[0]), let m = Int(parts[1]), let d = Int(parts[2]),
          (1...9999).contains(y), (1...12).contains(m), (1...31).contains(d) else {
        return nil
    }
    comps.year = y
    comps.month = m
    comps.day = d
    return comps
}

// Serialize NSDateComponents back to "YYYY-MM-DD" (year present) or
// "--MM-DD" (year absent). Returns "" if month/day are undefined so
// downstream JSON serialization doesn't emit Int.max-formatted garbage
// for partial NSDateComponents.
func serializeContactDateComponents(_ dc: NSDateComponents) -> String {
    let mRaw = dc.month, dRaw = dc.day, yRaw = dc.year
    guard mRaw != NSDateComponentUndefined, dRaw != NSDateComponentUndefined,
          (1...12).contains(mRaw), (1...31).contains(dRaw) else {
        return ""
    }
    if yRaw == NSDateComponentUndefined || yRaw <= 0 {
        return String(format: "--%02d-%02d", CInt(mRaw), CInt(dRaw))
    }
    return String(format: "%04d-%02d-%02d",
                  CInt(yRaw), CInt(mRaw), CInt(dRaw))
}

// Choose a writable event calendar for new events. Prefer the
// store's `defaultCalendarForNewEvents`; fall back to any writable
// calendar in `.local` source; fall back to the first writable.
func defaultEventCalendar(_ store: EKEventStore) -> EKCalendar? {
    if let c = store.defaultCalendarForNewEvents,
       c.allowsContentModifications { return c }
    let writable = store.calendars(for: .event).filter {
        $0.allowsContentModifications
    }
    if let local = writable.first(where: { $0.source.sourceType == .local }) {
        return local
    }
    return writable.first
}

// ISO8601 parser tolerating both `2026-05-15T14:00:00` (local time)
// and `2026-05-15T14:00:00Z` (UTC). iOS Reminders/Calendar JSON in
// SIBB uses ISO strings on the wire; we parse here and store as Date.
func parseISODate(_ s: String) -> Date? {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    if let d = f.date(from: s) { return d }
    // Fallback: no timezone → interpret in device local time.
    let f2 = DateFormatter()
    f2.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    f2.timeZone = TimeZone.current
    return f2.date(from: s)
}

func formatISODate(_ d: Date) -> String {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f.string(from: d)
}

// Shared serializer for EKRecurrenceRule, used by both
// `list_reminders` (EKReminder.recurrenceRules) and `list_events`
// (EKEvent.recurrenceRules). Both EventKit entity types expose the
// same `recurrenceRules` array shape, so one serializer handles both.
//
// Output shape mirrors the create_* parser's input format:
//   {"frequency": "daily|weekly|monthly|yearly",
//    "interval":  Int (>= 1),
//    "end_iso":   "YYYY-MM-DDTHH:MM:SS" (local TZ, exclusive: end_iso XOR end_count)
//    "end_count": Int (mutually exclusive with end_iso)}
//
// Returns nil if rule is unrecognized. `firstRecurrence` extracts
// the first rule from a `recurrenceRules` array (matches our
// "iOS Calendar/Reminders UI only edits the first rule" convention).
func firstRecurrence(_ rules: [EKRecurrenceRule]?) -> [String: Any]? {
    guard let rules = rules, let rule = rules.first else { return nil }
    let freqStr: String
    switch rule.frequency {
    case .daily:   freqStr = "daily"
    case .weekly:  freqStr = "weekly"
    case .monthly: freqStr = "monthly"
    case .yearly:  freqStr = "yearly"
    @unknown default: freqStr = "unknown"
    }
    var rec: [String: Any] = [
        "frequency": freqStr,
        "interval":  rule.interval,
    ]
    if let end = rule.recurrenceEnd {
        if end.occurrenceCount > 0 {
            rec["end_count"] = end.occurrenceCount
        } else if let endDate = end.endDate {
            let f = DateFormatter()
            f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            f.timeZone = TimeZone.current
            rec["end_iso"] = f.string(from: endDate)
        }
    }
    return rec
}

// Fetch every event within a wide window — used by wipe / list when
// no narrower window is specified by the caller. EventKit's
// predicateForEvents requires a window (unlike fetchReminders).
func fetchAllEvents(_ store: EKEventStore,
                    start: Date? = nil, end: Date? = nil) -> [EKEvent] {
    let now = Date()
    let windowStart = start ?? Calendar.current.date(byAdding: .year,
                                                      value: -1, to: now)!
    let windowEnd   = end ?? Calendar.current.date(byAdding: .year,
                                                    value: 2, to: now)!
    let cals = store.calendars(for: .event)
    let predicate = store.predicateForEvents(withStart: windowStart,
                                              end: windowEnd,
                                              calendars: cals)
    return store.events(matching: predicate)
}

// Pick a usable source for new reminders. Prefer the local source
// (always available in the simulator); fall back to whatever the
// store thinks is default.
func defaultReminderSource(_ store: EKEventStore) -> EKSource? {
    if let s = store.sources.first(where: { $0.sourceType == .local }) { return s }
    if let s = store.defaultCalendarForNewReminders()?.source { return s }
    return store.sources.first
}

// Find a reminder calendar (a.k.a. "list") by case-insensitive name match.
func findReminderList(_ store: EKEventStore, name: String) -> EKCalendar? {
    return store.calendars(for: .reminder).first {
        $0.title.lowercased() == name.lowercased()
    }
}

// Fetch all reminders matching the store's full predicate, synchronously.
// EKEventStore.fetchReminders is async; we wrap with a semaphore.
func fetchAllReminders(_ store: EKEventStore) -> [EKReminder] {
    let predicate = store.predicateForReminders(in: nil)
    var result: [EKReminder] = []
    let sem = DispatchSemaphore(value: 0)
    store.fetchReminders(matching: predicate) { rems in
        result = rems ?? []
        sem.signal()
    }
    _ = sem.wait(timeout: .now() + 10.0)
    return result
}

// Auto-dismiss any leftover permission dialog owned by SpringBoard.
//
// On iOS 17+, even when TCC has auth_value=2 for the test runner (so
// EKEventStore.requestAccess returns granted=true synchronously), iOS
// still posts a transparency dialog "<App> would like to access your
// Reminders" after the first programmatic access. The dialog is
// non-blocking — our setup keeps working — but it sticks on screen
// until tapped, which pollutes the agent's first observation. We
// snapshot SpringBoard, find any button labeled Allow/OK/Continue,
// and tap it. Best-effort: returns silently if no dialog is present.
func dismissPermissionDialogs() {
    let sb = appFor("com.apple.springboard")
    var snap: XCUIElementSnapshot? = nil
    SIBBSafeRun(nil) { snap = try? sb.snapshot() }
    guard let root = snap else { return }
    let dismissLabels: Set<String> = [
        "Allow", "OK", "Continue", "Allow Once",
        // iOS 17+ Photos prompts with these distinct labels; the
        // bare "Allow" doesn't appear in the photo-library auth
        // dialog. Without these, PHPhotoLibrary.requestAuthorization
        // hangs on .notDetermined even after TCC.db grant.
        "Allow Full Access", "Allow Access to All Photos",
    ]
    func find(_ s: XCUIElementSnapshot) -> CGRect? {
        if s.elementType == .button && dismissLabels.contains(s.label) {
            return s.frame
        }
        for c in s.children {
            if let f = find(c) { return f }
        }
        return nil
    }
    guard let f = find(root) else { return }
    let coord = sb.coordinate(withNormalizedOffset: .zero)
        .withOffset(CGVector(dx: f.midX, dy: f.midY))
    coord.tap()
    // Wait briefly for the dialog dismissal animation to complete.
    Thread.sleep(forTimeInterval: 0.3)
}

// ── Main server class ────────────────────────────────────────────
class SIBBServer: XCTestCase {

    func testRunServer() {
        // Socket path unique per simulator — use env var set by Python client
        let udid = ProcessInfo.processInfo.environment["SIBB_UDID"] ?? "default"
        let socketPath = "/tmp/sibb_xcuitest_\(udid).sock"

        // Remove stale socket
        try? FileManager.default.removeItem(atPath: socketPath)

        // Create Unix domain socket
        let serverFd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard serverFd >= 0 else { return }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        socketPath.withCString { ptr in
            withUnsafeMutablePointer(to: &addr.sun_path) {
                $0.withMemoryRebound(to: CChar.self, capacity: 108) {
                    _ = strcpy($0, ptr)
                }
            }
        }

        let addrLen = socklen_t(MemoryLayout<sockaddr_un>.size)
        withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(serverFd, $0, addrLen)
            }
        }
        listen(serverFd, 1)

        // Signal ready via stdout (xcodebuild will show this)
        print("SIBB_READY socket=\(socketPath)")
        fflush(stdout)

        // Extend test timeout — XCUITest will kill the test after
        // DefaultTestExecutionTimeAllowance seconds otherwise
        // We handle this by setting a very long continueAfterFailure window
        continueAfterFailure = true

        // Accept one connection
        let clientFd = accept(serverFd, nil, nil)
        guard clientFd >= 0 else { return }

        let connection = FileHandle(fileDescriptor: clientFd, closeOnDealloc: true)
        var currentApp: XCUIApplication?
        var currentBID: String?
        let fgCandidates = parseForegroundCandidates()
        print("SIBB_CANDIDATES count=\(fgCandidates.count)")
        fflush(stdout)
        // Fail-fast AX queries so a stuck accessibility call doesn't
        // hang observe for 60+ seconds and trigger Python timeouts.
        configureAXTimeout(3.0)
        fflush(stdout)

        // Command loop — readLine blocks until data arrives (correct for socket)
        while true {
            guard let data = readLine(from: connection),
                  !data.isEmpty,
                  let cmdData = data.data(using: .utf8),
                  let cmdDict = try? JSONSerialization.jsonObject(with: cmdData) as? [String: Any],
                  let type = cmdDict["type"] as? String
            else { continue }

            switch type {
            case "ping":
                sendResponse(connection, ["ok": true])

            case "frontmost":
                // Diagnostic: report the frontmost app PID via private API.
                let pid = getFrontmostPID()
                sendResponse(connection, ["ok": true, "pid": Int(pid)])

            case "create_list":
                // EventKit-backed: create a Reminders list (calendar of
                // type .reminder). Apple's API maintains the account
                // manifest, identifiers, and CK mirror automatically.
                let name = cmdDict["name"] as? String ?? ""
                guard !name.isEmpty else {
                    sendResponse(connection, ["ok": false, "error": "name required"])
                    break
                }
                guard let store = acquireReminderStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no reminders permission for test runner"])
                    break
                }
                guard let source = defaultReminderSource(store) else {
                    sendResponse(connection, ["ok": false,
                        "error": "no reminder source available"])
                    break
                }
                let cal = EKCalendar(for: .reminder, eventStore: store)
                cal.title = name
                cal.source = source
                do {
                    try store.saveCalendar(cal, commit: true)
                    dismissPermissionDialogs()
                    sendResponse(connection, ["ok": true,
                        "name": name, "identifier": cal.calendarIdentifier])
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "saveCalendar failed: \(error.localizedDescription)"])
                }

            case "create_reminder":
                // EventKit-backed: create a reminder in an existing list.
                // Optional fields (due_iso / notes / url) match the
                // settable surface of EKReminder.dueDateComponents /
                // .notes / .url. Due-date with a time component sets an
                // alarm at that moment per EventKit's default behavior;
                // date-only ISO strings get year/month/day only.
                let title    = cmdDict["title"]    as? String ?? ""
                let listName = cmdDict["list"]     as? String ?? ""
                let priority = cmdDict["priority"] as? String
                let done     = cmdDict["completed"] as? Bool ?? false
                let dueISO   = cmdDict["due_iso"]  as? String
                let notesStr = cmdDict["notes"]    as? String
                let urlStr   = cmdDict["url"]      as? String
                let recurrenceDict = cmdDict["recurrence"] as? [String: Any]
                guard !title.isEmpty, !listName.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "title and list required"])
                    break
                }
                guard let store = acquireReminderStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no reminders permission"])
                    break
                }
                guard let cal = findReminderList(store, name: listName) else {
                    sendResponse(connection, ["ok": false,
                        "error": "list \(listName) not found"])
                    break
                }
                let r = EKReminder(eventStore: store)
                r.title = title
                r.calendar = cal
                if let p = priority, let pi = EVENTKIT_PRIORITY[p.lowercased()] {
                    r.priority = pi
                }
                r.isCompleted = done
                if let iso = dueISO {
                    // Two input formats are accepted:
                    //   "YYYY-MM-DD"           → date-only (no hour/minute
                    //                            components → iOS Reminders
                    //                            renders as date-only in UI).
                    //   "YYYY-MM-DDTHH:MM:SS"  → date + time, local TZ
                    //                            (also accepts trailing "Z"
                    //                            for UTC via parseISODate).
                    if !iso.contains("T") {
                        let parts = iso.split(separator: "-")
                        if parts.count == 3,
                           let y = Int(parts[0]),
                           let m = Int(parts[1]),
                           let d = Int(parts[2]) {
                            var comps = DateComponents()
                            comps.year = y
                            comps.month = m
                            comps.day = d
                            r.dueDateComponents = comps
                        }
                    } else if let due = parseISODate(iso) {
                        r.dueDateComponents = Calendar.current.dateComponents(
                            [.year, .month, .day, .hour, .minute],
                            from: due)
                    }
                }
                if let n = notesStr, !n.isEmpty {
                    r.notes = n
                }
                if let us = urlStr, !us.isEmpty,
                   let parsed = URL(string: us) {
                    r.url = parsed
                }
                if let rec = recurrenceDict {
                    // Validation per the 2026-05-20 critic pass:
                    //   • Recurrence requires a due date. EKReminder
                    //     silently drops the rule on save without
                    //     dueDateComponents; we reject loudly instead.
                    //   • interval must be >= 1; EKRecurrenceRule
                    //     throws NSInvalidArgumentException otherwise.
                    //   • end_iso and end_count are mutually exclusive
                    //     (EKRecurrenceEnd is a sum type).
                    //   • Date-only end_iso parses as end-of-day local
                    //     (23:59:59) because UNTIL is RFC-5545 inclusive
                    //     and reminders due late in the day on the end
                    //     date would otherwise be excluded.
                    if dueISO == nil {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence requires due_iso"])
                        break
                    }
                    guard let freqRaw = rec["frequency"] as? String else {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence requires frequency"])
                        break
                    }
                    let freqMap: [String: EKRecurrenceFrequency] = [
                        "daily":   .daily,
                        "weekly":  .weekly,
                        "monthly": .monthly,
                        "yearly":  .yearly,
                    ]
                    guard let freq = freqMap[freqRaw.lowercased()] else {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence frequency must be one of "
                                     + "daily/weekly/monthly/yearly, got "
                                     + "\(freqRaw)"])
                        break
                    }
                    let interval = rec["interval"] as? Int ?? 1
                    if interval < 1 {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence interval must be >= 1, "
                                     + "got \(interval)"])
                        break
                    }
                    let hasEndISO   = rec["end_iso"] is String
                    let hasEndCount = rec["end_count"] is Int
                    if hasEndISO && hasEndCount {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence end_iso and end_count "
                                     + "are mutually exclusive"])
                        break
                    }
                    var end: EKRecurrenceEnd? = nil
                    if let endISO = rec["end_iso"] as? String {
                        if !endISO.contains("T") {
                            // Date-only → end-of-day local (23:59:59).
                            let parts = endISO.split(separator: "-")
                            if parts.count == 3,
                               let y = Int(parts[0]),
                               let m = Int(parts[1]),
                               let d = Int(parts[2]) {
                                var c = DateComponents()
                                c.year = y; c.month = m; c.day = d
                                c.hour = 23; c.minute = 59; c.second = 59
                                if let dt = Calendar.current.date(from: c) {
                                    end = EKRecurrenceEnd(end: dt)
                                }
                            }
                        } else if let dt = parseISODate(endISO) {
                            end = EKRecurrenceEnd(end: dt)
                        }
                    } else if let count = rec["end_count"] as? Int,
                               count > 0 {
                        end = EKRecurrenceEnd(occurrenceCount: count)
                    }
                    let rule = EKRecurrenceRule(
                        recurrenceWith: freq,
                        interval: interval,
                        end: end)
                    r.recurrenceRules = [rule]
                }
                do {
                    try store.save(r, commit: true)
                    dismissPermissionDialogs()
                    sendResponse(connection, ["ok": true, "title": title,
                        "list": listName, "identifier": r.calendarItemIdentifier])
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "save failed: \(error.localizedDescription)"])
                }

            case "list_lists":
                // EventKit-backed read. Returns every reminder list
                // (calendar of type .reminder) with its name + identifier.
                // Used by the verifier — same code path as create_list.
                guard let store = acquireReminderStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no reminders permission"])
                    break
                }
                let cals = store.calendars(for: .reminder)
                let rows: [[String: Any]] = cals.map { c in
                    [
                        "name":       c.title,
                        "identifier": c.calendarIdentifier,
                        "immutable":  c.isImmutable,
                    ]
                }
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true, "lists": rows])

            case "list_reminders":
                // EventKit-backed read of all (non-completed) reminders.
                // Optional filter on the list name (case-insensitive).
                // Surfaces due (ISO), notes, url when present — the
                // verifier-side fetcher passes these through so
                // agent_answer and attribute_eq checks can target them.
                let listFilter = cmdDict["list"] as? String
                let includeCompleted = cmdDict["include_completed"] as? Bool ?? false
                guard let store = acquireReminderStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no reminders permission"])
                    break
                }
                let rems = fetchAllReminders(store)
                var rows: [[String: Any]] = []
                for r in rems {
                    if !includeCompleted && r.isCompleted { continue }
                    if let lf = listFilter,
                       r.calendar.title.lowercased() != lf.lowercased() { continue }
                    var row: [String: Any] = [
                        "title":      r.title ?? "",
                        "list":       r.calendar.title,
                        "priority":   r.priority,
                        "completed":  r.isCompleted,
                        "identifier": r.calendarItemIdentifier,
                    ]
                    if let comps = r.dueDateComponents {
                        // Round-trip the canonical format so the verifier
                        // can do string equality:
                        //   No hour/minute set → "YYYY-MM-DD" (date-only,
                        //                         what iOS renders too).
                        //   Hour/minute set    → local-time ISO without Z
                        //                         ("YYYY-MM-DDTHH:MM:SS").
                        // We deliberately avoid `formatISODate` here
                        // because it emits UTC-Z and would force callers
                        // to think in two timezones.
                        if comps.hour == nil && comps.minute == nil {
                            if let y = comps.year, let m = comps.month,
                               let d = comps.day {
                                row["due"] = String(
                                    format: "%04d-%02d-%02d", y, m, d)
                            }
                        } else if let due = Calendar.current.date(from: comps) {
                            let f = DateFormatter()
                            f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
                            f.timeZone = TimeZone.current
                            row["due"] = f.string(from: due)
                        }
                    }
                    if let n = r.notes, !n.isEmpty {
                        row["notes"] = n
                    }
                    if let u = r.url {
                        row["url"] = u.absoluteString
                    }
                    // Surface the active recurrence rule (first one
                    // only — EKReminder.recurrenceRules is a list but
                    // iOS Reminders UI only edits the first). Inverse
                    // of the create_reminder mapping: frequency string,
                    // interval int, end_iso or end_count if set.
                    if let rules = r.recurrenceRules,
                       let rule  = rules.first {
                        let freqStr: String = {
                            switch rule.frequency {
                            case .daily:   return "daily"
                            case .weekly:  return "weekly"
                            case .monthly: return "monthly"
                            case .yearly:  return "yearly"
                            @unknown default: return "unknown"
                            }
                        }()
                        var rec: [String: Any] = [
                            "frequency": freqStr,
                            "interval":  rule.interval,
                        ]
                        if let end = rule.recurrenceEnd {
                            if end.occurrenceCount > 0 {
                                rec["end_count"] = end.occurrenceCount
                            } else if let endDate = end.endDate {
                                let f = DateFormatter()
                                f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
                                f.timeZone = TimeZone.current
                                rec["end_iso"] = f.string(from: endDate)
                            }
                        }
                        row["recurrence"] = rec
                    }
                    rows.append(row)
                }
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true, "reminders": rows])

            case "wipe_reminders":
                // EventKit-backed reset: delete every reminder and every
                // user-created list. The default 'Reminders' list cannot
                // be deleted (EKCalendar.allowsContentModifications==false
                // / EKCalendar.immutable on system lists), so we silently
                // skip those — they'll just be empty after the reminder
                // sweep.
                guard let store = acquireReminderStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no reminders permission"])
                    break
                }
                let rems = fetchAllReminders(store)
                for r in rems {
                    try? store.remove(r, commit: false)
                }
                try? store.commit()
                var deletedLists = 0
                for c in store.calendars(for: .reminder) where !c.isImmutable {
                    do {
                        try store.removeCalendar(c, commit: true)
                        deletedLists += 1
                    } catch {
                        // System lists may refuse removal; skip them.
                    }
                }
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true,
                    "removed_reminders": rems.count,
                    "removed_lists":     deletedLists])

            // ── Calendar (EKEvent) commands ─────────────────────────────
            //
            // Mirror the Reminders pattern: EventKit handles writes via
            // the same EKEventStore class; we use a separate store cache
            // because access grants are per-entity. Per-event identifiers
            // are eventIdentifier (NOT calendarItemIdentifier — the latter
            // changes when an event is moved; the former is stable for
            // SIBB's reset/identity semantics).

            case "create_event":
                // Required: title, start_iso, end_iso.
                // Optional: calendar (name), all_day (Bool), location, notes, url,
                //          recurrence (Dict: frequency, interval, end_iso XOR end_count).
                let title    = cmdDict["title"]     as? String ?? ""
                let startISO = cmdDict["start_iso"] as? String ?? ""
                let endISO   = cmdDict["end_iso"]   as? String ?? ""
                let calName  = cmdDict["calendar"]  as? String
                let allDay   = cmdDict["all_day"]   as? Bool ?? false
                let location = cmdDict["location"]  as? String
                let notes    = cmdDict["notes"]     as? String
                let urlStr   = cmdDict["url"]       as? String
                let recurrenceDict = cmdDict["recurrence"] as? [String: Any]
                guard !title.isEmpty, !startISO.isEmpty, !endISO.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "title, start_iso, end_iso required"])
                    break
                }
                guard let start = parseISODate(startISO),
                      let end   = parseISODate(endISO) else {
                    sendResponse(connection, ["ok": false,
                        "error": "could not parse start_iso/end_iso (ISO8601)"])
                    break
                }
                guard let store = acquireEventStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no calendar permission for test runner"])
                    break
                }
                let chosenCal: EKCalendar?
                if let n = calName {
                    chosenCal = store.calendars(for: .event)
                        .first(where: { $0.title.lowercased() == n.lowercased()
                                         && $0.allowsContentModifications })
                } else {
                    chosenCal = defaultEventCalendar(store)
                }
                guard let cal = chosenCal else {
                    sendResponse(connection, ["ok": false,
                        "error": "no writable calendar available"])
                    break
                }
                let ev = EKEvent(eventStore: store)
                ev.title     = title
                ev.startDate = start
                ev.endDate   = end
                ev.calendar  = cal
                ev.isAllDay  = allDay
                ev.location  = location
                ev.notes     = notes
                if let us = urlStr, !us.isEmpty {
                    if let parsed = URL(string: us) {
                        ev.url = parsed
                    } else {
                        // Don't silently drop — surface the parse
                        // failure so the agent (or the generator
                        // author) knows the input was rejected
                        // rather than treating empty-string readback
                        // as "no URL set."
                        sendResponse(connection, ["ok": false,
                            "error": "url failed to parse: \(us)"])
                        break
                    }
                }
                // Recurrence parsing — mirrors create_reminder's block,
                // minus the "requires due_iso" check (events always have
                // start/end). Validators ported verbatim from the Reminders
                // implementation per Phase 2c critic review.
                if let rec = recurrenceDict {
                    guard let freqRaw = rec["frequency"] as? String else {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence requires frequency"])
                        break
                    }
                    let freqMap: [String: EKRecurrenceFrequency] = [
                        "daily":   .daily,
                        "weekly":  .weekly,
                        "monthly": .monthly,
                        "yearly":  .yearly,
                    ]
                    guard let freq = freqMap[freqRaw.lowercased()] else {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence frequency must be one of "
                                     + "daily/weekly/monthly/yearly, got "
                                     + "\(freqRaw)"])
                        break
                    }
                    let interval = rec["interval"] as? Int ?? 1
                    if interval < 1 {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence interval must be >= 1, "
                                     + "got \(interval)"])
                        break
                    }
                    let hasEndISO   = rec["end_iso"] is String
                    let hasEndCount = rec["end_count"] is Int
                    if hasEndISO && hasEndCount {
                        sendResponse(connection, ["ok": false,
                            "error": "recurrence end_iso and end_count "
                                     + "are mutually exclusive"])
                        break
                    }
                    var endRule: EKRecurrenceEnd? = nil
                    if let endISO = rec["end_iso"] as? String {
                        if !endISO.contains("T") {
                            // Date-only → end-of-day local (RFC 5545
                            // UNTIL is inclusive).
                            let parts = endISO.split(separator: "-")
                            if parts.count == 3,
                               let y = Int(parts[0]),
                               let m = Int(parts[1]),
                               let d = Int(parts[2]) {
                                var c = DateComponents()
                                c.year = y; c.month = m; c.day = d
                                c.hour = 23; c.minute = 59; c.second = 59
                                if let dt = Calendar.current.date(from: c) {
                                    endRule = EKRecurrenceEnd(end: dt)
                                }
                            }
                        } else if let dt = parseISODate(endISO) {
                            endRule = EKRecurrenceEnd(end: dt)
                        }
                    } else if let count = rec["end_count"] as? Int,
                               count > 0 {
                        endRule = EKRecurrenceEnd(occurrenceCount: count)
                    }
                    let rule = EKRecurrenceRule(
                        recurrenceWith: freq,
                        interval: interval,
                        end: endRule)
                    ev.recurrenceRules = [rule]
                }
                do {
                    try store.save(ev, span: .thisEvent, commit: true)
                    dismissPermissionDialogs()
                    sendResponse(connection, ["ok": true,
                        "title":      title,
                        "calendar":   cal.title,
                        "identifier": ev.eventIdentifier ?? ""])
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "save failed: \(error.localizedDescription)"])
                }

            case "list_events":
                // Optional: calendar (name filter), start_iso/end_iso (window),
                // writable_only (Bool, default true — exclude US Holidays and
                // other read-only subscribed calendars; see IOS_SIM_QUIRKS §16),
                // master_only (Bool, default true — dedup recurring occurrences
                // to one row per series, master's startDate). Per probe Q6.2
                // (2026-05-21) iOS expands recurring events into N rows
                // sharing the same `eventIdentifier`; without dedup, every
                // count check on a recurring corpus is wrong.
                // Defaults to a ±1y window around now if no window specified.
                let calFilter = cmdDict["calendar"]  as? String
                let startISO  = cmdDict["start_iso"] as? String
                let endISO    = cmdDict["end_iso"]   as? String
                let writableOnly = (cmdDict["writable_only"] as? Bool) ?? true
                let masterOnly = (cmdDict["master_only"] as? Bool) ?? true
                guard let store = acquireEventStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no calendar permission"])
                    break
                }
                // Cross-process refresh attempt. iOS Calendar.app
                // mutations aren't visible to our cached store
                // immediately (IOS_SIM_QUIRKS §17). reset() is
                // deprecated but documented to "release any cached
                // values" — try it first to invalidate the cache,
                // then refreshSourcesIfNecessary() to pull updates.
                // Empirically validated 2026-05-21 that fresh-store-
                // per-read introduces NON-DETERMINISTIC same-process
                // reads (counts drift between consecutive queries).
                store.reset()
                store.refreshSourcesIfNecessary()
                let start = startISO.flatMap { parseISODate($0) }
                let end   = endISO.flatMap { parseISODate($0) }
                let events = fetchAllEvents(store, start: start, end: end)
                var rows: [[String: Any]] = []
                // Canonical wire format for start_iso/end_iso matches
                // the create_event side: local-time ISO without Z
                // ("YYYY-MM-DDTHH:MM:SS"). formatISODate emits UTC-Z
                // which round-trips through EventKit but breaks the
                // string-equality verifier path (mirrors Reminders'
                // due-date canonicalization at the case "list_reminders"
                // block above). All-day events get a date-only "YYYY-MM-DD"
                // form for both start and end (matches the iOS Calendar
                // UI's user-visible representation).
                let localFmt = DateFormatter()
                localFmt.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
                localFmt.timeZone = TimeZone.current
                let dateOnlyFmt = DateFormatter()
                dateOnlyFmt.dateFormat = "yyyy-MM-dd"
                dateOnlyFmt.timeZone = TimeZone.current
                // `events(matching:)` expands recurring masters into N
                // occurrences sharing the same eventIdentifier. When
                // masterOnly=true, dedup so we emit one row per series.
                // The chronologically-first occurrence is the master IF
                // the rule started in/after the query window (true for
                // all generator-seeded recurring events, which always
                // start in the future). For corpora with past-detached
                // occurrences, this approximation may differ; document
                // and revisit if a generator surfaces the gap.
                var seenIdentifiers = Set<String>()
                for e in events {
                    if writableOnly && !e.calendar.allowsContentModifications {
                        continue
                    }
                    if let cf = calFilter,
                       e.calendar.title.lowercased() != cf.lowercased() { continue }
                    if masterOnly, let eid = e.eventIdentifier {
                        if seenIdentifiers.contains(eid) { continue }
                        seenIdentifiers.insert(eid)
                    }
                    let startISO: String
                    let endISO: String
                    if e.isAllDay {
                        startISO = dateOnlyFmt.string(from: e.startDate)
                        endISO   = dateOnlyFmt.string(from: e.endDate)
                    } else {
                        startISO = localFmt.string(from: e.startDate)
                        endISO   = localFmt.string(from: e.endDate)
                    }
                    var row: [String: Any] = [
                        "title":      e.title ?? "",
                        "calendar":   e.calendar.title,
                        "start_iso":  startISO,
                        "end_iso":    endISO,
                        "all_day":    e.isAllDay,
                        "location":   e.location ?? "",
                        "notes":      e.notes ?? "",
                        "url":        e.url?.absoluteString ?? "",
                        "identifier": e.eventIdentifier ?? "",
                    ]
                    if let rec = firstRecurrence(e.recurrenceRules) {
                        row["recurrence"] = rec
                    }
                    rows.append(row)
                }
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true, "events": rows])

            case "wipe_events":
                // EventKit-backed reset: delete every event in writable
                // calendars. Default ±1y window. System calendars (Holidays,
                // birthdays) typically refuse modifications and are skipped.
                guard let store = acquireEventStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no calendar permission"])
                    break
                }
                let events = fetchAllEvents(store)
                var removed = 0
                for e in events {
                    if !e.calendar.allowsContentModifications { continue }
                    do {
                        try store.remove(e, span: .thisEvent, commit: false)
                        removed += 1
                    } catch {
                        // System events may refuse removal; skip.
                    }
                }
                try? store.commit()
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true,
                    "removed_events": removed])

            case "create_calendar":
                // Create a user-visible EKCalendar of type .event on
                // the writable local source. Used by T2/3 multi-calendar
                // tasks (move-between-calendars, delete-by-calendar).
                // Reject duplicates and reject the default "Calendar"
                // name so generators can't shadow the iOS default.
                let calName = cmdDict["name"] as? String ?? ""
                let colorHex = cmdDict["color"] as? String
                guard !calName.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "name required"])
                    break
                }
                guard let store = acquireEventStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no calendar permission"])
                    break
                }
                // Reject if a calendar with this name already exists
                // (case-insensitive). Prevents shadowing the default
                // "Calendar" AND duplicate-name collisions across reruns.
                if store.calendars(for: .event).contains(where: {
                    $0.title.lowercased() == calName.lowercased()
                }) {
                    sendResponse(connection, ["ok": false,
                        "error": "calendar with name \(calName) already exists"])
                    break
                }
                // Pick a writable source. EKSource.local on the sim is
                // the only reliably-present writable source.
                let writableSource = store.sources.first(where: {
                    $0.sourceType == .local
                })
                guard let src = writableSource else {
                    sendResponse(connection, ["ok": false,
                        "error": "no .local writable source available"])
                    break
                }
                let cal = EKCalendar(for: .event, eventStore: store)
                cal.title = calName
                cal.source = src
                if let hex = colorHex, hex.hasPrefix("#"), hex.count == 7 {
                    // "#RRGGBB" — parse to UIColor → cgColor. Bad hex
                    // strings fall back to the iOS-picked default.
                    let r = UInt8(hex[hex.index(hex.startIndex, offsetBy: 1)..<hex.index(hex.startIndex, offsetBy: 3)], radix: 16) ?? 0
                    let g = UInt8(hex[hex.index(hex.startIndex, offsetBy: 3)..<hex.index(hex.startIndex, offsetBy: 5)], radix: 16) ?? 0
                    let b = UInt8(hex[hex.index(hex.startIndex, offsetBy: 5)..<hex.index(hex.startIndex, offsetBy: 7)], radix: 16) ?? 0
                    cal.cgColor = UIColor(red: CGFloat(r) / 255.0,
                                            green: CGFloat(g) / 255.0,
                                            blue: CGFloat(b) / 255.0,
                                            alpha: 1.0).cgColor
                }
                do {
                    try store.saveCalendar(cal, commit: true)
                    dismissPermissionDialogs()
                    sendResponse(connection, ["ok": true,
                        "name": calName,
                        "identifier": cal.calendarIdentifier])
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "saveCalendar failed: \(error.localizedDescription)"])
                }

            case "list_calendars":
                // Return every writable, non-immutable user-event calendar.
                // Subscribed calendars (US Holidays, Birthdays, Siri
                // Suggestions) are skipped via allowsContentModifications.
                guard let store = acquireEventStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no calendar permission"])
                    break
                }
                // Cross-process refresh (see list_events comment).
                store.reset()
                store.refreshSourcesIfNecessary()
                var rows: [[String: Any]] = []
                for c in store.calendars(for: .event) {
                    if !c.allowsContentModifications { continue }
                    rows.append([
                        "name":       c.title,
                        "identifier": c.calendarIdentifier,
                        "source":     c.source.title,
                    ])
                }
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true, "calendars": rows])

            case "wipe_calendars":
                // Delete every user-CREATED calendar (anything writable
                // that isn't the iOS default "Calendar"). Mirrors
                // wipe_events but at the calendar-collection level.
                // Used by CalendarHandler.reset between episodes.
                guard let store = acquireEventStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no calendar permission"])
                    break
                }
                var removed = 0
                for c in store.calendars(for: .event) {
                    if !c.allowsContentModifications { continue }
                    // Preserve the iOS default "Calendar" — it's the
                    // only writable calendar on a fresh sim and removing
                    // it would orphan future events.
                    if c.title == "Calendar" { continue }
                    do {
                        try store.removeCalendar(c, commit: false)
                        removed += 1
                    } catch {
                        // Skip on failure (subscribed / system).
                    }
                }
                try? store.commit()
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true,
                    "removed_calendars": removed])

            // ── Contacts (CNContact) commands ───────────────────────────
            //
            // Backed by the Contacts framework — same shape as EventKit
            // (Reminders/Calendar): cache the store after the first
            // requestAccess, then drive create/list/wipe through a
            // CNSaveRequest. The on-sim "On My iPhone" container is
            // always available — no iCloud account configuration needed
            // for tests to write here.

            case "create_contact":
                // Required: given_name OR family_name (at least one).
                //
                // Optional simple fields:
                //   middle_name, nickname, phonetic_given_name,
                //   phonetic_family_name, phonetic_middle_name,
                //   organization, job_title, department,
                //   birthday ("YYYY-MM-DD" or "--MM-DD" for year-unknown)
                //
                // Optional labeled-multi-value arrays:
                //   phones: [{label, value}]            (CNLabeledValue<CNPhoneNumber>)
                //   emails: [{label, value}]            (CNLabeledValue<NSString>)
                //   postal_addresses: [{label, street, city, state,
                //                       postal_code, country}]
                //   urls:   [{label, value}]
                //   dates:  [{label, iso}]              (anniversary etc.)
                //
                // Legacy single-value `phone` / `email` strings are still
                // honored when the multi-value array is absent — keeps
                // existing generators working without churn.
                let given  = cmdDict["given_name"]    as? String ?? ""
                let family = cmdDict["family_name"]   as? String ?? ""
                let middle = cmdDict["middle_name"]   as? String ?? ""
                let nickname = cmdDict["nickname"]    as? String ?? ""
                let phoneticGiven  = cmdDict["phonetic_given_name"]  as? String ?? ""
                let phoneticFamily = cmdDict["phonetic_family_name"] as? String ?? ""
                let phoneticMiddle = cmdDict["phonetic_middle_name"] as? String ?? ""
                let org    = cmdDict["organization"]  as? String
                let jobTitle = cmdDict["job_title"]   as? String
                let department = cmdDict["department"] as? String
                let birthdayStr = cmdDict["birthday"] as? String
                let phone  = cmdDict["phone"]         as? String
                let email  = cmdDict["email"]         as? String
                let phones = cmdDict["phones"] as? [[String: String]]
                let emails = cmdDict["emails"] as? [[String: String]]
                let addresses = cmdDict["postal_addresses"] as? [[String: String]]
                let urls   = cmdDict["urls"]  as? [[String: String]]
                let dates  = cmdDict["dates"] as? [[String: String]]
                guard !given.isEmpty || !family.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "given_name or family_name required"])
                    break
                }
                // Reject silent-winner conflicts: legacy single + new array
                // simultaneously specified leaves the legacy form ignored
                // without warning. Force the caller to pick.
                if cmdDict["phone"] != nil && cmdDict["phones"] != nil {
                    sendResponse(connection, ["ok": false,
                        "error": "specify either 'phone' or 'phones', not both"])
                    break
                }
                if cmdDict["email"] != nil && cmdDict["emails"] != nil {
                    sendResponse(connection, ["ok": false,
                        "error": "specify either 'email' or 'emails', not both"])
                    break
                }
                guard let store = acquireContactStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no contacts permission for test runner"])
                    break
                }
                let contact = CNMutableContact()
                contact.givenName = given
                contact.familyName = family
                if !middle.isEmpty   { contact.middleName = middle }
                if !nickname.isEmpty { contact.nickname = nickname }
                if !phoneticGiven.isEmpty  { contact.phoneticGivenName  = phoneticGiven  }
                if !phoneticFamily.isEmpty { contact.phoneticFamilyName = phoneticFamily }
                if !phoneticMiddle.isEmpty { contact.phoneticMiddleName = phoneticMiddle }

                // Phones: prefer multi-value array, fall back to single
                if let ps = phones {
                    var entries: [CNLabeledValue<CNPhoneNumber>] = []
                    for entry in ps {
                        let v = entry["value"] ?? ""
                        if v.isEmpty { continue }
                        entries.append(CNLabeledValue(
                            label: canonicalPhoneLabel(entry["label"]),
                            value: CNPhoneNumber(stringValue: v)))
                    }
                    if !entries.isEmpty { contact.phoneNumbers = entries }
                } else if let p = phone, !p.isEmpty {
                    contact.phoneNumbers = [
                        CNLabeledValue(label: CNLabelPhoneNumberMobile,
                                       value: CNPhoneNumber(stringValue: p))
                    ]
                }

                // Emails: prefer multi-value array, fall back to single
                if let es = emails {
                    var entries: [CNLabeledValue<NSString>] = []
                    for entry in es {
                        let v = entry["value"] ?? ""
                        if v.isEmpty { continue }
                        entries.append(CNLabeledValue(
                            label: canonicalEmailLabel(entry["label"]),
                            value: v as NSString))
                    }
                    if !entries.isEmpty { contact.emailAddresses = entries }
                } else if let e = email, !e.isEmpty {
                    contact.emailAddresses = [
                        CNLabeledValue(label: CNLabelHome,
                                       value: e as NSString)
                    ]
                }

                // Postal addresses
                if let addrs = addresses {
                    var entries: [CNLabeledValue<CNPostalAddress>] = []
                    for entry in addrs {
                        let pa = CNMutablePostalAddress()
                        pa.street     = entry["street"]      ?? ""
                        pa.city       = entry["city"]        ?? ""
                        pa.state      = entry["state"]       ?? ""
                        pa.postalCode = entry["postal_code"] ?? ""
                        pa.country    = entry["country"]     ?? ""
                        entries.append(CNLabeledValue(
                            label: canonicalAddressLabel(entry["label"]),
                            value: pa))
                    }
                    if !entries.isEmpty { contact.postalAddresses = entries }
                }

                // URLs
                if let us = urls {
                    var entries: [CNLabeledValue<NSString>] = []
                    for entry in us {
                        let v = entry["value"] ?? ""
                        if v.isEmpty { continue }
                        entries.append(CNLabeledValue(
                            label: canonicalURLLabel(entry["label"]),
                            value: v as NSString))
                    }
                    if !entries.isEmpty { contact.urlAddresses = entries }
                }

                // Birthday (single NSDateComponents)
                if let bs = birthdayStr, !bs.isEmpty {
                    if let dc = parseContactDateComponents(bs) {
                        contact.birthday = dc as DateComponents
                    }
                }

                // Dates (multi-value)
                if let ds = dates {
                    var entries: [CNLabeledValue<NSDateComponents>] = []
                    for entry in ds {
                        let iso = entry["iso"] ?? ""
                        if let dc = parseContactDateComponents(iso) {
                            entries.append(CNLabeledValue(
                                label: canonicalDateLabel(entry["label"]),
                                value: dc))
                        }
                    }
                    if !entries.isEmpty { contact.dates = entries }
                }

                if let o = org, !o.isEmpty       { contact.organizationName = o }
                if let j = jobTitle, !j.isEmpty  { contact.jobTitle = j }
                if let d = department, !d.isEmpty { contact.departmentName = d }

                let saveReq = CNSaveRequest()
                saveReq.add(contact, toContainerWithIdentifier: nil)
                do {
                    try store.execute(saveReq)
                    dismissPermissionDialogs()
                    sendResponse(connection, ["ok": true,
                        "given_name": given, "family_name": family,
                        "identifier": contact.identifier])
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "save failed: \(error.localizedDescription)"])
                }

            case "list_contacts":
                // Returns every contact with the full schema the create
                // path supports. Optional `name_filter` matches
                // case-insensitively against either name component or
                // their concatenation.
                //
                // Legacy fields `phone` (first mobile-or-any) and `email`
                // (first home-or-any) are retained for backward compat
                // with existing generators. New fields are:
                //   middle_name, nickname, phonetic_*
                //   job_title, department
                //   phones:  [{label, value}]
                //   emails:  [{label, value}]
                //   postal_addresses: [{label, street, city, state, postal_code, country}]
                //   urls:    [{label, value}]
                //   dates:   [{label, iso}]
                //   birthday: "YYYY-MM-DD" or "--MM-DD" or "" (absent)
                let nameFilter = (cmdDict["name_filter"] as? String)?
                    .lowercased()
                guard let store = acquireContactStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no contacts permission"])
                    break
                }
                let keys: [CNKeyDescriptor] = [
                    CNContactGivenNameKey            as CNKeyDescriptor,
                    CNContactFamilyNameKey           as CNKeyDescriptor,
                    CNContactMiddleNameKey           as CNKeyDescriptor,
                    CNContactNicknameKey             as CNKeyDescriptor,
                    CNContactPhoneticGivenNameKey    as CNKeyDescriptor,
                    CNContactPhoneticFamilyNameKey   as CNKeyDescriptor,
                    CNContactPhoneticMiddleNameKey   as CNKeyDescriptor,
                    CNContactPhoneNumbersKey         as CNKeyDescriptor,
                    CNContactEmailAddressesKey       as CNKeyDescriptor,
                    CNContactPostalAddressesKey      as CNKeyDescriptor,
                    CNContactUrlAddressesKey         as CNKeyDescriptor,
                    CNContactDatesKey                as CNKeyDescriptor,
                    CNContactBirthdayKey             as CNKeyDescriptor,
                    CNContactOrganizationNameKey     as CNKeyDescriptor,
                    CNContactJobTitleKey             as CNKeyDescriptor,
                    CNContactDepartmentNameKey       as CNKeyDescriptor,
                    CNContactIdentifierKey           as CNKeyDescriptor,
                ]
                var rows: [[String: Any]] = []
                let request = CNContactFetchRequest(keysToFetch: keys)
                do {
                    try store.enumerateContacts(with: request) { c, _ in
                        if let nf = nameFilter {
                            let full = "\(c.givenName) \(c.familyName)"
                                .lowercased()
                            if !full.contains(nf)
                                && !c.givenName.lowercased().contains(nf)
                                && !c.familyName.lowercased().contains(nf) {
                                return
                            }
                        }
                        let phoneEntries: [[String: String]] = c.phoneNumbers.map {
                            ["label": friendlyLabel($0.label),
                             "value": $0.value.stringValue]
                        }
                        let emailEntries: [[String: String]] = c.emailAddresses.map {
                            ["label": friendlyLabel($0.label),
                             "value": $0.value as String]
                        }
                        let addrEntries: [[String: String]] = c.postalAddresses.map {
                            let pa = $0.value
                            return ["label":       friendlyLabel($0.label),
                                    "street":      pa.street,
                                    "city":        pa.city,
                                    "state":       pa.state,
                                    "postal_code": pa.postalCode,
                                    "country":     pa.country]
                        }
                        let urlEntries: [[String: String]] = c.urlAddresses.map {
                            ["label": friendlyLabel($0.label),
                             "value": $0.value as String]
                        }
                        let dateEntries: [[String: String]] = c.dates.map {
                            ["label": friendlyLabel($0.label),
                             "iso":   serializeContactDateComponents($0.value)]
                        }
                        let birthdayStr: String = {
                            if let b = c.birthday {
                                return serializeContactDateComponents(b as NSDateComponents)
                            }
                            return ""
                        }()
                        // Sparse row emission: scalar fields that are
                        // empty are OMITTED rather than emitted as "".
                        // Mirrors list_reminders' optional-key convention.
                        // Multi-value arrays stay (empty list is
                        // semantically "has zero phones / emails / etc.").
                        // The "phone" / "email" legacy single keys are
                        // ALSO sparse — verifier code that exact-match
                        // checks must use `field=None` for "no phone".
                        var row: [String: Any] = [
                            "given_name":  c.givenName,
                            "family_name": c.familyName,
                            "identifier":  c.identifier,
                            "phones":      phoneEntries,
                            "emails":      emailEntries,
                            "postal_addresses": addrEntries,
                            "urls":        urlEntries,
                            "dates":       dateEntries,
                        ]
                        if !c.middleName.isEmpty   { row["middle_name"] = c.middleName }
                        if !c.nickname.isEmpty     { row["nickname"] = c.nickname }
                        if !c.phoneticGivenName.isEmpty  { row["phonetic_given_name"]  = c.phoneticGivenName }
                        if !c.phoneticFamilyName.isEmpty { row["phonetic_family_name"] = c.phoneticFamilyName }
                        if !c.phoneticMiddleName.isEmpty { row["phonetic_middle_name"] = c.phoneticMiddleName }
                        if !c.organizationName.isEmpty   { row["organization"] = c.organizationName }
                        if !c.jobTitle.isEmpty           { row["job_title"]    = c.jobTitle }
                        if !c.departmentName.isEmpty    { row["department"]   = c.departmentName }
                        if !birthdayStr.isEmpty          { row["birthday"]     = birthdayStr }
                        if let pv = c.phoneNumbers.first?.value.stringValue,
                                  !pv.isEmpty            { row["phone"] = pv }
                        if let ev = c.emailAddresses.first?.value as String?,
                                  !ev.isEmpty            { row["email"] = ev }
                        rows.append(row)
                    }
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "enumerate failed: \(error.localizedDescription)"])
                    break
                }
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true, "contacts": rows])

            case "update_contact":
                // Required: identifier (string) — the CN identifier of an
                // existing contact. Accepts the same optional field set
                // as create_contact; any field NOT specified is left
                // unchanged.
                //
                // Clear-vs-omit semantics:
                //   - SCALAR fields (given_name, middle_name, organization,
                //     job_title, etc.): present key with "" CLEARS the
                //     value (CN allows empty strings). Omit to leave
                //     unchanged.
                //   - BIRTHDAY: present key with "" CLEARS (contact.birthday
                //     = nil). Omit to leave unchanged.
                //   - MULTI-VALUE arrays (phones, emails, postal_addresses,
                //     urls, dates): present key with `[]` CLEARS. Present
                //     with non-empty array REPLACES the entire array.
                //     Omit to leave unchanged.
                //   - JSON `null` for any field is REJECTED (ambiguous).
                //
                // Legacy single-value `phone` / `email` strings are honored
                // for backward compat (mirrors create_contact); they
                // REPLACE the array with a single mobile/home entry.
                // Pass either `phone` OR `phones`, not both.
                let ident = cmdDict["identifier"] as? String ?? ""
                guard !ident.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "identifier required"])
                    break
                }
                // Reject null for any field — silent-omit semantics for
                // null would be a foot-gun.
                let multiFields = ["phones", "emails", "postal_addresses",
                                    "urls", "dates"]
                var nullField: String? = nil
                for f in multiFields {
                    if cmdDict[f] is NSNull { nullField = f; break }
                }
                if let f = nullField {
                    sendResponse(connection, ["ok": false,
                        "error": "\(f): null not accepted; use [] to clear or omit to leave unchanged"])
                    break
                }
                if cmdDict["phone"] != nil && cmdDict["phones"] != nil {
                    sendResponse(connection, ["ok": false,
                        "error": "specify either 'phone' or 'phones', not both"])
                    break
                }
                if cmdDict["email"] != nil && cmdDict["emails"] != nil {
                    sendResponse(connection, ["ok": false,
                        "error": "specify either 'email' or 'emails', not both"])
                    break
                }
                guard let store = acquireContactStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no contacts permission"])
                    break
                }
                let keys: [CNKeyDescriptor] = [
                    CNContactGivenNameKey            as CNKeyDescriptor,
                    CNContactFamilyNameKey           as CNKeyDescriptor,
                    CNContactMiddleNameKey           as CNKeyDescriptor,
                    CNContactNicknameKey             as CNKeyDescriptor,
                    CNContactPhoneticGivenNameKey    as CNKeyDescriptor,
                    CNContactPhoneticFamilyNameKey   as CNKeyDescriptor,
                    CNContactPhoneticMiddleNameKey   as CNKeyDescriptor,
                    CNContactPhoneNumbersKey         as CNKeyDescriptor,
                    CNContactEmailAddressesKey       as CNKeyDescriptor,
                    CNContactPostalAddressesKey      as CNKeyDescriptor,
                    CNContactUrlAddressesKey         as CNKeyDescriptor,
                    CNContactDatesKey                as CNKeyDescriptor,
                    CNContactBirthdayKey             as CNKeyDescriptor,
                    CNContactOrganizationNameKey     as CNKeyDescriptor,
                    CNContactJobTitleKey             as CNKeyDescriptor,
                    CNContactDepartmentNameKey       as CNKeyDescriptor,
                ]
                var found: CNMutableContact? = nil
                do {
                    let existing = try store.unifiedContact(
                        withIdentifier: ident, keysToFetch: keys)
                    found = existing.mutableCopy() as? CNMutableContact
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "contact not found: \(error.localizedDescription)"])
                    break
                }
                guard let contact = found else {
                    sendResponse(connection, ["ok": false,
                        "error": "contact not mutable"])
                    break
                }
                if let v = cmdDict["given_name"]    as? String { contact.givenName = v }
                if let v = cmdDict["family_name"]   as? String { contact.familyName = v }
                if let v = cmdDict["middle_name"]   as? String { contact.middleName = v }
                if let v = cmdDict["nickname"]      as? String { contact.nickname = v }
                if let v = cmdDict["phonetic_given_name"]  as? String { contact.phoneticGivenName  = v }
                if let v = cmdDict["phonetic_family_name"] as? String { contact.phoneticFamilyName = v }
                if let v = cmdDict["phonetic_middle_name"] as? String { contact.phoneticMiddleName = v }
                if let v = cmdDict["organization"]  as? String { contact.organizationName = v }
                if let v = cmdDict["job_title"]     as? String { contact.jobTitle = v }
                if let v = cmdDict["department"]    as? String { contact.departmentName = v }

                if let ps = cmdDict["phones"] as? [[String: String]] {
                    var entries: [CNLabeledValue<CNPhoneNumber>] = []
                    for entry in ps {
                        let v = entry["value"] ?? ""
                        if v.isEmpty { continue }
                        entries.append(CNLabeledValue(
                            label: canonicalPhoneLabel(entry["label"]),
                            value: CNPhoneNumber(stringValue: v)))
                    }
                    contact.phoneNumbers = entries
                } else if let p = cmdDict["phone"] as? String {
                    // Legacy single-phone path: empty string clears the
                    // array; non-empty replaces with a single mobile entry.
                    if p.isEmpty {
                        contact.phoneNumbers = []
                    } else {
                        contact.phoneNumbers = [CNLabeledValue(
                            label: CNLabelPhoneNumberMobile,
                            value: CNPhoneNumber(stringValue: p))]
                    }
                }
                if let es = cmdDict["emails"] as? [[String: String]] {
                    var entries: [CNLabeledValue<NSString>] = []
                    for entry in es {
                        let v = entry["value"] ?? ""
                        if v.isEmpty { continue }
                        entries.append(CNLabeledValue(
                            label: canonicalEmailLabel(entry["label"]),
                            value: v as NSString))
                    }
                    contact.emailAddresses = entries
                } else if let e = cmdDict["email"] as? String {
                    if e.isEmpty {
                        contact.emailAddresses = []
                    } else {
                        contact.emailAddresses = [CNLabeledValue(
                            label: CNLabelHome,
                            value: e as NSString)]
                    }
                }
                if let addrs = cmdDict["postal_addresses"] as? [[String: String]] {
                    var entries: [CNLabeledValue<CNPostalAddress>] = []
                    for entry in addrs {
                        let pa = CNMutablePostalAddress()
                        pa.street     = entry["street"]      ?? ""
                        pa.city       = entry["city"]        ?? ""
                        pa.state      = entry["state"]       ?? ""
                        pa.postalCode = entry["postal_code"] ?? ""
                        pa.country    = entry["country"]     ?? ""
                        entries.append(CNLabeledValue(
                            label: canonicalAddressLabel(entry["label"]),
                            value: pa))
                    }
                    contact.postalAddresses = entries
                }
                if let us = cmdDict["urls"] as? [[String: String]] {
                    var entries: [CNLabeledValue<NSString>] = []
                    for entry in us {
                        let v = entry["value"] ?? ""
                        if v.isEmpty { continue }
                        entries.append(CNLabeledValue(
                            label: canonicalURLLabel(entry["label"]),
                            value: v as NSString))
                    }
                    contact.urlAddresses = entries
                }
                if let ds = cmdDict["dates"] as? [[String: String]] {
                    var entries: [CNLabeledValue<NSDateComponents>] = []
                    for entry in ds {
                        let iso = entry["iso"] ?? ""
                        if let dc = parseContactDateComponents(iso) {
                            entries.append(CNLabeledValue(
                                label: canonicalDateLabel(entry["label"]),
                                value: dc))
                        }
                    }
                    contact.dates = entries
                }
                if let bs = cmdDict["birthday"] as? String {
                    if bs.isEmpty {
                        contact.birthday = nil
                    } else if let dc = parseContactDateComponents(bs) {
                        contact.birthday = dc as DateComponents
                    }
                }
                let saveReq = CNSaveRequest()
                saveReq.update(contact)
                do {
                    try store.execute(saveReq)
                    dismissPermissionDialogs()
                    sendResponse(connection, ["ok": true,
                        "identifier": contact.identifier])
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "save failed: \(error.localizedDescription)"])
                }

            case "wipe_contacts":
                // Reset: delete every contact in the default container.
                // The container itself cannot be deleted; only its
                // contents. CN doesn't expose isImmutable like EK, so
                // we just try delete on each and tolerate any errors
                // from system-owned entries.
                guard let store = acquireContactStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no contacts permission"])
                    break
                }
                let keys: [CNKeyDescriptor] = [
                    CNContactIdentifierKey as CNKeyDescriptor,
                ]
                let saveReq = CNSaveRequest()
                var removed = 0
                let request = CNContactFetchRequest(keysToFetch: keys)
                do {
                    try store.enumerateContacts(with: request) { c, _ in
                        // enumerateContacts yields immutable CNContact;
                        // delete needs a CNMutableContact reference.
                        if let m = c.mutableCopy() as? CNMutableContact {
                            saveReq.delete(m)
                            removed += 1
                        }
                    }
                    try store.execute(saveReq)
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "wipe failed: \(error.localizedDescription)"])
                    break
                }
                dismissPermissionDialogs()
                sendResponse(connection, ["ok": true,
                    "removed_contacts": removed])

            // ── Files commands (FileManager-backed) ─────────────────────
            //
            // All paths are relative to the SIBBWorkspace directory
            // inside the runner sandbox. Absolute paths and `..` traversal
            // are rejected (see sibbWorkspaceURL). No permission grant
            // needed — FileManager works inside any process.

            case "create_file":
                // Required: path (relative, no `..`). Optional: content
                // (string, default ""), encoding ("utf-8" default,
                // "base64" decodes content as base64 before write).
                let path = cmdDict["path"] as? String ?? ""
                let content = cmdDict["content"] as? String ?? ""
                let encoding = cmdDict["encoding"] as? String ?? "utf-8"
                guard !path.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "path required"])
                    break
                }
                guard let url = sibbWorkspaceURL(path) else {
                    sendResponse(connection, ["ok": false,
                        "error": "path must be relative and not contain .."])
                    break
                }
                // Ensure parent dir exists — `Documents/SIBBWorkspace/`
                // is created by sibbWorkspaceRoot(), but nested dirs
                // (e.g. "notes/work/today.txt") need explicit creation.
                let parent = url.deletingLastPathComponent()
                try? FileManager.default.createDirectory(
                    at: parent, withIntermediateDirectories: true)
                let data: Data?
                switch encoding {
                case "base64":
                    data = Data(base64Encoded: content)
                default:
                    data = content.data(using: .utf8)
                }
                guard let bytes = data else {
                    sendResponse(connection, ["ok": false,
                        "error": "content decode failed (encoding=\(encoding))"])
                    break
                }
                do {
                    try bytes.write(to: url, options: .atomic)
                    sendResponse(connection, ["ok": true,
                        "path": path, "size": bytes.count])
                } catch {
                    sendResponse(connection, ["ok": false,
                        "error": "write failed: \(error.localizedDescription)"])
                }

            case "list_files":
                // Returns every regular file under `directory` (default
                // workspace root) with its relative path + size. Set
                // recursive=false to return only direct children.
                let dir = cmdDict["directory"] as? String ?? ""
                let recursive = cmdDict["recursive"] as? Bool ?? true
                let base: URL
                if dir.isEmpty {
                    base = sibbWorkspaceRoot()
                } else {
                    guard let resolved = sibbWorkspaceURL(dir) else {
                        sendResponse(connection, ["ok": false,
                            "error": "directory must be relative and not contain .."])
                        break
                    }
                    base = resolved
                }
                guard FileManager.default.fileExists(atPath: base.path) else {
                    // Empty dir is not an error — the workspace may
                    // genuinely have no files yet. Return [].
                    sendResponse(connection, ["ok": true, "files": []])
                    break
                }
                var rows: [[String: Any]] = []
                let root = sibbWorkspaceRoot().path
                let walker: (URL) -> Void = { url in
                    guard let vals = try? url.resourceValues(forKeys: [
                        .isRegularFileKey, .isDirectoryKey, .fileSizeKey]) else { return }
                    let rel = url.path.hasPrefix(root)
                        ? String(url.path.dropFirst(root.count)
                                     .drop { $0 == "/" })
                        : url.path
                    if vals.isRegularFile == true {
                        rows.append([
                            "path": rel,
                            "type": "file",
                            "size": vals.fileSize ?? 0,
                        ])
                    } else if vals.isDirectory == true {
                        rows.append([
                            "path": rel,
                            "type": "dir",
                            "size": 0,
                        ])
                    }
                }
                if recursive {
                    let keys: [URLResourceKey] = [
                        .isRegularFileKey, .isDirectoryKey, .fileSizeKey]
                    if let enumerator = FileManager.default.enumerator(
                            at: base, includingPropertiesForKeys: keys,
                            options: [], errorHandler: nil) {
                        for case let entry as URL in enumerator {
                            walker(entry)
                        }
                    }
                } else {
                    if let children = try? FileManager.default
                            .contentsOfDirectory(at: base,
                                                  includingPropertiesForKeys: nil) {
                        for child in children { walker(child) }
                    }
                }
                sendResponse(connection, ["ok": true, "files": rows])

            case "read_file":
                // Used by verifiers to assert on file content. Returns
                // utf-8 string by default; pass encoding="base64" for
                // binary safety.
                let path = cmdDict["path"] as? String ?? ""
                let encoding = cmdDict["encoding"] as? String ?? "utf-8"
                guard !path.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "path required"])
                    break
                }
                guard let url = sibbWorkspaceURL(path) else {
                    sendResponse(connection, ["ok": false,
                        "error": "path must be relative and not contain .."])
                    break
                }
                guard let data = try? Data(contentsOf: url) else {
                    sendResponse(connection, ["ok": false,
                        "error": "not found"])
                    break
                }
                let content: String
                switch encoding {
                case "base64":
                    content = data.base64EncodedString()
                default:
                    content = String(data: data, encoding: .utf8) ?? ""
                }
                sendResponse(connection, ["ok": true,
                    "path": path, "size": data.count, "content": content])

            case "wipe_files":
                // Remove every file and directory under the workspace,
                // then recreate the empty workspace dir. Idempotent —
                // first call on a fresh runner is a no-op since the
                // workspace doesn't exist yet.
                let root = sibbWorkspaceRoot()
                var removed = 0
                if let children = try? FileManager.default.contentsOfDirectory(
                        at: root, includingPropertiesForKeys: nil) {
                    for child in children {
                        if (try? FileManager.default.removeItem(at: child)) != nil {
                            removed += 1
                        }
                    }
                }
                sendResponse(connection, ["ok": true, "removed": removed])

            // ── Photos (PHPhotoLibrary / PHAsset) commands ──────────────
            //
            // Asymmetric handler: apply happens host-side via
            // `simctl addmedia` (Python shells out — no Swift needed
            // because the on-device path requires a host image file).
            // Reset + list are runner-side because PhotoKit is the only
            // way to enumerate or delete assets programmatically.

            case "list_photos":
                guard ensurePhotoLibraryAccess() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no photos permission for test runner"])
                    break
                }
                let opts = PHFetchOptions()
                // Stable ordering for verifier diff — creation date
                // ascending. Same field is exposed in row output.
                opts.sortDescriptors = [
                    NSSortDescriptor(key: "creationDate", ascending: true)]
                let assets = PHAsset.fetchAssets(with: opts)
                var rows: [[String: Any]] = []
                assets.enumerateObjects { asset, _, _ in
                    let mediaType: String
                    switch asset.mediaType {
                    case .image: mediaType = "image"
                    case .video: mediaType = "video"
                    case .audio: mediaType = "audio"
                    default:     mediaType = "unknown"
                    }
                    let iso: String
                    if let date = asset.creationDate {
                        let fmt = ISO8601DateFormatter()
                        iso = fmt.string(from: date)
                    } else {
                        iso = ""
                    }
                    rows.append([
                        "identifier":     asset.localIdentifier,
                        "media_type":     mediaType,
                        "pixel_width":    asset.pixelWidth,
                        "pixel_height":   asset.pixelHeight,
                        "duration":       asset.duration,
                        "creation_date":  iso,
                        "is_favorite":    asset.isFavorite,
                        "is_hidden":      asset.isHidden,
                    ])
                }
                sendResponse(connection, ["ok": true, "photos": rows])

            case "wipe_photos":
                // Remove every PHAsset accessible via the runner's
                // photo-library permission. iOS 14+ shows a system
                // confirmation dialog on deletion even with TCC=Allowed
                // — we tap through it via SpringBoard's "Delete" button.
                guard ensurePhotoLibraryAccess() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no photos permission"])
                    break
                }
                let assets = PHAsset.fetchAssets(with: nil)
                var asArray: [PHAsset] = []
                assets.enumerateObjects { a, _, _ in asArray.append(a) }
                if asArray.isEmpty {
                    sendResponse(connection, ["ok": true,
                        "removed_photos": 0])
                    break
                }
                // Background-thread the delete + a polling SpringBoard
                // alert tap, so we can wait on the completion semaphore
                // without deadlocking.
                let sem = DispatchSemaphore(value: 0)
                var deleteOk = false
                var deleteErr: String?
                PHPhotoLibrary.shared().performChanges({
                    PHAssetChangeRequest.deleteAssets(
                        asArray as NSFastEnumeration)
                }, completionHandler: { success, error in
                    deleteOk = success
                    if let e = error {
                        deleteErr = e.localizedDescription
                    }
                    sem.signal()
                })
                // iOS 17+ shows a "Delete N items?" system alert on
                // PHAssetChangeRequest.deleteAssets even with TCC
                // granted. The alert lives on SpringBoard. We poll for
                // up to 10s looking for a button whose label starts
                // with "Delete" inside any alert (label text varies:
                // "Delete Photo", "Delete Photos", "Delete N Items").
                let sb = appFor("com.apple.springboard")
                let pollDeadline = Date().addingTimeInterval(10.0)
                var tapped = false
                while !tapped && Date() < pollDeadline {
                    Thread.sleep(forTimeInterval: 0.3)
                    let alert = sb.alerts.firstMatch
                    if alert.exists {
                        // Match by prefix because iOS rewrites the
                        // label with the count: "Delete 3 Items" etc.
                        let pred = NSPredicate(
                            format: "label BEGINSWITH 'Delete'")
                        let btn = alert.buttons.matching(pred).firstMatch
                        if btn.exists {
                            btn.tap()
                            tapped = true
                        }
                    }
                }
                _ = sem.wait(timeout: .now() + 20.0)
                if deleteOk {
                    sendResponse(connection, ["ok": true,
                        "removed_photos": asArray.count])
                } else {
                    sendResponse(connection, ["ok": false,
                        "error": "delete failed: \(deleteErr ?? "unknown")"])
                }

            // ── Health (HealthKit / HKHealthStore) commands ────────────
            //
            // SIBB-v1 supports the quantity sample types declared in
            // HEALTH_QUANTITY_TYPES. Add new types there (not here)
            // — the cases below dispatch by table lookup.
            //
            // Wipe semantics: HealthKit only permits deletion of
            // samples the calling app wrote. wipe_health_samples
            // scopes the predicate to HKSource of the test runner,
            // so this resets between-episode state without touching
            // any pre-seeded data we might add later.

            case "create_health_sample":
                let typeName  = cmdDict["sample_type"] as? String ?? ""
                let valueRaw  = cmdDict["value"]
                let startISO  = cmdDict["start_iso"] as? String ?? ""
                let endISO    = cmdDict["end_iso"]   as? String ?? startISO
                guard let typeEntry = HEALTH_QUANTITY_TYPES[typeName] else {
                    sendResponse(connection, ["ok": false,
                        "error": "unknown sample_type \(typeName); " +
                                 "valid: \(HEALTH_QUANTITY_TYPES.keys.sorted())"])
                    break
                }
                let value: Double
                if let d = valueRaw as? Double { value = d }
                else if let i = valueRaw as? Int { value = Double(i) }
                else {
                    sendResponse(connection, ["ok": false,
                        "error": "value (Double or Int) required"])
                    break
                }
                guard !startISO.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "start_iso required"])
                    break
                }
                guard let store = acquireHealthStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no health permission for test runner"])
                    break
                }
                let fmt = ISO8601DateFormatter()
                guard let start = fmt.date(from: startISO),
                      let end = fmt.date(from: endISO) else {
                    sendResponse(connection, ["ok": false,
                        "error": "ISO8601 parse failed for start/end"])
                    break
                }
                let qtype = HKObjectType.quantityType(
                    forIdentifier: typeEntry.0)!
                let unit = HKUnit(from: typeEntry.1)
                let q = HKQuantity(unit: unit, doubleValue: value)
                let sample = HKQuantitySample(
                    type: qtype, quantity: q,
                    start: start, end: end)
                let sem = DispatchSemaphore(value: 0)
                var saveOk = false
                var saveErr: String?
                store.save(sample) { ok, error in
                    saveOk = ok
                    if let e = error {
                        saveErr = e.localizedDescription
                    }
                    sem.signal()
                }
                _ = sem.wait(timeout: .now() + 10.0)
                if saveOk {
                    sendResponse(connection, ["ok": true,
                        "sample_type": typeName,
                        "value": value,
                        "unit": typeEntry.1,
                        "identifier": sample.uuid.uuidString])
                } else {
                    sendResponse(connection, ["ok": false,
                        "error": "save failed: \(saveErr ?? "unknown")"])
                }

            case "list_health_samples":
                // Optional `sample_type` narrows to one type; without
                // it, the runner enumerates every type SIBB knows
                // about and concatenates the results. Optional
                // `start_iso` / `end_iso` window the predicate.
                let typeFilter = cmdDict["sample_type"] as? String
                let startISO = cmdDict["start_iso"] as? String
                let endISO   = cmdDict["end_iso"]   as? String
                guard let store = acquireHealthStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no health permission"])
                    break
                }
                let fmt = ISO8601DateFormatter()
                var pred: NSPredicate?
                if let s = startISO, let e = endISO,
                   let sd = fmt.date(from: s), let ed = fmt.date(from: e) {
                    pred = HKQuery.predicateForSamples(
                        withStart: sd, end: ed, options: [])
                }
                var typesToQuery: [(String, HKQuantityTypeIdentifier, String)] = []
                if let tn = typeFilter {
                    if let entry = HEALTH_QUANTITY_TYPES[tn] {
                        typesToQuery = [(tn, entry.0, entry.1)]
                    } else {
                        sendResponse(connection, ["ok": false,
                            "error": "unknown sample_type \(tn)"])
                        break
                    }
                } else {
                    for (name, entry) in HEALTH_QUANTITY_TYPES {
                        typesToQuery.append((name, entry.0, entry.1))
                    }
                }
                var rows: [[String: Any]] = []
                for (name, id, unitStr) in typesToQuery {
                    guard let qtype = HKObjectType.quantityType(
                            forIdentifier: id) else { continue }
                    let unit = HKUnit(from: unitStr)
                    let sem = DispatchSemaphore(value: 0)
                    let q = HKSampleQuery(
                        sampleType: qtype,
                        predicate: pred,
                        limit: HKObjectQueryNoLimit,
                        sortDescriptors: [NSSortDescriptor(
                            key: HKSampleSortIdentifierStartDate,
                            ascending: true)]
                    ) { _, samples, _ in
                        if let qs = samples as? [HKQuantitySample] {
                            for s in qs {
                                rows.append([
                                    "sample_type": name,
                                    "value":       s.quantity.doubleValue(for: unit),
                                    "unit":        unitStr,
                                    "start_iso":   fmt.string(from: s.startDate),
                                    "end_iso":     fmt.string(from: s.endDate),
                                    "identifier":  s.uuid.uuidString,
                                    "source":      s.sourceRevision.source.bundleIdentifier,
                                ])
                            }
                        }
                        sem.signal()
                    }
                    store.execute(q)
                    _ = sem.wait(timeout: .now() + 5.0)
                }
                sendResponse(connection, ["ok": true, "samples": rows])

            case "wipe_health_samples":
                // Delete only samples this runner authored. HealthKit
                // refuses cross-app deletion (security feature), which
                // happens to be exactly the reset semantics SIBB wants:
                // per-episode runner state goes away, anything seeded
                // by other means stays.
                guard let store = acquireHealthStore() else {
                    sendResponse(connection, ["ok": false,
                        "error": "no health permission"])
                    break
                }
                let bundleID = Bundle.main.bundleIdentifier ?? ""
                var removed = 0
                var failed = 0
                for (_, entry) in HEALTH_QUANTITY_TYPES {
                    guard let qtype = HKObjectType.quantityType(
                            forIdentifier: entry.0) else { continue }
                    // Scope deletion to samples with our bundle as source.
                    let pred = HKQuery.predicateForObjects(
                        from: HKSource.default())
                    _ = bundleID  // captured for future filter refinement
                    let sem = DispatchSemaphore(value: 0)
                    store.deleteObjects(of: qtype, predicate: pred) {
                            ok, count, error in
                        if ok {
                            removed += count
                        } else {
                            failed += 1
                        }
                        sem.signal()
                    }
                    _ = sem.wait(timeout: .now() + 5.0)
                }
                if failed > 0 {
                    sendResponse(connection, ["ok": false,
                        "error": "wipe partial: \(removed) deleted, " +
                                 "\(failed) type(s) failed"])
                } else {
                    sendResponse(connection, ["ok": true,
                        "removed_samples": removed])
                }

            case "states":
                // Diagnostic: state + frame per candidate.
                // `app.windows.count` blocks on AX queries for non-running
                // apps, so we use only `app.frame` which is a fast snapshot
                // of the cached AX state for the app handle.
                var rows: [[String: Any]] = []
                for bid in fgCandidates {
                    let app = appFor(bid)
                    let st: String
                    switch app.state {
                    case .unknown:                 st = "unknown"
                    case .notRunning:              st = "notRunning"
                    case .runningBackgroundSuspended: st = "bgSuspended"
                    case .runningBackground:       st = "background"
                    case .runningForeground:       st = "FOREGROUND"
                    @unknown default:              st = "??"
                    }
                    let f = app.frame
                    rows.append([
                        "bid": bid, "state": st,
                        "x": Double(f.origin.x), "y": Double(f.origin.y),
                        "w": Double(f.width),    "h": Double(f.height),
                    ])
                }
                sendResponse(connection, ["ok": true,
                                          "candidates_count": fgCandidates.count,
                                          "states": rows])

            case "launch", "attach":
                let bundleId = cmdDict["bundleId"] as? String ?? "com.apple.reminders"
                currentApp = XCUIApplication(bundleIdentifier: bundleId)
                currentBID = bundleId
                if bundleId == "com.apple.springboard" {
                    // Springboard is always running. Forcing .activate()
                    // can land us on an interrupted state — e.g. iOS
                    // restoring the last-foregrounded app on boot. Press
                    // home instead to guarantee the home screen.
                    XCUIDevice.shared.press(.home)
                    Thread.sleep(forTimeInterval: 0.5)
                    waitForSettle(currentApp!)
                } else {
                    // The XCUITest runner takes foreground at test launch;
                    // the target app needs to be brought forward whether
                    // it was already running (attach) or not (launch).
                    currentApp!.activate()
                    _ = currentApp!.wait(for: .runningForeground, timeout: 10)
                    // Use the stronger launch-aware settle. iOS Contacts
                    // and several other apps fire `runningForeground`
                    // well BEFORE the AX tree is fully populated, so an
                    // immediate observe gets the splash-screen state.
                    waitForLaunchSettle(currentApp!)
                }
                sendResponse(connection, ["ok": true])

            case "observe":
                // Python passes the foreground bundle ID (resolved from the
                // private XCUIDevice.accessibilityInterface.activeApplications
                // PID via launchctl PID→bundle mapping). We just switch and
                // dump that app's tree.
                if let bid = cmdDict["bundleId"] as? String, !bid.isEmpty {
                    if bid != currentBID {
                        currentApp = appFor(bid)
                        currentBID = bid
                    }
                }
                guard let app = currentApp else {
                    sendResponse(connection, ["ok": false, "error": "no_app"])
                    break
                }
                let treeData = dumpTree(app)
                var resp: [String: Any] = [
                    "ok":               true,
                    "elements":         treeData["elements"] as! [[String: Any]],
                    "keyboard_visible": treeData["keyboard_visible"] as! Bool,
                    "screen_width":     treeData["screen_width"] as! Double,
                    "screen_height":    treeData["screen_height"] as! Double,
                    "method":           treeData["method"] as? String ?? "snapshot",
                    "bundle_id":        currentBID ?? "",
                ]
                // Forward keyboard_frame when dumpTree populated it.
                // Pre-2026-05-27 this was missing from the response,
                // leaving Python's kb-occlusion filter as dead code.
                if let kf = treeData["keyboard_frame"] {
                    resp["keyboard_frame"] = kf
                }
                sendResponse(connection, resp)

            case "tap":
                guard let app = currentApp else {
                    sendResponse(connection, ["ok": false, "error": "no_app"]); break
                }
                if let x = cmdDict["x"] as? Double, let y = cmdDict["y"] as? Double {
                    let coord = app.coordinate(withNormalizedOffset: .zero)
                        .withOffset(CGVector(dx: x, dy: y))
                    coord.tap()
                } else if let ref = cmdDict["ref"] as? String, !ref.isEmpty {
                    let el = app.descendants(matching: .any)[ref]
                    if el.exists { el.tap() }
                    else { sendResponse(connection, ["ok": false, "error": "not_found"]); break }
                }
                waitForSettle(app)
                sendResponse(connection, ["ok": true])

            case "type":
                guard let app = currentApp,
                      let text = cmdDict["text"] as? String else {
                    sendResponse(connection, ["ok": false, "error": "bad_args"]); break
                }
                app.typeText(text)
                waitForSettle(app, timeout: 0.5)
                sendResponse(connection, ["ok": true])

            case "tap_then_type":
                // Atomic tap-to-focus + typeText with focus verification.
                // Replaces Python's tap+sleep+poll+typeText pattern (which
                // races against iOS's responder chain).
                //
                // Strategy (Policy A — fail-fast):
                //   1. Tap at (x, y).
                //   2. Poll up to focus_timeout_ms (default 1500) in
                //      100ms increments, taking fresh app.snapshot() and
                //      checking if a focused element's frame contains
                //      (x, y).
                //   3. If focus acquired → app.typeText(text). Return
                //      ok=true with focus_acquired=true and the
                //      acquired_ms duration.
                //   4. If timeout → DON'T type. Return ok=false,
                //      error="focus_not_acquired", with the currently-
                //      focused element's frame (so the agent can see
                //      what's focused instead).
                //
                // Why fail-fast: the prior "type anyway" behavior leaked
                // keystrokes to the previously-focused field when iOS'
                // responder chain hadn't caught up. Silent leakage is
                // worse than a clean failure — the agent can recover
                // from a clear "couldn't tap that element" by scrolling,
                // dismissing kb, or picking a different target.
                guard let app = currentApp,
                      let x = cmdDict["x"] as? Double,
                      let y = cmdDict["y"] as? Double,
                      let text = cmdDict["text"] as? String else {
                    sendResponse(connection, ["ok": false,
                        "error": "tap_then_type requires `x`, `y`, `text`"])
                    break
                }
                let timeoutMs = (cmdDict["focus_timeout_ms"] as? Int) ?? 1500
                let coord = app.coordinate(withNormalizedOffset: .zero)
                    .withOffset(CGVector(dx: x, dy: y))
                coord.tap()
                let pollStart = Date()
                let pollInterval: TimeInterval = 0.10
                let deadline = Date(timeInterval: TimeInterval(timeoutMs) / 1000.0,
                                     since: pollStart)
                var focusAcquired = false
                var acquiredMs: Int = 0
                var focusedFrameOnTimeout: CGRect? = nil
                while Date() < deadline {
                    Thread.sleep(forTimeInterval: pollInterval)
                    guard let snap = try? app.snapshot() else { continue }
                    // Find any focused element whose frame contains (x, y).
                    var hit: CGRect? = nil
                    func walk(_ s: XCUIElementSnapshot) {
                        if hit != nil { return }
                        if snapshotFocused(s) {
                            let f = s.frame
                            if f.contains(CGPoint(x: x, y: y)) {
                                hit = f
                                return
                            }
                            // Even if frame doesn't contain (x, y),
                            // record it for the diagnostic — agent
                            // wants to know what IS focused.
                            focusedFrameOnTimeout = f
                        }
                        for child in s.children { walk(child) }
                    }
                    walk(snap)
                    if hit != nil {
                        focusAcquired = true
                        acquiredMs = Int(Date().timeIntervalSince(pollStart) * 1000)
                        break
                    }
                }
                if !focusAcquired {
                    var resp: [String: Any] = [
                        "ok": false,
                        "error": "focus_not_acquired",
                        "tap_coords": [x, y],
                        "polled_ms": timeoutMs,
                    ]
                    if let f = focusedFrameOnTimeout {
                        resp["focused_frame"] = [
                            "x": Double(f.origin.x), "y": Double(f.origin.y),
                            "width": Double(f.width), "height": Double(f.height),
                        ]
                    }
                    sendResponse(connection, resp)
                    break
                }
                app.typeText(text)
                waitForSettle(app, timeout: 0.5)
                sendResponse(connection, [
                    "ok": true,
                    "focus_acquired": true,
                    "acquired_ms": acquiredMs,
                    "typed": text,
                ])

            case "clear_text":
                // Clear a text field by coordinate. Strategy:
                //   1. Single-tap at (x, y) — focuses the field. On
                //      iOS, tapping a text field puts the cursor at
                //      the END of the existing content (or where the
                //      tap landed if the touch is inside the text).
                //   2. Send `length_hint` delete-key keystrokes — the
                //      Python side passes the current value's char
                //      count, so we delete exactly enough to wipe
                //      everything. Capped at 200 to bound the worst
                //      case; defaults to 100 if no hint provided.
                //
                // We previously tried triple-tap-select-all + delete,
                // but XCUICoordinate.tap() doesn't accept
                // withNumberOfTaps: — that's XCUIElement-only — and
                // the scaffold-assigned ref doesn't match iOS's
                // accessibility identifier, so coord-based bulk-delete
                // is the only reliable path.
                guard let app = currentApp else {
                    sendResponse(connection, ["ok": false, "error": "no_app"]); break
                }
                guard let x = cmdDict["x"] as? Double,
                      let y = cmdDict["y"] as? Double else {
                    sendResponse(connection, ["ok": false,
                        "error": "clear_text requires `x` and `y` (Double)"])
                    break
                }
                let coord = app.coordinate(withNormalizedOffset: .zero)
                    .withOffset(CGVector(dx: x, dy: y))
                coord.tap()
                Thread.sleep(forTimeInterval: 0.20)
                // iOS 17+ Simulator: sending more than ~25 backspaces
                // to an already-empty UITextField crashes the keyboard
                // service (Appium #20088). Cap the burst at 24, return
                // stopped_early=true so the caller can re-issue CLEAR
                // for longer fields.
                let HARD_CAP = 24
                let hint = cmdDict["length_hint"] as? Int
                let requested = max(hint ?? 24, 1)
                let n = min(requested, HARD_CAP)
                let stoppedEarly = (requested > HARD_CAP)
                app.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue,
                                     count: n))
                Thread.sleep(forTimeInterval: 0.10)
                waitForSettle(app, timeout: 0.5)
                sendResponse(connection, [
                    "ok": true,
                    "coords": [x, y],
                    "deletes_sent": n,
                    "stopped_early": stoppedEarly,
                ])

            case "swipe":
                // Whole-app directional swipe via explicit normalized
                // coordinates (NOT XCUIApplication.swipeUp/Down/Left/Right
                // convenience methods, whose start origins are undocumented
                // and empirically do NOT invoke iOS system gestures like
                // Spotlight or home-screen page navigation).
                //
                // Verified empirically 2026-05-22 with sibb_probe_swipe_origins.py
                // (iPhone 17 Pro, iOS 26.3.1):
                //   down  (0.5,0.50)→(0.5,0.85)  invokes Spotlight from home
                //   left  (0.85,0.50)→(0.15,0.50) advances to next home page
                //   right (0.15,0.50)→(0.85,0.50) goes back a page
                //   up    (0.5,0.50)→(0.5,0.15)  opposite of down
                // Horizontal page-flip works at any y (no widget interception).
                guard let app = currentApp else {
                    sendResponse(connection, ["ok": false, "error": "no_app"]); break
                }
                let dir = cmdDict["direction"] as? String ?? "up"
                let pts: (sx: Double, sy: Double, ex: Double, ey: Double)? = {
                    switch dir {
                    case "down":  return (0.5,  0.50, 0.5,  0.85)
                    case "up":    return (0.5,  0.50, 0.5,  0.15)
                    case "left":  return (0.85, 0.50, 0.15, 0.50)
                    case "right": return (0.15, 0.50, 0.85, 0.50)
                    default: return nil
                    }
                }()
                guard let p = pts else {
                    sendResponse(connection, ["ok": false,
                        "error": "bad direction: \(dir) (expected up|down|left|right)"])
                    break
                }
                let start = app.coordinate(withNormalizedOffset:
                    CGVector(dx: p.sx, dy: p.sy))
                let end = app.coordinate(withNormalizedOffset:
                    CGVector(dx: p.ex, dy: p.ey))
                start.press(forDuration: 0.05, thenDragTo: end)
                waitForSettle(app)
                sendResponse(connection, ["ok": true])

            case "swipe_at":
                // Element-targeted swipe between explicit coordinates.
                // Required: x1, y1, x2, y2 (absolute screen coordinates).
                // Optional: duration_s (defaults to 0.05 — XCUITest's
                // standard drag press-then-drag duration).
                //
                // Used by SCROLL/SWIPE actions with an element ref —
                // the Python caller computes start/end from the
                // element's frame so the gesture is bounded by the
                // element (carousels, picker wheels, nested scroll
                // views, map panning). Without swipe_at, the only
                // option was app.swipeUp/swipeDown/etc which gesture
                // across the whole app and miss intra-element scroll.
                guard let app = currentApp else {
                    sendResponse(connection, ["ok": false, "error": "no_app"])
                    break
                }
                guard let x1 = cmdDict["x1"] as? Double,
                      let y1 = cmdDict["y1"] as? Double,
                      let x2 = cmdDict["x2"] as? Double,
                      let y2 = cmdDict["y2"] as? Double else {
                    sendResponse(connection, ["ok": false,
                        "error": "x1, y1, x2, y2 (Double) required"])
                    break
                }
                let duration = cmdDict["duration_s"] as? Double ?? 0.05
                // `settle` defaults to true (preserves old single-swipe
                // semantics). Python sets it to false on all but the LAST
                // swipe in a batch so picker-wheel cascades don't pay
                // the 2s waitForSettle cost per swipe (which empirically
                // hangs for 100s+ on a spinning wheel because the
                // descendants count keeps changing during animation).
                let settle = cmdDict["settle"] as? Bool ?? true
                // velocity_pps controls actual drag speed (px/s) — when
                // set, we use the richer press(...withVelocity:...)
                // variant. Default behavior matches XCUITest's vanilla
                // `press(forDuration:thenDragTo:)` which is a fast drag
                // at ~1000 px/s — interpreted as a fling by iOS picker
                // wheels. For wheel control, callers pass velocity_pps
                // ≤300 to get a controlled drag that lands ~1 tick.
                let velocity = cmdDict["velocity_pps"] as? Double
                let start = app.coordinate(withNormalizedOffset: .zero)
                    .withOffset(CGVector(dx: x1, dy: y1))
                let end = app.coordinate(withNormalizedOffset: .zero)
                    .withOffset(CGVector(dx: x2, dy: y2))
                if let v = velocity {
                    start.press(forDuration: duration,
                                thenDragTo: end,
                                withVelocity: XCUIGestureVelocity(v),
                                thenHoldForDuration: 0.1)
                } else {
                    start.press(forDuration: duration, thenDragTo: end)
                }
                if settle { waitForSettle(app) }
                sendResponse(connection, ["ok": true,
                    "from": [x1, y1], "to": [x2, y2],
                    "duration_s": duration, "settled": settle,
                    "velocity_pps": velocity as Any])

            case "dismiss_app_onboarding":
                // Programmatically dismiss in-app onboarding/upgrade dialogs
                // by launching the target app and tapping any of a known
                // dismiss-label set repeatedly until the screen stabilizes.
                // Used during baseline build so that clones don't see
                // welcome/iCloud/etc prompts on first launch of every
                // SIBB-11 app.
                //
                // Distinct from dismissPermissionDialogs() which only
                // touches SpringBoard's TCC permission alerts. This one
                // walks the foreground app's own snapshot, so it covers
                // app-owned onboarding flows (Reminders' "Welcome to
                // Reminders", Reminders' "Enable iCloud Syncing?" upgrade
                // prompt, Calendar's "Add Account?", etc.) that the
                // device-level plist suppression keys can't reach.
                //
                // Conservative — only taps buttons whose labels match a
                // fixed allow-list. New apps with different labels (e.g.
                // "Got It", "Start") need their labels added here.
                guard let bundle = cmdDict["bundle"] as? String else {
                    sendResponse(connection,
                        ["ok": false, "error": "bundle required"])
                    break
                }
                let onboardingApp = XCUIApplication(bundleIdentifier: bundle)
                onboardingApp.activate()
                _ = onboardingApp.wait(for: .runningForeground, timeout: 10)
                let dismissLabels: Set<String> = [
                    "Continue", "Not Now", "Skip", "Done", "OK",
                    "Get Started", "Maybe Later", "Cancel",
                    "No Thanks", "Later", "Dismiss"
                ]
                var totalTapped = 0
                for _ in 0..<6 {
                    // Settle wait — Reminders' iCloud upgrade prompt only
                    // appears 0.5-1s after Welcome dismissal, not
                    // synchronously.
                    Thread.sleep(forTimeInterval: 0.7)
                    var snap: XCUIElementSnapshot? = nil
                    SIBBSafeRun(nil) { snap = try? onboardingApp.snapshot() }
                    guard let root = snap else { break }
                    var found: CGRect? = nil
                    func walk(_ s: XCUIElementSnapshot) {
                        if found != nil { return }
                        if s.elementType == .button
                            && dismissLabels.contains(s.label) {
                            found = s.frame
                            return
                        }
                        for c in s.children {
                            walk(c)
                            if found != nil { return }
                        }
                    }
                    walk(root)
                    guard let f = found else { break }
                    let coord = onboardingApp.coordinate(
                        withNormalizedOffset: .zero
                    ).withOffset(CGVector(dx: f.midX, dy: f.midY))
                    coord.tap()
                    totalTapped += 1
                }
                onboardingApp.terminate()
                sendResponse(connection, [
                    "ok": true,
                    "taps": totalTapped,
                    "bundle": bundle,
                ])

            case "press":
                // Hardware buttons / system gestures. Drives at the device or
                // app coordinate level — currentApp is only needed as a
                // coordinate anchor for the gesture cases.
                let button = cmdDict["button"] as? String ?? "home"
                let device = XCUIDevice.shared
                let anchorApp = currentApp ?? appFor("com.apple.springboard")
                let screen = anchorApp.frame
                var ok = true
                var errMsg: String? = nil
                SIBBSafeRun(nil) {
                    switch button {
                    case "home":
                        device.press(.home)
                    case "back":
                        // iOS interactive-pop gesture: drag from left edge rightward.
                        let y = screen.height / 2
                        let start = anchorApp.coordinate(withNormalizedOffset: .zero)
                            .withOffset(CGVector(dx: 1,   dy: y))
                        let end   = anchorApp.coordinate(withNormalizedOffset: .zero)
                            .withOffset(CGVector(dx: 200, dy: y))
                        start.press(forDuration: 0.05, thenDragTo: end)
                    case "app_switcher":
                        // Swipe up from the bottom edge to ~mid-screen and hold;
                        // opens the recent-apps carousel on iPhones without
                        // a home button.
                        let startY = screen.height - 4
                        let endY   = screen.height * 0.45
                        let start = anchorApp.coordinate(withNormalizedOffset: .zero)
                            .withOffset(CGVector(dx: screen.width / 2, dy: startY))
                        let end   = anchorApp.coordinate(withNormalizedOffset: .zero)
                            .withOffset(CGVector(dx: screen.width / 2, dy: endY))
                        start.press(forDuration: 0.7, thenDragTo: end)
                    default:
                        ok = false
                        errMsg = "unknown button: \(button)"
                    }
                }
                // Wait for the system transition to settle so the caller's
                // next observe() sees the new screen, not the old one
                // mid-animation. XCUIDevice.press(.home) returns BEFORE the
                // animation begins — so we sleep 0.5 s first (lets the
                // transition start) and then poll the frontmost PID until
                // it's stable for 300 ms. Without the pre-sleep, the loop
                // would see the old PID looking "stable" and exit before
                // SpringBoard has actually taken over.
                if ok {
                    Thread.sleep(forTimeInterval: 0.5)
                    let deadline = Date().addingTimeInterval(2.0)
                    var lastPID: pid_t = -1
                    var stableSince = Date()
                    while Date() < deadline {
                        let pid = getFrontmostPID()
                        if pid > 0 && pid == lastPID {
                            if Date().timeIntervalSince(stableSince) > 0.3 { break }
                        } else {
                            stableSince = Date()
                            lastPID = pid
                        }
                        Thread.sleep(forTimeInterval: 0.1)
                    }
                    sendResponse(connection, ["ok": true, "button": button,
                                              "settled_pid": Int(lastPID)])
                } else {
                    sendResponse(connection, ["ok": false, "error": errMsg ?? "unknown error"])
                }

            case "geocode_query":
                // Resolve a free-text address/query string to a coordinate
                // via MKLocalSearch — the SAME backend Maps.app's search
                // box uses. Running this in-sim (vs Mac CLGeocoder) avoids
                // SDK-build drift between macOS MapKit and iOS-sim MapKit,
                // so design-time geocoded coords match what Maps.app
                // resolves at episode runtime.
                //
                // Threading: testRunServer() runs on main, and
                // MKLocalSearch.start{} schedules its callback onto main.
                // A naïve semaphore.wait() would block main → callback
                // never fires → 15 s timeout on every query. We spin the
                // main runloop until completion (or deadline) instead.
                let query = cmdDict["query"] as? String ?? ""
                guard !query.isEmpty else {
                    sendResponse(connection, ["ok": false,
                        "error": "query required"])
                    break
                }
                let request = MKLocalSearch.Request()
                request.naturalLanguageQuery = query
                let search = MKLocalSearch(request: request)
                var finished = false
                var result: [String: Any] = [
                    "ok": false, "error": "geocode_query timed out"]
                search.start { response, error in
                    if let error = error {
                        result = ["ok": false,
                            "error": "MKLocalSearch failed: \(error.localizedDescription)"]
                    } else if let resp = response,
                              let item = resp.mapItems.first {
                        let p = item.placemark
                        let c = p.coordinate
                        let parts: [String] = [
                            p.subThoroughfare, p.thoroughfare,
                            p.locality, p.administrativeArea,
                            p.postalCode, p.country
                        ].compactMap { $0 }
                        result = [
                            "ok": true,
                            "lat": c.latitude,
                            "lon": c.longitude,
                            "name": item.name ?? "",
                            "formatted_address": parts.joined(separator: ", "),
                            "matches_returned": resp.mapItems.count
                        ]
                    } else {
                        result = ["ok": false, "error": "no results"]
                    }
                    finished = true
                }
                let deadline = Date().addingTimeInterval(15.0)
                while !finished && Date() < deadline {
                    RunLoop.current.run(
                        mode: .default,
                        before: Date().addingTimeInterval(0.05))
                }
                sendResponse(connection, result)

            case "quit":
                sendResponse(connection, ["ok": true])
                return

            default:
                sendResponse(connection, ["ok": false, "error": "unknown:\(type)"])
            }
        }
    }
}

// ── Read one newline-terminated line from a socket fd ────────────
func readLine(from fh: FileHandle) -> String? {
    var result = Data()
    while true {
        var byte = UInt8(0)
        let n = read(fh.fileDescriptor, &byte, 1)
        if n <= 0 { return result.isEmpty ? nil : String(data: result, encoding: .utf8) }
        if byte == UInt8(ascii: "\n") { break }
        result.append(byte)
    }
    return String(data: result, encoding: .utf8)
}
SWIFT

# ── Objective-C NSException catcher ───────────────────────────────
# Swift can't catch Objective-C exceptions. XCUITest's snapshot(),
# accessibilityInterface, and similar private/AX calls occasionally
# throw NSException during UI transitions (animations, app launches,
# Spotlight mid-dismiss). Without a catch, the whole test process dies
# and the Unix socket goes with it. This tiny ObjC shim wraps a block
# in @try/@catch so Swift can keep running through transient AX faults.
cat > "$PROJ_DIR/SIBBTests/SIBBExceptionCatcher.h" << 'OBJC'
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Runs `block` under an Objective-C @try/@catch. Returns YES on success;
/// returns NO and writes the caught exception's description into
/// `*errorDescription` (if non-NULL) on NSException.
BOOL SIBBSafeRun(NSString * _Nullable * _Nullable errorDescription,
                 NS_NOESCAPE void (^block)(void));

NS_ASSUME_NONNULL_END
OBJC

cat > "$PROJ_DIR/SIBBTests/SIBBExceptionCatcher.m" << 'OBJC'
#import "SIBBExceptionCatcher.h"

BOOL SIBBSafeRun(NSString * _Nullable * _Nullable errorDescription,
                 NS_NOESCAPE void (^block)(void)) {
    @try {
        block();
        return YES;
    } @catch (NSException *exc) {
        if (errorDescription) {
            *errorDescription = [NSString stringWithFormat:@"%@: %@",
                                 exc.name ?: @"NSException",
                                 exc.reason ?: @""];
        }
        return NO;
    } @catch (id other) {
        if (errorDescription) {
            *errorDescription = @"unknown ObjC exception";
        }
        return NO;
    }
}
OBJC

# Bridging header so the Swift test target can call the ObjC shim.
cat > "$PROJ_DIR/SIBBTests/SIBBTests-Bridging-Header.h" << 'OBJC'
#import "SIBBExceptionCatcher.h"
OBJC

# ── XcodeGen project spec ─────────────────────────────────────────
cat > "$PROJ_DIR/project.yml" << 'YAML'
name: SIBBHelper
options:
  bundleIdPrefix: com.sibb
  deploymentTarget:
    iOS: "16.0"

settings:
  base:
    SWIFT_VERSION: "5.9"
    IPHONEOS_DEPLOYMENT_TARGET: "16.0"
    CODE_SIGNING_ALLOWED: NO
    CODE_SIGN_IDENTITY: ""

targets:
  SIBBApp:
    type: application
    platform: iOS
    sources: [SIBBApp]
    settings:
      base:
        INFOPLIST_FILE: SIBBApp/Info.plist
        PRODUCT_BUNDLE_IDENTIFIER: com.sibb.helper
        CODE_SIGNING_ALLOWED: NO

  SIBBTests:
    type: bundle.ui-testing
    platform: iOS
    sources: [SIBBTests]
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: com.sibb.tests
        TEST_TARGET_NAME: SIBBApp
        BUNDLE_LOADER: "$(TEST_HOST)"
        CODE_SIGNING_ALLOWED: NO
        SWIFT_OBJC_BRIDGING_HEADER: SIBBTests/SIBBTests-Bridging-Header.h
        CLANG_ENABLE_OBJC_ARC: YES
        # Xcode auto-injects Calendars/Contacts/etc. usage descriptions
        # into the test runner's Info.plist, but NOT Reminders (the
        # Reminders permission split off from Calendar in iOS 17 and
        # the auto-injection list wasn't updated). Without these keys
        # iOS still shows the user-facing prompt on requestFullAccess,
        # even when simctl-privacy says the runner is authorized.
        INFOPLIST_KEY_NSRemindersUsageDescription: "Set up benchmark state."
        INFOPLIST_KEY_NSRemindersFullAccessUsageDescription: "Set up benchmark state."
        # PhotoKit needs both keys explicitly: `NSPhotoLibraryUsage*` for
        # readWrite access, `NSPhotoLibraryAddUsage*` for add-only. Without
        # them, PHPhotoLibrary.requestAuthorization(for: .readWrite) returns
        # .denied immediately regardless of TCC.db state. iOS doesn't
        # auto-inject these for test bundles.
        INFOPLIST_KEY_NSPhotoLibraryUsageDescription: "Set up benchmark state."
        INFOPLIST_KEY_NSPhotoLibraryAddUsageDescription: "Set up benchmark state."
        # HealthKit needs both share (read) and update (write) descriptions.
        # Xcode auto-injects some test bundle plist keys but HealthKit's pair
        # isn't reliably auto-injected — declare explicitly so HKHealthStore.
        # requestAuthorization doesn't return .sharingDenied immediately.
        INFOPLIST_KEY_NSHealthShareUsageDescription: "Set up benchmark state."
        INFOPLIST_KEY_NSHealthUpdateUsageDescription: "Set up benchmark state."
    dependencies:
      - target: SIBBApp
        embed: false
YAML

# ── Generate and build ────────────────────────────────────────────
cd "$PROJ_DIR"
echo "Generating Xcode project..."
xcodegen generate

echo "Building SIBBHelper..."
UDID="${1:-19B95A95-614A-4ECA-B943-44FDADFD7A9F}"
echo "Building for simulator: $UDID"

xcodebuild build-for-testing \
  -project SIBBHelper.xcodeproj \
  -scheme SIBBTests \
  -destination "platform=iOS Simulator,id=$UDID" \
  -derivedDataPath "$PROJ_DIR/build" \
  CODE_SIGNING_ALLOWED=NO \
  2>&1 | grep -E "error:|warning:|BUILD|Compiling|Linking" | tail -20

echo ""
echo "════════════════════════════════════════════════"
echo "  Build complete."
echo "  Project: $PROJ_DIR"
echo "  Run: python3 sibb_xcuitest_client.py <UDID>"
echo "════════════════════════════════════════════════"
