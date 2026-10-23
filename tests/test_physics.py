"""连续比赛的行为验收，直接检查真实运行时轨迹与规则。"""

import math
from collections import Counter

import pytest

from app.physics import MatchPhysics
from app.simulator import SyntheticMatch, shot_xg
from app.models import EventType


@pytest.fixture(scope="module")
def game():
    match = SyntheticMatch()
    return match.generate(), match.player_frames([])


def test_all_players_move_without_teleporting(game):
    _, frames = game
    moved = [0.] * 22
    for first, second in zip(frames, frames[1:]):
        assert len(second["positions"]) == 22
        assert second["second"]-first["second"] == 0.5
        for i, (a, b) in enumerate(zip(first["positions"], second["positions"])):
            step = math.dist(a, b)
            assert step <= 6.5*0.5+0.03
            moved[i] += step
    assert min(moved) > 100
    counts = [sum(math.dist(a, b) > .15 for a, b in zip(frames[i]["positions"], frames[i+2]["positions"]))
              for i in range(20, 220, 2)]
    assert sum(counts)/len(counts) > 15


def test_pass_types_and_restarts_are_present(game):
    events, frames = game
    assert {e.attributes["pass_type"] for e in events if e.event_type == EventType.PASS} == {"short", "long", "through", "lofted"}
    assert {EventType.CORNER, EventType.THROW_IN, EventType.OFFSIDE, EventType.GOAL_KICK}.issubset({e.event_type for e in events})
    assert any(f["ball"]["z"] > 3 for f in frames)
    assert sum(frames[-1]["score"]) == sum(e.outcome == "goal" for e in events)
    assert 10 <= sum(e.event_type == EventType.SHOT for e in events) <= 50


def test_owner_stays_at_ball_and_receivers_are_teammates(game):
    events, frames = game
    lineup = SyntheticMatch().lineup()
    index = {p["player_id"]: i for i, p in enumerate(lineup)}
    for f in frames:
        if f["owner"]:
            assert math.dist(f["positions"][index[f["owner"]]], (f["ball"]["x"], f["ball"]["y"])) < 0.03
    for e in events:
        if e.event_type == EventType.PASS:
            assert e.attributes["receiver_id"] != e.player_id
            assert e.attributes["receiver_team_id"] == e.team_id


def test_restart_waits_for_taker_without_teleport():
    physics = MatchPhysics(SyntheticMatch().lineup(), 38, "test", shot_xg)
    before = list(physics.pos)
    physics.restart(EventType.THROW_IN, 1, (40., 0.))
    assert physics.pos == before
    assert physics.owner is None and physics.dead["team"] == 1
    assert physics.dead["kind"] == EventType.THROW_IN


@pytest.mark.parametrize("team", [0, 1])
def test_close_range_chance_ignores_previous_team_shot(team):
    physics = MatchPhysics(SyntheticMatch().lineup(), 38, "test", shot_xg)
    physics.dead = None
    physics.team = team
    physics.owner = team*11+9
    physics.ball = physics.local((98., 34.))
    physics.last_shot[team] = physics.t
    assert physics.wants_shot()
    physics.ball = physics.local((99., 3.))
    assert not physics.wants_shot()


@pytest.mark.parametrize("team,side", [(0, 0.), (0, 68.), (1, 0.), (1, 68.)])
def test_corner_waits_for_box_and_delivers_into_it(team, side):
    physics = MatchPhysics(SyntheticMatch().lineup(), 38, "test", shot_xg)
    physics.team = team
    physics.restart(EventType.CORNER, team, physics.local((105., side)))
    before = list(physics.pos)
    physics.plan()
    physics.t = physics.dead["ready"]
    assert not physics.restart_ready()
    assert physics.pos == before
    # Advance actual players until the set-piece shape is ready; no teleport.
    for _ in range(240):
        physics.move()
        physics.t += .25
        if physics.restart_ready():
            break
    assert physics.restart_ready()
    attackers = [physics.local(physics.pos[i]) for i in range(team*11+1, team*11+11)
                 if i != physics.dead["taker"]]
    assert sum(x > 88. and 22. < y < 46. for x,y in attackers) >= 5
    physics.owner = physics.dead["taker"]
    physics.ball = physics.pos[physics.owner]
    assert physics.pass_ball(EventType.CORNER)
    x,y = physics.local(physics.flight["end"])
    assert x > 85. and 20. < y < 48.
    assert physics.flight["style"] == "lofted"
