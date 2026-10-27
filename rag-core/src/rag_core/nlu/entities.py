"""Entity extraction and linking: question text -> typed mentions linked to KB ids.

The vocabulary is closed and small, so there is no NER model: a gazetteer built at index time
(:mod:`rag_core.nlu.gazetteer`), patterns for rooms, days and times, and rapidfuzz for typos and
ASR splits. Each stage only sees words that earlier stages left unclaimed:

1. **Patterns** on the raw text: times ("now", "at 2", "after lunch", "between 2 and 4"), days
   ("today", "Friday"), rooms ("room 874", "8 7 4", or a bare number that is a known room).
   "today"/"now"/"tomorrow" are resolved with an injectable clock (or ``now=`` per call).
2. **Codes**: faculty short codes ("SU", "DVJ") collide with words, so they match only as written
   (case-sensitive, and not in all-caps input) or next to a title/"sir"/"mam". A short code that
   is also a course code (RM) needs the honorific. Course codes that are everyday words (lexicon
   ``exact_case_codes``, e.g. SE, CV) match only as written.
3. **Gazetteer + lexicon phrases**, longest span first: exact, normalised and space-insensitive
   ("sri devi" -> "sridevi", "m l" -> "ml") matches, then rapidfuzz ``token_set_ratio`` >=
   ``thresholds.entity_match_min`` for typos. Fuzzy compares only spans and aliases with the same
   number of words, because token_set_ratio scores any subset as 100 ("learning" vs "deep
   learning"); a word-count mismatch is allowed only as a space-insensitive ratio for ASR splits.
4. "Dr <name not in the KB>" -> an unlinked faculty mention, so the answer can say so.

Linking never guesses between near-equal candidates: every candidate within
``entities.ambiguity_margin`` of the best is returned and the mention is flagged ``ambiguous``
(``canonical_id`` None in the contract :class:`Entity`), so the caller can ask "Did you mean Dr X
or Dr Y?". Context ``prior_entities`` may break the tie only when exactly one candidate is in it.
Facility and event mentions stay unlinked until verified data for them exists.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from rag_core.config import EntitySettings, Settings
from rag_core.contracts import Entity, EntityType, Probability
from rag_core.kb.models import HHMM, WEEKDAYS, Weekday
from rag_core.kb.parsing import DayWindow, resolve_clock, resolve_range
from rag_core.nlu.gazetteer import (
    AliasSource,
    Gazetteer,
    Token,
    compact,
    load_gazetteer,
    normalise,
    room_key,
    tokenize,
)

Clock = Callable[[], datetime]
_Date = date  # TimeRef has a field called "date"
# "context": not in the question; filled by the pipeline from the dialogue module's last_entities.
# "partial_name": part of a name matched a faculty member but the rest of the name didn't (unlinked).
Method = Literal["exact", "normalised", "fuzzy", "short_code", "pattern", "lexicon", "title", "context", "partial_name"]

# Words that never start or end a fuzzy match or form an unknown name ("Dr please").
_STOPWORDS = frozenset(
    """a an the is are was were be been am of to in on at for and or with by from as it its this that these
    those what where who whom whose when which how why does do did can could would will shall should may might
    i me my mine you your he him his she her they them their we us our there here please tell show give get
    find meet want need know see go come looking look like about any some all no not yes ok okay thanks thank
    hello hi hey good sorry excuse room rooms cabin office number id email mail phone contact teach teaches
    teaching teacher teachers take takes taking handle handles handling works work working faculty professor
    class classes timetable schedule free available consultation hours hour time research expert subject
    subjects course courses programme program department floor block lab staff way name details sitting sit
    sits share""".split()
)
_TEACH_WORDS = frozenset("teach teaches teaching taught take takes taking handle handles handling subject course paper".split())
# Dropped for a second exact lookup, so "MSc in AI and ML" finds the alias "MSc AI and ML".
_FUNCTION_WORDS = frozenset("a an the of in and for on with to".split())
_FUZZY_TYPES = frozenset({EntityType.FACULTY, EntityType.COURSE, EntityType.PROGRAMME, EntityType.TOPIC})
_TYPE_ORDER = {t: n for n, t in enumerate(EntityType)}

# --------------------------------------------------------------------------- output models


class TimeRef(BaseModel):
    """Resolved DAY or TIME. ``start == end`` is a point in time."""

    day: Weekday | None = None
    date: _Date | None = None
    start: HHMM | None = None
    end: HHMM | None = None
    approximate: bool = Field(
        default=False, description="A named period from settings ('morning', 'after lunch'), not a stated time."
    )


class Candidate(BaseModel):
    canonical_id: str
    name: str = Field(description="For 'Did you mean ...?': 'Dr Kavitha R', 'room 874', 'ML'.")
    score: Probability
    alias: str = Field(description="The alias that matched, as written in its source.")
    verified: bool = Field(default=True, description="False when the link relies on an unverified glossary entry.")


class EntityMention(BaseModel):
    """One recognised mention. Richer than the contract Entity; convert with :meth:`to_entity`."""

    type: EntityType
    text: str = Field(description="Surface form exactly as in the question.")
    start: int
    end: int
    method: Method
    score: Probability
    candidates: list[Candidate] = Field(default_factory=list, description="Best first; empty when unlinked.")
    ambiguous: bool = Field(default=False, description="Several candidates are equally good: ask, don't pick.")
    resolved_by_context: bool = Field(default=False, description="An ambiguity broken by context prior_entities.")
    kind: str | None = Field(default=None, description="Facility/event kind from the lexicon, e.g. 'washroom'.")
    time: TimeRef | None = None
    near_misses: list[Candidate] = Field(
        default_factory=list,
        description="Faculty whose name matched only part of this mention ('John' in 'Dr John Mathew'). "
        "Never linked: only offered as 'did you mean ...?'.",
    )
    needs_confirmation: bool = Field(
        default=False,
        description="The extra word may not be a name (all-lowercase text gives no case signal): ask the visitor "
        "to confirm the near miss instead of declining.",
    )

    @property
    def canonical_id(self) -> str | None:
        return self.candidates[0].canonical_id if self.candidates and not self.ambiguous else None

    def to_entity(self) -> Entity:
        return Entity(type=self.type, text=self.text, canonical_id=self.canonical_id, score=self.score)


class EntityExtraction(BaseModel):
    text: str
    mentions: list[EntityMention]

    @property
    def entities(self) -> list[Entity]:
        return [m.to_entity() for m in self.mentions]

    @property
    def ambiguous(self) -> list[EntityMention]:
        return [m for m in self.mentions if m.ambiguous]

    def of_type(self, type_: EntityType) -> list[EntityMention]:
        return [m for m in self.mentions if m.type == type_]


# --------------------------------------------------------------------------- time patterns

# Group names match rag_core.kb.parsing.resolve_range. The final lookahead rejects "2nd", "3rd".
_CLOCK = (
    r"(?<![\d.:])(?P<{p}h>\d{{1,2}})(?:(?P<{p}sep>[.:])(?P<{p}m>\d{{2}}))?(?!\d)"
    r"(?:\s*(?P<{p}ap>[ap])\.?m\b\.?)?(?P<{p}oc>\s*o['’]?\s?clock\b)?(?![A-Za-z])"
)
_RANGE = re.compile(
    rf"(?:\b(?P<cue>from|between)\s+)?{_CLOCK.format(p='s')}\s*(?P<sep>(?:to|till|until|and)\b|-|–)\s*{_CLOCK.format(p='e')}",
    re.IGNORECASE,
)
_POINT = re.compile(rf"(?:\b(?P<cue>at|around|by|after|before|till|until|from|since)\s+)?{_CLOCK.format(p='s')}", re.IGNORECASE)
_LUNCH = re.compile(
    r"\b(?:(?P<rel>after|before)\s+(?:the\s+)?lunch(?:\s+(?:break|time|hour))?"
    r"|(?:(?:during|at|in)\s+(?:the\s+)?)?lunch\s*(?:break|time|hour)|(?:during|at)\s+(?:the\s+)?lunch)\b",
    re.IGNORECASE,
)
_PERIOD = re.compile(
    r"\b(?:(?:this|in\s+the|during\s+the)\s+)?(?<!good\s)(?P<p>morning|forenoon|afternoon|evening|noon|midday)s?\b",
    re.IGNORECASE,
)
_PERIOD_NAMES = {"morning": "morning", "forenoon": "morning", "afternoon": "afternoon", "evening": "evening",
                 "noon": "noon", "midday": "noon"}
_NOW = re.compile(r"\b(?:right\s+now|as\s+of\s+now|at\s+the\s+moment|at\s+this\s+moment|at\s+present|now)\b", re.IGNORECASE)
_STARTS = {"after", "from", "since"}
_ENDS = {"before", "till", "until"}

_DAY_RELATIVE = re.compile(r"\b(?P<rel>day\s+after\s+tomorrow|tomorrow|tmrw|tmr|today|yesterday)(?:['’]?s)?\b", re.IGNORECASE)
_DAY_NAME = re.compile(r"\b(?P<d>" + "|".join(WEEKDAYS) + r")(?:['’]?s)?\b", re.IGNORECASE)
# Abbreviations that are also words (Sat, Sun, Wed, Mon) count only when capitalised.
_DAY_ABBR = re.compile(r"\b(?:(?P<cap>Mon|Tues?|Wed|Thu(?:rs?)?|Fri|Sat|Sun)|(?i:(?P<low>tues?|thu(?:rs?)?|fri)))\b\.?")
_RELATIVE_OFFSET = {"today": 0, "tomorrow": 1, "tmrw": 1, "tmr": 1, "yesterday": -1}

_ROOM_CUES = frozenset({"room", "rooms", "rm", "cabin"})
_ROOM_FILLERS = frozenset({"no", "number", "num", "nr"})
_DIGIT_WORDS = {w: str(n) for n, w in enumerate("zero one two three four five six seven eight nine".split())}
_ROOM_PIECE = re.compile(r"^\d+[a-z]?$")
_BARE_ROOM = re.compile(r"^\d{3,4}[a-z]?$")


def _hhmm(t: time | datetime) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


# --------------------------------------------------------------------------- internals


@dataclass(frozen=True)
class _Hit:
    type: EntityType
    canonical_id: str | None  # None for lexicon words: no KB data for them yet
    name: str
    alias: str
    verified: bool = True
    kind: str | None = None


@dataclass
class _Found:
    mention: EntityMention
    first: int | None  # token range, None when the mention covers no token
    last: int | None


@dataclass
class _State:
    text: str
    tokens: list[Token]
    consumed: set[int] = field(default_factory=set)
    found: list[_Found] = field(default_factory=list)
    claimed: list[tuple[int, int]] = field(default_factory=list)  # character spans taken by patterns

    def token_range(self, start: int, end: int) -> list[int]:
        return [i for i, t in enumerate(self.tokens) if t.start < end and t.end > start]

    def free(self, start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in self.claimed) and not (
            set(self.token_range(start, end)) & self.consumed
        )

    def add(self, mention: EntityMention, idxs: Sequence[int], *, consume: Iterable[int] | None = None) -> None:
        self.consumed.update(idxs if consume is None else consume)
        self.found.append(_Found(mention, min(idxs) if idxs else None, max(idxs) if idxs else None))


@dataclass
class _SpanMatch:
    pos: int  # index into the content-token list
    length: int
    exact: bool
    hits: list[tuple[_Hit, float]]  # (hit, 0-100 score)

    @property
    def best(self) -> float:
        return max(score for _, score in self.hits)


# --------------------------------------------------------------------------- extractor


class EntityExtractor:
    def __init__(
        self,
        gazetteer: Gazetteer,
        cfg: EntitySettings,
        *,
        match_min: float,
        unverified_weight: float,
        day_start: time,
        day_end: time,
        clock: Clock | None = None,
    ):
        self._cfg = cfg
        self._match_min = match_min
        self._unverified_weight = unverified_weight
        self._window = DayWindow.from_times(day_start, day_end)
        self._day_start, self._day_end = _hhmm(day_start), _hhmm(day_end)
        self._clock = clock or datetime.now

        lexicon = gazetteer.lexicon
        self._honorifics = lexicon.honorifics()
        self._titles = frozenset(normalise(t) for t in lexicon.titles)
        self._suffixes = frozenset(normalise(s) for s in lexicon.polite_suffixes)
        self._unknown_titles = frozenset(normalise(t) for t in lexicon.unknown_name_titles)
        self._adjacent = frozenset(normalise(w) for w in lexicon.name_adjacent_words)
        self._faculty_names = [
            (e.canonical_id, e.name, [w for w in normalise(e.name).split() if len(w) > 1 and w not in self._honorifics])
            for e in gazetteer.entries if e.type == EntityType.FACULTY
        ]

        self._exact: dict[str, list[_Hit]] = defaultdict(list)
        self._compact: dict[str, list[_Hit]] = defaultdict(list)
        self._stripped: dict[str, list[_Hit]] = defaultdict(list)  # function words removed
        self._fuzzy: list[tuple[str, str, int, _Hit]] = []  # (comparable form, no spaces, word count, hit)
        self._rooms: dict[str, list[_Hit]] = defaultdict(list)
        self._case_codes: list[tuple[re.Pattern[str], _Hit]] = []
        short_codes: list[tuple[str, _Hit]] = []

        for entry in gazetteer.entries:
            for alias in entry.aliases:
                hit = _Hit(entry.type, entry.canonical_id, entry.name, alias.text, alias.verified)
                if alias.source == AliasSource.ROOM:
                    self._rooms[room_key(alias.text)].append(hit)
                elif alias.source == AliasSource.SHORT_CODE:
                    short_codes.append((alias.text, hit))
                elif alias.exact_case:
                    self._case_codes.append((_token_pattern(alias.text, re.NOFLAG), hit))
                else:
                    self._index(alias.text, hit)

        # A kind with verified campus data has a kind entry ("facility:washroom"): its words link to it.
        # Kinds without data stay unlinked, so those questions still escalate. A word that is also the
        # name of one specific place ("computer lab") means that place, not the whole kind.
        known = {e.canonical_id for e in gazetteer.entries}
        for type_, kinds in ((EntityType.FACILITY, lexicon.facilities), (EntityType.EVENT, lexicon.events)):
            for kind, phrases in kinds.items():
                kind_id = f"{type_.value}:{kind}" if f"{type_.value}:{kind}" in known else None
                for phrase in phrases:
                    if kind_id and any(h.type == type_ and h.canonical_id for h in self._exact.get(self._content_norm(phrase), [])):
                        continue
                    name = kind.replace("_", " ")
                    self._index(phrase, _Hit(type_, kind_id, name, phrase, kind=kind), fuzzy=False)

        # A short code that is also another gazetteer word (course code RM) needs an honorific to mean a person.
        code_words = set(self._exact) | {h.alias.casefold() for _, h in self._case_codes}
        self._short_codes = [
            (_token_pattern(code, re.IGNORECASE), code, hit, code.casefold() in code_words)
            for code, hit in short_codes
        ]

    @classmethod
    def from_settings(
        cls, settings: Settings, gazetteer: Gazetteer | None = None, clock: Clock | None = None
    ) -> EntityExtractor:
        if gazetteer is None:
            gazetteer = load_gazetteer(settings.paths.gazetteer, settings.paths.facts_jsonl)
        return cls(
            gazetteer,
            settings.entities,
            match_min=settings.thresholds.entity_match_min,
            unverified_weight=settings.thresholds.unverified_glossary_weight,
            day_start=settings.kb.day_start,
            day_end=settings.kb.day_end,
            clock=clock,
        )

    def _content_norm(self, text: str) -> str:
        return " ".join(t.norm for t in tokenize(text) if t.norm not in self._honorifics)

    def _index(self, text: str, hit: _Hit, *, fuzzy: bool = True) -> None:
        norm = self._content_norm(text)
        if not norm:
            return
        self._exact[norm].append(hit)
        self._compact[compact(norm)].append(hit)
        if stripped := _strip_function_words(norm):
            self._stripped[stripped].append(hit)
        if fuzzy and hit.type in _FUZZY_TYPES:
            words = norm.split()
            if hit.type == EntityType.FACULTY:
                words = [w for w in words if len(w) > 1] or words  # initials don't help fuzzy matching
            form = " ".join(words)
            if len(compact(form)) >= self._cfg.fuzzy_min_chars:
                self._fuzzy.append((form, compact(form), len(words), hit))

    # ----------------------------------------------------------------------- entry point

    def extract(
        self, text: str, *, now: datetime | str | None = None, prior_entities: Iterable[str] = ()
    ) -> EntityExtraction:
        """``now``: ISO-8601 string or datetime (context ``now``); default: the clock."""
        if now is None:
            now = self._clock()
        elif isinstance(now, str):
            now = datetime.fromisoformat(now)
        state = _State(text, tokenize(text))
        self._times(state, now)
        self._days(state, now)
        self._room_mentions(state)
        self._codes(state)
        self._spans(state)
        self._reject_partial_names(state)
        self._unknown_names(state)
        self._drop_lab_after_course(state)

        prior = set(prior_entities)
        mentions = [self._tie_break(f.mention, prior) for f in state.found]
        mentions.sort(key=lambda m: (m.start, m.end, _TYPE_ORDER[m.type]))
        return EntityExtraction(text=text, mentions=mentions)

    # ----------------------------------------------------------------------- 1. patterns

    def _pattern_mention(
        self, state: _State, match: re.Match[str], type_: EntityType, ref: TimeRef | None, cid: str | None, name: str
    ) -> None:
        start, end = match.span()
        candidates = [Candidate(canonical_id=cid, name=name, score=1.0, alias=match.group())] if cid else []
        mention = EntityMention(
            type=type_, text=match.group(), start=start, end=end, method="pattern", score=1.0,
            candidates=candidates, time=ref,
        )
        state.claimed.append((start, end))
        state.add(mention, state.token_range(start, end))

    def _time_mention(self, state: _State, match: re.Match[str], ref: TimeRef | None) -> None:
        if ref is None:
            self._pattern_mention(state, match, EntityType.TIME, None, None, "")
            return
        value = ref.start if ref.start == ref.end else f"{ref.start}-{ref.end}"
        self._pattern_mention(state, match, EntityType.TIME, ref, f"time:{value}", value)

    def _span_ref(self, start: str | None, end: str | None, *, approximate: bool = False) -> TimeRef | None:
        if start is None or end is None or start > end:
            return None
        return TimeRef(start=start, end=end, approximate=approximate)

    def _times(self, state: _State, now: datetime) -> None:
        periods = self._cfg.periods
        for m in _RANGE.finditer(state.text):
            sep, cue = m["sep"].lower(), (m["cue"] or "").lower()
            if (sep == "and" and cue != "between") or (sep in "-–" and not (cue or m["sap"] or m["eap"])):
                continue
            if state.free(*m.span()):
                times = resolve_range(m, self._window)
                self._time_mention(state, m, self._span_ref(*times) if times else None)

        lunch = periods.lunch
        for m in _LUNCH.finditer(state.text):
            if not state.free(*m.span()):
                continue
            rel = (m["rel"] or "").lower()
            span = {
                "after": (_hhmm(lunch.end), self._day_end),
                "before": (self._day_start, _hhmm(lunch.start)),
            }.get(rel, (_hhmm(lunch.start), _hhmm(lunch.end)))
            self._time_mention(state, m, self._span_ref(*span, approximate=True))

        for m in _POINT.finditer(state.text):
            cue = (m["cue"] or "").lower()
            if not (cue or m["sap"] or m["soc"] or m["ssep"] == ":") or not state.free(*m.span()):
                continue
            t = resolve_clock(m["sh"], m["sm"], m["sap"], self._window)
            if t is None:
                ref = None
            elif cue in _STARTS:
                ref = self._span_ref(t, self._day_end)
            elif cue in _ENDS:
                ref = self._span_ref(self._day_start, t)
            else:
                ref = TimeRef(start=t, end=t)
            self._time_mention(state, m, ref)

        for m in _PERIOD.finditer(state.text):
            if state.free(*m.span()):
                period = getattr(periods, _PERIOD_NAMES[m["p"].lower()])
                self._time_mention(state, m, self._span_ref(_hhmm(period.start), _hhmm(period.end), approximate=True))

        for m in _NOW.finditer(state.text):
            if state.free(*m.span()):
                t = _hhmm(now)
                ref = TimeRef(day=WEEKDAYS[now.weekday()], date=now.date(), start=t, end=t)
                self._time_mention(state, m, ref)

    def _days(self, state: _State, now: datetime) -> None:
        today = now.date()

        def emit(m: re.Match[str], day: date) -> None:
            if state.free(*m.span()):
                name = WEEKDAYS[day.weekday()]
                self._pattern_mention(state, m, EntityType.DAY, TimeRef(day=name, date=day), f"day:{name.lower()}", name)

        def upcoming(weekday: str) -> date:
            target = [d.lower() for d in WEEKDAYS].index(weekday.lower())
            return today + timedelta(days=(target - today.weekday()) % 7)

        for m in _DAY_RELATIVE.finditer(state.text):
            rel = " ".join(m["rel"].lower().split())
            emit(m, today + timedelta(days=2 if rel == "day after tomorrow" else _RELATIVE_OFFSET[rel]))
        for m in _DAY_NAME.finditer(state.text):
            emit(m, upcoming(m["d"]))
        for m in _DAY_ABBR.finditer(state.text):
            abbr = (m["cap"] or m["low"]).lower()[:3]
            emit(m, upcoming(next(d for d in WEEKDAYS if d.lower().startswith(abbr))))

    def _room_pieces(self, state: _State, i: int) -> list[int]:
        pieces: list[int] = []
        while i < len(state.tokens) and len(pieces) < 5 and i not in state.consumed:
            norm = state.tokens[i].norm
            if _ROOM_PIECE.match(norm) or norm in _DIGIT_WORDS or (pieces and len(norm) == 1 and norm.isalpha()):
                pieces.append(i)
                i += 1
            else:
                break
        return pieces

    def _known_room(self, state: _State, pieces: list[int], min_len: int = 1) -> tuple[list[int], list[_Hit]] | None:
        for n in range(len(pieces), min_len - 1, -1):
            key = room_key("".join(_DIGIT_WORDS.get(state.tokens[i].norm, state.tokens[i].norm) for i in pieces[:n]))
            if hits := self._rooms.get(key):
                return pieces[:n], hits
        return None

    def _add_room(self, state: _State, first: int, used: list[int], hits: list[_Hit]) -> None:
        start, end = state.tokens[first].start, state.tokens[used[-1]].end
        candidates = [Candidate(canonical_id=h.canonical_id, name=h.name, score=1.0, alias=h.alias) for h in hits]
        mention = EntityMention(
            type=EntityType.ROOM, text=state.text[start:end], start=start, end=end, method="pattern",
            score=1.0, candidates=candidates, ambiguous=len(candidates) > 1,
        )
        state.add(mention, list(range(first, used[-1] + 1)), consume=used)

    def _room_mentions(self, state: _State) -> None:
        tokens = state.tokens
        for i, tok in enumerate(tokens):  # "room 874", "cabin no 871", "room 8 7 9", "room 999" (unlinked)
            if tok.norm not in _ROOM_CUES or i in state.consumed:
                continue
            j = i + 1
            while j < len(tokens) and tokens[j].norm in _ROOM_FILLERS:
                j += 1
            pieces = self._room_pieces(state, j)
            if not pieces or not (tokens[pieces[0]].norm[0].isdigit() or tokens[pieces[0]].norm in _DIGIT_WORDS):
                continue
            if known := self._known_room(state, pieces):
                self._add_room(state, i, *known)
            else:
                singles = [p for p in pieces if len(_DIGIT_WORDS.get(tokens[p].norm, tokens[p].norm)) == 1]
                used = singles if len(singles) > 1 and singles[0] == pieces[0] else pieces[:1]
                self._add_room(state, i, used, [])
        for i, tok in enumerate(tokens):  # bare: "how to go to 885", "8 7 4"
            if i in state.consumed:
                continue
            if _BARE_ROOM.match(tok.norm):
                known = self._known_room(state, self._room_pieces(state, i))
            elif tok.norm.isdigit() and len(tok.norm) == 1 or tok.norm in _DIGIT_WORDS:
                known = self._known_room(state, self._room_pieces(state, i), min_len=3)
            else:
                continue
            if known:
                self._add_room(state, i, *known)

    # ----------------------------------------------------------------------- 2. codes

    def _codes(self, state: _State) -> None:
        tokens, all_caps = state.tokens, not any(c.islower() for c in state.text)
        for pattern, code, hit, collides in self._short_codes:
            for m in pattern.finditer(state.text):
                idxs = state.token_range(*m.span())
                if not idxs or set(idxs) & state.consumed:
                    continue
                before = tokens[idxs[0] - 1].norm if idxs[0] > 0 else ""
                after = tokens[idxs[-1] + 1].norm if idxs[-1] + 1 < len(tokens) else ""
                # "Dr as" is a person, "same as sir said" is not: a code that is a common word needs the title.
                honorific = before in self._titles or (after in self._suffixes and code.casefold() not in _STOPWORDS)
                as_written = m.group() == code and not all_caps and not collides
                if honorific or as_written:
                    self._linked(state, idxs, [(hit, 100.0)], "short_code", self._cfg.short_code_score)
        for pattern, hit in self._case_codes:
            for m in pattern.finditer(state.text):
                idxs = state.token_range(*m.span())
                if idxs and not set(idxs) & state.consumed:
                    self._linked(state, idxs, [(hit, 100.0)], "exact")
        # An exact-case code also counts in lower case straight after a teaching word ("who teaches rm"):
        # there it can't be the word ("se") or the room cue ("rm 874").
        for pattern, hit in self._case_codes:
            for m in re.finditer(pattern.pattern, state.text, re.IGNORECASE):
                idxs = state.token_range(*m.span())
                if not idxs or set(idxs) & state.consumed or idxs[0] == 0:
                    continue
                if tokens[idxs[0] - 1].norm in _TEACH_WORDS and (idxs[-1] + 1 >= len(tokens) or not tokens[idxs[-1] + 1].norm.isdigit()):
                    self._linked(state, idxs, [(hit, 100.0)], "exact")

    # ----------------------------------------------------------------------- 3. gazetteer spans

    def _spans(self, state: _State) -> None:
        tokens = state.tokens
        content = [i for i, t in enumerate(tokens) if t.norm not in self._honorifics]
        matches: list[_SpanMatch] = []
        for pos in range(len(content)):
            for length in range(1, self._cfg.max_span_tokens + 1):
                idxs = content[pos : pos + length]
                if len(idxs) < length or set(idxs) & state.consumed:
                    break
                norm = " ".join(tokens[i].norm for i in idxs)
                hits = self._exact.get(norm)
                if not hits and length > 1:
                    stripped = _strip_function_words(norm)
                    hits = self._compact.get(compact(norm)) or (self._stripped.get(stripped) if stripped else None)
                if hits:
                    matches.append(_SpanMatch(pos, length, True, [(h, 100.0) for h in hits]))
                elif fuzzy := self._fuzzy_hits(tokens, idxs, norm):
                    matches.append(_SpanMatch(pos, length, False, fuzzy))

        taken: set[int] = set()
        for match in sorted(matches, key=lambda m: (-m.length, not m.exact, -m.best, m.pos)):
            positions = set(range(match.pos, match.pos + match.length))
            if positions & taken:
                continue
            taken |= positions
            idxs = content[match.pos : match.pos + match.length]
            raw = " ".join(state.text[tokens[idxs[0]].start : tokens[idxs[-1]].end].split())
            linked: dict[EntityType, list[tuple[_Hit, float]]] = defaultdict(list)
            for hit, score in match.hits:
                if hit.canonical_id is None:
                    self._lexicon_mention(state, idxs, hit)
                else:
                    linked[hit.type].append((hit, score))
            for hits in linked.values():
                method = "fuzzy" if not match.exact else ("exact" if any(h.alias == raw for h, _ in hits) else "normalised")
                self._linked(state, idxs, hits, method)

    def _fuzzy_hits(self, tokens: list[Token], idxs: list[int], norm: str) -> list[tuple[_Hit, float]]:
        words = norm.split()
        if (
            words[0] in _STOPWORDS
            or words[-1] in _STOPWORDS
            or any(ch.isdigit() for ch in norm)
            or len(compact(norm)) < self._cfg.fuzzy_min_chars
        ):
            return []
        # More words than the alias is an ASR split ("thiru navukkarasu") only if no word matches on its
        # own. Fewer words is a dropped word ("development" vs "API Development"): never a match.
        allow_split = not any(tokens[i].norm in self._exact for i in idxs)
        name_words = [w for w in words if len(w) > 1] or words
        best: dict[str, tuple[_Hit, float]] = {}
        for form, form_compact, n_words, hit in self._fuzzy:
            span = name_words if hit.type == EntityType.FACULTY else words
            score = 0.0
            if len(span) == n_words:
                score = fuzz.token_set_ratio(" ".join(span), form)
                if score >= self._match_min and not self._words_align(span, form.split()):
                    score = 0.0
            elif allow_split and len(span) > n_words:
                score = fuzz.ratio("".join(span), form_compact)
            if score >= self._match_min and score > best.get(hit.canonical_id, (hit, -1.0))[1]:
                best[hit.canonical_id] = (hit, score)
        return list(best.values())

    def _words_align(self, span: Sequence[str], alias: Sequence[str]) -> bool:
        """Every content word on each side has a close counterpart on the other.

        token_set_ratio rewards the shared words: "c programming" vs "programming fundamentals"
        scores 92 because "c" is short. This rejects that, while keeping "neha singh" ~ "neha singhal".
        """
        floor = self._cfg.fuzzy_word_min

        def covered(words: Sequence[str], others: Sequence[str]) -> bool:
            return all(max(fuzz.ratio(w, o) for o in others) >= floor for w in words if w not in _STOPWORDS)

        return covered(span, alias) and covered(alias, span)

    def _lexicon_mention(self, state: _State, idxs: list[int], hit: _Hit) -> None:
        start, end = state.tokens[idxs[0]].start, state.tokens[idxs[-1]].end
        mention = EntityMention(
            type=hit.type, text=state.text[start:end], start=start, end=end, method="lexicon", score=1.0, kind=hit.kind
        )
        state.add(mention, idxs)

    def _linked(
        self, state: _State, idxs: list[int], hits: list[tuple[_Hit, float]], method: Method, cap: float = 1.0
    ) -> None:
        """One mention per type: every candidate within the ambiguity margin of the best is kept."""
        by_id: dict[str, tuple[_Hit, float]] = {}
        for hit, score in hits:
            if score > by_id.get(hit.canonical_id, (hit, -1.0))[1]:
                by_id[hit.canonical_id] = (hit, score)
        top = max(score for _, score in by_id.values())
        kept = sorted(
            (pair for pair in by_id.values() if pair[1] >= top - self._cfg.ambiguity_margin),
            key=lambda pair: (-pair[1], pair[0].canonical_id),
        )
        candidates = [
            Candidate(
                canonical_id=hit.canonical_id,
                name=hit.name,
                # Glossary trust rule: a link through an unverified expansion is weighted down.
                score=round(min(cap, score / 100) * (1.0 if hit.verified else self._unverified_weight), 4),
                alias=hit.alias,
                verified=hit.verified,
            )
            for hit, score in kept
        ]
        type_ = kept[0][0].type
        first, last = idxs[0], idxs[-1]
        if type_ == EntityType.FACULTY:  # keep "Dr" / "ma'am" in the surface text
            if first > 0 and state.tokens[first - 1].norm in self._titles:
                first -= 1
            if last + 1 < len(state.tokens) and state.tokens[last + 1].norm in self._suffixes:
                last += 1
        start, end = state.tokens[first].start, state.tokens[last].end
        mention = EntityMention(
            type=type_, text=state.text[start:end], start=start, end=end, method=method,
            score=max(c.score for c in candidates), candidates=candidates, ambiguous=len(candidates) > 1,
            kind=kept[0][0].kind,
        )
        state.add(mention, list(range(first, last + 1)), consume=idxs)

    # ----------------------------------------------------------------------- 3b. name completeness

    def _reject_partial_names(self, state: _State) -> None:
        """Unlink a faculty match when the visitor's name goes on with words that aren't that person's.

        A first or last name alone is an alias ("John" -> Dr Tegil J John), so "Dr John Mathew" used
        to link Dr Tegil J John. When the words right next to the match look like more name and fit no
        word of any candidate's name, the visitor named someone else: the mention becomes unlinked
        (method ``partial_name``) and the candidates are kept only as ``near_misses``.

        What counts as "more name": any word beside the match that isn't a stopword, an honorific
        ("sir", "ma'am"), an ordinary word from the lexicon's ``name_adjacent_words`` ("inside", "left",
        "call"), or already claimed by another entity. Single-letter initials are skipped.
        - A Title-case word in mixed-case text ("Dr John Mathew") is a strong sign of another person:
          the answer declines and offers the near miss.
        - Otherwise (all-lowercase speech-to-text such as "where is john mathew", with or without a title,
          or a lowercase word in mixed text) case says nothing, so the answer asks "do you mean ...?"
          (``needs_confirmation``) and gives no facts until the visitor confirms.
        """
        text = state.text
        case_informative = any(c.isupper() for c in text) and any(c.islower() for c in text)
        for found in list(state.found):
            m = found.mention
            if m.type != EntityType.FACULTY or not m.candidates or m.method == "short_code" or found.first is None:
                continue
            name_words = {w for c in m.candidates for w in normalise(c.name).split() if w not in self._honorifics}
            if self._names_complete(state, found, m):
                continue  # the whole name was said ("Dr Deepa V Jose list ..."): nothing can be missing
            after = self._extra_name_words(state, found.last, +1, name_words, case_informative)
            # A word before the title belongs to the sentence, not the name: "Kindly Dr Nisha".
            titled = state.tokens[found.first].norm in self._titles
            before = [] if titled else self._extra_name_words(state, found.first, -1, name_words, case_informative)
            extra = after + before
            if not extra:
                continue
            first, last = min([found.first, *extra]), max([found.last, *extra])
            if first > 0 and state.tokens[first - 1].norm in self._titles:
                first -= 1  # "Dr Mathew John": the title belongs to the whole name
            strong = all(self._title_case(state.tokens[i].raw) for i in extra) and case_informative
            start, end = state.tokens[first].start, state.tokens[last].end
            mention = EntityMention(
                type=EntityType.FACULTY, text=text[start:end], start=start, end=end, method="partial_name",
                score=self._cfg.unknown_name_score, near_misses=list(m.candidates), needs_confirmation=not strong,
            )
            state.found.remove(found)
            state.add(mention, list(range(first, last + 1)))

    def _names_complete(self, state: _State, found: _Found, m: EntityMention) -> bool:
        """Every candidate's full name, initials included, was said, so the match isn't partial.

        Initials count: "Dr Sridevi" is only part of "Dr Sridevi R", so "dr sridevi krishnan" is still
        checked, while the full names in our own answers ("Dr Deepa V Jose list ...") are left alone.
        """
        said = [t.norm for t in state.tokens[found.first : found.last + 1] if t.norm not in self._honorifics]
        for c in m.candidates:
            for w in (w for w in normalise(c.name).split() if w not in self._honorifics):
                if len(w) == 1:
                    if w not in said:
                        return False
                elif not any(fuzz.ratio(w, s) >= self._cfg.fuzzy_word_min for s in said):
                    return False
        return True

    @staticmethod
    def _title_case(raw: str) -> bool:
        return raw[:1].isupper() and raw[1:].islower()

    def _extra_name_words(
        self, state: _State, edge: int, step: int, name_words: set[str], case_informative: bool
    ) -> list[int]:
        """Token indexes of unexplained name-like words directly beside a match (at most two)."""
        tokens, out = state.tokens, []
        i, prev = edge + step, edge
        initials = 0
        while 0 <= i < len(tokens) and len(out) < 2:
            tok = tokens[i]
            left, right = (tokens[prev], tok) if step > 0 else (tok, tokens[prev])
            gap = state.text[left.end : right.start]
            if gap.strip() not in ("", "."):
                break  # punctuation between them: not one name
            # "K. Mathew" continues a name only if "K" is not the matched person's own initial:
            # in "Dr Thirunavukkarasu V. However, ..." the full stop ends the sentence.
            if gap.strip() == "." and (len(left.norm) != 1 or left.norm in name_words):
                break
            if len(tok.norm) == 1 and tok.norm.isalpha() and initials < 2:  # an initial: "John K Mathew"
                initials += 1
                prev, i = i, i + step
                continue
            if not self._name_like(state, i, case_informative, step):
                break
            if any(fuzz.ratio(tok.norm, w) >= self._cfg.fuzzy_word_min for w in name_words):
                break  # part of the candidate's own name, e.g. a misspelt surname
            out.append(i)
            prev, i = i, i + step
        return out

    def _name_like(self, state: _State, i: int, case_informative: bool, step: int) -> bool:
        tok = state.tokens[i]
        norm = tok.norm
        if (
            not norm.isalpha()
            or len(norm) < 2
            or norm in _STOPWORDS
            or norm in _FUNCTION_WORDS
            or norm in self._honorifics
            or norm in self._suffixes
            or norm in self._adjacent
            or not state.free(tok.start, tok.end)
        ):
            return False
        after_title = i > 0 and state.tokens[i - 1].norm in self._titles
        if step < 0 and case_informative and self._title_case(tok.raw) and not after_title:
            # Before the match, a capital at the start of a sentence is just the sentence ("Kindly Nisha ma'am").
            if i == 0 or re.search(r"[.?!]", state.text[state.tokens[i - 1].end : tok.start]):
                return False
        if case_informative and self._title_case(tok.raw):
            return True
        if case_informative and step < 0:
            return False  # in mixed-case text a lowercase word before a name is the sentence ("matches George")
        # Lower case (or all caps): could be a surname typed or transcribed without capitals ("john mathew",
        # "Dr John mathew"), or an ordinary word not in the lexicon. Count it; the answer only asks to confirm.
        return len(norm) >= 3 and not (case_informative and tok.raw.isupper())

    # ----------------------------------------------------------------------- 4. leftovers

    def _unknown_names(self, state: _State) -> None:
        tokens = state.tokens
        for i, tok in enumerate(tokens):
            if tok.norm not in self._unknown_titles or i in state.consumed:
                continue
            names = []
            for j in range(i + 1, min(i + 3, len(tokens))):
                norm = tokens[j].norm
                if j in state.consumed or not norm.isalpha() or len(norm) < 2 or norm in _STOPWORDS or norm in self._honorifics:
                    break
                names.append(j)
            if names:
                start, end = tok.start, tokens[names[-1]].end
                words = [tokens[j].norm for j in names]
                case_informative = any(c.isupper() for c in state.text) and any(c.islower() for c in state.text)
                strong = case_informative and all(self._title_case(tokens[j].raw) for j in names)
                mention = EntityMention(
                    type=EntityType.FACULTY, text=state.text[start:end], start=start, end=end, method="title",
                    score=self._cfg.unknown_name_score, near_misses=self._similar_faculty(words),
                    needs_confirmation=not strong,
                )
                state.add(mention, [i, *names])

    def _similar_faculty(self, words: Sequence[str]) -> list[Candidate]:
        """Faculty with a name word close to one of ``words`` ("neta" ~ Neetha, Neha): suggestions only."""
        scored: dict[str, Candidate] = {}
        for cid, name, name_words in self._faculty_names:
            best = max((fuzz.ratio(w, n) for w in words for n in name_words), default=0.0)
            if best >= self._cfg.fuzzy_word_min:
                scored[cid] = Candidate(canonical_id=cid, name=name, score=round(best / 100, 4), alias="(similar name)")
        ranked = sorted(scored.values(), key=lambda c: (-c.score, c.canonical_id))
        return ranked[:3]

    def _drop_lab_after_course(self, state: _State) -> None:
        """'ML lab' / 'python lab' is a class, not a place: no facility mention there."""
        course_ends = {f.last for f in state.found if f.mention.type == EntityType.COURSE}
        state.found = [
            f for f in state.found
            if not (f.mention.type == EntityType.FACILITY and f.mention.kind == "lab" and f.first is not None
                    and f.first - 1 in course_ends)
        ]

    @staticmethod
    def _tie_break(mention: EntityMention, prior: set[str]) -> EntityMention:
        if not mention.ambiguous or not prior:
            return mention
        known = [c for c in mention.candidates if c.canonical_id in prior]
        if len(known) != 1:
            return mention
        rest = [c for c in mention.candidates if c is not known[0]]
        return mention.model_copy(update={"candidates": [known[0], *rest], "ambiguous": False, "resolved_by_context": True})


def _strip_function_words(norm: str) -> str | None:
    """'msc in ai and ml' -> 'msc ai ml'; None when nothing was removed or fewer than two words remain."""
    words = norm.split()
    kept = [w for w in words if w not in _FUNCTION_WORDS]
    if len(kept) == len(words) or len(kept) < 2 or words[0] in _FUNCTION_WORDS or words[-1] in _FUNCTION_WORDS:
        return None
    return " ".join(kept)


def _token_pattern(code: str, flags: re.RegexFlag) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9.]){re.escape(code)}(?![A-Za-z0-9])", flags)
