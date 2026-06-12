"""SIBB Hybrid baseline — Pattern 2 (Union-of-Toolsets).

Single LLM, one system prompt exposing BOTH the UI text grammar AND
the Apple-SDK tool catalog (deferred behind agent.search_tools). Per
turn the model picks its modality:

- Structured tool call         → API path: dispatcher executes,
                                  observation is the call's return value.
- Text matching UI grammar     → UI path: scaffold parses + executes,
                                  observation is the fresh AX tree.

JSONL stamps `action_type` per step so the per-task and aggregate
GUI/API split (the iOS analog of GUI-360°'s 81/19) falls out of the
trajectory. See `sibb/hybrid_baseline/PLAN.md` for the full design.

This file is the agent's per-turn loop. The batch runner that calls
into it lives in `sibb_hybrid_runner.py`. The terminal channels
(`ANSWER`/`agent.answer`, `FAIL`/`agent.fail`) are normalized at
dispatch time so the JSONL records one canonical event-kind per
terminal regardless of which surface the model used.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import random
import re
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_REPO_ROOT / "sibb" / "simulator"))

import sibb_llm as L  # noqa: E402

# UI scaffold: the existing primitives the hybrid reuses verbatim.
from sibb_scaffold import (  # noqa: E402
    AXReader, AXEnricher, AXTokenizer, SIBBScaffold, AXTree,
)
from sibb_assistant import (  # noqa: E402
    SYSTEM_PROMPT as UI_SYSTEM_PROMPT,
    execute as ui_execute,
)
from sibb_replay import GENERATORS  # noqa: E402
from sibb_episode import _baseline_resources_for  # noqa: E402
from sibb_state import (  # noqa: E402
    apply_initial_state, apply_pre_runner_setup,
)
from sibb_refs import resolve_refs  # noqa: E402
from sibb_verify import BaselineSnapshot  # noqa: E402

# API scaffold: tool catalog + dispatcher + same chat loop the API
# baseline uses for its catalog-assembly contract.
from sibb.api_baseline import sibb_api_tools as T  # noqa: E402
from sibb.api_baseline.sibb_api_assistant import (  # noqa: E402
    EpisodeOutcome, JsonLog as ApiJsonLog,
    DEFAULT_PROVIDER, DEFAULT_MODEL, DEFAULT_MAX_TURNS,
    DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT_S, DEFAULT_LLM_MAX_RETRIES,
    BudgetExceededError, make_client, resolve_generator_key,
)


SYSTEM_PROMPT_VERSION = "hybrid-v3"


# ---------------------------------------------------------------------------
# System prompt — UI prompt + API-tools section appended
# ---------------------------------------------------------------------------

# The hybrid agent sees the same UI grammar the UI baseline shows
# (line-by-line AX format, ANSWER/FAIL convention, gesture vocabulary),
# plus a single paragraph telling it that an Apple-SDK tool catalog is
# discoverable via agent.search_tools. Tools are NOT preloaded — the
# model must search to discover them, matching the API baseline's pure
# Design A contract.

_HYBRID_API_SECTION = """

== API TOOL CATALOG (in addition to the UI verbs above) ==

You ALSO have access to Apple-SDK tools that bypass the UI for some
tasks. The catalog is NOT preloaded — you discover it by emitting a
structured tool call to `agent.search_tools(query="...", k=5)`. The
result lists matching tools with their full input schemas; on
subsequent turns those tools become callable. Which tools exist for
which task domains is exactly what you discover via search — do not
assume.

The terminal channels are MIRRORED across the two surfaces:
  - `agent.answer(answer={...})` is the structured equivalent of the
    text `ANSWER {json}` you would emit in the UI flow. Either is
    accepted. The verifier sees the same payload.
  - `agent.fail(reason="...")` is the structured equivalent of the
    text `FAIL "reason"`. Either is accepted.

Per-turn modality choice:
  - If you emit a structured tool call, the next turn's observation is
    THAT TOOL CALL'S RETURN VALUE (focused, JSON-shaped).
  - If you emit a UI verb (text line), the next turn's observation is
    the FRESH AX TREE of the screen.

You will NOT receive both — the asymmetry is intentional. Use API
calls when you know what to query and want focused data. Use UI verbs
when you need to navigate or read on-screen state. Mix freely within
one episode — many tasks are easiest with an API setup followed by a
UI confirmation, or vice versa.

PREFER API WHEN AN APPLE SDK EXISTS — discover first, decide second.

The API path is dramatically CLEANER and SHORTER than UI when an
SDK covers the task. Generic shape of the contrast:

  TASK SHAPE A: a CREATE / MUTATE action on a stored entity

    UI path (6+ turns, 6+ AX-tree reads):
      PRESS home → TAP <app> → TAP <list/category> → TAP "+ New" →
      TYPE <fields> → TAP Done

    API path (1 turn, no AX-tree read):
      <namespace>.create_<entity>(<fields>)

  TASK SHAPE B: a LOOKUP / READ of a stored entity's attribute

    UI path (5+ turns):
      PRESS home → TAP <app> → search → TYPE <query> → TAP result
      → read screen
    API path (1 turn):
      <namespace>.list_<entity>(query=<value>)

API calls are ALSO more reliable than UI when available:
  - SDK schemas are type-safe — the verifier sees exactly what you
    pass; no risk of typing into the wrong field, mis-tapping a
    similarly-labeled element, or an AX tree that went stale during
    animations.
  - Production iOS assistants (Claude, Perplexity, ChatGPT, Apple
    Intelligence) all route through Apple SDKs when available — NOT
    synthetic taps. The API path IS the production-deployed path;
    the UI path is the fallback when no SDK exists.
  - One API call is one turn — six UI gestures are six turns of LLM
    inference, six AX-tree reads, six places things can go wrong.

So: before any task, call agent.search_tools FIRST with a query
describing what the task is asking for — e.g.
agent.search_tools(query="<verb the task asks for>"). The result
lists matching tools with full schemas; if one fits, call it
directly. The discovery turn is cheap; finding the right tool
typically saves 5-10 turns of UI gesture. Only fall back to UI if
search returns nothing applicable.

Concrete heuristic per task:
  1. Call agent.search_tools FIRST with a query describing the
     task's action (e.g. the verb + the entity from the
     instruction).
  2. Read the returned schemas. If a tool fits, call it directly
     and skip UI.
  3. If search returns nothing applicable, the task is UI-only —
     switch to UI verbs.
  4. Do NOT assume which apps have SDKs and which don't — let the
     search result decide. Skipping the discovery step and going
     straight to UI is the most common avoidable mistake.

Rules:
  - At most ONE action per turn (either one tool call or one UI verb).
    If you emit both, only the structured tool call is executed; the
    UI verb is logged as ignored.
  - For dates and times, system.now returns the device's current
    date/weekday/timezone — your training-cutoff date may be stale.
    Discover via agent.search_tools(query="current date").
  - When you've completed the task, emit one of the terminal channels
    above. Do NOT keep emitting actions after the task is done.
  - agent.fail is for when NO available action (UI or API) can complete
    the task — not for "the first thing I tried didn't work." A UI
    task that needs many gestures is still UI-doable; do not give up
    early because the SDK lookup returned nothing.

System prompt version: """ + SYSTEM_PROMPT_VERSION


def build_hybrid_system_prompt() -> str:
    """The hybrid system prompt: UI grammar + API section."""
    return UI_SYSTEM_PROMPT + _HYBRID_API_SECTION


# ---------------------------------------------------------------------------
# Per-turn dispatcher
# ---------------------------------------------------------------------------


@dataclass
class HybridStepResult:
    """One step's outcome — used by the runner to compute the
    action_type split per episode."""
    action_type: str             # "api" | "ui" | "ui_terminal" | "api_terminal" | "none"
    terminal: bool
    raw_verb: Optional[str]      # for UI path: original verb (TAP/TYPE/etc.)
    api_tool_name: Optional[str] # for API path: the tool that was called
    payload: Dict[str, Any]      # tool result OR ui execute() result
    answer_payload: Any          # populated on terminal answer (either source)
    fail_reason: Optional[str]   # populated on terminal fail (either source)


_TEXT_ANSWER_RE = re.compile(r"^\s*ANSWER\s+(.+?)\s*$", re.DOTALL | re.MULTILINE)
_TEXT_FAIL_RE = re.compile(r'^\s*FAIL\s+"?(.+?)"?\s*$', re.DOTALL | re.MULTILINE)


def thread_api_turn(llm,
                      messages: List[Dict[str, Any]],
                      *,
                      response: "L.LLMResponse",
                      payload: Dict[str, Any],
                      is_error: bool) -> List[Dict[str, Any]]:
    """Append the assistant's tool turn + the tool result to
    `messages` using the provider-aware helpers in sibb_llm.py.

    **Never construct content blocks of shape <TYPE>=tool_use or
    <TYPE>=tool_result inline.** That's Anthropic's wire format and
    Gemini's SDK rejects it with 45 pydantic ValidationErrors. Use
    this helper for every API-path turn that needs threading; the
    provider-specific translation lives inside
    `llm.append_assistant_with_tool_calls` and `llm.append_tool_result`.

    Returns the updated messages list. Idempotent only in the sense
    that calling it twice would double-thread; callers should call
    exactly once per API turn.
    """
    tc = response.tool_calls[0]
    messages = llm.append_assistant_with_tool_calls(
        messages, text=response.text, tool_calls=response.tool_calls)
    messages = llm.append_tool_result(
        messages,
        tool_call_id=tc.id, tool_name=tc.name,
        result=payload, is_error=is_error)
    return messages


def _normalize_text_answer(text: str) -> Optional[Any]:
    """Extract the JSON payload from `ANSWER {...}`. Returns the parsed
    object on success, the raw string if JSON parse fails (preserves the
    answer even when malformed), None if no ANSWER line."""
    m = _TEXT_ANSWER_RE.search(text)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _normalize_text_fail(text: str) -> Optional[str]:
    """Extract the reason from `FAIL "..."`. Returns the reason string
    on success, None if no FAIL line."""
    m = _TEXT_FAIL_RE.search(text)
    if not m:
        return None
    return m.group(1).strip()


async def dispatch_hybrid_step(
    *,
    llm_response: L.LLMResponse,
    api_dispatcher: T.APIToolDispatcher,
    ui_scaffold: SIBBScaffold,
    ui_reader: AXReader,
    ui_tree: AXTree,
) -> HybridStepResult:
    """Route the model's response to either the API dispatcher or the
    UI executor. Priority: structured tool call wins if both present
    (logged as ignored_multi_action when both surface).

    Terminal normalization: agent.answer ↔ text ANSWER, agent.fail ↔
    text FAIL. Both produce the same HybridStepResult.terminal=True
    with matching `answer_payload` / `fail_reason` fields.
    """
    # --- API path ------------------------------------------------------
    if llm_response.tool_calls:
        tc = llm_response.tool_calls[0]
        # Terminal check first — agent.answer / agent.fail short-circuit.
        if tc.name == "agent.answer":
            return HybridStepResult(
                action_type="api_terminal",
                terminal=True,
                raw_verb=None,
                api_tool_name=tc.name,
                payload={"ok": True, "via": "agent.answer"},
                answer_payload=tc.arguments.get("answer"),
                fail_reason=None,
            )
        if tc.name == "agent.fail":
            return HybridStepResult(
                action_type="api_terminal",
                terminal=True,
                raw_verb=None,
                api_tool_name=tc.name,
                payload={"ok": True, "via": "agent.fail"},
                answer_payload=None,
                fail_reason=str(tc.arguments.get("reason") or ""),
            )
        # Non-terminal API tool call.
        result = await api_dispatcher.dispatch(
            tc.name, tc.arguments or {})
        return HybridStepResult(
            action_type="api",
            terminal=bool(getattr(result, "terminal", False)),
            raw_verb=None,
            api_tool_name=tc.name,
            payload={"ok": result.ok, "payload": result.payload},
            answer_payload=None,
            fail_reason=None,
        )

    # --- UI path -------------------------------------------------------
    text = llm_response.text or ""
    # Terminal text channels first (ANSWER / FAIL) — these short-circuit
    # the scaffold parser, which already handles them but we want the
    # same normalized result shape as the API terminals.
    ans = _normalize_text_answer(text)
    if ans is not None:
        return HybridStepResult(
            action_type="ui_terminal",
            terminal=True,
            raw_verb="ANSWER",
            api_tool_name=None,
            payload={"ok": True, "via": "text_ANSWER"},
            answer_payload=ans,
            fail_reason=None,
        )
    fail_reason = _normalize_text_fail(text)
    if fail_reason is not None:
        return HybridStepResult(
            action_type="ui_terminal",
            terminal=True,
            raw_verb="FAIL",
            api_tool_name=None,
            payload={"ok": True, "via": "text_FAIL"},
            answer_payload=None,
            fail_reason=fail_reason,
        )

    # General UI verb path.
    action = ui_scaffold.parse_action(text)
    result = await ui_execute(ui_reader, action, ui_tree)
    return HybridStepResult(
        action_type="ui",
        terminal=bool(result.get("terminal")),
        raw_verb=action.raw_verb,
        api_tool_name=None,
        payload=result,
        answer_payload=result.get("answer_payload"),
        fail_reason=None,
    )


# ---------------------------------------------------------------------------
# Episode runner — single (generator, seed) → outcome
# ---------------------------------------------------------------------------


async def run_hybrid_episode(args) -> Tuple[int, EpisodeOutcome]:
    """Drive a single hybrid episode. Mirrors the API/UI assistants'
    contract: takes a SimpleNamespace-like args, returns
    (exit_code, EpisodeOutcome).

    Reuses the API path's resolve_refs / apply_initial_state /
    BaselineSnapshot / verifier and the UI path's AXReader / parse /
    execute. The per-turn dispatcher is `dispatch_hybrid_step`.
    """
    # --- Resolve generator + build task --------------------------------
    gen_key = resolve_generator_key(args.generator)
    if gen_key not in GENERATORS:
        raise SystemExit(
            f"unknown generator {args.generator!r}; "
            f"choose from {sorted(GENERATORS.keys())[:6]}…")
    gen_fn, verifier_fn = GENERATORS[gen_key]
    random.seed(args.seed)
    task = gen_fn()
    task.task_id = f"hybrid_{gen_key}_s{args.seed}"

    # Resolve SymbolicRef before the dispatcher sees spec entries
    # (mirrors sibb_api_assistant's contract).
    if getattr(task, "initial_state", None) and task.initial_state.spec:
        task.initial_state.spec = resolve_refs(task.initial_state.spec)
    if getattr(task, "verify_checks", None):
        task.verify_checks = resolve_refs(task.verify_checks)

    inject_reader = getattr(args, "inject_reader", None)
    owned_reader = inject_reader is None

    # --- Sim attach (owned-mode only) ----------------------------------
    if owned_reader:
        pre_report = await asyncio.to_thread(
            apply_pre_runner_setup, args.udid, task)
        if pre_report.get("errors"):
            return 3, EpisodeOutcome(
                task_id=task.task_id, generator=args.generator,
                passed=False,
                error=f"pre_runner: {'; '.join(pre_report['errors'])}")
        import subprocess as _sp
        _sp.run(["open", "-a", "Simulator",
                 "--args", "-CurrentDeviceUDID", args.udid],
                capture_output=True)
        await asyncio.sleep(2)
        reader = AXReader(args.udid)
        await reader.start(bundle_id=getattr(
            args, "bundle", "com.apple.springboard"))
    else:
        reader = inject_reader

    # --- Episode setup -------------------------------------------------
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = args.log_dir or os.path.join(_REPO_ROOT, "sibb",
                                              "api_baseline", "results",
                                              f"hybrid_run_{ts}")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir,
        f"episode_{gen_key}_{args.provider}_{args.model}_"
        f"s{args.seed}.jsonl")
    log = ApiJsonLog(log_path)
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
        "scaffold": "hybrid_baseline",
        "system_prompt_version": SYSTEM_PROMPT_VERSION,
    })

    llm = make_client(args.provider, model=args.model,
                       timeout=args.llm_timeout)
    api_dispatcher = T.APIToolDispatcher(reader._xcuitest)
    ui_scaffold = SIBBScaffold(args.udid)
    tokenizer = AXTokenizer()
    enricher = AXEnricher(vlm_client=None)

    outcome = EpisodeOutcome(
        task_id=task.task_id, generator=args.generator, passed=False)

    n_steps_ui = 0
    n_steps_api = 0
    # Bundles touched by successful API calls — fed to the verifier's
    # `observed_bundles` evidence gate for read-task answer checks.
    # Mirrors `_LoopOutcome.observed_bundles` in sibb_api_assistant.
    observed_bundles: List[str] = []
    # On the UI side, every action implies the foreground app is being
    # touched. We pre-seed observed_bundles with `task.apps` so
    # UI-driven reads also satisfy the gate. The API side narrows it
    # to bundles attributable to specific tools.
    for app_name in (task.apps or []):
        from sibb_state import canonicalize_app
        bid = canonicalize_app(app_name)
        if bid and bid not in observed_bundles:
            observed_bundles.append(bid)

    try:
        # --- Apply initial state via XCUITest socket -----------------
        state_report = await apply_initial_state(reader._xcuitest, task)
        log.append({"type": "initial_state", "report": state_report})
        if state_report.get("errors"):
            print(f"  setup errors: {state_report['errors']}")

        # --- Baseline snapshot for identity checks --------------------
        verify_checks = getattr(task, "verify_checks", None) or []
        baseline_resources = _baseline_resources_for(verify_checks)
        baseline: Optional[BaselineSnapshot] = None
        if baseline_resources:
            try:
                baseline = await BaselineSnapshot.capture(
                    reader._xcuitest, sorted(baseline_resources))
            except Exception as e:  # noqa: BLE001
                print(f"  baseline capture failed: "
                      f"{type(e).__name__}: {e}")

        # --- Verifier pre-check ---------------------------------------
        # (Identical-state shortcut: if the task is somehow already
        # satisfied, the verifier should not award credit on entry.
        # We still run to record `verify_before`.)
        # NOTE: hybrid skips the explicit `verify_before` snapshot for
        # parity with sibb_api_assistant — the existing verifier records
        # both pre and post automatically.

        # --- Per-turn loop --------------------------------------------
        system_prompt = build_hybrid_system_prompt()
        # Observation asymmetry (enforced — 2026-06-11):
        #   - Turn 0: include the AX tree as a courtesy so the agent
        #     has world state to start.
        #   - After a UI action: include the FRESH AX tree (the
        #     gesture changed the screen).
        #   - After an API action: do NOT include the AX tree — the
        #     tool_result is already threaded in the conversation, and
        #     the screen state hasn't changed in a way the agent needs
        #     to navigate. This matches the prompt's promise of
        #     asymmetric observation, halves the per-turn input
        #     tokens on API-heavy turns, and forces the agent to
        #     stay in the modality it just chose.
        instruction_msg = {
            "role": "user",
            "content": (
                f"INSTRUCTION:\n{task.instruction}\n\n"
                f"You will see the current AX tree on UI-action turns "
                f"(and turn 0). After API tool calls the next "
                f"observation is the tool's return value — the AX tree "
                f"is NOT re-rendered. Emit at most one action."),
        }
        messages: List[Dict[str, Any]] = [instruction_msg]
        answer_payload: Any = None
        fail_reason: Optional[str] = None
        running_cost_usd = 0.0
        turn_idx = -1
        # Tracks what the last action was so we can decide whether
        # to include the AX tree this turn. None on turn 0 means
        # "no prior action" → include the tree as a courtesy.
        last_action_type: Optional[str] = None

        for turn_idx in range(args.max_turns):
            # Asymmetric observation:
            # - ALWAYS fetch the tree (~1s, cheap; the dispatcher
            #   needs it for @ref resolution if the agent picks UI).
            # - Only INCLUDE it in the LLM prompt when the LAST
            #   action was UI or there was no prior action.
            #   That's where the token cost lives.
            include_tree_in_prompt = (
                last_action_type is None
                or last_action_type.startswith("ui"))

            tree = None
            try:
                tree = await reader.read()
                tree = await enricher.enrich(tree, screenshot=None)
            except Exception as e:  # noqa: BLE001
                log.append({"type": "ax_read_error",
                             "step": turn_idx,
                             "error": f"{type(e).__name__}: {e}"})
                tree = None

            if include_tree_in_prompt and tree is not None:
                tree_repr = tokenizer.tokenize(
                    tree, fmt="flat",
                    max_elements=getattr(args, "max_elements", 150))
                messages_for_chat = messages + [{
                    "role": "user",
                    "content": (
                        f"=== CURRENT AX TREE (step {turn_idx}) ===\n"
                        f"{tree_repr}\n"
                        f"=== END TREE ==="),
                }]
                log.append({"type": "tree_shown",
                             "step": turn_idx,
                             "n_tokens_est": len(tree_repr) // 4,
                             "reason": "after_ui_or_start"})
            else:
                messages_for_chat = messages
                log.append({"type": "tree_suppressed",
                             "step": turn_idx,
                             "reason": ("after_api_action"
                                        if last_action_type
                                        and last_action_type.startswith(
                                            "api")
                                        else "tree_unavailable")})

            # Assemble the catalog the model sees this turn (deferred
            # Tool Search, same as API baseline).
            turn_catalog_names = api_dispatcher.current_catalog()
            tool_defs = T.mcp_tools()
            tool_defs_by_name = {td["name"]: td for td in tool_defs}
            turn_tool_defs = [tool_defs_by_name[n]
                              for n in turn_catalog_names
                              if n in tool_defs_by_name]
            log.append({
                "type": "catalog",
                "step": turn_idx,
                "exposed": turn_catalog_names,
                "size": len(turn_tool_defs),
            })

            t0 = time.monotonic()
            try:
                resp: L.LLMResponse = await llm.chat(
                    messages_for_chat,
                    system=system_prompt,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    tools=turn_tool_defs,
                    tool_choice="auto",
                    parallel_tool_calls=False,
                )
            except BudgetExceededError as e:
                log.append({"type": "truncated", "step": turn_idx,
                             "reason": f"budget exceeded: {e}"})
                outcome.truncated = True
                outcome.truncation_reason = f"budget exceeded: {e}"
                break
            except Exception as e:  # noqa: BLE001
                log.append({"type": "llm_error", "step": turn_idx,
                             "error": f"{type(e).__name__}: {e}"})
                outcome.error = f"llm: {type(e).__name__}: {e}"
                break

            latency_ms = int((time.monotonic() - t0) * 1000)
            running_cost_usd += float(resp.cost_usd or 0.0)
            log.append({
                "type": "turn",
                "step": turn_idx,
                "text": resp.text,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name,
                      "arguments": tc.arguments}
                    for tc in resp.tool_calls
                ],
                "input_tokens": resp.input_tokens,
                "output_tokens": resp.output_tokens,
                "cost_usd": resp.cost_usd,
                "latency_ms": latency_ms,
            })

            # --- Dispatch the action --------------------------------
            step_result = await dispatch_hybrid_step(
                llm_response=resp,
                api_dispatcher=api_dispatcher,
                ui_scaffold=ui_scaffold,
                ui_reader=reader,
                ui_tree=tree,
            )

            log.append({
                "type": "action",
                "step": turn_idx,
                "action_type": step_result.action_type,
                "raw_verb": step_result.raw_verb,
                "api_tool_name": step_result.api_tool_name,
                "payload": step_result.payload,
                "answer_payload": step_result.answer_payload,
                "fail_reason": step_result.fail_reason,
                "terminal": step_result.terminal,
            })

            # Update last_action_type so the NEXT turn knows whether
            # to include the AX tree (asymmetric observation).
            last_action_type = step_result.action_type

            if step_result.action_type.startswith("ui"):
                n_steps_ui += 1
            elif step_result.action_type.startswith("api"):
                n_steps_api += 1
                # Track bundle attribution for the verifier's evidence
                # gate (mirrors sibb_api_assistant).
                if (step_result.api_tool_name
                        and step_result.payload.get("ok")):
                    bundle = T.TOOL_TO_BUNDLE.get(
                        step_result.api_tool_name)
                    if bundle and bundle not in observed_bundles:
                        observed_bundles.append(bundle)

            # Thread the assistant + tool_result for API-path turns so
            # the next chat() sees them. UI-path actions don't need
            # threading since the AX tree at the start of next turn
            # supplies the new observation. See `thread_api_turn`'s
            # docstring for the Anthropic-vs-Gemini wire-format trap
            # this helper exists to prevent.
            if step_result.action_type == "api" and resp.tool_calls:
                messages = thread_api_turn(
                    llm, messages,
                    response=resp,
                    payload=step_result.payload,
                    is_error=not step_result.payload.get("ok"))

            if step_result.terminal:
                answer_payload = step_result.answer_payload
                fail_reason = step_result.fail_reason
                break
        else:
            # Loop fell through without a terminal.
            outcome.truncated = True
            outcome.truncation_reason = (
                f"max_turns ({args.max_turns}) exhausted")
            log.append({"type": "exhausted",
                         "max_turns": args.max_turns})

        # --- Verifier post-check --------------------------------------
        verify_context = {
            "agent_answer": answer_payload,
            "agent_fail_reason": fail_reason,
            "observed_bundles": sorted(set(observed_bundles)),
        }
        # verifier_fn returns (passed: bool, checks: List[(label, ok)])
        # — the legacy tuple shape used by both sibb_assistant and
        # sibb_api_assistant.
        passed, checks_after = await verifier_fn(
            task, reader._xcuitest,
            context=verify_context, baseline=baseline)
        log.append({
            "type": "verify_after",
            "passed": passed,
            "checks": [(c[0], bool(c[1]) if c[1] is not None else None)
                       for c in (checks_after or [])],
            "terminal": (("api_answer" if answer_payload is not None
                           else "api_fail" if fail_reason
                           else "none")),
        })

        outcome.passed = passed
        outcome.turns_used = turn_idx + 1
        outcome.tool_calls_made = n_steps_ui + n_steps_api
        outcome.cost_usd = running_cost_usd
        # Hybrid-specific fields go on the outcome via __dict__ since
        # EpisodeOutcome is shared with the API baseline and we don't
        # want to fork its dataclass for an additive metric. The runner
        # reads these by name and rolls them into the per-episode
        # results.json.
        outcome.__dict__["n_steps_ui"] = n_steps_ui
        outcome.__dict__["n_steps_api"] = n_steps_api
        non_terminal = max(1, n_steps_ui + n_steps_api)
        outcome.__dict__["action_split_ratio"] = n_steps_api / non_terminal

    finally:
        if owned_reader:
            try:
                await reader.stop()
            except Exception:  # noqa: BLE001
                pass
        log.close()

    return (0 if outcome.passed else 1), outcome
