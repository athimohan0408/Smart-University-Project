"""Query-time retrieval: structured routes, RRF, category filter, rerank order and signals.

Offline: facts come from the raw sheet, dense search is a hashed bag-of-words stand-in, and BM25 is
the real index built in memory. The model-backed eval at the end needs `just index`.
"""

import pytest

from rag_core.contracts import Entity, EntityType, Intent
from rag_core.nlu.entities import Candidate, EntityMention
from rag_core.retrieval.eval_retrieval import TARGET_CONFIG, load_retrieval_examples, run_evaluation
from rag_core.retrieval.retriever import ROUTES, RetrievalOptions, reciprocal_rank_fusion


class FakeReranker:
    """Prefers passages that mention 'Saturday', to show rerank can't push past structured hits."""

    def score(self, query, passages):
        return [0.99 if "Saturday" in p else 0.1 for p in passages]


@pytest.fixture(scope="module")
def retriever(make_retriever):
    return make_retriever(reranker=FakeReranker())


def _faculty(fid):
    return Entity(type=EntityType.FACULTY, text=fid, canonical_id=f"faculty:{fid}", score=1.0)


def _ent(type_, cid):
    return Entity(type=type_, text=cid, canonical_id=cid, score=1.0)


def test_rrf_is_normalised_and_rewards_agreement():
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"], ["a", "c"]], k=60))
    assert fused["a"] == pytest.approx(1.0)  # first in both rankings
    assert fused["c"] > fused["b"]  # found by both beats found by one
    assert all(0 < s <= 1 for s in fused.values())
    assert reciprocal_rank_fusion([], k=60) == []


def test_find_person_pulls_location_and_profile_first(retriever):
    result = retriever.search("where is Dr Nisha Varghese's cabin", Intent.FIND_PERSON, [_faculty("nisha-varghese")])
    head = result.facts[:2]
    assert {f.fact_id for f in head} == {"faculty:nisha-varghese:location", "faculty:nisha-varghese:profile"}
    assert all(f.via == "structured" and f.score == 1.0 for f in head)
    assert [f.rank for f in result.facts] == list(range(1, len(result.facts) + 1))
    # then, at lower priority, the consultation hours ("is Dr X inside?" is asked as find_person too)
    assert result.facts[2].fact_id == "faculty:nisha-varghese:consultation" and result.facts[2].via == "structured"
    assert all(f.via != "structured" for f in result.facts[3:])
    assert result.signals.structured_hits == 3 and result.signals.top1_score == 1.0


def test_find_by_subject_pulls_the_course_and_its_teaching_facts(retriever):
    facts = retriever.retrieve("who teaches ML", Intent.FIND_BY_SUBJECT, [_ent(EntityType.COURSE, "course:ml")])
    structured = [f.fact_id for f in facts if f.via == "structured"]
    assert structured[0] == "course:ml"  # tier 0 before the tier-1 teaching facts
    assert all(fid == "course:ml" or (":teaching:ml" in fid) for fid in structured)


def test_availability_with_a_day_pulls_consultation_and_that_days_schedule(retriever):
    facts = retriever.retrieve(
        "is Dr Deepa Jose free on Friday",
        Intent.PERSON_AVAILABILITY,
        [_faculty("deepa-v-jose"), _ent(EntityType.DAY, "day:friday")],
    )
    structured = [f.fact_id for f in facts if f.via == "structured"]
    assert set(structured[:2]) == {"faculty:deepa-v-jose:consultation", "faculty:deepa-v-jose:schedule:friday"}
    assert structured[2:] == ["faculty:deepa-v-jose:location"]  # lower tier: where to find her


def test_ambiguous_mention_routes_to_every_candidate(retriever):
    george = EntityMention(
        type=EntityType.FACULTY, text="George", start=8, end=14, method="fuzzy", score=0.9, ambiguous=True,
        candidates=[
            Candidate(canonical_id="faculty:jossy-p-george", name="Dr Jossy P George", score=0.9, alias="george"),
            Candidate(canonical_id="faculty:naived-george-eapen", name="Dr Naived George Eapen", score=0.9, alias="george"),
        ],
    )
    facts = retriever.retrieve("where is George", Intent.FIND_PERSON, [george])
    structured = {f.fact_id for f in facts if f.via == "structured"}
    assert {"faculty:jossy-p-george:location", "faculty:naived-george-eapen:location"} <= structured


def test_category_filter_limits_semantic_hits(retriever, settings):
    result = retriever.search("what is Dr Neha Singhal's email", Intent.CONTACT_INFO, [])
    assert result.categories == ["faculty_profile"]
    assert {f.metadata["category"] for f in result.facts} == {"faculty_profile"}


def test_intents_without_kb_data_retrieve_nothing(retriever):
    for intent in (Intent.GREETING, Intent.FACILITY_INFO, Intent.EVENT_INFO, Intent.OUT_OF_SCOPE):
        result = retriever.search("where can I get drinking water", intent, [])
        assert result.facts == [] and result.signals.top1_score == 0.0, intent


def test_rerank_keeps_structured_hits_in_front(retriever):
    entities = [_faculty("deepa-v-jose"), _ent(EntityType.DAY, "day:friday")]
    facts = retriever.retrieve(
        "is Dr Deepa Jose free on Friday", Intent.PERSON_AVAILABILITY, entities, options=RetrievalOptions(rerank=True)
    )
    assert [f.via for f in facts[:3]] == ["structured"] * 3  # consultation, Friday, then location (tier 2)
    assert facts[3].fact_id.endswith(":saturday")  # the fake cross-encoder's favourite comes next
    assert facts[3].score == pytest.approx(0.99)


def test_signals_flag_unverified_facts(retriever):
    result = retriever.search("where is Dr Helen K Joy", Intent.FIND_PERSON, [_faculty("helen-k-joy")])
    assert result.signals.any_unverified  # her location carries a conflict caution
    assert 0.0 <= result.signals.top1_margin <= 1.0


def test_gold_set_is_valid_and_covers_routed_intents(entity_kb, settings):
    _, _, facts = entity_kb
    ids = {f.fact_id for f in facts}
    examples = load_retrieval_examples(settings.paths.retrieval_test)
    assert len(examples) >= 80
    assert [(ex.question, g) for ex in examples for g in ex.gold if g not in ids] == []
    assert {ex.intent for ex in examples} >= set(ROUTES) | {Intent.CONTACT_INFO}
    assert len({ex.question.casefold() for ex in examples}) == len(examples)


@pytest.mark.model
def test_retrieval_eval_meets_target_recall(settings, tmp_path):
    if not settings.paths.chroma_dir.exists():
        pytest.skip("no index: run `just index`")
    # Write to tmp: reports/retrieval_eval.md keeps the full ablation from `just eval-retrieval`.
    paths = settings.paths.model_copy(update={"retrieval_report": tmp_path / "retrieval_eval.md"})
    report_path, results = run_evaluation(settings.model_copy(update={"paths": paths}), with_rerank=False)
    top_k = settings.retrieval.top_k
    recall = results[TARGET_CONFIG].metrics([1], top_k)[f"recall@{top_k}"]
    assert "## Ablation" in report_path.read_text(encoding="utf-8")
    assert recall >= settings.retrieval.target_recall, f"Recall@{top_k} {recall:.3f}; see {report_path}"
