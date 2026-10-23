"""FastAPI service: synthetic event feed, live insights, replay stream and recap."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse
from starlette.middleware.gzip import GZipMiddleware

from app.agents import AudienceProfile, InsightWorkflow
from app.foundry import FoundryRuntime, foundry_configured
from app.llm import DeepSeekRuntime, llm_configured
from app.metrics import MetricsEngine
from app.models import EventType, InsightResponse, MatchEvent, MatchMetadata, MatchSnapshot
from app.simulator import SyntheticMatch

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env", encoding="utf-8-sig")
load_dotenv(BASE_DIR.parent / "matchpulse.env", encoding="utf-8-sig")

MATCH = SyntheticMatch()
foundry_runtime: FoundryRuntime | None = None
foundry_error: str | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global foundry_runtime, foundry_error
    if llm_configured() or foundry_configured():
        try:
            foundry_runtime = DeepSeekRuntime() if llm_configured() else FoundryRuntime()
            workflow.narrative_service = foundry_runtime
        except Exception as exc:  # keep serving with the local agents
            foundry_runtime, foundry_error = None, f"{type(exc).__name__}: {exc}"
    yield
    if foundry_runtime is not None:
        foundry_runtime.close()


app = FastAPI(
    title="MatchPulse Studio API",
    version="0.2.0",
    description="Real-time multi-agent intelligence for synthetic football matches",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.add_middleware(GZipMiddleware, minimum_size=1000)
from app.ingest import router as ingest_router
app.include_router(ingest_router)

# Timeline scrubbing: every moment is analysed independently (no cross-request memory).
workflow = InsightWorkflow(stateful=False)
_insight_lock = threading.Lock()
_card_cache: dict[tuple, InsightResponse] = {}


_EVENTS: tuple[MatchEvent, ...] = tuple(MATCH.generate())  # built once at import, before any request


def match_events() -> tuple[MatchEvent, ...]:
    return _EVENTS


def build_snapshot(minute: int, second: int | None = None) -> MatchSnapshot:
    return MetricsEngine().snapshot(list(match_events()), minute, second, home_team=MATCH.home_team,
                                    away_team=MATCH.away_team, match_id=MATCH.match_id)


def make_profile(mode: str, language: str, player: str | None, team: str | None) -> AudienceProfile:
    valid_team = team if team in (MATCH.home_team, MATCH.away_team) else None
    return AudienceProfile(mode=mode, language=language, favorite_player=player or None, favorite_team=valid_team)


@app.get("/")
def studio() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
def health() -> dict[str, str | bool | None]:
    ready = foundry_runtime is not None
    return {"status": "ok", "provider": foundry_runtime.provider if ready else "local", "llm_ready": ready,
            "foundry_ready": ready and foundry_runtime.provider == "foundry",
            "foundry_configured": foundry_configured(), "foundry_error": foundry_error}


@app.get("/api/match", response_model=MatchMetadata)
def match_metadata() -> MatchMetadata:
    return MATCH.metadata()


@app.get("/api/events", response_model=list[MatchEvent])
def events(limit: int = Query(default=30, ge=1, le=1000)) -> list[MatchEvent]:
    return list(match_events()[:limit])


@app.get("/api/dataset")
def dataset(format: str = Query(default="json", pattern="^(json|csv)$")):
    """Download synthetic data only. No environment values or internal prompts."""
    records = [e.model_dump(mode="json") for e in match_events()]
    if format == "csv":
        import csv
        import io
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(records[0]) if records else [])
        writer.writeheader()
        for record in records:
            writer.writerow({**record, "attributes": json.dumps(record["attributes"], ensure_ascii=False)})
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="matchpulse-synthetic.csv"'})
    return {"schema_version": "1.0", "synthetic": True, "metadata": MATCH.metadata().model_dump(),
            "events": records, "schema_url": "/api/dataset/schema"}


@app.get("/api/dataset/schema")
def dataset_schema():
    return {"schema_version": "1.0", "event_schema": MatchEvent.model_json_schema(),
            "units": {"x": "metres, 0..105 in acting team attacking direction", "y": "metres, 0..68",
                      "distance_m": "metres", "difficulty": "heuristic 0..1, higher is harder",
                      "ball_speed_kmh": "km/h, synthetic pass flight distance / duration",
                      "shot_speed_kmh": "km/h, synthetic configured shot speed",
                      "player_distance_m": "metres travelled since this player's previous event",
                      "player_speed_kmh": "peak km/h since this player's previous event",
                      "xg": "heuristic probability 0..1, not calibrated to real matches"},
            "limitations": ["Fictional teams and players; no real match data or video recognition.",
                             "Tracking is simulated, not measured; shot outcomes are probabilistic.",
                             "Player distance in event snapshots can lag continuous tracking between actions."]}


@lru_cache(maxsize=32)
def build_overlay_timeline(mode, language, player, team):
    # Deterministic export is reproducible and never runs hundreds of paid model requests.
    engine = InsightWorkflow(stateful=True)
    profile = make_profile(mode, language, player, team)
    times = set(range(0, 5401, 60))
    times.update(e.match_second for e in match_events() if e.event_type == EventType.SHOT
                 or player and e.player_id == player)
    cards=[]
    for second in sorted(times):
        snapshot=build_snapshot(second//60, second%60)
        card,_=engine.run(snapshot, profile)
        if card:
            cards.append(card.model_dump(mode="json"))
    # Renderer can resolve simultaneous cards by priority; timing uses match seconds.
    cards.sort(key=lambda c:c["display_at_second"])
    return {"schema_version":"1.0", "timebase":"match_seconds", "synthetic":True,
            "generation":"deterministic_verified_templates", "model_calls":0,
            "profile":{"mode":mode,"language":language,"player":player,"team":team},
            "cards":cards}


@app.get("/api/overlays/timeline")
def overlay_timeline(mode: str = Query(default="fan", pattern="^(fan|analyst)$"),
                     language: str = Query(default="en", pattern="^(en|zh)$"),
                     player: str | None = None, team: str | None = None):
    return build_overlay_timeline(mode, language, player, team)


@app.get("/api/moments")
def key_moments() -> list[dict]:
    """Small event index for replay navigation; independent of model availability."""
    return [{"event_id": e.event_id, "second": e.match_second,
             "kind": "goal" if e.outcome == "goal" else e.event_type.value,
             "team": e.team_id, "player": e.player_name}
            for e in match_events() if e.event_type in {EventType.SHOT, EventType.CORNER}]


@app.get("/api/insight", response_model=InsightResponse)
def insight(
    minute: int = Query(default=64, ge=0, le=90),
    second: int | None = Query(default=None, ge=0, le=59),
    mode: str = Query(default="fan", pattern="^(fan|analyst)$"),
    language: str = Query(default="en", pattern="^(en|zh)$"),
    player: str | None = None,
    team: str | None = None,
    quick: bool = False,
) -> InsightResponse:
    if quick:
        snapshot = build_snapshot(minute, second)
        overlay, trace = InsightWorkflow(stateful=False).run(snapshot, make_profile(mode, language, player, team))
        return InsightResponse(snapshot=snapshot, overlay=overlay, trace=trace)
    key = (minute, second, mode, language, player, team)
    cached = _card_cache.get(key)
    if cached is not None:
        return cached
    snapshot = build_snapshot(minute, second)
    with _insight_lock:  # one analysis per moment, shared by every audience profile
        result = workflow.analyze(snapshot, player)
    overlay, trace = workflow.present(result, snapshot, make_profile(mode, language, player, team))
    response = InsightResponse(snapshot=snapshot, overlay=overlay, trace=trace)
    with _insight_lock:
        if len(_card_cache) > 512:
            _card_cache.pop(next(iter(_card_cache)))
        _card_cache[key] = response
    return response


@app.get("/api/player-metrics")
def player_metrics() -> dict:
    return MATCH.player_metrics(list(match_events()))


@app.get("/api/lineup")
def lineup() -> list[dict]:
    return MATCH.lineup()


@app.get("/api/frames")
def player_frames() -> list[dict]:
    return MATCH.player_frames(list(match_events()))


@lru_cache
def _recap(language: str = "en") -> dict:
    events_list = list(match_events())
    return workflow.recap(events_list, build_snapshot(90), language)


@app.get("/api/recap")
def recap(language: str = Query(default="en", pattern="^(en|zh)$")) -> dict:
    return _recap(language)


def _packet(snapshot: MatchSnapshot, overlay, traces, second: int) -> str:
    packet = {
        "minute": snapshot.minute,
        "second": second,
        "snapshot": snapshot.model_dump(mode="json"),
        "overlay": overlay.model_dump(mode="json") if overlay else None,
        "trace": [trace.model_dump(mode="json") for trace in traces],
    }
    return f"event: match_update\ndata: {json.dumps(packet)}\n\n"


@app.get("/api/replay")
async def replay(
    speed: float = Query(default=12, ge=1, le=60),
    from_second: int = Query(default=0, ge=0, le=5400),
    mode: str = Query(default="fan", pattern="^(fan|analyst)$"),
    language: str = Query(default="en", pattern="^(en|zh)$"),
    player: str | None = None,
    team: str | None = None,
) -> StreamingResponse:
    events_list = list(match_events())
    profile = make_profile(mode, language, player, team)

    async def event_stream() -> AsyncIterator[str]:
        live = InsightWorkflow(narrative_service=foundry_runtime, stateful=True)
        # Moments before the start point are history, not news.
        live.announced_event_ids = {e.event_id for e in events_list
                                    if e.event_type == EventType.SHOT and e.match_second <= from_second}

        def moment(second: int):
            snapshot = build_snapshot(second // 60, second % 60)
            analysis = live.analyze(snapshot, player)
            overlay, traces = live.present(analysis, snapshot, profile)
            return snapshot, overlay, traces

        # Agent calls may hit the network: run them off the event loop so other clients stay live.
        quick = InsightWorkflow(stateful=True)
        quick.announced_event_ids = set(live.announced_event_ids)
        pending = None
        started = asyncio.get_running_loop().time()

        def immediate(second):
            snapshot = build_snapshot(second // 60, second % 60)
            overlay, traces = quick.run(snapshot, profile)
            return snapshot, overlay, traces

        def enrichment(result):
            snapshot, overlay, traces = result
            return "event: narrative_update\ndata: " + json.dumps({
                "second": snapshot.match_second,
                "overlay": overlay.model_dump(mode="json") if overlay else None,
                "trace": [t.model_dump(mode="json") for t in traces],
            }) + "\n\n"

        try:
            result = immediate(from_second)
            yield _packet(*result, from_second)
            if foundry_runtime is not None:
                pending = asyncio.create_task(asyncio.to_thread(moment, from_second))
            for event in events_list:
                if event.match_second <= from_second:
                    continue
                deadline = started + (event.match_second - from_second) / speed
                remaining = deadline - asyncio.get_running_loop().time()
                if pending is not None and remaining > 0:
                    done, _ = await asyncio.wait({pending}, timeout=remaining)
                    if done:
                        yield enrichment(pending.result())
                        pending = None
                await asyncio.sleep(max(0., deadline - asyncio.get_running_loop().time()))
                yield f"event: match_event\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"
                result = immediate(event.match_second)
                yield _packet(*result, event.match_second)
                if pending is not None and pending.done():
                    yield enrichment(pending.result())
                    pending = None
                if foundry_runtime is not None and pending is None and result[1] is not None:
                    pending = asyncio.create_task(asyncio.to_thread(moment, event.match_second))
            yield "event: replay_complete\ndata: {}\n\n"
        finally:
            if pending is not None:
                pending.cancel()

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
