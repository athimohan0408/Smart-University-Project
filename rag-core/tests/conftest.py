"""Shared fixtures built in memory from the raw sheet, so tests don't need `just index` artifacts.

``make_retriever`` gives a Retriever over those facts with a hashed bag-of-words stand-in for the
embedding model and dense index, and the real BM25 built in memory: offline and deterministic.
"""

import hashlib
import math
import re

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.kb.facts import FactContext, build_facts
from rag_core.kb.glossary import load_glossary
from rag_core.kb.ingest_faculty import ingest
from rag_core.kb.models import FacultyKB
from rag_core.nlu.gazetteer import build_gazetteer, load_lexicon
from rag_core.retrieval.bm25 import BM25Index
from rag_core.retrieval.retriever import FactCatalog, Retriever, topic_faculty

DIM = 64


def hashed_vector(text: str) -> list[float]:
    vec = [0.0] * DIM
    for word in re.findall(r"[a-z0-9]+", text.casefold()):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeEmbedder:
    model_name = "fake"

    def embed_query(self, text):
        return hashed_vector(text)


class FakeDense:
    def __init__(self, facts):
        self._facts = facts
        self._vectors = {f.fact_id: hashed_vector(f.text) for f in facts}

    @staticmethod
    def _cos(a, b):
        return sum(x * y for x, y in zip(a, b))

    def query(self, vector, n, categories):
        pool = [f for f in self._facts if categories is None or f.category.value in categories]
        ranked = sorted(pool, key=lambda f: (-self._cos(vector, self._vectors[f.fact_id]), f.fact_id))
        return [(f.fact_id, self._cos(vector, self._vectors[f.fact_id])) for f in ranked[:n]]

    def similarities(self, vector, fact_ids):
        return {fid: self._cos(vector, self._vectors[fid]) for fid in fact_ids}


@pytest.fixture(scope="session")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


# The fixtures below load settings themselves: some modules override `settings` at module scope.


@pytest.fixture(scope="session")
def entity_kb():
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    records = ingest(settings.paths.raw_faculty_xlsx, settings.kb)
    kb = FacultyKB(source="data/raw/faculty.xlsx", source_sha256="test", sheet=settings.kb.faculty_sheet, faculty=records)
    glossary = load_glossary(settings.paths.glossary)
    ctx = FactContext(
        glossary=glossary,
        glossary_source="data/kb/glossary.yaml",
        home_block=settings.kb.home_block,
        home_floor=settings.kb.home_floor,
        include_unverified_expansions=settings.facts.include_unverified_expansions,
    )
    return kb, glossary, build_facts(kb, ctx)


@pytest.fixture(scope="session")
def gazetteer(entity_kb):
    kb, glossary, facts = entity_kb
    lexicon = load_lexicon(load_settings(DEFAULT_SETTINGS_PATH).paths.entity_lexicon)
    return build_gazetteer(kb, glossary, lexicon, facts)


@pytest.fixture(scope="session")
def make_retriever(entity_kb, gazetteer):
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    _, _, facts = entity_kb
    bm25 = BM25Index.build(
        [f.fact_id for f in facts],
        [" ".join([f.text, *f.keywords]) for f in facts],
        k1=settings.retrieval.bm25_k1,
        b=settings.retrieval.bm25_b,
    )
    catalog = FactCatalog(facts, topic_faculty(gazetteer))

    def make(reranker=None):
        return Retriever(catalog, FakeDense(facts), bm25, FakeEmbedder(), settings.retrieval, reranker=reranker)

    return make
