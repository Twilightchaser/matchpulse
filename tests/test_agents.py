"""Agent workflow, Evidence Guardian, audience cards and recap."""

from __future__ import annotations

from app.agents import (
    AudienceProfile,
    EvidenceGuardianAgent,
    InsightWorkflow,
    MomentExplainerAgent,
    SharedMatchState,
    TacticalAnalystAgent,
)


def goal_candidate(snapshot):
    candidate = TacticalAnalystAgent().candidates(snapshot)[0]
    assert candidate.kind == "goal" and candidate.trigger_event_id == "evt-0329"
    return candidate


# ---------------------------------------------------------------- Evidence Guardian

def test_player_name_digits_do_not_trip_the_guardian(snap):
    s = snap(60)
    c = goal_candidate(s)
    c.explanation = MomentExplainerAgent.explain(c)
    assert "N. Player 10" in c.explanation
    ok, reason = EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))
    assert ok, reason


def test_guardian_rejects_invented_numbers_with_a_precise_reason(snap):
    s = snap(60)
    c = goal_candidate(s)
    c.explanation = "N. Player 10 scored from a 0.87 xG chance in the 64th minute."
    ok, reason = EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))
    assert not ok and "0.87" in reason and "64" in reason


def test_guardian_rejects_players_outside_cited_events(snap):
    s = snap(60)
    c = goal_candidate(s)
    c.explanation = "N. Player 4 scored at 59:14 from a 0.24 xG chance."
    ok, reason = EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))
    assert not ok and "N. Player 4" in reason
    c.explanation = "Player 9 scored at 59:14 from a 0.24 xG chance."  # shorthand for the cited scorer
    assert EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))[0]


def test_guardian_checks_unique_surname_without_full_name(snap):
    s = snap(60).model_copy(deep=True)
    c = goal_candidate(s)
    cited_ids = {event_id for evidence in c.evidence for event_id in evidence.event_ids}
    uncited = next(event for event in s.event_index.values()
                   if event.player_name == "N. Player 4" and event.event_id not in cited_ids)
    uncited.player_name = "Kofi Aderin"
    c.explanation = "Aderin created the decisive chance."
    ok, reason = EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))
    assert not ok and "Kofi Aderin" in reason


def test_guardian_accepts_old_evidence_and_rejects_future_ids(snap):
    s = snap(60)
    c = goal_candidate(s)
    c.explanation = MomentExplainerAgent.explain(c)
    assert any("evt-0108" in item.event_ids for item in c.evidence)  # 18:27 goal in the score evidence
    assert EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))[0]
    c.evidence[0].event_ids.append("evt-0400")
    ok, reason = EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))
    assert not ok and "evt-0400" in reason


def test_guardian_rejects_wrong_scorelines(snap):
    s = snap(60)
    c = goal_candidate(s)
    c.explanation = "N. Player 10 scored at 59:14 to make it 3-1."
    ok, reason = EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))
    assert not ok and "3-1" in reason
    c.explanation = "N. Player 10 scored at 59:14 to make it 2-1."
    assert EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))[0]


def test_percentage_phrasing_of_xg_is_allowed(snap):
    s = snap(60)
    c = goal_candidate(s)
    c.explanation = "N. Player 10 converted a chance that scores 24% of the time."
    assert EvidenceGuardianAgent().run(c, SharedMatchState(snapshot=s))[0]


def test_every_local_insight_in_the_match_verifies(snap):
    for stateful in (True, False):
        wf = InsightWorkflow(stateful=stateful)
        for minute in range(1, 91):
            result = wf.analyze(snap(minute))
            if result.candidate:
                assert result.verified, (minute, result.candidate.explanation)


# ---------------------------------------------------------------- workflow + LLM

def test_lying_llm_is_rejected_then_corrected(snap, fake):
    lie = "N. Player 10 scored from a 0.87 xG chance."
    service = fake(explanations=[lie, MomentExplainerAgent.explain])
    wf = InsightWorkflow(narrative_service=service, stateful=False)
    result = wf.analyze(snap(60))
    assert result.verified and result.provider == "foundry"
    statuses = [(t.agent, t.status) for t in result.traces]
    assert ("Evidence Guardian", "rejected") in statuses
    assert ("Moment Explainer", "revised") in statuses
    assert statuses[-1] == ("Evidence Guardian", "completed")
    assert service.explain_calls[0][1] is None
    assert "0.87" in service.explain_calls[1][1]  # the Guardian's exact complaint is sent back


def test_persistent_lies_fall_back_to_deterministic_copy(snap, fake):
    service = fake(explanations=["N. Player 4 scored from a 0.99 xG chance."])
    result = InsightWorkflow(narrative_service=service, stateful=False).analyze(snap(60))
    assert len(service.explain_calls) == 3
    assert result.verified and result.provider == "local"
    assert any(t.status == "fallback" for t in result.traces)
    assert result.candidate.explanation == MomentExplainerAgent.explain(result.candidate)


def test_llm_outage_degrades_gracefully(snap, fake):
    service = fake(explanations=["unused"], explain_error=TimeoutError(), localized=RuntimeError("down"))
    wf = InsightWorkflow(narrative_service=service, stateful=False)
    result = wf.analyze(snap(60))
    assert result.verified and result.provider == "local"
    card, traces = wf.present(result, snap(60), AudienceProfile(language="zh"))
    assert card.provider == "local" and card.verified
    assert any(t.status == "fallback" and t.agent == "Audience Director" for t in traces)


def test_localized_copy_is_fact_checked(snap, fake):
    service = fake(explanations=[MomentExplainerAgent.explain],
                   localized={"headline": "N. Player 10 scores", "body": "A 0.91 xG finish.", "eyebrow": "GOAL"})
    wf = InsightWorkflow(narrative_service=service, stateful=False)
    card, traces = wf.run(snap(60), AudienceProfile(mode="analyst"))
    assert card.provider == "local" and "0.91" not in card.body
    assert any(t.status == "rejected" and "0.91" in t.summary for t in traces)

    service.localized = {"headline": "N. Player 10 为 Northbridge FC 破门", "body": "0.24 xG 的机会，比分 2-1。",
                         "eyebrow": "进球"}
    card, _ = InsightWorkflow(narrative_service=service, stateful=False).run(snap(60), AudienceProfile(language="zh"))
    assert card.provider == "foundry" and card.headline.startswith("N. Player 10")


def test_live_replay_announces_each_goal_once(snap):
    wf = InsightWorkflow(stateful=True)
    goal_cards = 0
    for minute in range(55, 66):
        result = wf.analyze(snap(minute))
        goal_cards += bool(result.candidate and result.candidate.kind == "goal")
    assert goal_cards == 1


def test_state_insights_respect_cooldown(snap):
    wf = InsightWorkflow(stateful=True)
    shown = []
    for minute in range(1, 91):
        result = wf.analyze(snap(minute))
        if result.candidate and result.candidate.kind in {"control", "momentum_shift", "high_pressure"}:
            shown.append((result.candidate.team_id, result.candidate.kind, minute))
    for i, (team, kind, minute) in enumerate(shown):
        for team2, kind2, minute2 in shown[i + 1:]:
            if (team, kind) == (team2, kind2):
                assert minute2 - minute >= 6


def test_scrubbing_is_stateless(snap):
    wf = InsightWorkflow(stateful=False)
    first = wf.analyze(snap(60)).candidate
    wf.analyze(snap(80))
    again = InsightWorkflow(stateful=False).analyze(snap(60)).candidate
    assert first.kind == again.kind == "goal"


# ---------------------------------------------------------------- audience cards

def test_cards_per_profile(snap, match):
    wf = InsightWorkflow(stateful=False)
    s = snap(60)
    fan, _ = wf.run(s, AudienceProfile())
    analyst, _ = wf.run(s, AudienceProfile(mode="analyst"))
    zh, _ = wf.run(s, AudienceProfile(language="zh"))
    assert fan.body != analyst.body and len(fan.evidence) < len(analyst.evidence)
    assert "破门" in zh.headline and "scores" not in zh.headline
    assert fan.display_at_second == 59 * 60 + 14 and fan.kind == "goal"
    focus, _ = wf.run(s, AudienceProfile(mode="analyst", favorite_player="n-10", favorite_team=match.home_team))
    assert focus.eyebrow.startswith("YOUR TEAM · PLAYER FOCUS")
    assert focus.evidence[-1].label == "Player focus" and "N. Player 10" in focus.evidence[-1].value
    by_name, _ = wf.run(s, AudienceProfile(favorite_player="N. Player 10"))
    assert "PLAYER FOCUS" in by_name.eyebrow


def test_state_cards_sync_to_now(snap):
    wf = InsightWorkflow(stateful=True)
    for minute in range(55, 62):
        s = snap(minute)
        card, _ = wf.run(s, AudienceProfile())
        if card and card.kind in {"control", "momentum_shift", "high_pressure"}:
            assert card.display_at_second == s.match_second


# ---------------------------------------------------------------- recap

def test_recap_local_and_llm_validation(snap, events, fake):
    full = snap(90)
    result = InsightWorkflow().recap(events, full)
    assert result["summary_provider"] == "local" and "[evt-0329]" in result["summary"]
    agent = InsightWorkflow().recap_agent
    assert agent.validate(result["summary"], result, full)[0]

    good = "Northbridge FC came from behind to win 2-1 [evt-0108] [evt-0329]. N. Player 10 struck the winner at 59:14 [evt-0329]."
    ok_result = InsightWorkflow(narrative_service=fake(recap=good)).recap(events, full)
    assert ok_result["summary_provider"] == "foundry"

    for bad, needle in [
        ("Northbridge FC won 3-1 [evt-0329].", "3-1"),
        ("N. Player 4 scored the winner [evt-0329].", "N. Player 4"),
        ("Northbridge FC won 2-1 [evt-9999].", "evt-9999"),
        ("Northbridge FC won 2-1.", "no event ids"),
    ]:
        rejected = InsightWorkflow(narrative_service=fake(recap=bad)).recap(events, full)
        assert rejected["summary_provider"] == "local" and needle in rejected["summary_fallback"]
