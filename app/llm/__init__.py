"""Pluggable LLM providers for the purchasing agent.

A provider is a callable `post(payload) -> response` where both sides use the Anthropic
Messages shape (`system`, `messages`, `tools`, `max_tokens` in; `content`, `stop_reason`,
`usage` out). The agent loop only sees that shape; providers own the wire format.

    LLM_PROVIDER=anthropic   Anthropic Messages API (ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY | ANTHROPIC_AUTH_TOKEN, MODEL)
    LLM_PROVIDER=openai      OpenAI-compatible Chat Completions (OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL)

Unset: chosen from which credential is set (see `select_provider`). Providers only produce
tool calls; every call still goes through BuyerService, which alone decides about money.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from .anthropic import anthropic_post
from .errors import LLMError
from .openai import openai_post

PROVIDERS = ("anthropic", "openai")
Post = Callable[[dict[str, Any]], dict[str, Any]]

__all__ = ["LLMError", "PROVIDERS", "Post", "anthropic_post", "openai_post", "select_provider"]


def select_provider(env: Any = None) -> str:
    """LLM_PROVIDER=anthropic|openai; unset -> anthropic if an Anthropic credential is set,
    else openai if OPENAI_API_KEY is set, else anthropic (the historical default)."""
    env = os.environ if env is None else env
    p = (env.get("LLM_PROVIDER") or "").strip().lower()
    if p:
        if p not in PROVIDERS:
            raise ValueError(f"unknown LLM_PROVIDER {p!r} (use 'anthropic' or 'openai')")
        return p
    if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if env.get("OPENAI_API_KEY"):
        return "openai"
    return "anthropic"
