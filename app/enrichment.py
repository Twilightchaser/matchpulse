"""Deterministic evidence builders for milestones, phase changes and player stories.

Copy follows docs/WRITING_STYLE.md: say what happened in football terms, keep caveats about the
method in the docs, not on the broadcast graphic.
"""
from app.house_style import short_club, surname
from app.models import EventType, Evidence, InsightCandidate

OUTCOME_EN = {"saved": ", but it's saved", "blocked": ", but it's blocked", "failed": ", but it's off target",
              "goal": " and scores"}
OUTCOME_ZH = {"saved": "被门将扑出", "blocked": "被封堵", "failed": "偏出了", "goal": "球进了"}
CHINESE_NUMERALS = {2: "二", 3: "三", 4: "四", 5: "五"}
MIN_TEMPO_CHANGE = 5
TEMPO_COPY = {  # phase -> (headline, Chinese headline, detail, Chinese detail) phrasings
    "chaotic": [
        ("The game has opened up", "比赛变得开放",
         "The ball is changing hands all the time: {cur} turnovers in five minutes, up from {prev}.",
         "球权来回易手：近 5 分钟转换 {cur} 次，之前是 {prev} 次。"),
        ("End to end now", "攻防转换加快",
         "Neither side can keep the ball: {cur} turnovers in five minutes, up from {prev}.",
         "双方都很难控住球：近 5 分钟球权转换 {cur} 次，之前是 {prev} 次。"),
    ],
    "controlled": [
        ("The game has settled down", "比赛节奏慢了下来",
         "Fewer turnovers now: {cur} in five minutes, down from {prev}.",
         "球权转换明显减少：近 5 分钟 {cur} 次，之前是 {prev} 次。"),
        ("A calmer spell", "场面趋于平稳",
         "Both sides are keeping the ball longer: {cur} turnovers in five minutes, down from {prev}.",
         "双方控球时间变长：近 5 分钟球权转换 {cur} 次，之前是 {prev} 次。"),
    ],
}


def event_candidate(snapshot, event, kind, title, zh_title, detail, zh_detail, evidence, importance=.86):
    return InsightCandidate(insight_id=f"insight-{snapshot.match_id}-{kind}-{event.event_id}",
        team_id=event.team_id, minute=event.minute, kind=kind, importance=importance,
        headline=title, explanation="", trigger_event_id=event.event_id, evidence=evidence,
        facts={"detail": detail, "zh_detail": zh_detail, "zh_title": zh_title})


def _names(snapshot):
    return {p.player_name for p in snapshot.players.values()} | {e.player_name for e in snapshot.event_index.values()}


def extra_candidates(snapshot):
    events=sorted(snapshot.event_index.values(), key=lambda e:e.sequence)
    names=_names(snapshot)
    found=[]
    for e in events:
        if snapshot.match_second-e.match_second > 20 or e.event_type != EventType.SHOT:
            continue
        shots=[x for x in events if x.sequence<=e.sequence and x.event_type==EventType.SHOT and x.team_id==e.team_id]
        on_target=[x for x in shots if x.outcome in {"goal","saved"}]
        goals=[x for x in shots if x.player_id==e.player_id and x.outcome=="goal"]
        team_s, p = short_club(e.team_id), surname(e.player_name, names)
        title=zh=None
        if e.outcome in {"goal","saved"} and len(on_target)==1:
            title=f"{team_s}'s first shot on target"; zh=f"{e.team_id} 首次射正"
            detail=f"{e.player_name} forces a save." if e.outcome=="saved" else f"{e.player_name} scores with it."
            zh_detail=f"{e.player_name} 的射门被门将扑出。" if e.outcome=="saved" else f"{e.player_name} 这脚射门直接破门。"
            evidence=[Evidence(label="Milestone",value="1 shot on target",event_ids=[e.event_id])]
        elif e.outcome=="goal" and len(goals)>=2:
            count=len(goals)
            title=f"{e.player_name} scores again"
            zh={2:f"{e.player_name} 梅开二度",3:f"{e.player_name} 上演帽子戏法"}.get(count,f"{e.player_name} 本场第{CHINESE_NUMERALS.get(count,count)}球")
            detail=f"That's {count} goals in this match for {p}."
            zh_detail=f"{e.player_name} 本场已经打进 {count} 球。"
            evidence=[Evidence(label="Scorer goals this match",value=f"{count} goals",event_ids=[x.event_id for x in goals])]
        elif len(shots)%10==0:
            other=snapshot.away_team if e.team_id==snapshot.home_team else snapshot.home_team
            theirs=[x for x in events if x.sequence<=e.sequence and x.event_type==EventType.SHOT and x.team_id==other]
            n,m=len(shots),len(theirs)
            title=f"{team_s} reach {n} shots"; zh=f"{e.team_id} 本场射门达到 {n} 次"
            if m==0:
                detail=f"{short_club(other)} are still waiting for their first."
                zh_detail=f"对手 {other} 还一脚射门都没有。"
            else:
                detail=f"That's {n} to {short_club(other)}'s {m}."
                zh_detail=f"对手 {other} {'只有' if m<n else '也有'} {m} 次射门。"
            evidence=[Evidence(label="Shots",value=f"{n} vs {m}",event_ids=[x.event_id for x in shots+theirs])]
        if title:
            found.append(event_candidate(snapshot,e,"milestone",title,zh,detail,zh_detail,evidence,
                                         .99 if e.outcome=="goal" else .87))
    # Compare adjacent five-minute windows using the exact phase definition in MetricsEngine.
    if snapshot.match_second>=300 and events:
        now=snapshot.match_second
        def changes(end):
            window=[e for e in events if end-300<e.match_second<=end]
            return [b for a,b in zip(window,window[1:]) if (a.possession_team_id or a.team_id)!=(b.possession_team_id or b.team_id)]
        current,previous=changes(now),changes(now-60)
        from app.calibration import phase_for
        def window(end):
            return [e for e in events if end-300<e.match_second<=end]
        before,after=phase_for(window(now-60)),phase_for(window(now))
        cur,prev=len(current),len(previous)
        # A phase label can flip on a change of one or two turnovers; only a real change is a story.
        if before!=after and after in {"chaotic","controlled"} and current and abs(cur-prev)>=MIN_TEMPO_CHANGE:
            e=current[-1]
            if now-e.match_second <=20:
                options=[tuple(part.format(cur=cur,prev=prev) for part in row) for row in TEMPO_COPY[after]]
                title,zh_title,detail,zh_detail=options[0]
                candidate=event_candidate(snapshot,e,"rhythm",title,zh_title,detail,zh_detail,
                    [Evidence(label="Possession changes: previous / current 5 min",value=f"{prev} / {cur}",event_ids=[x.event_id for x in previous+current])],.78)
                candidate.facts["options"]=options  # the workflow rotates phrasings across a match
                found.append(candidate)
    return found


def player_candidate(snapshot, player_id):
    events=sorted(snapshot.event_index.values(),key=lambda e:e.sequence)
    names=_names(snapshot)
    for e in reversed(events):
        if snapshot.match_second-e.match_second>15:
            break
        if e.player_id!=player_id:
            continue
        significant=(e.event_type==EventType.SHOT or e.event_type==EventType.TACKLE and e.outcome=="success"
                     or e.event_type==EventType.PASS and e.outcome=="success" and e.attributes.get("progressive"))
        if not significant:
            continue
        name, p = e.player_name, surname(e.player_name, names)
        if e.event_type==EventType.SHOT:
            action="shot"
            title=f"{name} tests the keeper" if e.outcome=="saved" else f"{name} has a go"
            zh_title=f"{name} 射门考验门将" if e.outcome=="saved" else f"{name} 起脚射门"
            detail=f"{p} gets a shot away{OUTCOME_EN.get(e.outcome, '')}."
            zh_detail=f"{name} 起脚射门，{OUTCOME_ZH.get(e.outcome, '没有打进')}。"
            value=f"{name} · {float(e.attributes.get('xg',0)):.2f} xG"
        elif e.event_type==EventType.TACKLE:
            action="tackle"
            title=f"{name} wins it back"; zh_title=f"{name} 抢断成功"
            detail=f"{p} steps in and takes the ball off an opponent."
            zh_detail=f"{name} 果断上抢，把球断了下来。"
            value=f"{name} · won"
        else:
            action="progressive pass"
            title=f"{name} moves it forward"; zh_title=f"{name} 向前推进"
            detail=f"{p} finds a teammate further up the pitch."
            zh_detail=f"{name} 一脚向前传球，把进攻推了上去。"
            value=f"{name} · completed"
        return event_candidate(snapshot,e,"player_moment",title,zh_title,detail,zh_detail,
            [Evidence(label=action,value=value,event_ids=[e.event_id])],.91)
    return None
