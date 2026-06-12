"""L1 tests for sibb/api_baseline/sibb_api_assistant.py.

Tests the inner `run_agent_loop` against mocked LLM + dispatcher fakes
— no sim, no real provider SDK. We pin:

  - System prompt assembly (4 sections, instruction substituted)
  - Loop terminates on agent.answer (terminal=True from dispatcher)
  - Loop terminates on max_turns exhaustion (truncated)
  - Loop terminates on a turn with no tool_calls (text-only break)
  - Tool dispatch result threads back via append_tool_result
  - Failed dispatch (ok=False) sets is_error=True on the tool_result
  - BudgetExceededError mid-loop is logged as truncation, not error
  - llm_error path returns _LoopOutcome.llm_error set, doesn't continue
  - Parallel tool calls beyond #1 are logged as ignored
  - JSONL records emitted in the correct order/shape
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "sibb" / "benchmark"))

import sibb_llm as L  # noqa: E402
from sibb.api_baseline import sibb_api_assistant as A  # noqa: E402
from sibb.api_baseline import sibb_api_tools as T  # noqa: E402


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class FakeLLM:
    """A scripted LLM. Each chat() call pops the next response off a
    list. Records every call site's args for assertions."""

    def __init__(self, responses: List[L.LLMResponse]):
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.spent_usd = 0.0

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        if not self._responses:
            raise RuntimeError("FakeLLM exhausted — no scripted responses left")
        return self._responses.pop(0)

    def append_assistant_with_tool_calls(self, messages, *, text, tool_calls):
        """Trivial threading — just append a marker dict; the loop only
        cares about the LIST shape, not the wire format here."""
        return list(messages) + [{
            "role": "assistant", "text": text,
            "tool_calls": [(tc.name, tc.arguments) for tc in tool_calls],
        }]

    def append_tool_result(self, messages, *, tool_call_id, tool_name,
                            result, is_error=False):
        return list(messages) + [{
            "role": "tool",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "result": result,
            "is_error": is_error,
        }]


class FakeDispatcher:
    """A scripted dispatcher. Each dispatch() pops the next result off
    a list; records calls. Mirrors APIToolDispatcher's public surface:
    `dispatch()`, `answer_payload`, `current_catalog()`,
    `discovered_tools`."""

    def __init__(self, results: List[T.ToolCallResult],
                  initial_catalog: Optional[List[str]] = None):
        self._results = list(results)
        self.calls: List[Dict[str, Any]] = []
        self.answer_payload: Optional[Dict[str, Any]] = None
        self.discovered_tools: List[str] = []
        # The catalog the FakeDispatcher pretends is currently exposed.
        # Defaults to the empty list — assistant tests that don't care
        # about catalog assembly pass `all_tool_defs=[]` which makes
        # this irrelevant.
        self._initial_catalog = initial_catalog or []

    def current_catalog(self) -> List[str]:
        out = list(self._initial_catalog)
        for n in self.discovered_tools:
            if n not in out:
                out.append(n)
        return out

    async def dispatch(self, name, args):
        self.calls.append({"name": name, "args": args})
        if not self._results:
            raise RuntimeError(
                "FakeDispatcher exhausted — no scripted results left")
        result = self._results.pop(0)
        if result.terminal:
            # Mirror the real dispatcher's behavior of capturing the answer.
            self.answer_payload = {"answer": args.get("answer")}
        return result


@dataclass
class _CapturedLog:
    records: List[Dict[str, Any]] = field(default_factory=list)


class FakeLog:
    """Captures append() calls into a list instead of writing to disk."""

    def __init__(self):
        self.captured = _CapturedLog()

    def append(self, record):
        self.captured.records.append(record)

    def close(self):
        pass


# Helpers -------------------------------------------------------------------

def _llm_resp_with_tool_call(name: str, args: Dict[str, Any],
                              *, tc_id: str = "tc1",
                              text: str = "",
                              cost: float = 0.0001
                              ) -> L.LLMResponse:
    return L.LLMResponse(
        text=text, provider="fake", model="fake",
        input_tokens=100, output_tokens=10,
        tool_calls=[L.ToolCall(id=tc_id, name=name, arguments=args)],
        cost_usd=cost,
    )


def _llm_resp_text_only(text: str = "done", cost: float = 0.0001
                         ) -> L.LLMResponse:
    return L.LLMResponse(
        text=text, provider="fake", model="fake",
        input_tokens=80, output_tokens=5, tool_calls=[], cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def test_system_prompt_has_four_xml_sections_and_substitutes_instruction():
    p = A.build_system_prompt("Find Erin Wu's phone.",
                                 n_tools=11, max_turns=8)
    assert "<TASK>" in p and "</TASK>" in p
    assert "<TOOLS>" in p and "</TOOLS>" in p
    assert "<RULES>" in p and "</RULES>" in p
    assert "<ENVIRONMENT>" in p and "</ENVIRONMENT>" in p
    # Substitution happened.
    assert "budget is 8" in p
    assert "Find Erin Wu's phone." in p
    # Design A: prompt teaches the model about agent.search_tools.
    assert "agent.search_tools" in p


def test_system_prompt_orders_sections_task_first():
    p = A.build_system_prompt("x", n_tools=11, max_turns=8)
    i_task = p.index("<TASK>")
    i_tools = p.index("<TOOLS>")
    i_rules = p.index("<RULES>")
    i_env = p.index("<ENVIRONMENT>")
    assert i_task < i_tools < i_rules < i_env


def test_system_prompt_states_no_ui_fallback_anti_improvise_rule():
    """Adversarial reviewer attack #6: prompt biases agent toward API
    thinking. We OWN that bias — pin the explicit no-UI-improvise rule
    so future edits don't soften it. The phrase may wrap across
    newlines in the rendered prompt."""
    p = A.build_system_prompt("x", n_tools=11, max_turns=8)
    normalized = " ".join(p.split())
    assert "improvise UI gestures" in normalized, (
        "the no-UI-improvise rule should still be present")
    assert "no UI fallback" in normalized


# ---------------------------------------------------------------------------
# Loop termination on agent.answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_terminates_on_agent_answer():
    answer_args = {"answer": {"count": 3, "titles": ["a", "b", "c"]}}
    llm = FakeLLM([
        _llm_resp_with_tool_call("agent.answer", answer_args,
                                  tc_id="t1", text="Found them."),
    ])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={"received": True},
                          terminal=True),
    ])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "begin"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    assert out.agent_answer == {"answer": answer_args["answer"]}
    assert out.truncation_reason is None
    assert out.turns_used == 1
    assert out.tool_calls_made == 1
    # The dispatcher was called exactly once with agent.answer args.
    assert disp.calls == [{"name": "agent.answer", "args": answer_args}]
    # The LLM was NOT called again after the terminal dispatch.
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_loop_emits_summary_records_in_correct_order():
    answer_args = {"answer": 5}
    llm = FakeLLM([
        _llm_resp_with_tool_call("agent.answer", answer_args, tc_id="t1"),
    ])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={"received": True},
                          terminal=True),
    ])
    log = FakeLog()
    await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "begin"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)
    types = [r["type"] for r in log.captured.records]
    # catalog (per-turn exposed list) → turn → tool_call.
    assert types == ["catalog", "turn", "tool_call"]
    # tool_call payload has the dispatch flags.
    tc_rec = log.captured.records[2]
    assert tc_rec["terminal"] is True
    assert tc_rec["ok"] is True
    assert tc_rec["name"] == "agent.answer"


# ---------------------------------------------------------------------------
# Multi-turn loop with intermediate dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_threads_intermediate_tool_results_then_terminates():
    # Turn 1: list_reminders. Turn 2: agent.answer.
    llm = FakeLLM([
        _llm_resp_with_tool_call("eventkit.list_reminders",
                                  {"list": "Bills"}, tc_id="t1",
                                  text="Let me check Bills."),
        _llm_resp_with_tool_call("agent.answer",
                                  {"answer": 2}, tc_id="t2",
                                  text="Two items due today."),
    ])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={
            "ok": True,
            "reminders": [{"title": "Pay rent"}, {"title": "Pay phone"}],
        }),
        T.ToolCallResult(ok=True, payload={"received": True},
                          terminal=True),
    ])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "begin"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    assert out.turns_used == 2
    assert out.tool_calls_made == 2
    assert out.agent_answer == {"answer": 2}

    # The second chat() saw the threaded conversation: the user msg, the
    # assistant tool_use turn, the tool_result, then the new user prompt
    # implicit in the next chat call.
    second_call_msgs = llm.calls[1]["messages"]
    # Must include the assistant turn (our FakeLLM marker) and the tool
    # result marker.
    roles = [m.get("role") for m in second_call_msgs]
    assert "assistant" in roles
    assert "tool" in roles


# ---------------------------------------------------------------------------
# max_turns truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_truncates_on_max_turns_without_terminal():
    # Always emit a non-terminal call; never reach agent.answer.
    llm = FakeLLM([
        _llm_resp_with_tool_call("eventkit.list_reminders", {},
                                  tc_id=f"t{i}")
        for i in range(8)
    ])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={"reminders": []})
        for _ in range(8)
    ])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=3, max_tokens=512, temperature=0.0, log=log)

    assert out.agent_answer is None
    assert out.truncation_reason is not None
    assert "max_turns (3)" in out.truncation_reason
    assert out.turns_used == 3
    assert out.tool_calls_made == 3
    # Truncation record present.
    assert any(r.get("type") == "truncated"
                for r in log.captured.records)


# ---------------------------------------------------------------------------
# No-tool-call exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_breaks_on_text_only_response():
    llm = FakeLLM([_llm_resp_text_only("Nothing to do.")])
    disp = FakeDispatcher([])  # never called
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    assert out.tool_calls_made == 0
    assert out.agent_answer is None
    assert out.truncation_reason is None
    assert out.turns_used == 1
    assert any(r.get("type") == "no_tool_call_break"
                for r in log.captured.records)


# ---------------------------------------------------------------------------
# Failed dispatch flagged on tool_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_flags_is_error_on_failed_dispatch():
    # Turn 1: emit a tool call. Dispatcher returns ok=False.
    # Turn 2: agent.answer to terminate cleanly.
    llm = FakeLLM([
        _llm_resp_with_tool_call("eventkit.create_event",
                                  {"title": ""}, tc_id="t1"),
        _llm_resp_with_tool_call("agent.answer",
                                  {"answer": "could not"},
                                  tc_id="t2"),
    ])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=False, payload={"error": "title required"}),
        T.ToolCallResult(ok=True, payload={"received": True},
                          terminal=True),
    ])
    log = FakeLog()
    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    assert out.tool_calls_made == 2
    # The tool_result threaded back to the LLM on turn 2 must have
    # is_error=True from the FakeLLM marker.
    second_msgs = llm.calls[1]["messages"]
    tool_msg = next(m for m in second_msgs if m.get("role") == "tool")
    assert tool_msg["is_error"] is True
    assert tool_msg["result"] == {"error": "title required"}


# ---------------------------------------------------------------------------
# Budget exceeded mid-loop
# ---------------------------------------------------------------------------


class _BudgetBlowingLLM(FakeLLM):
    """Raises BudgetExceededError on the second call."""

    def __init__(self, responses):
        super().__init__(responses)

    async def chat(self, messages, **kwargs):
        if len(self.calls) >= 1:
            raise L.BudgetExceededError("budget exceeded")
        return await super().chat(messages, **kwargs)


@pytest.mark.asyncio
async def test_loop_truncates_on_budget_exceeded():
    llm = _BudgetBlowingLLM([
        _llm_resp_with_tool_call("eventkit.list_reminders", {},
                                  tc_id="t1"),
    ])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={"reminders": []}),
    ])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    assert "budget exceeded" in (out.truncation_reason or "")
    assert out.llm_error is None  # not a hard error — truncation path
    assert any(r.get("type") == "truncated"
                and "budget" in r.get("reason", "")
                for r in log.captured.records)


# ---------------------------------------------------------------------------
# Hard LLM error
# ---------------------------------------------------------------------------


class _BlowingLLM(FakeLLM):
    async def chat(self, messages, **kwargs):
        raise RuntimeError("provider 500")


@pytest.mark.asyncio
async def test_loop_returns_llm_error_outcome_without_continuing():
    llm = _BlowingLLM([])
    disp = FakeDispatcher([])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    assert out.llm_error is not None
    assert "RuntimeError" in out.llm_error
    assert "provider 500" in out.llm_error
    assert out.tool_calls_made == 0
    assert disp.calls == []  # no dispatch attempted


# ---------------------------------------------------------------------------
# Parallel tool calls beyond #1 ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_dispatches_only_first_tool_call_and_logs_ignored():
    """parallel_tool_calls=False at the chat() layer should prevent
    multi-call responses. But provider quirks happen — if the response
    arrives with N>1, we dispatch the first and emit a record naming
    the ignored ones."""
    extra_tc = L.ToolCall(id="t2",
                            name="eventkit.create_event",
                            arguments={"title": "y",
                                        "start_iso": "2026-01-01T10:00:00",
                                        "end_iso": "2026-01-01T11:00:00"})
    multi_resp = L.LLMResponse(
        text="", provider="fake", model="fake",
        input_tokens=20, output_tokens=5,
        tool_calls=[
            L.ToolCall(id="t1", name="eventkit.create_reminder",
                        arguments={"title": "x", "list": "y"}),
            extra_tc,
        ],
        cost_usd=0.0001,
    )
    answer_resp = _llm_resp_with_tool_call(
        "agent.answer", {"answer": "ok"}, tc_id="t3")
    llm = FakeLLM([multi_resp, answer_resp])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={"ok": True}),
        T.ToolCallResult(ok=True, payload={"received": True},
                          terminal=True),
    ])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    # Dispatcher saw exactly the first tool — not the second.
    assert [c["name"] for c in disp.calls] == [
        "eventkit.create_reminder", "agent.answer"]
    # An "ignored" record exists in the log naming the dropped tool.
    ignored = [r for r in log.captured.records
                if r.get("type") == "parallel_tool_call_ignored"]
    assert len(ignored) == 1
    assert ignored[0]["ignored"] == ["eventkit.create_event"]


# ---------------------------------------------------------------------------
# JSONL writer (the real one, in /tmp)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bucket-1 fixes (10-critic panel)
# ---------------------------------------------------------------------------


def test_resolve_generator_key_strips_gen_prefix_and_accepts_bare_form():
    """classification.yaml stores `gen_*` def names; GENERATORS dict
    registers bare names. Both forms must resolve to the same key."""
    assert A.resolve_generator_key("gen_add_reminder_to_existing_list") == (
        "add_reminder_to_existing_list")
    assert A.resolve_generator_key("add_reminder_to_existing_list") == (
        "add_reminder_to_existing_list")
    # Bare names without gen_ prefix pass through untouched, including
    # ones that happen to start with 'g' or 'gen'.
    assert A.resolve_generator_key("geocode_thing") == "geocode_thing"
    assert A.resolve_generator_key("general_other") == "general_other"


def test_system_prompt_does_not_promise_synthetic_update_anymore():
    """Bucket-1 fix #5: the prompt previously claimed the dispatcher
    "preserves untouched fields automatically and flag the operation as
    a synthetic update" — but the dispatcher's synthetic_update_reminder
    raises NotImplementedError. The stale language MUST be gone."""
    p = A.build_system_prompt("any task", n_tools=11, max_turns=8)
    # The dispatcher does NOT preserve fields — that language was a lie.
    assert "preserve untouched fields" not in p
    assert "synthetic update" not in p
    # The "api_only and feasible" classification-leak language is gone.
    assert "api_only" not in p


def test_system_prompt_encourages_legitimate_tool_chaining():
    """Bucket-1 fix #5: replaced the prompt's discouraging 'do NOT
    improvise' language with the more accurate 'do not improvise UI
    gestures' — legitimate cross-framework tool chains are the whole
    point of cross-app tasks like gen_maps_search_to_contact."""
    p = A.build_system_prompt("any task", n_tools=11, max_turns=8)
    assert "Reasonable tool chains" in p
    # Anti-improvise scoped to UI; the phrase may wrap across newlines.
    normalized = " ".join(p.split())
    assert "do NOT improvise UI gestures" in normalized


@pytest.mark.asyncio
async def test_loop_attributes_bundle_per_successful_tool_call():
    """Bucket-1 fix #9: the verifier's `_check_agent_answer` requires
    `context["observed_bundles"]` to contain the resource's bundle.
    The API agent must populate this from successful tool calls via
    TOOL_TO_BUNDLE — otherwise every read-task answer fails as
    `failure_kind=no_evidence`."""
    llm = FakeLLM([
        _llm_resp_with_tool_call("eventkit.list_reminders",
                                  {"list": "Bills"}, tc_id="t1"),
        _llm_resp_with_tool_call("cn.list_contacts",
                                  {"name_filter": "Erin"}, tc_id="t2"),
        _llm_resp_with_tool_call("agent.answer",
                                  {"answer": 5}, tc_id="t3"),
    ])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={"reminders": []}),
        T.ToolCallResult(ok=True, payload={"contacts": []}),
        T.ToolCallResult(ok=True, payload={"received": True}, terminal=True),
    ])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    # Both Reminders and Contacts bundles attributed; agent.answer is
    # python-only and NOT in TOOL_TO_BUNDLE, so doesn't appear.
    assert "com.apple.reminders" in out.observed_bundles
    assert "com.apple.MobileAddressBook" in out.observed_bundles
    # No duplicates from re-attributed bundles.
    assert len(out.observed_bundles) == len(set(out.observed_bundles))


@pytest.mark.asyncio
async def test_loop_does_not_attribute_bundle_on_failed_dispatch():
    """A tool call that returns ok=False didn't actually touch the
    system store. The observation gate must not be falsely satisfied
    by a failed call."""
    llm = FakeLLM([
        _llm_resp_with_tool_call("eventkit.list_reminders",
                                  {"list": "Bills"}, tc_id="t1"),
        _llm_resp_with_tool_call("agent.answer", {"answer": 0}, tc_id="t2"),
    ])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=False, payload={"error": "no permission"}),
        T.ToolCallResult(ok=True, payload={"received": True}, terminal=True),
    ])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    assert "com.apple.reminders" not in out.observed_bundles


@pytest.mark.asyncio
async def test_orphan_tool_calls_get_synthetic_tool_results():
    """Bucket-1 fix #6: when a provider emits N>1 tool calls despite
    parallel_tool_calls=False, the loop must thread tool_results for
    EVERY tool_use ID — orphans get a synthetic 'skipped per v1' result.
    Without this, Anthropic returns 400 on the NEXT chat() with
    'tool_use blocks without tool_results'."""
    extra_tc = L.ToolCall(id="t1_orphan",
                            name="eventkit.create_event",
                            arguments={"title": "x",
                                        "start_iso": "2026-01-01T10:00:00",
                                        "end_iso": "2026-01-01T11:00:00"})
    multi_resp = L.LLMResponse(
        text="", provider="fake", model="fake",
        input_tokens=20, output_tokens=5,
        tool_calls=[
            L.ToolCall(id="t1_primary",
                        name="eventkit.create_reminder",
                        arguments={"title": "x", "list": "y"}),
            extra_tc,
        ],
        cost_usd=0.0001,
    )
    answer_resp = _llm_resp_with_tool_call(
        "agent.answer", {"answer": "ok"}, tc_id="t2")
    llm = FakeLLM([multi_resp, answer_resp])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={"ok": True}),
        T.ToolCallResult(ok=True, payload={"received": True}, terminal=True),
    ])
    log = FakeLog()

    await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    # The second chat() saw threading for BOTH tool_use IDs.
    second_call_msgs = llm.calls[1]["messages"]
    tool_result_ids = [m.get("tool_call_id") for m in second_call_msgs
                        if m.get("role") == "tool"]
    assert "t1_primary" in tool_result_ids, (
        "primary tool_use_id missing from tool_results")
    assert "t1_orphan" in tool_result_ids, (
        "ORPHAN tool_use_id missing — Anthropic next-chat() would 400")


def test_tool_to_bundle_covers_every_apple_tool():
    """sibb_api_tools.TOOL_TO_BUNDLE must have an entry for every
    Swift-backed tool. Python-only tools (agent.answer +
    agent.search_tools + agent.fail) touch no system store and are
    exempt."""
    PYTHON_ONLY = {"agent.answer", "agent.search_tools", "agent.fail"}
    for tool in T.TOOLS:
        if tool.name in PYTHON_ONLY:
            assert tool.name not in T.TOOL_TO_BUNDLE, (
                f"{tool.name} is Python-only — should NOT have bundle "
                "attribution")
        else:
            assert tool.name in T.TOOL_TO_BUNDLE, (
                f"{tool.name} missing from TOOL_TO_BUNDLE — read-task "
                "answers will fail observation gate")
            assert T.TOOL_TO_BUNDLE[tool.name].startswith("com.apple."), (
                f"{tool.name} bundle isn't Apple — typo?")


# ---------------------------------------------------------------------------
# Bucket-2 fixes
# ---------------------------------------------------------------------------


def test_system_prompt_has_version_constant():
    """Bucket-2 fix (Critic 5 reproducibility): the prompt is stamped
    with a version so JSONL trajectories can be grouped by prompt."""
    assert A.SYSTEM_PROMPT_VERSION
    assert isinstance(A.SYSTEM_PROMPT_VERSION, str)


def test_system_prompt_version_is_v5():
    """Pin the exact prompt version so a content edit without a
    deliberate bump fails loudly. Bump this assertion when the prompt
    materially changes (so JSONL grouping by prompt_version stays
    meaningful for headline reproducibility)."""
    assert A.SYSTEM_PROMPT_VERSION == "v5"


def test_system_prompt_rule_5_bridges_answer_text_to_agent_answer_tool_call():
    """v5 fix: the in-instruction 'Output your final answer as: ANSWER
    {...}' convention is UI-baseline shape; the API baseline routes the
    same JSON through agent.answer's structured payload. Without this
    bridge rule, models emit ANSWER {...} as plain text and the
    verifier fails on missing agent.answer (next_event_lookup, run
    20260610_214315)."""
    p = A.build_system_prompt("any task", n_tools=11, max_turns=8)
    normalized = " ".join(p.split())
    # The bridge is present: ANSWER {...} payload → agent.answer arg.
    assert "Output your final answer as: ANSWER" in normalized
    assert "pass it as the `answer` argument to agent.answer" in normalized
    # And the explicit anti-pattern (no plain-text emission).
    assert "NOT as the literal word ANSWER in text" in normalized


def test_system_prompt_rule_5_forbids_python_call_syntax_as_text():
    """v5 fix follow-up (Agent-1 review): rule #5's example uses
    function-call notation. Without explicit guidance, some models
    have emitted literal Python-call text ('agent.answer(answer=…)')
    instead of a structured tool call. Pin the warning."""
    p = A.build_system_prompt("any task", n_tools=11, max_turns=8)
    normalized = " ".join(p.split())
    # The warning is present.
    assert "NOT as Python call syntax in text" in normalized
    # The structured-call shape is named explicitly.
    assert "STRUCTURED TOOL CALL" in normalized


def test_build_system_prompt_does_not_crash_on_literal_braces_in_rule_5():
    """v5 rule #5 contains literal {{...}} in the template that
    .format() escapes to {...}. A regression to single braces would
    raise IndexError ('Replacement index 0 out of range') — pin that
    the rendered prompt still contains the literal '{...}' example."""
    p = A.build_system_prompt("any task", n_tools=11, max_turns=8)
    normalized = " ".join(p.split())
    # The format-substitution survived and produced literal {...}
    # (line-wrap may put whitespace between ANSWER and {...}; normalize).
    assert "ANSWER {...}" in normalized
    # The instruction placeholder still substituted.
    assert "any task" in p


def test_system_prompt_asks_for_tool_choice_rationale():
    """Bucket-2 fix #13: ask the agent to briefly state its tool
    selection before emitting it. This surfaces the deliberation that
    Gemini's encrypted thought_signature otherwise hides."""
    p = A.build_system_prompt("any task", n_tools=11, max_turns=8)
    normalized = " ".join(p.split())
    assert ("briefly state which tool" in normalized
            or "briefly say which tool" in normalized)


def test_system_prompt_warns_against_unrequested_optional_fields():
    """Bucket-2 fix #13 cont.: the smoke saw Gemini hallucinate a
    due_iso the user did not request. Add an explicit rule against it."""
    p = A.build_system_prompt("any task", n_tools=11, max_turns=8)
    normalized = " ".join(p.split())
    assert "Do NOT add optional fields" in normalized
    assert "due date" in normalized  # the canonical example


@pytest.mark.asyncio
async def test_design_a_initial_catalog_is_minimal_reserve_only():
    """Pure Design A: on the first chat() call, the model sees ONLY
    the agent.* meta-tools (answer + search_tools + fail). Every
    Apple-SDK tool — including the ones it would obviously want for
    this task — is deferred."""
    answer_resp = _llm_resp_with_tool_call(
        "agent.answer", {"answer": "ok"}, tc_id="t1")
    llm = FakeLLM([answer_resp])

    # Use the REAL dispatcher with a fake reader so we exercise
    # current_catalog() correctly.
    class _NopReader:
        async def _send(self, cmd): return {"ok": True}
    disp = T.APIToolDispatcher(_NopReader())

    log = FakeLog()
    tool_defs = T.mcp_tools()
    await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user",
                              "content": "create a reminder"}],
        all_tool_defs=tool_defs, max_turns=8, max_tokens=512,
        temperature=0.0, log=log)

    chat_call = llm.calls[0]
    sent_names = {t["name"] for t in chat_call["tools"]}
    # The catalog is EXACTLY the three agent meta-tools.
    assert sent_names == {
        "agent.answer", "agent.search_tools", "agent.fail",
    }, (f"initial catalog drifted from pure Design A; got {sent_names}")
    # Sanity: every Apple-SDK tool is deferred (not exposed initially).
    assert "eventkit.create_reminder" not in sent_names
    assert "cn.create_contact" not in sent_names
    assert "mklocalsearch.query" not in sent_names


@pytest.mark.asyncio
async def test_design_a_search_tools_dispatch_marks_discovered():
    """When the model calls agent.search_tools, the dispatcher (a) runs
    BM25 over the deferred subset, (b) returns matching MCP defs in the
    payload, and (c) marks them as discovered so they appear in the
    catalog on the NEXT chat() call."""
    # Turn 0: model searches for "calendar". Dispatcher discovers
    # eventkit.create_calendar + eventkit.list_calendars.
    # Turn 1: model calls eventkit.create_calendar.
    # Turn 2: agent.answer to terminate cleanly.
    llm = FakeLLM([
        _llm_resp_with_tool_call(
            "agent.search_tools", {"query": "create a calendar", "k": 3},
            tc_id="t1"),
        _llm_resp_with_tool_call(
            "eventkit.create_calendar", {"name": "Work"}, tc_id="t2"),
        _llm_resp_with_tool_call(
            "agent.answer", {"answer": "ok"}, tc_id="t3"),
    ])

    # Real dispatcher with a fake reader that just acks Swift commands.
    class _NopReader:
        async def _send(self, cmd):
            return {"ok": True, "type": cmd.get("type")}
    disp = T.APIToolDispatcher(_NopReader())

    log = FakeLog()
    tool_defs = T.mcp_tools()
    await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user",
                              "content": "Create a calendar"}],
        all_tool_defs=tool_defs, max_turns=8, max_tokens=512,
        temperature=0.0, log=log)

    # The discovered set should now include calendar-related deferred
    # tools (BM25 over "create a calendar" against the 5 deferred ones).
    assert "eventkit.create_calendar" in disp.discovered_tools

    # On turn 0, the catalog log shows the initial set (no deferred).
    catalogs = [r for r in log.captured.records
                 if r.get("type") == "catalog"]
    assert len(catalogs) >= 2
    initial = set(catalogs[0]["exposed"])
    assert "eventkit.create_calendar" not in initial

    # On turn 1 (after the search), the catalog INCLUDES the discovered
    # tool — the model can now call it.
    second = set(catalogs[1]["exposed"])
    assert "eventkit.create_calendar" in second


@pytest.mark.asyncio
async def test_design_a_static_full_catalog_ablation():
    """With static_full_catalog=True, the loop ignores the dispatcher's
    discovery state and sends EVERY tool every turn. This is the
    --no-retrieval ablation path used to measure Tool Search's
    contribution to pass rate."""
    answer_resp = _llm_resp_with_tool_call(
        "agent.answer", {"answer": "ok"}, tc_id="t1")
    llm = FakeLLM([answer_resp])

    class _NopReader:
        async def _send(self, cmd): return {"ok": True}
    disp = T.APIToolDispatcher(_NopReader())

    log = FakeLog()
    tool_defs = T.mcp_tools()
    await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=tool_defs, max_turns=8, max_tokens=512,
        temperature=0.0, log=log,
        static_full_catalog=True)

    # FULL catalog exposed — all 13 tools (12 originally + agent.search_tools).
    chat_call = llm.calls[0]
    assert len(chat_call["tools"]) == len(tool_defs)
    # Deferred tools ARE in the catalog under static mode.
    sent_names = {t["name"] for t in chat_call["tools"]}
    assert "eventkit.create_calendar" in sent_names
    assert "mklocalsearch.query" in sent_names


@pytest.mark.asyncio
async def test_loop_logs_thinking_per_turn():
    """Bucket-2 fix #12: thinking blocks (Anthropic content blocks,
    Gemini thought_signature parts, OpenAI reasoning) flow through to
    the JSONL turn record so reasoning is auditable."""
    resp_with_thinking = L.LLMResponse(
        text="I'll add it.",
        provider="fake", model="fake",
        input_tokens=100, output_tokens=20,
        tool_calls=[L.ToolCall(
            id="t1", name="agent.answer", arguments={"answer": "done"})],
        thinking=[
            {"kind": "text", "text": "The user wants a reminder."},
            {"kind": "signature", "signature": "OPAQUE_BLOB"},
        ],
        cost_usd=0.0001,
    )
    llm = FakeLLM([resp_with_thinking])
    disp = FakeDispatcher([
        T.ToolCallResult(ok=True, payload={"received": True}, terminal=True),
    ])
    log = FakeLog()

    await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    turn_rec = next(r for r in log.captured.records
                     if r.get("type") == "turn")
    assert turn_rec["thinking"] == [
        {"kind": "text", "text": "The user wants a reminder."},
        {"kind": "signature", "signature": "OPAQUE_BLOB"},
    ]


@pytest.mark.asyncio
async def test_loop_llm_error_at_turn_0_records_1_turn_used():
    """Bucket-2 fix #6: on hard LLM error at turn 0, the loop attempted
    1 turn. Previously recorded turns_used=0 (off-by-one)."""
    class _BlowingLLM(FakeLLM):
        async def chat(self, messages, **kwargs):
            raise RuntimeError("provider 500")

    llm = _BlowingLLM([])
    disp = FakeDispatcher([])
    log = FakeLog()

    out = await A.run_agent_loop(
        llm=llm, dispatcher=disp, system="sys",
        initial_messages=[{"role": "user", "content": "x"}],
        all_tool_defs=[], max_turns=8, max_tokens=512, temperature=0.0, log=log)

    assert out.llm_error is not None
    # Fix: turns_used = turn_idx + 1, so 1 not 0.
    assert out.turns_used == 1, (
        f"expected turns_used=1 after first-turn fail; got {out.turns_used}")


def test_jsonlog_creates_parent_directory_and_writes_one_record_per_line():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "nested", "subdir", "log.jsonl")
        log = A.JsonLog(path)
        log.append({"type": "task", "task_id": "x"})
        log.append({"type": "turn", "step": 0, "text": "hi"})
        log.close()
        assert os.path.exists(path)
        with open(path) as fh:
            lines = fh.readlines()
        assert len(lines) == 2
        # Each line is valid JSON.
        assert json.loads(lines[0])["type"] == "task"
        assert json.loads(lines[1])["step"] == 0
