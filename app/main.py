"""FastAPI app for GuardRail Buyer."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .agent import LLMError, PurchasingAgent
from .guardrails import Policy
from .paypal_client import make_client
from .service import BuyerService, Catalog

ROOT = Path(__file__).resolve().parent.parent


class AgentRequest(BaseModel):
    request: str = Field(..., min_length=1, max_length=2000,
                         examples=["Buy 2 USB-C cables and a wireless mouse for the new hire"])


def build_service(db_path: str | None = None, paypal_mode: str | None = None) -> BuyerService:
    policy = Policy.load(os.environ.get("POLICY_PATH", ROOT / "policy.yaml"))
    catalog = Catalog.load(os.environ.get("CATALOG_PATH", ROOT / "data" / "catalog.json"))
    return BuyerService(policy, catalog, make_client(paypal_mode),
                        db_path or os.environ.get("DB_PATH", str(ROOT / "guardrail.db")))


def create_app(service: BuyerService | None = None, agent: PurchasingAgent | None = None) -> FastAPI:
    app = FastAPI(title="GuardRail Buyer", version="0.1.0",
                  description="AI purchasing agent with PayPal spend guardrails (Sandbox)")
    svc = service or build_service()
    app.state.service = svc
    app.state.agent = agent or PurchasingAgent(svc)

    @app.get("/health")
    def health():
        return {"ok": True, "paypal": svc.paypal.name, "model": app.state.agent.model}

    @app.get("/policy")
    def get_policy():
        return {**svc.policy.to_dict(), "spent_today": str(svc.spent_today()), "day": svc._today()}

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

    @app.post("/approvals/{approval_id}/approve")
    def approve(approval_id: str):
        return _decide(approval_id, svc.approve)

    @app.post("/approvals/{approval_id}/reject")
    def reject(approval_id: str):
        return _decide(approval_id, svc.reject)

    return app


app = None
if os.environ.get("GRB_NO_AUTOAPP") != "1":
    app = create_app()
