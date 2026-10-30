"""OpenAI-compatible Chat Completions provider with function calling.

Works with OpenAI, OpenRouter, DeepSeek, vLLM, Ollama or any gateway that serves
POST {OPENAI_BASE_URL}/chat/completions. The agent loop keeps Anthropic-shaped messages;
`to_openai_request` / `from_openai_response` translate at the HTTP boundary, so tool calls and
tool results map one-to-one and the tools (and the policy behind them) are the same for both.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .errors import LLMError, _safe_error_detail

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def to_openai_request(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """Anthropic Messages payload -> Chat Completions request (tools as functions,
    tool_use -> assistant.tool_calls, tool_result -> role "tool" messages)."""
    msgs: list[dict[str, Any]] = []
    if payload.get("system"):
        msgs.append({"role": "system", "content": payload["system"]})
    for m in payload.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        blocks = content if isinstance(content, list) else []
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text"))
        if role == "assistant":
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, default=str)}}
                     for b in blocks if b.get("type") == "tool_use"]
            out: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = calls
            msgs.append(out)
        else:
            for b in blocks:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    msgs.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                 "content": c if isinstance(c, str) else json.dumps(c, default=str)})
            if text:
                msgs.append({"role": "user", "content": text})
    req: dict[str, Any] = {"model": model, "messages": msgs}
    if payload.get("tools"):
        req["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                          "parameters": t.get("input_schema") or {"type": "object"}}}
                        for t in payload["tools"]]
        req["tool_choice"] = "auto"
    if payload.get("max_tokens"):
        field_ = os.environ.get("OPENAI_MAX_TOKENS_PARAM", "max_tokens").strip() or "max_tokens"
        if field_ not in ("max_tokens", "max_completion_tokens", "none"):
            raise LLMError("OPENAI_MAX_TOKENS_PARAM must be max_tokens, max_completion_tokens or none")
        if field_ != "none":
            req[field_] = payload["max_tokens"]
    return req


_FINISH = {"stop": "end_turn", "tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens",
           "content_filter": "refusal"}


def _tool_arguments(raw: Any) -> dict[str, Any]:
    """A JSON object (or a string decoding to one) is the input. A missing or blank value is a
    no-argument call ({}). Anything else (invalid JSON, a list, a number ...) is marked
    {"_invalid_arguments": True}, which the agent refuses before any tool runs."""
    if isinstance(raw, dict):  # some servers (Ollama) send an object, not a JSON string
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"_invalid_arguments": True}


def _tokens(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def from_openai_response(body: Any) -> dict[str, Any]:
    """Chat Completions response -> Anthropic-shaped {content, stop_reason, usage}.
    A malformed response is an LLMError (so the agent keeps any partial result), never a crash."""
    choices = body.get("choices") if isinstance(body, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    msg = choice.get("message") if choice else None
    if not isinstance(msg, dict):
        raise LLMError("Chat Completions response has no choices[0].message")
    content: list[dict[str, Any]] = []
    if isinstance(msg.get("content"), str) and msg["content"]:
        content.append({"type": "text", "text": msg["content"]})
    calls = msg.get("tool_calls")
    seen: set[str] = set()
    for i, tc in enumerate(calls if isinstance(calls, list) else []):
        fn = tc.get("function") if isinstance(tc, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if not isinstance(name, str) or not name:
            continue
        cid = tc.get("id") if isinstance(tc.get("id"), str) and tc.get("id") else f"call_{i}"
        while cid in seen:  # ids must be unique so every result maps back to its call
            cid += "_"
        seen.add(cid)
        content.append({"type": "tool_use", "id": cid, "name": name, "input": _tool_arguments(fn.get("arguments"))})
    finish = choice.get("finish_reason")
    if any(b["type"] == "tool_use" for b in content):
        reason = "tool_use"
    else:
        reason = _FINISH.get(finish or "stop", finish if isinstance(finish, str) else "end_turn")
    u = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    return {"content": content, "stop_reason": reason,
            "usage": {"input_tokens": _tokens(u.get("prompt_tokens")),
                      "output_tokens": _tokens(u.get("completion_tokens"))}}


def openai_post(payload: dict[str, Any], base_url: str | None = None, timeout: float = 90.0) -> dict[str, Any]:
    """POST {OPENAI_BASE_URL}/chat/completions with OPENAI_API_KEY as Bearer (optional for
    local servers such as Ollama / vLLM). Errors are redacted like the Messages API path."""
    base = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("OPENAI_MODEL") or payload.get("model")
    if not model:
        raise LLMError("set OPENAI_MODEL for LLM_PROVIDER=openai (e.g. the model id your endpoint serves)")
    headers = {"content-type": "application/json"}
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        headers["authorization"] = f"Bearer {key}"
    req = to_openai_request(payload, model)
    try:
        r = httpx.post(f"{base}/chat/completions", json=req, headers=headers, timeout=timeout)
    except httpx.TransportError as e:
        raise LLMError(f"Chat Completions transport error: {type(e).__name__}") from None
    if r.status_code != 200:
        hint = ""
        if r.status_code in (401, 403) and not key:
            hint = (" -- no OPENAI_API_KEY is set: put your key in .env, or restart with "
                    "AGENT_MODE=offline for the rule-based agent (no AI, no key)")
        raise LLMError(f"Chat Completions HTTP {r.status_code}: {_safe_error_detail(r, key)}{hint}")
    try:
        body = r.json()
    except ValueError:
        raise LLMError("Chat Completions returned a non-JSON body (not shown)") from None
    return from_openai_response(body)
