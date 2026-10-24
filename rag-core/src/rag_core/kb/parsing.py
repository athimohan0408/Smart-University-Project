"""Pure parsers for the faculty sheet's free-text cells. No I/O, so every rule is unit-testable.

Times: an explicit am/pm marker always wins. A time without one gets both readings (e.g. 1:45 ->
01:45 or 13:45), and only the reading inside the day window survives. The window is < 12 h
(enforced in config), so at most one reading can fit. If none or several pairs fit, the range is
left unparsed instead of guessed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import time

from rag_core.kb.models import WEEKDAYS, ClassSlot, ConsultationHours, ConsultationSlot, Weekday


@dataclass(frozen=True)
class DayWindow:
    """Minutes since midnight. Times written without am/pm must fall inside [start, end]."""

    start: int
    end: int

    @classmethod
    def from_times(cls, start: time, end: time) -> DayWindow:
        return cls(start.hour * 60 + start.minute, end.hour * 60 + end.minute)

    def __contains__(self, minutes: int) -> bool:
        return self.start <= minutes <= self.end


# --------------------------------------------------------------------------- time ranges

_CLOCK = r"(?<!\d)(?P<{p}h>\d{{1,2}})(?:[.:](?P<{p}m>\d{{2}}))?(?!\d)\s*(?:(?P<{p}ap>[ap])\.?m\.?)?"
_RANGE = re.compile(
    _CLOCK.format(p="s") + r"\s*(?:to|-|–)\s*" + _CLOCK.format(p="e"), re.IGNORECASE
)


def _readings(hour: str, minute: str | None, marker: str | None) -> list[tuple[int, bool]]:
    """Possible (minutes since midnight, needs_window_check) readings of one clock time."""
    h, m = int(hour), int(minute or 0)
    if h > 23 or m > 59:
        return []
    if marker:
        if not 1 <= h <= 12:
            return []
        return [((h % 12 + (12 if marker.lower() == "p" else 0)) * 60 + m, False)]
    if h == 0 or h > 12:  # already unambiguous 24h
        return [(h * 60 + m, False)]
    return [((h % 12) * 60 + m, True), ((h % 12 + 12) * 60 + m, True)]


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def resolve_range(match: re.Match[str], window: DayWindow) -> tuple[str, str] | None:
    """Turn a ``_RANGE`` match into 24h ("HH:MM", "HH:MM"), or None if it can't be pinned down."""
    starts = _readings(match["sh"], match["sm"], match["sap"])
    ends = _readings(match["eh"], match["em"], match["eap"])
    pairs = {
        (s, e)
        for s, s_check in starts
        for e, e_check in ends
        if s < e and (not s_check or s in window) and (not e_check or e in window)
    }
    if len(pairs) != 1:
        return None
    start, end = pairs.pop()
    return _hhmm(start), _hhmm(end)


def resolve_clock(hour: str, minute: str | None, marker: str | None, window: DayWindow) -> str | None:
    """One clock time -> "HH:MM", choosing am/pm by the window as for ranges; None if not unique."""
    fits = [m for m, check in _readings(hour, minute, marker) if not check or m in window]
    return _hhmm(fits[0]) if len(fits) == 1 else None


def parse_time_range(text: str, window: DayWindow) -> tuple[str, str] | None:
    """Parse a whole string like '11.45 to 1.45 pm' -> ('11:45', '13:45')."""
    match = _RANGE.fullmatch(text.strip())
    return resolve_range(match, window) if match else None


# --------------------------------------------------------------------------- timetable lines

# Programme codes as they appear in timetable lines. MSAIM before MSC is irrelevant for the regex
# (MSC can't match a prefix of MSAIM) but keeps the longest codes first by convention.
PROGRAMME_CODES = ("MSAIM", "MCA", "BCA", "BSC", "MSC")
# Programmes whose lines carry a stream token before the subject: '3BSC CS DBMS', '1BSC CS C PROG'.
_STREAM_PROGRAMMES = frozenset({"BSC", "MSC"})

_LINE = re.compile(r"^\s*(?P<range>.+?)\s+-\s+(?P<code>\S.*?)\s*$")
_CODE_HEAD = re.compile(
    rf"^(?:(?P<sem>\d{{1,2}})\s*)?(?P<prog>{'|'.join(PROGRAMME_CODES)})(?![A-Za-z])\s*-?\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_SECTION = re.compile(r"^(?P<section>[A-Z])(?:\s*-\s*|\s+)(?P<rest>\S.*)$")  # case-sensitive on purpose
_PARENS = re.compile(r"\(([^)]*)\)")
_SUBJECT_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9/&+]*$")
_BARE_CODE = re.compile(r"^[A-Z][A-Z0-9/&+]{1,11}$")


def parse_class_code(code: str) -> dict | None:
    """'2MCA A RM' / '7BCA-RM LAB' / '7 BCA BI (SG/JF)' / 'CPCG' -> ClassSlot fields, or None."""
    co_teachers = [t.strip() for group in _PARENS.findall(code) for t in re.split(r"[/,]", group) if t.strip()]
    code = _PARENS.sub(" ", code)

    fields: dict = {"semester": None, "programme": None, "stream": None, "section": None}
    head = _CODE_HEAD.match(code.strip())
    rest = code.strip()
    if head:
        fields["semester"] = int(head["sem"]) if head["sem"] else None
        fields["programme"] = head["prog"].upper()
        rest = head["rest"]
        if section := _SECTION.match(rest):
            fields["section"], rest = section["section"], section["rest"]

    tokens = rest.split()
    is_lab = len(tokens) > 1 and tokens[-1].upper() == "LAB"
    if is_lab:
        tokens.pop()
    if (
        fields["programme"] in _STREAM_PROGRAMMES
        and fields["section"] is None
        and len(tokens) >= 2
        and tokens[0].isalpha()
        and tokens[0].isupper()
    ):
        fields["stream"] = tokens.pop(0)

    if not tokens or not all(_SUBJECT_TOKEN.fullmatch(t) for t in tokens):
        return None
    if head is None and not (len(tokens) == 1 and _BARE_CODE.fullmatch(tokens[0])):
        return None  # without a programme, only a single upper-case code like 'MDC' is accepted
    return {**fields, "subject_code": " ".join(tokens), "is_lab": is_lab, "co_teachers": co_teachers}


def parse_timetable_line(line: str, day: Weekday, window: DayWindow) -> ClassSlot | None:
    """'11.45 to 1.45 pm - 2 MSAIM ML LAB' -> ClassSlot, or None if any part can't be read."""
    match = _LINE.match(line)
    if not match:
        return None
    times = parse_time_range(match["range"], window)
    code = parse_class_code(match["code"])
    if times is None or code is None:
        return None
    return ClassSlot(day=day, start_time=times[0], end_time=times[1], raw=line.strip(), **code)


# --------------------------------------------------------------------------- consultation hours

_DAY = re.compile(r"\b(" + "|".join(WEEKDAYS) + ")", re.IGNORECASE)
_SEPARATORS = re.compile(r"[\s:;,=()\-–.]+")


def parse_consultation_hours(raw: str | None, window: DayWindow) -> ConsultationHours:
    """Best effort. A segment yields slots only with exactly one day and readable time range(s).

    Segments that yield nothing, or leave unexplained text behind (e.g. 'BANGALORE CENTRAL
    CAMPUS'), are listed in ``unparsed_segments``. ``raw`` is always preserved.
    """
    if raw is None:
        return ConsultationHours(raw=None)
    slots: list[ConsultationSlot] = []
    unparsed: list[str] = []
    for segment in (s.strip() for s in raw.split(";")):
        if not segment:
            continue
        days = {d.capitalize() for d in _DAY.findall(segment)}
        ranges = [resolve_range(m, window) for m in _RANGE.finditer(segment)]
        if len(days) != 1 or not ranges or None in ranges:
            unparsed.append(segment)
            continue
        day = days.pop()
        slots.extend(ConsultationSlot(day=day, start=s, end=e) for s, e in ranges)
        leftover = _DAY.sub(" ", _RANGE.sub(" ", segment))
        if _SEPARATORS.sub("", leftover):
            unparsed.append(segment)
    return ConsultationHours(raw=raw, slots=slots, unparsed_segments=unparsed)


# --------------------------------------------------------------------------- names

_TITLE = re.compile(r"^\s*((?:(?:dr|fr|prof|rev)\.?\s+)+)", re.IGNORECASE)
_TITLE_CANONICAL = {"dr": "Dr", "fr": "Fr.", "prof": "Prof.", "rev": "Rev."}
_FR_SHORT_NAME = re.compile(r"^fr\b\.?", re.IGNORECASE)


def split_title(raw_name: str) -> tuple[str | None, str]:
    """'Dr DEEPA V JOSE' -> ('Dr', 'Deepa V Jose')."""
    name = " ".join(raw_name.split())
    match = _TITLE.match(name)
    title = None
    if match:
        title = " ".join(_TITLE_CANONICAL[t.rstrip(".").lower()] for t in match.group(1).split())
        name = name[match.end():]
    return title, name.title()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def dedupe(items: Iterable[str]) -> list[str]:
    """Drop case-insensitive duplicates, keeping the first spelling and order."""
    seen: set[str] = set()
    out = []
    for item in items:
        if item.casefold() not in seen:
            seen.add(item.casefold())
            out.append(item)
    return out


def build_aliases(full_name: str, title: str | None, short_name: str | None) -> list[str]:
    """First name, last name, name without initials, short code, and 'Fr.' variants for priests.

    Short codes are included as data, but NLU must only match them as exact, case-sensitive
    tokens: several ('AS', 'RM', 'SS') collide with words or course codes.
    """
    words = [w for w in full_name.split() if len(w.rstrip(".")) > 1]  # drop initials
    aliases: list[str] = []
    if words:
        aliases += [words[0], words[-1], " ".join(words)]
    if short_name:
        aliases.append(short_name)
    is_priest = (title is not None and "Fr." in title.split()) or bool(
        short_name and _FR_SHORT_NAME.match(short_name)
    )
    if is_priest and words:
        aliases += [f"Fr. {words[0]}", f"Fr {words[0]}", f"Father {words[0]}", f"Fr. {' '.join(words)}"]
    return [a for a in dedupe(aliases) if a.casefold() != full_name.casefold()]


# --------------------------------------------------------------------------- other cells

_LIST_SPLIT = re.compile(r"\s*(?:,|;|\band\b)\s*", re.IGNORECASE)


def split_research_areas(raw: str | None) -> list[str]:
    """Split on commas / 'and'. Lossy for phrases like 'MANET and VANET': keep the raw text too."""
    if not raw:
        return []
    return dedupe(p for p in (part.strip(" .") for part in _LIST_SPLIT.split(raw)) if p)


def split_semicolon_list(raw: str | None) -> list[str]:
    return dedupe(p.strip() for p in raw.split(";") if p.strip()) if raw else []


def normalise_room(raw: str | None) -> str | None:
    """Rooms are always text: '871' stays '871', '014' keeps its zero, '871.0' -> '871'."""
    if raw is None:
        return None
    room = " ".join(raw.split())
    if re.fullmatch(r"\d+\.0", room):
        room = room[:-2]
    return room or None


_ROOM_NUMBER = re.compile(r"^\d{1,4}[A-Za-z]?(?:[ -][A-Za-z0-9]{1,3})?$")


def is_room_number(room: str) -> bool:
    """'871', '014', '607A', '332 C', '628-11' -> True; 'Insight Zone 2, Centre for AI' -> False."""
    return bool(_ROOM_NUMBER.fullmatch(room))


_SENTENCE_BREAK = re.compile(r"(?<=\.)\s+(?=['\"A-Z])")
# Heuristic cues for curator notes that contradict a field. The report lists what they catch.
_CONFLICT_CUES = re.compile(r"\balso mentions\b|\bbut\b|\(old\)", re.IGNORECASE)


def note_conflicts(notes: str | None) -> list[str]:
    """Sentences in the Notes cell that contradict the row, e.g. 'Main list also mentions room 875'."""
    if not notes:
        return []
    return [s.strip() for s in _SENTENCE_BREAK.split(notes) if _CONFLICT_CUES.search(s)]
