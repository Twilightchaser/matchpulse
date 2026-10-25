"""Microsoft Foundry narrative agents (Microsoft Agent Framework).

Three specialised agents run on a Foundry model deployment:

* MomentExplainer - explains why a moment matters, revising on Evidence Guardian feedback.
* AudienceDirector - rewrites a verified insight for a viewer's mode and language.
* RecapWriter     - writes the full-time recap with event-id citations.

All outputs are checked by deterministic code in ``app.agents`` before they reach the screen.

Environment:
    FOUNDRY_PROJECT_ENDPOINT  Foundry project endpoint
    FOUNDRY_MODEL             model deployment name (e.g. gpt-4o-mini)
    FOUNDRY_TIMEOUT_SECONDS   per-call timeout, default 8
Authentication uses DefaultAzureCredential: `az login` locally, managed identity on Azure.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

from app.agents import AudienceProfile, event_lookup
from app.house_style import BANNED_EN, BANNED_ZH
from app.models import InsightCandidate, MatchSnapshot

# House style shared by all three agents (docs/WRITING_STYLE.md). The Evidence Guardian rejects
# the banned phrases, so the prompt and the checker always agree.
STYLE_RULES = (
    "Write like a live TV commentator or a newspaper match report, not like a report generator. "
    "Short sentences, active voice, one idea per sentence. Use football language: levelled, pulled one back, "
    "against the run of play, made the keeper work; in Chinese 扳平, 扳回一球, 反超, 错失良机. "
    "Say what the moment means for the game (who is on top, who needs a goal) instead of restating every number. "
    "Show minutes the broadcast way: the match minute plus one, e.g. 57:17 becomes 58' (Chinese: 第58分钟). "
    "No disclaimers or methodology on screen, no words about evidence or verification. Never use: "
    + ", ".join(f'"{p}"' for p in BANNED_EN + BANNED_ZH) + ". "
)
EXPLAINER_INSTRUCTIONS = (
    "You are the Moment Explainer for a live football broadcast built on synthetic match data. "
    "Explain in at most two sentences why the supplied moment matters, for an analyst audience. "
    + STYLE_RULES +
    "Hard rules: use only the supplied facts; copy every number exactly as written in the evidence "
    "(do not round, convert to percentages or add new statistics); mention only players that appear "
    "in the cited events; never invent clock times, scores or causes. "
    "If reviewer feedback is supplied, fix exactly those problems. Return plain text only."
)
DIRECTOR_INSTRUCTIONS = (
    "You are the Audience Director. Rewrite a verified football insight as a live on-screen lower-third. "
    "Analyst mode: precise, one or two numbers at most, the rest is meaning. "
    "Fan mode: short, plain and lively; no decimals, describe chance quality in words "
    "(xG 0.30+ a big chance, 0.12-0.30 a good chance, 0.05-0.12 a half-chance, below that a long shot; "
    "Chinese 绝佳机会, 不错的机会, 半个机会, 机会很小的射门). "
    + STYLE_RULES +
    "Write in the requested language. Keep team and player names exactly as given, including English names in "
    "Chinese output. Never translate club names. In English you may drop 'FC' or 'Athletic' from a club name. "
    "For Chinese, translate all ordinary terms: momentum share is 势头占比, vs is 对. Do not include English glosses "
    "or bilingual wording. Units and xG may remain unchanged. "
    "Use only numbers that appear in the insight or evidence, copied exactly; add no new facts. "
    'Return only JSON: {"headline": "...", "body": "..."} '
    "with a headline under 9 words and a body under 30 words."
)
RECAP_INSTRUCTIONS = (
    "You are the Recap Writer. Write a full-time match report of a synthetic football match in 4-5 sentences, "
    "using only the supplied facts, in the language requested in the facts. Structure: "
    "(1) the result and how it happened, e.g. came from behind, late winner, comfortable win; "
    "(2) the goals in order, joined with connectors (put X ahead, but Y levelled, Z won it); "
    "(3) the losing side's response or best chances, using the 'chasing' facts when present; "
    "(4) at most one sentence of statistics, with a point of view about who deserved what. "
    + STYLE_RULES +
    "Cite the supporting event_id in square brackets right after each goal or chance, e.g. [evt-0123]. "
    "The only scoreline you may write is the final score; describe earlier scores in words. "
    "Copy numbers exactly; do not add statistics or players that are not in the facts. Return plain text. "
    "Keep team and player names exactly as supplied; in English prose you may drop 'FC' or 'Athletic'. "
    "In Chinese output keep full club names, translate all other ordinary English words, and keep only names, "
    "units, xG and event-id citations unchanged."
)


def foundry_configured() -> bool:
    return bool(os.getenv("FOUNDRY_PROJECT_ENDPOINT") and os.getenv("FOUNDRY_MODEL"))


def _event_facts(snapshot: MatchSnapshot, candidate: InsightCandidate) -> list[dict[str, Any]]:
    index = event_lookup(snapshot)
    ids = list(dict.fromkeys(i for item in candidate.evidence for i in item.event_ids))
    facts = []
    for event_id in ids[:12]:
        event = index.get(event_id)
        if event is None:
            continue
        keep = {k: v for k, v in event.attributes.items() if k in {"xg", "distance_m", "shot_speed_kmh", "difficulty"}}
        facts.append({"event_id": event.event_id, "clock": event.clock, "team": event.team_id,
                      "player": event.player_name, "type": event.event_type.value, "outcome": event.outcome, **keep})
    return facts


def parse_card_json(text: str) -> dict[str, str]:
    """Headline and body are required. Card tags are set by the Audience Director, so a model
    eyebrow is optional and only kept for backwards compatibility."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Model card response contained no JSON object")
    parsed = json.loads(text[start:end + 1])
    if not all(isinstance(parsed.get(key), str) and parsed[key].strip() for key in ("headline", "body")):
        raise ValueError("Model card JSON is missing headline or body")
    card = {key: parsed[key].strip() for key in ("headline", "body")}
    if isinstance(parsed.get("eyebrow"), str) and parsed["eyebrow"].strip():
        card["eyebrow"] = parsed["eyebrow"].strip()
    return card


class FoundryRuntime:
    """Runs the Foundry agents on a private event loop so FastAPI's loop is never blocked."""

    provider = "foundry"

    def __init__(self, timeout_seconds: float | None = None) -> None:
        if not foundry_configured():
            raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT or FOUNDRY_MODEL is missing")
        try:
            from agent_framework.foundry import FoundryChatClient
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise RuntimeError("Install agent-framework-foundry and azure-identity") from exc
        client = FoundryChatClient(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            model=os.environ["FOUNDRY_MODEL"],
            credential=DefaultAzureCredential(exclude_interactive_browser_credential=True),
        )
        self.explainer = client.as_agent(name="MomentExplainer", instructions=EXPLAINER_INSTRUCTIONS)
        self.director = client.as_agent(name="AudienceDirector", instructions=DIRECTOR_INSTRUCTIONS)
        self.recap_writer = client.as_agent(name="RecapWriter", instructions=RECAP_INSTRUCTIONS)
        self.timeout_seconds = timeout_seconds or float(os.getenv("FOUNDRY_TIMEOUT_SECONDS", "8"))
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, name="foundry-event-loop", daemon=True)
        self.loop_thread.start()

    @staticmethod
    def _text(result: Any) -> str:
        if isinstance(result, str):
            return result.strip()
        direct = getattr(result, "text", None)
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        messages = getattr(result, "messages", None)
        if messages:
            return "\n".join(getattr(message, "text", "") or "" for message in messages).strip()
        # Never fall back to repr(): an object dump would reach the Guardian and waste revisions.
        return ""

    async def _run_agent(self, agent: Any, prompt: str) -> str:
        result = await asyncio.wait_for(agent.run(prompt), timeout=self.timeout_seconds)
        text = self._text(result)
        if not text:
            raise RuntimeError("Foundry returned an empty response")
        return text

    def _run_sync(self, agent: Any, prompt: str) -> str:
        future = asyncio.run_coroutine_threadsafe(self._run_agent(agent, prompt), self.loop)
        try:
            return future.result(timeout=self.timeout_seconds + 1)
        except TimeoutError:
            future.cancel()
            raise

    def explain(self, candidate: InsightCandidate, snapshot: MatchSnapshot, revision_note: str | None) -> str:
        payload = {
            "moment": {"kind": candidate.kind, "team": candidate.team_id, "minute": candidate.minute,
                       "headline": candidate.headline},
            "match": {"clock": f"{snapshot.minute:02d}:{snapshot.second:02d}", "phase": snapshot.phase,
                      "score": f"{snapshot.home_team} {snapshot.score_home}-{snapshot.score_away} {snapshot.away_team}"},
            "evidence": [item.model_dump() for item in candidate.evidence],
            "cited_events": _event_facts(snapshot, candidate),
        }
        prompt = (
            "Explain this live moment.\n"
            f"{json.dumps(payload, ensure_ascii=False)}\n"
            f"Previous draft: {candidate.explanation or 'none'}\n"
            f"Evidence Guardian feedback to fix: {revision_note or 'none'}"
        )
        return self._run_sync(self.explainer, prompt)

    def localize(self, candidate: InsightCandidate, profile: AudienceProfile) -> dict[str, str]:
        language = {"en": "English", "zh": "Simplified Chinese"}.get(profile.language, profile.language)
        payload = {
            "headline": candidate.headline, "explanation": candidate.explanation, "kind": candidate.kind,
            "team": candidate.team_id, "evidence": [item.model_dump() for item in candidate.evidence],
        }
        prompt = (
            f"Audience mode: {profile.mode}. Language: {language}. "
            f"Favourite club: {profile.favorite_team or 'none'}.\n"
            f"Verified insight: {json.dumps(payload, ensure_ascii=False)}"
        )
        return parse_card_json(self._run_sync(self.director, prompt))

    def recap(self, facts: dict[str, Any]) -> str:
        return self._run_sync(self.recap_writer, f"Verified match facts:\n{json.dumps(facts, ensure_ascii=False)}")

    def set_agents_for_test(self, explainer: Any = None, director: Any = None, recap_writer: Any = None) -> None:
        """Inject controllable agents for integration tests that must not call the cloud."""
        self.explainer = explainer or self.explainer
        self.director = director or self.director
        self.recap_writer = recap_writer or self.recap_writer

    def close(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.loop_thread.join(timeout=2)
        if not self.loop.is_closed():
            self.loop.close()
