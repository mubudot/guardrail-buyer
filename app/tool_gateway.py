"""Guarded purchasing tools for external agents (MCP clients, REST callers).

`GuardedTools` is the single entry point every external tool call goes through. It
validates arguments strictly (unknown keys, wrong types and out-of-range values are
refused, never coerced) and then calls `BuyerService`, the same server-side policy gate
the built-in agent uses: per-order cap, daily cap, hard maximum, merchant / category
allowlists, human approval over the soft caps, reservations before PayPal is called.

What an external agent can NOT do through these tools, by construction:

* choose a price, merchant, currency or payee: only catalog `product_id` + `quantity`
  are accepted; amounts come from the server catalog;
* approve, reject, cancel or capture anything: there is no such tool (a human approves
  in the dashboard with the approver token);
* see PayPal credentials, the approver token or the signed cancel state: results are
  reduced to a fixed set of public fields (`public_order`, `public_approval`).
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable

from .paypal_client import PayPalError
from .service import BuyerService

SESSION_RE = re.compile(r"mcp-[0-9a-f]{16}")

ORDER_FIELDS = ("id", "product_id", "product_name", "merchant", "category", "quantity", "amount",
                "currency", "status", "paypal_order_id", "paypal_status", "capture_id", "approval_id",
                "approve_url", "created_at", "updated_at", "paypal_live_status")
APPROVAL_FIELDS = ("id", "product_id", "product_name", "merchant", "category", "quantity", "amount",
                   "currency", "justification", "reasons", "status", "order_id", "created_at", "decided_at")
ORDER_RESULT = {"PAYER_ACTION_REQUIRED": "AWAITING_PAYER_APPROVAL", "COMPLETED": "ORDER_PLACED",
                "CREATING": "PAYMENT_UNCONFIRMED", "CAPTURING": "PAYMENT_UNCONFIRMED",
                "NEEDS_RECONCILIATION": "PAYMENT_UNCONFIRMED"}


class ToolInputError(ValueError):
    """The tool call's arguments are invalid; nothing was evaluated or spent."""


def public_order(o: dict[str, Any] | None) -> dict[str, Any] | None:
    if not o:
        return o
    return {k: o.get(k) for k in ORDER_FIELDS if k in o}


def public_approval(a: dict[str, Any] | None) -> dict[str, Any] | None:
    if not a:
        return a
    return {k: a.get(k) for k in APPROVAL_FIELDS if k in a}


def _public_result(out: dict[str, Any]) -> dict[str, Any]:
    out = dict(out)
    if isinstance(out.get("order"), dict):
        out["order"] = public_order(out["order"])
    return out


# ---- schemas (also published as the MCP inputSchema) -----------------------
_PID = {"type": "string", "minLength": 1, "maxLength": 64, "description": "catalog product id, e.g. 'P-1001'"}
_QTY = {"type": "integer", "minimum": 1, "maximum": 100}

TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "search_catalog", "title": "Search the catalog",
     "description": "Search the product catalog. Returns products with id, name, merchant, category and "
                    "price. Prices and merchants are set by the server; use the returned product ids.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
         "query": {"type": "string", "maxLength": 200, "description": "keywords, e.g. 'usb cable'"},
         "category": {"type": "string", "maxLength": 64},
         "max_price": {"type": "number", "minimum": 0}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "check_policy", "title": "Dry-run the spend policy",
     "description": "Dry-run the spend policy for a product and quantity: ALLOW, NEEDS_APPROVAL or DENY with "
                    "the reasons. Spends nothing.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["product_id", "quantity"],
                     "properties": {"product_id": _PID, "quantity": _QTY}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "create_order", "title": "Order through PayPal (policy-gated)",
     "description": "Order a catalog product. The server re-checks the policy: only ALLOW orders are reserved "
                    "against the daily budget and created as a PayPal order (the buyer approves the payment on "
                    "PayPal at approve_url). NEEDS_APPROVAL -> call request_approval. DENY is final.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["product_id", "quantity"],
                     "properties": {"product_id": _PID, "quantity": _QTY}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                     "openWorldHint": True}},
    {"name": "request_approval", "title": "Ask a human to approve",
     "description": "File a human approval request for an order over a soft cap (NEEDS_APPROVAL). A person "
                    "approves or rejects it in the GuardRail dashboard; denied orders cannot be escalated. "
                    "The justification must quote the user's stated purpose.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "required": ["product_id", "quantity", "justification"],
                     "properties": {"product_id": _PID, "quantity": _QTY,
                                    "justification": {"type": "string", "minLength": 1, "maxLength": 500}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "get_order_status", "title": "Order status",
     "description": "Status of an order (live from PayPal when it has a PayPal order): AWAITING_PAYER_APPROVAL "
                    "(with approve_url), ORDER_PLACED (capture COMPLETED), PAYMENT_UNCONFIRMED or FAILED.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["order_id"],
                     "properties": {"order_id": {"type": "string", "pattern": "^ORD-[0-9A-F]{10}$"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": True}},
    {"name": "get_approval_status", "title": "Approval status",
     "description": "Status of an approval request: PENDING, APPROVED (with the order it created), REJECTED, "
                    "DENIED_BY_POLICY or QUOTE_CHANGED.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["approval_id"],
                     "properties": {"approval_id": {"type": "string", "pattern": "^APR-[0-9A-F]{8}$"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "get_spend_summary", "title": "Budget headroom",
     "description": "Today's spend against the caps: captured, reserved, remaining daily budget, per-order cap "
                    "and hard maximum.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]
TOOL_NAMES = tuple(t["name"] for t in TOOL_SPECS)
_SPEC = {t["name"]: t["inputSchema"] for t in TOOL_SPECS}


def validate_args(name: str, args: Any) -> dict[str, Any]:
    """Strict validation against the published schema (no coercion). Raises ToolInputError."""
    schema = _SPEC.get(name)
    if schema is None:
        raise ToolInputError(f"unknown tool {name!r}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolInputError("arguments must be an object")
    props = schema["properties"]
    extra = sorted(set(args) - set(props))
    if extra:
        raise ToolInputError(f"unexpected argument(s) {', '.join(map(repr, extra))[:120]}: prices, merchants, "
                             f"amounts and payees are set by the server, not by the caller")
    for req in schema.get("required", []):
        if req not in args:
            raise ToolInputError(f"missing required argument {req!r}")
    for k, v in args.items():
        p = props[k]
        t = p["type"]
        if t == "string":
            if not isinstance(v, str):
                raise ToolInputError(f"{k} must be a string")
            if len(v) < p.get("minLength", 0) or len(v) > p.get("maxLength", 10_000):
                raise ToolInputError(f"{k} has an invalid length")
            if "pattern" in p and not re.fullmatch(p["pattern"].strip("^$"), v):
                raise ToolInputError(f"{k} has an invalid format")
        elif t == "integer":
            if isinstance(v, bool) or not isinstance(v, int) or not (p["minimum"] <= v <= p["maximum"]):
                raise ToolInputError(f"{k} must be a whole number between {p['minimum']} and {p['maximum']}")
        elif t == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                    or v < p.get("minimum", -math.inf):
                raise ToolInputError(f"{k} must be a finite non-negative number")
    return args


class GuardedTools:
    """Dispatch validated tool calls to BuyerService. `session_id` (an `mcp-<16 hex>`
    id the MCP server generates once per process) ties the calls of one agent session
    together, so split orders of the same SKU at the same merchant count as one purchase
    for the aggregate hard maximum."""

    def __init__(self, service: BuyerService):
        self.service = service

    @staticmethod
    def request_id(session_id: str | None) -> str | None:
        if session_id is None:
            return None
        if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
            raise ToolInputError("session_id must look like mcp-<16 lowercase hex>")
        return session_id

    def call(self, name: str, args: Any, session_id: str | None = None) -> dict[str, Any]:
        """Returns {"ok": True, "result": {...}} or {"ok": False, "error": "..."}; policy
        outcomes (DENIED, NEEDS_APPROVAL) are results, not errors."""
        try:
            a = validate_args(name, args)
            rid = self.request_id(session_id)
            return {"ok": True, "result": self._dispatch(name, a, rid)}
        except ToolInputError as e:
            return {"ok": False, "error": f"invalid arguments: {e}", "code": "INVALID_ARGUMENTS"}
        except KeyError as e:
            return {"ok": False, "error": str(e.args[0] if e.args else e)[:200], "code": "NOT_FOUND"}
        except (ValueError, TypeError, OverflowError, PayPalError) as e:
            return {"ok": False, "error": str(e)[:300], "code": "REFUSED"}
        except Exception as e:  # never leak internals
            return {"ok": False, "error": f"tool failed: {type(e).__name__}", "code": "INTERNAL"}

    def _dispatch(self, name: str, a: dict[str, Any], rid: str | None) -> dict[str, Any]:
        s = self.service
        handlers: dict[str, Callable[[], dict[str, Any]]] = {
            "search_catalog": lambda: {"products": s.catalog.search(a.get("query", ""), a.get("category"),
                                                                    a.get("max_price"))},
            "check_policy": lambda: s.check(a["product_id"], a["quantity"], request_id=rid),
            "create_order": lambda: _public_result(s.place_order(a["product_id"], a["quantity"], request_id=rid)),
            "request_approval": lambda: s.request_approval(a["product_id"], a["quantity"], a["justification"],
                                                           request_id=rid),
            "get_order_status": lambda: self._order(a["order_id"]),
            "get_approval_status": lambda: self._approval(a["approval_id"]),
            "get_spend_summary": s.spend_summary,
        }
        return handlers[name]()

    def _order(self, oid: str) -> dict[str, Any]:
        try:
            o = self.service.get_order(oid, refresh=True)
        except KeyError:
            raise KeyError(f"unknown order {oid}") from None
        res = ORDER_RESULT.get(o["status"], o["status"])  # FAILED, CANCELLED, EXPIRED, ... as is
        out = {"result": res, "order": public_order(o)}
        if o.get("refresh_error"):
            out["note"] = "live PayPal status unavailable; showing the stored status"
        return out

    def _approval(self, aid: str) -> dict[str, Any]:
        a = self.service.store.get("approvals", aid)
        if a is None:
            raise KeyError(f"unknown approval {aid}")
        out: dict[str, Any] = {"result": a["status"], "approval": public_approval(a)}
        if a.get("order_id"):
            o = self.service.store.get("orders", a["order_id"])
            out["order"] = public_order(o)
        return out
