"""Final cross-check (Codex, 2026-11-01): recovering an order whose payment outcome is unknown
(NEEDS_RECONCILIATION) is approver-only. The public capture and PayPal-return routes must not
retry create/capture for such an order; they leave it untouched and point to /reconcile."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.paypal_client import MockPayPal, PayPalError
from app.service import BuyerService


class _NoAgent:
    def run(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError("agent not used")


@pytest.fixture
def psvc(policy, catalog, clock):
    return BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)


def _boom(*_a, **_k):
    raise PayPalError("HTTP 503", status_code=503)


def _counting(fn, calls):
    def wrapped(*a, **k):
        calls.append(a)
        return fn(*a, **k)
    return wrapped


def test_ambiguous_capture_not_recovered_by_public_routes(psvc):
    c = TestClient(create_app(psvc, agent=_NoAgent(), approver_token="t"))
    o = psvc.place_order("P-1001", 1)["order"]
    psvc.paypal.approve(o["paypal_order_id"])
    real = psvc.paypal.capture_order
    psvc.paypal.capture_order = _boom
    assert psvc.complete_payment(o["paypal_order_id"])["status"] == "NEEDS_RECONCILIATION"
    calls = []
    psvc.paypal.capture_order = _counting(real, calls)
    before = psvc.spend_summary()

    r = c.post(f"/orders/{o['id']}/capture")
    assert r.status_code == 409 and r.json()["detail"]["result"] == "NEEDS_RECONCILIATION"
    assert "/reconcile" in r.json()["detail"]["message"]
    r = c.get("/paypal/return", params={"token": o["paypal_order_id"], "format": "json"})
    assert r.status_code == 200 and r.json()["result"] == "NEEDS_RECONCILIATION"
    r = c.get("/paypal/return", params={"token": o["paypal_order_id"]}, follow_redirects=False)
    assert r.status_code == 303 and "status=NEEDS_RECONCILIATION" in r.headers["location"]

    assert calls == []  # no capture retried without the approver token
    assert psvc.store.get("orders", o["id"])["status"] == "NEEDS_RECONCILIATION"
    assert psvc.spend_summary() == before

    done = c.post(f"/orders/{o['id']}/reconcile", headers={"X-Approver-Token": "t"}).json()
    assert done["status"] == "COMPLETED" and len(calls) == 1


def test_ambiguous_create_not_replayed_by_public_capture(psvc):
    c = TestClient(create_app(psvc, agent=_NoAgent(), approver_token="t"))
    real = psvc.paypal.create_order
    psvc.paypal.create_order = _boom
    out = psvc.place_order("P-1001", 1)
    oid = out["order"]["id"]
    assert psvc.store.get("orders", oid)["status"] == "NEEDS_RECONCILIATION"
    calls = []
    psvc.paypal.create_order = _counting(real, calls)
    before = psvc.spend_summary()

    r = c.post(f"/orders/{oid}/capture")
    assert r.status_code == 409 and r.json()["detail"]["result"] == "NEEDS_RECONCILIATION"
    assert calls == []  # the unknown create is not replayed from a public route
    assert psvc.store.get("orders", oid)["status"] == "NEEDS_RECONCILIATION"
    assert psvc.spend_summary() == before

    assert c.post(f"/orders/{oid}/reconcile").status_code == 401
    assert calls == []
