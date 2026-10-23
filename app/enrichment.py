"""Deterministic evidence builders for milestones, phase changes and player stories."""
from app.models import EventType, Evidence, InsightCandidate


def event_candidate(snapshot, event, kind, title, zh_title, detail, zh_detail, evidence, importance=.86):
    return InsightCandidate(insight_id=f"insight-{snapshot.match_id}-{kind}-{event.event_id}",
        team_id=event.team_id, minute=event.minute, kind=kind, importance=importance,
        headline=title, explanation="", trigger_event_id=event.event_id, evidence=evidence,
        facts={"detail": detail, "zh_detail": zh_detail, "zh_title": zh_title})


def extra_candidates(snapshot):
    events=sorted(snapshot.event_index.values(), key=lambda e:e.sequence)
    found=[]
    for e in events:
        if snapshot.match_second-e.match_second > 20 or e.event_type != EventType.SHOT:
            continue
        shots=[x for x in events if x.sequence<=e.sequence and x.event_type==EventType.SHOT and x.team_id==e.team_id]
        on_target=[x for x in shots if x.outcome in {"goal","saved"}]
        goals=[x for x in shots if x.player_id==e.player_id and x.outcome=="goal"]
        title=zh=None
        if e.outcome in {"goal","saved"} and len(on_target)==1:
            title=f"{e.team_id}: first shot on target"; zh=f"{e.team_id} 首次射正"
            detail=f"{e.player_name} records the first shot on target for {e.team_id}. The attack has now tested the goal."
            zh_detail=f"{e.player_name} 完成 {e.team_id} 本场首次射正，进攻终于直接威胁球门。"
            cited=[e];value="1 shot on target"
        elif e.outcome=="goal" and len(goals)>=2:
            title=f"{e.player_name}: {len(goals)} goals";zh=f"{e.player_name} 本场第 {len(goals)} 球"
            detail=f"{e.player_name} has scored {len(goals)} goals in this match, repeatedly turning chances into goals."
            zh_detail=f"{e.player_name} 本场已攻入 {len(goals)} 球，多次将机会转化为进球。"
            cited=goals;value=f"{len(goals)} goals"
        elif len(shots)%10==0:
            title=f"{e.team_id}: {len(shots)} shots";zh=f"{e.team_id} 本场第 {len(shots)} 次射门"
            detail=f"{e.team_id} have attempted {len(shots)} shots. Shot volume alone does not establish chance quality."
            zh_detail=f"{e.team_id} 本场已完成 {len(shots)} 次射门；次数反映进攻产出，但不代表每次机会质量。"
            cited=shots;value=f"{len(shots)} shots"
        if title:
            found.append(event_candidate(snapshot,e,"milestone",title,zh,detail,zh_detail,
                [Evidence(label="Milestone",value=value,event_ids=[x.event_id for x in cited])],.99 if e.outcome=="goal" else .87))
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
        if before!=after and after in {"chaotic","controlled"} and current:
            e=current[-1]
            if now-e.match_second <=20:
                title=f"Match rhythm: {before} to {after}"
                labels={"controlled":"受控","contested":"争夺","chaotic":"混乱"}
                detail=f"Possession changes rose from {len(previous)} to {len(current)}." if len(current)>len(previous) else f"Possession changes fell from {len(previous)} to {len(current)}."
                detail+=" These adjacent rolling windows suggest a change in stability, not a proven tactical cause."
                zh_detail=f"相邻滚动窗口的球权转换由 {len(previous)} 次变为 {len(current)} 次，提示比赛稳定性发生变化；不能仅据此断言战术原因。"
                found.append(event_candidate(snapshot,e,"rhythm",title,f"比赛节奏：{labels[before]}转为{labels[after]}",detail,zh_detail,
                    [Evidence(label="Possession changes: previous / current 5 min",value=f"{len(previous)} / {len(current)}",event_ids=[x.event_id for x in previous+current])],.78))
    return found


def player_candidate(snapshot, player_id):
    events=sorted(snapshot.event_index.values(),key=lambda e:e.sequence)
    for e in reversed(events):
        if snapshot.match_second-e.match_second>15:
            break
        if e.player_id!=player_id:
            continue
        significant=(e.event_type==EventType.SHOT or e.event_type==EventType.TACKLE and e.outcome=="success"
                     or e.event_type==EventType.PASS and e.outcome=="success" and e.attributes.get("progressive"))
        if not significant:
            continue
        action={EventType.SHOT:"shot",EventType.TACKLE:"tackle",EventType.PASS:"progressive pass"}[e.event_type]
        zh_action={EventType.SHOT:"射门",EventType.TACKLE:"成功抢断",EventType.PASS:"向前推进传球"}[e.event_type]
        value=f"{float(e.attributes.get('xg',0)):.2f} xG" if e.event_type==EventType.SHOT else f"{e.outcome}"
        return event_candidate(snapshot,e,"player_moment",f"Player watch: {e.player_name}",f"球员时刻：{e.player_name}",
            f"{e.player_name}: {action}, {e.outcome}. This is a contribution from your tracked player.",
            f"{e.player_name} 完成{zh_action}，结果：{dict(success='成功',failed='未成功',goal='进球',saved='被扑出',blocked='被封堵').get(e.outcome,e.outcome)}。这是你关注球员的本场贡献。",
            [Evidence(label=action,value=f"{e.player_name} · {value}",event_ids=[e.event_id])],.91)
    return None
