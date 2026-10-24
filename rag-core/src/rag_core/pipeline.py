"""End-to-end: resolved question -> RAGResult.

normalise -> intent -> entities -> (canned shortcut) -> retrieve -> generate -> grounding check ->
confidence -> escalation decision, each stage timed into ``latency_ms``
(nlu, retrieval, generation, scoring, total).

Models and indexes load once into a module-level :class:`Pipeline` (``get_pipeline``); call
:func:`warmup` at start-up so the first visitor doesn't wait. :func:`reload` swaps in a fresh
pipeline after the KB is rebuilt.

Context from the dialogue module (unknown keys are ignored):
- ``now``: ISO-8601 local datetime for "now"/"today" (default: the server clock; ``now=`` wins).
- ``last_entities``: canonical ids (or ``{"canonical_id": ...}``) from earlier turns. Used ONLY
  when the question links no KB entity at all, to fill that gap. Never coreference here.
- ``prior_entities``: canonical ids that may break a tie between equally good matches.
- ``session_id``: logged as given (an opaque id); never changes the answer.

Every request appends one JSON line to ``paths.query_log`` (schema in docs/interface.md). It holds
the question and the system's outputs only, nothing else about the visitor.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag_core.config import Settings, get_settings
from rag_core.contracts import Intent, RAGResult, RetrievedFact
from rag_core.generation.answer import AnswerGenerator, Generation
from rag_core.generation.llm_client import LLMClient
from rag_core.generation.templates import CANNED_INTENTS
from rag_core.nlu.entities import Candidate, EntityExtractor, EntityMention
from rag_core.nlu.gazetteer import Gazetteer, load_gazetteer
from rag_core.nlu.intent import IntentClassifier, IntentPrediction
from rag_core.retrieval.retriever import RetrievalResult, RetrievalSignals, Retriever
from rag_core.scoring.confidence import (
    KB_TYPES,
    NEEDS_ENTITY,
    Calibrator,
    ConfidenceFeatures,
    ConfidenceScorer,
    extract_features,
)
from rag_core.scoring.escalation import Decision, decide
from rag_core.scoring.grounding_check import CrossEncoderNLI, GroundingChecker, GroundingResult

log = logging.getLogger(__name__)

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def normalise_question(text: str) -> str:
    """Unicode NFKC, straight quotes, single spaces. Case and wording are kept for the NLU."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip()


def _context_ids(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    out = []
    for item in value:
        cid = item.get("canonical_id") if isinstance(item, Mapping) else item
        if isinstance(cid, str) and ":" in cid:
            out.append(cid)
    return out


@dataclass
class Trace:
    """Everything one request produced; the eval and the log read it."""

    result: RAGResult
    question: str
    now: datetime
    prediction: IntentPrediction
    mentions: list[EntityMention]
    retrieval: RetrievalResult | None
    generation: Generation
    grounding: GroundingResult
    features: ConfidenceFeatures | None
    heuristic_confidence: float
    decision: Decision
    context_filled: list[str] = field(default_factory=list)

    @property
    def retrieved_ids(self) -> list[str]:
        return [f.fact_id for f in self.retrieval.facts] if self.retrieval else []

    @property
    def retrieved(self) -> list[RetrievedFact]:
        return list(self.retrieval.facts) if self.retrieval else []


class QueryLog:
    """Append-only JSON lines for analytics. One line per request; see docs/interface.md."""

    def __init__(self, path: Path | None):
        self._path = path
        self._lock = threading.Lock()

    def write(self, trace: Trace, session_id: str | None) -> None:
        if self._path is None:
            return
        r = trace.result
        record = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "request_id": uuid.uuid4().hex,
            "session_id": session_id,
            "question": trace.question,
            "intent": r.intent.value,
            "intent_confidence": round(r.intent_confidence, 4),
            "entities": [e.model_dump(mode="json") for e in r.entities],
            "retrieved_fact_ids": trace.retrieved_ids,
            "sources": r.sources,
            "answer_source": trace.generation.source,
            "confidence": round(r.confidence, 4),
            "escalate": r.escalate,
            "escalate_reason": r.escalate_reason,
            "grounding_issues": [i.kind for i in trace.grounding.issues],
            "clarification": trace.decision.clarification,
            "latency_ms": {k: round(v, 1) for k, v in r.latency_ms.items()},
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        classifier: IntentClassifier,
        extractor: EntityExtractor,
        retriever: Retriever,
        generator: AnswerGenerator,
        checker: GroundingChecker,
        scorer: ConfidenceScorer,
        gazetteer: Gazetteer,
        query_log: QueryLog | None = None,
        clock=datetime.now,
    ):
        self.settings = settings
        self.classifier = classifier
        self.extractor = extractor
        self.retriever = retriever
        self.generator = generator
        self.checker = checker
        self.scorer = scorer
        self.gazetteer = gazetteer
        self.query_log = query_log or QueryLog(None)
        self._clock = clock

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        log_queries: bool = True,
        use_calibration: bool | None = None,
    ) -> Pipeline:
        from rag_core.retrieval.embeddings import Embedder

        embedder = Embedder.from_settings(settings.embeddings)  # one model for intent and retrieval
        gazetteer = load_gazetteer(settings.paths.gazetteer, settings.paths.facts_jsonl)
        extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
        nli = CrossEncoderNLI.from_settings(settings.scoring.nli) if settings.scoring.nli.enabled else None
        scoring = settings.scoring
        if use_calibration is not None:
            scoring = scoring.model_copy(update={"use_calibration": use_calibration})
        return cls(
            settings,
            classifier=IntentClassifier.from_settings(settings, embedder=embedder),
            extractor=extractor,
            retriever=Retriever.from_settings(settings, embedder=embedder),
            generator=AnswerGenerator.from_settings(settings, llm=llm),
            checker=GroundingChecker(extractor, nli, scoring.nli.max_contradiction),
            scorer=ConfidenceScorer(scoring, settings.thresholds.unverified_glossary_weight,
                                    Calibrator.load(settings.paths.confidence_model)),
            gazetteer=gazetteer,
            query_log=QueryLog(settings.paths.query_log if log_queries else None),
        )

    # ----------------------------------------------------------------------- helpers

    def _now(self, now: datetime | None, context: Mapping[str, Any]) -> datetime:
        if now is not None:
            return now
        value = context.get("now")
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                log.warning("ignoring unparseable context now=%r", value)
        elif isinstance(value, datetime):
            return value
        return self._clock()

    def _fill_from_context(self, intent: Intent, mentions: list[EntityMention], context: Mapping[str, Any]) -> list[str]:
        """Add last_entities only when the question links no KB entity and the intent needs one."""
        if intent not in NEEDS_ENTITY or any(m.type in KB_TYPES for m in mentions):
            return []
        filled = []
        for cid in _context_ids(context.get("last_entities")):
            entry = self.gazetteer.entry(cid)
            if entry is None or entry.type not in KB_TYPES:
                continue
            mentions.append(EntityMention(
                type=entry.type, text="", start=0, end=0, method="context", score=1.0,
                candidates=[Candidate(canonical_id=cid, name=entry.name, score=1.0, alias="(context)")],
            ))
            filled.append(cid)
        return filled

    # ----------------------------------------------------------------------- entry points

    def answer(self, question: str, context: Mapping[str, Any] | None = None, now: datetime | None = None) -> RAGResult:
        return self.run(question, context, now).result

    def run(self, question: str, context: Mapping[str, Any] | None = None, now: datetime | None = None) -> Trace:
        context = dict(context or {})
        timings: dict[str, float] = {}
        start = clock = time.perf_counter()

        def lap(stage: str) -> None:
            nonlocal clock
            t = time.perf_counter()
            timings[stage] = (t - clock) * 1000
            clock = t

        text = normalise_question(question)
        when = self._now(now, context)
        vector = self.retriever.embed_query(text)
        prediction = self.classifier.predict(text, query_vector=vector)
        intent = prediction.intent
        extraction = self.extractor.extract(text, now=when, prior_entities=_context_ids(context.get("prior_entities")))
        mentions = list(extraction.mentions)
        filled = self._fill_from_context(intent, mentions, context)
        lap("nlu")

        retrieval = None
        if intent not in CANNED_INTENTS:
            retrieval = self.retriever.search(text, intent, mentions, query_vector=vector)
        facts = list(retrieval.facts) if retrieval else []
        lap("retrieval")

        generation = self.generator.generate(text, intent, mentions, facts, when)
        lap("generation")

        grounding = self.checker.check(generation.draft, facts, question=text, question_mentions=mentions, intent=intent)
        if intent in CANNED_INTENTS:
            features, confidence, heuristic = None, prediction.probability, prediction.probability
        else:
            signals = retrieval.signals if retrieval else RetrievalSignals()
            features = extract_features(intent, prediction.probability, mentions, signals, facts, generation.draft, grounding)
            confidence, heuristic = self.scorer.score(features)
        decision = decide(
            intent, generation, grounding, confidence, mentions,
            threshold=self.settings.thresholds.answer_min_confidence,
            safe_message=self.settings.generation.safe_message,
        )
        lap("scoring")
        timings["total"] = (time.perf_counter() - start) * 1000

        result = RAGResult(
            answer=decision.answer,
            confidence=confidence,
            escalate=decision.escalate,
            escalate_reason=decision.reason.value if decision.reason else None,
            intent=intent,
            intent_confidence=prediction.probability,
            entities=[m.to_entity() for m in extraction.mentions],
            sources=decision.sources,
            latency_ms=timings,
        )
        trace = Trace(result, text, when, prediction, mentions, retrieval, generation, grounding, features,
                      heuristic, decision, filled)
        try:
            self.query_log.write(trace, context.get("session_id") if isinstance(context.get("session_id"), str) else None)
        except OSError as exc:  # logging must never break an answer
            log.warning("query log write failed: %s", exc)
        return trace

    def warmup(self) -> None:
        """Run one question through every stage (unlogged) so lazy init happens before the first visitor."""
        query_log, self.query_log = self.query_log, QueryLog(None)
        try:
            self.run("Where is room 874?")
        finally:
            self.query_log = query_log


_lock = threading.Lock()
_pipeline: Pipeline | None = None


def get_pipeline() -> Pipeline:
    global _pipeline
    with _lock:
        if _pipeline is None:
            _pipeline = Pipeline.from_settings(get_settings())
        return _pipeline


def reload() -> Pipeline:
    """Load a fresh pipeline (after `just index` or /reload-kb) and swap it in."""
    global _pipeline
    fresh = Pipeline.from_settings(get_settings())
    with _lock:
        _pipeline = fresh
    return fresh


def warmup() -> Pipeline:
    pipeline = get_pipeline()
    pipeline.warmup()
    return pipeline


def answer(question: str, context: dict | None = None, now: datetime | None = None) -> RAGResult:
    """The module's entry point. See the module docstring for the context keys."""
    return get_pipeline().answer(question, context, now)
