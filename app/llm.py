"""DeepSeek models behind Microsoft Agent Framework agents.

The three narrative agents (MomentExplainer, AudienceDirector, RecapWriter) are Microsoft Agent
Framework ``Agent`` objects created from an ``OpenAIChatCompletionClient`` that points at DeepSeek's
OpenAI-compatible API. Orchestration, revision loops and the Evidence Guardian are unchanged; only
the model behind the agents differs from the Foundry runtime.

If ``agent-framework`` is not installed the runtime falls back to a minimal HTTP transport so the
studio still runs, and ``transport`` reports which path is active.

Environment: LLM_API_KEY, LLM_MODEL, optional LLM_BASE_URL (default https://api.deepseek.com),
LLM_TIMEOUT_SECONDS (default 20) and LLM_TRANSPORT=http to force the fallback transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.foundry import DIRECTOR_INSTRUCTIONS, EXPLAINER_INSTRUCTIONS, RECAP_INSTRUCTIONS, FoundryRuntime

ALLOWED_BASE_URLS = ("https://api.deepseek.com", "https://api.deepseek.com/v1")
# DeepSeek hybrid models: keep answers fast and short for live cards.
REQUEST_OPTIONS = {"max_tokens": 700, "extra_body": {"thinking": {"type": "disabled"}}}


def llm_configured() -> bool:
    return bool(os.getenv("LLM_API_KEY") and os.getenv("LLM_MODEL"))


class DeepSeekRuntime(FoundryRuntime):
    provider = "deepseek"

    def __init__(self, timeout_seconds: float | None = None, async_client: Any = None) -> None:
        if not llm_configured():
            raise RuntimeError("LLM_API_KEY or LLM_MODEL is missing")
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
        if self.base_url not in ALLOWED_BASE_URLS:
            raise ValueError("DeepSeek endpoint must be https://api.deepseek.com or its /v1 endpoint")
        self.model = os.environ["LLM_MODEL"]
        self.timeout_seconds = timeout_seconds or float(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
        self.instructions = {"explainer": EXPLAINER_INSTRUCTIONS, "director": DIRECTOR_INSTRUCTIONS,
                             "recap": RECAP_INSTRUCTIONS}
        try:
            if os.getenv("LLM_TRANSPORT", "").lower() == "http":
                raise ImportError("HTTP transport forced by LLM_TRANSPORT")
            from agent_framework.openai import OpenAIChatCompletionClient
        except ImportError:
            self.transport = "http"
            self.explainer, self.director, self.recap_writer = (
                EXPLAINER_INSTRUCTIONS, DIRECTOR_INSTRUCTIONS, RECAP_INSTRUCTIONS)
            self.loop = None
            return
        client = OpenAIChatCompletionClient(model=self.model, api_key=os.environ["LLM_API_KEY"],
                                            base_url=self.base_url, async_client=async_client)
        self.explainer = client.as_agent(name="MomentExplainer", instructions=EXPLAINER_INSTRUCTIONS,
                                         default_options=REQUEST_OPTIONS)
        self.director = client.as_agent(name="AudienceDirector", instructions=DIRECTOR_INSTRUCTIONS,
                                        default_options=REQUEST_OPTIONS)
        self.recap_writer = client.as_agent(name="RecapWriter", instructions=RECAP_INSTRUCTIONS,
                                            default_options=REQUEST_OPTIONS)
        self.transport = "agent-framework"
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, name="deepseek-agents", daemon=True)
        self.loop_thread.start()

    def _run_sync(self, agent: Any, prompt: str) -> str:
        if self.transport == "agent-framework":
            return super()._run_sync(agent, prompt)
        return self._http(agent, prompt)

    def _http(self, instructions: str, prompt: str) -> str:
        payload = {"model": self.model, "messages": [{"role": "system", "content": instructions},
                                                     {"role": "user", "content": prompt}],
                   "stream": False, "max_tokens": REQUEST_OPTIONS["max_tokens"], **REQUEST_OPTIONS["extra_body"]}
        request = Request(self.base_url + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + os.environ["LLM_API_KEY"]})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"DeepSeek HTTP {exc.code}; check key, balance and model") from None
        except (URLError, TimeoutError):
            raise RuntimeError("DeepSeek connection failed or timed out") from None
        text = result["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("DeepSeek returned an empty response")
        return text.strip()

    def ping(self) -> str:
        """One tiny request through the same agents the studio uses."""
        if self.transport == "agent-framework":
            return self._run_sync(self.explainer, "Connection check. Reply with the single word OK.")
        return self._http("Reply only OK.", "Connection check. Reply OK.")

    def close(self) -> None:
        if self.loop is not None:
            super().close()
