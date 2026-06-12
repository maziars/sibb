"""L1 tests for the sibb_llm.py extensions (tenacity, cost, tool-calling,
logprobs, vLLM).

These tests pin behavior at the boundary between provider SDKs and the
SIBB driver — without spinning up real provider clients. We mock at the
SDK class level, attaching to the AnthropicClient / OpenAIClient /
GeminiClient instance's `_client` attribute after construction, and
exercise the chat() path with canned responses.

We deliberately do NOT install/import the real provider SDKs in the test
process when avoidable — `_run_with_retry` falls back to a single
attempt if tenacity can't pull provider exception classes. Tests focus
on:

  - cost computation + accumulation
  - tool-call translation (MCP shape → wire format)
  - tool-call parsing (wire format → ToolCall list)
  - logprobs / prompt_logprobs passthrough
  - budget gating
  - tenacity wrapper invariants (single attempt when max_retries=0)
  - SDK max_retries=0 contract
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import pathlib
import types
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "sibb" / "benchmark"))

import sibb_llm as M  # noqa: E402


# ---------------------------------------------------------------------------
# Pricing & cost
# ---------------------------------------------------------------------------


def test_pricing_table_has_every_default_model():
    """make_client uses _DEFAULT_MODELS; each provider's default must be
    priced — otherwise cost_usd stays None for the default run."""
    for prov, default_m in M._DEFAULT_MODELS.items():
        if prov == "vllm":
            # vLLM is local — no API pricing.
            assert default_m not in M._PRICING
        else:
            assert default_m in M._PRICING, (
                f"{prov} default model {default_m!r} missing from _PRICING")


def test_compute_cost_arithmetic():
    """In/out rates are per-million tokens; cost is linear sum.

    Bucket-2 pricing audit verified against provider docs 2026-06-10:
    rates updated for opus 4.7/4.8, gemini-2.5-flash, gemini-2.5-pro,
    gpt-5, gpt-5-mini. Without cached_input_tokens, the cost is just
    in_rate*input + out_rate*output."""
    # claude-haiku-4-5: (1.00 in, 5.00 out) per 1M
    c = M._compute_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert c == pytest.approx(6.00, rel=1e-9)
    # Small token counts on the headline-run model (gemini-2.5-flash):
    # 1000 * 0.30 / 1M + 500 * 2.50 / 1M = 0.0003 + 0.00125 = 0.00155
    c = M._compute_cost("gemini-2.5-flash", 1000, 500)
    assert c == pytest.approx(0.00155, rel=1e-9)


def test_compute_cost_subtracts_cached_for_openai_subset_convention():
    """OpenAI reports `prompt_tokens` as TOTAL input including the
    cached subset (`prompt_tokens_details.cached_tokens`). To avoid
    double-charging, _compute_cost must subtract cached from full-rate
    billable and bill the cached portion at the discounted rate."""
    # gpt-5-mini: 0.125 in, 1.00 out, 0.0625 cached per 1M.
    # 1000 prompt_tokens of which 400 cached, 100 output:
    #   uncached billable = 600 * 0.125/1M = 0.000075
    #   cached billable   = 400 * 0.0625/1M = 0.000025
    #   output            = 100 * 1.00/1M = 0.0001
    #   total = 0.0002
    c = M._compute_cost("gpt-5-mini", 1000, 100,
                          cached_input_tokens=400, provider="openai")
    assert c == pytest.approx(0.0002, rel=1e-9)


def test_compute_cost_keeps_disjoint_for_anthropic_convention():
    """Anthropic reports input_tokens as NON-cached billable; the
    cache_read_input_tokens are tracked separately. So we should NOT
    subtract — the two pools are already disjoint."""
    # haiku: 1.00 in, 5.00 out, 0.10 cached per 1M.
    # 1000 non-cached + 400 cached + 100 output:
    #   uncached = 1000 * 1.00/1M = 0.001
    #   cached   = 400 * 0.10/1M = 0.00004
    #   output   = 100 * 5.00/1M = 0.0005
    #   total = 0.00154
    c = M._compute_cost("claude-haiku-4-5", 1000, 100,
                          cached_input_tokens=400, provider="anthropic")
    assert c == pytest.approx(0.00154, rel=1e-9)


def test_pricing_table_corrections_landed():
    """Pin the exact rates so a regression to the old (wrong) table
    surfaces on test, not in a billing surprise."""
    assert M._PRICING["claude-opus-4-7"] == (5.00, 25.00, 0.50)
    assert M._PRICING["claude-opus-4-8"] == (5.00, 25.00, 0.50)
    assert M._PRICING["gemini-2.5-flash"] == (0.30, 2.50, None)
    assert M._PRICING["gemini-2.5-pro"] == (1.25, 10.00, None)
    assert M._PRICING["gpt-5"] == (0.625, 5.00, 0.3125)
    assert M._PRICING["gpt-5-mini"] == (0.125, 1.00, 0.0625)


def test_compute_cost_unknown_model_returns_none():
    assert M._compute_cost("nonexistent-model", 100, 100) is None
    # Missing tokens disable cost.
    assert M._compute_cost("claude-haiku-4-5", None, 100) is None
    assert M._compute_cost("claude-haiku-4-5", 100, None) is None


# ---------------------------------------------------------------------------
# Tool-call translators (MCP → wire format)
# ---------------------------------------------------------------------------


MCP_FIXTURE = [
    {
        "name": "eventkit.create_reminder",
        "description": "Create a reminder via EventKit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "list": {"type": "string"},
            },
            "required": ["title", "list"],
            "additionalProperties": False,
        },
    },
    {
        "name": "agent.answer",
        "description": "Submit final answer.",
        "inputSchema": {
            "type": "object",
            "properties": {"answer": {}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    },
]


def test_tools_to_anthropic_uses_input_schema_snake_case():
    out = M._tools_to_anthropic(MCP_FIXTURE)
    assert len(out) == 2
    assert out[0]["name"] == "eventkit.create_reminder"
    assert "input_schema" in out[0]
    # Must NOT emit camelCase `inputSchema` at this layer.
    assert "inputSchema" not in out[0]
    assert out[0]["input_schema"]["required"] == ["title", "list"]


def test_tools_to_openai_nests_under_function_with_strict():
    out = M._tools_to_openai(MCP_FIXTURE, strict=True)
    assert len(out) == 2
    assert out[0]["type"] == "function"
    fn = out[0]["function"]
    assert fn["name"] == "eventkit.create_reminder"
    assert fn["strict"] is True
    # OpenAI uses `parameters`, not `input_schema`.
    assert "parameters" in fn
    assert "input_schema" not in fn


def test_tools_to_openai_strict_false_omits_flag():
    out = M._tools_to_openai(MCP_FIXTURE, strict=False)
    assert "strict" not in out[0]["function"]


def test_tools_to_gemini_groups_under_function_declarations():
    out = M._tools_to_gemini(MCP_FIXTURE)
    # Gemini wants tools=[{function_declarations: [...]}]
    assert len(out) == 1
    decls = out[0]["function_declarations"]
    assert len(decls) == 2
    assert decls[0]["name"] == "eventkit.create_reminder"
    # Gemini uses `parameters`.
    assert "parameters" in decls[0]


def test_make_openai_strict_schema_promotes_optionals_to_nullable():
    """Bucket-2 fix #8: OpenAI strict mode requires `required` to equal
    every property key, with optionals expressed as nullable. The
    rewriter must walk the tree and apply this on every nested object."""
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "list": {"type": "string"},
            "priority": {"type": "string", "enum": ["high", "low"]},
            "recurrence": {
                "type": "object",
                "properties": {
                    "frequency": {"type": "string"},
                    "interval": {"type": "integer"},
                },
                "required": ["frequency"],
            },
        },
        "required": ["title", "list"],
    }
    out = M._make_openai_strict_schema(schema)
    # Outer: required includes ALL props; additionalProperties: False.
    assert set(out["required"]) == {"title", "list", "priority", "recurrence"}
    assert out["additionalProperties"] is False
    # Originally optional 'priority' is nullable.
    assert out["properties"]["priority"]["type"] == ["string", "null"]
    # Originally required 'title' is NOT nullified.
    assert out["properties"]["title"]["type"] == "string"
    # Nested object got the same treatment.
    rec = out["properties"]["recurrence"]
    assert set(rec["required"]) == {"frequency", "interval"}
    assert rec["additionalProperties"] is False
    assert rec["properties"]["interval"]["type"] == ["integer", "null"]
    assert rec["properties"]["frequency"]["type"] == "string"


def test_tools_to_openai_emits_strict_compliant_schema_by_default():
    """End-to-end: feeding the MCP fixture through the OpenAI translator
    with strict=True (default) produces a schema that OpenAI's validator
    would accept (every property in required, additionalProperties:false)."""
    out = M._tools_to_openai(MCP_FIXTURE, strict=True)
    fn = out[0]["function"]
    params = fn["parameters"]
    assert fn["strict"] is True
    assert params["additionalProperties"] is False
    assert set(params["required"]) == set(params["properties"].keys())


def test_normalize_tool_choice_openai_maps_any_to_required():
    """Anthropic uses "any" for forced tool use; OpenAI uses "required".
    The translator must remap so a caller passing "any" doesn't fall
    through to the malformed `{type:function, function:{name:"any"}}`."""
    assert M._normalize_tool_choice_openai("any") == "required"


def test_anthropic_retryable_includes_529_overloaded():
    """Bucket-2 fix #3: 529 OverloadedError is the most common Claude
    transient in 2026. The class lives under anthropic._exceptions,
    not the package top level — _retryable_excs_for must walk
    APIStatusError subclasses to find it. Same treatment for the
    other transient 5xx kin (ServiceUnavailableError,
    DeadlineExceededError)."""
    excs = M._retryable_excs_for("anthropic")
    try:
        import anthropic
    except ImportError:
        pytest.skip("anthropic SDK not installed")
    names = {cls.__name__ for cls in excs}
    # At least one of the 5xx subclasses must be present.
    assert "OverloadedError" in names, (
        f"OverloadedError missing from retryable set; got {names}")


def test_gemini_clienterror_is_NOT_in_retryable_set():
    """Bucket-2 fix #4: gerrors.ClientError (4xx supertype) used to be
    blanket-retried, which burns attempts on auth/badreq errors. We
    deliberately exclude it; 429/408 are routed through the predicate."""
    excs = M._retryable_excs_for("gemini")
    try:
        from google.genai import errors as gerrors
        assert gerrors.ClientError not in excs
        # ServerError IS in the set.
        assert gerrors.ServerError in excs
    except ImportError:
        pytest.skip("google-genai not installed")


def test_tools_to_gemini_strips_additional_properties_recursively():
    """Empirical sim-smoke (2026-06-10): Gemini's REST API rejects
    parameters with `additionalProperties` as
    `Unknown name "additional_properties"`. The sanitizer MUST strip
    it from every nested object, otherwise every Gemini tool-calling
    request 400s before reaching the model."""
    schema = {
        "type": "object",
        "properties": {
            "outer": {"type": "string"},
            "nested": {
                "type": "object",
                "properties": {"inner": {"type": "string"}},
                "additionalProperties": False,
            },
            "items_arr": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"k": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
        "required": ["outer"],
    }
    out = M._tools_to_gemini([{
        "name": "x",
        "description": "x",
        "inputSchema": schema,
    }])
    decl = out[0]["function_declarations"][0]
    params = decl["parameters"]
    assert "additionalProperties" not in params
    nested = params["properties"]["nested"]
    assert "additionalProperties" not in nested
    items = params["properties"]["items_arr"]["items"]
    assert "additionalProperties" not in items
    # The legitimate fields survive.
    assert params["required"] == ["outer"]
    assert nested["properties"]["inner"]["type"] == "string"


def test_sanitize_gemini_schema_drops_oneof_anyof_const_refs():
    """Gemini's parameters parser rejects oneOf/anyOf/allOf/const/$ref
    too. The sanitizer drops all of them so future tool authors don't
    accidentally break Gemini compatibility."""
    schema = {
        "type": "object",
        "properties": {
            "either": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "shared": {"$ref": "#/$defs/SomeType"},
            "literal": {"const": "X"},
        },
        "anyOf": [{"required": ["either"]}],
    }
    out = M._sanitize_gemini_schema(schema)
    assert "anyOf" not in out
    assert "oneOf" not in out["properties"]["either"]
    assert "$ref" not in out["properties"]["shared"]
    assert "const" not in out["properties"]["literal"]


# ---------------------------------------------------------------------------
# tool_choice normalization
# ---------------------------------------------------------------------------


def test_normalize_tool_choice_anthropic():
    assert M._normalize_tool_choice_anthropic("auto") == {"type": "auto"}
    assert M._normalize_tool_choice_anthropic("any") == {"type": "any"}
    assert M._normalize_tool_choice_anthropic("none") == {"type": "none"}
    assert M._normalize_tool_choice_anthropic("agent.answer") == {
        "type": "tool", "name": "agent.answer"}
    # Dict passthrough.
    d = {"type": "tool", "name": "x", "disable_parallel_tool_use": True}
    assert M._normalize_tool_choice_anthropic(d) == d


def test_normalize_tool_choice_openai():
    assert M._normalize_tool_choice_openai("auto") == "auto"
    assert M._normalize_tool_choice_openai("required") == "required"
    assert M._normalize_tool_choice_openai("none") == "none"
    assert M._normalize_tool_choice_openai("agent.answer") == {
        "type": "function", "function": {"name": "agent.answer"}}


def test_normalize_tool_choice_gemini():
    assert M._normalize_tool_choice_gemini("auto") == {
        "function_calling_config": {"mode": "AUTO"}}
    assert M._normalize_tool_choice_gemini("any") == {
        "function_calling_config": {"mode": "ANY"}}
    assert M._normalize_tool_choice_gemini("none") == {
        "function_calling_config": {"mode": "NONE"}}
    out = M._normalize_tool_choice_gemini("agent.answer")
    assert out["function_calling_config"]["mode"] == "ANY"
    assert out["function_calling_config"]["allowed_function_names"] == [
        "agent.answer"]


# ---------------------------------------------------------------------------
# Tenacity wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_retry_single_attempt_when_max_retries_zero():
    """`max_retries=0` must execute the coro exactly once and return —
    no backoff, no swallowing of errors."""
    calls = {"n": 0}

    async def coro():
        calls["n"] += 1
        return "result"

    out = await M._run_with_retry("openai", 0, coro)
    assert out == "result"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_run_with_retry_reraises_non_retryable_immediately():
    """A non-retryable exception (e.g. ValueError) must surface on the
    first call — no silent retry."""
    calls = {"n": 0}

    async def coro():
        calls["n"] += 1
        raise ValueError("config wrong")

    with pytest.raises(ValueError, match="config wrong"):
        await M._run_with_retry("openai", 5, coro)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_run_with_retry_retries_asyncio_timeout():
    """asyncio.TimeoutError is in every provider's retryable set;
    confirm tenacity retries it then succeeds."""
    calls = {"n": 0}

    async def coro():
        calls["n"] += 1
        if calls["n"] < 2:
            raise asyncio.TimeoutError("transient")
        return "ok"

    out = await M._run_with_retry("openai", 3, coro)
    assert out == "ok"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Tool-call result wrappers
# ---------------------------------------------------------------------------


def test_toolcall_dataclass_shape():
    tc = M.ToolCall(id="x", name="eventkit.create_event",
                     arguments={"title": "Date Night"})
    assert tc.id == "x"
    assert tc.name == "eventkit.create_event"
    assert tc.arguments == {"title": "Date Night"}


def test_llmresponse_defaults():
    r = M.LLMResponse(text="hi", provider="anthropic", model="claude-haiku-4-5")
    assert r.tool_calls == []
    assert r.cost_usd is None
    assert r.completion_logprobs is None
    assert r.prompt_logprobs is None


# ---------------------------------------------------------------------------
# Budget gating
# ---------------------------------------------------------------------------


class _MinimalClient(M.LLMClient):
    """A subclass with no SDK requirement for unit-testing base methods."""

    def __init__(self, **kw):
        # Skip LLMClient.__init__ super-chain that asserts SDK; just set
        # the state directly.
        self.model = kw.get("model", "claude-haiku-4-5")
        self.api_key = None
        self.timeout = 60.0
        self.max_retries = 0
        self.budget_usd_max = kw.get("budget_usd_max")
        self.spent_usd = kw.get("spent_usd", 0.0)


def test_budget_gate_raises_when_at_or_above_cap():
    c = _MinimalClient(budget_usd_max=1.00, spent_usd=1.00)
    with pytest.raises(M.BudgetExceededError, match="budget exceeded"):
        c._gate_budget()


def test_budget_gate_passes_below_cap():
    c = _MinimalClient(budget_usd_max=1.00, spent_usd=0.50)
    c._gate_budget()  # No raise.


def test_budget_gate_disabled_when_cap_is_none():
    c = _MinimalClient(budget_usd_max=None, spent_usd=999_999.0)
    c._gate_budget()  # No raise — cap disabled.


def test_record_cost_accumulates_only_priced_responses():
    c = _MinimalClient()
    c._record_cost(M.LLMResponse(text="", provider="x", model="y",
                                   cost_usd=0.0035))
    c._record_cost(M.LLMResponse(text="", provider="x", model="y",
                                   cost_usd=None))  # ignored
    c._record_cost(M.LLMResponse(text="", provider="x", model="y",
                                   cost_usd=0.001))
    assert c.spent_usd == pytest.approx(0.0045)


# ---------------------------------------------------------------------------
# Provider SDK max_retries=0 contract — pin via construction
# ---------------------------------------------------------------------------
#
# Skipped if SDK not installed; checked against the freshly-constructed
# instance's `_client.max_retries` attribute. The OpenAI / Anthropic
# SDKs expose this directly; Gemini SDK doesn't have the knob.


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_module("anthropic"),
                     reason="anthropic SDK not installed")
def test_anthropic_client_sets_sdk_max_retries_zero():
    os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
    c = M.AnthropicClient(model="claude-haiku-4-5", max_retries=5)
    # tenacity owns retries — the SDK must be 0.
    assert c._client.max_retries == 0
    assert c.max_retries == 5  # the user-facing knob is preserved


@pytest.mark.skipif(not _has_module("openai"),
                     reason="openai SDK not installed")
def test_openai_client_sets_sdk_max_retries_zero():
    os.environ.setdefault("OPENAI_API_KEY", "fake")
    c = M.OpenAIClient(model="gpt-5-mini", max_retries=5)
    assert c._client.max_retries == 0
    assert c.max_retries == 5


@pytest.mark.skipif(not _has_module("openai"),
                     reason="openai SDK not installed")
def test_vllm_client_inherits_max_retries_zero_and_sets_base_url():
    c = M.VLLMClient(model="Qwen/Qwen3-8B",
                       base_url="http://localhost:8000/v1",
                       max_retries=5)
    assert c._client.max_retries == 0
    # The injected base_url is what the SDK actually uses.
    assert "localhost:8000" in str(c._client.base_url)
    # vLLM-served models aren't in the API-pricing table; cost stays None.
    assert M._compute_cost(c.model, 100, 100) is None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_factory_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown provider"):
        M.make_client("notreal")


def test_factory_lists_vllm_as_available():
    assert "vllm" in M.available_providers()
    assert M.default_model("vllm") == "Qwen/Qwen3-8B"


@pytest.mark.skipif(not _has_module("openai"),
                     reason="openai SDK not installed")
def test_factory_passes_base_url_only_to_compatible_providers():
    """`base_url` is only meaningful for openai/vllm. The factory passes
    it through for those and ignores it for others."""
    os.environ.setdefault("OPENAI_API_KEY", "fake")
    c = M.make_client("openai", base_url="https://proxy.example.com/v1")
    assert "proxy.example.com" in str(c._client.base_url)


# ---------------------------------------------------------------------------
# Tool-call parsing — provider chat() smoke
# ---------------------------------------------------------------------------
#
# We construct the client with a fake `_client` attribute that returns a
# canned response, then call chat() and inspect the parsed shape.


class _AnthropicBlock:
    """Mimics anthropic.types.ContentBlock — text or tool_use."""
    def __init__(self, btype: str, **kw):
        self.type = btype
        for k, v in kw.items():
            setattr(self, k, v)


class _AnthropicUsage:
    def __init__(self, input_tokens, output_tokens,
                  cache_read_input_tokens=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _AnthropicResponse:
    def __init__(self, content, usage, stop_reason="end_turn"):
        self.content = content
        self.usage = usage
        self.stop_reason = stop_reason


def _stub_anthropic_client(canned_response):
    """Build an AnthropicClient whose _client.messages.create returns
    `canned_response`. Doesn't touch the real SDK."""
    if not _has_module("anthropic"):
        pytest.skip("anthropic SDK not installed")
    os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
    client = M.AnthropicClient(model="claude-haiku-4-5", max_retries=0)
    fake_messages = MagicMock()
    fake_messages.create = AsyncMock(return_value=canned_response)
    client._client = MagicMock()
    client._client.messages = fake_messages
    return client, fake_messages


@pytest.mark.asyncio
async def test_anthropic_chat_parses_tool_use_block_into_toolcall():
    canned = _AnthropicResponse(
        content=[
            _AnthropicBlock("text", text="I'll add it."),
            _AnthropicBlock(
                "tool_use",
                id="toolu_01ABC",
                name="eventkit.create_reminder",
                input={"title": "Pay rent", "list": "Bills"}),
        ],
        usage=_AnthropicUsage(input_tokens=500, output_tokens=80),
        stop_reason="tool_use",
    )
    client, fake = _stub_anthropic_client(canned)

    resp = await client.chat(
        [{"role": "user", "content": "Add Pay rent to Bills"}],
        system="You add reminders.",
        tools=MCP_FIXTURE,
        tool_choice="auto",
    )

    assert resp.text == "I'll add it."
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "toolu_01ABC"
    assert tc.name == "eventkit.create_reminder"
    assert tc.arguments == {"title": "Pay rent", "list": "Bills"}
    # Cost computed from haiku rates: 500*1/1M + 80*5/1M
    assert resp.cost_usd == pytest.approx(0.0009)
    assert client.spent_usd == pytest.approx(0.0009)

    # Translator emitted snake_case input_schema.
    call_kwargs = fake.create.await_args.kwargs
    assert "tools" in call_kwargs
    assert call_kwargs["tools"][0]["input_schema"]["required"] == [
        "title", "list"]
    # parallel_tool_calls defaults to False → disable_parallel_tool_use=True
    tc_param = call_kwargs.get("tool_choice", {})
    assert tc_param.get("disable_parallel_tool_use") is True


@pytest.mark.asyncio
async def test_anthropic_chat_system_cache_wraps_text_block():
    canned = _AnthropicResponse(
        content=[_AnthropicBlock("text", text="ok")],
        usage=_AnthropicUsage(input_tokens=100, output_tokens=10),
    )
    client, fake = _stub_anthropic_client(canned)
    await client.chat([{"role": "user", "content": "hi"}],
                       system="LONG PREFIX",
                       system_cache=True)
    sent = fake.create.await_args.kwargs
    assert isinstance(sent["system"], list)
    assert sent["system"][0]["cache_control"]["type"] == "ephemeral"
    assert sent["system"][0]["text"] == "LONG PREFIX"


@pytest.mark.asyncio
async def test_anthropic_chat_betas_emit_anthropic_beta_header():
    canned = _AnthropicResponse(
        content=[_AnthropicBlock("text", text="ok")],
        usage=_AnthropicUsage(input_tokens=10, output_tokens=10),
    )
    client, fake = _stub_anthropic_client(canned)
    await client.chat([{"role": "user", "content": "x"}],
                       betas=["tool-search-2025-11-19", "computer-use-2025"])
    sent = fake.create.await_args
    headers = sent.kwargs.get("extra_headers", {})
    assert "anthropic-beta" in headers
    val = headers["anthropic-beta"]
    assert "tool-search-2025-11-19" in val
    assert "computer-use-2025" in val
    # Comma-separated, not list.
    assert isinstance(val, str)


# ---------------------------------------------------------------------------
# OpenAI tool-call parsing
# ---------------------------------------------------------------------------


def _stub_openai_client(canned_response):
    if not _has_module("openai"):
        pytest.skip("openai SDK not installed")
    os.environ.setdefault("OPENAI_API_KEY", "fake")
    client = M.OpenAIClient(model="gpt-5-mini", max_retries=0)
    fake_chat = MagicMock()
    fake_chat.completions = MagicMock()
    fake_chat.completions.create = AsyncMock(return_value=canned_response)
    client._client = MagicMock()
    client._client.chat = fake_chat
    return client, fake_chat


@pytest.mark.asyncio
async def test_openai_chat_parses_tool_calls_with_json_args():
    # OpenAI: choice.message.tool_calls = [{id, function:{name,arguments(str)}}]
    fn = MagicMock()
    fn.name = "eventkit.create_reminder"
    fn.arguments = json.dumps({"title": "Pay rent", "list": "Bills"})
    tc_obj = MagicMock()
    tc_obj.id = "call_abc"
    tc_obj.function = fn

    msg = MagicMock()
    msg.content = None
    msg.tool_calls = [tc_obj]

    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "tool_calls"
    choice.logprobs = None

    usage = MagicMock()
    usage.prompt_tokens = 200
    usage.completion_tokens = 30
    usage.prompt_tokens_details = None

    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    resp.prompt_logprobs = None

    client, _ = _stub_openai_client(resp)
    out = await client.chat(
        [{"role": "user", "content": "Add Pay rent"}],
        tools=MCP_FIXTURE)
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].id == "call_abc"
    assert out.tool_calls[0].arguments["title"] == "Pay rent"
    # Cost: gpt-5-mini = 0.50 in, 1.50 out per million.
    # 200 * 0.50/1M + 30 * 1.50/1M = 0.0001 + 0.000045 = 0.000145
    assert out.cost_usd == pytest.approx(0.000145)


@pytest.mark.asyncio
async def test_openai_chat_handles_logprobs_passthrough():
    # OpenAI: choice.logprobs.content = [{logprob, token, ...}, ...]
    lp_item_1 = MagicMock(); lp_item_1.logprob = -0.5
    lp_item_2 = MagicMock(); lp_item_2.logprob = -1.2
    lps = MagicMock(); lps.content = [lp_item_1, lp_item_2]

    msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
    choice = MagicMock(); choice.message = msg
    choice.finish_reason = "stop"
    choice.logprobs = lps
    usage = MagicMock(); usage.prompt_tokens = 10; usage.completion_tokens = 2
    usage.prompt_tokens_details = None
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    resp.prompt_logprobs = None

    client, fake_chat = _stub_openai_client(resp)
    out = await client.chat([{"role": "user", "content": "x"}],
                              logprobs=True, top_logprobs=5)
    assert out.completion_logprobs == [-0.5, -1.2]
    # Confirm the SDK was actually told to emit logprobs.
    sent = fake_chat.completions.create.await_args.kwargs
    assert sent["logprobs"] is True
    assert sent["top_logprobs"] == 5


@pytest.mark.asyncio
async def test_openai_chat_prompt_logprobs_routes_through_extra_body():
    """prompt_logprobs is a vLLM extension; the OpenAI translator must
    forward it via extra_body so vanilla OpenAI doesn't see it."""
    msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
    choice = MagicMock(); choice.message = msg
    choice.finish_reason = "stop"; choice.logprobs = None
    usage = MagicMock(); usage.prompt_tokens = 10; usage.completion_tokens = 2
    usage.prompt_tokens_details = None
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    resp.prompt_logprobs = None

    client, fake_chat = _stub_openai_client(resp)
    await client.chat([{"role": "user", "content": "x"}], prompt_logprobs=1)
    sent = fake_chat.completions.create.await_args.kwargs
    assert sent.get("extra_body") == {"prompt_logprobs": 1}


@pytest.mark.asyncio
async def test_openai_chat_strict_mode_is_default_on_tools():
    """The translator MUST emit strict=True by default to match the
    paper's Methodology commitment."""
    msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
    choice = MagicMock(); choice.message = msg
    choice.finish_reason = "stop"; choice.logprobs = None
    usage = MagicMock(); usage.prompt_tokens = 5; usage.completion_tokens = 1
    usage.prompt_tokens_details = None
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    resp.prompt_logprobs = None

    client, fake_chat = _stub_openai_client(resp)
    await client.chat([{"role": "user", "content": "x"}], tools=MCP_FIXTURE)
    sent = fake_chat.completions.create.await_args.kwargs
    assert sent["tools"][0]["function"]["strict"] is True


@pytest.mark.asyncio
async def test_openai_chat_parallel_tool_calls_forwards_through():
    msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
    choice = MagicMock(); choice.message = msg
    choice.finish_reason = "stop"; choice.logprobs = None
    usage = MagicMock(); usage.prompt_tokens = 5; usage.completion_tokens = 1
    usage.prompt_tokens_details = None
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    resp.prompt_logprobs = None

    client, fake_chat = _stub_openai_client(resp)
    await client.chat([{"role": "user", "content": "x"}],
                       tools=MCP_FIXTURE,
                       parallel_tool_calls=False)
    sent = fake_chat.completions.create.await_args.kwargs
    assert sent["parallel_tool_calls"] is False


# ---------------------------------------------------------------------------
# Existing call-site compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_chat_signature_still_works_without_tool_args():
    """sibb_assistant.py calls chat(history, system=..., max_tokens=...,
    temperature=...). The extension MUST NOT break that signature."""
    msg = MagicMock(); msg.content = "TAP @e012"
    msg.tool_calls = None
    choice = MagicMock(); choice.message = msg
    choice.finish_reason = "stop"; choice.logprobs = None
    usage = MagicMock(); usage.prompt_tokens = 100; usage.completion_tokens = 5
    usage.prompt_tokens_details = None
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    resp.prompt_logprobs = None

    client, _ = _stub_openai_client(resp)
    out = await client.chat(
        [{"role": "user", "content": "obs"}],
        system="UI prompt",
        max_tokens=512,
        temperature=0.7,
    )
    assert out.text == "TAP @e012"
    assert out.tool_calls == []
    # Cost still tracked.
    assert out.cost_usd is not None
