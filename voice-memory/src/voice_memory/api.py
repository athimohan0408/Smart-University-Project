"""HTTP API for the kiosk UI. Run: ``python -m voice_memory.api`` (api.host:api.port, default 127.0.0.1:8001).

- ``POST /turn``        ``{"session_id": "kiosk-01", "text": "where is her office?", "speak": true}`` -> TurnResult
- ``POST /turn/audio``  multipart: ``session_id`` + ``audio`` (16-bit PCM WAV, any rate) -> TurnResult
- ``POST /receptionist`` ``{"session_id": ...}``: the on-screen "Talk to the receptionist" button -> Ticket
- ``POST /session/reset`` ``{"session_id": ...}``: a new visitor; forget the conversation
- ``GET /health``: this module, plus rag-core's /health when reachable

TurnResult carries the screen ``answer``, the spoken ``speech`` (and its WAV as base64 when
``speak``), the buttons in ``options``, ``room_ids`` for navigation, and ``latency_ms``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from voice_memory.config import get_settings
from voice_memory.handoff import Ticket
from voice_memory.receptionist import Receptionist, TurnResult, build_receptionist
from voice_memory.speech.audio import read_wav, resample

_state: dict[str, Receptionist] = {}


def receptionist() -> Receptionist:
    if "r" not in _state:
        _state["r"] = build_receptionist(get_settings())
    return _state["r"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(receptionist)  # load the speech models before the first visitor
    yield


app = FastAPI(title="voice-memory: CHRIST University 8th floor receptionist", version="0.1.0", lifespan=lifespan)


class TurnRequest(BaseModel):
    session_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    speak: bool = True


class SessionRequest(BaseModel):
    session_id: str = Field(min_length=1)


@app.post("/turn", response_model=TurnResult)
async def turn(request: TurnRequest) -> TurnResult:
    return await run_in_threadpool(receptionist().handle_text, request.session_id, request.text, speak=request.speak)


@app.post("/turn/audio", response_model=TurnResult)
async def turn_audio(session_id: str = Form(...), audio: UploadFile = File(...), speak: bool = Form(True)) -> TurnResult:
    try:
        samples, rate = read_wav(await audio.read())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"audio must be 16-bit PCM WAV: {exc}") from exc
    target = get_settings().audio.sample_rate
    samples = resample(samples, rate, target)
    return await run_in_threadpool(receptionist().handle_audio, session_id, samples, target, speak=speak)


@app.post("/receptionist", response_model=Ticket)
async def call_receptionist(request: SessionRequest) -> Ticket:
    return await run_in_threadpool(receptionist().press_receptionist_button, request.session_id)


@app.post("/session/reset")
async def reset(request: SessionRequest) -> dict:
    receptionist().new_visitor(request.session_id)
    return {"status": "reset"}


@app.get("/health")
async def health() -> dict:
    r = receptionist()
    out = {"status": "ok", "faculty_known": len(r.kb.people), "stt": type(r.stt).__name__,
           "tts": type(r.tts._engine).__name__, "rag": None}
    try:
        out["rag"] = r.rag.health() if hasattr(r.rag, "health") else "inprocess"
    except Exception as exc:  # noqa: BLE001
        out["rag"] = f"unreachable: {exc}"
    return out


def main() -> None:
    import uvicorn

    cfg = get_settings().api
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
