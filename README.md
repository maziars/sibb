# SIBB

**Smartphone Interaction Benchmark for Bots** — a reproducible iOS benchmark for
evaluating AI agents on real iPhone-simulator tasks.

SIBB exercises language-model-driven agents on multi-step tasks across the
system apps that ship with iOS, using Apple's own XCUITest framework as the
action substrate and database/file-level ground truth for verification. Tasks
are procedurally generated with seedable distractor noise, so the corpus
remains a moving target after release.

---

## Examples

Two real episodes — Gemini 2.5 Flash driving the iOS Simulator end-to-end.
Each was captured at real-time playback (3 fps); verification reads ground
truth directly from the underlying data store.

<table>
<tr>
<td width="50%" align="center"><b>Complete a reminder</b></td>
<td width="50%" align="center"><b>Create a calendar event</b></td>
</tr>
<tr>
<td><img src="docs/media/complete_specific_reminder.gif" alt="agent completes a specific reminder" width="100%"/></td>
<td><img src="docs/media/create_event_with_title_time.gif" alt="agent creates a calendar event" width="100%"/></td>
</tr>
<tr>
<td><i>"Open Reminders. 'Update roadmap' is done — check it off."</i></td>
<td><i>"Open Calendar. Create an event titled 'Date Night' tomorrow from 3pm to 3:45pm."</i></td>
</tr>
<tr>
<td align="center"><sub>5 steps · verifier PASS (reads EventKit reminder store)</sub></td>
<td align="center"><sub>16 steps · verifier PASS (reads EventKit calendar store)</sub></td>
</tr>
</table>

---

## What's in the box

- **72 task generators** across 11 system apps — Reminders, Calendar, Contacts,
  Files, Photos, Health, Maps, Safari, Messages, Settings, Shortcuts
- **Cross-app workflows** such as `Messages → Contacts → Maps` (parse an
  address out of a message, save it to a contact's card, start navigation)
- **Database-backed verifiers** that read state directly from EventKit, the
  Reminders sqlite store, `Contacts.framework`, Maps' rstorage, the Files
  sandbox, and PhotoKit. No VLM-as-judge.
- **Persistent XCUITest server** built on Apple's first-party UI testing
  framework — full iOS accessibility-tree access, ~200–400 ms per observation
- **Procedural task generation** with seedable randomized springboard layouts,
  distractor records, and baseline noise
- **Uniform LLM driver** with async clients for Anthropic, Google, and OpenAI
- **~85 tests** across a four-layer pyramid: pure-Python unit tests,
  fake-reader integration tests, simulator-backed integration tests, and
  Swift JSON-envelope contract tests

---

## Quick start

### Prerequisites

- macOS with Xcode 26.x (or newer) and the iOS Simulator installed
- A booted iOS 26.x simulator — find its UDID with `xcrun simctl list devices`
- Python 3.9+ (the project is tested against the system Python at
  `/Library/Developer/CommandLineTools/usr/bin/python3`)

### One-time setup

```bash
export SIBB_UDID=<your-simulator-UDID>

# Build the XCUITest server bundle (Apple's UI-testing framework; lives in
# ~/SIBBHelper, outside this repo). Takes 1–2 minutes.
cd simulator
chmod +x sibb_xcuitest_setup.sh
./sibb_xcuitest_setup.sh "$SIBB_UDID"
```

### Inspect what the agent sees

```bash
cd benchmark
python3 sibb_inspect_screen.py "$SIBB_UDID" --bundle com.apple.reminders
```

This dumps the accessibility tree the way the scaffold tokenizes it for the
LLM — useful for sanity-checking what an agent is given on any iOS screen.

### Generate a task and run it

```bash
# Procedurally generate a task corpus
python3 sibb_task_generator_v3.py

# Run an LLM-driven episode (set your model's API key first)
export GEMINI_API_KEY=...
python3 sibb_assistant.py --task complete_specific_reminder \
                          --model gemini-2.5-flash --max-turns 15
```

The full setup walkthrough — TCC permissions, baseline cloning, prewarm
quirks, log paths — is in [`docs/SIBB_RUNBOOK.md`](docs/SIBB_RUNBOOK.md).

---

## Architecture

```
                                       LLM driver
                                  ┌──────────────────┐
                                  │  sibb_assistant  │  Anthropic / Google / OpenAI
                                  └────────┬─────────┘
                                           │
   Task generator                  Scaffold (AX bridge)              Verifier
   ─────────────                   ─────────────────────              ────────
   sibb_task_                      sibb_scaffold.py                   sibb_verify.py
   generator_v3.py     ───►        AXReader → AXEnricher → ───►       reads EventKit,
   72 generators                   AXTokenizer                        sqlite, plist,
   procedural noise                ~200–400 ms / observation          rstorage, CN…
                                           │
                                           ▼
                          Swift XCUITest server (persistent)
                                sibb_xcuitest_setup.sh
                                       │
                                       ▼
                              iOS Simulator (real apps)
```

### Why XCUITest

XCUITest is Apple's first-party UI-testing framework — the same one Apple
engineers use to test iOS itself. It exposes the full accessibility tree
(`AXUIElement` hierarchy, focus state, labels, frames) and supports
arbitrary tap / swipe / scroll synthesis. Unlike `idb` (Meta), which lost
iOS 26 compatibility, XCUITest is always current with the iOS SDK.

### Why database-level verification

A verifier that reads ground-truth state from EventKit / sqlite / rstorage
cannot be spoofed by an agent that "convinces" a VLM judge or whose final
screen happens to look correct. Every SIBB verifier checks the underlying
data store the app would persist to, not just the rendered UI.

---

## Repository layout

```
sibb/
├── simulator/        XCUITest server, baseline prewarm, AX probes
├── benchmark/        Task generation, scaffold, verifier, LLM driver
├── tests/            Four-layer test pyramid
│                       └── unit/   integration/   e2e/   contract/
└── docs/             Runbook, design notes, iOS quirks, app coverage
```

A more detailed tour:

- [`simulator/README.md`](simulator/README.md) — XCUITest server, simulator control
- [`benchmark/README.md`](benchmark/README.md) — scaffold, task grammar, verifier
- [`tests/README.md`](tests/README.md) — test pyramid, fake-reader fixtures

---

## Documentation

| Doc | Read it for |
|---|---|
| [`docs/SIBB_RUNBOOK.md`](docs/SIBB_RUNBOOK.md) | Complete setup, TCC, baseline cloning |
| [`docs/IOS_SIM_QUIRKS.md`](docs/IOS_SIM_QUIRKS.md) | Quirks of `simctl` / iOS / TCC that surprised us |
| [`docs/APP_COVERAGE.md`](docs/APP_COVERAGE.md) | Which iOS apps SIBB covers and why |
| [`docs/REAL_DEVICE_PORT.md`](docs/REAL_DEVICE_PORT.md) | Why this is simulator-only (real-device deployment investigation) |
| [`docs/MAPS_VERIFICATION.md`](docs/MAPS_VERIFICATION.md) | How the Maps active-route verifier works |
| [`docs/AGENT_TOOL_NOTES.md`](docs/AGENT_TOOL_NOTES.md) | Per-app notes on accessibility quirks |
| [`docs/research_summary.md`](docs/research_summary.md) | Design rationale for SIBB |

---

## Status

This is research code. It is actively developed; interfaces will change;
some scripts under `simulator/` are exploratory probes whose UDIDs are
hard-coded for the original developer's setup and need to be adjusted
before they will run on yours. The main paths — `sibb_assistant.py`,
`sibb_scaffold.py`, the task generators, the test suite — read their
simulator UDID from the `SIBB_UDID` environment variable and are portable.

The Swift `sibb_xcuitest_setup.sh` builds an Xcode project at
`~/SIBBHelper/` — that directory lives outside this repository and is
regenerated by the setup script when iOS / Xcode updates require it.

---

## A note on related work

SIBB is one of several iOS-agent evaluation efforts. UI-driving benchmarks on
adjacent platforms include
[AndroidWorld](https://github.com/google-research/android_world) (Android),
[OSWorld](https://github.com/xlang-ai/OSWorld) (Linux/macOS/Windows desktop),
and [WebArena](https://github.com/web-arena-x/webarena) (web). On iOS
specifically, [UINavBench](https://openaccess.thecvf.com/content/ICCV2025/html/Agrawal_UINavBench_A_Framework_for_Comprehensive_Evaluation_of_Interactive_Digital_Agents_ICCV_2025_paper.html)
(Apple, ICCV 2025) describes a 116-task benchmark on physical devices; as of
this writing it has not been publicly released. [ShortcutsBench](https://arxiv.org/abs/2407.00132)
(ICLR 2025) covers iOS API-call sequences without UI execution. SIBB
differs by combining (a) the public iOS Simulator as the substrate, (b)
database/file-level ground-truth verification, (c) procedural task
generation, and (d) cross-app workflow tasks.

---

## License

[MIT](LICENSE) — use freely; please cite this repository if it helps
your research.

---

## Author

Built by Maziar Sanjabi. Issues and pull requests welcome.
