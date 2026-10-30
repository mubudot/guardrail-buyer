"""Final re-check (2026-10-30): upstream LLM errors never expose raw bodies or secrets."""
import httpx
import pytest

from app.agent import LLMError, anthropic_post


def _raise_with(monkeypatch, response):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    with pytest.raises(LLMError) as ei:
        anthropic_post({"model": "m"})
    return str(ei.value)


def test_structured_error_keeps_type_and_redacts_configured_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SECRETSECRETSECRET")
    body = {"type": "error", "error": {"type": "authentication_error",
                                       "message": "invalid x-api-key sk-ant-api03-SECRETSECRETSECRET"}}
    msg = _raise_with(monkeypatch, httpx.Response(401, json=body))
    assert msg.startswith("Messages API HTTP 401: authentication_error: invalid x-api-key")
    assert "SECRET" not in msg


def test_other_configured_secrets_and_long_tokens_are_masked(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "pp-secret-value")
    monkeypatch.setenv("APPROVER_TOKEN", "approver-xyz")
    body = {"error": {"type": "invalid_request_error",
                      "message": "echo pp-secret-value approver-xyz Bearer abc.def " + "Z" * 48}}
    msg = _raise_with(monkeypatch, httpx.Response(400, json=body))
    for leaked in ("pp-secret-value", "approver-xyz", "abc.def", "Z" * 48):
        assert leaked not in msg
    assert "invalid_request_error" in msg


def test_non_json_body_is_never_echoed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    msg = _raise_with(monkeypatch, httpx.Response(502, text="<html>proxy says x-api-key=topsecret</html>"))
    assert "topsecret" not in msg and "non-JSON error body" in msg


def test_agent_endpoint_502_carries_only_the_safe_detail(monkeypatch):
    from fastapi.testclient import TestClient
    from app.agent import PurchasingAgent
    from app.main import create_app
    from app.paypal_client import MockPayPal
    from tests.test_review5_fixes import _svc

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAKME-123456789")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
        500, json={"error": {"type": "api_error", "message": "upstream saw sk-ant-LEAKME-123456789"}}))
    s = _svc(MockPayPal())
    app = create_app(s, approver_token="t", agent_rate_limit=None)
    app.state.agent = PurchasingAgent(s)
    r = TestClient(app).post("/agent/request", json={"request": "buy 1 usb-c cable"})
    assert "LEAKME" not in r.text
    assert "api_error" in r.text


def test_markdown_relative_links_resolve():
    """Every relative link/anchor in tracked Markdown points at something that exists (judges click them)."""
    import os
    import re
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        files = subprocess.check_output(["git", "-C", root, "ls-files", "*.md"], text=True).split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")

    def anchors(path):
        out = set()
        for line in open(path, encoding="utf-8"):
            m = re.match(r"^#{1,6}\s+(.*)", line)
            if m:
                out.add(re.sub(r"[^\w\- ]", "", m.group(1).strip().lower()).replace(" ", "-"))
        return out

    broken = []
    for f in files:
        p = os.path.join(root, f)
        for text, url in re.findall(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)", open(p, encoding="utf-8").read()):
            if re.match(r"^(https?|mailto):", url):
                continue
            path, _, anc = url.partition("#")
            tgt = os.path.normpath(os.path.join(os.path.dirname(p), path)) if path else p
            if not os.path.exists(tgt) or (anc and tgt.endswith(".md") and anc not in anchors(tgt)):
                broken.append(f"{f}: {url}")
    assert broken == []
