from fastapi.testclient import TestClient
import pytest
from app.main import MATCH, app, match_events
from app.ingest import sessions

@pytest.fixture
def client(monkeypatch):
    sessions.clear()
    monkeypatch.setenv("INGEST_API_KEY","test-ingest")
    return TestClient(app,headers={"X-Ingest-Key":"test-ingest"})


def event(i,second=1):
    return dict(event_id=f"evt-{i:04d}",match_id="external",sequence=i,minute=0,second=second,
                event_type="pass",team_id="A",player_id="p1",player_name="Alex",x=50,y=34,outcome="success")


def test_auth_and_disabled(monkeypatch):
    monkeypatch.delenv("INGEST_API_KEY",raising=False)
    assert TestClient(app).post("/api/live/matches",json={"match_id":"x","home_team":"A","away_team":"B"}).status_code==503
    monkeypatch.setenv("INGEST_API_KEY","test")
    assert TestClient(app).get("/api/live/matches/x/snapshot").status_code==401


def test_batch_idempotence_rollback_and_isolation(client):
    assert client.post("/api/live/matches",json={"match_id":"external","home_team":"A","away_team":"B"}).status_code==201
    path="/api/live/matches/external/events"
    assert client.post(path,json={"events":[event(1)]}).json()["accepted"]==1
    assert client.post(path,json={"events":[event(1)]}).json()["duplicates"]==1
    # A later invalid record must roll back the earlier valid record in the same batch.
    bad={**event(3),"team_id":"Unknown"}
    assert client.post(path,json={"events":[event(2),bad]}).status_code==422
    assert len(sessions["external"]["events"])==1
    assert client.post(path,json={"events":[{**event(1),"x":20}]}).status_code==409
    assert client.post(path,json={"events":[{**event(2),"attributes":{"xg":"bad"}}]}).status_code==422
    response=client.get("/api/live/matches/external/snapshot").json()
    assert response["snapshot"]["metrics"]["A"]["passes"]==1
    assert not response["tracking_available"]
    assert client.get("/api/match").json()["match_id"]!="external"


def test_ingested_match_events_reproduce_the_demo_goal_card(monkeypatch):
    """The insight workflow consumes normalized events, independent of the simulator source."""
    from app import main

    monkeypatch.setenv("INGEST_API_KEY", "test-ingest")
    monkeypatch.setattr(main, "llm_configured", lambda: False)
    monkeypatch.setattr(main, "foundry_configured", lambda: False)
    monkeypatch.setattr(main, "foundry_runtime", None)
    monkeypatch.setattr(main.workflow, "narrative_service", None)
    sessions.clear()

    cutoff = 57 * 60 + 20
    source_events = [item for item in match_events() if item.match_second <= cutoff]
    assert any(item.outcome == "goal" and item.match_second == 57 * 60 + 17 for item in source_events)
    # Reuse the fixture ID so its stable insight ID also selects the same local-copy variant.
    match_id = MATCH.match_id
    headers = {"X-Ingest-Key": "test-ingest"}

    with TestClient(app, headers=headers) as client:
        created = client.post("/api/live/matches", json={
            "match_id": match_id,
            "home_team": MATCH.home_team,
            "away_team": MATCH.away_team,
            "source": "synthetic",
        })
        assert created.status_code == 201

        sent = 0
        for offset in range(0, len(source_events), 200):
            batch = [item.model_dump(mode="json") for item in source_events[offset:offset + 200]]
            response = client.post(f"/api/live/matches/{match_id}/events", json={"events": batch})
            assert response.status_code == 200
            sent += response.json()["accepted"]
        assert sent == len(source_events)

        live = client.get(f"/api/live/matches/{match_id}/insight", params={
            "language": "en", "mode": "fan", "team": MATCH.away_team, "player": "r-11",
        })
        demo = client.get("/api/insight", params={
            "minute": 57, "second": 20, "language": "en", "mode": "fan",
            "team": MATCH.away_team, "player": "r-11",
        })
        assert live.status_code == demo.status_code == 200

    live_card = live.json()["overlay"]
    demo_card = demo.json()["overlay"]
    assert live.json()["source"] == "synthetic"
    assert live_card["verified"] is True
    assert {key: live_card[key] for key in ("kind", "headline", "body", "evidence", "team_id")} == {
        key: demo_card[key] for key in ("kind", "headline", "body", "evidence", "team_id")
    }
    sessions.pop(match_id, None)
