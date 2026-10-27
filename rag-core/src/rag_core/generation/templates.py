"""Answers that don't need the LLM.

1. **Canned answers** for greeting, smalltalk/thanks, escalate_to_human and out_of_scope: fixed
   text, no retrieval, no LLM, so they cost no latency.
2. **The fact composer**: a deterministic answer built straight from the structured facts, used
   when the LLM is down, times out or is switched off (``generation.mode: template``; tests and the
   offline eval use it). It answers only from facts about the entities the question links to, and
   returns None when it has no template for the question, so the pipeline escalates instead of
   guessing.

Fact texts come from ``kb/facts.py``, so the small parsers here follow its sentence shapes; a test
builds every fact and checks they still parse. Values are copied verbatim from the facts (names,
rooms, emails, codes); only times are reformatted for speech ("13:45" -> "1:45 pm").
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from rag_core.config import GenerationSettings
from rag_core.contracts import EntityType, Intent, RetrievedFact
from rag_core.generation.prompts import AnswerDraft
from rag_core.kb.parsing import slugify
from rag_core.nlu.entities import EntityMention

# --------------------------------------------------------------------------- canned answers

GREETING = (
    "Hello, and welcome to the Computer Science department on the 8th floor of Central Block. "
    "How can I help you today?"
)
THANKS = "You're welcome. Have a good day!"
ABOUT_ME = (
    "I'm the virtual receptionist for this floor. I can help you find faculty members, rooms, "
    "timetables and consultation hours."
)
HUMAN = "Of course. Please speak to the receptionist at the desk, who will be happy to help you."
OUT_OF_SCOPE = (
    "I'm sorry, I can only help with faculty, rooms, timetables and consultation hours on this floor. "
    "For anything else, please ask the receptionist at the desk."
)
NO_DATA = {
    Intent.EVENT_INFO: "I'm sorry, I don't have verified information about events. Please check with the receptionist at the desk.",
    Intent.FACILITY_INFO: "I'm sorry, I don't have verified information about facilities on this floor. Please check with the receptionist at the desk.",
}
CANNED_INTENTS = frozenset({Intent.GREETING, Intent.SMALLTALK_OR_THANKS, Intent.ESCALATE_TO_HUMAN, Intent.OUT_OF_SCOPE})
_THANKS = re.compile(r"\b(thanks?|thank\s+you|thx|bye|goodbye|see\s+you|cheers|great|ok(ay)?|cool)\b", re.IGNORECASE)


def canned_answer(intent: Intent, question: str) -> str | None:
    """Fixed text for intents that never need facts; None for every other intent."""
    if intent == Intent.GREETING:
        return GREETING
    if intent == Intent.SMALLTALK_OR_THANKS:
        return THANKS if _THANKS.search(question) else ABOUT_ME
    if intent == Intent.ESCALATE_TO_HUMAN:
        return HUMAN
    if intent == Intent.OUT_OF_SCOPE:
        return OUT_OF_SCOPE
    return None


# --------------------------------------------------------------------------- speech helpers


def spoken_time(hhmm: str) -> str:
    """'13:45' -> '1:45 pm', '09:00' -> '9 am', '12:00' -> '12 pm'."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    suffix = "am" if hour < 12 else "pm"
    hour = hour % 12 or 12
    return f"{hour}{f':{minute:02d}' if minute else ''} {suffix}"


def spoken_range(start: str, end: str) -> str:
    return f"{spoken_time(start)} to {spoken_time(end)}"


def and_join(items: Sequence[str]) -> str:
    items = list(items)
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _or_join(items: Sequence[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def name_list(names: Sequence[str], limit: int) -> str:
    names = list(dict.fromkeys(names))
    if len(names) <= limit:
        return and_join(names)
    rest = len(names) - limit
    return ", ".join(names[:limit]) + f" and {rest} other{'s' if rest > 1 else ''}"


_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# --------------------------------------------------------------------------- fact parsers

_CAUTION = re.compile(r"\s*Caution: the source records contain a conflicting note(?: in the record of [^:]+)?: (.*?)(?=\s*Caution:|$)")
_SLOT = re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) (\d{1,2}:\d{2})-(\d{1,2}:\d{2})")
_ENTRY = re.compile(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2}) (.+)")
_EMAIL = re.compile(r"Email: (\S+@\S+?|not listed)\.(?:\s|$)")
_RESEARCH = re.compile(r"Research areas: (.*?)\.(?: Programmes taught| Currently| Email|$)")
_TAUGHT_BY = re.compile(r"Taught by: (.*?)\.")
_TAUGHT_TO = re.compile(r"Taught to: (.*?)\. Taught by")
_PROG_FACULTY = re.compile(r"Faculty with \S+ classes in the timetable: (.*?)\.")
_PROG_SUBJECTS = re.compile(r"Subject codes in the \S+ timetable: (.*?)\.")
_RAW_HOURS = re.compile(r'consultation hours, as listed: "(.*?)"\.')


def cautions(text: str) -> list[str]:
    return [m.group(1).strip().rstrip(".") for m in _CAUTION.finditer(text)]


def strip_cautions(text: str) -> str:
    return _CAUTION.sub("", text).strip()


def consultation_slots(text: str) -> list[tuple[str, str, str]]:
    """(day, start, end) from the '24-hour time' part of a consultation fact."""
    part = text.split("24-hour time:", 1)
    if len(part) < 2:
        return []
    return _SLOT.findall(part[1].split(". Unclear part", 1)[0])


def schedule_entries(text: str) -> list[tuple[str, str, str]]:
    """(start, end, class code) from 'On Monday, Dr X has: 7:30-9:00 2 MSAIM ML; ...'."""
    if " has: " not in text:
        return []
    body = text.split(" has: ", 1)[1].rstrip(".")
    return [m.groups() for e in body.split("; ") if (m := _ENTRY.fullmatch(e.strip()))]


def _person_of(text: str) -> str:
    m = re.match(r"(Dr [^']+?)(?:'s|\s+is\s|\s+teaches\s|\s+\(short name)", text)
    return m.group(1) if m else ""


def _schedule_person(text: str) -> str:
    """'On Monday, Dr X has: ...' / 'On Monday, no classes are listed for Dr X in the timetable.' -> 'Dr X'."""
    m = re.match(r"On \w+, (?:no classes are listed for )?(Dr .+?)(?: has:| in the timetable)", text)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- composer

_WHERE = re.compile(r"\b(where|cabin|office|room|sit|sits|sitting|seated|find|located|location|way|reach|get to)\b", re.IGNORECASE)
_PHONE = re.compile(r"\b(phone|mobile|cell|telephone|contact number|number|call|whatsapp|extension|landline)\b", re.IGNORECASE)
_WHO_TEACHES = re.compile(r"\b(who|faculty|teacher|teachers|professors?|staff)\b", re.IGNORECASE)
_CONTACT_CUE = re.compile(
    r"\b(e-?mail|mail|contact|phone|mobile|cell|number|call|reach|extension|landline|whatsapp|id)\b", re.IGNORECASE)
# A yes/no question about whether someone is around: answered with availability, not a profile.
_PRESENCE = re.compile(
    r"^\s*(is|are|has|have|did|will)\b.*\b(in|inside|around|here|there|left|gone|come|came|arrived|back|"
    r"available|free|busy|present|absent)\b", re.IGNORECASE)
_YES_NO = re.compile(r"^\s*(does|do|did|is|are|has|have|can|will)\b", re.IGNORECASE)
# "anyone working on IoT" asks about research areas, "who teaches IoT" about the course
_RESEARCH_CUE = re.compile(
    r"\b(research\w*|work(?:s|ing)?\s+on|expert\w*|speciali[sz]\w*|areas?|interests?|stud(?:y|ies))\b", re.IGNORECASE)
_WHEN = re.compile(r"\b(when|which days?|what days?|what time|timings?|schedule|days)\b", re.IGNORECASE)
# Words that make a question about availability rather than location ("office hours", "is she in her cabin").
_AVAIL_CUE = re.compile(
    r"\b(free|available|availability|meet|meeting|consult\w*|hours?|timings?|time|when|now|today|tomorrow|busy|"
    r"appointment)\b|^\s*(is|are|will|can|could|may|does|do|would|has|have)\b", re.IGNORECASE)
_PERSON_INTENTS = frozenset({Intent.FIND_PERSON, Intent.CONTACT_INFO, Intent.PERSON_AVAILABILITY, Intent.SCHEDULE_QUERY})
_PERSONAL = re.compile(
    r"\b(home|house|residence|residential|lives?|living|salary|salaries|earns?|age|how old|married|husband|wife|"
    r"children|kids|family|religion|caste|personal)\b", re.IGNORECASE)
_WHERE_CLASS = re.compile(r"\b(where|which room|located|location|held|classrooms?|venue)\b", re.IGNORECASE)
# Topics the KB has no data for (CLAUDE.md: fees, admissions, hostel, placements, transport, results -> out of scope).
_NOT_IN_KB_TOPIC = re.compile(
    r"\b(fees?|cost|costs|price|pay|admissions?|apply|eligibility|cut-?off|hostel|placements?|salary|scholarships?|"
    r"transport|bus|results?|duration|seats|intake)\b|\bhow (many (semesters?|years?)|long)\b", re.IGNORECASE)


# kb/campus.py sentence shapes (a test builds sample campus facts and checks they parse).
_CAMPUS_CATEGORIES = frozenset({"facility", "event"})
_CAMPUS_INTENTS = frozenset({Intent.FIND_ROOM, Intent.FACILITY_INFO, Intent.EVENT_INFO})
_PLACE = re.compile(r"(?P<name>.+?) \([a-z ]+\) is in (?P<where>.+?) \((?P<here>on this floor|not on this floor)\)\.")
_DIRECTIONS = re.compile(r" Directions: (.*?)\.(?= Open hours| Opening hours)")
_OPEN_HOURS = re.compile(r" Open hours, 24-hour time: (.*?)\.(?: |$)")
_RANGE24 = re.compile(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})")
_NOTE = re.compile(r" Note: (.*?)\.$")
_OPEN_CUE = re.compile(r"\b(open|opens|close|closes|closed|hours?|timings?|time|when|until|till)\b", re.IGNORECASE)
_EVENT = re.compile(
    r"Event: (?P<title>.+?) \((?P<kind>[^()]+)\) on (?P<when>\w+, \d{1,2} \w+ \d{4})"
    r"(?:, (?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2}) \(24-hour time\)|, starting (?P<start2>\d{1,2}:\d{2}) \(24-hour time\))?"
    r"(?:, at (?P<venue>.+?)\.(?: |$)|\. The venue is not listed\.)"
)
_ORGANISER = re.compile(r" Organised by: (.*?)\.(?: |$)")
_AUDIENCE = re.compile(r" For: (.*?)\.(?: |$)")


class FactComposer:
    """Deterministic spoken answers from structured facts. ``compose`` returns None when unsure."""

    def __init__(self, cfg: GenerationSettings):
        self._cfg = cfg

    # -- entry point

    def compose(
        self,
        question: str,
        intent: Intent,
        mentions: Sequence[EntityMention],
        facts: Sequence[RetrievedFact],
        now: datetime,
    ) -> AnswerDraft | None:
        campus = [f for f in facts if f.via == "structured" and f.metadata.get("category") in _CAMPUS_CATEGORIES]
        if intent in NO_DATA and not campus:
            return self._unanswerable(NO_DATA[intent])
        by_id = {f.fact_id: f for f in facts}
        linked: dict[EntityType, list[str]] = defaultdict(list)
        for m in mentions:
            if m.canonical_id:
                linked[m.type].append(m.canonical_id)

        ambiguous = [m for m in mentions if m.ambiguous and m.type == EntityType.FACULTY]
        if ambiguous:
            return self._clarify(ambiguous[0])
        unknown = [m for m in mentions if m.type == EntityType.FACULTY and not m.candidates]
        other_kb = any(linked.get(t) for t in (EntityType.ROOM, EntityType.COURSE, EntityType.PROGRAMME, EntityType.TOPIC))
        unknown_note = ""
        if unknown and not linked.get(EntityType.FACULTY) and other_kb:
            # "Who teaches ML? I think it's Dr Sharma.": answer the part the KB covers. When the name partly
            # matched someone, say plainly that it isn't in the list (without repeating it, so it can't
            # sound like a confirmation).
            if unknown[0].near_misses:
                unknown_note = " The other name you mentioned isn't in the faculty list."
            unknown = []
        if unknown and not linked.get(EntityType.FACULTY):
            u = unknown[0]
            near = [c.name for c in u.near_misses][: self._cfg.max_clarification_options]
            if u.needs_confirmation and near:
                # No case signal ("dr sridevi krishnan"): ask, never answer for the near miss.
                return AnswerDraft(
                    answer=f"Just to make sure I have the right person: do you mean {_or_join(near)}?",
                    answerable=True, cited_fact_ids=[], needs_clarification=True, clarification_options=near,
                )
            did_you_mean = f" Did you mean {_or_join(near)}?" if near else ""
            return self._unanswerable(
                f"I'm sorry, I can't find anyone called {u.text} in the faculty list.{did_you_mean} "
                "Please check with the receptionist at the desk."
            )
        unknown_rooms = [m for m in mentions if m.type == EntityType.ROOM and not m.candidates]
        if (unknown_rooms and not linked.get(EntityType.ROOM) and not linked.get(EntityType.FACULTY)
                and intent in (Intent.FIND_ROOM, Intent.FIND_PERSON)):
            return self._unanswerable(
                f"I'm sorry, I can't find {unknown_rooms[0].text} in my records. Please check with the receptionist at the desk."
            )

        if intent in _PERSON_INTENTS and _PERSONAL.search(question):
            # home address, salary, age, family...: not in the KB and not ours to share
            return self._unanswerable(
                "I'm sorry, I can't share personal details about staff. Please ask the receptionist at the desk.")

        if campus and intent in _CAMPUS_INTENTS and not linked.get(EntityType.FACULTY):
            office_rooms = [f for f in facts if f.via == "structured" and f.metadata.get("category") == "room"]
            if not office_rooms:
                return self._campus(question, intent, mentions, campus, now)

        if unknown_rooms and linked.get(EntityType.FACULTY) and intent in (Intent.FIND_ROOM, Intent.FIND_PERSON):
            # "I heard Dr X moved to room 900": give the recorded room, never confirm the visitor's.
            draft = self._find_person(question, linked, by_id, mentions, now)
            if draft is not None and draft.answerable:
                draft.answer = (
                    f"According to my records, {draft.answer} I can't confirm any other room for them, "
                    "so please check with the receptionist at the desk if it has changed."
                )
            return draft

        handler = {
            Intent.FIND_PERSON: self._find_person,
            Intent.FIND_ROOM: self._find_room,
            Intent.CONTACT_INFO: self._contact,
            Intent.PERSON_AVAILABILITY: self._availability,
            Intent.FIND_BY_SUBJECT: self._by_subject,
            Intent.SCHEDULE_QUERY: self._schedule,
            Intent.PROGRAMME_INFO: self._programme,
        }.get(intent)
        if handler is None:
            return None
        draft = handler(question, linked, by_id, mentions, now)
        if draft is not None and unknown_note and draft.answerable:
            draft.answer += unknown_note
        return draft

    # -- campus places and events (kb/campus.py sentence shapes)

    def _campus(
        self, question: str, intent: Intent, mentions: Sequence[EntityMention], facts: Sequence[RetrievedFact], now: datetime,
    ) -> AnswerDraft | None:
        events = [f for f in facts if f.metadata.get("category") == "event"]
        places = [f for f in facts if f.metadata.get("category") == "facility"]
        if intent == Intent.EVENT_INFO and events:
            return self._events(question, mentions, events, now)
        if places:
            return self._places(question, places)
        return self._events(question, mentions, events, now) if events else None

    def _places(self, question: str, places: Sequence[RetrievedFact]) -> AnswerDraft:
        here = [f for f in places if "(on this floor)." in f.text]
        ordered = [*here, *(f for f in places if f not in here)]
        limit = self._cfg.max_names_spoken
        shown = ordered[:limit]
        wants_hours = bool(_OPEN_CUE.search(question))
        sentences = []
        for n, fact in enumerate(shown):
            m = _PLACE.match(fact.text)
            if not m:
                continue
            where = m["where"] + (", here on this floor" if m["here"] == "on this floor" else ", which is not on this floor")
            sentences.append(f"{m['name']} is in {where}.")
            if len(shown) == 1 and (d := _DIRECTIONS.search(fact.text)):
                sentences.append(f"{d[1]}.")
            if wants_hours:
                if h := _OPEN_HOURS.search(fact.text):
                    ranges = [spoken_range(*r) for r in _RANGE24.findall(h[1])]
                    sentences.append(f"It is open {and_join(ranges)}.")
                else:
                    sentences.append(f"Opening hours for {m['name']} are not listed.")
            if n == 0 and len(shown) == 1 and (note := _NOTE.search(fact.text)):
                sentences.append(f"{note[1]}.")
        if not sentences:
            return self._unanswerable(self._cfg.safe_message)
        if len(ordered) > limit:
            rest = len(ordered) - limit
            sentences.append(f"There {'is' if rest == 1 else 'are'} {rest} more listed; please ask at the desk for the others.")
        return self._draft(" ".join(sentences), [f.fact_id for f in shown])

    def _events(
        self, question: str, mentions: Sequence[EntityMention], events: Sequence[RetrievedFact], now: datetime,
    ) -> AnswerDraft:
        from datetime import date, timedelta

        def when_of(fact: RetrievedFact) -> date | None:
            part = fact.fact_id.split(":")
            try:
                return date.fromisoformat(part[1]) if len(part) == 3 else None
            except ValueError:
                return None

        today = now.date()
        target, label = None, None
        if re.search(r"\btomorrow\b", question, re.IGNORECASE):
            target, label = today + timedelta(days=1), "tomorrow"
        elif re.search(r"\b(today|tonight|now)\b", question, re.IGNORECASE):
            target, label = today, "today"
        elif day := self._day(mentions):
            offset = (_WEEK.index(day) - today.weekday()) % 7
            target, label = today + timedelta(days=offset), f"on {day}"
        dated = sorted(((when_of(f), f) for f in events), key=lambda p: (p[0] or date.max, p[1].fact_id))
        if target is not None:
            chosen = [f for d, f in dated if d == target]
        else:
            chosen = [f for d, f in dated if d is not None and d >= today]
        if not chosen:
            when = label or "coming up"
            return self._unanswerable(
                f"I'm sorry, I don't have any events listed {when}. Please check with the receptionist at the desk.")
        limit = self._cfg.max_names_spoken
        shown = chosen[:limit]
        spoken = [self._event_sentence(f, detail=len(shown) == 1) for f in shown]
        answer = " ".join(s for s in spoken if s)
        if len(chosen) > limit:
            answer += f" There are {len(chosen) - limit} more events listed; please ask at the desk for the others."
        return self._draft(answer, [f.fact_id for f in shown])

    @staticmethod
    def _event_sentence(fact: RetrievedFact, detail: bool) -> str:
        m = _EVENT.match(fact.text)
        if not m:
            return ""
        when = re.sub(r" \d{4}$", "", m["when"])  # "Friday, 6 November 2026" -> "Friday, 6 November"
        out = f"{m['title']} ({m['kind']}) is on {when}"
        if m["start"] and m["end"]:
            out += f", from {spoken_range(m['start'], m['end'])}"
        elif m["start2"]:
            out += f", starting at {spoken_time(m['start2'])}"
        out += f", at {m['venue']}." if m["venue"] else ". The venue is not listed."
        if detail:
            for label, cue in (("Organised by", _ORGANISER), ("For", _AUDIENCE)):
                if found := cue.search(fact.text):
                    out += f" {label} {found[1]}."
        return out

    # -- helpers

    def _draft(self, answer: str, cited: Sequence[str], answerable: bool = True) -> AnswerDraft:
        return AnswerDraft(
            answer=answer, answerable=answerable, cited_fact_ids=list(dict.fromkeys(cited)),
            needs_clarification=False, clarification_options=[],
        )

    def _unanswerable(self, answer: str, cited: Sequence[str] = ()) -> AnswerDraft:
        return self._draft(answer, cited, answerable=False)

    def _clarify(self, mention: EntityMention) -> AnswerDraft:
        names = [c.name for c in mention.candidates][: self._cfg.max_clarification_options]
        return AnswerDraft(
            answer=f"I found more than one person who matches {mention.text}: {_or_join(names)}. Which one do you mean?",
            answerable=True, cited_fact_ids=[], needs_clarification=True, clarification_options=names,
        )

    @staticmethod
    def _faculty(linked) -> list[str]:
        return [cid.split(":", 1)[1] for cid in linked.get(EntityType.FACULTY, [])]

    @staticmethod
    def _day(mentions: Sequence[EntityMention]) -> str | None:
        for m in mentions:
            if m.type == EntityType.DAY and m.canonical_id:
                return m.canonical_id.split(":", 1)[1].capitalize()
            if m.type == EntityType.TIME and m.time and m.time.day:
                return m.time.day
        return None

    @staticmethod
    def _time_point(mentions: Sequence[EntityMention]) -> str | None:
        for m in mentions:
            if m.type == EntityType.TIME and m.time and m.time.start and m.time.start == m.time.end:
                return m.time.start
        return None

    def _location_sentence(self, fact: RetrievedFact) -> tuple[str, bool]:
        """Speakable location; False when the location isn't listed."""
        text = strip_cautions(fact.text)
        if "office location is not listed" in text:
            return text, False
        sentences = [s.strip() for s in re.split(r"(?<=\.)\s+", text) if s.strip()]
        first = sentences[0].replace(" (on this floor).", ", here on this floor.")
        m = re.match(r"(Dr .+?) is not on this floor \([^)]*\); office is in (.+)\.$", first)
        if m:
            first = f"{m.group(1)} is not on this floor. Their office is in {m.group(2)}."
        out = [first, *sentences[1:2]]
        for note in cautions(fact.text):
            out.append(f"However, the records disagree: {note.replace('(', '').replace(')', '')}.")
        return " ".join(out), True

    # -- intents

    def _location_brief(self, fact: RetrievedFact) -> str:
        """One sentence per person, for answers about several people."""
        text = strip_cautions(fact.text)
        name = _person_of(text)
        if "office location is not listed" in text:
            return f"{name}'s office location isn't listed."
        first = re.split(r"(?<=\.)\s+", text)[0]
        first = first.replace(" (on this floor).", ", here on this floor.")
        m = re.match(r"(Dr .+?) is not on this floor \([^)]*\); office is in (.+)\.$", first)
        if m:
            first = f"{m.group(1)} is not on this floor; their office is in {m.group(2)}."
        notes = cautions(fact.text)
        if notes:
            first = first[:-1] + f" (the records also say: {notes[0].replace('(', '').replace(')', '')})."
        return first

    def _find_person(self, question, linked, by_id, mentions, now):
        faculty = self._faculty(linked)
        if len(faculty) > 1 and _WHERE.search(question):
            # "Where are Dr A and Dr B?": answer each person the facts cover
            locs = [by_id[f"faculty:{fid}:location"] for fid in faculty[: self._cfg.max_names_spoken]
                    if f"faculty:{fid}:location" in by_id]
            if len(locs) > 1:
                return self._draft(" ".join(self._location_brief(f) for f in locs), [f.fact_id for f in locs])
        if faculty:
            fid = faculty[0]
            if (_PRESENCE.search(question) and not linked.get(EntityType.ROOM)
                    and f"faculty:{fid}:consultation" in by_id):
                # "Is Dr X inside?" / "Has Dr X left?": whether they can be met, not who they are
                return self._availability(question, linked, by_id, mentions, now)
            loc = by_id.get(f"faculty:{fid}:location")
            prof = by_id.get(f"faculty:{fid}:profile")
            if loc is None and prof is None:
                return None
            if loc is not None and (_WHERE.search(question) or prof is None):
                sentence, listed = self._location_sentence(loc)
                if not listed:
                    return self._unanswerable(
                        f"I'm sorry, {_person_of(loc.text)}'s office location isn't listed in my records. "
                        "Please check with the receptionist at the desk.", [loc.fact_id])
                return self._draft(sentence, [loc.fact_id])
            name = _person_of(prof.text)
            research = _RESEARCH.search(prof.text)
            areas = f", with research areas in {research.group(1)}" if research and research.group(1) != "not listed" else ""
            parts = [f"{name} is a faculty member in the Computer Science department{areas}."]
            cited = [prof.fact_id]
            if loc is not None:
                sentence, listed = self._location_sentence(loc)
                if listed:
                    # the whole location statement (an off-floor one is two sentences), without sharers or notes
                    parts.append(sentence.split(" They share")[0].split(" However,")[0])
                    cited.append(loc.fact_id)
            return self._draft(" ".join(parts), cited)
        rooms = linked.get(EntityType.ROOM, [])
        if rooms and rooms[0] in by_id:
            return self._room_answer(by_id[rooms[0]])
        if linked.get(EntityType.COURSE) or linked.get(EntityType.TOPIC):
            return self._by_subject(question, linked, by_id, mentions, now)
        return None

    def _room_answer(self, fact: RetrievedFact) -> AnswerDraft:
        text = strip_cautions(fact.text)
        m = re.match(r"Room (\S+) is in (.+?) \((on this floor|not on this floor)\)\. It is the office of (.+)\.$", text)
        if not m:
            return self._draft(text, [fact.fact_id])
        room, where, here, people = m.groups()
        lead = f"Room {room} is here on this floor, in {where}." if here == "on this floor" else f"Room {room} is not on this floor. It is in {where}."
        answer = f"{lead} It is the office of {people}."
        for note in cautions(fact.text):
            answer += f" However, the records disagree: {note.replace('(', '').replace(')', '')}."
        return self._draft(answer, [fact.fact_id])

    def _gap_for(self, linked, by_id, kind: str) -> AnswerDraft | None:
        """Programme/course facts say classroom locations are not listed."""
        for type_ in (EntityType.PROGRAMME, EntityType.COURSE):
            for cid in linked.get(type_, []):
                if cid in by_id and "Classroom locations" in by_id[cid].text:
                    code = by_id[cid].metadata.get("programme" if type_ == EntityType.PROGRAMME else "subject_code") or cid.split(":")[1]
                    return self._unanswerable(
                        f"I'm sorry, {kind} locations for {code} aren't listed in my records. "
                        "Please check with the receptionist at the desk.", [cid])
        return None

    def _find_room(self, question, linked, by_id, mentions, now):
        rooms = linked.get(EntityType.ROOM, [])
        if rooms and rooms[0] in by_id:
            return self._room_answer(by_id[rooms[0]])
        if self._faculty(linked):
            return self._find_person(question, linked, by_id, mentions, now)
        return self._gap_for(linked, by_id, "classroom")

    def _contact(self, question, linked, by_id, mentions, now):
        faculty = self._faculty(linked)
        if not faculty or f"faculty:{faculty[0]}:profile" not in by_id:
            return None
        if _WHERE.search(question) and not _CONTACT_CUE.search(question):
            # "sudhakar sir office please" read as contact_info: it asks where, so answer where
            return self._find_person(question, linked, by_id, mentions, now)
        prof = by_id[f"faculty:{faculty[0]}:profile"]
        name = _person_of(prof.text)
        email = _EMAIL.search(prof.text)
        listed = email is not None and email.group(1) != "not listed"
        if _PHONE.search(question) and not re.search(r"\b(e-?mail|mail id)\b", question, re.IGNORECASE):
            extra = f" Their listed email is {email.group(1)}." if listed else ""
            return self._unanswerable(
                f"I'm sorry, I can't verify a phone number for {name}.{extra} "
                "Please check with the receptionist at the desk.", [prof.fact_id])
        if not listed:
            return self._unanswerable(
                f"I'm sorry, {name}'s email isn't listed in my records. Please check with the receptionist at the desk.",
                [prof.fact_id])
        return self._draft(f"{name}'s email is {email.group(1)}.", [prof.fact_id])

    def _grouped_slots(self, slots) -> str:
        by_time: dict[tuple[str, str], list[str]] = defaultdict(list)
        for day, start, end in slots:
            by_time[(start, end)].append(day)
        return "; ".join(f"{and_join(days)}, {spoken_range(s, e)}" for (s, e), days in by_time.items())

    def _availability(self, question, linked, by_id, mentions, now):
        faculty = self._faculty(linked)
        if not faculty:
            return None
        if _WHERE.search(question) and not _AVAIL_CUE.search(question):
            # "dr X office please" read as availability: it asks where, so answer where
            return self._find_person(question, linked, by_id, mentions, now)
        fid = faculty[0]
        cons = by_id.get(f"faculty:{fid}:consultation")
        if cons is None:
            return None
        name = _person_of(cons.text) or re.sub(r"^Consultation hours for (.+) are not listed\.$", r"\1", cons.text)
        if "are not listed" in cons.text:
            return self._unanswerable(
                f"I'm sorry, consultation hours for {name} aren't listed in my records. "
                "Please check with the receptionist at the desk.", [cons.fact_id])
        slots = consultation_slots(cons.text)
        raw = _RAW_HOURS.search(cons.text)
        if not slots:
            quoted = raw.group(1) if raw else ""
            if self._day(mentions) or self._time_point(mentions):
                # "is X free now?" with hours we can't read: unknown, so escalate (CLAUDE.md)
                return self._unanswerable(
                    f"{name}'s consultation hours are listed as \"{quoted}\", so I can't tell whether they're free then. "
                    "Please check with the receptionist at the desk.", [cons.fact_id])
            return self._draft(
                f"{name}'s consultation hours are listed as \"{quoted}\", but I can't confirm the days and times. "
                "Please check with the receptionist at the desk.", [cons.fact_id])
        unclear = " Part of the entry is unclear, so please confirm with the receptionist." if "Unclear part" in cons.text else ""
        day = self._day(mentions)
        if day is None:
            return self._draft(f"{name}'s consultation hours are {self._grouped_slots(slots)}.{unclear}", [cons.fact_id])

        cited = [cons.fact_id]
        sched = by_id.get(f"faculty:{fid}:schedule:{day.lower()}")
        classes = schedule_entries(sched.text) if sched else []
        point = self._time_point(mentions)
        on_day = [(s, e) for d, s, e in slots if d == day]
        if point is not None:
            in_cons = [(s, e) for s, e in on_day if _minutes(s) <= _minutes(point) < _minutes(e)]
            in_class = [(s, e, c) for s, e, c in classes if _minutes(s) <= _minutes(point) < _minutes(e)]
            if sched is not None:
                cited.append(sched.fact_id)
            if in_class:
                s, e, c = in_class[0]
                answer = f"According to the timetable, {name} has a class right now, {c}, from {spoken_range(s, e)}."
                overlap = in_cons or [(cs, ce) for cs, ce in on_day if _minutes(cs) > _minutes(point)]
                if overlap:  # the records list consultation hours too: say both, don't pick one
                    answer += f" Their consultation hours today are listed as {and_join([spoken_range(a, b) for a, b in overlap])}."
                return self._draft(answer + unclear, cited)
            if in_cons:
                s, e = in_cons[0]
                return self._draft(f"Yes, {name}'s consultation hours on {day} are {spoken_range(s, e)}, which includes now.{unclear}", cited)
            later = [f"{spoken_range(s, e)}" for s, e in on_day if _minutes(s) > _minutes(point)]
            tail = f" Their consultation hours later today are {and_join(later)}." if later else f" Their consultation hours are {self._grouped_slots(slots)}."
            return self._draft(f"{name} has no class or consultation listed right now.{tail}{unclear}", cited)
        if on_day:
            answer = f"On {day}, {name}'s consultation hours are {and_join([spoken_range(s, e) for s, e in on_day])}."
        else:
            answer = f"{name} has no consultation hours listed on {day}. They are listed for {self._grouped_slots(slots)}."
        if sched is not None:
            cited.append(sched.fact_id)
            if classes:
                answer += f" They have classes that day starting at {and_join([spoken_time(s) for s, _, _ in classes])}."
            else:
                answer += " The timetable lists no classes for them that day."
        return self._draft(answer + unclear, cited)

    def _subject_label(self, fact: RetrievedFact) -> str:
        code = str(fact.metadata.get("subject_code", ""))
        m = re.match(r"Course: (.*?)\. Taught to", fact.text)
        desc = m.group(1) if m else ""
        if desc and "not verified" not in desc:
            return desc  # verified glossary name, e.g. "Machine Learning (ML)"
        return code

    def _by_subject(self, question, linked, by_id, mentions, now):
        limit = self._cfg.max_names_spoken
        faculty = self._faculty(linked)
        if faculty and not any(linked.get(t) for t in (EntityType.COURSE, EntityType.TOPIC, EntityType.PROGRAMME)):
            # no subject in the question ("who is Dr X" read as find_by_subject): it is about the person
            return self._find_person(question, linked, by_id, mentions, now)
        if not faculty and _WHERE_CLASS.search(question):
            # "where is the dbms lab" read as find_by_subject: it asks where, and classrooms aren't listed
            gap = self._gap_for(linked, by_id, "classroom")
            if gap is not None:
                return gap
        topic_first = bool(linked.get(EntityType.TOPIC)) and bool(_RESEARCH_CUE.search(question))
        for cid in ([] if topic_first else linked.get(EntityType.COURSE, [])):
            code = cid.split(":", 1)[1]
            if faculty:
                teach = by_id.get(f"faculty:{faculty[0]}:teaching:{code}")
                if teach is not None:
                    groups = re.search(r"teaches .*?: (.*?)\.(?: Programmes| Co-taught|$)", teach.text)
                    label = str(teach.metadata.get("subject_code", code.upper()))
                    to = f" to {groups.group(1).split('; ')[0]}" if groups else ""
                    name = _person_of(teach.text)
                    days = sorted(
                        (f for f in by_id.values() if f.metadata.get("category") == "schedule"
                         and f.metadata.get("faculty_id") == faculty[0]
                         and code in {slugify(c) for c in str(f.metadata.get("subject_code", "")).split(",") if c}),
                        key=lambda f: _WEEK.index(str(f.metadata["day"])),
                    )
                    on = and_join([str(f.metadata["day"]) for f in days])
                    cited = [teach.fact_id, *(f.fact_id for f in days)]
                    if days and _WHEN.search(question):
                        return self._draft(f"{name} teaches {label} on {on},{to}.", cited)
                    return self._draft(f"Yes, {name} teaches {label}{to}" + (f", on {on}." if days else "."), cited)
            course = by_id.get(cid)
            if course is not None:
                teachers = _TAUGHT_BY.search(course.text)
                if teachers:
                    names = teachers.group(1).split(", ")
                    return self._draft(f"{self._subject_label(course)} is taught by {name_list(names, limit)}.", [cid])
        topics = linked.get(EntityType.TOPIC, [])
        if topics:
            profiles = [f for f in by_id.values() if f.via == "structured" and f.metadata.get("category") == "faculty_profile"]
            topic_text = next((m.text for m in mentions if m.type == EntityType.TOPIC), "that area")
            if profiles:
                names = [_person_of(f.text) for f in profiles]
                verb = "lists" if len(names) == 1 else "list"
                return self._draft(
                    f"{name_list(names, limit)} {verb} {topic_text} among their research areas.",
                    [f.fact_id for f in profiles[:limit]] if len(names) > limit else [f.fact_id for f in profiles])
        for cid in linked.get(EntityType.PROGRAMME, []):
            prog = by_id.get(cid)
            if prog is not None and (teachers := _PROG_FACULTY.search(prog.text)):
                code = prog.metadata.get("programme", "")
                return self._draft(f"Faculty with {code} classes in the timetable include {name_list(teachers.group(1).split(', '), limit)}.", [cid])
        return None

    def _schedule(self, question, linked, by_id, mentions, now):
        faculty = self._faculty(linked)
        day = self._day(mentions)
        courses = [cid.split(":", 1)[1] for cid in linked.get(EntityType.COURSE, [])]
        limit = self._cfg.max_names_spoken
        if faculty:
            fid = faculty[0]
            if day:
                sched = by_id.get(f"faculty:{fid}:schedule:{day.lower()}")
                if sched is None:
                    return None
                name = _schedule_person(sched.text)
                entries = schedule_entries(sched.text)
                point = self._time_point(mentions)
                if point is not None:
                    now_class = [(s, e, c) for s, e, c in entries if _minutes(s) <= _minutes(point) < _minutes(e)]
                    if now_class:
                        s, e, c = now_class[0]
                        return self._draft(f"Yes, according to the timetable {name} has {c} right now, from {spoken_range(s, e)}.", [sched.fact_id])
                    return self._draft(f"No, the timetable lists no class for {name} right now.", [sched.fact_id])
                if not entries:
                    return self._draft(f"The timetable lists no classes for {name} on {day}.", [sched.fact_id])
                spoken = [f"{c} from {spoken_range(s, e)}" for s, e, c in entries[:limit]]
                more = f", and {len(entries) - limit} more" if len(entries) > limit else ""
                return self._draft(f"On {day}, {name} has {and_join(spoken)}{more}.", [sched.fact_id])
            if courses:
                days = sorted(
                    (f for f in by_id.values() if f.via == "structured" and f.metadata.get("category") == "schedule"
                     and f.metadata.get("faculty_id") == fid),
                    key=lambda f: _WEEK.index(str(f.metadata["day"])),
                )
                if days:
                    code = str(days[0].metadata.get("subject_code", "")).split(",")
                    label = next((c for c in code if c.lower().replace(" ", "-").replace("/", "-") == courses[0]), courses[0].upper())
                    name = _schedule_person(days[0].text)
                    yes = "Yes, " if _YES_NO.search(question) else ""
                    return self._draft(f"{yes}{name} teaches {label} on {and_join([str(f.metadata['day']) for f in days])}.",
                                       [f.fact_id for f in days])
            teaching = [f for f in by_id.values() if f.metadata.get("category") == "teaching"
                        and str(f.metadata.get("faculty_id", "")).split(",")[0] == fid]
            if teaching:
                codes = [str(f.metadata["subject_code"]) for f in teaching]
                return self._draft(f"{_person_of(teaching[0].text)} teaches {and_join(codes)}.", [f.fact_id for f in teaching])
            return None
        if courses and day:
            days = [f for f in by_id.values() if f.via == "structured" and f.metadata.get("category") == "schedule"]
            if days:
                parts = []
                for f in days[:limit]:
                    who = _schedule_person(f.text)
                    slots = [f"{spoken_range(s, e)}" for s, e, c in schedule_entries(f.text)
                             if c.split()[-1].lower() == courses[0] or courses[0] in c.lower().replace(" ", "-")]
                    parts.append(f"{who}, {and_join(slots)}" if slots else who)
                return self._draft(f"On {day}, {courses[0].upper()} classes are taken by {and_join(parts)}.", [f.fact_id for f in days[:limit]])
        if courses:
            return self._by_subject(question, linked, by_id, mentions, now)
        return None

    def _programme(self, question, linked, by_id, mentions, now):
        # Programme facts describe programmes. A misrouted "where are the classes" or "what are the fees"
        # must not get a programme summary: answer the gap, or decline.
        if _WHERE_CLASS.search(question):
            return self._gap_for(linked, by_id, "classroom")
        if _NOT_IN_KB_TOPIC.search(question):
            return self._unanswerable(
                "I'm sorry, I don't have verified information about that. Please ask the receptionist at the desk.")
        limit = self._cfg.max_names_spoken
        for cid in linked.get(EntityType.PROGRAMME, []):
            prog = by_id.get(cid)
            if prog is None:
                continue
            code = prog.metadata.get("programme", "")
            teachers = _PROG_FACULTY.search(prog.text)
            subjects = _PROG_SUBJECTS.search(prog.text)
            if _WHO_TEACHES.search(question) and teachers:
                return self._draft(
                    f"Faculty with {code} classes in the timetable include {name_list(teachers.group(1).split(', '), limit)}.", [cid])
            if subjects:
                codes = subjects.group(1).split(", ")
                n_fac = len(teachers.group(1).split(", ")) if teachers else 0
                return self._draft(
                    f"{code} has {len(codes)} subject codes in this floor's timetable, including {name_list(codes, limit)}, "
                    f"taught by {n_fac} faculty members. I can't verify the programme's full name.", [cid])
            return self._unanswerable(f"I'm sorry, I don't have verified details about {code}. Please check with the receptionist at the desk.", [cid])
        for cid in linked.get(EntityType.COURSE, []):
            course = by_id.get(cid)
            if course is None:
                continue
            to = _TAUGHT_TO.search(course.text)
            by = _TAUGHT_BY.search(course.text)
            label = self._subject_label(course)
            note = "" if label != str(course.metadata.get("subject_code")) else " I can't verify its full name."
            groups = to.group(1).split("; ")[0] if to else ""
            teachers = name_list(by.group(1).split(", "), limit) if by else ""
            return self._draft(f"{label} is a subject taught to {groups} by {teachers}.{note}", [cid])
        return None
