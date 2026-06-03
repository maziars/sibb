# Agent tool / action notes

Empirical nuances learned while using SIBB's manual replay tool against
the iOS 26.3 simulator. **This file is the source of truth for the
eventual `SYSTEM_PROMPT`** (`CLAUDE.md` → Phase 2 item #8). When we
codify the prompt, sections here become tool descriptions and behavior
notes for the model.

Append findings as we encounter them. Each note should describe what
*actually* happens on this stack, not what the action's name suggests.

---

## TAP

- `TAP @eXXXX` — preferred form. Refs are valid for **one** observation
  only; they regenerate every `observe()` call.
- `TAP "Label"` — substring match by accessibility label. Useful when
  the label is stable and you want to be robust across observations.
- `TAP (x, y)` — raw-coordinate tap. Bypasses AX entirely. Use when
  you need to tap a point that has no AX element (decorative imagery,
  map locations, custom canvases) or a dimmed background.
- The on-screen keyboard is filtered out of observations. Do not try
  to tap individual keys — always use `TYPE`.

## TYPE

- `TYPE @eXXXX "text"` — taps the target to focus, waits ~400 ms for
  the keyboard, then types.
- Auto-focus requires the field to be tappable. If the field is inside
  a modal that hasn't been opened yet, navigate to that modal first
  with TAP, then TYPE.

## SCROLL

- **Direction is *content-direction*, NOT finger-direction.** `SCROLL down`
  reveals more content below (which physically requires the finger to drag
  upward). `SCROLL up` reveals content above. Same for `left`/`right`.
  This matches every web/mobile scroll API (Selenium, Playwright, Appium).
- **Element ref IS honored** as of D3. `SCROLL @e0033 down` scrolls
  inside the element with frame `e0033` — useful for horizontal
  carousels, embedded scroll views, map panning, and any nested
  scrollable region that isn't the main app body. Gesture is bounded
  by the element's frame (80% of height/width, 10% inset from each
  edge), so the swipe stays inside the element's hit area. Without a
  ref, `SCROLL down` falls back to a whole-app swipe.
- **`amount` = number of swipes**, default 1. `SCROLL down 3` does three
  swipes. Each swipe is roughly one screen-height of content (whole-app)
  or one element-height of content (when `@ref` is supplied).
- **Recognizing scrollable regions in observations**: look for elements
  with role `[scroll]` — covers `UIScrollView` / `UITableView` /
  `UICollectionView`. After D3, these appear in observations even when
  unlabeled (the unlabeled-`[scroll]` filter was removed). Practical
  cues:
  - **Long visible list of `[cell]` elements** → the parent is a scroll
    container, even when the `[scroll]` element itself is the parent.
  - **Frame shape hints overflow direction**: wider-than-tall typically
    indicates a horizontal carousel; taller-than-wide a vertical feed.
    XCUITest doesn't expose `contentSize`, so the agent has to infer
    from frame proportions and surrounding context.
  - **Multiple `[scroll]` elements in one screen** → nested scrolls.
    Pick the innermost one whose frame contains the content you want
    to move, not the outermost (which scrolls the whole page).
- **Picker wheels need `ADJUST`, not `SCROLL`.** Date/time pickers
  expose `[pickerWheel]` role; use `ADJUST @ref up/down N` to nudge
  the selection by N positions. Using `SCROLL` on a picker wheel
  works but is imprecise.
- **Scrollable but AX-invisible**: see "App-specific AX visibility
  quirks" below. WKWebView page content (Safari), MKMapView panning
  (Maps), and custom-drawn canvases have no AX-visible scroll
  container. Use whole-app `SCROLL` (no ref) or `TAP (x, y)` with a
  computed coordinate; element-targeted `SCROLL @ref` won't reach
  inside these surfaces.

## SWIPE

- **Direction is *finger-direction*** (the gesture itself), opposite of
  SCROLL. `SWIPE down` drags from top to bottom — used for things like
  swipe-down-to-dismiss a sheet, swipe-down to reveal Notification Center.
- **Element ref IS honored** as of D3 — same coordinate-bounded
  mechanism as `SCROLL @ref`. `SWIPE @e0042 left` drags the finger
  leftward inside the element's frame. Useful for things like:
  - Dismissing an app-switcher card: `SWIPE @<card> up` force-quits
    the app (vs `PRESS app_switcher` which just opens the carousel).
  - Sheet-grabber expand/collapse: `SWIPE @<grabber> up/down` (see
    "Sheet Grabber" pattern below).
  - Carousel paging that needs a flick rather than a discrete scroll.
- **Without a ref**, falls back to whole-app swipe (gestures across
  the screen center). Useful for system gestures that aren't tied to
  any AX element:
  - Pulling down Notification Center (whole-app `SWIPE down` from top)
  - Navigating between SpringBoard pages (whole-app `SWIPE left`/`right`)
  - Dismissing the app-switcher itself (whole-app `SWIPE up`)

## PRESS — hardware / gesture

Each `PRESS` waits ~1.4 s for the foreground transition to settle, so
the next observation reflects the post-transition state.

- **`PRESS home`** — `XCUIDevice.press(.home)`. Returns to the home
  screen (SpringBoard).
  - **Important:** this *backgrounds* the current app, it does not close
    it. Re-launching the app via `TAP "<AppName>"` from home **resumes
    the previous in-app state**, including modal alerts, half-filled
    forms, scroll positions. So `PRESS home` is **not an escape from app
    state** — only from the screen.

- **`PRESS back`** — left-edge swipe (interactive-pop gesture).
  - Pops the navigation stack in apps using `UINavigationController`
    (Reminders, Settings, Mail, Maps, …). Does nothing in apps without
    nav stacks.
  - **Empirically dismisses confirmation alerts.** Verified working on
    Reminders' "Are you sure you want to discard changes?" prompt that
    has no visible Cancel button — `PRESS back` returned the user to
    the edit screen unchanged.
  - First-choice escape for one-button alerts.

- **`PRESS app_switcher`** — swipe-up-and-hold from the bottom edge.
  - Opens the recent-apps carousel. From here, `TAP <card>` switches
    apps and `SWIPE @<card> up` force-quits.
  - The right way to **actually exit an app** (e.g. to abandon Reminders
    edit state) is `PRESS app_switcher` → swipe up the Reminders card.

## DONE / FAIL

- Terminal — the action loop stops and the scaffold runs the AFTER
  verifier. No further actions accepted in the episode.
- `DONE "summary"` — agent claims success.
- `FAIL "reason"` — agent gives up. Required for "impossible" tasks
  (the task generator may produce these as
  `expected_behavior = "fail_gracefully"`).

## Refs (`@eXXXX`)

- Stable **only within the current observation**. Always re-read the
  latest observation before issuing a TAP/TYPE — references from an old
  observation are meaningless.
- Four-digit zero-padding is for visual alignment; the number has no
  semantic meaning (it's a counter assigned during AX-tree traversal).

---

## Observation gaps to know about

- **Async events between turns are invisible.** A notification banner
  that pops up and auto-dismisses between two actions can disappear
  without ever entering an observation. The loop is strict observe →
  act → observe; nothing happens in the background. (Locked in by
  `CLAUDE.md` → Key Design Decisions; full ramifications in
  `PHASE1_PROGRESS.md` → Design assumptions.)
- **Mid-animation snapshots.** The first observation right after an
  action that triggers a transition may show 0 or very few elements.
  The next observation (or a small wait + observe again) catches up.
- **Keyboard is filtered.** `keyboard_visible: True` in the header
  tells you it's on screen, but the keys themselves are not in the
  element list.
- **Hittability is NOT in the snapshot path.** Some elements may
  appear tappable in the observation but be visually occluded. If a
  `TAP` looked successful (no error) but the screen didn't change,
  the element was likely covered by an overlay — re-observe and try a
  different target.

---

## SF Symbol icon vocabulary

iOS often exposes a button's accessibility label as the underlying
**SF Symbol name** instead of a human-readable label (when the developer
didn't set `accessibilityLabel` explicitly). The agent will see strings
like `"circle"` or `"ellipsis"` and has to know what they typically mean.

Until VLM enrichment lands (Phase 3), include the table below in the
`SYSTEM_PROMPT` so the agent maps symbol names to likely intent.

| SF Symbol label (as seen in AX) | Typical meaning |
|---|---|
| `circle` | Empty checkbox / mark-as-complete (Reminders) |
| `checkmark.circle`, `checkmark.circle.fill` | Completed / selected |
| `circle.inset.filled`, `largecircle.fill.circle` | Selected radio option |
| `ellipsis`, `ellipsis.circle` | More / overflow menu |
| `xmark`, `xmark.circle`, `xmark.circle.fill` | Close / clear field / delete |
| `plus`, `plus.circle`, `plus.circle.fill` | Add / new item |
| `chevron.right`, `chevron.left` | Disclosure / back |
| `chevron.up`, `chevron.down` | Expand / collapse |
| `square.and.arrow.up` | Share / export |
| `square.and.pencil` | Compose / new document |
| `magnifyingglass` | Search |
| `gearshape`, `gear` | Settings |
| `info.circle` | Info / help |
| `star`, `star.fill` | Favorite (unset / set) |
| `flag`, `flag.fill` | Flag / priority marker |
| `trash` | Delete |
| `pencil` | Edit |
| `line.3.horizontal` | Sort / list options / hamburger menu |
| `paperplane`, `paperplane.fill` | Send |
| `mic`, `mic.fill` | Voice input |
| `person.crop.circle`, `person.fill` | Profile / account |
| `calendar` | Date picker / Calendar tab |
| `clock` | Time / recents |

**Caveat:** the mapping is conventional, not guaranteed. A custom app
can use any symbol for any meaning. Confirm via the symbol's
neighbouring context (label nearby, screen role) before acting.

Suffix variants `.fill` / `.circle` / `.square` / `.slash` are
decorative — the underlying noun is what matters. `bell` and
`bell.slash` are still both "notifications," with the slash variant
typically indicating "muted/off."

---

## Modal / overlay dismissal — empirically

| Pattern | Dismiss method |
|---|---|
| Two-button alert (Cancel / Keep Editing / etc.) | TAP the cancel-equivalent button |
| One-button alert with explicit OK | TAP the OK button |
| One-button alert with no cancel and you want to bail | `PRESS back` (verified on Reminders' "discard changes?" prompt) |
| Action sheet (Cancel listed in the sheet) | TAP "Cancel" or sometimes TAP (x, y) outside |
| Date / time picker | Look for "Done" or "Cancel" in the toolbar |
| Share sheet | TAP "Cancel" or swipe down |
| System permission alert (SpringBoard, e.g. "Allow Location?") | Must pick a button — no tap-outside dismissal |
| In-app modal sheet with handle | Swipe down from the top of the sheet |

When in doubt: try `PRESS back` before resorting to `PRESS home` or
force-quit via app_switcher, because the latter two preserve the in-app
state and reopening the app brings the same alert back.

---

## Common iOS UI patterns the agent should recognize

### Bottom sheets with a "Sheet Grabber" handle

Many iOS apps (Maps, Music's Now Playing, Photos' info sheet, Files'
preview, sometimes Reminders' date picker, third-party apps) use a
bottom sheet that **starts collapsed** and reveals more content as
it's expanded. The handle at the top is exposed in AX as:

```
@eXXXX [btn] "Sheet Grabber" = Half screen   @(<cx>, <cy>)
@eXXXX [btn] "Sheet Grabber" = Collapsed     @(<cx>, <cy>)
@eXXXX [btn] "Sheet Grabber" = Full screen   @(<cx>, <cy>)
```

The `value` after `=` is the *current* state. Three states cycle in
order: **Collapsed → Half screen → Full screen → Collapsed → …**

To change state, the agent can:

1. **`TAP @<Sheet Grabber>`** — toggles to the next state. The simplest
   action; one tap moves Collapsed → Half, another moves Half → Full.
2. **`SCROLL up` (whole-app)** — usually expands the sheet to its
   larger state, same effect as dragging the grabber up.
3. **`TAP` any input inside the sheet** (e.g. a search field) — many
   apps auto-expand the sheet to Full when their content is focused.
   This is the most "natural" path when the agent's goal is to search
   or pick an item.

**Heuristic for the agent**: if a screen looks suspiciously sparse and
contains an element labelled "Sheet Grabber", the rest of the relevant
UI is hidden inside the sheet. Expand it before concluding the screen
has nothing useful. This is *not* a Maps quirk — it's a common iOS
pattern (`UISheetPresentationController`).

### Navigation bar back

Most apps using `UINavigationController` show a back chevron / labelled
button at the top-left (`@eXXXX [btn] "Back"`). `TAP`-ing it pops the
current screen, equivalent to `PRESS back` but explicit. Prefer the
explicit button when it's visible — it's more predictable than the
edge-swipe gesture.

### Tab bars

Bottom tab bars in apps (e.g. Health, Photos, App Store) expose each
tab as a `[btn]` or `[tab]` with the tab's label. They're stable
across screens within the app and are the cheapest navigation between
top-level sections.

## App-specific AX visibility quirks

Some apps expose much less than others through the AX tree. This is a
WebKit / MapKit / UIKit-rendering limitation, not a SIBB bug.

- **Safari** — Web content is rendered in `WKWebView`. Only elements
  with explicit ARIA roles or semantic HTML (`<button>`, `<a>`, `<h1>`,
  labelled `<input>`, etc.) appear in the AX tree. Pages built with
  generic `<div>` markup expose nothing. **Visible**: URL bar, tabs,
  share button, bookmark menu, reader-mode buttons. **Invisible**:
  most page content, links, form fields without labels. Tasks should
  use the Safari chrome (open URL via address bar, tap share, etc.),
  not page interaction. WebDriverAgent works around this by speaking
  the Web Inspector remote-debug protocol — separate channel we don't
  have.

- **Maps** — `MKMapView` renders the map via `CALayer`, not `UIView`.
  **Invisible**: streets, POIs, businesses, landmarks, the map itself.
  **Visible (always)**: Map Modes button, Tracking button, account
  button, the bottom-sheet's collapsed search field and Sheet Grabber.
  **Visible only after engaging the bottom sheet**: recents,
  favorites, suggested places, search results, Directions, transit
  overlays. The bottom sheet starts collapsed — only the search field
  is showing — and the agent must either:
    1. `TAP` the search field to focus it (auto-expands the sheet to
       full screen and shows recents + suggestions), OR
    2. `TAP` the "Sheet Grabber" handle (toggles Collapsed → Half →
       Full), OR
    3. `SCROLL up` on the sheet to expand it.
  Programmatically-added `MKAnnotation` pins do show up if labelled.
  Tasks should be designed around search-based navigation; when an
  observation in Maps looks too sparse, the most likely cause is the
  bottom sheet being collapsed — not a bug, just unexpanded UI.

- **Custom-drawn views in any app** (canvases, signature pads, chart
  surfaces, game scenes) have the same problem — invisible to AX. The
  workaround is the same: design tasks around the surrounding UIKit
  chrome, or rely on a VLM (`AXEnricher.vlm_client`) for content
  awareness.

The agent can `TAP (x, y)` blindly on a Map or web view if it has some
external reason to know the coordinate (e.g. it computed coords from a
prior screenshot+VLM pass). Without VLM, content-aware actions in
these apps will fail by design.

## App-state nuances

- **`PRESS home` ≠ closing the app.** Background ≠ terminate. State
  persists.
- **To truly leave an app**: `PRESS app_switcher`, find the app's card,
  swipe up to force-quit.
- **Reminders preserves edit state** across backgrounding — an
  unfinished reminder reopens to the same form, including any alert
  that was on screen.
- **Spotlight Search**: an empty Spotlight has only the search field;
  results appear only after typing into it.
- **SpringBoard owns**: home screen, App Library, Spotlight, App
  Switcher, Control Center, lock screen, notification banners, system
  permission alerts. Whenever any of these are visible, `bundle_id`
  in the observation reports `com.apple.springboard`.

---

## SpringBoard IconState — dock vs page de-duplication

When randomizing the home-screen `IconState.plist`, an app placed in
the dock (`buttonBar`) **must NOT also appear on any page** in
`iconLists`. iOS deduplicates silently and the visible dock ends up
empty — the plist still contains the dock entries on disk but
SpringBoard refuses to render them.

`sibb_randomize_layout.py` handles this automatically: after the dock
is randomized, it strips any of the chosen dock-apps out of every page.
If a future tool edits the plist directly, it must do the same — or
the dock will mysteriously appear empty after boot.

## State setup / verification — use Apple's framework APIs, never the SQLite stores

**Lesson learned the hard way (2026-05-14)**: writing rows directly into
an app's CoreData SQLite store does NOT produce a usable state. Reminders
in particular maintains *parallel* metadata that's invisible to a row-only
diff:

- The row in `ZREMCDBASELIST` / `ZREMCDREMINDER` (the obvious part).
- `ZIDENTIFIER` — 16-byte UUID per row.
- `ZPARENTACCOUNT` — relationship FK that mirrors `ZACCOUNT`.
- `ZSORTINGSTYLE` — e.g. `"manual"`.
- `ZREMCDACCOUNTLISTDATA.ZORDEREDIDENTIFIERMAP` — **NSKeyedArchiver-encoded
  `REMOrderedIdentifierMap`** listing the UUIDs of every list the account
  "knows about." Without an entry here, Reminders ignores the row.
- `ZRESOLUTIONTOKENMAP_V3_JSONDATA` — per-row CRDT replica state JSON.
- `ZCKDIRTYFLAGS` / `ZCKCLOUDSTATE` — CloudKit mirror state.
- `Z_PRIMARYKEY.Z_MAX` per `Z_ENT` — must be bumped or future CoreData
  writes collide on the PK we used.

Maintaining all of this from outside the framework is a losing battle:
it's iOS-version-specific, NSKeyedArchiver is annoying to round-trip in
Python, and any one missing piece causes Reminders to silently drop the
row. The same applies to Calendar (CalendarEventStore), Contacts (a
similar archived manifest), Photos, Mail, etc.

**Right answer: drive the OS framework API from inside the XCUITest target.**
Our test runner has full `EventKit` / `Contacts` / `CalendarKit` / etc.
access just like a normal app. We expose Apple-API-backed commands over
the existing Unix socket:

| Command (JSON) | What it does |
|---|---|
| `{"type":"create_list","name":…}` | `EKCalendar(for: .reminder)` + `saveCalendar` |
| `{"type":"create_reminder","title":…,"list":…,"priority":…}` | `EKReminder` + `save` |
| `{"type":"wipe_reminders"}` | `fetchReminders` → bulk remove → `removeCalendar` for user lists |
| `{"type":"list_lists"}` | `store.calendars(for: .reminder)` |
| `{"type":"list_reminders","list":…}` | `fetchReminders` filtered by calendar.title |

Apple's API maintains the manifest, CRDT, CK mirror, and PK counters
*atomically*. Setup is fast (~100 ms per write), version-robust, and
symmetric — the same channel handles reads, so the verifier never needs
to know the schema.

### Required wiring for any EventKit-style framework on iOS 17+

Three things must be in place or the runner gets dialogs / denial. Our
`XCUITestReader.start()` handles all three automatically.

1. **TCC pre-grant via `simctl privacy` BEFORE xcodebuild launches.**
   Granting after the runner is already running still triggers iOS's
   transparency dialog on the first call. `ensure_runner_permissions(udid)`
   runs `simctl privacy grant reminders / calendar / contacts
   com.sibb.tests.xctrunner` *before* the test runner spawns.

2. **`NSRemindersUsageDescription` in the runner's `Info.plist`.**
   Xcode auto-injects this for test runners (so do `NSCalendarsUsageDescription`,
   `NSContactsUsageDescription`, etc.). NSRemindersFullAccessUsageDescription
   is NOT auto-injected — so prefer the legacy `requestAccess(to: .reminder)`
   API (deprecated, but still works on iOS 17+ and only needs the
   auto-injected key). If we ever need iOS-17 fine-grained permissions
   that *do* require a new usage-description key, inject via
   `INFOPLIST_KEY_*` in `project.yml` or a custom Info.plist.

3. **Auto-dismiss the transparency dialog after first access.** Even with
   TCC=Allowed and the right Info.plist key, iOS 17+ still posts a
   non-blocking "<App> would like to access your Reminders" dialog after
   the first programmatic call. EventKit returns `granted=true` and our
   operation succeeds, but the dialog sticks until tapped and pollutes the
   agent's view. Our Swift `dismissPermissionDialogs()` walks Springboard's
   snapshot for `Allow / OK / Continue` buttons and taps them after every
   EventKit-touching command.

When we add a new app's handler (Calendar, Contacts, etc.):

- Add the relevant `simctl privacy grant <service>` call in
  `ensure_runner_permissions`.
- Use the Apple framework (EventKit / Contacts / etc.) from Swift.
- Call `dismissPermissionDialogs()` at the end of each command.
- Define the spec entry types (`{"type":"create_event", …}`) and wire
  them into the Python handler class.

The whole pattern is in `RemindersHandler` and the EventKit commands in
`sibb_xcuitest_setup.sh`. Copy that shape for new apps.

## Verifier hazards (Phase 1 only)

- Verifier reads the iOS 26 CoreData store. Reminders DB layout: lists
  in `ZREMCDBASELIST.ZNAME`, items in `ZREMCDREMINDER` joined via
  `ZLIST = ZREMCDBASELIST.Z_PK`.
- Multiple `Data-<UUID>.sqlite` files can coexist; we pick the one
  with user data (`find_db` in `sibb_verify_reminders.py`).
- `ZFLAGGED` always 0 without iCloud — flagged checks are informational
  only, not part of pass/fail.
- Verifier-BEFORE may already pass for tasks reusing list names across
  the same session (Phase 1 doesn't reset state between episodes).
