"""One visitor turn, end to end: voice in -> memory -> rag-core -> hand-off -> voice out.

    audio --STT--> transcript
        --(message / meeting dialogue?)-------------------------------------------------> reply
        --split into questions--> each: memory.rewrite --> standalone question --rag-core POST /answer--> RAGResult
        --memory.observe--> (escalate? -> hand-off offer / ticket) --TTS--> audio

Most turns have one question. "Where is Dr Nisha's cabin and is she free on Friday?" has two: they are answered in
order (so "she" in the second is resolved from the first) and joined into one reply. A question memory itself
must ask first ("Which lift are you at?", "Did you mean Dr Binayak Dutta?") stops the turn there; what was left of
the utterance is asked again once the visitor has answered.

Every stage is timed into ``latency_ms`` (stt, memory, rag, handoff, tts, total) and every question is
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

from voice_memory.compound import split_questions
from voice_memory.config import Settings
from voice_memory.handoff import Handoff, Ticket
from voice_memory.kb import KBView
from voice_memory.memory import ConversationMemory, Rewrite
from voice_memory.origins import OriginResolver
from voice_memory.rag_client import RagClient, RagUnavailable
from voice_memory.speech.stt import SpeechToText
from voice_memory.speech.tts import Speaker
from voice_memory.tasks import TaskManager

Outcome = Literal[
    "answered", "clarification", "escalated", "handoff", "declined", "social", "no_speech",
    "task", "task_done", "task_cancelled",  # a message / meeting request: in progress, sent, called off
]
_SOCIAL = {"greeting", "smalltalk_or_thanks"}
_CLARIFY = re.compile(r"\b(which one do you mean|do you mean|did you mean)\b", re.I)
_LOCATION_INTENTS = {"find_person", "find_room", "facility_info"}
_TASK_PLACEHOLDER = "[message or meeting request]"  # what the turn log keeps of a request dialogue: no names, no contacts
_UNSET = object()


class Origin(BaseModel):
    place_id: str = Field(description="Map place id the visitor is starting from, e.g. facility:lift-north.")
    name: str = Field(description="How to say it: 'the north lift'.")
    changed: bool = Field(default=False, description="Named (or changed) in this turn.")


class TurnPart(BaseModel):
    """One of several questions answered in a single turn."""

    question: str
    answer: str
    outcome: str
    intent: str | None = None
    escalate: bool = False
    escalate_reason: str | None = None


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
    origin: Origin | None = Field(default=None, description="Where the visitor said they are starting from, if not the kiosk.")
    parts: list[TurnPart] = Field(default_factory=list, description="Set when the utterance held several questions.")
    task: str | None = Field(default=None, description="'message' or 'meeting' while a request dialogue is running.")
    request_id: str | None = Field(default=None, description="Set when a message / meeting request was sent.")
    latency_ms: dict[str, float] = Field(default_factory=dict)


@dataclass
class _Pending:
    question: str
    reason: str
    answer: str


@dataclass
class _Run:
    """What happened to one question of an utterance."""

    text: str
    rewrite: Rewrite
    result: dict | None = None  # rag-core's answer
    unavailable: bool = False  # rag-core could not be reached
    elapsed_ms: float = 0.0

    @property
    def local(self) -> bool:
        return self.rewrite.local_reply is not None

    @property
    def answer(self) -> str:
        if self.local:
            return self.rewrite.local_reply or ""
        return (self.result or {}).get("answer", "")

    @property
    def escalated(self) -> bool:
        return self.unavailable or bool((self.result or {}).get("escalate"))

    @property
    def outcome(self) -> str:
        if self.local:
            return "clarification"
        if self.escalated:
            return "escalated"
        if (self.result or {}).get("intent") in _SOCIAL:
            return "social"
        return "clarification" if _CLARIFY.search(self.answer) else "answered"


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
        tasks: TaskManager | None = None,
    ):
        self.settings = settings
        self.kb = kb
        self.rag = rag
        self.stt = stt
        self.tts = tts
        self.handoff = handoff or Handoff(settings.handoff, settings.secrets.receptionist_webhook_url)
        self.memory = memory or ConversationMemory(
            kb, settings.memory, names=settings.names, origins=OriginResolver.load(settings.memory.origins_file)
        )
        self.tasks = tasks or TaskManager(
            settings.tasks, kb, matcher=self.memory.matcher, focus_person=self.memory.focus_person_id,
            webhook_url=settings.secrets.receptionist_webhook_url,
        )
        self._offers: dict[str, _Pending] = {}  # sessions waiting for a yes/no to "shall I call the receptionist?"
        self._queued: dict[str, list[str]] = {}  # questions left over when memory had to ask something first
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

        # 2. "leave a message" / "book a meeting": a short dialogue that ends in a request
        if (task := self.tasks.handle(session_id, text)) is not None:
            lap("tasks")
            if task.person_id:
                self.memory.remember_person(session_id, task.person_id)
            return self._finish(
                session_id, text, text, [], None, task.answer, task.answer, task.outcome, start, timings,
                speak, escalate=False, reason=None, options=task.options, source=_source, task=task.kind,
                request_id=task.request_id,
            )

        # 3. one question, or several: each one is made standalone, sent to rag-core, and remembered
        questions = [text]
        if self.settings.memory.max_parts > 1 and not self.memory.pending_clarification(session_id):
            questions = split_questions(text)
        runs, leftover = self._run(session_id, questions, lap)
        return self._compose(session_id, text, runs, leftover, start, timings, speak, lap, _source)

    def press_receptionist_button(self, session_id: str) -> Ticket:
        """The on-screen 'Talk to the receptionist' button."""
        pending = self._offers.pop(session_id, None)
        state = self.memory.session(session_id)
        return self.handoff.alert(session_id, pending.question if pending else (state.last_question or ""),
                                  "visitor_button", pending.answer if pending else "")

    def new_visitor(self, session_id: str) -> None:
        self._offers.pop(session_id, None)
        self._queued.pop(session_id, None)
        self.tasks.reset(session_id)
        self.memory.reset(session_id)

    # ----------------------------------------------------------------------- questions

    def _run(self, session_id: str, questions: list[str], lap) -> tuple[list[_Run], list[str]]:
        """Answer the questions in order. Stops at the first one memory must ask about; returns what is left."""
        runs: list[_Run] = []
        queue = list(questions)
        limit = self.settings.memory.max_parts
        carried = self._queued.pop(session_id, [])
        i = 0
        while i < len(queue) and len(runs) < limit:
            tick = time.perf_counter()
            rewrite = self.memory.rewrite(session_id, queue[i])
            lap("memory")
            run = _Run(queue[i], rewrite)
            if i == 0 and carried and any(r.startswith(("clarification", "name", "origin")) for r in rewrite.rules):
                queue.extend(carried)  # the visitor just answered what was asked: now the rest of what they said
                carried = []
            if not run.local:
                try:
                    run.result = self.rag.answer(rewrite.question, rewrite.context)
                except RagUnavailable:
                    run.unavailable = True
                lap("rag")
                if run.result is not None:
                    self.memory.observe(session_id, rewrite, run.result)
                    lap("memory")
            run.elapsed_ms = (time.perf_counter() - tick) * 1000
            runs.append(run)
            i += 1
            if run.local or run.unavailable:
                break  # nothing sensible to say to the later questions yet
        leftover = queue[i:]
        if runs and runs[-1].local and leftover:
            self._queued[session_id] = leftover
        return runs, leftover

    def _compose(self, session_id, text, runs: list[_Run], leftover: list[str], start, timings, speak, lap, source) -> TurnResult:
        answers = [r.answer for r in runs]
        results = [r.result for r in runs if r.result]
        escalated = next((r for r in runs if r.escalated), None)
        speech_tail, options, ticket = "", [], None
        if escalated is not None:
            if escalated.unavailable:
                offer = self.handoff.unavailable()
                reason = "rag_unavailable"
                said = self.settings.handoff.unavailable_message
                answers[runs.index(escalated)] = said
                self._offers[session_id] = _Pending(escalated.rewrite.question, reason, "")
                speech_tail, options = offer.speech[len(said):].strip(), offer.options
            else:
                offer = self.handoff.offer(session_id, escalated.rewrite.question, escalated.result)
                reason = escalated.result.get("escalate_reason")
                speech_tail = offer.speech[len(escalated.answer):].strip()
                options, ticket = offer.options, offer.ticket
                if offer.awaiting_reply:
                    self._offers[session_id] = _Pending(escalated.rewrite.question, reason or "", escalated.answer)
            lap("handoff")
        else:
            reason = None
        local = next((r for r in runs if r.local), None)
        if local is not None:
            options = list(local.rewrite.options)

        entities = _merge_entities(results)
        room_ids = list(dict.fromkeys(e["canonical_id"] for e in entities if (e.get("canonical_id") or "").startswith("room:")))
        last = runs[-1].rewrite
        origin = None
        if last.origin:
            origin = Origin(place_id=last.origin, name=last.origin_name or last.origin, changed=any(r.rewrite.origin_changed for r in runs))
        overflow = ""
        if leftover and local is None:
            overflow = "I've answered the first few questions. Ask me the rest and I'll answer those too."
        note = ""
        if any(r.rewrite.origin_changed for r in runs) and escalated is None and local is None and (
            room_ids or any((res.get("intent") in _LOCATION_INTENTS) for res in results)
        ):
            note = f"I'll show you the way from {last.origin_name or 'the kiosk'}."
        screen = " ".join(a for a in [*answers, note, overflow] if a)
        spoken = " ".join(a for a in [*answers, note, overflow, speech_tail] if a)

        outcomes = [r.outcome for r in runs]
        if local is not None:
            outcome: Outcome = "clarification"
        elif escalated is not None:
            outcome = "handoff" if ticket else "escalated"
        elif all(o == "social" for o in outcomes):
            outcome = "social"
        elif "clarification" in outcomes:
            outcome = "clarification"
        else:
            outcome = "answered"
        parts = []
        if len(runs) > 1:
            parts = [TurnPart(question=r.rewrite.question, answer=r.answer, outcome=r.outcome,
                              intent=(r.result or {}).get("intent"), escalate=r.escalated,
                              escalate_reason=(r.result or {}).get("escalate_reason")) for r in runs]
        return self._finish(
            session_id, text, " | ".join(r.rewrite.question for r in runs), [rule for r in runs for rule in r.rewrite.rules],
            results[0] if results else None, screen, spoken, outcome, start, timings, speak,
            escalate=escalated is not None, reason=reason, options=options, ticket=ticket, source=source,
            entities=entities, room_ids=room_ids, origin=origin, parts=parts,
            confidence=min((r.get("confidence") for r in results if r.get("confidence") is not None), default=None),
            part_logs=[(r.rewrite.question, r.outcome, r.result, r.rewrite) for r in runs],
        )

    # ----------------------------------------------------------------------- helpers

    def _local(self, session_id, text, answer, outcome, start, timings, speak, *, ticket=None, rules=(), source="text",
               offer_reply=False):
        return self._finish(session_id, text, text, list(rules), None, answer, answer, outcome, start, timings, speak,
                            escalate=False, reason=None, options=[], ticket=ticket, source=source,
                            offer_reply=offer_reply)

    def _finish(self, session_id, transcript, question, rules, result, answer, speech, outcome, start, timings, speak,
                *, escalate, reason, options, ticket=None, source="text", offer_reply=False, entities=None, room_ids=None,
                origin=_UNSET, parts=(), confidence=None, task=None, request_id=None, part_logs=None) -> TurnResult:
        audio = None
        spoken = speech
        if speak:
            said = self.tts.say(speech)
            spoken, audio = said.text, said.wav
            timings["tts"] = said.latency_ms
        timings["total"] = (time.perf_counter() - start) * 1000
        entities = list(entities if entities is not None else (result or {}).get("entities", []))
        if room_ids is None:
            room_ids = [e["canonical_id"] for e in entities if (e.get("canonical_id") or "").startswith("room:")]
        if origin is _UNSET:  # replies that don't come from a question (hand-off, tasks): the start point still applies
            kept = self.memory.origin_of(session_id)
            origin = Origin(place_id=kept[0], name=kept[1]) if kept else None
        turn = TurnResult(
            session_id=session_id, turn_id=uuid.uuid4().hex[:12], transcript=transcript, question=question,
            rewrite_rules=list(rules), answer=answer, speech=spoken,
            audio_wav_base64=base64.b64encode(audio).decode("ascii") if audio else None,
            outcome=outcome, escalate=escalate, escalate_reason=reason, options=list(options), ticket=ticket,
            intent=(result or {}).get("intent"),
            confidence=confidence if confidence is not None else (result or {}).get("confidence"),
            entities=entities, room_ids=list(room_ids), origin=origin, parts=list(parts), task=task,
            request_id=request_id, latency_ms={k: round(v, 1) for k, v in timings.items()},
        )
        self._log(turn, result, source, offer_reply, part_logs)
        return turn

    def _log(self, turn: TurnResult, result: dict | None, source: str, offer_reply: bool, part_logs=None) -> None:
        base = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session_id": turn.session_id,
            "input": source,
            "offer_reply": offer_reply,  # a yes/no to "shall I call the receptionist?": not a new query
        }
        if not part_logs:
            records = [{**base, "turn_id": turn.turn_id, "question": turn.question,
                        "rewritten": turn.question != turn.transcript, "rewrite_rules": turn.rewrite_rules,
                        "outcome": turn.outcome, "intent": turn.intent, "escalate_reason": turn.escalate_reason,
                        "latency_ms": turn.latency_ms,
                        "rag_server_ms": ((result or {}).get("latency_ms") or {}).get("total")}]
        else:
            # one record per question, so completion rate counts each question asked; the whole turn's timings
            # belong to the first one (the visitor waited once)
            records = []
            for i, (question, outcome, res, rewrite) in enumerate(part_logs):
                records.append({
                    **base, "turn_id": turn.turn_id if i == 0 else f"{turn.turn_id}-{i + 1}", "question": question,
                    "rewritten": question != turn.transcript, "rewrite_rules": list(rewrite.rules), "outcome": outcome,
                    "intent": (res or {}).get("intent"), "escalate_reason": (res or {}).get("escalate_reason"),
                    "latency_ms": turn.latency_ms if i == 0 else {},
                    "rag_server_ms": ((res or {}).get("latency_ms") or {}).get("total"),
                    **({"part": i + 1, "of": len(part_logs)} if len(part_logs) > 1 else {}),
                })
        if turn.outcome.startswith("task"):
            records[0]["question"] = _TASK_PLACEHOLDER
            records[0]["kind"] = turn.task
        path = self.settings.metrics.turn_log
        try:
            with self._log_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    for record in records:
                        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break a turn


def _merge_entities(results: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for res in results:
        for e in res.get("entities", []):
            key = (e.get("canonical_id"), e.get("text"), e.get("type"))
            if key not in seen:
                seen.add(key)
                out.append(e)
    return out


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
