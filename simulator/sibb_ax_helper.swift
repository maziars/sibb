import Cocoa
import ApplicationServices

// SIBB AX Helper — reads accessibility tree from iOS Simulator
// Compile: swiftc sibb_ax_helper.swift -o sibb_ax_helper -framework Cocoa -framework ApplicationServices
// Usage:   ./sibb_ax_helper <UDID>
// Output:  JSON array of accessible elements

func readElement(_ element: AXUIElement, depth: Int = 0, maxDepth: Int = 12) -> [[String: Any]] {
    var results: [[String: Any]] = []
    if depth > maxDepth { return results }

    var label: AnyObject?
    var role: AnyObject?
    var value: AnyObject?
    var hint: AnyObject?
    var enabled: AnyObject?
    var frame: AnyObject?
    var children: AnyObject?

    AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &role)
    AXUIElementCopyAttributeValue(element, kAXTitleAttribute as CFString, &label)
    if label == nil {
        AXUIElementCopyAttributeValue(element, kAXDescriptionAttribute as CFString, &label)
    }
    AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value)
    AXUIElementCopyAttributeValue(element, kAXHelpAttribute as CFString, &hint)
    AXUIElementCopyAttributeValue(element, kAXEnabledAttribute as CFString, &enabled)
    AXUIElementCopyAttributeValue(element, kAXFrameAttribute as CFString, &frame)
    AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &children)

    var dict: [String: Any] = [:]
    if let r = role as? String    { dict["AXRole"] = r }
    if let l = label as? String, !l.isEmpty { dict["AXLabel"] = l }
    if let v = value as? String, !v.isEmpty { dict["AXValue"] = v }
    if let h = hint as? String, !h.isEmpty  { dict["AXHint"] = h }
    if let e = enabled as? Bool  { dict["AXEnabled"] = e }

    if let f = frame {
        var frameRect = CGRect.zero
        let cfValue = f as! AXValue
        AXValueGetValue(cfValue, AXValueType.cgRect, &frameRect)
        dict["frame"] = [
            "x": Double(frameRect.origin.x),
            "y": Double(frameRect.origin.y),
            "width": Double(frameRect.size.width),
            "height": Double(frameRect.size.height)
        ]
    }

    if !dict.isEmpty { results.append(dict) }

    if let childArray = children as? [AXUIElement] {
        for child in childArray {
            results.append(contentsOf: readElement(child, depth: depth + 1, maxDepth: maxDepth))
        }
    }

    return results
}

func findSimulatorWindow() -> AXUIElement? {
    let apps = NSWorkspace.shared.runningApplications
    for app in apps {
        if app.bundleIdentifier == "com.apple.iphonesimulator" {
            let axApp = AXUIElementCreateApplication(app.processIdentifier)
            return axApp
        }
    }
    return nil
}

// Request accessibility permission
let trusted = AXIsProcessTrusted()
if !trusted {
    fputs("ERROR: Accessibility permission required.\n", stderr)
    fputs("Go to: System Settings → Privacy & Security → Accessibility\n", stderr)
    fputs("Add Terminal and enable it, then try again.\n", stderr)
    exit(1)
}

guard let simApp = findSimulatorWindow() else {
    fputs("ERROR: Simulator.app not running. Open it first.\n", stderr)
    exit(1)
}

let elements = readElement(simApp)

guard let json = try? JSONSerialization.data(
    withJSONObject: elements,
    options: [.prettyPrinted]
),
let str = String(data: json, encoding: .utf8) else {
    fputs("ERROR: Failed to serialize JSON\n", stderr)
    exit(1)
}

print(str)
