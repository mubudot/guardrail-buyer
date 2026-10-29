"""Business layer: catalog, order/approval store and the BuyerService.

Every money-moving path goes through `BuyerService`, which re-runs the
guardrail engine server-side. The AI agent can *ask*; it only ever calls these
methods via tools. Safety properties enforced here (see tests/test_concurrency.py):

* budget check + provisional reservation happen in ONE SQLite write transaction
  (BEGIN IMMEDIATE) *before* PayPal is called, so concurrent requests (threads or
  processes sharing the DB file) cannot all pass against the same spend;
* order lifecycle changes are conditional UPDATEs (compare-and-set on status),
  with a durable CAPTURING state that cancel/expiry cannot override;
* an approval is claimed once (PENDING -> EXECUTING) together with its order and
  reservation; retries return the existing order;
* an approval is bound to its quote (product, quantity, amount, currency,
  merchant, category); a changed quote needs a fresh approval;
* ambiguous PayPal outcomes keep the reservation (NEEDS_RECONCILIATION) and are
  resolved from the provider's order status; only a confirmed COMPLETED capture
  counts as COMPLETED.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .guardrails import (Decision, OrderRequest, Policy, SpendLedger, Verdict,
                         evaluate, hard_rules_only, money)
from .paypal_client import MockPayPal, PayPalClient, PayPalError


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
    """SQLite store for orders and approvals. One connection in autocommit mode;
    `tx()` opens an explicit BEGIN IMMEDIATE write transaction (serialized across
    threads by the RLock and across processes by SQLite's write lock)."""

    def __init__(self, db_path: str = ":memory:"):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, product_id TEXT, product_name TEXT, merchant TEXT,
                category TEXT, quantity INTEGER, amount TEXT, currency TEXT,
                status TEXT, paypal_order_id TEXT, paypal_status TEXT, capture_id TEXT,
                approval_id TEXT, decision TEXT, created_at TEXT, approve_url TEXT,
                updated_at TEXT, request_id TEXT);
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, product_id TEXT, product_name TEXT, merchant TEXT,
                category TEXT, quantity INTEGER, amount TEXT, currency TEXT,
                justification TEXT, reasons TEXT, status TEXT, order_id TEXT,
                created_at TEXT, decided_at TEXT, request_id TEXT);
            """
        )
        # migrate DBs created by older versions
        for table, wanted in (("orders", ("approve_url", "updated_at", "request_id")),
                              ("approvals", ("request_id",))):
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col in wanted:
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS orders_paypal ON orders(paypal_order_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS orders_req ON orders(request_id, product_id)")
        try:  # one order per approval (B2). Old DBs that already hold duplicates keep working.
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS orders_one_per_approval "
                              "ON orders(approval_id) WHERE approval_id IS NOT NULL")
        except sqlite3.IntegrityError:  # pragma: no cover - legacy data only
            pass

    @contextmanager
    def tx(self):
        """Write transaction. Re-entrant: a nested tx() joins the outer one."""
        with self.lock:
            if self.conn.in_transaction:
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def find(self, table: str, **where: Any) -> dict[str, Any] | None:
        cond = " AND ".join(f"{k}=?" for k in where)
        with self.lock:
            r = self.conn.execute(f"SELECT * FROM {table} WHERE {cond}", list(where.values())).fetchone()
        return self._decode(dict(r)) if r else None

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        with self.lock:
            self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))

    def update(self, table: str, row_id: str, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*fields.values(), row_id])

    def get(self, table: str, row_id: str) -> dict[str, Any] | None:
        with self.lock:
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
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY created_at DESC, rowid DESC", args).fetchall()
        return [self._decode(dict(r)) for r in rows]


# Order lifecycle (orders.status). Every arrow is a conditional UPDATE on the old status.
#   CREATING  -- budget checked and amount RESERVED in the same transaction, PayPal not called yet
#   CREATING -> PAYER_ACTION_REQUIRED          (PayPal order exists, buyer must approve)
#            -> FAILED                         (PayPal definitively refused to create; released)
#            -> NEEDS_RECONCILIATION           (create outcome unknown; still reserved)
#   PAYER_ACTION_REQUIRED -> CAPTURING         (capture call in flight; cancel/expiry can't touch it)
#                         -> CANCELLED / EXPIRED   (released)
#   CAPTURING -> COMPLETED                     (capture confirmed COMPLETED)
#             -> PAYER_ACTION_REQUIRED         (PayPal: buyer has not approved yet)
#             -> CAPTURE_FAILED                (definitive decline/refusal; released)
#             -> NEEDS_RECONCILIATION          (5xx / transport error / capture PENDING; still reserved)
#   NEEDS_RECONCILIATION -> any of the above, decided from PayPal's order status (reconcile()).
#   MOCK_RESET: mock-provider order lost on restart (mock state is in-memory); released.
# Spend accounting: the ledger row is written when the order row is (before PayPal is
# called) and deleted only on a released status. A reservation stays on the day it was made.
RESERVED_STATUSES = ("CREATING", "PAYER_ACTION_REQUIRED", "CAPTURING", "NEEDS_RECONCILIATION")
OPEN_STATUSES = RESERVED_STATUSES  # backwards-compatible name
RELEASED_STATUSES = ("CANCELLED", "EXPIRED", "CAPTURE_FAILED", "FAILED", "MOCK_RESET")
COUNTED_STATUSES = RESERVED_STATUSES + ("COMPLETED",)
STUCK_AFTER = timedelta(minutes=10)  # CREATING/CAPTURING older than this may be reconciled


class BuyerService:
    def __init__(self, policy: Policy, catalog: Catalog, paypal: PayPalClient,
                 db_path: str = ":memory:", clock=None, approval_ttl: timedelta | None = None):
        self.policy, self.catalog, self.paypal = policy, catalog, paypal
        self.store = Store(db_path)
        # same connection + lock: budget check and reservation share one transaction
        self.ledger = SpendLedger(conn=self.store.conn, lock=self.store.lock)
        self._clock = clock or (lambda: datetime.now(ZoneInfo("UTC")))
        self.approval_ttl = approval_ttl or timedelta(
            minutes=int(os.environ.get("PAYPAL_APPROVAL_TTL_MIN", "180")))

    def _ts(self, dt: datetime | None = None) -> str:
        return (dt or self._clock()).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")

    # ---- helpers -------------------------------------------------------
    def _today(self) -> str:
        return self.policy.today(self._clock())

    def spent_today(self) -> Decimal:
        return self.ledger.spent_on(self._today(), self.policy.currency)

    def _line(self, product_id: str, quantity: int) -> tuple[dict[str, Any], OrderRequest]:
        p = self.catalog.get(product_id)
        if p is None:
            raise KeyError(f"unknown product_id {product_id}")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1 or quantity > 100:
            raise ValueError("quantity must be an integer between 1 and 100")
        amount = money(money(p["price"]) * quantity)
        req = OrderRequest(merchant=p["merchant"], category=p["category"], amount=amount,
                           currency=p.get("currency", "USD"), description=f"{quantity} x {p['name']}")
        return p, req

    @staticmethod
    def _quote(product_id: str, quantity: int, amount: Any, currency: str, merchant: str,
               category: str) -> dict[str, Any]:
        """The material terms a human approval is bound to."""
        return {"product_id": product_id, "quantity": int(quantity), "amount": str(money(amount)),
                "currency": str(currency).upper(), "merchant": merchant, "category": category}

    def _related_prior(self, request_id: str | None, product_id: str, merchant: str) -> Decimal:
        """Anti-splitting: amount already ordered (not released) for the same SKU at the
        same merchant within the same agent request."""
        if not request_id:
            return Decimal("0")
        qs = ",".join("?" for _ in COUNTED_STATUSES)
        with self.store.lock:
            rows = self.store.conn.execute(
                f"SELECT amount FROM orders WHERE request_id=? AND product_id=? AND lower(merchant)=lower(?) "
                f"AND status IN ({qs})", [request_id, product_id, merchant, *COUNTED_STATUSES]).fetchall()
        return money(sum((money(r[0]) for r in rows), Decimal("0")))

    def _evaluate(self, p: dict[str, Any], req: OrderRequest, request_id: str | None) -> Decision:
        return evaluate(self.policy, req, self.spent_today(),
                        self._related_prior(request_id, p["id"], p["merchant"]))

    # ---- read-only check ----------------------------------------------
    def check(self, product_id: str, quantity: int = 1, request_id: str | None = None) -> dict[str, Any]:
        p, req = self._line(product_id, quantity)
        d = self._evaluate(p, req, request_id)
        return {"product_id": product_id, "product_name": p["name"], "quantity": quantity,
                "amount": str(req.amount), "currency": req.currency,
                "spent_today": str(self.spent_today()), **d.to_dict()}

    # ---- atomic state changes -----------------------------------------
    def _transition(self, oid: str, from_statuses: tuple[str, ...], to: str, release: bool = False,
                    **fields: Any) -> bool:
        """Compare-and-set the order status. Releases the reservation in the same
        transaction when `release`. Returns False (and changes nothing) if the order is
        no longer in one of `from_statuses`."""
        sets = {"status": to, "updated_at": self._ts(), **fields}
        qs = ",".join("?" for _ in from_statuses)
        with self.store.tx() as c:
            cur = c.execute(f"UPDATE orders SET {','.join(f'{k}=?' for k in sets)} "
                            f"WHERE id=? AND status IN ({qs})", [*sets.values(), oid, *from_statuses])
            if cur.rowcount != 1:
                return False
            if release:
                c.execute("DELETE FROM spend WHERE order_ref=?", (oid,))
            return True

    def _reserve(self, p: dict[str, Any], req: OrderRequest, quantity: int, decision: Decision,
                 approval_id: str | None = None, request_id: str | None = None) -> str:
        """Insert the order (CREATING) and its spend reservation. Caller holds store.tx()."""
        oid = "ORD-" + uuid.uuid4().hex[:10].upper()
        now = self._ts()
        self.store.insert("orders", {
            "id": oid, "product_id": p["id"], "product_name": p["name"], "merchant": p["merchant"],
            "category": p["category"], "quantity": quantity, "amount": str(req.amount),
            "currency": req.currency, "status": "CREATING", "paypal_order_id": None,
            "paypal_status": None, "capture_id": None, "approval_id": approval_id,
            "decision": json.dumps(decision.to_dict()), "created_at": now,
            "approve_url": None, "updated_at": now, "request_id": request_id})
        self.ledger.record(self._today(), req.amount, req.currency, oid)
        return oid

    # ---- money-moving --------------------------------------------------
    def _create_at_provider(self, oid: str) -> dict[str, Any]:
        """Call PayPal create_order for a reserved CREATING order. Idempotent: the PayPal
        request id is our order id, so a replay returns the same PayPal order."""
        o = self.store.get("orders", oid)
        try:
            created = self.paypal.create_order(money(o["amount"]), o["currency"],
                                               f"{o['quantity']} x {o['product_name']}", oid,
                                               items=[{"sku": o["product_id"], "quantity": o["quantity"]}])
        except PayPalError as e:
            if e.ambiguous:
                return self._uncertain(oid, ("CREATING",), f"create outcome unknown: {e}")
            self._transition(oid, ("CREATING",), "FAILED", release=True, paypal_status=str(e)[:200])
            return {**self.store.get("orders", oid), "error": str(e)}
        except Exception as e:  # unexpected: treat as unknown outcome, keep the reservation
            return self._uncertain(oid, ("CREATING",), f"create outcome unknown: {type(e).__name__}: {e}")
        self._transition(oid, ("CREATING",), "PAYER_ACTION_REQUIRED", paypal_order_id=created["id"],
                         paypal_status=created["status"], approve_url=created.get("approve_url"))
        if created["status"] == "APPROVED":  # mock auto-approve
            return self._capture(oid)
        return self.store.get("orders", oid)

    def _uncertain(self, oid: str, from_statuses: tuple[str, ...], msg: str) -> dict[str, Any]:
        self._transition(oid, from_statuses, "NEEDS_RECONCILIATION", paypal_status=msg[:200])
        return {**self.store.get("orders", oid), "error": msg}

    # PayPal issues that mean "the buyer has not approved yet": not fatal, keep the order open.
    NOT_YET_APPROVED = ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED")

    def _capture(self, oid: str, from_statuses: tuple[str, ...] = ("PAYER_ACTION_REQUIRED",)) -> dict[str, Any]:
        if not self._transition(oid, from_statuses, "CAPTURING"):
            o = self.store.get("orders", oid)
            if o["status"] == "COMPLETED":
                return {**o, "result": "ALREADY_CAPTURED"}
            if o["status"] == "CAPTURING":
                return {**o, "result": "CAPTURE_IN_PROGRESS"}
            raise ValueError(f"order {oid} is {o['status']}, cannot capture")
        o = self.store.get("orders", oid)
        try:
            cap = self.paypal.capture_order(o["paypal_order_id"])
        except PayPalError as e:
            issue = getattr(e, "issue", None) or ""
            if issue in self.NOT_YET_APPROVED or any(t in str(e) for t in self.NOT_YET_APPROVED):
                self._transition(oid, ("CAPTURING",), "PAYER_ACTION_REQUIRED")
                return {**self.store.get("orders", oid), "result": "NOT_APPROVED_YET", "error": str(e),
                        "paypal_issue": issue or None}
            if e.ambiguous:
                return self._after_uncertain_capture(oid, str(e), issue)
            self._transition(oid, ("CAPTURING",), "CAPTURE_FAILED", release=True, paypal_status=str(e)[:200])
            return {**self.store.get("orders", oid), "error": str(e), "paypal_issue": issue or None}
        except Exception as e:  # anything else: the capture may have happened
            return self._after_uncertain_capture(oid, f"{type(e).__name__}: {e}", None)
        return self._apply_capture(oid, cap, ("CAPTURING",))

    def _after_uncertain_capture(self, oid: str, msg: str, issue: str | None) -> dict[str, Any]:
        """Keep the reservation, then ask PayPal once what actually happened."""
        self._transition(oid, ("CAPTURING",), "NEEDS_RECONCILIATION",
                         paypal_status=f"capture outcome unknown: {msg}"[:200])
        out = self._reconcile_once(oid, ("NEEDS_RECONCILIATION",), allow_capture=False)
        return {**out, "error": msg, "paypal_issue": issue or None}

    def _apply_capture(self, oid: str, cap: dict[str, Any], from_statuses: tuple[str, ...]) -> dict[str, Any]:
        """Only a COMPLETED order with a COMPLETED capture counts as COMPLETED."""
        st, cap_status = cap.get("status"), cap.get("capture_status")
        paypal_status = f"{st}" + (f"/{cap_status}" if cap_status and cap_status != st else "")
        if st == "COMPLETED" and cap_status == "COMPLETED":
            to, release = "COMPLETED", False
        elif cap_status in ("DECLINED", "FAILED") or st == "VOIDED":
            to, release = "CAPTURE_FAILED", True
        else:  # capture PENDING, or no capture status: money may move later
            to, release = "NEEDS_RECONCILIATION", False
            paypal_status = f"{st}/{cap_status or 'NO_CAPTURE_STATUS'}"
        self._transition(oid, from_statuses, to, release=release, paypal_status=paypal_status,
                         capture_id=cap.get("capture_id"))
        return self.store.get("orders", oid)

    # ---- reconciliation -------------------------------------------------
    def _reconcile_once(self, oid: str, from_statuses: tuple[str, ...], allow_capture: bool) -> dict[str, Any]:
        o = self.store.get("orders", oid)
        if not o.get("paypal_order_id"):
            # create outcome unknown: replay create with the same idempotency key
            if not self._transition(oid, from_statuses, "CREATING"):
                return {**self.store.get("orders", oid), "result": "STATE_CHANGED"}
            out = self._create_at_provider(oid)
            return {**out, "result": self._result(out)}
        try:
            live = self.paypal.get_order(o["paypal_order_id"])
        except (PayPalError, NotImplementedError) as e:
            return {**o, "result": "STILL_UNCONFIRMED", "error": str(e)}
        st = live.get("status")
        if st == "COMPLETED":
            out = self._apply_capture(oid, live, from_statuses)
        elif st == "APPROVED" and allow_capture:
            out = self._capture(oid, from_statuses)
        elif st in ("CREATED", "SAVED", "PAYER_ACTION_REQUIRED"):
            self._transition(oid, from_statuses, "PAYER_ACTION_REQUIRED", paypal_status=st)
            out = self.store.get("orders", oid)
        elif st == "VOIDED":
            self._transition(oid, from_statuses, "CANCELLED", release=True, paypal_status=st)
            out = self.store.get("orders", oid)
        else:
            out = {**self.store.get("orders", oid), "paypal_live_status": st}
        out = dict(out)
        out.setdefault("result", self._result(out))
        return out

    def reconcile(self, oid: str) -> dict[str, Any]:
        """Resolve an order whose payment outcome is unknown from PayPal's order status.
        Also picks up CREATING/CAPTURING orders stuck for STUCK_AFTER (e.g. a crash)."""
        o = self.store.get("orders", oid)
        if o is None:
            raise KeyError(oid)
        stuck_cutoff = self._ts(self._clock() - STUCK_AFTER)
        if o["status"] == "NEEDS_RECONCILIATION" or (
                o["status"] in ("CREATING", "CAPTURING") and (o.get("updated_at") or "") < stuck_cutoff):
            return self._reconcile_once(oid, (o["status"],), allow_capture=True)
        return {**o, "result": "NOTHING_TO_RECONCILE"}

    # ---- order status / manual capture / cancel by our order id -------
    def get_order(self, oid: str, refresh: bool = False) -> dict[str, Any]:
        """Our order record; with refresh=True also fetch the live status from PayPal."""
        self.expire_stale()
        o = self.store.get("orders", oid)
        if o is None:
            raise KeyError(oid)
        if refresh and o.get("paypal_order_id"):
            try:
                live = self.paypal.get_order(o["paypal_order_id"])
            except (PayPalError, NotImplementedError) as e:
                o["refresh_error"] = str(e)
                return o
            o["paypal_live_status"] = live["status"]
        return o

    def capture(self, oid: str) -> dict[str, Any]:
        """Try to capture an open order (e.g. buyer approved but the redirect was lost).
        Returns result=NOT_APPROVED_YET without changing anything if the buyer has not approved."""
        self.expire_stale()
        o = self.store.get("orders", oid)
        if o is None:
            raise KeyError(oid)
        if o["status"] == "COMPLETED":
            return {**o, "result": "ALREADY_CAPTURED"}
        if o["status"] == "CAPTURING":
            return {**o, "result": "CAPTURE_IN_PROGRESS"}
        if o["status"] == "NEEDS_RECONCILIATION":
            return self.reconcile(oid)
        if o["status"] != "PAYER_ACTION_REQUIRED":
            raise ValueError(f"order {oid} is {o['status']}, cannot capture")
        out = self._capture(oid)
        out.setdefault("result", "CAPTURED" if out["status"] == "COMPLETED" else out["status"])
        return out

    def cancel(self, oid: str) -> dict[str, Any]:
        """Cancel an order that is still waiting for the buyer, and release its reservation,
        atomically. Orders being captured, captured, or of unknown outcome are never
        cancelled here (result=NOT_CANCELLABLE). PayPal Orders v2 has no cancel call for
        uncaptured orders; an unapproved PayPal order simply expires, and our status check
        refuses to capture a CANCELLED order."""
        if self.store.get("orders", oid) is None:
            raise KeyError(oid)
        if self._transition(oid, ("PAYER_ACTION_REQUIRED",), "CANCELLED", release=True):
            return {**self.store.get("orders", oid), "result": "CANCELLED"}
        o = self.store.get("orders", oid)
        return {**o, "result": "ALREADY_CANCELLED" if o["status"] == "CANCELLED" else "NOT_CANCELLABLE"}

    # ---- PayPal buyer-approval round trip --------------------------------
    def _by_paypal_id(self, paypal_order_id: str) -> dict[str, Any]:
        o = self.store.find("orders", paypal_order_id=paypal_order_id)
        if o is None:
            raise KeyError(paypal_order_id)
        return o

    def complete_payment(self, paypal_order_id: str) -> dict[str, Any]:
        """Buyer returned from PayPal after approving: capture (idempotent)."""
        return self.capture(self._by_paypal_id(paypal_order_id)["id"])

    def cancel_payment(self, paypal_order_id: str) -> dict[str, Any]:
        """Buyer cancelled on PayPal: release the reserved spend if still awaiting approval."""
        return self.cancel(self._by_paypal_id(paypal_order_id)["id"])

    def expire_stale(self) -> list[str]:
        """Expire orders the buyer never approved within approval_ttl (atomically; an
        order already moved to CAPTURING is never expired)."""
        cutoff = self._ts(self._clock() - self.approval_ttl)
        q = "SELECT id FROM orders WHERE status='PAYER_ACTION_REQUIRED' AND created_at < ?"
        with self.store.lock:
            if not self.store.conn.execute(q, (cutoff,)).fetchone():
                return []
        expired = []
        with self.store.tx() as c:
            for (oid,) in c.execute(q, (cutoff,)).fetchall():
                cur = c.execute("UPDATE orders SET status='EXPIRED', updated_at=? "
                                "WHERE id=? AND status='PAYER_ACTION_REQUIRED'", (self._ts(), oid))
                if cur.rowcount == 1:
                    c.execute("DELETE FROM spend WHERE order_ref=?", (oid,))
                    expired.append(oid)
        return expired

    def invalidate_lost_mock_orders(self) -> list[str]:
        """MockPayPal keeps its orders in memory. After a restart, open orders that point
        at a mock order this process does not know can never be approved or captured:
        mark them MOCK_RESET and release their reservations (B9)."""
        if not isinstance(self.paypal, MockPayPal):
            return []
        lost = []
        for st in ("PAYER_ACTION_REQUIRED", "CAPTURING", "NEEDS_RECONCILIATION"):
            for o in self.store.list("orders", st):
                pid = o.get("paypal_order_id")
                if pid and pid.startswith("MOCK-") and not self.paypal.known(pid):
                    if self._transition(o["id"], (st,), "MOCK_RESET", release=True,
                                        paypal_status="mock order lost on restart"):
                        lost.append(o["id"])
        return lost

    def spend_summary(self) -> dict[str, Any]:
        """Today's spend split into captured vs reserved, against the caps."""
        self.expire_stale()
        day, cur = self._today(), self.policy.currency
        captured = reserved = Decimal("0")
        unconfirmed = 0
        for d, cents, c, ref in self.ledger.entries():
            if d != day or c != cur:
                continue
            amt = Decimal(cents) / 100
            o = self.store.get("orders", ref) if ref else None
            if o and o["status"] in RESERVED_STATUSES:
                reserved += amt
                unconfirmed += o["status"] == "NEEDS_RECONCILIATION"
            else:
                captured += amt
        total = captured + reserved
        cap = self.policy.daily_cap
        return {"day": day, "currency": cur, "captured": str(money(captured)),
                "reserved": str(money(reserved)), "total": str(money(total)),
                "daily_cap": str(cap), "remaining": str(money(max(cap - total, Decimal("0")))),
                "per_order_cap": str(self.policy.per_order_cap),
                "hard_max_per_order": str(self.policy.hard_max_per_order),
                "needs_reconciliation": unconfirmed}

    @staticmethod
    def _result(order: dict[str, Any]) -> str:
        st = order["status"]
        if st == "PAYER_ACTION_REQUIRED":
            return "AWAITING_PAYER_APPROVAL"
        if st == "COMPLETED":
            return "ORDER_PLACED"
        if st in ("CREATING", "CAPTURING", "NEEDS_RECONCILIATION"):
            return "PAYMENT_UNCONFIRMED"
        return "FAILED"

    def place_order(self, product_id: str, quantity: int = 1, request_id: str | None = None) -> dict[str, Any]:
        p, req = self._line(product_id, quantity)
        with self.store.tx():  # check + reserve atomically, before any PayPal I/O (B1)
            d = self._evaluate(p, req, request_id)
            if d.verdict == Verdict.DENY:
                return {"result": "DENIED", **d.to_dict()}
            if d.verdict == Verdict.NEEDS_APPROVAL:
                return {"result": "NEEDS_APPROVAL", **d.to_dict(),
                        "next_step": "call request_approval with a justification; a human must approve"}
            oid = self._reserve(p, req, quantity, d, request_id=request_id)
        order = self._create_at_provider(oid)
        return {"result": self._result(order), "order": order, **d.to_dict()}

    def request_approval(self, product_id: str, quantity: int, justification: str,
                         request_id: str | None = None) -> dict[str, Any]:
        p, req = self._line(product_id, quantity)
        d = self._evaluate(p, req, request_id)
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
            "created_at": self._ts(), "decided_at": None, "request_id": request_id})
        return {"result": "APPROVAL_REQUESTED", "approval_id": aid, **d.to_dict()}

    def approve(self, approval_id: str) -> dict[str, Any]:
        """Human approval. Claims the approval exactly once (PENDING -> EXECUTING) in the
        same transaction that creates its single order and reservation. A repeated call
        returns the existing order. A changed quote (B5) or a deny rule refuses."""
        with self.store.tx() as c:
            a = self.store.get("approvals", approval_id)
            if a is None:
                raise KeyError(approval_id)
            if a["status"] != "PENDING":
                if a.get("order_id"):
                    o = self.store.get("orders", a["order_id"])
                    return {"result": self._result(o), "order": o, "replayed": True}
                raise ValueError(f"approval is already {a['status']}")
            approved = self._quote(a["product_id"], a["quantity"], a["amount"], a["currency"],
                                   a["merchant"], a["category"])
            p = self.catalog.get(a["product_id"])
            current = None
            if p is not None:
                current = self._quote(p["id"], a["quantity"], money(money(p["price"]) * int(a["quantity"])),
                                      p.get("currency", "USD"), p["merchant"], p["category"])
            if current != approved:
                c.execute("UPDATE approvals SET status='QUOTE_CHANGED', decided_at=? WHERE id=? AND status='PENDING'",
                          (self._ts(), approval_id))
                return {"result": "QUOTE_CHANGED", "approval_id": approval_id, "approved_quote": approved,
                        "current_quote": current,
                        "note": "the product's terms changed since approval was requested; nothing was "
                                "bought. Request a fresh approval for the current quote."}
            p, req = self._line(a["product_id"], int(a["quantity"]))
            d = hard_rules_only(self.policy, req)  # policy may have changed since the request
            if d.verdict == Verdict.DENY:
                c.execute("UPDATE approvals SET status='DENIED_BY_POLICY', decided_at=? WHERE id=? AND status='PENDING'",
                          (self._ts(), approval_id))
                return {"result": "DENIED", **d.to_dict()}
            claim = c.execute("UPDATE approvals SET status='EXECUTING', decided_at=? WHERE id=? AND status='PENDING'",
                              (self._ts(), approval_id))
            if claim.rowcount != 1:  # pragma: no cover - impossible inside the write transaction
                raise ValueError("approval was claimed concurrently")
            oid = self._reserve(p, req, int(a["quantity"]), d, approval_id=approval_id,
                                request_id=a.get("request_id"))
            c.execute("UPDATE approvals SET order_id=? WHERE id=?", (oid, approval_id))
        order = self._create_at_provider(oid)
        with self.store.tx() as c:
            c.execute("UPDATE approvals SET status='APPROVED' WHERE id=? AND status='EXECUTING'", (approval_id,))
        return {"result": self._result(order), "order": order}

    def reject(self, approval_id: str) -> dict[str, Any]:
        with self.store.tx() as c:
            a = self.store.get("approvals", approval_id)
            if a is None:
                raise KeyError(approval_id)
            cur = c.execute("UPDATE approvals SET status='REJECTED', decided_at=? WHERE id=? AND status='PENDING'",
                            (self._ts(), approval_id))
            if cur.rowcount != 1:
                raise ValueError(f"approval is already {a['status']}")
        return {"result": "REJECTED", "approval_id": approval_id}
