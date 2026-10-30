"""Dashboard polish (summary cards, approval confirmation dialog, connection banner) with the
real vendored AG Grid in jsdom, plus the pure formatting helpers of app/static/ui.js in plain node.

The jsdom test needs `node` and the `jsdom` package (GRB_JSDOM_NODE_PATH or NODE_PATH pointing at a
node_modules directory that has it) and is skipped otherwise; the helper test needs only node."""
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
NODE = shutil.which("node")


def _jsdom_env():
    if not NODE:
        return None
    env = {**os.environ}
    np = os.environ.get("GRB_JSDOM_NODE_PATH") or os.environ.get("NODE_PATH")
    if np:
        env["NODE_PATH"] = np
    ok = subprocess.run([NODE, "-e", "require('jsdom')"], env=env, capture_output=True).returncode == 0
    return env if ok else None


JSDOM_ENV = _jsdom_env()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.skipif(JSDOM_ENV is None, reason="node + jsdom not available (set GRB_JSDOM_NODE_PATH)")
def test_summary_cards_approval_dialog_and_banner(tmp_path):
    db = tmp_path / "ui.db"
    svc = BuyerService(Policy.load(ROOT / "policy.yaml"), Catalog.load(ROOT / "data" / "catalog.json"),
                       MockPayPal(), str(db))
    assert svc.place_order("P-1001", 2, request_id="REQ-C")["result"] == "ORDER_PLACED"  # mock: captured at once
    assert svc.place_order("P-5001", 1, request_id="REQ-G")["result"] == "DENIED"
    appr = svc.request_approval("P-1003", 1, "keyboard for the new hire", request_id="REQ-K")
    svc.store.conn.close()

    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "GRB_NO_DOTENV": "1", "DB_PATH": str(db),
           "AGENT_MODE": "offline", "PAYPAL_MODE": "mock", "APPROVER_TOKEN": "tok"}
    env.pop("GRB_NO_AUTOAPP", None)
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                            "--port", str(port)], cwd=ROOT, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
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
        r = subprocess.run([NODE, str(ROOT / "scripts" / "ui_dashboard_jsdom.cjs"), base, "tok", appr["approval_id"]],
                           env=JSDOM_ENV, capture_output=True, text=True, timeout=150)
        print(r.stdout, r.stderr)
        assert r.returncode == 0 and "UI DASHBOARD OK" in r.stdout, r.stdout + r.stderr
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            srv.kill()
            srv.wait()


HELPERS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = {document: {getElementById() { return null; }}, Intl, Number, String, Object, Math, console};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') +
  ';globalThis.__t = {usd, statusCell, toneOf, statusLabel, escapeHtml, pct};', ctx);
const t = ctx.__t;
console.log(JSON.stringify({
  usd: [t.usd('25.98'), t.usd(1234.5), t.usd(null), t.usd(''), t.usd('abc'), t.usd('0')],
  xss: t.statusCell({value: '<img src=x onerror=alert(1)>'}),
  pill: t.statusCell({value: 'PAYER_ACTION_REQUIRED'}),
  empty: t.statusCell({value: null}),
  tones: ['COMPLETED', 'NEEDS_APPROVAL', 'DENY', 'AWAITING_PAYER_APPROVAL', 'SOMETHING_NEW'].map(t.toneOf),
  label: t.statusLabel('CAPTURE_FAILED'),
  pct: [t.pct(25.98, 150), t.pct(500, 150), t.pct(5, 0), t.pct(-1, 10)],
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not available")
def test_ui_formatting_helpers(tmp_path):
    h = tmp_path / "h.cjs"
    h.write_text(HELPERS)
    r = subprocess.run([NODE, str(h), str(ROOT / "app/static/ui.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["usd"] == ["$25.98", "$1,234.50", "", "", "", "$0.00"]
    assert "<img" not in out["xss"] and "&lt;img" in out["xss"]  # status text is escaped in grid cells
    assert 'class="st PAYER_ACTION_REQUIRED tone-warn"' in out["pill"] and "AWAITING PAYER" in out["pill"]
    assert out["empty"] == ""
    assert out["tones"] == ["ok", "warn", "bad", "warn", "neutral"]
    assert out["label"] == "CAPTURE FAILED"
    assert out["pct"][0] == pytest.approx(17.32) and out["pct"][1:] == [100, 0, 0]
