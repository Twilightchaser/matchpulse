"""固定战术场景验证，避免依赖整场随机比分。"""
import pytest

from app.tactics import Player, advance, attacking_targets, choose_pass, defending_targets, distance, pass_score


def p(name, x, y, role="CM"):
    return Player(name, role, (x, y), (x, y))


def test_blocked_lane_loses_to_open_lane():
    carrier = p("a", 40, 34)
    blocked, opened = p("b", 65, 34), p("c", 48, 15)
    defenders = [p("d", 52, 34)]
    assert pass_score(carrier.position, blocked.position, defenders)["lane_risk"] > 0.9
    assert choose_pass(carrier, [carrier, blocked, opened], defenders)[0] == opened


def test_no_safe_pass_and_no_self_pass():
    carrier = p("a", 40, 34)
    assert choose_pass(carrier, [carrier], []) is None
    assert choose_pass(carrier, [carrier, p("b", 65, 34)], [p("d", 52, 34)]) is None


def test_single_presser_unique_marking_and_keeper():
    defenders = [p("d1", 55, 30), p("d2", 70, 20), p("d3", 70, 48), p("g", 100, 34, "GK")]
    attackers = [p("a", 50, 30), p("b", 72, 20), p("c", 75, 48)]
    targets = defending_targets((50, 30), defenders, attackers, "a")
    assert sum(t["task"] == "press" for t in targets.values()) == 1
    marks = [t["mark"] for t in targets.values() if t["task"] == "screen"]
    assert len(marks) == len(set(marks)) == 2
    assert targets["g"]["task"] == "goalkeeper"


def test_elastic_shape_and_speed_limit():
    team = [p("a", 40, 34), p("b", 65, 12), p("c", 65, 56)]
    first = attacking_targets((40, 34), team, [], "a")
    second = attacking_targets((70, 45), team, [], "a")
    assert first["b"] != second["b"]
    assert distance((40, 34), advance((40, 34), (90, 60), 0.1)) <= 0.60001
    with pytest.raises(ValueError):
        advance((40, 34), (50, 34), -1)


def test_defender_stays_goal_side_of_penalty_area_runner():
    defenders=[p("d1",85,20),p("d2",40,34),p("g",100,34,"GK")]
    attackers=[p("a",82,20),p("b",90,34)]
    targets=defending_targets((82,20),defenders,attackers,"a")
    marker=next(t for t in targets.values() if t["task"]=="screen")
    assert marker["target"][0]>=90.8
