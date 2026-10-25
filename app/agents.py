"""Multi-agent match intelligence.

Pipeline per match moment::

    Tactical Analyst  ->  Moment Explainer (model or deterministic)  ->  Evidence Guardian
          ^                         ^                                       |
          |                         +---- rejected: revise with feedback ----+
          |                                                                 v
    shared match state  <-----------  Audience Director (per viewer, model or local)

The model writes the story; deterministic code owns the facts. The Evidence Guardian rejects any
narrative that cites unknown events, uses numbers that are not in the evidence, names a player
who is not in the cited events, or uses phrasing banned by docs/WRITING_STYLE.md, and feeds the
precise reason back for revision.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.calibration import CONTROL_SHARE, HIGH_PRESS_REGAINS, SHIFT_SHARE, high_regains
from app.house_style import (
    banned_phrase,
    chance_tier,
    kind_label,
    minute_mark,
    ordinal,
    reason_zh,
    short_club,
    surname,
    variant,
)
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
KIND_COOLDOWN_MINUTES = {"transition": 4, "chance": 3, "milestone": 5, "player_moment": 3, "rhythm": 12,
                         "high_pressure": 10, "momentum": 8}
MATCH_LEVEL_KINDS = {"rhythm"}  # about the match, not one team
# "On top" and "the game swings" are the same story told twice; they share one cooldown per team.
SHARED_COOLDOWN = {"control": "momentum", "momentum_shift": "momentum"}
# State cards describe a spell of play. Before eight minutes there is no spell to describe, and a
# "100% vs 0%" share built from two progressive passes is noise, not a story.
STATE_KINDS = {"control", "momentum_shift", "high_pressure", "rhythm"}
MIN_STATE_SECOND = 8 * 60
MIN_FINAL_THIRD_ACTIONS = 3


def cooldown_key(candidate: "InsightCandidate") -> tuple[str, str]:
    team = "match" if candidate.kind in MATCH_LEVEL_KINDS else candidate.team_id
    return team, SHARED_COOLDOWN.get(candidate.kind, candidate.kind)


def cooldown_minutes(candidate: "InsightCandidate") -> int:
    return KIND_COOLDOWN_MINUTES.get(SHARED_COOLDOWN.get(candidate.kind, candidate.kind), COOLDOWN_MINUTES)


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


def _trace(agent: str, started: float, summary: str, status: str = "completed", provider: str = "local",
           zh: str | None = None) -> AgentTrace:
    return AgentTrace(agent=agent, status=status, provider=provider, summary=summary, summary_zh=zh,
                      duration_ms=max(1, int((time.perf_counter() - started) * 1000)))


def _model_name(service: Any) -> str:
    return {"deepseek": "DeepSeek", "foundry": "Foundry"}.get(getattr(service, "provider", "foundry"), "The model")


def event_lookup(snapshot: MatchSnapshot) -> dict[str, MatchEvent]:
    return snapshot.event_index or {event.event_id: event for event in snapshot.recent_events}


def player_names(snapshot: MatchSnapshot) -> set[str]:
    return ({p.player_name for p in snapshot.players.values()}
            | {e.player_name for e in event_lookup(snapshot).values()})


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
    for surname_, owners in surnames.items():
        if len(owners) == 1 and re.search(rf"(?<!\w){re.escape(surname_)}(?!\w)", remainder, re.IGNORECASE):
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
        problems.append(f"scorelines not in evidence: {', '.join(bad_scores)}")
    bad_numbers = sorted(numbers_in(text, names) - allowed_numbers(evidence, cited_events, names), key=float)
    if bad_numbers:
        problems.append(f"numbers not backed by evidence: {', '.join(bad_numbers)}")
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
        problems.append(f"players not in cited events: {', '.join(sorted(unsupported))}")
    phrase = banned_phrase(text)
    if phrase:
        problems.append(f"banned phrase: {phrase}")
    return "; ".join(problems) or None


# --------------------------------------------------------------------------- copy tables
# One table per kind keeps the four voices (fan/analyst x English/Chinese) side by side.

ORDINAL_WORDS = {2: "Second", 3: "Third", 4: "Fourth"}
CHINESE_NUMERALS = {2: "二", 3: "三", 4: "四", 5: "五"}

GOAL_HEADLINE = {
    "opener": ("{player} opens the scoring", "{player} 破门，{team} 首开纪录"),
    "comeback": ("{player} completes the turnaround", "{player} 破门，{team} 完成反超"),
    "again": ("{player} restores {team_s}'s lead", "{player} 破门，{team} 再次领先"),
    "level": ("{player} equalises", "{player} 破门，{team} 扳平比分"),
    "double": ("{player} doubles {team_s}'s lead", "{player} 破门，{team} 领先两球"),
    "extend": ("{player} extends {team_s}'s lead", "{player} 破门，{team} 扩大领先"),
    "back": ("{player} pulls one back", "{player} 破门，{team} 扳回一球"),
}
GOAL_ANALYST_EN = "{p} scored from a {xg} xG chance."
GOAL_FAN_EN = (  # by chance tier: big chance, good chance, half-chance, long shot
    ("{p} makes no mistake.", "{p} doesn't miss from there."),
    ("{p} takes it well.", "{p} finishes it off.", "Good finish from {p}."),
    ("{p} makes the most of a half-chance.", "Not much on, but {p} finds a way."),
    ("{p} scores out of almost nothing.", "Where did that come from? {p} makes something out of nothing."),
)
GOAL_FAN_ZH = (
    ("这么好的机会，{p} 没有浪费。", "{p} 稳稳把机会变成了进球。"),
    ("{p} 把握住了机会。", "{p} 没有浪费这次机会。"),
    ("机会不算大，{p} 还是打进了。", "半个机会，{p} 也抓住了。"),
    ("几乎没有机会，{p} 硬是打进了。", "{p} 无中生有，打进一球。"),
)
GOAL_FAN_STATE_EN = {"comeback": " {team_s} have turned it round"}

SHOT_OUTCOME_ZH = {"saved": "被门将扑出", "blocked": "被封堵", "failed": "偏出了", "goal": "球进了"}
CHANCE_HEADLINE = {  # outcome -> (English phrasings, Chinese phrasings)
    "saved": (("{player} denied by the keeper", "{player} can't beat the keeper"),
              ("{player} 的射门被扑出", "{player} 的射门被门将挡出")),
    "blocked": (("{player} has a shot blocked", "{player}'s shot is charged down"),
                ("{player} 的射门被封堵", "{player} 的射门被后卫挡出")),
    "failed": (("{player} misses a big chance",), ("{player} 错失良机",)),
}
CHANCE_ANALYST_EN = {"saved": ", and the keeper saves", "blocked": ", but it's blocked", "failed": ", but it's off target"}
# The headline already says what happened to the shot; the body says how good the chance was.
CHANCE_FAN_EN = {
    0: {"saved": ("{p} should score from there.", "Big chance, and a big save to keep it out."),
        "blocked": ("{p} should score from there, but a defender gets in the way.",),
        "failed": ("{p} should score from there.",)},
    1: {"saved": ("Good chance for {team_s}, well saved.",),
        "blocked": ("Good chance for {team_s}, but it's blocked.",),
        "failed": ("Good chance for {team_s}, but {p} can't keep it on target.",)},
}
CHANCE_FAN_ZH = (("这是一次绝佳机会，{p} 本该破门。", "门前绝佳机会，{p} 没能把握住。"),
                 ("{team} 这次进攻打出了不错的机会。",))
CHANCE_PUSH_EN = ("{team_s} have {share} of the momentum.", "{team_s} are pushing: {share} of the momentum.")
STATE_FAN = {  # kind -> list of (English, Chinese) phrasings
    "control": [
        ("{l} are on top, and most of the play is in {t}'s half.", "{leader} 占据主动，比赛主要在 {trailer} 半场进行。"),
        ("It's all {l} at the moment. {t} can't get out.", "现在几乎是 {leader} 的节奏，{trailer} 被压在半场出不来。"),
        ("{l} have {t} pinned back.", "{leader} 把 {trailer} 压在了自己半场。"),
    ],
    "momentum_shift": [
        ("{l} are on top now, after a spell where {prev_s} had the upper hand.", "场面变了，{leader} 从 {previous} 手里接过了主动权。"),
        ("{l} have wrestled control back from {prev_s}.", "{leader} 从 {previous} 手中夺回了场面优势。"),
        ("The pressure has switched. It's {l} pushing now.", "风向变了，现在是 {leader} 在压着打。"),
    ],
    "high_pressure": [
        ("{team_s} keep winning the ball in {other_s}'s half.", "{team} 在前场不断把球抢回来，{other} 很难从后场出球。"),
        ("{team_s} are pressing high, and {other_s} can't get out.", "{team} 前场逼抢很凶，{other} 出球越来越难。"),
    ],
}
PRESS_ANALYST = (
    ("{team_s} have won the ball back {count} times in {other_s}'s half in the last five minutes. "
     "{other_s} can't get out.",
     "近 5 分钟 {team} 在对方半场 {count} 次抢回球权，{other} 很难从后场出球。"),
    ("{count} regains in {other_s}'s half in five minutes. {team_s}'s press is working.",
     "近 5 分钟 {team} 已在对方半场抢回球权 {count} 次，{other} 一出球就被围抢。"),
)
TRANSITION_OUTCOME_EN = {"goal": " that goes in", "saved": ", saved", "blocked": ", blocked", "failed": ", off target"}
TRANSITION_FAN_EN = (
    "{r} wins it, and {seconds} seconds later {sh} is shooting.",
    "From {r} winning it to {sh}'s shot: {seconds} seconds.",
    "{team_s} go straight up the other end. {sh} shoots {seconds} seconds after {r} won it.",
)
TRANSITION_FAN_SAME_EN = ("{r} wins it back and is shooting {seconds} seconds later.",
                          "{r} wins it back, and {seconds} seconds later has a shot.")
TRANSITION_FAN_ZH = ("{r} 抢回球权，{seconds} 秒后 {sh} 已经起脚射门，{result}。",
                     "从 {r} 断球到 {sh} 射门，只用了 {seconds} 秒，{result}。")
TRANSITION_FAN_OUTCOME_EN = {"goal": " And it's in.", "saved": " The keeper saves.", "blocked": " It's blocked.",
                             "failed": " It goes off target."}


def _fill(template: str, facts: dict[str, Any]) -> str:
    return template.format(**facts)


def _pick(candidate: InsightCandidate, count: int) -> int:
    """Index of the phrasing to use. Live replays rotate per card type (see InsightWorkflow), so two
    counter-attacks in a row never read the same; standalone moments use a stable hash."""
    return candidate.facts.get("variant", variant(candidate.insight_id, 997)) % count


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
        others = player_names(snapshot)
        found: list[InsightCandidate] = []

        for shot in (e for e in lookback if e.event_type == EventType.SHOT):
            if shot.outcome == "goal":
                found.append(self._goal(shot, snapshot, events, window, shares, others))
            elif float(shot.attributes.get("xg", 0)) >= CHANCE_XG:
                found.append(self._chance(shot, snapshot, window, shares, others))
            regain = next((e for e in reversed(events)
                           if e.sequence < shot.sequence and e.team_id == shot.team_id and _is_regain(e)
                           and 0 <= shot.match_second - e.match_second <= TRANSITION_SECONDS), None)
            dangerous = shot.outcome in {"goal", "saved"} or float(shot.attributes.get("xg", 0)) >= TRANSITION_MIN_XG
            if regain is not None and dangerous and _no_turnover_between(events, regain, shot):
                found.append(self._transition(regain, shot, snapshot, others))

        settled = now >= MIN_STATE_SECOND
        previous = snapshot.previous_momentum_leader
        if settled and self._territory_backs(leader, trailer, window):
            if previous and previous != leader and shares[leader] >= SHIFT_SHARE:
                found.append(self._momentum("momentum_shift", leader, trailer, previous, snapshot, window, events, shares))
            elif shares[leader] >= CONTROL_SHARE:
                found.append(self._momentum("control", leader, trailer, None, snapshot, window, events, shares))

        if settled:
            for team in shares:
                press = high_regains(window, team)
                if len(press) >= HIGH_PRESS_REGAINS:
                    found.append(self._press(team, press, snapshot, shares))

        from app.enrichment import extra_candidates
        found.extend(c for c in extra_candidates(snapshot) if settled or c.kind not in STATE_KINDS)
        found.sort(key=lambda c: (c.importance, c.minute), reverse=True)
        return found

    @staticmethod
    def _territory_backs(leader: str, trailer: str, window: list[MatchEvent]) -> bool:
        """A momentum card needs play in the final third to point at, not only a share."""
        ahead = sum(1 for e in window if e.team_id == leader and e.x >= ATTACKING_THIRD_X)
        behind = sum(1 for e in window if e.team_id == trailer and e.x >= ATTACKING_THIRD_X)
        return ahead >= MIN_FINAL_THIRD_ACTIONS and ahead > behind

    def run(self, state: SharedMatchState) -> tuple[InsightCandidate | None, AgentTrace]:
        started = time.perf_counter()
        snapshot = state.snapshot
        found = self.candidates(snapshot)
        fresh = [c for c in found if not self._suppressed(c, state)]
        state.suppressed = len(found) - len(fresh)
        if not fresh:
            state.handoffs.append("tactical_analysis:statistics_only")
            note_en = f" {state.suppressed} already shown." if state.suppressed else ""
            note_zh = f"另有 {state.suppressed} 个已经播过。" if state.suppressed else ""
            return None, _trace(self.name, started, f"Nothing worth a graphic right now; stats updated.{note_en}",
                                zh=f"暂时没有值得上屏的时刻，只更新数据。{note_zh}")
        chosen = fresh[0]
        state.handoffs.append("tactical_analysis -> moment_explainer")
        shown_en = f"; {state.suppressed} already shown" if state.suppressed else ""
        shown_zh = f"，{state.suppressed} 个已经播过" if state.suppressed else ""
        team_en = "" if chosen.kind in MATCH_LEVEL_KINDS else f" ({chosen.team_id})"
        team_zh = "" if chosen.kind in MATCH_LEVEL_KINDS else f"（{chosen.team_id}）"
        return chosen, _trace(
            self.name, started,
            f"Picked {kind_label(chosen.kind)}{team_en} from {len(found)} candidate(s){shown_en}.",
            zh=f"从 {len(found)} 个候选中选出：{kind_label(chosen.kind, 'zh')}{team_zh}{shown_zh}。")

    @staticmethod
    def _suppressed(candidate: InsightCandidate, state: SharedMatchState) -> bool:
        if candidate.trigger_event_id and candidate.trigger_event_id in state.announced_event_ids:
            return True
        if candidate.kind == "goal":
            return False  # a goal is always news
        last = state.cooldowns.get(cooldown_key(candidate))
        return last is not None and candidate.minute - last < cooldown_minutes(candidate)

    # ---- candidate builders: evidence values and facts are formatted once, here

    @staticmethod
    def _momentum_evidence(team: str, other: str, shares: dict[str, float], window: list[MatchEvent]) -> Evidence:
        ids = [e.event_id for e in window if e.team_id == team][-5:]
        return Evidence(label=f"Momentum share (last {WINDOW_MINUTES} min)",
                        value=f"{shares[team]:.1f}% vs {shares[other]:.1f}%", event_ids=ids)

    @staticmethod
    def _goal_state(goal: MatchEvent, goals: list[MatchEvent]) -> tuple[str, int]:
        """How a goal changed the game for the scoring side, and the margin afterwards."""
        scored = conceded = 0
        trailed = False
        for earlier in goals:
            if earlier.sequence >= goal.sequence:
                break
            if earlier.team_id == goal.team_id:
                scored += 1
            else:
                conceded += 1
            trailed |= scored < conceded
        margin = scored + 1 - conceded
        if scored == conceded:
            state = "opener" if scored == 0 else "comeback" if trailed else "again"
        elif margin == 0:
            state = "level"
        elif scored > conceded:
            state = "double" if margin == 2 else "extend"
        else:
            state = "back"
        return state, margin

    def _goal(self, goal: MatchEvent, snapshot: MatchSnapshot, events: list[MatchEvent],
              window: list[MatchEvent], shares: dict[str, float], others: set[str]) -> InsightCandidate:
        goals = [e for e in events if e.outcome == "goal" and e.sequence <= goal.sequence]
        home_goals = sum(1 for e in goals if e.team_id == snapshot.home_team)
        away_goals = len(goals) - home_goals
        other = snapshot.away_team if goal.team_id == snapshot.home_team else snapshot.home_team
        xg_value = float(goal.attributes.get("xg", 0))
        xg = f"{xg_value:.2f}"
        state, margin = self._goal_state(goal, goals)
        ours, theirs = (home_goals, away_goals) if goal.team_id == snapshot.home_team else (away_goals, home_goals)
        scorer_goals = sum(e.player_id == goal.player_id for e in goals)
        facts = {"player": goal.player_name, "p": surname(goal.player_name, others), "team": goal.team_id,
                 "team_s": short_club(goal.team_id), "other": other, "other_s": short_club(other),
                 "clock": goal.clock, "mark": minute_mark(goal.minute), "xg": xg, "tier": chance_tier(xg_value),
                 "pct": round(xg_value * 100), "home": snapshot.home_team, "away": snapshot.away_team,
                 "score": f"{home_goals}-{away_goals}", "lead": f"{ours}-{theirs}", "state": state, "margin": margin,
                 "scorer_goals": scorer_goals, "share": f"{shares[goal.team_id]:.1f}%",
                 "other_share": f"{shares[other]:.1f}%", "share_value": shares[goal.team_id]}
        return InsightCandidate(
            insight_id=f"insight-{snapshot.match_id}-{goal.event_id}", team_id=goal.team_id, minute=goal.minute,
            kind="goal", importance=1.0, trigger_event_id=goal.event_id,
            headline=_fill(GOAL_HEADLINE[state][0], facts), explanation="",
            evidence=[
                Evidence(label="Goal", value=f"{goal.clock} · {goal.player_name}", event_ids=[goal.event_id]),
                Evidence(label="Chance quality", value=f"{xg} xG", event_ids=[goal.event_id]),
                Evidence(label="Scorer goals this match", value=f"{scorer_goals} goal{'' if scorer_goals == 1 else 's'}",
                         event_ids=[e.event_id for e in goals if e.player_id == goal.player_id]),
                Evidence(label="Score", value=f"{home_goals}-{away_goals}", event_ids=[e.event_id for e in goals]),
                self._momentum_evidence(goal.team_id, other, shares, window),
            ],
            facts=facts,
        )

    def _chance(self, shot: MatchEvent, snapshot: MatchSnapshot, window: list[MatchEvent],
                shares: dict[str, float], others: set[str]) -> InsightCandidate:
        other = snapshot.away_team if shot.team_id == snapshot.home_team else snapshot.home_team
        xg_value = float(shot.attributes.get("xg", 0))
        xg = f"{xg_value:.2f}"
        # distance_m is how far the ball travelled (0.7 m when the keeper blocks at the shooter's feet);
        # viewers expect the distance from goal when the shot was struck.
        dist = f"{math.hypot(105 - shot.x, 34 - shot.y):.1f} m"
        outcome = shot.outcome if shot.outcome in CHANCE_HEADLINE else "failed"
        facts = {"player": shot.player_name, "p": surname(shot.player_name, others), "team": shot.team_id,
                 "team_s": short_club(shot.team_id), "clock": shot.clock, "mark": minute_mark(shot.minute), "xg": xg,
                 "tier": min(1, chance_tier(xg_value)), "pct": round(xg_value * 100), "distance": dist,
                 "outcome": outcome, "share": f"{shares[shot.team_id]:.1f}%", "share_value": shares[shot.team_id]}
        facts["headline_options"] = (("{player} misses the target",) if outcome == "failed" and facts["tier"]
                                     else CHANCE_HEADLINE[outcome][0])
        headline = facts["headline_options"][0]
        return InsightCandidate(
            insight_id=f"insight-{snapshot.match_id}-{shot.event_id}", team_id=shot.team_id, minute=shot.minute,
            kind="chance", importance=round(min(0.9, 0.62 + xg_value * 0.5), 2), trigger_event_id=shot.event_id,
            headline=_fill(headline, facts), explanation="",
            evidence=[
                Evidence(label="Shot chance", value=f"{xg} xG", event_ids=[shot.event_id]),
                Evidence(label="Shot distance", value=dist, event_ids=[shot.event_id]),
                Evidence(label="Outcome", value=shot.outcome, event_ids=[shot.event_id]),
                self._momentum_evidence(shot.team_id, other, shares, window),
            ],
            facts=facts,
        )

    @staticmethod
    def _transition(regain: MatchEvent, shot: MatchEvent, snapshot: MatchSnapshot, others: set[str]) -> InsightCandidate:
        seconds = shot.match_second - regain.match_second
        xg = f"{float(shot.attributes.get('xg', 0)):.2f}"
        facts = {"team": shot.team_id, "team_s": short_club(shot.team_id), "regain_player": regain.player_name,
                 "r": surname(regain.player_name, others), "regain_clock": regain.clock, "shooter": shot.player_name,
                 "sh": surname(shot.player_name, others), "shot_clock": shot.clock, "xg": xg, "seconds": seconds,
                 "outcome": shot.outcome, "same": regain.player_id == shot.player_id, "mark": minute_mark(shot.minute)}
        return InsightCandidate(
            insight_id=f"insight-{snapshot.match_id}-{shot.event_id}-transition", team_id=shot.team_id,
            minute=shot.minute, kind="transition", importance=0.95 if shot.outcome == "goal" else 0.82,
            trigger_event_id=shot.event_id,
            headline=f"{facts['team_s']} break in {seconds} seconds", explanation="",
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
        facts = {"leader": leader, "l": short_club(leader), "trailer": trailer, "t": short_club(trailer),
                 "share": f"{shares[leader]:.1f}%", "trailer_share": f"{shares[trailer]:.1f}%", "attacks": attacks,
                 "al": len(final_third[leader]), "at": len(final_third[trailer]), "shots": shot_line}
        importance = min(0.72, 0.62 + (shares[leader] - CONTROL_SHARE) / 100)
        headline = f"{facts['l']} on top"
        if kind == "momentum_shift" and previous:
            start = snapshot.match_second - 2 * MOMENTUM_WINDOW_SECONDS
            earlier = [e for e in events if e.team_id == previous
                       and start < e.match_second <= snapshot.match_second - MOMENTUM_WINDOW_SECONDS]
            evidence.append(Evidence(label="Previous momentum leader", value=previous,
                                     event_ids=[e.event_id for e in earlier][-3:]))
            facts.update(previous=previous, prev_s=short_club(previous))
            importance = min(0.82, 0.74 + (shares[leader] - SHIFT_SHARE) / 200)
            headline = f"The game swings {facts['l']}'s way"
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
            headline=f"{short_club(team)} winning it high up the pitch", explanation="",
            evidence=[
                Evidence(label=f"Ball won in opponent half (last {WINDOW_MINUTES} min)", value=f"{count} regains",
                         event_ids=[e.event_id for e in press][-6:]),
                Evidence(label=f"Momentum share (last {WINDOW_MINUTES} min)",
                         value=f"{shares[team]:.1f}% vs {shares[other]:.1f}%",
                         event_ids=[e.event_id for e in press][-3:]),
            ],
            facts={"team": team, "team_s": short_club(team), "other": other, "other_s": short_club(other),
                   "count": count, "share": f"{shares[team]:.1f}%"},
        )


def _is_regain(event: MatchEvent) -> bool:
    return (event.event_type == EventType.POSSESSION_CHANGE
            or (event.event_type == EventType.TACKLE and event.outcome == "success"))


def _no_turnover_between(events: list[MatchEvent], regain: MatchEvent, shot: MatchEvent) -> bool:
    between = [e for e in events if regain.sequence <= e.sequence < shot.sequence]
    return all((e.possession_team_id or shot.team_id) == shot.team_id for e in between)


def _required(candidate: InsightCandidate, keys: set[str] | None) -> bool:
    """Templates run only when every fact they use is present; otherwise callers fall back."""
    return keys is not None and keys.issubset(candidate.facts)


REQUIRED_FACTS = {
    "goal": {"p", "team_s", "other_s", "mark", "xg", "tier", "state", "home", "score", "away", "lead", "share",
             "other_share", "share_value", "scorer_goals", "team", "other"},
    "chance": {"p", "team", "team_s", "mark", "xg", "tier", "distance", "outcome", "share", "share_value"},
    "transition": {"r", "sh", "team", "team_s", "xg", "seconds", "outcome", "same"},
    "high_pressure": {"team", "team_s", "other", "other_s", "count"},
    "momentum_shift": {"leader", "l", "previous", "prev_s", "share", "al", "at"},
    "control": {"leader", "l", "trailer", "t", "share", "al", "at"},
}


class MomentExplainerAgent:
    """Deterministic explanation in the analyst voice (the fallback for the model explainer)."""

    name = "Moment Explainer"

    def run(self, candidate: InsightCandidate, state: SharedMatchState,
            revision_note: str | None = None) -> tuple[InsightCandidate, AgentTrace]:
        started = time.perf_counter()
        candidate.explanation = self.explain(candidate)
        state.handoffs.append("moment_explainer -> evidence_guardian")
        if revision_note:
            return candidate, _trace(self.name, started, f"Rewrote the line from the match facts after: {revision_note}",
                                     "revised", zh=f"根据比赛事实重写，原因：{reason_zh(revision_note)}")
        return candidate, _trace(self.name, started, "Wrote the line from the match facts.",
                                 zh="根据比赛事实写出这条文案。")

    @staticmethod
    def explain(candidate: InsightCandidate) -> str:
        f = candidate.facts
        if "detail" in f:
            return f["detail"]
        if not _required(candidate, REQUIRED_FACTS.get(candidate.kind)):
            return candidate.headline
        kind = candidate.kind
        if kind == "goal":
            text = _fill(GOAL_ANALYST_EN, f)
            if f["share_value"] >= 55:
                text += f" {f['team_s']} had {f['share']} of the momentum over the last five minutes."
            elif f["share_value"] <= 45:
                text += f" It was against the run of play: {f['other_s']} had {f['other_share']} of the momentum."
            if f["scorer_goals"] in ORDINAL_WORDS:
                text += f" {ORDINAL_WORDS[f['scorer_goals']]} goal of the match for {f['p']}."
            return text
        if kind == "chance":
            text = f"{f['p']} gets a {f['xg']} xG chance{CHANCE_ANALYST_EN[f['outcome']]}."
            if f["share_value"] >= 60:
                text += " " + _fill(CHANCE_PUSH_EN[_pick(candidate, 2)], f)
            return text
        if kind == "transition":
            ending = TRANSITION_OUTCOME_EN.get(f["outcome"], "")
            if f["same"]:
                return f"{f['r']} wins it back and has a {f['xg']} xG shot {f['seconds']} seconds later{ending}."
            return f"{f['r']} wins it back, and {f['seconds']} seconds later {f['sh']} has a {f['xg']} xG shot{ending}."
        if kind == "high_pressure":
            return _fill(PRESS_ANALYST[_pick(candidate, len(PRESS_ANALYST))][0], f)
        if kind == "momentum_shift":
            return (f"{f['l']} have taken over from {f['prev_s']}: {f['share']} of the momentum and "
                    f"{f['al']} final-third actions to {f['at']} in the last five minutes.")
        return (f"{f['l']} are camped in {f['t']}'s half: {f['al']} final-third actions to {f['at']} "
                f"and {f['share']} of the momentum in the last five minutes.")


class EvidenceGuardianAgent:
    """Fact checker for every narrative: event ids, numbers, player names and banned phrasing."""

    name = "Evidence Guardian"

    def run(self, candidate: InsightCandidate, state: SharedMatchState) -> tuple[bool, str]:
        index = event_lookup(state.snapshot)
        cited_ids = {event_id for item in candidate.evidence for event_id in item.event_ids}
        if not cited_ids:
            return False, "no event ids cited"
        missing = sorted(cited_ids - index.keys())
        if missing:
            return False, f"unknown or future event ids: {', '.join(missing[:5])}"
        mentioned_ids = set(EVENT_ID_RE.findall(candidate.explanation))
        if mentioned_ids - index.keys():
            return False, f"narrative mentions unknown event ids: {', '.join(sorted(mentioned_ids - index.keys())[:5])}"
        cited = [index[event_id] for event_id in cited_ids]
        problem = check_copy(candidate.explanation, candidate.evidence, cited, state.snapshot)
        if problem:
            return False, problem
        return True, f"Checked {len(cited_ids)} events: every number and name matches."

    @staticmethod
    def passed_zh(reason: str) -> str:
        found = re.match(r"Checked (\d+) events", reason)
        return f"核对了 {found.group(1)} 条事件，数字和球员名全部对得上。" if found else reason_zh(reason)

    def check_localized(self, copy: dict[str, str], candidate: InsightCandidate, snapshot: MatchSnapshot) -> str | None:
        index = event_lookup(snapshot)
        cited = [index[i] for item in candidate.evidence for i in item.event_ids if i in index]
        text = " ".join(copy.get(key, "") for key in ("headline", "body"))
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
            headline, body = localized["headline"], localized["body"]
        elif chinese:
            headline, body = self._chinese_copy(candidate, analyst)
        else:
            headline = candidate.headline
            body = candidate.explanation if analyst else self._fan_copy(candidate)

        # Tags name the moment, like a broadcast graphic; they are never written by the model.
        tags = [kind_label(candidate.kind, "zh") if chinese else kind_label(candidate.kind).upper()]
        index = event_lookup(snapshot)
        cited = [index[i] for item in candidate.evidence for i in item.event_ids if i in index]
        evidence = list(candidate.evidence) if analyst else list(candidate.evidence[:2])
        player = self._resolve_player(profile.favorite_player, snapshot)
        if player is not None:
            evidence.append(self._player_evidence(player, snapshot))
            if candidate.kind != "player_moment" and any(event.player_id == player for event in cited):
                tags.insert(0, "球员聚焦" if chinese else "PLAYER FOCUS")
        if profile.favorite_team and candidate.kind not in MATCH_LEVEL_KINDS:
            ours = profile.favorite_team == candidate.team_id
            tags.insert(0, ("你的球队" if ours else "对手") if chinese else ("YOUR TEAM" if ours else "OPPONENT"))

        # Event-driven cards sync to their event; state cards (control, swings, press) to "now".
        trigger = index.get(candidate.trigger_event_id or "")
        if trigger and trigger.event_type == EventType.SHOT and "shot_speed_kmh" in trigger.attributes:
            evidence.insert(len(evidence) - 1 if player is not None else len(evidence),
                            Evidence(label="Shot speed", value=f"{float(trigger.attributes['shot_speed_kmh']):.1f} km/h",
                                     event_ids=[trigger.event_id]))
        card = OverlayCard(
            insight_id=candidate.insight_id,
            display_at_second=(trigger.match_second if trigger and candidate.kind not in STATE_KINDS
                               else snapshot.match_second),
            priority="critical" if candidate.importance >= 0.95 else "high" if candidate.importance >= 0.72 else "medium",
            mode=profile.mode, language=profile.language, eyebrow=" · ".join(tags), headline=headline, body=body,
            evidence=evidence, verified=verified, provider=provider, kind=candidate.kind, team_id=candidate.team_id,
        )
        mode_zh = "分析师" if analyst else "球迷"
        language_en = "Chinese" if chinese else "English"
        focus_en, focus_zh = (" Your player is tagged.", "，并标注关注的球员") if player else ("", "")
        return card, _trace(self.name, started, f"Built the {profile.mode} card in {language_en}.{focus_en}",
                            provider=provider, zh=f"生成{mode_zh}模式的{'中文' if chinese else '英文'}卡片{focus_zh}。")

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
        kind = candidate.kind
        if "fan_detail" in f:
            return f["fan_detail"]
        if "detail" in f:
            return candidate.explanation or f["detail"]
        if not _required(candidate, REQUIRED_FACTS.get(kind)):
            return candidate.explanation or candidate.headline
        if kind == "goal":
            options = GOAL_FAN_EN[f["tier"]]
            text = _fill(options[_pick(candidate, len(options))], f)
            against_run = f["share_value"] <= 45  # the other side had the momentum
            if f["state"] in GOAL_FAN_STATE_EN:
                text += _fill(GOAL_FAN_STATE_EN[f["state"]], f) + (", against the run of play." if against_run else ".")
            elif against_run:
                text += " That's against the run of play."
            if f["state"] == "back" and f["margin"] == -1:
                text += " Game on."
            return f"{text} {f['home']} {f['score']} {f['away']}."
        if kind == "chance":
            options = CHANCE_FAN_EN[f["tier"]][f["outcome"]]
            return _fill(options[_pick(candidate, len(options))], f)
        if kind == "transition":
            ending = TRANSITION_FAN_OUTCOME_EN.get(f["outcome"], "")
            options = TRANSITION_FAN_SAME_EN if f["same"] else TRANSITION_FAN_EN
            return _fill(options[_pick(candidate, len(options))], f) + ending
        options = STATE_FAN[kind]
        return _fill(options[_pick(candidate, len(options))][0], f)

    @staticmethod
    def _chinese_copy(candidate: InsightCandidate, analyst: bool) -> tuple[str, str]:
        f = candidate.facts
        kind = candidate.kind
        if "zh_detail" in f:
            return f["zh_title"], f.get("zh_fan_detail", f["zh_detail"]) if not analyst else f["zh_detail"]
        if not _required(candidate, REQUIRED_FACTS.get(kind)):
            return candidate.headline, candidate.explanation or candidate.headline
        if kind == "goal":
            headline = _fill(GOAL_HEADLINE[f["state"]][1], f)
            if analyst:
                body = f"{f['p']} 把握住了 {f['xg']} xG 的机会。"
                if f["share_value"] >= 55:
                    body += f"近 {WINDOW_MINUTES} 分钟 {f['team']} 的势头占比 {f['share']}。"
                elif f["share_value"] <= 45:
                    body += f"这球有些逆势：近 {WINDOW_MINUTES} 分钟 {f['other']} 的势头占比是 {f['other_share']}。"
                if f["scorer_goals"] in CHINESE_NUMERALS:
                    body += f"这是 {f['p']} 本场第{CHINESE_NUMERALS[f['scorer_goals']]}个进球。"
            else:
                options = GOAL_FAN_ZH[f["tier"]]
                body = _fill(options[_pick(candidate, len(options))], f)
                if f["share_value"] <= 45:
                    body += f"此前几分钟一直是 {f['other']} 占优。"
                body += f"比分 {f['home']} {f['score']} {f['away']}。"
            return headline, body
        if kind == "chance":
            options = CHANCE_HEADLINE[f["outcome"]][1]
            headline = _fill(options[_pick(candidate, len(options))], f)
            result = SHOT_OUTCOME_ZH[f["outcome"]]
            if analyst:
                body = f"{f['p']} 获得 {f['xg']} xG 的机会，射门{result}。"
                if f["share_value"] >= 60:
                    body += f"近 {WINDOW_MINUTES} 分钟 {f['team']} 的势头占比 {f['share']}。"
            else:
                options = CHANCE_FAN_ZH[f["tier"]]
                body = _fill(options[_pick(candidate, len(options))], f)
            return headline, body
        if kind == "transition":
            headline = f"{f['team']} 断球后 {f['seconds']} 秒完成射门"
            result = SHOT_OUTCOME_ZH.get(f["outcome"], "")
            if analyst:
                body = (f"{f['r']} 断球后 {f['seconds']} 秒就自己完成射门（{f['xg']} xG），{result}。" if f["same"] else
                        f"{f['r']} 断球，{f['seconds']} 秒后 {f['sh']} 完成射门（{f['xg']} xG），{result}。")
            elif f["same"]:
                body = f"{f['r']} 抢回球权，{f['seconds']} 秒后就自己起脚射门，{result}。"
            else:
                body = _fill(TRANSITION_FAN_ZH[_pick(candidate, 2)], {**f, "result": result})
            return headline, body
        if kind == "high_pressure":
            headline = f"{f['team']} 前场逼抢奏效"
            body = (_fill(PRESS_ANALYST[_pick(candidate, len(PRESS_ANALYST))][1], f)
                    if analyst else _fill(STATE_FAN[kind][_pick(candidate, 2)][1], f))
            return headline, body
        if kind == "momentum_shift":
            headline = f"场面转向 {f['leader']}"
            body = (f"此前 {WINDOW_MINUTES} 分钟由 {f['previous']} 占优，近 {WINDOW_MINUTES} 分钟 {f['leader']} 的势头占比升到 "
                    f"{f['share']}，进攻三区动作 {f['al']} 对 {f['at']}。"
                    if analyst else _fill(STATE_FAN[kind][_pick(candidate, 3)][1], f))
            return headline, body
        headline = f"{f['leader']} 占据场面优势"
        body = (f"近 {WINDOW_MINUTES} 分钟 {f['leader']} 的势头占比 {f['share']}，进攻三区动作 {f['al']} 对 {f['at']}，"
                f"比赛基本压在 {f['trailer']} 半场。"
                if analyst else _fill(STATE_FAN["control"][_pick(candidate, 3)][1], f))
        return headline, body


# --------------------------------------------------------------------------- recap

OUTCOME_RECAP_EN = {"saved": "the keeper kept it out", "blocked": "it was blocked", "failed": "it went off target"}
OUTCOME_RECAP_ZH = {"saved": "被门将扑出", "blocked": "被封堵", "failed": "射偏了"}


def _clock_minute(clock: str) -> int:
    return int(clock.split(":")[0])


def _cite(item: dict[str, Any]) -> str:
    return f" [{item['event_id']}]"


def goal_story(goals: list[dict[str, Any]], home: str, away: str) -> list[dict[str, Any]]:
    """Annotate full-time goals with the game state each one created."""
    tally = {home: 0, away: 0}
    trailed = {home: False, away: False}
    scorer_count: dict[str, int] = {}
    rows = []
    for goal in goals:
        team = goal["team"]
        other = away if team == home else home
        before, against = tally[team], tally[other]
        tally[team] += 1
        margin = tally[team] - against
        if before == against:
            state = "opener" if before == 0 else "comeback" if trailed[team] else "again"
        elif margin == 0:
            state = "level"
        elif before > against:
            state = "double" if margin == 2 else "extend"
        else:
            state = "back"
        for side in (home, away):
            trailed[side] |= tally[side] < tally[away if side == home else home]
        scorer_count[goal["player"]] = scorer_count.get(goal["player"], 0) + 1
        rows.append({**goal, "state": state, "margin": margin, "nth": scorer_count[goal["player"]],
                     "minute_index": _clock_minute(goal["minute"]),
                     "broadcast_minute": minute_mark(_clock_minute(goal["minute"]))})
    return rows


def _decisive_index(rows: list[dict[str, Any]], winner: str | None, loser_goals: int) -> int | None:
    """The winner's goal that put them ahead for good: their (loser goals + 1)th goal."""
    if winner is None:
        return None
    count = 0
    for i, row in enumerate(rows):
        if row["team"] == winner:
            count += 1
            if count == loser_goals + 1:
                return i
    return None


class RecapAgent:
    """Full-time facts, a deterministic match report, and validation for model-written recaps."""

    name = "Recap Agent"

    def facts(self, events: list[MatchEvent], snapshot: MatchSnapshot) -> dict[str, Any]:
        goals = [e for e in events if e.event_type == EventType.SHOT and e.outcome == "goal"]
        chances = sorted([e for e in events if e.event_type == EventType.SHOT and e.outcome != "goal"],
                         key=lambda e: float(e.attributes.get("xg", 0)), reverse=True)[:3]
        home, away = snapshot.home_team, snapshot.away_team
        teams = (home, away)

        def chance(e: MatchEvent) -> dict[str, Any]:
            return {"minute": e.clock, "broadcast_minute": minute_mark(e.minute), "team": e.team_id,
                    "player": e.player_name, "xg": float(e.attributes.get("xg", 0)), "outcome": e.outcome,
                    "event_id": e.event_id}

        story = goal_story([{"minute": e.clock, "team": e.team_id, "player": e.player_name,
                             "xg": float(e.attributes.get("xg", 0)), "event_id": e.event_id} for e in goals], home, away)
        result = {
            "match_id": snapshot.match_id,
            "home_team": home,
            "away_team": away,
            "score": {home: snapshot.score_home, away: snapshot.score_away},
            "goals": story,
            "key_chances": [chance(e) for e in chances],
            "team_stats": {t: {"xg": snapshot.metrics[t].xg, "shots": snapshot.metrics[t].shots,
                               "shots_on_target": snapshot.metrics[t].shots_on_target,
                               "possession_pct": snapshot.metrics[t].possession_pct,
                               "pass_accuracy": snapshot.metrics[t].pass_accuracy} for t in teams},
            "chasing": None,
        }
        h, a = snapshot.score_home, snapshot.score_away
        winner, loser = (home, away) if h > a else (away, home) if a > h else (None, None)
        decisive = _decisive_index(story, winner, min(h, a))
        if decisive is not None:
            winning_goal = goals[decisive]
            after = [e for e in events if e.event_type == EventType.SHOT and e.team_id == loser
                     and e.sequence > winning_goal.sequence]
            big = sorted((e for e in after if e.outcome != "goal" and float(e.attributes.get("xg", 0)) >= 0.30),
                         key=lambda e: float(e.attributes.get("xg", 0)), reverse=True)[:2]
            result["chasing"] = {"team": loser, "after_event_id": winning_goal.event_id, "shots": len(after),
                                 "xg": round(sum(float(e.attributes.get("xg", 0)) for e in after), 2),
                                 "chances": [chance(e) for e in sorted(big, key=lambda e: e.sequence)]}
        return result

    # ---- English report

    def _goal_clause_en(self, row: dict[str, Any], decisive: bool, total_goals: int, draw: bool, last: bool,
                        first_mention: bool, same_scorer: bool) -> str:
        m = row["minute_index"]
        mark = row["broadcast_minute"]
        short = surname(row["player"])
        name = row["player"] if first_mention else short
        team = short_club(row["team"])
        when = (" just before half-time" if 40 <= m <= 44 else " just after the break" if 45 <= m <= 49
                else f" in the {ordinal(mark)} minute" if m >= 85 else f" on {mark}'")
        tier = chance_tier(row["xg"])
        quality = ", out of almost nothing" if tier == 3 else ", making the most of a half-chance" if tier == 2 else ""
        state = row["state"]
        if row["nth"] == 3:  # a hat-trick is the headline of that goal, whatever it did to the score
            tail = {"comeback": f" to turn it round on {mark}'", "level": f" with the equaliser{when}",
                    "again": f" to put {team} back in front on {mark}'", "back": f" to pull one back{when}"}
            clause = f"{short} completed a hat-trick{tail.get(state, when)}"
            return clause + (", and it proved to be the winner" if decisive and total_goals > 1 else "")
        if decisive:
            if total_goals == 1:
                return f"{name} scored the only goal{when}{quality}"
            if row["nth"] == 2 and not same_scorer:
                return f"{short} scored again{when} to win it"
            if same_scorer:
                name = f"{short} then"
            return f"{name} won it on {mark}'{quality}" if m < 85 else f"{name} won it{when}{quality}"
        if same_scorer:
            name = f"{short} then"
        if state == "opener":
            return f"{name} put {team} ahead on {mark}'"
        if state == "comeback":
            return f"{name} turned it round on {mark}'"
        if state == "again":
            return f"{name} put {team} back in front on {mark}'"
        if state == "level":
            return f"{name} rescued a point{when}" if draw and last and m >= 80 else f"{name} levelled{when}"
        if state == "double":
            return f"{name} doubled the lead{when}"
        if state == "extend":
            return f"{name} added another{when}"
        return f"{name} pulled one back{when}"

    def _story_en(self, facts: dict[str, Any]) -> str:
        home, away = facts["home_team"], facts["away_team"]
        h, a = facts["score"][home], facts["score"][away]
        rows = facts["goals"]
        stats = facts["team_stats"]
        draw = h == a
        winner, loser = (None, None) if draw else ((home, away) if h > a else (away, home))
        w, l = max(h, a), min(h, a)
        decisive = _decisive_index(rows, winner, l)
        sentences: list[str] = []

        # 1. Result and arc
        if draw:
            sentences.append(f"{home} and {away} drew {h}-{a}.")
        else:
            row = rows[decisive]
            venue = f" at {short_club(loser)}" if winner == away else f" over {loser}"
            comeback = any(r["team"] == winner and r["state"] == "comeback" for r in rows[:decisive + 1])
            if comeback:
                sentences.append(f"{winner} came from behind to win {w}-{l}{venue}.")
            elif row["minute_index"] >= 80:
                sentences.append(f"{winner} snatched a late {w}-{l} win{venue}.")
            elif w - l >= 3:
                sentences.append(f"{winner} were comfortable {w}-{l} winners{venue}.")
            else:
                sentences.append(f"{winner} beat {loser} {w}-{l}." if winner == home else
                                 f"{winner} won {w}-{l} at {short_club(loser)}.")

        # 2. Goals in order, two clauses per sentence; the winner gets a sentence of its own
        mentioned: set[str] = set()

        def clause(i: int) -> tuple[str, str]:
            row = rows[i]
            text = self._goal_clause_en(row, i == decisive, len(rows), draw, i == len(rows) - 1,
                                        row["player"] not in mentioned,
                                        i > 0 and rows[i - 1]["player"] == row["player"] and i - 1 != decisive)
            mentioned.add(row["player"])
            return text + _cite(row), row["team"]

        def group(indices: list[int]) -> None:
            for start in range(0, len(indices), 2):
                parts = [clause(i) for i in indices[start:start + 2]]
                text = parts[0][0]
                if len(parts) == 2:
                    text += (", but " if parts[1][1] != parts[0][1] else ", and ") + parts[1][0]
                sentences.append(text[0].upper() + text[1:] + ".")

        if decisive is None:
            group(list(range(len(rows))))
        else:
            group(list(range(decisive)))
            group([decisive])
            group(list(range(decisive + 1, len(rows))))

        # 3. The losing side's response, or the chance that could have won a draw
        chasing = facts.get("chasing")
        if chasing and chasing["shots"] >= 3:
            team = short_club(chasing["team"])
            aim = "equalise" if w - l == 1 else "get back into it"
            after = "after that" if decisive == len(rows) - 1 else "after the winner"
            text = f"{team} had {chasing['shots']} shots {after}"
            big = chasing["chances"]
            if len(big) == 2 and big[0]["player"] == big[1]["player"]:
                ending = ("was denied both times" if all(c["outcome"] in {"saved", "blocked"} for c in big)
                          else "could not take either")
                text += (f", and {big[0]['player']} had two big chances to {aim}, on {big[0]['broadcast_minute']}' "
                         f"and {big[1]['broadcast_minute']}', but {ending}{_cite(big[0])}{_cite(big[1])}")
            elif big:
                best = max(big, key=lambda c: c["xg"])
                verb = "equalised" if aim == "equalise" else "scored"
                text += (f", and {best['player']} should have {verb} on {best['broadcast_minute']}', "
                         f"but {OUTCOME_RECAP_EN[best['outcome']]}{_cite(best)}")
            else:
                text += " but could not find a way back"
            sentences.append(text + ".")
        elif chasing and decisive is not None and rows[decisive]["minute_index"] < 80:
            sentences.append(f"{short_club(winner)} saw it out without much trouble.")
        elif draw and facts["key_chances"]:
            best = facts["key_chances"][0]
            lead = "had the best chance to win it" if h else "had the best chance of the match"
            sentences.append(f"{best['player']} {lead} on {best['broadcast_minute']}', "
                             f"but {OUTCOME_RECAP_EN.get(best['outcome'], 'it did not go in')}{_cite(best)}.")

        # 4. One line of numbers, with a point of view
        if draw:
            hx, ax = stats[home]["xg"], stats[away]["xg"]
            if abs(hx - ax) < 0.3:
                sentences.append(f"There was little between them: {hx:.2f} xG to {ax:.2f}.")
            else:
                better, worse = (home, away) if hx > ax else (away, home)
                sentences.append(f"{short_club(better)} created more, {stats[better]['xg']:.2f} xG to "
                                 f"{stats[worse]['xg']:.2f}, but could not find a winner.")
        else:
            wx, lx = stats[winner]["xg"], stats[loser]["xg"]
            ws, ls = short_club(winner), short_club(loser)
            if lx - wx >= 1.0:
                sentences.append(f"{ls} created far more, {lx:.2f} xG to {wx:.2f} from {stats[loser]['shots']} shots "
                                 f"to {stats[winner]['shots']}, and still lost.")
            elif lx > wx:
                sentences.append(f"{ls} edged the chances, {lx:.2f} xG to {wx:.2f}, but {ws} were more clinical.")
            elif wx - lx >= 1.0:
                sentences.append(f"{ws} deserved it: {wx:.2f} xG to {lx:.2f}.")
            else:
                sentences.append(f"{ws} shaded the chances as well, {wx:.2f} xG to {lx:.2f}.")
        return " ".join(sentences)

    # ---- Chinese report

    def _goal_clause_zh(self, row: dict[str, Any], decisive: bool, total_goals: int, draw: bool, last: bool,
                        first_mention: bool = True, again: bool = False) -> str:
        m = row["minute_index"]
        mark = f"第{row['broadcast_minute']}分钟"
        when = "在上半场结束前" if 40 <= m <= 44 else "在下半场开始不久" if 45 <= m <= 49 else f"在{mark}"
        player = row["player"] if first_mention else surname(row["player"])
        team = row["team"]
        tier = chance_tier(row["xg"])
        quality = "几乎凭空" if tier == 3 else "抓住半个机会" if tier == 2 else ""
        state = row["state"]
        hat = "，上演帽子戏法" if row["nth"] == 3 else "，梅开二度" if row["nth"] == 2 else ""
        if decisive:
            if total_goals == 1:
                return f"{mark}，{player} {quality}打进全场唯一的进球"
            if state == "comeback":
                return f"{mark}，{player} {quality}完成反超{hat}，这粒进球成为制胜球"
            return f"{mark}，{player} {quality}打进制胜球{hat}"
        # "Wren 又在第55分钟扳平比分": a second clause for the same scorer reads as one run of play.
        lead = f"{player} 又" if again else f"{player} "
        if state == "opener":
            text = f"{player} {mark}的进球让 {team} 取得领先"
        elif state == "comeback":
            text = f"{lead}在{mark}帮助 {team} 反超比分"
        elif state == "again":
            text = f"{lead}在{mark}帮助 {team} 再次领先"
        elif state == "level":
            text = f"{lead}{when}扳平比分" + (f"，为 {team} 抢回一分" if draw and last and m >= 80 else "")
        elif state == "double":
            text = f"{lead}{when}把领先优势扩大到两球"
        elif state == "extend":
            text = f"{lead}{when}再下一城"
        else:
            text = f"{lead}{when}扳回一球"
        return text + hat

    def _story_zh(self, facts: dict[str, Any]) -> str:
        home, away = facts["home_team"], facts["away_team"]
        h, a = facts["score"][home], facts["score"][away]
        rows = facts["goals"]
        stats = facts["team_stats"]
        draw = h == a
        winner, loser = (None, None) if draw else ((home, away) if h > a else (away, home))
        w, l = max(h, a), min(h, a)
        decisive = _decisive_index(rows, winner, l)
        sentences: list[str] = []

        if draw:
            sentences.append(f"{home} 与 {away} 0-0 战平，双方都没有进球。" if h == 0 else f"{home} 与 {away} {h}-{a} 战平。")
        else:
            row = rows[decisive]
            venue = "客场" if winner == away else "主场"
            comeback = any(r["team"] == winner and r["state"] == "comeback" for r in rows[:decisive + 1])
            if comeback:
                sentences.append(f"{winner} {venue} {w}-{l} 逆转 {loser}。")
            elif row["minute_index"] >= 80:
                sentences.append(f"{winner} {venue} {w}-{l} 绝杀 {loser}。")
            elif w - l >= 3:
                sentences.append(f"{winner} {venue} {w}-{l} 大胜 {loser}。")
            else:
                sentences.append(f"{winner} {venue} {w}-{l} 击败 {loser}。")

        mentioned: set[str] = set()

        def clause(i: int) -> str:
            row = rows[i]
            again = i > 0 and rows[i - 1]["player"] == row["player"] and i - 1 != decisive
            text = self._goal_clause_zh(row, i == decisive, len(rows), draw, i == len(rows) - 1,
                                        row["player"] not in mentioned, again)
            mentioned.add(row["player"])
            return text + _cite(row)

        def group(indices: list[int]) -> None:
            for start in range(0, len(indices), 2):
                sentences.append("，".join(clause(i) for i in indices[start:start + 2]) + "。")

        if decisive is None:
            group(list(range(len(rows))))
        else:
            group(list(range(decisive)))
            group([decisive])
            group(list(range(decisive + 1, len(rows))))

        chasing = facts.get("chasing")
        if chasing and chasing["shots"] >= 3:
            team = chasing["team"]
            level = w - l == 1
            after = "此后" if decisive == len(rows) - 1 else "制胜球之后，"
            text = f"{after} {team} 又射门 {chasing['shots']} 次".replace("， ", "，")
            big = chasing["chances"]
            if len(big) == 2 and big[0]["player"] == big[1]["player"]:
                ending = ("都被门将扑出" if all(c["outcome"] == "saved" for c in big)
                          else "都没能打进")
                text += (f"，{big[0]['player']} 在第{big[0]['broadcast_minute']}分钟和第{big[1]['broadcast_minute']}分钟"
                         f"两次获得绝佳机会，{ending}{_cite(big[0])}{_cite(big[1])}")
            elif big:
                best = max(big, key=lambda c: c["xg"])
                aim = "扳平的绝佳机会" if level else "绝佳机会"
                text += (f"，{best['player']} 第{best['broadcast_minute']}分钟获得{aim}，"
                         f"可惜{OUTCOME_RECAP_ZH[best['outcome']]}{_cite(best)}")
            else:
                text += "，但始终没能扳平" if level else "，但没能缩小差距"
            sentences.append(text + "。")
        elif chasing and decisive is not None and rows[decisive]["minute_index"] < 80:
            sentences.append(f"{winner} 此后稳住了局面。")
        elif draw and facts["key_chances"]:
            best = facts["key_chances"][0]
            sentences.append(f"全场最好的机会属于 {best['player']}（第{best['broadcast_minute']}分钟），"
                             f"可惜{OUTCOME_RECAP_ZH.get(best['outcome'], '没能打进')}{_cite(best)}。")

        if draw:
            hx, ax = stats[home]["xg"], stats[away]["xg"]
            if abs(hx - ax) < 0.3:
                sentences.append(f"双方预期进球 {hx:.2f} 比 {ax:.2f}，几乎不分高下。")
            else:
                better, worse = (home, away) if hx > ax else (away, home)
                sentences.append(f"{better} 的预期进球以 {stats[better]['xg']:.2f} 比 {stats[worse]['xg']:.2f} 占优，"
                                 f"但没能打进制胜球。")
        else:
            wx, lx = stats[winner]["xg"], stats[loser]["xg"]
            if lx - wx >= 1.0:
                sentences.append(f"{loser} 的预期进球以 {lx:.2f} 比 {wx:.2f} 遥遥领先，射门 {stats[loser]['shots']} 比 "
                                 f"{stats[winner]['shots']}，最终却输掉了比赛。")
            elif lx > wx:
                sentences.append(f"{loser} 的预期进球以 {lx:.2f} 比 {wx:.2f} 略占上风，但 {winner} 把握机会更好。")
            elif wx - lx >= 1.0:
                sentences.append(f"{winner} 赢得名副其实，预期进球 {wx:.2f} 比 {lx:.2f}。")
            else:
                sentences.append(f"{winner} 的预期进球也以 {wx:.2f} 比 {lx:.2f} 稍占优势。")
        return "".join(sentences)

    def summary(self, facts: dict[str, Any], language: str = "en") -> str:
        return self._story_zh(facts) if language == "zh" else self._story_en(facts)

    def validate(self, text: str, facts: dict[str, Any], snapshot: MatchSnapshot) -> tuple[bool, str]:
        if not text or not text.strip():
            return False, "empty recap"
        index = event_lookup(snapshot)
        cited = set(EVENT_ID_RE.findall(text))
        if not cited:
            return False, "recap cites no event ids"
        if cited - index.keys():
            return False, f"recap cites unknown event ids: {', '.join(sorted(cited - index.keys())[:5])}"
        names = known_names(snapshot)
        score = tuple(str(v) for v in facts["score"].values())
        bad_scores = sorted({f"{a}-{b}" for a, b in SCORELINE_RE.findall(strip_names(EVENT_ID_RE.sub(" ", text), names))
                             if (a, b) not in (score, score[::-1])})
        if bad_scores:
            return False, f"recap scorelines contradict the result: {', '.join(bad_scores)}"
        chasing = facts.get("chasing") or {}
        items = facts["goals"] + facts["key_chances"] + list(chasing.get("chances", []))
        allowed = {"90", str(len(facts["goals"]))}
        for value in facts["score"].values():
            allowed.add(str(value))
        for item in items:
            minute, second = (int(part) for part in item["minute"].split(":"))
            allowed.update({str(minute), str(minute + 1), str(second), _canon(str(item["xg"])),
                            _canon(str(round(item["xg"] * 100)))})
        for stats in facts["team_stats"].values():
            for value in stats.values():
                allowed.update({_canon(str(value)), _canon(str(round(value)))})
        for key in ("shots", "xg"):
            if key in chasing:
                allowed.update({_canon(str(chasing[key])), _canon(str(round(chasing[key])))})
        bad = sorted(numbers_in(EVENT_ID_RE.sub(" ", text), names) - allowed, key=float)
        if bad:
            return False, f"recap numbers not in facts: {', '.join(bad)}"
        all_players = {e.player_name for e in index.values()}
        allowed_players = {item["player"] for item in items}
        unsupported = sorted(p for p in players_mentioned(text, names, all_players)
                             if p not in allowed_players and not any(n.endswith(p) for n in allowed_players))
        if unsupported:
            return False, f"recap names players outside the facts: {', '.join(unsupported)}"
        phrase = banned_phrase(text)
        if phrase:
            return False, f"banned phrase: {phrase}"
        return True, "recap verified"

    def run(self, events: list[MatchEvent], snapshot: MatchSnapshot, language: str = "en") -> dict[str, Any]:
        facts = self.facts(events, snapshot)
        return {**facts, "summary": self.summary(facts, language), "summary_provider": "local",
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
        self.kind_counts: dict[str, int] = {}  # cards shown per type, to rotate phrasings
        self._analysis_cache: dict[tuple[str, int, int], AnalysisResult] = {}

    def _rejected(self, reason: str, started: float | None = None) -> AgentTrace:
        return _trace(self.checker.name, started or time.perf_counter(), f"Rejected: {reason}", "rejected",
                      zh=f"退回：{reason_zh(reason)}")

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
                traces.append(_trace("Player Scout", time.perf_counter(),
                                     "Your player was just involved, so their moment goes on screen instead.",
                                     zh="你关注的球员刚有参与，改为展示这一时刻。"))
        if candidate is None:
            return self._remember(key, AnalysisResult(None, traces, False))

        self._assign_variant(candidate)
        provider = self._explain(candidate, snapshot, state, traces, revision_note=None)
        verified, reason = self.checker.run(candidate, state)
        while not verified and state.retries < MAX_REVISIONS:
            traces.append(self._rejected(reason))
            state.retries += 1
            state.handoffs.append(f"evidence_guardian -> moment_explainer revision {state.retries}")
            provider = self._explain(candidate, snapshot, state, traces, revision_note=reason, provider=provider)
            verified, reason = self.checker.run(candidate, state)

        if not verified:
            traces.append(self._rejected(reason))
            traces.append(_trace(self.checker.name, time.perf_counter(),
                                 f"Still failing after {MAX_REVISIONS} rewrites; using the local template.", "fallback",
                                 zh=f"重写 {MAX_REVISIONS} 次仍未通过，改用本地模板。"))
            candidate, trace = self.explainer.run(candidate, state)
            traces.append(trace)
            provider = "local"
            verified, reason = self.checker.run(candidate, state)
        traces.append(_trace(self.checker.name, time.perf_counter(), reason, zh=self.checker.passed_zh(reason))
                      if verified else self._rejected(reason))

        if verified:
            self._commit(candidate, state)
        return self._remember(key, AnalysisResult(candidate, traces, verified, provider if verified else "local"))

    def _explain(self, candidate: InsightCandidate, snapshot: MatchSnapshot, state: SharedMatchState,
                 traces: list[AgentTrace], revision_note: str | None, provider: str | None = None) -> str:
        use_llm = self.narrative_service is not None and provider in (None, "foundry", "deepseek")
        if use_llm:
            started = time.perf_counter()
            name = _model_name(self.narrative_service)
            service_provider = getattr(self.narrative_service, "provider", "foundry")
            try:
                candidate.explanation = self.narrative_service.explain(candidate, snapshot, revision_note)
                if revision_note:
                    summary = f"{name} rewrote the line after the Guardian flagged: {revision_note}"
                    summary_zh = f"{name} 按校验意见重写：{reason_zh(revision_note)}"
                else:
                    summary, summary_zh = f"{name} wrote the line.", f"由 {name} 撰写文案。"
                traces.append(_trace(self.explainer.name, started, summary, "revised" if revision_note else "completed",
                                     service_provider, zh=summary_zh))
                return service_provider
            except Exception as exc:  # network, auth, timeout, malformed output
                traces.append(_trace(self.explainer.name, started,
                                     f"Model call failed ({type(exc).__name__}); the local template takes over.",
                                     "fallback", service_provider,
                                     zh=f"模型调用失败（{type(exc).__name__}），改用本地模板。"))
        candidate, trace = self.explainer.run(candidate, state, revision_note=revision_note)
        traces.append(trace)
        return "local"

    def _assign_variant(self, candidate: InsightCandidate) -> None:
        number = self.kind_counts.get(candidate.kind, 0) if self.stateful else variant(candidate.insight_id, 997)
        candidate.facts["variant"] = number
        headlines = candidate.facts.get("headline_options")
        if headlines:
            candidate.headline = _fill(headlines[number % len(headlines)], candidate.facts)
        options = candidate.facts.get("options")
        if options:  # enrichment cards carry whole alternative phrasings
            title, zh_title, detail, zh_detail = options[number % len(options)]
            candidate.headline = title
            candidate.facts.update(zh_title=zh_title, detail=detail, zh_detail=zh_detail)

    def _commit(self, candidate: InsightCandidate, state: SharedMatchState) -> None:
        if not self.stateful:
            return
        self.kind_counts[candidate.kind] = self.kind_counts.get(candidate.kind, 0) + 1
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
            name = _model_name(self.narrative_service)
            service_provider = getattr(self.narrative_service, "provider", "foundry")
            try:
                localized = self.narrative_service.localize(candidate, profile)
                problem = self.checker.check_localized(localized, candidate, snapshot)
                if not problem and profile.mode == "fan" and re.search(r"\d\.\d", f"{localized['headline']} {localized['body']}"):
                    problem = "fan copy uses decimals"  # house style: fans get chance quality in words
                if not problem and profile.language == "zh":
                    from app.language import chinese_copy_problem
                    names = [snapshot.home_team, snapshot.away_team] + [e.player_name for e in event_lookup(snapshot).values()]
                    problem = chinese_copy_problem(" ".join((localized["headline"], localized["body"])), names)
                if problem:
                    traces.append(_trace(self.checker.name, started,
                                         f"Rejected the model's card copy: {problem}. Using the local version.", "rejected",
                                         zh=f"模型写的卡片文案未通过：{reason_zh(problem)}。改用本地版本。"))
                    localized, provider = None, "local"
                else:
                    provider = service_provider
                    mode_zh = "分析师" if profile.mode == "analyst" else "球迷"
                    traces.append(_trace(self.director.name, started,
                                         f"{name} wrote the {profile.mode} copy; the Guardian checked it.",
                                         provider=service_provider, zh=f"{name} 撰写{mode_zh}文案，已通过校验。"))
            except Exception as exc:
                localized, provider = None, "local"
                traces.append(_trace(self.director.name, started,
                                     f"Model call failed ({type(exc).__name__}); using the local copy.",
                                     "fallback", service_provider,
                                     zh=f"模型调用失败（{type(exc).__name__}），改用本地文案。"))
        card, trace = self.director.run(candidate, True, profile, snapshot, provider, localized)
        traces.append(trace)
        return card, traces

    def run(self, snapshot: MatchSnapshot, profile: AudienceProfile) -> tuple[OverlayCard | None, list[AgentTrace]]:
        return self.present(self.analyze(snapshot, profile.favorite_player), snapshot, profile)

    def recap(self, events: list[MatchEvent], snapshot: MatchSnapshot, language: str = "en") -> dict[str, Any]:
        result = self.recap_agent.run(events, snapshot, language)
        result["language"] = language
        if self.narrative_service is None:
            return result
        facts = {k: result[k] for k in ("home_team", "away_team", "score", "goals", "key_chances", "team_stats", "chasing")}
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
        card.body = ("细节未能通过核对，只显示标题。" if profile.language == "zh"
                     else "The details could not be checked, so only the headline is shown.")
        card.provider = "local"
        return card
