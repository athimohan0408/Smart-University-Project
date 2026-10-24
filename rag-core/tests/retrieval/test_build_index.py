import hashlib
import json

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings

from rag_core.config import DEFAULT_SETTINGS_PATH, EmbeddingProfile, EmbeddingSettings, load_settings
from rag_core.kb.facts import FactCategory, FactContext, build_facts
from rag_core.kb.glossary import load_glossary
from rag_core.kb.ingest_faculty import ingest
from rag_core.kb.models import FacultyKB
from rag_core.retrieval import bm25 as bm25_module
from rag_core.retrieval.bm25 import BM25Index
from rag_core.retrieval.build_index import build_index
from rag_core.retrieval.embeddings import Embedder


class FakeEmbedder:
    """Deterministic, offline stand-in for the sentence-transformer."""

    model_name = "fake-embedder"
    dimension = 8

    def embed_passages(self, texts):
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([b / 255 + 0.01 for b in digest[: self.dimension]])
        return vectors


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def facts(settings):
    records = ingest(settings.paths.raw_faculty_xlsx, settings.kb)
    kb = FacultyKB(source="data/raw/faculty.xlsx", source_sha256="test", sheet="s", faculty=records)
    ctx = FactContext(
        glossary=load_glossary(settings.paths.glossary),
        glossary_source="data/kb/glossary.yaml",
        home_block=settings.kb.home_block,
        home_floor=settings.kb.home_floor,
        include_unverified_expansions=True,
    )
    return build_facts(kb, ctx)


def _build(tmp_path, facts, settings):
    return build_index(
        facts,
        FakeEmbedder(),
        chroma_dir=tmp_path / "chroma",
        collection="test_facts",
        distance="cosine",
        bm25_path=tmp_path / "bm25.pkl",
        facts_path=tmp_path / "facts.jsonl",
        bm25_k1=settings.retrieval.bm25_k1,
        bm25_b=settings.retrieval.bm25_b,
    )


def _collection(tmp_path):
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"), settings=ChromaSettings(anonymized_telemetry=False))
    return client.get_collection("test_facts")


def test_rebuild_is_idempotent(tmp_path, facts, settings):
    first = _build(tmp_path, facts, settings)
    jsonl_first = (tmp_path / "facts.jsonl").read_bytes()
    second = _build(tmp_path, facts, settings)

    assert (tmp_path / "facts.jsonl").read_bytes() == jsonl_first
    assert first.digest == second.digest
    col = _collection(tmp_path)
    assert col.count() == len(facts)  # dropped and recreated, not appended
    assert sorted(col.get(include=[])["ids"]) == sorted(f.fact_id for f in facts)
    assert col.metadata["facts_digest"] == second.digest
    assert col.metadata["embedding_model"] == "fake-embedder"


def test_facts_jsonl_and_chroma_metadata(tmp_path, facts, settings):
    _build(tmp_path, facts, settings)
    lines = [json.loads(line) for line in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["fact_id"] for line in lines] == [f.fact_id for f in facts]
    assert sum(line["category"] == FactCategory.FACULTY_LOCATION for line in lines) == 36

    stored = _collection(tmp_path).get(ids=["faculty:nisha-varghese:location"], include=["metadatas", "documents"])
    meta = stored["metadatas"][0]
    assert meta["category"] == "faculty_location" and meta["room"] == "874" and meta["verified"] is True
    assert stored["documents"][0].startswith("Dr Nisha Varghese's office is room 874")


def test_bm25_index_round_trips_and_ranks(tmp_path, facts, settings):
    _build(tmp_path, facts, settings)
    index = BM25Index.load(tmp_path / "bm25.pkl")
    assert index.fact_ids == [f.fact_id for f in facts]
    assert index.search("Where is Nisha Varghese's office?", 1)[0][0] == "faculty:nisha-varghese:location"
    assert index.search("Father Jossy office", 3)[0][0].startswith("faculty:jossy-p-george")  # via alias keywords


def test_bm25_rejects_a_different_tokenizer(tmp_path, monkeypatch):
    path = tmp_path / "bm25.pkl"
    BM25Index.build(["a"], ["room 871"], k1=1.5, b=0.75).save(path)
    monkeypatch.setattr(bm25_module, "TOKENIZER_VERSION", bm25_module.TOKENIZER_VERSION + 1)
    with pytest.raises(ValueError, match="tokenizer"):
        BM25Index.load(path)


def test_embedder_applies_query_and_passage_prefixes():
    seen = []

    def encode(texts):
        seen.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    embedder = Embedder(EmbeddingProfile(name="m", query_prefix="Q: ", passage_prefix="P: "), encode, dimension=2)
    embedder.embed_query("where is room 871")
    embedder.embed_passages(["Room 871 is in Central Block."])
    assert seen == ["Q: where is room 871", "P: Room 871 is in Central Block."]


def test_bge_and_minilm_profiles(settings):
    models = settings.embeddings.models
    assert settings.embeddings.active.name == "BAAI/bge-small-en-v1.5"
    assert models["bge-small"].query_prefix.startswith("Represent this sentence")
    assert models["bge-small"].passage_prefix == ""
    assert models["minilm"].name == "sentence-transformers/all-MiniLM-L6-v2"
    assert models["minilm"].query_prefix == models["minilm"].passage_prefix == ""
    switched = settings.embeddings.model_copy(update={"model": "minilm"})
    assert switched.active.name == "sentence-transformers/all-MiniLM-L6-v2"


def test_unknown_embedding_profile_is_rejected(settings):
    with pytest.raises(ValueError, match="not one of"):
        EmbeddingSettings(**{**settings.embeddings.model_dump(), "model": "nope"})
