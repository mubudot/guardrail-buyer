"""Regressions for CODEX_REVIEW_2 (fix round 5): N1/N2 monotonic + exclusive recovery,
N3 single-process mock lock, N4 aggregate hard rule at approval, B3 signed cancel
state, B6 fail-closed approver config + agent rate limit. Interleavings are forced
with threading Events/Barriers, never with sleeps."""
import json
import subprocess
import sys
import threading
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import ROOT, RateLimiter, create_app, parse_rate_limit
from app.paypal_client import MockPayPal, PayPalError, SandboxPayPal
from app.service import STUCK_AFTER, BuyerService, MockLockError


class _NoAgent:
    model = "none"

    def run(self, text):
        from app.agent import AgentResult
        return AgentResult(final_text=f"ok: {text}")


class GatedReadMock(MockPayPal):
    """get_order takes its snapshot immediately, then (once armed) holds the reply until
    `go` is set: a once-current provider read delivered late (CODEX_REVIEW_2 N1)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.armed = False
        self.entered, self.go = threading.Event(), threading.Event()

    def get_order(self, order_id):
        snap = super().get_order(order_id)
        if self.armed:
            self.armed = False
            self.entered.set()
            assert self.go.wait(5)
        return snap


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except Exception as e:  # pragma: no cover - surfaced by the assertions
            out["error"] = e
    t = threading.Thread(target=run)
    t.start()
    return t, out


def _needs_reconciliation_with_paypal_id(svc):
    """Order awaiting the buyer, then a capture attempt answered 503 -> NEEDS_RECONCILIATION."""
    o = svc.place_order("P-1002", 2)["order"]  # 49.00
    real = svc.paypal.capture_order
    svc.paypal.capture_order = lambda oid: (_ for _ in ()).throw(PayPalError("HTTP 503", status_code=503))
    assert svc.capture(o["id"])["status"] == "NEEDS_RECONCILIATION"
    svc.paypal.capture_order = real
    return o


def _n1_schedule(svc, o):
    """Reconciler A reads PAYER_ACTION_REQUIRED and is held; buyer approves; reconciler B
    captures and gets COMPLETED/PENDING; then A's stale reply is delivered."""
    svc.paypal.armed = True
    ta, a_out = _in_thread(lambda: svc.reconcile(o["id"]))
    assert svc.paypal.entered.wait(5)                      # A holds a PAYER_ACTION_REQUIRED snapshot
    svc.paypal.approve(o["paypal_order_id"])               # buyer approves on PayPal
    svc.paypal.capture_order = lambda oid: {"id": oid, "status": "COMPLETED", "capture_id": "CAP-P",
                                            "capture_status": "PENDING", "raw": {}}
    b = svc.reconcile(o["id"])                             # reconciler B captures -> PENDING
    assert b["status"] == "NEEDS_RECONCILIATION" and b["capture_id"] == "CAP-P"
    svc.paypal.go.set()
    ta.join(5)
    assert "error" not in a_out
    return a_out["value"]


# ---- N1: a superseded provider read can never move the order backward --------------
def test_n1_stale_reconcile_read_cannot_reopen_cancellation(policy, catalog, clock):
    svc = BuyerService(policy, catalog, GatedReadMock(require_payer_approval=True), ":memory:", clock=clock)
    o = _needs_reconciliation_with_paypal_id(svc)
    a = _n1_schedule(svc, o)
    cur = svc.store.get("orders", o["id"])
    assert a["status"] == cur["status"] == "NEEDS_RECONCILIATION"   # not PAYER_ACTION_REQUIRED
    assert a["result"] == "SUPERSEDED"
    assert svc.cancel(o["id"])["result"] == "NOT_CANCELLABLE"
    clock.now += svc.approval_ttl + timedelta(hours=1)
    assert svc.expire_stale() == []
    s = svc.spend_summary()
    assert (s["reserved"], s["needs_reconciliation"]) == ("49.00", 1)  # pending capture still counted
    # later PayPal confirms the pending capture
    svc.paypal.get_order = lambda oid: {"id": oid, "status": "COMPLETED", "capture_id": "CAP-P",
                                        "capture_status": "COMPLETED", "raw": {}}
    assert svc.reconcile(o["id"])["status"] == "COMPLETED"
    assert svc.spend_summary()["captured"] == "49.00"


def test_n1_generation_guard_alone_rejects_superseded_read(policy, catalog, clock):
    """Same schedule with the 'capture may have happened' flag cleared (as on a row
    written by an older version): the persisted generation alone rejects A's result."""
    svc = BuyerService(policy, catalog, GatedReadMock(require_payer_approval=True), ":memory:", clock=clock)
    o = _needs_reconciliation_with_paypal_id(svc)
    svc.store.update("orders", o["id"], capture_unknown=0)
    gen_before = svc.store.get("orders", o["id"])["op_gen"]
    a = _n1_schedule(svc, o)
    assert a["result"] == "SUPERSEDED"
    cur = svc.store.get("orders", o["id"])
    assert cur["status"] == "NEEDS_RECONCILIATION" and cur["op_gen"] > gen_before
    assert svc.cancel(o["id"])["result"] == "NOT_CANCELLABLE"
    assert svc.spend_summary()["reserved"] == "49.00"


def test_n1_possibly_captured_order_never_returns_to_awaiting(policy, catalog, clock):
    """Fresh (not stale) provider says the buyer has not approved, but a capture call
    was ambiguous earlier: keep it reserved and non-cancellable."""
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    o = _needs_reconciliation_with_paypal_id(svc)
    out = svc.reconcile(o["id"])
    assert out["status"] == "NEEDS_RECONCILIATION" and out["paypal_live_status"] == "PAYER_ACTION_REQUIRED"
    assert svc.store.get("orders", o["id"])["capture_unknown"] == 1
    assert svc.cancel(o["id"])["result"] == "NOT_CANCELLABLE"
    c = TestClient(create_app(svc, agent=_NoAgent(), approver_token="t"))
    assert c.post(f"/orders/{o['id']}/cancel", headers={"X-Approver-Token": "t"}).status_code == 409
    st = svc.cancel_state(o["id"])
    assert c.get(f"/paypal/cancel?token={o['paypal_order_id']}&state={st}&format=json").json()["result"] == \
        "NOT_CANCELLABLE"
    # a capture attempt the provider refuses as not approved still does not reopen it
    real = svc.paypal.capture_order
    svc.paypal.get_order = lambda oid: {"id": oid, "status": "APPROVED", "raw": {}}
    svc.paypal.capture_order = lambda oid: real(oid)  # mock order is not approved -> 422
    out = svc.reconcile(o["id"])
    assert out["result"] == "NOT_APPROVED_YET" and out["status"] == "NEEDS_RECONCILIATION"
    assert svc.spend_summary()["reserved"] == "49.00"
    # the provider voiding the order is the definitive release
    svc.paypal.get_order = lambda oid: {"id": oid, "status": "VOIDED", "raw": {}}
    assert svc.reconcile(o["id"])["status"] == "CANCELLED" and svc.spend_summary()["total"] == "0.00"


class GatedCaptureMock(MockPayPal):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.entered, self.go = threading.Event(), threading.Event()
        self.calls = 0

    def capture_order(self, order_id):
        self.calls += 1
        self.entered.set()
        assert self.go.wait(5)
        return super().capture_order(order_id)


def test_n1_stuck_reconcile_racing_live_capture_ends_completed_once(policy, catalog, clock):
    svc = BuyerService(policy, catalog, GatedCaptureMock(require_payer_approval=True), ":memory:", clock=clock)
    o = svc.place_order("P-1002", 2)["order"]
    svc.paypal.approve(o["paypal_order_id"])
    ta, a_out = _in_thread(lambda: svc.capture(o["id"]))
    assert svc.paypal.entered.wait(5)
    clock.now += STUCK_AFTER + timedelta(minutes=1)        # capture looks stuck
    tb, b_out = _in_thread(lambda: svc.reconcile(o["id"]))  # claims it, sees APPROVED, captures too
    for _ in range(500):
        if svc.paypal.calls == 2:
            break
        threading.Event().wait(0.01)
    assert svc.store.get("orders", o["id"])["status"] == "CAPTURING"  # B's own claim
    svc.paypal.go.set()
    ta.join(5)
    tb.join(5)
    assert svc.store.get("orders", o["id"])["status"] == "COMPLETED"
    assert svc.paypal.orders[o["paypal_order_id"]]["status"] == "COMPLETED"
    s = svc.spend_summary()
    assert (s["captured"], s["reserved"]) == ("49.00", "0.00")


# ---- N2: a completed create replay goes through capture verification ---------------
def _lost_create_response(svc, then):
    real = svc.paypal.create_order

    def create_then_lose(*a, **k):
        created = real(*a, **k)
        then(created["id"])
        raise PayPalError("create_order failed: transport error ReadTimeout", issue="TRANSPORT_ERROR",
                          ambiguous=True)
    svc.paypal.create_order = create_then_lose
    r = svc.place_order("P-1001", 1)
    assert r["order"]["status"] == "NEEDS_RECONCILIATION" and r["order"]["paypal_order_id"] is None
    svc.paypal.create_order = real
    return r["order"]


def _approve_and_capture_at_provider(svc):
    def f(pid):
        svc.paypal.approve(pid)
        svc.paypal.capture_order(pid)
    return f


def test_n2_completed_create_replay_is_verified_not_cancellable(policy, catalog, clock):
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    o = _lost_create_response(svc, _approve_and_capture_at_provider(svc))
    out = svc.reconcile(o["id"])  # replay returns the same, already COMPLETED PayPal order
    assert out["status"] == "COMPLETED" and out["capture_id"].startswith("MOCKCAP-")
    assert out["result"] == "ORDER_PLACED"
    assert svc.cancel(o["id"])["result"] == "NOT_CANCELLABLE"
    assert svc.spend_summary()["captured"] == "12.99"


def test_n2_completed_replay_unverifiable_stays_reserved(policy, catalog, clock):
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    o = _lost_create_response(svc, _approve_and_capture_at_provider(svc))
    svc.paypal.get_order = lambda oid: (_ for _ in ()).throw(PayPalError("HTTP 503", status_code=503))
    out = svc.reconcile(o["id"])
    assert out["status"] == "NEEDS_RECONCILIATION" and out["result"] == "STILL_UNCONFIRMED"
    row = svc.store.get("orders", o["id"])
    assert row["capture_unknown"] == 1 and row["paypal_order_id"]
    assert svc.cancel(o["id"])["result"] == "NOT_CANCELLABLE"
    clock.now += svc.approval_ttl + timedelta(hours=1)
    assert svc.expire_stale() == [] and svc.spend_summary()["reserved"] == "12.99"


def test_n2_approved_create_replay_is_captured(policy, catalog, clock):
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    o = _lost_create_response(svc, svc.paypal.approve)
    out = svc.reconcile(o["id"])
    assert out["status"] == "COMPLETED" and svc.spend_summary()["captured"] == "12.99"


def test_n2_replay_with_unexpected_status_is_not_awaiting(policy, catalog, clock):
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    o = _lost_create_response(svc, lambda pid: None)
    real = svc.paypal.create_order
    svc.paypal.create_order = lambda *a, **k: {**real(*a, **k), "status": "SOMETHING_NEW"}
    out = svc.reconcile(o["id"])
    assert out["status"] == "NEEDS_RECONCILIATION" and svc.cancel(o["id"])["result"] == "NOT_CANCELLABLE"


# ---- N3: mock mode is single-process ---------------------------------------------
def test_n3_second_mock_app_on_same_db_fails_fast_and_spares_inflight_capture(policy, catalog, clock, tmp_path):
    db = str(tmp_path / "shared.db")
    a = BuyerService(policy, catalog, GatedCaptureMock(require_payer_approval=True), db, clock=clock)
    create_app(a, agent=_NoAgent(), approver_token="t")      # worker A owns the DB
    o = a.place_order("P-1002", 2)["order"]
    a.paypal.approve(o["paypal_order_id"])
    ta, a_out = _in_thread(lambda: a.complete_payment(o["paypal_order_id"]))
    assert a.paypal.entered.wait(5)
    b = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), db, clock=clock)
    with pytest.raises(MockLockError, match="single-process"):
        create_app(b, agent=_NoAgent(), approver_token="t")  # worker B refuses to start
    assert b.store.get("orders", o["id"])["status"] == "CAPTURING"  # A's capture untouched
    a.paypal.go.set()
    ta.join(5)
    assert a_out["value"]["status"] == "COMPLETED" and a.spend_summary()["captured"] == "49.00"
    a.release_mock_lock()                                      # A stopped -> a restart is allowed
    create_app(b, agent=_NoAgent(), approver_token="t")
    assert b.store.get("orders", o["id"])["status"] == "COMPLETED"  # history kept
    b.release_mock_lock()


def test_n3_second_mock_process_exits_with_clear_message(policy, catalog, clock, tmp_path):
    db = str(tmp_path / "proc.db")
    a = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), db, clock=clock)
    create_app(a, agent=_NoAgent(), approver_token="t")
    env = {"PATH": "/usr/bin:/bin", "GRB_NO_DOTENV": "1", "PAYPAL_MODE": "mock", "DB_PATH": db,
           "AGENT_MODE": "offline", "APPROVER_TOKEN": "t", "PYTHONPATH": str(ROOT)}
    r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0
    assert "single-process" in r.stderr and db in r.stderr
    a.release_mock_lock()
    r = subprocess.run([sys.executable, "-c", "import app.main; print('started')"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "started" in r.stdout


def test_n3_sandbox_mode_takes_no_mock_lock(policy, catalog, clock, tmp_path):
    db = str(tmp_path / "sb.db")
    mk = lambda: BuyerService(policy, catalog, SandboxPayPal(auth="proxy", transport=httpx.MockTransport(
        lambda r: httpx.Response(404))), db, clock=clock)
    create_app(mk(), agent=_NoAgent(), approver_token="t")
    create_app(mk(), agent=_NoAgent(), approver_token="t")  # several real-provider workers are fine


# ---- N4: approval re-checks the aggregate hard maximum ---------------------------
def test_n4_queued_approvals_same_request_cannot_exceed_hard_max(service):
    a1 = service.request_approval("P-1004", 1, "monitor 1", request_id="REQ-M")["approval_id"]
    a2 = service.request_approval("P-1004", 1, "monitor 2", request_id="REQ-M")["approval_id"]
    assert service.approve(a1)["order"]["amount"] == "329.00"
    second = service.approve(a2)
    assert second["result"] == "DENIED" and "hard maximum" in second["reasons"][0]
    assert service.store.get("approvals", a2)["status"] == "DENIED_BY_POLICY"
    assert len(service.store.list("orders")) == 1 and str(service.spent_today()) == "329.00"


def test_n4_concurrent_approvals_same_request_one_executes(policy, catalog, clock):
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    ids = [svc.request_approval("P-1004", 1, f"m{i}", request_id="REQ-C")["approval_id"] for i in range(2)]
    start, out = threading.Barrier(2), [None, None]

    def work(i):
        start.wait()
        out[i] = svc.approve(ids[i])["result"]
    ts = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert sorted(out) == ["AWAITING_PAYER_APPROVAL", "DENIED"]
    assert str(svc.spent_today()) == "329.00"


def test_n4_released_order_frees_the_aggregate_and_soft_caps_stay_waived(service):
    a1 = service.request_approval("P-1004", 1, "m1", request_id="REQ-R")["approval_id"]
    a2 = service.request_approval("P-1004", 1, "m2", request_id="REQ-OTHER")["approval_id"]
    service.approve(a1)
    # a different request: the per-request aggregate does not apply, and the daily cap
    # (a soft cap) is waived by the human approval -> 658 total today
    assert service.approve(a2)["result"] == "ORDER_PLACED" and str(service.spent_today()) == "658.00"


# ---- B3: signed cancel state ------------------------------------------------------
def test_b3_cancel_requires_signed_state(policy, catalog, clock):
    svc = BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)
    c = TestClient(create_app(svc, agent=_NoAgent(), approver_token="t"))
    o = svc.place_order("P-1001", 1)["order"]
    other = svc.place_order("P-2001", 1)["order"]
    pid = o["paypal_order_id"]
    assert c.get(f"/paypal/cancel?token={pid}&format=json").status_code == 403
    assert c.get(f"/paypal/cancel?token={pid}&state=deadbeef&format=json").status_code == 403
    assert c.get(f"/paypal/cancel?token={pid}&state={svc.cancel_state(other['id'])}&format=json").status_code == 403
    assert svc.store.get("orders", o["id"])["status"] == "PAYER_ACTION_REQUIRED"
    assert svc.spend_summary()["reserved"] == "20.48"
    ok = c.get(f"/paypal/cancel?token={pid}&state={svc.cancel_state(o['id'])}&format=json").json()
    assert ok["result"] == "CANCELLED" and svc.spend_summary()["reserved"] == "7.49"


def test_b3_sandbox_cancel_url_carries_state_and_secret_is_per_db(policy, catalog, clock, tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(201, json={"id": "S1", "status": "PAYER_ACTION_REQUIRED", "links": []})
    db = str(tmp_path / "s.db")
    svc = BuyerService(policy, catalog, SandboxPayPal(auth="proxy", transport=httpx.MockTransport(handler)),
                       db, clock=clock)
    o = svc.place_order("P-1001", 1)["order"]
    ctx = json.loads(seen[0].content)["payment_source"]["paypal"]["experience_context"]
    assert ctx["cancel_url"].endswith(f"/paypal/cancel?state={svc.cancel_state(o['id'])}")
    # another process on the same DB verifies the same state (secret stored in the DB)
    svc2 = BuyerService(policy, catalog, MockPayPal(), db, clock=clock)
    assert svc2.verify_cancel_state(o["id"], svc.cancel_state(o["id"]))
    assert not svc2.verify_cancel_state(o["id"], "") and not svc2.verify_cancel_state(o["id"], None)


# ---- B6: fail-closed approver config, agent rate limit -----------------------------
def test_b6_empty_approver_token_fails_closed_without_demo_mode(service):
    c = TestClient(create_app(service, agent=_NoAgent(), approver_token=""))
    a = service.request_approval("P-1003", 1, "kb")["approval_id"]
    assert c.post(f"/approvals/{a}/approve").status_code == 503
    assert c.post(f"/approvals/{a}/reject").status_code == 503
    assert c.post("/orders/ORD-X/cancel").status_code == 503
    assert service.store.get("approvals", a)["status"] == "PENDING"
    assert c.get("/health").json()["approver_mode"] == "disabled"


def test_b6_demo_mode_env_opens_approvals(service, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    c = TestClient(create_app(service, agent=_NoAgent(), approver_token=""))
    a = service.request_approval("P-1003", 1, "kb")["approval_id"]
    assert c.post(f"/approvals/{a}/approve").status_code == 200
    assert c.get("/health").json()["approver_mode"] == "open-demo"


def test_b6_agent_rate_limit(service):
    c = TestClient(create_app(service, agent=_NoAgent(), approver_token="t", agent_rate_limit=(2, 60)))
    assert [c.post("/agent/request", json={"request": "x"}).status_code for _ in range(2)] == [200, 200]
    r = c.post("/agent/request", json={"request": "x"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert c.get("/health").json()["agent_rate_limit"] == "2/60s"


def test_b6_rate_limiter_window_and_config(monkeypatch, service):
    t = [0.0]
    rl = RateLimiter(2, 10, clock=lambda: t[0])
    assert rl.check("ip") is None and rl.check("ip") is None and rl.check("ip") == 10
    assert rl.check("other") is None          # per client
    t[0] = 10.5
    assert rl.check("ip") is None             # window slid
    assert parse_rate_limit(None) == (20, 60.0) and parse_rate_limit("5/30") == (5, 30.0)
    assert parse_rate_limit("off") is None and parse_rate_limit("0") is None
    monkeypatch.setenv("AGENT_RATE_LIMIT", "off")
    c = TestClient(create_app(service, agent=_NoAgent(), approver_token="t"))
    assert all(c.post("/agent/request", json={"request": "x"}).status_code == 200 for _ in range(25))
