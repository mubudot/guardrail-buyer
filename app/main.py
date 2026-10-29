"""FastAPI app for GuardRail Buyer: REST API, PayPal return/cancel, dashboard."""
from __future__ import annotations

import hmac
import html
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
# Auto-load .env (never overrides variables already set in the environment).
if os.environ.get("GRB_NO_DOTENV") != "1":
    load_dotenv(ROOT / ".env", override=False)

from .agent import LLMError, PurchasingAgent  # noqa: E402
from .guardrails import Policy  # noqa: E402
from .paypal_client import MockPayPal, PayPalError, make_client  # noqa: E402
from .service import BuyerService, Catalog  # noqa: E402

log = logging.getLogger("guardrail")
STATIC = ROOT / "app" / "static"


class AgentRequest(BaseModel):
    request: str = Field(..., min_length=1, max_length=2000,
                         examples=["Buy 2 USB-C cables and a wireless mouse for the new hire"])


def build_service(db_path: str | None = None, paypal_mode: str | None = None) -> BuyerService:
    policy = Policy.load(os.environ.get("POLICY_PATH", ROOT / "policy.yaml"))
    catalog = Catalog.load(os.environ.get("CATALOG_PATH", ROOT / "data" / "catalog.json"))
    return BuyerService(policy, catalog, make_client(paypal_mode),
                        db_path or os.environ.get("DB_PATH", str(ROOT / "guardrail.db")))


def create_app(service: BuyerService | None = None, agent: PurchasingAgent | None = None,
               approver_token: str | None = None) -> FastAPI:
    app = FastAPI(title="GuardRail Buyer", version="0.2.0",
                  description="AI purchasing agent with PayPal spend guardrails (Sandbox)")
    svc = service or build_service()
    app.state.service = svc
    app.state.agent = agent or PurchasingAgent(svc)
    token = approver_token if approver_token is not None else os.environ.get("APPROVER_TOKEN", "")
    if not token:
        log.warning("APPROVER_TOKEN not set: approve/reject endpoints are OPEN (dev mode)")

    def require_approver(x_approver_token: str | None = Header(default=None),
                         authorization: str | None = Header(default=None)):
        if not token:
            return
        given = x_approver_token or ""
        if not given and authorization and authorization.lower().startswith("bearer "):
            given = authorization[7:]
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "approver token required (X-Approver-Token header)")

    # ---- dashboard -----------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "index.html")

    # ---- API -----------------------------------------------------------
    @app.get("/health")
    def health():
        return {"ok": True, "paypal": svc.paypal.name, "model": app.state.agent.model,
                "approver_auth": bool(token)}

    @app.get("/policy")
    def get_policy():
        return {**svc.policy.to_dict(), "spent_today": str(svc.spent_today()), "day": svc._today()}

    @app.get("/spend")
    def get_spend():
        return svc.spend_summary()

    @app.get("/catalog")
    def get_catalog(q: str = "", category: str | None = None):
        return svc.catalog.search(q, category, limit=100)

    @app.post("/agent/request")
    def agent_request(body: AgentRequest):
        try:
            return app.state.agent.run(body.request).to_dict()
        except LLMError as e:
            raise HTTPException(502, str(e))

    @app.get("/orders")
    def list_orders(status: str | None = None):
        svc.expire_stale()
        return svc.store.list("orders", status)

    @app.get("/approvals")
    def list_approvals(status: str | None = None):
        return svc.store.list("approvals", status)

    def _decide(approval_id: str, fn):
        try:
            return fn(approval_id)
        except KeyError:
            raise HTTPException(404, f"approval {approval_id} not found")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/approvals/{approval_id}/approve", dependencies=[Depends(require_approver)])
    def approve(approval_id: str):
        return _decide(approval_id, svc.approve)

    @app.post("/approvals/{approval_id}/reject", dependencies=[Depends(require_approver)])
    def reject(approval_id: str):
        return _decide(approval_id, svc.reject)

    # ---- PayPal buyer round trip ---------------------------------------
    # PayPal redirects the buyer to return_url?token=<paypal order id>&PayerID=...
    # after approval, or to cancel_url?token=<paypal order id> on cancel.
    @app.get("/paypal/return")
    def paypal_return(token: str, PayerID: str | None = None, format: str = "html"):
        try:
            order = svc.complete_payment(token)
        except KeyError:
            raise HTTPException(404, "unknown PayPal order")
        except ValueError as e:
            raise HTTPException(409, str(e))
        if format == "json":
            return order
        return RedirectResponse(f"/?paid={order['id']}&status={order['status']}", status_code=303)

    @app.get("/paypal/cancel")
    def paypal_cancel(token: str, format: str = "html"):
        try:
            order = svc.cancel_payment(token)
        except KeyError:
            raise HTTPException(404, "unknown PayPal order")
        if format == "json":
            return order
        return RedirectResponse(f"/?cancelled={order['id']}", status_code=303)

    @app.get("/paypal/mock-checkout", response_class=HTMLResponse, include_in_schema=False)
    def mock_checkout(token: str):
        """Stand-in for the PayPal approval page when PAYPAL_MODE=mock + PAYPAL_MOCK_APPROVAL=1."""
        if not isinstance(svc.paypal, MockPayPal):
            raise HTTPException(404, "mock checkout only exists in mock mode")
        try:
            o = svc.paypal.get_order(token)["raw"]
        except PayPalError:
            raise HTTPException(404, "unknown mock order")
        t = html.escape(token)
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Mock PayPal checkout</title>
<link rel="stylesheet" href="/static/app.css"></head><body class="checkout">
<div class="card"><div class="brand">Mock checkout <span>stands in for the PayPal approval page</span></div>
<p>{html.escape(o['description'])}</p><p class="amount">{html.escape(o['amount'])} {html.escape(o['currency'])}</p>
<a class="btn primary" href="/paypal/mock-approve?token={t}">Pay now</a>
<a class="btn" href="/paypal/cancel?token={t}">Cancel and return</a></div></body></html>"""

    @app.get("/paypal/mock-approve", include_in_schema=False)
    def mock_approve(token: str):
        if not isinstance(svc.paypal, MockPayPal):
            raise HTTPException(404, "mock checkout only exists in mock mode")
        try:
            svc.paypal.approve(token)
        except PayPalError:
            raise HTTPException(404, "unknown mock order")
        return RedirectResponse(f"/paypal/return?token={token}&PayerID=MOCKPAYER", status_code=303)

    return app


app = None
if os.environ.get("GRB_NO_AUTOAPP") != "1":
    app = create_app()
