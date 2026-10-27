"""Full turns with a scripted rag-core: memory, hand-off, turn log, metrics and the HTTP API."""

import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from tests.conftest import NISHA, ROOM_874, ScriptedRag, result
from voice_memory import api
from voice_memory.metrics import compute, read_log, to_markdown
from voice_memory.receptionist import Receptionist
from voice_memory.speech.stt import TextSTT, Transcript
from voice_memory.speech.tts import NoTTS, Speaker

RULES = [
    (r"^hello", result("Hello, and welcome.", intent="greeting")),
    (r"where is Dr Nisha Varghese's (cabin|office)",
     result("Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor, here on this floor.",
            entities=[NISHA])),
    (r"is Dr Nisha Varghese free",
     result("On Friday, Dr Nisha Varghese's consultation hours are not listed.", entities=[NISHA],
            intent="person_availability")),
    (r"phone", result("I'm sorry, I can't verify a phone number for Dr Nisha Varghese. Please check with the receptionist "
                      "at the desk.", entities=[NISHA], intent="contact_info", escalate=True, reason="not_in_kb")),
    (r"Dr George\b", result("I found more than one person who matches Dr George: Dr Jossy P George or Dr Naived George "
                            "Eapen. Which one do you mean?", entities=[("faculty", "Dr George", None)])),
    (r"Dr Jossy P George", result("Dr Jossy P George's office is ...", entities=[("faculty", "Dr Jossy P George",
                                                                                 "faculty:jossy-p-george")])),
    (r"room 874", result("Room 874 is the office of Dr Nisha Varghese.", entities=[ROOM_874], intent="find_room")),
    (r"talk to (a|someone|the receptionist)", result("Of course. Please speak to the receptionist at the desk.",
                                                     intent="escalate_to_human", escalate=True, reason="user_requested")),
]


class FakeSTT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio, sample_rate):
        time.sleep(0.05)
        return Transcript(text=self.text, latency_ms=50.0)


@pytest.fixture
def rag():
    return ScriptedRag(RULES)


@pytest.fixture
def desk(settings, kb, rag):
    return Receptionist(settings, kb=kb, rag=rag, stt=TextSTT(), tts=Speaker(NoTTS(), settings.speech))


def test_follow_up_reaches_rag_core_resolved(desk, rag):
    desk.handle_text("k1", "where is Dr Nisha Varghese's cabin?")
    turn = desk.handle_text("k1", "is she free on Friday?")
    assert rag.calls[-1][0] == "is Dr Nisha Varghese free on Friday?"
    assert turn.outcome == "answered" and turn.rewrite_rules == ["person:female"]
    assert turn.speech.startswith("On Friday, Doctor Nisha Varghese")  # spoken copy is speakable


def test_navigation_gets_room_ids(desk):
    turn = desk.handle_text("k1", "who sits in room 874")
    assert turn.room_ids == ["room:874"]


def test_escalation_offer_then_yes_creates_a_ticket(desk, settings):
    turn = desk.handle_text("k1", "what is Dr Nisha Varghese's phone number?")
    assert turn.outcome == "escalated" and turn.escalate_reason == "not_in_kb"
    assert turn.options == ["Talk to the receptionist", "Ask another question"]
    assert turn.speech.endswith("Would you like me to call the receptionist for you?")
    yes = desk.handle_text("k1", "yes please")
    assert yes.outcome == "handoff" and yes.ticket is not None
    assert yes.ticket.question == "what is Dr Nisha Varghese's phone number?" and yes.ticket.reason == "not_in_kb"


def test_escalation_offer_then_no_or_a_new_question(desk, rag):
    desk.handle_text("k1", "what is Dr Nisha Varghese's phone number?")
    assert desk.handle_text("k1", "no thanks").outcome == "declined"
    desk.handle_text("k1", "what is Dr Nisha Varghese's phone number?")
    turn = desk.handle_text("k1", "where is her office")  # neither yes nor no: just the next question
    assert turn.outcome == "answered" and rag.calls[-1][0] == "where is Dr Nisha Varghese's office"


def test_asking_for_a_human_alerts_immediately(desk, settings):
    turn = desk.handle_text("k1", "I want to talk to someone")
    assert turn.outcome == "handoff" and turn.ticket and turn.ticket.reason == "user_requested"


def test_clarification_round_trip(desk, rag):
    assert desk.handle_text("k1", "where is Dr George?").outcome == "clarification"
    turn = desk.handle_text("k1", "the first one")
    assert rag.calls[-1][0] == "where is Dr Jossy P George?" and turn.outcome == "answered"


def test_rag_core_down_escalates_politely(desk, rag):
    rag.down = True
    turn = desk.handle_text("k1", "where is Dr Nisha Varghese's cabin?")
    assert turn.escalate and turn.escalate_reason == "rag_unavailable" and "receptionist" in turn.speech
    rag.down = False
    assert desk.handle_text("k1", "yes").ticket.reason == "rag_unavailable"


def test_receptionist_button(desk):
    desk.handle_text("k1", "where is Dr Nisha Varghese's cabin?")
    ticket = desk.press_receptionist_button("k1")
    assert ticket.reason == "visitor_button" and ticket.question == "where is Dr Nisha Varghese's cabin?"


def test_voice_turn_times_every_stage(settings, kb, rag):
    desk = Receptionist(settings, kb=kb, rag=rag, stt=FakeSTT("where is Dr Nisha Varghese's cabin"),
                        tts=Speaker(NoTTS(), settings.speech))
    turn = desk.handle_audio("k1", np.zeros(16000, np.float32), 16000)
    assert turn.transcript == "where is Dr Nisha Varghese's cabin" and turn.outcome == "answered"
    assert {"stt", "memory", "rag", "tts", "total"} <= set(turn.latency_ms)
    assert turn.latency_ms["total"] >= turn.latency_ms["stt"] == 50.0  # total runs from the start of STT
    silent = Receptionist(settings, kb=kb, rag=rag, stt=FakeSTT(""), tts=Speaker(NoTTS(), settings.speech))
    assert silent.handle_audio("k1", np.zeros(10, np.float32), 16000).outcome == "no_speech"


def test_turn_log_and_metrics(desk, settings):
    desk.handle_text("k1", "hello")
    desk.handle_text("k1", "where is Dr Nisha Varghese's cabin?")  # answered
    desk.handle_text("k1", "where is Dr George?")  # clarification ...
    desk.handle_text("k1", "the first one")  # ... completed: one query
    desk.handle_text("k1", "what is her phone number")  # escalated
    desk.handle_text("k1", "yes")  # offer reply: not a new query
    desk.handle_text("k2", "where is Dr George?")  # abandoned
    records = read_log(settings.metrics.turn_log)
    assert len(records) == 7 and all("total" in r["latency_ms"] for r in records)
    assert not any("audio" in json.dumps(r) for r in records)
    report = compute(records, settings.memory.session_idle_s)
    c = report.completion
    assert (c.completed, c.escalated, c.abandoned, c.queries) == (2, 1, 1, 4)
    assert c.rate == 0.5 and c.escalations_by_reason == {"not_in_kb": 1}
    assert c.sessions == 2 and c.sessions_fully_completed == 0
    md = to_markdown(report, 3000, 0.85, "turns.jsonl")
    assert "Completion rate **50.0%**" in md and "| total |" in md


def test_http_api(desk, monkeypatch):
    monkeypatch.setitem(api._state, "r", desk)
    with TestClient(api.app) as client:
        first = client.post("/turn", json={"session_id": "k9", "text": "where is Dr Nisha Varghese's cabin?", "speak": False})
        assert first.status_code == 200 and first.json()["outcome"] == "answered"
        follow = client.post("/turn", json={"session_id": "k9", "text": "where is her office", "speak": False}).json()
        assert follow["question"] == "where is Dr Nisha Varghese's office"
        assert client.post("/receptionist", json={"session_id": "k9"}).json()["reason"] == "visitor_button"
        assert client.post("/session/reset", json={"session_id": "k9"}).json() == {"status": "reset"}
        again = client.post("/turn", json={"session_id": "k9", "text": "where is her office", "speak": False}).json()
        assert again["question"] == "where is her office"  # new visitor: nothing remembered
        bad = client.post("/turn/audio", data={"session_id": "k9"}, files={"audio": ("a.wav", b"not a wav")})
        assert bad.status_code == 422
