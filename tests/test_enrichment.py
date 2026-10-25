from fastapi.testclient import TestClient
from app.models import MatchEvent, EventType
from app.metrics import MetricsEngine
from app.agents import InsightWorkflow, AudienceProfile, MomentExplainerAgent, EvidenceGuardianAgent, SharedMatchState
from app.enrichment import extra_candidates, player_candidate


def event(seq, second, kind="pass", team="Home", player="p1", outcome="success", **attrs):
    return MatchEvent(event_id=f"evt-{seq:04d}", match_id="test", sequence=seq,
        minute=second//60, second=second%60, event_type=kind,team_id=team,
        player_id=player,player_name="Alex" if player=="p1" else "Ben",x=80,y=34,
        outcome=outcome,attributes=attrs,possession_team_id=team)


def snapshot(events, second):
    return MetricsEngine().snapshot(events,second//60,second%60,home_team="Home",away_team="Away")


def verify(candidate,snap):
    candidate.explanation=MomentExplainerAgent.explain(candidate)
    assert EvidenceGuardianAgent().run(candidate,SharedMatchState(snap))[0]


def test_first_target_and_brace_and_shot_ten_have_real_citations():
    events=[event(1,10,"shot",outcome="saved",xg=.2),event(2,20,"shot",outcome="goal",xg=.2),event(3,30,"shot",outcome="goal",xg=.2)]
    for time in (10,30):
        snap=snapshot(events,time)
        cs=[c for c in extra_candidates(snap) if c.trigger_event_id==events[0 if time==10 else 2].event_id]
        assert cs
        verify(cs[0],snap)
    events=[event(i,i*10,"shot",outcome="failed",xg=.1) for i in range(1,11)]
    snap=snapshot(events,100)
    candidate=next(c for c in extra_candidates(snap) if len(c.evidence[0].event_ids)==10)
    verify(candidate,snap)


def test_player_progressive_pass_generates_own_story_and_viewpoint():
    snap=snapshot([event(1,60,progressive=True,distance_m=24)],60)
    candidate=player_candidate(snap,"p1")
    verify(candidate,snap)
    wf=InsightWorkflow(stateful=False)
    card,_=wf.run(snap,AudienceProfile(favorite_player="p1",favorite_team="Home"))
    assert card.kind=="player_moment" and card.verified
    # The viewer's club and player are tags on the graphic, not boilerplate glued to the copy.
    assert card.eyebrow=="YOUR TEAM · PLAYER FOCUS"
    assert card.body.startswith("Alex") and "Ben" not in card.body


def test_rhythm_uses_same_possession_change_definition_as_metrics():
    events=[event(i,100+i*3,team="Home") for i in range(1,6)]
    events += [event(i+6,310+i*2,team="Away" if i%2==0 else "Home") for i in range(14)]
    snap=snapshot(events,338)
    cs=[c for c in extra_candidates(snap) if c.kind=="rhythm"]
    assert snap.phase=="chaotic" and cs
    verify(cs[0],snap)


def test_dataset_exports_roundtrip_and_schema():
    from app.main import app
    import csv,io,json
    client=TestClient(app)
    data=client.get("/api/dataset").json()
    assert data["synthetic"] and data["events"]
    csv_response=client.get("/api/dataset?format=csv")
    rows=list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(rows)==len(data["events"])
    assert json.loads(rows[0]["attributes"])==data["events"][0]["attributes"]
    assert client.get(data["schema_url"]).json()["units"]["xg"]


def test_overlay_timeline_is_verified_ordered_and_without_model_calls():
    from app.main import build_overlay_timeline
    result=build_overlay_timeline("fan","zh",None,None)
    cards=result["cards"]
    assert result["model_calls"]==0 and cards
    assert all(c["verified"] and c["provider"]=="local" and c["duration_seconds"]>0 for c in cards)
    assert [c["display_at_second"] for c in cards]==sorted(c["display_at_second"] for c in cards)
    assert len({c["insight_id"] for c in cards})==len(cards)
