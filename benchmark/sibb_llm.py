"""SIBB LLM client — uniform async chat across providers.

Currently supports:
  - anthropic   (claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-7)
  - gemini      (gemini-2.5-flash, gemini-2.5-pro)
  - openai      (gpt-5, gpt-5-mini, ...)
  - vllm        (locally-served, OpenAI-compatible — Qwen3, Llama, ...)

Wire-format used by the driver (provider-agnostic):

    messages = [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."},
        ...
    ]
    resp = await client.chat(messages, system="...", max_tokens=1024)
    print(resp.text, resp.cost_usd, resp.tool_calls)

For tool-calling (used by the API agent under `sibb/api_baseline/`):

    tools = sibb_api_tools.mcp_tools()   # MCP-shape definitions
    resp = await client.chat(
        messages, system="...", tools=tools, tool_choice="auto",
        max_tokens=1024,
    )
    if resp.tool_calls:
        # dispatch via APIToolDispatcher → append tool_result to messages
        ...

Provider SDKs are imported lazily so a missing SDK doesn't break startup
for unrelated providers. API keys are read from environment variables
(`ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `GOOGLE_API_KEY`,
`OPENAI_API_KEY`) unless passed explicitly.

Operational notes:
  - Retries / backoff are handled by `tenacity` at the chat() boundary
    using exponential backoff. Provider SDK `max_retries` is force-set
    to 0 in every client constructor — letting both layers retry would
    stack multiplicatively (e.g. 6 × 6 = 36 attempts on a 529).
  - Cost is computed from a per-model `_PRICING` table when token
    counts are available; populated as `LLMResponse.cost_usd` and
    accumulated on the client as `client.spent_usd`. The pricing
    table is verified periodically but treated as approximate — DO
    NOT rely on it for billing-grade accounting.
  - vLLM is served via the OpenAI-compatible Chat Completions API; the
    `VLLMClient` subclass injects `base_url` and defaults `api_key`
    to "EMPTY" so the SDK doesn't reject the request. Use it for RL
    training-time rollouts where logprobs / prompt_logprobs are needed.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


# ───────────────────────────── result wrappers ───────────────────────────────


@dataclass
class ToolCall:
    """Provider-normalized tool-use record emitted by the model.

    The `id` is the per-call identifier the provider expects in the
    tool_result return path (Anthropic `tool_use_id`, OpenAI
    `tool_call_id`, Gemini's function-call name reused).

    `arguments` is the JSON-decoded args dict. On strict-mode-enforced
    providers (Anthropic / OpenAI with `strict: true`) it satisfies the
    tool's `input_schema` by construction; on Gemini we re-validate
    upstream.
    """
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    stop_reason: Optional[str] = None
    # NEW: tool-calling normalization
    tool_calls: List[ToolCall] = field(default_factory=list)
    # NEW: cost tracking; populated when input/output tokens are known
    cost_usd: Optional[float] = None
    # NEW: vLLM / RL training hooks — passthrough fields. Each entry is
    # a list of float logprob values per token; None when the backend
    # doesn't expose them (Anthropic structurally lacks this).
    completion_logprobs: Optional[List[float]] = None
    prompt_logprobs: Optional[List[float]] = None
    # NEW: model reasoning trace — populated when the provider exposes
    # one. Each entry is a dict describing one thinking block:
    #   {"kind": "text",      "text": "...the visible reasoning..."}
    #   {"kind": "signature", "signature": "<opaque base64>"}
    # Anthropic returns `thinking` content blocks (text). Gemini 2.5
    # returns `thought_signature` parts (opaque). OpenAI returns
    # `reasoning` summary text on supported models. Empty list when
    # none was emitted.
    thinking: List[Dict[str, Any]] = field(default_factory=list)
    # NEW: raw provider response — kept off __repr__ to avoid spam.
    raw: Any = field(default=None, repr=False)


# ───────────────────────────── pricing (per million tokens) ──────────────────
#
# USD per 1M tokens — verified 2026-06-10 against provider docs:
#  - Anthropic claude pricing docs (platform.claude.com/docs/en/about-claude/pricing)
#  - Gemini API pricing (ai.google.dev/gemini-api/docs/pricing)
#  - OpenAI API pricing (openai.com/api/pricing)
#
# Tuple is (input_rate, output_rate, cached_input_rate). cached_input_rate
# is the discount applied to *cache read* tokens (Anthropic emits a
# separate `cache_read_input_tokens` field; OpenAI's
# `prompt_tokens_details.cached_tokens` is a subset of `prompt_tokens`;
# Gemini reports `cached_content_token_count`). Set to None when no
# documented implicit-cache discount applies.
#
# An earlier draft had Opus 4.x at 15/75, Gemini Flash at 0.15/0.60,
# and GPT-5 at 10/30 — those were wrong by 2-16x. Corrected per the
# Bucket-2 cost audit (Critic 4 in the 10-critic panel).

_PRICING: Dict[str, Tuple[float, float, Optional[float]]] = {
    # Anthropic (cache reads at 10% of input rate)
    "claude-haiku-4-5":      (1.00,  5.00,  0.10),
    "claude-sonnet-4-6":     (3.00, 15.00,  0.30),
    "claude-opus-4-7":       (5.00, 25.00,  0.50),
    "claude-opus-4-8":       (5.00, 25.00,  0.50),
    # Google Gemini (no documented implicit-cache discount; explicit
    # cachedContent API has a separate billing model not modeled here)
    "gemini-2.5-flash":      (0.30,  2.50,  None),
    "gemini-2.5-pro":        (1.25, 10.00,  None),
    # OpenAI (cache reads at 50% of input rate per platform docs)
    "gpt-5":                 (0.625, 5.00,  0.3125),
    "gpt-5-mini":            (0.125, 1.00,  0.0625),
}


# Provider-by-provider semantic for `cached_input_tokens`:
#   anthropic: input_tokens is the NON-cached billable; cached_input_tokens
#              is billed separately at the discounted rate.
#   openai / gemini: input_tokens (or prompt_tokens) is the TOTAL; the
#              cached subset gets the discount.
_CACHED_TOKEN_INCLUDED_IN_INPUT: Dict[str, bool] = {
    "anthropic": False,   # already subtracted
    "openai":    True,    # subset of input_tokens
    "vllm":      True,    # OpenAI-compat
    "gemini":    True,    # subset of input_tokens
}


def _compute_cost(model: str,
                  input_tokens: Optional[int],
                  output_tokens: Optional[int],
                  cached_input_tokens: Optional[int] = None,
                  provider: Optional[str] = None) -> Optional[float]:
    """Return USD cost or None if the model isn't priced or tokens missing.

    When `cached_input_tokens` is populated AND the model has a
    cached_input_rate, the cached subset is billed at the discounted rate.
    Whether that subset is subtracted from `input_tokens` first depends on
    the provider's reporting convention (see _CACHED_TOKEN_INCLUDED_IN_INPUT).
    """
    rates = _PRICING.get(model)
    if rates is None or input_tokens is None or output_tokens is None:
        return None
    in_rate, out_rate, cached_rate = rates

    if (cached_input_tokens
            and cached_rate is not None
            and provider is not None):
        if _CACHED_TOKEN_INCLUDED_IN_INPUT.get(provider, True):
            # Subset semantic — subtract first to avoid double-charging.
            non_cached = max(0, input_tokens - cached_input_tokens)
            return ((in_rate * non_cached
                       + cached_rate * cached_input_tokens
                       + out_rate * output_tokens)
                      / 1_000_000.0)
        # Disjoint semantic (Anthropic) — input_tokens already excludes cached.
        return ((in_rate * input_tokens
                   + cached_rate * cached_input_tokens
                   + out_rate * output_tokens)
                  / 1_000_000.0)

    return (in_rate * input_tokens + out_rate * output_tokens) / 1_000_000.0


# ───────────────────────────── tenacity retry ────────────────────────────────
#
# Provider SDKs each have their own transient-error class hierarchy. We
# import them lazily inside `_retryable_excs_for(provider)` so that
# tenacity setup doesn't require every SDK to be installed.

def _retryable_excs_for(provider: str) -> Tuple[type, ...]:
    """Provider-specific transient exception classes that tenacity should
    retry on. Non-listed exceptions propagate immediately — including
    BadRequestError (400-class), AuthenticationError (401), and our own
    BudgetExceededError, none of which retries would help.

    Anthropic note: 529 OverloadedError is the most common Claude
    transient in 2026 and is NOT a subclass of InternalServerError
    (500). The Bucket-2 audit (Critic 2) flagged that without explicit
    inclusion, 529s burn the call on the first attempt. We include
    OverloadedError directly.

    Gemini note: `google.genai.errors.ClientError` is the supertype
    for 4xx — including 401/403/400 which should NOT retry. We do
    NOT include ClientError here; transient throttling (429) is caught
    by the predicate in `_is_retryable_status` and an extra wrapper."""
    excs: List[type] = []
    # Always include asyncio's TimeoutError and the stdlib ConnectionError.
    excs.append(asyncio.TimeoutError)
    excs.append(ConnectionError)
    if provider == "anthropic":
        try:
            import anthropic
            excs.extend([
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
            ])
            # 529 Overloaded — its own subclass of APIStatusError, not
            # exported at the top level of the anthropic package. Walk
            # APIStatusError subclasses to find it (and ServiceUnavailable
            # / DeadlineExceeded which are the other transient 5xx kin).
            for sub in anthropic.APIStatusError.__subclasses__():
                if sub.__name__ in ("OverloadedError",
                                      "ServiceUnavailableError",
                                      "DeadlineExceededError"):
                    excs.append(sub)
        except ImportError:
            pass
    elif provider in ("openai", "vllm"):
        try:
            import openai
            excs.extend([
                openai.APIConnectionError,
                openai.APITimeoutError,
                openai.RateLimitError,
                openai.InternalServerError,
            ])
        except ImportError:
            pass
    elif provider == "gemini":
        # ServerError (5xx) is unconditionally retryable.
        # ClientError (4xx) is ONLY retryable on 429 — handled by the
        # predicate in `_is_gemini_retryable_clienterror`.
        try:
            from google.genai import errors as gerrors
            excs.append(gerrors.ServerError)
            # ClientError handled by predicate; do NOT blanket-retry.
        except ImportError:
            pass
    return tuple(excs)


def _is_gemini_retryable_clienterror(exc: BaseException) -> bool:
    """Return True iff `exc` is a google-genai ClientError representing a
    transient condition we should retry (429 throttling, 408 timeout).
    All other 4xx (400 BadRequest, 401 Unauthorized, 403 Forbidden,
    404 NotFound) MUST propagate to surface the real problem."""
    try:
        from google.genai import errors as gerrors
    except ImportError:
        return False
    if not isinstance(exc, gerrors.ClientError):
        return False
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code in (408, 429)


# Tenacity wall-clock cap. Even with all retries succeeding eventually,
# we cap a single chat() call at this many seconds total. Without this,
# `wait_exponential_jitter(max=30) * 7 + per_attempt_timeout * 7` can
# reach ~6.5 minutes per call (Bucket-2 Critic 2). 120s leaves room for
# 2-3 transient retries on a normal-sized prompt.
_TENACITY_WALL_CLOCK_S = 120.0


async def _run_with_retry(provider: str, max_retries: int, coro_factory):
    """Run `coro_factory()` with tenacity-style exponential backoff.

    `coro_factory` is a 0-arg callable that returns the coroutine to
    await; we re-invoke it per attempt because awaiting the same
    coroutine twice raises.

    `max_retries=0` disables retry (we still call coro_factory() once).
    """
    if max_retries <= 0:
        return await coro_factory()
    try:
        from tenacity import (AsyncRetrying, stop_after_attempt,
                                stop_after_delay,
                                wait_exponential_jitter,
                                retry_if_exception_type,
                                retry_if_exception)
    except ImportError:
        # Soft-fall to a single attempt if tenacity is unavailable —
        # production environments must install it.
        return await coro_factory()

    retryable = _retryable_excs_for(provider)
    type_predicate = retry_if_exception_type(retryable)

    # Gemini gets an extra predicate that retries SOME ClientError 4xx
    # (429 throttle, 408 timeout) without blanket-retrying auth/badreq.
    if provider == "gemini":
        retry_predicate = (type_predicate
                            | retry_if_exception(
                                _is_gemini_retryable_clienterror))
    else:
        retry_predicate = type_predicate

    retryer = AsyncRetrying(
        stop=(stop_after_attempt(max_retries + 1)
               | stop_after_delay(_TENACITY_WALL_CLOCK_S)),
        wait=wait_exponential_jitter(initial=1.0, max=30.0),
        retry=retry_predicate,
        reraise=True,
    )
    async for attempt in retryer:
        with attempt:
            return await coro_factory()
    # Defensive: AsyncRetrying with reraise=True won't fall through.
    raise RuntimeError("tenacity AsyncRetrying exited without result")


# ───────────────────────────── tool-call translators ─────────────────────────
#
# Each provider has a slightly different tool-calling wire format. The
# canonical input is MCP-shape: {name, description, inputSchema}.
# These translators sit just above the provider SDK and stay narrow —
# anything beyond the conversion lives in the chat() methods.

def _tools_to_anthropic(tools: List[Dict[str, Any]]
                         ) -> List[Dict[str, Any]]:
    """MCP-shape → Anthropic. Anthropic uses `input_schema` (snake_case)
    and accepts an extra `cache_control: {type: 'ephemeral'}` per tool;
    we don't set that here (caller injects via `system_cache`/betas)."""
    out: List[Dict[str, Any]] = []
    for t in tools:
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t["inputSchema"],
        })
    return out


def _make_openai_strict_schema(schema: Any) -> Any:
    """Rewrite a JSON-Schema-Draft-2020-12 schema into the subset
    OpenAI strict-mode FC accepts.

    Strict-mode rules (OpenAI docs):
      - Every object MUST have `additionalProperties: false`.
      - For every object, `required` MUST equal `properties.keys()`.
        Optional fields must be expressed by making the field nullable,
        i.e. `"type": ["string", "null"]` instead of omitting from required.
      - `type` MUST be present on every leaf.

    Our hand-authored schemas in sibb_api_tools.py have many optional
    fields and use the natural pattern of listing only required ones.
    Without this rewriter, OpenAI 400s on every tool. The rewriter
    walks the schema and:
      1. Adds `additionalProperties: false` to every object.
      2. Promotes every property to `required`.
      3. Makes originally-optional properties nullable
         (`type` becomes `[T, "null"]`).
    """
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            out[k] = _make_openai_strict_schema(v)
        elif isinstance(v, list):
            out[k] = [_make_openai_strict_schema(it) for it in v]
        else:
            out[k] = v

    if out.get("type") == "object" and "properties" in out:
        props = out["properties"]
        original_required = set(out.get("required", []))
        all_keys = list(props.keys())
        out["required"] = all_keys
        out["additionalProperties"] = False
        # Make originally-optional properties nullable.
        for k, prop in props.items():
            if k in original_required:
                continue
            if not isinstance(prop, dict):
                continue
            t = prop.get("type")
            if t is None:
                # Already polymorphic / no type — leave alone.
                continue
            if isinstance(t, str):
                prop["type"] = [t, "null"]
            elif isinstance(t, list) and "null" not in t:
                prop["type"] = list(t) + ["null"]
    return out


def _tools_to_openai(tools: List[Dict[str, Any]],
                     strict: bool = True
                     ) -> List[Dict[str, Any]]:
    """MCP-shape → OpenAI ChatCompletions. OpenAI nests under
    `function: {name, description, parameters}` and accepts `strict:
    true` at the function level for schema-conformant output.

    When `strict=True`, the schema is rewritten via
    `_make_openai_strict_schema` so optional fields become nullable
    and every object is closed."""
    out: List[Dict[str, Any]] = []
    for t in tools:
        params = (_make_openai_strict_schema(t["inputSchema"])
                   if strict else t["inputSchema"])
        fn = {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": params,
        }
        if strict:
            fn["strict"] = True
        out.append({"type": "function", "function": fn})
    return out


def _sanitize_gemini_schema(node: Any) -> Any:
    """Recursively strip JSON-Schema fields Gemini's parameters parser
    rejects.

    Gemini accepts an OpenAPI 3.0 subset and actively 400s on fields it
    doesn't recognize — empirically including `additionalProperties`
    (which it sees as `additional_properties` after camelCase
    normalization), `additional_properties` itself, `$schema`, and
    `$ref`. Strip them from every nested object before sending.

    We keep all the fields Gemini DOES accept: type, properties,
    required, description, default, enum, items, minimum, maximum,
    nullable. Schemas authored against our existing tools use only
    these, plus the rejected `additionalProperties` — so a recursive
    pop is sufficient."""
    _REJECTED = ("additionalProperties", "additional_properties",
                  "$schema", "$ref", "$id", "$defs", "definitions",
                  "oneOf", "anyOf", "allOf", "not", "const")
    if isinstance(node, dict):
        return {k: _sanitize_gemini_schema(v) for k, v in node.items()
                if k not in _REJECTED}
    if isinstance(node, list):
        return [_sanitize_gemini_schema(v) for v in node]
    return node


def _tools_to_gemini(tools: List[Dict[str, Any]]
                      ) -> List[Dict[str, Any]]:
    """MCP-shape → Gemini. Gemini groups under
    `tools=[{function_declarations: [...]}]` with `parameters` for the
    schema.

    The MCP schema is JSON-Schema-Draft-2020-12; Gemini accepts an
    OpenAPI 3.0 subset. The sanitizer drops fields Gemini rejects
    (`additionalProperties`, `$ref`, oneOf/anyOf, etc.). Strict-mode
    behavior is NOT honored at parse time — the dispatcher re-validates
    upstream."""
    decls: List[Dict[str, Any]] = []
    for t in tools:
        decls.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": _sanitize_gemini_schema(t["inputSchema"]),
        })
    return [{"function_declarations": decls}]


# ───────────────────────────── base ──────────────────────────────────────────


class LLMClient:
    """Per-call interface. Subclasses implement `chat`.

    Operational state on the instance:
      - `spent_usd`: USD accumulated across every successful chat() call
        whose `LLMResponse.cost_usd` was populated. Subtract from
        `budget_usd_max` for budget gating.
      - `budget_usd_max`: optional cap; when set, chat() raises
        `BudgetExceededError` BEFORE making the call if spend would
        exceed it. None disables budget gating.
    """

    provider: str = "unknown"

    def __init__(self, *, model: str, api_key: Optional[str] = None,
                 timeout: float = 60.0, max_retries: int = 6,
                 budget_usd_max: Optional[float] = None):
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.budget_usd_max = budget_usd_max
        self.spent_usd: float = 0.0

    def _gate_budget(self) -> None:
        if (self.budget_usd_max is not None
                and self.spent_usd >= self.budget_usd_max):
            raise BudgetExceededError(
                f"budget exceeded: spent ${self.spent_usd:.4f} "
                f">= cap ${self.budget_usd_max:.4f}")

    def _record_cost(self, resp: LLMResponse) -> None:
        if resp.cost_usd is not None:
            self.spent_usd += resp.cost_usd

    async def chat(self, messages: List[Dict[str, Any]], *,
                   system: str = "",
                   max_tokens: int = 1024,
                   temperature: Optional[float] = None,
                   tools: Optional[List[Dict[str, Any]]] = None,
                   tool_choice: Optional[Union[str, Dict[str, Any]]] = None,
                   betas: Optional[List[str]] = None,
                   system_cache: bool = False,
                   logprobs: bool = False,
                   top_logprobs: Optional[int] = None,
                   prompt_logprobs: Optional[int] = None,
                   parallel_tool_calls: bool = False
                   ) -> LLMResponse:
        raise NotImplementedError

    # --- Tool-result threading helpers ----------------------------------
    #
    # Each provider wants tool_use → tool_result threaded back into the
    # next turn's messages in its own shape. The agent loop calls
    # `append_assistant_with_tool_calls(...)` after a model turn that
    # contained tool_use blocks, then `append_tool_result(...)` per
    # tool the dispatcher executed. Both return a NEW message list so
    # the loop never has to know the wire shape.

    def append_assistant_with_tool_calls(
        self,
        messages: List[Dict[str, Any]],
        *,
        text: str,
        tool_calls: List["ToolCall"],
    ) -> List[Dict[str, Any]]:
        """Append the assistant turn (text + tool_use blocks) so the
        provider sees the same content it just emitted on the NEXT
        turn. Without this round-tripping, providers reject the
        tool_result message that follows."""
        raise NotImplementedError

    def append_tool_result(
        self,
        messages: List[Dict[str, Any]],
        *,
        tool_call_id: str,
        tool_name: str,
        result: Any,
        is_error: bool = False,
    ) -> List[Dict[str, Any]]:
        """Append a tool_result for one dispatched tool. `result` is
        JSON-serializable; the wrapper str()ifies on a per-provider
        basis as needed."""
        raise NotImplementedError


class BudgetExceededError(RuntimeError):
    """Raised by chat() when client.spent_usd >= client.budget_usd_max."""


# ───────────────────────────── anthropic ─────────────────────────────────────


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, *, model: str = "claude-haiku-4-5",
                 api_key: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 6,
                 budget_usd_max: Optional[float] = None):
        super().__init__(model=model, api_key=api_key, timeout=timeout,
                         max_retries=max_retries,
                         budget_usd_max=budget_usd_max)
        try:
            import anthropic  # noqa
        except ImportError as e:
            raise ImportError(
                "AnthropicClient requires `anthropic`. "
                "Install: pip install --user anthropic"
            ) from e
        self._anthropic = anthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "AnthropicClient: ANTHROPIC_API_KEY not set"
            )
        # CRITICAL: SDK-level retries OFF. tenacity wraps chat() so SDK
        # + tenacity wouldn't stack multiplicatively (49-attempt blowup).
        self._client = anthropic.AsyncAnthropic(
            api_key=key, timeout=timeout, max_retries=0)

    async def chat(self, messages, *, system="", max_tokens=1024,
                   temperature=None, tools=None, tool_choice=None,
                   betas=None, system_cache=False,
                   logprobs=False, top_logprobs=None, prompt_logprobs=None,
                   parallel_tool_calls=False):
        self._gate_budget()

        # System content: string or, if caching requested, structured.
        sys_param: Any = system
        if system_cache and system:
            sys_param = [{
                "type": "text", "text": system,
                "cache_control": {"type": "ephemeral"},
            }]

        kwargs: Dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=sys_param,
            messages=messages,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools is not None:
            kwargs["tools"] = _tools_to_anthropic(tools)
            if tool_choice is not None:
                kwargs["tool_choice"] = _normalize_tool_choice_anthropic(
                    tool_choice)
            if not parallel_tool_calls:
                # Anthropic API: disable_parallel_tool_use lives at top
                # level on tool_choice.
                tc = kwargs.get("tool_choice") or {"type": "auto"}
                if isinstance(tc, dict):
                    tc["disable_parallel_tool_use"] = True
                    kwargs["tool_choice"] = tc
        # Anthropic logprobs: not exposed via public API as of 2026-01.
        # We silently ignore the logprobs flags rather than raising.

        async def _call():
            request = self._client.messages
            if betas:
                # `with_raw_response`-free betas path: the official SDK
                # exposes `messages.create` accepting `extra_headers`.
                return await request.create(
                    extra_headers={
                        "anthropic-beta": ",".join(betas)},
                    **kwargs)
            return await request.create(**kwargs)

        resp = await _run_with_retry(
            self.provider, self.max_retries, _call)

        # Parse content blocks: text, tool_use, and thinking are all possible.
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        thinking: List[Dict[str, Any]] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))
            elif btype == "thinking":
                # Extended-thinking content block — visible reasoning.
                thinking.append({
                    "kind": "text",
                    "text": getattr(block, "thinking", "") or "",
                })
            elif btype == "redacted_thinking":
                # Opaque trace that Anthropic redacted server-side.
                thinking.append({
                    "kind": "redacted",
                    "data": getattr(block, "data", ""),
                })
        text = "".join(text_parts)

        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", None) if usage else None
        out_tok = getattr(usage, "output_tokens", None) if usage else None
        # Anthropic surfaces cache hits separately; capture for accounting.
        cached_in = (getattr(usage, "cache_read_input_tokens", None)
                      if usage else None)
        cost = _compute_cost(self.model, in_tok, out_tok,
                              cached_input_tokens=cached_in,
                              provider=self.provider)

        out = LLMResponse(
            text=text, provider=self.provider, model=self.model,
            input_tokens=in_tok, output_tokens=out_tok,
            cached_input_tokens=cached_in,
            stop_reason=getattr(resp, "stop_reason", None),
            tool_calls=tool_calls,
            cost_usd=cost,
            thinking=thinking,
            raw=resp,
        )
        self._record_cost(out)
        return out

    def append_assistant_with_tool_calls(self, messages, *, text,
                                            tool_calls):
        """Anthropic: assistant content is a list of blocks. Append one
        message with optional text block + one tool_use block per call."""
        blocks: List[Dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in tool_calls:
            blocks.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            })
        return list(messages) + [{"role": "assistant", "content": blocks}]

    def append_tool_result(self, messages, *, tool_call_id, tool_name,
                            result, is_error=False):
        """Anthropic: tool_result is a content block on a user message.
        Multiple tool_results can be batched in one user message, but the
        SIBB agent loop runs one tool per turn so we always append a
        fresh user message."""
        content_str = (json.dumps(result, default=str)
                       if not isinstance(result, str) else result)
        return list(messages) + [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": content_str,
                "is_error": is_error,
            }],
        }]


def _normalize_tool_choice_anthropic(
    tc: Union[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Convert simple strings ('auto', 'any', 'none', '<tool>') to
    Anthropic's structured tool_choice dict shape."""
    if isinstance(tc, dict):
        return tc
    if tc in ("auto", "any", "none"):
        return {"type": tc}
    # Otherwise assume a tool name.
    return {"type": "tool", "name": tc}


# ───────────────────────────── gemini ────────────────────────────────────────


class GeminiClient(LLMClient):
    """Google Gemini via the modern `google-genai` SDK.

    Install: pip install --user google-genai
    API key env var: GEMINI_API_KEY (falls back to GOOGLE_API_KEY).
    """

    provider = "gemini"

    def __init__(self, *, model: str = "gemini-2.5-flash",
                 api_key: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 6,
                 budget_usd_max: Optional[float] = None):
        # google-genai SDK doesn't expose a max_retries knob; tenacity
        # handles retries entirely at this level.
        super().__init__(model=model, api_key=api_key, timeout=timeout,
                         max_retries=max_retries,
                         budget_usd_max=budget_usd_max)
        try:
            from google import genai  # noqa
        except ImportError as e:
            raise ImportError(
                "GeminiClient requires `google-genai`. "
                "Install: pip install --user google-genai"
            ) from e
        self._genai = genai
        key = (api_key
               or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
        if not key:
            raise RuntimeError(
                "GeminiClient: GEMINI_API_KEY (or GOOGLE_API_KEY) not set"
            )
        self._client = genai.Client(api_key=key)

    async def chat(self, messages, *, system="", max_tokens=1024,
                   temperature=None, tools=None, tool_choice=None,
                   betas=None, system_cache=False,
                   logprobs=False, top_logprobs=None, prompt_logprobs=None,
                   parallel_tool_calls=False):
        self._gate_budget()

        # Convert messages to Gemini Content list. assistant→model.
        # Tool-call legs (function_call / function_response parts) are
        # already in Gemini shape if the caller threaded them through;
        # plain-text content is wrapped under parts:[{text: ...}].
        contents: List[Dict[str, Any]] = []
        for m in messages:
            role = "model" if m.get("role") == "assistant" else "user"
            content = m.get("content")
            if isinstance(content, list):
                # Caller has already shaped parts.
                contents.append({"role": role, "parts": content})
            else:
                contents.append({"role": role,
                                   "parts": [{"text": str(content)}]})

        config: Dict[str, Any] = {
            "system_instruction": system or None,
            "max_output_tokens": max_tokens,
        }
        if temperature is not None:
            config["temperature"] = temperature
        if tools is not None:
            config["tools"] = _tools_to_gemini(tools)
            if tool_choice is not None:
                config["tool_config"] = _normalize_tool_choice_gemini(
                    tool_choice)
        if logprobs:
            # Gemini exposes per-token logprobs through `response_logprobs`
            # + `logprobs` (number to return). The SDK passes them through.
            config["response_logprobs"] = True
            if top_logprobs is not None:
                config["logprobs"] = top_logprobs
        # Drop None values so the SDK doesn't reject them.
        config = {k: v for k, v in config.items() if v is not None}

        async def _call():
            return await self._client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )

        resp = await _run_with_retry(
            self.provider, self.max_retries, _call)

        # Parse text + function_call + thought_signature parts. SDK
        # exposes `.text` for the concatenated text path, but tool
        # calls and thinking traces require walking
        # `candidates[0].content.parts`.
        text = getattr(resp, "text", "") or ""
        tool_calls: List[ToolCall] = []
        thinking: List[Dict[str, Any]] = []
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            parts = (getattr(getattr(candidates[0], "content", None),
                              "parts", None) or [])
            for idx, p in enumerate(parts):
                fc = getattr(p, "function_call", None)
                if fc is not None:
                    # Gemini doesn't emit a stable per-call id; synthesize
                    # one for tool_result threading.
                    tc_id = f"gemini-{idx}-{getattr(fc, 'name', '?')}"
                    args = getattr(fc, "args", None) or {}
                    if hasattr(args, "to_dict"):
                        args = args.to_dict()
                    elif not isinstance(args, dict):
                        args = dict(args) if args else {}
                    tool_calls.append(ToolCall(
                        id=tc_id,
                        name=getattr(fc, "name", "?"),
                        arguments=args,
                    ))
                    continue
                # Gemini 2.5 emits encrypted thought_signature parts when
                # the model used extended thinking. The trace itself is
                # opaque (Google doesn't expose plaintext reasoning to
                # third parties) but we capture it so the trajectory log
                # records *that* the model reasoned, even if not what.
                ts = getattr(p, "thought_signature", None)
                if ts is not None:
                    thinking.append({
                        "kind": "signature",
                        "signature": (ts if isinstance(ts, str)
                                       else str(ts)),
                    })
                    continue

        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", None) if usage else None
        out_tok = (getattr(usage, "candidates_token_count", None)
                    if usage else None)
        cached_in = (getattr(usage, "cached_content_token_count", None)
                      if usage else None)
        cost = _compute_cost(self.model, in_tok, out_tok,
                              cached_input_tokens=cached_in,
                              provider=self.provider)

        out = LLMResponse(
            text=text, provider=self.provider, model=self.model,
            input_tokens=in_tok, output_tokens=out_tok,
            cached_input_tokens=cached_in,
            stop_reason=None,
            tool_calls=tool_calls,
            cost_usd=cost,
            thinking=thinking,
            raw=resp,
        )
        self._record_cost(out)
        return out

    def append_assistant_with_tool_calls(self, messages, *, text,
                                            tool_calls):
        """Gemini: assistant role is "model"; function_call parts can
        live alongside text parts in a single Content. We emit the
        same wire shape the SDK round-trips."""
        parts: List[Dict[str, Any]] = []
        if text:
            parts.append({"text": text})
        for tc in tool_calls:
            parts.append({
                "function_call": {
                    "name": tc.name,
                    "args": tc.arguments,
                },
            })
        return list(messages) + [{"role": "assistant", "content": parts}]

    def append_tool_result(self, messages, *, tool_call_id, tool_name,
                            result, is_error=False):
        """Gemini: function_response is a part on a user-role Content.
        The `name` MUST match the function_call's name; `tool_call_id`
        is unused on Gemini (Gemini doesn't model per-call ids)."""
        if is_error:
            response_obj: Any = {"error": result}
        elif isinstance(result, dict):
            response_obj = result
        else:
            response_obj = {"result": result}
        return list(messages) + [{
            "role": "user",
            "content": [{
                "function_response": {
                    "name": tool_name,
                    "response": response_obj,
                },
            }],
        }]


def _normalize_tool_choice_gemini(
    tc: Union[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Gemini's tool_config shape:
        {function_calling_config: {mode: AUTO|ANY|NONE, allowed_function_names: [...]}}
    """
    if isinstance(tc, dict):
        return tc
    if tc == "auto":
        return {"function_calling_config": {"mode": "AUTO"}}
    if tc == "any":
        return {"function_calling_config": {"mode": "ANY"}}
    if tc == "none":
        return {"function_calling_config": {"mode": "NONE"}}
    # Specific tool name → ANY with allowed list of 1.
    return {"function_calling_config": {
        "mode": "ANY", "allowed_function_names": [tc]}}


# ───────────────────────────── openai ────────────────────────────────────────


class OpenAIClient(LLMClient):
    """OpenAI via the `openai` SDK.

    Install: pip install --user openai
    API key env var: OPENAI_API_KEY.
    """

    provider = "openai"

    def __init__(self, *, model: str = "gpt-5-mini",
                 api_key: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 6,
                 budget_usd_max: Optional[float] = None,
                 base_url: Optional[str] = None):
        super().__init__(model=model, api_key=api_key, timeout=timeout,
                         max_retries=max_retries,
                         budget_usd_max=budget_usd_max)
        try:
            import openai  # noqa
        except ImportError as e:
            raise ImportError(
                "OpenAIClient requires `openai`. "
                "Install: pip install --user openai"
            ) from e
        self._openai = openai
        key = api_key or os.environ.get("OPENAI_API_KEY") or "EMPTY"
        if not key:
            raise RuntimeError(
                "OpenAIClient: OPENAI_API_KEY not set"
            )
        # Subclasses (VLLMClient) supply base_url; the OpenAI SDK
        # ignores it when None.
        ctor_kwargs: Dict[str, Any] = dict(
            api_key=key, timeout=timeout, max_retries=0)
        if base_url is not None:
            ctor_kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**ctor_kwargs)

    async def chat(self, messages, *, system="", max_tokens=1024,
                   temperature=None, tools=None, tool_choice=None,
                   betas=None, system_cache=False,
                   logprobs=False, top_logprobs=None, prompt_logprobs=None,
                   parallel_tool_calls=False):
        self._gate_budget()

        full = (([{"role": "system", "content": system}] if system else [])
                  + list(messages))
        kwargs: Dict[str, Any] = dict(
            model=self.model,
            messages=full,
            max_tokens=max_tokens,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools is not None:
            kwargs["tools"] = _tools_to_openai(tools, strict=True)
            if tool_choice is not None:
                kwargs["tool_choice"] = _normalize_tool_choice_openai(
                    tool_choice)
            kwargs["parallel_tool_calls"] = parallel_tool_calls
        if logprobs:
            kwargs["logprobs"] = True
            if top_logprobs is not None:
                kwargs["top_logprobs"] = top_logprobs
        # prompt_logprobs is a vLLM extension to the OpenAI API; OpenAI
        # itself rejects unknown fields. Surface it via extra_body so
        # the subclass (VLLMClient) sees it but vanilla OpenAI clients
        # don't see it at all.
        if prompt_logprobs is not None:
            kwargs.setdefault("extra_body", {})["prompt_logprobs"] = (
                prompt_logprobs)

        async def _call():
            return await self._client.chat.completions.create(**kwargs)

        resp = await _run_with_retry(
            self.provider, self.max_retries, _call)

        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""

        # Parse tool_calls from the message.
        tool_calls: List[ToolCall] = []
        raw_tc = getattr(msg, "tool_calls", None) or []
        for tc in raw_tc:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            args_str = getattr(fn, "arguments", "") or "{}"
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                # Strict-mode prevents this; defensive fallback.
                args = {"_raw": args_str}
            tool_calls.append(ToolCall(
                id=getattr(tc, "id", "") or "",
                name=getattr(fn, "name", "") or "",
                arguments=args,
            ))

        # Logprobs (OpenAI native: choices[0].logprobs.content list).
        comp_lps: Optional[List[float]] = None
        lps_obj = getattr(choice, "logprobs", None)
        content_lps = getattr(lps_obj, "content", None) if lps_obj else None
        if content_lps:
            comp_lps = [getattr(item, "logprob", 0.0)
                          for item in content_lps]

        # prompt_logprobs is vLLM-only; the OpenAI server returns nothing.
        # vLLM surfaces them on the top-level resp.prompt_logprobs.
        prompt_lps: Optional[List[float]] = None
        plp_raw = getattr(resp, "prompt_logprobs", None)
        if isinstance(plp_raw, list):
            # vLLM emits a list of per-token dicts {token_id: {logprob, ...}}.
            # Flatten to the chosen-token logprob list for simplicity here;
            # callers needing the full shape read resp.raw.
            flat: List[float] = []
            for entry in plp_raw:
                if entry is None:
                    continue
                if isinstance(entry, dict):
                    # First (only) key is the chosen token id.
                    for v in entry.values():
                        lp = (v.get("logprob") if isinstance(v, dict)
                              else None)
                        if lp is not None:
                            flat.append(lp)
                        break
            if flat:
                prompt_lps = flat

        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None) if usage else None
        out_tok = (getattr(usage, "completion_tokens", None)
                    if usage else None)
        cached_in = None
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        if details is not None:
            cached_in = getattr(details, "cached_tokens", None)
        cost = _compute_cost(self.model, in_tok, out_tok,
                              cached_input_tokens=cached_in,
                              provider=self.provider)

        # OpenAI reasoning models (o1, o3, gpt-5) expose a reasoning
        # summary on `choice.message.reasoning_content` or similar; the
        # field name varies by model. Capture whatever is present.
        thinking: List[Dict[str, Any]] = []
        reasoning_text = getattr(msg, "reasoning_content", None)
        if not reasoning_text:
            reasoning_text = getattr(msg, "reasoning", None)
        if reasoning_text:
            thinking.append({
                "kind": "text",
                "text": (reasoning_text if isinstance(reasoning_text, str)
                          else str(reasoning_text)),
            })

        out = LLMResponse(
            text=text, provider=self.provider, model=self.model,
            input_tokens=in_tok, output_tokens=out_tok,
            cached_input_tokens=cached_in,
            stop_reason=getattr(choice, "finish_reason", None),
            tool_calls=tool_calls,
            cost_usd=cost,
            completion_logprobs=comp_lps,
            prompt_logprobs=prompt_lps,
            thinking=thinking,
            raw=resp,
        )
        self._record_cost(out)
        return out

    def append_assistant_with_tool_calls(self, messages, *, text,
                                            tool_calls):
        """OpenAI: assistant message carries `tool_calls` list as a
        separate field alongside `content`. `arguments` MUST be a JSON
        string (the SDK requires it)."""
        oa_tool_calls = [{
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": json.dumps(tc.arguments, default=str),
            },
        } for tc in tool_calls]
        msg: Dict[str, Any] = {
            "role": "assistant",
            "content": text if text else None,
        }
        if oa_tool_calls:
            msg["tool_calls"] = oa_tool_calls
        return list(messages) + [msg]

    def append_tool_result(self, messages, *, tool_call_id, tool_name,
                            result, is_error=False):
        """OpenAI: tool result lives in a role="tool" message with
        `tool_call_id` linking back to the assistant turn's tool_call.
        `tool_name` is unused at the wire level but kept for API parity."""
        content_str = (json.dumps(result, default=str)
                       if not isinstance(result, str) else result)
        return list(messages) + [{
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content_str,
        }]


def _normalize_tool_choice_openai(
    tc: Union[str, Dict[str, Any]]
) -> Union[str, Dict[str, Any]]:
    """Normalize cross-provider tool_choice strings to OpenAI's vocab.

    Accepts both OpenAI's native vocab (`auto` / `required` / `none`)
    and the Anthropic-style `any` (mapped to `required`). Without
    this mapping a caller passing `"any"` falls through to the
    specific-tool branch and emits {type:"function", function:{name:"any"}}
    which OpenAI 400s on as a missing tool. Bucket-2 Critic 3."""
    if isinstance(tc, dict):
        return tc
    if tc in ("auto", "required", "none"):
        return tc
    if tc == "any":
        # Anthropic semantics for "any" = OpenAI "required" (must call
        # SOME tool, model picks which).
        return "required"
    # Specific tool name → forced.
    return {"type": "function", "function": {"name": tc}}


# ───────────────────────────── vLLM (OpenAI-compatible) ──────────────────────


class VLLMClient(OpenAIClient):
    """vLLM-served local model via OpenAI-compatible Chat Completions API.

    Use when training-time rollouts need `prompt_logprobs` (off-policy
    importance sampling for RL) or when running offline experiments
    against a self-hosted Qwen3 / Llama policy.

    `base_url` defaults to the standard vLLM serve endpoint; override
    when running on a non-default port or remote machine.

    vLLM caveats vs vanilla OpenAI:
      - Pricing is locally computed (training/serving cost, not API).
        We do NOT entry the `_PRICING` table; cost_usd stays None.
      - `strict: true` on tool functions is accepted but NOT enforced
        — use `guided_json` (vLLM's offer) if schema-conformant
        decoding is required. The translator emits strict=True
        regardless so the wire format matches OpenAI's; document the
        asymmetry in the paper Methodology.
    """

    provider = "vllm"

    def __init__(self, *, model: str = "Qwen/Qwen3-8B",
                 api_key: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 6,
                 budget_usd_max: Optional[float] = None,
                 base_url: str = "http://localhost:8000/v1"):
        super().__init__(model=model,
                         api_key=api_key or "EMPTY",
                         timeout=timeout,
                         max_retries=max_retries,
                         budget_usd_max=budget_usd_max,
                         base_url=base_url)


# ───────────────────────────── factory ───────────────────────────────────────


_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "gemini":    "gemini-2.5-flash",
    "openai":    "gpt-5-mini",
    "vllm":      "Qwen/Qwen3-8B",
}

_PROVIDER_CLASSES = {
    "anthropic": AnthropicClient,
    "gemini":    GeminiClient,
    "openai":    OpenAIClient,
    "vllm":      VLLMClient,
}


def make_client(provider: str, *, model: Optional[str] = None,
                api_key: Optional[str] = None,
                timeout: float = 60.0,
                max_retries: int = 6,
                budget_usd_max: Optional[float] = None,
                base_url: Optional[str] = None) -> LLMClient:
    """Build a client for `provider`. Uses the provider's default model
    if `model` is None.

    `base_url` is accepted for `vllm` / `openai`; ignored for the others.
    """
    provider = provider.lower()
    if provider not in _PROVIDER_CLASSES:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Choose from {sorted(_PROVIDER_CLASSES)}."
        )
    cls = _PROVIDER_CLASSES[provider]
    kwargs: Dict[str, Any] = dict(
        model=model or _DEFAULT_MODELS[provider],
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
        budget_usd_max=budget_usd_max,
    )
    if base_url is not None and provider in ("vllm", "openai"):
        kwargs["base_url"] = base_url
    return cls(**kwargs)


def available_providers() -> List[str]:
    return sorted(_PROVIDER_CLASSES.keys())


def default_model(provider: str) -> str:
    return _DEFAULT_MODELS[provider.lower()]
