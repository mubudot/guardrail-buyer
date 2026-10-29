"""Claude purchasing agent using the Anthropic Messages API with tool use.

The agent never touches PayPal directly: its tools call BuyerService, which
enforces the guardrail policy server-side.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .service import BuyerService

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TURNS = 8

SYSTEM_PROMPT = """You are GuardRail Buyer, a purchasing agent for a small team.
You buy items from the sandbox catalog and pay through PayPal, strictly under the spend policy.
Rules:
- Do not introduce yourself; get straight to work.
- Always search_catalog first to find real product_ids; never invent ids or prices.
- Call check_policy before buying. Then call create_order.
- If the result is NEEDS_APPROVAL, call request_approval with a short business justification and
  tell the user a human must approve it. Never split an order into smaller orders to dodge a cap.
- If the result is DENIED, do not try to work around it; explain why.
- If the request is ambiguous, pick the cheapest product that clearly matches.
- Finish with a short summary: what was bought (order id, amount), what awaits approval (approval id), what was refused.
"""

TOOLS: list[dict[str, Any]] = [
    {"name": "search_catalog",
     "description": "Search the sandbox product catalog. Returns products with id, name, merchant, category, price.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "keywords, e.g. 'usb cable'"},
         "category": {"type": "string"},
         "max_price": {"type": "number"}}, "required": ["query"]}},
    {"name": "check_policy",
     "description": "Dry-run the spend policy for a product and quantity. Returns ALLOW, NEEDS_APPROVAL or DENY with reasons. Does not spend.",
     "input_schema": {"type": "object", "properties": {
         "product_id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}},
         "required": ["product_id", "quantity"]}},
    {"name": "create_order",
     "description": "Buy a product via PayPal. The server re-checks policy; only ALLOW orders are placed.",
     "input_schema": {"type": "object", "properties": {
         "product_id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}},
         "required": ["product_id", "quantity"]}},
    {"name": "request_approval",
     "description": "Ask a human to approve an order that exceeds a cap (NEEDS_APPROVAL). Creates a pending approval.",
     "input_schema": {"type": "object", "properties": {
         "product_id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1},
         "justification": {"type": "string"}}, "required": ["product_id", "quantity", "justification"]}},
]


class LLMError(RuntimeError):
    pass


def anthropic_post(payload: dict[str, Any], base_url: str | None = None, timeout: float = 90.0) -> dict[str, Any]:
    """POST /v1/messages. The API key is read from ANTHROPIC_API_KEY if set; when
    calling through a gateway that injects auth on the wire, leave it unset."""
    base = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        headers["x-api-key"] = key
    r = httpx.post(f"{base}/v1/messages", json=payload, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise LLMError(f"Messages API HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


@dataclass
class AgentResult:
    final_text: str
    transcript: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    turns: int = 0
    stopped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"final_text": self.final_text, "transcript": self.transcript, "usage": self.usage,
                "turns": self.turns, "stopped_reason": self.stopped_reason}


class PurchasingAgent:
    def __init__(self, service: BuyerService, model: str | None = None,
                 post: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 max_turns: int = MAX_TURNS, max_tokens: int = 1024):
        self.service = service
        self.model = model or os.environ.get("MODEL") or DEFAULT_MODEL
        self.post = post or anthropic_post
        self.max_turns, self.max_tokens = max_turns, max_tokens

    def _run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        s = self.service
        try:
            if name == "search_catalog":
                return {"products": s.catalog.search(args.get("query", ""), args.get("category"), args.get("max_price"))}
            if name == "check_policy":
                return s.check(args["product_id"], int(args.get("quantity", 1)))
            if name == "create_order":
                return s.place_order(args["product_id"], int(args.get("quantity", 1)))
            if name == "request_approval":
                return s.request_approval(args["product_id"], int(args.get("quantity", 1)),
                                          str(args.get("justification", "")))
            return {"error": f"unknown tool {name}"}
        except (KeyError, ValueError) as e:
            return {"error": str(e)}

    def run(self, request: str) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
        res = AgentResult(final_text="")
        res.transcript.append({"role": "user", "text": request})
        for turn in range(1, self.max_turns + 1):
            resp = self.post({"model": self.model, "max_tokens": self.max_tokens, "system": SYSTEM_PROMPT,
                              "tools": TOOLS, "messages": messages})
            res.turns = turn
            u = resp.get("usage", {})
            res.usage["input_tokens"] += int(u.get("input_tokens", 0))
            res.usage["output_tokens"] += int(u.get("output_tokens", 0))
            content = resp.get("content", [])
            messages.append({"role": "assistant", "content": content})
            texts = [b["text"] for b in content if b.get("type") == "text" and b.get("text")]
            if texts:
                res.transcript.append({"role": "assistant", "text": "\n".join(texts)})
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if resp.get("stop_reason") != "tool_use" or not tool_uses:
                res.final_text = "\n".join(texts)
                res.stopped_reason = resp.get("stop_reason", "")
                return res
            results = []
            for b in tool_uses:
                out = self._run_tool(b["name"], b.get("input") or {})
                res.transcript.append({"role": "tool", "name": b["name"], "input": b.get("input"), "output": out})
                results.append({"type": "tool_result", "tool_use_id": b["id"],
                                "content": json.dumps(out, default=str)})
            messages.append({"role": "user", "content": results})
        res.stopped_reason = "max_turns"
        res.final_text = res.final_text or "Stopped: too many steps without finishing."
        return res
