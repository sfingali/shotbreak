"""LLM provider abstraction.

Speaks two wire protocols:
  - Anthropic native Messages API (POST /v1/messages)
  - OpenAI-compatible chat completions (POST /chat/completions) — used for
    OpenAI, DeepSeek, local (llama.cpp/vLLM/Ollama-compatible) servers, etc.

Provider selection and credentials come from config.yaml (core/config.py does
the ${VAR} interpolation). The Anthropic provider specifically reads
ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL from the environment via that
interpolation, per DESIGN.md §2.5.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from shotbreak.core.config import get_provider_config

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


class LLMError(RuntimeError):
    """Raised when a provider call fails (bad config, HTTP error, refusal)."""


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    latency_ms: int
    raw: dict = field(default_factory=dict)


def complete(
    config: dict,
    provider_name: str,
    system: str,
    user_content: str,
    *,
    max_tokens: int = 4096,
    json_schema: dict | None = None,
    timeout_seconds: float = 120.0,
) -> LLMResponse:
    """Call the configured provider and return a standardized LLMResponse.

    If json_schema is given, the response is constrained to match it —
    Anthropic via output_config.format, OpenAI-compatible via response_format.
    """
    provider_cfg = get_provider_config(config, provider_name)
    kind = provider_cfg.get("kind", "openai_compatible")

    if kind == "anthropic":
        return _complete_anthropic(
            provider_cfg, system, user_content, max_tokens, json_schema, timeout_seconds
        )
    return _complete_openai_compatible(
        provider_cfg, system, user_content, max_tokens, json_schema, timeout_seconds
    )


def _complete_anthropic(
    provider_cfg: dict,
    system: str,
    user_content: str,
    max_tokens: int,
    json_schema: dict | None,
    timeout_seconds: float,
) -> LLMResponse:
    api_key = provider_cfg.get("api_key") or ""
    if not api_key:
        raise LLMError(
            "Anthropic provider has no API key. Set ANTHROPIC_API_KEY in the environment."
        )
    base_url = provider_cfg.get("base_url") or DEFAULT_ANTHROPIC_BASE_URL
    model = provider_cfg["model"]

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    if json_schema is not None:
        body["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    start = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(f"{base_url.rstrip('/')}/v1/messages", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise LLMError(f"Anthropic request failed: {exc}") from exc
    latency_ms = int((time.monotonic() - start) * 1000)

    if resp.status_code != 200:
        raise LLMError(f"Anthropic API error {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    if data.get("stop_reason") == "refusal":
        raise LLMError(f"Anthropic refused the request: {data.get('stop_details')}")

    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    usage = data.get("usage", {}) or {}

    return LLMResponse(
        text=text,
        input_tokens=usage.get("input_tokens", 0) or 0,
        output_tokens=usage.get("output_tokens", 0) or 0,
        provider="anthropic",
        model=data.get("model", model),
        latency_ms=latency_ms,
        raw=data,
    )


def _complete_openai_compatible(
    provider_cfg: dict,
    system: str,
    user_content: str,
    max_tokens: int,
    json_schema: dict | None,
    timeout_seconds: float,
) -> LLMResponse:
    api_key = provider_cfg.get("api_key") or ""
    base_url = (provider_cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise LLMError("OpenAI-compatible provider has no base_url configured.")
    model = provider_cfg["model"]

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": json_schema, "strict": True},
        }

    headers = {"content-type": "application/json"}
    if api_key and api_key != "not-needed":
        headers["Authorization"] = f"Bearer {api_key}"

    start = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(f"{base_url}/chat/completions", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise LLMError(f"{model} request failed: {exc}") from exc
    latency_ms = int((time.monotonic() - start) * 1000)

    if resp.status_code != 200:
        raise LLMError(f"{model} API error {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    choice = data["choices"][0]
    text = choice["message"]["content"] or ""
    usage = data.get("usage", {}) or {}

    return LLMResponse(
        text=text,
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
        provider=provider_cfg.get("kind", "openai_compatible"),
        model=data.get("model", model),
        latency_ms=latency_ms,
        raw=data,
    )
