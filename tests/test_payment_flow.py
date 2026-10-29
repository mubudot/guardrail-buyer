"""Buyer-approval round trip, spend reservation, approver token, Sandbox client wire format."""
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import strip_intro
from app.main import create_app
from app.paypal_client import MockPayPal, PayPalError, SandboxPayPal
from app.service import BuyerService


@pytest.fixture
def payer_service(policy, catalog, clock):
    return BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)


def test_order_waits_for_payer_and_reserves_budget(payer_service):
    r = payer_service.place_order("P-1001", 2)
    assert r["result"] == "AWAITING_PAYER_APPROVAL"
    o = r["order"]
    assert o["status"] == "PAYER_ACTION_REQUIRED" and "mock-checkout?token=" in o["approve_url"]
    s = payer_service.spend_summary()
    assert (s["reserved"], s["captured"], s["total"]) == ("25.98", "0.00", "25.98")
    assert str(payer_service.spent_today()) == "25.98"  # reservation counts toward the daily cap


def test_capture_on_return_after_approval(payer_service):
    o = payer_service.place_order("P-1001", 1)["order"]
    # returning before the buyer approved does not capture and keeps the reservation
    early = payer_service.complete_payment(o["paypal_order_id"])
    assert early["status"] == "PAYER_ACTION_REQUIRED" and "ORDER_NOT_APPROVED" in early["error"]
    payer_service.paypal.approve(o["paypal_order_id"])
    done = payer_service.complete_payment(o["paypal_order_id"])
    assert done["status"] == "COMPLETED" and done["capture_id"].startswith("MOCKCAP-")
    # idempotent: a second return (browser refresh) does not double capture
    assert payer_service.complete_payment(o["paypal_order_id"])["status"] == "COMPLETED"
    s = payer_service.spend_summary()
    assert (s["captured"], s["reserved"]) == ("12.99", "0.00")


def test_cancel_releases_reservation(payer_service):
    o = payer_service.place_order("P-1002", 2)["order"]
    assert payer_service.cancel_payment(o["paypal_order_id"])["status"] == "CANCELLED"
    assert str(payer_service.spent_today()) == "0.00"
    with pytest.raises(ValueError):
        payer_service.complete_payment(o["paypal_order_id"])
    with pytest.raises(KeyError):
        payer_service.cancel_payment("NOPE")


def test_unapproved_orders_expire_and_release(payer_service, clock):
    o = payer_service.place_order("P-1002", 2)["order"]
    clock.now += payer_service.approval_ttl + timedelta(minutes=1)
    assert payer_service.expire_stale() == [o["id"]]
    assert payer_service.store.get("orders", o["id"])["status"] == "EXPIRED"
    assert str(payer_service.ledger.spent_on("2026-10-30", "USD")) == "0.00"


def test_reservations_block_cap_dodging(payer_service):
    for _ in range(3):  # 3 x 49.00 open orders, none paid yet
        assert payer_service.place_order("P-1002", 2)["result"] == "AWAITING_PAYER_APPROVAL"
    assert payer_service.place_order("P-2001", 1)["result"] == "NEEDS_APPROVAL"


def test_capture_failure_releases(payer_service):
    o = payer_service.place_order("P-1001", 1)["order"]
    payer_service.paypal.approve(o["paypal_order_id"])

    def boom(_):
        raise PayPalError("UNPROCESSABLE_ENTITY INSTRUMENT_DECLINED")
    payer_service.paypal.capture_order = boom
    out = payer_service.complete_payment(o["paypal_order_id"])
    assert out["status"] == "CAPTURE_FAILED"
    assert str(payer_service.spent_today()) == "0.00"


def test_approved_request_goes_through_payer_flow(payer_service):
    a = payer_service.request_approval("P-1003", 1, "keyboard")
    r = payer_service.approve(a["approval_id"])
    assert r["result"] == "AWAITING_PAYER_APPROVAL" and r["order"]["approval_id"] == a["approval_id"]


def test_http_round_trip_and_mock_checkout(payer_service):
    c = TestClient(create_app(payer_service, agent=_NoAgent(), approver_token=""))
    o = payer_service.place_order("P-1001", 1)["order"]
    page = c.get(f"/paypal/mock-checkout?token={o['paypal_order_id']}")
    assert page.status_code == 200 and "Pay now" in page.text and "Mock checkout" in page.text
    r = c.get(f"/paypal/mock-approve?token={o['paypal_order_id']}", follow_redirects=True)
    assert r.status_code == 200 and "GuardRail Buyer" in r.text  # landed on the dashboard
    assert payer_service.store.get("orders", o["id"])["status"] == "COMPLETED"
    o2 = payer_service.place_order("P-1001", 1)["order"]
    j = c.get(f"/paypal/cancel?token={o2['paypal_order_id']}&format=json").json()
    assert j["status"] == "CANCELLED"
    assert c.get("/paypal/return?token=NOPE").status_code == 404
    assert c.get("/spend").json()["captured"] == "12.99"


class _NoAgent:
    model = "none"


def test_approver_token_required(service):
    c = TestClient(create_app(service, agent=_NoAgent(), approver_token="s3cret"))
    a = service.request_approval("P-1003", 1, "kb")["approval_id"]
    assert c.post(f"/approvals/{a}/approve").status_code == 401
    assert c.post(f"/approvals/{a}/approve", headers={"X-Approver-Token": "wrong"}).status_code == 401
    assert c.post(f"/approvals/{a}/reject", headers={"Authorization": "Bearer s3cret"}).json()["result"] == "REJECTED"
    assert c.get("/health").json()["approver_auth"] is True


def test_dashboard_served(service):
    c = TestClient(create_app(service, agent=_NoAgent(), approver_token=""))
    r = c.get("/")
    assert r.status_code == 200 and "ag-grid-community" in r.text
    assert c.get("/static/app.js").status_code == 200


def test_strip_intro():
    assert strip_intro("I'm GuardRail Buyer, a purchasing agent for a small team. Found 2 cables.") == "Found 2 cables."
    assert strip_intro("Bought it.") == "Bought it."


def _fake_paypal(seen):
    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "TKN", "expires_in": 3600})
        if req.url.path == "/v2/checkout/orders":
            return httpx.Response(200, json={"id": "5O1", "status": "PAYER_ACTION_REQUIRED", "links": [
                {"rel": "self", "href": "x"},
                {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=5O1"}]})
        if req.url.path.endswith("/capture"):
            return httpx.Response(201, json={"id": "5O1", "status": "COMPLETED", "purchase_units": [
                {"payments": {"captures": [{"id": "CAP1", "status": "COMPLETED"}]}}]})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def test_sandbox_oauth_wire_format():
    seen = []
    pp = SandboxPayPal("cid", "sec", auth="oauth", transport=_fake_paypal(seen),
                       return_url="http://h/r", cancel_url="http://h/c")
    o = pp.create_order(12.5, "USD", "1 x Cable", "ORD-1")
    assert o["approve_url"].endswith("token=5O1") and o["status"] == "PAYER_ACTION_REQUIRED"
    tok, create = seen[0], seen[1]
    assert tok.headers["authorization"].startswith("Basic ")
    assert create.headers["authorization"] == "Bearer TKN" and create.headers["paypal-request-id"] == "ORD-1"
    body = json.loads(create.content)
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "12.50"}
    assert body["payment_source"]["paypal"]["experience_context"]["return_url"] == "http://h/r"
    assert pp.capture_order("5O1")["capture_id"] == "CAP1"
    assert len([r for r in seen if r.url.path == "/v1/oauth2/token"]) == 1  # token cached


def test_sandbox_proxy_mode_sends_no_credentials(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    seen = []
    pp = SandboxPayPal(auth="proxy", transport=_fake_paypal(seen))
    pp.create_order(1, "USD", "x", "ORD-2")
    assert [r.url.path for r in seen] == ["/v2/checkout/orders"]
    assert "authorization" not in seen[0].headers


# ---- iteration 2: real Sandbox error shapes (recorded from api-m.sandbox.paypal.com) ----
NOT_APPROVED_422 = {
    "name": "UNPROCESSABLE_ENTITY",
    "details": [{"issue": "ORDER_NOT_APPROVED",
                 "description": "Payer has not yet approved the Order for payment."}],
    "message": "The requested action could not be performed, semantically incorrect, or failed business validation.",
    "debug_id": "f132290fbbab9"}


def _sandbox_states(state):
    """Fake Sandbox whose order is approved only once state['approved'] is True."""
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        p = req.url.path
        if p == "/v2/checkout/orders":
            return httpx.Response(200, json={"id": "8AB", "status": "PAYER_ACTION_REQUIRED", "links": [
                {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=8AB"}]})
        if p == "/v2/checkout/orders/8AB/capture":
            if state.get("captured"):
                return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY",
                                                 "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
            if not state.get("approved"):
                return httpx.Response(422, json=NOT_APPROVED_422)
            state["captured"] = True
            return httpx.Response(201, json={"id": "8AB", "status": "COMPLETED", "purchase_units": [
                {"payments": {"captures": [{"id": "CAP9", "status": "COMPLETED"}]}}]})
        if p == "/v2/checkout/orders/8AB":
            st = "COMPLETED" if state.get("captured") else ("APPROVED" if state.get("approved") else "PAYER_ACTION_REQUIRED")
            body = {"id": "8AB", "status": st}
            if state.get("captured"):
                body["purchase_units"] = [{"payments": {"captures": [{"id": "CAP9", "status": "COMPLETED"}]}}]
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
    return httpx.MockTransport(handler), seen


def test_sandbox_error_is_parsed():
    transport, _ = _sandbox_states({})
    pp = SandboxPayPal(auth="proxy", transport=transport)
    pp.create_order(1, "USD", "x", "ORD-9")
    with pytest.raises(PayPalError) as ei:
        pp.capture_order("8AB")
    e = ei.value
    assert (e.status_code, e.issue, e.debug_id) == (422, "ORDER_NOT_APPROVED", "f132290fbbab9")
    assert "Payer has not" not in str(e)  # no raw body echoed into stored status


def test_sandbox_capture_uses_fresh_request_id_and_handles_already_captured():
    state = {}
    transport, seen = _sandbox_states(state)
    pp = SandboxPayPal(auth="proxy", transport=transport)
    with pytest.raises(PayPalError):
        pp.capture_order("8AB")
    state["approved"] = True
    assert pp.capture_order("8AB")["capture_id"] == "CAP9"
    ids = [r.headers["paypal-request-id"] for r in seen if r.url.path.endswith("/capture")]
    assert len(set(ids)) == 2  # a retried capture must not replay the earlier 422
    again = pp.capture_order("8AB")  # ORDER_ALREADY_CAPTURED -> current order, no error
    assert again["status"] == "COMPLETED" and again["capture_id"] == "CAP9"


def test_service_with_sandbox_not_approved_then_approved(policy, catalog, clock):
    state = {}
    transport, _ = _sandbox_states(state)
    svc = BuyerService(policy, catalog, SandboxPayPal(auth="proxy", transport=transport), ":memory:", clock=clock)
    c = TestClient(create_app(svc, approver_token="t"))
    o = svc.place_order("P-1001", 1)["order"]
    assert o["paypal_order_id"] == "8AB"
    r = c.post(f"/orders/{o['id']}/capture")
    assert r.status_code == 409 and r.json()["detail"]["paypal_issue"] == "ORDER_NOT_APPROVED"
    assert c.get(f"/orders/{o['id']}").json()["status"] == "PAYER_ACTION_REQUIRED"
    assert svc.spend_summary()["reserved"] == "12.99"
    # return redirect before approval: no capture, back to dashboard
    r = c.get("/paypal/return", params={"token": "8AB", "format": "json"})
    assert r.json()["result"] == "NOT_APPROVED_YET"
    state["approved"] = True
    live = c.get(f"/orders/{o['id']}").json()
    assert live["paypal_live_status"] == "APPROVED"
    done = c.post(f"/orders/{o['id']}/capture").json()
    assert done["status"] == "COMPLETED" and done["capture_id"] == "CAP9"
    assert c.post(f"/orders/{o['id']}/capture").json()["result"] == "ALREADY_CAPTURED"
    assert svc.spend_summary()["captured"] == "12.99"


def test_cancel_endpoint_releases_and_blocks_capture(policy, catalog, clock):
    transport, _ = _sandbox_states({})
    svc = BuyerService(policy, catalog, SandboxPayPal(auth="proxy", transport=transport), ":memory:", clock=clock)
    c = TestClient(create_app(svc, approver_token="t"))
    o = svc.place_order("P-1001", 1)["order"]
    assert c.post(f"/orders/{o['id']}/cancel").status_code == 401
    assert c.post(f"/orders/{o['id']}/cancel", headers={"X-Approver-Token": "t"}).json()["status"] == "CANCELLED"
    assert svc.spend_summary()["total"] == "0.00"
    assert c.post(f"/orders/{o['id']}/capture").status_code == 409
    assert c.get("/orders/ORD-NOPE").status_code == 404


def test_capture_declined_releases(policy, catalog, clock):
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    o = svc.place_order("P-1001", 1)["order"]
    svc.paypal.approve(o["paypal_order_id"])
    svc.paypal.capture_order = lambda oid: {"id": oid, "status": "COMPLETED", "capture_id": "C",
                                            "capture_status": "DECLINED", "raw": {}}
    out = svc.complete_payment(o["paypal_order_id"])
    assert out["status"] == "CAPTURE_FAILED" and svc.spend_summary()["total"] == "0.00"
