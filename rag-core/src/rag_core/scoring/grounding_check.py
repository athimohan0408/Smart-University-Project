"""Post-generation grounding check: is every claim in the answer supported by a cited fact?

No LLM. Two hard rules, then an optional NLI pass:
1. **Citations**: every ``cited_fact_id`` must be among the facts retrieved for this question.
   Anything else is a hallucinated citation.
2. **Claims**: room numbers, times, emails, phone-like numbers, faculty names and subject /
   programme codes are pulled out of the ANSWER (the entity extractor plus regexes) and each one
   must appear in the cited facts' text or metadata:
   - rooms by room key ("607A" = "607 a"), also bare 3-4 digit numbers;
   - times by 24h value: the answer's "1:45 pm" must match a time in a cited fact ("13:45" or
     "1.45 pm"); a fact time without am/pm counts for both readings;
   - emails verbatim (case-insensitive); phone-like digit runs are never in the KB;
   - faculty by KB id (the cited facts' ``faculty_id``) or display name in the cited text;
   - course / programme codes by code.
   When the draft declines (``answerable`` false), a room or name the visitor said may be echoed
   ("I can't find room 900"). Names of the candidates of an ambiguous question entity, and the
   near misses of a partly matched name, may be used to ask "did you mean ...?".
3. **Right person** (when the intent is given): an answer to a question about a person must be about
   that person. If the question links a faculty member and the answer names faculty but not them,
   or the question names someone not in the KB (nothing else linked) and the answer names anyone,
   it is a ``wrong_person`` answer: grounded in the KB, but about somebody else.
4. **NLI** (``scoring.nli.enabled``): each answer sentence against the cited facts; a sentence the
   model reads as contradicted fails.

Any unsupported item fails the check, listing the offending spans. The pipeline then escalates
with GROUNDING_FAILED and never returns the generated answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from rag_core.config import NLISettings
from rag_core.contracts import EntityType, Intent, RetrievedFact
from rag_core.generation.prompts import AnswerDraft
from rag_core.nlu.entities import EntityExtractor, EntityMention
from rag_core.nlu.gazetteer import room_key

IssueKind = Literal[
    "hallucinated_citation", "unsupported_room", "unsupported_time", "unsupported_email", "unsupported_phone",
    "unsupported_name", "unsupported_code", "unsupported_number", "wrong_person", "nli_contradiction",
]
# Intents whose subject is a person: the answer must be about the person asked for.
PERSON_INTENTS = frozenset({Intent.FIND_PERSON, Intent.CONTACT_INFO, Intent.PERSON_AVAILABILITY, Intent.SCHEDULE_QUERY})
_OTHER_KB_TYPES = frozenset({EntityType.ROOM, EntityType.COURSE, EntityType.PROGRAMME, EntityType.TOPIC})


class GroundingIssue(BaseModel):
    kind: IssueKind
    span: str = Field(description="The offending text in the answer (or the bad fact id).")


class GroundingResult(BaseModel):
    passed: bool
    issues: list[GroundingIssue] = Field(default_factory=list)
    claims_checked: int = 0
    uses_unverified_glossary: bool = Field(
        default=False, description="The answer names a code by an unverified glossary expansion ('Machine Learning')."
    )

    @property
    def spans(self) -> list[str]:
        return [i.span for i in self.issues]


# --------------------------------------------------------------------------- patterns

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w@.])\+?\d[\d\s\-()]{6,}\d(?![\w@])")
_ROOM = re.compile(r"\brooms?\s+(?:no\.?\s*|number\s+)?(\d{1,4}(?:[ -]?[A-Za-z]\b)?(?:-\d{1,3})?)", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.:@/-])(\d{3,4}[A-Za-z]?)(?![\w:@])")
_TIME_AMPM = re.compile(r"(?<![\d.:])(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s?m\b\.?", re.IGNORECASE)
_TIME_BARE = re.compile(r"(?<![\d.:])(\d{1,2})[:.](\d{2})(?![\d])(?!\s*[ap]\.?\s?m\b)", re.IGNORECASE)


def _times_with_marker(text: str) -> set[str]:
    out = set()
    for h, m, ap in _TIME_AMPM.findall(text):
        hour, minute = int(h), int(m or 0)
        if not (1 <= hour <= 12 and minute < 60):
            continue
        hour = hour % 12 + (12 if ap.lower() == "p" else 0)
        out.add(f"{hour:02d}:{minute:02d}")
    return out


def _times_bare(text: str) -> set[str]:
    """A time written without am/pm: every reading (h:mm and h+12:mm)."""
    out = set()
    for h, m in _TIME_BARE.findall(text):
        hour, minute = int(h), int(m)
        if hour > 23 or minute > 59:
            continue
        out.add(f"{hour:02d}:{minute:02d}")
        if hour < 12:
            out.add(f"{hour + 12:02d}:{minute:02d}")
    return out


def fact_times(text: str) -> set[str]:
    """Every 24h time a fact's text supports."""
    times = _times_with_marker(text) | _times_bare(text)
    # Ranges like "1:45 pm - 3:45 pm" are covered above; "2PM to 3PM" too. "Monday: 2-4" is not a
    # clock time, but consultation facts restate it as "14:00-16:00" in their 24-hour part.
    return times


def answer_times(text: str) -> list[tuple[str, set[str]]]:
    """(span, possible 24h readings) for each time in the answer."""
    out = []
    for m in _TIME_AMPM.finditer(text):
        readings = _times_with_marker(m.group(0))
        if readings:
            out.append((m.group(0).strip().rstrip("."), readings))
    for m in _TIME_BARE.finditer(text):
        readings = _times_bare(m.group(0))
        if readings:
            out.append((m.group(0), readings))
    return out


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


# --------------------------------------------------------------------------- NLI


class NLIModel(Protocol):
    def contradiction(self, premise: str, hypotheses: Sequence[str]) -> list[float]:
        """Probability that each hypothesis contradicts the premise."""
        ...


class CrossEncoderNLI:
    """cross-encoder/nli-deberta-v3-* : logits over (contradiction, entailment, neutral)."""

    def __init__(self, model):
        self._model = model
        labels = {v.lower(): k for k, v in getattr(model.config, "id2label", {}).items()} if hasattr(model, "config") else {}
        self._contradiction = labels.get("contradiction", 0)

    @classmethod
    def from_settings(cls, cfg: NLISettings) -> CrossEncoderNLI:
        from sentence_transformers import CrossEncoder

        return cls(CrossEncoder(cfg.model, device=cfg.device))

    def contradiction(self, premise: str, hypotheses: Sequence[str]) -> list[float]:
        if not hypotheses:
            return []
        import torch

        scores = self._model.predict(
            [(premise, h) for h in hypotheses], activation_fn=torch.nn.Softmax(dim=-1),
            show_progress_bar=False, convert_to_numpy=True,
        )
        return [float(row[self._contradiction]) for row in scores]


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


# --------------------------------------------------------------------------- checker


class GroundingChecker:
    def __init__(self, extractor: EntityExtractor, nli: NLIModel | None = None, max_contradiction: float = 0.5):
        self._extractor = extractor
        self._nli = nli
        self._max_contradiction = max_contradiction

    def check(
        self,
        draft: AnswerDraft,
        retrieved: Sequence[RetrievedFact],
        *,
        question: str = "",
        question_mentions: Iterable[EntityMention] = (),
        intent: Intent | None = None,
    ) -> GroundingResult:
        question_mentions = list(question_mentions)
        issues: list[GroundingIssue] = []
        by_id = {f.fact_id: f for f in retrieved}
        for fid in draft.cited_fact_ids:
            if fid not in by_id:
                issues.append(GroundingIssue(kind="hallucinated_citation", span=fid))
        cited = [by_id[fid] for fid in draft.cited_fact_ids if fid in by_id]

        answer = draft.answer
        corpus = " ".join(f.text for f in cited)
        corpus_cf = corpus.casefold()
        echo_ok = not draft.answerable
        question_cf = question.casefold()
        allowed_ids = {c.canonical_id for m in question_mentions if m.ambiguous for c in m.candidates}
        if echo_ok or draft.needs_clarification:  # "did you mean <near miss>?" in a decline or a question
            allowed_ids |= {c.canonical_id for m in question_mentions for c in m.near_misses}
        if echo_ok:
            allowed_ids |= {m.canonical_id for m in question_mentions if m.canonical_id}

        checked = 0
        # emails, then phone-like numbers (emails removed first so their digits don't count)
        for email in _EMAIL.findall(answer):
            checked += 1
            if email.casefold() not in corpus_cf:
                issues.append(GroundingIssue(kind="unsupported_email", span=email))
        no_email = _EMAIL.sub(" ", answer)
        corpus_digits = {_digits(m) for m in _PHONE.findall(corpus)}
        for phone in _PHONE.findall(no_email):
            if len(_digits(phone)) >= 7 and not _TIME_AMPM.search(phone):
                checked += 1
                if _digits(phone) not in corpus_digits:
                    issues.append(GroundingIssue(kind="unsupported_phone", span=phone.strip()))

        # rooms
        fact_rooms = {room_key(str(f.metadata.get("room", ""))) for f in cited if f.metadata.get("room")}
        fact_rooms |= {room_key(r) for r in _ROOM.findall(corpus)}
        fact_rooms |= {room_key(n) for n in _NUMBER.findall(corpus)}
        seen_rooms: set[str] = set()

        def room_ok(span: str) -> bool:
            key = room_key(span)
            return key in fact_rooms or (echo_ok and re.search(rf"\b{re.escape(span.casefold())}\b", question_cf) is not None)

        for span in _ROOM.findall(answer):
            seen_rooms.add(room_key(span))
            checked += 1
            if not room_ok(span):
                issues.append(GroundingIssue(kind="unsupported_room", span=f"room {span}"))
        no_times = _TIME_BARE.sub(" ", _TIME_AMPM.sub(" ", no_email))
        for span in _NUMBER.findall(no_times):
            if room_key(span) in seen_rooms:
                continue
            checked += 1
            if not room_ok(span):
                issues.append(GroundingIssue(kind="unsupported_number", span=span))

        # times
        supported_times = fact_times(corpus)
        for span, readings in answer_times(no_email):
            checked += 1
            if not readings & supported_times:
                issues.append(GroundingIssue(kind="unsupported_time", span=span))

        # names and codes, via the entity extractor on the answer
        fact_faculty = {x for f in cited for x in str(f.metadata.get("faculty_id", "")).split(",") if x}
        fact_codes = {c.casefold() for f in cited for c in str(f.metadata.get("subject_code", "")).split(",") if c}
        fact_progs = {c.casefold() for f in cited for c in str(f.metadata.get("programme", "")).split(",") if c}
        uses_unverified = False
        named_faculty: dict[str, str] = {}  # canonical id -> surface text, people the answer talks about
        for m in self._extractor.extract(answer).mentions:
            if m.type == EntityType.FACULTY:
                for c in ([c for c in m.candidates] if m.ambiguous else m.candidates[:1]):
                    named_faculty.setdefault(c.canonical_id, m.text)
                checked += 1
                if not self._name_ok(m, fact_faculty, corpus_cf, allowed_ids, echo_ok, question_cf):
                    issues.append(GroundingIssue(kind="unsupported_name", span=m.text))
            elif m.type in (EntityType.COURSE, EntityType.PROGRAMME) and m.candidates:
                checked += 1
                pool = fact_codes if m.type == EntityType.COURSE else fact_progs
                ok = any(
                    c.name.casefold() in pool or c.name.casefold() in corpus_cf or c.canonical_id in allowed_ids
                    for c in m.candidates
                )
                if not ok:
                    issues.append(GroundingIssue(kind="unsupported_code", span=m.text))
                if any(not c.verified for c in m.candidates) and m.text.casefold() not in {c.name.casefold() for c in m.candidates}:
                    uses_unverified = True

        if intent in PERSON_INTENTS and draft.answerable and not draft.needs_clarification and named_faculty:
            asked = {m.canonical_id for m in question_mentions if m.type == EntityType.FACULTY and m.canonical_id}
            unknown = any(m.type == EntityType.FACULTY and not m.candidates for m in question_mentions)
            other = any(m.type in _OTHER_KB_TYPES and m.canonical_id for m in question_mentions)
            if (asked and not asked & set(named_faculty)) or (not asked and unknown and not other):
                issues += [GroundingIssue(kind="wrong_person", span=text) for text in dict.fromkeys(named_faculty.values())]

        if self._nli is not None and cited and draft.answerable:
            sentences = [s for s in _SENTENCE.split(answer) if s.strip()]
            for sentence, p in zip(sentences, self._nli.contradiction(corpus, sentences)):
                if p > self._max_contradiction:
                    issues.append(GroundingIssue(kind="nli_contradiction", span=sentence))

        return GroundingResult(passed=not issues, issues=issues, claims_checked=checked, uses_unverified_glossary=uses_unverified)

    @staticmethod
    def _name_ok(m: EntityMention, fact_faculty, corpus_cf, allowed_ids, echo_ok, question_cf) -> bool:
        if not m.candidates:  # "Dr <unknown>": fine only as an echo of the visitor's words in a decline
            return echo_ok and m.text.casefold() in question_cf
        for c in m.candidates:
            slug = c.canonical_id.split(":", 1)[1]
            if slug in fact_faculty or c.name.casefold() in corpus_cf or c.canonical_id in allowed_ids:
                return True
        return False
