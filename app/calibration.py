"""Insight thresholds, expressed as rates and percentiles so they survive simulator changes.

Values come from ``python scripts/calibrate.py`` (seeds 15, 16, 30, 41, 44, 46). Re-run it after
changing the simulator; tests/test_live_match.py fails if the live match drifts out of range.
"""

from __future__ import annotations

from app.models import EventType, MatchEvent

# Share of consecutive events in the 5-minute window where possession changes hands.
CONTROLLED_TURNOVER_RATE = 0.176   # p20: calmest fifth of match windows
CHAOTIC_TURNOVER_RATE = 0.216      # p80: most broken-up fifth

# Momentum share (0-100) of the leading team in the 5-minute window.
CONTROL_SHARE = 72.9    # p90: genuine dominance
SHIFT_SHARE = 62.6      # p60: the new leader is clearly ahead
DECISIVE_SHARE = 60.5   # p50: the previous window had a real leader

# Ball regains (interceptions, recoveries, won tackles) in the opponent half per team per window.
HIGH_PRESS_REGAINS = 8  # ~p98: a standout spell, not routine pressing
HIGH_PRESS_X = 52.5


def turnover_rate(window: list[MatchEvent]) -> float:
    if len(window) < 2:
        return 0.0
    changes = sum(1 for a, b in zip(window, window[1:])
                  if (a.possession_team_id or a.team_id) != (b.possession_team_id or b.team_id))
    return changes / (len(window) - 1)


def phase_for(window: list[MatchEvent]) -> str:
    rate = turnover_rate(window)
    if rate >= CHAOTIC_TURNOVER_RATE:
        return "chaotic"
    if rate <= CONTROLLED_TURNOVER_RATE:
        return "controlled"
    return "contested"


def is_regain(event: MatchEvent) -> bool:
    return (event.event_type == EventType.POSSESSION_CHANGE
            or (event.event_type == EventType.TACKLE and event.outcome == "success"))


def high_regains(window: list[MatchEvent], team: str) -> list[MatchEvent]:
    return [e for e in window if e.team_id == team and e.x >= HIGH_PRESS_X and is_regain(e)]
