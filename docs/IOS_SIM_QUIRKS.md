# iOS Simulator quirks

Behaviors of the iOS Simulator / `simctl` / TCC / Xcode that have
bitten SIBB during development. Each entry names the iOS version
on which we observed it, the symptom, the workaround, and what to
re-test when iOS / Xcode update — most of these are version-sensitive
and may quietly change shape between releases.

When you hit a baffling new failure on a new iOS version, **start
here first**. Most of these have no public documentation; Apple
Forums + trial-and-error built this list.

Format:
- **Symptom** — how it manifests
- **Surface** — what code path
- **Observed on** — iOS / Xcode version we hit it
- **Workaround** — current fix + file/line pointer
- **Revisit on** — what version bump might change behavior

---

## 1. `kTCCServicePhotos` doesn't survive `simctl clone`

- **Symptom**: After `simctl clone <baseline> <clone>`, the clone's
  `TCC.db` shows `kTCCServicePhotos | <bundle> | 0` (denied) even
  though the baseline had it `=2` (granted). Other services on the
  same row stay granted across the clone.
- **Surface**: `PHPhotoLibrary.requestAuthorization(for: .readWrite)`
  returns `.notDetermined` or `.denied` on cloned sims. Manifests
  as `wipe_photos` / `list_photos` failing with
  `"no photos permission for test runner"`.
- **Observed on**: iOS 26.3, Xcode 26.3. Empirically verified by
  diffing `sqlite3 TCC.db` between baseline and clone.
- **Workaround**: `sibb_baseline.acquire_clone()` re-runs
  `ensure_runner_permissions()` after every clone-boot. Idempotent
  (~1-2 s) and re-grants the dropped Photos service.
- **Revisit on**: every iOS minor (Apple has shipped TCC clone-
  preservation changes silently in `.x.1` updates before). Test:
  `SELECT auth_value FROM access WHERE service='kTCCServicePhotos'`
  on a baseline vs its clone. If the clone shows `=2`, the
  workaround is no longer needed — remove the re-grant call.

---

## 2. PhotoKit needs explicit `NSPhotoLibrary*UsageDescription` keys

- **Symptom**: `PHPhotoLibrary.requestAuthorization(for: .readWrite)`
  returns `.denied` immediately even when `TCC.db` says
  `kTCCServicePhotos | <bundle> | 2`. No system dialog shows.
- **Surface**: PhotoKit read/write inside the XCUITest runner.
- **Observed on**: iOS 17+, Xcode 15+ (tested on Xcode 26.3).
- **Workaround**: explicit `INFOPLIST_KEY_NSPhotoLibraryUsage*` in
  the runner's project.yml (`sibb_xcuitest_setup.sh:1949-1961`).
  Xcode auto-injects many usage descriptions for test bundles but
  PhotoKit's pair (`NSPhotoLibraryUsageDescription` for read+write,
  `NSPhotoLibraryAddUsageDescription` for add-only) isn't reliably
  auto-injected — set both explicitly.
- **Revisit on**: Xcode major versions. Apple has expanded the
  auto-injection list before; if Xcode N+1 starts auto-injecting
  these, the explicit keys are still harmless. If Apple ADDS a
  new key (e.g. `NSPhotoLibraryEditUsageDescription`), this list
  will need updating.

---

## 3. iOS 17+ "Delete N items?" confirmation on PhotoKit deletion

- **Symptom**: `PHPhotoLibrary.shared().performChanges` with
  `PHAssetChangeRequest.deleteAssets` calls back with
  `success=false, error=nil` ("delete failed: unknown" in our
  Swift response) even with full TCC + Info.plist permissions.
- **Surface**: `wipe_photos` Swift command.
- **Observed on**: iOS 17+. iOS 16 deleted silently.
- **Workaround**: After issuing `performChanges`, poll
  `SpringBoard.alerts.firstMatch` for up to 10 s; tap any button
  whose label `BEGINSWITH 'Delete'` (the label varies with the
  count: "Delete Photo", "Delete Photos", "Delete 3 Items").
  Implemented in `sibb_xcuitest_setup.sh`'s `wipe_photos` case.
- **Revisit on**: iOS minor versions. The button-label format
  has been stable since iOS 17.0 but Apple has changed Photos
  delete-confirmation copy in beta builds.

---

## 4. `kTCCServiceXxx` doesn't auto-invalidate SpringBoard's cache
(the B3 race)

- **Symptom**: `simctl privacy grant <service> <bundle>` writes
  `TCC.db` correctly (`SELECT auth_value` shows `=2`) but the
  runner's first `requestAccess` call returns `granted=false`.
  ~50% flake under parallel xcodebuild load; ~25% solo.
- **Surface**: every EventKit / Contacts / PhotoKit auth call
  inside the runner.
- **Observed on**: iOS 17+; race window widens under parallel
  `xcodebuild test-without-building` because SpringBoard's
  natural cache-refresh cadence is slower than the runner's first
  framework call.
- **Workaround**: three layers in
  (a) `launchctl
  kickstart -k system/com.apple.SpringBoard` after every TCC grant
  (`sibb_simctl.restart_springboard`), (b) Swift-side retry loop
  in `acquireReminderStore` / `acquireEventStore` /
  `acquireContactStore` / `ensurePhotoLibraryAccess`, (c) [F1
  RESOLVED] clone-from-baseline removes per-episode prewarm so the
  race window only opens once during baseline build, not per worker.
- **Revisit on**: iOS minor versions. The B3 fix was verified 5/5
  on iOS 26.3. If Apple ships a TCC daemon refactor (every few
  iOS majors), SpringBoard restart may become unnecessary OR may
  stop working — re-verify the parallel L2 with the SpringBoard
  restart commented out.

---

## 5. `NSRemindersFullAccessUsageDescription` not auto-injected

- **Symptom**: `EKEventStore.requestFullAccessToReminders` shows
  a user-facing prompt on iOS 17+ even with TCC granted — UI test
  hangs waiting for the prompt to resolve.
- **Surface**: Reminders write access from the runner.
- **Observed on**: iOS 17+. Reminders permission split off from
  Calendar in iOS 17; Xcode's auto-injection list wasn't updated.
- **Workaround**: explicit
  `INFOPLIST_KEY_NSRemindersFullAccessUsageDescription` in the
  runner's project.yml. We additionally use the legacy
  `requestAccess(to: .reminder)` path which only needs
  `NSRemindersUsageDescription` (which IS auto-injected).
- **Revisit on**: Apple deprecating the legacy `requestAccess`
  path (currently still works on iOS 26). ---

## 6. In-app onboarding flows aren't plist-suppressible

- **Symptom**: Reminders' "Welcome to Reminders" screen and "Enable
  iCloud Syncing?" prompt show on first launch even after writing
  every plausible suppression key (`HaveShownWelcomeExperience`,
  `HasShownFirstLaunch`, `DidShowCloudKitMigrationDialog`,
  `HaveShownWelcomeExperience` in the AppGroup container, etc.).
- **Surface**: Any UI test that activates Reminders. Framework
  calls (`EKEventStore`) work fine because they bypass the UI;
  the welcome state only matters for AX-tree-based tasks.
- **Observed on**: iOS 26.3. The Welcome state key
  (`lastSeenWelcomeScreenVersion` in `group.com.apple.reminders`)
  CAN be set via `simctl spawn defaults write`, but the iCloud
  upgrade prompt is time/state-based, not persistently
  plist-controlled — there's no key that suppresses it.
- **Workaround**: `dismiss_app_onboarding` Swift command in
  `sibb_xcuitest_setup.sh`. Activates each SIBB-11 bundle and
  walks the AX snapshot for dismiss-button labels (Continue / Not
  Now / Skip / Done / OK / Get Started / Maybe Later / Cancel /
  No Thanks / Later / Dismiss), up to 6 chained dialogs.
  Run during `ensure_baseline_sim()` after `sibb_prewarm.sh`.
- **Revisit on**: iOS minor versions for label-text changes.
  Major-version onboarding redesigns may add labels we don't
  match (e.g. "Got It", "Start"). Symptom would be: cloned sims
  start showing dialogs that weren't there before. Add new labels
  to the Swift label set.

---

## 7. `simctl clone` flakes intermittently on the same machine

- **Symptom**: `xcrun simctl clone <src> <name>` returns
  `Failed to clone device` on a syntactically valid invocation.
  Retrying immediately succeeds. ~5% rate observed during D1b
  parallel-orchestrator development.
- **Surface**: Every clone-from-baseline call. Without retry, a
  parallel batch occasionally fails one episode.
- **Observed on**: All iOS versions tested. Documented by Apple in
  [Developer Forums #713921](https://developer.apple.com/forums/thread/713921).
- **Workaround**: `simctl_clone()` retries up to 3 times with
  linear backoff (`sibb_simctl.py:simctl_clone`).
- **Revisit on**: Apple acknowledging or fixing the issue. As of
  2026-05 there's no fix announcement.

---

## 8. `PHPhotoLibrary.requestAuthorization` auth-dialog labels are
Photos-specific

- **Symptom**: First call to PhotoKit auth shows a system alert
  with buttons "Allow Full Access" / "Limit Access" / "Don't
  Allow". The generic dismiss labels (Allow / OK / Continue /
  Allow Once) don't match — `dismissPermissionDialogs` doesn't tap
  the right button and the auth call returns `.notDetermined`.
- **Surface**: PhotoKit auth retry path on first runner session
  for a given clone.
- **Observed on**: iOS 14+ (when Apple introduced the limited-
  access option).
- **Workaround**: `dismissPermissionDialogs` label set in
  `sibb_xcuitest_setup.sh` extended with "Allow Full Access" and
  "Allow Access to All Photos". Note: we deliberately do NOT
  include "Limit Access" — partial access doesn't support
  deletion, which `wipe_photos` requires.
- **Revisit on**: iOS Photos UI rewrites. The button-label format
  has been stable since iOS 14 but Apple has tweaked the copy.

---

## 9. Many .app bundles exist in the runtime image but are gutted stubs

- **Symptom**: `ls .../RuntimeRoot/Applications/` shows ~250
  .app bundles including Mail (`MobileMail.app`), Notes
  (`MobileNotes.app`), Clock (`MobileTimer.app`), Music,
  Podcasts, Books, Camera, FaceTime, Calculator, Freeform,
  Journal, etc. None can be launched —
  `xcrun simctl launch <udid> com.apple.<name>` returns
  `FBSOpenApplicationServiceErrorDomain code=4`.
- **Why** (the actual underlying state, not just "Apple doesn't
  register them"): the .app bundles are **stripped to just the
  localization resources**. Confirmed by directory diff:
  ```
  Reminders.app (installed):
    Reminders.app/_CodeSignature/
    Reminders.app/Assets.car
    Reminders.app/Info.plist
    Reminders.app/AppIcon*.png
    Reminders.app/<binary>
    Reminders.app/ar.lproj/  bg.lproj/  ... (localizations)
  MobileNotes.app (in runtime, unlaunchable):
    MobileNotes.app/Extensions/
    MobileNotes.app/ar.lproj/  bg.lproj/  ... (localizations only)
    # No Info.plist, no binary, no Assets.car, no signature.
  ```
  Apple ships only the `.lproj` directories — presumably to power
  system-wide UI strings (Spotlight result labels, share-sheet
  target names) without shipping the real apps. Trying to install
  via `simctl install <path-to-stub.app>` fails with
  `IXErrorDomain code=13: Missing bundle ID` because there's no
  Info.plist to read from.
- **Surface**: any code path that assumes a given Apple app is
  available on iOS sim. Mail / Music / Camera are the famous ones;
  Notes / Clock / Books / Podcasts are deceptively close (in the
  runtime directory listing, but the bundles are stubs).
- **Observed on**: iOS 26.3. Behavior is identical on iOS 17+ per
  Apple Community threads going back years.
- **Workaround**: **none viable for SIBB**. The bundles are
  incomplete, so there's literally nothing to install. The only
  theoretical paths are deeply unsupported:
  1. Extract a complete `.app` from a real iOS device backup
     (firmware decryption / jailbroken extraction).
  2. Reconcile architectures (real-device binaries are
     `arm64-apple-ios`, simulator wants `arm64-apple-ios-simulator`
     on Apple Silicon, `x86_64-apple-ios-simulator` on Intel —
     incompatible without re-linking).
  3. Bypass code-signing (the sim is more lenient than a real
     device but not entirely absent).
  Each step has multiple ways to fail and breaks across iOS
  versions. Treat the ~21 user-facing apps from `simctl listapps`
  as the hard ceiling for SIBB purposes.
- **Revisit on**: every iOS major version. Apple periodically
  adds (Games + Preview added in iOS 26 ;
  Passwords added in iOS 18) or removes apps from the
  simulator's installed set. Re-run
  `/tmp/sibb_list_apps.py <udid>` after each iOS upgrade to
  audit the delta.

### What's actually installed on iOS 26.3 simulator (21 apps)

Confirmed via `simctl listapps` on a fresh baseline:

```
Calendar, Contacts, Files, Fitness, Health, Maps, Messages, News,
Passwords, Photos, Preview, Reminders, Safari, Settings,
Shortcuts, Wallet, Web, Pride, Unity, Weather Wallpaper,
SIBBTests-Runner
```

(Plus the three Posters apps which are lock-screen wallpapers, not
real apps — and SIBBTests-Runner is our test harness.)

### What's in the runtime but NOT installed (intentional Apple gap)

Confirmed via launch attempts on iOS 26.3:

```
Mail, Notes, Clock, Music, Podcasts, Books, Camera, FaceTime,
Calculator, Freeform, Journal, AppStore, AppleTV, Image
Playground, Photo Booth, Magnifier, Measure, Compass, Tips,
Voice Memos, Stocks, Weather, Find My, Home, Sports
```

Some have plausible reasons (Camera/FaceTime → hardware), most
don't.

---

## 10. HealthKit on simulator: separate DB, no `simctl` grant path

- **Symptom**: `HKHealthStore.requestAuthorization(toShare:read:)`
  returns silently denied (or never triggers a usable consent UI)
  on iOS simulator. `simctl privacy` has no `health` service —
  `xcrun simctl help privacy` lists: all, calendar, contacts*,
  location*, photos*, media-library, microphone, motion, reminders,
  siri. Health is absent.
- **Why**: HealthKit stores authorization in its own SQLite
  database (`data/Library/Health/healthdb_secure.sqlite`), NOT in
  TCC.db. There's nothing for `simctl privacy` to write to. The
  consent flow that DOES work on a real device shows a sheet
  INSIDE the requesting app (not on SpringBoard) listing each
  sample type with toggles, which is hard to drive headlessly.
- **Surface**: any HealthKit access from the test runner.
  `HealthHandler` in `sibb_state.py` is currently sim-limited
  because of this.
- **Observed on**: iOS 26.3. Open since iOS 10+ per multi-year
  GitHub issues. Apple Developer documentation explicitly says:
  *"The simulator has no Health data and you should always test
  on a real iPhone."*
- **Workaround**: **none ready**. Two paths exist on paper, neither
  shipped:
  1. **Runner-side dialog tap-through**: extend
     `dismissPermissionDialogs` (or add a Health-specific helper)
     to walk the runner's own `XCUIApplication` snapshot (not
     SpringBoard's) for the consent sheet — "Turn On All",
     "Allow" buttons. Risk: sheet layout varies per iOS version.
  2. **Direct `healthdb_secure.sqlite` writes**: pre-populate the
     authorization tables for the runner bundle. Risk: undocumented
     schema, breaks on iOS updates.
- **Status**: HealthHandler scaffold is complete (L1+L1.5 pass)
  but L2 sim tests are skipped. Re-enable when one of the
  workarounds above is implemented OR when Apple ships proper
  simctl support.
- **Revisit on**: every Xcode major. Apple has expanded simctl
  capabilities over time (privacy gained services, media inject
  via addmedia, etc.) — Health may eventually join.

**Refs**:
- [Apple HealthKit auth docs](https://developer.apple.com/documentation/healthkit/authorizing-access-to-health-data)
- [wix/AppleSimulatorUtils #26](https://github.com/wix/AppleSimulatorUtils/issues/26)
- [Apple Developer Forums — HealthKit auth on sim](https://developer.apple.com/forums/thread/80735)

---

## 11. Messages.app: multi-store architecture defeats sms.db injection

- **Symptom**: SQL-inserting a conversation + handle + message into
  `data/Library/SMS/sms.db` succeeds (we have the trigger drop-and-
  restore pattern working for the chat-table UDF triggers), but
  Messages.app does NOT display the seeded conversation in its UI
  list. Meanwhile, the simulator's Messages screen DOES show
  "conversations" with phone numbers like `+1 (888) 555-1212` and
  `+1 (555) 564-8583` — which on inspection are sample contacts
  from the iOS sim's bundled AddressBook (Kate Bell, Anna Haro,
  Hank M. Zakroff, John Appleseed, et al.), rendered as
  contact-suggestion cells in the same `ScrollArea 'Conversations'`
  AX container. Indistinguishable from real conversation cells at
  the AX layer.
- **Can we exploit those phantom cells by injecting our own contacts?**
  **No** — tested 2026-05-16. Created custom contacts (Alice
  Wonderland +15550112233, Bob Builder +15559876543) via
  `CNContactStore` on a fresh baseline clone, then launched
  Messages. The same two hardcoded phantom cells (Kate Bell's +1
  (888) 555-1212 and Anna Haro's +1 (555) 564-8583) still appear;
  our custom contacts do NOT. So Messages' empty-state UI is
  hardcoded to those two specific numbers, not dynamically rendered
  from AddressBook. Contact injection is not a backdoor.
- **Why**: iOS Messages reads from MULTIPLE stores:
  - `sms.db` — the SMS/conversation store we tried.
  - `IMDPersistence.framework`'s identity-based conversation
    database — a separate store managed by `IMDPersistenceAgent`
    XPC service. Conversations need to be cross-registered here
    OR the Messages app filters them out of its display.
  - iMessage account state — without a configured Apple ID + Sign-in,
    iMessage features (which gate most of the conversation display
    logic on iOS 17+) are not active.
  Apple has architecturally split Messages internals across
  these frameworks deliberately to make app-level injection
  difficult — this is a security feature, not an oversight.
- **What works empirically**:
  - We CAN drop the chat-table UDF triggers (verify_chat_insert,
    verify_chat_update, before_deleting_chat_delete_chat_background_trigger,
    before_delete_chat_update_sync_chat_deletes, after_delete_on_chat)
    plus the message-table `after_delete_on_message_plugin` trigger,
    do INSERTs into `handle` / `chat` / `chat_handle_join` / `message`
    / `chat_message_join`, then re-create the triggers from saved
    DDLs. The inserts succeed and the rows are present.
  - We CAN read `sms.db` for verification — for tasks that involve
    the agent sending a message via UI compose, we could in principle
    verify the outbound message appeared in the DB.
- **What doesn't work**:
  - Outbound send actually delivering — no telephony hardware
    (`com.apple.MobileSMS` queues the message but it never sends).
  - Pre-seeded inbound messages displaying in the Messages UI —
    sms.db inserts go through but Messages filters them at the UI
    layer pending the IMDPersistence cross-registration.
- **Surface**: any SIBB task involving Messages content (read
  existing thread, see incoming message, etc.). Pure-UI tasks
  (navigate to compose, type text, tap Send) work at the AX layer
  but the resulting message goes nowhere.
- **Observed on**: iOS 26.3. The multi-store split started around
  iOS 15-16 with the iMessage Apps / Tapback rewrite and is more
  entrenched in newer versions.
- **Workaround**: **none ready**. Two paths exist:
  1. **Reverse-engineer IMDPersistenceAgent's database**: find
     where it stores its conversation registrations, write to it
     alongside `sms.db`. High research cost (undocumented schema,
     XPC interaction patterns), iOS-version brittle.
  2. **Configure an iMessage account on the sim**: sign in with an
     Apple ID that has iMessage enabled. Doable manually but not
     scriptable; couples SIBB to per-machine credentials.
- **Status**: Messages handler deferred. SIBB-11's "Messages" item
  remains uncovered.
- **Revisit on**: Apple shipping an Xcode-supplied iMessage sandbox
  account for sim (would resolve the account-setup wall), OR
  community reverse-engineering of IMDPersistence's store.

- **Loopback workaround (2026-05-24, empirically usable)** — when
  you UI-compose+send a message in a JA/KB phantom thread, the
  simulator's IDS-firewall (no-account fallback) immediately
  re-emits the same text as a synthetic INBOUND from "unknown
  sender, report spam?" — landing on the OTHER demo thread. So:
  send to JA, the same text appears as gray bubble in KB; send to
  KB, lands in JA. The mechanism is the no-account fallback, NOT a
  daemon we control. State is **in-memory only**: nothing persists
  to `sms.db` — system log (`var/db/diagnostics/Persist/*.tracev3`)
  is the only on-disk trace of the marker text. Verifier can't
  read the bubble; it has to verify the agent's downstream actions
  (the contact they created, the Maps history they wrote, etc.).
- **Loopback lifetime** (probe v2, 8 scenarios on iOS 26.3 sim):

  | Scenario | Bubble survives? |
  |---|---|
  | Foreground idle | ≥5 min |
  | Home-press background | ≥2 min |
  | Background to another single app | ≥2 min |
  | Cycle through 4 apps (40 s) | yes |
  | `simctl terminate com.apple.MobileSMS` | **LOST immediately** |
  | App-switcher swipe-up gesture (XCUITest) | inconclusive — gesture unreliable on iOS 26 sim |

  Only `simctl terminate` reliably evicts the bubble. The prior
  "30-60 s idle decay" report was a probe artifact (navigation
  tapped the title-bar phone-number button which opens Contact
  Details; the bubble was still in the thread, just not on the
  Details screen).
- **HARD CONSTRAINT for any SIBB pipeline that uses the loopback**:
  do NOT `simctl terminate com.apple.MobileSMS` between the
  pre-runner send and the verifier. Audit confirmed there's no
  such termination call in the current codebase (only Safari is
  ever terminated). Any future cleanup code that adds blanket
  app-termination must whitelist Messages.
- **Shared preview-slot quirk** — when JA receives a sent message,
  BOTH JA and KB inbox cells display the same preview text (e.g.
  `"+1 (888) 555-1212, MARKER, 7:40 PM"` AND `"+1 (555) 564-8583,
  MARKER, 7:40 PM"`). The two phantom cells share one "last-sent"
  slot in the empty-state UI. Verifier cannot treat cell-1 and
  cell-2 as independently addressable inbox rows.
- **Probe script**: `sibb/simulator/sibb_probe_messages_lifetime.py`
  (the staleness-matrix probe v2) preserved for regression.

**Refs**:
- [IMDPersistence.framework runtime headers — nst/iOS-Runtime-Headers](https://github.com/nst/iOS-Runtime-Headers/tree/master/PrivateFrameworks/IMDPersistence.framework)
- [iOS sample contacts (Kate Bell etc.) — Apple Community thread](https://discussions.apple.com/thread/256226383)
- SIBB viability investigation 2026-05-16: ran clone+plant+SQL-inject
  against fresh baseline clone with full UDF-trigger drop list;
  inserts succeeded but Messages.app didn't surface the seeded
  conversation. Phantom cells visible were AddressBook-sourced
  contact suggestions.

---

## 12. News.app: partial functionality on simulator

Capability investigation 2026-05-16 — running on a fresh
baseline clone, no iCloud sign-in, sim network = host network.

**What works:**
- News.app launches and shows real cached headlines on the Today
  feed — actual articles from The Wall Street Journal, The
  Associated Press, Bloomberg, etc. These appear in the AX tree
  as `Other` elements with labels of the form
  `"<source>, <title>"`.
- Tab bar navigation: Today / News+ / Audio / Following / Search.
  All four tabs reach their initial view without errors.
- Search bar opens; queries can be typed (return results depend
  on network/sign-in state).
- `applenews://` URL scheme: opens News; routing to specific
  channels via `applenews:///channel/<id>` returns rc=0 but the
  resulting view may fail to populate.

**What's unreliable:**
- Tapping a Today-feed article cell to enter article detail
  often shows "Cannot Connect / Retry". Cause unclear — could be
  paywall (News+), missing iCloud sign-in gating free article
  fetch too, or just cached metadata without cached body. Some
  free articles may work; we don't have a reliable signal for
  which.
- The `Following` tab is empty on a fresh clone (nothing's been
  subscribed/saved).

**What's blocked for programmatic state inject:**
- The user's saved articles live in a binary plist at
  `Containers/Data/Application/<App-UUID>/Documents/com.apple.news.public-com.apple.news.private-production/reading-list`.
- The file has a **6-byte header wrapper** (`00 07 08 03 1a 2a`)
  prepended to a standard `bplist00`. Semantics of the wrapper
  bytes are undocumented. Even if we wrote a correctly-wrapped
  plist, we'd need valid Apple-News-server-issued article IDs
  (format `rl-<hex>_`) which we can't mint locally.
- Companion file `reading-list-commands` (same wrapper) contains
  pending save/unsave commands queued for server sync.
- `myArticlesRequest` (XML plist) is the server-bound query
  used to populate the Following → Saved view.

**Filesystem state to be aware of:**
```
data/Library/News/com.apple.news.public-com.apple.news.private-production/
  referralItems/com.apple.news.widget/    # widget-rendered article data
  actionQueue                              # save/share commands

data/Containers/Shared/AppGroup/<UUID>/com.apple.news.public-com.apple.news.private-production/
  article_exposures                        # SQLite — tracks user taps
  appConfigurationMirror                   # remote config snapshot
  myArticlesRequest                        # pending server request
  tabi/                                    # ML model cache

data/Containers/Data/Application/<App-UUID>/Documents/com.apple.news.public-com.apple.news.private-production/
  reading-list                             # 6-byte wrapper + bplist (saves)
  reading-list-commands                    # 6-byte wrapper + bplist (queue)
```

**Handler implications (sibb_state.NewsHandler v1):**
- No apply primitive — there's nothing reliable to seed.
- No reset — feed snapshot is server-curated and re-issued on
  next launch.
- `news.headlines` resource fetcher scrapes the current view's AX
  tree (`Other` elements with `<source>, <title>` labels,
  filtering out scroll-bar noise). Works for Today feed AND
  category landing pages (same label format).
- `news.recipes` resource fetcher scrapes the Recipe Catalog
  view's `<source>, RECIPE, <duration?>, <title>` labels.
  Caller must navigate UI to Search → Food → Recipe Catalog
  before invoking — the fetcher only observes, doesn't drive.

**Tasks that work with v1:**
- "Find an article about <topic> in the Today feed" — match
  against `news.headlines` rows.
- "Find a recipe in the Food category" — navigate UI, then
  match against `news.recipes` rows (filterable by source,
  duration, title substring).
- "Read a Food article" — Food articles open reliably with
  full body; agent can scroll + extract content from AX.
- "Switch to <X> tab" / "Browse <X> category" — UI navigation
  + AX verification.

**Tasks that need v2:**
- "Save the article/recipe X" — Save button typically lives
  in More menu; we haven't confirmed end-to-end save → server
  sync works on sim. reading-list binary plist with the 6-byte
  wrapper is still the underlying store.
- "Search News for keyword X" — search endpoint unavailable
  on sim ("Search Unavailable").
- "Subscribe to channel X" — needs iCloud sign-in.

**Refs**:
- [RhetTbull/apple-news-to-sqlite](https://github.com/RhetTbull/apple-news-to-sqlite)
  — macOS implementation; documents the bplist (post-wrapper)
  schema fields but doesn't address the iOS 6-byte wrapper.
- [Dave Bullock's reading-list reverse engineering](https://eecue.com/blog/reverse-engineering-read-later-data-from-the-apple-news-app)
  — same caveat (macOS, not iOS).

### News persistence map (verified 2026-05-17)

Investigation drove News through view → save → grocery-list →
sub-view navigation, diffing the filesystem at each step. Result
is a complete map of where state lives.

**News Documents directory** (`data/Containers/Data/Application/<App-UUID>/Documents/com.apple.news.public-com.apple.news.private-production/`):

| File | Size when empty | Purpose | Programmatic? |
|---|---|---|---|
| `reading-list` | 48 bytes | Saved Stories (Following → Saved Stories) | Wrapped bplist (6-byte header) — not parseable without RE |
| `reading-list-commands` | 71 bytes | Pending save/unsave ops, server-bound | Same wrapper |
| `history` | 48 bytes | Following → History UI | Same wrapper |
| `history-commands` | 71 bytes | Pending history ops | Same wrapper |
| `audio-playlist` | 48 bytes | Audio tab playlist | Same wrapper |
| `audio-playlist-commands` | 71 bytes | Pending audio ops | Same wrapper |
| `issue-history` | 48 bytes | News+ magazine issue history | Same wrapper |
| `personalization-data` | 48 bytes | User personalization state | Same wrapper |
| `personalization-data-commands` | 71 bytes | Pending personalization ops | Same wrapper |
| `personalization-sessions/` | (directory) | Per-session personalization logs | — |
| `BundleSubscription` | ~43 KB | iCloud bundle subscription state | — |
| `aggregate_store` | 107 bytes | **JSON** — ML feature aggregates (decayRate, base64-encoded float vectors) | ✅ JSON-parseable; decay model state |
| `actionQueue` | ~520 bytes | NSKeyedArchiver bplist of pending actions | ✅ NSKeyedArchiver is decodable in Python (`plistlib` + `bplist00`) |

**News Caches directory** (`data/Containers/Data/Application/<App-UUID>/Library/Caches/News/com.apple.news.public-production-143441/`):

| File | Size | Purpose | Programmatic? |
|---|---|---|---|
| `recipes-inventory.db` | ~8.4 MB | **SQLite — 2520 recipes** (table `RecipeItem` with id/DefaultScore/dates/SurfacedBy/PBData blob) | ✅ IDs + scores + dates queryable; **titles/ingredients are inside PBData protobuf** (Apple's schema not public) |
| `food-hub-feed-offline-db` | (SQLite) | Offline cache for Food hub | Likely SQLite |
| `recipe-catalog-feed-offline-db` | (SQLite) | Offline cache for Recipe Catalog | Likely SQLite |
| `audio-feed-offline-db` | (SQLite) | Offline cache for Audio feed | Likely SQLite |
| `magazine-feed-offline-db` | (SQLite) | News+ magazine offline cache | Likely SQLite |
| `puzzle-hub-feed-offline-db` | (SQLite) | Puzzles tab offline cache | Likely SQLite |
| `search-hub-feed-offline-db` | (SQLite) | Search results cache | Likely SQLite |
| `offline-manifest-saved-feed` | 193 bytes (empty) | **JSON** — saved-stories manifest with `recordIDs` array | ✅ JSON-parseable; populated as saves succeed |
| `offline-manifest-recipe-box-feed` | 193 bytes (empty) | **JSON** — saved-recipes manifest | ✅ JSON-parseable |
| `offline-manifest-history-feed` | 193 bytes (empty) | **JSON** — history feed manifest | ✅ JSON-parseable |
| `offline-manifest-{food-hub,recipe-catalog,audio,magazine,puzzle-hub,search-hub,today,sports,shared-with-you}-feed` | 193 bytes (empty) | JSON manifests for each feed type | ✅ |
| `issue-article-inventory` | ~18 MB | Article inventory for News+ issues | Format unknown |

**App Group container** (`data/Containers/Shared/AppGroup/<AppGroup-UUID>/com.apple.news.public-com.apple.news.private-production/`):

| File | Purpose | Programmatic? |
|---|---|---|
| `article_exposures` | **SQLite** — `ItemExposure` table tracks taps/views | ✅ Plain SQLite; **rows grow per recipe/article viewed** (verified 0→2 after one view) |
| `myArticlesRequest` | **XML plist** — server-bound feed-list query | ✅ Plain XML plist |
| `appConfigurationMirror` | Remote config snapshot | Binary |
| `tabi/models/` + `tabi/resources/` | On-device ML models (recipe scoring, tag scoring, diversity) | CoreML `.mlmodelc` — not introspected |

### Reminders integration (Add to Grocery List)

- **The "Add to Grocery List" action does NOT write to Reminders'
  Core Data on a fresh simulator.** Verified by diffing all three
  Reminders SQLite stores (`Data-local.sqlite`,
  `Data-<UUID-1>.sqlite`, `Data-<UUID-2>.sqlite`) before and after
  the action. Zero new reminders, zero new lists.
- **On a real device with iCloud signed in**, News reportedly does
  add a reminder whose title is the Apple News article URL. The
  flow uses CloudKit-mediated sync between News and Reminders.
- **Implication**: News→Reminders cross-app verification doesn't
  work in SIBB sim environment. Same shape as the
  Messages multi-store issue (§11) — Apple's apps integrate via
  iCloud-backed services that no-op without sign-in.
- **News-internal grocery state location remains unknown** on
  sim — the action doesn't seem to write to any of the locally
  inspectable files either. May be queued in
  `reading-list-commands` (wrapped bplist) or `actionQueue`
  (NSKeyedArchiver bplist) for eventual server submission.

### Share button

- Recipe detail view's `Button 'Share'` and More menu's
  `Button 'Share Recipe'` are present in the AX tree (Share is
  the standard iOS share sheet).
- **Did not successfully probe the share-sheet AX tree in this
  investigation** — the recipe detail view dismisses too eagerly
  when the More menu opens, and follow-up taps land on wrong
  surfaces. Worth a focused retry.
- **Share targets on a simulator** are typically very limited:
  Copy, Mail (if installed — Mail is not, see §9), AirDrop (no
  peers), Messages (deferred §11). On a real device the share
  sheet has the standard rich set.

### Programmatic verification surfaces for News

Now that the map is complete, the realistic verifier-AFTER paths:

| Verifier check | Surface |
|---|---|
| "Agent opened a recipe / article" | `article_exposures.ItemExposure` (SQLite, plain) |
| "Agent saved a story" | `offline-manifest-saved-feed.recordIDs` (JSON), once save propagates |
| "Agent saved a recipe" | `offline-manifest-recipe-box-feed.recordIDs` (JSON) |
| "Agent navigated to History tab" | `offline-manifest-history-feed.recordIDs` populates |
| "Agent's view triggered behavior aggregates" | `aggregate_store` JSON — `aggregates.f0` base64 float vector decay |
| "Agent added recipe to Grocery List" | ❌ no local surface verified on sim; iCloud-dependent |
| "Agent shared content" | ❌ share-sheet outcome not yet probed |
| "Agent opened recipe titled X" | ⚠️ no programmatic title→ID mapping; AX-observe nav-bar title at agent's stopping state |

---

---

## 13. Passwords.app: keychain SQL countable, writes entitlement-blocked

Investigation 2026-05-17 against iOS 18+ Passwords.app
(`com.apple.Passwords`), the standalone password manager that
split out of Settings → Passwords.

**Architecture:**
- App is a UI on top of the system Keychain. Storage is
  `data/Library/Keychains/keychain-2-debug.db` (SQLite).
- Two relevant tables:
  - `inet` — internet passwords (websites, app logins)
  - `genp` — generic passwords (metadata, system entries)
- Items are AES-encrypted at rest. The `data` BLOB is encrypted
  with a key derived from the device passcode; sim with no
  passcode uses a fixed device-UUID-derived key. We can't decrypt
  without that key.
- **No SQL triggers** that call user-defined functions (unlike
  Messages §11) — direct SQL writes don't fail with "no such
  function". But see access-group caveat below.

**Adding a password via the UI (verified 2026-05-17):**
The New Password form creates **3 new `inet` rows + 7 new `genp`
metadata rows** across multiple access groups:

| Access group | Purpose |
|---|---|
| `com.apple.cfnetwork` | Safari AutoFill |
| `com.apple.password-manager` | Passwords-app-visible entries |
| `com.apple.password-manager.password-evaluations` | Security audit / reused-password detection |
| `apple` (in `genp` table) | Metadata wrappers (sync state, share state) |

The Passwords-app's UI list reads from `com.apple.password-manager`.

**Why the test runner can't write programmatically:**
- `kSecAttrAccessGroup = "com.apple.password-manager"` is reserved
  for system apps via entitlement. Our test runner bundle
  (`com.sibb.tests.xctrunner`) lacks the entitlement.
- `SecItemAdd` from the runner with that access group fails with
  `errSecMissingEntitlement` (-34018).
- Writing to the SQLite directly is also impractical: the `data`
  BLOB needs to be encrypted with the system key, which we don't
  have.

**Auth flow on simulator:**
- First launch shows Welcome screen with Continue, then a
  Notifications permission alert, then a Lock screen
  (`StaticText 'Passwords Is Locked'` + `Button 'Unlock'`).
- **Tapping Unlock auto-succeeds on sim with no passcode set** —
  no Face ID matching required. The lock overlay sticks around
  visually but content is accessible underneath.
- The 3 pre-seeded Wi-Fi passwords in the Wi-Fi category are
  Apple's bundled simulator test data.

**v1 PasswordsHandler scope:**
- Bundle registration + canonicalization.
- No apply primitive (entitlement-blocked).
- No reset (clone-from-baseline; documented gap).
- `passwords.entry_count` resource fetcher counts `inet` rows by
  access group — doesn't decrypt, just counts. Verification
  pattern: take a count before agent acts, take another after,
  compare delta.
- `passwords.entry_exists` resource fetcher — see "Hash-equality
  verification" below; stronger than count, doesn't need a
  before/after baseline.

**Hash-equality verification (verified 2026-05-17):**

The `data` BLOB is encrypted, but the **`acct` and `srvr` columns
are 20-byte SHA-1 hashes of the plaintext values**, stored
unencrypted as a lookup index. Verified empirically:

```
UI input "UNIQUSERNAMEDEF"          → acct = 9AA9C248E984BD940940A49F530E9CB8619BB890
SHA-1("UNIQUSERNAMEDEF")            =        9AA9C248E984BD940940A49F530E9CB8619BB890  ✅
UI input "uniqsite-jkl.example.com" → srvr = 796662CA71132F46A0935EFAA7B86DE40589ABC7
SHA-1("uniqsite-jkl.example.com")   =        796662CA71132F46A0935EFAA7B86DE40589ABC7  ✅
```

This is hash-equality only (one-way), not decryption. We can
**verify a known plaintext exists in the keychain** without
needing the encryption key:

```python
import hashlib, sqlite3
def password_entry_exists(udid, service, account):
    db = _keychain_db_path(udid)
    s = hashlib.sha1(service.encode("utf-8")).digest()
    a = hashlib.sha1(account.encode("utf-8")).digest()
    return sqlite3.connect(db).execute(
        "SELECT 1 FROM inet WHERE srvr=? AND acct=? AND tomb=0 LIMIT 1",
        (s, a)).fetchone() is not None
```

What this enables:
- "Did the agent add a password for `example.com` with username
  `alice@example.com`?" → exact `WHERE acct=? AND srvr=?` match
  against precomputed hashes. No baseline/delta needed.
- Cross-app routing checks: a Safari AutoFill prompt's saved
  credential lands in `com.apple.cfnetwork` with the same
  hashed `acct`/`srvr`; we can verify the credential reached
  the right access group.
- Tombstone awareness: `WHERE tomb=0` excludes soft-deleted
  rows that the Passwords app no longer surfaces in its list.

What this does **not** enable:
- Verifying the password *value* the agent typed (only the
  username + server are hashed; the password is in the
  encrypted `data` BLOB).
- Pattern queries ("any password for `*.example.com`") — hash
  inputs must be exact; one-way function.
- Recovering plaintext from an unknown row (preimage-resistant
  by design).

**Programmatic reading of password values (clarification):**

The user/server are **indexable by hash equality** but not
recoverable; the password value is **fully encrypted** and not
recoverable. Three different surfaces:

| What | Where | Programmatically readable? |
|---|---|---|
| Row counts, access group, dates, `data` length, `tomb` flag | `inet`/`genp` plaintext columns | YES (clear) |
| Username, server (when we know the plaintext we're looking for) | `acct`, `srvr` columns | YES via SHA-1 hash equality |
| Password value | `data` BLOB | NO — `SFAuthenticatedCiphertext` (NSKeyedArchiver-wrapped IV + AES ciphertext + HMAC). Key is in `securityd`'s keybag. |
| Runner's own keychain items (added via `SecItemAdd` to its own access group) | runner's keychain partition | YES via `SecItemCopyMatching` with `kSecReturnData=YES` — but those items aren't Passwords-app-visible |

Bottom line: SIBB verification covers **(a) row counts**,
**(b) "is this specific service+username saved" via hash
equality**, and **(c) AX-tree observation of the Passwords list
UI** — enough for every reasonable Passwords task that doesn't
require asserting on the password value itself.

**Tasks workable with v1:**
- "Add a new password for example.com / alice / hunter2" — agent
  drives New Password UI; verifier asserts
  `passwords.entry_count` delta of +1 in
  `com.apple.password-manager`.
- "Find a password for sibb-test.example.com" — agent uses Search
  field; verifier reads AX tree of search results.
- "How many passwords are saved?" — `passwords.entry_count`
  fetcher returns the count directly.

**Tasks NOT workable in v1:**
- Pre-seeding specific passwords from the verifier side (can't
  SecItemAdd into the right access group).
- Asserting on password value (encrypted; we can only count).
- Auto-fill credential testing (needs an authenticating relying
  party site + sign-in flow).

**UI display quirk:**
iOS auto-shortens long Title fields in the password list. Typing
"sibb-test.example.com" results in a list cell labeled just
"Example". Use the **username** as the verification identifier in
AX assertions — it's preserved verbatim.

---

## 14. Safari web content via AX — what works, what doesn't

Empirical probe 2026-05-17 across 8 archetypes on iOS 26.3
Safari. Earlier docs ([CLAUDE.md item #11](../../CLAUDE.md))
flagged Safari as "AX-invisible unless ARIA-tagged" — that's
overly pessimistic for the bulk of the web. The real pattern:
**semantic HTML works, custom rendering doesn't**, and iOS
collapses long body text into summarized chunks.

**Universal behaviors:**
- The page `<title>` shows up as a top-level `Other` element with
  the title as `label`. Reliable anchor for "are we on the right
  page" checks.
- ARIA landmarks (`banner`, `navigation`, `main`, `search`)
  surface as `Other` elements whose `label` is literally the
  landmark name. Free structural map of the page.
- **[SUPERSEDED 2026-06-01 — see "Role facts the original probe
  under-reported" below.]** Previous claim said "Links never appear
  as a `Link` role — they're either `Button` or `StaticText`." The
  follow-up walk on iOS 26.3 observed `link` role on Wikipedia
  pages; behavior may be page- or version-dependent. Don't *require*
  `link` role in selectors; do accept it opportunistically.
- `<img alt="...">` becomes `Image` with the alt as `label`.
  Images with no alt show as `Image label=''` — useful for
  detection ("is there a missing-alt image?") but uninformative.
- The Safari chrome (Address `TextField`, `Back`, `Page Menu`,
  `refresh`, `More`) is present in every tree. Don't treat
  "TextField present" as a signal that the *page* has a form —
  filter by parent / by position below the URL bar.

**Empirical results:**

| Archetype | URL | Total elems | AX exposure | Task-design verdict |
|---|---|---|---|---|
| Plain HTML | `example.com` | 18 | Full page text + the lone link as both StaticText and Button | ✅ Trivially driveable |
| Wikipedia article | `en.wikipedia.org/wiki/IOS` | 68 | Title + nav + section headers + viewport-slice of body. Tables/infoboxes partly visible. **Body text is VIEWPORT-bounded** — see 2026-06-01 follow-up. | ✅ For structure / navigation + extract-a-fact-with-SCROLL tasks; ⚠️ "read full article" is turn-budget-heavy and retention isn't guaranteed |
| Search engine | `duckduckgo.com` | 53 | Search TextField with placeholder, all nav buttons | ✅ Ideal — search box is a first-class AX TextField |
| Minimal news (link-heavy) | `news.ycombinator.com` | 313 | Every story title, score, timestamp, comment count visible as StaticText; titles also as Buttons | ✅ **Gold standard** — semantic HTML + no JS = dense, complete tree |
| Modern SPA | `github.com/torvalds/linux` | 108 | Nav, sign-in, repo name, title, ARIA landmarks. README body NOT re-probed under viewport-walk methodology (only `wiki/*` was) — assume same viewport-bounded behavior pending re-run | ✅ For UI-chrome tasks; ⚠️ body retention untested |
| Canvas / map | `google.com/maps` | 22 | **Only the "Upgrade to a smarter Google Maps" promo** is in the tree. The map itself is invisible. | ❌ Use Maps.app instead; web Maps is unworkable |
| News with paywall | `nytimes.com` | 67 | Top nav, subscribe banner, headlines, image alts | ✅ Workable but image alts vary; expect blanks |
| Video site | `youtube.com` | 25 | **Only empty-state UI** ("Try searching to get started"). Video grid is not in tree even when populated. | ❌ Video thumbnails/content not surfaced — opaque |

**Body-text exposure: viewport-bounded slice, not full-page
truncation (2026-06-01 follow-up).** The original 2026-05-17
probe took a single snapshot per page and concluded "long body
summarized to fragments". A follow-up walk
(`sibb_probe_safari_body_reader.py`) that snapshots, scrolls,
re-snapshots across `wiki/IOS` and `wiki/Pluto` showed the
count of substantive (≥40 char) labels grows with each scroll:

| Page | baseline | +1 scroll | +2 | +3 | +4 |
|---|---|---|---|---|---|
| `wiki/IOS` (Demo+Baseline both runs) | 3 | 3 | 9 | 10 | 10 |
| `wiki/Pluto` Demo | 9 | 9 | 10 | 12 | 17 |
| `wiki/Pluto` Baseline | 6 | 6 | 7 | 8 | 15 |

The growing union supports a viewport-bounded model: the AX
tree at any moment carries the slice of body content near the
viewport, and scrolling re-windows the tree.

**Quantitative caveats** — what this finding does NOT mean:
- **Coverage is small per scroll.** Pluto reached cumulative 15
  substantive labels after 4 scrolls. The article has ~600
  sentences, so ≤3% of the body becomes available within a
  realistic scroll budget. "Agent can read the full body" is
  NOT supported — at ~3–4 new substantive labels per scroll,
  reading the whole article would take ~150–200 scrolls. Realistic
  tasks: "find this fact / answer this question" where the answer
  is plausibly within the first ~5 viewports.
- **Retention: HIGH (2026-06-03 follow-up).** A retention pass
  (scroll down 4 → scroll back up 4 → re-snapshot) recovered
  3/3 baseline labels on wiki/IOS and 7/7 on wiki/Pluto. The AX
  tree behaves cumulatively for a scroll-back path; once-visible
  body labels reappear when the agent navigates back to where they
  were. Generators can treat body reads as "extract a fact within
  the first 4-5 viewports; the agent can re-find it by scrolling
  back if needed." Caveat: this was measured only on Wikipedia,
  and only via element-targeted SCROLL within the WebView frame
  — fixed-coordinate swipes that escape the WebView bounds will
  not pan the page at all (see SCROLL section below).
- **Generalization is untested.** The probe only walked
  `wiki/IOS` and `wiki/Pluto`. Other content types (Reddit
  threads, PDF in Safari, long Substack/blog posts, paywalled
  articles past the fold, YouTube transcripts) were NOT
  re-probed under the viewport-walk methodology. Don't assume
  the same behavior elsewhere without re-running the probe.
- **Each scroll is a turn.** The agent's turn budget (default
  30 in `sibb_assistant.py`) is finite; body-reading tasks must
  budget scrolls accordingly.
- **n = 2 articles, single run per sim.** The above data is a
  single walk through two Wikipedia pages on one iOS 26.3 sim
  (and a baseline replica). Not a corpus study.

**Role facts the original probe under-reported (single-run,
iOS 26.3 Wikipedia only):**
- Body sentences come through under role `text` (the scaffold's
  normalized name; raw is `XCUIElementTypeStaticText`). The
  earlier per-page "25 StaticText" was a single-viewport count
  inside a *69-element scroll-container parent*; scrolling re-
  parents the visible window and exposes a new set of `text`
  elements.
- Wikipedia inline links came through as role `link` whose
  label is *the surrounding sentence fragment*. The earlier
  "Links never appear as a `Link` role" claim does NOT hold on
  iOS 26.3 Wikipedia — both `link` and `btn` were observed.
  Treat `link` as opportunistic: accept it in selectors, don't
  require it (behavior may be page-specific or version-dependent
  and the older claim could still hold on other content types).
- Element counts per snapshot fluctuate widely as scrolling
  re-windows the tree (e.g. wiki/Pluto: 158 → 274 → 284 → 259
  → 119). The 284→119 drop at end-of-article is consistent
  with hitting page end; a mid-walk 197→109 on wiki/IOS suggests
  AX nodes can be evicted, not merely shifted. Either way,
  agents should query by *label content + role*, never by
  absolute element index across turns.

**SCROLL must target a scrollable AX element (2026-06-03).** The
2026-06-01 probe used fixed `swipe_at` coordinates (e.g. from
`y=H*0.85` to `y=H*0.18`) and worked on a Safari layout where the
URL bar was at the BOTTOM. A re-run with the bar at the TOP
discovered the swipe path passed through chrome (URL bar / tab
strip) and iOS treated the gesture as a chrome interaction
rather than a WebView pan — every snapshot returned identical
element counts because no scrolling actually happened. The fix
is to find the scrollable element (Safari WebView has
`role: "web"`; Maps' map has `role: "map"`/`"scroll"`; lists are
`role: "table"` or `"collection"`) and bound the swipe entirely
within its frame. This generalizes across apps: SCROLL is for
panning a scrollable AX element, not for whole-screen gestures
(those go through SWIPE). The agent grammar enforces this — bare
`SCROLL down` without an `@ref` returns an error suggesting SWIPE.

**Bottom-bar URL chrome on iOS 26.** Safari moved the URL bar +
Page Menu + Back/More buttons to the BOTTOM by default. The bar
auto-collapses to a pill after the user scrolls *down*. To
interact with chrome (Page Menu / Format Menu / Reader toggle),
the agent must first wake the bar.

The durable detection signal is the AX tree, not pixel y:
- If an element with `label="Page Menu"` is present in the
  current snapshot, the bar is expanded — proceed to chrome
  interaction.
- If absent, the bar is collapsed — wake it before searching
  again.

Wake mechanism (empirically observed on iPhone 17 Pro 393×852):
a slow, short upward swipe (≈150 px at ~200 pps) in the page
body re-expands the bar. Fast/long swipes scroll the page
without restoring the bar. These exact numbers are sim-/device-
specific — when porting to another device class or iOS bump,
the SIGNAL (`label="Page Menu"` present) remains valid; the
wake gesture may need re-tuning.

**Reader Mode toggle path on iOS 26 — UNVERIFIED.** The
2026-06-01 follow-up attempted to toggle Reader Mode via Page
Menu but the probe's popover-row search was not properly scoped
and the resulting "Show Reader" tap landed on a Settings row
("Safari intelligently displays summaries..."). Reader Mode was
never actually entered, so we have NO data on whether Reader
Mode expands body-text AX exposure on iOS 26. Treat Reader Mode
as a deferred investigation — re-run the probe with a
popover-scoped finder before designing any generator that
depends on it.

**What this means for SIBB Safari task design:**

Tasks the agent CAN do via AX:
- Type into search boxes (DuckDuckGo, Google, site search) and
  tap a result link.
- Fill out forms (login, signup, contact, checkout). **Our
  mock-site approach is firmly in this regime.**
- Navigate link-heavy aggregator sites (HN-style front pages,
  comment threads, search results).
- Identify ARIA landmarks to decide "am I in the main content vs
  the nav".
- Click buttons by their visible label.
- Read short prose (headers, captions, button labels, error
  messages on a login form).
- Detect cookie banners and dismiss-by-button-label.

Tasks the agent CAN'T do via AX (need VLM, or use the native
app instead):
- Read map content from web Maps (use Maps.app).
- Interact with embedded video players (no `<video>` controls
  exposed beyond playpause buttons in the best case).
- Interpret canvas-based charts/graphs (Tableau, custom data
  viz).
- Interact with rich text editors that use `contenteditable`
  with shadow DOM (most modern WYSIWYG editors).
- Anything inside an `<iframe>` from a cross-origin domain
  (frame contents are typically pruned).

Full-body article ingestion ("summarize this article") remains
in this list — coverage per scroll is small (≤3% of Pluto's body
after 4 scrolls) and retention across scrolls is untested. But
**extracting a specific fact from the body** (e.g. "find Pluto's
discoverer", "answer this question") moves to the CAN list, with
the SCROLL turn-budget caveat above.

**The mock-site contract (relevant for our password tasks):**
Our `sibb_mock_site.py` deliberately uses plain semantic HTML —
`<form>`, `<input type="text">`, `<input type="password">`,
`<button>`, `placeholder` attributes, ARIA-implicit form
landmark. This puts us firmly in the "fully readable" regime
(probed shape was identical to the form patterns above). No
ARIA hacks needed.

**Revisit on:** every iOS major. WebKit accessibility has shifted
between iOS 17 → 18 → 26; the summarization-of-long-text behavior
may tighten or loosen. Re-run
`sibb/benchmark/sibb_probe_safari_ax.py` (the same script that
produced the table above) on a fresh sim when iOS bumps, then
diff against the recorded numbers — material changes go into
this section.

---

## 15. Wallet.app: PKPass signature enforced on sim, Apple Pay impossible

Investigation 2026-05-17.

**Two flows, both blocked on sim:**

| Flow | Sim feasibility |
|---|---|
| **Add Card** (Apple Pay credit/debit/transit) | ❌ Impossible by design. Requires Secure Element hardware (sim has none), Apple Pay provisioning entitlement, and a bank-side authorization round-trip. The UI launches but card scan / manual entry / activation cannot complete. |
| **Add Pass** (PKPass boarding/event/loyalty/generic) | ❌ Apple-cert-gated. PassKit on sim enforces full signature validation — confirmed empirically by sideloading an unsigned `.pkpass` and reading the PassKit log. |

**The smoking gun (PassKit log on unsigned-pass sideload):**

```
PassKitCore [com.apple.passkit:Validation] Verifying signature for pass ... using network:1
PassKitCore [com.apple.passkit:Validation] Signature validation: *** FAILED ***
PassKitCore [com.apple.passkit:General] Invalid data error reading pass ...
                                        Signature is missing
            Error Domain=PKPassKitErrorDomain Code=1 "Signature is missing"
```

**What it would take to make PKPass work:**
- Apple Developer Program membership ($99/yr)
- A registered Pass Type ID + downloaded Pass Type ID Certificate
- A PKCS#7 detached signing pipeline (`openssl smime -sign -binary -inkey
  pass-key.pem -signer pass-cert.pem -certfile WWDR-cert.pem -in
  manifest.json -out signature -outform DER -noattr`)
- Per-task pass templates pre-signed at build time, *or* signing
  at episode-setup time (slow and requires the key on the test
  machine)
- Plan for distribution: the private key cannot be checked into
  a public repo, so any open-sourcing of SIBB needs a separate
  story (mock cert + test-only sim that disables validation?)

**Decision: DEFER Wallet.** The signing pipeline is implementable
but the cost/value ratio is poor relative to Shortcuts and Fitness.
Revisit if a SIBB use-case specifically needs Wallet (e.g. "agent
adds boarding pass from email").

**Probe artifact** kept at `/tmp/sibb_pkpass_probe/` for reference
— minimal `pass.json` + manifest, demonstrates the structure but
fails validation by design (no signature).

---

## 16. Calendar (EKEvent) baseline on iOS 26.3 sim

Probe 2026-05-20 (`sibb/simulator/sibb_probe_calendar.py`). Findings
that shape every Calendar generator going forward.

### Writable calendars on a fresh sim

Exactly **one** writable calendar exists by default:

| Name | Writable | Pre-populated |
|---|---|---|
| `Calendar` | ✅ | Empty |
| `Birthdays` | ❌ | Empty until Contacts have birthdays |
| `Siri Suggestions` | ❌ | Empty |
| `US Holidays` | ❌ | **152+ events across ±1y window** (see below) |

`Home`, `Work`, `Personal`, `Family` etc. **do not auto-exist** —
attempting `create_event` with `calendar: "Work"` errors with
`"no writable calendar available"` (Swift code at `sibb_xcuitest_setup.sh:1309`).
**Multi-calendar tasks require a `create_calendar` Swift command
to bootstrap named calendars before pre-runner state setup.**

### TCC scope — `requestAccess(to: .event)` STILL gives full read+write

The Swift uses the deprecated `requestAccess(to: .event)` at
`sibb_xcuitest_setup.sh:531`. Critic worry (May 2026) was that
iOS 17+ resolves this to writeOnly, breaking `predicateForEvents`
reads. **Confirmed empirically on iOS 26.3 sim:** full read access
granted. `list_events` returns the event we just created (and ALL
holidays — see below).

Defer switching to `requestFullAccessToEvents` until a future iOS
breaks this. The deprecated API still works; the migration costs
Info.plist key + Xcode test-host re-signing.

### `US Holidays` pollution — the biggest gotcha

`list_events` over the default ±1y window returns **~150 read-only
holiday events** every time, in addition to whatever we created.
They live in `US Holidays` (read-only — `allowsContentModifications == false`),
so `wipe_events` correctly skips them, but every reporting query
(`list_events_today`, `list_events_this_week`) will see them.

**Mitigation (must land before any Tier 5 reporting generator):**
add a `writable_only: Bool = true` flag to the `list_events` Swift
command. Default ON. Tier 5 tasks then naturally exclude holidays.
Generators that *want* to read holidays (probably none) opt out.

### All-day events in time-window selectors — cheat path closed

Concern: could an agent set `all_day=True` on a target event to
make it vanish from a `start_iso=14:00 / end_iso=16:00` count
check? **Empirically no.** iOS `predicateForEvents` includes
all-day events (spanning `[D 00:00, D+1 00:00)`) in any window
that overlaps the day. Verified: an all-day event AND a 14:30-15:30
timed event both appear in a 14:00-16:00 window query.

**Implication:** generators don't need to emit `attribute_eq(all_day=False)`
guards for window-based count checks. Saves boilerplate.

### All-day events: `end_iso == start_iso` on single-day events

Probe re-run 2026-05-21 caught a subtle EventKit quirk. Input to
`create_event`:

```json
{"all_day": true,
 "start_iso": "2026-05-22T00:00:00",
 "end_iso":   "2026-05-23T00:00:00"}
```

`list_events` returns:

```json
{"all_day": true,
 "start_iso": "2026-05-22",
 "end_iso":   "2026-05-22"}
```

**Not `end_iso=2026-05-23` as a naïve reader of the input might
expect.** iOS stores `EKEvent.endDate` for all-day events as the
*last second of the inclusive last day*. When `dateOnlyFmt` (local
TZ) formats it, the resulting date is the LAST day of the span,
not the day after.

**Consequence for verification:** generators asserting the start /
end of an all-day event must use the inclusive-last-day form. For a
single-day all-day event, `end_iso == start_iso`. For multi-day
spans (3-day all-day from May 22), `end_iso = 2026-05-24` (the last
day), not `2026-05-25`.

**Mirrored in the fake reader** (`sibb/tests/fakes/fake_reader.py`):
`_create_event` decrements `end_iso` by one day when `all_day=True`
AND the input has a `T*` time component (signalling exclusive-end
convention). Without this, L1.5 tests would silently diverge from
L2 sim tests.

Affects: `gen_toggle_event_all_day` (T1.6) and any future generator
that creates all-day events. Was found via the post-T1 critic pass;
the original generator assumed `end = start + 1 day` and would have
shipped a verifier-against-itself bug undetectable at L1.5.

---

## 17. EKEventStore cache doesn't auto-refresh across processes

Surfaced 2026-05-21 during T1 manual demo. Symptom: user toggles
"All Day" via iOS Calendar.app UI, saves, sees the event re-rendered
as all-day. Verifier immediately reads via `list_events` and gets
`all_day=False`. The toggle DID save successfully — but our XCTest
runner's cached `EKEventStore` doesn't see the mutation.

**Why:** iOS Calendar.app and the XCTest runner are separate
processes, each with their own `EKEventStore`. The cached
`EventStoreCache.instance` (see `sibb_xcuitest_setup.sh`) retains
event references that don't auto-invalidate when another process
mutates the underlying CalendarDB SQLite.

**Apple developer-forum consensus**: `refreshSourcesIfNecessary()`
is the intended API. Originally for remote sources (iCloud, Google
Calendar) where delays of 30s to several minutes are common, but
empirically helps local-source data on sim too.

**Fix** (landed in `sibb_xcuitest_setup.sh`'s `list_events` +
`list_calendars` cases): call `store.refreshSourcesIfNecessary()`
before every read. Cheap on the success path; forces cache sync.

**Reminders has the same latent bug** for `list_reminders`. Apply the
same fix when convenient. Not yet bitten in Reminders demos —
probably because Reminders mutations happen via SIBB socket
(same-process) rather than the iOS Reminders.app UI.

## 18. Calendar.app search bar does NOT index events created via EKEventStore

Surfaced 2026-05-21 during T1 manual demo. User opens iOS
Calendar.app, types into the search bar, expects to find the seeded
events — finds nothing. Events ARE present (visible in Day/Week
views), but Calendar's search is Spotlight-backed and only indexes
events that were created via the **iOS Calendar UI**, not via
`EKEventStore.save(...)` programmatically.

**Apple developer-forum consensus**: iOS doesn't expose a manual
reindex command for Calendar. Settings → Siri & Search → Calendar
has a "Show Content in Search" toggle but no reindex action.
Programmatic events get indexed *eventually* via background sync —
on the order of hours, not minutes.

**Implication for the SIBB agent path**:
- Agent cannot use Calendar's search bar to find events.
- Agent must navigate via Day / Week / Month views to locate events
  by date, then tap the event tile to open / edit.
- This shapes the eventual `SYSTEM_PROMPT` for the LLM driver — list
  the navigation idioms explicitly so the agent doesn't waste turns
  trying search.

**Workarounds NOT to attempt:**
- Adding events via `CSSearchableIndex` programmatically — Apple
  docs hint this is the way, but reports of `indexSearchableItems()`
  returning success while items remain unsearchable are widespread.
- `mdimport` etc — macOS-only; no iOS sim equivalent.
- Settings toggle — already on by default for Calendar.

**Reminders.app** also has Spotlight integration. Same expected
limitation if a Reminders agent flow tries to search for reminders;
not yet validated empirically.

---

## 19. Maps.app: Directions screen quirks — route activation + AX labels

On the route-chooser screen (after tapping Directions and a transport
mode), iOS Maps' AX tree has two non-obvious aspects:

**(a) Route-summary buttons** are labeled with their ETA + mode,
e.g. `[btn] "5h 31m, driving"` or `[btn] "12 min, walking"`. Tapping
one selects that route — plist `NavigationUserActivityDefault` gets
written with the route's response UUID (this is what
`sibb_maps_reader.read_active_route_full` reads). Per-route summary
**details** (distance "6.9 mi", arrival "10:22 ETA", "Fastest" tag)
also surface as labeled `[other]` cells alongside the buttons; the
scaffold fix in §20 ensures they reach the agent.

**(b) `"Steps"` buttons** sit next to each route. AX label is
literally `"Steps"`, icon is `chevron.forward.2` (≫). **In the iOS
26 simulator, tapping Steps STARTS navigation with that route AND
shows the step-by-step list** — both actions happen together,
despite the label suggesting only the latter. The plist active-nav
blob (`NavigationUserActivityDefault`) is written with that
route's response UUID; the unified verifier reads this as
`is_activated=True`.

Brief history: an empirical retest on 2026-05-30 claimed Steps did
NOT start nav (no `GraphDirections/<UUID>` file written). That
test used GraphDirections file presence as the activation criterion
— but the Phase A++ reader (2026-05-31) determined GraphDirections
is NOT required for activation; the plist blob alone is the canonical
signal. Steps DOES write the plist blob and counts as committed nav.
The §19 retraction was based on the wrong criterion; restored the
"tap Steps" guidance 2026-05-31.

**The explicit GO / Start button** that appears on a real iPhone
is **not reliably present** in the sim's AX tree — agents should
not hunt for it. Steps is the commit path on the sim.

Documented in `sibb_assistant.py` SYSTEM_PROMPT under APP-SPECIFIC
NOTES ("Maps.app — starting navigation"). See
`sibb_maps_reader.read_active_route_full` +
`sibb_verify._fetch_maps_active_route`.

---

## 20. Scaffold surfaces labeled `[other]` elements; locale-pinned for noise regex

iOS' AX tree contains many `UIView`-rooted elements that map to
`ElementRole.OTHER` (token: `[el]`). Two distinct cases:

**Unlabeled `[other]`** — empty structural containers. Dropped by the
pre-existing `SKIP_IF_UNLABELED` rule at `sibb_scaffold.py:_read_xcuitest`.

**Labeled `[other]`** — Apple's UIAccessibility text content that
iOS did NOT promote to `[btn]`/`[cell]`/`[text]`. These carry
discriminative info that the agent can't get elsewhere. Examples:

- Maps Directions: per-route summary cells
  `[el] "21 min, 10:22 ETA · 6.9 mi, Fastest"` sit next to `[btn] "Steps"`
  — without the `[el]` text the agent can't distinguish route 1 from
  route 2 from route 3.
- Photos: year/month group headers as `[el] "May 2024"`.
- Calendar: month-grid day cells.

We surface labeled `[other]` to the agent (change landed
2026-05-30 in `sibb_scaffold.py:_filter`). A small denylist drops
UIKit-emitted noise that leaks through:

- `[el] "Vertical scroll bar, 1 page"` / `[el] "Horizontal scroll bar, 1 page"`
  — `UIScrollView` indicator chrome
- `[el] "Loading"` / `"Loading…"` — transient `UIActivityIndicatorView` labels
- `[el] "Dimming View"` — modal-presentation backdrop

The denylist (`_NOISE_OTHER_LABEL_RE` in `sibb_scaffold.py`) is
anchored with `^…$` to avoid matching legitimate labels containing
these words as substrings (e.g. a hypothetical app named "Loading
dock").

**Locale dependency**: the denylist matches English. UIKit's
accessibility strings are localized — `"Vertikale Bildlaufleiste"`
in German, `"Barre de défilement verticale"` in French, etc. If the
sim's locale is not en-US, the denylist silently 0%-matches and
scrollbar noise leaks back through. `sibb_prewarm.sh` pins
`AppleLocale=en_US` and `AppleLanguages=[en-US]` in
`.GlobalPreferences.plist` to ensure consistency. Re-prewarm any
existing baseline simulator after pulling this change so the pin takes effect.

**Future**: a multi-pass classifier (label-redundancy detection,
pair-and-merge into adjacent interactive elements, etc.) was
designed but deferred — the simpler denylist covers Maps' route
summaries which was the motivating case. Re-evaluate if any other
SIBB-11 app surfaces a similar gap.

---

## 21. Safari auto-zoom puts WebView content in zoomed-doc coords

**When this bites**: iOS Safari auto-zooms when the user focuses a
form input whose computed `font-size < 16px`. After zoom, the AX
snapshot reports WebView content (form fields, buttons) in **zoomed-
document** coordinates — scaled by the current zoom factor — while
iOS Safari chrome (URL bar, keyboard) stays in **screen** coordinates.
The two coord systems coexist in the same snapshot. Symptoms:

* `keyboard_frame.y` larger than `screen_height` (e.g. y=891 on a 874-
  tall screen — geometrically impossible in screen coords).
* Form container frame wider than `screen_width` (e.g. 563 wide on
  402-wide screen).
* Small leaf elements (a 122×31 submit button) still **fit** within
  screen bounds even though their coords are in zoomed-doc space —
  the naive "is frame within screen?" viewport filter lets them
  through. A TAP at the reported coord lands on a dead area.

**What `sibb_scaffold` does** (`sibb_scaffold.py:_read_xcuitest`):

Detects zoom via two signals (Step 4, 2026-06-07 — stateless,
per-frame):

1. **Swift-reported `zoom_scale`** — authoritative when present
   (`dumpTree` probes WKWebView via KVC). Placeholder slot today;
   the KVC probe is still a no-op.
2. **kb-above-screen heuristic** — `kb_y_top > screen_h` is the
   smoking-gun symptom we observed empirically. Has not been seen
   firing in any probe yet.

**No latch, no per-reader state.** The previous design used a 3-signal
cascade (Swift > overflow-width > kb-above-screen) with a 2-snapshot
release latch on `AXReader`. Empirical probe
(`sibb_probe_autozoom_lifecycle.py`, 2026-06-06) showed signals are
stable across consecutive frames — no flicker. The same probe also
showed the overflow heuristic was a false positive on every Safari
page with content wider than the viewport (baseline state with no
zoom reported overflow=1.60). Step 4 dropped both the latch and the
overflow heuristic. Detection is effectively always False today,
which is honest — the agent uses `DOUBLE_TAP (200, 100)` defensively
when they experience a zoom problem rather than trusting a header
tag we can't reliably set.

**Chrome bounds are runtime-derived** (not pixel-tuned for one
device + orientation):

* `top_chrome_bottom`: max-bottom of any small AX text near `y=0`
  (status bar / tab strip). Falls back to 50px.
* `bottom_chrome_top`: min(URL bar y, keyboard top). Falls back to
  `screen_h - 100`. As of Step 3 (2026-06-06) the accessory bar is
  NOT one of the inputs — its elements are agent-visible UI, not
  chrome.

This makes the filter land correctly across iPhone SE / Pro Max /
iPad / portrait + landscape without per-device tuning.

**Predictive bar + inputAccessoryView**: Swift still emits the union
of the "Typing Predictions" strip and any `Previous` / `Next` / `Done`
keyboard accessory frames as `accessory_bar_frame`. As of 2026-06-06
this is **diagnostic-only** — `keyboard_y_min` is now just
`kb_frame.y`. The empirical probe
(`sibb_probe_autozoom_lifecycle.py`) showed iOS exposes
`Previous` / `Next` / `Done` as fully-labeled `[btn]` elements with
real frames in the AX tree; the agent SHOULD be able to tap them
(`Done` dismisses the kb; `Next`/`Previous` walk form focus; the
prediction words autofill). Hiding them via union was a bug. Form
fields BEHIND the bar are still filtered — they ARE genuinely below
the kb top, so the bare `kb_frame.y` threshold catches them.

**Orientation**: derived from `screen_w > screen_h` and exposed as
`tree.orientation`; surfaces as `LANDSCAPE` in the assistant's step
header.

**Tests**: `sibb/tests/unit/test_scaffold_zoom_detection.py` covers
both surviving signal paths (Swift `zoom_scale` + `kb-above-screen`),
the stateless per-frame behavior (no latch as of Step 4), runtime
chrome derivation, accessory-bar diagnostic surfacing, orientation
derivation, and a landscape regression that asserts form fields at
`y > 302` are not dropped as "chrome".

**Smoking-gun probe**: `sibb/simulator/sibb_probe_safari_form_ax.py`
streams the live AX while a human / agent interacts with the form;
the probe data from 2026-06-05 (form container 563px wide on 402px
screen; submit button 122×31 at zoomed-doc center; kb_top=891 vs
screen_h=874) is what motivated this entry.

**Correction (2026-06-06)**: an earlier version of this section
claimed AX coords for WebView content were in zoomed-doc coordinates
and that a coord-system filter was needed. **Screenshot-overlay
verification (`sibb_probe_pinch_recovery.py` with the annotated PNG
output) disproved this.** AX element frames ARE real screen
coordinates even when auto-zoomed:

* The form-container reported at 563×194 on a 402-wide screen just
  means the element extends past the visible viewport — the visible
  portion is at its reported coords, the rest is clipped.
* The submit button reported at center (61, 580) is **exactly where
  the button is painted on screen**. TAPs at those coords hit.
* The existing `_is_fully_visible` filter (geometric bbox check
  against screen + kb) is doing the right job: elements whose entire
  frame falls outside the viewport get filtered, the rest reach the
  agent at correct coords.

Zoom detection therefore stays as an **informational** signal
(`AUTO-ZOOMED=1.5x(swift|overflow|kb_above_screen)` in the
tokenizer header), but does NOT drive any further filtering.

**Recovery options for the agent**:

| Action | Resets WebView zoom? |
|---|---|
| `TAP @<URL bar>` (opens Smart Search) | ❌ no |
| `PRESS home` (drop to springboard) | ❌ no |
| App-switcher (`PRESS app_switcher`) trip | ❌ no |
| `PINCH out` via XCUITest | ❌ no — Apple's WebKit bug 234584-class limits programmatic pinch on WKWebView |
| Two rapid `xc.tap()` calls at same coord | ❌ no — synthetic-tap event path doesn't feed WebKit's double-tap recognizer |
| **`DOUBLE_TAP (x, y)` via `XCUICoordinate.doubleTap()`** on a non-input region | **✅ yes — verified 2026-06-06 via real-trackpad probe; the native gesture API dispatches through the same touch pipeline as real input devices, which IS what WebKit's zoom-fit recognizer listens to** |
| Manual Option+drag on the sim (pinch) | ✅ yes (host-side gesture, also agent-reachable via DOUBLE_TAP now) |

**Recommendation**: when AUTO-ZOOMED is detected, emit
`DOUBLE_TAP (x, y)` with `x, y` in the top quarter of the viewport
(e.g. `(200, 100)`) — page chrome / heading area, safe to double-tap
without triggering input focus. This is the canonical Safari zoom
reset.

**Sim probe for verification**:
`sibb/simulator/sibb_probe_pinch_recovery.py` opens the RSVP form,
focuses an input (triggers auto-zoom), issues `PINCH out`, and saves
six annotated PNGs to `/tmp/sibb_probe_*.png`:
* `1a` / `1b` — baseline raw + scaffold-filtered (what the agent sees)
* `2a` / `2b` — post-focus raw + filtered
* `3a` / `3b` — post-pinch raw + filtered

Compare visually to confirm AX coords match painted positions.

**Robustness hardening (task #210–#228, 2026-06-06)**:

* `AXReader.reset_episode_state()` was needed only for the zoom latch.
  Step 4 (2026-06-07) dropped the latch and removed the method along
  with calls from `sibb_assistant.py`, `sibb_replay.py`,
  `sibb_episode.py`, `sibb_episode_runner.py`. Cross-episode state
  on `AXReader` is now zero. (Earlier docs:)
* Replay's pre-tap occlusion guard consumes `keyboard_y_min` (=
  `kb_frame.y` as of Step 3 — bare keyboard top, no longer unioned
  with accessory bar). Taps below the kb top are rejected with a
  "below the keyboard" diagnostic; the bar's elements (Done / Next
  / Previous / predictions) are now agent-tappable and live above
  this threshold.
* Python-side `_derive_chrome_bounds` gates English-literal label
  matches (`Address`, etc.) behind a role + geometry check: role
  key normalized via `_chrome_role_key` to one of
  `{btn, button, input, textfield, search, toolbar, other}`
  (handles BOTH raw XCUITest lowercase strings — production —
  AND `ElementRole` enums — test fixtures), `frame.height ≤ 60`, and
  position guard `fr.y < kb_y_top` when the keyboard is up
  (orientation-independent) else `fr.y ≥ screen_h * 0.5` as a portrait
  fallback. Sheet "Done" buttons, nav-bar confirmations, and tall
  toolbars no longer spuriously shrink the chrome region.
* Swift-side `gatherAccessory` (in `sibb_xcuitest_setup.sh`) now
  requires `keyboardVisible == true`, `frame.y < keyboardTop`
  (orientation-independent — the earlier `> screen_h * 0.5` clause
  silently rejected the predictive bar in landscape, where the kb
  top sits at ~y=170 and the bar at ~y=130 was below the half-screen
  line), and per-label role+height gating (`.button` + `height < 50`
  for Done/Next/Previous; `.other` allowed for the Typing Predictions
  container with `height < 60`). **Requires a SIBBHelper rebuild**
  (`./sibb_xcuitest_setup.sh <UDID>`) to take effect on the sim.
* (Earlier doc — superseded by Step 4 bullet above:
  `reset_episode_state()` and its 4 runner call sites were the
  Step-3-era mitigation for the latch leak. Step 4 dropped the
  latch entirely, removing the need for the reset call.)
* Replay's pre-tap occlusion check now ROUTES focused-below-kb
  fields to raw `type_text` (skipping the tap entirely) instead
  of rejecting them. The scaffold exempts focused fields from
  the visibility filter so the agent can see them, and the
  executor must be symmetric — the kb already holds the field's
  responder, retap-then-type would just hit the keyboard. The
  rejection branch remains for non-focused below-kb fields (which
  the visibility filter normally drops, but is the safety net).
* The `bottom_chrome_top >= screen_h * 0.5` clamp was relaxed —
  it was forcing the chrome region to live below the half-screen
  line even when the keyboard genuinely covered more than half
  the screen (landscape iPhone case). New invariant: `bottom >
  top` only.
* Dead labels (`toolbar`, `tab bar`) pruned from
  `_SAFARI_BOTTOM_CHROME_LABELS` — after the role+height gate
  landed, no real Safari element can pass them. The dead-code
  `_is_chrome` helper was removed (leftover from the 2026-06-06
  coord-system filter revert).
* Executor result dict now exposes `kb_y_min_used` — the actual
  occlusion threshold the pre-tap decision used. Tests assert
  on this structured field instead of substring-matching the
  human-readable error message.
* Tree now carries `top_chrome_bottom` + `bottom_chrome_top`
  alongside the existing `zoom_factor` / `zoom_source` /
  `coord_system_zoomed` / `orientation` / `keyboard_y_min` /
  `kb_filtered_count` / `viewport_filtered_count`. Assistant's
  per-turn JSONL record now includes a `"diagnostics"` sub-dict
  with these values via `_tree_diagnostics(tree)` in
  `sibb_assistant.py`. The contract is pinned by an L1 test
  (`test_assistant_diagnostics.py`) — the key set is held stable.
* `SCROLL_PAGE` verb added — content-direction semantic synonym
  for SWIPE. The clipped-button benchmark showed agents trip on
  iOS SWIPE direction semantics (SWIPE down = finger down =
  content goes UP not down) and loop. `SCROLL_PAGE down` =
  "show me lower content" maps internally to `SWIPE up`. Per-turn
  agents now have a verb whose direction matches their
  intuition. SYSTEM_PROMPT gained a "DIRECTION SEMANTICS"
  callout under SWIPE.

### Distractor-stack hit-zones under auto-zoom (2026-06-07)

**When this bites**: stacked `<button>` elements (the form's real
Submit + 2-3 distractor buttons rendered by
`harness_layout.distractor_buttons`) are categorically unhittable
in their middle position under iOS Safari auto-zoom — *not because
of a coord-system bug, but because the rendered layout doesn't
respect Apple's 44 pt minimum tap target.*

Empirically observed via `sibb/simulator/sibb_probe_zoom_hit_zone.py`
on the seed=1 RSVP form (page_seed=506456970, font-size=13 px) AND
its `force_font_size=16` no-zoom control (same `page_seed`, only
the zoom condition differs):

**Two distinct effects compounding under zoom:**

1. **AX-frame inflation by ~2 pt** — WebKit reports wider buttons
   ("Discard Changes" 162 px, "Remind Me Later" 162 px) as h=32 under
   zoom vs h=30 under no-zoom. The narrow middle button ("Preview"
   94 px) stays at h=30. With zero inter-form margin, neighbor AX
   frames overlap by 2 pt at top and bottom, and iOS hit-test snaps
   the middle button's hits to whichever neighbor owns the overlapped
   strip. **Step 5c fix**: 8 px `margin-top` on every distractor
   form. Verified: AX frames no longer overlap.

2. **"Fat finger" hit-area inflation by ~11 pt each direction** —
   even after fixing (1), the empirical hit-zone of each wide neighbor
   extends ~11 pt past its AX-reported bottom (and the next neighbor
   extends ~11 pt above its AX-reported top). The narrow middle button
   in between has no inflated counterpart, so its only y range free
   of neighbors is a narrow strip (~16 pt at 4 pt scan resolution:
   y=649..665, 5 sampled taps) — and at those values, no tap registers
   any POST. The visual button is rendered too small for iOS's touch
   pipeline to route the tap. **Step 5d fix**: `min-height: 44px;
   min-width: 44px; padding: 8px 16px` on every submit/distractor
   button (including the clipped variant's inline-built Submit).

**Compare conditions** (`page_seed=506456970`, x_scan=47, scan-step=4 pt;
"Hits" column is from the post-5c, pre-5d layout — the intermediate
state where AX frames are clean but button rendering is still UA
default):

| Button | Zoom AX (pre-fix) | Zoom AX (post-5c) | No-zoom AX (control) | Hits (post-5c, pre-5d) |
|---|---|---|---|---|
| submit | y=553..585 (h=32) | y=553..585 | y=549..579 (h=30) | — (validation popup) |
| discard | y=602..634 (h=32) | y=602..634 | y=594..624 (h=30) | y=597..645 (49 sample pts ≈ 48 pt span) |
| **preview** | **y=632..662 (h=30) — 2 pt overlap both sides** | **y=642..672 (clean)** | **y=624..654 (h=30)** | **GHOSTED in zoom, hits y=625..649 in no-zoom** |
| remind | y=660..692 (h=32) | y=680..712 | y=653..683 (h=30) | y=649..697 (49 sample pts ≈ 48 pt span) |

**No-zoom drifts:** discard −4 pt, preview −2 pt, remind +3 pt
(all ≤ 4 pt from AX center). iOS hit-test is accurate to within
±4 pt at the same font under no-zoom; no systematic directional bias.

**End-to-end verification:** after 5b+5c+5d landed, the LLM driver
(`sibb_assistant.py`, gemini-2.5-flash) passes seed=1 (zoom, font 13 px)
and seed=3 (no-zoom, font 16 px) in 8 turns each. The probe still shows
Preview as ghosted in its artificial keep-keyboard-up environment,
but the agent never needs to tap Preview — that's a distractor.

**Refuted hypotheses** (left in record because they came up in
analysis):

- "AX-frames are document coords, not screen coords." — WebKit
  source `WebAccessibilityObjectWrapperIOS.mm` converts AX rects via
  `convertRectToSpace(rect, AccessibilityConversionSpace::Screen)`,
  which applies the page-to-screen transform including current zoom.
  The 29-pt button spacing is therefore the actual on-screen
  spacing already scaled by zoom factor.
- "iOS hit-test biases closely-stacked targets toward one direction."
  — empirical drifts in the no-zoom control are ±4 pt and not
  directional; refuted.

**Recommendation for any new harness page that stacks tappables**:

- ≥8 px inter-element vertical margin (`margin-top:8px` per form)
- ≥44 pt × 44 pt button size via inline style or shared CSS rule
  (Apple HIG; in iOS Safari CSS px and pt are interchangeable at
  default zoom)
- Don't assume `<button>` UA default geometry is enough; iOS Safari
  honors it loosely under zoom, especially for buttons narrower than
  their neighbors.

**Probe**: `sibb/simulator/sibb_probe_zoom_hit_zone.py [--only zoom|nozoom]`
— sweeps Y at 4 pt resolution across the stack and reports
per-button empirical hit zones vs AX frames. Re-run after any
distractor-rendering change.

### HTML5 form validation tooltip text is invisible to XCUITest (2026-06-07)

**When this bites**: an iOS Safari form with `required` inputs left
empty, then Submit-tapped, shows the standard HTML5 validation
balloon ("Please fill out this field") on the offending field. iOS
makes the balloon's existence visible to assistive technologies but
exposes the **message text** only via a transient accessibility
ANNOUNCEMENT (`UIAccessibility.post(notification: .announcement,
…)`), not via the static AX tree XCUITest reads.

**Verified empirically** with
`sibb/simulator/sibb_probe_html5_validation_bubble.py` on the
seed=1 RSVP form (font 13 px → zoom triggered → kb up → empty-fields
Submit-tap). Captured the FULL raw + scaffold-filtered AX trees at
0.5 s / 1.2 s / 3.2 s post-tap. Across all 6 captures (5 needle
searches × multiple element fields):

* The popup IS present as a single AX element:
  `role=other, label='dismiss popup'` (no `value`, no child text).
* The message text matches NONE of the needles
  `{please fill, fill out, required, this field, missing}` in any
  capture, raw or filtered.

**Consequences for SIBB**:

* Agent can detect "something popped up" (the `dismiss popup`
  element appears, and the AX tree often duplicates the underlying
  page content as a second window — both visible signals).
* Agent CANNOT read which field is empty or what the rule is.
* `submit_form()` (in `harness_layout.py`) already emits a separate
  server-rendered `role="alert"` div on the response when an
  empty-required POST gets through — that's the agent's actual
  observable signal. The iOS-native popup is decorative for our
  agents.

**No fix needed at the scaffold layer** — the message text is just
not exposed by iOS. The in-AX alert div is the canonical channel.

**Open follow-ups**:

* The Swift `zoom_scale` slot is wired into the response envelope
  but the actual KVC probe of `WKWebView.scrollView.zoomScale` is a
  placeholder. The Python heuristics are what's load-bearing today.
  Closing this gap removes the last brittleness in zoom detection.
* The accessory-bar union still matches English literals
  (`"Typing Predictions"`, `"Previous"`, etc.). The role+geometry
  gates above degrade gracefully when iOS localizes the strings
  (we lose the predictive-bar union but keep the kb-frame guard),
  but a pure role-based match is the eventual fix.

---

## §22. iOS sim Safari URL-bar does NOT honor `/etc/resolver/test`

**Discovered**: 2026-06-07 while sim-verifying `gen_safari_rsvp_form`
with `open_at_start=False` (agent has to TYPE the URL rather than
having it pre-loaded via `simctl openurl`).

**Symptom**: agent types `http://lumen-festival.test:54100/event` in
Safari's URL bar → Safari classifies it as a search query → loads
`google.com/search?q=...` instead of the MockSite.

**Root cause**: iOS sim Safari's URL-bar input runs its own DNS
classification in a path that does NOT read the host's
`/etc/resolver/test` config. `.test` lookups fail in that path,
Safari treats unresolvable input as a search query, falls through
to the default search engine.

**Asymmetry**: `simctl openurl <UDID> <hostname-url>` works fine —
that path routes through host-side networking which DOES honor
`/etc/resolver/test`. The HTTP server receives the request with
`Host: aurora-conference.test:<port>`. The hostname-aware path was
the only path exercised by the original DNS plumbing work (D6/D7);
the agent-types-URL path wasn't.

**Empirical verification** (the probe):
1. Host-side `scripts/sibb_verify_dns_resolver.py` passes all four
   checks (resolver config + DNS server + macOS resolver + loopback
   HTTP via hostname).
2. `simctl openurl <UDID> http://aurora-conference.test:<port>/probe`
   → MockSite receives a request with `Host: aurora-conference.test:<port>`.
3. Agent types same URL in URL bar → Safari goes to Google search,
   MockSite receives nothing.

**Fix (committed Step 5j)**: `gen_safari_rsvp_form` and
`gen_safari_rsvp_form_clipped` embed `127.0.0.1` (numeric IP) in the
agent-shown `event_url`. The `hostname` field stays in the
mock_site spec for logging continuity but is decorative from the
agent's perspective.

**What we don't fix**: the friendly-hostname realism in the prompt
when the agent must type. Numeric IP is acceptable because the test
exercises URL-typing-from-instruction, not URL-from-memory.

**Open question**: is the URL-bar DNS path tunable? Possible
workarounds for a future hostname-realism upgrade:
- Pre-load a `<a href="http://team.test:port/event">` page that
  the agent taps (link navigation likely uses CFNetwork, which
  honors the host resolver — same code path as `simctl openurl`).
- Investigate `defaults write` against any sim-side DNS plist.

---

## §23. Safari Saved Credit Cards on iOS 18+ unified with Wallet — sim-blocked

**Discovered**: 2026-06-08 while probing whether SIBB could build a
`gen_safari_creditcard_autofill_payment_form` task by seeding the
Safari AutoFill credit-card store host-side (the same trick §13 uses
for Passwords, where SHA-1 of plaintext is checked into the keychain
alongside the encrypted blob).

**Verdict**: **NOT FEASIBLE on iOS 26.3 sim. Drop from roadmap.**

**Root cause**: iOS 18+ unified Safari AutoFill credit cards with
Wallet & Apple Pay. The `Settings → Apps → Safari → AutoFill → Saved
Credit Cards` row in iOS 26.3 carries the literal on-screen subtitle
`"Manage cards in Wallet & Apple Pay Settings."` Tapping the row in
sim bounces back to Settings root — the push to Wallet's panel
silently fails. The Wallet `+` ("Add Card") triggers the canonical
`"Could Not Set Up Apple Pay"` alert (same blocker as §15 — no
Secure Element on sim).

**Empirical evidence** (from
`sibb/simulator/sibb_probe_safari_autofill_creditcards.py`):

1. Settings → Safari → AutoFill panel is reachable and looks viable.
2. Tapping "Saved Credit Cards" row → bounces back to Settings root.
3. Wallet `+` → Apple Pay setup error (same §15 wall).
4. Host-side seed paths exhausted: SHA-1 hash sweep across every
   plausible keychain service name (`SafariCreditCard`,
   `com.apple.Safari.CreditCard`, `WBSCreditCardAutoFill`,
   `com.apple.creditcards`, …) returns ZERO rows. No
   `CreditCards.plist`, no `AutoFillCreditCard.*` file, no
   `com.apple.creditcards` access group on disk. The §13 Passwords
   trick has no analog for credit cards.
5. With an empty store, Safari's AutoFill chip above the keyboard
   never surfaces on a page with `<input autocomplete="cc-number">`
   (only the default `Previous / Next / Done` accessory bar shows).
   Empirically verified with a host-served numeric IP form.

**Probe artifacts** kept at `/tmp/sibb_cc_probe/` (screenshots, AX
JSON dumps, summary.json) and the reusable probe script lives at
`sibb/simulator/sibb_probe_safari_autofill_creditcards.py`
(`--step settings|add_card|inspect_kc|serve_form|all`). Re-run if a
future iOS version separates the AutoFill store from Wallet — that
would re-open the door.

**Implication for benchmark design**: payment forms in SIBB shop
tasks must use the same typed-field RSVP-style pattern. No
AutoFill-chip variation possible on sim. Cross-app "fetch the card
from somewhere else" patterns can still work via Files / Contacts
notes / a separate `/account/cards` page on the MockSite — those
don't depend on the iOS AutoFill subsystem.

**Reference**: `sibb_runs/safari_creditcards_autofill_probe.md` (full
1570-word report with sources). Cross-link from `§15` (Wallet /
Apple Pay) since the architectural unification ties these two walls
together.

---

## How to add an entry

When a new iOS / Xcode version surfaces a quirk:

1. Add a section here with the format above.
2. Cite the commit that introduced the workaround.
3. If it's a TCC / permission quirk, also cross-link from
   the runbook section B.
4. If it's an iOS-version-deprecation quirk (will eventually need
   replacement), raise an issue
   with the deprecation runway.
