"""SQLite persistence for orders, approvals, decisions and the spend ledger: versioned schema
migrations, BEGIN IMMEDIATE write transactions and small row helpers. Business rules live in
BuyerService (service.py); this module stores rows and names the order statuses."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

from datetime import timedelta

from .guardrails import SpendLedger

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


class Store:
    """SQLite store for orders and approvals. One connection in autocommit mode;
    `tx()` opens an explicit BEGIN IMMEDIATE write transaction (serialized across
    threads by the RLock and across processes by SQLite's write lock)."""

    def __init__(self, db_path: str = ":memory:"):
        self.lock = threading.RLock()
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self._enable_wal()
        self.migrate()

    def _enable_wal(self, timeout: float = 30.0) -> None:
        """Switching a rollback-journal DB to WAL needs a moment without other connections, and
        SQLite does not apply the busy timeout to it: several processes/threads opening the same
        legacy DB at once get an immediate "database is locked". Retry (up to the busy timeout) instead."""
        deadline = time.monotonic() + timeout          # same budget as the connection's busy timeout
        while True:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    # ---- versioned schema migrations (fix round 7, CODEX_REVIEW_4 N1) ----------
    # The schema version lives in the SQLite header (PRAGMA user_version), which is
    # part of the database file and changes transactionally. Every migration runs in
    # ONE explicit BEGIN IMMEDIATE ... COMMIT that contains its DDL (ALTER/CREATE), its
    # data backfill AND the version bump, so a crash at any point leaves the DB at the
    # previous version with none of the migration applied; the next open re-runs it.
    # Versions written by rounds <= 6 never set user_version, so they are all version 0
    # and get every migration below, including the corrective re-backfill (v3).
    # Python's executescript() commits implicitly, so migrations use execute() only.
    @staticmethod
    def _cols(c, table: str) -> set[str]:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

    def _m1_base(self, c) -> None:
        """Base tables plus the columns added in rounds 1-4 (idempotent on old DBs)."""
        c.execute("""CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, product_id TEXT, product_name TEXT, merchant TEXT,
            category TEXT, quantity INTEGER, amount TEXT, currency TEXT,
            status TEXT, paypal_order_id TEXT, paypal_status TEXT, capture_id TEXT,
            approval_id TEXT, decision TEXT, created_at TEXT, approve_url TEXT,
            updated_at TEXT, request_id TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY, product_id TEXT, product_name TEXT, merchant TEXT,
            category TEXT, quantity INTEGER, amount TEXT, currency TEXT,
            justification TEXT, reasons TEXT, status TEXT, order_id TEXT,
            created_at TEXT, decided_at TEXT, request_id TEXT)""")
        c.execute(SpendLedger.SCHEMA)
        c.execute("CREATE INDEX IF NOT EXISTS spend_ref ON spend(order_ref)")
        for table, wanted in (("orders", ("approve_url", "updated_at", "request_id")),
                              ("approvals", ("request_id",))):
            cols = self._cols(c, table)
            for col in wanted:
                if col not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS orders_paypal ON orders(paypal_order_id)")
        c.execute("CREATE INDEX IF NOT EXISTS orders_req ON orders(request_id, product_id)")
        try:  # one order per approval (B2). Old DBs that already hold duplicates keep working.
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS orders_one_per_approval "
                      "ON orders(approval_id) WHERE approval_id IS NOT NULL")
        except sqlite3.IntegrityError:  # pragma: no cover - legacy data only
            pass

    _OPEN_UNCERTAIN = ("UPDATE orders SET capture_unknown=1 "
                       "WHERE status IN ('NEEDS_RECONCILIATION', 'CAPTURING') AND capture_unknown=0")

    def _m2_recovery_columns(self, c) -> None:
        """Round 5: per-order operation generation + "a capture may have happened" flag.
        ALTER and backfill are one transaction (with the version bump): a DB written
        without the flag cannot tell whether an unresolved order ever had an ambiguous
        capture, so every order open in NEEDS_RECONCILIATION / CAPTURING is treated as
        "a capture may have happened" -- it can never return to PAYER_ACTION_REQUIRED,
        be cancelled or expire, only be resolved from the provider."""
        cols = self._cols(c, "orders")
        for col in ("op_gen", "capture_unknown"):
            if col not in cols:
                c.execute(f"ALTER TABLE orders ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        if "capture_unknown" not in cols:
            c.execute(self._OPEN_UNCERTAIN)

    def _m3_capture_unknown_corrective(self, c) -> None:
        """Corrective re-backfill (CODEX_REVIEW_4 N1) for DBs that already had the column
        when v2 ran, i.e. DBs migrated by rounds 5/6 outside this version scheme: round 5
        added the flag with no backfill at all, and round 6 skipped its backfill whenever
        the column existed or the ALTER had been autocommitted before a crash. Their open
        NEEDS_RECONCILIATION / CAPTURING orders get capture_unknown=1 now. Version-gated:
        runs exactly once per DB, never on later restarts."""
        c.execute(self._OPEN_UNCERTAIN)

    def _m4_audit(self, c) -> None:
        """Round 6 audit log + order timeline; round 7 index for the bounded related query."""
        c.execute("""CREATE TABLE IF NOT EXISTS decisions (
            id TEXT PRIMARY KEY, created_at TEXT, stage TEXT, request_id TEXT,
            product_id TEXT, product_name TEXT, merchant TEXT, category TEXT,
            quantity INTEGER, amount TEXT, currency TEXT, verdict TEXT, outcome TEXT,
            rule_hit TEXT, reasons TEXT, rules TEXT, spent_before TEXT,
            order_id TEXT, approval_id TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS decisions_created ON decisions(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS decisions_order ON decisions(order_id)")
        c.execute("CREATE INDEX IF NOT EXISTS decisions_req ON decisions(request_id, product_id, created_at)")
        c.execute("""CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, at TEXT,
            from_status TEXT, to_status TEXT, op_gen INTEGER, note TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS order_events_order ON order_events(order_id)")

    MIGRATIONS = ((1, "_m1_base"), (2, "_m2_recovery_columns"),
                  (3, "_m3_capture_unknown_corrective"), (4, "_m4_audit"))
    SCHEMA_VERSION = MIGRATIONS[-1][0]

    def schema_version(self) -> int:
        with self.lock:
            return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        """Apply every pending migration, each in its own atomic transaction. Safe with
        several processes opening the DB at once: the version is re-read under the
        write lock, so a migration another process already applied is skipped."""
        with self.lock:
            for version, name in self.MIGRATIONS:
                if self.schema_version() >= version:
                    continue
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    current = self.conn.execute("PRAGMA user_version").fetchone()[0]
                    if current < version:
                        getattr(self, name)(self.conn)
                        self.conn.execute(f"PRAGMA user_version = {int(version)}")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise
                self.conn.execute("COMMIT")
            v = self.schema_version()
            if v > self.SCHEMA_VERSION:
                raise RuntimeError(f"{self.db_path}: database schema version {v} is newer than this "
                                   f"code supports ({self.SCHEMA_VERSION}); refusing to run an old version on it")
            return v

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
        for k in ("reasons", "decision", "rules"):
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
