"""Deterministic metrics: every number a narrative uses can be traced back to events.

Coordinates follow the convention in ``app.models.MatchEvent``: each event is in its acting
team's attacking frame, so ``x >= ATTACKING_THIRD_X`` means "in the final third" for either team.
"""

from __future__ import annotations

from collections import defaultdict

from app.calibration import DECISIVE_SHARE, phase_for
from app.models import EventType, MatchEvent, MatchSnapshot, PlayerMetrics, TeamMetrics

MOMENTUM_WINDOW_SECONDS = 300
ATTACKING_THIRD_X = 70
PROMPT_EVENT_LIMIT = 12
MAX_POSSESSION_GAP_SECONDS = 30
MOMENTUM_DECISIVE_SHARE = DECISIVE_SHARE  # kept for backwards-compatible imports

# Momentum counts threatening actions only. Routine passes and pressure are excluded: with ~160
# events per 5-minute window they would drown the signal and pin every share near 50/50.
W_SHOT, W_XG, W_FINAL_THIRD_PASS, W_PROGRESSIVE, W_HIGH_REGAIN = 4.0, 10.0, 1.0, 1.5, 2.5
HIGH_REGAIN_X = 52.5


def _is_regain(event: MatchEvent) -> bool:
    return (event.event_type == EventType.POSSESSION_CHANGE
            or (event.event_type == EventType.TACKLE and event.outcome == "success"))


def momentum_raw(events: list[MatchEvent], team_id: str) -> float:
    score = 0.0
    for e in events:
        if e.team_id != team_id:
            continue
        if e.event_type == EventType.SHOT:
            score += W_SHOT + W_XG * float(e.attributes.get("xg", 0))
        elif e.event_type == EventType.PASS and e.outcome == "success":
            if e.x >= ATTACKING_THIRD_X:
                score += W_FINAL_THIRD_PASS
            if e.attributes.get("progressive"):
                score += W_PROGRESSIVE
        elif _is_regain(e) and e.x >= HIGH_REGAIN_X:
            score += W_HIGH_REGAIN
    return score


def momentum_shares(events: list[MatchEvent], home: str, away: str) -> dict[str, float]:
    """Normalised share of momentum in a window. The two teams always sum to 100."""
    raw = {home: momentum_raw(events, home), away: momentum_raw(events, away)}
    total = raw[home] + raw[away]
    if total <= 0:
        return {home: 50.0, away: 50.0}
    home_share = round(raw[home] / total * 100, 1)
    return {home: home_share, away: round(100 - home_share, 1)}


def infer_teams(events: list[MatchEvent]) -> tuple[str, str]:
    """Fallback when match metadata is not supplied: the kick-off team is the home side."""
    kickoff = next((e for e in events if e.attributes.get("kickoff")), events[0] if events else None)
    if kickoff is None:
        raise ValueError("home_team/away_team are required when there are no events")
    home = kickoff.team_id
    away = next((e.team_id for e in events if e.team_id != home), None)
    if away is None:
        raise ValueError("Only one team found in events; pass home_team/away_team explicitly")
    return home, away


class MetricsEngine:
    def snapshot(
        self,
        events: list[MatchEvent],
        upto_minute: int,
        upto_second: int | None = None,
        *,
        home_team: str | None = None,
        away_team: str | None = None,
        match_id: str | None = None,
    ) -> MatchSnapshot:
        """Build the match state at ``upto_minute:upto_second`` (whole minute if second is None)."""
        if home_team is None or away_team is None:
            home_team, away_team = infer_teams(events)
        second = 59 if upto_second is None else upto_second
        cutoff = upto_minute * 60 + second
        selected = sorted((e for e in events if e.match_second <= cutoff), key=lambda e: e.sequence)

        metrics = {team: TeamMetrics(team_id=team) for team in (home_team, away_team)}
        players: dict[str, PlayerMetrics] = {}
        pass_distance: dict[str, float] = defaultdict(float)
        pass_difficulty: dict[str, float] = defaultdict(float)
        possession_seconds: dict[str, float] = defaultdict(float)
        score = {home_team: 0, away_team: 0}

        for index, event in enumerate(selected):
            team = metrics.get(event.team_id)
            if team is None:
                continue
            player = players.setdefault(event.player_id, PlayerMetrics(
                player_id=event.player_id, player_name=event.player_name, team_id=event.team_id))
            if event.event_type in {EventType.PASS, EventType.SHOT, EventType.POSSESSION_CHANGE} or (
                    event.event_type == EventType.TACKLE and event.outcome == "success"):
                player.touches += 1
            player.distance_m += float(event.attributes.get("player_distance_m", 0))
            speed = float(event.attributes.get("player_speed_kmh", 0))
            player.top_speed_kmh = max(player.top_speed_kmh, speed)
            player.sprints += 1 if event.attributes.get("sprint") else 0

            if event.event_type == EventType.PASS:
                team.passes += 1
                player.passes += 1
                pass_distance[team.team_id] += float(event.attributes.get("distance_m", 0))
                pass_difficulty[team.team_id] += float(event.attributes.get("difficulty", 0))
                if event.outcome == "success":
                    team.passes_completed += 1
                    player.passes_completed += 1
                    if event.attributes.get("progressive"):
                        team.progressive_passes += 1
            elif event.event_type == EventType.SHOT:
                xg = float(event.attributes.get("xg", 0))
                team.shots += 1
                team.xg += xg
                player.shots += 1
                player.xg += xg
                team.max_shot_speed_kmh = max(team.max_shot_speed_kmh, float(event.attributes.get("shot_speed_kmh", 0)))
                if event.outcome in {"goal", "saved"}:
                    team.shots_on_target += 1
                if event.outcome == "goal":
                    team.goals += 1
                    player.goals += 1
                    score[event.team_id] += 1
            elif event.event_type == EventType.PRESSURE:
                team.pressures += 1
                player.pressures += 1
            elif event.event_type == EventType.TACKLE and event.outcome == "success":
                team.tackles_won += 1
                player.tackles_won += 1
            elif event.event_type == EventType.POSSESSION_CHANGE:
                team.possession_changes_won += 1
                player.recoveries += 1
            if event.x >= ATTACKING_THIRD_X:
                team.attacking_actions += 1

            # Possession: time until the next event is credited to the team holding the ball.
            holder = event.possession_team_id or event.team_id
            next_second = selected[index + 1].match_second if index + 1 < len(selected) else cutoff
            possession_seconds[holder] += min(MAX_POSSESSION_GAP_SECONDS, max(0, next_second - event.match_second))

        total_possession = sum(possession_seconds[t] for t in metrics)
        for team in metrics.values():
            team.pass_accuracy = round(team.passes_completed / team.passes * 100, 1) if team.passes else 0
            team.avg_pass_distance_m = round(pass_distance[team.team_id] / team.passes, 1) if team.passes else 0
            team.avg_pass_difficulty = round(pass_difficulty[team.team_id] / team.passes, 2) if team.passes else 0
            team.xg = round(team.xg, 2)
            team.possession_pct = (round(possession_seconds[team.team_id] / total_possession * 100, 1)
                                   if total_possession else 50.0)
        for player in players.values():
            player.xg = round(player.xg, 2)
            player.distance_m = round(player.distance_m, 1)

        window = [e for e in selected if e.match_second > cutoff - MOMENTUM_WINDOW_SECONDS]
        shares = momentum_shares(window, home_team, away_team)
        for team_id, share in shares.items():
            metrics[team_id].momentum_score = share

        previous_window = [e for e in selected
                           if cutoff - 2 * MOMENTUM_WINDOW_SECONDS < e.match_second <= cutoff - MOMENTUM_WINDOW_SECONDS]
        previous_leader = None
        if previous_window:
            previous = momentum_shares(previous_window, home_team, away_team)
            leader = max(previous, key=previous.get)
            if previous[leader] >= DECISIVE_SHARE:
                previous_leader = leader

        phase = phase_for(window)

        return MatchSnapshot(
            match_id=match_id or (events[0].match_id if events else "unknown"),
            minute=upto_minute,
            second=second,
            home_team=home_team,
            away_team=away_team,
            score_home=score[home_team],
            score_away=score[away_team],
            phase=phase,
            metrics=metrics,
            players=players,
            recent_events=window[-PROMPT_EVENT_LIMIT:],
            previous_momentum_leader=previous_leader,
            event_index={event.event_id: event for event in selected},
        )
