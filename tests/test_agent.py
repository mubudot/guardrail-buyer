"""Agent loop tests with a scripted fake LLM (no network, no spend)."""
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
