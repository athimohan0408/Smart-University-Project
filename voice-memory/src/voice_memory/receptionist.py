"""One visitor turn, end to end: voice in -> memory -> rag-core -> hand-off -> voice out.

    audio --STT--> transcript --memory.rewrite--> standalone question --rag-core POST /answer--> RAGResult
          --memory.observe--> (escalate? -> hand-off offer / ticket) --TTS--> audio

Every stage is timed into ``latency_ms`` (stt, memory, rag, handoff, tts, total) and every turn is
appended to the turn log (``metrics.turn_log``) for the two metrics this module owns: response
latency and query completion rate (``python -m voice_memory.metrics``).
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from voice_memory.config import Settings
from voice_memory.handoff import Handoff, Ticket
from voice_memory.kb import KBView
from voice_memory.memory import ConversationMemory
from voice_memory.rag_client import RagClient, RagUnavailable
from voice_memory.speech.stt import SpeechToText
from voice_memory.speech.tts import Speaker

Outcome = Literal["answered", "clarification", "escalated", "handoff", "declined", "social", "no_speech"]
_SOCIAL = {"greeting", "smalltalk_or_thanks"}
_CLARIFY = re.compile(r"\b(which one do you mean|do you mean|did you mean)\b", re.I)


class TurnResult(BaseModel):
    session_id: str
    turn_id: str
    transcript: str = Field(description="What the visitor said (STT output) or typed.")
    question: str = Field(description="The standalone question sent to rag-core.")
    rewrite_rules: list[str] = Field(default_factory=list)
    answer: str = Field(description="Text for the screen.")
    speech: str = Field(description="Exactly what the voice says (answer + hand-off offer, made speakable).")
    audio_wav_base64: str | None = None
    outcome: Outcome
    escalate: bool
    escalate_reason: str | None = None
    options: list[str] = Field(default_factory=list, description="Buttons for the kiosk UI.")
    ticket: Ticket | None = None
    intent: str | None = None
    confidence: float | None = None
    entities: list[dict] = Field(default_factory=list)
    room_ids: list[str] = Field(default_factory=list, description="room: ids for the navigation module.")
    latency_ms: dict[str, float] = Field(default_factory=dict)


@dataclass
class _Pending:
    question: str
    reason: str
    answer: str


class Receptionist:
    def __init__(
        self,
        settings: Settings,
        *,
        kb: KBView,
        rag: RagClient,
        stt: SpeechToText,
        tts: Speaker,
        handoff: Handoff | None = None,
        memory: ConversationMemory | None = None,
    ):
        self.settings = settings
        self.kb = kb
        self.rag = rag
        self.stt = stt
        self.tts = tts
        self.handoff = handoff or Handoff(settings.handoff, settings.secrets.receptionist_webhook_url)
        self.memory = memory or ConversationMemory(kb, settings.memory)
        self._offers: dict[str, _Pending] = {}  # sessions waiting for a yes/no to "shall I call the receptionist?"
        self._log_lock = threading.Lock()

    # ----------------------------------------------------------------------- entry points

    def handle_audio(self, session_id: str, audio: np.ndarray, sample_rate: int, *, speak: bool = True) -> TurnResult:
        start = time.perf_counter()
        transcript = self.stt.transcribe(audio, sample_rate)
        if not transcript.text.strip():
            return self._local(session_id, "", "Sorry, I didn't catch that. Could you say it again?", "no_speech",
                               start, {"stt": transcript.latency_ms}, speak, source="voice")
        return self.handle_text(session_id, transcript.text, speak=speak, _start=start,
                                _timings={"stt": transcript.latency_ms}, _source="voice")

    def handle_text(self, session_id: str, text: str, *, speak: bool = True, _start: float | None = None,
                    _timings: dict[str, float] | None = None, _source: str = "text") -> TurnResult:
        start = _start if _start is not None else time.perf_counter()
        timings = dict(_timings or {})
        clock = time.perf_counter()

        def lap(stage: str) -> None:
            nonlocal clock
            now = time.perf_counter()
            timings[stage] = timings.get(stage, 0.0) + (now - clock) * 1000
            clock = now

        # 1. a reply to "Would you like me to call the receptionist?"
        if (pending := self._offers.pop(session_id, None)) is not None:
            accepted = self.handoff.reply(text)
            if accepted is True:
                ticket = self.handoff.alert(session_id, pending.question, pending.reason, pending.answer)
                lap("handoff")
                return self._local(session_id, text, self.settings.handoff.confirmed_message, "handoff", start,
                                   timings, speak, ticket=ticket, source=_source, offer_reply=True)
            if accepted is False:
                return self._local(session_id, text, self.settings.handoff.declined_message, "declined", start,
                                   timings, speak, source=_source, offer_reply=True)

        # 2. memory: follow-up -> standalone question
        rewrite = self.memory.rewrite(session_id, text)
        lap("memory")
        if rewrite.local_reply:
            return self._local(session_id, text, rewrite.local_reply, "clarification", start, timings, speak,
                               rules=rewrite.rules, source=_source)

        # 3. rag-core
        try:
            result = self.rag.answer(rewrite.question, rewrite.context)
        except RagUnavailable:
            lap("rag")
            offer = self.handoff.unavailable()
            self._offers[session_id] = _Pending(rewrite.question, "rag_unavailable", "")
            return self._finish(session_id, text, rewrite.question, rewrite.rules, None, offer.speech,
                                offer.speech, "escalated", start, timings, speak, escalate=True,
                                reason="rag_unavailable", options=offer.options, source=_source)
        lap("rag")
        self.memory.observe(session_id, rewrite, result)
        lap("memory")

        # 4. escalation message + receptionist option
        answer = result["answer"]
        speech, options, ticket = answer, [], None
        if result.get("escalate"):
            offer = self.handoff.offer(session_id, rewrite.question, result)
            speech, options, ticket = offer.speech, offer.options, offer.ticket
            if offer.awaiting_reply:
                self._offers[session_id] = _Pending(rewrite.question, result.get("escalate_reason") or "", answer)
            outcome: Outcome = "handoff" if ticket else "escalated"
            lap("handoff")
        elif result.get("intent") in _SOCIAL:
            outcome = "social"
        elif _CLARIFY.search(answer):
            outcome = "clarification"
        else:
            outcome = "answered"
        return self._finish(session_id, text, rewrite.question, rewrite.rules, result, answer, speech, outcome,
                            start, timings, speak, escalate=bool(result.get("escalate")),
                            reason=result.get("escalate_reason"), options=options, ticket=ticket, source=_source)

    def press_receptionist_button(self, session_id: str) -> Ticket:
        """The on-screen 'Talk to the receptionist' button."""
        pending = self._offers.pop(session_id, None)
        state = self.memory.session(session_id)
        return self.handoff.alert(session_id, pending.question if pending else (state.last_question or ""),
                                  "visitor_button", pending.answer if pending else "")

    def new_visitor(self, session_id: str) -> None:
        self._offers.pop(session_id, None)
        self.memory.reset(session_id)

    # ----------------------------------------------------------------------- helpers

    def _local(self, session_id, text, answer, outcome, start, timings, speak, *, ticket=None, rules=(), source="text",
               offer_reply=False):
        return self._finish(session_id, text, text, list(rules), None, answer, answer, outcome, start, timings, speak,
                            escalate=False, reason=None, options=[], ticket=ticket, source=source,
                            offer_reply=offer_reply)

    def _finish(self, session_id, transcript, question, rules, result, answer, speech, outcome, start, timings, speak,
                *, escalate, reason, options, ticket=None, source="text", offer_reply=False) -> TurnResult:
        audio = None
        spoken = speech
        if speak:
            said = self.tts.say(speech)
            spoken, audio = said.text, said.wav
            timings["tts"] = said.latency_ms
        timings["total"] = (time.perf_counter() - start) * 1000
        entities = list((result or {}).get("entities", []))
        turn = TurnResult(
            session_id=session_id, turn_id=uuid.uuid4().hex[:12], transcript=transcript, question=question,
            rewrite_rules=list(rules), answer=answer, speech=spoken,
            audio_wav_base64=base64.b64encode(audio).decode("ascii") if audio else None,
            outcome=outcome, escalate=escalate, escalate_reason=reason, options=options, ticket=ticket,
            intent=(result or {}).get("intent"), confidence=(result or {}).get("confidence"), entities=entities,
            room_ids=[e["canonical_id"] for e in entities if (e.get("canonical_id") or "").startswith("room:")],
            latency_ms={k: round(v, 1) for k, v in timings.items()},
        )
        self._log(turn, result, source, offer_reply)
        return turn

    def _log(self, turn: TurnResult, result: dict | None, source: str, offer_reply: bool) -> None:
        record = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": turn.session_id,
            "turn_id": turn.turn_id,
            "input": source,
            "question": turn.question,
            "rewritten": turn.question != turn.transcript,
            "rewrite_rules": turn.rewrite_rules,
            "outcome": turn.outcome,
            "offer_reply": offer_reply,  # a yes/no to "shall I call the receptionist?": not a new query
            "intent": turn.intent,
            "escalate_reason": turn.escalate_reason,
            "latency_ms": turn.latency_ms,
            "rag_server_ms": ((result or {}).get("latency_ms") or {}).get("total"),
        }
        path = self.settings.metrics.turn_log
        try:
            with self._log_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break a turn


def build_receptionist(settings: Settings, *, rag: RagClient | None = None) -> Receptionist:
    from voice_memory.rag_client import make_rag_client
    from voice_memory.speech.stt import make_stt
    from voice_memory.speech.tts import make_tts

    kb = KBView.load(settings.kb.faculty_json, settings.kb.gazetteer)
    return Receptionist(
        settings,
        kb=kb,
        rag=rag or make_rag_client(settings.rag),
        stt=make_stt(settings.stt, settings.speech.language, kb),
        tts=make_tts(settings.tts, settings.speech),
    )
