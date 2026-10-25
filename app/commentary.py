"""Match commentary for the event feed: one line per moment a viewer would notice.

Routine passes, pressure, throw-ins and goal kicks are left out; ~2,800 raw events become about
eighty lines. Every line is built from one event (or a pass and the shot it set up), so it needs no
model call and no fact check. Analyst mode adds a short data line under the sentence.
"""

from __future__ import annotations

import math
from typing import Any

from app.house_style import short_club, surname
from app.models import EventType, MatchEvent

HALF_TIME_SECOND = 45 * 60
FULL_TIME_SECOND = 90 * 60
BIG_CHANCE_XG = 0.30
HIGH_REGAIN_X = 70          # final third of the team that wins the ball back
ASSIST_WINDOW_SECONDS = 12  # a pass this close before a shot, to the shooter, set it up

# Phrasings rotate through each list as the match goes on, so the feed does not repeat itself.
PASS_EN = {  # how the creator found the shooter
    "through": ("{c} slips {s} through",),
    "lofted": ("{c} lifts it to {s}",),
    "long": ("{c} picks out {s} with a long ball",),
    "short": ("{c} lays it off to {s}", "{c} finds {s}", "{c} sets up {s}"),
}
PASS_ZH = {"through": ("{c} 直塞 {s}",), "lofted": ("{c} 挑传找到 {s}",), "long": ("{c} 长传找到 {s}",),
           "short": ("{c} 把球交给 {s}", "{c} 传给 {s}", "{c} 做球给 {s}")}
REGAIN_EN = {"tackle": ("{s} wins the ball back in the final third.", "{s} wins it high up the pitch.",
                        "Good press from {s}, who wins it back."),
             "interception": ("{s} cuts out a pass in the final third.", "{s} reads it and intercepts high up.")}
REGAIN_ZH = {"tackle": ("{s} 在前场抢断成功。", "{s} 前场逼抢得手。", "{s} 高位抢回球权。"),
             "interception": ("{s} 在前场断下传球。", "{s} 前场拦截成功。")}
GOAL_PASS_EN = {"through": "{c}'s through ball", "lofted": "{c}'s lofted pass", "long": "{c}'s long ball",
                "short": "{c}'s pass"}
GOAL_PASS_ZH = {"through": "{c} 直塞", "lofted": "{c} 挑传", "long": "{c} 长传", "short": "{c} 传球"}
SHOT_AFTER_PASS_EN = {"saved": ", and the keeper saves.", "blocked": ", but it's blocked.",
                      "failed": ", but the shot is off target."}
SHOT_ALONE_EN = {"saved": "{s} shoots, and the keeper saves.", "blocked": "{s}'s shot is blocked.",
                 "failed": "{s} shoots off target."}
SHOT_ZH = {"saved": "射门被门将扑出", "blocked": "射门被封堵", "failed": "射门偏出"}


def _goal_distance(event: MatchEvent) -> float:
    return math.hypot(105 - event.x, 34 - event.y)


def _creator(events: list[MatchEvent], index: int) -> MatchEvent | None:
    """The completed pass to the shooter just before the shot, skipping the defenders' actions."""
    shot = events[index]
    for earlier in reversed(events[:index]):
        if shot.match_second - earlier.match_second > ASSIST_WINDOW_SECONDS:
            return None
        if earlier.team_id != shot.team_id:
            continue  # pressure or a failed tackle by the defending side
        if (earlier.event_type == EventType.PASS and earlier.outcome == "success"
                and earlier.attributes.get("receiver_id") == shot.player_id and earlier.player_id != shot.player_id):
            return earlier
        return None
    return None


def _in_box(event: MatchEvent) -> bool:
    end_x, end_y = float(event.attributes.get("end_x", 0)), float(event.attributes.get("end_y", 0))
    return end_x >= 88.5 and 13.85 <= end_y <= 54.15


def build_commentary(events: list[MatchEvent], home: str, away: str, language: str = "en",
                     mode: str = "fan") -> list[dict[str, Any]]:
    events = sorted(events, key=lambda e: e.sequence)
    zh, analyst = language == "zh", mode == "analyst"
    names = {e.player_name for e in events}
    by_id = {e.player_id: e.player_name for e in events}
    short = {name: surname(name, names) for name in names}
    score = {home: 0, away: 0}
    lines: list[dict[str, Any]] = []
    used_passes: set[str] = set()
    half_time_done = False
    turns: dict[str, int] = {}

    def rotate(key: str, options: tuple[str, ...]) -> str:
        turns[key] = turns.get(key, -1) + 1
        return options[turns[key] % len(options)]

    def team_name(team: str) -> str:
        return team if zh else short_club(team)

    def scoreline() -> str:
        return (f"{home} {score[home]}-{score[away]} {away}" if zh else
                f"{short_club(home)} {score[home]}-{score[away]} {short_club(away)}")

    def add(event: MatchEvent | None, second: int, kind: str, text: str, detail: str = "", team: str | None = None) -> None:
        lines.append({"event_id": event.event_id if event else None, "second": second,
                      "clock": f"{second // 60:02d}:{second % 60:02d}", "team": team or (event.team_id if event else None),
                      "kind": kind, "text": text, "detail": detail if analyst else ""})

    def half_time() -> None:
        add(None, HALF_TIME_SECOND, "half_time",
            f"半场结束，{scoreline()}。" if zh else f"Half-time: {scoreline()}.")

    # Shots that a pass set up; the pass is folded into the shot line instead of its own line.
    creators = {}
    for i, event in enumerate(events):
        if event.event_type == EventType.SHOT:
            creator = _creator(events, i)
            if creator is not None:
                creators[event.event_id] = creator
                used_passes.add(creator.event_id)

    for event in events:
        second = event.match_second
        if not half_time_done and second >= HALF_TIME_SECOND:
            half_time()
            half_time_done = True
        s = short[event.player_name]
        kind = event.event_type
        if kind == EventType.KICKOFF:
            if second == 0:
                add(event, second, "kickoff", f"比赛开始，{home} 对 {away}。" if zh else f"Kick-off. {home} v {away}.")
            elif second == HALF_TIME_SECOND:
                add(event, second, "kickoff", "下半场开始。" if zh else "The second half is under way.")
            continue
        if kind == EventType.SHOT:
            xg = float(event.attributes.get("xg", 0))
            detail = (f"xG {xg:.2f} · {_goal_distance(event):.1f} m · "
                      f"{float(event.attributes.get('shot_speed_kmh', 0)):.0f} km/h")
            creator = creators.get(event.event_id)
            pass_type = creator.attributes.get("pass_type", "short") if creator else None
            c = short[creator.player_name] if creator else ""
            if event.outcome == "goal":
                score[event.team_id] += 1
                if zh:
                    lead = GOAL_PASS_ZH.get(pass_type, GOAL_PASS_ZH["short"]).format(c=c) + "，" if creator else ""
                    text = f"进球！{lead}{s} 破门。{scoreline()}。"
                else:
                    assist = " from " + GOAL_PASS_EN.get(pass_type, GOAL_PASS_EN["short"]).format(c=c) if creator else ""
                    text = f"Goal! {s} scores{assist}. {scoreline()}."
                add(event, second, "goal", text, detail)
                continue
            outcome = event.outcome if event.outcome in SHOT_ZH else "failed"
            big = xg >= BIG_CHANCE_XG
            if zh:
                if creator:
                    phrase = rotate("pass:" + pass_type, PASS_ZH.get(pass_type, PASS_ZH["short"]))
                    text = f"{phrase.format(c=c, s=s)}，{SHOT_ZH[outcome]}。"
                else:
                    text = f"{s} {SHOT_ZH[outcome]}。"
                text = ("绝佳机会！" if big else "") + text
            else:
                if creator:
                    phrase = rotate("pass:" + pass_type, PASS_EN.get(pass_type, PASS_EN["short"]))
                    text = phrase.format(c=c, s=s) + SHOT_AFTER_PASS_EN[outcome]
                else:
                    text = SHOT_ALONE_EN[outcome].format(s=s)
                text = ("Big chance! " if big else "") + text
            add(event, second, "big_chance" if big else "shot", text, detail)
        elif kind == EventType.CORNER:
            add(event, second, "corner", f"{event.team_id} 获得角球。" if zh else f"Corner to {team_name(event.team_id)}.")
        elif kind == EventType.OFFSIDE:
            add(event, second, "offside", f"{s} 越位。" if zh else f"{s} is caught offside.")
        elif kind in (EventType.TACKLE, EventType.POSSESSION_CHANGE) and event.outcome == "success" \
                and event.x >= HIGH_REGAIN_X:
            how = "tackle" if kind == EventType.TACKLE else "interception"
            text = rotate("regain:" + how, (REGAIN_ZH if zh else REGAIN_EN)[how]).format(s=s)
            to_goal = 105 - event.x
            add(event, second, "regain", text, f"距对方球门 {to_goal:.0f} 米" if zh else f"{to_goal:.0f} m from goal")
        elif kind == EventType.PASS and event.outcome == "success" and _in_box(event) \
                and event.event_id not in used_passes:
            receiver = by_id.get(str(event.attributes.get("receiver_id", "")), "")
            if receiver:
                r = short.get(receiver, receiver)
                text = f"{s} 把球传进禁区，找到 {r}。" if zh else f"{s} finds {r} in the box."
                add(event, second, "box_entry", text,
                    f"{float(event.attributes.get('distance_m', 0)):.1f} m · "
                    f"{float(event.attributes.get('ball_speed_kmh', 0)):.0f} km/h")
    if not half_time_done:
        half_time()
    add(None, FULL_TIME_SECOND, "full_time", f"全场结束，{scoreline()}。" if zh else f"Full-time: {scoreline()}.")
    return lines
