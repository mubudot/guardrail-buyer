"""Fix round 7 (docs/CODEX_REVIEW_4.md): versioned atomic migrations with a corrective
capture_unknown re-backfill (N1), mock ownership keyed on the canonical DB identity (N2),
and the bounded / paged audit API (N5). The dashboard parts of N3-N5 are exercised by
tests/test_ui_jsdom.py (real AG Grid in jsdom)."""
import os
import sqlite3
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.offline_agent import OfflineAgent
from app.paypal_client import MockPayPal
from app.service import BuyerService, MockLockError, Store

from test_review3_fixes import LEGACY_SCHEMA, _legacy_db

ROOT = Path(__file__).resolve().parent.parent

# What round 5 (fe14faa) did to an old DB: add both columns with DEFAULT 0, no backfill.
ROUND5_ALTER = ("ALTER TABLE orders ADD COLUMN op_gen INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE orders ADD COLUMN capture_unknown INTEGER NOT NULL DEFAULT 0")
# Plus what round 6 (9709fba) added on top (audit tables, meta), still without user_version.
ROUND6_DDL = (
    "CREATE TABLE decisions (id TEXT PRIMARY KEY, created_at TEXT, stage TEXT, request_id TEXT, "
    "product_id TEXT, product_name TEXT, merchant TEXT, category TEXT, quantity INTEGER, amount TEXT, "
    "currency TEXT, verdict TEXT, outcome TEXT, rule_hit TEXT, reasons TEXT, rules TEXT, spent_before TEXT, "
    "order_id TEXT, approval_id TEXT)",
    "CREATE INDEX decisions_created ON decisions(created_at)",
    "CREATE TABLE order_events (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, at TEXT, "
    "from_status TEXT, to_status TEXT, op_gen INTEGER, note TEXT)",
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
)


def _pre_migrated(path: Path, rows, extra=()) -> None:
    _legacy_db(path, rows)
    c = sqlite3.connect(path)
    for stmt in (*ROUND5_ALTER, *extra):
        c.execute(stmt)
    c.commit()
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0  # rounds <= 6 never set it
    c.close()


def _uv(path) -> int:
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def _flags(path) -> dict:
    c = sqlite3.connect(path)
    try:
        return dict(c.execute("SELECT id, capture_unknown FROM orders").fetchall())
    finally:
        c.close()


# ---- N1: versioned, atomic, corrective migrations ---------------------------------
def test_fresh_db_is_at_current_schema_version(tmp_path):
    s = Store(str(tmp_path / "fresh.db"))
    assert s.schema_version() == Store.SCHEMA_VERSION == 4
    s.conn.close()
    assert Store(":memory:").schema_version() == 4


@pytest.mark.parametrize("extra", [(), ROUND6_DDL], ids=["round5-migrated", "round6-migrated"])
def test_previously_migrated_db_gets_corrective_backfill(policy, catalog, clock, tmp_path, extra):
    """CODEX_REVIEW_4 N1 repro 1: a DB already opened by round 5 (column present, flag 0)
    used to skip the backfill -> reconcile to PAYER_ACTION_REQUIRED, cancel, $49 released.
    Now the corrective v3 migration marks it; it stays reserved until the provider resolves it."""
    db = tmp_path / "prior.db"
    pp = MockPayPal(require_payer_approval=True)
    created = pp.create_order(Decimal("49.00"), "USD", "2 x Wireless Mouse", "ORD-LEGACY")
    _pre_migrated(db, [("ORD-LEGACY", "NEEDS_RECONCILIATION", created["id"]),
                       ("ORD-CAPT", "CAPTURING", "MOCK-B"), ("ORD-AWAIT", "PAYER_ACTION_REQUIRED", "MOCK-C"),
                       ("ORD-DONE", "COMPLETED", "MOCK-D")], extra)
    assert _flags(db)["ORD-LEGACY"] == 0
    svc = BuyerService(policy, catalog, pp, str(db), clock=clock)
    assert svc.store.schema_version() == 4
    assert _flags(db) == {"ORD-LEGACY": 1, "ORD-CAPT": 1, "ORD-AWAIT": 0, "ORD-DONE": 0}
    out = svc.reconcile("ORD-LEGACY")
    assert out["status"] == "NEEDS_RECONCILIATION" and out.get("paypal_live_status") == "PAYER_ACTION_REQUIRED"
    assert svc.cancel("ORD-LEGACY")["result"] == "NOT_CANCELLABLE"
    assert svc.spent_today() == Decimal("196.00")  # 4 x $49: nothing released
    svc.store.conn.close()


def _crash_open(db: Path, needle: str) -> subprocess.CompletedProcess:
    """Open the DB with the current Store in a child that dies (exit 77, no cleanup) the
    moment SQLite starts a statement containing `needle`."""
    code = (
        "import os, sqlite3, sys\n"
        "real = sqlite3.connect\n"
        "def connect(*a, **k):\n"
        "    c = real(*a, **k)\n"
        "    c.set_trace_callback(lambda s: os._exit(77) if sys.argv[2] in s else None)\n"
        "    return c\n"
        "sqlite3.connect = connect\n"
        "from app.service import Store\n"
        "Store(sys.argv[1])\n"
        "print('NOT REACHED')\n")
    return subprocess.run([sys.executable, "-c", code, str(db), needle], cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": str(ROOT), "GRB_NO_AUTOAPP": "1"},
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("needle,version_after_crash", [
    ("UPDATE orders SET capture_unknown", 1),          # crash right before the v2 backfill
    ("ADD COLUMN capture_unknown", 1),                 # crash between the two v2 ALTERs
    ("CREATE TABLE IF NOT EXISTS decisions", 3),       # crash inside v4 (after v2/v3 committed)
])
def test_interrupted_upgrade_is_resumed(policy, catalog, clock, tmp_path, needle, version_after_crash):
    """CODEX_REVIEW_4 N1 repro 2: the round-6 ALTER was autocommitted, so a crash before the
    backfill left flag 0 forever. Now ALTER + backfill + version bump are one transaction:
    after the crash nothing of that migration is visible and the next open redoes it."""
    db = tmp_path / "legacy.db"
    pp = MockPayPal(require_payer_approval=True)
    created = pp.create_order(Decimal("49.00"), "USD", "2 x Wireless Mouse", "ORD-LEGACY")
    _legacy_db(db, [("ORD-LEGACY", "NEEDS_RECONCILIATION", created["id"])])
    r = _crash_open(db, needle)
    assert r.returncode == 77, r.stdout + r.stderr
    assert _uv(db) == version_after_crash
    c = sqlite3.connect(db)
    cols = {row[1] for row in c.execute("PRAGMA table_info(orders)")}
    c.close()
    if version_after_crash < 2:  # the half-done v2 was rolled back entirely, no lone ALTER survived
        assert "op_gen" not in cols and "capture_unknown" not in cols
    svc = BuyerService(policy, catalog, pp, str(db), clock=clock)
    assert svc.store.schema_version() == 4
    assert svc.store.get("orders", "ORD-LEGACY")["capture_unknown"] == 1
    svc.reconcile("ORD-LEGACY")
    assert svc.cancel("ORD-LEGACY")["result"] == "NOT_CANCELLABLE"
    assert svc.spent_today() == Decimal("49.00")
    svc.store.conn.close()


def test_failed_migration_rolls_back_completely(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    _legacy_db(db, [("ORD-1", "NEEDS_RECONCILIATION", "MOCK-A")])

    def boom(self, c):
        c.execute("CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY)")
        raise RuntimeError("disk full")
    monkeypatch.setattr(Store, "_m4_audit", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        Store(str(db))
    assert _uv(db) == 3
    c = sqlite3.connect(db)
    assert c.execute("SELECT name FROM sqlite_master WHERE name='decisions'").fetchone() is None
    c.close()
    monkeypatch.undo()
    s = Store(str(db))
    assert s.schema_version() == 4 and s.get("orders", "ORD-1")["capture_unknown"] == 1


def test_migrations_run_once_not_on_every_restart(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db, [("ORD-1", "PAYER_ACTION_REQUIRED", "MOCK-A")])
    Store(str(db)).conn.close()
    s = Store(str(db))
    s.update("orders", "ORD-1", status="NEEDS_RECONCILIATION")  # e.g. a create-order 503 later on
    s.conn.close()
    assert Store(str(db)).get("orders", "ORD-1")["capture_unknown"] == 0


def test_concurrent_first_open_migrates_once(tmp_path):
    db = tmp_path / "legacy.db"
    _pre_migrated(db, [("ORD-1", "NEEDS_RECONCILIATION", "MOCK-A")])
    errors, stores = [], []

    def opener():
        try:
            stores.append(Store(str(db)))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)
    ts = [threading.Thread(target=opener) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and len(stores) == 6
    assert {s.schema_version() for s in stores} == {4}
    assert _flags(db) == {"ORD-1": 1}


def test_newer_schema_is_refused(tmp_path):
    db = tmp_path / "future.db"
    Store(str(db)).conn.close()
    c = sqlite3.connect(db)
    c.execute("PRAGMA user_version = 99")
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        Store(str(db))


# ---- N2: mock ownership keyed on the canonical DB identity ------------------------
def _cli(env_extra, *args):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "AGENT_MODE": "offline", "PAYPAL_MODE": "mock",
           "GRB_NO_DOTENV": "1", **env_extra}
    return subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="POSIX symlinks")
def test_cli_through_symlink_cannot_steal_mock_db(policy, catalog, tmp_path):
    """CODEX_REVIEW_4 N2 repro: alias.db -> owned.db. Before: the CLI locked alias.db.mock.lock,
    marked the server's awaiting $49 order MOCK_RESET and inserted its own order. Now: exit 3,
    nothing touched."""
    owned, alias = tmp_path / "owned.db", tmp_path / "links" / "alias.db"
    alias.parent.mkdir()
    server = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), str(owned))
    server.invalidate_lost_mock_orders()
    try:
        o = server.place_order("P-1002", 2)["order"]
        alias.symlink_to(owned)
        r = _cli({"CLI_DB_PATH": str(alias), "PAYPAL_MOCK_APPROVAL": "1"}, "a USB-C cable")
        assert r.returncode == 3, r.stdout + r.stderr
        assert "single-process" in r.stderr and str(owned.resolve()) in r.stderr
        assert server.store.get("orders", o["id"])["status"] == "PAYER_ACTION_REQUIRED"
        assert [x["id"] for x in server.store.list("orders")] == [o["id"]]
        assert server.spent_today() == Decimal("49.00")
        assert not (alias.parent / "alias.db.mock.lock").exists()  # lock lives next to the real file
        assert (tmp_path / "owned.db.mock.lock").exists()
    finally:
        server.release_mock_lock()
        server.store.conn.close()
    r = _cli({"CLI_DB_PATH": str(alias)}, "a USB-C cable")  # server gone: the alias may own it now
    assert r.returncode == 0, r.stderr


def test_relative_and_dotdot_aliases_share_one_lock(policy, catalog, tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    a = BuyerService(policy, catalog, MockPayPal(), str(tmp_path / "x.db"))
    a.acquire_mock_lock()
    monkeypatch.chdir(tmp_path / "sub")
    b = BuyerService(policy, catalog, MockPayPal(), "../sub/../x.db")
    with pytest.raises(MockLockError, match="resolved from"):
        b.acquire_mock_lock()
    a.release_mock_lock()
    b.acquire_mock_lock()
    b.release_mock_lock()
    assert a.db_identity() == b.db_identity()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hard links")
def test_hard_linked_db_refused_in_mock_mode(policy, catalog, tmp_path):
    db = tmp_path / "x.db"
    s = BuyerService(policy, catalog, MockPayPal(), str(db))
    os.link(db, tmp_path / "y.db")
    with pytest.raises(MockLockError, match="hard links"):
        s.acquire_mock_lock()


# ---- N5: bounded, paged, compact audit API ----------------------------------------
def _seed_decisions(svc, n, ts="2026-10-30T10:00:00+00:00", request_id="REQ-BULK", product="P-1001"):
    with svc.store.tx() as c:
        c.executemany(
            "INSERT INTO decisions (id, created_at, stage, request_id, product_id, product_name, merchant, "
            "category, quantity, amount, currency, verdict, outcome, rule_hit, reasons, rules, spent_before) "
            "VALUES (?, ?, 'check', ?, ?, 'USB-C Cable', 'Sandbox Electronics', 'office-supplies', 1, "
            "'12.99', 'USD', 'ALLOW', 'CHECKED', '', '[]', '[{\"rule\": \"currency\", \"outcome\": \"PASS\"}]', '0')",
            [(f"DEC-B{i:06d}", ts, request_id, product) for i in range(n)])


def _client(service):
    return TestClient(create_app(service, agent=OfflineAgent(service), approver_token="t"))


def test_decisions_page_reports_total_and_pages_with_cursor(service):
    _seed_decisions(service, 1003)  # identical timestamps: the cursor must tie-break on rowid
    c = _client(service)
    p1 = c.get("/decisions").json()
    assert p1["total"] == 1003 and len(p1["items"]) == 1000 and p1["has_more"] and p1["limit"] == 1000
    assert p1["items"][0]["id"] == "DEC-B001002"  # newest (highest rowid) first
    p2 = c.get(f"/decisions?before_id={p1['next_before_id']}").json()
    assert [r["id"] for r in p2["items"]] == ["DEC-B000002", "DEC-B000001", "DEC-B000000"]
    assert not p2["has_more"] and p2["next_before_id"] is None and p2["total"] == 1003
    ids = [r["id"] for r in p1["items"] + p2["items"]]
    assert len(set(ids)) == 1003  # no gap, no duplicate across pages
    # small pages, cursor stable while newer rows arrive
    q1 = c.get("/decisions?limit=2").json()
    service.place_order("P-2001", 1)
    q2 = c.get(f"/decisions?limit=2&before_id={q1['next_before_id']}").json()
    assert [r["id"] for r in q2["items"]] == ["DEC-B001000", "DEC-B000999"]
    assert q2["total"] == 1004
    assert c.get("/decisions?before_id=DEC-NOPE").status_code == 404


def test_older_decision_is_reachable_beyond_first_window(service):
    """CODEX_REVIEW_4 N5 repro: an old persisted decision beyond the newest 1000 was
    unreachable. Paging now reaches it."""
    service.place_order("P-5001", 1)  # the old, interesting row (a deny)
    old = service.list_decisions(1)[0]["id"]
    with service.store.tx() as c:
        c.execute("UPDATE decisions SET created_at='2026-10-29T00:00:00+00:00' WHERE id=?", (old,))
    _seed_decisions(service, 1001)
    c = _client(service)
    seen, cursor = [], None
    while True:
        page = c.get("/decisions" + (f"?before_id={cursor}" if cursor else "")).json()
        seen += [r["id"] for r in page["items"]]
        if not page["has_more"]:
            break
        cursor = page["next_before_id"]
    assert old not in seen[:1000] and old in seen and len(seen) == 1002


def test_list_is_compact_detail_is_full(service):
    service.place_order("P-1001", 1)
    c = _client(service)
    row = c.get("/decisions").json()["items"][0]
    assert "rules" not in row and "spent_before" not in row
    assert {"id", "verdict", "rule_hit", "amount", "merchant", "risk", "order_status", "reasons"} <= set(row)
    d = c.get(f"/decisions/{row['id']}").json()
    assert d["rules"] and d["decision"]["spent_before"] is not None and d["as_of"]


def test_related_decisions_bounded_and_indexed(service):
    service.place_order("P-1001", 1, request_id="REQ-BULK")
    _seed_decisions(service, 120)
    did = service.list_decisions(1)[0]["id"]
    d = service.decision_detail(did)
    assert len(d["related_decisions"]) == service.RELATED_MAX == 50
    assert d["related_total"] == 121
    plan = " ".join(r[3] for r in service.store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM decisions WHERE request_id=? AND product_id=? "
        "ORDER BY created_at, rowid LIMIT 50", ("REQ-BULK", "P-1001")))
    assert "decisions_req" in plan
