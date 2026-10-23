"""Authenticated normalized event ingress, isolated from the synthetic demo.

In-memory single-worker pilot adapter; not a licensed provider connector or tracking engine.
"""
import math
import hmac
import os
from threading import RLock
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from app.models import MatchEvent
from app.metrics import MetricsEngine
from app.agents import AudienceProfile, InsightWorkflow


def authenticate(x_ingest_key: str | None = Header(default=None)):
    expected=os.getenv("INGEST_API_KEY", "")
    if not expected:
        raise HTTPException(503,"External ingestion is disabled; configure INGEST_API_KEY")
    if not x_ingest_key or not hmac.compare_digest(x_ingest_key,expected):
        raise HTTPException(401,"Invalid ingestion credential")


router=APIRouter(prefix="/api/live",dependencies=[Depends(authenticate)],tags=["External event pilot"])
lock=RLock()
sessions={}


class LiveMatch(BaseModel):
    match_id: str = Field(min_length=1,max_length=80,pattern=r"^[A-Za-z0-9_-]+$")
    home_team: str = Field(min_length=1,max_length=100)
    away_team: str = Field(min_length=1,max_length=100)
    source: Literal["synthetic","licensed_live"] = "synthetic"


class Batch(BaseModel):
    events: list[MatchEvent] = Field(min_length=1,max_length=200)


@router.post("/matches",status_code=201)
def create_match(metadata:LiveMatch):
    if metadata.home_team==metadata.away_team:
        raise HTTPException(422,"Teams must differ")
    with lock:
        if metadata.match_id in sessions:
            raise HTTPException(409,"Match already exists")
        if len(sessions)>=16:
            raise HTTPException(429,"Pilot match capacity reached")
        sessions[metadata.match_id]={"metadata":metadata,"events":[],"ids":{}}
    return {**metadata.model_dump(),"storage":"process_memory","tracking_available":False}


def session(match_id):
    if match_id not in sessions:
        raise HTTPException(404,"Unknown match")
    return sessions[match_id]


@router.post("/matches/{match_id}/events")
def append_events(match_id:str,batch:Batch):
    with lock:
        live=session(match_id)
        metadata=live["metadata"]
        pending=[]
        known=dict(live["ids"])
        tail=live["events"][-1] if live["events"] else None
        duplicate=0
        for event in batch.events:
            if event.match_id!=match_id or event.team_id not in {metadata.home_team,metadata.away_team}:
                raise HTTPException(422,"Event match or team does not match metadata")
            if event.possession_team_id not in {None,metadata.home_team,metadata.away_team}:
                raise HTTPException(422,"Unknown possession team")
            for key in ("xg","difficulty","distance_m","ball_speed_kmh","shot_speed_kmh","player_distance_m","player_speed_kmh"):
                if key in event.attributes:
                    value=event.attributes[key]
                    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 or key in {"xg","difficulty"} and value>1:
                        raise HTTPException(422,f"Invalid numeric event attribute: {key}")
            if event.event_id in known:
                if known[event.event_id] != event:
                    raise HTTPException(409,"Conflicting event id")
                duplicate+=1
                continue
            if tail and (event.sequence<=tail.sequence or event.match_second<tail.match_second):
                raise HTTPException(409,"Events must have increasing sequence and nondecreasing match time")
            pending.append(event); known[event.event_id]=event;tail=event
        if len(live["events"])+len(pending)>20000:
            raise HTTPException(413,"Pilot event capacity reached")
        # Commit only after the entire batch passes validation.
        live["events"].extend(pending);live["ids"]=known
        return {"accepted":len(pending),"duplicates":duplicate,"total":len(live["events"])}


@router.get("/matches/{match_id}/snapshot")
def live_snapshot(match_id:str):
    with lock:
        live=session(match_id);events=list(live["events"]);meta=live["metadata"]
    second=events[-1].match_second if events else 0
    snap=MetricsEngine().snapshot(events,second//60,second%60,home_team=meta.home_team,away_team=meta.away_team,match_id=match_id)
    return {"source":meta.source,"tracking_available":False,"snapshot":snap.model_dump(mode="json")}


@router.get("/matches/{match_id}/insight")
def live_insight(match_id:str,mode:str=Query(default="fan",pattern="^(fan|analyst)$"),
                 language:str=Query(default="en",pattern="^(en|zh)$"),player:str|None=None,team:str|None=None):
    with lock:
        live=session(match_id);events=list(live["events"]);meta=live["metadata"]
    second=events[-1].match_second if events else 0
    snap=MetricsEngine().snapshot(events,second//60,second%60,home_team=meta.home_team,away_team=meta.away_team,match_id=match_id)
    # Reuse the configured model, but never share history with the demo or other matches.
    from app.main import foundry_runtime
    workflow=InsightWorkflow(narrative_service=foundry_runtime,stateful=False)
    card,traces=workflow.run(snap,AudienceProfile(mode,language,team,player))
    return {"source":meta.source,"snapshot":snap.model_dump(mode="json"),
            "overlay":card.model_dump(mode="json") if card else None,
            "trace":[t.model_dump(mode="json") for t in traces]}
