"""Build the retrieval indexes from the KB: facts.jsonl, Chroma (dense) and BM25 (lexical), plus
the entity-linking gazetteer (rag_core.nlu.gazetteer), which carries the same facts digest.

Run: ``python -m rag_core.retrieval.build_index``  (or ``just index``, which runs ingest first)

Idempotent: the Chroma collection is dropped and recreated, and bm25.pkl / facts.jsonl are
replaced atomically. Fact ids come only from the data, so the same input gives the same ids. All
three outputs carry the same facts digest, so a stale or mixed index can be detected.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError

from rag_core.config import PROJECT_ROOT, Settings, get_settings
from rag_core.kb.facts import (
    CATEGORIES_WITHOUT_SOURCE,
    Fact,
    FactCategory,
    FactContext,
    build_facts,
    facts_digest,
)
from rag_core.kb.glossary import load_glossary
from rag_core.kb.models import FacultyKB
from rag_core.retrieval.bm25 import BM25Index


class PassageEmbedder(Protocol):
    model_name: str

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class BuildResult:
    facts: list[Fact]
    digest: str
    chroma_count: int
    timings_s: dict[str, float] = field(default_factory=dict)


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def load_kb(settings: Settings) -> FacultyKB:
    path = settings.paths.faculty_json
    if not path.exists():
        raise SystemExit(f"{_display(path)} not found: run `just ingest` first")
    return FacultyKB.model_validate_json(path.read_text(encoding="utf-8"))


def fact_context(settings: Settings) -> FactContext:
    return FactContext(
        glossary=load_glossary(settings.paths.glossary),
        glossary_source=_display(settings.paths.glossary),
        home_block=settings.kb.home_block,
        home_floor=settings.kb.home_floor,
        include_unverified_expansions=settings.facts.include_unverified_expansions,
    )


def load_facts(settings: Settings) -> list[Fact]:
    return build_facts(load_kb(settings), fact_context(settings))


def bm25_document(fact: Fact) -> str:
    return " ".join([fact.text, *fact.keywords])


def write_facts_jsonl(facts: Sequence[Fact], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for fact in facts:
            fh.write(fact.model_dump_json() + "\n")
    os.replace(tmp, path)


def write_chroma(
    facts: Sequence[Fact],
    embeddings: Sequence[Sequence[float]],
    *,
    chroma_dir: Path,
    collection: str,
    distance: str,
    model_name: str,
    digest: str,
) -> int:
    """Drop + recreate the collection, then add every fact. Returns the stored count."""
    client = chromadb.PersistentClient(path=str(chroma_dir), settings=ChromaSettings(anonymized_telemetry=False))
    try:
        client.delete_collection(collection)
    except NotFoundError:
        pass
    col = client.create_collection(
        collection,
        configuration={"hnsw": {"space": distance}},
        metadata={"embedding_model": model_name, "facts_digest": digest, "fact_count": len(facts)},
        embedding_function=None,  # we always pass our own embeddings
    )
    batch = client.get_max_batch_size()
    for start in range(0, len(facts), batch):
        chunk = facts[start : start + batch]
        col.add(
            ids=[f.fact_id for f in chunk],
            embeddings=[list(v) for v in embeddings[start : start + batch]],
            documents=[f.text for f in chunk],
            metadatas=[f.chroma_metadata() for f in chunk],
        )
    return col.count()


def build_index(
    facts: Sequence[Fact],
    embedder: PassageEmbedder,
    *,
    chroma_dir: Path,
    collection: str,
    distance: str,
    bm25_path: Path,
    facts_path: Path,
    bm25_k1: float,
    bm25_b: float,
) -> BuildResult:
    timings: dict[str, float] = {}
    facts = list(facts)
    digest = facts_digest(facts)

    start = time.perf_counter()
    embeddings = embedder.embed_passages([f.text for f in facts])  # before any write: no half-built index
    timings["embed"] = time.perf_counter() - start

    start = time.perf_counter()
    count = write_chroma(
        facts, embeddings, chroma_dir=chroma_dir, collection=collection, distance=distance,
        model_name=embedder.model_name, digest=digest,
    )
    if count != len(facts):
        raise RuntimeError(f"Chroma holds {count} facts, expected {len(facts)}")
    timings["chroma"] = time.perf_counter() - start

    start = time.perf_counter()
    BM25Index.build([f.fact_id for f in facts], [bm25_document(f) for f in facts], bm25_k1, bm25_b).save(bm25_path)
    timings["bm25"] = time.perf_counter() - start

    start = time.perf_counter()
    write_facts_jsonl(facts, facts_path)
    timings["facts_jsonl"] = time.perf_counter() - start
    return BuildResult(facts=facts, digest=digest, chroma_count=count, timings_s=timings)


def main() -> None:
    from rag_core.nlu.gazetteer import build_gazetteer, load_lexicon, save_gazetteer
    from rag_core.retrieval.embeddings import Embedder

    settings = get_settings()
    paths = settings.paths
    t0 = time.perf_counter()

    kb, ctx = load_kb(settings), fact_context(settings)
    facts = build_facts(kb, ctx)
    t_facts = time.perf_counter() - t0

    start = time.perf_counter()
    embedder = Embedder.from_settings(settings.embeddings)
    t_model = time.perf_counter() - start

    result = build_index(
        facts,
        embedder,
        chroma_dir=paths.chroma_dir,
        collection=settings.vector_store.collection,
        distance=settings.vector_store.distance,
        bm25_path=paths.bm25_index,
        facts_path=paths.facts_jsonl,
        bm25_k1=settings.retrieval.bm25_k1,
        bm25_b=settings.retrieval.bm25_b,
    )
    # Entity-linking vocabulary from the same facts (room/course/programme ids) + KB + lexicon.
    gazetteer = build_gazetteer(kb, ctx.glossary, load_lexicon(paths.entity_lexicon), result.facts)
    save_gazetteer(gazetteer, paths.gazetteer)
    total = time.perf_counter() - t0

    counts = Counter(f.category for f in result.facts)
    print(f"facts: {len(result.facts)}  (digest {result.digest[:12]})")
    for category in FactCategory:
        note = "  (no source data yet)" if category in CATEGORIES_WITHOUT_SOURCE and not counts[category] else ""
        print(f"  {category.value:<22}{counts[category]:>5}{note}")
    print(f"unverified facts: {sum(not f.verified for f in result.facts)}")
    print(f"embedding model: {embedder.model_name} (dim {embedder.dimension}, profile '{settings.embeddings.model}')")
    print(
        f"wrote: {_display(paths.facts_jsonl)}, {_display(paths.bm25_index)}, "
        f"{_display(paths.chroma_dir)} [{settings.vector_store.collection}: {result.chroma_count}], "
        f"{_display(paths.gazetteer)} [{len(gazetteer.entries)} entities]"
    )
    t = result.timings_s
    print(
        f"build time: {total:.1f}s  (facts {t_facts:.2f}s, model load {t_model:.1f}s, embed {t['embed']:.1f}s, "
        f"chroma {t['chroma']:.2f}s, bm25 {t['bm25']:.2f}s, facts.jsonl {t['facts_jsonl']:.2f}s)"
    )


if __name__ == "__main__":
    main()
