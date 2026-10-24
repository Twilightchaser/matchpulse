"""Reproducible, football-realistic synthetic event generator.

Model overview
--------------
* The ball has a location in the attacking frame of the team in possession.
* Attacking actions (passes, shots) belong to the team in possession. Defensive actions
  (pressure, tackles, interceptions/recoveries) belong to the defending team and are
  recorded in the defending team's own frame.
* Pass difficulty grows with distance, forward progression and recent pressure; completion
  probability falls with difficulty.
* Shots only happen from advanced positions. Expected goals (xG) come from a logistic model
  on shot distance and goal-mouth angle, and a goal is drawn with probability equal to xG,
  so goals are consistent with chance quality.
* Second half kicks off at 45:00 with the away team; after a goal the conceding team
  restarts from the centre spot.
"""

from __future__ import annotations

import math
import random
from functools import lru_cache
from dataclasses import dataclass

from app.models import EventType, MatchEvent, MatchMetadata

# Seed chosen so the demo match contains goals for both sides across both halves.
DEFAULT_SEED = 15  # demo story: Northbridge lead 17', Riverside equalise 43' and win it 57'

PITCH_LENGTH = 105.0
PITCH_WIDTH = 68.0
GOAL_WIDTH = 7.32


@dataclass(frozen=True)
class TeamProfile:
    team_id: str
    players: tuple[str, ...]
    press_intensity: float
    directness: float


TEAMS = (
    TeamProfile("Northbridge FC", (
        "Evan Calder", "Milo Hartwell", "Jonah Mercer", "Amari Voss", "Felix Rowe",
        "Kian Sorrell", "Tariq Fenwick", "Dario Vale", "Theo Marsh", "Luca Wren",
        "Sami Ellery",
    ), 0.76, 0.62),
    TeamProfile("Riverside Athletic", (
        "Idris Kellan", "Nico Bellamy", "Kofi Aderin", "Rafael Saye", "Owen Larkmere",
        "Malik Arden", "Emil Kestrel", "Nolan Veycroft", "Hugo Selwyn", "Mateo Arlen",
        "Elias Nouri",
    ), 0.58, 0.48),
)

FORMATION_433 = (
    ("GK", 8, 34), ("LB", 23, 9), ("LCB", 17, 25), ("RCB", 17, 43), ("RB", 23, 59),
    ("DM", 39, 34), ("LCM", 49, 20), ("RCM", 49, 48),
    ("LW", 67, 10), ("ST", 73, 34), ("RW", 67, 58),
)

# Shirt numbers (1-based) most likely to act in each third of the pitch (attacking frame).
ZONE_PLAYERS = {
    "defensive": (2, 3, 4, 5, 6, 6, 7, 8),
    "middle": (2, 5, 6, 7, 7, 8, 8, 9, 11),
    "attacking": (7, 8, 9, 9, 10, 10, 10, 11, 11),
}


def shot_xg(x: float, y: float) -> float:
    """Logistic xG on distance and goal-mouth angle (attacking frame, goal at x=105)."""
    dx = PITCH_LENGTH - x
    dy = y - PITCH_WIDTH / 2
    distance = math.hypot(dx, dy)
    half_goal = GOAL_WIDTH / 2
    angle = math.atan2(GOAL_WIDTH * dx, dx * dx + dy * dy - half_goal * half_goal)
    if angle < 0:
        angle += math.pi
    logit = -0.92 + 1.06 * angle - 0.093 * distance
    return round(min(0.85, max(0.01, 1 / (1 + math.exp(-logit)))), 2)


class SyntheticMatch:
    """连续比赛的公共接口；事件与动画帧来自同一次物理模拟。"""

    def __init__(self, seed: int = DEFAULT_SEED, match_id: str = "synthetic-001") -> None:
        self.seed, self.match_id = seed, match_id

    @property
    def home_team(self):
        return TEAMS[0].team_id

    @property
    def away_team(self):
        return TEAMS[1].team_id

    def metadata(self):
        return MatchMetadata(match_id=self.match_id, home_team=self.home_team,
                             away_team=self.away_team, seed=self.seed)

    def lineup(self):
        return [{"team_id": team.team_id, "player_id": f"{team.team_id[0].lower()}-{i+1}",
                 "player_name": team.players[i], "number": i+1, "role": role,
                 "x": x if side == 0 else 105-x, "y": y if side == 0 else 68-y}
                for side, team in enumerate(TEAMS) for i, (role, x, y) in enumerate(FORMATION_433)]

    @staticmethod
    @lru_cache(maxsize=4)
    def _simulate(seed, match_id, minutes):
        from app.physics import MatchPhysics
        match = SyntheticMatch(seed, match_id)
        return MatchPhysics(match.lineup(), seed, match_id, shot_xg).run(minutes)

    def generate(self, minutes=90):
        return self._simulate(self.seed, self.match_id, minutes)[0]

    def player_frames(self, events):
        # 页面始终播放完整比赛；事件参数保留当前接口调用方式。
        return self._simulate(self.seed, self.match_id, 90)[1]

    def player_positions(self, events, upto_second):
        frames = self.player_frames(events)
        frame = frames[min(len(frames)-1, max(0, int(upto_second*2)))]
        return [dict(p, x=xy[0], y=xy[1]) for p, xy in zip(self.lineup(), frame["positions"])]

    def player_metrics(self, events):
        frames = self.player_frames(events)
        result = {p["player_id"]: {"player_name": p["player_name"], "team_id": p["team_id"],
                  "distance_km": 0., "peak_speed_kmh": 0., "events": 0,
                  "measure": "synthetic continuous tracking"} for p in self.lineup()}
        for a, b in zip(frames, frames[1:]):
            for p, x, y in zip(self.lineup(), a["positions"], b["positions"]):
                row = result[p["player_id"]]
                movement = math.dist(x, y)
                row["distance_km"] += movement/1000
                row["peak_speed_kmh"] = max(row["peak_speed_kmh"], movement/(b["second"]-a["second"])*3.6)
        for e in events:
            result[e.player_id]["events"] += 1
        return result

