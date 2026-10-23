from fastapi.testclient import TestClient
import pytest
from app.main import app
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
