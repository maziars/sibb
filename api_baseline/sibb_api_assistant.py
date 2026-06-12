"""Option A+ API agent — agent loop for the API-only baseline.

This is the API-side counterpart to `sibb/benchmark/sibb_assistant.py`.
The shape mirrors Anthropic's canonical `loop.py` sample (~250 lines,
MIT):

    while True:
        resp = await llm.chat(messages, tools=...)
        log_turn(resp)
        if not resp.tool_calls:
            break                              # model gave up text-only
        for tc in resp.tool_calls:             # v1: one per turn
            result = await dispatcher.dispatch(tc.name, tc.arguments)
            if result.terminal:                # agent.answer → end
                break_outer()
            messages = llm.append_*(messages, ...)

What's specifically NOT in this loop:
  - AX tree reading. The API agent never touches the AX tree; the
    `XCUITestReader` it owns is used only to forward Swift commands.
  - Per-turn observation building. The "observation" is whatever the
    last tool call returned; the model takes it from there.
  - SCROLL / TAP / PRESS verbs. Those live in `sibb/benchmark/`.

CLI usage:

    python -m sibb.api_baseline.sibb_api_assistant \\
        --udid <YOUR-SIM-UDID> \\
        --provider gemini --model gemini-2.5-flash \\
        --generator add_reminder_to_existing_list \\
        --seed 0

The runner (`sibb_api_runner.py`) calls `run_api_episode(args)`
directly for batch experiments — same entrypoint either way.

Python 3.9 — no PEP-604 union syntax. No edits to `sibb/benchmark/`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# sys.path so the existing sibb/benchmark and sibb/simulator modules can
# be imported without restructuring the repo. The UI assistant uses the
# same pattern (it lives inside sibb/benchmark/ so its imports are bare;
# we live one directory up).
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
for _sub in ("sibb/simulator", "sibb/benchmark"):
    _p = os.path.join(_REPO_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Existing UI scaffold modules — READ-ONLY imports. We do not edit them.
from sibb_xcuitest_client import XCUITestReader  # noqa: E402
from sibb_state import apply_initial_state, apply_pre_runner_setup  # noqa: E402
from sibb_refs import resolve_refs  # noqa: E402
from sibb_verify import BaselineSnapshot  # noqa: E402
from sibb_replay import GENERATORS  # noqa: E402
from sibb_episode import (  # noqa: E402
    _baseline_resources_for, simctl_boot, simctl_wait_booted,
)
from sibb_llm import (  # noqa: E402
    make_client, available_providers, BudgetExceededError, ToolCall,
    LLMResponse,
)

# Our API-side tools.
from sibb.api_baseline.sibb_api_tools import (  # noqa: E402
    APIToolDispatcher,
    TOOLS,
    TOOL_TO_BUNDLE,
    mcp_tools,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Hard cap from the consolidated memo: 8 turns. Lower than the UI agent's
# ~15 since each API call accomplishes more.
DEFAULT_MAX_TURNS = 8
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_LLM_MAX_RETRIES = 5

# Default v1 model — matches classification.yaml.
DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash"

# System-prompt version stamp written into the task JSONL record. Bumped
# whenever the prompt text changes so trajectories can be grouped by
# prompt version for reproducibility analysis (Critic 5).
SYSTEM_PROMPT_VERSION = "v5"  # v4 + ANSWER-text-vs-agent.answer bridge rule


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """\
<TASK>
You are an assistant for an iPhone user, fulfilling tasks by calling
public Apple-SDK functions ("tools") on the user's device. You have one
specific task to complete. You succeed by leaving the device in the
state the user asked for (or, for read tasks, by returning the right
answer through the agent.answer tool).

The task is given to you as an INSTRUCTION below; do exactly what it
asks. Do not add, refuse, or seek confirmation.
</TASK>

<TOOLS>
Your INITIAL catalog contains only two tools:
  - agent.search_tools(query, k) — discover Apple-SDK tools by keyword.
  - agent.answer(answer)         — submit your final answer (read tasks).

The actual iOS API tools — EventKit (reminders, calendars, events),
Contacts (CRUD), MapKit MKLocalSearch (place resolution), and others —
are NOT in your initial catalog. You MUST call agent.search_tools to
discover them. Once retrieved, those tools become callable on this
and subsequent turns.

Typical first move: call agent.search_tools with a query describing
what capability you need. For example:
  - Task asks to add a reminder  →  search_tools(query="add reminder to list")
  - Task asks for an address     →  search_tools(query="resolve address")
  - Task asks to edit a contact  →  search_tools(query="update contact")

The search returns matching tool definitions (name, description, full
input schema) in the tool_result payload. Read them, then call the
tool that fits.

If search_tools returns nothing that fits the task, do NOT improvise
UI gestures or invent missing tools. Emit agent.answer with an honest
description of what's blocking you. There is no UI fallback — what no
tool covers genuinely cannot be done from the agent side.

Reasonable tool chains across different frameworks are encouraged
(e.g. MKLocalSearch result → cn.update_contact). Most tasks complete
in 2–4 tool calls including the search step.
</TOOLS>

<RULES>
1. Each turn, emit AT MOST ONE tool call. Parallel calls are disabled.
2. NEVER fabricate identifiers. If a task requires an existing
   identifier (calendar ID, contact ID, reminder list name), look it up
   first via the appropriate list_* tool.
3. For dates and times, use the canonical ISO formats: "YYYY-MM-DD" for
   date-only, "YYYY-MM-DDTHH:MM:SS" for time-of-day (local timezone).
   For year-omitted birthdays use "--MM-DD".
4. When you've completed a mutate task, DO NOT call agent.answer —
   simply stop emitting tool calls. The verifier reads the resulting
   on-device state.
5. When you've completed a read/lookup task, emit agent.answer EXACTLY
   ONCE with your final answer payload, then stop. If the instruction
   contains a literal example like "Output your final answer as: ANSWER
   {{...}}", that JSON object is the SHAPE the verifier expects — pass
   it as the `answer` argument to agent.answer. Emit it as a STRUCTURED
   TOOL CALL with name="agent.answer" and arguments={{"answer": <your
   JSON object>}} — NOT as Python call syntax in text, NOT as the
   literal word ANSWER in text. There is no text channel; the verifier
   only reads agent.answer's structured payload.
6. The turn budget is {max_turns}. Spend turns efficiently — a
   well-scoped task should fit comfortably inside the budget.
7. Before emitting a tool call, briefly state which tool you are
   picking and why — one sentence is enough. Keep it concise; the
   purpose is to make your selection auditable, not to elaborate.
8. Pass only the fields the task explicitly requested. Do NOT add
   optional fields the user did not ask for (e.g. don't set a due
   date on a reminder unless one is specified) — verifier checks
   guard against irrelevant edits.
</RULES>

<ENVIRONMENT>
The device is an iOS 26 simulator with the standard set of system apps
pre-installed and TCC permissions pre-granted for Calendar, Reminders,
Contacts, and Location. The user has NOT opted into any third-party
account state (no iCloud sync, no iMessage account).

Time on the device matches your wall-clock at task start.

You are running headless — there is no human in the loop after the
INSTRUCTION below. Do not wait for a confirmation. Just call the tool.
</ENVIRONMENT>

INSTRUCTION:
{instruction}
"""


def build_system_prompt(instruction: str, *,
                          n_tools: int,
                          max_turns: int) -> str:
    """Render the four-section system prompt with the per-episode bits
    substituted in."""
    return SYSTEM_PROMPT_TEMPLATE.format(
        n_tools=n_tools,
        max_turns=max_turns,
        instruction=instruction,
    )


# ---------------------------------------------------------------------------
# JSONL trajectory log
# ---------------------------------------------------------------------------


class JsonLog:
    """Append-only JSONL writer. One record per call; lines are flushed."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._f = open(path, "w", buffering=1)

    def append(self, record: Dict[str, Any]) -> None:
        self._f.write(json.dumps(record, default=str) + "\n")

    def close(self) -> None:
        self._f.close()


# ---------------------------------------------------------------------------
# verify helper (mirrors sibb_assistant.verify_via shape)
# ---------------------------------------------------------------------------


async def _verify(verifier_fn, task, reader, *,
                   context=None, baseline=None
                   ) -> Tuple[bool, List[Tuple[str, Optional[bool]]]]:
    return await verifier_fn(task, reader, context=context,
                                baseline=baseline)


# ---------------------------------------------------------------------------
# Outcome dataclass
# ---------------------------------------------------------------------------


@dataclass
class EpisodeOutcome:
    task_id: str
    generator: str
    passed: bool
    verifier_checks: List[Tuple[str, Optional[bool]]] = field(
        default_factory=list)
    turns_used: int = 0
    tool_calls_made: int = 0
    answer_payload: Optional[Dict[str, Any]] = None
    truncated: bool = False
    truncation_reason: Optional[str] = None
    cost_usd: float = 0.0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# The inner agent loop — extracted so unit tests can exercise it with
# mocked LLM clients and dispatchers, without sim setup.
# ---------------------------------------------------------------------------


@dataclass
class _LoopOutcome:
    agent_answer: Optional[Dict[str, Any]] = None
    truncation_reason: Optional[str] = None
    turns_used: int = 0
    tool_calls_made: int = 0
    llm_error: Optional[str] = None
    # Set of bundle ids whose system store the agent's successful tool
    # calls touched. Threaded into the verifier as
    # `context["observed_bundles"]` so read-task answer checks satisfy
    # `_check_agent_answer`'s observation gate.
    observed_bundles: List[str] = field(default_factory=list)


async def run_agent_loop(
    *,
    llm,
    dispatcher,
    system: str,
    initial_messages: List[Dict[str, Any]],
    all_tool_defs: List[Dict[str, Any]],
    max_turns: int,
    max_tokens: int,
    temperature: float,
    log: JsonLog,
    static_full_catalog: bool = False,
) -> _LoopOutcome:
    """The agent's observe→decide→act loop.

    Model-driven Tool Search (Design A): on each turn the loop sends
    the dispatcher's CURRENT catalog to `chat()`. The current catalog
    starts as the always-loaded set (non-deferred tools + agent.answer
    + agent.search_tools) and grows when the model calls
    `agent.search_tools` to discover deferred tools.

    `static_full_catalog=True` is the ablation path — every tool is
    sent every turn (no deferred discovery). Used to measure the
    contribution of Tool Search to pass rate.

    Caller owns the LLM client, the dispatcher, the JSONL log, and the
    sim. This function owns only the message-history threading and the
    per-turn catalog assembly. Returns a `_LoopOutcome`.
    """
    out = _LoopOutcome()
    messages = list(initial_messages)
    truncation_reason: Optional[str] = None
    turn_idx = -1  # for the empty-loop case (max_turns=0)

    # Quick-lookup map by name so we can assemble per-turn catalogs
    # without re-encoding the MCP definitions.
    tool_defs_by_name: Dict[str, Dict[str, Any]] = {
        td["name"]: td for td in all_tool_defs}

    for turn_idx in range(max_turns):
        # ---- Assemble the catalog the model sees this turn -----------
        if static_full_catalog:
            turn_catalog_names = list(tool_defs_by_name.keys())
        else:
            # Model-driven Tool Search: dispatcher tracks what the model
            # has discovered via agent.search_tools. The catalog grows
            # as the agent retrieves; never shrinks within an episode.
            turn_catalog_names = dispatcher.current_catalog()
        turn_tool_defs = [tool_defs_by_name[n]
                           for n in turn_catalog_names
                           if n in tool_defs_by_name]
        log.append({
            "type": "catalog",
            "step": turn_idx,
            "exposed": turn_catalog_names,
            "size": len(turn_tool_defs),
            "static": static_full_catalog,
        })

        t0 = time.monotonic()
        try:
            resp: LLMResponse = await llm.chat(
                messages,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=turn_tool_defs,
                tool_choice="auto",
                parallel_tool_calls=False,
            )
        except BudgetExceededError as e:
            truncation_reason = f"budget exceeded: {e}"
            log.append({"type": "truncated", "step": turn_idx,
                         "reason": truncation_reason})
            break
        except Exception as e:  # noqa: BLE001 — fatal LLM-side error
            log.append({"type": "llm_error", "step": turn_idx,
                         "error": f"{type(e).__name__}: {e}"})
            out.llm_error = f"llm: {type(e).__name__}: {e}"
            # Bucket-2 fix #6: turn_idx + 1 (we DID attempt this turn
            # — failure on attempt 1 means turns_used=1, not 0).
            out.turns_used = turn_idx + 1
            return out

        latency_ms = int((time.monotonic() - t0) * 1000)

        log.append({
            "type": "turn",
            "step": turn_idx,
            "text": resp.text,
            "tool_calls": [
                {"id": tc.id, "name": tc.name,
                  "arguments": tc.arguments}
                for tc in resp.tool_calls
            ],
            "thinking": resp.thinking,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "cached_input_tokens": resp.cached_input_tokens,
            "cost_usd": resp.cost_usd,
            "stop_reason": resp.stop_reason,
            "latency_ms": latency_ms,
        })

        # No tool calls — model emitted text and stopped. Valid for tasks
        # where the agent decided nothing more is required (or hit a
        # giving-up state).
        if not resp.tool_calls:
            log.append({"type": "no_tool_call_break", "step": turn_idx})
            break

        # v1: at most one tool call per turn (parallel_tool_calls=False).
        # If the provider emits N>1 anyway, we still dispatch only the
        # first — but we MUST thread the assistant message and emit
        # synthetic-skipped tool_results for the orphans, or the next
        # chat() will 400 ("tool_use blocks without tool_results").
        primary_tc = resp.tool_calls[0]
        orphan_tcs = resp.tool_calls[1:]
        if orphan_tcs:
            log.append({
                "type": "parallel_tool_call_ignored",
                "step": turn_idx,
                "ignored": [tc.name for tc in orphan_tcs],
            })

        # Thread the ENTIRE assistant turn back, including orphans, so
        # tool_use IDs and tool_results match up.
        messages = llm.append_assistant_with_tool_calls(
            messages, text=resp.text, tool_calls=list(resp.tool_calls))

        # Dispatch only the first tool call.
        result = await dispatcher.dispatch(
            primary_tc.name, primary_tc.arguments)
        out.tool_calls_made += 1
        log.append({
            "type": "tool_call",
            "step": turn_idx,
            "name": primary_tc.name,
            "id": primary_tc.id,
            "arguments": primary_tc.arguments,
            "ok": result.ok,
            "payload": result.payload,
            "terminal": result.terminal,
            "synthetic_update": result.synthetic_update,
            "latency_ms": result.latency_ms,
        })

        # Attribute the bundle the tool touched so the verifier's
        # observation gate (sibb_verify.py _check_agent_answer) is
        # satisfied for read-task answers. Only successful, non-terminal
        # calls count — failed dispatches didn't actually touch a store.
        if result.ok and not result.terminal:
            bundle = TOOL_TO_BUNDLE.get(primary_tc.name)
            if bundle and bundle not in out.observed_bundles:
                out.observed_bundles.append(bundle)

        if result.terminal:
            out.agent_answer = dispatcher.answer_payload
            break

        # Primary tool result.
        messages = llm.append_tool_result(
            messages,
            tool_call_id=primary_tc.id,
            tool_name=primary_tc.name,
            result=result.payload,
            is_error=not result.ok,
        )

        # Synthetic tool_results for orphaned parallel tool_use blocks.
        # The agent contract is one tool per turn; orphans get a clean
        # "skipped per v1 policy" payload that the model can ignore on
        # the next turn.
        for orphan in orphan_tcs:
            messages = llm.append_tool_result(
                messages,
                tool_call_id=orphan.id,
                tool_name=orphan.name,
                result={"skipped": "parallel_tool_calls disabled in v1"},
                is_error=False,
            )

    else:
        # for/else: loop exhausted without break.
        truncation_reason = (
            f"max_turns ({max_turns}) exhausted without terminal "
            "answer or text-only break")
        log.append({"type": "truncated", "step": max_turns,
                     "reason": truncation_reason})

    out.turns_used = turn_idx + 1
    out.truncation_reason = truncation_reason
    return out


# ---------------------------------------------------------------------------
# Generator-key resolution
# ---------------------------------------------------------------------------


def resolve_generator_key(name: str) -> str:
    """Normalize a generator name to the form used in GENERATORS.

    classification.yaml stores Python def names ("gen_add_reminder…").
    The runtime GENERATORS dict in sibb_replay.py registers under the
    short form ("add_reminder…"). Accept either; the dispatcher uses
    the short form for lookup."""
    if name.startswith("gen_"):
        return name[len("gen_"):]
    return name


# ---------------------------------------------------------------------------
# The episode loop
# ---------------------------------------------------------------------------


async def run_api_episode(args) -> Tuple[int, EpisodeOutcome]:
    """Execute one task end-to-end against a connected sim.

    Returns (exit_code, outcome). exit_code is 0 if the verifier passed,
    1 if it failed, 2 on an LLM-side error, 3 on a setup error.
    """
    # 1. Resolve generator + verifier.
    # classification.yaml lists Python def names ("gen_*"); the runtime
    # GENERATORS dict registers the bare form (no prefix). Accept either
    # so a copy-paste from classification.yaml works directly.
    gen_key = resolve_generator_key(args.generator)
    if gen_key not in GENERATORS:
        raise SystemExit(
            f"unknown generator {args.generator!r}; choose from "
            f"{sorted(GENERATORS.keys())[:10]}…")
    gen_fn, verifier_fn = GENERATORS[gen_key]

    random.seed(args.seed)
    task = gen_fn()
    task.task_id = f"api_baseline_{gen_key}_s{args.seed}"

    print(f"\n=== {task.task_id} ===")
    print(f"Provider: {args.provider} ({args.model})")
    print(f"Task:     {task.instruction[:120]}…"
           if len(task.instruction) > 120 else
           f"Task:     {task.instruction}")

    # 2. LLM client — built BEFORE the sim socket so config errors abort
    # cheap.
    try:
        llm = make_client(
            args.provider, model=args.model,
            timeout=args.llm_timeout,
            max_retries=args.llm_max_retries,
            budget_usd_max=args.budget_usd_max,
        )
    except Exception as e:
        return 3, EpisodeOutcome(
            task_id=task.task_id, generator=args.generator, passed=False,
            error=f"llm init: {type(e).__name__}: {e}")

    # 3+4. Pre-runner + sim boot + XCUITest server attach.
    #
    # Two modes:
    #
    # (A) STANDALONE (args.inject_reader is None) — default for the
    #     CLI entrypoint. Runs the full pre-flight: apply_pre_runner_setup
    #     (which shuts the sim down to edit Springboard plists), then
    #     simctl_boot, then a fresh XCUITest server attach. Owns the
    #     reader lifecycle; closes it in the finally.
    #
    # (B) INJECTED (args.inject_reader is set) — used by the batch
    #     runner (sibb_api_runner.py) which has already done the
    #     pre-flight ONCE at batch start. Skips pre_runner / boot /
    #     reader.start() entirely; reuses the injected reader.
    #     Does NOT close the reader in finally.
    #
    # The injected-reader pattern mirrors sibb_episode.py:183-212's
    # `inject_reader` skip-pre-runner branch. It is the canonical fix
    # for the iOS-test-runner-churn flakiness documented across
    # Maestro / Appium / WebDriverAgent — see Maestro #3254, #3318,
    # WDA #507, Apple Forums #118920. Reusing one XCUITest runner
    # across many episodes eliminates the boot churn that otherwise
    # crashes the sim every 3-20 episodes.
    inject_reader = getattr(args, "inject_reader", None)
    owned_reader = inject_reader is None

    # Resolve SymbolicRef instances BEFORE the dispatcher and verifier
    # see entries — handlers and verifiers consume pure strings only.
    # Mirrors sibb_episode.py:175-181 (UI baseline). Idempotent on
    # already-resolved structures.
    if getattr(task, "initial_state", None) and task.initial_state.spec:
        task.initial_state.spec = resolve_refs(task.initial_state.spec)
    if getattr(task, "verify_checks", None):
        task.verify_checks = resolve_refs(task.verify_checks)

    if owned_reader:
        pre_report = await asyncio.to_thread(
            apply_pre_runner_setup, args.udid, task)
        if pre_report.get("errors"):
            for e in pre_report["errors"]:
                print(f"  pre_runner error: {e}")
            return 3, EpisodeOutcome(
                task_id=task.task_id, generator=args.generator,
                passed=False,
                error=f"pre_runner: {'; '.join(pre_report['errors'])}")
        await simctl_boot(args.udid)
        await simctl_wait_booted(args.udid)
        # Settle window — SpringBoard finishes wiring AX queries ~2s
        # after Booted on warm sims.
        await asyncio.sleep(2.0)

        reader = XCUITestReader(
            udid=args.udid, bundle_id="com.apple.reminders")
        try:
            await reader.start()
        except Exception as e:  # noqa: BLE001 — surface a hard start
            return 3, EpisodeOutcome(
                task_id=task.task_id, generator=args.generator,
                passed=False,
                error=f"reader.start: {type(e).__name__}: {e}")
    else:
        # Injected: trust the batch runner. Note that any pre_runner
        # entries on this task's spec will NOT be applied (would
        # require sim shutdown, which would kill the shared reader).
        # api_only tasks rarely depend on pre_runner state; the runner
        # documents this compromise.
        reader = inject_reader

    # 5. JSONL log set up.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = args.log_dir or os.path.join(_REPO_ROOT, "sibb",
                                              "api_baseline", "results",
                                              f"run_{ts}")
    log_path = os.path.join(
        log_dir,
        f"episode_{args.generator}_{args.provider}_{args.model}_"
        f"s{args.seed}.jsonl")
    log = JsonLog(log_path)
    log.append({
        "type": "task",
        "task_id": task.task_id,
        "generator": gen_key,
        "seed": args.seed,
        "instruction": task.instruction,
        "apps": list(task.apps),
        "params": {k: str(v) for k, v in task.params.items()},
        "provider": args.provider,
        "model": args.model,
        "max_turns": args.max_turns,
        "scaffold": "api_baseline_option_a_plus",
        "system_prompt_version": SYSTEM_PROMPT_VERSION,
        "retrieval": ("model_driven_search" if args.retrieval
                       else "static_full_catalog"),
    })

    outcome = EpisodeOutcome(
        task_id=task.task_id, generator=args.generator, passed=False)

    try:
        # 5. Apply initial state via the same pre-runner the UI baseline
        # uses — identical starting state guarantees fair comparison.
        state_report = await apply_initial_state(reader, task)
        log.append({"type": "initial_state", "report": state_report})
        if state_report.get("errors"):
            print(f"  setup errors: {state_report['errors']}")

        # 6. Capture baseline for identity-kind checks.
        verify_checks = getattr(task, "verify_checks", None) or []
        baseline_resources = _baseline_resources_for(verify_checks)
        baseline: Optional[BaselineSnapshot] = None
        if baseline_resources:
            try:
                baseline = await BaselineSnapshot.capture(
                    reader, sorted(baseline_resources))
            except Exception as e:
                print(f"  baseline capture failed: "
                      f"{type(e).__name__}: {e}")

        # 7. Verify-before — sanity check the starting state.
        passed_before, checks_before = await _verify(
            verifier_fn, task, reader, baseline=baseline)
        log.append({
            "type": "verify_before",
            "passed": passed_before,
            "checks": [(c, bool(ok) if ok is not None else None)
                        for c, ok in checks_before],
        })
        if passed_before:
            print("  ⚠ verifier already passes — likely setup misconfig")

        # 8. Build tool catalog + dispatcher.
        dispatcher = APIToolDispatcher(reader)
        tool_defs = mcp_tools()

        # 9. System prompt + first user message.
        system = build_system_prompt(
            task.instruction,
            n_tools=len(TOOLS),
            max_turns=args.max_turns,
        )
        # The initial user message is intentionally minimal — the
        # instruction is anchored in the system prompt.
        messages: List[Dict[str, Any]] = [
            {"role": "user",
             "content": "Begin. Call exactly one tool per turn."},
        ]

        # 10. The agent loop — extracted for testability.
        # Snapshot cost BEFORE the loop so the per-episode delta is
        # correct even when the runner reuses one LLM client across
        # tasks (Critic 4: outcome.cost_usd was cumulative; needs delta).
        cost_before = llm.spent_usd
        loop_outcome = await run_agent_loop(
            llm=llm, dispatcher=dispatcher,
            system=system, initial_messages=messages,
            all_tool_defs=tool_defs,
            max_turns=args.max_turns,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            log=log,
            # `--no-retrieval` ablation: send the full catalog every
            # turn instead of relying on agent.search_tools.
            static_full_catalog=not args.retrieval,
        )

        if loop_outcome.llm_error is not None:
            outcome.error = loop_outcome.llm_error
            return 2, outcome

        agent_answer = loop_outcome.agent_answer
        truncation_reason = loop_outcome.truncation_reason
        outcome.turns_used = loop_outcome.turns_used
        outcome.tool_calls_made = loop_outcome.tool_calls_made
        outcome.cost_usd = llm.spent_usd - cost_before
        outcome.truncated = truncation_reason is not None
        outcome.truncation_reason = truncation_reason
        outcome.answer_payload = agent_answer

        # 11. Verify after — same pipeline as UI baseline.
        # `observed_bundles` satisfies the verifier's evidence gate for
        # read-task answer checks (sibb_verify._check_agent_answer
        # `observation_required`). The UI baseline gets this from AX
        # tree reads; the API agent attributes each successful tool
        # call's bundle via TOOL_TO_BUNDLE.
        context: Dict[str, Any] = {
            "observed_bundles": sorted(set(loop_outcome.observed_bundles)),
        }
        if agent_answer is not None:
            context["agent_answer"] = agent_answer.get("answer")
        passed_after, checks_after = await _verify(
            verifier_fn, task, reader,
            context=context, baseline=baseline)
        log.append({
            "type": "verify_after",
            "passed": passed_after,
            "checks": [(c, bool(ok) if ok is not None else None)
                        for c, ok in checks_after],
            "context": list(context.keys()),
        })
        outcome.passed = passed_after
        outcome.verifier_checks = checks_after

        log.append({
            "type": "summary",
            "passed": passed_after,
            "turns_used": outcome.turns_used,
            "tool_calls_made": outcome.tool_calls_made,
            "truncated": outcome.truncated,
            "truncation_reason": truncation_reason,
            "cost_usd": outcome.cost_usd,
        })

        print(f"  result:   {'PASS' if passed_after else 'FAIL'} "
                f"(turns={outcome.turns_used}, "
                f"tools={outcome.tool_calls_made}, "
                f"cost=${outcome.cost_usd:.4f})")

        return (0 if passed_after else 1), outcome

    finally:
        # Only close the reader if THIS episode opened it. When the
        # batch runner injects a shared reader, the runner owns the
        # lifecycle.
        if owned_reader:
            await _close_reader_safely(reader)
        log.close()


def _close_reader_and_log(reader, log) -> None:
    """Best-effort reader close + log close for the error path."""
    log.close()


async def _close_reader_safely(reader) -> None:
    try:
        await reader.stop()
    except Exception as e:
        print(f"  reader.stop() failed: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="API-only agent for one SIBB task. "
                     "See sibb/api_baseline/README.md.")
    p.add_argument("--udid", required=True,
                    help="iOS simulator UDID")
    p.add_argument("--generator", required=True,
                    help="Generator name (e.g. add_reminder_to_existing_list)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--provider", default=DEFAULT_PROVIDER,
                    choices=available_providers())
    p.add_argument("--model", default=None,
                    help="Defaults to the provider's default model.")
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--llm-timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--llm-max-retries", type=int,
                    default=DEFAULT_LLM_MAX_RETRIES)
    p.add_argument("--budget-usd-max", type=float, default=None,
                    help="Per-episode cost ceiling; BudgetExceededError "
                         "if exceeded mid-loop. NOTE: soft cap — the "
                         "call that crosses the cap completes; the NEXT "
                         "call raises.")
    p.add_argument("--retrieval", action="store_true",
                    default=True,
                    help="Use model-driven Tool Search (default ON): "
                         "deferred tools are hidden from the initial "
                         "catalog; the model must call agent.search_tools "
                         "to discover them. Matches Anthropic's "
                         "production Tool Search BM25 shape.")
    p.add_argument("--no-retrieval", dest="retrieval", action="store_false",
                    help="Static-catalog ablation: send the full tool "
                         "catalog on every turn. Used to measure the "
                         "contribution of Tool Search to pass rate.")
    p.add_argument("--log-dir", default=None,
                    help="Where to write the JSONL log. Defaults to "
                         "sibb/api_baseline/results/run_<ts>/")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.model is None:
        from sibb_llm import default_model
        args.model = default_model(args.provider)
    exit_code, _ = asyncio.run(run_api_episode(args))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
