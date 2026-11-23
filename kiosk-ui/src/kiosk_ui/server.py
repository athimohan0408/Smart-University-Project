"""Kiosk web server: serves the kiosk screen and the analytics dashboard, and talks to voice-memory.

Run: ``python -m kiosk_ui`` (api.host:api.port, default 127.0.0.1:8002), with voice-memory's API running.

- ``GET  /``                 kiosk screen          ``GET /dashboard``  analytics dashboard
- ``POST /api/turn``         ``{"session_id", "text", "chosen_option"?, "at"?}`` -> ``{"turn": TurnResult, "routes": [...],
                             "position"?}``. ``at`` is a map place id ("facility:lift-north") from a sign's QR code: routes then
                             start there unless the visitor says otherwise, and "Where am I?" answers from it.
- ``POST /api/turn/audio``   multipart ``session_id`` + ``audio`` (16-bit PCM WAV) [+ ``at``] -> same as /api/turn
- ``GET  /api/position``     ``?at=facility:lift-north`` -> where that is, in words, and what is close by
- ``POST /api/receptionist`` ``{"session_id"}``: the "Talk to the receptionist" button
- ``POST /api/session/reset`` ``{"session_id"}``: a new visitor
- ``GET  /api/map?from=``    floor map for drawing   ``GET /api/route/{place_id}?from=``  one route
- ``GET  /api/nearest/{what}?from=``  route to the nearest lift / stairs / washroom / water
- ``GET  /api/places``       every place (Explore the floor, phone page)   ``GET /api/place/{id}``  a place card
- ``POST /api/feedback``     ``{"session_id", "turn_id", "helpful"}``: thumbs up / down under an answer
- ``GET  /go?from=&to=``     the phone walkthrough (QR stickers at the lifts)   ``GET /qr``  printable stickers
- ``GET  /admin``            the admin dashboard (API under /api/admin, see admin_api.py)
- ``GET  /api/announcements``  what the office announces on the kiosk today
- ``GET  /api/analytics?days=7``  dashboard numbers   ``GET /api/health``

``from`` is where the route starts: ``kiosk`` (the default) or a lift / staircase in the map's ``starts``.

The browser only ever talks to this server, so voice-memory needs no CORS and can stay on localhost.
"""

from __future__ import annotations

import base64
import re
import socket
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kiosk_ui.admin_api import create_admin_router
from kiosk_ui.admin_store import AdminStore
from kiosk_ui.analytics import Dashboard, load_and_compute
from kiosk_ui.config import Settings, get_settings
from kiosk_ui.directory import FacultyDirectory
from kiosk_ui.eventlog import EventLog
from kiosk_ui.greeting import Greeting, Voice
from kiosk_ui.navigation import NEAREST, FloorMap, Position, Route
from kiosk_ui.places import PlaceBook, PlaceCard, PlaceSummary

STATIC = Path(__file__).resolve().parent / "static"
PANORAMAS = "panoramas"  # data/panoramas/ next to the map file: 360-degree photos, served at /panoramas/
PHOTOS = "photos"  # data/photos/ next to the map file: phone photos of doors and landmarks, served at /photos/
SORRY = ("I'm sorry, I'm having trouble answering right now. "
         "Please speak to the receptionist at the desk.")
# "where is room 815?" that the knowledge base can't answer, but the floor map can
_ROOM_NUMBER = re.compile(r"\b(\d{3}[a-z]?)\b", re.IGNORECASE)
_WHERE = re.compile(r"\b(where|how (do|can) i (get|go|reach|find)|way to|directions?|find|locate|reach)\b", re.IGNORECASE)
# "Where am I?": answered from the floor map, whatever the receptionist service made of it
_WHERE_AM_I = re.compile(
    r"\bwhere\s+(?:am\s+i|are\s+we|exactly\s+am\s+i)\b|\bwhere\s+is\s+this(?:\s+place)?\s*[?.!]*$|"
    r"\bwhat\s+(?:floor|place|block)\s+(?:is\s+this|am\s+i\s+on)\b|\bwhich\s+(?:floor|block)\s+(?:is\s+this|am\s+i\s+on)\b|"
    r"\bi(?:'m|\s+am)\s+lost\b|\bi\s+(?:don'?t|do\s+not)\s+know\s+where\s+i\s+am\b",
    re.IGNORECASE,
)
_REQUEST_DIALOGUE = "[message or meeting request]"  # what the event log keeps of a "leave a message" / "book a meeting" turn
# a question about WHERE a place is, as opposed to what it is or when it is open
_LOCATION = re.compile(r"\b(where|how\s+(?:do|can|to)\s+(?:i\s+)?(?:get|go|reach|find)|way\s+to|directions?|find|locate|reach|"
                       r"nearest|closest|near|is\s+there|are\s+there|take\s+me|guide\s+me|show\s+me)\b", re.IGNORECASE)
_WASHROOM = re.compile(r"\b(washrooms?|restrooms?|toilets?|bathrooms?|loo)\b", re.IGNORECASE)
_NOT_WHERE = re.compile(r"\b(phone|number of|email|mail|contact|hours?|free|available|timetable|class(es)?|teach)", re.IGNORECASE)


def lan_address() -> str | None:
    """This computer's address on the local network (the Wi-Fi), for QR codes phones can open. No packet is sent:
    connecting a UDP socket only asks the OS which interface would be used."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith(("127.", "0.")) else ip


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
    at: str | None = None  # a map place id from a sign's QR code: where this visitor is standing


class SessionIn(BaseModel):
    session_id: str = Field(min_length=1)


class FeedbackIn(BaseModel):
    session_id: str = Field(min_length=1)
    turn_id: str | None = None
    helpful: bool


class TurnOut(BaseModel):
    turn: dict
    routes: list[Route]
    off_map_rooms: list[str] = Field(description="room ids in the answer that aren't on this floor's map")
    notices: list[str] = Field(default_factory=list, description="the office's notes on this answer: a person on "
                               "leave, a closed room")
    position: Position | None = Field(default=None, description="Set for 'Where am I?': where they are, to mark on the map.")


def create_app(settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None,
               rag_transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()
    fmap = FloorMap.load(settings.map.file)
    store = AdminStore(settings.admin.data_dir)
    info_file = settings.map.places_info or settings.map.file.parent / "places_info.yaml"
    # the faculty sheet and place cards, replaced when the office publishes a new timetable
    kb = SimpleNamespace(directory=None, book=None)

    def refresh_kb() -> None:
        kb.directory = FacultyDirectory.load(settings.kb.faculty_json)
        kb.book = PlaceBook.load(fmap, info_file, kb.directory, store)

    refresh_kb()
    log = EventLog(settings.logs.ui_events, settings.kiosk.id)
    greeting = Greeting(settings.kiosk.greeting)
    voice = Voice(settings.kiosk.speak)
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
    photo_dir = settings.map.file.parent / PHOTOS
    photo_dir.mkdir(parents=True, exist_ok=True)
    uploads = settings.admin.data_dir / "photos"  # door photos uploaded from the admin dashboard (admin-*.jpg)

    @app.get("/photos/{name}", include_in_schema=False)
    def photo(name: str) -> FileResponse:
        """A photo: the team's in data/photos/, or one uploaded from the admin dashboard."""
        folder = uploads if name.startswith("admin-") else photo_dir
        path = (folder / name).resolve()
        if path.parent != folder.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="no such photo")
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    def http() -> httpx.Client:
        if "http" not in state:  # used outside the lifespan (e.g. a TestClient without `with`)
            state["http"] = httpx.Client(base_url=settings.upstream.voice_memory_url,
                                         timeout=settings.upstream.timeout_s, transport=transport)
        return state["http"]

    def dress(route: Route) -> Route:
        """The office's door photo on the last step (when it uploaded one for this place)."""
        place = fmap.place(route.place_id)
        if place is not None and (photo := store.state.photos.get(place.id)) and route.step_photos:
            route = route.model_copy(update={"step_photos": [*route.step_photos[:-1], photo]})
        return route

    def notices(turn: dict, routes: list[Route]) -> list[str]:
        """What the office has said that this answer should carry: a person on leave, a closed room."""
        out, now = [], datetime.now()
        if turn.get("outcome") == "answered":
            for e in turn.get("entities") or []:
                cid = e.get("canonical_id") or ""
                if cid.startswith("faculty:") and (s := store.status_now(cid, now)):
                    out.append(f"{kb.directory.display.get(cid, e.get('text'))}: {s} (from the department office).")
        for r in routes:
            if (c := store.closure_now(r.place_id, now.date())) is not None:
                until = f" until {c.until:%a %d %b}".replace(" 0", " ") if c.until else ""
                out.append(f"{r.destination or r.label} is closed{until}: {c.reason}.")
        return list(dict.fromkeys(out))

    def say(turn: dict, text: str, **fields) -> bool:
        wav = voice.wav(text)
        turn.update(answer=text, speech=text, audio_wav_base64=base64.b64encode(wav).decode() if wav else None,
                    escalate=False, escalate_reason=None, ticket=None, answered_by="kiosk_floor_map",
                    kb_escalate_reason="not_in_kb", **{"outcome": "answered", "options": [], "room_ids": [], **fields})
        return True

    def answer_facility(turn: dict, asked: str, origin_id: str | None) -> bool:
        """"Where is the lift / the washroom / drinking water / the staircase / the CCD café?": the nearest one, from the
        floor plan, with a route. Only where a place is; whether it is open, working or free is never claimed."""
        found = fmap.find_by_name(asked)
        found = [p for p in found if p.kind != "room"]
        if not found:
            if _WASHROOM.search(asked):  # a washroom, but whose? ask, don't guess
                return say(turn, "Which washroom do you need: the Gents or the Ladies?", outcome="clarification",
                           options=["Where is the Gents washroom?", "Where is the Ladies washroom?"])
            return False
        start_place = fmap.origin_place(origin_id)
        here = start_place.origin_name if start_place else fmap.start_name
        options = []
        for place in found:
            if start_place is not None and place.id == start_place.id:
                continue  # they are standing at it
            if (r := fmap.route(place.id, origin_id)) is not None:
                options.append((r.length_m, place, r))
        if not options:
            place = found[0]
            where = (fmap.whereabouts(place.id) or "").replace(" · ", ", ")
            if start_place is not None and place.id == start_place.id:
                return say(turn, f"You are at {here}.")
            return say(turn, f"{place.spoken_name} is here on the 8th floor, {where}. "
                             "I can't draw the way there on the map yet.")
        length, place, route = min(options, key=lambda o: o[0])
        where = (fmap.whereabouts(place.id) or "").replace(" · ", ", ")
        others = [p.origin_name for p in found if p.id != place.id and p.id != (start_place.id if start_place else None)]
        distance = f"about {max(1, round(length))} m from {here}, {where}. I've shown you the way on the map."
        if len(found) == 1:  # only one on the floor: no "nearest"
            text = f"{place.spoken_name} is {distance}"
        else:
            text = f"The nearest {place.aliases[0] if place.aliases else place.label.lower()} is {place.origin_name}, {distance}"
        if others:
            text += " This floor also has " + (", ".join(others[:-1]) + " and " + others[-1] if len(others) > 1 else others[0]) + "."
        return say(turn, text, room_ids=[place.id])

    def answer_from_map(turn: dict, origin_id: str | None = None) -> bool:
        """A place on the floor map that the knowledge base doesn't hold (rooms 808-815, the MCA Lab, a lift, a washroom,
        drinking water ...): say where it is, from the department's floor plan, instead of "I can't find it". Only for
        "where is ...?" questions; it never says what a room is or who is in it."""
        if turn.get("parts"):  # several questions in one reply: replacing it would drop the other answers
            return False
        if not (turn.get("escalate") and turn.get("escalate_reason") == "not_in_kb"):
            return False
        asked = f"{turn.get('transcript') or ''} {turn.get('question') or ''}"
        if _NOT_WHERE.search(asked):
            return False
        if _LOCATION.search(asked) and answer_facility(turn, asked, origin_id):
            return True
        if not (turn.get("intent") == "find_room" or _WHERE.search(asked)):
            return False
        named = [p for p in fmap.find_by_name(asked) if p.kind == "room"]  # "the MCA lab"
        numbered = (fmap.place(f"room:{n}") for n in dict.fromkeys(m.group(1).casefold() for m in _ROOM_NUMBER.finditer(asked)))
        for place in [*named, *numbered]:
            if place is None or place.kind != "room" or not place.routable:
                continue
            number_shown, _, what = (part.strip() for part in place.label.partition("·"))
            name = f"Room {number_shown}, the {what}," if what else f"Room {number_shown}"
            where = (fmap.whereabouts(place.id) or "").replace(" · ", ", ")
            text = f"{name} is here on the 8th floor, {where}. I've shown you the way on the map."
            wav = voice.wav(text)
            turn.update(answer=text, speech=text, audio_wav_base64=base64.b64encode(wav).decode() if wav else None,
                        outcome="answered", escalate=False, escalate_reason=None, options=[], ticket=None,
                        room_ids=[place.id], answered_by="kiosk_floor_map", kb_escalate_reason="not_in_kb")
            return True
        return False

    def answer_from_position(turn: dict, origin_id: str | None) -> Position | None:
        """"Where am I?": say where the kiosk (or the place they told us they are at) is, from the map. Never a guess:
        the position is the kiosk's own, or the one the visitor named."""
        asked = f"{turn.get('transcript') or ''} {turn.get('question') or ''}"
        if not _WHERE_AM_I.search(asked) or str(turn.get("outcome", "")).startswith("task"):
            return None
        position = fmap.position(origin_id)
        text = position.text
        if origin_id is None:
            text += " Tell me where you would like to go and I will show you the way from here."
        else:
            text += " Tell me where you would like to go and I will show you the way from there."
        wav = voice.wav(text)
        turn.update(answer=text, speech=text, audio_wav_base64=base64.b64encode(wav).decode() if wav else None,
                    outcome="answered", escalate=False, escalate_reason=None, options=[], ticket=None, room_ids=[],
                    answered_by="kiosk_position")
        return position

    def finish_turn(session_id: str, call, transcript_hint: str | None, chosen_option: bool,
                    at: str | None = None) -> TurnOut:
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
        # where the visitor starts: what they told the receptionist ("from the lift"), else the sign they scanned
        start_place = fmap.origin_place((turn.get("origin") or {}).get("place_id")) or fmap.origin_place(at)
        origin_id = start_place.id if start_place else None
        position = answer_from_position(turn, origin_id)
        if position is None and not answer_from_map(turn, origin_id):
            turn["options"] = kb.directory.clarification_options(turn)
            turn["room_ids"] = list(dict.fromkeys([*(turn.get("room_ids") or []), *kb.directory.rooms_in_answer(turn)]))
        routes, off_map = [], []
        for rid in turn["room_ids"]:
            if (r := fmap.route(rid, origin_id)) is not None:
                routes.append(dress(r))
            else:
                off_map.append(rid)
        round_trip = round((time.perf_counter() - start) * 1000, 1)
        if chosen_option:
            log.append("option_chosen", session_id, option=transcript_hint)
        in_dialogue = str(turn.get("outcome", "")).startswith("task")  # names, messages, contacts: never logged here
        log.append("turn", session_id, turn_id=turn.get("turn_id"),
                   question=_REQUEST_DIALOGUE if in_dialogue else turn.get("transcript"),
                   resolved_question=_REQUEST_DIALOGUE if in_dialogue else turn.get("question"),
                   outcome=turn.get("outcome"), intent=turn.get("intent"), origin=origin_id,
                   escalate=turn.get("escalate"), escalate_reason=turn.get("escalate_reason"),
                   entities=turn.get("entities") or [], room_ids=turn.get("room_ids") or [],
                   route_shown=bool(routes), options_shown=len(turn.get("options") or []),
                   answered_by=turn.get("answered_by", "voice-memory"),
                   round_trip_ms=round_trip, upstream_total_ms=(turn.get("latency_ms") or {}).get("total"))
        return TurnOut(turn=turn, routes=routes, off_map_rooms=off_map, position=position,
                       notices=notices(turn, routes))

    @app.get("/", include_in_schema=False)
    def kiosk_page() -> HTMLResponse:
        return page("kiosk.html")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page() -> HTMLResponse:
        return page("dashboard.html")

    @app.get("/go", include_in_schema=False)
    def phone_page() -> HTMLResponse:
        return page("go.html")

    @app.get("/qr", include_in_schema=False)
    def qr_page() -> HTMLResponse:
        return page("qr.html")

    @app.get("/admin", include_in_schema=False)
    def admin_page() -> HTMLResponse:
        return page("admin.html")

    @app.get("/api/announcements")
    def announcements() -> list[dict]:
        return [{"id": a.id, "text": a.text} for a in store.announcements_now(date.today())]

    app.include_router(create_admin_router(store=store, settings=settings, kb=kb, fmap=fmap,
                                           refresh_kiosk=refresh_kb, rag_transport=rag_transport))

    @app.get("/api/config")
    def config() -> dict:
        return {"kiosk_id": settings.kiosk.id, "idle_reset_s": settings.kiosk.idle_reset_s,
                "speak": settings.kiosk.speak, "map_verified": fmap.verified, "public_url": public_url(),
                "wake": settings.kiosk.wake.model_dump()}

    def public_url() -> str | None:
        """Where phones reach this server: kiosk.public_url when set; otherwise, when the server listens on the
        network (api.host 0.0.0.0), this computer's Wi-Fi address; otherwise None (only the kiosk can reach it)."""
        if settings.kiosk.public_url:
            return settings.kiosk.public_url.rstrip("/")
        if settings.api.host in ("0.0.0.0", "::") and (ip := lan_address()):
            return f"http://{ip}:{settings.api.port}"
        return None

    @app.get("/api/greeting")
    def greeting_info() -> dict:
        has_audio = settings.kiosk.speak and greeting.wav() is not None
        return {"text": greeting.text, "audio_url": "/api/greeting.wav" if has_audio else None}

    @app.get("/api/greeting.wav", include_in_schema=False)
    def greeting_audio() -> Response:
        if not settings.kiosk.speak or (data := greeting.wav()) is None:
            raise HTTPException(status_code=404, detail="no greeting audio: the browser speaks the greeting")
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-cache"})

    @app.get("/api/speak", include_in_schema=False)
    def speak(text: str = Query(min_length=1, max_length=300)) -> Response:
        """A navigation step, spoken with the same voice as the answers (the walkthrough reads each step aloud)."""
        if (data := voice.wav(text)) is None:
            raise HTTPException(status_code=404, detail="no kiosk voice: the browser speaks instead")
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/turn", response_model=TurnOut)
    def turn(body: TurnIn) -> TurnOut:
        payload = {"session_id": body.session_id, "text": body.text, "speak": settings.kiosk.speak}
        return finish_turn(body.session_id, lambda: http().post("/turn", json=payload), body.text, body.chosen_option,
                           body.at)

    @app.post("/api/turn/audio", response_model=TurnOut)
    async def turn_audio(session_id: str = Form(..., min_length=1), audio: UploadFile = File(...),
                         at: str | None = Form(None)) -> TurnOut:
        data = await audio.read()
        files = {"audio": ("speech.wav", data, "audio/wav")}
        form = {"session_id": session_id, "speak": str(settings.kiosk.speak).lower()}
        return finish_turn(session_id, lambda: http().post("/turn/audio", data=form, files=files), None, False, at)

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

    def origin_or_404(start_id: str | None):
        if (origin := fmap.origin(start_id)) is None:
            raise HTTPException(status_code=404, detail=f"unknown start {start_id!r}: use kiosk or one of "
                                f"{', '.join(fmap.starts)}")
        return origin

    @app.get("/api/map")
    def floor_map(start_id: str | None = Query(default=None, alias="from")) -> dict:
        origin = origin_or_404(start_id)
        start = fmap.nodes[origin.node]
        starts = [{"id": "kiosk", "name": fmap.start_name}] + [{"id": k, "name": s.name} for k, s in fmap.starts.items()]
        return {**fmap.model_dump(include={"verified", "verified_by", "floor", "viewbox", "corridor", "places"}),
                "start_point": [start.x, start.y], "start_id": (start_id or "kiosk").casefold(),
                "start_name": origin.name, "starts": starts}

    @app.get("/api/position", response_model=Position)
    def position(at: str | None = Query(default=None)) -> Position:
        """Where the kiosk is (no ``at``) or where a place on the map is, for a sign's QR code."""
        if at and fmap.origin_place(at) is None:
            raise HTTPException(status_code=404, detail=f"{at} is not a place a visitor can start from on the {fmap.floor} map")
        return fmap.position(at)

    @app.get("/api/route/{place_id}", response_model=Route)
    def route(place_id: str, start_id: str | None = Query(default=None, alias="from")) -> Route:
        origin_or_404(start_id)
        if (r := fmap.route(place_id, start_id)) is None:
            raise HTTPException(status_code=404, detail=f"{place_id} is not on the {fmap.floor} map")
        return dress(r)

    @app.get("/api/nearest/{what}", response_model=Route)
    def nearest(what: str, start_id: str | None = Query(default=None, alias="from")) -> Route:
        origin_or_404(start_id)
        if what.casefold() not in NEAREST:
            raise HTTPException(status_code=404, detail=f"nearest what? one of {', '.join(NEAREST)}")
        if (r := fmap.nearest(what, start_id)) is None:
            raise HTTPException(status_code=404, detail=f"no {what} on the {fmap.floor} map")
        return dress(r)

    @app.get("/api/places", response_model=list[PlaceSummary])
    def places() -> list[PlaceSummary]:
        return kb.book.summaries()

    @app.get("/api/place/{place_id}", response_model=PlaceCard)
    def place_card(place_id: str) -> PlaceCard:
        if (card := kb.book.card(place_id)) is None:
            raise HTTPException(status_code=404, detail=f"{place_id} is not on the {fmap.floor} map")
        return card

    @app.post("/api/feedback")
    def feedback(body: FeedbackIn) -> dict:
        log.append("feedback", body.session_id, turn_id=body.turn_id, helpful=body.helpful)
        return {"status": "thanks"}

    @app.get("/api/analytics", response_model=Dashboard)
    def analytics(days: float | None = Query(default=None, gt=0)) -> Dashboard:
        logs = settings.logs
        d = load_and_compute(logs.rag_queries, logs.voice_turns, logs.ui_events, days=days)
        for dest in d.top_destinations:  # a person's name and 8th-floor office, for the dashboard list and map
            if name := kb.directory.display.get(dest["id"]):
                dest["name"] = name
            if room := kb.directory.rooms.get(dest["id"]):
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
