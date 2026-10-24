"""The labelled intent data: coverage per intent and no leakage from the held-out set."""

from collections import Counter

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.contracts import Intent
from rag_core.nlu.intent import load_examples, normalise


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def train(settings):
    return load_examples(settings.paths.intent_train)


@pytest.fixture(scope="module")
def test(settings):
    return load_examples(settings.paths.intent_test)


def test_every_intent_has_training_examples(train):
    counts = Counter(e.intent for e in train)
    assert {i: counts[i] for i in Intent if counts[i] < 25} == {}


def test_every_intent_has_held_out_examples(test):
    counts = Counter(e.intent for e in test)
    assert {i: counts[i] for i in Intent if counts[i] < 10} == {}


def test_no_held_out_question_appears_in_training(train, test):
    seen = {normalise(e.text) for e in train}
    assert [e.text for e in test if normalise(e.text) in seen] == []


def test_no_duplicate_training_questions(train):
    counts = Counter(normalise(e.text) for e in train)
    assert [t for t, n in counts.items() if n > 1] == []
