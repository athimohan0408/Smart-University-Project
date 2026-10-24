"""The LLM client's JSON handling, offline: schema closing, parsing, validate + retry once."""

import json

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.generation.llm_client import (
    FakeLLMClient,
    LLMError,
    LLMUnavailable,
    json_schema_for,
    make_llm_client,
    parse_json_object,
)
from rag_core.generation.prompts import AnswerDraft

GOOD = {"answer": "Room 874.", "answerable": True, "cited_fact_ids": ["room:874"],
        "needs_clarification": False, "clarification_options": []}


def test_schema_is_closed_for_strict_structured_output():
    schema = json_schema_for(AnswerDraft)
    assert schema["additionalProperties"] is False
    assert schema["required"] == sorted(AnswerDraft.model_fields)


def test_parse_tolerates_fences_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": 1} Hope that helps.') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")


def test_invalid_reply_is_retried_once_with_the_error_appended():
    replies = iter(['{"answer": "x"}', json.dumps(GOOD)])
    client = FakeLLMClient(lambda system, user: next(replies))
    assert client.generate("sys", "question", AnswerDraft) == GOOD
    assert len(client.calls) == 2
    assert "previous reply was not valid" in client.calls[1][1] and "answerable" in client.calls[1][1]


def test_second_invalid_reply_raises():
    client = FakeLLMClient(lambda system, user: "not json at all")
    with pytest.raises(LLMError, match="after one retry"):
        client.generate("sys", "q", AnswerDraft)
    assert len(client.calls) == 2


def test_missing_sdk_is_reported_as_unavailable():
    pytest.importorskip("rag_core")  # always present; documents intent
    try:
        import anthropic  # noqa: F401
    except ImportError:
        with pytest.raises(LLMUnavailable, match="pip install"):
            make_llm_client(load_settings(DEFAULT_SETTINGS_PATH))
    else:
        pytest.skip("anthropic is installed")


def test_llm_settings_match_the_spec():
    cfg = load_settings(DEFAULT_SETTINGS_PATH).llm
    assert cfg.temperature == 0.0 and cfg.max_retries == 1 and cfg.timeout_s > 0
