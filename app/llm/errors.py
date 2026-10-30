"""LLM provider errors and the redaction rule every provider applies to upstream error bodies."""
from __future__ import annotations

import os
import re

import httpx


class LLMError(RuntimeError):
    pass


_SECRETISH = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|[A-Za-z0-9_-]{40,})")
# Documented Messages API error types; anything else is reported as unknown_error.
_ERROR_TYPES = frozenset({"invalid_request_error", "authentication_error", "billing_error", "permission_error",
                          "not_found_error", "request_too_large", "rate_limit_error", "timeout_error", "api_error",
                          "overloaded_error",
                          # OpenAI-compatible servers (OpenAI, OpenRouter, vLLM, Ollama ...)
                          "server_error", "insufficient_quota", "rate_limit_exceeded", "invalid_api_key",
                          "model_not_found", "context_length_exceeded", "requests", "tokens"})


def _configured_secrets(key: str | None) -> list[str]:
    return [v for v in (key, os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("ANTHROPIC_AUTH_TOKEN"),
                        os.environ.get("OPENAI_API_KEY"), os.environ.get("PAYPAL_CLIENT_SECRET"),
                        os.environ.get("APPROVER_TOKEN"), os.environ.get("POLICY_OWNER_TOKEN"),
                        os.environ.get("STATE_SECRET"), os.environ.get("PAYPAL_CLIENT_ID")) if v]


def _safe_error_detail(r: httpx.Response, key: str | None) -> str:
    """What an upstream error may show to an anonymous /agent/request caller (HTTP 502): only a known
    Messages API `error.type` and a short `error.message`, never the raw body (a gateway may echo
    headers or keys there). If the message contains any configured secret it is withheld entirely;
    otherwise key-like tokens are masked."""
    try:
        err = r.json().get("error") or {}
        if isinstance(err, str):  # some OpenAI-compatible servers (e.g. Ollama): {"error": "text"}
            err = {"message": err}
        etype, msg = err.get("type"), err.get("message")
        if not (isinstance(etype, str) and etype in _ERROR_TYPES) and isinstance(err.get("code"), str):
            etype = err["code"]  # OpenAI puts the specific reason in `code` (e.g. invalid_api_key)
    except (ValueError, AttributeError):
        return "non-JSON error body (not shown)"
    # Only plain strings are shown: str() of an object/list would re-encode (escape) a secret so the
    # substring check below could miss it.
    msg = msg if isinstance(msg, str) else ""
    etype = etype if isinstance(etype, str) and etype in _ERROR_TYPES else "unknown_error"
    secrets_ = _configured_secrets(key)
    if any(v in msg for v in secrets_):
        return f"{etype} (message withheld: it contained a configured secret)"
    msg = _SECRETISH.sub("[redacted]", msg)
    return f"{etype}: {msg[:200]}" if msg else etype
