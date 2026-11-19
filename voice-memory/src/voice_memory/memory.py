"""Conversation memory: turn a follow-up into a standalone question before it reaches rag-core.

rag-core's contract says the question arrives *resolved*: "where is her office?" must reach it as
"where is Dr Deepa V Jose's office?". This module keeps a small per-visitor memory and rewrites:

- **pronouns for people**: she / her / hers / he / him / his, and "ma'am" / "sir" used alone, become
  the last person discussed, chosen by the sheet's Gender column ("her" never picks a man);
- **places and events**: "that room", "the same lab", "there", "it" become the last room, place or
  event discussed;
- **ellipsis**: "and Dr Sridevi?" / "what about room 875?" / "and on Friday?" reuse the previous
  question with that one part swapped;
- **clarification replies**: after "Which one do you mean: Dr Jossy P George or Dr Naived George
  Eapen?", "the first one" / "Jossy" / "yes" becomes the previous question with the chosen name.

Rules are deterministic and conservative. When a pronoun has no safe referent (nobody discussed yet,
or "her" but the last person is a man), the question is left as it is and ``unresolved`` says so:
rag-core then finds no entity and escalates instead of guessing.

Usage::

    memory = ConversationMemory(kb, settings.memory)
    rw = memory.rewrite("kiosk-01", "where is her office?")
    result = rag.answer(rw.question, rw.context)
    memory.observe("kiosk-01", rw, result)
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from voice_memory.config import MemorySettings, NamesSettings
from voice_memory.kb import KBView
from voice_memory.names import NameMatcher
from voice_memory.origins import OriginResolver

FocusKind = Literal["person", "place", "event", "subject"]

_FEMALE = {"she", "her", "hers", "herself"}
_MALE = {"he", "him", "his", "himself"}
_HONORIFIC_FEMALE = {"ma'am", "maam", "mam", "madam", "miss"}
_HONORIFIC_MALE = {"sir"}
# After "her"/"his" these mean the pronoun is an object or a time, not a possessive ("meet her today").
_NOT_A_NOUN = {
    "today", "tomorrow", "now", "later", "on", "at", "in", "after", "before", "about", "please", "again", "there",
    "here", "for", "and", "or", "if", "when", "to", "from", "by", "this", "next", "right", "sometime", "the", "a",
}
_ORDINALS = {
    "first": 0, "1st": 0, "one": 0, "1": 0, "former": 0,
    "second": 1, "2nd": 1, "two": 1, "2": 1, "latter": -1, "last": -1,
    "third": 2, "3rd": 2, "three": 2, "3": 2,
}
# words that don't say which option is meant ("the west one", "number two", "I'm at the north")
_FILLER = frozenset({"the", "a", "an", "ones", "please", "option", "number", "no", "i", "im", "am", "at", "near", "by",
                     "side", "its", "is", "that", "this", "pick", "choose", "want", "mean", "meant", "said", "go", "it", "one"})
_YES = re.compile(r"^\s*(yes|yeah|yep|yup|correct|right|exactly|that's (?:him|her|right|the one)|haan|ha|ok(?:ay)?)\b", re.I)
_NO = re.compile(r"^\s*(no|nope|neither|none|not (?:him|her|them|that one)|wrong)\b", re.I)
_ELLIPSIS = re.compile(r"^\s*(?:and|also|what about|how about|and what about|then)\s+(?P<rest>.+?)\s*[?.!]*\s*$", re.I)
_WH = re.compile(r"\b(where|who|when|what|which|how|is|are|can|does|do|will|tell|show|any)\b", re.I)
_DAY_WORDS = r"(monday|tuesday|wednesday|thursday|friday|saturday|today|tomorrow|now|this (?:morning|afternoon|evening))"
_DAY = re.compile(rf"\b(?:on\s+)?{_DAY_WORDS}\b", re.I)
_ROOM_REF = re.compile(r"\b(that|this|the same)\s+(room|cabin|office|place|lab|classroom|hall|one)\b", re.I)
_THERE = re.compile(r"\b(get|go|going|reach|walk|way)\b.*\bthere\b|\bthere\b.*\b(how|far|way)\b", re.I)
_EVENT_REF = re.compile(r"\b(that|this|the same|the)\s+(event|seminar|workshop|talk|session|lecture|fest|hackathon|programme)\b", re.I)
_IT = re.compile(r"\b(it)\b", re.I)
_IT_CUE = re.compile(r"\b(where|when|what time|open|close|closes|opens|start|starts|end|ends|held|located|far|reach)\b", re.I)
_CLARIFY = re.compile(r"\b(which one do you mean|do you mean|did you mean)\b", re.I)


@dataclass
class Focus:
    kind: FocusKind
    canonical_id: str
    name: str
    said_as: str  # surface form in the last question that mentioned it ("Dr Nisha Varghese", "room 874")
    turn: int
    gender: str | None = None


@dataclass
class Clarification:
    question: str  # the resolved question that got "which one do you mean?"
    mention: str  # the ambiguous words in it ("Dr George")
    options: list[str]  # display names offered, in the order they were said
    kind: str = "rag"  # rag: rag-core asked (question already resolved); name / origin: memory asked (re-run on the pick)


@dataclass
class SessionState:
    session_id: str
    updated: float = field(default_factory=time.monotonic)
    turn: int = 0
    focus: dict[FocusKind, Focus] = field(default_factory=dict)
    last_question: str | None = None
    last_day: str | None = None
    pending: Clarification | None = None
    recent_ids: list[str] = field(default_factory=list)
    origin: str | None = None  # map place id the visitor said they are starting from; None = the kiosk

    def latest(self, *kinds: FocusKind) -> Focus | None:
        found = [self.focus[k] for k in kinds if k in self.focus]
        return max(found, key=lambda f: f.turn) if found else None


class Rewrite(BaseModel):
    """What memory did with one utterance."""

    original: str
    question: str = Field(description="Standalone question to send to rag-core.")
    changed: bool
    rules: list[str] = Field(default_factory=list, description="Which rules fired, for logs and tests.")
    unresolved: list[str] = Field(default_factory=list, description="References memory could not resolve.")
    context: dict[str, Any] = Field(default_factory=dict, description="rag-core RAGRequest.context.")
    local_reply: str | None = Field(
        default=None, description="Answer memory gives itself (the visitor said 'no' to 'do you mean X?')."
    )
    origin: str | None = Field(default=None, description="Map place id the visitor is starting from, or None (the kiosk).")
    origin_name: str | None = None
    origin_changed: bool = Field(default=False, description="The visitor named a new start point in this utterance.")
    options: list[str] = Field(default_factory=list, description="Buttons for a question memory asks itself.")


def _sub_word(pattern: str, repl: str, text: str, flags=re.I) -> tuple[str, int]:
    return re.subn(rf"(?<![\w']){pattern}(?![\w'])", repl, text, flags=flags)


class ConversationMemory:
    """Per-session memory. Thread-safe; sessions expire after ``session_idle_s`` of silence."""

    def __init__(self, kb: KBView, cfg: MemorySettings, clock=time.monotonic, *, names: NamesSettings | None = None,
                 origins: OriginResolver | None = None):
        self._kb = kb
        self._cfg = cfg
        self._clock = clock
        self._matcher = (
            NameMatcher.from_kb(kb, auto_min=names.auto_min, ask_min=names.ask_min, margin=names.margin,
                                max_options=names.max_options)
            if names is not None and names.enabled else None
        )
        self._origins = origins or OriginResolver()
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------------- sessions

    def session(self, session_id: str) -> SessionState:
        with self._lock:
            state = self._sessions.get(session_id)
            now = self._clock()
            if state is None or now - state.updated > self._cfg.session_idle_s or state.turn >= self._cfg.max_turns:
                state = SessionState(session_id, updated=now)
                self._sessions[session_id] = state
            return state

    def reset(self, session_id: str) -> None:
        """A new visitor (the UI's 'start over', or the kiosk saw nobody for a while)."""
        with self._lock:
            self._sessions.pop(session_id, None)

    # ----------------------------------------------------------------------- rewrite

    def rewrite(self, session_id: str, utterance: str) -> Rewrite:
        state = self.session(session_id)
        text = re.sub(r"\s+", " ", utterance).strip()
        previous_origin = state.origin

        if state.pending is not None:
            kind = state.pending.kind
            picked = self._clarification_reply(state, text)
            if picked is not None:
                question, pick_rules, local = picked
                if local is not None or kind == "rag":
                    return self._finish(state, text, question, pick_rules, [], local, previous_origin)
                # memory's own question (which lift? which name?): the picked question goes through the full pipeline
                return self._rewrite_core(state, text, question, pick_rules, previous_origin)
        return self._rewrite_core(state, text, text, [], previous_origin)

    def _rewrite_core(self, state: SessionState, original: str, text: str, rules: list[str],
                      previous_origin: str | None) -> Rewrite:
        unresolved: list[str] = []

        # a start point ("from the lift"): taken out of the question and remembered
        question, origin_rules, local = self._apply_origin(state, text)
        rules = [*rules, *origin_rules]
        if local is not None:
            options = list(state.pending.options) if state.pending and state.pending.kind == "origin" else []
            return self._finish(state, original, question, rules, [], local, previous_origin, options=options)

        # misheard / misspelled names: fixed quietly, or asked about
        question, name_rules, local, options = self._fix_names(state, question)
        rules = [*rules, *name_rules]
        if local is not None:
            return self._finish(state, original, question, rules, [], local, previous_origin, options=options)

        if (m := _ELLIPSIS.match(question)) and state.last_question and not _WH.match(m["rest"]):
            swapped = self._swap(state, m["rest"])
            if swapped:
                question, rule = swapped
                rules.append(rule)

        if not any(r.startswith("ellipsis") or r == "origin:again" for r in rules):
            question = self._resolve_people(state, question, rules, unresolved)
            question = self._resolve_places(state, question, rules, unresolved)
        return self._finish(state, original, question, rules, unresolved, None, previous_origin)

    # -- start point

    def _apply_origin(self, state: SessionState, text: str) -> tuple[str, list[str], str | None]:
        match = self._origins.find(text)
        if match is None:
            return text, [], None
        rest = self._origins.strip(text, match)
        if match.kiosk:
            state.origin = None
            rule, said = "origin:kiosk", "the kiosk"
        elif len(match.candidates) == 1:
            state.origin = match.candidates[0].id
            rule, said = "origin:set", match.candidates[0].name
        else:
            options = [c.name for c in match.candidates]
            noun = match.candidates[0].aliases[0]
            state.pending = Clarification(text, match.mention, options, kind="origin")
            listed = ", ".join(options[:-1]) + f" or {options[-1]}"
            return text, ["origin:ask"], f"There is more than one {noun} on this floor. Which one are you at: {listed}?"
        if self._origins.only_a_start(rest):
            if state.last_question:
                return state.last_question, [rule, "origin:again"], None
            return rest, [rule], f"Okay, I'll start from {said}. Where would you like to go?"
        return rest, [rule], None

    # -- misheard names

    def _fix_names(self, state: SessionState, text: str) -> tuple[str, list[str], str | None, list[str]]:
        if self._matcher is None:
            return text, [], None, []
        fixes = self._matcher.scan(text)
        if not fixes:
            return text, [], None, []
        if asks := [f for f in fixes if not f.replacement]:
            fix = asks[0]
            options = list(fix.options)
            state.pending = Clarification(text, fix.heard, options, kind="name")
            if len(options) == 1:
                reply = f"I did not catch that name clearly. Did you mean {options[0]}?"
            else:
                listed = ", ".join(options[:-1]) + f" or {options[-1]}"
                reply = f"I did not catch that name clearly. Which one did you mean: {listed}?"
            return text, ["name:ask"], reply, options
        return self._matcher.apply(text, fixes), ["name:corrected"], None, []

    def _finish(self, state, original, question, rules, unresolved, local_reply=None, previous_origin=None,
                options=()) -> Rewrite:
        question = re.sub(r"\s+", " ", question).strip()
        context: dict[str, Any] = {"session_id": state.session_id}
        if self._cfg.send_prior_entities and state.recent_ids:
            context["prior_entities"] = state.recent_ids[-self._cfg.max_prior_entities :]
        if self._cfg.send_last_entities and (f := state.latest("person", "place", "event", "subject")):
            context["last_entities"] = [f.canonical_id]
        return Rewrite(
            original=original, question=question, changed=question != original, rules=rules,
            unresolved=unresolved, context=context, local_reply=local_reply, options=list(options),
            origin=state.origin, origin_name=self._origins.name_of(state.origin) if state.origin else None,
            origin_changed=state.origin != previous_origin,
        )

    # -- people

    def _person_for(self, state: SessionState, gender: str | None) -> Focus | None:
        person = state.focus.get("person")
        if person is None:
            return None
        if gender is not None and person.gender is not None and person.gender != gender:
            return None  # "her" after talking about a man: don't guess
        return person

    def _resolve_people(self, state: SessionState, q: str, rules: list[str], unresolved: list[str]) -> str:
        words = set(re.findall(r"[a-z']+", q.casefold()))
        fem, masc = words & _FEMALE, words & _MALE
        hon_f, hon_m = words & _HONORIFIC_FEMALE, words & _HONORIFIC_MALE
        if not (fem or masc or hon_f or hon_m):
            return q
        if self._kb.mentions_person(q):
            return q  # "where is Dr X and is she free": the pronoun is about the name in the question
        for gender, pronouns, honorifics in (("female", fem, hon_f), ("male", masc, hon_m)):
            if not (pronouns or honorifics):
                continue
            person = self._person_for(state, gender)
            if person is None:
                unresolved.extend(sorted(pronouns | honorifics))
                continue
            name = person.name
            before = q
            if pronouns:
                possessive = "her" if gender == "female" else "his"
                # possessive + noun ("her office", "his email") -> "Dr X's office"
                q = re.sub(
                    rf"(?<![\w']){possessive}\s+(?!(?:{'|'.join(_NOT_A_NOUN)})\b)(?=[a-z])",
                    f"{name}'s ", q, flags=re.I,
                )
                q, _ = _sub_word("hers" if gender == "female" else "his", f"{name}'s", q)
                for p in ("she", "he", "her", "him", "herself", "himself"):
                    if p in pronouns:
                        q, _ = _sub_word(p, name, q)
            if honorifics:
                # "is ma'am in the cabin?" / "is sir free?": the honorific stands for the person.
                for h in sorted(honorifics, key=len, reverse=True):
                    q, _ = _sub_word(re.escape(h), name, q)
                q = re.sub(rf"\b(the|your)\s+{re.escape(name)}", name, q, flags=re.I)
            if q != before:
                rules.append(f"person:{gender}")
        return q

    # -- places and events

    def _resolve_places(self, state: SessionState, q: str, rules: list[str], unresolved: list[str]) -> str:
        place = state.latest("place")
        event = state.latest("event")
        if m := _EVENT_REF.search(q):
            if event is not None and not _mentions_name(q, event):
                q = q[: m.start()] + event.name + q[m.end():]
                rules.append("event:that")
                return q
        if m := _ROOM_REF.search(q):
            word = m.group(2).casefold()
            target = place
            if word in ("cabin", "office") and (person := state.focus.get("person")) and (
                place is None or person.turn >= place.turn
            ):
                q = q[: m.start()] + f"{person.name}'s {word}" + q[m.end():]
                rules.append("place:office")
                return q
            if word == "one":
                target = state.latest("place", "event")
            if target is not None:
                q = q[: m.start()] + target.name + q[m.end():]
                rules.append(f"{target.kind}:that")
            else:
                unresolved.append(m.group(0))
            return q
        if _THERE.search(q):
            target = state.latest("place", "person")
            if target is not None:
                where = target.name if target.kind == "place" else f"{target.name}'s office"
                q, n = re.subn(r"\b(get|go|going|walk|come)\s+there\b", rf"\1 to {where}", q, flags=re.I)
                if not n:
                    q, n = _sub_word("there", where, q)
                if n:
                    rules.append("place:there")
            else:
                unresolved.append("there")
            return q
        if _IT.search(q) and _IT_CUE.search(q):
            target = state.latest("place", "event")
            person = state.focus.get("person")
            if target is not None and (person is None or target.turn >= person.turn):
                q, n = _sub_word("it", target.name, q)
                if n:
                    rules.append(f"{target.kind}:it")
            elif target is None and person is None:
                unresolved.append("it")
        return q

    # -- ellipsis

    def _swap(self, state: SessionState, rest: str) -> tuple[str, str] | None:
        """'and Dr Sridevi?' -> previous question with the person swapped; 'and on Friday?' -> day swapped."""
        prev = state.last_question or ""
        if _DAY.fullmatch(rest.strip()):
            day = rest.strip()
            if state.last_day and re.search(rf"\b(on\s+)?{re.escape(state.last_day)}\b", prev, re.I):
                new = re.sub(rf"\b(on\s+)?{re.escape(state.last_day)}\b", _with_on(day), prev, count=1, flags=re.I)
            else:
                new = re.sub(r"\s*\?*\s*$", "", prev) + f" {_with_on(day)}?"
            return new, "ellipsis:day"
        kind: FocusKind
        if re.match(r"(room|cabin)\s+\w+", rest, re.I):
            kind = "place"
        elif self._kb.mentions_person(rest):
            kind = "person"
        else:
            kind = "subject" if "subject" in state.focus else ("place" if "place" in state.focus else "person")
        old = state.focus.get(kind)
        if old is None or not old.said_as or old.said_as.casefold() not in prev.casefold():
            return None
        new_text = rest.strip().rstrip("?.! ")
        if kind == "person" and re.search(re.escape(old.said_as) + r"(?:'s|’s)\b", prev, re.IGNORECASE):
            # "and Dr Sridevi's office?" after "... Dr Nisha's office?": the name replaces the name, not the noun after it
            if possessive := re.match(r"(?P<name>.+?)(?:'s|’s)\b", new_text):
                new_text = possessive["name"]
        new = re.sub(re.escape(old.said_as), new_text.replace("\\", ""), prev, count=1, flags=re.I)
        return new, f"ellipsis:{kind}"

    # -- clarification replies

    def _clarification_reply(self, state: SessionState, text: str) -> tuple[str, list[str], str | None] | None:
        pending = state.pending
        options = pending.options
        low = text.casefold().strip(" .?!")
        choice: str | None = None
        if _NO.match(low) and len(low.split()) <= 4:
            state.pending = None
            return text, ["clarification:declined"], (
                "Okay. Could you tell me the person's full name? Or I can call the receptionist for you."
            )
        if len(options) == 1 and _YES.match(low):
            choice = options[0]
        words = re.findall(r"[a-z0-9]+", low)
        content = [w for w in words if w not in _FILLER and w not in _ORDINALS]
        if choice is None and not content:  # "the first one", "second", "1": a position, with nothing else said
            for word in words:
                if word in _ORDINALS and len(words) <= 4:
                    idx = _ORDINALS[word]
                    if -len(options) <= idx < len(options):
                        choice = options[idx]
                        break
        if choice is None:
            core = " ".join(content) or low  # "the west one" -> "west": the words that say WHICH one
            scored = [
                (max(fuzz.partial_ratio(core, o.casefold()), fuzz.token_set_ratio(core, o.casefold())), o) for o in options
            ]
            scored.sort(reverse=True)
            if scored and scored[0][0] >= self._cfg.option_match_min and (
                len(scored) == 1 or scored[0][0] - scored[1][0] >= 5
            ):
                choice = scored[0][1]
        if choice is None:
            if _WH.search(low) and len(low.split()) > 3:
                state.pending = None  # a new question: let it through
            return None
        state.pending = None
        q = pending.question
        if pending.mention and re.search(re.escape(pending.mention), q, re.I):
            q = re.sub(re.escape(pending.mention), choice, q, count=1, flags=re.I)
        else:
            q = f"{q.rstrip('?. ')} ({choice})?"
        return q, ["clarification:picked"], None

    # ----------------------------------------------------------------------- small public helpers

    @property
    def matcher(self) -> NameMatcher | None:
        return self._matcher

    def focus_person_id(self, session_id: str) -> str | None:
        """canonical id of the person last discussed in this session, or None."""
        focus = self.session(session_id).focus.get("person")
        return focus.canonical_id if focus else None

    def pending_clarification(self, session_id: str) -> bool:
        return self.session(session_id).pending is not None

    def origin_of(self, session_id: str) -> tuple[str, str] | None:
        """(map place id, spoken name) the visitor said they are starting from, or None (the kiosk)."""
        origin = self.session(session_id).origin
        return (origin, self._origins.name_of(origin)) if origin else None

    def remember_person(self, session_id: str, canonical_id: str) -> None:
        """Make this person the one "she"/"he"/"her office" means next (after a message or meeting request)."""
        state = self.session(session_id)
        with self._lock:
            state.turn += 1
            state.updated = self._clock()
            state.focus["person"] = Focus(
                kind="person", canonical_id=canonical_id, name=self._kb.name_of(canonical_id),
                said_as=self._kb.name_of(canonical_id), turn=state.turn, gender=self._kb.gender_of(canonical_id),
            )
            if canonical_id not in state.recent_ids:
                state.recent_ids.append(canonical_id)

    # ----------------------------------------------------------------------- observe

    def observe(self, session_id: str, rewrite: Rewrite, result: Any) -> None:
        """Update memory from rag-core's RAGResult (a pydantic model or its dict) for this turn."""
        r = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        state = self.session(session_id)
        with self._lock:
            state.turn += 1
            state.updated = self._clock()
            state.last_question = rewrite.question
            if day := _DAY.search(rewrite.question):
                state.last_day = day.group(1)
            answer = r.get("answer", "")
            clarifying = bool(_CLARIFY.search(answer)) and not r.get("escalate")
            for ent in r.get("entities", []):
                cid, kind = ent.get("canonical_id"), _focus_kind(ent.get("type", ""))
                if not cid or kind is None:
                    continue
                state.focus[kind] = Focus(
                    kind=kind, canonical_id=cid, name=self._kb.name_of(cid), said_as=ent.get("text") or "",
                    turn=state.turn, gender=self._kb.gender_of(cid) if kind == "person" else None,
                )
                if cid not in state.recent_ids:
                    state.recent_ids.append(cid)
            if clarifying:
                options = [p.name for p in self._kb.people_in(answer)]
                ambiguous = next(
                    (e.get("text") for e in r.get("entities", []) if e.get("type") == "faculty" and not e.get("canonical_id")),
                    "",
                )
                state.pending = Clarification(rewrite.question, ambiguous or "", options) if options else None
            else:
                state.pending = None


def _focus_kind(entity_type: str) -> FocusKind | None:
    return {
        "faculty": "person", "room": "place", "facility": "place", "event": "event",
        "course": "subject", "programme": "subject", "topic": "subject",
    }.get(entity_type)


def _mentions_name(q: str, focus: Focus) -> bool:
    return focus.name.casefold() in q.casefold()


def _with_on(day: str) -> str:
    d = day.strip()
    return d if re.match(r"(on\s|today|tomorrow|now|this\s)", d, re.I) else f"on {d}"
