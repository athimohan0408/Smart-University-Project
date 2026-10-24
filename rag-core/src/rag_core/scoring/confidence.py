"""Answer confidence in [0, 1] from the pipeline's signals.

1. **Transparent formula** (always computed): a weighted sum of features in [0, 1]
   (``scoring.weights``), times ``scoring.unverified_fact_weight`` when a cited fact is unverified
   and times ``thresholds.unverified_glossary_weight`` when the answer itself uses an unverified
   glossary expansion.
2. **Calibrated** (``just calibrate`` -> ``paths.confidence_model``): a logistic regression fitted
   on the QA dev split, features -> "the answer was correct", so the score is a probability. Used
   when the artifact exists and ``scoring.use_calibration`` is on; the heuristic is kept alongside
   it for the log.

Canned intents (greeting, thanks, human, out of scope) take the intent probability: there is no
retrieval or grounding to judge.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from pydantic import BaseModel

from rag_core.config import ScoringSettings
from rag_core.contracts import EntityType, Intent, RetrievedFact
from rag_core.generation.prompts import AnswerDraft
from rag_core.nlu.entities import EntityMention
from rag_core.retrieval.retriever import RetrievalSignals
from rag_core.scoring.grounding_check import GroundingResult

KB_TYPES = frozenset({EntityType.FACULTY, EntityType.ROOM, EntityType.COURSE, EntityType.PROGRAMME, EntityType.TOPIC})
# Intents answered from facts about a specific KB entity: no linked entity => low confidence.
NEEDS_ENTITY = frozenset({
    Intent.FIND_PERSON, Intent.PERSON_AVAILABILITY, Intent.CONTACT_INFO, Intent.SCHEDULE_QUERY,
    Intent.FIND_BY_SUBJECT, Intent.FIND_ROOM, Intent.PROGRAMME_INFO,
})
FEATURE_ORDER = (
    "heuristic", "intent", "entity", "retrieval", "margin", "structured", "answerable", "grounded",
    "unverified_fact", "unverified_glossary",
)
CALIBRATOR_VERSION = 1


class ConfidenceFeatures(BaseModel):
    intent: float
    entity: float
    retrieval: float
    margin: float
    structured: float
    answerable: float
    grounded: float
    unverified_fact: float
    unverified_glossary: float


def entity_score(intent: Intent, mentions: Sequence[EntityMention]) -> float:
    """Min link score over the KB entities in the question; an unlinked person/room counts 0."""
    kb = [m for m in mentions if m.type in KB_TYPES]
    if not kb:
        return 0.0 if intent in NEEDS_ENTITY else 1.0
    return min(m.score if m.candidates else 0.0 for m in kb)


def extract_features(
    intent: Intent,
    intent_probability: float,
    mentions: Sequence[EntityMention],
    signals: RetrievalSignals,
    retrieved: Sequence[RetrievedFact],
    draft: AnswerDraft,
    grounding: GroundingResult,
) -> ConfidenceFeatures:
    by_id = {f.fact_id: f for f in retrieved}
    cited = [by_id[fid] for fid in draft.cited_fact_ids if fid in by_id]
    return ConfidenceFeatures(
        intent=intent_probability,
        entity=entity_score(intent, mentions),
        retrieval=signals.top1_score,
        margin=signals.top1_margin,
        structured=float(any(f.via == "structured" for f in cited)),
        answerable=float(draft.answerable),
        grounded=float(grounding.passed),
        unverified_fact=float(any(f.metadata.get("verified") is False for f in cited)),
        unverified_glossary=float(grounding.uses_unverified_glossary),
    )


@dataclass
class Calibrator:
    model: object  # sklearn LogisticRegression
    features: tuple[str, ...]
    dev_examples: int
    version: int = CALIBRATOR_VERSION

    def predict(self, vector: Sequence[float]) -> float:
        return float(self.model.predict_proba(np.asarray([vector]))[0, 1])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @staticmethod
    def load(path: Path) -> Calibrator | None:
        if not path.exists():
            return None
        cal = joblib.load(path)
        if not isinstance(cal, Calibrator) or cal.version != CALIBRATOR_VERSION or cal.features != FEATURE_ORDER:
            return None  # stale artifact: fall back to the heuristic until `just calibrate` is re-run
        return cal


class ConfidenceScorer:
    def __init__(self, cfg: ScoringSettings, unverified_glossary_weight: float, calibrator: Calibrator | None = None):
        self._cfg = cfg
        self._glossary_weight = unverified_glossary_weight
        self._calibrator = calibrator if cfg.use_calibration else None

    @property
    def calibrated(self) -> bool:
        return self._calibrator is not None

    def heuristic(self, f: ConfidenceFeatures) -> float:
        w = self._cfg.weights
        score = (
            w.intent * f.intent + w.entity * f.entity + w.retrieval * f.retrieval + w.margin * f.margin
            + w.structured * f.structured + w.answerable * f.answerable + w.grounded * f.grounded
        )
        if f.unverified_fact:
            score *= self._cfg.unverified_fact_weight
        if f.unverified_glossary:
            score *= self._glossary_weight
        return min(1.0, max(0.0, score))

    def vector(self, f: ConfidenceFeatures) -> list[float]:
        values = {"heuristic": self.heuristic(f), **f.model_dump()}
        return [float(values[k]) for k in FEATURE_ORDER]

    def score(self, f: ConfidenceFeatures) -> tuple[float, float]:
        """(confidence used for the decision, the transparent heuristic)."""
        h = self.heuristic(f)
        if self._calibrator is None:
            return h, h
        return min(1.0, max(0.0, self._calibrator.predict(self.vector(f)))), h


def expected_calibration_error(probs: Sequence[float], correct: Sequence[bool], bins: int) -> float:
    """Weighted mean |accuracy - confidence| over equal-width bins."""
    if not probs:
        return 0.0
    p = np.asarray(probs, dtype=float)
    y = np.asarray(correct, dtype=float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1], right=True), 0, bins - 1)
    ece = 0.0
    for b in range(bins):
        mask = idx == b
        if mask.any():
            ece += mask.mean() * abs(y[mask].mean() - p[mask].mean())
    return float(ece)
