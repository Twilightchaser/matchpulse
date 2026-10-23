import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
import pytest
from app.llm import DeepSeekRuntime


def test_deepseek_http_fallback(monkeypatch):
    monkeypatch.setenv("LLM_TRANSPORT", "http")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    response = MagicMock()
    response.read.return_value = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
    response.__enter__.return_value = response
    with patch("app.llm.urlopen", return_value=response) as send:
        rt = DeepSeekRuntime()
        assert rt._run_sync(rt.explainer, "test") == "OK"
        body = json.loads(send.call_args.args[0].data)
        assert body["thinking"]["type"] == "disabled"
        assert body["messages"][0]["content"] == rt.explainer
    with patch("app.llm.urlopen", side_effect=HTTPError("url", 401, "secret", {}, None)):
        with pytest.raises(RuntimeError, match="HTTP 401") as error:
            rt._run_sync(rt.explainer, "test")
        assert "secret" not in str(error.value)


def test_reject_unexpected_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LLM_BASE_URL", "https://untrusted.example")
    with pytest.raises(ValueError):
        DeepSeekRuntime()


def test_deepseek_runs_through_agent_framework(monkeypatch, snap):
    """The studio's agents are Microsoft Agent Framework agents; only the model is DeepSeek."""
    pytest.importorskip("agent_framework.openai")
    import httpx
    from openai import AsyncOpenAI
    from app.agents import InsightWorkflow, MomentExplainerAgent

    monkeypatch.delenv("LLM_TRANSPORT", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    sent = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append((str(request.url), request.headers.get("authorization"), body))
        text = "OK"
        if "Explain this live moment" in body["messages"][-1]["content"]:
            text = "N. Player 10 scored at 59:14 from a 0.24 xG chance, making it 2-1."
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]})

    client = AsyncOpenAI(api_key="test-secret", base_url="https://api.deepseek.com",
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(reply)))
    rt = DeepSeekRuntime(timeout_seconds=5, async_client=client)
    try:
        assert rt.transport == "agent-framework"
        assert type(rt.explainer).__name__ == "Agent"
        assert rt.ping() == "OK"
        result = InsightWorkflow(narrative_service=rt, stateful=False).analyze(snap(60))
        assert result.verified and result.provider == "deepseek"
        assert result.candidate.explanation != MomentExplainerAgent.explain(result.candidate)
    finally:
        rt.close()
    url, auth, body = sent[-1]
    assert url == "https://api.deepseek.com/chat/completions"
    assert auth == "Bearer test-secret"
    assert body["model"] == "deepseek-flash" and body["thinking"] == {"type": "disabled"}
    assert body["messages"][0]["role"] == "system" and "Moment Explainer" in body["messages"][0]["content"]
