"""Two short conversations that end in a request to a faculty member: "leave a message" and "book a meeting".

Both are slot-filling dialogues. The receptionist asks one thing at a time, in the order below, and anything the
visitor already said ("book a meeting with Dr Deepa tomorrow at 3 about my project") is taken from the first
utterance, so those questions are skipped.

    message : who -> what to tell them -> your name -> how to reach you (optional)  -> confirm
    meeting : who -> day -> time -> what about (optional) -> your name -> how to reach you (optional) -> confirm

What it does NOT do: it never says a meeting is booked. It writes a *request* (``tasks.requests``, one JSON line
each, and the receptionist webhook when ``RECEPTIONIST_WEBHOOK_URL`` is set) and tells the visitor the faculty
member will confirm. The only facts it checks are ones it can: the person is in the faculty sheet, the day is in
``tasks.meeting_days``, the time is inside ``meeting_earliest``-``meeting_latest`` and not already past.

The visitor's name and contact are asked for because a message or request is useless without them. They go into
that one request and nowhere else (not the turn log, not the dashboard); "skip" leaves the contact out.

A visitor can say "cancel" at any point, or ask for the receptionist, which leaves the dialogue and hands over.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal

import httpx
from pydantic import BaseModel, Field

from voice_memory.config import TaskSettings
from voice_memory.kb import KBView, Person
from voice_memory.names import NameMatcher

log = logging.getLogger(__name__)

SKIPPED = ""  # an optional slot the visitor chose to leave out (None = not asked yet)

_FLAGS = re.IGNORECASE
_MESSAGE = re.compile(
    r"\b(?:leave|take|give|send|pass|drop|write|record)\b[^.?!]{0,30}\b(?:message|note)\b"
    r"|\bmessage\s+(?:for|to)\s+\w"
    r"|\b(?:want|like|wish|need)\s+to\s+(?:leave|send)\s+(?:a\s+)?(?:message|note)\b",
    _FLAGS,
)
_MEETING = re.compile(
    r"\b(?:book|schedule|fix|arrange|request|set\s*up|make|get)\b[^.?!]{0,30}\b(?:meeting|appointment|slot)\b"
    r"|\b(?:want|need|like|wish)\b[^.?!]{0,20}\b(?:a|an)\s+(?:meeting|appointment)\b"
    r"|\bappointment\s+with\b",
    _FLAGS,
)
_CANCEL = re.compile(r"^\s*(?:cancel|stop|never\s*mind|forget\s+it|quit|exit|abort|leave\s+it)\b|\bcancel\s+(?:that|this|it|the)\b", _FLAGS)
_HUMAN = re.compile(r"\b(?:talk|speak)\s+to\s+(?:a\s+|the\s+)?(?:receptionist|human|person|someone)\b|\b(?:call|get)\s+(?:the\s+)?(?:receptionist|someone)\b", _FLAGS)
_YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|correct|right|send\s+it|go\s+ahead|please\s+do|haan|ha|that'?s\s+(?:right|correct)|confirm)\b", _FLAGS)
_NO = re.compile(r"^\s*(?:no|nope|nah|don'?t|do\s+not|cancel|stop|wrong|not)\b", _FLAGS)
_SKIP = re.compile(r"^\s*(?:skip|no\s+thanks|no\s+thank\s+you|none|nothing|pass|don'?t\s+have|i\s+don'?t\s+want\s+to|not\s+needed|leave\s+it\s+out)\b", _FLAGS)
_PRONOUN = re.compile(r"^\s*(?:her|him|them|she|he|they|the\s+same\s+(?:person|one))\s*[.!?]?\s*$", _FLAGS)
_ORDINALS = {"first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2,
             "last": -1, "latter": -1, "former": 0}
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_WHO = re.compile(
    r"\b(?:for|to|with)\s+(?!(?:leave|send|take|give|book|schedule|fix|arrange|request|make|get|set|a|an|the|my|me)\b)"
    r"(?P<who>(?:(?:dr|prof|professor|fr|father|doctor|mr|ms|mrs)\.?\s+)?[a-z][a-z.'-]*(?:\s+[a-z][a-z.'-]*){0,3}?)"
    r"(?=\s+(?:saying|says|that|about|regarding|to\s+(?:tell|say|discuss)|tomorrow|today|on|at|by|next|this|please|because|"
    r"if|when|and|so|in|around|after|before)\b|\s*[,:;.?!]|\s*$)",
    _FLAGS,
)
_TITLE_WORDS = r"(?:dr|prof|professor|fr|father|doctor|mr|ms|mrs)"
_MESSAGE_TEXT = re.compile(r"(?:\b(?:saying|says?|that|telling\s+(?:them|him|her)|tell\s+(?:them|him|her))\b|:)\s*(?P<text>.+)$", _FLAGS)
_PURPOSE = re.compile(r"\b(?:about|regarding|concerning|to\s+discuss|in\s+connection\s+with|on\s+the\s+topic\s+of)\s+(?P<text>.+?)(?=\s+(?:tomorrow|today|on|at)\b|[.?!]|$)", _FLAGS)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
_MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(_MONTHS)} | {"sept": 9}
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                 "ten": 10, "eleven": 11, "twelve": 12}


# ----------------------------------------------------------------------------- day and time parsing


def _hhmm(text: str) -> tuple[int, int]:
    h, m = text.split(":")
    return int(h), int(m)


def _clock_text(h: int, m: int) -> str:
    return f"{(h % 12) or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


def _day_text(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()].capitalize()} {d.day} {_MONTHS[d.month - 1].capitalize()}"


def parse_day(text: str, today: date, allowed: list[str]) -> tuple[date | None, str | None]:
    """(date, None) when the text names a day that can be requested; (None, reason) when it names one that can't;
    (None, None) when it names no day at all."""
    low = re.sub(r"\s+", " ", text.casefold())
    found: date | None = None
    if re.search(r"\bday after tomorrow\b", low):
        found = today + timedelta(days=2)
    elif re.search(r"\b(?:tomorrow|tmrw|tomorow)\b", low):
        found = today + timedelta(days=1)
    elif re.search(r"\btoday\b", low):
        found = today
    if found is None:
        m = re.search(rf"\b(?P<next>next\s+|this\s+|coming\s+|on\s+)?(?P<wd>{'|'.join(_WEEKDAYS)}|{'|'.join(sorted(_WEEKDAY_ABBR, key=len, reverse=True))})\b", low)
        if m:
            wd = _WEEKDAYS.index(m["wd"]) if m["wd"] in _WEEKDAYS else _WEEKDAY_ABBR[m["wd"]]
            ahead = (wd - today.weekday()) % 7
            if (m["next"] or "").strip() == "next" and ahead == 0:
                ahead = 7
            found = today + timedelta(days=ahead)
    if found is None:
        month_alt = "|".join(sorted([*_MONTHS, *_MONTH_ABBR], key=len, reverse=True))
        m = re.search(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mo>{month_alt})\b", low) or \
            re.search(rf"\b(?P<mo>{month_alt})\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b", low)
        if m:
            mo = _MONTHS.index(m["mo"]) + 1 if m["mo"] in _MONTHS else _MONTH_ABBR[m["mo"]]
            found = _safe_date(today.year, mo, int(m["d"]))
            if found and found < today and (today - found).days > 180:
                found = _safe_date(today.year + 1, mo, int(m["d"]))
            elif found is None:
                return None, "That date doesn't exist."
        elif m := re.search(r"\b(?P<d>\d{1,2})\s*(?:st|nd|rd|th)\b", low):
            found = _safe_date(today.year, today.month, int(m["d"]))
            if found is None or found < today:
                nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
                found = _safe_date(nxt.year, nxt.month, int(m["d"]))
        elif m := re.search(r"\b(?P<d>\d{1,2})[/-](?P<mo>\d{1,2})(?:[/-]\d{2,4})?\b", low):
            found = _safe_date(today.year, int(m["mo"]), int(m["d"]))
            if found is None:
                return None, "That date doesn't exist."
            if found < today and (today - found).days > 180:
                found = _safe_date(today.year + 1, int(m["mo"]), int(m["d"]))
    if found is None:
        return None, None
    if found < today:
        return None, "That day has already passed."
    allowed_set = {a.casefold() for a in allowed}
    if _WEEKDAYS[found.weekday()] not in allowed_set:
        days = [d.capitalize() for d in _WEEKDAYS if d in allowed_set]
        span = f"{days[0]} to {days[-1]}" if len(days) > 2 else " and ".join(days)
        return None, f"{_WEEKDAYS[found.weekday()].capitalize()} isn't available: meeting requests are for {span}."
    return found, None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


@dataclass
class ParsedTime:
    value: str | None = None  # "15:30" or a period: "morning" / "afternoon"
    reason: str | None = None  # a time that can't be used
    ask_ampm: tuple[int, int] | None = None  # (hour, minute) when am or pm is needed


def parse_time(text: str, earliest: str, latest: str, *, allow_bare: bool, ampm_for: tuple[int, int] | None = None) -> ParsedTime:
    """Read a time of day. ``allow_bare``: a lone number counts ("3", "four thirty") because a time was just asked for."""
    low = text.casefold()
    lo, hi = _hhmm(earliest), _hhmm(latest)
    window = f"{_clock_text(*lo)} and {_clock_text(*hi)}"
    too_early_or_late = f"I can only take meeting requests between {window}."

    def inside(h: int, m: int) -> bool:
        return lo <= (h, m) <= hi

    def clock(h: int, m: int) -> ParsedTime:
        return ParsedTime(f"{h:02d}:{m:02d}") if inside(h, m) else ParsedTime(reason=too_early_or_late)

    if ampm_for is not None:  # "is that 3 am or 3 pm?" -> the answer
        h, m = ampm_for
        if re.search(r"\b(?:a\.?m\.?|morning)\b", low):
            return clock(h % 12, m)
        if re.search(r"\b(?:p\.?m\.?|afternoon|evening|noon)\b", low):
            return clock(h % 12 + 12, m)
        return ParsedTime(ask_ampm=ampm_for)
    if re.search(r"\bnoon\b|\bmidday\b", low):
        return clock(12, 0)
    for word, n in _NUMBER_WORDS.items():  # "half past three", "four thirty", "three o'clock"
        low = re.sub(rf"\b{word}\b", str(n), low)
    low = re.sub(r"\bhalf\s+past\s+(\d{1,2})\b", r"\1:30", low)
    low = re.sub(r"\bquarter\s+past\s+(\d{1,2})\b", r"\1:15", low)
    low = re.sub(r"\bquarter\s+to\s+(\d{1,2})\b", lambda m: f"{int(m[1]) - 1}:45", low)
    low = re.sub(r"\b(\d{1,2})\s+thirty\b", r"\1:30", low)
    low = re.sub(r"\b(\d{1,2})\s+o'?\s*clock\b", r"\1:00", low)
    pattern = r"(?<![\d/-])(?P<h>\d{1,2})(?:\s*[:.h]\s*(?P<m>\d{2}))?\s*(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?)?(?![\d/]|\s*(?:st|nd|rd|th)\b)"
    cue = r"(?:\bat|\bby|\baround|\bafter|\bbefore|\bfrom|@|\bat\s+about)\s*"
    for m in re.finditer(pattern, low):
        marked = bool(m["ap"]) or bool(m["m"]) or re.search(cue + r"$", low[: m.start()]) is not None
        if not (marked or allow_bare):
            continue
        h, mi = int(m["h"]), int(m["m"] or 0)
        if mi > 59 or h > 23:
            continue
        ap = (m["ap"] or "").replace(".", "").replace(" ", "")
        if ap == "am":
            return clock(h % 12, mi) if h <= 12 else ParsedTime(reason="That isn't a time I can read.")
        if ap == "pm":
            return clock(h % 12 + 12, mi) if h <= 12 else ParsedTime(reason="That isn't a time I can read.")
        if h >= 13 or (h == 0):
            return clock(h, mi)
        if re.search(r"\b(?:morning)\b", low):
            return clock(h % 12, mi)
        if re.search(r"\b(?:afternoon|evening)\b", low):
            return clock(h % 12 + 12, mi)
        options = [(h % 12, mi), (h % 12 + 12, mi)]
        fits = [o for o in options if inside(*o)]
        if len(fits) == 1:
            return ParsedTime(f"{fits[0][0]:02d}:{fits[0][1]:02d}")
        if not fits:
            return ParsedTime(reason=too_early_or_late)
        return ParsedTime(ask_ampm=(h, mi))
    if re.search(r"\bmorning\b", low):
        return ParsedTime("morning")
    if re.search(r"\bafternoon\b", low):
        return ParsedTime("afternoon")
    if re.search(r"\bevening\b|\bnight\b", low):
        return ParsedTime(reason=too_early_or_late)
    return ParsedTime()


# ----------------------------------------------------------------------------- the dialogue


class TaskReply(BaseModel):
    answer: str
    options: list[str] = Field(default_factory=list)
    outcome: Literal["task", "task_done", "task_cancelled"]
    kind: Literal["message", "meeting"]
    person_id: str | None = Field(default=None, description="The faculty member it is about, so 'she' works afterwards.")
    request_id: str | None = None


@dataclass
class _Task:
    kind: Literal["message", "meeting"]
    updated: float
    slots: dict[str, object] = field(default_factory=dict)  # name -> value; missing = not collected yet
    ask: str = "recipient"  # recipient | pick | text | day | time | ampm | purpose | visitor | contact | confirm
    retries: int = 0
    candidates: list[Person] = field(default_factory=list)
    ampm_for: tuple[int, int] | None = None

    @property
    def order(self) -> list[str]:
        if self.kind == "message":
            return ["recipient", "text", "visitor", "contact"]
        return ["recipient", "day", "time", "purpose", "visitor", "contact"]


class TaskManager:
    """Runs the two dialogues for every session. ``handle`` returns None when the utterance isn't for it."""

    def __init__(self, cfg: TaskSettings, kb: KBView, *, matcher: NameMatcher | None = None, focus_person=None,
                 webhook_url: str | None = None, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = datetime.now):
        self._cfg = cfg
        self._kb = kb
        self._matcher = matcher
        self._focus_person = focus_person  # session_id -> canonical id of the person last discussed, or None
        self._webhook = webhook_url
        self._client = client
        self._clock = clock
        self._now = now
        self._tasks: dict[str, _Task] = {}
        self._lock = threading.Lock()
        self._by_name = {p.name: p for p in kb.people.values()}

    # ----------------------------------------------------------------------- entry points

    def active(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and self._clock() - task.updated <= self._cfg.idle_s

    def reset(self, session_id: str) -> None:
        self._tasks.pop(session_id, None)

    def handle(self, session_id: str, text: str) -> TaskReply | None:
        if not self._cfg.enabled:
            return None
        text = re.sub(r"\s+", " ", text).strip()
        task = self._tasks.get(session_id)
        if task is not None and self._clock() - task.updated > self._cfg.idle_s:
            self._tasks.pop(session_id, None)
            task = None
        if task is None:
            kind = self._trigger(text)
            if kind is None:
                return None
            task = _Task(kind, self._clock())
            self._tasks[session_id] = task
            self._take_from_first_utterance(session_id, task, text)
            return self._next(session_id, task)
        task.updated = self._clock()
        if _HUMAN.search(text):
            self._tasks.pop(session_id, None)
            return None  # the normal path escalates to the receptionist
        free_text = task.ask in ("text", "purpose", "visitor")
        wants_out = _CANCEL.search(text) and (not free_text or len(text.split()) <= 4)
        if wants_out or (task.ask == "confirm" and _NO.match(text)):
            return self._cancel(session_id, task)
        return self._fill(session_id, task, text)

    @staticmethod
    def _trigger(text: str) -> Literal["message", "meeting"] | None:
        if _MEETING.search(text):
            return "meeting"
        if _MESSAGE.search(text):
            return "message"
        return None

    # ----------------------------------------------------------------------- the first utterance

    def _take_from_first_utterance(self, session_id: str, task: _Task, text: str) -> None:
        who = _WHO.search(text)
        if who:
            people, asks = self._resolve(session_id, who["who"])
            if len(people) == 1:
                task.slots["recipient"] = people[0]
            elif len(people) > 1:
                task.candidates = people
            elif asks:  # something that sounds like a name: check it with the visitor
                task.candidates = asks
        elif self._pronoun_person(session_id, text):
            task.slots["recipient"] = self._pronoun_person(session_id, text)
        if task.kind == "message":
            if "recipient" in task.slots and (m := _MESSAGE_TEXT.search(text)):
                cleaned = self._clean_message(m["text"])
                if len(cleaned.split()) >= 2:
                    task.slots["text"] = cleaned
        else:
            today = self._now().date()
            day, _ = parse_day(text, today, self._cfg.meeting_days)
            if day:
                task.slots["day"] = day
            parsed = parse_time(text, self._cfg.meeting_earliest, self._cfg.meeting_latest, allow_bare=False)
            if parsed.value and not self._past(task, parsed.value):
                task.slots["time"] = parsed.value
            if m := _PURPOSE.search(text):
                task.slots["purpose"] = m["text"].strip().rstrip(".")

    # ----------------------------------------------------------------------- asking

    def _who(self, task: _Task) -> str:
        person = task.slots.get("recipient")
        return person.name if isinstance(person, Person) else "them"

    def _next(self, session_id: str, task: _Task) -> TaskReply:
        """The next thing to ask (or the confirmation), given what has been collected."""
        prompts = self._cfg.prompts
        task.retries = 0
        if task.candidates and "recipient" not in task.slots:
            task.ask = "pick"
            names = [p.name for p in task.candidates]
            if len(names) == 1:
                return self._reply(task, f"Did you mean {names[0]}?", ["Yes", "No"])
            listed = ", ".join(names[:-1]) + f" or {names[-1]}"
            return self._reply(task, prompts.which_one.format(options=listed), names)
        for slot in task.order:
            if slot in task.slots:
                continue
            task.ask = slot
            who = self._who(task)
            if slot == "recipient":
                text = prompts.ask_recipient_message if task.kind == "message" else prompts.ask_recipient_meeting
                return self._reply(task, text)
            if slot == "text":
                return self._reply(task, prompts.ask_message.format(who=who))
            if slot == "day":
                return self._reply(task, prompts.ask_day.format(who=who))
            if slot == "time":
                day = task.slots.get("day")
                return self._reply(task, prompts.ask_time.format(day=_day_text(day) if isinstance(day, date) else "that day"))
            if slot == "purpose":
                return self._reply(task, prompts.ask_purpose, [prompts.option_skip])
            if slot == "visitor":
                return self._reply(task, prompts.ask_visitor_name.format(who=who))
            if slot == "contact":
                return self._reply(task, prompts.ask_contact.format(who=who), [prompts.option_skip])
        task.ask = "confirm"
        return self._reply(task, self._summary(task), [prompts.option_yes, prompts.option_no])

    def _reply(self, task: _Task, answer: str, options: list[str] | None = None, *, outcome="task", request_id=None) -> TaskReply:
        person = task.slots.get("recipient")
        return TaskReply(answer=answer, options=options or [], outcome=outcome, kind=task.kind,
                         person_id=person.canonical_id if isinstance(person, Person) else None,
                         request_id=request_id)

    def _summary(self, task: _Task) -> str:
        prompts = self._cfg.prompts
        s = task.slots
        contact = f", contact {s['contact']}" if s.get("contact") else ""
        visitor = s.get("visitor") or "you"
        if task.kind == "message":
            return prompts.confirm_message.format(who=self._who(task), text=s["text"], visitor=visitor, contact=contact)
        day, when = s["day"], s["time"]
        when_text = f"{_day_text(day)} at {_clock_text(*_hhmm(when))}" if re.fullmatch(r"\d\d:\d\d", str(when)) else f"{_day_text(day)}, {when}"
        purpose = f", about {s['purpose']}" if s.get("purpose") else ""
        return prompts.confirm_meeting.format(who=self._who(task), when=when_text, purpose=purpose, visitor=visitor, contact=contact)

    # ----------------------------------------------------------------------- answers

    def _retry(self, session_id: str, task: _Task, message: str, options: list[str] | None = None) -> TaskReply:
        task.retries += 1
        if task.retries > self._cfg.max_retries:
            self._tasks.pop(session_id, None)
            return self._reply(task, self._cfg.prompts.gave_up, outcome="task_cancelled")
        return self._reply(task, message, options)

    def _fill(self, session_id: str, task: _Task, text: str) -> TaskReply:
        prompts = self._cfg.prompts
        ask = task.ask

        if ask in ("recipient", "pick"):
            return self._fill_recipient(session_id, task, text)

        if ask == "confirm":
            if _YES.match(text):
                return self._finish(session_id, task)
            return self._retry(session_id, task, "Please say yes to send it, or no to cancel.",
                               [prompts.option_yes, prompts.option_no])

        if ask == "text":
            cleaned = self._clean_message(text)
            if not cleaned:
                return self._retry(session_id, task, prompts.ask_message.format(who=self._who(task)))
            task.slots["text"] = cleaned[:500]
        elif ask == "visitor":
            name = self._clean_name(text)
            if not name:
                return self._retry(session_id, task, prompts.ask_visitor_name.format(who=self._who(task)))
            task.slots["visitor"] = name
        elif ask == "purpose":
            task.slots["purpose"] = SKIPPED if _SKIP.match(text) else text.strip()[:300]
        elif ask == "contact":
            if _SKIP.match(text):
                task.slots["contact"] = SKIPPED
            else:
                contact = self._parse_contact(text)
                if contact is None:
                    return self._retry(session_id, task, prompts.bad_contact, [prompts.option_skip])
                task.slots["contact"] = contact
        elif ask == "day":
            day, reason = parse_day(text, self._now().date(), self._cfg.meeting_days)
            if day is None:
                return self._retry(session_id, task, prompts.bad_day.format(reason=reason or "I didn't catch the day."))
            task.slots["day"] = day
        elif ask in ("time", "ampm"):
            parsed = parse_time(text, self._cfg.meeting_earliest, self._cfg.meeting_latest, allow_bare=True,
                                ampm_for=task.ampm_for if ask == "ampm" else None)
            if parsed.ask_ampm:
                task.ask, task.ampm_for = "ampm", parsed.ask_ampm
                h, m = parsed.ask_ampm
                am, pm = (f"{h}:{m:02d} am", f"{h}:{m:02d} pm") if m else (f"{h} am", f"{h} pm")
                question = prompts.ask_ampm.format(am=am, pm=pm)
                if ask == "ampm":  # still not clear after being asked: count it, so this can't loop for ever
                    return self._retry(session_id, task, question, [am, pm])
                return self._reply(task, question, [am, pm])
            if parsed.value is None:
                return self._retry(session_id, task, prompts.bad_time.format(reason=parsed.reason or "I didn't catch the time."))
            if self._past(task, parsed.value):
                return self._retry(session_id, task, prompts.bad_time.format(reason="That time has already passed today."))
            task.slots["time"], task.ampm_for = parsed.value, None
        return self._next(session_id, task)

    def _past(self, task: _Task, value: str) -> bool:
        day = task.slots.get("day")
        if not (isinstance(day, date) and re.fullmatch(r"\d\d:\d\d", value)):
            return False
        now = self._now()
        return day == now.date() and _hhmm(value) <= (now.hour, now.minute)

    def _fill_recipient(self, session_id: str, task: _Task, text: str) -> TaskReply:
        prompts = self._cfg.prompts
        if task.candidates:  # "which one do you mean?" / "did you mean Dr X?"
            low = text.casefold().strip(" .?!")
            choice: Person | None = None
            if len(task.candidates) == 1 and _YES.match(low):
                choice = task.candidates[0]
            elif len(task.candidates) == 1 and _NO.match(low):
                task.candidates = []
                return self._retry(session_id, task, "Okay. Please say the full name of the person.")
            else:
                for word in re.findall(r"[a-z0-9]+", low):
                    if word in _ORDINALS and len(low.split()) <= 4 and -len(task.candidates) <= _ORDINALS[word] < len(task.candidates):
                        choice = task.candidates[_ORDINALS[word]]
                        break
                if choice is None:
                    people, _ = self._resolve(session_id, text)
                    narrowed = [p for p in people if p in task.candidates]
                    if len(narrowed) == 1:
                        choice = narrowed[0]
            if choice is None:
                names = [p.name for p in task.candidates]
                return self._retry(session_id, task, prompts.which_one.format(options=" or ".join(names)), names)
            task.slots["recipient"], task.candidates = choice, []
            return self._next(session_id, task)
        people, asks = self._resolve(session_id, text)
        if len(people) == 1:
            task.slots["recipient"] = people[0]
            return self._next(session_id, task)
        if len(people) > 1:
            task.candidates = people
            task.retries = 0
            return self._next(session_id, task)
        if asks:  # nothing matched exactly, but something sounded like a name
            task.candidates = asks
            return self._next(session_id, task)
        return self._retry(session_id, task, "I couldn't find anyone with that name on the faculty list. "
                                             "Please say the full name, for example Dr Deepa V Jose.")

    # ----------------------------------------------------------------------- reading what the visitor said

    def _pronoun_person(self, session_id: str, text: str) -> Person | None:
        if not (_PRONOUN.match(text) or re.search(r"\b(?:for|to)\s+(?:her|him)\b", text, _FLAGS)):
            return None
        cid = self._focus_person(session_id) if self._focus_person else None
        return self._kb.people.get(cid) if cid else None

    def _resolve(self, session_id: str, text: str) -> tuple[list[Person], list[Person]]:
        """(people the words name, people they might be meant to sound like)."""
        if (p := self._pronoun_person(session_id, text)) is not None:
            return [p], []
        fixed, asks = text, []
        if self._matcher is not None:
            fixes = self._matcher.scan(text)
            fixed = self._matcher.apply(text, fixes)
            for fix in fixes:
                if not fix.replacement:
                    asks.extend(self._by_name[n] for n in fix.options if n in self._by_name)
        return self._kb.find_people(fixed), asks

    @staticmethod
    def _clean_message(text: str) -> str:
        text = re.sub(r"^\s*(?:(?:please\s+)?(?:tell|say\s+to|inform)\s+(?:him|her|them|dr\.?\s+\w+)\s*(?:that)?|(?:my|the)\s+message\s+is|say(?:ing)?\s+(?:that)?|that)\s*[:,-]?\s*", "", text, flags=_FLAGS)
        return text.strip().strip('"').strip()

    @staticmethod
    def _clean_name(text: str) -> str:
        text = re.sub(r"^\s*(?:my\s+name\s+is|i\s+am|i'm|this\s+is|it'?s|it\s+is|call\s+me|name\s+is|myself)\s+", "", text, flags=_FLAGS)
        text = re.sub(r"[^\w .'-]", "", text).strip()
        words = text.split()
        if not words or len(words) > 5 or any(ch.isdigit() for ch in text):
            return ""
        return " ".join(w if w.isupper() and len(w) <= 3 else w.capitalize() for w in words)

    @staticmethod
    def _parse_contact(text: str) -> str | None:
        spoken = re.sub(r"\s+(?:at\s+the\s+rate(?:\s+of)?|at)\s+", "@", text, flags=_FLAGS)
        spoken = re.sub(r"\s+dot\s+", ".", spoken, flags=_FLAGS)
        # letters spelled out one by one ("j o h n at gmail dot com")
        spoken = re.sub(r"\b(?:[a-z0-9]\s+){2,}[a-z0-9](?=@)", lambda m: re.sub(r"\s+", "", m.group(0)), spoken, flags=_FLAGS)
        if m := _EMAIL.search(spoken):
            return m.group(0).lower()
        digits = re.sub(r"\D", "", text)
        if 7 <= len(digits) <= 15:
            return ("+" if text.strip().startswith("+") else "") + digits
        return None

    # ----------------------------------------------------------------------- done

    def _cancel(self, session_id: str, task: _Task) -> TaskReply:
        self._tasks.pop(session_id, None)
        return self._reply(task, self._cfg.prompts.cancelled, outcome="task_cancelled")

    def _finish(self, session_id: str, task: _Task) -> TaskReply:
        s = task.slots
        person: Person = s["recipient"]  # type: ignore[assignment]
        record = {
            "schema_version": 1,
            "request_id": uuid.uuid4().hex[:12],
            "type": task.kind,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session_id": session_id,
            "status": "pending",
            "recipient_id": person.canonical_id,
            "recipient": person.name,
            "visitor_name": s.get("visitor"),
            "contact": s.get("contact") or None,
        }
        if task.kind == "message":
            record["message"] = s["text"]
        else:
            record.update(meeting_date=s["day"].isoformat(), meeting_day=_WEEKDAYS[s["day"].weekday()],
                          meeting_time=s["time"], purpose=s.get("purpose") or None)
        self._store(record)
        self._tasks.pop(session_id, None)
        prompts = self._cfg.prompts
        done = prompts.sent_message if task.kind == "message" else prompts.sent_meeting
        return self._reply(task, done.format(who=person.name), outcome="task_done", request_id=record["request_id"])

    def _store(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._cfg.requests.parent.mkdir(parents=True, exist_ok=True)
            with self._cfg.requests.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self._webhook:
            try:
                client = self._client or httpx.Client(timeout=self._cfg.webhook_timeout_s)
                client.post(self._webhook, json=record, timeout=self._cfg.webhook_timeout_s)
            except httpx.HTTPError as exc:  # the request is on disk; the desk alert is best effort
                log.warning("receptionist webhook failed: %s", exc)
