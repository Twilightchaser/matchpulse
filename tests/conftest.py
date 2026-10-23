from __future__ import annotations

import pytest

from app.metrics import MetricsEngine
from fixture_match import SyntheticMatch


@pytest.fixture(scope="session")
def match():
    return SyntheticMatch()


@pytest.fixture(scope="session")
def events(match):
    return match.generate()


@pytest.fixture
def snap(match, events):
    def build(minute: int, second: int | None = None):
        return MetricsEngine().snapshot(events, minute, second, home_team=match.home_team,
                                        away_team=match.away_team, match_id=match.match_id)
    return build


class FakeNarrative:
    """Scripted stand-in for FoundryRuntime: returns queued answers and records every prompt."""

    def __init__(self, explanations=None, localized=None, recap=None, explain_error=None):
        self.explanations = list(explanations or [])
        self.localized = localized
        self.recap_text = recap
        self.explain_error = explain_error
        self.explain_calls: list[tuple[str, str | None]] = []
        self.localize_calls = 0

    def explain(self, candidate, snapshot, revision_note):
        self.explain_calls.append((candidate.kind, revision_note))
        if self.explain_error:
            raise self.explain_error
        text = self.explanations.pop(0) if len(self.explanations) > 1 else self.explanations[0]
        return text(candidate) if callable(text) else text

    def localize(self, candidate, profile):
        self.localize_calls += 1
        if isinstance(self.localized, Exception):
            raise self.localized
        if callable(self.localized):
            return self.localized(candidate, profile)
        if self.localized is None:
            raise RuntimeError("no localized copy scripted")
        return self.localized

    def recap(self, facts):
        if isinstance(self.recap_text, Exception):
            raise self.recap_text
        return self.recap_text(facts) if callable(self.recap_text) else self.recap_text


@pytest.fixture
def fake():
    return FakeNarrative
