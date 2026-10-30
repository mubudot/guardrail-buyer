"""Read-only views for the dashboard and API: the guardrail audit trail (list, keyset pages,
detail with the payment timeline) and spend summaries against the caps. Mixed into
BuyerService (service.py); nothing here moves money."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .guardrails import money
from .store import RESERVED_STATUSES


class ReportingMixin:
    # ---- audit (fix round 6) ----------------------------------------------
    _DECISIONS_Q = ("SELECT d.*, COALESCE(o.status, ao.status) AS order_status, "
                    "COALESCE(d.order_id, a.order_id) AS linked_order_id, a.status AS approval_status "
                    "FROM decisions d LEFT JOIN approvals a ON a.id=d.approval_id "
                    "LEFT JOIN orders o ON o.id=d.order_id LEFT JOIN orders ao ON ao.id=a.order_id ")

    # compact list projection (CODEX_REVIEW_4 N5): the grid needs no per-rule detail, so
    # `rules` / `spent_before` stay out of the list and are loaded lazily by decision_detail
    _LIST_COLS = ("id", "created_at", "stage", "request_id", "product_id", "product_name", "merchant",
                  "category", "quantity", "amount", "currency", "verdict", "outcome", "rule_hit",
                  "reasons", "order_id", "approval_id")
    AUDIT_PAGE_MAX = 5000

    def _decision_rows(self, where: str, args: list[Any], limit: int, compact: bool) -> list[dict[str, Any]]:
        sel = ", ".join(f"d.{c}" for c in self._LIST_COLS) if compact else "d.*"
        q = self._DECISIONS_Q.replace("SELECT d.*", f"SELECT {sel}", 1)
        with self.store.lock:
            rows = self.store.conn.execute(
                q + where + "ORDER BY d.created_at DESC, d.rowid DESC LIMIT ?", [*args, limit]).fetchall()
        out = []
        for r in rows:
            d = self.store._decode(dict(r))
            d["risk"] = self._risk(d)
            out.append(d)
        return out

    def list_decisions(self, limit: int = 1000, decision_id: str | None = None) -> list[dict[str, Any]]:
        """Every guardrail decision, newest first, with the current status of the order /
        approval it led to (so the audit grid can highlight NEEDS_RECONCILIATION).
        Full rows (including every rule); the HTTP list uses decisions_page()."""
        self.expire_stale()
        limit = max(1, min(int(limit), self.AUDIT_PAGE_MAX))
        where, args = ("WHERE d.id=? ", [decision_id]) if decision_id else ("", [])
        return self._decision_rows(where, args, limit, compact=False)

    def decisions_page(self, limit: int = 1000, before_id: str | None = None) -> dict[str, Any]:
        """One page of the audit log for the dashboard / API (CODEX_REVIEW_4 N5): compact
        rows, newest first, at most `limit` (1..5000), strictly older than the decision
        `before_id` (keyset cursor on created_at, rowid: stable while new rows arrive).
        Returns the TOTAL number of decisions so a client can say "showing N of TOTAL",
        and `next_before_id` to fetch the next older page (None when exhausted)."""
        self.expire_stale()
        limit = max(1, min(int(limit), self.AUDIT_PAGE_MAX))
        where, args = "", []
        if before_id:
            with self.store.lock:
                cur = self.store.conn.execute("SELECT created_at, rowid FROM decisions WHERE id=?",
                                              (before_id,)).fetchone()
            if cur is None:
                raise KeyError(before_id)
            where = "WHERE (d.created_at < ? OR (d.created_at = ? AND d.rowid < ?)) "
            args = [cur[0], cur[0], cur[1]]
        rows = self._decision_rows(where, args, limit + 1, compact=True)
        has_more = len(rows) > limit
        rows = rows[:limit]
        with self.store.lock:
            total = self.store.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        return {"items": rows, "total": total, "limit": limit, "before_id": before_id,
                "has_more": has_more, "next_before_id": rows[-1]["id"] if has_more and rows else None}

    @staticmethod
    def _risk(d: dict[str, Any]) -> str:
        """The single highlight class of an audit row (most severe first)."""
        if d.get("order_status") == "NEEDS_RECONCILIATION":
            return "NEEDS_RECONCILIATION"
        if d.get("verdict") == "DENY":
            return "DENY"
        if d.get("verdict") == "NEEDS_APPROVAL":
            return "NEEDS_APPROVAL"
        return "ALLOW"

    def order_timeline(self, oid: str) -> list[dict[str, Any]]:
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT at, from_status, to_status, op_gen, note FROM order_events WHERE order_id=? "
                "ORDER BY id", (oid,)).fetchall()
        return [dict(r) for r in rows]

    def decision_detail(self, did: str) -> dict[str, Any]:
        """One decision with its quote, every rule evaluated, the approval it raised or
        executed, and the payment timeline (order status transitions) of its order."""
        rows = self.list_decisions(1, decision_id=did)
        if not rows:
            raise KeyError(did)
        d = rows[0]
        quote = self._quote(d["product_id"], d["quantity"], d["amount"], d["currency"],
                            d["merchant"], d["category"])
        approval = self.store.get("approvals", d["approval_id"]) if d.get("approval_id") else None
        oid = d.get("linked_order_id")
        order = self.store.get("orders", oid) if oid else None
        timeline = self.order_timeline(oid) if oid else []
        if order and not timeline:  # order written by a version without the timeline table
            timeline = [{"at": order["created_at"], "from_status": None, "to_status": "CREATING",
                         "op_gen": None, "note": "legacy order: only creation and current status known"}]
            if order["status"] != "CREATING":
                timeline.append({"at": order.get("updated_at"), "from_status": None,
                                 "to_status": order["status"], "op_gen": order.get("op_gen"),
                                 "note": order.get("paypal_status")})
        related, related_total = [], 0
        if d.get("request_id"):
            # bounded + indexed (decisions_req): the first RELATED_MAX decisions of this
            # request/product, plus the total so the panel can say what it omitted
            with self.store.lock:
                related = [dict(r) for r in self.store.conn.execute(
                    "SELECT id, created_at, stage, verdict, outcome, rule_hit FROM decisions "
                    "WHERE request_id=? AND product_id=? ORDER BY created_at, rowid LIMIT ?",
                    (d["request_id"], d["product_id"], self.RELATED_MAX)).fetchall()]
                related_total = self.store.conn.execute(
                    "SELECT COUNT(*) FROM decisions WHERE request_id=? AND product_id=?",
                    (d["request_id"], d["product_id"])).fetchone()[0]
        return {"decision": d, "quote": quote, "rules": d.get("rules") or [], "approval": approval,
                "order": order, "timeline": timeline, "related_decisions": related,
                "related_total": related_total, "as_of": self._ts()}

    RELATED_MAX = 50

    def spend_summary(self) -> dict[str, Any]:
        """Today's spend split into captured vs reserved, against the caps."""
        self.sync_policy()
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

    def spend_history(self, days: int = 14) -> dict[str, Any]:
        """Daily spend for the last `days` days (policy timezone, today last), split into
        captured and still-reserved amounts, with the current caps for reference."""
        self.sync_policy()
        self.expire_stale()
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        today = datetime.fromisoformat(self._today()).date()
        span = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        rows = {d: {"captured": Decimal("0"), "reserved": Decimal("0"), "orders": 0} for d in span}
        cur = self.policy.currency
        for d, cents, c, ref in self.ledger.entries():
            if d not in rows or c != cur:
                continue
            o = self.store.get("orders", ref) if ref else None
            key = "reserved" if o and o["status"] in RESERVED_STATUSES else "captured"
            rows[d][key] += Decimal(cents) / 100
            rows[d]["orders"] += 1
        return {"currency": cur, "daily_cap": str(self.policy.daily_cap),
                "per_order_cap": str(self.policy.per_order_cap),
                "days": [{"day": d, "captured": str(money(r["captured"])), "reserved": str(money(r["reserved"])),
                          "total": str(money(r["captured"] + r["reserved"])), "orders": r["orders"]}
                         for d, r in rows.items()]}
