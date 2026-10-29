"""Business layer: catalog, order/approval store and the BuyerService.

Every money-moving path goes through `BuyerService`, which re-runs the
guardrail engine server-side. The AI agent can *ask*, but it cannot bypass
policy: it only ever calls these methods via tools.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .guardrails import (Decision, OrderRequest, Policy, SpendLedger, Verdict,
                         evaluate, hard_rules_only, money)
from .paypal_client import PayPalClient, PayPalError


class Catalog:
    def __init__(self, products: list[dict[str, Any]]):
        self.products = {p["id"]: p for p in products}

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, product_id: str) -> dict[str, Any] | None:
        return self.products.get(product_id)

    def search(self, query: str = "", category: str | None = None,
               max_price: Any = None, limit: int = 8) -> list[dict[str, Any]]:
        words = [w for w in query.lower().split() if w]
        out = []
        for p in self.products.values():
            hay = f"{p['name']} {p['merchant']} {p['category']}".lower()
            score = sum(1 for w in words if w in hay) if words else 1
            if words and score == 0:
                continue
            if category and p["category"].lower() != category.lower():
                continue
            if max_price is not None and money(p["price"]) > money(max_price):
                continue
            out.append((score, p))
        out.sort(key=lambda t: (-t[0], money(t[1]["price"])))
        return [p for _, p in out[:limit]]


class Store:
    def __init__(self, db_path: str = ":memory:"):
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, product_id TEXT, product_name TEXT, merchant TEXT,
                category TEXT, quantity INTEGER, amount TEXT, currency TEXT,
                status TEXT, paypal_order_id TEXT, paypal_status TEXT, capture_id TEXT,
                approval_id TEXT, decision TEXT, created_at TEXT, approve_url TEXT,
                updated_at TEXT);
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, product_id TEXT, product_name TEXT, merchant TEXT,
                category TEXT, quantity INTEGER, amount TEXT, currency TEXT,
                justification TEXT, reasons TEXT, status TEXT, order_id TEXT,
                created_at TEXT, decided_at TEXT);
            """
        )
        # migrate DBs created by v0.1 (no approve_url / updated_at columns)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(orders)")}
        for col in ("approve_url", "updated_at"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
        self.conn.commit()

    def find(self, table: str, **where: Any) -> dict[str, Any] | None:
        cond = " AND ".join(f"{k}=?" for k in where)
        with self._lock:
            r = self.conn.execute(f"SELECT * FROM {table} WHERE {cond}", list(where.values())).fetchone()
        return self._decode(dict(r)) if r else None

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        with self._lock:
            self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
            self.conn.commit()

    def update(self, table: str, row_id: str, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*fields.values(), row_id])
            self.conn.commit()

    def get(self, table: str, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        return self._decode(dict(r)) if r else None

    @staticmethod
    def _decode(d: dict[str, Any]) -> dict[str, Any]:
        for k in ("reasons", "decision"):
            if isinstance(d.get(k), str):
                d[k] = json.loads(d[k])
        return d

    def list(self, table: str, status: str | None = None) -> list[dict[str, Any]]:
        q, args = f"SELECT * FROM {table}", []
        if status:
            q, args = q + " WHERE status=?", [status]
        with self._lock:
            rows = self.conn.execute(q + " ORDER BY created_at DESC, rowid DESC", args).fetchall()
        return [self._decode(dict(r)) for r in rows]


# Order lifecycle (orders.status):
#   CREATING -> COMPLETED                      (mock auto-approve: create + capture at once)
#   CREATING -> PAYER_ACTION_REQUIRED -> COMPLETED   (buyer approved on PayPal, captured on return)
#                                     -> CANCELLED   (buyer hit cancel on PayPal)
#                                     -> EXPIRED     (not approved within approval_ttl)
#                                     -> CAPTURE_FAILED
#   CREATING -> FAILED                          (PayPal create call failed)
# Spend accounting: the amount is RESERVED in the daily ledger as soon as the
# PayPal order exists (so an agent cannot open many unpaid orders to dodge the
# daily cap) and RELEASED on CANCELLED / EXPIRED / CAPTURE_FAILED. FAILED orders
# never reserve. A reservation stays on the day it was made.
OPEN_STATUSES = ("CREATING", "PAYER_ACTION_REQUIRED")
RELEASED_STATUSES = ("CANCELLED", "EXPIRED", "CAPTURE_FAILED", "FAILED")


class BuyerService:
    def __init__(self, policy: Policy, catalog: Catalog, paypal: PayPalClient,
                 db_path: str = ":memory:", clock=None, approval_ttl: timedelta | None = None):
        self.policy, self.catalog, self.paypal = policy, catalog, paypal
        self.store = Store(db_path)
        self.ledger = SpendLedger(db_path)
        self._clock = clock or (lambda: datetime.now(ZoneInfo("UTC")))
        self.approval_ttl = approval_ttl or timedelta(
            minutes=int(os.environ.get("PAYPAL_APPROVAL_TTL_MIN", "180")))

    def _ts(self) -> str:
        return self._clock().astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

    # ---- helpers -------------------------------------------------------
    def _today(self) -> str:
        return self.policy.today(self._clock())

    def spent_today(self) -> Decimal:
        return self.ledger.spent_on(self._today(), self.policy.currency)

    def _line(self, product_id: str, quantity: int) -> tuple[dict[str, Any], OrderRequest]:
        p = self.catalog.get(product_id)
        if p is None:
            raise KeyError(f"unknown product_id {product_id}")
        if not isinstance(quantity, int) or quantity < 1 or quantity > 100:
            raise ValueError("quantity must be an integer between 1 and 100")
        amount = money(money(p["price"]) * quantity)
        req = OrderRequest(merchant=p["merchant"], category=p["category"], amount=amount,
                           currency=p.get("currency", "USD"), description=f"{quantity} x {p['name']}")
        return p, req

    # ---- read-only check ----------------------------------------------
    def check(self, product_id: str, quantity: int = 1) -> dict[str, Any]:
        p, req = self._line(product_id, quantity)
        d = evaluate(self.policy, req, self.spent_today())
        return {"product_id": product_id, "product_name": p["name"], "quantity": quantity,
                "amount": str(req.amount), "currency": req.currency,
                "spent_today": str(self.spent_today()), **d.to_dict()}

    # ---- money-moving --------------------------------------------------
    def _execute(self, p: dict[str, Any], req: OrderRequest, quantity: int,
                 decision: Decision, approval_id: str | None = None) -> dict[str, Any]:
        oid = "ORD-" + uuid.uuid4().hex[:10].upper()
        now = self._ts()
        row = {"id": oid, "product_id": p["id"], "product_name": p["name"], "merchant": p["merchant"],
               "category": p["category"], "quantity": quantity, "amount": str(req.amount),
               "currency": req.currency, "status": "CREATING", "paypal_order_id": None,
               "paypal_status": None, "capture_id": None, "approval_id": approval_id,
               "decision": json.dumps(decision.to_dict()), "created_at": now,
               "approve_url": None, "updated_at": now}
        self.store.insert("orders", row)
        try:
            created = self.paypal.create_order(req.amount, req.currency, req.description, oid,
                                               items=[{"sku": p["id"], "quantity": quantity}])
        except PayPalError as e:
            self.store.update("orders", oid, status="FAILED", paypal_status=str(e)[:200], updated_at=self._ts())
            out = self.store.get("orders", oid)
            out["error"] = str(e)
            return out
        # Reserve budget as soon as the PayPal order exists.
        self.ledger.record(self._today(), req.amount, req.currency, oid)
        self.store.update("orders", oid, paypal_order_id=created["id"], paypal_status=created["status"],
                          approve_url=created.get("approve_url"), updated_at=self._ts())
        if created["status"] == "APPROVED":
            return self._capture(oid)
        self.store.update("orders", oid, status="PAYER_ACTION_REQUIRED")
        return self.store.get("orders", oid)

    def _capture(self, oid: str) -> dict[str, Any]:
        o = self.store.get("orders", oid)
        try:
            cap = self.paypal.capture_order(o["paypal_order_id"])
        except PayPalError as e:
            # Not approved yet is not fatal: the buyer may still approve.
            if "ORDER_NOT_APPROVED" in str(e) or "PAYER_ACTION_REQUIRED" in str(e):
                out = self.store.get("orders", oid)
                out["error"] = str(e)
                return out
            self.store.update("orders", oid, status="CAPTURE_FAILED", paypal_status=str(e)[:200],
                              updated_at=self._ts())
            self.ledger.release(oid)
            out = self.store.get("orders", oid)
            out["error"] = str(e)
            return out
        status = "COMPLETED" if cap["status"] == "COMPLETED" else cap["status"]
        self.store.update("orders", oid, status=status, paypal_status=cap["status"],
                          capture_id=cap.get("capture_id"), updated_at=self._ts())
        return self.store.get("orders", oid)

    # ---- PayPal buyer-approval round trip --------------------------------
    def _by_paypal_id(self, paypal_order_id: str) -> dict[str, Any]:
        o = self.store.find("orders", paypal_order_id=paypal_order_id)
        if o is None:
            raise KeyError(paypal_order_id)
        return o

    def complete_payment(self, paypal_order_id: str) -> dict[str, Any]:
        """Buyer returned from PayPal after approving: capture (idempotent)."""
        self.expire_stale()
        o = self._by_paypal_id(paypal_order_id)
        if o["status"] == "COMPLETED":
            return o
        if o["status"] != "PAYER_ACTION_REQUIRED":
            raise ValueError(f"order {o['id']} is {o['status']}, cannot capture")
        return self._capture(o["id"])

    def cancel_payment(self, paypal_order_id: str) -> dict[str, Any]:
        """Buyer cancelled on PayPal: release the reserved spend."""
        o = self._by_paypal_id(paypal_order_id)
        if o["status"] in OPEN_STATUSES:
            self.store.update("orders", o["id"], status="CANCELLED", updated_at=self._ts())
            self.ledger.release(o["id"])
        return self.store.get("orders", o["id"])

    def expire_stale(self) -> list[str]:
        """Expire orders the buyer never approved within approval_ttl."""
        cutoff = (self._clock() - self.approval_ttl).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        expired = []
        for o in self.store.list("orders", "PAYER_ACTION_REQUIRED"):
            if o["created_at"] < cutoff:
                self.store.update("orders", o["id"], status="EXPIRED", updated_at=self._ts())
                self.ledger.release(o["id"])
                expired.append(o["id"])
        return expired

    def spend_summary(self) -> dict[str, Any]:
        """Today's spend split into captured vs reserved, against the caps."""
        self.expire_stale()
        day, cur = self._today(), self.policy.currency
        captured = reserved = Decimal("0")
        for d, cents, c, ref in self.ledger.entries():
            if d != day or c != cur:
                continue
            amt = Decimal(cents) / 100
            o = self.store.get("orders", ref) if ref else None
            if o and o["status"] in OPEN_STATUSES:
                reserved += amt
            else:
                captured += amt
        total = captured + reserved
        cap = self.policy.daily_cap
        return {"day": day, "currency": cur, "captured": str(money(captured)),
                "reserved": str(money(reserved)), "total": str(money(total)),
                "daily_cap": str(cap), "remaining": str(money(max(cap - total, Decimal("0")))),
                "per_order_cap": str(self.policy.per_order_cap),
                "hard_max_per_order": str(self.policy.hard_max_per_order)}

    @staticmethod
    def _result(order: dict[str, Any]) -> str:
        if order["status"] == "PAYER_ACTION_REQUIRED":
            return "AWAITING_PAYER_APPROVAL"
        return "ORDER_PLACED" if order["status"] == "COMPLETED" else "FAILED"

    def place_order(self, product_id: str, quantity: int = 1) -> dict[str, Any]:
        p, req = self._line(product_id, quantity)
        d = evaluate(self.policy, req, self.spent_today())
        if d.verdict == Verdict.DENY:
            return {"result": "DENIED", **d.to_dict()}
        if d.verdict == Verdict.NEEDS_APPROVAL:
            return {"result": "NEEDS_APPROVAL", **d.to_dict(),
                    "next_step": "call request_approval with a justification; a human must approve"}
        order = self._execute(p, req, quantity, d)
        return {"result": self._result(order), "order": order, **d.to_dict()}

    def request_approval(self, product_id: str, quantity: int, justification: str) -> dict[str, Any]:
        p, req = self._line(product_id, quantity)
        d = evaluate(self.policy, req, self.spent_today())
        if d.verdict == Verdict.DENY:
            return {"result": "DENIED", **d.to_dict(), "note": "denied orders cannot be escalated"}
        if d.verdict == Verdict.ALLOW:
            return {"result": "NOT_NEEDED", **d.to_dict(), "note": "within policy; use create_order"}
        aid = "APR-" + uuid.uuid4().hex[:8].upper()
        self.store.insert("approvals", {
            "id": aid, "product_id": p["id"], "product_name": p["name"], "merchant": p["merchant"],
            "category": p["category"], "quantity": quantity, "amount": str(req.amount),
            "currency": req.currency, "justification": justification[:500],
            "reasons": json.dumps(list(d.reasons)), "status": "PENDING", "order_id": None,
            "created_at": self._ts(), "decided_at": None})
        return {"result": "APPROVAL_REQUESTED", "approval_id": aid, **d.to_dict()}

    def approve(self, approval_id: str) -> dict[str, Any]:
        a = self.store.get("approvals", approval_id)
        if a is None:
            raise KeyError(approval_id)
        if a["status"] != "PENDING":
            raise ValueError(f"approval is already {a['status']}")
        p, req = self._line(a["product_id"], a["quantity"])
        d = hard_rules_only(self.policy, req)  # policy may have changed since the request
        if d.verdict == Verdict.DENY:
            self.store.update("approvals", approval_id, status="DENIED_BY_POLICY", decided_at=self._ts())
            return {"result": "DENIED", **d.to_dict()}
        self.store.update("approvals", approval_id, status="APPROVED", decided_at=self._ts())
        order = self._execute(p, req, a["quantity"], d, approval_id)
        self.store.update("approvals", approval_id, order_id=order["id"])
        return {"result": self._result(order), "order": order}

    def reject(self, approval_id: str) -> dict[str, Any]:
        a = self.store.get("approvals", approval_id)
        if a is None:
            raise KeyError(approval_id)
        if a["status"] != "PENDING":
            raise ValueError(f"approval is already {a['status']}")
        self.store.update("approvals", approval_id, status="REJECTED", decided_at=self._ts())
        return {"result": "REJECTED", "approval_id": approval_id}
