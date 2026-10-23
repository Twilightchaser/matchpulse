# 固定旧赛程仅用于叙事与证据校验回归，运行时不使用。
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
import threading
from dataclasses import dataclass

from app.models import EventType, MatchEvent, MatchMetadata

# Seed chosen so the demo match contains goals for both sides across both halves.
DEFAULT_SEED = 38

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
    TeamProfile("Northbridge FC", tuple(f"N. Player {i}" for i in range(1, 12)), 0.76, 0.62),
    TeamProfile("Riverside Athletic", tuple(f"R. Player {i}" for i in range(1, 12)), 0.58, 0.48),
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
    """Generates events from a fixed seed so tests and demo recordings stay stable."""

    def __init__(self, seed: int = DEFAULT_SEED, match_id: str = "synthetic-001") -> None:
        self.seed = seed
        self.random = random.Random(seed)
        self.match_id = match_id

    @property
    def home_team(self) -> str:
        return TEAMS[0].team_id

    @property
    def away_team(self) -> str:
        return TEAMS[1].team_id

    def metadata(self) -> MatchMetadata:
        return MatchMetadata(match_id=self.match_id, home_team=self.home_team,
                             away_team=self.away_team, seed=self.seed)

    # ------------------------------------------------------------------ generation

    _generation_lock = threading.Lock()

    def generate(self, minutes: int = 90) -> list[MatchEvent]:
        # The generator mutates self.random; concurrent callers (e.g. parallel API requests on a
        # cold cache) would interleave draws and corrupt the match, so generation is serialised.
        with self._generation_lock:
            return self._generate(minutes)

    def _generate(self, minutes: int) -> list[MatchEvent]:
        self.random = random.Random(self.seed)
        # Separate stream for running estimates so they never change the match itself.
        pace_random = random.Random(self.seed + 10_000)
        events: list[MatchEvent] = []
        last_spot: dict[str, tuple[float, float]] = {}
        last_action: dict[str, int] = {}
        possession = 0
        ball_x, ball_y = 52.5, 34.0
        clock = 0
        pressured = False
        second_half_started = False
        kickoff = True
        carrier_number = 10
        receiver_random = random.Random(self.seed + 20_000)

        while True:
            if kickoff:
                gap = 0
            else:
                gap = self.random.randint(6, 15)
            clock += gap
            if not second_half_started and clock >= 45 * 60:
                clock, possession, ball_x, ball_y = 45 * 60, 1, 52.5, 34.0
                second_half_started, kickoff, pressured = True, True, False
                carrier_number = 10
            if clock >= minutes * 60:
                break

            attacker, defender = TEAMS[possession], TEAMS[1 - possession]
            if kickoff:
                kind = EventType.PASS
            elif ball_x >= 76 and self.random.random() < 0.22 * (0.8 + attacker.directness * 0.4):
                kind = EventType.SHOT
            else:
                roll = self.random.random()
                kind = (EventType.PASS if roll < 0.64 else EventType.PRESSURE if roll < 0.76
                        else EventType.TACKLE if roll < 0.85 else EventType.POSSESSION_CHANGE)

            if kind == EventType.PASS:
                actor, x, y, outcome, attrs, new_ball, flips = self._pass(attacker, ball_x, ball_y, pressured, kickoff)
                pressured = False
            elif kind == EventType.SHOT:
                actor, x, y, outcome, attrs, new_ball, flips = self._shot(attacker, ball_x, ball_y)
                pressured = False
            else:
                actor, x, y, outcome, attrs, new_ball, flips = self._defensive(kind, defender, ball_x, ball_y)
                if kind == EventType.PRESSURE and outcome == "success":
                    pressured = True
                if kind == EventType.POSSESSION_CHANGE:
                    attrs["from_team"] = attacker.team_id
                    attrs["to_team"] = defender.team_id
            kickoff = False

            next_possession = 1 - possession if flips else possession
            player_number = self._pick_player(x) if actor == "attacker" or actor == "defender" else 1
            if kind == EventType.SHOT:
                player_number = self.random.choice(ZONE_PLAYERS["attacking"])
            if attrs.pop("_keeper", False):
                player_number = 1
            team = attacker if actor == "attacker" else defender
            if actor == "attacker":
                player_number = carrier_number
            player_id = f"{team.team_id[:1].lower()}-{player_number}"
            if kind == EventType.PASS:
                # 接球队员来自传球队伍；失败传球单独记录拦截者。
                choices = [n for n in ZONE_PLAYERS[
                    "defensive" if attrs["end_x"] < 35 else "middle" if attrs["end_x"] < 70 else "attacking"
                ] if n != player_number]
                receiver_number = receiver_random.choice(choices)
                attrs["receiver_id"] = f"{attacker.team_id[:1].lower()}-{receiver_number}"
                attrs["receiver_team_id"] = attacker.team_id
                if flips:
                    carrier_number = receiver_random.choice(ZONE_PLAYERS["defensive"])
                    attrs["interceptor_id"] = f"{defender.team_id[:1].lower()}-{carrier_number}"
                else:
                    carrier_number = receiver_number
            elif flips:
                carrier_number = 10 if outcome == "goal" else (1 if kind == EventType.SHOT else player_number)
            previous = last_spot.get(player_id, self._base_position(player_number))
            last_spot[player_id] = (x, y)
            # Off-ball running between actions (outfield ~1.6-2.2 m/s, keepers ~1 m/s), at least the
            # straight-line move. Gives realistic ~10 km per 90 minutes for outfield players.
            elapsed = clock - last_action.get(player_id, 0)
            last_action[player_id] = clock
            pace = pace_random.uniform(0.85, 1.15) if player_number == 1 else pace_random.uniform(1.6, 2.2)
            attrs["player_distance_m"] = round(max(math.dist(previous, (x, y)), elapsed * pace), 1)
            attrs["player_speed_kmh"] = self._speed(kind)
            attrs["sprint"] = attrs["player_speed_kmh"] >= 28
            events.append(MatchEvent(
                event_id=f"evt-{len(events) + 1:04d}",
                match_id=self.match_id,
                sequence=len(events) + 1,
                minute=clock // 60,
                second=clock % 60,
                event_type=kind,
                team_id=team.team_id,
                player_id=player_id,
                player_name=team.players[player_number - 1],
                x=x,
                y=y,
                outcome=outcome,
                attributes=attrs,
                possession_team_id=TEAMS[next_possession].team_id,
            ))

            ball_x, ball_y = new_ball
            possession = next_possession
            if outcome == "goal":
                clock += self.random.randint(40, 70)  # celebration and restart
                ball_x, ball_y, kickoff = 52.5, 34.0, True
        return events

    def _pass(self, team: TeamProfile, bx: float, by: float, pressured: bool, kickoff: bool):
        if kickoff:
            dx, dy = -self.random.uniform(6, 14), self.random.uniform(-6, 6)
        else:
            dx = self.random.gauss(4 + 10 * team.directness, 11)
            dy = self.random.gauss(0, 12)
        ex = min(103.0, max(2.0, bx + dx))
        ey = min(66.0, max(2.0, by + dy))
        distance = math.dist((bx, by), (ex, ey))
        difficulty = min(1.0, 0.12 + distance / 75 + max(0.0, ex - bx) / 120 + (0.15 if pressured else 0)
                         + (0.1 if ex > 88 else 0))
        success = kickoff or self.random.random() < 0.99 - 0.45 * difficulty
        attrs = {
            "distance_m": round(distance, 1),
            "difficulty": round(difficulty, 2),
            "progressive": bool(ex - bx >= 12 and ex >= 55),
            "end_x": round(ex, 1),
            "end_y": round(ey, 1),
            "ball_speed_kmh": round(26 + distance * 1.15 + self.random.uniform(-4, 6), 1),
            "under_pressure": pressured,
        }
        if kickoff:
            attrs["kickoff"] = True
        if success:
            return "attacker", round(bx, 1), round(by, 1), "success", attrs, (ex, ey), False
        # Turnover: the opponent picks the ball up where the pass ended (in their frame).
        return "attacker", round(bx, 1), round(by, 1), "failed", attrs, (PITCH_LENGTH - ex, PITCH_WIDTH - ey), True

    def _shot(self, team: TeamProfile, bx: float, by: float):
        x = min(102.0, max(74.0, bx + self.random.uniform(-4, 4)))
        y = min(62.0, max(6.0, by + (34 - by) * self.random.uniform(0.0, 0.3)))
        xg = shot_xg(x, y)
        roll = self.random.random()
        outcome = "goal" if roll < xg else "saved" if roll < xg + (1 - xg) * 0.45 else "blocked"
        attrs = {
            "distance_m": round(math.dist((x, y), (PITCH_LENGTH, PITCH_WIDTH / 2)), 1),
            "xg": xg,
            "shot_speed_kmh": round(self.random.uniform(64, 118), 1),
            "on_target": outcome in {"goal", "saved"},
        }
        # Goal: kickoff handled by the caller. Otherwise keeper/defender collects near own goal.
        new_ball = (52.5, 34.0) if outcome == "goal" else (self.random.uniform(6, 18), self.random.uniform(20, 48))
        return "attacker", round(x, 1), round(y, 1), outcome, attrs, new_ball, True

    def _defensive(self, kind: EventType, defender: TeamProfile, bx: float, by: float):
        # Location in the defender's own frame.
        x = round(min(104.0, max(1.0, PITCH_LENGTH - bx + self.random.uniform(-3, 3))), 1)
        y = round(min(67.0, max(1.0, PITCH_WIDTH - by + self.random.uniform(-3, 3))), 1)
        if kind == EventType.PRESSURE:
            intensity = round(self.random.uniform(defender.press_intensity - 0.15, 1.0), 2)
            outcome = "success" if self.random.random() < intensity * 0.6 else "failed"
            return "defender", x, y, outcome, {"intensity": intensity}, (bx, by), False
        if kind == EventType.TACKLE:
            won = self.random.random() < 0.52 + (defender.press_intensity - 0.6) * 0.3
            if won:
                return "defender", x, y, "success", {}, (x, y), True
            return "defender", x, y, "failed", {}, (min(103.0, bx + self.random.uniform(2, 8)), by), False
        attrs = {"recovery_type": self.random.choice(("interception", "loose_ball", "second_ball"))}
        if x < 10 and abs(y - 34) < 12:
            attrs["_keeper"] = True
        return "defender", x, y, "success", attrs, (x, y), True

    def _pick_player(self, x: float) -> int:
        zone = "defensive" if x < 35 else "middle" if x < 70 else "attacking"
        return self.random.choice(ZONE_PLAYERS[zone])

    def _speed(self, kind: EventType) -> float:
        low, high = {
            EventType.PASS: (6.0, 24.0),
            EventType.SHOT: (14.0, 31.0),
            EventType.PRESSURE: (18.0, 33.0),
            EventType.TACKLE: (12.0, 30.0),
            EventType.POSSESSION_CHANGE: (10.0, 31.0),
        }[kind]
        return round(self.random.uniform(low, high), 1)

    @staticmethod
    def _base_position(number: int) -> tuple[float, float]:
        _, x, y = FORMATION_433[number - 1]
        return float(x), float(y)

    # ------------------------------------------------------------------ views

    def player_metrics(self, events: list[MatchEvent]) -> dict[str, dict[str, float | int | str]]:
        """Estimated distance and peak speed per player from event coordinates."""
        result: dict[str, dict[str, float | int | str]] = {}
        for event in sorted(events, key=lambda item: item.sequence):
            row = result.setdefault(event.player_id, {
                "player_name": event.player_name, "team_id": event.team_id, "distance_km": 0.0,
                "peak_speed_kmh": 0.0, "events": 0, "measure": "synthetic estimate from event coordinates",
            })
            row["distance_km"] = float(row["distance_km"]) + float(event.attributes.get("player_distance_m", 0)) / 1000
            row["peak_speed_kmh"] = max(float(row["peak_speed_kmh"]), float(event.attributes.get("player_speed_kmh", 0)))
            row["events"] = int(row["events"]) + 1
        for row in result.values():
            row["distance_km"] = round(float(row["distance_km"]), 2)
        return result

    def lineup(self) -> list[dict[str, float | str | int]]:
        """Both fictional 4-3-3 line-ups with kick-off positions (home attacks left to right)."""
        lineup: list[dict[str, float | str | int]] = []
        for team_index, team in enumerate(TEAMS):
            for index, (role, x, y) in enumerate(FORMATION_433):
                lineup.append({
                    "team_id": team.team_id,
                    "player_id": f"{team.team_id[:1].lower()}-{index + 1}",
                    "player_name": team.players[index],
                    "number": index + 1,
                    "role": role,
                    "x": x if team_index == 0 else PITCH_LENGTH - x,
                    "y": y if team_index == 0 else PITCH_WIDTH - y,
                })
        return lineup

    @staticmethod
    def _screen(event: MatchEvent) -> tuple[float, float]:
        """Convert an event from its team frame to screen coordinates (home attacks to the right)."""
        if event.team_id == TEAMS[0].team_id:
            return event.x, event.y
        return PITCH_LENGTH - event.x, PITCH_WIDTH - event.y

    def player_positions(self, events: list[MatchEvent], upto_second: int) -> list[dict[str, float | str | int]]:
        current = {player["player_id"]: dict(player) for player in self.lineup()}
        for event in events:
            if event.match_second > upto_second or event.player_id not in current:
                continue
            player = current[event.player_id]
            player["x"], player["y"] = self._screen(event)
            player["last_event_second"] = event.match_second
            player["last_event_type"] = event.event_type.value
        return list(current.values())

    def player_frames(self, events: list[MatchEvent]) -> list[dict]:
        """Per-event team-shape frames for the front end to interpolate on the match clock."""
        base = self.lineup()
        positions = {player["player_id"]: {"x": float(player["x"]), "y": float(player["y"])} for player in base}
        frames = [{
            "second": 0,
            "ball": {"x": 52.5, "y": 34.0},
            "positions": [[positions[p["player_id"]]["x"], positions[p["player_id"]]["y"]] for p in base],
        }]
        for event in events:
            ball_x, ball_y = self._screen(event)
            attacking_home = (event.possession_team_id or event.team_id) == TEAMS[0].team_id
            ball_progress = (ball_x - 52.5) / 52.5
            for player in base:
                player_id = str(player["player_id"])
                is_home = player["team_id"] == TEAMS[0].team_id
                base_x, base_y = float(player["x"]), float(player["y"])
                if is_home == attacking_home:
                    shift_x = max(-11, min(11, ball_progress * 10))
                    shift_y = max(-5, min(5, (ball_y - base_y) * 0.12))
                else:
                    shift_x = max(-8, min(8, ball_progress * 7))
                    shift_y = max(-5, min(5, (ball_y - base_y) * 0.1))
                position = {"x": max(2, min(103, base_x + shift_x)), "y": max(2, min(66, base_y + shift_y))}
                if player_id == event.player_id:
                    position = {"x": ball_x, "y": ball_y}
                positions[player_id] = position
            # 防守施压时球仍归持球队员，不能把球吸到施压者脚下。
            if event.event_type in {EventType.PRESSURE, EventType.TACKLE} and event.possession_team_id != event.team_id:
                previous_ball = frames[-1]["ball"]
                ball_x, ball_y = previous_ball["x"], previous_ball["y"]
            frames.append({
                "second": event.match_second,
                "ball": {"x": ball_x, "y": ball_y,
                         "event_type": "goal" if event.outcome == "goal" else event.event_type.value},
                "positions": [[positions[p["player_id"]]["x"], positions[p["player_id"]]["y"]] for p in base],
            })
            if event.event_type == EventType.PASS:
                ex, ey = float(event.attributes["end_x"]), float(event.attributes["end_y"])
                if event.team_id != self.home_team:
                    ex, ey = PITCH_LENGTH - ex, PITCH_WIDTH - ey
                receiver = event.attributes.get("interceptor_id") or event.attributes.get("receiver_id")
                if receiver in positions:
                    # 接球队员在出球时已处于接应位置，避免球员随球飞行。
                    positions[receiver] = {"x": ex, "y": ey}
                    frames[-1]["positions"] = [[positions[p["player_id"]]["x"], positions[p["player_id"]]["y"]] for p in base]
                duration = max(0.3, float(event.attributes["distance_m"]) / (float(event.attributes["ball_speed_kmh"]) / 3.6))
                frames.append({
                    "second": event.match_second + min(4.5, duration),
                    "ball": {"x": ex, "y": ey, "event_type": "interception" if event.outcome == "failed" else "reception"},
                    "positions": [[positions[p["player_id"]]["x"], positions[p["player_id"]]["y"]] for p in base],
                })
        return frames
