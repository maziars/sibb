# App coverage strategy

What apps SIBB can cover, what's blocked, and what to do about the
gaps. Audit done 2026-05-16 against iOS 26.3 simulator.

## TL;DR

- **Installed on iOS 26.3 simulator: 21 user-facing apps** (verified
  via `xcrun simctl listapps`). This is the hard ceiling — anything
  else is intentionally excluded by Apple and not installable
  ([`IOS_SIM_QUIRKS.md` §9](IOS_SIM_QUIRKS.md)).
- **Original design wanted ~20 apps**. We have direct access to
  most via the 21-installed set, but **6 Tier-A/B apps from the
  original design ship as gutted runtime stubs**: Notes, Mail,
  Clock (Tier A) + Podcasts, Music, Books (Tier B).
- **OSS substitutes are viable for Notes, Mail, Books** with
  AppleVis-confirmed strong VoiceOver / AX quality.
- **Build-our-own makes sense for Clock and Calculator**.
- **Podcasts, Music, Voice Memos**: OSS candidates exist but AX
  quality is unverified; defer until verified or until benchmark
  needs them.
- **Phone, Keynote, Pages, Numbers, Voice Memos**: not in
  runtime, no OSS substitute meaningfully helps.

## Full app inventory

| Original app | Tier | Available in sim? | Handler status | OSS substitute | Build-our-own? |
|---|---|---|---|---|---|
| Reminders     | A | ✅ installed                | ✅ onboarded     | — | — |
| Calendar      | A | ✅ installed                | ✅ onboarded     | — | — |
| Contacts      | A | ✅ installed                | ✅ onboarded     | — | — |
| Settings      | A | ✅ installed                | ✅ onboarded     | — | — |
| Files         | A | ✅ installed                | ✅ onboarded     | — | — |
| Photos        | A | ✅ installed                | ✅ onboarded     | — | — |
| Health        | A | ✅ installed (sim-limited)  | scaffolded (L1+L1.5 pass; L2 skipped — see IOS_SIM_QUIRKS §10) | — | — |
| Maps          | A | ✅ installed                | ✅ minimal (no inject; v1 = bundle_id + location TCC only) | — | — |
| Shortcuts     | A | ✅ installed                | ✅ minimal handler — `apply(type="run", name, input)` via `shortcuts://run-shortcut?...` URL scheme; `shortcuts.installed` AX-read fetcher. No create/edit/delete (Apple-API gap); no run-by-name for Automations (use UI "Run Immediately").    | — | — |
| **Notes**     | A | ❌ gutted stub              | gap              | [Simplenote](https://github.com/Automattic/simplenote-ios) | — |
| **Clock**     | A | ❌ gutted stub              | gap              | none found       | yes (~½ day) |
| **Mail**      | A | ❌ gutted stub              | gap              | [ProtonMail iOS](https://github.com/ProtonMail/ios-mail) | — |
| Safari        | B | ✅ installed                | ✅ bookmarks (v1)         | — | — |
| Messages      | B | ✅ installed (sim-hostile)  | deferred (multi-store architecture; sms.db inserts succeed but UI filters at IMDPersistence layer — see IOS_SIM_QUIRKS §11) | — | — |
| Home          | B | ❌ unclear (HomeKit-tied)   | gap              | hard           | hard |
| **Books**     | B | ❌ gutted stub              | gap              | [Readest](https://github.com/readest/readest) | — |
| **Podcasts**  | B | ❌ gutted stub              | gap              | [cuappdev/podcast-ios](https://github.com/cuappdev/podcast-ios) (AX unverified) | possible |
| **Music**     | B | ❌ gutted stub              | gap              | [bpolat/Music-Player](https://github.com/bpolat/Music-Player) (AX unverified) | possible |
| **Voice Memos**| B | ❌ not in runtime          | gap              | [VoiceMemosClone](https://github.com/HassanElDesouky/VoiceMemosClone) (AX unverified) | possible |
| Phone         | B | ❌ not in runtime          | impossible       | n/a — telephony hardware | n/a |
| Keynote       | B | ❌ not in runtime          | impossible       | n/a — no equivalent | n/a |
| Pages         | B | ❌ not in runtime          | impossible       | n/a | n/a |
| Numbers       | B | ❌ not in runtime          | impossible       | n/a | n/a |
| **Calculator**| Excluded ("stateless") | ❌ gutted stub | reconsider | several OSS, easy to verify | **yes (~½ day)** |
| Freeform      | Excluded ("no AX tree") | ✅ in runtime, ❌ stripped | reconsider when iOS includes it | hard | hard |
| Camera        | Excluded (hw) | ❌                            | impossible       | n/a | n/a |
| FaceTime      | Excluded (hw) | ❌                            | impossible       | n/a | n/a |
| Weather, Stocks, TV | Excluded (network) | ❌                  | impossible       | n/a | n/a |

### Apps newly discovered installed (weren't on original list)

These ship installed but weren't in the original Tier A/B
breakdown. All are viable handler targets:

| App | Bundle ID | Data API | Notes |
|---|---|---|---|
| Fitness   | com.apple.Fitness    | Healthdb host-side sqlite (read) + HealthKit (write, TBD) | ✅ minimal handler (v1: registry + `fitness.activity_summary` fetcher reading `activity_caches`). Host-side reads bypass the HealthKit auth gate that blocks Swift writes (IOS_SIM_QUIRKS §10). On iPhone-only sims, only Move ring + steps populated; Exercise/Stand columns NULL (need Apple Watch). Workout writes (HKWorkout) deferred until first Fitness task. |
| News      | com.apple.news       | AX-tree headlines (v1) | ✅ minimal handler; feed reads work, article detail unreliable. See IOS_SIM_QUIRKS §12 |
| Passwords | com.apple.Passwords  | Keychain SQL (counts + SHA-1 hash equality) + AX UI | ✅ minimal handler (v1: registry, `passwords.entry_count`, `passwords.entry_exists`). Values are encrypted (`SFAuthenticatedCiphertext`); `acct`/`srvr` columns store SHA-1 of plaintext → hash-equality verification works without decryption. Writes blocked by `com.apple.password-manager` access-group entitlement; agent must use New Password UI. See IOS_SIM_QUIRKS §13. |
| Wallet    | com.apple.Passbook   | `PassKit` / `PKPass`        | ⛔ **Deferred 2026-05-17.** Apple Pay (Add Card) impossible by design — no Secure Element on sim. PKPass sideload requires Apple-issued PassKit cert ($99/yr Developer Program + per-pass signing pipeline); empirically verified sim enforces signature validation just like real device. See IOS_SIM_QUIRKS §15. |
| Preview   | com.apple.Preview    | Filesystem                  | ⛔ **Skipped 2026-05-17.** Empirically unlaunchable on sim — `FBSOpenApplicationServiceErrorDomain code=1`. Same stub pattern as Mail/Notes/Clock — see IOS_SIM_QUIRKS §9. |

### News-specific notes

News v1 handler ships AX-tree-based feed reading only. Earlier
speculation (May 2026) about reading-list plist writes turned out
to be blocked by an undocumented 6-byte file-header wrapper. Full
capability analysis lives in [`IOS_SIM_QUIRKS.md` §12](IOS_SIM_QUIRKS.md).

**Quick task feasibility table** (verified 2026-05-16/17):

| Task | Feasible? | How |
|---|---|---|
| Find an article about topic X in Today feed | ✅ | `news.headlines` fetcher reads AX tree, match titles |
| Switch tabs (Today/News+/Audio/Following/Search) | ✅ | UI navigation, AX verification |
| Tap a Today-feed article to read body | ⚠️ | Often "Cannot Connect"; some free articles work |
| Browse Search-tab CATEGORIES (Food/Sports/etc.) | ✅ | Real content, ~14 categories |
| Tap a Food article to read full body | ✅ | Food articles open reliably (~18 scroll pages); Like/Dislike/Share/More toolbar |
| Browse Recipe Catalog under Food | ✅ | Real recipes from Real Simple, Epicurious, Simply Recipes — `news.recipes` fetcher |
| Filter recipes by chip (Vegetarian/<30 Min/...) | ✅ | UI taps on filter chips, AX verification of result list |
| Search News by keyword | ❌ | "Search Unavailable" — server endpoint missing |
| **Verify agent viewed a specific recipe/article** | ✅ | Query `article_exposures.ItemExposure` SQLite (plain SQL; rows grow per view) |
| **Verify a save completed (story/recipe)** | ✅ | Parse `offline-manifest-{saved,recipe-box}-feed` JSON `recordIDs` array |
| **Verify agent navigated to History tab** | ✅ | `offline-manifest-history-feed` JSON populated |
| **Add recipe to Grocery List → check Reminders** | ❌ on sim | Requires iCloud sign-in; News→Reminders sync via CloudKit no-ops without account |
| Programmatically inject saved recipe | ❌ | reading-list files have 6-byte wrapper of unknown semantics |
| Decode recipe titles/ingredients from inventory DB | ⚠️ | `recipes-inventory.db` has 2520 IDs+scores+dates but content in PBData protobuf (Apple's `.proto` not public) |
| Distinguish News+ paywalled vs free content | ✅ | AX-tree markers: `Image 'newsplus.fill'`, `"Included in an Apple News+ subscription"` text, `"Free Recipes"` section headers — all reliably present |
| Share content via Share button | ⚠️ | Share button exists; share-sheet probe not completed; targets on sim limited (no Mail, no Messages, no AirDrop peers) |

For richer "agent reads + reacts to news" tasks where article
body matters, **NetNewsWire** (see OSS substitutes table) is a
genuinely better fit — local RSS feeds, real content, full AX,
no account required.

## OSS substitute notes (where evidence is non-trivial)

### Notes — Simplenote (Automattic)

- **Why this candidate**: actively maintained by Automattic, large
  user base, native Swift + UIKit.
- **AX evidence**:
  [AppleVis review](https://www.applevis.com/apps/ios/productivity/simplenote)
  *"fully accessible with VoiceOver, easy to navigate and use…
  All buttons are clearly labeled."* Used daily by VoiceOver
  users in the AppleVis community.
- **Build risk**: SPM-based, should build cleanly for sim. Has
  cloud sync (Simperium) which we'd need to disable for offline
  test use.

### Mail — ProtonMail iOS

- **Why this candidate**: mature, production-quality, native Swift.
  Thunderbird iOS is also an option but newer (less AX evidence).
- **AX evidence**:
  [AppleVis review](https://www.applevis.com/apps/ios/productivity/protonmail-encrypted-email)
  *"mostly accessible with VoiceOver, minor issues, easy to deal
  with."*
- **Build risk**: Rust SDK dependency for crypto; may complicate
  sim builds. Account setup required — would need a test account
  or local-only mock backend.

### Books — Readest

- **Why this candidate**: explicit accessibility support documented
  (VoiceOver / TalkBack / NVDA / Orca). Cross-platform but iOS-
  buildable.
- **AX evidence**: README explicitly claims screen-reader support
  across major readers.
- **Build risk**: Cross-platform (Tauri-style?) — verify it's
  actually native iOS, not a web wrapper that produces a degraded
  AX tree.

### Clock — build our own

- No good OSS iOS Clock app exists (clock apps are mostly widgets,
  not full apps with alarm/timer/stopwatch tabs).
- Native iOS Clock has 5 tabs: World Clock, Alarm, Stopwatch,
  Timer, Bedtime. Each is a simple list/grid + edit modal.
- Estimated effort: ~½ to 1 day for a SwiftUI implementation that
  covers Alarm + Timer (the two tabs with persistent state worth
  setting up).
- AX is automatic — native `UIDatePicker`, `UITableView`,
  `UISwitch` all carry rich AX labels by default.

### Calculator — reconsider, build our own

- **Why originally excluded**: original design said "stateless" —
  no persistent store to verify against.
- **Reconsidered (2026-05-16)**: the on-screen display IS the
  source of truth, accessible via the AX tree's display-label
  element. Multi-step calculations ("compute 15% tip then split
  3 ways") have real sequential structure.
- **OSS options exist** ([rskworld/swift-ios-calculator](https://github.com/rskworld/swift-ios-calculator),
  [andreiui/swift-calculator](https://github.com/andreiui/swift-calculator))
  but **building our own is cheaper than vetting OSS** —
  Calculator UI is ~200 LOC, AX is trivial.
- Estimated effort: ~½ day.

### Podcasts / Music / Voice Memos — verify before commit

OSS candidates exist for each:
- Podcasts: cuappdev/podcast-ios (student-org), rafaelclaycon/PodcastApp
- Music: bpolat/Music-Player
- Voice Memos: HassanElDesouky/VoiceMemosClone

But **none have documented AX support**, and personal/learning
projects typically have NO explicit accessibility work. Before
committing to any of these:

1. Grep the repo for `accessibilityLabel` / `accessibilityIdentifier`
   / `XCUIElementType` references. Frequency indicates AX care.
2. Build the app for simulator (~30 min – 2 hr per repo).
3. Install via `simctl install` on a clone of our baseline.
4. Dump AX tree on each screen via the existing `AXReader`.
5. Compare structure to what a real Apple-equivalent agent would
   need.

~2-4 hours per app to verify. **Don't sink that cost until we
actually need the app for the benchmark.**

## When to revisit

Trigger this analysis again when:

- **Agent training plateaus** on the in-hand app set and we need
  app diversity for generalization. OSS additions (Simplenote +
  ProtonMail + Readest + Calculator + Clock) would add 5 well-
  validated apps with diverse UI patterns.
- **Benchmark scope reframes** from "iPhone-as-shipped" to "iOS
  apps in general" — OSS additions become baseline rather than
  optional.
- **iOS major version releases**. Apple has been known to add
  back previously-stripped apps (Games + Preview in iOS 26,
  Passwords in iOS 18). Re-run `simctl listapps` after each
  major iOS upgrade — `IOS_SIM_QUIRKS.md` §9 has the
  one-shot audit command.

## Maximum reachable app universe (if we exhaust everything)

- 21 installed Apple apps (currently 5 onboarded, 11+ ready for
  onboarding, 1 deferred)
- + 3 strong-AX OSS additions (Simplenote, ProtonMail, Readest)
- + 2 self-built (Clock, Calculator)
- − 1 excluded for being mostly-AX-invisible (Freeform)
- = **~25 apps**, which compares favorably with AndroidWorld's 20
  apps and well above OSWorld's 9.

This is more than the original design called for. We have plenty
of runway before app diversity becomes the limit.
