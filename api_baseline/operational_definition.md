# Operational definition — "API-doable" under iOS

This document gives a precise, falsifiable definition of when a SIBB task is
*API-doable* on iOS. It is the rubric the second-rater
(planned for v2) applies when reproducing the labels in
`classification.yaml`, and it is the methodological commitment the paper
makes.

## §1. The core definition

> **A SIBB task is _API-doable_ iff there exists a sequence of calls from a
> public, headers-published Apple SDK that, given the task's input
> parameters, produces a state mutation or returned value verifying the
> task's end condition — without requiring user gesture, on-flow system-modal
> confirmation, or screen scraping of another app's UI.**

Five terms are load-bearing and must be unpacked. The rater applies them
literally.

* **Public, headers-published Apple SDK.** The class or function is
  declared in a public Apple SDK header (`<Foundation/Foundation.h>`,
  `<EventKit/EventKit.h>`, `<Contacts/Contacts.h>`, `<MapKit/MapKit.h>`,
  etc.) shipped with the latest Xcode SDK and is NOT annotated
  `__attribute__((unavailable))`, `NS_UNAVAILABLE`, or
  `@available(*, deprecated)`. This is a *documentary* test, not an
  App-Store-Review test (see §1.bis on why).
* **Verifying the task's end condition.** The verifier in
  `sibb/benchmark/sibb_verify.py` is the ground truth. If the verifier
  passes on the API-only call sequence, the task is API-doable.
* **User gesture.** A post-launch human touch / key / pointer / hardware-
  button event. *Not* counted as a gesture: programmatic `URLSession`
  redirects; `MKMapItem.openInMaps(launchOptions:)` (a foreground
  app-switch with no human input); Universal Links resolved by the
  system before the agent is awake; `simctl openurl` invoked from the
  test harness.
* **On-flow system-modal confirmation.** A modal that gates the
  call sequence *during* the task. **First-launch authorization
  prompts** (TCC dialogs: Calendar, Contacts, Photos, Location) are
  **explicitly exempt** — the test harness pre-grants them via the
  existing `ensure_runner_permissions(udid)` path, and they would
  not appear in a deployed agent's steady-state operation.
* **Screen scraping of another app's UI.** Reading rendered pixels,
  the AX tree, or memory of *another running app's* UI surface.
  *Not* counted as scraping: reading shared persistent system stores
  through their public read APIs (`CNContactStore` for contacts,
  `EKEventStore` for events/reminders, `MKLocalSearch` for places).
  `UIPasteboard` access is treated as a shared system store under
  this definition — but no task in the v1 corpus depends on
  pasteboard reads, so the question is academic for now.

### §1.bis  Why headers-grounded, not App-Store-Review-grounded

An earlier draft tied C1 to "App-Store-allowed." A reviewer correctly
flagged this as a moving target: App Store Review has historically
rejected uses of public APIs (CallKit outside VoIP, NetworkExtension
for traffic inspection, LocalAuthentication for non-auth flows) without
the header itself being marked unavailable. SIBB's claim cannot live
or die by what AR happens to approve on a given week.

We rebase C1 on the **header presence + availability annotation** test,
which is mechanically checkable from `xcrun --sdk iphoneos --show-sdk-path`
and gives raters a deterministic input. The paper notes separately that
AR can reject anything regardless — that is a deployment risk, not a
platform-ceiling claim.

## §2. The four cuts

The definition is parameterized by *which* Apple APIs count. We commit to
**C1**, the strictest "deployable on a normal user device" cut. The other
three cuts are documented so the paper is honest about how its numbers would
shift under different choices.

| Cut | Includes                                              | Excludes                                    | Why this cut |
|-----|-------------------------------------------------------|---------------------------------------------|--------------|
| **C1 — load-bearing for SIBB v1** | Public, headers-published, non-deprecated APIs only | Private API, MDM-only, supervised-device, Shortcuts URL chain | "Will run on a normal iPhone with a third-party agent app installed from the App Store" — the deployable-product reality test |
| **C2** | C1 + private APIs that work empirically              | Truly private + completely undocumented      | Permissive "what works in practice" — reveals upper bound but cannot ship |
| **C3** | C1 + MDM-supervised entitlements                     | non-MDM consumer device                      | Enterprise scope only — not a consumer agent |
| **C4** | C1 + Shortcuts URL chain (`shortcuts://`)            | Strict cross-app dispatch                    | Permissive "any indirect path" — Shortcuts is a quasi-public IPC channel |

**Why C1?** Three reasons:

1. SIBB's goal is to characterize the ceiling a *deployable iOS agent* faces.
   C2 / C3 / C4 numbers would describe scenarios that cannot ship to a normal
   user, undermining the paper's "what can a productized agent actually do"
   framing.
2. C1 maps directly to a documentary, rater-verifiable test (header presence +
   no `__attribute__((unavailable))`), eliminating the moving-target problem
   that "App-Store-allowed" introduced.
3. Cohen's κ is more stable under C1 because the inclusion criterion is
   citation-grounded (Apple SDK headers) rather than empirical
   ("does it work for me today"), which raters can verify independently.

Each `ui_only` task in `classification.yaml` carries a `c4_would_flip` field
so a reviewer can immediately see whether the Shortcuts-permissive cut would
change our headline numbers. At v1 the answer is "no" for every ui_only task
— but the field exists for honest accounting.

## §3. The three-class taxonomy

Each SIBB task gets exactly one of these labels:

* **`api_only`** — Cleanly solvable under C1. Some sub-step may need a
  workaround (e.g. our `list+wipe+create` for the missing `update_event`
  Swift handler), but the **platform** supports every step.
* **`ui_only`** — Structurally requires UI driving. No public Apple SDK call
  sequence mutates the verifier-checked state. **Agent scores 0% by
  construction (under C1, with the toolset we expose).**
* **`hybrid`** — Some sub-steps API-doable, but the verifier cannot be
  satisfied without UI under our currently-exposed toolset.

The v1 headline experiment scores only `api_only` and `ui_only` tasks
(29 total). The `hybrid` set (2 Calendar update tasks) is included in the
second-rater κ pass so the rater faces a 3-class target — a more demanding
exercise than 2-class — but excluded from the headline numbers because we
do not yet have a Swift `update_event` handler.

### §3.bis  The list+wipe+create caveat

Several `api_only` Reminders generators (`gen_make_reminder_recurring`,
`gen_set_priority`, `gen_add_notes_to_reminder`) use a Python
`list → delete → re-create` workaround for the missing `update_reminder`
Swift handler. **This is a measurement hazard.** The verifier's
`_check_identity` family was designed against UI mutations that preserve
`eventIdentifier`; recreating with a fresh identifier can:

1. Silently pass an `identity` check that uses `exclude_match` on the target
   title, because the deleted-and-recreated target is excluded from the
   diff over baseline.
2. Silently fail an `attribute_list_length` check on recurring reminders if
   the agent re-creates an occurrence instead of the master.
3. Silently pass a `time_within` check off the fresh `creationDate`.

**Mitigation, in order of strictness**:

* The runner's tool dispatcher **MUST copy every public field from the
  original listed item before re-create** (`dueDateComponents`, `notes`,
  `priority`, `recurrenceRules`, `alarms`, `location` etc.) and log
  `synthetic_update: true` plus the field-copy list to the trajectory.
* An L1 test (`sibb/tests/unit/api_baseline/test_synthetic_update_safety.py`,
  TODO) constructs a known-bad mutation (e.g. wipe-without-notes-copy)
  and asserts it fails on at least one of the affected verifier kinds.
* The paper's Methodology section names the workaround explicitly and
  reports any `synthetic_update` runs separately if pass rates diverge
  from non-synthetic-update tasks by >5%.

## §4. Why the API-doable distinction matters

The label decides what success means:

* On an `api_only` task, a failed run bounds the **agent-side ceiling** —
  parameter-binding mistakes, wrong tool selection, hallucinated arguments,
  or (where applicable) workaround-related corruption. The agent could in
  principle have solved it; this iteration's reasoning came up short.
* On a `ui_only` task, a failed run reveals nothing about agent capability —
  it is the **platform-side ceiling** under C1. No agent, no matter how
  capable, could solve it through the API surface we expose.

The empirical *separation* of these two ceilings is SIBB's central
methodological contribution.

---

## §B. Worked borderline examples

Ten cases where the call goes one way or another and the rater needs to
follow the same reasoning we did. Each example pins one part of §1 to a
concrete decision.

### B1 — `gen_maps_search_to_contact`  →  `api_only`

> *"Look up Apple Park, then save its address to John Doe's contact under
> 'work'."*

* `MKLocalSearch.start()` is public. Returns `MKMapItem.placemark.postalAddress`
  as a `CNPostalAddress`. ✓
* `CNSaveRequest.update(_:)` + `CNContactStore.execute(_:)` is public. ✓
* Verifier matches on (contact name, postal address fields). ✓

**No UI required for either half.** This is the canonical cross-app API success.

### B2 — `gen_message_save_sender`  →  `ui_only`

> *"Read the latest received message and save the sender's name and phone to
> a new contact."*

* No public iOS framework reads inbound iMessage bodies. The CKMessage /
  IMHandle classes Messages.app uses are private. Messages Extensions can
  read the **draft being composed**, not received history. Apple Developer
  Forum 42310 confirms this explicitly. ✗
* ILMessageFilterExtension exists but operates *before* delivery on
  unrecognized senders, in a separate filter process, and exposes only the
  message body / sender handle as a one-shot filtering decision — not as
  an ongoing read interface for a third-party agent app.

The agent's toolbox has no instrument that can satisfy "extract the sender
from the latest received message." **0% by construction under C1**.

### B3 — `gen_change_event_title`  →  `hybrid` (under our v1 toolset)

> *"Open Calendar. Rename the 'Date Night' event to 'Dinner & Movie'."*

* `EKEvent.title` is a public R/W property. ✓
* `EKEventStore.save(_:span:commit:)` is public. ✓
* **However**, `SIBBServer.swift` exposes no `update_event` command. Our
  workaround (`list → wipe → create`) would re-create the event with a new
  `eventIdentifier`, which is fine for SIBB's verifier (matches on title
  + time, not on identifier). ✓

So: the **platform** supports it (`api_only` under a broader cut), but our
**exposed tools** require the workaround.

**Acknowledged asymmetry**: the v1 slate scores parallel Reminders update
tasks (`gen_make_reminder_recurring`, `gen_set_priority`, `gen_add_notes_to_reminder`)
as `api_only` *via the same workaround*. The reason this Calendar task is
labeled `hybrid` instead is that EKEvent has additional iOS-side fields
(attendees, alerts, attachments, source calendar identity) that the
workaround's field-copy list does not cleanly preserve under all generator
seeds we ran. This asymmetry is real, surfaced to the second-rater, and
documented in the paper Methodology.

### B4 — `gen_safari_bookmark_specific_url`  →  `ui_only`

> *"Bookmark https://example.com in Safari, in the 'Travel' folder."*

* Safari bookmarks have no public iOS CRUD API outside of Safari Extensions —
  and Safari Extensions on iOS don't expose bookmark mutation from a third-
  party app context. `WKWebView` (which is what a third-party app would
  embed) is not Safari and has no shared bookmark store. ✗
* iOS Shared Web Credentials / iCloud Keychain expose password autofill,
  not bookmarks.

A productized third-party agent has no path to mutate MobileSafari's
bookmark database. **`ui_only`.**

### B5 — `gen_create_contact_with_address`  →  `api_only`

> *"Create a new contact 'Erin Wu' with phone (555) 010-9876 and the home
> address 1100 W Sunset Blvd, Los Angeles, CA 90012."*

* `CNMutableContact` with `givenName` / `familyName` / `phoneNumbers` /
  `postalAddresses` (all public R/W) — single `cn.create_contact` call. ✓

Trivially API-doable. Listed only to contrast with the `ui_only` Messages
tasks that *look* equally simple in instruction text.

### B6 — `gen_message_to_contact_to_maps`  →  `ui_only`

> *"Send a message thread says: 'Need directions to Joe Smith's place.' Look
> up Joe Smith's address and start turn-by-turn driving directions to it."*

* Inbound message read — `ui_only` (B2).
* Address lookup — `api_only` (B5-style).
* Turn-by-turn navigation activation — contested. `MKMapItem.openInMaps(launchOptions:)`
  opens Maps but does not auto-start navigation (Apple Developer Forum
  724907). However `MKDirections.calculate()` produces an `MKRoute`
  programmatically, and writing the rstorage active-route blob is what
  the Phase A++ verifier reads. A reviewer could argue the Maps half has
  an API path. **Open probe**: confirm whether the rstorage blob is writable
  from a third-party process outside Maps.app under C1. Either way, the
  message-read half is still UI-required under C1 (Forum 42310).

Because the message-read leg fails C1 regardless, this task is **`ui_only`**.
The verifier's `geo_within_m` check happens to land on Maps state, but
that's downstream of the unreachable message-read.

### B7 — `gen_list_due_today`  →  `api_only`

> *"How many reminders are due today across all my lists?"*

* `predicateForIncompleteReminders(withDueDateStarting:ending:)` on
  `EKEventStore` is public. Returns an `[EKReminder]`. ✓
* `agent.answer` carries the count. ✓

**Pure read.** Even tasks that are "just lookups" need to be classified;
this one is the simple case.

### B8 — `gen_set_event_location`  →  `hybrid` (mirrors B3)

> *"Change the 'Dentist Appointment' event location to '123 Main St, Suite 4'."*

Same structure as B3. `EKEvent.location` is public R/W; no `update_event`
in our Swift wrapper. The platform supports it; our exposed tools require
the `list+wipe+create` workaround. Same EKEvent field-copy hazards as B3.

Included to surface rater sensitivity to "wrapper gaps" vs "platform gaps."
The paper distinguishes these in §3.bis of the Methodology section.

### B9 — `gen_set_contact_birthday_no_year`  →  `api_only`

> *"Set Jane Smith's birthday to October 5 — leave the year unset."*

* `CNContact.birthday` is a public `DateComponents?` field. A
  `DateComponents(year: nil, month: 10, day: 5)` is well-formed under
  iOS's serialization to the year-omitted sentinel. ✓
* Swift has `update_contact` so no workaround needed. ✓

Easier than B3/B8 — direct update path. **`api_only`**.

### B10 — `gen_make_reminder_recurring`  →  `api_only` (with caveat)

> *"Make the 'Pay rent' reminder repeat every month."*

* `EKReminder.recurrenceRules` is public. ✓
* `EKEventStore.save(_:commit:)` is public. ✓
* But: no `update_reminder` Swift handler → list+wipe+create workaround.

Under the §3.bis caveat: the dispatcher MUST copy `dueDateComponents`,
`notes`, `priority`, `alarms` from the listed item before save. With that
mitigation, **`api_only`**. The L1 test pinning the field-copy invariant
gates the runner against silent-pass regressions.

---

## §C. Process for the second-rater

A planned (v2) second-rater pipeline will run two LLM raters (Gemini 2.5 Pro and Claude
Opus 4.7) on the 29 scored tasks + 2 hybrid extras (31-task κ target). Each
rater sees:

1. This `operational_definition.md` document in full, at a **pinned commit
   hash**. The rubric is frozen before the κ pass starts.
2. The generator's *instruction string* (from
   `sibb/benchmark/sibb_task_generator_v3.py`).
3. The generator's *verifier checks* (from the same source — what the task
   actually requires to pass).
4. The generator's *flow* and *initial_state.expected_behavior* fields —
   needed to distinguish abstain-because-infeasible from abstain-because-failure.
5. **Nothing else.** The rater does NOT see `classification.yaml`.

The rater emits one label per task plus a one-sentence rationale.

### §C.bis  Pre-registration to avoid circular acceptance

The rubric (this document at the pinned commit hash) is **frozen before the
κ pass runs**. The published κ is whatever falls out of the first run. We
do **not** iterate on the rubric until κ ≥ 0.65; that would make the
statistic measure "consensus after coordination" rather than "independent
agreement," which would be circular.

If κ < 0.65 on the first frozen-rubric run, we publish that as the finding
("the API/UI distinction is fuzzier than the rubric admits") and the paper's
methodology section acknowledges the construct's limits. Subsequent rubric
revisions go into v2 with a new pre-registered κ pass.

**Interpretation per Landis & Koch (1977)**:
- κ ≥ 0.81: almost perfect
- 0.61 ≤ κ < 0.81: substantial
- 0.41 ≤ κ < 0.61: moderate
- 0.21 ≤ κ < 0.41: fair
- κ < 0.21: slight

We target ≥ 0.61 as a *minimum credible* finding; ≥ 0.81 would be a strong
result; below 0.41 would require treating the construct itself as a v2
research question.

## §D. What this definition deliberately does NOT cover

* **Performance.** A task may be `api_only` and still take 10× longer through
  the API than UI. We measure pass/fail, not wall-clock.
* **Reliability.** EventKit has bugs (cross-process sync, see
  `IOS_SIM_QUIRKS.md` §17). An `api_only` task may flake. v1 is single-seed;
  multi-seed control is v2.
* **Agent capability.** The label says nothing about whether an LLM can
  *reason its way* to the right tool call. `api_only` is a platform claim;
  agent-side failures are scored separately (the agent-ceiling).
* **System-prompt influence.** The agent's prompt frames the toolbox as
  "Apple frameworks." This may bias toward API thinking even on `ui_only`
  tasks. v1 reports raw pass rate; the prompt-ablation arm (neutral prompt,
  same toolbox) is v2.
* **Deployment legality outside iOS.** Android / macOS / Web have different
  ceilings. Cross-platform comparison is `sibb_paper_d2plus_landscape_2026-05-31.md`,
  not this rubric.
* **App Store Review.** AR can reject anything regardless of header
  availability (see §1.bis). The rubric is a platform-capability claim,
  not a shippability claim.

## §E. Citation lineage

* App Store Review Guidelines §2.5.1, §2.5.2 — public-APIs-only and
  self-contained restrictions (referenced but not load-bearing per §1.bis).
* Digital Markets Act, March 2024 (EU) p.14 — sandbox preservation is
  preserved under sideloading.
* Apple Developer Forum 42310 — inbound iMessage access.
* Apple Developer Forum 724907 — Maps turn-by-turn API gap.
* Saltzer, Reed, Clark 1984 — end-to-end argument grounds the verifier
  position.
* Landis & Koch 1977 — κ interpretation.
