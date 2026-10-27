"""The entity-linking vocabulary (gazetteer), built at index time from the KB.

Sources, each kept with its trust level:
- faculty.json: names, name parts, ingest aliases ("Father Jossy"), short codes, research areas.
- the facts: room, course and programme ids, so every id the linker returns exists in the index.
- glossary.yaml: code expansions and aliases. A ``full_name: TODO`` is never used, and aliases
  from an unverified entry are marked ``verified=False`` (the linker lowers their score).
- entity_lexicon.yaml: facility/event words, honorifics, codes that are also everyday words.

``just index`` writes it to ``paths.gazetteer`` with the facts digest, so a stale file is
detected on load. Building is pure; text normalisation shared with the extractor lives here too.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from rag_core.contracts import EntityType
from rag_core.kb.facts import Fact, FactCategory, facts_digest
from rag_core.kb.glossary import Glossary, GlossaryEntry
from rag_core.kb.models import FacultyKB
from rag_core.kb.parsing import slugify

# --------------------------------------------------------------------------- text normalisation

# Dotted acronyms stay one token ("M.Sc", "M.C.A", "P.K."); otherwise words, numbers and "&".
# A trailing possessive is left out of the token's span: "Jose's" -> "Jose".
_TOKEN = re.compile(r"(?:[A-Za-z]\.){2,}[A-Za-z]?|[A-Za-z]\.[A-Za-z]+|[A-Za-z0-9]+(?:['’][A-Za-z]+)*|&")
_POSSESSIVE = re.compile(r"['’]s$", re.IGNORECASE)


@dataclass(frozen=True)
class Token:
    raw: str
    norm: str
    start: int
    end: int


def tokenize(text: str) -> list[Token]:
    """Tokens with character offsets into ``text``; ``norm`` is lower case without dots or apostrophes."""
    tokens = []
    for match in _TOKEN.finditer(text):
        raw, start, end = match.group(), match.start(), match.end()
        if possessive := _POSSESSIVE.search(raw):
            raw, end = raw[: possessive.start()], start + possessive.start()
        norm = "and" if raw == "&" else re.sub(r"['’.]", "", raw.casefold())
        if norm:
            tokens.append(Token(raw, norm, start, end))
    return tokens


def normalise(text: str) -> str:
    """'M.Sc AI & ML' -> 'msc ai and ml'; "Deepa's" -> 'deepa'; "ma'am" -> 'maam'."""
    return " ".join(t.norm for t in tokenize(text))


def compact(norm: str) -> str:
    """Spaces removed, for ASR splits: 'sri devi' -> 'sridevi', '8 7 4' -> '874'."""
    return norm.replace(" ", "")


def room_key(room: str) -> str:
    """Comparison key for room numbers: '014' and '14' -> '14', '332 C' -> '332C', '628-11' -> '62811'."""
    key = re.sub(r"[^A-Z0-9]", "", room.upper())
    return re.sub(r"^0+(?=\d)", "", key)


# --------------------------------------------------------------------------- models


class Lexicon(BaseModel):
    """data/kb/entity_lexicon.yaml. Recognition vocabulary only, never a source of facts."""

    model_config = ConfigDict(extra="forbid")

    facilities: dict[str, list[str]]
    events: dict[str, list[str]]
    titles: list[str]
    unknown_name_titles: list[str]
    polite_suffixes: list[str]
    exact_case_codes: list[str]
    name_adjacent_words: list[str] = Field(
        default_factory=list,
        description="Ordinary words said right next to a person's name that are never part of one "
        "('Is Dr John inside', 'call Dr Nisha'): they don't make a partly matched name look longer.",
    )

    def honorifics(self) -> frozenset[str]:
        return frozenset(normalise(w) for w in [*self.titles, *self.polite_suffixes])


def load_lexicon(path: Path) -> Lexicon:
    with path.open(encoding="utf-8") as fh:
        return Lexicon.model_validate(yaml.safe_load(fh) or {})


class AliasSource(StrEnum):
    NAME = "name"  # full name, name without initials, first + last, ingest aliases
    NAME_PART = "name_part"  # one word of a name: "George" may belong to several people
    SHORT_CODE = "short_code"  # timetable code: exact case only, or next to an honorific
    CODE = "code"  # subject / programme code as written in the timetable
    GLOSSARY = "glossary"  # expansion or alias from glossary.yaml
    RESEARCH_AREA = "research_area"
    ACRONYM = "acronym"  # "(NLP)" in "Natural Language Processing (NLP)"
    ROOM = "room"


class Alias(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(description="As written in the source; normalised by the extractor at load time.")
    source: AliasSource
    verified: bool = Field(default=True, description="False when it comes from an unverified glossary entry.")
    exact_case: bool = Field(default=False, description="Match only as written, case-sensitive (codes that are words).")


class GazetteerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_id: str
    type: EntityType
    name: str = Field(description="What to show in 'Did you mean ...?': 'Dr Deepa V Jose', 'room 874', 'ML'.")
    aliases: list[Alias]
    faculty_ids: list[str] = Field(default_factory=list, description="Faculty this course/programme/topic/room relates to.")


class Gazetteer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts_digest: str = Field(description="Digest of the facts it was built with (= sha256 of facts.jsonl).")
    entries: list[GazetteerEntry]
    lexicon: Lexicon

    def entry(self, canonical_id: str) -> GazetteerEntry | None:
        return next((e for e in self.entries if e.canonical_id == canonical_id), None)


class GazetteerUnavailable(RuntimeError):
    """The gazetteer is missing or was built from other facts: run `just index`."""


# --------------------------------------------------------------------------- builders


def _dedupe_aliases(aliases: Iterable[Alias]) -> list[Alias]:
    """One alias per normalised form (per exact-case flag); the first, most trusted source wins."""
    seen: set[tuple[str, bool]] = set()
    out = []
    for alias in aliases:
        key = (alias.text if alias.exact_case else normalise(alias.text), alias.exact_case)
        if key[0] and key not in seen:
            seen.add(key)
            out.append(alias)
    return out


def _faculty_entries(kb: FacultyKB, honorifics: frozenset[str]) -> list[GazetteerEntry]:
    entries = []
    for fac in kb.faculty:
        words = [w for w in fac.full_name.split() if len(w.rstrip(".")) > 1]  # drop initials
        names = [fac.full_name, " ".join(words)]
        if len(words) > 2:
            names.append(f"{words[0]} {words[-1]}")  # "Rupali Wagh" for Rupali Sunil Wagh
        aliases = [Alias(text=n, source=AliasSource.NAME) for n in names]
        aliases += [
            Alias(text=a, source=AliasSource.NAME)
            for a in fac.aliases
            if a != fac.short_name and normalise(a) not in honorifics
        ]
        aliases += [Alias(text=w, source=AliasSource.NAME_PART) for w in words if normalise(w) not in honorifics]
        if fac.short_name:
            aliases.append(Alias(text=fac.short_name, source=AliasSource.SHORT_CODE, exact_case=True))
        entries.append(
            GazetteerEntry(
                canonical_id=f"faculty:{fac.id}",
                type=EntityType.FACULTY,
                name=fac.display_name,
                aliases=_dedupe_aliases(aliases),
                faculty_ids=[fac.id],
            )
        )
    return entries


def _glossary_aliases(entry: GlossaryEntry | None) -> list[Alias]:
    if entry is None:
        return []
    names = ([entry.full_name] if entry.is_expanded else []) + list(entry.aliases)
    return [Alias(text=n, source=AliasSource.GLOSSARY, verified=entry.verified) for n in names]


def _code_entries(
    facts: Sequence[Fact], category: FactCategory, type_: EntityType, lookup, code_of, lexicon: Lexicon
) -> list[GazetteerEntry]:
    exact_case = {c.casefold() for c in lexicon.exact_case_codes}
    entries = []
    for fact in (f for f in facts if f.category == category):
        code = code_of(fact)
        aliases = [Alias(text=code, source=AliasSource.CODE, exact_case=code.casefold() in exact_case)]
        entries.append(
            GazetteerEntry(
                canonical_id=fact.fact_id,
                type=type_,
                name=code,
                aliases=_dedupe_aliases(aliases + _glossary_aliases(lookup(code))),
                faculty_ids=[i for i in fact.faculty_id.split(",") if i],
            )
        )
    return entries


def _room_entries(facts: Sequence[Fact]) -> list[GazetteerEntry]:
    return [
        GazetteerEntry(
            canonical_id=f.fact_id,
            type=EntityType.ROOM,
            name=f"room {f.room}",
            aliases=[Alias(text=f.room, source=AliasSource.ROOM)],
            faculty_ids=[i for i in f.faculty_id.split(",") if i],
        )
        for f in facts
        if f.category == FactCategory.ROOM
    ]


_PAREN_ACRONYM = re.compile(r"^(?P<name>.*?)\s*\((?P<acronym>[^)]+)\)\s*$")
_PLURAL_ACRONYM = re.compile(r"^(?P<stem>[A-Z]{2,})s$")


def _topic_entries(kb: FacultyKB) -> list[GazetteerEntry]:
    """Research areas -> topics. 'Machine Learning (ML)' and 'machine learning' are one topic."""
    names: dict[str, str] = {}
    aliases: dict[str, list[Alias]] = defaultdict(list)
    faculty: dict[str, list[str]] = defaultdict(list)
    for fac in kb.faculty:
        for area in fac.research_areas:
            match = _PAREN_ACRONYM.match(area)
            name = (match["name"] if match else area).strip()
            slug = slugify(name)
            if not slug:
                continue
            names.setdefault(slug, name)
            forms = [Alias(text=name, source=AliasSource.RESEARCH_AREA)]
            if match:
                forms.append(Alias(text=match["acronym"].strip(), source=AliasSource.ACRONYM))
            for form in list(forms):
                if plural := _PLURAL_ACRONYM.match(form.text):  # "LLMs" -> also "LLM"
                    forms.append(Alias(text=plural["stem"], source=AliasSource.ACRONYM))
            aliases[slug] += forms
            if fac.id not in faculty[slug]:
                faculty[slug].append(fac.id)
    return [
        GazetteerEntry(
            canonical_id=f"topic:{slug}",
            type=EntityType.TOPIC,
            name=names[slug],
            aliases=_dedupe_aliases(aliases[slug]),
            faculty_ids=faculty[slug],
        )
        for slug in sorted(names)
    ]


def _campus_entries(facts: Sequence[Fact], lexicon: Lexicon) -> list[GazetteerEntry]:
    """Places and events from the campus sheet (kb/campus.py), plus one entry per kind that has data.

    A kind entry (``facility:washroom``) is what a lexicon word links to ("where is the washroom"), so
    only kinds with at least one verified place or event get one. ``event:event`` covers every event.
    """
    from rag_core.kb.campus import event_kind, kind_of

    entries: list[GazetteerEntry] = []
    kinds: dict[tuple[EntityType, str], None] = {}
    for f in facts:
        if f.category == FactCategory.FACILITY:
            type_, kind = EntityType.FACILITY, kind_of(f.fact_id)
        elif f.category == FactCategory.EVENT:
            type_, kind = EntityType.EVENT, event_kind(f)
        else:
            continue
        name, *aliases = [k for k in f.keywords if k]
        kind_word = (kind or "").replace("_", " ")
        entries.append(GazetteerEntry(
            canonical_id=f.fact_id,
            type=type_,
            name=name,
            aliases=_dedupe_aliases(Alias(text=a, source=AliasSource.NAME) for a in [name, *aliases] if a != kind_word),
        ))
        if kind:
            kinds[(type_, kind)] = None
            if type_ == EntityType.EVENT:
                kinds[(type_, "event")] = None
    for type_, kind in kinds:
        entries.append(GazetteerEntry(canonical_id=f"{type_.value}:{kind}", type=type_, name=kind.replace("_", " "), aliases=[]))
    # A classroom or lab number that is no faculty office ("room 806") becomes a room entity too, so
    # "where is room 806" links; the catalog maps it to the place's fact.
    offices = {room_key(f.room) for f in facts if f.category == FactCategory.ROOM}
    rooms: dict[str, str] = {}
    for f in facts:
        if f.category == FactCategory.FACILITY and f.room and room_key(f.room) not in offices:
            rooms.setdefault(f"room:{slugify(f.room)}", f.room)
    entries += [
        GazetteerEntry(canonical_id=cid, type=EntityType.ROOM, name=f"room {room}", aliases=[Alias(text=room, source=AliasSource.ROOM)])
        for cid, room in rooms.items()
    ]
    return entries


def build_gazetteer(kb: FacultyKB, glossary: Glossary, lexicon: Lexicon, facts: Sequence[Fact]) -> Gazetteer:
    """Deterministic: the same KB, glossary, lexicon and facts give the same gazetteer."""
    entries = [
        *_faculty_entries(kb, lexicon.honorifics()),
        *_code_entries(facts, FactCategory.COURSE, EntityType.COURSE, glossary.subject, lambda f: f.subject_code, lexicon),
        *_code_entries(facts, FactCategory.PROGRAMME, EntityType.PROGRAMME, glossary.programme, lambda f: f.programme, lexicon),
        *_room_entries(facts),
        *_topic_entries(kb),
        *_campus_entries(facts, lexicon),
    ]
    ids = [e.canonical_id for e in entries]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"duplicate canonical ids in the gazetteer: {dupes}")
    return Gazetteer(facts_digest=facts_digest(facts), entries=entries, lexicon=lexicon)


# --------------------------------------------------------------------------- persistence


def save_gazetteer(gazetteer: Gazetteer, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(gazetteer.model_dump_json(indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_gazetteer(path: Path, facts_jsonl: Path | None = None) -> Gazetteer:
    """Load and, when ``facts_jsonl`` exists, check it was built from those facts."""
    if not path.exists():
        raise GazetteerUnavailable(f"{path} not found: run `just index`")
    gazetteer = Gazetteer.model_validate_json(path.read_text(encoding="utf-8"))
    if facts_jsonl is not None and facts_jsonl.exists():
        if hashlib.sha256(facts_jsonl.read_bytes()).hexdigest() != gazetteer.facts_digest:
            raise GazetteerUnavailable(f"{path} was built from different facts than {facts_jsonl}: run `just index`")
    return gazetteer
