# SIBB Hybrid Baseline — Engineering Plan

**Pattern**: Pattern 2 — Union-of-Toolsets (single LLM, one system prompt
exposing both UI text grammar AND API tools, per-step `action_type` tag).
Per-turn dispatcher; no external router.

**Goal**: Third scaffold for the head-to-head, parallel to
`sibb/api_baseline/` and `sibb/benchmark/sibb_ui_runner.py`. Same 26-task
slate, same seed, same model. Reports per-step `action_type` distribution
(the iOS analog of GUI-360°'s 81/19 split).

**Why Pattern 2 over Pattern 5 (Code-as-Action)** — see PLAN context
in the design notes. Short
version: production convergence (MobileWorld/MCPWorld/GUI-360°/ChatGPT
Agent), empirical data favors explicit per-step modality choice,
preserves the JSONL `action_type` measurement, lower complexity for
Cohen's κ failure-mode attribution.

---

## Design decisions locked

| Decision | Choice |
|---|---|
| Observation asymmetry | **PRESERVE**: UI action → AX tree; API call → call output only. The asymmetry is the data. Do NOT add `agent.observe()`. |
| Per-step modality choice | Model decides per turn. Text line → UI; structured tool call → API. |
| ANSWER terminal | Both `ANSWER {json}` (text) and `agent.answer(answer={...})` (FC) accepted. Normalized to the same payload. |
| FAIL terminal | Both `FAIL "reason"` (text) and new `agent.fail(reason=...)` (FC) accepted. |
| `agent.search_tools` index | **Scoped to API tools only.** UI verbs always in prompt; not in BM25 index. |
| Always-loaded tools | 10 UI verbs + `ANSWER` + `agent.answer` + `agent.fail` + `agent.search_tools` |
| Deferred (behind search) | 11 Apple-SDK tools + `system.now` + `system.locale` (matches API-baseline pure Design A) |
| Two valid actions same turn | First valid wins; second logged `ignored_multi_action` (mirrors UI scaffold's existing rule, task #262) |

---

## Phase 1 — System prompt + dispatcher (~1.5 days)

### 1.1 `sibb/hybrid_baseline/sibb_hybrid_assistant.py` (~250 LOC)

Fork from `sibb/api_baseline/sibb_api_assistant.py`. Key changes:

- **System prompt** (new `SYSTEM_PROMPT_TEMPLATE`, ~6KB):
  - `<TASK>` (verbatim instruction)
  - `<UI VERBS>` — the 10 text-grammar verbs with refs/coords from
    the UI scaffold's existing SYSTEM_PROMPT. Includes the
    LANDSCAPE / AUTO-ZOOMED header notes (task #226-#228).
  - `<API TOOLS>` — Anthropic Tool Search BM25 instructions; same
    paragraph as API baseline's pure Design A prompt about the
    initial catalog being only `agent.search_tools` + `agent.answer`
    + `agent.fail`.
  - `<RULES>` — per-turn rules, including: "emit AT MOST ONE
    action per turn (either UI verb or tool call, not both)";
    "you'll see the AX tree after UI actions but only the call
    result after API calls"; the v5 ANSWER ↔ agent.answer bridge.
  - `<ENVIRONMENT>` — iOS 26 sim notes.
  - Bump `SYSTEM_PROMPT_VERSION = "hybrid-v1"`.

- **`run_hybrid_episode`** (mirrors `run_api_episode`):
  - Reuse `resolve_refs` for SymbolicRef
  - Reuse `apply_initial_state` / `apply_pre_runner_setup`
  - `inject_reader` (an `AXReader`, like UI side) — needed because
    we observe the AX tree
  - Per-turn loop:
    1. Build chat input: instruction + current AX tree (post-UI-action)
       OR latest tool result (post-API-action)
    2. Call LLM with full tool catalog (deferred-style)
    3. Parse response: structured tool call? → dispatch as `action_type=api`
       Otherwise text line matching UI grammar? → execute via existing UI executor; `action_type=ui`
    4. Capture observation:
       - UI path: fresh AX tree (already what UI scaffold returns)
       - API path: tool result payload
    5. Log per-turn JSONL with `action_type`, `name`/`verb`, `payload`/`args`, `observation_kind`, etc.

### 1.2 Per-turn dispatcher (`_dispatch_action`)

```
def _dispatch_action(turn_response) -> (action_type, executor_result):
    # Priority: structured tool call wins if both present
    if turn_response.tool_calls:
        for tc in turn_response.tool_calls:
            if tc.name == "agent.fail":
                return ("api_terminal", _normalize_fail(tc.args))
            if tc.name == "agent.answer":
                return ("api_terminal", _normalize_answer(tc.args))
            # API dispatch (search_tools or Apple-SDK tool)
            return ("api", api_dispatcher.dispatch(tc))
    # Text path: try UI grammar
    text = turn_response.text.strip()
    if text.startswith("ANSWER "):
        return ("ui_terminal", _normalize_answer_from_text(text))
    if text.startswith("FAIL "):
        return ("ui_terminal", _normalize_fail_from_text(text))
    parsed = ui_parser.parse(text)  # existing parser
    if parsed:
        return ("ui", ui_executor.execute(parsed))
    return ("none", {"reason": "no_action"})
```

### 1.3 `agent.fail` tool (new)

Add to `sibb_api_tools.py`:

```python
APITool(
    name="agent.fail",
    description="Submit FAIL with a brief reason when the task "
                "cannot be completed. Use sparingly — only when no "
                "tool covers what's needed.",
    input_schema={
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
        "additionalProperties": False,
    },
    command_type=None,
    is_terminal=True,
    defer_loading=False,  # always reachable
),
```

### 1.4 Normalizers (`_normalize_answer`, `_normalize_fail`)

Idempotent — both paths produce the same JSONL event-kind and the
same verifier-readable shape.

## Phase 2 — Runner (~1 day)

### 2.1 `sibb/hybrid_baseline/sibb_hybrid_runner.py` (~200 LOC)

Fork from `sibb/benchmark/sibb_ui_runner.py` (already has the
AXReader healthcheck + recycle we need). Substitute UA →
hybrid_assistant:

- Reuse `parse_classification_slate` / `select_tasks` / `TaskResult`
  / `aggregate_table4` / `write_table4_csv` from
  `sibb.api_baseline.sibb_api_runner`
- Reuse `_is_ax_reader_alive` / `_recycle_ax_reader` from
  `sibb.benchmark.sibb_ui_runner`
- `_build_episode_args` exposes `inject_reader` to hybrid_assistant
- Per-episode JSONL post-mortem extracts per-step `action_type`
  to compute the iOS GUI/API split

### 2.2 Headline output schema additions

Add fields to per-episode TaskResult / results.json:

- `n_steps_ui` (count of `action_type=ui` steps)
- `n_steps_api` (count of `action_type=api` steps)
- `action_split_ratio` (n_steps_api / (n_steps_ui + n_steps_api))

This is the iOS 81/19 measurement.

## Phase 3 — Tests (~1 day)

L1 tests in `sibb/tests/unit/hybrid_baseline/`:

- `test_system_prompt_version_is_hybrid_v1`
- `test_system_prompt_mentions_both_ui_verbs_and_api_tool_search`
- `test_system_prompt_normalizes_answer_text_to_agent_answer_payload`
- `test_dispatcher_routes_text_TAP_to_ui_executor`
- `test_dispatcher_routes_structured_call_to_api_dispatcher`
- `test_dispatcher_first_action_wins_on_multi_action_turn`
- `test_normalize_fail_text_produces_same_event_as_agent_fail_tool`
- `test_normalize_answer_text_produces_same_event_as_agent_answer_tool`
- `test_agent_fail_tool_is_terminal`
- `test_agent_fail_tool_is_always_loaded`
- `test_search_tools_index_excludes_ui_verbs`

Plus the determinism-fairness pins from `test_ui_api_fairness.py`
are automatically inherited — both runners go through the same
GENERATORS dict with same seed handling. Add one source-text pin:
- `test_hybrid_runner_uses_same_classification_slate_as_api_runner`
  (asserts the slate-parse function is imported, not re-implemented)

## Phase 4 — Sim validation (~0.5 day)

Sim-test on 3 representative tasks before the full slate:
- 1 api_only that API baseline PASSED + UI baseline FAILED:
  `set_contact_birthday` — does the hybrid agent pick API for this?
- 1 ui_only that UI baseline PASSED:
  `message_save_sender` — does the hybrid agent fall back to UI?
- 1 api_only with date arithmetic (where API failed):
  `create_event_with_title_time` — does the hybrid discover
  `system.now` OR fall back to UI to read today's date from Calendar?

## Phase 5 — Full 26-task run + comparison + commit (~1 day)


      (API / UI / Hybrid)

      action-split statistic

      "Hybrid pass" column + correlation analysis on whichever
      difficulty estimate (UI or API) the agent actually used per turn


---

## LOC + day totals

| Phase | LOC | Days |
|---|---|---|
| Phase 0 — pre-build | 0 | 0.5 |
| Phase 1 — assistant + dispatcher + agent.fail | ~280 | 1.5 |
| Phase 2 — runner + headline schema | ~200 | 1.0 |
| Phase 3 — L1 tests | ~250 | 1.0 |
| Phase 4 — sim validation | 0 | 0.5 |
| Phase 5 — full run + comparison + commit | 0 | 1.0 |
| **Total** | **~730 LOC** | **~5.5 days** |

(Engineering plan in `sibb_consolidated_findings_engineering_plan_2026-06-09.md`
estimated 360 LOC / 3.5 days but assumed Pattern 5 / code-act. Pattern 2
takes more LOC for the dispatcher + dual-terminal normalizer but
produces a cleaner action_split measurement.)

---

## What the hybrid scaffold reports that the others can't

1. **iOS GUI/API split** (analog of GUI-360° 81/19). Per-task and
   aggregate.
2. **Per-step modality choice given both available** — the agent's
   revealed preference. Useful for the Discussion section's
   "production deployment lens."
3. **Fallback patterns**: API call FAILS → does the agent retry, or
   switch to UI? OBSERVE called after an API call → did the agent
   want to confirm before continuing? Both are empirical hooks.
4. **Asymmetric observation effect** (if any): tasks where the
   agent went UI-heavy because it needed the AX tree to navigate
   intermediate state. Compare hybrid PASS rate on these vs.
   API-only PASS rate.

## What this commit does NOT do

- No code-act ablation (Pattern 5). That's a separate ~1 day add-on
  if you want it for the API-side Discussion ablation. The hybrid
  scaffold itself is Pattern 2.
- No multi-seed runs (same n=1 as API/UI baselines).
- No cutoff-fresh model ablation. Use same gemini-2.5-flash for
  comparability with the API/UI numbers.
- No tool retrieval over UI verbs. They stay always-loaded.
