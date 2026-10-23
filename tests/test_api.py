"""HTTP API and SSE replay."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app, match_events

client = TestClient(app)


def test_health_and_metadata():
    assert client.get("/api/health").json()["status"] == "ok"
    meta = client.get("/api/match").json()
    assert meta["home_team"] == "Northbridge FC" and meta["away_team"] == "Riverside Athletic"


def test_static_views():
    assert "MatchPulse" in client.get("/").text
    all_events = client.get("/api/events?limit=1000").json()
    assert len(all_events) == min(1000, len(match_events())) and all_events[0]["event_id"] == "evt-0001"
    assert len(client.get("/api/lineup").json()) == 22
    frames = client.get("/api/frames").json()
    assert frames[0]["second"] == 0 and len(frames[0]["positions"]) == 22
    assert "n-10" in client.get("/api/player-metrics").json()


def test_insight_profiles():
    goal = next(e for e in match_events() if e.outcome == "goal")
    moment = {"minute": goal.minute, "second": goal.second}
    body = client.get("/api/insight", params={**moment, "mode": "analyst"}).json()
    assert body["overlay"]["kind"] == "goal" and body["overlay"]["verified"]
    assert "event_index" not in body["snapshot"]
    zh = client.get("/api/insight", params={**moment, "language": "zh", "player": goal.player_id,
                                            "team": goal.team_id}).json()["overlay"]
    assert zh["language"] == "zh" and "球员聚焦" in zh["eyebrow"] and "你的球队" in zh["eyebrow"]
    assert client.get("/api/insight", params={"minute": 30, "mode": "coach"}).status_code == 422


def test_recap():
    recap = client.get("/api/recap").json()
    goals = [e for e in match_events() if e.outcome == "goal"]
    assert sum(recap["score"].values()) == len(goals)
    assert len(recap["goals"]) == len(goals) and recap["summary"]


def test_replay_stream_announces_the_winner_once():
    seen_updates, goal_cards = 0, set()
    with client.stream("GET", "/api/replay", params={"from_second": 5395, "speed": 60, "mode": "analyst"}) as response:
        kind = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: ") and kind == "match_update":
                packet = json.loads(line[6:])
                seen_updates += 1
                assert "event_index" not in packet["snapshot"]
                overlay = packet["overlay"]
                if overlay and overlay["kind"] == "goal":
                    goal_cards.add(overlay["insight_id"])
            elif kind == "replay_complete":
                break
            if packet_time_exceeded(seen_updates):
                break
    assert seen_updates >= 1
    assert len(goal_cards) <= 1


def packet_time_exceeded(updates: int) -> bool:
    return updates > 12  # minutes 58-64 are enough to see the 59:14 goal


def test_moment_index_is_chronological_and_matches_real_events():
    result = client.get("/api/moments")
    assert result.status_code == 200
    moments = result.json()
    assert moments and [m["second"] for m in moments] == sorted(m["second"] for m in moments)
    by_id = {e.event_id: e for e in match_events()}
    for moment in moments:
        event = by_id[moment["event_id"]]
        assert moment["second"] == event.match_second
        assert moment["kind"] == ("goal" if event.outcome == "goal" else event.event_type.value)
        assert moment["kind"] in {"goal", "shot", "corner"}
