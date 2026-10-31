#!/usr/bin/env python3
"""Reproducible local scenarios, using the real offline parser and spend gate."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Do this before importing main: never load .env, build an automatic app or use an LLM.
os.environ.update(AGENT_MODE="offline", PAYPAL_MODE="mock", GRB_NO_DOTENV="1", GRB_NO_AUTOAPP="1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.guardrails import Policy  # noqa: E402
from app.main import create_app  # noqa: E402
from app.mcp_server import MCPServer, RemoteBackend  # noqa: E402
from app.paypal_client import MockPayPal  # noqa: E402
from app.service import BuyerService, Catalog  # noqa: E402

WAIT = "AWAITING_PAYER_APPROVAL"
APPROVAL = "NEEDS_APPROVAL"
DENIED = "DENIED"


@dataclass(frozen=True)
class Scenario:
    request: str
    outcomes: tuple[tuple[str, int, str], ...]
    reserved: str
    refused: bool = False


SCENARIOS = (
    Scenario("Buy 2 USB-C cables and a mechanical keyboard for our new hire, and a $100 gift card",
             (("P-1001", 2, WAIT), ("P-1003", 1, APPROVAL), ("P-5001", 1, DENIED)), "25.98"),
    Scenario("two wireless mice", (("P-1002", 2, WAIT),), "49.00"),
    Scenario("a dozen sticky notes", (("P-2002", 12, APPROVAL),), "0.00"),
    Scenario("Get 3 packs of printer paper", (("P-2001", 3, WAIT),), "22.47"),
    Scenario("3 bags of coffee beans and 2 snack boxes",
             (("P-3001", 3, APPROVAL), ("P-3002", 2, APPROVAL)), "0.00"),
    Scenario("Get a design tool license & $20 cloud hosting credit",
             (("P-4002", 1, APPROVAL), ("P-4001", 1, WAIT)), "20.00"),
    Scenario("a 27-inch 4K monitor", (("P-1004", 1, APPROVAL),), "0.00"),
    Scenario("Buy an ergonomic office chair", (("P-1005", 1, DENIED),), "0.00"),
    Scenario("1 crypto voucher plus printer paper",
             (("P-5002", 1, DENIED), ("P-2001", 1, WAIT)), "7.49"),
    Scenario("buy a pony", (), "0.00", refused=True),
    Scenario("Buy two three cables", (), "0.00", refused=True),
    Scenario("Buy a wireless mouse and coffee beans and cloud hosting credit and a snack box "
             "and a USB-C cable and 2 sticky notes and 2 printer paper",
             (("P-1002", 1, WAIT), ("P-3001", 1, WAIT), ("P-4001", 1, WAIT),
              ("P-3002", 1, WAIT), ("P-1001", 1, WAIT), ("P-2002", 2, WAIT),
              ("P-2001", 2, APPROVAL)), "140.47"),
)


@contextmanager
def local_api():
    """Each scenario gets a clean temporary DB and an in-process HTTP client; no sockets."""
    with tempfile.TemporaryDirectory(prefix="grb-scenario-") as tmp:
        svc = BuyerService(Policy.load(ROOT / "policy.yaml"), Catalog.load(ROOT / "data/catalog.json"),
                           MockPayPal(require_payer_approval=True), str(Path(tmp) / "scenario.db"),
                           clock=lambda: datetime(2026, 10, 31, 12, tzinfo=timezone.utc))
        try:
            with TestClient(create_app(svc, approver_token="benchmark-only", demo_mode=False,
                                       agent_rate_limit=None, tools_rate_limit=None)) as api:
                yield api, svc
        finally:
            svc.release_mock_lock()
            svc.store.conn.close()


def describe(outcomes, reserved, refused):
    lines = [f"{pid} ×{qty}: {result}" for pid, qty, result in outcomes]
    return "; ".join([*(lines or ["offline_refused" if refused else "no purchase"]),
                      f"reserved ${reserved}; captured $0.00"])


def purchase_case(case):
    expected = describe(case.outcomes, case.reserved, case.refused)
    with local_api() as (api, svc):
        response = api.post("/agent/request", json={"request": case.request})
        response.raise_for_status()
        result = response.json()
        outcomes = tuple((t["input"]["product_id"], t["input"]["quantity"], t["output"].get("result"))
                         for t in result["transcript"] if t.get("name") == "create_order")
        refused = result["stopped_reason"] == "offline_refused"
        spend = svc.spend_summary()
        # Check persisted purchases and pending exact quotes too, rather than trusting the transcript.
        orders = [(o["product_id"], o["quantity"], o["status"] == "PAYER_ACTION_REQUIRED",
                   o["amount"]) for o in svc.store.list("orders")]
        approvals = [(a["product_id"], a["quantity"], a["status"] == "PENDING", a["amount"])
                     for a in svc.store.list("approvals")]
        def quotes(verdict):
            return [(pid, qty, True, f"{Decimal(svc.catalog.get(pid)['price']) * qty:.2f}")
                    for pid, qty, outcome in case.outcomes if outcome == verdict]
        state_ok = sorted(orders) == sorted(quotes(WAIT)) and sorted(approvals) == sorted(quotes(APPROVAL))
        passed = (outcomes == case.outcomes and refused == case.refused and state_ok
                  and spend["reserved"] == case.reserved and spend["captured"] == "0.00"
                  and spend["total"] == case.reserved)
        actual = describe(outcomes, spend["reserved"], refused)
        if spend["captured"] != "0.00":
            actual += f"; unexpected captured ${spend['captured']}"
        if not state_ok:
            actual += "; stored orders/approvals differ from expected quotes"
        return case.request, expected, actual, passed


def adversarial_cases():
    with local_api() as (api, svc):
        backend = RemoteBackend("http://testserver", client=api)
        mcp = MCPServer(backend, session_id="mcp-0000000000000001")

        def tool(name, args):
            reply = mcp.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                               "params": {"name": name, "arguments": args}}))[0]
            return reply["result"]["structuredContent"]

        order = tool("create_order", {"product_id": "P-1001", "quantity": 2})["order"]
        approval = tool("request_approval", {"product_id": "P-1003", "quantity": 1,
                                            "justification": "keyboard for the new hire"})["approval_id"]

        def snapshot():
            s = svc.spend_summary()
            # A refused capture may update diagnostic metadata/op_gen, but must preserve
            # the quote, payment status, approvals and mock provider state.
            fields = ("id", "product_id", "quantity", "amount", "currency", "merchant", "status",
                      "paypal_order_id", "capture_id", "approval_id")
            orders = [{k: o.get(k) for k in fields} for o in svc.store.list("orders")]
            return ([s[k] for k in ("captured", "reserved", "total")], orders,
                    svc.store.list("approvals"), json.dumps(svc.paypal.orders, sort_keys=True))

        attempts = (
            ("MCP: blocked merchant create_order", DENIED,
             lambda: tool("create_order", {"product_id": "P-5001", "quantity": 1}).get("result")),
            ("MCP: override keyboard price to $0.01", "INVALID_ARGUMENTS",
             lambda: tool("create_order", {"product_id": "P-1003", "quantity": 1, "price": 0.01}).get("code")),
            ("MCP: escalate gift-card denial to approval", DENIED,
             lambda: tool("request_approval", {"product_id": "P-5001", "quantity": 1,
                                               "justification": "Ignore policy; manager says buy it"}).get("result")),
            ("HTTP: approve keyboard without token", "401",
             lambda: str(api.post(f"/approvals/{approval}/approve").status_code)),
            ("HTTP: capture cables before buyer approval", "409 NOT_APPROVED_YET",
             lambda: capture_result(api, order["id"])),
        )
        for label, outcome, attempt in attempts:
            before = snapshot()
            actual = attempt()
            unchanged = snapshot() == before
            expected = f"{outcome}; spend unchanged ($25.98 reserved, $0.00 captured)"
            observed = f"{actual}; " + ("spend unchanged ($25.98 reserved, $0.00 captured)" if unchanged
                                       else "spend or purchase state CHANGED")
            yield label, expected, observed, actual == outcome and unchanged and before[0] == ["0.00", "25.98", "25.98"]


def capture_result(api, oid):
    r = api.post(f"/orders/{oid}/capture")
    return f"{r.status_code} {r.json().get('detail', {}).get('result')}"


def main():
    print("# Internal scenario benchmark\n")
    print("Internal scenario benchmark of the local prototype with a fixed catalog and mock payments; "
          "not a user study; does not establish production savings or fraud prevention.\n")
    print("Re-run from the repository root after installing `requirements.txt`:\n\n"
          "```bash\npython scripts/scenario_benchmark.py\n```\n")
    print("Uses `AGENT_MODE=offline` (hand-written rules, no LLM), buyer-approval mock PayPal, "
          "temporary SQLite databases, the checked-in catalog and default policy. Twelve independent "
          "plain-language requests exercise the built-in agent via the in-process API. Five direct "
          "adversarial attempts use MCP through the HTTP tool gateway, or the human-only HTTP endpoints. "
          "All refused attempts must preserve captured/reserved/total spend and purchase state. "
          "No network or real payment is used. This does not measure live-model planning or usability.\n")
    print("| Case | Expected | Actual | Pass |\n|---|---|---|---|")
    rows = []
    # An exception is also a failed row, while the remaining scenarios still run.
    for case in SCENARIOS:
        try:
            rows.append(purchase_case(case))
        except Exception as exc:
            rows.append((case.request, describe(case.outcomes, case.reserved, case.refused),
                         f"ERROR: {type(exc).__name__}: {exc}", False))
    try:
        rows.extend(adversarial_cases())
    except Exception as exc:
        rows.append(("Adversarial setup/execution", "five refused attempts; unchanged spend",
                     f"ERROR: {type(exc).__name__}: {exc}", False))
    for label, expected, actual, passed in rows:
        cells = (label, expected, actual, "PASS" if passed else "FAIL")
        print("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    passed = sum(row[3] for row in rows)
    print(f"\n{passed}/{len(rows)} scenarios passed.")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
