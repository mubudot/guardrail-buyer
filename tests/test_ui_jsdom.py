"""Dashboard regressions for CODEX_REVIEW_4 N3-N5 with the real vendored AG Grid in jsdom.

Needs `node` and the `jsdom` package (e.g. `npm i jsdom@24`, then point GRB_JSDOM_NODE_PATH
or NODE_PATH at that node_modules directory). Skipped when either is missing, so the
offline README block still runs with Python only."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app.guardrails import Policy
from app.paypal_client import MockPayPal
from app.service import BuyerService, Catalog

ROOT = Path(__file__).resolve().parent.parent


def _node_env():
    node = shutil.which("node")
    if not node:
        return None, None
    env = {**os.environ}
    np = os.environ.get("GRB_JSDOM_NODE_PATH") or os.environ.get("NODE_PATH")
    if np:
        env["NODE_PATH"] = np
    ok = subprocess.run([node, "-e", "require('jsdom')"], env=env, capture_output=True).returncode == 0
    return (node, env) if ok else (None, None)


NODE, NODE_ENV = _node_env()
pytestmark = pytest.mark.skipif(NODE is None, reason="node + jsdom not available (set GRB_JSDOM_NODE_PATH)")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_audit_tab_review4(tmp_path):
    catalog = json.loads((ROOT / "data" / "catalog.json").read_text())
    catalog.append({"id": "P-9999", "name": "Formula Widget", "merchant": "=1+1", "category": "office-supplies",
                    "price": "5.00", "currency": "USD"})
    cat_path = tmp_path / "catalog.json"
    cat_path.write_text(json.dumps(catalog))
    db = tmp_path / "ui.db"
    svc = BuyerService(Policy.load(ROOT / "policy.yaml"), Catalog.load(cat_path), MockPayPal(), str(db))
    with svc.store.tx() as c:  # 1001 old decisions, older than everything below
        c.executemany(
            "INSERT INTO decisions (id, created_at, stage, request_id, product_id, product_name, merchant, "
            "category, quantity, amount, currency, verdict, outcome, rule_hit, reasons, rules, spent_before) "
            "VALUES (?, '2026-01-01T00:00:00+00:00', 'check', 'REQ-OLD', 'P-1001', 'USB-C Charging Cable 2m', "
            "'Sandbox Electronics', 'office-supplies', 1, '12.99', 'USD', 'ALLOW', 'CHECKED', '', '[]', '[]', '0')",
            [(f"DEC-OLD{i:05d}",) for i in range(1001)])
    deny = svc.place_order("P-9999", 1, request_id="REQ-F")
    appr = svc.request_approval("P-1003", 1, "JUSTIFY-PENDING keyboard for the new hire", request_id="REQ-K")
    rows = svc.list_decisions(10)
    deny_id = next(r["id"] for r in rows if r["product_id"] == "P-9999")
    pending_id = next(r["id"] for r in rows if r["stage"] == "approval_request")
    approval_id = appr["approval_id"]
    assert deny["result"] == "DENIED" and appr["result"] == "APPROVAL_REQUESTED"
    svc.store.conn.close()

    port = _free_port()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])), "GRB_NO_DOTENV": "1", "DB_PATH": str(db),
           "CATALOG_PATH": str(cat_path), "AGENT_MODE": "offline", "PAYPAL_MODE": "mock", "APPROVER_TOKEN": "tok"}
    env.pop("GRB_NO_AUTOAPP", None)
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                            "--port", str(port)], cwd=ROOT, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via HTTP(S)_PROXY
        for _ in range(150):
            if srv.poll() is not None:
                pytest.fail("server exited: " + srv.stdout.read())
            try:
                direct.open(base + "/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("server did not start")
        r = subprocess.run([NODE, str(ROOT / "scripts" / "ui_review4_jsdom.cjs"), base, "tok",
                            json.dumps({"pending": pending_id, "deny": deny_id, "approval": approval_id})],
                           # Match the dashboard harness's allowance for real AG Grid in jsdom.
                           env=NODE_ENV, capture_output=True, text=True, timeout=300)
        print(r.stdout, r.stderr)
        assert r.returncode == 0 and "UI REVIEW4 OK" in r.stdout, r.stdout + r.stderr
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            srv.kill()
            srv.wait()
