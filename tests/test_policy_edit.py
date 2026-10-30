"""Owner-editable policy (PUT /policy, AG Grid policy editor) and spend history (AG Charts)."""
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.paypal_client import MockPayPal
from app.service import BuyerService
from app.tool_gateway import GuardedTools

ROOT = Path(__file__).resolve().parent.parent


def client(service, **kw):
    kw.setdefault("approver_token", "appr")
    return TestClient(create_app(service, agent_rate_limit=None, tools_rate_limit=None, **kw))


def test_policy_edit_needs_the_owner_token(service, monkeypatch):
    c = client(service)
    assert c.put("/policy", json={"per_order_cap": "100"}).status_code == 401
    assert c.put("/policy", json={"per_order_cap": "100"}, headers={"X-Owner-Token": "nope"}).status_code == 401
    assert c.get("/health").json()["policy_owner_mode"] == "approver-token"
    r = c.put("/policy", json={"per_order_cap": "100"}, headers={"X-Owner-Token": "appr"})
    assert r.status_code == 200 and r.json()["result"] == "UPDATED"
    # a dedicated owner token replaces the approver token for policy edits
    monkeypatch.setenv("POLICY_OWNER_TOKEN", "owner")
    c2 = client(service)
    assert c2.get("/health").json()["policy_owner_mode"] == "owner-token"
    assert c2.put("/policy", json={"per_order_cap": "90"}, headers={"X-Owner-Token": "appr"}).status_code == 401
    assert c2.put("/policy", json={"per_order_cap": "90"}, headers={"X-Owner-Token": "owner"}).status_code == 200


def test_policy_edit_fails_closed_without_tokens(service):
    assert client(service, approver_token="").put("/policy", json={"daily_cap": "200"}).status_code == 503
    open_demo = client(service, approver_token="", demo_mode=True)
    assert open_demo.put("/policy", json={"daily_cap": "200"}).status_code == 200


def test_edit_changes_decisions_and_is_recorded(service):
    c = client(service)
    h = {"X-Owner-Token": "appr"}
    assert service.check("P-1003", 1)["verdict"] == "NEEDS_APPROVAL"  # keyboard 89 > 50
    r = c.put("/policy", json={"per_order_cap": "100.00"}, headers=h).json()
    assert r["change"]["changes"] == {"per_order_cap": {"from": "50.00", "to": "100.00"}}
    assert service.check("P-1003", 1)["verdict"] == "ALLOW"
    assert c.get("/policy").json()["per_order_cap"] == "100.00"
    assert c.put("/policy", json={"per_order_cap": "100"}, headers=h).json()["result"] == "UNCHANGED"
    hist = c.get("/policy/history").json()
    assert len(hist) == 1 and hist[0]["actor"] == "owner" and hist[0]["id"].startswith("POL-")


@pytest.mark.parametrize("body,why", [
    ({"currency": "EUR"}, "not editable"),
    ({"timezone": "Asia/Tokyo"}, "not editable"),
    ({"per_order_cap": "-5"}, "positive amount"),
    ({"per_order_cap": "0"}, "between"),
    ({"per_order_cap": "10.005"}, "at most 2 decimals"),
    ({"per_order_cap": True}, "amount"),
    ({"per_order_cap": None}, "amount"),
    ({"hard_max_per_order": "99999999"}, "positive amount"),
    ({"daily_cap": "1e3"}, "positive amount"),
    ({"per_order_cap": "200"}, "must not exceed daily_cap"),
    ({"hard_max_per_order": "40"}, "hard_max_per_order"),
    ({"allowed_categories": []}, "cannot be empty"),
    ({"allowed_merchants": []}, "cannot be empty"),
    ({"allowed_merchants": ["Sandbox Cloud", "sandbox cloud"]}, "duplicates"),
    ({"blocked_merchants": ["Sandbox Cloud"]}, "both allowed and blocked"),
    ({"allowed_categories": "food"}, "list"),
    ({"allowed_categories": ["food", 3]}, "list"),
    ({"allowed_categories": ["food", ""]}, "printable"),
    ({"allowed_categories": ["food", "x" * 81]}, "printable"),
    ({"allowed_categories": ["fo\x00od"]}, "printable"),
    ({}, "JSON object"),
])
def test_invalid_edits_are_refused_and_change_nothing(service, body, why):
    c = client(service)
    before = c.get("/policy").json()
    r = c.put("/policy", json=body, headers={"X-Owner-Token": "appr"})
    assert r.status_code == 422, r.json()
    if why:
        assert why in r.json()["detail"]
    assert c.get("/policy").json() == before and c.get("/policy/history").json() == []


def test_non_object_body_is_refused(service):
    c = client(service)
    assert c.put("/policy", json=["per_order_cap", "1"], headers={"X-Owner-Token": "appr"}).status_code == 422


def test_blocking_a_merchant_reaches_agents_and_pending_approvals(service):
    c = client(service)
    tools = GuardedTools(service)
    aid = service.request_approval("P-4002", 1, "design licence")["approval_id"]  # Sandbox Cloud, 120
    assert tools.call("create_order", {"product_id": "P-4001", "quantity": 1})["result"]["result"] == "ORDER_PLACED"
    pol = c.get("/policy").json()
    body = {"allowed_merchants": [m for m in pol["allowed_merchants"] if m != "sandbox cloud"],
            "blocked_merchants": pol["blocked_merchants"] + ["sandbox cloud"]}
    assert c.put("/policy", json=body, headers={"X-Owner-Token": "appr"}).status_code == 200
    r = tools.call("create_order", {"product_id": "P-4001", "quantity": 1})["result"]
    assert r["result"] == "DENIED" and any("blocked" in x for x in r["reasons"])
    assert service.approve(aid)["result"] == "DENIED"
    assert service.store.get("approvals", aid)["status"] == "DENIED_BY_POLICY"


def test_policy_override_is_shared_through_the_database(policy, catalog, tmp_path):
    db = str(tmp_path / "shared.db")
    a = BuyerService(policy, catalog, MockPayPal(), db)
    b = BuyerService(policy, catalog, MockPayPal(), db)  # e.g. a second Sandbox-mode process
    assert b.check("P-1003", 1)["verdict"] == "NEEDS_APPROVAL"
    a.update_policy({"per_order_cap": "95"})
    assert b.check("P-1003", 1)["verdict"] == "ALLOW"  # picked up without a restart
    c = BuyerService(policy, catalog, MockPayPal(), db)  # and after a restart
    assert str(c.policy.per_order_cap) == "95.00"


def test_spend_history(service, clock):
    service.paypal = pp = MockPayPal(require_payer_approval=True)

    def paid(pid, qty):
        o = service.place_order(pid, qty)["order"]
        pp.approve(o["paypal_order_id"])
        assert service.complete_payment(o["paypal_order_id"])["status"] == "COMPLETED"

    service.place_order("P-1001", 1)                    # reserved today (awaiting payer): 12.99
    paid("P-2001", 2)                                   # captured today: 14.98
    clock.now -= timedelta(days=2)
    paid("P-3001", 1)                                   # captured 2 days ago: 28.00
    clock.now += timedelta(days=2)
    h = client(service).get("/spend/history?days=7").json()
    assert len(h["days"]) == 7 and h["days"][-1]["day"] == "2026-10-30" and h["daily_cap"] == "150.00"
    today, two_ago = h["days"][-1], h["days"][-3]
    assert (today["captured"], today["reserved"], today["total"], today["orders"]) == ("14.98", "12.99", "27.97", 2)
    assert (two_ago["captured"], two_ago["orders"]) == ("28.00", 1)
    assert h["days"][0]["total"] == "0.00"
    c = client(service)
    for bad in (0, 91, "x"):
        assert c.get(f"/spend/history?days={bad}").status_code == 422


def test_dashboard_ships_the_policy_tab_and_vendored_charts(service):
    c = client(service)
    html = c.get("/").text
    assert 'id="tab-policy"' in html and "/static/policy.js" in html
    assert 'id="tab-ops"' in html and 'id="tab-audit"' in html  # existing tabs untouched
    assert "ag-charts" not in html  # charts library is loaded lazily by the Policy tab only
    js = c.get("/static/policy.js").text
    assert "/static/vendor/ag-charts-community-10.3.3/ag-charts-community.min.js" in js and "X-Owner-Token" in js
    vend = ROOT / "app/static/vendor/ag-charts-community-10.3.3"
    assert (vend / "LICENSE.txt").read_text().startswith("The MIT License")
    assert c.get("/static/vendor/ag-charts-community-10.3.3/ag-charts-community.min.js").status_code == 200


def test_policy_script_only_uses_existing_elements_and_community_features(service):
    import re
    c = client(service)
    html, js = c.get("/").text, c.get("/static/policy.js").text
    ids = set(re.findall(r'\$\("([\w-]+)"\)', js))
    assert {"policy-caps", "policy-merchants", "policy-categories", "spend-chart", "policy-save"} <= ids
    assert not {i for i in ids if f'id="{i}"' not in html}
    assert "enterprise" not in js.lower() and "LicenseManager" not in js
