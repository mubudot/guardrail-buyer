"""Regressions for CODEX_REVIEW_1 B4 (ambiguous outcomes), B5 (quote binding),
B8 (offline negation / quantity 0), splitting aggregation, B9 (mock restart) and
the vendored AG Grid assets."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import LLMError, PurchasingAgent
from app.main import ROOT, create_app
from app.offline_agent import OfflineAgent
from app.paypal_client import MockPayPal, PayPalError, SandboxPayPal
from app.service import BuyerService


@pytest.fixture
def psvc(policy, catalog, clock):
    return BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)


def _approved_order(svc, pid="P-1001", qty=1):
    o = svc.place_order(pid, qty)["order"]
    svc.paypal.approve(o["paypal_order_id"])
    return o


# ---- B4: ambiguous capture / create outcomes ---------------------------------
def test_b4_capture_succeeds_then_500_is_counted_not_released(psvc):
    o = _approved_order(psvc)
    real = psvc.paypal.capture_order

    def capture_then_500(oid):
        real(oid)  # money moved at the provider...
        raise PayPalError("capture_order failed: HTTP 500 INTERNAL_SERVER_ERROR", status_code=500)
    psvc.paypal.capture_order = capture_then_500
    out = psvc.complete_payment(o["paypal_order_id"])
    # ...the immediate provider check confirms the capture
    assert out["status"] == "COMPLETED" and out["capture_id"].startswith("MOCKCAP-")
    assert psvc.spend_summary()["captured"] == "12.99"


def test_b4_500_without_capture_keeps_reservation_then_reconciles(psvc):
    o = _approved_order(psvc)
    real = psvc.paypal.capture_order
    psvc.paypal.capture_order = lambda oid: (_ for _ in ()).throw(PayPalError("HTTP 503", status_code=503))
    out = psvc.complete_payment(o["paypal_order_id"])
    assert out["status"] == "NEEDS_RECONCILIATION" and out["result"] == "PAYMENT_UNCONFIRMED"
    s = psvc.spend_summary()
    assert (s["reserved"], s["needs_reconciliation"]) == ("12.99", 1)
    assert psvc.cancel(o["id"])["result"] == "NOT_CANCELLABLE"  # unknown outcome is never released
    psvc.paypal.capture_order = real
    done = psvc.reconcile(o["id"])  # provider says APPROVED -> capture now
    assert done["status"] == "COMPLETED" and psvc.spend_summary()["captured"] == "12.99"
    assert psvc.reconcile(o["id"])["result"] == "NOTHING_TO_RECONCILE"


def test_b4_unexpected_exception_is_ambiguous(psvc):
    o = _approved_order(psvc)
    psvc.paypal.capture_order = lambda oid: (_ for _ in ()).throw(RuntimeError("socket closed"))
    out = psvc.complete_payment(o["paypal_order_id"])
    assert out["status"] == "NEEDS_RECONCILIATION" and str(psvc.spent_today()) == "12.99"


@pytest.mark.parametrize("cap_status", ["PENDING", None])
def test_b4_pending_or_missing_capture_status_is_not_completed(psvc, cap_status):
    o = _approved_order(psvc)
    psvc.paypal.capture_order = lambda oid: {"id": oid, "status": "COMPLETED", "capture_id": "C1",
                                             "capture_status": cap_status, "raw": {}}
    psvc.paypal.get_order = lambda oid: {"id": oid, "status": "COMPLETED", "capture_id": "C1",
                                         "capture_status": cap_status, "raw": {}}
    out = psvc.complete_payment(o["paypal_order_id"])
    assert out["status"] == "NEEDS_RECONCILIATION" and psvc.spend_summary()["reserved"] == "12.99"
    # later the provider reports the capture COMPLETED
    psvc.paypal.get_order = lambda oid: {"id": oid, "status": "COMPLETED", "capture_id": "C1",
                                         "capture_status": "COMPLETED", "raw": {}}
    assert psvc.reconcile(o["id"])["status"] == "COMPLETED"
    assert psvc.spend_summary()["captured"] == "12.99"


def test_b4_ambiguous_create_keeps_reservation_and_replays_idempotently(psvc):
    real = psvc.paypal.create_order

    def create_then_timeout(*a, **k):
        real(*a, **k)  # PayPal created it, but the response was lost
        raise PayPalError("create_order failed: transport error ReadTimeout", issue="TRANSPORT_ERROR",
                          ambiguous=True)
    psvc.paypal.create_order = create_then_timeout
    r = psvc.place_order("P-1002", 2)
    assert r["result"] == "PAYMENT_UNCONFIRMED" and r["order"]["status"] == "NEEDS_RECONCILIATION"
    assert str(psvc.spent_today()) == "49.00"  # still counts against the daily cap
    psvc.paypal.create_order = real
    out = psvc.reconcile(r["order"]["id"])
    assert out["status"] == "PAYER_ACTION_REQUIRED" and len(psvc.paypal.orders) == 1  # same PayPal order


def test_b4_definite_create_failure_still_releases(psvc):
    psvc.paypal.create_order = lambda *a, **k: (_ for _ in ()).throw(
        PayPalError("HTTP 422 UNPROCESSABLE_ENTITY", status_code=422))
    r = psvc.place_order("P-1001", 1)
    assert r["result"] == "FAILED" and str(psvc.spent_today()) == "0.00"


def test_b4_paypal_error_ambiguity_flags():
    assert PayPalError("x", status_code=500).ambiguous
    assert not PayPalError("x", status_code=422).ambiguous
    assert not PayPalError("x").ambiguous


def _sandbox(handler):
    return SandboxPayPal(auth="proxy", transport=httpx.MockTransport(handler))


def test_b4_sandbox_transport_errors_are_normalized(policy, catalog, clock):
    state = {"captured": False}

    def handler(req):
        p = req.url.path
        if p == "/v2/checkout/orders":
            return httpx.Response(201, json={"id": "T1", "status": "PAYER_ACTION_REQUIRED", "links": []})
        if p.endswith("/capture"):
            state["captured"] = True  # captured at PayPal, then the connection drops
            raise httpx.ReadTimeout("timed out", request=req)
        if p == "/v2/checkout/orders/T1":
            body = {"id": "T1", "status": "COMPLETED" if state["captured"] else "APPROVED"}
            if state["captured"]:
                body["purchase_units"] = [{"payments": {"captures": [{"id": "CAPX", "status": "COMPLETED"}]}}]
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={})
    pp = _sandbox(handler)
    with pytest.raises(PayPalError) as ei:
        pp.capture_order("T1")
    assert ei.value.ambiguous and ei.value.issue == "TRANSPORT_ERROR"
    state["captured"] = False
    svc = BuyerService(policy, catalog, pp, ":memory:", clock=clock)
    o = svc.place_order("P-1001", 1)["order"]
    out = svc.capture(o["id"])  # no raw httpx exception escapes
    assert out["status"] == "COMPLETED" and out["capture_id"] == "CAPX"


def test_b4_sandbox_5xx_is_ambiguous():
    pp = _sandbox(lambda req: httpx.Response(502, json={"name": "BAD_GATEWAY"}))
    with pytest.raises(PayPalError) as ei:
        pp.create_order(1, "USD", "x", "ORD-1")
    assert ei.value.ambiguous and ei.value.status_code == 502


def test_b4_reconcile_endpoint(psvc):
    c = TestClient(create_app(psvc, agent=_NoAgent(), approver_token="t"))
    o = _approved_order(psvc)
    real = psvc.paypal.capture_order
    psvc.paypal.capture_order = lambda oid: (_ for _ in ()).throw(PayPalError("HTTP 503", status_code=503))
    assert psvc.complete_payment(o["paypal_order_id"])["status"] == "NEEDS_RECONCILIATION"
    psvc.paypal.capture_order = real
    assert c.post(f"/orders/{o['id']}/reconcile").json()["status"] == "COMPLETED"
    assert c.post("/orders/ORD-NOPE/reconcile").status_code == 404


def test_b4_agent_keeps_partial_result_on_llm_error(service):
    calls = []

    def post(payload):
        calls.append(1)
        if len(calls) == 1:
            return {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "t1", "name": "create_order",
                 "input": {"product_id": "P-1001", "quantity": 1}}]}
        raise LLMError("Messages API transport error: ReadTimeout")
    res = PurchasingAgent(service, post=post).run("buy a cable")
    assert res.stopped_reason == "llm_error" and "bought" in res.final_text
    assert str(service.spent_today()) == "12.99"


# ---- B5: approvals bind the quote ----------------------------------------------
def test_b5_price_change_requires_fresh_approval(service):
    aid = service.request_approval("P-1003", 1, "keyboard")["approval_id"]
    service.catalog.products["P-1003"] = {**service.catalog.products["P-1003"], "price": "499.00"}
    out = service.approve(aid)
    assert out["result"] == "QUOTE_CHANGED"
    assert out["approved_quote"]["amount"] == "89.00" and out["current_quote"]["amount"] == "499.00"
    assert service.store.list("orders") == [] and str(service.spent_today()) == "0.00"
    assert service.store.get("approvals", aid)["status"] == "QUOTE_CHANGED"
    with pytest.raises(ValueError):
        service.approve(aid)  # stays refused; a fresh request is needed
    fresh = service.request_approval("P-1003", 1, "keyboard at new price")
    assert service.approve(fresh["approval_id"])["order"]["amount"] == "499.00"


@pytest.mark.parametrize("change", [{"merchant": "Sandbox Furniture Co"}, {"currency": "EUR"},
                                    {"category": "software"}])
def test_b5_material_term_change_refused(service, change):
    aid = service.request_approval("P-1003", 1, "keyboard")["approval_id"]
    service.catalog.products["P-1003"] = {**service.catalog.products["P-1003"], **change}
    assert service.approve(aid)["result"] == "QUOTE_CHANGED"
    assert service.store.list("orders") == []


def test_b5_removed_product_and_api_409(service):
    c = TestClient(create_app(service, agent=_NoAgent(), approver_token=""))
    aid = service.request_approval("P-1003", 1, "keyboard")["approval_id"]
    del service.catalog.products["P-1003"]
    r = c.post(f"/approvals/{aid}/approve")
    assert r.status_code == 409 and "fresh approval" in r.json()["detail"]
    assert service.store.list("orders") == []


# ---- B8: offline parser refuses negation and quantity 0 ----------------------------
@pytest.mark.parametrize("text", [
    "Don't buy any USB-C cables", "do not buy a wireless mouse", "Dont order coffee beans",
    "never buy printer paper", "no keyboards please", "buy 2 cables and don’t buy the mouse",
    "buy a monitor if it is under $300"])
def test_b8_negated_or_conditional_requests_buy_nothing(service, text):
    res = OfflineAgent(service).run(text)
    assert res.stopped_reason == "offline_refused" and "Nothing was ordered" in res.final_text
    assert service.store.list("orders") == [] and service.store.list("approvals") == []


@pytest.mark.parametrize("text", ["0 USB-C cables", "zero wireless mice"])
def test_b8_quantity_zero_buys_nothing(service, text):
    res = OfflineAgent(service).run(text)
    assert "quantity 0" in res.final_text and service.store.list("orders") == []


def test_b8_positive_requests_with_notes_still_work(service):
    res = OfflineAgent(service).run("2 sticky notes")
    assert service.store.list("orders")[0]["product_id"] == "P-2002" and res.stopped_reason == "offline_rules"


# ---- splitting: same SKU/merchant within one agent request counts together --------
def test_split_orders_in_one_request_aggregate(service):
    r = [service.place_order("P-1002", 1, request_id="REQ-A")["result"] for _ in range(3)]
    assert r == ["ORDER_PLACED", "ORDER_PLACED", "NEEDS_APPROVAL"]  # 24.50, 49.00, 73.50 > 50
    third = service.check("P-1002", 1, request_id="REQ-A")
    assert third["verdict"] == "NEEDS_APPROVAL" and "split orders count as one purchase" in third["reasons"][0]
    assert service.request_approval("P-1002", 1, "3rd mouse", request_id="REQ-A")["result"] == "APPROVAL_REQUESTED"
    # a different request, or a different SKU in the same request, is not aggregated
    assert service.place_order("P-1002", 1, request_id="REQ-B")["result"] == "ORDER_PLACED"
    assert service.place_order("P-1001", 1, request_id="REQ-A")["result"] == "ORDER_PLACED"


def test_split_released_orders_do_not_count(psvc):
    o = psvc.place_order("P-1002", 2, request_id="REQ-C")["order"]  # 49.00
    assert psvc.place_order("P-1002", 1, request_id="REQ-C")["result"] == "NEEDS_APPROVAL"
    psvc.cancel(o["id"])
    assert psvc.place_order("P-1002", 1, request_id="REQ-C")["result"] == "AWAITING_PAYER_APPROVAL"


def test_split_via_offline_agent(service):
    res = OfflineAgent(service).run("a wireless mouse, a wireless mouse, a wireless mouse")
    created = [t["output"]["result"] for t in res.transcript if t.get("name") == "create_order"]
    assert created == ["ORDER_PLACED", "ORDER_PLACED", "NEEDS_APPROVAL"]
    assert res.to_dict()["request_id"].startswith("REQ-")
    assert all(o["request_id"] == res.request_id for o in service.store.list("orders"))


def test_split_via_tool_calls_of_claude_agent(service):
    """A model that splits 3 mice into 3 create_order calls is caught server-side."""
    tool = {"type": "tool_use", "name": "create_order", "input": {"product_id": "P-1002", "quantity": 1}}
    replies = iter([
        {"stop_reason": "tool_use", "content": [{**tool, "id": f"t{i}"} for i in range(3)]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "<summary>done</summary>"}]}])
    res = PurchasingAgent(service, post=lambda p: next(replies)).run("3 mice")
    created = [t["output"]["result"] for t in res.transcript if t.get("name") == "create_order"]
    assert created == ["ORDER_PLACED", "ORDER_PLACED", "NEEDS_APPROVAL"]


# ---- B9: mock provider state is in-memory; restart invalidates old mock orders -----
def test_b9_restart_marks_lost_mock_orders(policy, catalog, clock, tmp_path):
    db = str(tmp_path / "g.db")
    svc1 = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), db, clock=clock)
    o = svc1.place_order("P-1001", 1)["order"]
    done = svc1.place_order("P-1002", 1)["order"]
    svc1.paypal.approve(done["paypal_order_id"])
    svc1.complete_payment(done["paypal_order_id"])
    # "restart": new process, same DB, fresh in-memory mock
    svc2 = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), db, clock=clock)
    c = TestClient(create_app(svc2, agent=_NoAgent(), approver_token=""))
    assert svc2.store.get("orders", o["id"])["status"] == "MOCK_RESET"
    assert svc2.store.get("orders", done["id"])["status"] == "COMPLETED"  # history kept
    s = svc2.spend_summary()
    assert (s["captured"], s["reserved"]) == ("24.50", "0.00")
    r = c.get(f"/paypal/mock-checkout?token={o['paypal_order_id']}")
    assert r.status_code == 404 and "reset when the server restarts" in r.json()["detail"]


# ---- vendored AG Grid: dashboard works without a CDN ------------------------------
def test_dashboard_uses_vendored_ag_grid(service):
    c = TestClient(create_app(service, agent=_NoAgent(), approver_token=""))
    html = c.get("/").text
    assert "cdn.jsdelivr" not in html and "http://" not in html and "https://" not in html
    base = "/static/vendor/ag-grid-community-32.3.3/"
    for f in ("ag-grid-community.min.js", "ag-grid.min.css", "ag-theme-quartz.min.css"):
        assert base + f in html
        assert c.get(base + f).status_code == 200
    lic = (ROOT / "app/static/vendor/ag-grid-community-32.3.3/LICENSE.txt").read_text()
    assert "MIT" in lic and "AG GRID LTD" in lic


class _NoAgent:
    model = "none"
