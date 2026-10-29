"""Guardrail engine: decides whether an AI agent may spend money.

`evaluate()` is a pure function (no I/O): given a policy, a proposed order and
how much has already been spent today, it returns ALLOW / NEEDS_APPROVAL / DENY
plus human-readable reasons. `SpendLedger` persists daily spend in SQLite.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import yaml


def money(value: Any) -> Decimal:
    """Parse a value into a 2-decimal Decimal (never use floats for money)."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Verdict(str, Enum):
    ALLOW = "ALLOW"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    DENY = "DENY"


@dataclass(frozen=True)
class Policy:
    currency: str = "USD"
    per_order_cap: Decimal = money(50)          # above this -> human approval
    daily_cap: Decimal = money(150)             # cumulative/day above this -> human approval
    hard_max_per_order: Decimal = money(500)    # above this -> always DENY
    allowed_categories: tuple[str, ...] = ()    # empty = any category
    allowed_merchants: tuple[str, ...] = ()     # empty = any merchant
    blocked_merchants: tuple[str, ...] = ()
    timezone: str = "UTC"                       # day boundary for the daily cap

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Policy":
        def lower_tuple(key: str) -> tuple[str, ...]:
            return tuple(str(x).strip().lower() for x in (d.get(key) or []))

        p = cls(
            currency=str(d.get("currency", "USD")).upper(),
            per_order_cap=money(d.get("per_order_cap", 50)),
            daily_cap=money(d.get("daily_cap", 150)),
            hard_max_per_order=money(d.get("hard_max_per_order", 500)),
            allowed_categories=lower_tuple("allowed_categories"),
            allowed_merchants=lower_tuple("allowed_merchants"),
            blocked_merchants=lower_tuple("blocked_merchants"),
            timezone=str(d.get("timezone", "UTC")),
        )
        if p.per_order_cap <= 0 or p.daily_cap <= 0 or p.hard_max_per_order <= 0:
            raise ValueError("caps must be positive")
        if p.hard_max_per_order < p.per_order_cap:
            raise ValueError("hard_max_per_order must be >= per_order_cap")
        ZoneInfo(p.timezone)  # validate
        return p

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)
        return cls.from_dict(data or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "per_order_cap": str(self.per_order_cap),
            "daily_cap": str(self.daily_cap),
            "hard_max_per_order": str(self.hard_max_per_order),
            "allowed_categories": list(self.allowed_categories),
            "allowed_merchants": list(self.allowed_merchants),
            "blocked_merchants": list(self.blocked_merchants),
            "timezone": self.timezone,
        }

    def today(self, now: datetime | None = None) -> str:
        now = now or datetime.now(ZoneInfo("UTC"))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo("UTC"))
        return now.astimezone(ZoneInfo(self.timezone)).date().isoformat()


@dataclass(frozen=True)
class OrderRequest:
    merchant: str
    category: str
    amount: Decimal
    currency: str = "USD"
    description: str = ""


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict.value, "reasons": list(self.reasons)}


def evaluate(policy: Policy, order: OrderRequest, spent_today: Decimal = Decimal("0"),
             related_prior: Decimal = Decimal("0")) -> Decision:
    """Pure policy check. DENY reasons win over NEEDS_APPROVAL reasons.

    `related_prior` is the amount already ordered for the same SKU at the same merchant
    within the same agent request (anti-splitting). The per-purchase caps
    (per_order_cap, hard_max_per_order) apply to `amount + related_prior`; the daily
    cap is unaffected because those orders are already in `spent_today`."""
    deny: list[str] = []
    approve: list[str] = []
    amount = money(order.amount)
    spent = money(spent_today)
    prior = money(related_prior)
    purchase = amount + prior  # the whole purchase this order belongs to
    merchant = order.merchant.strip().lower()
    category = order.category.strip().lower()

    if order.currency.upper() != policy.currency:
        deny.append(f"currency {order.currency.upper()} not allowed (policy currency is {policy.currency})")
    if amount <= 0:
        deny.append("amount must be positive")
    if merchant in policy.blocked_merchants:
        deny.append(f"merchant '{order.merchant}' is blocked")
    elif policy.allowed_merchants and merchant not in policy.allowed_merchants:
        deny.append(f"merchant '{order.merchant}' is not on the allowlist")
    if policy.allowed_categories and category not in policy.allowed_categories:
        deny.append(f"category '{order.category}' is not on the allowlist")
    if amount > policy.hard_max_per_order:
        deny.append(f"amount {amount} exceeds hard maximum {policy.hard_max_per_order} {policy.currency}")
    elif prior > 0 and purchase > policy.hard_max_per_order:
        deny.append(f"same item in this request totals {purchase} (already {prior}), over hard maximum "
                    f"{policy.hard_max_per_order} {policy.currency}")

    if deny:
        return Decision(Verdict.DENY, tuple(deny))

    if amount > policy.per_order_cap:
        approve.append(f"amount {amount} exceeds per-order cap {policy.per_order_cap} {policy.currency}")
    elif prior > 0 and purchase > policy.per_order_cap:
        approve.append(f"same item from the same merchant in this request totals {purchase} "
                       f"(already ordered {prior}), over per-order cap {policy.per_order_cap} {policy.currency}; "
                       f"split orders count as one purchase")
    if spent + amount > policy.daily_cap:
        approve.append(
            f"daily spend would be {spent + amount} (already {spent}), over daily cap {policy.daily_cap} {policy.currency}"
        )
    if approve:
        return Decision(Verdict.NEEDS_APPROVAL, tuple(approve))
    return Decision(Verdict.ALLOW, (f"within caps: {amount} <= {policy.per_order_cap}, today {spent + amount} <= {policy.daily_cap}",))


def hard_rules_only(policy: Policy, order: OrderRequest,
                    related_prior: Decimal = Decimal("0")) -> Decision:
    """Used when a human approves: the soft caps (per-order cap, daily cap) are waived;
    every deny rule still applies, including the aggregate hard maximum over
    `related_prior` (same SKU/merchant already ordered in the same agent request)."""
    d = evaluate(policy, order, Decimal("0"), related_prior)
    if d.verdict == Verdict.DENY:
        return d
    return Decision(Verdict.ALLOW, ("approved by human; deny-rules re-checked",))


class SpendLedger:
    """Daily spend tracking backed by SQLite.

    One row per order reference. A row is a *reservation* while its order is open
    and *captured spend* once the order completes; it is deleted when the order is
    released (cancelled / expired / definitively failed). `BuyerService` shares its
    connection and lock with the ledger so the budget check and the reservation
    happen in one SQLite transaction."""

    SCHEMA = """CREATE TABLE IF NOT EXISTS spend (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   day TEXT NOT NULL,
                   amount_cents INTEGER NOT NULL,
                   currency TEXT NOT NULL,
                   order_ref TEXT,
                   created_at TEXT NOT NULL)"""

    def __init__(self, db_path: str = ":memory:", conn: sqlite3.Connection | None = None,
                 lock: "threading.RLock | None" = None):
        self._lock = lock or threading.RLock()
        if conn is None:
            conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level=None)
        self._conn = conn  # autocommit (isolation_level=None) unless inside BEGIN ... COMMIT
        with self._lock:
            self._conn.execute(self.SCHEMA)
            self._conn.execute("CREATE INDEX IF NOT EXISTS spend_ref ON spend(order_ref)")

    def record(self, day: str, amount: Decimal, currency: str, order_ref: str | None = None) -> None:
        cents = int(money(amount) * 100)
        with self._lock:
            self._conn.execute(
                "INSERT INTO spend(day, amount_cents, currency, order_ref, created_at) VALUES (?,?,?,?,?)",
                (day, cents, currency.upper(), order_ref, datetime.now(ZoneInfo("UTC")).isoformat()),
            )

    def release(self, order_ref: str) -> int:
        """Drop the spend reserved for an order (cancelled / expired / failed capture)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM spend WHERE order_ref=?", (order_ref,))
            return cur.rowcount

    def spent_on(self, day: str, currency: str) -> Decimal:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) FROM spend WHERE day=? AND currency=?",
                (day, currency.upper()),
            ).fetchone()
        return money(Decimal(row[0]) / 100)

    def entries(self) -> Iterable[tuple]:
        with self._lock:
            return list(self._conn.execute("SELECT day, amount_cents, currency, order_ref FROM spend ORDER BY id"))
