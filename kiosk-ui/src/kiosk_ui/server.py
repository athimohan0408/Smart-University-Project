"""Kiosk web server: serves the kiosk screen and the analytics dashboard, and talks to voice-memory.

Run: ``python -m kiosk_ui`` (api.host:api.port, default 127.0.0.1:8002), with voice-memory's API running.

- ``GET  /``                 kiosk screen          ``GET /dashboard``  analytics dashboard
- ``POST /api/turn``         ``{"session_id", "text", "chosen_option"?}`` -> ``{"turn": TurnResult, "routes": [...]}``
- ``POST /api/turn/audio``   multipart ``session_id`` + ``audio`` (16-bit PCM WAV) -> same as /api/turn
- ``POST /api/receptionist`` ``{"session_id"}``: the "Talk to the receptionist" button
- ``POST /api/session/reset`` ``{"session_id"}``: a new visitor
- ``GET  /api/map``          floor map for drawing   ``GET /api/route/{place_id}``  one route
- ``GET  /api/analytics?days=7``  dashboard numbers   ``GET /api/health``

The browser only ever talks to this server, so voice-memory needs no CORS and can stay on localhost.
"""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kiosk_ui.analytics import Dashboard, load_and_compute
from kiosk_ui.config import Settings, get_settings
from kiosk_ui.directory import FacultyDirectory
from kiosk_ui.eventlog import EventLog
from kiosk_ui.greeting import Greeting
from kiosk_ui.navigation import FloorMap, Route

STATIC = Path(__file__).resolve().parent / "static"
PANORAMAS = "panoramas"  # data/panoramas/ next to the map file: 360-degree photos, served at /panoramas/
SORRY = ("I'm sorry, I'm having trouble answering right now. "
         "Please speak to the receptionist at the desk.")


def page(name: str) -> HTMLResponse:
    """An HTML page whose /static/ links carry the files' version, so a browser never runs stale code."""
    version = format(max(int(p.stat().st_mtime) for p in STATIC.iterdir() if p.is_file()), "x")
    html = (STATIC / name).read_text(encoding="utf-8")
    html = re.sub(r'(/static/[\w.-]+\.(?:js|css))"', rf'\1?v={version}"', html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


class TurnIn(BaseModel):
    session_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    chosen_option: bool = False  # the visitor tapped a clarification button instead of typing/speaking


class SessionIn(BaseModel):
    session_id: str = Field(min_length=1)


class TurnOut(BaseModel):
    turn: dict
    routes: list[Route]
    off_map_rooms: list[str] = Field(description="room ids in the answer that aren't on this floor's map")


def create_app(settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()
    fmap = FloorMap.load(settings.map.file)
    directory = FacultyDirectory.load(settings.kb.faculty_json)
    log = EventLog(settings.logs.ui_events, settings.kiosk.id)
    greeting = Greeting(settings.kiosk.greeting)
    state: dict[str, httpx.Client] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # one keep-alive connection to voice-memory (a new connection per request costs ~200 ms on Windows)
        state["http"] = httpx.Client(base_url=settings.upstream.voice_memory_url,
                                     timeout=settings.upstream.timeout_s, transport=transport)
        if settings.kiosk.speak:
            greeting.warm_up()
        yield
        state.pop("http").close()

    app = FastAPI(title="kiosk-ui: CHRIST University 8th floor receptionist screen", version="0.1.0",
                  lifespan=lifespan)

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # the kiosk browser runs for weeks: make it re-check (ETag, usually a 304) so an update shows up
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    panorama_dir = settings.map.file.parent / PANORAMAS
    panorama_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/panoramas", StaticFiles(directory=panorama_dir), name="panoramas")

    def http() -> httpx.Client:
        if "http" not in state:  # used outside the lifespan (e.g. a TestClient without `with`)
            state["http"] = httpx.Client(base_url=settings.upstream.voice_memory_url,
                                         timeout=settings.upstream.timeout_s, transport=transport)
        return state["http"]

    def finish_turn(session_id: str, call, transcript_hint: str | None, chosen_option: bool) -> TurnOut:
        start = time.perf_counter()
        try:
            response = call()
            response.raise_for_status()
            turn = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            round_trip = round((time.perf_counter() - start) * 1000, 1)
            log.append("error", session_id, where="voice-memory /turn", detail=type(exc).__name__,
                       question=transcript_hint, round_trip_ms=round_trip)
            turn = {"session_id": session_id, "transcript": transcript_hint or "", "question": transcript_hint or "",
                    "answer": SORRY, "speech": SORRY, "outcome": "error", "escalate": True,
                    "escalate_reason": "kiosk_upstream_error", "options": [], "room_ids": [], "latency_ms": {}}
            return TurnOut(turn=turn, routes=[], off_map_rooms=[])
        turn["options"] = directory.clarification_options(turn)
        turn["room_ids"] = list(dict.fromkeys([*(turn.get("room_ids") or []), *directory.rooms_in_answer(turn)]))
        routes, off_map = [], []
        for rid in turn["room_ids"]:
            if (r := fmap.route(rid)) is not None:
                routes.append(r)
            else:
                off_map.append(rid)
        round_trip = round((time.perf_counter() - start) * 1000, 1)
        if chosen_option:
            log.append("option_chosen", session_id, option=transcript_hint)
        log.append("turn", session_id, turn_id=turn.get("turn_id"), question=turn.get("transcript"),
                   resolved_question=turn.get("question"), outcome=turn.get("outcome"), intent=turn.get("intent"),
                   escalate=turn.get("escalate"), escalate_reason=turn.get("escalate_reason"),
                   entities=turn.get("entities") or [], room_ids=turn.get("room_ids") or [],
                   route_shown=bool(routes), options_shown=len(turn.get("options") or []),
                   round_trip_ms=round_trip, upstream_total_ms=(turn.get("latency_ms") or {}).get("total"))
        return TurnOut(turn=turn, routes=routes, off_map_rooms=off_map)

    @app.get("/", include_in_schema=False)
    def kiosk_page() -> HTMLResponse:
        return page("kiosk.html")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page() -> HTMLResponse:
        return page("dashboard.html")

    @app.get("/api/config")
    def config() -> dict:
        return {"kiosk_id": settings.kiosk.id, "idle_reset_s": settings.kiosk.idle_reset_s,
                "speak": settings.kiosk.speak, "map_verified": fmap.verified}

    @app.get("/api/greeting")
    def greeting_info() -> dict:
        has_audio = settings.kiosk.speak and greeting.wav() is not None
        return {"text": greeting.text, "audio_url": "/api/greeting.wav" if has_audio else None}

    @app.get("/api/greeting.wav", include_in_schema=False)
    def greeting_audio() -> Response:
        if not settings.kiosk.speak or (data := greeting.wav()) is None:
            raise HTTPException(status_code=404, detail="no greeting audio: the browser speaks the greeting")
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-cache"})

    @app.post("/api/turn", response_model=TurnOut)
    def turn(body: TurnIn) -> TurnOut:
        payload = {"session_id": body.session_id, "text": body.text, "speak": settings.kiosk.speak}
        return finish_turn(body.session_id, lambda: http().post("/turn", json=payload), body.text, body.chosen_option)

    @app.post("/api/turn/audio", response_model=TurnOut)
    async def turn_audio(session_id: str = Form(..., min_length=1), audio: UploadFile = File(...)) -> TurnOut:
        data = await audio.read()
        files = {"audio": ("speech.wav", data, "audio/wav")}
        form = {"session_id": session_id, "speak": str(settings.kiosk.speak).lower()}
        return finish_turn(session_id, lambda: http().post("/turn/audio", data=form, files=files), None, False)

    @app.post("/api/receptionist")
    def receptionist(body: SessionIn) -> dict:
        log.append("receptionist_button", body.session_id)
        try:
            response = http().post("/receptionist", json={"session_id": body.session_id})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.append("error", body.session_id, where="voice-memory /receptionist", detail=type(exc).__name__)
            return {"status": "kiosk_upstream_error",
                    "message": "Please walk over to the receptionist at the desk; they will help you."}

    @app.post("/api/session/reset")
    def reset(body: SessionIn) -> dict:
        log.append("session_reset", body.session_id)
        try:
            http().post("/session/reset", json={"session_id": body.session_id}).raise_for_status()
        except httpx.HTTPError:
            pass  # a new session id is used anyway, so the old memory is never consulted again
        return {"status": "reset"}

    @app.get("/api/map")
    def floor_map() -> dict:
        start = fmap.nodes[fmap.start]
        return {**fmap.model_dump(include={"verified", "verified_by", "floor", "viewbox", "corridor", "places"}),
                "start_point": [start.x, start.y]}

    @app.get("/api/route/{place_id}", response_model=Route)
    def route(place_id: str) -> Route:
        if (r := fmap.route(place_id)) is None:
            raise HTTPException(status_code=404, detail=f"{place_id} is not on the {fmap.floor} map")
        return r

    @app.get("/api/analytics", response_model=Dashboard)
    def analytics(days: float | None = Query(default=None, gt=0)) -> Dashboard:
        logs = settings.logs
        d = load_and_compute(logs.rag_queries, logs.voice_turns, logs.ui_events, days=days)
        for dest in d.top_destinations:  # a person's name and 8th-floor office, for the dashboard list and map
            if name := directory.display.get(dest["id"]):
                dest["name"] = name
            if room := directory.rooms.get(dest["id"]):
                dest["room"] = f"room:{room.casefold()}"
        return d

    @app.get("/api/health")
    def health() -> dict:
        out = {"status": "ok", "map_places": len(fmap.places), "map_verified": fmap.verified, "voice_memory": None}
        try:
            out["voice_memory"] = http().get("/health", timeout=5).json()
        except (httpx.HTTPError, ValueError) as exc:
            out["voice_memory"] = f"unreachable: {type(exc).__name__}"
        return out

    return app


def main() -> None:
    import uvicorn

    cfg = get_settings().api
    uvicorn.run(create_app(), host=cfg.host, port=cfg.port)
