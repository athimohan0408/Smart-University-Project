"""QA gold set hygiene (offline), metric definitions, and the CI-style regression check (model)."""

from collections import Counter

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.eval.qa import contains, contradicted_values, is_correct, load_qa, normalise
from rag_core.eval.run_eval import HEADLINE, evaluate, load_baseline, regressions
from rag_core.nlu.intent import load_examples


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def qa(settings):
    return load_qa(settings.paths.eval_testset)


def test_gold_set_shape(qa):
    assert len(qa) >= 150
    assert len({q.id for q in qa}) == len(qa) and len({q.question.casefold() for q in qa}) == len(qa)
    assert sum(not q.answerable for q in qa) >= 25
    assert sum(q.difficulty == "adversarial" for q in qa) >= 10
    per_category = Counter(q.category for q in qa)
    assert min(per_category.values()) >= 5, per_category
    assert {q.gold_intent for q in qa} >= set(type(qa[0].gold_intent))  # every intent covered


# The 120-question test split is frozen: new cases go to dev only. Fingerprint of its rows, canonical JSON.
FROZEN_TEST_SHA256 = "b276955e84e5fef049ba2c211111d12cecb6e053facc99454857b6bfeeec2878"


def test_test_split_is_frozen(settings):
    import hashlib
    import json

    rows = [json.loads(line) for line in settings.paths.eval_testset.read_text(encoding="utf-8").splitlines() if line.strip()]
    test = [r for r in rows if r["split"] == "test"]
    assert len(test) == 120
    digest = hashlib.sha256("".join(json.dumps(r, sort_keys=True) for r in test).encode()).hexdigest()
    assert digest == FROZEN_TEST_SHA256, "the frozen test split changed: add new questions to dev instead"


def test_gold_ids_exist(qa, entity_kb, gazetteer):
    _, _, facts = entity_kb
    fact_ids = {f.fact_id for f in facts}
    known = {e.canonical_id for e in gazetteer.entries}
    for q in qa:
        assert set(q.gold_fact_ids) <= fact_ids, q.id
        for g in q.gold_entities:
            for cid in g.ambiguous or [g.id]:
                assert cid is None or cid.startswith(("day:", "time:")) or cid in known, (q.id, cid)


def test_independent_of_the_intent_seed_sets(qa, settings):
    seeds = {e.text.casefold().strip() for p in (settings.paths.intent_train, settings.paths.intent_test) for e in load_examples(p)}
    assert [q.question for q in qa if q.question.casefold().strip() in seeds] == []


def test_keypoint_matching_normalises_times_and_case():
    assert normalise("Monday 1:45 PM to 3:45 pm.") == "monday 13:45 to 15:45"
    assert contains("Her hours are 1:45 pm to 2:45 pm", "13:45")
    assert contains("Room 607A, Block II", "607a") and not contains("room 8740", "874")
    assert contradicted_values("Her email is x@y.in, room 900.", "room 874 email z@y.in") == ["x@y.in", "900"]


def test_grading_rules(qa):
    q = next(x for x in qa if x.forbidden and x.answerable)
    assert not is_correct(q, " ".join(q.gold_answer_keypoints + q.forbidden), escalated=False)
    unanswerable = next(x for x in qa if not x.answerable and not x.forbidden)
    assert is_correct(unanswerable, "anything", escalated=True)
    assert not is_correct(unanswerable, "anything", escalated=False)


def test_regression_rule():
    base = {"qa_accuracy": 0.90, "hallucination_rate": 0.01}
    assert regressions({"qa_accuracy": 0.885, "hallucination_rate": 0.02}, base, 0.02) == []
    assert len(regressions({"qa_accuracy": 0.87, "hallucination_rate": 0.04}, base, 0.02)) == 2


@pytest.mark.model
@pytest.mark.parametrize("split", ["dev", "test"])
def test_no_answer_is_about_the_wrong_person(settings, split):
    """Safety gate, zero tolerance: no answered question may be about a different faculty member than the
    gold one (the "Dr John Mathew" -> Dr Tegil J John bug). A check only; nothing is tuned on it."""
    if not settings.paths.chroma_dir.exists() or not settings.paths.intent_model.exists():
        pytest.skip("no index or intent model: run `just index` and `just train-intent`")
    run = evaluate(settings, split=split)
    wrong = [(r.ex.id, r.ex.question, r.result.answer) for r in run.rows if r.wrong_person]
    assert wrong == []


@pytest.mark.model
def test_no_headline_metric_regressed(settings):
    """CI-style: fails if a headline metric is > eval.max_regression worse than reports/baseline.json."""
    baseline = load_baseline(settings.paths.eval_baseline)
    if baseline is None:
        pytest.skip("no reports/baseline.json: run `just eval-baseline`")
    if not settings.paths.chroma_dir.exists() or not settings.paths.intent_model.exists():
        pytest.skip("no index or intent model: run `just index` and `just train-intent`")
    run = evaluate(settings, split="test")
    assert set(run.headline()) == set(HEADLINE)
    bad = regressions(run.headline(), baseline, settings.eval.max_regression)
    assert bad == [], "regressed vs reports/baseline.json: " + "; ".join(bad)
