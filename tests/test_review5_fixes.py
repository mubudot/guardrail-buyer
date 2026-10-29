"""Regressions for CODEX_REVIEW_5 (cross-check round, 2026-10-30) and the judge-repro log."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import LLMError, PurchasingAgent, anthropic_post
from app.guardrails import Policy
from app.main import create_app
from app.offline_agent import OfflineAgent
from app.paypal_client import MockPayPal, SandboxPayPal
from app.service import BuyerService, Catalog

CABLE = dict(id="P-1001", name="Cable", price="12.99", merchant="Sandbox Electronics",
             category="office-supplies", currency="USD")


def _svc(paypal):
    return BuyerService(Policy.load("policy.yaml"), Catalog([CABLE]), paypal)


# ---- #1 recovery errors must not erase spend --------------------------------------
def test_already_captured_verification_401_keeps_reservation():
    def wire(req):
        if req.method == "POST" and req.url.path == "/v2/checkout/orders":
            return httpx.Response(201, json={"id": "PP1", "status": "PAYER_ACTION_REQUIRED", "links": []})
        if req.url.path.endswith("/capture"):
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY",
                                             "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
        return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
    s = _svc(SandboxPayPal(auth="proxy", transport=httpx.MockTransport(wire)))
    o = s.place_order("P-1001", 1)["order"]
    out = s.capture(o["id"])
    assert out["status"] == "NEEDS_RECONCILIATION"
    assert s.store.get("orders", o["id"])["capture_unknown"] == 1
    assert str(s.spent_today()) == "12.99"
    # and it can no longer be cancelled
    assert s.cancel(o["id"])["result"] == "NOT_CANCELLABLE"


@pytest.mark.parametrize("replay_status,released", [(401, False), (403, False), (409, False),
                                                    (429, False), (422, True), (400, True)])
def test_uncertain_create_replay_refusal(replay_status, released):
    calls = []

    def wire(req):
        calls.append(req.headers.get("paypal-request-id"))
        if len(calls) == 1:
            raise httpx.ReadTimeout("lost response", request=req)
        return httpx.Response(replay_status, json={"name": "X", "details": [{"issue": "SOMETHING"}]})
    s = _svc(SandboxPayPal(auth="proxy", transport=httpx.MockTransport(wire)))
    o = s.place_order("P-1001", 1)["order"]
    assert s.store.get("orders", o["id"])["status"] == "NEEDS_RECONCILIATION"
    r = s.reconcile(o["id"])
    assert calls[0] == calls[1] == o["id"]  # same idempotency key on the replay
    if released:
        assert r["status"] == "FAILED" and str(s.spent_today()) == "0.00"
    else:
        assert r["status"] == "NEEDS_RECONCILIATION" and str(s.spent_today()) == "12.99"


def test_first_create_refusal_still_fails_and_releases():
    wire = lambda req: httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
    s = _svc(SandboxPayPal(auth="proxy", transport=httpx.MockTransport(wire)))
    out = s.place_order("P-1001", 1)
    assert out["order"]["status"] == "FAILED" and str(s.spent_today()) == "0.00"


# ---- #2 strict tool arguments ------------------------------------------------------
@pytest.mark.parametrize("q", [True, 1.9, "2", float("inf"), None, [1], 0, -1, 101])
def test_tool_quantity_types_are_rejected(q):
    s = _svc(MockPayPal())
    a = PurchasingAgent(s, post=lambda p: {})
    out = a._run_tool("create_order", {"product_id": "P-1001", "quantity": q})
    assert "error" in out and s.store.list("orders") == []


def test_tool_product_id_must_be_string():
    a = PurchasingAgent(_svc(MockPayPal()), post=lambda p: {})
    assert "error" in a._run_tool("create_order", {"product_id": ["P-1001"], "quantity": 1})
    assert "error" in a._run_tool("create_order", "not-an-object")


def _scripted(turns):
    it = iter(turns)
    return lambda payload: next(it)


def _tool_turn(i, name, inp):
    return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}


def test_invalid_quantity_preserves_partial_purchase_result():
    s = _svc(MockPayPal())
    huge = json.loads("1e309")
    a = PurchasingAgent(s, post=_scripted([
        _tool_turn(1, "create_order", {"product_id": "P-1001", "quantity": 1}),
        _tool_turn(2, "create_order", {"product_id": "P-1001", "quantity": huge}),
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": ""}]}]))
    c = TestClient(create_app(s, agent=a, approver_token="t", agent_rate_limit=None))
    r = c.post("/agent/request", json={"request": "a cable"})
    assert r.status_code == 200
    txt = r.json()["final_text"]
    assert "bought and paid" in txt and "not ordered" in txt
    assert str(s.spent_today()) == "12.99"


# ---- #3 max turns keeps completed purchases in the answer ------------------------------
def test_max_turns_reports_orders_already_created():
    s = _svc(MockPayPal())
    turns = [_tool_turn(1, "create_order", {"product_id": "P-1001", "quantity": 1})] + \
            [_tool_turn(i, "search_catalog", {"query": "x"}) for i in range(2, 20)]
    a = PurchasingAgent(s, post=_scripted(turns), max_turns=4)
    res = a.run("a cable")
    assert res.stopped_reason == "max_turns"
    assert "bought and paid" in res.final_text and "too many steps" in res.final_text


# ---- #4 offline quantity > 100 explains the refusal ----------------------------------
def test_offline_quantity_over_100_explains_refusal():
    s = BuyerService(Policy.load("policy.yaml"), Catalog.load("data/catalog.json"), MockPayPal())
    res = OfflineAgent(s).run("Buy 101 USB-C cables")
    assert "between 1 and 100" in res.final_text
    assert s.store.list("orders") == []


# ---- #5 malformed cancel state -> 403 -------------------------------------------------
@pytest.mark.parametrize("state", ["é", "%00", "zz" * 16, "A" * 32, "1" * 33])
def test_non_ascii_or_malformed_cancel_state_returns_403(state):
    s = _svc(MockPayPal(require_payer_approval=True))
    o = s.place_order("P-1001", 1)["order"]
    c = TestClient(create_app(s, approver_token="t", agent_rate_limit=None))
    r = c.get("/paypal/cancel", params={"token": o["paypal_order_id"], "state": state, "format": "json"})
    assert r.status_code == 403
    assert s.store.get("orders", o["id"])["status"] == "PAYER_ACTION_REQUIRED"


# ---- repro R2: no key in Claude mode gives an actionable message ------------------------
def test_missing_key_401_hint(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401, json={"type": "error"}))
    with pytest.raises(LLMError, match="AGENT_MODE=offline"):
        anthropic_post({"model": "m"})


# ---- re-review 1 ----------------------------------------------------------------------
@pytest.mark.parametrize("oauth_status", [400, 401, 422])
def test_replay_oauth_failure_keeps_reservation(oauth_status):
    state = {"orders": 0}

    def wire(req):
        if req.url.path == "/v1/oauth2/token":
            if state["orders"] == 0:
                return httpx.Response(200, json={"access_token": "A", "expires_in": 0})
            return httpx.Response(oauth_status, json={"error": "invalid_client"})
        state["orders"] += 1
        raise httpx.ReadTimeout("lost response", request=req)
    s = _svc(SandboxPayPal(client_id="id", client_secret="sec", transport=httpx.MockTransport(wire)))
    o = s.place_order("P-1001", 1)["order"]
    assert s.store.get("orders", o["id"])["status"] == "NEEDS_RECONCILIATION"
    r = s.reconcile(o["id"])
    assert state["orders"] == 1  # the replay never reached the Orders API
    assert r["status"] == "NEEDS_RECONCILIATION" and str(s.spent_today()) == "12.99"


def test_summary_survives_non_object_tool_input():
    s = _svc(MockPayPal())
    a = PurchasingAgent(s, post=_scripted([
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "create_order", "input": {"product_id": "P-1001", "quantity": 1}},
            {"type": "tool_use", "id": "t2", "name": "create_order", "input": ["P-1001"]}]}]), max_turns=1)
    c = TestClient(create_app(s, agent=a, approver_token="t", agent_rate_limit=None))
    r = c.post("/agent/request", json={"request": "a cable"})
    assert r.status_code == 200 and "bought and paid" in r.json()["final_text"]


@pytest.mark.parametrize("body", [b"not json", b'{"no_token": 1}', b'{"access_token": "A", "expires_in": "x"}'])
def test_unreadable_oauth_body_is_an_oauth_error(body):
    from app.paypal_client import PayPalError
    pp = SandboxPayPal(client_id="id", client_secret="sec",
                       transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body)))
    with pytest.raises(PayPalError) as ei:
        pp.get_order("X")
    assert ei.value.source == "oauth" and ei.value.ambiguous is False
