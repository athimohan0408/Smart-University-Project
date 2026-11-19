"""Misheard and misspelled names.

Speech-to-text and typing both mangle faculty names: "Dipa Josey", "Binayak Datta", "Sridhevi", "Naveed".
rag-core already fuzzy-matches names, but only against the whole question; a name that is too far off is
simply "not found" and the visitor is escalated. This module sits in front of it and decides, word by word:

- **fix it quietly** when one faculty member is clearly meant (``Josey`` -> ``Jose``): the corrected
  question goes to rag-core and the screen shows "I understood: ...";
- **ask** when it is close but not certain, or two people are equally close: "Did you mean Dr Binayak
  Dutta?" (one option: yes/no; several: the buttons the visitor already knows from rag-core's own
  clarifications);
- **leave it alone** otherwise, so rag-core handles it (and escalates if it really isn't a known name).

Scoring combines the spelling similarity (rapidfuzz) with a *sound* similarity: a small phonetic key that
folds the spellings Indian names vary on (``th/t``, ``dh/d``, ``v/w``, ``ph/f``, double letters, vowels)
so "Datta" and "Dutta", or "Deepa" and "Dipa", score as the same name. Nothing here invents a name: every
candidate is a word of a name in the knowledge base.

Only words that look like a name are touched: right after a title ("Dr", "Prof", "Father"), right before
"sir"/"ma'am", or a word of four or more letters that is not an ordinary English word. Settings:
``names:`` in config/settings.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

_TOKEN = re.compile(r"[A-Za-z][A-Za-z']*")
_TITLES = frozenset({"dr", "doctor", "prof", "professor", "fr", "father", "mr", "ms", "mrs"})
_HONORIFIC_AFTER = frozenset({"sir", "mam", "maam", "ma'am", "madam"})
# words a name usually follows when there is no title: "where is binayak dutta", "meet deepa jose", "email of ..."
_NAME_PRECEDERS = frozenset({"is", "are", "was", "of", "for", "with", "meet", "see", "find", "call", "about", "contact",
                             "whom", "to", "reach", "visit", "locate"})
_TITLE_BEFORE = re.compile(r"(?:dr|doctor|prof|professor|fr|father)\.?\s*$", re.IGNORECASE)

# Words that must never be "corrected" into a name: question words, places, days and the vocabulary of what
# visitors ask for. A name that is also an English word ("Joy", "Grace") is handled by the titled position.
STOPWORDS = frozenset("""
a about after again all also am an and any are around as ask at available be been before behind below book
booking both but by call called can cabin cabins canteen class classes classroom classrooms come contact could
day days department desk did direction directions do does done door down email faculty find first floor for
free friday from get give go going good got had has have help her here hers him his hostel hours how if in
inside into is it its just know lab labs later left leave lift like list locate located location look make
may me meet meeting message might monday more morning most much must my near need next no not now number of
office offices on one only open or other our out over phone please programme programmes right room rooms
saturday say see seat shall she should show sir some someone staff staircase stairs sunday take talk teach
teaches teacher teaching tell than thank thanks that the their them then there these they this those through
thursday time timings timetable to today tomorrow tuesday under until up us use visit want was washroom water
way we wednesday week well were what when where which who whom whose why will with without work would year
yes you your yours also afternoon evening hello hi hey good please sorry okay ok sitting sit sits seated share shares
shared human humans data deep many weather soon kahan hai kaha where's wheres whats thats whos
""".split())


def phonetic_key(word: str) -> str:
    """A rough sound key for an Indian-English name: similar-sounding spellings give the same key."""
    w = re.sub(r"[^a-z]", "", word.casefold())
    if not w:
        return ""
    for src, dst in (("ph", "f"), ("sh", "s"), ("ch", "c"), ("kh", "k"), ("gh", "g"), ("th", "t"), ("dh", "d"),
                     ("bh", "b"), ("jh", "j"), ("ck", "k"), ("ee", "i"), ("oo", "u"), ("ou", "u")):
        w = w.replace(src, dst)
    w = w.replace("q", "k").replace("x", "ks").replace("w", "v").replace("z", "s").replace("y", "i")
    w = re.sub(r"(.)\1+", r"\1", w)  # double letters: "Joseph" / "Josef", "Rajesh" / "Raajesh"
    head, tail = w[0], re.sub(r"[aeiouh]", "", w[1:])  # vowels (and h) carry the least information; keep the first letter
    return head + tail


def _similarity(token: str, word: str) -> float:
    """0-100: spelling similarity, lifted when the two sound alike."""
    spelling = fuzz.ratio(token, word)
    key_t, key_w = phonetic_key(token), phonetic_key(word)
    sound = fuzz.ratio(key_t, key_w)
    blended = (spelling + sound) / 2 + (8 if key_t == key_w and len(key_t) >= 3 else 0)
    return min(100.0, max(spelling, blended))


@dataclass(frozen=True)
class Candidate:
    word: str  # the name word it matches, as in the knowledge base
    score: float
    faculty_ids: tuple[str, ...]


@dataclass(frozen=True)
class NameFix:
    """One word (or title + word) the matcher acted on."""

    span: tuple[int, int]  # in the text, including a leading title
    heard: str  # the words as said
    replacement: str | None  # corrected words (auto-fix), else None
    options: tuple[str, ...] = ()  # display names to ask about (ask), else ()
    candidates: tuple[Candidate, ...] = field(default=(), compare=False)


@dataclass
class NameMatcher:
    """Matches words of a question to the words of faculty names. Build one per knowledge base."""

    words: dict[str, set[str]]  # name word -> faculty ids that have it
    display: dict[str, str]  # faculty id -> "Dr Deepa V Jose"
    auto_min: float = 88.0  # at or above this, with a clear winner: fix without asking
    ask_min: float = 74.0  # at or above this: ask "did you mean"
    margin: float = 8.0  # the winner must beat the runner-up (another person) by this much to be fixed quietly
    min_len: int = 3
    max_options: int = 3

    @classmethod
    def from_kb(cls, kb, **settings) -> NameMatcher:
        words: dict[str, set[str]] = {}
        for cid, person in kb.people.items():
            for w in person.name_words:
                if len(w) >= 3:
                    words.setdefault(w, set()).add(cid)
        for w, ids in kb._name_index.items():  # aliases ("Father Jossy", "Sridevi")
            words.setdefault(w, set()).update(ids)
        return cls(words, {cid: p.name for cid, p in kb.people.items()}, **settings)

    # ----------------------------------------------------------------------- scoring

    def candidates(self, token: str) -> list[Candidate]:
        """Name words near ``token``, best first, one entry per word, only those that reach ``ask_min``."""
        t = token.casefold().strip("'")
        out = []
        for word, ids in self.words.items():
            if abs(len(word) - len(t)) > 3:
                continue
            score = _similarity(t, word)
            if score >= self.ask_min:
                out.append(Candidate(word, round(score, 1), tuple(sorted(ids))))
        return sorted(out, key=lambda c: (-c.score, c.word))

    def is_known(self, token: str) -> bool:
        return token.casefold().strip("'") in self.words

    # ----------------------------------------------------------------------- fixing a question

    def scan(self, text: str) -> list[NameFix]:
        """Every name-like word in ``text`` that is not a known name, with what to do about it."""
        tokens = []
        for m in _TOKEN.finditer(text):
            word = m.group(0)
            if re.search(r"['’]s$", word, re.IGNORECASE):  # "Varghese's": the name is "Varghese"
                word = word[:-2]
            tokens.append((word, m.start(), m.start() + len(word)))
        fixes: list[NameFix] = []
        in_name = False  # the previous word was an unknown name word: this one may continue it ("Dipa Josey")
        for i, (tok, start, end) in enumerate(tokens):
            low = tok.casefold()
            previous = tokens[i - 1][0].casefold() if i else ""
            if low in STOPWORDS or low in _TITLES or low in _HONORIFIC_AFTER or self.is_known(low) or len(low) < self.min_len:
                in_name = False
                continue
            titled = bool(_TITLE_BEFORE.search(text[:start]))
            before_honorific = i + 1 < len(tokens) and tokens[i + 1][0].casefold() in _HONORIFIC_AFTER
            strong = titled or before_honorific or in_name  # a place only a name can stand
            after_known_name = previous != "" and self.is_known(previous) and previous not in STOPWORDS
            weak = (i == 0 or previous in _NAME_PRECEDERS or after_known_name) and len(low) >= 4  # where a name usually stands
            if not (strong or weak):
                in_name = False
                continue
            cands = self.candidates(low)
            fix = self._decide(tok, start, end, cands, text, titled=titled, quiet_only=not strong) if cands else None
            in_name = strong and fix is not None
            if fix is not None:
                fixes.append(fix)
        return self._merge_adjacent(text, fixes)

    @staticmethod
    def _merge_adjacent(text: str, fixes: list[NameFix]) -> list[NameFix]:
        """"Dr Dipa Josey": two unknown words in a row are one name, asked about once."""
        merged: list[NameFix] = []
        for fix in fixes:
            prev = merged[-1] if merged else None
            if (prev is not None and not prev.replacement and not fix.replacement
                    and text[prev.span[1]:fix.span[0]].strip() == ""):
                both = tuple(o for o in prev.options if o in fix.options) or prev.options
                merged[-1] = NameFix((prev.span[0], fix.span[1]), text[prev.span[0]:fix.span[1]], None, both,
                                     prev.candidates + fix.candidates)
            else:
                merged.append(fix)
        return merged

    def _decide(self, tok: str, start: int, end: int, cands: list[Candidate], text: str, *, titled: bool,
                quiet_only: bool = False) -> NameFix | None:
        best = cands[0]
        span_start = start
        if titled and (m := _TITLE_BEFORE.search(text[:start])):
            span_start = m.start()  # ask about "Dr Datta", not just "Datta"
        span = (span_start, end)
        heard = text[span_start:end]
        # who else is nearly as close? (different people, not different words of the same person)
        best_ids = set(best.faculty_ids)
        rivals = [c for c in cands[1:] if not set(c.faculty_ids) <= best_ids and best.score - c.score < self.margin]
        if best.score >= self.auto_min and not rivals:
            return NameFix(span, heard, _match_case(tok, best.word), candidates=tuple(cands))
        if quiet_only:
            return None  # only a name-shaped place, not a sure name: never interrupt the visitor for it
        # ask: the people behind the closest words
        ids: list[str] = []
        for c in [best, *rivals]:
            for fid in c.faculty_ids:
                if fid not in ids:
                    ids.append(fid)
        options = tuple(self.display[i] for i in ids[: self.max_options] if i in self.display)
        if not options:
            return None
        return NameFix(span, heard, None, options, tuple(cands))

    def apply(self, text: str, fixes: list[NameFix]) -> str:
        """The text with every quiet fix applied (fixes that ask are left for the caller)."""
        out = text
        for fix in sorted((f for f in fixes if f.replacement), key=lambda f: f.span[0], reverse=True):
            start, end = fix.span
            out = out[:start] + _swap_last_word(out[start:end], fix.replacement) + out[end:]
        return out


def _match_case(heard: str, word: str) -> str:
    return word.upper() if heard.isupper() and len(heard) > 1 else word.capitalize()


def _swap_last_word(span_text: str, replacement: str) -> str:
    m = list(_TOKEN.finditer(span_text))
    if not m:
        return replacement
    last = m[-1]
    return span_text[: last.start()] + replacement + span_text[last.end():]
