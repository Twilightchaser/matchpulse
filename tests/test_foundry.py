"""FoundryRuntime plumbing with the real Agent Framework client but fake async agents (no cloud calls)."""

from __future__ import annotations

import asyncio

import pytest

from app.agents import AudienceProfile, InsightWorkflow, MomentExplainerAgent
from app.foundry import parse_card_json

pytest.importorskip("agent_framework.foundry")


class ScriptedAgent:
    def __init__(self, replies, delay=0.0):
        self.replies = list(replies)
        self.delay = delay
        self.prompts: list[str] = []

    async def run(self, prompt):
        self.prompts.append(prompt)
        await asyncio.sleep(self.delay)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return type("Response", (), {"text": reply})()


@pytest.fixture
def runtime(monkeypatch):
    from app.foundry import FoundryRuntime

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/demo")
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-4o-mini")
    rt = FoundryRuntime(timeout_seconds=0.5)
    yield rt
    rt.close()


def test_parse_card_json_handles_fenced_output():
    card = parse_card_json('```json\n{"headline": "H", "body": "B", "eyebrow": "E"}\n```')
    assert card == {"headline": "H", "body": "B", "eyebrow": "E"}
    with pytest.raises(ValueError):
        parse_card_json('{"headline": "H"}')


def test_runtime_revision_loop_and_card(runtime, snap):
    s = snap(60)
    honest = "N. Player 10 scored at 59:14 from a 0.24 xG chance, making it 2-1."
    explainer = ScriptedAgent(["N. Player 10 scored from a 0.81 xG chance.", honest])
    director = ScriptedAgent(['{"headline": "N. Player 10 scores", "body": "A 0.24 xG finish makes it 2-1.", "eyebrow": "GOAL"}'])
    runtime.set_agents_for_test(explainer=explainer, director=director)
    wf = InsightWorkflow(narrative_service=runtime, stateful=False)
    card, traces = wf.run(s, AudienceProfile(mode="analyst"))
    assert card.provider == "foundry" and card.body == "A 0.24 xG finish makes it 2-1."
    assert "0.81" in explainer.prompts[1]          # Guardian feedback reached the LLM prompt
    assert "evt-0329" in explainer.prompts[0]      # cited event facts are in the prompt
    assert any(t.status == "revised" and t.provider == "foundry" for t in traces)


def test_runtime_timeout_falls_back(runtime, snap):
    runtime.set_agents_for_test(explainer=ScriptedAgent(["slow"], delay=2.0))
    result = InsightWorkflow(narrative_service=runtime, stateful=False).analyze(snap(60))
    assert result.verified and result.provider == "local"
    assert result.candidate.explanation == MomentExplainerAgent.explain(result.candidate)
