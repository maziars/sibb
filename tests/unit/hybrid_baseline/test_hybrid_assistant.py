"""L1 tests for sibb/hybrid_baseline/sibb_hybrid_assistant.py.

Covers the Pattern 2 dispatcher contract: per-turn routing to UI vs
API based on the LLM response, terminal normalization (ANSWER ↔
agent.answer, FAIL ↔ agent.fail), and the SYSTEM_PROMPT composition
(UI prompt + API tool-search section). No sim, no real provider SDK.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(REPO_ROOT / "sibb" / "simulator"))

import sibb_llm as L  # noqa: E402
from sibb.hybrid_baseline import sibb_hybrid_assistant as H  # noqa: E402
from sibb.api_baseline import sibb_api_tools as T  # noqa: E402


# ---------------------------------------------------------------------------
# Prompt assertions
# ---------------------------------------------------------------------------


def test_hybrid_prompt_version_is_hybrid_v3():
    """Pin the prompt version. v3 bumps to match the asymmetric-
    observation behavior the prompt always promised: AX tree only
    appears after UI actions (and turn 0); after API actions the
    next observation is the tool's return value only. The v1/v2
    prompts described this asymmetry but the implementation sent
    the tree on every turn anyway. v3 fixes the implementation."""
    assert H.SYSTEM_PROMPT_VERSION == "hybrid-v3"


def test_hybrid_prompt_encourages_api_discovery():
    """v2/v3 fix: the v1 baseline showed Gemini going 97% UI / 3%
    API on the 26-task slate. The prompt now actively encourages
    agent.search_tools as the first move for ANY task. v3 generalized
    the previous v2 prompt to NOT enumerate specific app domains —
    the agent must discover which tools exist via search rather than
    being told which app categories have SDKs."""
    p = H.build_hybrid_system_prompt()
    # The encouragement clause is present.
    assert "PREFER API" in p
    # The discovery-first heuristic is present.
    assert "search_tools FIRST" in p or "search_tools first" in p.lower()
    # The "do not assume" guard against pre-classifying domains.
    assert "do not assume" in p.lower() or "do NOT assume" in p
    # The anti-fail-too-early guard.
    assert "do not give up" in p.lower() or "not for" in p


def test_hybrid_prompt_does_not_leak_specific_sdk_namespaces():
    """v3 explicitly removed app-name and SDK-name enumeration so the
    agent must discover the catalog via agent.search_tools rather than
    being told the answer. Pin the absence — if a future edit adds the
    list back, this trips."""
    p = H.build_hybrid_system_prompt()
    # The API section (not the UI section) is what we're pinning.
    api_section = H._HYBRID_API_SECTION
    # SDK namespaces should NOT appear (they'd leak the answer):
    leaks = ["EventKit", "MKLocalSearch", "eventkit.create_reminder",
             "cn.list_contacts", "Reminders.app", "Calendar.app",
             "Contacts.app"]
    for leak in leaks:
        assert leak not in api_section, (
            f"API-section prompt leaks specific SDK/app name: {leak!r}. "
            f"v3's whole point is that the agent discovers tools via "
            f"agent.search_tools instead of reading the catalog off "
            f"the prompt. If this is intentional, remove the leak from "
            f"the test list AND document the intent in PLAN.md.")


def test_hybrid_prompt_contains_both_ui_grammar_and_api_section():
    """The hybrid prompt is composed: UI grammar followed by the
    API tool-search section. Pin both halves."""
    p = H.build_hybrid_system_prompt()
    # UI grammar markers (from sibb_assistant.SYSTEM_PROMPT):
    assert "OBSERVATION FORMAT" in p
    # API section markers (this file):
    assert "API TOOL CATALOG" in p
    assert "agent.search_tools" in p
    assert "agent.answer(answer=" in p
    assert "agent.fail(reason=" in p


def test_hybrid_prompt_documents_observation_asymmetry():
    """The asymmetry — UI verb → AX tree, API call → return value —
    is a core design feature, not a bug. The prompt must spell it out."""
    p = H.build_hybrid_system_prompt()
    assert "FRESH AX TREE" in p
    assert "tool call's return value" in p.lower()


def test_hybrid_prompt_warns_against_stale_training_date():
    """Same epistemic-failure-mode warning the API baseline learned
    the hard way (the date hallucination across 3 of 4 api_only
    failures). Pin the system.now hint."""
    p = H.build_hybrid_system_prompt()
    assert "system.now" in p
    assert "stale" in p.lower() or "training" in p.lower()


def test_hybrid_prompt_enforces_one_action_per_turn():
    """Mirror the UI scaffold's task-#262 multi-action rejection +
    the API scaffold's parallel-tool-calls-off rule. Both apply here
    because either surface could in principle emit ≥2 actions."""
    p = H.build_hybrid_system_prompt()
    assert "ONE action per turn" in p


# ---------------------------------------------------------------------------
# Dispatcher contract
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLMResponse:
    """Minimal stand-in for sibb_llm.LLMResponse — just the fields the
    dispatcher reads."""
    text: str = ""
    tool_calls: List[Any] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: Optional[str] = None
    cached_input_tokens: Optional[int] = None
    thinking: List[Any] = field(default_factory=list)


@dataclass
class _FakeToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


class _FakeAPIDispatcher:
    """Records every (name, args) the dispatcher dispatches; returns
    a canned ok=True payload."""

    def __init__(self) -> None:
        self.calls: List = []
        self.observed_bundles: List[str] = []
        self.discovered_tools: List[str] = []

    def current_catalog(self) -> List[str]:
        return ["agent.search_tools", "agent.answer", "agent.fail"]

    async def dispatch(self, name: str, args: Dict[str, Any]):
        self.calls.append((name, args))
        # Mirror the real dispatcher's ToolCallResult shape.
        return T.ToolCallResult(
            ok=True, payload={"created_id": "fake-1"})


class _FakeUIScaffold:
    """parse_action returns a canned AgentAction. Records every input."""

    def __init__(self) -> None:
        self.parsed: List[str] = []

    def parse_action(self, text: str):
        self.parsed.append(text)
        # Minimal AgentAction stand-in.
        @dataclass
        class _A:
            action_type: str = "tap"
            raw_verb: str = "TAP"
            target_ref: Optional[str] = "e0042"
            target_label: Optional[str] = None
            text: Optional[str] = None
            direction: Optional[str] = None
            amount: Optional[int] = None
            reason: Optional[str] = None
            answer_payload: Any = None
            parse_error: Optional[str] = None
        return _A()


async def _fake_ui_execute(reader, action, tree):
    """Stand-in for sibb_assistant.execute. Returns ok=True."""
    return {"success": True, "ok": True, "terminal": False,
            "ref": action.target_ref}


# Monkey-patch sibb_assistant.execute → _fake_ui_execute at module load
# time for the dispatcher tests, so the dispatch_hybrid_step's UI path
# doesn't try to hit a real reader.

@pytest.fixture
def patched_ui_execute(monkeypatch):
    """Swap the dispatcher's `ui_execute` for the test stub."""
    monkeypatch.setattr(H, "ui_execute", _fake_ui_execute)


# --- Routing ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_routes_structured_call_to_api_path(
        patched_ui_execute):
    """When tool_calls are present, the dispatcher routes to the API
    side regardless of what the text field says."""
    resp = _FakeLLMResponse(
        text="TAP @e0042",  # would route to UI if no tool call
        tool_calls=[_FakeToolCall(
            id="t1", name="eventkit.create_event",
            arguments={"title": "X",
                        "start_iso": "2026-07-08T12:00:00",
                        "end_iso": "2026-07-08T13:00:00"})])
    api_disp = _FakeAPIDispatcher()
    ui_scaffold = _FakeUIScaffold()
    res = await H.dispatch_hybrid_step(
        llm_response=resp, api_dispatcher=api_disp,
        ui_scaffold=ui_scaffold, ui_reader=None, ui_tree=None)
    assert res.action_type == "api"
    assert res.api_tool_name == "eventkit.create_event"
    assert res.terminal is False
    # Tool call was dispatched, UI text was NOT parsed.
    assert len(api_disp.calls) == 1
    assert ui_scaffold.parsed == []


@pytest.mark.asyncio
async def test_dispatch_routes_text_verb_to_ui_path(patched_ui_execute):
    """When no tool_calls, the text is parsed via the UI scaffold."""
    resp = _FakeLLMResponse(text="TAP @e0042")
    api_disp = _FakeAPIDispatcher()
    ui_scaffold = _FakeUIScaffold()
    res = await H.dispatch_hybrid_step(
        llm_response=resp, api_dispatcher=api_disp,
        ui_scaffold=ui_scaffold, ui_reader=None, ui_tree=None)
    assert res.action_type == "ui"
    assert res.raw_verb == "TAP"
    assert res.terminal is False
    # UI parser was hit, API dispatcher was NOT.
    assert ui_scaffold.parsed == ["TAP @e0042"]
    assert api_disp.calls == []


# --- Terminal normalization -----------------------------------------------


@pytest.mark.asyncio
async def test_agent_answer_tool_call_produces_terminal_with_payload(
        patched_ui_execute):
    resp = _FakeLLMResponse(
        text="",
        tool_calls=[_FakeToolCall(
            id="t1", name="agent.answer",
            arguments={"answer": {"items": [{"title": "X"}]}})])
    api_disp = _FakeAPIDispatcher()
    res = await H.dispatch_hybrid_step(
        llm_response=resp, api_dispatcher=api_disp,
        ui_scaffold=_FakeUIScaffold(), ui_reader=None, ui_tree=None)
    assert res.terminal is True
    assert res.action_type == "api_terminal"
    assert res.answer_payload == {"items": [{"title": "X"}]}
    # The terminal is intercepted before the API dispatcher is called.
    assert api_disp.calls == []


@pytest.mark.asyncio
async def test_text_ANSWER_produces_same_terminal_event(patched_ui_execute):
    """ANSWER {json} as text must produce the same HybridStepResult
    shape as agent.answer(answer={...})."""
    resp = _FakeLLMResponse(
        text='ANSWER {"items": [{"title": "X"}]}')
    res = await H.dispatch_hybrid_step(
        llm_response=resp, api_dispatcher=_FakeAPIDispatcher(),
        ui_scaffold=_FakeUIScaffold(), ui_reader=None, ui_tree=None)
    assert res.terminal is True
    assert res.action_type == "ui_terminal"
    assert res.answer_payload == {"items": [{"title": "X"}]}


@pytest.mark.asyncio
async def test_text_ANSWER_with_malformed_json_preserves_raw_string(
        patched_ui_execute):
    """If the JSON inside ANSWER {...} doesn't parse, fall back to the
    raw string so the verifier still gets SOMETHING. The verifier may
    reject it but the answer isn't silently dropped."""
    resp = _FakeLLMResponse(text='ANSWER {not really json}')
    res = await H.dispatch_hybrid_step(
        llm_response=resp, api_dispatcher=_FakeAPIDispatcher(),
        ui_scaffold=_FakeUIScaffold(), ui_reader=None, ui_tree=None)
    assert res.terminal is True
    assert res.action_type == "ui_terminal"
    assert isinstance(res.answer_payload, str)
    assert "not really json" in res.answer_payload


@pytest.mark.asyncio
async def test_agent_fail_tool_call_produces_terminal_with_reason(
        patched_ui_execute):
    resp = _FakeLLMResponse(
        text="",
        tool_calls=[_FakeToolCall(
            id="t1", name="agent.fail",
            arguments={"reason": "no API for inbound iMessage"})])
    res = await H.dispatch_hybrid_step(
        llm_response=resp, api_dispatcher=_FakeAPIDispatcher(),
        ui_scaffold=_FakeUIScaffold(), ui_reader=None, ui_tree=None)
    assert res.terminal is True
    assert res.action_type == "api_terminal"
    assert res.fail_reason == "no API for inbound iMessage"


@pytest.mark.asyncio
async def test_text_FAIL_produces_same_terminal_event(patched_ui_execute):
    resp = _FakeLLMResponse(
        text='FAIL "no API for inbound iMessage"')
    res = await H.dispatch_hybrid_step(
        llm_response=resp, api_dispatcher=_FakeAPIDispatcher(),
        ui_scaffold=_FakeUIScaffold(), ui_reader=None, ui_tree=None)
    assert res.terminal is True
    assert res.action_type == "ui_terminal"
    assert res.fail_reason == "no API for inbound iMessage"


# --- agent.fail tool catalog integration ----------------------------------


def test_agent_fail_is_in_non_deferred_reserve():
    """The fail terminal must be reachable from turn 1 — not deferred
    behind agent.search_tools."""
    reserve = T.non_deferred_tool_names()
    assert "agent.fail" in reserve


def test_agent_fail_tool_is_terminal_and_has_no_command_type():
    fail = T.TOOLS_BY_NAME["agent.fail"]
    assert fail.is_terminal is True
    assert fail.defer_loading is False
    assert fail.command_type is None
    # Schema: required `reason` string, closed for strict-mode.
    sch = fail.input_schema
    assert sch["type"] == "object"
    assert "reason" in sch["properties"]
    assert sch["required"] == ["reason"]
    assert sch["additionalProperties"] is False


# --- Search-tools indexing scope ------------------------------------------


def test_agent_search_tools_does_not_surface_ui_verbs():
    """The hybrid prompt scopes search to the API tool catalog. UI
    verbs (TAP/TYPE/SCROLL/...) stay always-loaded in the prompt and
    are NOT in the BM25 index. A query for 'tap a button' should
    return API tools, not pretend there's a `ui.tap`."""
    idx = T.BM25ToolIndex()
    hits = idx.top_k("tap a button", k=8)
    # No "ui.*" entries should appear (the API catalog has none).
    assert not any(name.startswith("ui.") for name in hits), (
        f"BM25 index leaked a UI verb namespace: {hits}")


# ---------------------------------------------------------------------------
# Provider-format defense — the Anthropic↔Gemini bug we hit on 2026-06-11
# ---------------------------------------------------------------------------
#
# The hybrid agent's per-turn API-action threading MUST go through the
# provider-aware helpers in sibb_llm.py (`append_assistant_with_tool_calls`
# and `append_tool_result`). Manually constructing Anthropic-style
# {"role": "assistant", "content": [{"type": "tool_use", ...}]} blocks
# breaks Gemini with 45 pydantic ValidationErrors on the next chat() call.
#
# Three layers of defense pin this:
#   1. Source-text pin: forbid raw `{"type": "tool_use"` strings in the
#      hybrid module.
#   2. Source-text pin: assert the module uses the provider-aware helpers.
#   3. L1.5 contract: `thread_api_turn` invokes both helpers in order.


def test_hybrid_assistant_does_not_inline_construct_anthropic_tool_blocks():
    """Source-text pin — the bug we hit at v3 sim time. The hybrid
    module MUST NOT contain manually constructed Anthropic-format
    tool_use / tool_result content blocks. They break Gemini.

    The only acceptable mention of these strings is inside DOCSTRINGS
    or COMMENTS warning against the pattern, so we allow them in
    those contexts but forbid the structural pattern."""
    import pathlib
    src = (pathlib.Path(H.__file__)).read_text()
    # Direct structural-pattern forbids — these can only appear as
    # actual content-block construction.
    forbidden_patterns = [
        '"type": "tool_use"',
        "'type': 'tool_use'",
        '"type": "tool_result"',
        "'type': 'tool_result'",
    ]
    for pat in forbidden_patterns:
        # We DO allow these strings inside the docstring that warns
        # about the bug — filter to lines that are part of code, not
        # the comment.
        # Easiest filter: strip lines that start with `#` or `"""`.
        for line_no, line in enumerate(src.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') \
                    or stripped.startswith("'''"):
                continue
            # And the docstring body itself — anything that's clearly
            # narrative text rather than code.
            if pat in stripped:
                # Permit it if the surrounding line looks like an
                # error-message or comment (the docstring inside
                # thread_api_turn mentions the pattern as a warning).
                if any(marker in stripped.lower() for marker in (
                        "anthropic", "warning", "do not", "do_not",
                        "forbidden", "must not", "f'", 'f"',
                        "warn", "wrong")):
                    continue
                raise AssertionError(
                    f"sibb_hybrid_assistant.py:{line_no} appears to "
                    f"construct an Anthropic-format tool block:\n"
                    f"  {line.rstrip()}\n"
                    f"Use thread_api_turn() instead; it routes through "
                    f"llm.append_* which translates per-provider.")


def test_hybrid_assistant_uses_provider_aware_threading_helpers():
    """Source-text pin — assert thread_api_turn delegates to the
    sibb_llm helpers. If a future edit drops these calls and
    re-introduces inline construction, this trips."""
    import pathlib
    src = (pathlib.Path(H.__file__)).read_text()
    assert "llm.append_assistant_with_tool_calls(" in src, (
        "thread_api_turn must call llm.append_assistant_with_tool_calls "
        "to round-trip the assistant's tool_use turn into the next "
        "chat()'s context, per-provider.")
    assert "llm.append_tool_result(" in src, (
        "thread_api_turn must call llm.append_tool_result so the agent "
        "sees its own tool's return value on the next chat() call.")


def test_thread_api_turn_invokes_both_helpers_in_order():
    """L1.5 contract — `thread_api_turn` calls both helpers in the
    correct order. Uses a recording FakeLLM that captures every helper
    invocation.

    This catches three classes of regression that source-text pins
    miss:
      - calling only one of the two helpers
      - calling them in the wrong order
      - re-introducing the inline pattern under a different docstring
        guise that source-text rules would tolerate"""
    invocations: List[Dict[str, Any]] = []

    class _RecordingFakeLLM:
        def append_assistant_with_tool_calls(
                self, messages, *, text, tool_calls):
            invocations.append({
                "method": "append_assistant_with_tool_calls",
                "messages_len_before": len(messages),
                "text": text,
                "n_tool_calls": len(tool_calls),
            })
            return messages + [{"role": "assistant", "_marker":
                                "assistant-helper"}]

        def append_tool_result(self, messages, *, tool_call_id,
                                  tool_name, result, is_error):
            invocations.append({
                "method": "append_tool_result",
                "messages_len_before": len(messages),
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "is_error": is_error,
            })
            return messages + [{"role": "user", "_marker":
                                "result-helper"}]

    # Build the LLMResponse + payload the helper expects.
    resp = _FakeLLMResponse(
        text="searching for tools",
        tool_calls=[_FakeToolCall(
            id="t1", name="agent.search_tools",
            arguments={"query": "create reminder"})])
    payload = {"ok": True, "payload": {"matches": []}}

    out = H.thread_api_turn(
        _RecordingFakeLLM(), [{"role": "user", "content": "start"}],
        response=resp, payload=payload, is_error=False)

    # Both helpers called, in the right order.
    assert len(invocations) == 2
    assert invocations[0]["method"] == "append_assistant_with_tool_calls"
    assert invocations[1]["method"] == "append_tool_result"
    # The second sees the result of the first (chained messages list).
    assert invocations[1]["messages_len_before"] == 2
    # Tool-call id / name plumbed through.
    assert invocations[1]["tool_call_id"] == "t1"
    assert invocations[1]["tool_name"] == "agent.search_tools"
    # `out` is the result of the chained appends — caller can keep going.
    assert len(out) == 3
