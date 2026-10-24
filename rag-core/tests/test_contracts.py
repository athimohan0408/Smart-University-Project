import pytest
from pydantic import ValidationError

from rag_core.contracts import Entity, EntityType, Intent, RAGRequest, RAGResult


def _result(**overrides) -> RAGResult:
    fields = dict(
        answer="Dr Deepa V Jose's office is room 877, Central Block, Eighth Floor.",
        confidence=0.9,
        escalate=False,
        intent=Intent.FIND_PERSON,
        intent_confidence=0.95,
        entities=[
            Entity(
                type=EntityType.FACULTY,
                text="Deepa Jose",
                canonical_id="faculty:deepa-v-jose",
                score=0.97,
            )
        ],
        sources=["faculty:deepa-v-jose#office"],
        latency_ms={"nlu": 3.1, "retrieval": 12.0, "total": 15.1},
    )
    return RAGResult(**{**fields, **overrides})


def test_valid_result_round_trips_through_json():
    result = _result()
    assert RAGResult.model_validate_json(result.model_dump_json()) == result


def test_escalation_requires_reason():
    with pytest.raises(ValidationError, match="escalate_reason"):
        _result(escalate=True)
    assert _result(escalate=True, escalate_reason="no_supporting_facts").escalate


def test_reason_without_escalation_is_rejected():
    with pytest.raises(ValidationError, match="escalate_reason"):
        _result(escalate_reason="low_confidence")


@pytest.mark.parametrize("field", ["confidence", "intent_confidence"])
@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_probabilities_are_bounded(field, value):
    with pytest.raises(ValidationError):
        _result(**{field: value})


def test_negative_latency_is_rejected():
    with pytest.raises(ValidationError):
        _result(latency_ms={"total": -1.0})


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        _result(room="877")


def test_request_strips_and_requires_question():
    assert RAGRequest(question="  where is room 877?  ").question == "where is room 877?"
    with pytest.raises(ValidationError):
        RAGRequest(question="   ")
