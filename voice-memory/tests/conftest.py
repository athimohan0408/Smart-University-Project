"""Offline fixtures: a small KB view with real names and genders from the faculty sheet, a scripted
rag-core stand-in, and settings whose logs go to a temp folder."""

from __future__ import annotations

import re

import pytest

from voice_memory.config import load_settings
from voice_memory.kb import KBView
from voice_memory.rag_client import RagUnavailable

PEOPLE = [  # (id, display name, Gender column)
    ("faculty:nisha-varghese", "Dr Nisha Varghese", "Female"),
    ("faculty:deepa-v-jose", "Dr Deepa V Jose", "Female"),
    ("faculty:binayak-dutta", "Dr Binayak Dutta", "Male"),
    ("faculty:jossy-p-george", "Dr Jossy P George", "Male"),
    ("faculty:naived-george-eapen", "Dr Naived George Eapen", "Male"),
    ("faculty:tegil-j-john", "Dr Tegil J John", "Male"),
]


@pytest.fixture
def kb() -> KBView:
    view = KBView()
    for cid, name, gender in PEOPLE:
        view.add_person(cid, name, gender)
    view.names["room:874"] = "room 874"
    view.names["facility:lab:sample-aiml-lab"] = "Sample AIML Lab"
    view.names["event:2026-10-23:sample-python-workshop"] = "Sample Python Workshop"
    return view


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    return s.model_copy(update={
        "handoff": s.handoff.model_copy(update={"tickets": tmp_path / "handoffs.jsonl"}),
        "metrics": s.metrics.model_copy(update={"turn_log": tmp_path / "turns.jsonl", "report": tmp_path / "m.md"}),
        "stt": s.stt.model_copy(update={"backend": "text"}),
        "tts": s.tts.model_copy(update={"backend": "none"}),
    })


def result(answer, *, entities=(), intent="find_person", escalate=False, reason=None, confidence=0.95):
    return {
        "answer": answer, "confidence": confidence, "escalate": escalate, "escalate_reason": reason,
        "intent": intent, "intent_confidence": 0.99,
        "entities": [{"type": t, "text": text, "canonical_id": cid, "score": 1.0} for t, text, cid in entities],
        "sources": [], "latency_ms": {"total": 20.0},
    }


class ScriptedRag:
    """rag-core stand-in: the first rule whose regex matches the question answers it."""

    def __init__(self, rules):
        self.rules = rules
        self.calls: list[tuple[str, dict]] = []
        self.down = False

    def answer(self, question, context):
        self.calls.append((question, context))
        if self.down:
            raise RagUnavailable("connection refused")
        for pattern, res in self.rules:
            if re.search(pattern, question, re.I):
                return res
        return result("I'm sorry, I can't verify that. Please check with the receptionist at the desk.",
                      intent="find_room", escalate=True, reason="low_confidence", confidence=0.2)


NISHA = ("faculty", "Dr Nisha Varghese", "faculty:nisha-varghese")
DEEPA = ("faculty", "Dr Deepa V Jose", "faculty:deepa-v-jose")
BINAYAK = ("faculty", "Dr Binayak Dutta", "faculty:binayak-dutta")
ROOM_874 = ("room", "room 874", "room:874")
