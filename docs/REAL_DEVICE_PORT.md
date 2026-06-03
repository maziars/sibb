# SIBB on Real iPhones — Feasibility & Showcase Paths

**Date:** 2026-05-19 · **Status:** investigation complete, no implementation yet
· **Source:** synthesized from 8 parallel sub-agent investigations
across transport, code-signing, XCUITest behavior, permissions,
dev-loop, background capability, runner lifespan, alternative iOS
APIs, sideloading, and Apple Intelligence developer surfaces.
Per-agent reports retained in this session's transcript.

This doc is the canonical record of what is and is not possible when
taking SIBB's agent loop (observe → LLM → act) onto real iPhones,
under three different deployment models — and which model fits what
goal.

---

## TL;DR

Three deployment models, three answers:

| Model | Can it drive the iPhone UI? | Productizable to end users? |
|---|---|---|
| **A. Standalone iPhone app from the App Store** (user downloads, opens, uses) | **NO** — Apple sandbox forbids cross-app AX read and tap injection categorically | Yes-to-ship, but the capability isn't there |
| **B. Mac-tethered XCUITest showcase** (Developer Mode + cable + xcodebuild on Mac) | **YES** — full cross-app driving, screenshot, AX read, action injection | No — it's a tech demo, not a shippable app |
| **C. Android equivalent** (pivot platform) | YES — `AccessibilityService` gives exactly this capability | Yes |

**The cross-app driving capability SIBB has on the simulator
fundamentally does not exist for a downloadable iOS app. Period.** This
isn't a missing API — it's a load-bearing pillar of iOS's security
model. Apple has made it explicit (WWDC 25, App Intents docs, Siri V2
roadmap delayed to spring 2026) that **Siri / Apple Intelligence is the
sole cross-app orchestrator**; third-party apps participate only by
*exposing* their actions via App Intents, never by *driving* others.

---

## How we know — the evidence

Three independent sub-agent investigations triangulated to the same
answer:

### 1. Standalone-app ground truth

Every shipping AI assistant on the App Store reaches Reminders/Calendar
through **data frameworks** (EventKit, Contacts, MapKit, MessageUI) or
by **invoking App Intents** the target app voluntarily published —
never by driving UIs:

| App | Mechanism |
|---|---|
| Claude iOS | EventKit + tool calls (`reminder_create_v0`, etc.) — direct data API |
| Perplexity Assistant | App Intents + URL schemes |
| ChatGPT iOS | Internal Tasks; no cross-app drive |
| Gemini / Pi / Replika / Arc Search | URL schemes, share sheet, web |

iOS Security Model (Apple's docs): *"an inability to access any other
process on the system, even if that process is owned by the same UID."*
Cross-app reads/writes go only through system-mediated services with
explicit entitlements; the entitlements that would permit cross-app AX
or event injection (`com.apple.private.accessibility.*`) are reserved
for Apple binaries and rejected at App Store review and code-sign time.

`UIAccessibility` exposes an app's *own* tree to assistive tech — it is
**not** an API for *reading* another app. iOS has no analogue to
Android's `AccessibilityService` for third-party consumers.

### 2. Sideloading paths don't help

| Path | Cross-app capability? | Notes |
|---|---|---|
| **TrollStore** (arbitrary entitlements) | Would, but… | **Frozen at iOS 17.0.** Apple patched CoreTrust in 17.0.1 (Oct 2023); no successor for 2+ years. Dead-end. |
| **Jailbreak tweaks** (root + private XPC) | Yes, technically | No public jailbreak for iOS 17.5+/A15+; dies on every iOS bump. SIBB-scale reproducibility impossible. |
| **AltStore + free dev cert** | Same sandbox | 7-day re-sign treadmill; no extra entitlements. |
| **AltStore PAL (EU DMA)** | **Same sandbox** | DMA forced *distribution* open, not *capability*. Apple's compliance doc: "Apps distributed via alternative marketplaces are still subject to the same operating system enforcement of privacy, security, and entitlement rules as App Store apps." |
| **Enterprise certs ($299/yr)** | No | Sign-only; zero extra entitlements. Apple aggressively revokes for non-employee use. |
| **MDM Supervised + ASAM** | No | Supervised mode is **restrictive, not permissive** — limits the device, doesn't elevate one app over others. |

### 3. Apple's roadmap doesn't open this up

Apple Intelligence + App Intents + Siri V2 (delayed to spring 2026)
all keep **Siri as the sole orchestrator**:

> *"While your app can understand and use the Entity in your AppIntents,
> other apps can't understand your entity or use it in their AppIntents."*
> — Apple's "Making onscreen content available to Siri" docs

> *"This expanded capability will work in Apple apps and is facilitated
> by third-party developers exposing their apps' on-screen content to
> Siri through the App Intents API."* — MacRumors on Siri V2

iOS 26 expanded the App-Intents-provider surface (Visual Intelligence,
snippets, deferred properties) but added zero "drive another app"
entitlements. MCP support rumored for iOS 19 would expose
Apple Intelligence as an MCP host — still Siri-mediated, not
third-party-driven. **iOS 20 (2027) and 21 (2028) most likely add
more domains, not arbitrary cross-app UI driving.**

---

## What about Developer Mode without a Mac?

**No. Confidence 5/5.** Verified by a focused investigation.

Developer Mode lets you *install* and *launch* test bundles. It does
not grant runtime privileges. XCUITest's cross-app AX privileges are
granted dynamically by **`testmanagerd`** when a remote DTX client
(Xcode / xcodebuild / pymobiledevice3 / go-ios) opens the
`dtxproxy:XCTestManager_IDEInterface:XCTestManager_DaemonConnectionInterface`
channel and runs the `_IDE_initiateControlSessionForTestProcessID:`
sequence. That handshake — *not* the code signature or any
entitlement — is what hands the runner cross-app AX read and
synthetic-event-injection rights.

The required bootstrap chain:

```
host process  →  usbmuxd / RemoteXPC tunnel  →  lockdownd  →
  com.apple.testmanagerd.lockdown  →  testmanagerd  →
    DTX channel to XCTRunner  →  privileges enabled
```

**Step 1 (the external DTX client) is mandatory and cannot be
substituted from on-device.** Concretely:

- Tapping `WebDriverAgentRunner.xctrunner` from SpringBoard launches
  the process as a plain UI-less app — no cross-app AX, no synthetic
  taps. Useless without testmanagerd activation.
- `simctl` / `devicectl` *can* launch the runner, but it's inert until
  a remote DTX session attaches; once that session drops the runner
  is reaped within seconds (verified empirically — pymobiledevice3
  issue #1666, go-ios issue #391).
- lockdownd's `com.apple.testmanagerd.lockdown` service requires
  pairing-record authentication. No on-device process can initiate
  the channel.
- `xctest` / `XCTAutomationSupport` is a private framework with an
  **allowed-clients list enforced at link time** (Xcode 14+). A
  regular signed app cannot legally link it to self-bootstrap.
- No public or restricted iOS entitlement (in iOS 17 / 18 / 26) mints
  XCUITest-equivalent privileges to a third-party app.

**Jailbreak DOES change this answer.** With root + AMFI bypass you can
side-load a binary entitled to talk to `com.apple.testmanagerd.lockdown`
over the loopback usbmuxd socket, or run a stub DTX client on-device.
This is how jailbreak AX-automation tweaks work. On a stock
Developer-Mode device: no path.

**Bottom line:** the iPhone needs a Mac (or some external host running
pymobiledevice3 / xcodebuild) to *establish* the XCUITest session. The
session cannot survive the host disconnecting — `testmanagerd` reaps
the runner within seconds once the DTX channel drops. "Tethered" is
a hard requirement of the architecture, not just for the demo.

---

## Option A — App-Intents standalone iPhone app (productizable)

**What it is:** a regular App Store iOS app that exposes its own
`AppIntent`s, uses Apple's data frameworks directly (EventKit /
Contacts / MapKit / MessageUI / etc.) for tasks that have a data API,
and invokes other apps' App Intents via Shortcuts + URL schemes for
the rest.

**Why this is the right model for a real consumer assistant:** it's
what Claude / Perplexity / Pi already ship. It's App-Store legal. It
covers ~60–70% of common assistant tasks because Apple-stock apps
mostly publish intents and have data APIs. The remaining 30–40% (apps
without intents, novel UI flows) is genuinely impossible on iOS today.

**SIBB scaffold reuse:** **minimal**. The agent loop, action grammar,
and AX-reading code don't apply. What ports cleanly is the
conversational front-end and the LLM-driver architecture concept; the
backend is entirely Apple's data frameworks.

**Effort:** 2–4 weeks for a polished Reminders/Calendar/Contacts
demo; longer for breadth across apps.

**Scope of coverage:**

| Capability | Available |
|---|---|
| Add reminder / event / contact | ✓ via EventKit / Contacts |
| Query existing reminders / events | ✓ via EventKit / Contacts |
| Send message to a contact | ✓ via MessageUI (user-confirmed) |
| Open Maps to a location | ✓ via MapKit |
| Search Spotlight | ✓ via Core Spotlight |
| Invoke a Shortcut by name | ✓ via URL scheme |
| Invoke another app's `AppIntent` | ✓ if the app published one |
| Drive Safari to fill a form | ✗ |
| Drive a third-party app without App Intents | ✗ |
| Read another app's onscreen state | ✗ |

**Pattern Apple seems to bless:** Foundation Models on-device (iOS 26)
for the LLM + your own `AppIntent`s for your features + Shortcuts URL
chaining for external apps + EventKit/Contacts/MapKit for stock-app
manipulation.

---

## Option B — Mac-tethered XCUITest showcase (demonstrable, not shippable)

**What it is:** SIBB's existing simulator scaffold pointed at a real
iPhone via USB. The Mac runs `pymobiledevice3 tunneld` + an
`xcodebuild test`-equivalent process holding the testmanagerd
assertion alive; the iPhone runs the XCUITest test bundle SIBB already
builds. The LLM driver (Python script on the Mac) reads AX over USB,
sends to an LLM, parses the action, sends back to the device. Same
agent loop as the simulator.

**Why this is a valid showcase even if it's not shippable:** it
demonstrates the *capability* — "an LLM can drive a real iPhone the
same way it drives our simulator." The audience understands the
tether as a research / demonstration constraint, similar to how an
Android equivalent would be shown via a debug-mode device.

### What works on-device (verified across 8 sub-agent reports)

| Component | Status |
|---|---|
| `XCUIApplication.snapshot()` for foreground-app AX | ✓ same API, **5–20× slower** than sim |
| Tap / type / swipe / `press(.home)` injection into foreground app | ✓ same API |
| `XCUIScreen.main.screenshot()` | ✓ same API |
| Cross-app driving while runner backgrounded | ✓ same as Appium production |
| Action grammar (TAP / TYPE / SCROLL / DONE / ANSWER) | ✓ verbatim |

### What does NOT survive

- **Unix socket** at `/tmp/sibb_xcuitest.sock` — device sandbox; must
  switch to TCP via `pymobiledevice3 tunneld`
- **Background lifetime beyond a few hours** — WDA leaks ~20 MB → 2 GB,
  forces restart
- **Lock screen + auto-lock** — kills the session reliably
- **Test runner without a live xcodebuild/pymobiledevice3 connection**
  — reaped within seconds once testmanagerd assertion drops
- **`UIBackgroundModes`** isn't the answer — the lifeline is
  testmanagerd's assertion, not Info.plist
- **iOS 26.4.2 has a regression** that reaps the runner at T+1.2s on
  background-intent launch; iOS 26.3 (our sim) and earlier 26.x is fine

### Showcase build path (5–7 days)

1. **Day 1: Pair & sign.** Free Apple ID is sufficient (7-day cert,
   re-sign weekly). Enable Developer Mode on the iPhone. Build
   SIBBHelper for device target via Xcode; install via cable.

2. **Day 2–3: Transport.** `pip install pymobiledevice3`; run
   `sudo python3 -m pymobiledevice3 remote tunneld` as a LaunchDaemon.
   Switch Swift server from `UnixDomainSocketAcceptor` to
   `NWListener(using: .tcp, on: 27015)`. Update
   `sibb_xcuitest_client.py` to connect via the tunneld-forwarded port
   with reconnect-with-backoff.

3. **Day 3–4: Survive screen lock.** Auto-Lock = Never, passcode off
   for the demo period. Add `UIBackgroundModes = ["fetch"]` (mostly
   for hygiene — see caveat above). 20-s Swift-side heartbeat to keep
   the TCP socket warm.

4. **Day 4–5: Permissions.** Mirror every `NSUsageDescription` from
   the host app's Info.plist into the runner's Info.plist (see
   Appendix A). Skip HealthKit unless the demo specifically needs it
   (free tier can't grant the entitlement). User taps "Allow" on each
   TCC dialog interactively the first time.

5. **Day 5: Latency check.** Benchmark `XCUIApplication.snapshot()`
   p50/p95/p99 on home / Reminders / Safari. **Decision gate:** if
   p50 > 2 s after `snapshotMaxDepth` tuning, the UX of the demo
   suffers; revisit. For an interactive assistant 1–3 s per observe
   is acceptable; users expect "the model thinks for a moment."

6. **Day 5–7: LLM driver.** Build `sibb_assistant.py` (new ~150-line
   script, currently missing — `sibb_replay.py` is human-driven).
   Connects to the device's TCP socket, reads AX, sends to Claude/etc.
   API, parses response, executes. Health-check ping every 5 s; auto-
   relaunch the runner every ~30 actions (the documented
   WebDriverAgent #507 lifetime ceiling).

### Recurring costs

- Free Apple ID = $0 (7-day cert rotation, manageable for a personal
  demo)
- One iPhone = ~$430 (SE 3rd gen) and up
- One USB cable
- No fastlane, no MDM, no cloud Macs

### Verification before committing

Do this in ~2 hours on Day 1 to de-risk the rest:

1. Install SIBBHelper on the iPhone via Xcode
2. Run the existing `sibb_inspect_screen.py` over the new TCP path
3. Grab AX trees from: Home / Reminders / Settings sub-page / Safari /
   Maps
4. Confirm tree shapes look familiar vs the sim equivalents

If that smoke test passes, the rest of the 5–7 day build is mostly
plumbing.

---

## Option C — Both (recommended for the showcase)

Ship Option A as the **product-shaped demo** (downloadable
standalone-iPhone app, App-Intents + EventKit, fully App-Store-legal)
**alongside** Option B as the **capability demo** (Mac-tethered
XCUITest, drives any app's UI). The contrast IS the story:

> *Here is the most capable assistant a third-party developer can ship
> on iOS today (Option A). Here is the same agent loop driving the
> same iPhone, but now Apple's sandbox is bypassed via developer-only
> tooling (Option B). The gap is what Apple owns and won't open up.*

This framing avoids overselling Option A as a panacea and avoids
hiding the fact that Option B can't be a consumer app.

---

## Option D — Pivot to Android

`AccessibilityService` on Android gives exactly the cross-app driving
capability iOS withholds. User-grantable, App-Store-distributable,
productizable. If the goal is "show how to build an LLM-driven phone
assistant **that ships**", Android is the platform where the answer
is "yes."

SIBB's scaffold doesn't reuse — the AX tree, action grammar, and
runtime model would need an Android-side rewrite. But the *agent
loop logic* (observe → LLM → action → execute → repeat) is identical.

Worth flagging as an option if the showcase audience cares about
"shippable", not "demonstrable."

---

## Decision matrix

| If your goal is… | Pick |
|---|---|
| Showcase that an LLM can drive a real iPhone the way SIBB drives the sim | **B** |
| Build a downloadable iPhone assistant your friends can install | **A** |
| Tell the most honest story about iOS's actual capability ceiling | **A + B** |
| Build an actual product | **A** (limited surface) or **D** (Android) |

---

## Appendix A — Info.plist & entitlement checklist (Option B)

For the Mac-tethered XCUITest showcase on a real iPhone, the runner's
Info.plist needs:

```
NSRemindersFullAccessUsageDescription
NSCalendarsFullAccessUsageDescription
NSContactsUsageDescription
NSPhotoLibraryUsageDescription
NSLocationWhenInUseUsageDescription
UIBackgroundModes = ["fetch"]
```

Skip HealthKit entitlements unless the demo specifically uses Health
(paid Apple Developer Program required — $99/yr — and the free tier
cannot grant it).

## Appendix B — Device verification checklist (Option B)

Smoke-test on real iPhone in the first day:

1. `XCUIApplication.snapshot()` — p50/p95/p99 latency
2. `XCUIDevice.shared.accessibilityInterface.activeApplications()` —
   selector present + filters SpringBoard
3. `XCUIScreen.main.screenshot()` — PNG returned, size sane
4. `XCUIElement.tap()` / `press(forDuration:thenDragTo:)` — coords map 1:1
5. `XCUIElement.typeText()` with empty / single char / `\n` / emoji
6. `XCUIDevice.shared.press(.home)` — both lock-screen and active-app
7. Cross-app capture during system modal (TCC dialog, notification,
   control center)
8. NSException Obj-C shim — force a private-API throw, confirm Swift
   catches
9. TCP socket survival across 30 minutes idle + screen lock + screen
   wake
10. Per-handler Swift commands (`reminders.create_list`,
    `calendar.create_event`, `shortcuts.run`, etc.)

## Appendix C — Sources

**Standalone-app ground truth:**
- [Apple — Security of runtime process](https://support.apple.com/guide/security/security-of-runtime-process-sec15bfe098e/web)
- [Apple — Protecting user data with App Sandbox](https://developer.apple.com/documentation/security/protecting-user-data-with-app-sandbox)
- [Apple — App Intents](https://developer.apple.com/documentation/appintents/)
- [Apple — Making onscreen content available to Siri](https://developer.apple.com/documentation/appintents/making-onscreen-content-available-to-siri-and-apple-intelligence)
- [Testing Claude's Native Integration with Reminders and Calendar on iOS — MacStories](https://www.macstories.net/notes/testing-claudes-native-integration-with-reminders-and-calendar-on-ios-and-ipados/)
- [Apple Plans to Release Delayed Siri Apple Intelligence Features in Spring 2026 — MacRumors](https://www.macrumors.com/2025/06/12/apple-intelligence-siri-spring-2026/)

**Sideloading & jailbreak:**
- [opa334/TrollStore — README](https://github.com/opa334/TrollStore)
- [TrollStore on iOS 17.0.1+: Can You Still Install It? — iDevice Central](https://idevicecentral.com/tweaks/can-you-install-trollstore-on-ios-17-0-1-ios-18-3/)
- [Introducing AltStore PAL — Riley Testut](https://rileytestut.com/blog/2024/04/17/introducing-altstore-pal/)
- [Apple — Complying with the DMA (PDF)](https://developer.apple.com/security/complying-with-the-dma.pdf)

**Mac-tethered XCUITest:**
- [pymobiledevice3 — GitHub](https://github.com/doronz88/pymobiledevice3)
- [pymobiledevice3 iOS 17+ Tunnels guide](https://github.com/doronz88/pymobiledevice3/blob/master/docs/guides/ios17-tunnels.md)
- [Appium XCUITest Real-Device Config](https://appium.github.io/appium-xcuitest-driver/latest/preparation/real-device-config/)
- [Appium #15457 — WDA memory leak 20MB→2GB](https://github.com/appium/appium/issues/15457)
- [WebDriverAgent #507 — session dies after 30 tests](https://github.com/facebookarchive/WebDriverAgent/issues/507)
- [pymobiledevice3 #1666 — iOS 26.4.2 runner reaped at T+1.2s](https://github.com/doronz88/pymobiledevice3/issues/1666)
- [Appium — Run Preinstalled WDA](https://appium.github.io/appium-xcuitest-driver/latest/guides/run-prebuilt-wda/)

**Apple Intelligence trajectory:**
- [Explore new advances in App Intents — WWDC25](https://developer.apple.com/videos/play/wwdc2025/275/)
- [Get to know App Intents — WWDC25](https://developer.apple.com/videos/play/wwdc2025/244/)
- [iOS 26 App Intents — Crosley](https://blakecrosley.com/blog/app-intents-2-ios-26-additions)
- [iOS 26 Shortcuts new actions — 9to5Mac](https://9to5mac.com/2025/12/09/ios-26s-shortcuts-app-adds-25-new-actions-heres-everything-new/)

**Self-bootstrap verification (Mac-free XCUITest):**
- [go-ios issue #391 — runwda needs testmanagerd channel](https://github.com/danielpaulus/go-ios/issues/391)
- [pymobiledevice3 RemoteXPC docs (iOS 17+)](https://github.com/doronz88/pymobiledevice3/blob/master/misc/RemoteXPC.md)
- [Appium issue #17174 — XCTAutomationSupport allowed-clients enforcement (Xcode 14+)](https://github.com/appium/appium/issues/17174)
- [WebDriverAgent issue #689 — can WDA work without Xcode?](https://github.com/facebookarchive/WebDriverAgent/issues/689)
- [Appium — Run Preinstalled WebDriverAgentRunner](https://appium.github.io/appium-xcuitest-driver/9.0/guides/run-preinstalled-wda/)
- [facebook/idb fbxctest Details — testmanagerd's role](https://github.com/facebook/idb/blob/main/fbxctest/Documentation/Details.md)
