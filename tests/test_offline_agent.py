"""Offline (rule-based, no AI) agent mode."""
import pytest

from app.agent import PurchasingAgent
from app.main import create_app
from app.offline_agent import OFFLINE_MODEL_NAME, OfflineAgent, make_agent

DEMO = ("Buy 2 USB-C charging cables and a mechanical keyboard for our new hire, "
        "and also a $100 gift card as a welcome present.")


def test_parse_demo_request(service):
    lines = OfflineAgent(service).parse(DEMO)
    assert [(l["product_id"], l["quantity"]) for l in lines] == [("P-1001", 2), ("P-1003", 1), ("P-5001", 1)]


@pytest.mark.parametrize("text,expected", [
    ("3 bags of coffee beans and 2 snack boxes", [("P-3001", 3), ("P-3002", 2)]),
    ("two wireless mice; a dozen sticky notes", [("P-1002", 2), ("P-2002", 12)]),
    ("a 27-inch 4K monitor", [("P-1004", 1)]),
    ("1 crypto voucher plus printer paper", [("P-5002", 1), ("P-2001", 1)]),
    ("Get a design tool license & $20 cloud hosting credit", [("P-4002", 1), ("P-4001", 1)]),
])
def test_parse_variants(service, text, expected):
    got = [(l["product_id"], l["quantity"]) for l in OfflineAgent(service).parse(text)]
    assert got == expected


def test_run_demo_goes_through_guardrails(service):
    res = OfflineAgent(service).run(DEMO)
    tools = [t for t in res.transcript if t["role"] == "tool" and t["name"] != "search_catalog"]
    results = {(t["input"]["product_id"], t["name"]): (t["output"].get("result") or t["output"].get("verdict"))
               for t in tools}
    assert results[("P-1001", "create_order")] == "ORDER_PLACED"
    assert results[("P-1003", "create_order")] == "NEEDS_APPROVAL"
    assert results[("P-1003", "request_approval")] == "APPROVAL_REQUESTED"
    assert results[("P-5001", "create_order")] == "DENIED"
    assert ("P-5001", "request_approval") not in results  # a DENY is never escalated
    assert len(service.store.list("approvals", "PENDING")) == 1
    assert str(service.spent_today()) == "25.98"
    assert res.final_text.startswith("[offline rule-based mode, no AI]")
    assert "refused" in res.final_text and "needs human approval" in res.final_text
    assert res.usage == {"input_tokens": 0, "output_tokens": 0}


def test_unknown_item_refused_and_nothing_bought(service):
    # fix round 5: words outside the catalog vocabulary are outside the grammar -> refused
    res = OfflineAgent(service).run("buy a pony")
    assert res.stopped_reason == "offline_refused" and '"pony"' in res.final_text
    assert "Supported offline grammar" in res.final_text
    assert service.store.list("orders") == []


def test_nothing_matched(service):
    res = OfflineAgent(service).run("hello there")
    assert res.stopped_reason == "offline_refused" and "Nothing was ordered" in res.final_text


def test_make_agent_modes(service, monkeypatch):
    monkeypatch.delenv("AGENT_MODE", raising=False)
    assert isinstance(make_agent(service), PurchasingAgent)  # Claude stays the default
    monkeypatch.setenv("AGENT_MODE", "offline")
    assert isinstance(make_agent(service), OfflineAgent)
    with pytest.raises(ValueError):
        make_agent(service, "gpt")


def test_api_offline_mode(service, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AGENT_MODE", "offline")
    c = TestClient(create_app(service, approver_token="t"))
    h = c.get("/health").json()
    assert h["agent_mode"] == "offline" and h["model"] == OFFLINE_MODEL_NAME
    r = c.post("/agent/request", json={"request": DEMO}).json()
    assert r["stopped_reason"] == "offline_rules"
    assert len(c.get("/approvals?status=PENDING").json()) == 1
