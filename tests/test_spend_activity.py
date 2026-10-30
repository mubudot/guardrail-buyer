"""GET /spend guardrail activity counters behind the dashboard's summary cards
(blocked attempts / escalations / auto-approved orders today, policy timezone)."""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import create_app
from app.paypal_client import MockPayPal
from app.service import BuyerService


def test_counts_attempts_not_rows(service):
    s = service.spend_summary()
    assert (s["blocked_today"], s["escalated_today"], s["allowed_today"]) == (0, 0, 0)
    # dry-run check + order for the same item and request count as ONE attempt
    service.check("P-5001", 1, request_id="REQ-G")
    service.place_order("P-5001", 1, request_id="REQ-G")
    service.check("P-1001", 2, request_id="REQ-C")
    service.place_order("P-1001", 2, request_id="REQ-C")
    service.check("P-1003", 1, request_id="REQ-K")
    service.place_order("P-1003", 1, request_id="REQ-K")  # over the per-order cap: NEEDS_APPROVAL, no order
    service.request_approval("P-1003", 1, "keyboard for the new hire", request_id="REQ-K")
    s = service.spend_summary()
    assert (s["blocked_today"], s["escalated_today"], s["allowed_today"]) == (1, 1, 1)
    # a read-only check that is never followed by an order is not an auto-approved order
    service.check("P-1001", 1, request_id="REQ-X")
    # another request for the denied item is another blocked attempt
    service.place_order("P-5001", 1, request_id="REQ-G2")
    s = service.spend_summary()
    assert (s["blocked_today"], s["escalated_today"], s["allowed_today"]) == (2, 1, 1)


def test_refused_or_unneeded_approval_requests_are_not_escalations(service):
    assert service.request_approval("P-5001", 1, "gift card", request_id="REQ-A")["result"] == "DENIED"
    assert service.request_approval("P-1001", 1, "cheap cable", request_id="REQ-B")["result"] == "NOT_NEEDED"
    s = service.spend_summary()
    assert s["escalated_today"] == 0 and s["blocked_today"] == 1 and s["allowed_today"] == 0
    assert service.request_approval("P-1003", 1, "keyboard", request_id="REQ-C")["result"] == "APPROVAL_REQUESTED"
    assert service.spend_summary()["escalated_today"] == 1


def test_day_boundary_follows_the_policy_timezone(policy, catalog, clock):
    tz = ZoneInfo("Asia/Taipei")
    svc = BuyerService(replace(policy, timezone="Asia/Taipei"), catalog, MockPayPal(), ":memory:", clock=clock)
    clock.now = datetime(2026, 10, 30, 23, 30, tzinfo=tz)       # late on the 30th, Taipei
    svc.place_order("P-5001", 1, request_id="REQ-LATE")
    assert svc.spend_summary()["blocked_today"] == 1
    clock.now = datetime(2026, 10, 31, 0, 30, tzinfo=tz)        # an hour later it is a new policy day
    s = svc.spend_summary()
    assert s["day"] == "2026-10-31" and s["blocked_today"] == 0
    svc.place_order("P-5001", 1, request_id="REQ-EARLY")
    assert svc.spend_summary()["blocked_today"] == 1
    clock.now += timedelta(days=1)
    assert svc.spend_summary()["blocked_today"] == 0


def test_spend_endpoint_exposes_the_counters(service):
    c = TestClient(create_app(service, approver_token="t", agent_rate_limit=None, tools_rate_limit=None))
    service.place_order("P-5001", 1, request_id="REQ-1")
    body = c.get("/spend").json()
    assert (body["blocked_today"], body["escalated_today"], body["allowed_today"]) == (1, 0, 0)
    assert {"captured", "reserved", "daily_cap", "remaining"} <= body.keys()  # existing fields unchanged
