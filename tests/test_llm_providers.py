"""Pluggable LLM providers (app/llm): request shape, tool-call mapping both ways, provider
selection and error redaction, all with mocked HTTP (no network)."""
import json

import httpx
import pytest

from app.agent import PurchasingAgent
from app.llm import LLMError, select_provider
from app.llm.anthropic import anthropic_post
from app.llm.openai import from_openai_response, openai_post, to_openai_request


class Capture:
    """httpx.post stand-in: records the request, answers with queued responses."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def oa_reply(content=None, tool_calls=None, finish="stop", usage=(7, 3)):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                                     "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})


def oa_call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


# ------------------------------------------------------------------ provider selection

@pytest.mark.parametrize("env,expected", [
    ({}, "anthropic"),
    ({"ANTHROPIC_API_KEY": "a"}, "anthropic"),
    ({"ANTHROPIC_AUTH_TOKEN": "t"}, "anthropic"),
    ({"OPENAI_API_KEY": "o"}, "openai"),
    ({"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a"}, "anthropic"),
    ({"LLM_PROVIDER": "openai"}, "openai"),
    ({"LLM_PROVIDER": " OpenAI ", "ANTHROPIC_API_KEY": "a"}, "openai"),
    ({"LLM_PROVIDER": "anthropic", "OPENAI_API_KEY": "o"}, "anthropic"),
])
def test_select_provider(env, expected):
    assert select_provider(env) == expected


def test_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        select_provider({"LLM_PROVIDER": "gemini"})


def test_agent_picks_openai_provider_and_requires_a_model(service, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        PurchasingAgent(service)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    a = PurchasingAgent(service)
    assert (a.provider, a.model, a.post) == ("openai", "gpt-test", openai_post)


# ------------------------------------------------------------------ Anthropic request shape

def test_anthropic_request_shape_and_auth_headers(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8080/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-123")
    cap = Capture(httpx.Response(200, json={"content": [], "stop_reason": "end_turn"}))
    monkeypatch.setattr(httpx, "post", cap)
    anthropic_post({"model": "m", "messages": []})
    c = cap.calls[0]
    assert c["url"] == "http://localhost:8080/v1/messages"
    assert c["headers"]["authorization"] == "Bearer tok-123" and "x-api-key" not in c["headers"]
    assert c["headers"]["anthropic-version"] == "2023-06-01"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-1")  # an API key wins over the bearer token
    cap.responses.append(httpx.Response(200, json={"content": []}))
    anthropic_post({"model": "m", "messages": []})
    assert cap.calls[1]["headers"]["x-api-key"] == "key-1" and "authorization" not in cap.calls[1]["headers"]


# ------------------------------------------------------------------ OpenAI translation

def test_to_openai_request_maps_system_tools_tool_calls_and_results():
    payload = {"model": "ignored", "max_tokens": 99, "system": "SYS",
               "tools": [{"name": "check_policy", "description": "d",
                          "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}}}],
               "messages": [
                   {"role": "user", "content": "buy a cable"},
                   {"role": "assistant", "content": [
                       {"type": "text", "text": "checking"},
                       {"type": "tool_use", "id": "c1", "name": "check_policy", "input": {"product_id": "P-1"}},
                       {"type": "tool_use", "id": "c2", "name": "search_catalog", "input": {"query": "x"}}]},
                   {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": "c1", "content": '{"verdict": "ALLOW"}'},
                       {"type": "tool_result", "tool_use_id": "c2", "content": {"products": []}}]}]}
    req = to_openai_request(payload, "gpt-x")
    assert req["model"] == "gpt-x" and req["max_tokens"] == 99 and req["tool_choice"] == "auto"
    assert req["tools"] == [{"type": "function", "function": {"name": "check_policy", "description": "d",
                                                              "parameters": payload["tools"][0]["input_schema"]}}]
    m = req["messages"]
    assert m[0] == {"role": "system", "content": "SYS"}
    assert m[1] == {"role": "user", "content": "buy a cable"}
    assert m[2]["role"] == "assistant" and m[2]["content"] == "checking"
    assert [tc["id"] for tc in m[2]["tool_calls"]] == ["c1", "c2"]
    assert json.loads(m[2]["tool_calls"][0]["function"]["arguments"]) == {"product_id": "P-1"}
    assert m[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"verdict": "ALLOW"}'}
    assert m[4] == {"role": "tool", "tool_call_id": "c2", "content": '{"products": []}'}
    assert len(m) == 5


def test_assistant_without_text_sends_null_content_and_max_tokens_param(monkeypatch):
    msgs = [{"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "t", "input": {}}]}]
    req = to_openai_request({"messages": msgs, "max_tokens": 5}, "m")
    assert req["messages"][0]["content"] is None and "tools" not in req
    monkeypatch.setenv("OPENAI_MAX_TOKENS_PARAM", "max_completion_tokens")
    req = to_openai_request({"messages": [], "max_tokens": 5}, "m")
    assert req["max_completion_tokens"] == 5 and "max_tokens" not in req
    monkeypatch.setenv("OPENAI_MAX_TOKENS_PARAM", "none")
    assert "max_tokens" not in to_openai_request({"messages": [], "max_tokens": 5}, "m")
    monkeypatch.setenv("OPENAI_MAX_TOKENS_PARAM", "bogus")
    with pytest.raises(LLMError):
        to_openai_request({"messages": [], "max_tokens": 5}, "m")


def test_from_openai_response_parses_tool_calls():
    body = {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        oa_call("call_a", "check_policy", {"product_id": "P-1", "quantity": 2}),
        {"id": "call_b", "type": "function", "function": {"name": "search_catalog", "arguments": {"query": "x"}}},
        {"id": "call_c", "type": "function", "function": {"name": "create_order", "arguments": "{not json"}},
        {"id": "call_d", "type": "function", "function": {"name": "create_order", "arguments": "[1, 2]"}},
    ]}}], "usage": {"prompt_tokens": 11, "completion_tokens": 4}}
    out = from_openai_response(body)
    assert out["stop_reason"] == "tool_use" and out["usage"] == {"input_tokens": 11, "output_tokens": 4}
    assert [(b["id"], b["name"], b["input"]) for b in out["content"]] == [
        ("call_a", "check_policy", {"product_id": "P-1", "quantity": 2}),
        ("call_b", "search_catalog", {"query": "x"}),
        ("call_c", "create_order", {"_invalid_arguments": True}),
        ("call_d", "create_order", {"_invalid_arguments": True})]


def test_tool_calls_with_finish_stop_missing_or_duplicate_ids_still_map():
    # Ollama-style: finish_reason "stop" although tool calls are present; ids missing / repeated
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "", "tool_calls": [
        {"function": {"name": "search_catalog", "arguments": "{}"}},
        {"id": "x", "function": {"name": "check_policy", "arguments": "{}"}},
        {"id": "x", "function": {"name": "check_policy", "arguments": "{}"}},
        {"id": "y", "function": {"arguments": "{}"}}]}}]}
    out = from_openai_response(body)
    ids = [b["id"] for b in out["content"]]
    assert out["stop_reason"] == "tool_use" and len(ids) == 3 and len(set(ids)) == 3


@pytest.mark.parametrize("finish,expected", [("stop", "end_turn"), ("length", "max_tokens"), (None, "end_turn")])
def test_finish_reason_mapping(finish, expected):
    body = {"choices": [{"finish_reason": finish, "message": {"content": "<summary>x</summary>"}}]}
    out = from_openai_response(body)
    assert out["stop_reason"] == expected and out["content"] == [{"type": "text", "text": "<summary>x</summary>"}]


def test_malformed_response_is_an_llm_error():
    with pytest.raises(LLMError):
        from_openai_response({"choices": []})


def test_openai_post_request_shape(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1/")
    monkeypatch.setenv("OPENAI_MODEL", "llama-test")
    cap = Capture(oa_reply("hi"))
    monkeypatch.setattr(httpx, "post", cap)
    out = openai_post({"model": "claude-x", "messages": [{"role": "user", "content": "q"}]})
    c = cap.calls[0]
    assert c["url"] == "http://localhost:11434/v1/chat/completions"
    assert c["json"]["model"] == "llama-test"  # OPENAI_MODEL wins over the payload's model
    assert "authorization" not in c["headers"]  # no key: local servers need none
    assert out["content"] == [{"type": "text", "text": "hi"}]
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abcdefgh")
    cap.responses.append(oa_reply("hi"))
    openai_post({"messages": []})
    assert cap.calls[1]["headers"]["authorization"] == "Bearer sk-test-abcdefgh"


def test_openai_default_base_is_the_official_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "m")
    cap = Capture(oa_reply("ok"))
    monkeypatch.setattr(httpx, "post", cap)
    openai_post({"messages": []})
    assert cap.calls[0]["url"] == "https://api.openai.com/v1/chat/completions"


# ------------------------------------------------------------------ OpenAI error redaction

def _oa_error(monkeypatch, response):
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    with pytest.raises(LLMError) as ei:
        openai_post({"messages": []})
    return str(ei.value)


def test_openai_error_withholds_message_with_configured_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-SECRETSECRETSECRET")
    body = {"error": {"message": "Incorrect API key provided: sk-proj-SECRETSECRETSECRET",
                      "type": "invalid_request_error", "code": "invalid_api_key"}}
    msg = _oa_error(monkeypatch, httpx.Response(401, json=body))
    assert msg.startswith("Chat Completions HTTP 401: invalid_request_error (message withheld")
    assert "SECRET" not in msg


def test_openai_error_masks_other_secrets_and_tokens(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "pp-secret-value")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anth-token-1")
    body = {"error": {"message": "echo Bearer abc.def sk-otherkey12345678 " + "Q" * 45, "type": None,
                      "code": "rate_limit_exceeded"}}
    msg = _oa_error(monkeypatch, httpx.Response(429, json=body))
    for leaked in ("abc.def", "sk-otherkey12345678", "Q" * 45):
        assert leaked not in msg
    assert "rate_limit_exceeded" in msg
    body = {"error": {"message": "saw pp-secret-value and anth-token-1", "type": "server_error"}}
    msg = _oa_error(monkeypatch, httpx.Response(500, json=body))
    assert "pp-secret-value" not in msg and "anth-token-1" not in msg and "withheld" in msg


def test_openai_string_error_and_non_json_body(monkeypatch):
    msg = _oa_error(monkeypatch, httpx.Response(404, json={"error": "model 'x' not found"}))
    assert msg.startswith("Chat Completions HTTP 404: unknown_error: model 'x' not found")
    msg = _oa_error(monkeypatch, httpx.Response(502, text="<html>gateway Authorization: Bearer zzz</html>"))
    assert "zzz" not in msg and "non-JSON" in msg


def test_openai_missing_key_hint_and_transport_error(monkeypatch):
    msg = _oa_error(monkeypatch, httpx.Response(401, json={"error": {"message": "no key", "type": "x"}}))
    assert "OPENAI_API_KEY" in msg and "AGENT_MODE=offline" in msg

    def boom(*a, **k):
        raise httpx.ConnectError("connect to secret-host failed")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(LLMError) as ei:
        openai_post({"messages": []})
    assert "secret-host" not in str(ei.value) and "ConnectError" in str(ei.value)


# ------------------------------------------------------------------ end to end: the policy stays the authority

def test_openai_agent_loop_maps_tools_and_policy_decides(service, monkeypatch):
    """The model asks to buy something over the cap via create_order; the server refuses or holds it
    exactly as with the Anthropic provider, and each tool result goes back under its tool_call_id."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    cap = Capture(
        oa_reply(None, [oa_call("c1", "search_catalog", {"query": "usb"})], finish="tool_calls"),
        oa_reply(None, [oa_call("c2", "create_order", {"product_id": "P-1003", "quantity": 1}),
                        oa_call("c3", "create_order", {"product_id": "P-1001", "quantity": 1})], finish="tool_calls"),
        oa_reply("<summary>- done</summary>"))
    monkeypatch.setattr(httpx, "post", cap)
    res = PurchasingAgent(service).run("buy a keyboard and a cable")
    assert res.final_text == "- done" and res.turns == 3
    assert res.usage == {"input_tokens": 21, "output_tokens": 9}
    tools = [t for t in res.transcript if t["role"] == "tool"]
    assert [t["name"] for t in tools] == ["search_catalog", "create_order", "create_order"]
    keyboard, cable = tools[1]["output"], tools[2]["output"]
    assert keyboard["result"] != "ORDER_PLACED"  # over the per-order cap: the gate refuses / holds it
    assert cable["result"] == "ORDER_PLACED"
    third = cap.calls[2]["json"]["messages"]
    tool_msgs = [m for m in third if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3"]
    assert json.loads(tool_msgs[2]["content"])["result"] == "ORDER_PLACED"
    assert cap.calls[0]["json"]["messages"][0]["role"] == "system"


def test_invalid_tool_arguments_are_refused_not_guessed(service, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    bad = {"id": "c1", "type": "function", "function": {"name": "create_order", "arguments": "{oops"}}
    cap = Capture(oa_reply(None, [bad], finish="tool_calls"), oa_reply("<summary>- nothing</summary>"))
    monkeypatch.setattr(httpx, "post", cap)
    res = PurchasingAgent(service).run("buy")
    out = [t for t in res.transcript if t["role"] == "tool"][0]["output"]
    assert "valid JSON" in out["error"]
    assert service.store.list("orders") == []


# ------------------------------------------------------------------ Codex review fixes (malformed replies, strict args, no traceback)

@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}), ({"a": 1}, {"a": 1}), (None, {}), ("", {}), ("  ", {}),
    ("{x", {"_invalid_arguments": True}), ("[1]", {"_invalid_arguments": True}), ("1", {"_invalid_arguments": True}),
    ("null", {"_invalid_arguments": True}), (3, {"_invalid_arguments": True}), (True, {"_invalid_arguments": True}),
    ([1], {"_invalid_arguments": True}),
])
def test_tool_arguments_strict(raw, expected):
    body = {"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "t", "arguments": raw}}]}}]}
    assert from_openai_response(body)["content"][0]["input"] == expected


@pytest.mark.parametrize("body", [None, "x", {"choices": "x"}, {"choices": [None]}, {"choices": [{"message": "x"}]}])
def test_malformed_shapes_are_llm_errors(body):
    with pytest.raises(LLMError):
        from_openai_response(body)


def test_odd_fields_never_crash():
    body = {"choices": [{"finish_reason": 7, "message": {"content": "x", "tool_calls": [None, "x", {"function": "x"}]}}],
            "usage": {"prompt_tokens": "NaN", "completion_tokens": -1}}
    assert from_openai_response(body) == {"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn",
                                          "usage": {"input_tokens": 0, "output_tokens": 0}}


def test_malformed_reply_after_a_purchase_keeps_the_partial_result(service, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    cap = Capture(oa_reply(None, [oa_call("c1", "create_order", {"product_id": "P-1001", "quantity": 1})], finish="tool_calls"),
                  httpx.Response(200, json={"choices": [{"message": "garbage"}]}))
    monkeypatch.setattr(httpx, "post", cap)
    res = PurchasingAgent(service).run("a cable")
    assert res.stopped_reason == "llm_error"
    assert "bought and paid" in res.final_text and "stopped early" in res.final_text


def test_user_text_after_tool_results_keeps_valid_order():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "also this"},
                                         {"type": "tool_result", "tool_use_id": "a", "content": "r"}]}]
    out = to_openai_request({"messages": msgs}, "m")["messages"]
    assert [m["role"] for m in out] == ["tool", "user"]  # tool results must follow the assistant tool_calls


def test_cli_prints_redacted_llm_error_without_traceback(monkeypatch, capsys):
    from app import cli
    monkeypatch.setenv("PAYPAL_MODE", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "m")

    def boom(*a, **k):
        raise httpx.ConnectError("connect to secret-host.internal failed")
    monkeypatch.setattr(httpx, "post", boom)
    assert cli.main(["a cable"]) == 1
    err = capsys.readouterr().err
    assert "ConnectError" in err and "secret-host" not in err and "Traceback" not in err


def test_transport_error_does_not_chain_the_raw_exception(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "m")

    def boom(*a, **k):
        raise httpx.ConnectError("secret-host")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(LLMError) as ei:
        openai_post({"messages": []})
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


@pytest.mark.parametrize("body", [[], "x", {"content": "x"}, {"content": [None]}, {"content": [{"type": "text"}]},
                                  {"content": [{"type": "tool_use", "name": "t", "input": {}}]},
                                  {"content": [{"type": "tool_use", "id": "a", "name": "t", "input": "x"}]}])
def test_anthropic_malformed_200_is_an_llm_error(monkeypatch, body):
    monkeypatch.setattr(httpx, "post", Capture(httpx.Response(200, json=body)))
    with pytest.raises(LLMError):
        anthropic_post({"model": "m", "messages": []})


def test_anthropic_odd_usage_is_normalised(monkeypatch):
    monkeypatch.setattr(httpx, "post", Capture(httpx.Response(200, json={"content": [], "usage": {"input_tokens": "x"}})))
    out = anthropic_post({"model": "m", "messages": []})
    assert out["usage"] == {"input_tokens": 0, "output_tokens": 0} and out["stop_reason"] == ""


def test_anthropic_malformed_reply_after_a_purchase_keeps_the_partial_result(service, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    first = {"content": [{"type": "tool_use", "id": "t1", "name": "create_order",
                          "input": {"product_id": "P-1001", "quantity": 1}}], "stop_reason": "tool_use"}
    monkeypatch.setattr(httpx, "post", Capture(httpx.Response(200, json=first), httpx.Response(200, json=[])))
    res = PurchasingAgent(service).run("a cable")
    assert res.stopped_reason == "llm_error"
    assert "bought and paid" in res.final_text and "stopped early" in res.final_text
