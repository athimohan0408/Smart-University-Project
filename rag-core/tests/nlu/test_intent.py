"""Unit tests for the intent layers with a fake embedder and a tiny model (no downloads, no LLM)."""

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression

from rag_core.config import DEFAULT_SETTINGS_PATH, EmbeddingProfile, load_settings
from rag_core.contracts import Intent
from rag_core.nlu.intent import (
    INTENT_DESCRIPTIONS,
    IntentClassifier,
    IntentModel,
    IntentModelMismatch,
    llm_system_prompt,
    load_examples,
    match_rule,
    parse_llm_intent,
)

LABELS = list(Intent)
UNSURE = "something vague"
SCALE = 5.0  # one-hot length used both to train the tiny model and by the fake embedder


class FakeEmbedder:
    """'<intent value>' -> scaled one-hot vector for that intent; anything else -> zeros (model unsure)."""

    profile = EmbeddingProfile(name="fake-embedder")

    def embed_queries(self, texts):
        vectors = []
        for text in texts:
            vec = np.zeros(len(LABELS))
            if text in {i.value for i in LABELS}:
                vec[[i.value for i in LABELS].index(text)] = SCALE
            vectors.append(vec.tolist())
        return vectors


class FakeLLM:
    def __init__(self, reply="", error=None):
        self.reply, self.error, self.calls = reply, error, []

    def complete(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.reply


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def tiny_model():
    X = np.eye(len(LABELS)) * SCALE
    y = [i.value for i in LABELS]
    clf = LogisticRegression(C=100, max_iter=5000).fit(X, y)
    return IntentModel(
        classifier=clf, embedding_model="fake-embedder", query_prefix="", c=100,
        cv_accuracy=1.0, cv_by_c={100: 1.0}, train_examples=len(LABELS), train_sha256="test",
    )


def _classifier(settings, model, llm=None, **cfg):
    return IntentClassifier(model, FakeEmbedder(), settings.intent.model_copy(update=cfg), llm)


# --------------------------------------------------------------------------- rules


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Hello!", Intent.GREETING),
        ("good morning madam", Intent.GREETING),
        ("hii there", Intent.GREETING),
        ("Thank you so much!", Intent.SMALLTALK_OR_THANKS),
        ("ok bye", Intent.SMALLTALK_OR_THANKS),
        ("I want to talk to a human", Intent.ESCALATE_TO_HUMAN),
        ("connect me to the receptionist", Intent.ESCALATE_TO_HUMAN),
        ("receptionist please", Intent.ESCALATE_TO_HUMAN),
        ("I don't want to talk to a machine", Intent.ESCALATE_TO_HUMAN),
    ],
)
def test_rules_fire_on_unambiguous_text(text, intent):
    assert match_rule(text).intent == intent


@pytest.mark.parametrize(
    "text",
    [
        "hi, where is room 874?",  # greeting + a real question: the model decides
        "hello can you tell me nisha mam cabin",
        "thanks, and who teaches ML?",
        "I want to talk to Dr Nisha Varghese",  # a specific person is not "a human"
        "speak to someone who teaches machine learning",
        "can i get the email of deepa mam",
    ],
)
def test_rules_stay_out_of_real_questions(text):
    assert match_rule(text) is None


def test_rules_agree_with_every_training_label(settings):
    for example in load_examples(settings.paths.intent_train):
        rule = match_rule(example.text)
        assert rule is None or rule.intent == example.intent, (example.text, rule.name)


# --------------------------------------------------------------------------- LLM prompt + parsing


def test_prompt_lists_every_intent_and_demands_json():
    prompt = llm_system_prompt()
    assert set(INTENT_DESCRIPTIONS) == set(Intent)
    assert all(i.value in prompt for i in Intent)
    assert "JSON only" in prompt


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"intent": "find_room"}', Intent.FIND_ROOM),
        ('```json\n{"intent": "contact_info"}\n```', Intent.CONTACT_INFO),
        ('Sure! {"intent": " event_info "}', Intent.EVENT_INFO),
        ('{"intent": "weather"}', None),
        ("find_room", None),
        ('{"label": "find_room"}', None),
        ("", None),
        ("{not json}", None),
    ],
)
def test_parse_llm_intent(reply, expected):
    assert parse_llm_intent(reply) == expected


# --------------------------------------------------------------------------- classifier layers


def test_confident_model_prediction(settings, tiny_model):
    pred = _classifier(settings, tiny_model).predict("contact_info")
    assert (pred.intent, pred.source) == (Intent.CONTACT_INFO, "model")
    assert pred.probability > settings.intent.llm_fallback_threshold
    assert len(pred.top) == settings.intent.top_n
    assert pred.top[0].intent == Intent.CONTACT_INFO
    assert [s.probability for s in pred.top] == sorted((s.probability for s in pred.top), reverse=True)


def test_query_vector_is_reused(settings, tiny_model):
    vector = FakeEmbedder().embed_queries(["find_room"])[0]
    assert _classifier(settings, tiny_model).predict("ignored text", query_vector=vector).intent == Intent.FIND_ROOM


def test_rule_overrides_model_and_keeps_a_distribution(settings, tiny_model):
    pred = _classifier(settings, tiny_model).predict("hello")  # embeds to zeros: the model is unsure
    assert (pred.intent, pred.source, pred.rule) == (Intent.GREETING, "rule", "greeting_only")
    assert pred.probability == settings.intent.rule_confidence
    assert pred.top[0].intent == Intent.GREETING
    assert pred.model_intent != Intent.GREETING or pred.model_probability < 1


def test_llm_fallback_used_when_model_is_unsure(settings, tiny_model):
    llm = FakeLLM('{"intent": "event_info"}')
    pred = _classifier(settings, tiny_model, llm, llm_fallback_enabled=True).predict(UNSURE)
    assert (pred.intent, pred.source, pred.llm_attempted) == (Intent.EVENT_INFO, "llm", True)
    assert pred.probability == settings.intent.llm_fallback_confidence
    assert pred.model_probability < settings.intent.llm_fallback_threshold
    assert llm.calls and llm.calls[0][1] == UNSURE


def test_llm_not_called_when_model_is_confident(settings, tiny_model):
    llm = FakeLLM('{"intent": "event_info"}')
    pred = _classifier(settings, tiny_model, llm, llm_fallback_enabled=True).predict("find_room")
    assert pred.source == "model" and not llm.calls


def test_llm_fallback_switch(settings, tiny_model):
    llm = FakeLLM('{"intent": "event_info"}')
    clf = _classifier(settings, tiny_model, llm, llm_fallback_enabled=False)
    assert clf.predict(UNSURE).source == "model" and not llm.calls
    assert clf.predict(UNSURE, use_llm=True).source == "llm"  # per-call override, used by eval


@pytest.mark.parametrize("llm", [FakeLLM(error=TimeoutError("slow")), FakeLLM("I think it's about events")])
def test_llm_failure_keeps_the_model_answer(settings, tiny_model, llm):
    pred = _classifier(settings, tiny_model, llm, llm_fallback_enabled=True).predict(UNSURE)
    assert pred.source == "model" and pred.llm_attempted
    assert pred.intent == pred.model_intent


# --------------------------------------------------------------------------- artifact


def test_artifact_round_trip(tmp_path, tiny_model):
    path = tmp_path / "intent.joblib"
    tiny_model.save(path)
    assert IntentModel.load(path).labels == tiny_model.labels


def test_model_for_another_embedding_model_is_rejected(settings, tiny_model):
    other = IntentModel(**{**tiny_model.__dict__, "embedding_model": "BAAI/bge-small-en-v1.5"})
    with pytest.raises(IntentModelMismatch, match="train-intent"):
        _classifier(settings, other)


def test_model_with_stale_intents_is_rejected(settings):
    stale = LogisticRegression().fit(np.eye(2), ["find_person", "faculty_location"])
    model = IntentModel(stale, "fake-embedder", "", 1.0, 1.0, {1.0: 1.0}, 2, "x")
    with pytest.raises(IntentModelMismatch, match="intents changed"):
        _classifier(settings, model)


def test_missing_artifact_says_how_to_fix(tmp_path):
    with pytest.raises(IntentModelMismatch, match="just train-intent"):
        IntentModel.load(tmp_path / "missing.joblib")
