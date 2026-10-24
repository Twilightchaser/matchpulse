"""Multi-agent match intelligence.

Pipeline per match moment::

    Tactical Analyst  ->  Moment Explainer (Foundry LLM or deterministic)  ->  Evidence Guardian
          ^                         ^                                             |
          |                         +------ rejected: revise with feedback -------+
          |                                                                       v
    shared match state  <------------  Audience Director (per viewer, Foundry or local)

The LLM writes the story; deterministic code owns the facts. The Evidence Guardian rejects any
narrative that cites unknown events, uses numbers that are not in the evidence, or names a
player who is not in the cited events, and feeds the precise reason back for revision.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.calibration import CONTROL_SHARE, HIGH_PRESS_REGAINS, SHIFT_SHARE, high_regains
from app.metrics import ATTACKING_THIRD_X, MOMENTUM_WINDOW_SECONDS
from app.models import (
    AgentTrace,
    EventType,
    Evidence,
    InsightCandidate,
    MatchEvent,
    MatchSnapshot,
    OverlayCard,
)

# --------------------------------------------------------------------------- tuning

CHANCE_XG = 0.25              # non-goal shots at or above this xG are "big chances"
TRANSITION_SECONDS = 15       # regain-to-shot time that counts as a counter-attack
TRANSITION_MIN_XG = 0.10      # off-target counter shots below this are not worth a card
EVENT_LOOKBACK_SECONDS = 120  # event-driven insights must be this recent
COOLDOWN_MINUTES = 6          # state insights (control/shift/press) per team and kind
# Event-driven cards are paced too, so one busy spell cannot fill the screen with the same story.
KIND_COOLDOWN_MINUTES = {"transition": 4, "chance": 3, "milestone": 5, "player_moment": 3, "rhythm": 8, "high_pressure": 10}
MATCH_LEVEL_KINDS = {"rhythm"}  # about the match, not one team


def cooldown_key(candidate: "InsightCandidate") -> tuple[str, str]:
    team = "match" if candidate.kind in MATCH_LEVEL_KINDS else candidate.team_id
    return team, candidate.kind
MAX_REVISIONS = 2
WINDOW_MINUTES = MOMENTUM_WINDOW_SECONDS // 60

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
GENERIC_PLAYER_RE = re.compile(r"(?:\b([A-Z])\.\s*)?\bPlayer\s+(\d+)")
EVENT_ID_RE = re.compile(r"evt-\d+")
SCORELINE_RE = re.compile(r"(?<![\d.:])(\d+)\s*[-–]\s*(\d+)(?!\d|\.\d|:)")


class NarrativeService(Protocol):
    def explain(self, candidate: InsightCandidate, snapshot: MatchSnapshot, revision_note: str | None) -> str: ...

    def localize(self, candidate: InsightCandidate, profile: "AudienceProfile") -> dict[str, str]: ...

    def recap(self, facts: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class AudienceProfile:
    mode: str = "fan"
    language: str = "en"
    favorite_team: str | None = None
    favorite_player: str | None = None  # player_id or player_name


@dataclass
class SharedMatchState:
    """Match context every agent reads and writes during one moment."""

    snapshot: MatchSnapshot
    storylines: list[str] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)
    retries: int = 0
    announced_event_ids: set[str] = field(default_factory=set)
    cooldowns: dict[tuple[str, str], int] = field(default_factory=dict)
    suppressed: int = 0


@dataclass
class AnalysisResult:
    candidate: InsightCandidate | None
    traces: list[AgentTrace]
    verified: bool
    provider: str = "local"


def _trace(agent: str, started: float, summary: str, status: str = "completed", provider: str = "local") -> AgentTrace:
    return AgentTrace(agent=agent, status=status, provider=provider, summary=summary,
                      duration_ms=max(1, int((time.perf_counter() - started) * 1000)))


def event_lookup(snapshot: MatchSnapshot) -> dict[str, MatchEvent]:
    return snapshot.event_index or {event.event_id: event for event in snapshot.recent_events}


# --------------------------------------------------------------------------- fact checking

def _canon(token: str) -> str:
    value = float(token)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def known_names(snapshot: MatchSnapshot) -> list[str]:
    names = {snapshot.home_team, snapshot.away_team}
    names.update(event.player_name for event in event_lookup(snapshot).values())
    names.update(player.player_name for player in snapshot.players.values())
    return sorted(names, key=len, reverse=True)


def _name_pattern(name: str) -> re.Pattern[str]:
    # Whole-name match: "N. Player 1" must not match inside "N. Player 10".
    return re.compile(re.escape(name) + r"(?!\d)")


def strip_names(text: str, names: list[str]) -> str:
    for name in names:
        text = _name_pattern(name).sub(" ", text)
    return GENERIC_PLAYER_RE.sub(" ", text)


def numbers_in(text: str, names: list[str]) -> set[str]:
    return {_canon(token) for token in NUMBER_RE.findall(strip_names(text, names))}


def allowed_numbers(evidence: list[Evidence], cited_events: list[MatchEvent], names: list[str]) -> set[str]:
    allowed = {str(WINDOW_MINUTES)}
    for item in evidence:
        for token in NUMBER_RE.findall(strip_names(item.value, names)):
            allowed.add(_canon(token))
            value = float(token)
            if 0 < value < 1:  # 0.31 xG may be phrased as 31%
                allowed.add(_canon(str(round(value * 100))))
    for event in cited_events:
        allowed.update({str(event.minute), str(event.minute + 1), str(event.second)})
        for value in event.attributes.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                allowed.add(_canon(str(value)))
    return allowed


def players_mentioned(text: str, names: list[str], player_names: set[str]) -> set[str]:
    """Known full names, unique surnames, and generic 'Player N' references."""
    found = {name for name in player_names if _name_pattern(name).search(text)}
    remainder = text
    for name in names:
        remainder = _name_pattern(name).sub(" ", remainder)
    surnames: dict[str, set[str]] = {}
    for name in player_names:
        if " " in name and not re.search(r"\bPlayer\s+\d+$", name):
            surnames.setdefault(name.rsplit(" ", 1)[-1], set()).add(name)
    for surname, owners in surnames.items():
        if len(owners) == 1 and re.search(rf"(?<!\w){re.escape(surname)}(?!\w)", remainder, re.IGNORECASE):
            found.update(owners)
    for initial, number in GENERIC_PLAYER_RE.findall(remainder):
        found.add(f"{initial + '. ' if initial else ''}Player {number}")
    return found


def check_copy(text: str, evidence: list[Evidence], cited_events: list[MatchEvent], snapshot: MatchSnapshot) -> str | None:
    """Return a precise problem description, or None if the copy is fully supported."""
    if not text or not text.strip():
        return "empty narrative"
    names = known_names(snapshot)
    problems = []
    clean = strip_names(EVENT_ID_RE.sub(" ", text), names)
    evidence_scores = {m for item in evidence for m in SCORELINE_RE.findall(strip_names(item.value, names))}
    bad_scores = sorted({f"{a}-{b}" for a, b in SCORELINE_RE.findall(clean)
                         if (a, b) not in evidence_scores and (b, a) not in evidence_scores})
    if bad_scores:
        problems.append(f"scorelines not in evidence: {bad_scores}")
    bad_numbers = sorted(numbers_in(text, names) - allowed_numbers(evidence, cited_events, names), key=float)
    if bad_numbers:
        problems.append(f"numbers not backed by evidence: {bad_numbers}")
    all_players = {e.player_name for e in event_lookup(snapshot).values()} | {p.player_name for p in snapshot.players.values()}
    cited_players = {event.player_name for event in cited_events}
    unsupported = []
    for player in players_mentioned(text, names, all_players):
        if player in cited_players:
            continue
        if player not in all_players and any(name.endswith(player) for name in cited_players):
            continue  # "Player 10" shorthand for a cited "N. Player 10"
        unsupported.append(player)
    if unsupported:
        problems.append(f"players not in cited events: {sorted(unsupported)}")
    return "; ".join(problems) or None


# --------------------------------------------------------------------------- agents

class TacticalAnalystAgent:
    """Turns the deterministic snapshot into ranked, evidence-backed insight candidates."""

    name = "Tactical Analyst"

    def candidates(self, snapshot: MatchSnapshot) -> list[InsightCandidate]:
        index = event_lookup(snapshot)
        events = sorted(index.values(), key=lambda e: e.sequence)
        now = snapshot.match_second
        window = [e for e in events if e.match_second > now - MOMENTUM_WINDOW_SECONDS]
        lookback = [e for e in events if e.match_second > now - EVENT_LOOKBACK_SECONDS]
        shares = {team: snapshot.metrics[team].momentum_score for team in (snapshot.home_team, snapshot.away_team)}
        leader = max(shares, key=shares.get)
        trailer = snapshot.away_team if leader == snapshot.home_team else snapshot.home_team
        found: list[InsightCandidate] = []

        for shot in (e for e in lookback if e.event_type == EventType.SHOT):
            if shot.outcome == "goal":
                found.append(self._goal(shot, snapshot, events, window, shares))
            elif float(shot.attributes.get("xg", 0)) >= CHANCE_XG:
                found.append(self._chance(shot, snapshot, window, shares))
            regain = next((e for e in reversed(events)
                           if e.sequence < shot.sequence and e.team_id == shot.team_id and _is_regain(e)
                           and 0 <= shot.match_second - e.match_second <= TRANSITION_SECONDS), None)
            dangerous = shot.outcome in {"goal", "saved"} or float(shot.attributes.get("xg", 0)) >= TRANSITION_MIN_XG
            if regain is not None and dangerous and _no_turnover_between(events, regain, shot):
                found.append(self._transition(regain, shot, snapshot))

        previous = snapshot.previous_momentum_leader
        if previous and previous != leader and shares[leader] >= SHIFT_SHARE:
            found.append(self._momentum("momentum_shift", leader, trailer, previous, snapshot, window, events, shares))
        elif shares[leader] >= CONTROL_SHARE:
            found.append(self._momentum("control", leader, trailer, None, snapshot, window, events, shares))

        for team in shares:
            press = high_regains(window, team)
            if len(press) >= HIGH_PRESS_REGAINS:
                found.append(self._press(team, press, snapshot, shares))

        from app.enrichment import extra_candidates
        found.extend(extra_candidates(snapshot))
        found.sort(key=lambda c: (c.importance, c.minute), reverse=True)
        return found

    def run(self, state: SharedMatchState) -> tuple[InsightCandidate | None, AgentTrace]:
        started = time.perf_counter()
        snapshot = state.snapshot
        found = self.candidates(snapshot)
        fresh = [c for c in found if not self._suppressed(c, state)]
        state.suppressed = len(found) - len(fresh)
        if not fresh:
            state.handoffs.append("tactical_analysis:statistics_only")
            note = f" {state.suppressed} trigger(s) already covered." if state.suppressed else ""
            return None, _trace(self.name, started,
                                f"Routine phase: shared statistics updated; narrative agents not invoked.{note}")
        chosen = fresh[0]
        state.handoffs.append("tactical_analysis -> moment_explainer")
        return chosen, _trace(self.name, started,
                              f"Selected {chosen.kind} for {chosen.team_id} (importance {chosen.importance:.2f}) "
                              f"from {len(found)} trigger(s); {state.suppressed} already covered.")

    @staticmethod
    def _suppressed(candidate: InsightCandidate, state: SharedMatchState) -> bool:
        if candidate.trigger_event_id and candidate.trigger_event_id in state.announced_event_ids:
            return True
        if candidate.kind == "goal":
            return False  # a goal is always news
        last = state.cooldowns.get(cooldown_key(candidate))
        return last is not None and candidate.minute - last < KIND_COOLDOWN_MINUTES.get(candidate.kind, COOLDOWN_MINUTES)

    # ---- candidate builders: evidence values and facts are formatted once, here

    @staticmethod
    def _momentum_evidence(team: str, other: str, shares: dict[str, float], window: list[MatchEvent]) -> Evidence:
        ids = [e.event_id for e in window if e.team_id == team][-5:]
        return Evidence(label=f"Momentum share (last {WINDOW_MINUTES} min)",
                        value=f"{shares[team]:.1f}% vs {shares[other]:.1f}%", event_ids=ids)

    def _goal(self, goal: MatchEvent, snapshot: MatchSnapshot, events: list[MatchEvent],
              window: list[MatchEvent], shares: dict[str, float]) -> InsightCandidate:
        goals = [e for e in events if e.outcome == "goal" and e.sequence <= goal.sequence]
        home_goals = sum(1 for e in goals if e.team_id == snapshot.home_team)
        away_goals = len(goals) - home_goals
        other = snapshot.away_team if goal.team_id == snapshot.home_team else snapshot.home_team
        xg = f"{float(goal.attributes.get('xg', 0)):.2f}"
        facts = {"player": goal.player_name, "team": goal.team_id, "clock": goal.clock, "xg": xg,
                 "pct": round(float(xg) * 100), "home": snapshot.home_team, "away": snapshot.away_team,
                 "score": f"{home_goals}-{away_goals}"}
        return InsightCandidate(
            insight_id=f"insight-{snapshot.match_id}-{goal.event_id}", team_id=goal.team_id, minute=goal.minute,
            kind="goal", importance=1.0, trigger_event_id=goal.event_id,
            headline=f"{goal.player_name} scores for {goal.team_id}", explanation="",
            evidence=[
                Evidence(label="Goal", value=f"{goal.clock} · {goal.player_name}", event_ids=[goal.event_id]),
                Evidence(label="Chance quality", value=f"{xg} xG", event_ids=[goal.event_id]),
                Evidence(label="Scorer goals this match", value=f"{sum(e.player_id == goal.player_id for e in goals)} goals",
                         event_ids=[e.event_id for e in goals if e.player_id == goal.player_id]),
                Evidence(label="Score", value=f"{home_goals}-{away_goals}", event_ids=[e.event_id for e in goals]),
                self._momentum_evidence(goal.team_id, other, shares, window),
            ],
            facts=facts,
        )

    def _chance(self, shot: MatchEvent, snapshot: MatchSnapshot, window: list[MatchEvent],
                shares: dict[str, float]) -> InsightCandidate:
        other = snapshot.away_team if shot.team_id == snapshot.home_team else snapshot.home_team
        xg_value = float(shot.attributes.get("xg", 0))
        xg = f"{xg_value:.2f}"
        dist = f"{float(shot.attributes.get('distance_m', 0)):.1f} m"
        facts = {"player": shot.player_name, "team": shot.team_id, "clock": shot.clock, "xg": xg,
                 "pct": round(xg_value * 100), "distance": dist, "outcome": shot.outcome}
        verb = "is denied by the keeper" if shot.outcome == "saved" else "sees a big chance blocked"
        return InsightCandidate(
            insight_id=f"insight-{snapshot.match_id}-{shot.event_id}", team_id=shot.team_id, minute=shot.minute,
            kind="chance", importance=round(min(0.9, 0.62 + xg_value * 0.5), 2), trigger_event_id=shot.event_id,
            headline=f"{shot.player_name} {verb}", explanation="",
            evidence=[
                Evidence(label="Shot chance", value=f"{xg} xG", event_ids=[shot.event_id]),
                Evidence(label="Shot distance", value=dist, event_ids=[shot.event_id]),
                Evidence(label="Outcome", value=shot.outcome, event_ids=[shot.event_id]),
                self._momentum_evidence(shot.team_id, other, shares, window),
            ],
            facts=facts,
        )

    @staticmethod
    def _transition(regain: MatchEvent, shot: MatchEvent, snapshot: MatchSnapshot) -> InsightCandidate:
        seconds = shot.match_second - regain.match_second
        xg = f"{float(shot.attributes.get('xg', 0)):.2f}"
        facts = {"team": shot.team_id, "regain_player": regain.player_name, "regain_clock": regain.clock,
                 "shooter": shot.player_name, "shot_clock": shot.clock, "xg": xg, "seconds": seconds,
                 "outcome": shot.outcome}
        return InsightCandidate(
            insight_id=f"insight-{snapshot.match_id}-{shot.event_id}-transition", team_id=shot.team_id,
            minute=shot.minute, kind="transition", importance=0.95 if shot.outcome == "goal" else 0.82,
            trigger_event_id=shot.event_id,
            headline=f"{shot.team_id} turn defence into attack in {seconds} seconds", explanation="",
            evidence=[
                Evidence(label="Ball regained", value=f"{regain.clock} · {regain.player_name}", event_ids=[regain.event_id]),
                Evidence(label="Shot", value=f"{shot.clock} · {xg} xG", event_ids=[shot.event_id]),
                Evidence(label="Regain to shot", value=f"{seconds} s", event_ids=[regain.event_id, shot.event_id]),
            ],
            facts=facts,
        )

    def _momentum(self, kind: str, leader: str, trailer: str, previous: str | None, snapshot: MatchSnapshot,
                  window: list[MatchEvent], events: list[MatchEvent], shares: dict[str, float]) -> InsightCandidate:
        final_third = {team: [e for e in window if e.team_id == team and e.x >= ATTACKING_THIRD_X]
                       for team in (leader, trailer)}
        shots = {team: [e for e in window if e.team_id == team and e.event_type == EventType.SHOT]
                 for team in (leader, trailer)}
        attacks = f"{len(final_third[leader])} vs {len(final_third[trailer])}"
        shot_line = f"{len(shots[leader])} vs {len(shots[trailer])}"
        evidence = [
            self._momentum_evidence(leader, trailer, shares, window),
            Evidence(label=f"Final-third actions (last {WINDOW_MINUTES} min)", value=attacks,
                     event_ids=[e.event_id for e in final_third[leader]][-6:]),
            Evidence(label=f"Shots (last {WINDOW_MINUTES} min)", value=shot_line,
                     event_ids=[e.event_id for e in shots[leader] + shots[trailer]]),
        ]
        facts = {"leader": leader, "trailer": trailer, "share": f"{shares[leader]:.1f}%",
                 "trailer_share": f"{shares[trailer]:.1f}%", "attacks": attacks, "shots": shot_line}
        importance = min(0.72, 0.62 + (shares[leader] - CONTROL_SHARE) / 100)
        headline = f"{leader} are taking control"
        if kind == "momentum_shift" and previous:
            start = snapshot.match_second - 2 * MOMENTUM_WINDOW_SECONDS
            earlier = [e for e in events if e.team_id == previous
                       and start < e.match_second <= snapshot.match_second - MOMENTUM_WINDOW_SECONDS]
            evidence.append(Evidence(label="Previous momentum leader", value=previous,
                                     event_ids=[e.event_id for e in earlier][-3:]))
            facts["previous"] = previous
            importance = min(0.82, 0.74 + (shares[leader] - SHIFT_SHARE) / 200)
            headline = f"Momentum swings to {leader}"
        return InsightCandidate(
            insight_id=f"insight-{snapshot.match_id}-{kind}-{leader[:1].lower()}-{snapshot.minute}", team_id=leader,
            minute=snapshot.minute, kind=kind, importance=round(importance, 2), headline=headline,
            explanation="", evidence=evidence, facts=facts,
        )

    def _press(self, team: str, press: list[MatchEvent], snapshot: MatchSnapshot, shares: dict[str, float]) -> InsightCandidate:
        other = snapshot.away_team if team == snapshot.home_team else snapshot.home_team
        count = len(press)
        return InsightCandidate(
            insight_id=f"insight-{snapshot.match_id}-press-{team[:1].lower()}-{snapshot.minute}", team_id=team,
            minute=snapshot.minute, kind="high_pressure", importance=round(min(0.72, 0.6 + count * 0.02), 2),
            headline=f"{team} press high and win the ball back", explanation="",
            evidence=[
                Evidence(label=f"Ball won in opponent half (last {WINDOW_MINUTES} min)", value=f"{count} regains",
                         event_ids=[e.event_id for e in press][-6:]),
                Evidence(label=f"Momentum share (last {WINDOW_MINUTES} min)",
                         value=f"{shares[team]:.1f}% vs {shares[other]:.1f}%",
                         event_ids=[e.event_id for e in press][-3:]),
            ],
            facts={"team": team, "other": other, "count": count, "share": f"{shares[team]:.1f}%"},
        )


def _is_regain(event: MatchEvent) -> bool:
    return (event.event_type == EventType.POSSESSION_CHANGE
            or (event.event_type == EventType.TACKLE and event.outcome == "success"))


def _no_turnover_between(events: list[MatchEvent], regain: MatchEvent, shot: MatchEvent) -> bool:
    between = [e for e in events if regain.sequence <= e.sequence < shot.sequence]
    return all((e.possession_team_id or shot.team_id) == shot.team_id for e in between)


class MomentExplainerAgent:
    """Deterministic, evidence-grounded explanation (the fallback for the Foundry explainer)."""

    name = "Moment Explainer"

    def run(self, candidate: InsightCandidate, state: SharedMatchState,
            revision_note: str | None = None) -> tuple[InsightCandidate, AgentTrace]:
        started = time.perf_counter()
        candidate.explanation = self.explain(candidate)
        state.handoffs.append("moment_explainer -> evidence_guardian")
        if revision_note:
            return candidate, _trace(self.name, started, f"Rewrote the narrative from evidence after review: {revision_note}", "revised")
        return candidate, _trace(self.name, started, "Generated a deterministic, evidence-grounded explanation.")

    @staticmethod
    def explain(candidate: InsightCandidate) -> str:
        f = candidate.facts
        if "detail" in f:
            return f["detail"]
        if candidate.kind == "goal":
            return (f"{f['player']} scored at {f['clock']} from a {f['xg']} xG chance, "
                    f"making it {f['home']} {f['score']} {f['away']}.")
        if candidate.kind == "chance":
            ending = "was saved" if f["outcome"] == "saved" else "was blocked"
            return (f"At {f['clock']}, {f['player']} had a {f['xg']} xG chance from {f['distance']} and it {ending}. "
                    f"{f['team']} are creating openings of real quality.")
        if candidate.kind == "transition":
            return (f"{f['regain_player']} won the ball at {f['regain_clock']} and {f['shooter']} had a shot worth "
                    f"{f['xg']} xG at {f['shot_clock']}, only {f['seconds']} s after the regain.")
        if candidate.kind == "high_pressure":
            return (f"{f['team']} have won the ball back {f['count']} times in the opponent half over the last "
                    f"{WINDOW_MINUTES} minutes, turning defence into attack close to {f['other']}'s goal.")
        if candidate.kind == "momentum_shift":
            return (f"{f['previous']} had the upper hand in the previous {WINDOW_MINUTES} minutes, but {f['leader']} now "
                    f"hold {f['share']} of the momentum share, with {f['attacks']} final-third actions.")
        return (f"{f['leader']} hold {f['share']} of the momentum share over the last {WINDOW_MINUTES} minutes, "
                f"with {f['attacks']} final-third actions and {f['shots']} shots against {f['trailer']}.")


class EvidenceGuardianAgent:
    """Fact checker for every narrative: event ids, numbers and player names."""

    name = "Evidence Guardian"

    def run(self, candidate: InsightCandidate, state: SharedMatchState) -> tuple[bool, str]:
        index = event_lookup(state.snapshot)
        cited_ids = {event_id for item in candidate.evidence for event_id in item.event_ids}
        if not cited_ids:
            return False, "no event ids cited"
        missing = sorted(cited_ids - index.keys())
        if missing:
            return False, f"unknown or future event ids: {missing[:5]}"
        mentioned_ids = set(EVENT_ID_RE.findall(candidate.explanation))
        if mentioned_ids - index.keys():
            return False, f"narrative mentions unknown event ids: {sorted(mentioned_ids - index.keys())[:5]}"
        cited = [index[event_id] for event_id in cited_ids]
        problem = check_copy(candidate.explanation, candidate.evidence, cited, state.snapshot)
        if problem:
            return False, problem
        return True, f"{len(cited_ids)} event ids, all numbers and player names verified"

    def check_localized(self, copy: dict[str, str], candidate: InsightCandidate, snapshot: MatchSnapshot) -> str | None:
        index = event_lookup(snapshot)
        cited = [index[i] for item in candidate.evidence for i in item.event_ids if i in index]
        text = " ".join(copy.get(key, "") for key in ("headline", "body", "eyebrow"))
        # The demo clubs are identifiers, not localization targets.
        aliases = {"Northbridge FC": ("北桥",), "Riverside Athletic": ("河畔", "河岸竞技")}
        for club, translated in aliases.items():
            if club in (snapshot.home_team, snapshot.away_team) and any(alias in text for alias in translated):
                return f"Keep club name exactly as supplied: {club}; do not translate it"
        return check_copy(text, candidate.evidence, cited, snapshot)


class AudienceDirectorAgent:
    """Shapes one verified insight into a viewer-specific on-screen card."""

    name = "Audience Director"

    def run(self, candidate: InsightCandidate, verified: bool, profile: AudienceProfile, snapshot: MatchSnapshot,
            provider: str = "local", localized: dict[str, str] | None = None) -> tuple[OverlayCard, AgentTrace]:
        started = time.perf_counter()
        analyst = profile.mode == "analyst"
        chinese = profile.language == "zh"
        if localized:
            headline, body, eyebrow = localized["headline"], localized["body"], localized["eyebrow"]
        elif chinese:
            headline, body, eyebrow = self._chinese_copy(candidate, analyst)
        else:
            headline = candidate.headline
            body = candidate.explanation if analyst else self._fan_copy(candidate)
            eyebrow = "TACTICAL SIGNAL" if analyst else "MATCH PULSE"

        index = event_lookup(snapshot)
        cited = [index[i] for item in candidate.evidence for i in item.event_ids if i in index]
        evidence = list(candidate.evidence) if analyst else list(candidate.evidence[:2])
        player = self._resolve_player(profile.favorite_player, snapshot)
        if player is not None:
            evidence.append(self._player_evidence(player, snapshot))
            if any(event.player_id == player for event in cited):
                eyebrow = ("球员聚焦 · " if chinese else "PLAYER FOCUS · ") + eyebrow
        if profile.favorite_team and profile.favorite_team == candidate.team_id:
            eyebrow = ("你的球队 · " if chinese else "YOUR TEAM · ") + eyebrow

        if profile.favorite_team and not analyst:
            ours = profile.favorite_team == candidate.team_id
            if candidate.kind == "goal":
                prefix = ("你的球队破门！" if ours else "对手进球，你的球队需要回应。") if chinese else ("Your club scores! " if ours else "Your club has conceded. ")
            else:
                prefix = (("关注你的球队：" if ours else "对手动态：") if chinese else ("Your club: " if ours else "Opposition watch: "))
            body = prefix + body

        # Event-driven cards sync to their event; state cards (control, swings, press) to "now".
        trigger = index.get(candidate.trigger_event_id or "")
        if trigger and trigger.event_type == EventType.SHOT and "shot_speed_kmh" in trigger.attributes:
            evidence.insert(len(evidence)-1 if player is not None else len(evidence), Evidence(label="Shot speed", value=f"{float(trigger.attributes['shot_speed_kmh']):.1f} km/h", event_ids=[trigger.event_id]))
        card = OverlayCard(
            insight_id=candidate.insight_id,
            display_at_second=trigger.match_second if trigger else snapshot.match_second,
            priority="critical" if candidate.importance >= 0.95 else "high" if candidate.importance >= 0.72 else "medium",
            mode=profile.mode, language=profile.language, eyebrow=eyebrow, headline=headline, body=body,
            evidence=evidence, verified=verified, provider=provider, kind=candidate.kind, team_id=candidate.team_id,
        )
        return card, _trace(self.name, started, f"Produced {profile.mode}/{profile.language} card"
                            + (" with player focus." if player else "."), provider=provider)

    @staticmethod
    def _resolve_player(favorite: str | None, snapshot: MatchSnapshot) -> str | None:
        if not favorite:
            return None
        if favorite in snapshot.players:
            return favorite
        return next((pid for pid, p in snapshot.players.items() if p.player_name == favorite), None)

    @staticmethod
    def _player_evidence(player_id: str, snapshot: MatchSnapshot) -> Evidence:
        p = snapshot.players[player_id]
        recent = [e.event_id for e in sorted(event_lookup(snapshot).values(), key=lambda e: e.sequence)
                  if e.player_id == player_id][-3:]
        return Evidence(label="Player focus",
                        value=f"{p.player_name} · {p.touches} touches · {p.passes_completed}/{p.passes} passes · {p.xg:.2f} xG",
                        event_ids=recent)

    @staticmethod
    def _fan_copy(candidate: InsightCandidate) -> str:
        f = candidate.facts
        required = {
            "goal": {"player", "home", "score", "away"},
            "chance": {"team", "player", "pct", "outcome"},
            "transition": {"team", "seconds"},
            "high_pressure": {"team", "count"},
            "momentum_shift": {"leader", "previous"},
            "control": {"leader", "trailer"},
        }.get(candidate.kind, set())
        if candidate.kind in {"milestone", "rhythm", "player_moment"}:
            return candidate.explanation or f["detail"]
        if not required.issubset(f):
            return candidate.explanation or candidate.headline
        return {
            "goal": lambda: f"GOAL! {f['player']} finishes it. {f['home']} {f['score']} {f['away']}.",
            "chance": lambda: (f"Huge chance for {f['team']}: {f['player']} had an opening that goes in about "
                               f"{f['pct']}% of the time, but it {'was saved' if f['outcome'] == 'saved' else 'was blocked'}."),
            "transition": lambda: f"{f['team']} won the ball and were shooting {f['seconds']} seconds later. Fast and direct.",
            "high_pressure": lambda: f"{f['team']} are hunting the ball high up the pitch and forcing mistakes.",
            "momentum_shift": lambda: f"The game has turned: {f['leader']} have taken control away from {f['previous']}.",
            "control": lambda: f"{f['leader']} are on top right now, and most of the play is in {f['trailer']}'s half.",
        }[candidate.kind]()

    @staticmethod
    def _chinese_copy(candidate: InsightCandidate, analyst: bool) -> tuple[str, str, str]:
        f = candidate.facts
        kind = candidate.kind
        if "zh_detail" in f:
            return f["zh_title"], f["zh_detail"], "战术洞察" if analyst else "比赛脉搏"
        required = {
            "goal": {"player", "team", "clock", "xg", "home", "score", "away"},
            "chance": {"player", "team", "clock", "xg", "distance", "outcome", "pct"},
            "transition": {"team", "seconds", "regain_clock", "regain_player", "shot_clock", "shooter", "xg"},
            "high_pressure": {"team", "count"},
            "momentum_shift": {"leader", "previous", "share", "attacks"},
            "control": {"leader", "share", "attacks", "shots", "trailer"},
        }.get(kind, set())
        if not required.issubset(f):
            text = candidate.explanation or candidate.headline
            return candidate.headline, text, "战术洞察" if analyst else "比赛脉搏"
        if kind == "goal":
            headline = f"{f['player']} 为 {f['team']} 破门"
            body = (f"{f['clock']}，{f['player']} 把握住一次 {f['xg']} xG 的机会，比分变为 {f['home']} {f['score']} {f['away']}。"
                    if analyst else f"进球！{f['home']} {f['score']} {f['away']}。")
        elif kind == "chance":
            result = "被门将扑出" if f["outcome"] == "saved" else "被封堵"
            headline = f"{f['player']} 错失良机"
            body = (f"{f['clock']}，{f['player']} 在 {f['distance']} 处获得一次 {f['xg']} xG 的机会，射门{result}。"
                    if analyst else f"{f['team']} 制造了一次约 {f['pct']}% 进球概率的好机会，可惜射门{result}。")
        elif kind == "transition":
            headline = f"{f['team']} 断球后 {f['seconds']} 秒完成射门"
            body = (f"{f['regain_clock']} {f['regain_player']} 断球，{f['shot_clock']} {f['shooter']} 射门（{f['xg']} xG），"
                    f"由守转攻仅用 {f['seconds']} 秒。" if analyst else f"{f['team']} 抢回球权后迅速打出威胁反击。")
        elif kind == "high_pressure":
            headline = f"{f['team']} 高位逼抢奏效"
            body = (f"近 {WINDOW_MINUTES} 分钟 {f['team']} 在对方半场 {f['count']} 次抢回球权。"
                    if analyst else f"{f['team']} 在前场积极逼抢，迫使对手频频失误。")
        elif kind == "momentum_shift":
            headline = f"比赛势头转向 {f['leader']}"
            body = (f"前 {WINDOW_MINUTES} 分钟由 {f['previous']} 占优，如今 {f['leader']} 拿下 {f['share']} 的势头份额，"
                    f"进攻三区动作 {f['attacks']}。" if analyst else f"比赛风向变了，{f['leader']} 从 {f['previous']} 手中夺回主动。")
        else:
            headline = f"{f['leader']} 正在掌控比赛"
            body = (f"近 {WINDOW_MINUTES} 分钟 {f['leader']} 占据 {f['share']} 的势头份额，进攻三区动作 {f['attacks']}，射门 {f['shots']}。"
                    if analyst else f"{f['leader']} 目前占据主动，比赛主要在 {f['trailer']} 半场进行。")
        return headline, body, "战术洞察" if analyst else "比赛脉搏"


class RecapAgent:
    """Full-time facts, a deterministic cited recap, and validation for LLM-written recaps."""

    name = "Recap Agent"

    def facts(self, events: list[MatchEvent], snapshot: MatchSnapshot) -> dict[str, Any]:
        goals = [e for e in events if e.event_type == EventType.SHOT and e.outcome == "goal"]
        chances = sorted([e for e in events if e.event_type == EventType.SHOT and e.outcome != "goal"],
                         key=lambda e: float(e.attributes.get("xg", 0)), reverse=True)[:3]
        teams = (snapshot.home_team, snapshot.away_team)
        return {
            "match_id": snapshot.match_id,
            "home_team": snapshot.home_team,
            "away_team": snapshot.away_team,
            "score": {snapshot.home_team: snapshot.score_home, snapshot.away_team: snapshot.score_away},
            "goals": [{"minute": e.clock, "team": e.team_id, "player": e.player_name,
                       "xg": float(e.attributes.get("xg", 0)), "event_id": e.event_id} for e in goals],
            "key_chances": [{"minute": e.clock, "team": e.team_id, "player": e.player_name,
                             "xg": float(e.attributes.get("xg", 0)), "outcome": e.outcome, "event_id": e.event_id}
                            for e in chances],
            "team_stats": {t: {"xg": snapshot.metrics[t].xg, "shots": snapshot.metrics[t].shots,
                               "shots_on_target": snapshot.metrics[t].shots_on_target,
                               "possession_pct": snapshot.metrics[t].possession_pct,
                               "pass_accuracy": snapshot.metrics[t].pass_accuracy} for t in teams},
        }

    def summary(self, facts: dict[str, Any]) -> str:
        home, away = facts["home_team"], facts["away_team"]
        h, a = facts["score"][home], facts["score"][away]
        goal_refs = " ".join(f"[{g['event_id']}]" for g in facts["goals"])
        best = facts["key_chances"][0] if facts["key_chances"] else None
        if h == a:
            first = f"{home} and {away} drew {h}-{a}"
        else:
            winner, loser = (home, away) if h > a else (away, home)
            first = f"{winner} beat {loser} {max(h, a)}-{min(h, a)}"
        refs = goal_refs or (f"[{best['event_id']}]" if best else "")
        sentences = [f"{first} {refs}".strip() + "."]
        for goal in facts["goals"]:
            sentences.append(f"{goal['player']} scored for {goal['team']} at {goal['minute']} "
                             f"from a {goal['xg']:.2f} xG chance [{goal['event_id']}].")
        stats = facts["team_stats"]
        sentences.append(f"{home} created {stats[home]['xg']:.2f} xG from {stats[home]['shots']} shots, against "
                         f"{stats[away]['xg']:.2f} xG from {stats[away]['shots']} shots for {away}.")
        if best:
            sentences.append(f"The best chance that did not go in fell to {best['player']} at {best['minute']} "
                             f"({best['xg']:.2f} xG) [{best['event_id']}].")
        more = max((home, away), key=lambda t: stats[t]["possession_pct"])
        sentences.append(f"{more} had {stats[more]['possession_pct']:.1f}% of the ball.")
        return " ".join(sentences)

    def validate(self, text: str, facts: dict[str, Any], snapshot: MatchSnapshot) -> tuple[bool, str]:
        if not text or not text.strip():
            return False, "empty recap"
        index = event_lookup(snapshot)
        cited = set(EVENT_ID_RE.findall(text))
        if not cited:
            return False, "recap cites no event ids"
        if cited - index.keys():
            return False, f"recap cites unknown event ids: {sorted(cited - index.keys())[:5]}"
        names = known_names(snapshot)
        score = tuple(str(v) for v in facts["score"].values())
        bad_scores = sorted({f"{a}-{b}" for a, b in SCORELINE_RE.findall(strip_names(EVENT_ID_RE.sub(" ", text), names))
                             if (a, b) not in (score, score[::-1])})
        if bad_scores:
            return False, f"recap scorelines contradict the result: {bad_scores}"
        allowed = {"90", str(len(facts["goals"]))}
        for value in facts["score"].values():
            allowed.add(str(value))
        for item in facts["goals"] + facts["key_chances"]:
            minute, second = (int(part) for part in item["minute"].split(":"))
            allowed.update({str(minute), str(minute + 1), str(second), _canon(str(item["xg"])),
                            _canon(str(round(item["xg"] * 100)))})
        for stats in facts["team_stats"].values():
            for value in stats.values():
                allowed.update({_canon(str(value)), _canon(str(round(value)))})
        bad = sorted(numbers_in(EVENT_ID_RE.sub(" ", text), names) - allowed, key=float)
        if bad:
            return False, f"recap numbers not in facts: {bad}"
        all_players = {e.player_name for e in index.values()}
        allowed_players = {item["player"] for item in facts["goals"] + facts["key_chances"]}
        unsupported = sorted(p for p in players_mentioned(text, names, all_players)
                             if p not in allowed_players and not any(n.endswith(p) for n in allowed_players))
        if unsupported:
            return False, f"recap names players outside the facts: {unsupported}"
        return True, "recap verified"

    def run(self, events: list[MatchEvent], snapshot: MatchSnapshot) -> dict[str, Any]:
        facts = self.facts(events, snapshot)
        return {**facts, "summary": self.summary(facts), "summary_provider": "local",
                "metrics": {team: value.model_dump() for team, value in snapshot.metrics.items()}}


# --------------------------------------------------------------------------- workflow

class InsightWorkflow:
    """Each match moment is analysed and verified once; audience cards are produced per viewer.

    ``stateful=True`` (live replay) remembers announced events and cooldowns so the same goal or
    storyline is not repeated. ``stateful=False`` (timeline scrubbing) treats every moment
    independently so jumping around the timeline always shows what matters at that moment.
    """

    def __init__(self, narrative_service: NarrativeService | None = None, *, stateful: bool = True) -> None:
        self.analyst = TacticalAnalystAgent()
        self.explainer = MomentExplainerAgent()
        self.checker = EvidenceGuardianAgent()
        self.director = AudienceDirectorAgent()
        self.recap_agent = RecapAgent()
        self.narrative_service = narrative_service
        self.stateful = stateful
        self.storylines: list[str] = []
        self.announced_event_ids: set[str] = set()
        self.cooldowns: dict[tuple[str, str], int] = {}
        self._analysis_cache: dict[tuple[str, int, int], AnalysisResult] = {}

    def analyze(self, snapshot: MatchSnapshot, preferred_player: str | None = None) -> AnalysisResult:
        index = event_lookup(snapshot)
        latest = max((event.sequence for event in index.values()), default=0)
        key = (snapshot.match_id, snapshot.match_second, latest, preferred_player)
        if key in self._analysis_cache:
            return self._analysis_cache[key]

        state = SharedMatchState(
            snapshot=snapshot, storylines=self.storylines,
            announced_event_ids=self.announced_event_ids if self.stateful else set(),
            cooldowns=self.cooldowns if self.stateful else {},
        )
        candidate, analyst_trace = self.analyst.run(state)
        traces = [analyst_trace]
        if preferred_player:
            from app.enrichment import player_candidate
            focused = player_candidate(snapshot, preferred_player)
            if focused and not self.analyst._suppressed(focused, state) and (candidate is None or candidate.importance < .95):
                candidate = focused
                traces.append(_trace("Player Scout", time.perf_counter(), "Tracked player contribution selected from event evidence."))
        if candidate is None:
            return self._remember(key, AnalysisResult(None, traces, False))

        provider = self._explain(candidate, snapshot, state, traces, revision_note=None)
        verified, reason = self.checker.run(candidate, state)
        while not verified and state.retries < MAX_REVISIONS:
            traces.append(_trace(self.checker.name, time.perf_counter(), f"Rejected: {reason}", "rejected"))
            state.retries += 1
            state.handoffs.append(f"evidence_guardian -> moment_explainer revision {state.retries}")
            provider = self._explain(candidate, snapshot, state, traces, revision_note=reason, provider=provider)
            verified, reason = self.checker.run(candidate, state)

        if not verified:
            traces.append(_trace(self.checker.name, time.perf_counter(), f"Rejected: {reason}", "rejected"))
            traces.append(_trace(self.checker.name, time.perf_counter(),
                                 f"{MAX_REVISIONS} revisions exhausted; switching to the deterministic explainer.", "fallback"))
            candidate, trace = self.explainer.run(candidate, state)
            traces.append(trace)
            provider = "local"
            verified, reason = self.checker.run(candidate, state)
        traces.append(_trace(self.checker.name, time.perf_counter(), reason if verified else f"Rejected: {reason}",
                             "completed" if verified else "rejected"))

        if verified:
            self._commit(candidate, state)
        return self._remember(key, AnalysisResult(candidate, traces, verified, provider if verified else "local"))

    def _explain(self, candidate: InsightCandidate, snapshot: MatchSnapshot, state: SharedMatchState,
                 traces: list[AgentTrace], revision_note: str | None, provider: str | None = None) -> str:
        use_llm = self.narrative_service is not None and provider in (None, "foundry", "deepseek")
        if use_llm:
            started = time.perf_counter()
            try:
                candidate.explanation = self.narrative_service.explain(candidate, snapshot, revision_note)
                summary = (f"Revised with the configured model after Guardian feedback: {revision_note}" if revision_note
                           else "Generated the moment explanation with the configured model.")
                traces.append(_trace(self.explainer.name, started, summary, "revised" if revision_note else "completed", getattr(self.narrative_service, "provider", "foundry")))
                return getattr(self.narrative_service, "provider", "foundry")
            except Exception as exc:  # network, auth, timeout, malformed output
                traces.append(_trace(self.explainer.name, started,
                                     f"Model call failed ({type(exc).__name__}); deterministic explainer takes over.",
                                     "fallback", getattr(self.narrative_service, "provider", "foundry")))
        candidate, trace = self.explainer.run(candidate, state, revision_note=revision_note)
        traces.append(trace)
        return "local"

    def _commit(self, candidate: InsightCandidate, state: SharedMatchState) -> None:
        if not self.stateful:
            return
        if candidate.trigger_event_id:
            self.announced_event_ids.add(candidate.trigger_event_id)
        self.cooldowns[cooldown_key(candidate)] = candidate.minute
        self.storylines.append(f"{candidate.team_id}:{candidate.kind}:{candidate.minute}")
        self.storylines = self.storylines[-12:]
        state.snapshot.storylines = list(self.storylines)

    def _remember(self, key: tuple[str, int, int], result: AnalysisResult) -> AnalysisResult:
        self._analysis_cache[key] = result
        if len(self._analysis_cache) > 256:
            self._analysis_cache.pop(next(iter(self._analysis_cache)))
        return result

    def present(self, result: AnalysisResult, snapshot: MatchSnapshot,
                profile: AudienceProfile) -> tuple[OverlayCard | None, list[AgentTrace]]:
        traces = list(result.traces)
        candidate = result.candidate
        if candidate is None:
            return None, traces
        if not result.verified:
            return self._fallback_card(candidate, profile, snapshot), traces
        localized = None
        provider = result.provider
        if self.narrative_service is not None:
            started = time.perf_counter()
            try:
                localized = self.narrative_service.localize(candidate, profile)
                problem = self.checker.check_localized(localized, candidate, snapshot)
                if not problem and profile.language == "zh":
                    from app.language import chinese_copy_problem
                    names = [snapshot.home_team, snapshot.away_team] + [e.player_name for e in event_lookup(snapshot).values()]
                    problem = chinese_copy_problem(" ".join(localized.values()), names)
                if problem:
                    traces.append(_trace(self.checker.name, started,
                                         f"Rejected model audience copy: {problem}. Using local copy.", "rejected"))
                    localized, provider = None, "local"
                else:
                    provider = getattr(self.narrative_service, "provider", "foundry")
                    traces.append(_trace(self.director.name, started,
                                         f"Model wrote {profile.mode}/{profile.language} copy; Guardian verified it.",
                                         provider=getattr(self.narrative_service, "provider", "foundry")))
            except Exception as exc:
                localized, provider = None, "local"
                traces.append(_trace(self.director.name, started,
                                     f"Model localization failed ({type(exc).__name__}); using local copy.",
                                     "fallback", getattr(self.narrative_service, "provider", "foundry")))
        card, trace = self.director.run(candidate, True, profile, snapshot, provider, localized)
        traces.append(trace)
        return card, traces

    def run(self, snapshot: MatchSnapshot, profile: AudienceProfile) -> tuple[OverlayCard | None, list[AgentTrace]]:
        return self.present(self.analyze(snapshot, profile.favorite_player), snapshot, profile)

    def recap(self, events: list[MatchEvent], snapshot: MatchSnapshot, language: str = "en") -> dict[str, Any]:
        result = self.recap_agent.run(events, snapshot)
        if language == "zh":
            home, away = result["home_team"], result["away_team"]
            score = result["score"]
            text = [f"{home} {score[home]}-{score[away]} {away}。"]
            for goal in result["goals"]:
                text.append(f"{goal['minute']}，{goal['player']} 为 {goal['team']} 进球，机会质量为 {goal['xg']:.2f} xG [{goal['event_id']}]。")
            result["summary"] = " ".join(text)
        result["language"] = language
        if self.narrative_service is None:
            return result
        facts = {k: result[k] for k in ("home_team", "away_team", "score", "goals", "key_chances", "team_stats")}
        facts["language"] = "Simplified Chinese" if language == "zh" else "English"
        try:
            generated = self.narrative_service.recap(facts)
            if language == "zh":
                from app.language import chinese_copy_problem
                problem = chinese_copy_problem(generated, [snapshot.home_team, snapshot.away_team] + [e.player_name for e in events])
                if problem:
                    raise ValueError(problem)
            ok, reason = self.recap_agent.validate(generated, result, snapshot)
        except Exception as exc:
            ok, reason, generated = False, type(exc).__name__, ""
        if ok:
            result["summary"], result["summary_provider"] = generated.strip(), getattr(self.narrative_service, "provider", "foundry")
        else:
            result["summary_fallback"] = reason
        return result

    def _fallback_card(self, candidate: InsightCandidate, profile: AudienceProfile, snapshot: MatchSnapshot) -> OverlayCard:
        card, _ = self.director.run(candidate, False, profile, snapshot)
        card.eyebrow = "VERIFIED MATCH STATE"
        card.body = "Only deterministic, verified match information is shown."
        card.provider = "local"
        return card
