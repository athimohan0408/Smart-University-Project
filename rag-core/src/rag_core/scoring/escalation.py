"""Escalation decision: answer, ask, or hand over to the human receptionist.

Checked in this order (the first that applies wins):
1. USER_REQUESTED: the visitor asked for a person.
2. OUT_OF_SCOPE: fees, admissions, weather...
3. Greeting / smalltalk: answered, never escalated.
4. GROUNDING_FAILED: the answer states something the cited facts don't support, or cites a fact
   that wasn't retrieved. The generated answer is NEVER returned: the visitor hears
   ``generation.safe_message``.
5. Clarification: the draft asks "which one do you mean?" (several options) or "do you mean X?" (one
   near miss to confirm), so it is answered. With no option to offer, or an ambiguous entity the
   draft silently answered for, AMBIGUOUS_ENTITY.
6. LLM_ERROR: the LLM failed and no template could answer.
7. NOT_IN_KB: the generator says the facts don't hold the answer (it already said so politely and
   passed grounding, so that answer is kept).
8. LOW_CONFIDENCE: confidence below ``thresholds.answer_min_confidence`` (the safe message).

``escalate_reason`` values are short machine-readable codes; analytics groups by them.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel

from rag_core.contracts import Intent
from rag_core.generation.answer import Generation
from rag_core.generation.templates import HUMAN, OUT_OF_SCOPE
from rag_core.nlu.entities import EntityMention
from rag_core.scoring.grounding_check import GroundingResult


class EscalationReason(StrEnum):
    LOW_CONFIDENCE = "low_confidence"
    NOT_IN_KB = "not_in_kb"
    GROUNDING_FAILED = "grounding_failed"
    AMBIGUOUS_ENTITY = "ambiguous_entity"
    USER_REQUESTED = "user_requested"
    OUT_OF_SCOPE = "out_of_scope"
    LLM_ERROR = "llm_error"


class Decision(BaseModel):
    answer: str
    escalate: bool
    reason: EscalationReason | None = None
    sources: list[str]
    clarification: bool = False


def decide(
    intent: Intent,
    generation: Generation,
    grounding: GroundingResult,
    confidence: float,
    mentions: Sequence[EntityMention],
    *,
    threshold: float,
    safe_message: str,
) -> Decision:
    draft = generation.draft

    def escalate(reason: EscalationReason, answer: str = safe_message, sources: Sequence[str] = ()) -> Decision:
        return Decision(answer=answer, escalate=True, reason=reason, sources=list(sources))

    if intent == Intent.ESCALATE_TO_HUMAN:
        return escalate(EscalationReason.USER_REQUESTED, HUMAN)
    if intent == Intent.OUT_OF_SCOPE:
        return escalate(EscalationReason.OUT_OF_SCOPE, OUT_OF_SCOPE)
    if intent in (Intent.GREETING, Intent.SMALLTALK_OR_THANKS):
        return Decision(answer=draft.answer, escalate=False, sources=[])
    if not grounding.passed:
        return escalate(EscalationReason.GROUNDING_FAILED)
    if draft.needs_clarification:
        if draft.clarification_options:
            return Decision(answer=draft.answer, escalate=False, sources=draft.cited_fact_ids, clarification=True)
        return escalate(EscalationReason.AMBIGUOUS_ENTITY)
    if any(m.ambiguous and not m.resolved_by_context for m in mentions) and draft.answerable:
        return escalate(EscalationReason.AMBIGUOUS_ENTITY)  # it answered for one of several people
    if generation.source == "none" and generation.llm_error:
        return escalate(EscalationReason.LLM_ERROR)
    if not draft.answerable:
        return escalate(EscalationReason.NOT_IN_KB, draft.answer, draft.cited_fact_ids)
    if confidence < threshold:
        return escalate(EscalationReason.LOW_CONFIDENCE)
    return Decision(answer=draft.answer, escalate=False, sources=draft.cited_fact_ids)
