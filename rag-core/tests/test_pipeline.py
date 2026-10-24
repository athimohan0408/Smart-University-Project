"""End-to-end pipeline, offline: fake LLM + fake intent model, real extraction, grounding, scoring.

The injected-fault tests feed the pipeline an LLM answer with a wrong room, then one with a fake
citation: both must be blocked (never returned) and escalated as grounding_failed.
"""

import json
from datetime import datetime

import pytest

from rag_core.contracts import Intent, RAGResult, RetrievedFact
from rag_core.generation.answer import AnswerGenerator
from rag_core.generation.llm_client import FakeLLMClient, LLMError
from rag_core.nlu.entities import EntityExtractor
from rag_core.nlu.intent import IntentPrediction, IntentScore
from rag_core.pipeline import Pipeline, QueryLog, normalise_question
from rag_core.scoring.confidence import ConfidenceScorer
from rag_core.scoring.grounding_check import GroundingChecker

NOW = datetime(2026, 10, 20, 14, 30)


class FakeClassifier:
    def __init__(self, intent: Intent, p: float = 0.97):
        self.intent, self.p = intent, p

    def predict(self, text, query_vector=None):
        top = [IntentScore(intent=self.intent, probability=self.p)]
        return IntentPrediction(intent=self.intent, probability=self.p, top=top, source="model",
                                model_intent=self.intent, model_probability=self.p)


def llm_reply(**fields):
    base = {"answer": "", "answerable": True, "cited_fact_ids": [], "needs_clarification": False, "clarification_options": []}
    return FakeLLMClient(lambda system, user: json.dumps({**base, **fields}))


@pytest.fixture(scope="module")
def build(settings, gazetteer, make_retriever):
    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    retriever = make_retriever()
    template_cfg = settings.generation.model_copy(update={"mode": "template"})

    def make(intent: Intent, llm=None, query_log=None) -> Pipeline:
        gen_cfg = settings.generation if llm is not None else template_cfg
        return Pipeline(
            settings,
            classifier=FakeClassifier(intent),
            extractor=extractor,
            retriever=retriever,
            generator=AnswerGenerator(gen_cfg, llm),
            checker=GroundingChecker(extractor),
            scorer=ConfidenceScorer(settings.scoring.model_copy(update={"use_calibration": False}),
                                    settings.thresholds.unverified_glossary_weight),
            gazetteer=gazetteer,
            query_log=query_log,
            clock=lambda: NOW,
        )

    return make


Q = "where is Dr Nisha Varghese's cabin?"


def test_grounded_llm_answer_is_returned_with_its_sources(build):
    llm = llm_reply(answer="Dr Nisha Varghese's office is room 874, here on this floor.",
                    cited_fact_ids=["faculty:nisha-varghese:location"])
    r = build(Intent.FIND_PERSON, llm).answer(Q)
    assert not r.escalate and r.escalate_reason is None
    assert r.answer.startswith("Dr Nisha Varghese's office is room 874")
    assert r.sources == ["faculty:nisha-varghese:location"]
    assert set(r.latency_ms) == {"nlu", "retrieval", "generation", "scoring", "total"}


def test_injected_wrong_room_is_blocked_and_escalated(build, settings):
    llm = llm_reply(answer="Dr Nisha Varghese's office is room 875, here on this floor.",
                    cited_fact_ids=["faculty:nisha-varghese:location"])
    trace = build(Intent.FIND_PERSON, llm).run(Q)
    r = trace.result
    assert r.escalate and r.escalate_reason == "grounding_failed"
    assert r.answer == settings.generation.safe_message and "875" not in r.answer
    assert r.sources == []
    assert ("unsupported_room", "room 875") in [(i.kind, i.span) for i in trace.grounding.issues]


def test_injected_fake_citation_is_blocked_and_escalated(build, settings):
    llm = llm_reply(answer="Dr Nisha Varghese's office is room 874.", cited_fact_ids=["faculty:nisha-varghese:office"])
    trace = build(Intent.FIND_PERSON, llm).run(Q)
    assert trace.result.escalate and trace.result.escalate_reason == "grounding_failed"
    assert trace.result.answer == settings.generation.safe_message
    # the bad id is flagged, and with nothing real cited the room and name are unsupported too
    assert ("hallucinated_citation", "faculty:nisha-varghese:office") in [(i.kind, i.span) for i in trace.grounding.issues]


def test_unanswerable_phone_question_escalates_not_in_kb(build):
    llm = llm_reply(answer="I'm sorry, I can't verify a phone number for Dr Nisha Varghese. Please check with the receptionist at the desk.",
                    answerable=False, cited_fact_ids=[])
    r = build(Intent.CONTACT_INFO, llm).answer("what is Dr Nisha's phone number?")
    assert r.escalate and r.escalate_reason == "not_in_kb"


def test_llm_outage_falls_back_to_templates(build):
    def down(system, user):
        raise LLMError("timeout")
    trace = build(Intent.FIND_PERSON, FakeLLMClient(down)).run(Q)
    assert trace.generation.source == "fallback" and trace.generation.llm_error == "timeout"
    assert "874" in trace.result.answer and not trace.result.escalate


def test_llm_outage_without_a_template_escalates_llm_error(build):
    def down(system, user):
        raise LLMError("timeout")
    r = build(Intent.FIND_ROOM, FakeLLMClient(down)).answer("where can I find the dean's office")
    assert r.escalate and r.escalate_reason == "llm_error"


def test_canned_intents_skip_retrieval_and_the_llm(build):
    llm = FakeLLMClient(lambda s, u: pytest.fail("LLM must not be called"))
    trace = build(Intent.GREETING, llm).run("hello")
    assert trace.retrieval is None and not trace.result.escalate
    trace = build(Intent.ESCALATE_TO_HUMAN, llm).run("I want to talk to a person")
    assert trace.result.escalate_reason == "user_requested"
    assert build(Intent.OUT_OF_SCOPE, llm).answer("what are the hostel fees").escalate_reason == "out_of_scope"


def test_ambiguous_name_is_a_clarification_not_an_escalation(build):
    trace = build(Intent.FIND_PERSON).run("where is George")
    assert not trace.result.escalate and trace.decision.clarification
    assert "Which one do you mean?" in trace.result.answer


def test_unknown_surname_declines_and_offers_the_near_miss(build, settings):
    trace = build(Intent.FIND_PERSON).run("Is Dr John Mathew in today?")
    r = trace.result
    assert r.escalate and r.escalate_reason == "not_in_kb"
    assert "can't find anyone called Dr John Mathew" in r.answer and "Did you mean Dr Tegil J John?" in r.answer
    assert "881" not in r.answer and r.sources == []
    assert [e.canonical_id for e in r.entities if e.type.value == "faculty"] == [None]
    assert not any(f.via == "structured" and "tegil" in f.fact_id for f in trace.retrieved)  # nothing pulled for him


def test_lowercase_partial_name_asks_for_confirmation(build):
    trace = build(Intent.FIND_PERSON).run("where is dr sridevi krishnan")
    r = trace.result
    assert not r.escalate and trace.decision.clarification
    assert r.answer == "Just to make sure I have the right person: do you mean Dr Sridevi R?"
    assert "875" not in r.answer and r.sources == []


def test_injected_llm_answer_about_the_near_miss_is_blocked(build, settings, monkeypatch):
    """Worst case: semantic search fetched the near miss's fact and the LLM answered from it with a
    valid citation. Only the right-person check stands between that and the visitor."""
    llm = llm_reply(answer="Dr Tegil J John's office is room 881, here on this floor.",
                    cited_fact_ids=["faculty:tegil-j-john:location"])
    pipe = build(Intent.FIND_PERSON, llm)
    real_search = pipe.retriever.search

    def search_with_tegil(*args, **kwargs):
        res = real_search(*args, **kwargs)
        if any(f.fact_id == "faculty:tegil-j-john:location" for f in res.facts):
            return res
        fact = pipe.retriever.catalog.get("faculty:tegil-j-john:location")
        extra = RetrievedFact(fact_id=fact.fact_id, text=fact.text, metadata=fact.chroma_metadata(),
                              score=0.5, rank=len(res.facts) + 1, via="bm25")
        return res.model_copy(update={"facts": [*res.facts, extra]})

    monkeypatch.setattr(pipe.retriever, "search", search_with_tegil)
    trace = pipe.run("Is Dr John Mathew in today?")
    assert "faculty:tegil-j-john:location" in trace.retrieved_ids
    assert trace.result.escalate_reason == "grounding_failed" and trace.result.answer == settings.generation.safe_message
    assert "wrong_person" in [i.kind for i in trace.grounding.issues]


def test_injected_llm_answer_about_another_person_is_blocked(build, settings):
    pipe = build(Intent.FIND_PERSON)
    other = next(f for f in pipe.run(Q).retrieved
                 if f.metadata.get("category") == "faculty_location" and f.metadata.get("faculty_id") != "nisha-varghese")
    who, room = other.text.split("'s office is room ")[0], other.metadata["room"]
    llm = llm_reply(answer=f"{who}'s office is room {room}.", cited_fact_ids=[other.fact_id])
    trace = build(Intent.FIND_PERSON, llm).run(Q)
    assert trace.result.escalate_reason == "grounding_failed"
    assert "wrong_person" in [i.kind for i in trace.grounding.issues]


def test_last_entities_fill_only_a_missing_entity(build):
    pipe = build(Intent.FIND_PERSON)
    filled = pipe.run("where is her office?", {"last_entities": ["faculty:deepa-v-jose"]})
    assert filled.context_filled == ["faculty:deepa-v-jose"] and "877" in filled.result.answer
    kept = pipe.run(Q, {"last_entities": ["faculty:deepa-v-jose"]})
    assert kept.context_filled == [] and "874" in kept.result.answer
    assert filled.result.entities == []  # the contract lists what the question said, not the context


def test_query_log_line(build, tmp_path):
    path = tmp_path / "queries.jsonl"
    build(Intent.FIND_PERSON, query_log=QueryLog(path)).answer(Q, {"session_id": "kiosk-1"})
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["intent"] == "find_person" and record["session_id"] == "kiosk-1"
    assert record["sources"] == ["faculty:nisha-varghese:location"]
    assert set(record) >= {"timestamp", "question", "entities", "retrieved_fact_ids", "confidence", "escalate",
                           "escalate_reason", "latency_ms"}


def test_result_is_a_valid_contract(build):
    r = build(Intent.FIND_PERSON).answer(Q)
    RAGResult.model_validate(r.model_dump())
    assert normalise_question("  where’s  room 874 ") == "where's room 874"
