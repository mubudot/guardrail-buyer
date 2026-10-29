"""Concurrency regressions for CODEX_REVIEW_1 B1 (budget race), B2 (double approval)
and B3 (cancel/expiry vs capture). Threads + barriers/events force the interleavings
the review reproduced; every test asserts the safe outcome."""
import threading
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.paypal_client import MockPayPal
from app.service import BuyerService


class SlowCreateMock(MockPayPal):
    """Mock PayPal whose create_order is slow, widening the check->reserve window."""

    def __init__(self, delay=0.2, **kw):
        super().__init__(**kw)
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def create_order(self, *a, **k):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return super().create_order(*a, **k)


def _run_parallel(n, fn):
    """Start n threads at the same instant (barrier) and collect fn(i) results."""
    start = threading.Barrier(n)
    out = [None] * n

    def work(i):
        start.wait()
        out[i] = fn(i)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    return out


# ---- B1: concurrent orders must not exceed the daily cap -------------------
def test_b1_concurrent_orders_same_service_respect_daily_cap(policy, catalog, clock):
    svc = BuyerService(policy, catalog, SlowCreateMock(require_payer_approval=True), ":memory:", clock=clock)
    results = _run_parallel(4, lambda i: svc.place_order("P-1002", 2)["result"])  # 4 x 49.00, cap 150
    assert sorted(results) == ["AWAITING_PAYER_APPROVAL"] * 3 + ["NEEDS_APPROVAL"]
    assert svc.spent_today() <= policy.daily_cap and str(svc.spent_today()) == "147.00"
    assert svc.paypal.calls == 3  # the refused order never reached PayPal


def test_b1_concurrent_orders_separate_connections_respect_daily_cap(policy, catalog, clock, tmp_path):
    """Separate BuyerService instances on one DB file = separate SQLite connections,
    the same situation as several server processes."""
    db = str(tmp_path / "shared.db")
    svcs = [BuyerService(policy, catalog, SlowCreateMock(require_payer_approval=True), db, clock=clock)
            for _ in range(4)]
    results = _run_parallel(4, lambda i: svcs[i].place_order("P-1002", 2)["result"])
    assert sorted(results) == ["AWAITING_PAYER_APPROVAL"] * 3 + ["NEEDS_APPROVAL"]
    assert str(svcs[0].spent_today()) == "147.00"
    assert len(svcs[0].store.list("orders")) == 3


def test_b1_reservation_exists_before_paypal_is_called(policy, catalog, clock):
    seen = {}

    class Spy(MockPayPal):
        def create_order(self, amount, currency, description, reference_id, items=None):
            seen["spent_during_create"] = str(svc.spent_today())
            seen["status_during_create"] = svc.store.get("orders", reference_id)["status"]
            return super().create_order(amount, currency, description, reference_id, items)
    svc = BuyerService(policy, catalog, Spy(require_payer_approval=True), ":memory:", clock=clock)
    svc.place_order("P-1001", 1)
    assert seen == {"spent_during_create": "12.99", "status_during_create": "CREATING"}


# ---- B2: one approval -> exactly one order ---------------------------------
def test_b2_concurrent_approve_creates_one_order(policy, catalog, clock):
    svc = BuyerService(policy, catalog, SlowCreateMock(require_payer_approval=True), ":memory:", clock=clock)
    aid = svc.request_approval("P-1003", 1, "keyboard")["approval_id"]
    results = _run_parallel(3, lambda i: svc.approve(aid))
    order_ids = {r["order"]["id"] for r in results}
    assert len(order_ids) == 1
    assert len(svc.store.list("orders")) == 1 and svc.paypal.calls == 1
    assert str(svc.spent_today()) == "89.00"
    assert svc.store.get("approvals", aid)["order_id"] in order_ids
    assert svc.store.get("approvals", aid)["status"] == "APPROVED"


def test_b2_concurrent_approve_separate_connections(policy, catalog, clock, tmp_path):
    db = str(tmp_path / "shared.db")
    svcs = [BuyerService(policy, catalog, SlowCreateMock(require_payer_approval=True), db, clock=clock)
            for _ in range(3)]
    aid = svcs[0].request_approval("P-1003", 1, "keyboard")["approval_id"]
    results = _run_parallel(3, lambda i: svcs[i].approve(aid))
    assert len({r["order"]["id"] for r in results}) == 1
    assert len(svcs[0].store.list("orders")) == 1 and str(svcs[0].spent_today()) == "89.00"


# ---- B3: cancel / expiry cannot override a capture --------------------------
class GatedCaptureMock(MockPayPal):
    """capture_order signals `entered`, then blocks until `go` is set."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered, self.go = threading.Event(), threading.Event()

    def capture_order(self, order_id):
        self.entered.set()
        assert self.go.wait(5)
        return super().capture_order(order_id)


def _start_capture(svc, paypal_id):
    out = {}
    t = threading.Thread(target=lambda: out.update(svc.complete_payment(paypal_id)))
    t.start()
    assert svc.paypal.entered.wait(5)
    return t, out


def test_b3_get_cancel_during_capture_does_not_erase_spend(policy, catalog, clock):
    svc = BuyerService(policy, catalog, GatedCaptureMock(require_payer_approval=True), ":memory:", clock=clock)
    c = TestClient(create_app(svc, agent=_NoAgent(), approver_token="t"))
    o = svc.place_order("P-1002", 2)["order"]
    svc.paypal.approve(o["paypal_order_id"])
    t, out = _start_capture(svc, o["paypal_order_id"])
    assert svc.store.get("orders", o["id"])["status"] == "CAPTURING"
    # unauthenticated buyer-cancel redirect arrives mid-capture
    j = c.get(f"/paypal/cancel?token={o['paypal_order_id']}&format=json").json()
    assert j["result"] == "NOT_CANCELLABLE" and j["status"] == "CAPTURING"
    assert str(svc.spent_today()) == "49.00"
    svc.paypal.go.set()
    t.join(5)
    assert out["status"] == "COMPLETED"
    s = svc.spend_summary()
    assert (s["captured"], s["reserved"]) == ("49.00", "0.00")
    # cancelling a completed order is refused and changes nothing
    assert c.get(f"/paypal/cancel?token={o['paypal_order_id']}&format=json").json()["result"] == "NOT_CANCELLABLE"
    assert c.post(f"/orders/{o['id']}/cancel", headers={"X-Approver-Token": "t"}).status_code == 409
    assert svc.spend_summary()["captured"] == "49.00"


def test_b3_expiry_during_capture_does_not_override(policy, catalog, clock):
    svc = BuyerService(policy, catalog, GatedCaptureMock(require_payer_approval=True), ":memory:", clock=clock)
    o = svc.place_order("P-1002", 2)["order"]
    svc.paypal.approve(o["paypal_order_id"])
    t, out = _start_capture(svc, o["paypal_order_id"])
    clock.now += svc.approval_ttl + timedelta(minutes=5)
    assert svc.expire_stale() == []
    svc.paypal.go.set()
    t.join(5)
    assert out["status"] == "COMPLETED" and svc.spend_summary()["captured"] == "49.00"


def test_b3_concurrent_double_capture_single_capture(policy, catalog, clock):
    svc = BuyerService(policy, catalog, GatedCaptureMock(require_payer_approval=True), ":memory:", clock=clock)
    o = svc.place_order("P-1001", 1)["order"]
    svc.paypal.approve(o["paypal_order_id"])
    t, out = _start_capture(svc, o["paypal_order_id"])
    second = svc.complete_payment(o["paypal_order_id"])  # browser refresh during capture
    assert second["result"] == "CAPTURE_IN_PROGRESS"
    svc.paypal.go.set()
    t.join(5)
    assert out["status"] == "COMPLETED" and svc.spend_summary()["captured"] == "12.99"


def test_b3_cancel_while_awaiting_still_works(policy, catalog, clock):
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    o = svc.place_order("P-1001", 1)["order"]
    assert svc.cancel_payment(o["paypal_order_id"])["result"] == "CANCELLED"
    assert svc.cancel_payment(o["paypal_order_id"])["result"] == "ALREADY_CANCELLED"
    assert str(svc.spent_today()) == "0.00"


class _NoAgent:
    model = "none"
