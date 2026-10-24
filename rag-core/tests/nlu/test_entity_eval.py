"""The gold entity set is well-formed, and extraction meets the target on it. Writes reports/entity_eval.md."""

import re
from collections import Counter

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.contracts import EntityType
from rag_core.nlu.entities import EntityExtractor
from rag_core.nlu.eval_entities import load_entity_examples, run_evaluation

CLOCK_WORDS = re.compile(r"\b(now|today|tomorrow|tonight|yesterday|at the moment|at present)\b", re.IGNORECASE)


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def examples(settings):
    return load_entity_examples(settings.paths.entity_test)


def test_gold_ids_exist(examples, gazetteer):
    known = {e.canonical_id for e in gazetteer.entries}
    for ex in examples:
        for gold in ex.entities:
            for cid in gold.ambiguous or [gold.id]:
                if cid is None or cid.startswith(("day:", "time:")):
                    continue
                assert cid in known, (ex.text, cid)


def test_clock_dependent_questions_pin_the_clock(examples):
    assert [ex.text for ex in examples if CLOCK_WORDS.search(ex.text) and ex.now is None] == []


def test_gold_set_covers_every_type_and_ambiguity(examples):
    counts = Counter(g.type for ex in examples for g in ex.entities)
    wanted = {EntityType.FACULTY, EntityType.COURSE, EntityType.TOPIC, EntityType.ROOM, EntityType.PROGRAMME,
              EntityType.DAY, EntityType.TIME, EntityType.EVENT, EntityType.FACILITY}
    assert {t for t in wanted if counts[t] < 5} == set()
    assert sum(bool(g.ambiguous) for ex in examples for g in ex.entities) >= 2
    assert any(not ex.entities for ex in examples)  # questions with nothing to find
    assert len({ex.text.casefold() for ex in examples}) == len(examples)


def test_entity_f1_and_ambiguity(settings, gazetteer):
    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    report_path, result = run_evaluation(settings, extractor)
    _, _, f1 = result.micro
    total, flagged, silent = result.ambiguity

    assert "## Precision / recall / F1 by type" in report_path.read_text(encoding="utf-8")
    assert f1 >= settings.entities.target_f1, f"entity F1 {f1:.3f} < {settings.entities.target_f1}; see {report_path}"
    assert (flagged, silent) == (total, 0), f"ambiguous names: {flagged}/{total} flagged, {silent} silently picked"
