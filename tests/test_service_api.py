from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.paypal_client import MockPayPal, PayPalError, SandboxPayPal


def test_small_order_is_placed_and_counted(service):
    r = service.place_order("P-1001", 2)
    assert r["result"] == "ORDER_PLACED"
    assert r["order"]["status"] == "COMPLETED" and r["order"]["capture_id"].startswith("MOCKCAP-")
    assert str(service.spent_today()) == "25.98"


def test_over_cap_requires_approval_then_approve(service):
    r = service.place_order("P-1003", 1)  # 89.00 > 50
    assert r["result"] == "NEEDS_APPROVAL"
    assert service.store.list("orders") == []
    a = service.request_approval("P-1003", 1, "new hire keyboard")
    assert a["result"] == "APPROVAL_REQUESTED"
    done = service.approve(a["approval_id"])
    assert done["result"] == "ORDER_PLACED"
    assert service.store.get("approvals", a["approval_id"])["status"] == "APPROVED"
    assert str(service.spent_today()) == "89.00"
    with pytest.raises(ValueError):
        service.approve(a["approval_id"])


def test_reject(service):
    a = service.request_approval("P-4002", 1, "license")
    assert service.reject(a["approval_id"])["result"] == "REJECTED"
    assert service.store.list("orders") == []


def test_denied_cannot_be_escalated(service):
    assert service.place_order("P-5001", 1)["result"] == "DENIED"
    assert service.request_approval("P-5001", 1, "please")["result"] == "DENIED"
    assert service.place_order("P-1005", 1)["result"] == "DENIED"  # 640 > hard max


def test_request_approval_not_needed_when_allowed(service):
    assert service.request_approval("P-1001", 1, "x")["result"] == "NOT_NEEDED"


def test_daily_cap_kicks_in_and_resets_next_day(service, clock):
    for _ in range(3):
        assert service.place_order("P-1002", 2)["result"] == "ORDER_PLACED"  # 49.00 each -> 147
    assert service.place_order("P-2001", 1)["result"] == "NEEDS_APPROVAL"  # 147 + 7.49 > 150
    clock.now += timedelta(days=1)
    assert service.place_order("P-2001", 1)["result"] == "ORDER_PLACED"


def test_bad_inputs(service):
    with pytest.raises(KeyError):
        service.check("NOPE", 1)
    with pytest.raises(ValueError):
        service.check("P-1001", 0)


def test_paypal_failure_marks_order_failed_and_not_counted(service):
    class Broken(MockPayPal):
        def create_order(self, *a, **k):
            raise PayPalError("boom")
    service.paypal = Broken()
    r = service.place_order("P-1001", 1)
    assert r["result"] == "FAILED" and r["order"]["status"] == "FAILED"
    assert str(service.spent_today()) == "0.00"


def test_mock_cannot_double_capture():
    m = MockPayPal()
    o = m.create_order(1, "USD", "x", "ref")
    m.capture_order(o["id"])
    with pytest.raises(PayPalError):
        m.capture_order(o["id"])


def test_sandbox_requires_env(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("PAYPAL_AUTH", raising=False)
    with pytest.raises(PayPalError):
        SandboxPayPal()


class FakeAgent:
    model = "fake"

    def __init__(self, svc):
        self.svc = svc

    def run(self, text):
        from app.agent import AgentResult
        self.svc.place_order("P-1001", 1)
        return AgentResult(final_text=f"did: {text}")


def test_rest_endpoints(service):
    client = TestClient(create_app(service, FakeAgent(service), approver_token=""))
    assert client.get("/health").json()["paypal"] == "mock"
    pol = client.get("/policy").json()
    assert pol["per_order_cap"] == "50.00" and pol["spent_today"] == "0.00"
    r = client.post("/agent/request", json={"request": "buy a cable"})
    assert r.status_code == 200 and r.json()["final_text"] == "did: buy a cable"
    orders = client.get("/orders").json()
    assert len(orders) == 1 and orders[0]["decision"]["verdict"] == "ALLOW"
    a = service.request_approval("P-1004", 1, "monitor")
    assert len(client.get("/approvals?status=PENDING").json()) == 1
    assert client.post(f"/approvals/{a['approval_id']}/approve").json()["result"] == "ORDER_PLACED"
    assert client.post(f"/approvals/{a['approval_id']}/approve").status_code == 409
    assert client.post("/approvals/APR-NOPE/reject").status_code == 404
    assert client.post("/agent/request", json={"request": ""}).status_code == 422
