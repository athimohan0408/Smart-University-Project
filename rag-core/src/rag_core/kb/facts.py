"""Turn validated KB records into small, self-contained, citable facts: one retrieval chunk each.

Every fact names its subject in full, so a fact retrieved on its own is unambiguous. Gaps are
written as explicit "not listed" facts so the retriever finds them and the answer says the
information isn't available instead of inventing it. Conflicts recorded in the sheet's notes are
written into the affected facts as a caution, and those facts get ``verified=False``.

Pure functions only (no I/O, no models), so fact ids and text are fully deterministic.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from rag_core.kb.glossary import Glossary
from rag_core.kb.models import TIMETABLE_DAYS, ClassSlot, Faculty, FacultyKB
from rag_core.kb.parsing import dedupe, is_room_number, slugify

if TYPE_CHECKING:
    from rag_core.kb.campus import CampusKB, Place


class FactCategory(StrEnum):
    FACULTY_PROFILE = "faculty_profile"
    FACULTY_LOCATION = "faculty_location"
    FACULTY_CONSULTATION = "faculty_consultation"
    TEACHING = "teaching"
    SCHEDULE = "schedule"
    ROOM = "room"
    PROGRAMME = "programme"
    COURSE = "course"
    EVENT = "event"
    FACILITY = "facility"


# Categories whose source is optional: they come only from the verified campus sheet (kb/campus.py).
CATEGORIES_WITHOUT_SOURCE = frozenset({FactCategory.EVENT, FactCategory.FACILITY})


class Fact(BaseModel):
    """One retrieval chunk. Multi-valued fields are comma-joined so Chroma metadata stays scalar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    category: FactCategory
    text: str
    faculty_id: str = ""
    room: str = ""
    floor: str = ""
    day: str = ""
    subject_code: str = ""
    programme: str = ""
    verified: bool = Field(
        description="True when every statement comes straight from the source sheet or a verified "
        "glossary entry, with no recorded conflict or unreadable part."
    )
    source_file: str
    keywords: list[str] = Field(
        default_factory=list, description="Extra lexical-match terms (aliases) for BM25; never shown to the LLM."
    )

    def chroma_metadata(self) -> dict[str, str | bool]:
        return self.model_dump(mode="json", exclude={"text", "keywords"})


@dataclass(frozen=True)
class FactContext:
    glossary: Glossary
    glossary_source: str
    home_block: str
    home_floor: str
    include_unverified_expansions: bool


# --------------------------------------------------------------------------- text helpers


def _join(values: Iterable[str | None]) -> str:
    return ",".join(dedupe(v for v in values if v))


def _and_join(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _hm(hhmm: str) -> str:
    """'07:30' -> '7:30' for readable fact text."""
    return f"{int(hhmm[:2])}:{hhmm[3:]}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _line_code(slot: ClassSlot) -> str:
    """The class-code part of the raw timetable line: '7.30 to 9 am - 5 MSAIM TML' -> '5 MSAIM TML'."""
    return re.split(r"\s+-\s+", slot.raw, maxsplit=1)[-1]


def _caution(sentence: str, whose: str | None = None) -> str:
    owner = f" in the record of {whose}" if whose else ""
    return f" Caution: the source records contain a conflicting note{owner}: {sentence}"


_LOCATION_NOTE = re.compile(r"\broom\b|\bblock\b|\bfloor\b|\b\d{3,4}[A-Z]?\b", re.IGNORECASE)
_ROOM_TOKEN = re.compile(r"\b(\d{3,4}[A-Z]?)\b")


def _location_conflicts(fac: Faculty) -> list[str]:
    return [c for c in fac.conflicts if _LOCATION_NOTE.search(c)]


def _profile_conflicts(fac: Faculty) -> list[str]:
    return [c for c in fac.conflicts if not _LOCATION_NOTE.search(c)]


class _Describer:
    """Glossary-aware names for programme and subject codes, honouring the verified flag."""

    def __init__(self, ctx: FactContext):
        self._ctx = ctx

    def subject(self, code: str) -> tuple[str, bool]:
        entry = self._ctx.glossary.subject(code)
        if entry and entry.verified:
            return f"{entry.full_name} ({code})", True
        if entry and entry.is_expanded and self._ctx.include_unverified_expansions:
            return f'subject code {code} ("{entry.full_name}", full name not verified)', False
        return f"subject code {code} (full name not verified)", False

    def programme(self, code: str) -> tuple[str, bool]:
        entry = self._ctx.glossary.programme(code)
        if entry and entry.verified:
            return f"{entry.full_name} ({code})", True
        if entry and entry.is_expanded and self._ctx.include_unverified_expansions:
            return f'{code} ("{entry.full_name}", programme name not verified)', False
        return f"{code} (programme name not verified)", False

    def programmes_sentence(self, codes: Iterable[str]) -> tuple[str, bool]:
        described = [self.programme(c) for c in sorted(set(codes))]
        if not described:
            return "", True
        return f" Programmes: {'; '.join(d for d, _ in described)}.", all(v for _, v in described)


def _class_groups(slots: Iterable[ClassSlot]) -> list[str]:
    """'2nd semester MSAIM, classes and lab' per (semester, programme, stream, section)."""
    kinds: dict[tuple, set[bool]] = defaultdict(set)
    for s in slots:
        kinds[(s.semester, s.programme, s.stream, s.section)].add(s.is_lab)
    phrases = []
    def order(group: tuple) -> tuple:
        sem, prog, stream, section = group
        return (sem or 0, prog or "", stream or "", section or "")

    for (sem, prog, stream, section), labs in sorted(kinds.items(), key=lambda kv: order(kv[0])):
        who = []
        if sem is not None:
            who.append(f"{_ordinal(sem)} semester")
        if prog:
            who.append(prog if sem is not None else f"{prog} (semester not listed)")
        if stream:
            who.append(f"{stream} stream")
        if section:
            who.append(f"section {section}")
        kind = "classes and lab" if labs == {True, False} else ("lab" if True in labs else "classes")
        phrases.append(f"{' '.join(who) or 'programme and semester not listed'}, {kind}")
    return phrases


# --------------------------------------------------------------------------- faculty facts


def _office_key(fac: Faculty) -> tuple[str | None, str | None, str | None]:
    return fac.location.room, fac.location.block, fac.location.floor


def _profile_fact(fac: Faculty, source: str) -> Fact:
    name = fac.display_name
    text = f"{name}" + (f" (short name {fac.short_name})" if fac.short_name else "")
    text += f" is a faculty member in the {fac.department} department." if fac.department else " is a faculty member."
    if fac.specialization:
        text += f" Specialisation: {fac.specialization}."
    research = fac.research_areas_raw.rstrip(". ") if fac.research_areas_raw else "not listed"
    text += f" Research areas: {research}."
    if fac.programmes_taught:
        text += f" Programmes taught: {', '.join(fac.programmes_taught)}."
    if fac.currently_handling:
        text += f" Currently handling: {fac.currently_handling}."
    text += f" Email: {fac.email or 'not listed'}."
    if fac.weekly_hours is not None:
        text += f" Weekly teaching hours: {fac.weekly_hours}."
    conflicts = _profile_conflicts(fac)
    text += "".join(_caution(c) for c in conflicts)
    return Fact(
        fact_id=f"faculty:{fac.id}:profile",
        category=FactCategory.FACULTY_PROFILE,
        text=text,
        faculty_id=fac.id,
        verified=not conflicts,
        source_file=source,
        keywords=[fac.full_name, *fac.aliases],
    )


def _location_fact(fac: Faculty, sharers: Sequence[Faculty], ctx: FactContext, source: str) -> Fact:
    name, loc = fac.display_name, fac.location
    place = None
    if loc.room:
        place = f"room {loc.room}" if is_room_number(loc.room) else f'listed as "{loc.room}"'
    where = ", ".join(p for p in (loc.block, loc.floor) if p)

    if not (loc.room or loc.block or loc.floor):
        text = f"{name}'s office location is not listed."
    elif loc.on_8th_floor:
        text = f"{name}'s office is {', '.join(p for p in (place, where) if p)} (on this floor)."
    else:
        text = (
            f"{name} is not on this floor ({ctx.home_block}, {ctx.home_floor}); office is in "
            + ", ".join(p for p in (where or None, place or "room not listed") if p)
            + "."
        )
    if sharers:
        text += f" They share this office with {_and_join([s.display_name for s in sharers])}."
    conflicts = _location_conflicts(fac)
    text += "".join(_caution(c) for c in conflicts)
    return Fact(
        fact_id=f"faculty:{fac.id}:location",
        category=FactCategory.FACULTY_LOCATION,
        text=text,
        faculty_id=fac.id,
        room=loc.room or "",
        floor=loc.floor or "",
        verified=not conflicts,
        source_file=source,
        keywords=[fac.full_name, *fac.aliases],
    )


def _consultation_fact(fac: Faculty, source: str) -> Fact:
    name, hours = fac.display_name, fac.consultation_hours
    slots = "; ".join(f"{s.day} {_hm(s.start)}-{_hm(s.end)}" for s in hours.slots)
    if hours.status == "missing":
        text = f"Consultation hours for {name} are not listed."
    else:
        text = f'{name}\'s consultation hours, as listed: "{hours.raw}".'
        if hours.status == "parsed":
            text += f" In 24-hour time: {slots}."
        elif hours.status == "partial":
            unclear = "; ".join(f'"{seg}"' for seg in hours.unparsed_segments)
            text += f" Readable part in 24-hour time: {slots}. Unclear part: {unclear}."
        else:
            text += " The days and times in this entry could not be confirmed."
    return Fact(
        fact_id=f"faculty:{fac.id}:consultation",
        category=FactCategory.FACULTY_CONSULTATION,
        text=text,
        faculty_id=fac.id,
        day=_join(s.day for s in hours.slots),
        verified=hours.status in ("parsed", "missing"),
        source_file=source,
        keywords=[fac.full_name, *fac.aliases],
    )


def _teaching_facts(
    fac: Faculty, by_short_name: dict[str, Faculty], ctx: FactContext, describe: _Describer, source: str
) -> list[Fact]:
    by_code: dict[str, list[ClassSlot]] = defaultdict(list)
    for slot in fac.timetable:
        by_code[slot.subject_code].append(slot)

    facts = []
    for code in sorted(by_code, key=str.casefold):
        slots = by_code[code]
        subject, subject_ok = describe.subject(code)
        programmes = [s.programme for s in slots if s.programme]
        prog_text, prog_ok = describe.programmes_sentence(programmes)
        text = f"{fac.display_name} teaches {subject}: {'; '.join(_class_groups(slots))}.{prog_text}"

        co = sorted({c for s in slots for c in s.co_teachers if c != fac.short_name})
        if co:
            names = [
                f"{by_short_name[c].display_name} ({c})" if c in by_short_name else f"{c} (not identified in the faculty list)"
                for c in co
            ]
            text += f" Co-taught with: {', '.join(names)}."
        facts.append(
            Fact(
                fact_id=f"faculty:{fac.id}:teaching:{slugify(code)}",
                category=FactCategory.TEACHING,
                text=text,
                faculty_id=_join([fac.id, *(by_short_name[c].id for c in co if c in by_short_name)]),
                subject_code=code,
                programme=_join(programmes),
                verified=subject_ok and prog_ok,
                source_file=f"{source},{ctx.glossary_source}",
                keywords=[fac.full_name, *fac.aliases],
            )
        )
    return facts


def _schedule_facts(fac: Faculty, source: str) -> list[Fact]:
    facts = []
    for day in TIMETABLE_DAYS:
        slots = sorted((s for s in fac.timetable if s.day == day), key=lambda s: (s.start_time, s.end_time))
        if slots:
            entries = "; ".join(f"{_hm(s.start_time)}-{_hm(s.end_time)} {_line_code(s)}" for s in slots)
            text = f"On {day}, {fac.display_name} has: {entries}."
        else:
            text = f"On {day}, no classes are listed for {fac.display_name} in the timetable."
        facts.append(
            Fact(
                fact_id=f"faculty:{fac.id}:schedule:{day.lower()}",
                category=FactCategory.SCHEDULE,
                text=text,
                faculty_id=fac.id,
                day=day,
                subject_code=_join(s.subject_code for s in slots),
                programme=_join(s.programme for s in slots),
                verified=True,
                source_file=source,
                keywords=[fac.full_name, *fac.aliases],
            )
        )
    return facts


# --------------------------------------------------------------------------- shared-entity facts


def _room_facts(faculty: Sequence[Faculty], ctx: FactContext, source: str) -> list[Fact]:
    occupants: dict[tuple, list[Faculty]] = defaultdict(list)
    for fac in faculty:
        if fac.location.room and is_room_number(fac.location.room):
            occupants[_office_key(fac)].append(fac)

    # Notes elsewhere may mention a room, e.g. "Main list also mentions room 875".
    mentions: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for fac in faculty:
        for sentence in _location_conflicts(fac):
            for room in set(_ROOM_TOKEN.findall(sentence)):
                mentions[room.casefold()].append((sentence, fac.display_name))

    slug_counts = defaultdict(int)
    for room, _, _ in occupants:
        slug_counts[slugify(room)] += 1

    facts = []
    for (room, block, floor), people in sorted(occupants.items(), key=lambda kv: tuple(v or "" for v in kv[0])):
        on_floor = people[0].location.on_8th_floor
        where = ", ".join(p for p in (block, floor) if p) or "block and floor not listed"
        text = (
            f"Room {room} is in {where} ({'on this floor' if on_floor else 'not on this floor'}). "
            f"It is the office of {_and_join([p.display_name for p in people])}."
        )
        cautions = [(c, p.display_name) for p in people for c in _location_conflicts(p)]
        cautions += mentions.get(room.casefold(), [])
        cautions = list(dict.fromkeys(cautions))
        text += "".join(_caution(sentence, whose) for sentence, whose in cautions)

        slug = slugify(room)
        fact_id = f"room:{slug}" if slug_counts[slug] == 1 else f"room:{slugify(block or 'unknown')}:{slug}"
        facts.append(
            Fact(
                fact_id=fact_id,
                category=FactCategory.ROOM,
                text=text,
                faculty_id=_join(p.id for p in people),
                room=room,
                floor=floor or "",
                verified=not cautions,
                source_file=source,
                keywords=[f"room {room}", *(p.full_name for p in people)],
            )
        )
    return facts


def _places_sentence(code: str, places: Sequence[Place]) -> str:
    listed = []
    for p in places:
        where = ", ".join(x for x in (f"room {p.room}" if p.room else None, p.block, p.floor) if x)
        listed.append(f"{p.name} ({where})")
    return f" Classrooms and labs used by {code}: {'; '.join(listed)}."


def _programme_facts(
    faculty: Sequence[Faculty], ctx: FactContext, describe: _Describer, source: str,
    places: dict[str, list[Place]] | None = None,
) -> list[Fact]:
    slots_by_prog: dict[str, list[tuple[Faculty, ClassSlot]]] = defaultdict(list)
    for fac in faculty:
        for slot in fac.timetable:
            if slot.programme:
                slots_by_prog[slot.programme].append((fac, slot))

    codes = sorted(set(slots_by_prog) | set(ctx.glossary.programmes), key=str.casefold)
    facts = []
    for code in codes:
        entry = ctx.glossary.programme(code)
        described, ok = describe.programme(code)
        pairs = slots_by_prog.get(code, [])
        text = f"Programme {described}."
        if entry and entry.aliases:
            text += f" Also written as: {', '.join(entry.aliases)}."
        if pairs:
            teachers = dedupe(f.display_name for f, _ in pairs)
            subjects = sorted({s.subject_code for _, s in pairs}, key=str.casefold)
            text += f" Faculty with {code} classes in the timetable: {', '.join(teachers)}."
            text += f" Subject codes in the {code} timetable: {', '.join(subjects)}."
        else:
            text += f" No {code} classes appear in the timetable."
        if places and places.get(code):
            text += _places_sentence(code, places[code])
        else:
            text += f" Classroom locations for {code} are not listed."
        facts.append(
            Fact(
                fact_id=f"programme:{slugify(code)}",
                category=FactCategory.PROGRAMME,
                text=text,
                faculty_id=_join(f.id for f, _ in pairs),
                subject_code=_join(sorted({s.subject_code for _, s in pairs}, key=str.casefold)),
                programme=code,
                verified=ok,
                source_file=f"{source},{ctx.glossary_source}",
                keywords=list(entry.aliases) if entry else [],
            )
        )
    return facts


def _course_facts(faculty: Sequence[Faculty], ctx: FactContext, describe: _Describer, source: str) -> list[Fact]:
    by_code: dict[str, list[tuple[Faculty, ClassSlot]]] = defaultdict(list)
    for fac in faculty:
        for slot in fac.timetable:
            by_code[slot.subject_code].append((fac, slot))

    facts = []
    for code in sorted(by_code, key=str.casefold):
        pairs = by_code[code]
        subject, subject_ok = describe.subject(code)
        programmes = [s.programme for _, s in pairs if s.programme]
        prog_text, prog_ok = describe.programmes_sentence(programmes)
        teachers = dedupe(f.display_name for f, _ in pairs)
        entry = ctx.glossary.subject(code)
        text = (
            f"Course: {subject}. Taught to: {'; '.join(_class_groups(s for _, s in pairs))}."
            f" Taught by: {', '.join(teachers)}.{prog_text} Classroom locations for {code} are not listed."
        )
        facts.append(
            Fact(
                fact_id=f"course:{slugify(code)}",
                category=FactCategory.COURSE,
                text=text,
                faculty_id=_join(f.id for f, _ in pairs),
                subject_code=code,
                programme=_join(programmes),
                verified=subject_ok and prog_ok,
                source_file=f"{source},{ctx.glossary_source}",
                keywords=list(entry.aliases) if entry else [],
            )
        )
    return facts


# --------------------------------------------------------------------------- entry point


def build_facts(kb: FacultyKB, ctx: FactContext, campus: CampusKB | None = None) -> list[Fact]:
    """All facts in a fixed order; fact ids are derived only from the data, so rebuilds match.

    ``campus``: verified places and events (kb/campus.py). Without it, or with an empty sheet, the
    facts are exactly the faculty-only ones.
    """
    from rag_core.kb.campus import build_campus_facts, programme_places

    source = kb.source
    describe = _Describer(ctx)
    by_short_name = {f.short_name: f for f in kb.faculty if f.short_name}
    by_office: dict[tuple, list[Faculty]] = defaultdict(list)
    for fac in kb.faculty:
        if fac.location.room:
            by_office[_office_key(fac)].append(fac)

    facts: list[Fact] = []
    for fac in kb.faculty:
        sharers = [f for f in by_office.get(_office_key(fac), []) if f.id != fac.id] if fac.location.room else []
        facts.append(_profile_fact(fac, source))
        facts.append(_location_fact(fac, sharers, ctx, source))
        facts.append(_consultation_fact(fac, source))
        facts.extend(_teaching_facts(fac, by_short_name, ctx, describe, source))
        facts.extend(_schedule_facts(fac, source))
    facts.extend(_room_facts(kb.faculty, ctx, source))
    facts.extend(_programme_facts(kb.faculty, ctx, describe, source, programme_places(campus)))
    facts.extend(_course_facts(kb.faculty, ctx, describe, source))
    if campus is not None:
        facts.extend(build_campus_facts(campus, ctx.home_block, ctx.home_floor))

    ids = [f.fact_id for f in facts]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"duplicate fact ids: {dupes}")
    return facts


def facts_digest(facts: Sequence[Fact]) -> str:
    """sha256 over the facts as written to facts.jsonl (one JSON line each), so it equals the file's hash."""
    sha = hashlib.sha256()
    for fact in facts:
        sha.update(fact.model_dump_json().encode("utf-8") + b"\n")
    return sha.hexdigest()
