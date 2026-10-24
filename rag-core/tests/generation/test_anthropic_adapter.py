"""The real Anthropic SDK + our adapter, driven through a mock HTTP transport (no network, no key).

This checks what a live call can't be made to do on demand: refusals, timeouts, 5xx/401 errors,
malformed and truncated output, and that every such failure falls back to the templates in the
pipeline. It also checks the exact request we send (model, structured output, effort, refusal
fallbacks, no temperature). The live-network check is `python -m rag_core.generation.validate_llm`.
"""

import json
from datetime import datetime

import pytest

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from rag_core.config import Secrets  # noqa: E402
from rag_core.contracts import Intent  # noqa: E402
from rag_core.generation.answer import AnswerGenerator  # noqa: E402
from rag_core.generation.llm_client import AnthropicClient, LLMError  # noqa: E402
from rag_core.generation.prompts import AnswerDraft  # noqa: E402
from rag_core.nlu.entities import EntityExtractor  # noqa: E402
from rag_core.nlu.intent import IntentPrediction, IntentScore  # noqa: E402
from rag_core.pipeline import Pipeline  # noqa: E402
from rag_core.scoring.confidence import ConfidenceScorer  # noqa: E402
from rag_core.scoring.grounding_check import GroundingChecker  # noqa: E402

KEY = "sk-ant-test-DO-NOT-LEAK-0123456789"
NOW = datetime(2026, 10, 20, 14, 30)


def message(text, stop_reason="end_turn", thinking=True):
    content = ([{"type": "thinking", "thinking": "", "signature": "sig"}] if thinking else []) + [{"type": "text", "text": text}]
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 20}}


def draft_json(**fields):
    base = {"answer": "", "answerable": True, "cited_fact_ids": [], "needs_clarification": False, "clarification_options": []}
    return json.dumps({**base, **fields})


class Script:
    """Replays responses (dict = 200 JSON, int = error status, Exception = raised) and records requests."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return httpx2.Response(step, json={"type": "error", "error": {"type": "api_error", "message": f"status {step}"}})
        return httpx2.Response(200, json=step)


def make_client(settings, script, **llm_overrides):
    cfg = settings.llm.model_copy(update=llm_overrides)
    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(script))
    return AnthropicClient(cfg, Secrets(anthropic_api_key=KEY), http_client=http)


def test_missing_credentials_are_reported_at_startup(settings, monkeypatch):
    from rag_core.generation.llm_client import LLMUnavailable

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    try:
        client = AnthropicClient(settings.llm, Secrets())
    except LLMUnavailable as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)
    else:  # an `ant auth login` profile on this machine resolved credentials
        assert client._client.credentials is not None


def test_request_shape(settings):
    script = Script(message(draft_json(answer="Room 874.", cited_fact_ids=["room:874"])))
    client = make_client(settings, script)
    out = client.generate("SYSTEM", "USER", AnswerDraft)
    assert out["answer"] == "Room 874."  # the thinking block is ignored, the text block parsed
    assert (client.usage.calls, client.usage.input_tokens, client.usage.output_tokens) == (1, 100, 20)  # for cost reports
    [req] = script.requests
    body = json.loads(req.content)
    assert req.url.path == "/v1/messages"
    assert "server-side-fallback-2026-07-01" in req.headers.get("anthropic-beta", "")
    # the key reaches the API in an auth header (`x-api-key` in older SDKs, `authorization` in 2.x)
    assert KEY in req.headers.get("x-api-key", "") + req.headers.get("authorization", "")
    assert body["model"] == settings.llm.model and body["max_tokens"] == settings.llm.max_tokens
    assert "temperature" not in body  # current Claude models reject sampling parameters
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == settings.llm.effort
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert body["system"] == "SYSTEM" and body["messages"] == [{"role": "user", "content": "USER"}]


def test_without_refusal_fallbacks_uses_the_stable_endpoint(settings):
    script = Script(message(draft_json(answer="ok")))
    make_client(settings, script, anthropic_fallbacks=False).generate("S", "U", AnswerDraft)
    body = json.loads(script.requests[0].content)
    assert "fallbacks" not in body and "server-side-fallback" not in script.requests[0].headers.get("anthropic-beta", "")


def test_malformed_output_is_retried_once_then_succeeds(settings):
    script = Script(message("Sure! The room is 874."), message(draft_json(answer="Room 874.")))
    assert make_client(settings, script).generate("S", "U", AnswerDraft)["answer"] == "Room 874."
    second_user = json.loads(script.requests[1].content)["messages"][0]["content"]
    assert "previous reply was not valid" in second_user


def test_malformed_twice_raises(settings):
    script = Script(message('{"answer": "no other fields"}'))
    with pytest.raises(LLMError, match="after one retry"):
        make_client(settings, script).generate("S", "U", AnswerDraft)
    assert len(script.requests) == 2


@pytest.mark.parametrize("stop,match", [("refusal", "refusal"), ("max_tokens", "max_tokens")])
def test_refusal_and_truncation_raise(settings, stop, match):
    script = Script(message("", stop_reason=stop))
    with pytest.raises(LLMError, match=match):
        make_client(settings, script).generate("S", "U", AnswerDraft)


def test_timeout_is_retried_then_raises(settings):
    script = Script(httpx2.ReadTimeout("timed out"))
    with pytest.raises(LLMError, match="Timeout"):
        make_client(settings, script, max_retries=1).generate("S", "U", AnswerDraft)
    assert len(script.requests) == 2  # one SDK retry (llm.max_retries = 1), then give up


def test_server_error_is_retried_and_recovers(settings):
    script = Script(500, message(draft_json(answer="Room 874.")))
    assert make_client(settings, script, max_retries=1).generate("S", "U", AnswerDraft)["answer"] == "Room 874."


def test_auth_error_raises_without_retry(settings):
    script = Script(401)
    with pytest.raises(LLMError, match="AuthenticationError"):
        make_client(settings, script, max_retries=1).generate("S", "U", AnswerDraft)
    assert len(script.requests) == 1


# --------------------------------------------------------------------------- through the pipeline


class FakeClassifier:
    def __init__(self, intent):
        self.intent = intent

    def predict(self, text, query_vector=None):
        return IntentPrediction(intent=self.intent, probability=0.97, top=[IntentScore(intent=self.intent, probability=0.97)],
                                source="model", model_intent=self.intent, model_probability=0.97)


@pytest.fixture(scope="module")
def pipeline_with(settings, gazetteer, make_retriever):
    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    retriever = make_retriever()

    def make(intent, script, **llm_overrides):
        llm = make_client(settings, script, **llm_overrides)
        return Pipeline(
            settings, classifier=FakeClassifier(intent), extractor=extractor, retriever=retriever,
            generator=AnswerGenerator(settings.generation, llm), checker=GroundingChecker(extractor),
            scorer=ConfidenceScorer(settings.scoring.model_copy(update={"use_calibration": False}),
                                    settings.thresholds.unverified_glossary_weight),
            gazetteer=gazetteer, clock=lambda: NOW,
        )

    return make


Q = "where is Dr Nisha Varghese's cabin?"


def test_grounded_llm_answer_flows_through(pipeline_with):
    script = Script(message(draft_json(answer="Dr Nisha Varghese's office is room 874, here on this floor.",
                                       cited_fact_ids=["faculty:nisha-varghese:location"])))
    trace = pipeline_with(Intent.FIND_PERSON, script).run(Q)
    assert trace.generation.source == "llm" and not trace.result.escalate
    assert trace.result.sources == ["faculty:nisha-varghese:location"]
    sent = json.loads(script.requests[0].content)["messages"][0]["content"]
    assert "[faculty:nisha-varghese:location]" in sent  # the numbered facts reached the model


@pytest.mark.parametrize("failure", [
    pytest.param(lambda: message("", stop_reason="refusal"), id="refusal"),
    pytest.param(lambda: httpx2.ReadTimeout("timed out"), id="timeout"),
    pytest.param(lambda: message("not json"), id="malformed"),
    pytest.param(lambda: 529, id="overloaded"),
])
def test_every_llm_failure_falls_back_to_templates(pipeline_with, failure):
    trace = pipeline_with(Intent.FIND_PERSON, Script(failure()), max_retries=0).run(Q)
    assert trace.generation.source == "fallback" and trace.generation.llm_error
    assert "874" in trace.result.answer and not trace.result.escalate


def test_llm_wrong_person_answer_is_blocked(pipeline_with, settings):
    """The model answers about the near miss for an unknown name. Even if its citation were valid, the
    right-person check must stop it; here the near miss's fact isn't retrieved, so both checks fire."""
    script = Script(message(draft_json(answer="Dr Tegil J John's office is room 881, here on this floor.",
                                       cited_fact_ids=["faculty:tegil-j-john:location"])))
    trace = pipeline_with(Intent.FIND_PERSON, script).run("Is Dr John Mathew in today?")
    assert trace.result.escalate_reason == "grounding_failed"
    assert trace.result.answer == settings.generation.safe_message and "881" not in trace.result.answer


def test_near_miss_is_flagged_to_the_model(pipeline_with):
    script = Script(message(draft_json(answer="I'm sorry, I can't find anyone called Dr John Mathew in the faculty list. "
                                       "Please check with the receptionist at the desk.", answerable=False)))
    trace = pipeline_with(Intent.FIND_PERSON, script).run("Is Dr John Mathew in today?")
    sent = json.loads(script.requests[0].content)["messages"][0]["content"]
    assert 'faculty "Dr John Mathew": not found in the records (similar name, a DIFFERENT person: Dr Tegil J John)' in sent
    assert "never answer with facts about anyone else" in json.loads(script.requests[0].content)["system"]
    assert trace.result.escalate_reason == "not_in_kb"


def test_api_key_never_leaks(pipeline_with, tmp_path):
    from rag_core.pipeline import QueryLog

    script = Script(message(draft_json(answer="Dr Nisha Varghese's office is room 874.",
                                       cited_fact_ids=["faculty:nisha-varghese:location"])))
    pipe = pipeline_with(Intent.FIND_PERSON, script)
    pipe.query_log = QueryLog(tmp_path / "q.jsonl")
    trace = pipe.run(Q)
    assert KEY not in trace.result.model_dump_json()
    assert KEY not in (tmp_path / "q.jsonl").read_text(encoding="utf-8")
    assert KEY not in pipe.generator.llm_status and KEY not in repr(pipe.settings)
