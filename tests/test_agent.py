"""Agent loop tests with a scripted fake LLM (no network, no spend)."""
import json

from app.agent import PurchasingAgent, TOOLS


def scripted(responses):
    calls = []

    def post(payload):
        calls.append(payload)
        return responses[len(calls) - 1]
    post.calls = calls
    return post


def tool_use(i, name, inp):
    return {"type": "tool_use", "id": f"tu_{i}", "name": name, "input": inp}


def test_agent_executes_tools_and_finishes(service):
    post = scripted([
        {"stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 10},
         "content": [{"type": "text", "text": "Searching."}, tool_use(1, "search_catalog", {"query": "keyboard"})]},
        {"stop_reason": "tool_use", "usage": {"input_tokens": 200, "output_tokens": 10},
         "content": [tool_use(2, "create_order", {"product_id": "P-1003", "quantity": 1})]},
        {"stop_reason": "tool_use", "usage": {"input_tokens": 300, "output_tokens": 10},
         "content": [tool_use(3, "request_approval", {"product_id": "P-1003", "quantity": 1, "justification": "j"})]},
        {"stop_reason": "end_turn", "usage": {"input_tokens": 400, "output_tokens": 20},
         "content": [{"type": "text", "text": "Approval requested."}]},
    ])
    res = PurchasingAgent(service, model="m", post=post).run("buy a keyboard")
    assert res.final_text == "Approval requested."
    assert res.usage == {"input_tokens": 1000, "output_tokens": 50}
    tools = [t for t in res.transcript if t["role"] == "tool"]
    assert [t["name"] for t in tools] == ["search_catalog", "create_order", "request_approval"]
    assert tools[1]["output"]["result"] == "NEEDS_APPROVAL"
    assert len(service.store.list("approvals", "PENDING")) == 1
    # tool results are fed back with the right ids
    last_user = post.calls[-1]["messages"][-2]  # shared list; final assistant msg appended after
    assert last_user["content"][0]["tool_use_id"] == "tu_3"
    assert post.calls[0]["tools"] == TOOLS and post.calls[0]["model"] == "m"


def test_agent_stops_after_max_turns(service):
    loop = {"stop_reason": "tool_use", "usage": {},
            "content": [tool_use(1, "search_catalog", {"query": "cable"})]}
    res = PurchasingAgent(service, post=scripted([loop] * 5), max_turns=3).run("loop")
    assert res.stopped_reason == "max_turns" and res.turns == 3


def test_agent_tool_errors_are_returned_not_raised(service):
    post = scripted([
        {"stop_reason": "tool_use", "usage": {}, "content": [tool_use(1, "create_order", {"product_id": "BAD", "quantity": 1})]},
        {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "ok"}]},
    ])
    res = PurchasingAgent(service, post=post).run("x")
    assert "error" in res.transcript[1]["output"]


def test_model_from_env(service, monkeypatch):
    monkeypatch.setenv("MODEL", "claude-test")
    assert PurchasingAgent(service, post=lambda p: {}).model == "claude-test"


def test_cli_prints_clean_summary_and_uses_memory_db(monkeypatch, capsys, tmp_path):
    import app.agent as agent_mod
    from app import cli
    monkeypatch.setenv("DB_PATH", str(tmp_path / "server.db"))  # server DB must not be touched
    monkeypatch.delenv("CLI_DB_PATH", raising=False)
    monkeypatch.setenv("PAYPAL_MODE", "mock")
    resp = [{"stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1},
             "content": [tool_use(1, "create_order", {"product_id": "P-1001", "quantity": 1})]},
            {"stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
             "content": [{"type": "text", "text": "<summary>- cable bought</summary>"}]}]
    monkeypatch.setattr(agent_mod, "anthropic_post", scripted(resp))
    assert cli.main(["a cable"]) == 0
    out = capsys.readouterr().out
    assert "- cable bought" in out and "<summary>" not in out
    assert not (tmp_path / "server.db").exists()


def test_empty_final_message_falls_back_to_tool_results(service):
    post = scripted([
        {"stop_reason": "tool_use", "usage": {},
         "content": [tool_use(1, "request_approval", {"product_id": "P-1003", "quantity": 1, "justification": "j"}),
                     tool_use(2, "create_order", {"product_id": "P-5001", "quantity": 1})]},
        {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "<summary></summary>"}]},
    ])
    res = PurchasingAgent(service, post=post).run("keyboard and gift card")
    assert "needs human approval (APR-" in res.final_text and "P-5001: refused" in res.final_text


# --- UI E2E note: "Bought:" before the buyer has paid (docs/SANDBOX_E2E.md) ---------------------

def _service_awaiting_payer(policy, catalog, clock):
    from app.paypal_client import MockPayPal
    from app.service import BuyerService
    return BuyerService(policy, catalog, MockPayPal(require_payer_approval=True), ":memory:", clock=clock)


def _order_then_summary(summary_text):
    return scripted([
        {"stop_reason": "tool_use", "usage": {},
         "content": [tool_use(1, "create_order", {"product_id": "P-1001", "quantity": 2})]},
        {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": summary_text}]},
    ])


def _order_then_live_summary(template):
    """Fake model: creates an order, then writes `template` with the real order id it got back."""
    calls = []

    def post(payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"stop_reason": "tool_use", "usage": {},
                    "content": [tool_use(1, "create_order", {"product_id": "P-1001", "quantity": 2})]}
        out = json.loads(payload["messages"][-1]["content"][0]["content"])
        return {"stop_reason": "end_turn", "usage": {},
                "content": [{"type": "text", "text": template.format(oid=out["order"]["id"])}]}
    return post


SANDBOX_SUMMARY = ("<summary>\n- Bought: 2x USB-C Charging Cable 2m (Order {oid}, $25.98 USD)\n"
                   "- Awaiting payer approval: PayPal order 34M919590A962473E before capture\n"
                   "- Nothing refused\n</summary>")


def test_bought_is_reworded_while_order_awaits_payer(policy, catalog, clock):
    """The live Sandbox run said "- Bought: ..." for a PAYER_ACTION_REQUIRED order; the summary must
    say the order was created and awaits the buyer, never that it was bought."""
    svc = _service_awaiting_payer(policy, catalog, clock)
    res = PurchasingAgent(svc, post=_order_then_live_summary(SANDBOX_SUMMARY)).run("2 cables")
    out = res.transcript[1]["output"]
    assert out["result"] == "AWAITING_PAYER_APPROVAL" and out["order"]["status"] == "PAYER_ACTION_REQUIRED"
    assert "bought" not in res.final_text.lower(), res.final_text
    assert res.final_text.startswith(f"- 2 x P-1001: order created, awaiting buyer approval, order "
                                     f"{out['order']['id']} $25.98; not paid until the buyer approves at "), res.final_text


def test_bought_kept_for_completed_order_id(service):
    res = PurchasingAgent(service, post=_order_then_live_summary(SANDBOX_SUMMARY)).run("2 cables")
    assert res.transcript[1]["output"]["order"]["status"] == "COMPLETED"
    assert res.final_text.startswith("- Bought: 2x USB-C Charging Cable 2m")


def test_idless_bought_reworded_when_nothing_completed(policy, catalog, clock):
    svc = _service_awaiting_payer(policy, catalog, clock)
    res = PurchasingAgent(svc, post=_order_then_summary("<summary>- I bought 2 cables for $25.98</summary>")).run("x")
    assert "bought" not in res.final_text and "order created, awaiting buyer approval" in res.final_text


def test_negated_bought_is_left_alone(policy, catalog, clock):
    """CODEX UI review #3: a correct negation must not be rewritten."""
    svc = _service_awaiting_payer(policy, catalog, clock)
    for text in ("- {oid} created, not bought yet: the buyer must approve it on PayPal",
                 "- Nothing was purchased yet; {oid} awaits the buyer",
                 "- {oid} wasn't bought yet, it awaits the buyer"):
        res = PurchasingAgent(svc, post=_order_then_live_summary("<summary>" + text + "</summary>")).run("x")
        oid = res.transcript[1]["output"]["order"]["id"]
        assert res.final_text == text.format(oid=oid)


def test_negation_in_another_clause_does_not_hide_a_claim():
    """CODEX UI review 2: "Nothing refused. Bought ..." is still a purchase claim."""
    from app.agent import claims_purchase
    for text in ("- Nothing refused. Bought 2 cables", "- No denials; bought 2 cables", "Not refused: bought 2 cables",
                 "- Nothing refused\n- Bought: 2 cables", "- Bought: 2 cables"):
        assert claims_purchase(text), text
    for text in ("- not bought yet", "- Nothing was purchased", "- It wasn't bought", "- never bought, awaiting buyer",
                 "- created, not yet bought"):
        assert not claims_purchase(text), text


def test_mixed_paid_and_unpaid_orders_use_per_order_wording(policy, catalog, clock):
    """CODEX UI review #2/#4: one paid and one unpaid order in the same run, with a "Bought" line
    that names neither (or both). Each order must be worded by its own status."""
    svc = _service_awaiting_payer(policy, catalog, clock)
    calls = []

    def post(payload):
        calls.append(payload)
        if len(calls) == 1:   # cable: buyer must approve
            return {"stop_reason": "tool_use", "usage": {},
                    "content": [tool_use(1, "create_order", {"product_id": "P-1001", "quantity": 1})]}
        if len(calls) == 2:   # mouse: captured at once
            svc.paypal.require_payer_approval = False
            return {"stop_reason": "tool_use", "usage": {},
                    "content": [tool_use(2, "create_order", {"product_id": "P-1002", "quantity": 1})]}
        return {"stop_reason": "end_turn", "usage": {},
                "content": [{"type": "text", "text": "<summary>- Bought: a cable and a mouse</summary>"}]}
    res = PurchasingAgent(svc, post=post).run("a cable and a mouse")
    cable, mouse = (t["output"]["order"] for t in res.transcript if t.get("name") == "create_order")
    assert cable["status"] == "PAYER_ACTION_REQUIRED" and mouse["status"] == "COMPLETED"
    lines = res.final_text.splitlines()
    assert lines[0].startswith(f"- 1 x P-1001: order created, awaiting buyer approval, order {cable['id']}")
    assert lines[1] == f"- 1 x P-1002: bought and paid, order {mouse['id']} $24.50"


def test_unconfirmed_and_failed_orders_keep_their_own_wording():
    """CODEX UI review #1: not every unpaid order is awaiting the buyer."""
    from app.agent import correct_paid_wording
    tr = [{"role": "tool", "name": "create_order", "input": {"product_id": "P-1001", "quantity": 1},
           "output": {"result": "PAYMENT_UNCONFIRMED",
                      "order": {"id": "ORD-U1", "status": "NEEDS_RECONCILIATION", "amount": "12.99"}}},
          {"role": "tool", "name": "create_order", "input": {"product_id": "P-1002", "quantity": 1},
           "output": {"result": "FAILED", "order": {"id": "ORD-F1", "status": "CAPTURE_FAILED", "amount": "24.50",
                                                     "error": "INSTRUMENT_DECLINED"}}}]
    out = correct_paid_wording("- Bought: ORD-U1 and ORD-F1", tr)
    assert "awaiting buyer approval" not in out and "bought" not in out.lower()
    assert "ORD-U1 $12.99 payment outcome unconfirmed" in out
    assert "PayPal order failed: INSTRUMENT_DECLINED" in out


def test_fallback_summary_wording_by_status(policy, catalog, clock, service):
    from app.agent import fallback_summary
    svc = _service_awaiting_payer(policy, catalog, clock)
    res = PurchasingAgent(svc, post=_order_then_summary("<summary></summary>")).run("x")
    assert "order created, awaiting buyer approval" in res.final_text and "bought" not in res.final_text
    res = PurchasingAgent(service, post=_order_then_summary("<summary></summary>")).run("x")
    assert "bought and paid" in res.final_text
    assert fallback_summary([]) == ""
