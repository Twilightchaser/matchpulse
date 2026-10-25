"""Shared data contracts across modules."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class EventType(StrEnum):
    PASS = "pass"
    SHOT = "shot"
    TACKLE = "tackle"
    PRESSURE = "pressure"
    POSSESSION_CHANGE = "possession_change"
    OFFSIDE = "offside"
    CORNER = "corner"
    THROW_IN = "throw_in"
    GOAL_KICK = "goal_kick"
    KICKOFF = "kickoff"


class MatchEvent(BaseModel):
    """One synthetic match event.

    Coordinates are in the acting team's attacking frame: x=0 is the team's own goal line and
    x=105 is the goal it attacks. Defensive actions (pressure, tackle, possession_change) are
    attributed to the defending team, in that team's own frame.
    """

    event_id: str
    match_id: str
    sequence: int = Field(ge=1)
    minute: int = Field(ge=0, le=120)
    second: int = Field(ge=0, le=59)
    event_type: EventType
    team_id: str
    player_id: str
    player_name: str
    x: float = Field(ge=0, le=105)
    y: float = Field(ge=0, le=68)
    outcome: Literal["success", "failed", "goal", "saved", "blocked"]
    attributes: dict[str, Any] = Field(default_factory=dict)
    possession_team_id: str | None = Field(default=None, description="Team in possession after this event")

    @property
    def clock(self) -> str:
        return f"{self.minute:02d}:{self.second:02d}"

    @property
    def match_second(self) -> int:
        return self.minute * 60 + self.second


class MatchMetadata(BaseModel):
    match_id: str
    home_team: str
    away_team: str
    seed: int


class TeamMetrics(BaseModel):
    team_id: str
    passes: int = 0
    passes_completed: int = 0
    pass_accuracy: float = 0
    progressive_passes: int = 0
    avg_pass_distance_m: float = 0
    avg_pass_difficulty: float = 0
    shots: int = 0
    shots_on_target: int = 0
    goals: int = 0
    xg: float = 0
    max_shot_speed_kmh: float = 0
    pressures: int = 0
    tackles_won: int = 0
    possession_changes_won: int = 0
    attacking_actions: int = 0
    possession_pct: float = 50
    momentum_score: float = 50


class PlayerMetrics(BaseModel):
    player_id: str
    player_name: str
    team_id: str
    touches: int = 0
    passes: int = 0
    passes_completed: int = 0
    shots: int = 0
    goals: int = 0
    xg: float = 0
    tackles_won: int = 0
    recoveries: int = 0
    pressures: int = 0
    distance_m: float = 0
    top_speed_kmh: float = 0
    sprints: int = 0


class Evidence(BaseModel):
    label: str
    value: str
    event_ids: list[str] = Field(default_factory=list)


InsightKind = Literal["momentum_shift", "high_pressure", "transition", "chance", "goal", "control", "milestone", "rhythm", "player_moment"]


class InsightCandidate(BaseModel):
    insight_id: str
    team_id: str
    minute: int
    kind: InsightKind
    importance: float = Field(ge=0, le=1)
    headline: str
    explanation: str
    evidence: list[Evidence]
    trigger_event_id: str | None = None
    # Pre-formatted values shared by the deterministic templates (same strings as the evidence).
    facts: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)


class OverlayCard(BaseModel):
    insight_id: str
    display_at_second: int
    duration_seconds: int = 8
    priority: Literal["low", "medium", "high", "critical"]
    mode: Literal["analyst", "fan"]
    language: Literal["en", "zh"]
    eyebrow: str
    headline: str
    body: str
    evidence: list[Evidence]
    verified: bool
    provider: Literal["foundry", "deepseek", "local"] = "local"
    kind: InsightKind | None = None
    team_id: str | None = None


class AgentTrace(BaseModel):
    agent: str
    status: Literal["completed", "rejected", "revised", "fallback", "skipped"]
    provider: Literal["foundry", "deepseek", "local"] = "local"
    summary: str
    summary_zh: str | None = None  # same note for the Chinese studio; never machine-translated
    duration_ms: int


class MatchSnapshot(BaseModel):
    match_id: str
    minute: int
    second: int = 59
    home_team: str
    away_team: str
    score_home: int
    score_away: int
    phase: Literal["controlled", "contested", "chaotic"]
    metrics: dict[str, TeamMetrics]
    players: dict[str, PlayerMetrics] = Field(default_factory=dict)
    recent_events: list[MatchEvent]
    previous_momentum_leader: str | None = None
    storylines: list[str] = Field(default_factory=list)
    # Every event up to the snapshot time. Used by the Evidence Guardian; never serialized.
    event_index: dict[str, MatchEvent] = Field(default_factory=dict, exclude=True, repr=False)

    @property
    def match_second(self) -> int:
        return self.minute * 60 + self.second


class InsightResponse(BaseModel):
    snapshot: MatchSnapshot
    overlay: OverlayCard | None
    trace: list[AgentTrace]
