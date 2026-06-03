"""SIBB LLM client — uniform async chat across providers.

Currently supports:
  - anthropic   (claude-haiku-4-5, claude-sonnet-4-6, claude-opus-4-7)
  - gemini      (gemini-2.5-flash, gemini-2.5-pro)
  - openai      (gpt-5, gpt-5-mini, ...)

Wire-format used by the driver (provider-agnostic):

    messages = [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."},
        ...
    ]
    text = await client.chat(messages, system="...", max_tokens=1024)

Provider SDKs are imported lazily so a missing SDK doesn't break startup
for unrelated providers. API keys are read from environment variables
(`ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `GOOGLE_API_KEY`,
`OPENAI_API_KEY`) unless passed explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ───────────────────────────── result wrapper ────────────────────────────────

@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    stop_reason: Optional[str] = None
    raw: Any = field(default=None, repr=False)


# ───────────────────────────── base ──────────────────────────────────────────

class LLMClient:
    """Per-call interface. Subclasses implement `chat`."""

    provider: str = "unknown"

    def __init__(self, *, model: str, api_key: Optional[str] = None,
                 timeout: float = 60.0, max_retries: int = 6):
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries

    async def chat(self, messages: List[Dict[str, str]], *,
                   system: str = "",
                   max_tokens: int = 1024,
                   temperature: Optional[float] = None) -> LLMResponse:
        raise NotImplementedError


# ───────────────────────────── anthropic ─────────────────────────────────────

class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, *, model: str = "claude-haiku-4-5",
                 api_key: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 6):
        super().__init__(model=model, api_key=api_key, timeout=timeout,
                         max_retries=max_retries)
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
        # SDK retries on 408/409/429/5xx (including 529 overloaded) with
        # exponential backoff. Default is 2 — raise to handle sustained
        # overload windows that briefly affect a region.
        self._client = anthropic.AsyncAnthropic(
            api_key=key, timeout=timeout, max_retries=max_retries)

    async def chat(self, messages, *, system="", max_tokens=1024,
                   temperature=None):
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = await self._client.messages.create(**kwargs)
        # Concatenate any text blocks (model may emit multiple).
        text_parts = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        text = "".join(text_parts)
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            stop_reason=getattr(resp, "stop_reason", None),
            raw=resp,
        )


# ───────────────────────────── gemini ────────────────────────────────────────

class GeminiClient(LLMClient):
    """Google Gemini via the modern `google-genai` SDK.

    Install: pip install --user google-genai
    API key env var: GEMINI_API_KEY (falls back to GOOGLE_API_KEY).
    """

    provider = "gemini"

    def __init__(self, *, model: str = "gemini-2.5-flash",
                 api_key: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 6):
        # google-genai SDK doesn't expose a max_retries knob on the
        # client; keep the attribute so the interface matches and we
        # can wrap chat() with manual retries later if needed.
        super().__init__(model=model, api_key=api_key, timeout=timeout,
                         max_retries=max_retries)
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
                   temperature=None):
        # Gemini's content schema: list of Content{role, parts:[{text}]}.
        # OpenAI-style 'assistant' maps to Gemini 'model'.
        contents = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})

        config = {
            "system_instruction": system or None,
            "max_output_tokens": max_tokens,
        }
        if temperature is not None:
            config["temperature"] = temperature
        # Drop None values so the SDK doesn't reject them.
        config = {k: v for k, v in config.items() if v is not None}

        resp = await self._client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        text = getattr(resp, "text", "") or ""
        usage = getattr(resp, "usage_metadata", None)
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            input_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            output_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
            stop_reason=None,
            raw=resp,
        )


# ───────────────────────────── openai ────────────────────────────────────────

class OpenAIClient(LLMClient):
    """OpenAI via the `openai` SDK.

    Install: pip install --user openai
    API key env var: OPENAI_API_KEY.
    """

    provider = "openai"

    def __init__(self, *, model: str = "gpt-5-mini",
                 api_key: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 6):
        super().__init__(model=model, api_key=api_key, timeout=timeout,
                         max_retries=max_retries)
        try:
            import openai  # noqa
        except ImportError as e:
            raise ImportError(
                "OpenAIClient requires `openai`. "
                "Install: pip install --user openai"
            ) from e
        self._openai = openai
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "OpenAIClient: OPENAI_API_KEY not set"
            )
        # OpenAI SDK retries on 408/429/500 with exponential backoff.
        # Default is 2 — match the Anthropic ceiling.
        self._client = openai.AsyncOpenAI(
            api_key=key, timeout=timeout, max_retries=max_retries)

    async def chat(self, messages, *, system="", max_tokens=1024,
                   temperature=None):
        full = ([{"role": "system", "content": system}] if system else []) + list(messages)
        kwargs = dict(
            model=self.model,
            messages=full,
            max_tokens=max_tokens,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            stop_reason=getattr(choice, "finish_reason", None),
            raw=resp,
        )


# ───────────────────────────── factory ───────────────────────────────────────

_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "gemini":    "gemini-2.5-flash",
    "openai":    "gpt-5-mini",
}

_PROVIDER_CLASSES = {
    "anthropic": AnthropicClient,
    "gemini":    GeminiClient,
    "openai":    OpenAIClient,
}


def make_client(provider: str, *, model: Optional[str] = None,
                api_key: Optional[str] = None,
                timeout: float = 60.0,
                max_retries: int = 6) -> LLMClient:
    """Build a client for `provider`. Uses the provider's default model
    if `model` is None."""
    provider = provider.lower()
    if provider not in _PROVIDER_CLASSES:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            f"Choose from {sorted(_PROVIDER_CLASSES)}."
        )
    cls = _PROVIDER_CLASSES[provider]
    return cls(model=model or _DEFAULT_MODELS[provider],
               api_key=api_key,
               timeout=timeout,
               max_retries=max_retries)


def available_providers() -> List[str]:
    return sorted(_PROVIDER_CLASSES.keys())


def default_model(provider: str) -> str:
    return _DEFAULT_MODELS[provider.lower()]
