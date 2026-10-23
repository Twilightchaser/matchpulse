"""Synthetic data generator and metrics engine."""

from __future__ import annotations

from app.metrics import MetricsEngine
from app.models import EventType, MatchEvent
from fixture_match import DEFAULT_SEED, SyntheticMatch, shot_xg


def test_generation_is_deterministic(events):
    again = SyntheticMatch(seed=DEFAULT_SEED).generate()
    assert [e.model_dump() for e in again] == [e.model_dump() for e in events]


def test_concurrent_generation_is_safe():
    from concurrent.futures import ThreadPoolExecutor

    shared = SyntheticMatch()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: [e.event_id + e.clock for e in shared.generate()], range(6)))
    assert all(result == results[0] for result in results)


def test_demo_match_story(events):
    goals = [(e.clock, e.team_id) for e in events if e.outcome == "goal"]
    assert goals == [("18:27", "Riverside Athletic"), ("44:10", "Northbridge FC"), ("59:14", "Northbridge FC")]


def test_event_mix_is_football_realistic():
    totals = {"shots": 0, "xg": 0.0, "goals": 0, "passes": 0, "completed": 0}
    for seed in range(1, 21):
        for e in SyntheticMatch(seed=seed).generate():
            if e.event_type == EventType.SHOT:
                totals["shots"] += 1
                totals["xg"] += e.attributes["xg"]
                totals["goals"] += e.outcome == "goal"
            if e.event_type == EventType.PASS:
                totals["passes"] += 1
                totals["completed"] += e.outcome == "success"
    assert 20 <= totals["shots"] / 20 <= 36
    assert 2.0 <= totals["xg"] / 20 <= 4.0
    assert 1.5 <= totals["goals"] / 20 <= 4.5
    assert 0.74 <= totals["completed"] / totals["passes"] <= 0.86


def test_xg_model_is_monotonic():
    assert shot_xg(99.5, 34) > shot_xg(94, 34) > shot_xg(88.5, 34) > shot_xg(80, 34)
    assert shot_xg(100, 10) < shot_xg(100, 34)


def test_defensive_actions_belong_to_the_ball_winner(events):
    for e in events:
        if e.event_type == EventType.POSSESSION_CHANGE:
            assert e.possession_team_id == e.team_id == e.attributes["to_team"]
            assert e.attributes["from_team"] != e.team_id
        if e.event_type == EventType.TACKLE and e.outcome == "success":
            assert e.possession_team_id == e.team_id


def test_kickoffs(events):
    assert events[0].attributes.get("kickoff") and events[0].team_id == "Northbridge FC"
    second_half = next(e for e in events if e.minute >= 45)
    assert second_half.clock == "45:00" and second_half.team_id == "Riverside Athletic"


def test_home_away_come_from_metadata_not_alphabet(events):
    renamed = [MatchEvent(**{**e.model_dump(), "team_id": {"Northbridge FC": "Zeta Town",
                                                           "Riverside Athletic": "Alpha United"}[e.team_id]})
               for e in events]
    snap = MetricsEngine().snapshot(renamed, 90, home_team="Zeta Town", away_team="Alpha United")
    assert (snap.home_team, snap.away_team) == ("Zeta Town", "Alpha United")
    assert (snap.score_home, snap.score_away) == (2, 1)
    inferred = MetricsEngine().snapshot(renamed, 90)
    assert inferred.home_team == "Zeta Town"


def test_momentum_is_a_share_that_never_caps(snap):
    seen_both_high = False
    for minute in range(1, 91):
        s = snap(minute)
        home, away = (s.metrics[t].momentum_score for t in (s.home_team, s.away_team))
        assert abs(home + away - 100) < 0.11
        assert 0 <= home <= 100
        seen_both_high |= home == away == 100
    assert not seen_both_high


def test_possession_and_player_totals(snap):
    s = snap(90)
    home, away = s.metrics[s.home_team], s.metrics[s.away_team]
    assert abs(home.possession_pct + away.possession_pct - 100) < 0.11
    for team in (home, away):
        players = [p for p in s.players.values() if p.team_id == team.team_id]
        assert sum(p.passes for p in players) == team.passes
        assert sum(p.passes_completed for p in players) == team.passes_completed
        assert sum(p.goals for p in players) == team.goals
        assert team.passes_completed <= team.passes
        assert 0 < team.avg_pass_distance_m < 45 and 0 < team.avg_pass_difficulty <= 1
    outfield = [p for p in s.players.values() if not p.player_id.endswith("-1")]
    assert max(p.distance_m for p in outfield) > 8000


def test_evidence_index_covers_history_but_is_never_serialized(snap):
    s = snap(60)
    assert len(s.recent_events) <= 12
    assert "evt-0108" in s.event_index          # 18:27 goal, far outside the prompt window
    assert all(e.match_second <= s.match_second for e in s.event_index.values())
    assert "event_index" not in s.model_dump()
    assert "event_index" not in s.model_dump_json()


def test_early_and_empty_moments_do_not_crash(snap):
    s = snap(0, 0)
    assert s.metrics[s.home_team].momentum_score == 50 or len(s.event_index) >= 1
    empty = MetricsEngine().snapshot([], 10, home_team="A", away_team="B", match_id="m")
    assert empty.score_home == 0 and empty.metrics["A"].momentum_score == 50
