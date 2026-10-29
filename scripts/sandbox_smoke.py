#!/usr/bin/env python3
"""Smoke test against the REAL PayPal Sandbox (no buyer login, no money).

Two modes:
  python scripts/sandbox_smoke.py                    # in-process: BuyerService + SandboxPayPal, no LLM
  python scripts/sandbox_smoke.py --app http://localhost:8000 [--agent]
                                                     # against a running app (PAYPAL_MODE=sandbox);
                                                     # --agent also sends one natural-language request
                                                     # through the Claude agent (costs a few cents)

It checks: guardrails (ALLOW / NEEDS_APPROVAL / DENY) -> real Sandbox order
(PAYER_ACTION_REQUIRED + sandbox checkoutnow approve URL) -> live GET status ->
capture before buyer approval returns ORDER_NOT_APPROVED and changes nothing ->
cancel releases the reserved budget. Buyer approval itself needs a Sandbox
personal account in a browser and is out of scope here.

Env for in-process mode: PAYPAL_AUTH=proxy (egress proxy injects Basic auth) or
PAYPAL_AUTH=oauth with PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GRB_NO_AUTOAPP", "1")

FAILS: list[str] = []


def check(cond: bool, label: str, detail: object = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail != "" else ""))
    if not cond:
        FAILS.append(label)


def in_process() -> None:
    from app.guardrails import Policy
    from app.paypal_client import SandboxPayPal
    from app.service import BuyerService, Catalog

    svc = BuyerService(Policy.load(ROOT / "policy.yaml"), Catalog.load(ROOT / "data" / "catalog.json"),
                       SandboxPayPal(), ":memory:")
    print(f"PayPal base: {svc.paypal.base_url}  auth={svc.paypal.auth}")

    print("1. ALLOW -> real Sandbox order")
    r = svc.place_order("P-1001", 1)
    o = r.get("order") or {}
    check(r["result"] == "AWAITING_PAYER_APPROVAL", "guardrails ALLOW, order awaiting buyer", r["result"])
    check(bool(o.get("paypal_order_id")), "PayPal order id", o.get("paypal_order_id"))
    check("sandbox.paypal.com/checkoutnow" in (o.get("approve_url") or ""), "approve URL", o.get("approve_url"))
    check(svc.spend_summary()["reserved"] == "12.99", "12.99 reserved against daily cap")

    print("2. GET live status")
    live = svc.get_order(o["id"], refresh=True)
    check(live.get("paypal_live_status") == "PAYER_ACTION_REQUIRED", "PayPal reports PAYER_ACTION_REQUIRED",
          live.get("paypal_live_status") or live.get("refresh_error"))

    print("3. capture before buyer approval")
    cap = svc.capture(o["id"])
    check(cap.get("result") == "NOT_APPROVED_YET", "capture -> NOT_APPROVED_YET", cap.get("error"))
    check(cap["status"] == "PAYER_ACTION_REQUIRED", "order stays open, reservation kept",
          svc.spend_summary()["reserved"])
    ret = svc.complete_payment(o["paypal_order_id"])
    check(ret.get("result") == "NOT_APPROVED_YET", "/paypal/return before approval does not capture")

    print("4. cancel path")
    c = svc.cancel(o["id"])
    check(c["status"] == "CANCELLED" and svc.spend_summary()["total"] == "0.00", "cancel releases budget")
    try:
        svc.capture(o["id"])
        check(False, "capture after cancel refused")
    except ValueError:
        check(True, "capture after cancel refused")

    print("5. NEEDS_APPROVAL -> human approve -> real Sandbox order")
    a = svc.request_approval("P-1003", 1, "smoke test: keyboard above per-order cap")
    check(a["result"] == "APPROVAL_REQUESTED", "keyboard $89 needs approval", a["result"])
    ap = svc.approve(a["approval_id"])
    check(ap["result"] == "AWAITING_PAYER_APPROVAL" and ap["order"].get("paypal_order_id"),
          "approved -> Sandbox order", ap["order"].get("paypal_order_id"))
    svc.cancel(ap["order"]["id"])

    print("6. DENY never reaches PayPal")
    d = svc.place_order("P-5001", 1)
    check(d["result"] == "DENIED", "gift card denied", "; ".join(d.get("reasons", [])))
    check(len(svc.store.list("orders")) == 2, "only 2 PayPal orders were created")
    print("orders:", json.dumps([(x["id"], x["paypal_order_id"], x["status"]) for x in svc.store.list("orders")]))


def against_app(base: str, token: str, use_agent: bool) -> None:
    import httpx
    # trust_env=False: a local app must not be reached through HTTP(S)_PROXY
    c = httpx.Client(base_url=base.rstrip("/"), timeout=180, trust_env=False)
    h = c.get("/health").json()
    check(h.get("paypal") == "sandbox", "app runs in sandbox mode", h)
    hdr = {"X-Approver-Token": token} if token else {}
    if use_agent:
        print("agent request")
        r = c.post("/agent/request", json={"request":
            "Buy 1 USB-C charging cable and 1 mechanical keyboard for the new hire, and a $100 gift card."}).json()
        print("  agent:", (r.get("final_text") or "")[:300].replace("\n", " "))
        print("  usage:", r.get("usage"))
    orders = c.get("/orders").json()
    open_ = [o for o in orders if o["status"] == "PAYER_ACTION_REQUIRED"]
    check(bool(open_), "at least one open Sandbox order", len(open_))
    if not open_:
        return
    o = open_[0]
    check("sandbox.paypal.com/checkoutnow" in (o.get("approve_url") or ""), "approve URL", o.get("approve_url"))
    live = c.get(f"/orders/{o['id']}").json()
    check(live.get("paypal_live_status") == "PAYER_ACTION_REQUIRED", "GET /orders/{id} live status",
          live.get("paypal_live_status"))
    r = c.post(f"/orders/{o['id']}/capture")
    check(r.status_code == 409 and r.json()["detail"]["paypal_issue"] == "ORDER_NOT_APPROVED",
          "capture before approval -> 409 ORDER_NOT_APPROVED", r.status_code)
    r = c.get("/paypal/cancel", params={"token": o["paypal_order_id"], "format": "json"})
    check(r.status_code == 403, "unsigned /paypal/cancel -> 403 (needs the signed state from the cancel_url)",
          r.status_code)
    r = c.post(f"/orders/{o['id']}/cancel", headers=hdr)
    check(r.status_code == 200 and r.json()["status"] == "CANCELLED", "POST /orders/{id}/cancel -> CANCELLED",
          r.status_code)
    print("approvals pending:", [(a["id"], a["product_name"]) for a in c.get("/approvals?status=PENDING").json()])
    print("spend:", c.get("/spend").json())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", help="base URL of a running app (PAYPAL_MODE=sandbox)")
    ap.add_argument("--agent", action="store_true", help="also send one request through the LLM agent")
    ap.add_argument("--token", default=os.environ.get("APPROVER_TOKEN", ""))
    a = ap.parse_args()
    if a.app:
        against_app(a.app, a.token, a.agent)
    else:
        os.environ.setdefault("PAYPAL_AUTH", "proxy")
        in_process()
    print("\nRESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
