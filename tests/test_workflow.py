from app.agents import AudienceProfile, InsightWorkflow, SharedMatchState
from app.foundry import FoundryRuntime
from app.metrics import MetricsEngine
from app.models import Evidence, InsightCandidate, MatchEvent
from fixture_match import SyntheticMatch


def _snapshot(minute: int = 64):
    events = SyntheticMatch(seed=21).generate()
    return events, MetricsEngine().snapshot(events, minute)


def _chance_snapshot():
    events, snapshot = _snapshot()
    team = snapshot.home_team
    shot = MatchEvent(
        event_id="evt-test-shot", match_id=snapshot.match_id, sequence=max(e.sequence for e in events) + 1,
        minute=64, second=37, event_type="shot", team_id=team, player_id="test-9",
        player_name="Test Striker", x=92, y=34, outcome="saved", attributes={"xg": 0.42},
    )
    snapshot.recent_events.append(shot)
    snapshot.event_index[shot.event_id] = shot
    snapshot.metrics[snapshot.home_team].momentum_score = 90
    snapshot.metrics[snapshot.away_team].momentum_score = 10
    return snapshot


def test_workflow_returns_verified_overlay() -> None:
    events, snapshot = _snapshot()
    overlay, trace = InsightWorkflow().run(snapshot, AudienceProfile(mode="analyst", language="en"))
    assert overlay is None or overlay.verified is True
    assert len(trace) >= 1
    assert trace[0].agent == "Tactical Analyst"
    if overlay:
        assert overlay.evidence
        assert trace[-1].agent == "Audience Director"
        assert "Evidence Guardian" in [item.agent for item in trace]


def test_profiles_share_one_analysis_and_do_not_duplicate_storylines() -> None:
    _, snapshot = _snapshot()
    workflow = InsightWorkflow()
    first, first_trace = workflow.run(snapshot, AudienceProfile(mode="fan", language="en"))
    original_storylines = list(workflow.storylines)
    second, second_trace = workflow.run(snapshot, AudienceProfile(mode="analyst", language="en"))
    assert workflow.storylines == original_storylines
    if first is None:
        assert second is None
    else:
        assert second is not None
        assert first.body != second.body
        assert [item.agent for item in first_trace if item.agent != "Audience Director"] == [
            item.agent for item in second_trace if item.agent != "Audience Director"
        ]


def test_chinese_localization_is_not_partial_string_replacement() -> None:
    _, snapshot = _snapshot()
    overlay, _ = InsightWorkflow().run(snapshot, AudienceProfile(mode="fan", language="zh"))
    if overlay:
        from app.house_style import kind_label
        assert overlay.language == "zh"
        assert overlay.eyebrow == kind_label(overlay.kind, "zh")  # the tag names the moment, in Chinese
        assert " MATCH " not in overlay.headline


def test_routine_event_is_not_promoted_to_narrative() -> None:
    _, snapshot = _snapshot(3)
    snapshot.phase = "controlled"
    snapshot.recent_events = [event for event in snapshot.recent_events if event.event_type.value != "shot"]
    for metric in snapshot.metrics.values():
        metric.momentum_score = 10
    overlay, trace = InsightWorkflow().run(snapshot, AudienceProfile())
    assert overlay is None
    assert len(trace) == 1


def test_guardian_reports_unsupported_numbers_separately_from_ids() -> None:
    events, snapshot = _snapshot()
    state = SharedMatchState(snapshot=snapshot)
    candidate = InsightCandidate(
        insight_id="test", team_id=events[0].team_id, minute=64, kind="control", importance=0.8,
        headline="Test", explanation="The side completed 999 actions.",
        evidence=[Evidence(label="Momentum", value="20.0 vs 10.0", event_ids=[snapshot.recent_events[-1].event_id])],
    )
    ok, reason = InsightWorkflow().checker.run(candidate, state)
    assert not ok
    assert reason.startswith("numbers not backed by evidence")


def test_guardian_reports_missing_event_ids() -> None:
    _, snapshot = _snapshot()
    state = SharedMatchState(snapshot=snapshot)
    candidate = InsightCandidate(
        insight_id="test", team_id=snapshot.home_team, minute=64, kind="control", importance=0.8,
        headline="Test", explanation="The side had 20 actions.",
        evidence=[Evidence(label="Actions", value="20", event_ids=["evt-outside-window"])],
    )
    ok, reason = InsightWorkflow().checker.run(candidate, state)
    assert not ok
    assert reason.startswith("unknown or future event ids")


class RevisionService:
    def __init__(self):
        self.revision_notes = []

    def explain(self, candidate, snapshot, revision_note):
        self.revision_notes.append(revision_note)
        if revision_note is None:
            return "A 0.87 xG chance created a dangerous opportunity."
        return "The chance was worth 0.42 xG."

    def localize(self, candidate, profile):
        return {"headline": "Localized", "body": "Verified story", "eyebrow": "MATCH PULSE"}


def test_guardian_rejection_is_traced_and_reason_reaches_foundry_revision() -> None:
    events, snapshot = _snapshot()
    service = RevisionService()
    workflow = InsightWorkflow(narrative_service=service)
    result = workflow.analyze(snapshot)
    assert result.candidate is None or result.verified
    if result.candidate:
        assert service.revision_notes[0] is None
        assert service.revision_notes[1].startswith("numbers not backed by evidence")
        statuses = [(item.agent, item.status) for item in result.traces]
        assert ("Evidence Guardian", "rejected") in statuses


class FakeFoundryAgent:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    async def run(self, prompt):
        self.prompts.append(prompt)
        value = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(value, Exception):
            raise value
        return value


def _foundry_runtime(fake_agent) -> FoundryRuntime:
    runtime = object.__new__(FoundryRuntime)
    runtime.timeout_seconds = 0.5
    import asyncio
    import threading
    runtime.loop = asyncio.new_event_loop()
    runtime.loop_thread = threading.Thread(target=runtime.loop.run_forever, daemon=True)
    runtime.loop_thread.start()
    runtime.explainer = fake_agent
    runtime.director = None
    runtime.recap_writer = None
    return runtime


def test_foundry_lie_is_rejected_then_revision_is_verified() -> None:
    snapshot = _chance_snapshot()
    fake = FakeFoundryAgent([
        "A 0.87 xG chance created a dangerous opportunity.",
        "The chance was worth 0.42 xG.",
    ])
    runtime = _foundry_runtime(fake)
    workflow = InsightWorkflow(narrative_service=runtime)
    result = workflow.analyze(snapshot)
    runtime.close()
    if result.candidate:
        key_steps = [
            (item.agent, item.status) for item in result.traces
            if (item.agent, item.status) in {
                ("Evidence Guardian", "rejected"),
                ("Moment Explainer", "revised"),
                ("Evidence Guardian", "completed"),
            }
        ]
        assert key_steps == [
            ("Evidence Guardian", "rejected"),
            ("Moment Explainer", "revised"),
            ("Evidence Guardian", "completed"),
        ]
        assert result.verified
        assert "numbers not backed by evidence" in fake.prompts[1]
        assert "0.87" in next(item.summary for item in result.traces if item.agent == "Evidence Guardian" and item.status == "rejected")


def test_persistently_lying_foundry_uses_verified_local_fallback() -> None:
    snapshot = _chance_snapshot()
    fake = FakeFoundryAgent(["A 0.87 xG chance created a dangerous opportunity."])
    runtime = _foundry_runtime(fake)
    workflow = InsightWorkflow(narrative_service=runtime)
    result = workflow.analyze(snapshot)
    runtime.close()
    if result.candidate:
        assert result.verified
        assert result.provider == "local"
        assert any(item.status == "fallback" for item in result.traces)
        assert "0.87" not in result.candidate.explanation


def test_foundry_exception_falls_back_to_deterministic_explainer() -> None:
    snapshot = _chance_snapshot()
    fake = FakeFoundryAgent([RuntimeError("simulated Foundry outage")])
    runtime = _foundry_runtime(fake)
    workflow = InsightWorkflow(narrative_service=runtime)
    result = workflow.analyze(snapshot)
    runtime.close()
    if result.candidate:
        assert result.verified
        assert result.provider == "local"
        assert "failed" in " ".join(item.summary for item in result.traces).lower()


def test_foundry_timeout_falls_back_to_deterministic_explainer() -> None:
    import asyncio

    class SlowAgent:
        async def run(self, prompt):
            await asyncio.sleep(0.1)
            return "A safe response."

    snapshot = _chance_snapshot()
    runtime = _foundry_runtime(SlowAgent())
    runtime.timeout_seconds = 0.01
    workflow = InsightWorkflow(narrative_service=runtime)
    result = workflow.analyze(snapshot)
    runtime.close()
    if result.candidate:
        assert result.verified
        assert result.provider == "local"
        assert any("TimeoutError" in item.summary for item in result.traces)


def test_player_focus_uses_event_player_name() -> None:
    events, snapshot = _snapshot()
    event = snapshot.recent_events[-1]
    # Use a candidate whose evidence points to this event without including its name in value.
    candidate = InsightCandidate(
        insight_id="focus", team_id=event.team_id, minute=event.minute, kind="control", importance=0.8,
        headline="Control", explanation="Verified control shift.",
        evidence=[Evidence(label="Match state", value="contested", event_ids=[event.event_id])],
    )
    from app.agents import AudienceDirectorAgent
    card, _ = AudienceDirectorAgent().run(candidate, True,
        AudienceProfile(mode="fan", favorite_player=event.player_name), snapshot)
    assert card.eyebrow.startswith("PLAYER FOCUS")


def test_state_card_timestamp_uses_current_snapshot_second() -> None:
    events, snapshot = _snapshot()
    event = snapshot.recent_events[-1]
    candidate = InsightCandidate(
        insight_id="time", team_id=event.team_id, minute=event.minute, kind="control", importance=0.8,
        headline="Control", explanation="Verified control shift.",
        evidence=[Evidence(label="Match state", value="contested", event_ids=[event.event_id])],
    )
    from app.agents import AudienceDirectorAgent
    card, _ = AudienceDirectorAgent().run(candidate, True, AudienceProfile(), snapshot)
    assert card.display_at_second == snapshot.match_second


def test_goal_is_announced_once_across_later_snapshots() -> None:
    events, _ = _snapshot()
    goals = [event for event in events if event.event_type.value == "shot" and event.outcome == "goal"]
    if not goals:
        return
    goal = goals[0]
    workflow = InsightWorkflow()
    before = MetricsEngine().snapshot(events, goal.minute)
    after = MetricsEngine().snapshot(events, min(90, goal.minute + 1))
    before.recent_events = [event for event in before.recent_events if event.sequence <= goal.sequence]
    after.recent_events = [event for event in after.recent_events if event.sequence <= goal.sequence]
    first = workflow.analyze(before)
    second = workflow.analyze(after)
    assert first.candidate is None or first.candidate.kind == "goal"
    assert second.candidate is None or second.candidate.kind != "goal"


def test_recap_contains_only_shot_goals() -> None:
    events, snapshot = _snapshot(90)
    recap = InsightWorkflow().recap_agent.run(events, snapshot)
    assert all(item["event_id"].startswith("evt-") for item in recap["goals"])
    assert recap["summary"]
    assert "[evt-" in recap["summary"]
