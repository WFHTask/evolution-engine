"""Unified LLM call layer.

Supports two backends selected by ``ModelSpec.api_base_url``:

* **Anthropic native** (default, ``api_base_url`` is None):
  Uses the ``anthropic`` SDK directly.  Works with any valid Anthropic API key.

* **OpenAI-compatible** (``api_base_url`` is set):
  Uses the ``openai`` SDK with a custom ``base_url``.  Works with OpenAI,
  DeepSeek, Groq, Together AI, Ollama, local vLLM, or any other provider
  that exposes an OpenAI-style ``/v1/chat/completions`` endpoint.

Example ``evolution.yml`` snippets
------------------------------------
Anthropic (default)::

    models:
      actor: "claude-sonnet-4"
      judge: "claude-opus-4"

OpenAI::

    models:
      actor:
        name: "gpt-4o"
        api_base_url: "https://api.openai.com/v1"
        api_key_env: "OPENAI_API_KEY"
      judge:
        name: "gpt-4o"
        api_base_url: "https://api.openai.com/v1"
        api_key_env: "OPENAI_API_KEY"

Local Ollama::

    models:
      actor:
        name: "llama3.1:8b"
        api_base_url: "http://localhost:11434/v1"
        api_key_env: "OLLAMA_API_KEY"   # set to any non-empty string
      judge:
        name: "llama3.1:70b"
        api_base_url: "http://localhost:11434/v1"
        api_key_env: "OLLAMA_API_KEY"

DeepSeek / OpenRouter / etc.::

    models:
      actor:
        name: "deepseek/deepseek-chat"
        api_base_url: "https://openrouter.ai/api/v1"
        api_key_env: "OPENROUTER_API_KEY"
      judge:
        name: "anthropic/claude-opus-4"
        api_base_url: "https://openrouter.ai/api/v1"
        api_key_env: "OPENROUTER_API_KEY"
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from config_loader import resolve_env_path

if TYPE_CHECKING:
    from config_loader import ModelSpec

logger = logging.getLogger("evolution.llm")


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int


def _resolve(v: Optional[str]) -> Optional[str]:
    """Expand ``env:VAR_NAME`` references at call time; pass plain strings through."""
    if v is None:
        return None
    return resolve_env_path(v)


def call_llm(
    model_spec: "ModelSpec",
    system: str,
    user: str,
    max_tokens: int = 1800,
) -> LLMResponse:
    """Call the LLM described by *model_spec* and return the text response.

    ``model_spec.name`` and ``model_spec.api_base_url`` may use ``env:VAR_NAME``
    syntax — they are resolved here at call time, so the YAML can stay secret-free.

    Each invocation creates a **fresh client instance** — no shared
    conversation state between Actor and Judge calls.
    """
    api_key = os.getenv(model_spec.api_key_env)
    if not api_key:
        raise RuntimeError(f"{model_spec.api_key_env} is not set")

    model_name = _resolve(model_spec.name) or model_spec.name
    base_url = _resolve(model_spec.api_base_url)

    if base_url:
        return _call_openai_compatible(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            system=system,
            user=user,
            max_tokens=max_tokens,
        )
    else:
        return _call_anthropic(
            model=model_name,
            api_key=api_key,
            system=system,
            user=user,
            max_tokens=max_tokens,
        )


def _call_anthropic(
    model: str,
    api_key: str,
    system: str,
    user: str,
    max_tokens: int,
) -> LLMResponse:
    from anthropic import Anthropic  # type: ignore[import]

    logger.info("LLM [anthropic] model=%s (api_key=...%s)", model, api_key[-6:])
    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(block.text for block in msg.content if hasattr(block, "text"))
    in_tok = getattr(msg.usage, "input_tokens", 0)
    out_tok = getattr(msg.usage, "output_tokens", 0)
    logger.info("LLM [anthropic] response length=%d chars, tokens in=%d out=%d", len(text), in_tok, out_tok)
    return LLMResponse(text=text, input_tokens=in_tok, output_tokens=out_tok)


def _call_openai_compatible(
    model: str,
    api_key: str,
    base_url: str,
    system: str,
    user: str,
    max_tokens: int,
) -> LLMResponse:
    from openai import OpenAI  # type: ignore[import]

    logger.info("LLM [openai-compat] model=%s base_url=%s (api_key=...%s)", model, base_url, api_key[-6:])
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    text = resp.choices[0].message.content or ""
    in_tok = resp.usage.prompt_tokens if resp.usage else 0
    out_tok = resp.usage.completion_tokens if resp.usage else 0
    logger.info("LLM [openai-compat] response length=%d chars, tokens in=%d out=%d", len(text), in_tok, out_tok)
    return LLMResponse(text=text, input_tokens=in_tok, output_tokens=out_tok)
