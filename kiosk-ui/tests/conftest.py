from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from kiosk_ui.config import MODULE_ROOT, Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Real settings and map, with every log redirected into tmp_path."""
    raw = yaml.safe_load((MODULE_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    raw["map"]["file"] = str(MODULE_ROOT / "data" / "floor_map.yaml")
    raw["kiosk"]["speak"] = False  # no voice model in tests (the greeting would load Piper)
    raw["logs"] = {"ui_events": str(tmp_path / "ui_events.jsonl"), "rag_queries": str(tmp_path / "queries.jsonl"),
                   "voice_turns": str(tmp_path / "turns.jsonl")}
    faculty = {"faculty": [
        {"id": "nisha-varghese", "title": "Dr", "full_name": "Nisha Varghese",
         "location": {"block": "Central Block", "floor": "Eighth Floor", "room": "874", "on_8th_floor": True}},
        {"id": "jossy-p-george", "title": "Dr", "full_name": "Jossy P George",
         "location": {"block": "Block I", "floor": "Ground Floor", "room": "014", "on_8th_floor": False}},
        {"id": "naived-george-eapen", "title": "Dr", "full_name": "Naived George Eapen", "location": {}},
    ]}
    (tmp_path / "faculty.json").write_text(json.dumps(faculty), encoding="utf-8")
    raw["kb"] = {"faculty_json": str(tmp_path / "faculty.json")}
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_settings(path)


def fake_turn(text: str, session_id: str = "s1") -> dict:
    """A voice-memory TurnResult for a few scripted questions."""
    base = {"session_id": session_id, "turn_id": "t1", "transcript": text, "question": text, "rewrite_rules": [],
            "speech": "", "audio_wav_base64": None, "escalate": False, "escalate_reason": None, "options": [],
            "ticket": None, "intent": "find_person", "confidence": 0.95, "entities": [], "room_ids": [],
            "latency_ms": {"total": 42.0}}
    q = text.casefold()
    if "phone" in q:
        return {**base, "answer": "I'm sorry, I can't verify a phone number. Please check with the receptionist.",
                "outcome": "escalated", "escalate": True, "escalate_reason": "not_in_kb", "intent": "contact_info",
                "options": ["Yes, call the receptionist", "No, thanks"]}
    if "cabin" in q and "sudhakar" in q:  # a person on this floor, room only in the answer text
        return {**base, "answer": "Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor.",
                "outcome": "answered",
                "entities": [{"type": "faculty", "text": "Dr Nisha Varghese",
                              "canonical_id": "faculty:nisha-varghese", "score": 1.0}]}
    if "nisha" in q:
        return {**base, "answer": "Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor.",
                "outcome": "answered", "room_ids": ["room:874"],
                "entities": [{"type": "faculty", "text": "Dr Nisha Varghese",
                              "canonical_id": "faculty:nisha-varghese", "score": 1.0}]}
    if "ashwini" in q:
        return {**base, "answer": "Dr Ashwini Patil is not on this floor. Their office is in Block II, room 607A.",
                "outcome": "answered", "room_ids": ["room:607a"]}
    if "george" in q:
        return {**base, "answer": "I found more than one person: Dr Jossy P George or Dr Naived George Eapen. "
                "Which one do you mean?", "outcome": "clarification",
                "options": ["Dr Jossy P George", "Dr Naived George Eapen"]}
    return {**base, "answer": "Hello! How can I help?", "outcome": "social", "intent": "greeting"}


@pytest.fixture
def voice_memory():
    """httpx transport standing in for voice-memory's API; records the requests it got."""
    calls: list[tuple[str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") else None
        calls.append((path, body))
        if path == "/turn":
            return httpx.Response(200, json=fake_turn(body["text"], body["session_id"]))
        if path == "/turn/audio":
            return httpx.Response(200, json=fake_turn("where is Dr Nisha Varghese's cabin"))
        if path == "/receptionist":
            return httpx.Response(200, json={"ticket_id": "T1", "timestamp": "2026-10-26T10:00:00+00:00",
                                             "session_id": body["session_id"], "question": "",
                                             "reason": "visitor_button", "answer_given": ""})
        if path == "/session/reset":
            return httpx.Response(200, json={"status": "reset"})
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    transport.calls = calls
    return transport
