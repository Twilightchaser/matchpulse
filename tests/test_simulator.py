from app.models import EventType
from app.simulator import SyntheticMatch
from app.metrics import MetricsEngine


def test_simulation_is_deterministic() -> None:
    first = SyntheticMatch(seed=21).generate(minutes=10)
    second = SyntheticMatch(seed=21).generate(minutes=10)
    assert first == second
    assert len(first) > 20


def test_simulation_contains_core_event_types() -> None:
    events = SyntheticMatch(seed=21).generate()
    event_types = {event.event_type for event in events}
    assert {EventType.PASS, EventType.SHOT, EventType.PRESSURE, EventType.POSSESSION_CHANGE}.issubset(event_types)
    assert all(event.match_id == "synthetic-001" for event in events)


def test_match_has_22_players_and_continuous_position_frames() -> None:
    simulator = SyntheticMatch(seed=21)
    lineup = simulator.lineup()
    events = simulator.generate(minutes=10)
    frames = simulator.player_frames(events)
    assert len(lineup) == 22
    assert len({player["player_id"] for player in lineup}) == 22
    assert all(len(frame["positions"]) == 22 for frame in frames)
    assert frames[0]["second"] == 0
    assert frames[-1]["second"] > frames[0]["second"]
    assert any(
        first[0] != second[0] or first[1] != second[1]
        for first, second in zip(frames[0]["positions"], frames[-1]["positions"])
    )
    lineup_names = {player["player_id"]: player["player_name"] for player in lineup}
    assert all(lineup_names[event.player_id] == event.player_name for event in events)


def test_snapshot_can_stop_inside_a_minute() -> None:
    events = SyntheticMatch(seed=21).generate(minutes=4)
    snapshot = MetricsEngine().snapshot(events, 2, 15)
    assert all(event.minute < 2 or event.minute == 2 and event.second <= 15 for event in snapshot.recent_events)


def test_pass_reception_and_ball_ownership() -> None:
    """施压保持现有球权；完成的传球以实际接球者记录结果。"""
    match = SyntheticMatch()
    events = match.generate()
    holder = None
    for event in events:
        if event.event_type == EventType.PRESSURE:
            assert event.possession_team_id == holder
        holder = event.possession_team_id
        if event.event_type == EventType.PASS and event.outcome == "success":
            assert event.attributes["actual_receiver_id"][0] == event.player_id[0]
            assert event.attributes["receiver_team_id"] == event.team_id
    frames = match.player_frames(events)
    assert all(a["second"] < b["second"] for a, b in zip(frames, frames[1:]))

