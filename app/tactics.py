"""战术决策核心：统一使用进攻方从左向右的米制坐标。"""

from dataclasses import dataclass
from math import hypot

Point = tuple[float, float]


@dataclass(frozen=True)
class Player:
    player_id: str
    role: str
    position: Point
    anchor: Point


def distance(a: Point, b: Point) -> float:
    return hypot(a[0] - b[0], a[1] - b[1])


def clamp(p: Point) -> Point:
    return max(1., min(104., p[0])), max(1., min(67., p[1]))


def lane_risk(ball: Point, target: Point, defenders: list[Player]) -> float:
    """比较球与防守者到达线路投影点的时间，返回最危险拦截概率代理值。"""
    dx, dy = target[0] - ball[0], target[1] - ball[1]
    length2 = dx * dx + dy * dy
    if length2 < 1:
        return 1.
    risk = 0.
    for defender in defenders:
        px, py = defender.position
        t = ((px - ball[0]) * dx + (py - ball[1]) * dy) / length2
        if not 0.05 < t < 1.05:
            continue
        t = min(1., t)
        intercept = ball[0] + t * dx, ball[1] + t * dy
        ball_time = distance(ball, intercept) / 15.
        defender_time = 0.25 + max(0., distance(defender.position, intercept) - 0.8) / 6.
        risk = max(risk, max(0., min(1., 0.5 + (ball_time - defender_time) / 0.8)))
    return risk


def pass_score(ball: Point, target: Point, defenders: list[Player]) -> dict[str, float]:
    """各项保留用于调试；权重属于演示启发式参数，尚未做比赛数据标定。"""
    length = distance(ball, target)
    space = min((distance(target, d.position) for d in defenders), default=12.)
    risk = lane_risk(ball, target, defenders)
    progression = max(-1., min(1., (target[0] - ball[0]) / 25.))
    score = 1.8 * min(space / 10., 1.) + progression - 0.025 * length - 3.5 * risk
    if length < 3 or length > 45:
        score -= 4.
    return {"score": score, "lane_risk": risk, "space_m": space, "distance_m": length}


def choose_pass(carrier: Player, teammates: list[Player], defenders: list[Player]):
    """无安全选择时返回 None，调用方进入护球或带球分支。"""
    options = [(p, pass_score(carrier.position, p.position, defenders))
               for p in teammates if p.player_id != carrier.player_id]
    safe = [(p, score) for p, score in options if score["lane_risk"] < 0.65 and score["score"] > -0.5]
    return max(safe, key=lambda item: (item[1]["score"], item[0].player_id), default=None)


def attacking_targets(ball: Point, teammates: list[Player], defenders: list[Player], carrier_id: str) -> dict[str, Point]:
    """弹性阵型加局部空当搜索；顺序占位惩罚避免多人选择同一接应点。"""
    result = {}
    offside_line = sorted((d.position[0] for d in defenders), reverse=True)
    limit = max(ball[0], offside_line[1]) if len(offside_line) > 1 else 104.
    for player in sorted(teammates, key=lambda p: p.player_id):
        if player.player_id == carrier_id:
            result[player.player_id] = ball
            continue
        if player.role == "GK":
            result[player.player_id] = (min(16., max(5., ball[0] * 0.16)), 34. + (ball[1] - 34.) * 0.15)
            continue
        ax, ay = player.anchor
        anchor = clamp((ax + (ball[0] - 52.5) * 0.38, ay + (ball[1] - 34.) * 0.22))
        candidates = [clamp((min(anchor[0] + dx, max(50., limit - 1.)), anchor[1] + dy))
                      for dx in (-6., 0., 6.) for dy in (-6., 0., 6.)]
        def utility(p):
            spacing = sum(max(0., 6. - distance(p, other)) for other in result.values())
            return pass_score(ball, p, defenders)["score"] - 0.06 * distance(p, anchor) - 0.7 * spacing
        result[player.player_id] = max(candidates, key=utility)
    return result


def defending_targets(ball: Point, defenders: list[Player], attackers: list[Player], carrier_id: str):
    """防守球门位于右侧；唯一逼抢者，其余队员一对一封线并保留阵型约束。"""
    field = [p for p in defenders if p.role != "GK"]
    presser = min(field, key=lambda p: (distance(p.position, ball), p.player_id), default=None)
    threats = sorted((a for a in attackers if a.player_id != carrier_id and a.role != "GK"),
                     key=lambda a: (-a.position[0], a.player_id))
    available = {p.player_id: p for p in field if p != presser}
    result = {presser.player_id: {"target": clamp((ball[0] + 1.2, ball[1])), "task": "press", "mark": carrier_id}} if presser else {}
    for attacker in threats:
        if not available:
            break
        defender = min(available.values(), key=lambda d: distance(d.position, attacker.position) + 0.3 * distance(d.anchor, attacker.position))
        del available[defender.player_id]
        x, y = attacker.position
        # 向持球者方向切入传球线，并稍偏向本方球门保护纵深。
        length = max(1., distance(ball, attacker.position))
        cover = (x + 2. * (ball[0] - x) / length + 1., y + 2. * (ball[1] - y) / length)
        target = clamp((cover[0] * 0.8 + defender.anchor[0] * 0.2, cover[1] * 0.8 + defender.anchor[1] * 0.2))
        if x > 60.:
            target = clamp((max(x+0.8,target[0]),target[1]))
        result[defender.player_id] = {"target": target, "task": "screen", "mark": attacker.player_id}
    for p in available.values():
        result[p.player_id] = {"target": clamp(p.anchor), "task": "cover", "mark": None}
    for p in defenders:
        if p.role == "GK":
            result[p.player_id] = {"target": (100., max(28., min(40., ball[1]))), "task": "goalkeeper", "mark": None}
    return result


def advance(position: Point, target: Point, dt: float, speed: float = 6.) -> Point:
    """按秒推进并限制速度；调用方应使用固定时间步长。"""
    if dt < 0 or speed < 0:
        raise ValueError("时间步长和速度必须非负")
    length = distance(position, target)
    fraction = min(1., speed * dt / length) if length else 0.
    return clamp((position[0] + (target[0] - position[0]) * fraction, position[1] + (target[1] - position[1]) * fraction))
