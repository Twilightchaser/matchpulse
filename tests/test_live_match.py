"""Sanity checks on the match the studio actually serves (app.simulator), not the frozen fixture.

The other suites run on tests/fixture_match.py so narrative regressions stay stable. These tests
catch the opposite failure: simulator changes that silently break what viewers see (a match
without a story, an indicator stuck on one value, one card repeated all match).
If they fail after a simulator change, re-run scripts/calibrate.py and update app/calibration.py.
"""

from __future__ import annotations

import statistics as st
from collections import Counter

import pytest

from app.agents import InsightWorkflow
from app.calibration import CONTROL_SHARE
from app.metrics import MetricsEngine
from app.models import EventType
from app.simulator import SyntheticMatch


@pytest.fixture(scope="module")
def live():
    match = SyntheticMatch()
    events = match.generate()
    snaps = [MetricsEngine().snapshot(events, minute, home_team=match.home_team, away_team=match.away_team,
                                      match_id=match.match_id) for minute in range(1, 91)]
    return match, events, snaps


def test_demo_story_is_the_one_the_video_tells(live):
    _, events, _ = live
    goals = [(e.clock, e.team_id) for e in events if e.outcome == "goal"]
    assert goals == [("17:46", "Northbridge FC"), ("43:41", "Riverside Athletic"), ("57:17", "Riverside Athletic")]


def test_match_totals_are_football_realistic(live):
    _, _, snaps = live
    final = snaps[-1]
    for team in final.metrics.values():
        assert 70 <= team.pass_accuracy <= 90
        assert 5 <= team.shots <= 25
        assert 30 <= team.possession_pct <= 70
    assert final.score_home + final.score_away == 3


def test_phase_indicator_moves(live):
    _, _, snaps = live
    phases = Counter(s.phase for s in snaps)
    assert set(phases) == {"controlled", "contested", "chaotic"}
    assert max(phases.values()) <= 0.7 * len(snaps)


def test_momentum_has_real_spread(live):
    _, _, snaps = live
    leader = [max(t.momentum_score for t in s.metrics.values()) for s in snaps[5:]]
    assert 52 <= st.median(leader) <= 70
    assert any(value >= CONTROL_SHARE for value in leader)


def test_cards_are_varied_paced_and_verified(live):
    _, _, snaps = live
    workflow = InsightWorkflow(stateful=True)
    kinds = Counter()
    for snapshot in snaps:
        result = workflow.analyze(snapshot)
        if result.candidate:
            assert result.verified, (snapshot.minute, result.candidate.explanation)
            kinds[result.candidate.kind] += 1
    total = sum(kinds.values())
    assert 15 <= total <= 40, kinds
    assert len(kinds) >= 6, kinds
    assert max(kinds.values()) <= 0.35 * total, kinds
    assert kinds["goal"] == 3


def test_speed_metrics_vary(live):
    _, events, _ = live
    shot_speeds = {e.attributes["shot_speed_kmh"] for e in events if e.event_type == EventType.SHOT}
    assert len(shot_speeds) >= 5 and all(60 <= v <= 120 for v in shot_speeds)
    ball_speeds = [e.attributes["ball_speed_kmh"] for e in events if e.event_type == EventType.PASS]
    assert st.pstdev(ball_speeds) > 5
