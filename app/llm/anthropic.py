"""Anthropic Messages API provider: POST {ANTHROPIC_BASE_URL}/v1/messages with tool use.

The agent loop already speaks this format, so the provider is a thin HTTP call.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from .errors import LLMError, _safe_error_detail

DEFAULT_BASE_URL = "https://api.anthropic.com"


def anthropic_post(payload: dict[str, Any], base_url: str | None = None, timeout: float = 90.0) -> dict[str, Any]:
    """POST /v1/messages. Auth: ANTHROPIC_API_KEY (x-api-key) or ANTHROPIC_AUTH_TOKEN
    (Authorization: Bearer, for gateways that use bearer tokens); when calling through a
    gateway that injects auth on the wire, leave both unset."""
    base = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    key = os.environ.get("ANTHROPIC_API_KEY")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if key:
        headers["x-api-key"] = key
    elif token:
        headers["authorization"] = f"Bearer {token}"
        key = token
    try:
        r = httpx.post(f"{base}/v1/messages", json=payload, headers=headers, timeout=timeout)
    except httpx.TransportError as e:
        raise LLMError(f"Messages API transport error: {type(e).__name__}") from None
    if r.status_code != 200:
        hint = ""
        if r.status_code in (401, 403) and not key:
            # judge without a key (review log R2): say how to proceed instead of a bare 401
            hint = (" -- no ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set: put your key in .env, or restart with "
                    "AGENT_MODE=offline for the rule-based agent (no AI, no key)")
        raise LLMError(f"Messages API HTTP {r.status_code}: {_safe_error_detail(r, key)}{hint}")
    try:
        body = r.json()
    except ValueError:
        raise LLMError("Messages API returned a non-JSON body (not shown)") from None
    return validate_messages_response(body)


def _count(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def validate_messages_response(body: Any) -> dict[str, Any]:
    """Check the shape of a 200 response before the agent uses it. A malformed body is an
    LLMError, so the agent keeps and reports any partial result (orders already placed)
    instead of failing the whole request with HTTP 500."""
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, list):
        raise LLMError("Messages API response has no content list (malformed response, not shown)")
    for b in content:
        kind = b.get("type") if isinstance(b, dict) else None
        if not isinstance(kind, str):
            raise LLMError("Messages API response has a malformed content block (not shown)")
        if kind == "text" and not isinstance(b.get("text"), str):
            raise LLMError("Messages API response has a text block without text (not shown)")
        if kind == "tool_use" and not (isinstance(b.get("id"), str) and b["id"] and isinstance(b.get("name"), str)
                                       and b["name"] and isinstance(b.get("input", {}), dict)):
            raise LLMError("Messages API response has a malformed tool_use block (not shown)")
    stop = body.get("stop_reason")
    u = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return {**body, "content": content, "stop_reason": stop if isinstance(stop, str) else "",
            "usage": {"input_tokens": _count(u.get("input_tokens")), "output_tokens": _count(u.get("output_tokens"))}}
