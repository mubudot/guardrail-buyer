"""Fix round 6 (docs/CODEX_REVIEW_3.md): legacy capture-uncertainty backfill, structured
offline parsing, CLI mock-DB ownership, rate-limit validation, and the decision audit log."""
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.guardrails import OrderRequest, evaluate, explain_rules
from app.main import ROOT, ConfigError, RateLimiter, create_app, parse_rate_limit
from app.offline_agent import GrammarError, OfflineAgent
from app.paypal_client import MockPayPal
from app.service import BuyerService, Store

# ---- 1. legacy DB upgrade: capture_unknown backfill --------------------------------
# Exact schema written by e302d94 (before op_gen / capture_unknown existed).
LEGACY_SCHEMA = """
CREATE TABLE orders (
    id TEXT PRIMARY KEY, product_id TEXT, product_name TEXT, merchant TEXT,
    category TEXT, quantity INTEGER, amount TEXT, currency TEXT,
    status TEXT, paypal_order_id TEXT, paypal_status TEXT, capture_id TEXT,
    approval_id TEXT, decision TEXT, created_at TEXT, approve_url TEXT,
    updated_at TEXT, request_id TEXT);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY, product_id TEXT, product_name TEXT, merchant TEXT,
    category TEXT, quantity INTEGER, amount TEXT, currency TEXT,
    justification TEXT, reasons TEXT, status TEXT, order_id TEXT,
    created_at TEXT, decided_at TEXT, request_id TEXT);
CREATE TABLE spend (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
    amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, order_ref TEXT, created_at TEXT NOT NULL);
"""


def _legacy_db(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows: (order id, status, paypal order id). Each $49.00 (2 x Wireless Mouse), reserved."""
    c = sqlite3.connect(path)
    c.executescript(LEGACY_SCHEMA)
    for oid, st, pp in rows:
        c.execute("INSERT INTO orders (id, product_id, product_name, merchant, category, quantity, amount, "
                  "currency, status, paypal_order_id, paypal_status, decision, created_at, updated_at) VALUES "
                  "(?, 'P-1002', 'Wireless Mouse', 'Sandbox Electronics', 'office-supplies', 2, '49.00', 'USD', "
                  "?, ?, 'capture outcome unknown: 503', '{\"verdict\": \"ALLOW\", \"reasons\": []}', "
                  "'2026-10-30T11:59:00+00:00', '2026-10-30T11:59:00+00:00')", (oid, st, pp))
        c.execute("INSERT INTO spend (day, amount_cents, currency, order_ref, created_at) "
                  "VALUES ('2026-10-30', 4900, 'USD', ?, '2026-10-30T11:59:00')", (oid,))
    c.commit()
    c.close()


def test_migration_backfills_capture_unknown_for_open_legacy_orders(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db, [("ORD-RECON", "NEEDS_RECONCILIATION", "MOCK-A"), ("ORD-CAPT", "CAPTURING", "MOCK-B"),
                    ("ORD-AWAIT", "PAYER_ACTION_REQUIRED", "MOCK-C"), ("ORD-DONE", "COMPLETED", "MOCK-D")])
    s = Store(str(db))
    flags = {r["id"]: r["capture_unknown"] for r in s.list("orders")}
    assert flags == {"ORD-RECON": 1, "ORD-CAPT": 1, "ORD-AWAIT": 0, "ORD-DONE": 0}
    assert all(r["op_gen"] == 0 for r in s.list("orders"))
    s.conn.close()
    # re-opening an already migrated DB does not re-run the backfill
    s2 = Store(str(db))
    s2.update("orders", "ORD-AWAIT", status="NEEDS_RECONCILIATION")
    s2.conn.close()
    s3 = Store(str(db))
    assert s3.get("orders", "ORD-AWAIT")["capture_unknown"] == 0


def test_legacy_ambiguous_capture_cannot_be_cancelled_after_upgrade(policy, catalog, clock, tmp_path):
    """CODEX_REVIEW_3 #1 repro: old-version NEEDS_RECONCILIATION after a capture 503, provider
    now says the order awaits the buyer. Before the fix: -> PAYER_ACTION_REQUIRED, cancel
    released $49. Now: stays NEEDS_RECONCILIATION, cancel refused, $49 stays reserved."""
    db = tmp_path / "legacy.db"
    pp = MockPayPal(require_payer_approval=True)
    created = pp.create_order(Decimal("49.00"), "USD", "2 x Wireless Mouse", "ORD-LEGACY")
    _legacy_db(db, [("ORD-LEGACY", "NEEDS_RECONCILIATION", created["id"])])
    svc = BuyerService(policy, catalog, pp, str(db), clock=clock)
    assert svc.spent_today() == Decimal("49.00")
    out = svc.reconcile("ORD-LEGACY")
    assert out["status"] == "NEEDS_RECONCILIATION" and out.get("paypal_live_status") == "PAYER_ACTION_REQUIRED"
    assert svc.cancel("ORD-LEGACY")["result"] == "NOT_CANCELLABLE"
    clock.now = clock.now.replace(hour=23)  # past the approval TTL: expiry must not release it either
    svc.expire_stale()
    assert svc.store.get("orders", "ORD-LEGACY")["status"] == "NEEDS_RECONCILIATION"
    assert svc.spent_today() == Decimal("49.00")
    # a confirmed capture still resolves it forward
    pp.approve(created["id"])
    pp.capture_order(created["id"])
    assert svc.reconcile("ORD-LEGACY")["status"] == "COMPLETED"
    assert svc.spent_today() == Decimal("49.00")
    svc.store.conn.close()


# ---- 2. offline parser: structured, quantity only from its slot --------------------
@pytest.mark.parametrize("text,expected", [
    ("Buy a USB-C cable for two interns", [("P-1001", 1)]),
    ("Buy a USB-C cable for zero downtime", [("P-1001", 1)]),
    ("buy 3 USB-C cables for one intern", [("P-1001", 3)]),
    ("get a wireless mouse as one of two gifts", [("P-1002", 1)]),
    ("buy a pair of wireless mice", [("P-1002", 2)]),
    ("get a couple of snack boxes for ten people", [("P-3002", 2)]),
    ("a single mechanical keyboard", [("P-1003", 1)]),
])
def test_purpose_tail_never_changes_quantity(service, text, expected):
    agent = OfflineAgent(service)
    assert agent.refusal(text) is None
    assert [(l["product_id"], l["quantity"]) for l in agent.parse(text)] == expected


def test_parse_clause_is_structured(service):
    got = OfflineAgent(service).parse_clause("please buy 2 USB-C charging cables for the new hire")
    assert got == {"verb": "buy", "quantity": 2, "quantity_text": "2", "item": "USB-C charging cables",
                   "purpose": "for the new hire"}
    got = OfflineAgent(service).parse_clause("a dozen sticky notes")
    assert (got["verb"], got["quantity"], got["quantity_text"], got["purpose"]) == (None, 12, "a dozen", None)


def test_two_interns_executes_one_cable(service):
    res = OfflineAgent(service).run("Buy a USB-C cable for two interns")
    orders = service.store.list("orders")
    assert len(orders) == 1 and orders[0]["quantity"] == 1 and orders[0]["amount"] == "12.99"
    assert res.stopped_reason == "offline_rules"


@pytest.mark.parametrize("text", [
    "Buy two three USB-C cables", "buy 2 3 USB-C cables", "buy 2 two USB-C cables",
    "buy a 2 wireless mice", "buy two dozen sticky notes", "buy USB-C cables two",
    "buy a wireless mouse 3", "buy one a keyboard"])
def test_ambiguous_quantities_refused(service, text):
    agent = OfflineAgent(service)
    with pytest.raises(GrammarError):
        agent.validate(text)
    res = agent.run(text)
    assert res.stopped_reason == "offline_refused" and service.store.list("orders") == []
    assert [t for t in res.transcript if t["role"] == "tool"] == []


def test_parse_marks_ungrammatical_clause_unexecutable(service):
    lines = OfflineAgent(service).parse("Buy two three USB-C cables")
    assert lines[0]["product_id"] is None and lines[0]["quantity"] == 0 and "ambiguous" in lines[0]["error"]


# ---- 3. CLI takes the mock DB lock ------------------------------------------------
def _cli(env_extra, *args):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "AGENT_MODE": "offline", "PAYPAL_MODE": "mock",
           "GRB_NO_DOTENV": "1", **env_extra}
    return subprocess.run([sys.executable, "-m", "app.cli", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)


def test_cli_refuses_mock_db_held_by_server(policy, catalog, tmp_path):
    db = str(tmp_path / "shared.db")
    server = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), db)
    server.acquire_mock_lock()
    try:
        r = _cli({"CLI_DB_PATH": db, "PAYPAL_MOCK_APPROVAL": "1"}, "a USB-C cable")
        assert r.returncode == 3, r.stdout + r.stderr
        assert "single-process" in r.stderr and "cannot share a mock-mode database" in r.stderr
        assert server.store.list("orders") == []  # nothing was inserted behind the server's back
    finally:
        server.release_mock_lock()
    r = _cli({"CLI_DB_PATH": db}, "a USB-C cable")  # lock free: the CLI may own the DB
    assert r.returncode == 0, r.stderr
    assert "ORDER_PLACED" in r.stdout
    # and while the CLI is not running, the lock is free again
    server.acquire_mock_lock()
    server.release_mock_lock()


def test_cli_default_in_memory_unaffected():
    r = _cli({}, "2 sticky notes")
    assert r.returncode == 0 and "spent_today=19.98" in r.stdout


# ---- 4. AGENT_RATE_LIMIT validated at startup ---------------------------------------
@pytest.mark.parametrize("value", ["0/60", "-1/60", "5/0", "abc", "5/x", "1.5/60", "5/-3", "5/inf", "/60", "5/"])
def test_invalid_rate_limit_rejected(value):
    with pytest.raises(ConfigError):
        parse_rate_limit(value)


@pytest.mark.parametrize("value,expected", [("off", None), ("OFF", None), ("none", None), ("0", None),
                                            ("20/60", (20, 60.0)), (" 3 / 10 ", (3, 10.0)), ("7", (7, 60.0))])
def test_valid_rate_limit(value, expected):
    assert parse_rate_limit(value) == expected


def test_invalid_rate_limit_fails_app_startup(service, monkeypatch):
    monkeypatch.setenv("AGENT_RATE_LIMIT", "0/60")
    with pytest.raises(ConfigError):
        create_app(service, approver_token="t")
    with pytest.raises(ConfigError):
        RateLimiter(0, 60)
    with pytest.raises(ConfigError):
        RateLimiter(1, float("inf"))


def test_invalid_rate_limit_server_exits_cleanly(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "AGENT_RATE_LIMIT": "0/60", "GRB_NO_DOTENV": "1",
           "AGENT_MODE": "offline", "DB_PATH": str(tmp_path / "x.db")}
    env.pop("GRB_NO_AUTOAPP", None)
    r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "AGENT_RATE_LIMIT='0/60' is invalid" in r.stderr
    assert "IndexError" not in r.stderr and "Traceback" not in r.stderr


# ---- C. decision audit log ----------------------------------------------------------
DEMO = ("Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, "
        "and also a $100 gift card as a welcome present.")


def _client(service):
    return TestClient(create_app(service, agent=OfflineAgent(service), approver_token="t"))


def test_every_decision_is_persisted(service):
    c = _client(service)
    c.post("/agent/request", json={"request": DEMO})
    rows = c.get("/decisions").json()["items"]
    by = {(r["product_id"], r["stage"]): r for r in rows}
    assert by[("P-1001", "order")]["verdict"] == "ALLOW" and by[("P-1001", "order")]["outcome"] == "RESERVED"
    assert by[("P-1001", "order")]["order_id"] and by[("P-1001", "order")]["risk"] == "ALLOW"
    kb = by[("P-1003", "order")]
    assert kb["verdict"] == "NEEDS_APPROVAL" and kb["rule_hit"] == "per_order_cap" and kb["risk"] == "NEEDS_APPROVAL"
    assert by[("P-1003", "approval_request")]["approval_id"]
    gc = by[("P-5001", "order")]
    assert gc["verdict"] == "DENY" and gc["risk"] == "DENY" and gc["outcome"] == "DENIED"
    assert "merchant_blocklist" in gc["rule_hit"] or "category_allowlist" in gc["rule_hit"]
    assert by[("P-1001", "check")]["stage"] == "check"  # read-only checks are audited too
    for r in rows:
        assert r["id"].startswith("DEC-") and r["request_id"] and r["created_at"] and r["merchant"]
        assert r["amount"] and "rules" not in r  # compact list (round 7 N5): rules load lazily
        assert c.get(f"/decisions/{r['id']}").json()["rules"]
    # newest first
    assert [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)


def test_decision_detail_has_quote_rules_and_timeline(service):
    service.paypal.require_payer_approval = True
    c = _client(service)
    c.post("/agent/request", json={"request": "Buy 2 USB-C cables"})
    dec = next(r for r in c.get("/decisions").json()["items"] if r["stage"] == "order")
    d = c.get(f"/decisions/{dec['id']}").json()
    assert d["quote"] == {"product_id": "P-1001", "quantity": 2, "amount": "25.98", "currency": "USD",
                          "merchant": "Sandbox Electronics", "category": "office-supplies"}
    assert {r["rule"] for r in d["rules"]} >= {"currency", "merchant_allowlist", "category_allowlist",
                                               "hard_max_per_order", "per_order_cap", "daily_cap"}
    assert all(r["outcome"] == "PASS" for r in d["rules"] if r["outcome"] != "SKIPPED")
    assert [t["to_status"] for t in d["timeline"]] == ["CREATING", "PAYER_ACTION_REQUIRED"]
    oid = d["order"]["id"]
    service.paypal.approve(d["order"]["paypal_order_id"])
    assert c.post(f"/orders/{oid}/capture").json()["status"] == "COMPLETED"
    d = c.get(f"/decisions/{dec['id']}").json()
    assert [(t["from_status"], t["to_status"]) for t in d["timeline"]] == [
        (None, "CREATING"), ("CREATING", "PAYER_ACTION_REQUIRED"),
        ("PAYER_ACTION_REQUIRED", "CAPTURING"), ("CAPTURING", "COMPLETED")]
    assert [t["op_gen"] for t in d["timeline"]] == [1, 2, 3, 4]
    assert c.get("/decisions/DEC-NOPE").status_code == 404


def test_approval_execution_is_audited_and_linked(service):
    c = _client(service)
    c.post("/agent/request", json={"request": "a mechanical keyboard"})
    aid = c.get("/approvals?status=PENDING").json()[0]["id"]
    c.post(f"/approvals/{aid}/approve", headers={"X-Approver-Token": "t"})
    rows = c.get("/decisions").json()["items"]
    ex = next(r for r in rows if r["stage"] == "approval_execute")
    assert ex["verdict"] == "ALLOW" and ex["rule_hit"] == "human_approval" and ex["approval_id"] == aid
    assert ex["order_status"] == "COMPLETED"
    waived = {r["rule"]: r["outcome"] for r in c.get(f"/decisions/{ex['id']}").json()["rules"]}
    assert waived["per_order_cap"] == "WAIVED"
    req = next(r for r in rows if r["stage"] == "approval_request")
    d = c.get(f"/decisions/{req['id']}").json()  # the request links to the order its approval created
    assert d["approval"]["status"] == "APPROVED" and d["order"]["status"] == "COMPLETED"
    assert d["timeline"][-1]["to_status"] == "COMPLETED"


def test_reconciliation_risk_highlight(service):
    from app.paypal_client import PayPalError
    c = _client(service)
    service.paypal.require_payer_approval = True
    c.post("/agent/request", json={"request": "2 USB-C cables"})
    o = service.store.list("orders")[0]
    service.paypal.approve(o["paypal_order_id"])

    def boom(_):
        raise PayPalError("503 upstream", status_code=503)
    service.paypal.capture_order = boom
    service.paypal.get_order = boom
    assert service.capture(o["id"])["status"] == "NEEDS_RECONCILIATION"
    row = next(r for r in c.get("/decisions").json()["items"] if r["stage"] == "order")
    assert row["order_status"] == "NEEDS_RECONCILIATION" and row["risk"] == "NEEDS_RECONCILIATION"


def test_explain_rules_agrees_with_evaluate(policy, catalog):
    cases = [(p, q, spent) for p in catalog.products.values() for q in (1, 3, 12) for spent in ("0", "180")]
    for p, q, spent in cases:
        req = OrderRequest(merchant=p["merchant"], category=p["category"],
                           amount=Decimal(p["price"]) * q, currency="USD", description="x")
        d = evaluate(policy, req, Decimal(spent))
        outs = {r["outcome"] for r in explain_rules(policy, req, Decimal(spent))}
        want = "DENY" if "DENY" in outs else ("NEEDS_APPROVAL" if "NEEDS_APPROVAL" in outs else "ALLOW")
        assert d.verdict.value == want, (p["id"], q, spent)


def test_audit_decisions_table_migrates_into_old_db(tmp_path, policy, catalog, clock):
    db = tmp_path / "legacy.db"
    _legacy_db(db, [("ORD-OLD", "COMPLETED", "MOCK-X")])
    svc = BuyerService(policy, catalog, MockPayPal(), str(db), clock=clock)
    assert svc.list_decisions() == []
    svc.place_order("P-2001", 1)
    assert len(svc.list_decisions()) == 1
    svc.store.conn.close()


# ---- dashboard assets resolve -----------------------------------------------------
def test_dashboard_references_resolve(service):
    import re
    c = _client(service)
    html = c.get("/").text
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert len(refs) == 8 and all(c.get(u).status_code == 200 for u in refs)  # +policy.js (Policy tab), +ui.js (shared helpers), +favicon.svg
    for needed in ('id="audit"', 'id="audit-search"', 'id="audit-csv"', 'id="audit-detail"', 'id="tab-audit"'):
        assert needed in html
    js = c.get("/static/app.js").text
    for needed in ("exportDataAsCsv", "quickFilterText", "cellClassRules", "getRowId", "onRowClicked",
                   "/decisions", "agNumberColumnFilter", "floatingFilter"):
        assert needed in js
    assert "Enterprise" not in js and "agSetColumnFilter" not in js  # Community features only
    ids = set(re.findall(r'\$\("([\w-]+)"\)', js))
    missing = {i for i in ids if f'id="{i}"' not in html}
    assert not missing, missing  # every element the script looks up exists in the page
