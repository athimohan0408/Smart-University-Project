"""I/O contract of the language + RAG core (Portion 1).

Upstream (dialogue/context module) sends an already-resolved question, with pronouns replaced,
plus an optional context dict. This module returns a :class:`RAGResult`. Downstream consumers
(kiosk UI, TTS, navigation, analytics) depend on these shapes, so change them only with the
whole team's agreement and update CLAUDE.md in the same change.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveInt, model_validator

Probability = Annotated[float, Field(ge=0.0, le=1.0)]

# Chroma only stores scalar metadata values, so facts are restricted to the same types.
MetadataValue = str | int | float | bool


class Intent(StrEnum):
    """What the visitor wants. Labelling guidelines with boundary cases are in CLAUDE.md (NLU)."""

    FIND_PERSON = "find_person"  # who is X / where is X's cabin / tell me about X
    PERSON_AVAILABILITY = "person_availability"  # is X free now / when can I meet X / consultation hours
    FIND_BY_SUBJECT = "find_by_subject"  # who teaches ML / who works on blockchain
    FIND_ROOM = "find_room"  # where is room 874 / the AIML classroom / washroom / a lab
    SCHEDULE_QUERY = "schedule_query"  # what does X teach on Monday / which class is in the lab now
    PROGRAMME_INFO = "programme_info"  # what is MSc AIML / programmes, semesters, subjects
    EVENT_INFO = "event_info"  # seminars, workshops, fests, guest lectures
    FACILITY_INFO = "facility_info"  # wifi, drinking water, printing, lift, canteen, waiting area
    CONTACT_INFO = "contact_info"  # email / phone of X or an office
    GREETING = "greeting"
    SMALLTALK_OR_THANKS = "smalltalk_or_thanks"  # thanks, bye, who are you
    ESCALATE_TO_HUMAN = "escalate_to_human"  # I want to talk to a person
    OUT_OF_SCOPE = "out_of_scope"  # weather, fees, admissions, anything not about this floor


class EntityType(StrEnum):
    FACULTY = "faculty"
    ROOM = "room"
    PROGRAMME = "programme"
    COURSE = "course"
    TOPIC = "topic"  # research area / subject, e.g. "machine learning"
    DAY = "day"
    TIME = "time"
    BLOCK = "block"
    FLOOR = "floor"
    FACILITY = "facility"
    EVENT = "event"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(_Contract):
    type: EntityType
    text: str = Field(description="Surface form exactly as it appeared in the question.")
    canonical_id: str | None = Field(
        default=None,
        description="KB id, '<type>:<slug>' (e.g. 'faculty:deepa-v-jose', 'room:877'). "
        "None when the mention was recognised but could not be linked to the KB.",
    )
    score: Probability = Field(description="Recognition/linking confidence.")


RetrievalVia = Literal["structured", "dense", "bm25", "hybrid"]


class RetrievedFact(_Contract):
    fact_id: str
    text: str = Field(description="Verbatim fact text as stored in the KB.")
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)
    score: Probability = Field(
        description="Retriever score, higher is better: 1.0 for a structured hit, otherwise the "
        "normalised RRF of the dense and BM25 ranks (1.0 = ranked first by both), or the "
        "cross-encoder probability when reranking is on. Compare scores only within one list."
    )
    rank: PositiveInt = Field(description="1-based position in the result list.")
    via: RetrievalVia = Field(
        description="structured = pulled by exact KB id from a linked entity; dense / bm25 = found "
        "by that retriever only; hybrid = found by both."
    )


class RAGRequest(_Contract):
    model_config = ConfigDict(str_strip_whitespace=True)

    question: str = Field(min_length=1, description="Resolved question (no dangling pronouns).")
    context: dict[str, Any] | None = Field(
        default=None,
        description="Optional hints from the dialogue module; unknown keys are ignored. "
        "See CLAUDE.md for the recognised keys.",
    )


class RAGResult(_Contract):
    answer: str
    confidence: Probability
    escalate: bool
    escalate_reason: str | None = None
    intent: Intent
    intent_confidence: Probability
    entities: list[Entity] = Field(default_factory=list)
    sources: list[str] = Field(
        default_factory=list, description="fact_ids actually cited in the answer."
    )
    latency_ms: dict[str, NonNegativeFloat] = Field(
        default_factory=dict,
        description="Per-stage wall time: nlu, retrieval, generation, scoring, total.",
    )

    @model_validator(mode="after")
    def _escalation_has_reason(self) -> RAGResult:
        if self.escalate and not (self.escalate_reason and self.escalate_reason.strip()):
            raise ValueError("escalate=True requires a non-empty escalate_reason")
        if not self.escalate and self.escalate_reason is not None:
            raise ValueError("escalate_reason must be None when escalate=False")
        return self
