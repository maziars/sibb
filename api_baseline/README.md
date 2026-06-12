# SIBB API Baseline (Option A+)

An **API-only counterpart** to the SIBB UI agent. It tries the same task
corpus using public Apple-SDK calls only (EventKit, Contacts, MapKit, …)
and produces the empirical numbers that ground SIBB's central
ceiling-separation claim.

This is **not** a replacement for the UI scaffold. It runs side-by-side as a
separate baseline. The UI scaffold under `sibb/benchmark/` is **untouched**.

## Why this directory exists

SIBB's paper-level motivation rests on durable structural arguments —
end-to-end interaction (Saltzer/Reed/Clark 1984), the long tail of user
intents (Scaffidi/Shaw/Myers 2005), production-agent convergence on UI
driving (every frontier lab independently chose UI), and the transparency
/ graduated-autonomy framing of WorkArena (Drouin et al. 2024). For the
durable framing in full, see
`memory/sibb_paper_motivation_synthesis_2026-05-31.md`.

What this directory adds is a single empirical handle on one consequence of
those arguments: that for *some* iOS tasks, the platform's currently-exposed
public SDK surface contains no call sequence that mutates the verifier-
checked state. The headline numbers from this directory feed Table 4 of the
paper. The structural arguments live elsewhere and survive whatever Table 4
ends up showing.

* On **22 API-doable** tasks (Reminders / Calendar / Contacts / Cross-app)
  the API agent should solve most of them. The gap to 100% bounds the
  *agent-side* ceiling.
* On **7 UI-required** tasks (Messages-driven, Maps turn-by-turn nav,
  Safari bookmarks) the API agent scores **0% by construction under our
  C1 cut**. This is the *platform-side* ceiling under the toolset we
  currently expose.

Predicted Table 4 in the paper looks like this:

| Subset                  |  n | UI baseline | API baseline                  |
|-------------------------|---:|-------------|-------------------------------|
| API-doable Reminders    |  8 | (run)       | ~70–90%                       |
| API-doable Calendar     |  6 | (run)       | ~70–90%                       |
| API-doable Contacts     |  6 | (run)       | ~70–90%                       |
| API-doable Cross-app    |  2 | (run)       | ~50–80%                       |
| UI-required             |  7 | (run)       | **0% (by construction, C1)**  |
| **TOTAL**               | 29 |             |                               |

The "by construction" floor is contingent on (a) Cohen's κ ≥ 0.61 between
the two LLM raters on the classification under the **frozen** rubric (see
§C.bis of `operational_definition.md` — pre-registration matters), and
(b) the L1 safety test for the list+wipe+create workaround passing (see
§3.bis of the operational definition).

## Layout

```
sibb/api_baseline/
├── README.md                   ← this file
├── classification.yaml         ← per-task API-only / UI-only / hybrid labels
├── operational_definition.md   ← 4 cuts + worked borderline examples
├── sibb_api_tools.py           ← MCP-shape tool defs + dispatcher
├── sibb_api_assistant.py       ← agent loop (fork of Anthropic loop.py)
├── sibb_api_runner.py          ← experiment driver over the 26-task slate
├── stitch_results.py           ← merge results.json files across runs
├── sibb_api_second_rater.py    ← Cohen's κ on the classification labels
├── sibb_api_code_act.py        ← code-act ablation (5 tasks)
└── results/                    ← gitignored; timestamped run dirs
```

Per-provider wire-format translators (Anthropic / OpenAI / Gemini) live in
`sibb/benchmark/sibb_llm.py` so that any future scaffold reusing native
function calling gets them for free. `sibb_api_tools.py` owns only the
MCP-shape tool *definitions* and the *dispatcher* that calls into
`sibb_llm.py` for wire-format translation.

Tests live in `sibb/tests/unit/api_baseline/`.

## Strict isolation from the UI scaffold

Six guarantees so this directory never collides with active UI work:

1. **No edits** to any file under `sibb/benchmark/` for purposes of this
   directory — except the planned `sibb_llm.py` extension (~290 → ~520
   LOC) for cross-scaffold operational gains and provider translators.
   That extension is owned by the separate `sibb_llm.py` work item in
   `memory/sibb_consolidated_findings_engineering_plan_2026-06-09.md`
   Part 5 and is scheduled to land *before* the API agent depends on it.
2. **No edits** to `SIBBServer.swift` or `sibb_xcuitest_setup.sh`. We use
   the 11 *existing* Swift handlers as-is.
3. **No edits** to existing test files. New tests in
   `sibb/tests/unit/api_baseline/`.
4. **Classification is YAML, not Python** — easy to merge if multiple people
   edit different sections.
5. **Results live in `results/`**, gitignored.
6. **All new files prefixed `sibb_api_*`** — easy to grep.

**Acknowledged soft leak**: the existing `ensure_runner_permissions(udid)`
TCC pre-grant path in `sibb/benchmark/sibb_state.py` enumerates services
declared by *handler* modules under `sibb/benchmark/`. If the API agent
ever needs a TCC service not declared by any handler we'd either edit
those handlers or duplicate the list. Today the 11 tools we expose are
all covered by existing handler declarations, so the leak is latent only.

## What the agent calls

Eleven tools, all wrappers around existing Swift handlers in
`SIBBServer.swift` plus one Python-only answer-submission tool:

| Tool                       | Swift handler         | Framework         |
|----------------------------|-----------------------|-------------------|
| `eventkit.create_event`    | `create_event`        | EventKit          |
| `eventkit.list_events`     | `list_events`         | EventKit          |
| `eventkit.create_calendar` | `create_calendar`     | EventKit          |
| `eventkit.list_calendars`  | `list_calendars`      | EventKit          |
| `eventkit.create_reminder` | `create_reminder`     | EventKit          |
| `eventkit.list_reminders`  | `list_reminders`      | EventKit          |
| `eventkit.create_list`     | `create_list`         | EventKit          |
| `cn.create_contact`        | `create_contact`      | Contacts          |
| `cn.list_contacts`         | `list_contacts`       | Contacts          |
| `cn.update_contact`        | `update_contact`      | Contacts          |
| `mklocalsearch.query`      | `geocode_query`†      | MapKit            |
| `agent.answer`             | (Python only)         | —                 |

† Public tool namespace is `mklocalsearch.query`; the existing Swift
handler retains its legacy name `geocode_query`. The dispatcher in
`sibb_api_tools.py` maps the public name to the Swift command type.

No `update_event` / `update_reminder` exist on the Swift side. Update
tasks fall back to Python `list → wipe → create-new` with a
`synthetic_update: true` flag in the trajectory log. **The dispatcher
MUST copy every public field from the listed item before the create**
(`dueDateComponents`, `notes`, `priority`, `recurrenceRules`, `alarms`,
EKEvent's `location` / `attendees` / `alarms`, etc.) — see §3.bis of
`operational_definition.md` for the measurement hazard and L1 safety
test that gates this behavior.

### Code-act ablation

A 5-task ablation uses Anthropic's `code_execution` tool instead of
native function calling. The exact beta-header version string and tool
ID are pinned at code-write time against Anthropic's current developer
docs. The ablation doubles as the v2 hybrid prototype (Approach C —
code-as-action wrapper).

## Protocol stack (locked decisions)

| Aspect              | Choice                                                  |
|---------------------|---------------------------------------------------------|
| Tool format         | MCP-style JSON Schema (`name`, `description`, `inputSchema`) |
| Wire format         | Per-provider translator in `sibb_llm.py` — Anthropic, OpenAI, Gemini |
| Tool invocation     | Native function calling with `strict: true` (Anthropic / OpenAI); plain FC (Gemini) |
| Tool search         | Anthropic Tool Search BM25 (`tool_search_tool_bm25_20251119`) with `defer_loading: true`. `agent.answer` plus 1–2 most-frequent tools remain **non-deferred** (Anthropic 400s on all-deferred catalogs). Rationale is ecological validity — at n=11 BM25 recall@5 ≈ 100%, so we expect no measurable accuracy lift; the scaffold *shape* matches v2 production deployment regime. |
| Agent loop          | Canonical sampling_loop, cap=8 turns, one tool/turn     |
| Parallel calls      | `parallel_tool_calls: false`                            |
| System prompt       | 4 XML sections (`<TASK>` / `<TOOLS>` / `<RULES>` / `<ENVIRONMENT>`). Target ~3–4K tokens once per-tool descriptions are written — leaner descriptions if the budget runs over. |
| Retries / backoff   | `tenacity`; provider SDK `max_retries=0` (CRITICAL — otherwise 49-attempt stacking) |
| Trajectory log      | Polymorphic JSONL with `type`-tagged records matching the UI baseline's vocabulary (`task`, `initial_state`, `verify_before`, `turn`, `action`, `verify_after`, `truncated`, `exhausted`, `llm_error`, `abort`) plus one new `tool_call` record type. `agent.answer` payload routes through the same `context["agent_answer"]` slot the UI baseline uses, so `verify_via(...)` sees a single shape regardless of baseline. |

Rationales for each choice live in the project memory under
`memory/sibb_consolidated_findings_engineering_plan_2026-06-09.md`.

## Where the data comes from

* **Task definitions** — `sibb/benchmark/sibb_task_generator_v3.py`. Each
  generator returns a `Task` dataclass with `instruction`, `initial_state`,
  `verify_checks`. The API runner picks generators by name from
  `classification.yaml`.
* **State setup** — `sibb/benchmark/sibb_state.py::apply_initial_state`.
  Same pre-runner the UI baseline uses, so the two agents face identical
  starting state. Socket-only; no AX read.
* **Verification** — `sibb/benchmark/sibb_verify.py`. Same `BaselineSnapshot`
  + `verify_checks` pipeline, including the same `context["agent_answer"]`
  channel for `agent.answer` results.
* **Swift socket** — `sibb/benchmark/sibb_xcuitest_client.py`. The persistent
  XCUITest socket the UI baseline already uses; we send Apple-SDK commands
  through it.

## How to run

```bash
# Smoke (3 tasks: 1 Reminders, 1 Calendar, 1 Contacts), v1 model only
/Library/Developer/CommandLineTools/usr/bin/python3 \
    -m sibb.api_baseline.sibb_api_runner \
    --udid $SIBB_UDID \
    --provider gemini --model gemini-2.5-flash \
    --smoke

# Full v1 run (26 tasks × gemini-2.5-flash × 1 seed)
/Library/Developer/CommandLineTools/usr/bin/python3 \
    -m sibb.api_baseline.sibb_api_runner \
    --udid $SIBB_UDID \
    --provider gemini --model gemini-2.5-flash \
    --seeds 0 \
    --results-dir sibb/api_baseline/results

# Cohen's κ second-rater pass — runs against pinned operational_definition.md
/Library/Developer/CommandLineTools/usr/bin/python3 \
    -m sibb.api_baseline.sibb_api_second_rater \
    --raters gemini-2.5-pro,claude-opus-4-7 \
    --rubric-commit HEAD

# Stitch results across multiple runs into a single headline artifact
/Library/Developer/CommandLineTools/usr/bin/python3 \
    -m sibb.api_baseline.stitch_results \
    --runs sibb/api_baseline/results/run_<ts1> \
           sibb/api_baseline/results/run_<ts2> \
    --out  sibb/api_baseline/results/headline_<label>.json \
    --description "Stitched headline (mid-run crash recovery)"
```

Module-form invocation requires `sibb/__init__.py` and
`sibb/api_baseline/__init__.py` (both shipped). Results land under
`results/run_<ts>/`:

* `trajectories.jsonl` — polymorphic `type`-tagged records, one per event
* `results.json` — per-task pass/fail + verifier breakdown
* `table4.csv` — aggregate per-subset numbers (paste into paper)
* `kappa.json` — second-rater agreement on the classification.yaml labels

## What this directory deliberately does NOT do

* No new Swift handlers — we work with the 11 existing ones.
* No edits to `sibb/benchmark/` for any purpose except the planned
  `sibb_llm.py` extension (operational + translator layer).
* No hybrid agent (that's v2 — Approach C in
  `memory/sibb_paper_hybrid_scaffold_landscape_2026-06-09.md`).
* No multi-model evaluation in v1 (gemini-2.5-flash only).
* No multi-seed runs in v1 (n=26 is the bottleneck; multi-seed control is v2).
* No failure-mode tagger over trajectories (deferred to v2).
* No prompt-ablation arm (v2; v1 reports raw pass rate with `<TOOLS>`-style
  prompt).
* No UI baseline run (assumed running in parallel under `sibb/benchmark/`).

## Pointers

* **Engineering plan**:
  `memory/sibb_paper_option_a_plus_plan_2026-05-31.md`
* **Locked decisions across UI / API / Hybrid scaffolds**:
  `memory/sibb_consolidated_findings_engineering_plan_2026-06-09.md`
* **Durable paper motivation** (lead-with-this framing):
  `memory/sibb_paper_motivation_synthesis_2026-05-31.md`
* **Scaffold protocol decisions** (native FC, tool search, etc.):
  `memory/sibb_paper_scaffold_design_synthesis_2026-06-09.md`
* **Why not LiteLLM / Pydantic AI**:
  `memory/sibb_paper_llm_abstraction_decision_2026-06-09.md`,
  `memory/sibb_paper_pydantic_ai_evaluation_2026-06-09.md`
* **Operational definition (4 cuts + worked examples)**:
  [`operational_definition.md`](operational_definition.md)
* **Per-task classification**:
  [`classification.yaml`](classification.yaml)
