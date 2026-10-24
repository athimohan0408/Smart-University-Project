"""Query-time retrieval: question + intent + entities -> the KB facts to answer from.

1. **Structured routing** (:data:`ROUTES`). When entities are linked to KB ids, the facts about
   exactly those ids are pulled from the fact catalogue: find_person + faculty:nisha-varghese ->
   her location and profile facts; find_by_subject + course:ml -> the ML course fact and every ML
   teaching fact; person_availability + a day -> the consultation fact and that day's schedule.
   These score 1.0, come first and carry ``via="structured"``. Inside that block a more specific
   route (lower tier) comes first, then the fact closer to the question in embedding space. An
   ambiguous mention routes to every candidate: the answer stage asks "did you mean ...?", and
   retrieval never picks one.
2. **Hybrid semantic search**, for everything else and as backup: Chroma dense top-k and BM25
   top-k, both limited to the fact categories that can answer the intent
   (``retrieval.intent_categories``, a Chroma ``where`` filter), merged by reciprocal rank fusion.
3. **Rerank** (optional, ``retrieval.rerank.enabled``): a cross-encoder scores the candidates.
   Structured hits stay in front. The other facts take the cross-encoder probability as score.
4. The top ``retrieval.top_k`` facts come back with score, rank and via, plus
   :class:`RetrievalSignals` for the confidence scorer.

Every score is in [0, 1]. The retriever never drops facts on a score threshold: deciding that the
facts are too weak to answer from is the scorer's job.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import chromadb
import numpy as np
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError
from pydantic import BaseModel, Field, NonNegativeInt

from rag_core.config import RetrievalSettings, Settings, get_settings
from rag_core.contracts import Entity, EntityType, Intent, Probability, RetrievalVia, RetrievedFact
from rag_core.kb.facts import Fact, FactCategory
from rag_core.kb.parsing import slugify
from rag_core.nlu.entities import EntityMention
from rag_core.nlu.gazetteer import Gazetteer, load_gazetteer
from rag_core.retrieval.bm25 import BM25Index
from rag_core.retrieval.rerank import CrossEncoderReranker, Reranker

STRUCTURED_SCORE = 1.0  # an exact KB-id match is the top of the scale


class RetrievalIndexUnavailable(RuntimeError):
    """An index file is missing, or the indexes were built from different facts: run `just index`."""


# --------------------------------------------------------------------------- structured routes


@dataclass(frozen=True)
class Route:
    """Pull the facts of ``category`` about the linked ids of every type in ``needs``.

    Fires only when every ``needs`` type has a linked id and no ``unless`` type has one. A fact
    matches when, for each needed type, it is about at least one of the linked ids of that type.
    """

    category: FactCategory
    needs: frozenset[EntityType]
    unless: frozenset[EntityType] = frozenset()
    tier: int = 0  # lower = more specific, ranked first

    def fires(self, linked: Mapping[EntityType, set[str]]) -> bool:
        return all(linked.get(t) for t in self.needs) and not any(linked.get(t) for t in self.unless)


def _route(category: FactCategory, *needs: EntityType, unless: Iterable[EntityType] = (), tier: int = 0) -> Route:
    return Route(category, frozenset(needs), frozenset(unless), tier)


_C = FactCategory
_FAC, _ROOM, _COURSE, _PROG, _TOPIC, _DAY = (
    EntityType.FACULTY, EntityType.ROOM, EntityType.COURSE, EntityType.PROGRAMME, EntityType.TOPIC, EntityType.DAY,
)

# Intents without routes (greetings, events, facilities, out of scope) get no structured hits.
ROUTES: dict[Intent, tuple[Route, ...]] = {
    Intent.FIND_PERSON: (
        _route(_C.FACULTY_LOCATION, _FAC),
        _route(_C.FACULTY_PROFILE, _FAC),
        _route(_C.ROOM, _ROOM),  # "who sits in room 874"
        _route(_C.FACULTY_CONSULTATION, _FAC, tier=2),  # "is Dr X inside?" is a presence question
        _route(_C.FACULTY_PROFILE, _TOPIC, unless=[_FAC], tier=1),  # "who is the blockchain person"
        _route(_C.COURSE, _COURSE, unless=[_FAC], tier=1),  # "who is the ML teacher"
    ),
    Intent.PERSON_AVAILABILITY: (
        _route(_C.FACULTY_CONSULTATION, _FAC),
        _route(_C.SCHEDULE, _FAC, _DAY),  # classes that day; "now" and "today" carry their day
        _route(_C.FACULTY_LOCATION, _FAC, tier=2),  # "is she in her cabin": where to go if she is
    ),
    Intent.CONTACT_INFO: (
        _route(_C.FACULTY_PROFILE, _FAC),  # the email is in the profile
        _route(_C.FACULTY_LOCATION, _FAC, tier=1),  # visiting the office is a way to contact them too
    ),
    Intent.FIND_BY_SUBJECT: (
        _route(_C.COURSE, _COURSE),  # one fact that lists every teacher
        _route(_C.TEACHING, _COURSE, _FAC),  # "does Dr X teach ML"
        _route(_C.FACULTY_PROFILE, _TOPIC, unless=[_COURSE]),  # research areas
        _route(_C.PROGRAMME, _PROG, unless=[_COURSE, _TOPIC]),  # "who teaches in MSc AIML"
        # "machine learning" links a course and a topic: teachers and researchers compete on similarity
        _route(_C.TEACHING, _COURSE, unless=[_FAC], tier=1),
        _route(_C.FACULTY_PROFILE, _TOPIC, tier=1),
        _route(_C.SCHEDULE, _FAC, _COURSE, tier=1),  # "does / when does Dr X teach OS": the days too
        # "does Dr X work on blockchain": her areas answer a no too; with a course the teaching fact answers
        _route(_C.FACULTY_PROFILE, _FAC, unless=[_COURSE], tier=1),
    ),
    Intent.FIND_ROOM: (
        _route(_C.ROOM, _ROOM),
        _route(_C.FACULTY_LOCATION, _FAC),
        # "where is the MSc AIML classroom / the ML class": these facts say the location isn't listed
        _route(_C.PROGRAMME, _PROG, unless=[_ROOM, _FAC]),
        _route(_C.COURSE, _COURSE, unless=[_ROOM, _FAC]),
    ),
    Intent.SCHEDULE_QUERY: (
        _route(_C.SCHEDULE, _FAC, _DAY),
        _route(_C.SCHEDULE, _FAC, _COURSE, unless=[_DAY]),  # "when does Dr X teach DAA"
        _route(_C.TEACHING, _FAC, _COURSE),
        _route(_C.SCHEDULE, _COURSE, _DAY, unless=[_FAC]),  # "ML classes on Monday"
        _route(_C.SCHEDULE, _PROG, _DAY, unless=[_FAC, _COURSE]),  # "MCA classes on Friday"
        _route(_C.TEACHING, _FAC, unless=[_DAY, _COURSE], tier=1),  # "what does Dr X teach"
        _route(_C.SCHEDULE, _FAC, unless=[_DAY, _COURSE], tier=2),  # the whole week, after what they teach
        _route(_C.COURSE, _COURSE, unless=[_FAC], tier=1),
        _route(_C.TEACHING, _COURSE, unless=[_FAC, _DAY], tier=2),
    ),
    Intent.PROGRAMME_INFO: (
        _route(_C.PROGRAMME, _PROG),
        _route(_C.COURSE, _COURSE),
    ),
}


def linked_ids(entities: Iterable[Entity | EntityMention]) -> dict[EntityType, set[str]]:
    """KB ids per entity type. An ambiguous mention gives all its candidates, and a time that
    carries a day ("now") also gives that day."""
    linked: dict[EntityType, set[str]] = defaultdict(set)
    for e in entities:
        if isinstance(e, EntityMention):
            ids = [c.canonical_id for c in e.candidates] if e.ambiguous else [e.canonical_id]
            if e.time is not None and e.time.day is not None:
                linked[EntityType.DAY].add(f"day:{e.time.day.lower()}")
        else:
            ids = [e.canonical_id]
        linked[e.type].update(i for i in ids if i)
    return dict(linked)


def _split(value: str) -> list[str]:
    return [v for v in value.split(",") if v]


class FactCatalog:
    """The facts in memory, indexed by the entity ids each fact is about.

    Ids follow the gazetteer: ``faculty:<id>`` (the faculty field, so a room fact is about its
    occupants and a teaching fact about its co-teachers too), ``course:<slug>``,
    ``programme:<slug>``, ``day:<weekday>``, ``room:<slug>`` (a room fact and its occupants'
    location facts) and ``topic:<slug>`` (the profiles of the faculty with that research area).
    """

    def __init__(self, facts: Sequence[Fact], topic_faculty: Mapping[str, Iterable[str]] | None = None):
        self._facts: dict[str, Fact] = {f.fact_id: f for f in facts}
        self._position = {fid: n for n, fid in enumerate(self._facts)}
        self._by_category: dict[FactCategory, set[str]] = defaultdict(set)
        self._by_entity: dict[str, set[str]] = defaultdict(set)

        offices = {
            (fac, f.room): f.fact_id
            for f in facts
            if f.category == FactCategory.ROOM
            for fac in _split(f.faculty_id)
        }
        topics_of: dict[str, set[str]] = defaultdict(set)
        for topic, faculty in (topic_faculty or {}).items():
            for fac in faculty:
                topics_of[fac].add(topic)

        for f in facts:
            self._by_category[f.category].add(f.fact_id)
            faculty = _split(f.faculty_id)
            ids = {f"faculty:{x}" for x in faculty}
            ids |= {f"course:{slugify(c)}" for c in _split(f.subject_code)}
            ids |= {f"programme:{slugify(p)}" for p in _split(f.programme)}
            ids |= {f"day:{d.lower()}" for d in _split(f.day)}
            if f.category == FactCategory.ROOM:
                ids.add(f.fact_id)
            elif f.category == FactCategory.FACULTY_LOCATION and f.room:
                ids |= {offices[(x, f.room)] for x in faculty if (x, f.room) in offices}
            if f.category == FactCategory.FACULTY_PROFILE:
                ids |= {t for x in faculty for t in topics_of[x]}
            for eid in ids:
                self._by_entity[eid].add(f.fact_id)

    def __len__(self) -> int:
        return len(self._facts)

    def __contains__(self, fact_id: object) -> bool:
        return fact_id in self._facts

    def get(self, fact_id: str) -> Fact:
        return self._facts[fact_id]

    def position(self, fact_id: str) -> int:
        """Order in facts.jsonl, the final tie-break."""
        return self._position[fact_id]

    def in_categories(self, categories: Collection[FactCategory]) -> set[str]:
        return set().union(*(self._by_category.get(c, set()) for c in categories))

    def route(self, route: Route, linked: Mapping[EntityType, set[str]]) -> set[str]:
        ids = set(self._by_category.get(route.category, set()))
        for type_ in route.needs:
            ids &= set().union(*(self._by_entity.get(eid, set()) for eid in linked.get(type_, ())))
        return ids


def topic_faculty(gazetteer: Gazetteer) -> dict[str, list[str]]:
    """Faculty per research topic. A topic also covers the longer research areas that contain it as whole
    words: "image processing" includes whoever lists "digital image processing"."""
    topics = {e.canonical_id: list(e.faculty_ids) for e in gazetteer.entries if e.type == EntityType.TOPIC}
    out = {}
    for tid, faculty in topics.items():
        slug = tid.split(":", 1)[1]
        wider = [f for other, fs in topics.items() if other != tid and f"-{slug}-" in f"-{other.split(':', 1)[1]}-" for f in fs]
        out[tid] = list(dict.fromkeys([*faculty, *wider]))
    return out


# --------------------------------------------------------------------------- dense index + fusion


class DenseIndex(Protocol):
    def query(self, vector: Sequence[float], n: int, categories: Collection[str] | None) -> list[tuple[str, float]]:
        """Top ``n`` (fact_id, cosine similarity), best first, optionally limited to categories."""
        ...

    def similarities(self, vector: Sequence[float], fact_ids: Collection[str]) -> dict[str, float]:
        """Cosine similarity between the query and each given fact."""
        ...


class ChromaDenseIndex:
    """Dense search over the Chroma collection that build_index writes."""

    def __init__(self, collection, distance: str):
        self._col = collection
        self._distance = distance

    @classmethod
    def open(cls, chroma_dir: Path, name: str, distance: str) -> ChromaDenseIndex:
        client = chromadb.PersistentClient(path=str(chroma_dir), settings=ChromaSettings(anonymized_telemetry=False))
        try:
            return cls(client.get_collection(name), distance)
        except NotFoundError as exc:
            raise RetrievalIndexUnavailable(f"Chroma collection {name!r} not found in {chroma_dir}: run `just index`") from exc

    @property
    def metadata(self) -> dict:
        return dict(self._col.metadata or {})

    def _similarity(self, distance: float) -> float:
        # cosine and ip store 1 - similarity; l2 stores the squared distance, 2 - 2 * cosine for unit vectors
        return 1.0 - distance / 2 if self._distance == "l2" else 1.0 - distance

    def query(self, vector: Sequence[float], n: int, categories: Collection[str] | None) -> list[tuple[str, float]]:
        where = None
        if categories is not None:
            values = sorted(categories)
            where = {"category": values[0]} if len(values) == 1 else {"category": {"$in": values}}
        result = self._col.query(query_embeddings=[list(vector)], n_results=n, where=where, include=["distances"])
        return [(fid, self._similarity(d)) for fid, d in zip(result["ids"][0], result["distances"][0])]

    def similarities(self, vector: Sequence[float], fact_ids: Collection[str]) -> dict[str, float]:
        if not fact_ids:
            return {}
        stored = self._col.get(ids=list(fact_ids), include=["embeddings"])
        query = np.asarray(vector, dtype=float)
        out = {}
        for fid, embedding in zip(stored["ids"], stored["embeddings"]):
            emb = np.asarray(embedding, dtype=float)
            norm = float(np.linalg.norm(query) * np.linalg.norm(emb))
            out[fid] = float(query @ emb) / norm if norm else 0.0
        return out


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int) -> list[tuple[str, float]]:
    """Fuse rankings (fact ids, best first) by RRF: sum of 1 / (k + rank).

    Scores are divided by the best possible sum (first in every ranking), so they lie in (0, 1] and
    1.0 means every retriever ranked the fact first. Ties go to the better single rank, then fact id.
    """
    total: dict[str, float] = defaultdict(float)
    best_rank: dict[str, int] = {}
    for ranking in rankings:
        for rank, fid in enumerate(ranking, start=1):
            total[fid] += 1.0 / (k + rank)
            best_rank[fid] = min(rank, best_rank.get(fid, rank))
    if not total:
        return []
    best = len(rankings) / (k + 1)
    fused = [(fid, min(1.0, score / best)) for fid, score in total.items()]
    return sorted(fused, key=lambda pair: (-pair[1], best_rank[pair[0]], pair[0]))


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class RetrievalOptions:
    """Which stages run. The defaults are what the kiosk runs; the eval ablation switches stages off."""

    dense: bool = True
    bm25: bool = True
    structured: bool = True
    rerank: bool | None = None  # None: retrieval.rerank.enabled
    category_filter: bool = True


class RetrievalSignals(BaseModel):
    """Retrieval evidence for the confidence scorer, computed on the returned facts."""

    top1_score: Probability = 0.0
    top1_margin: Probability = Field(default=0.0, description="Top-1 minus top-2 score (the top-1 score when one fact).")
    structured_hits: NonNegativeInt = Field(default=0, description="Returned facts pulled by exact KB id.")
    any_unverified: bool = Field(default=False, description="A returned fact has verified=False: lower the confidence.")
    dense_top1_similarity: float | None = Field(
        default=None,
        description="Best cosine similarity in the (category-filtered) dense search; None when it didn't run. "
        "Compare with thresholds.retrieval_min_similarity.",
    )

    @classmethod
    def from_facts(cls, facts: Sequence[RetrievedFact], dense_top1_similarity: float | None = None) -> RetrievalSignals:
        top1 = facts[0].score if facts else 0.0
        top2 = facts[1].score if len(facts) > 1 else 0.0
        return cls(
            top1_score=top1,
            top1_margin=max(0.0, top1 - top2),
            structured_hits=sum(f.via == "structured" for f in facts),
            any_unverified=any(f.metadata.get("verified") is False for f in facts),
            dense_top1_similarity=dense_top1_similarity,
        )


class RetrievalResult(BaseModel):
    facts: list[RetrievedFact]
    signals: RetrievalSignals
    categories: list[FactCategory] | None = Field(
        description="Semantic-search filter that was applied: None = every category, [] = search skipped."
    )
    latency_ms: dict[str, float] = Field(description="Per step: embed, structured, dense, bm25, rerank, total.")


@dataclass
class _Candidate:
    fact_id: str
    via: RetrievalVia
    score: float
    tier: int = 0
    rerank: float = 0.0


# --------------------------------------------------------------------------- retriever


class QueryEmbedder(Protocol):
    model_name: str

    def embed_query(self, text: str) -> list[float]: ...


class Retriever:
    def __init__(
        self,
        catalog: FactCatalog,
        dense: DenseIndex,
        bm25: BM25Index,
        embedder: QueryEmbedder,
        cfg: RetrievalSettings,
        reranker: Reranker | None = None,
    ):
        self._catalog = catalog
        self._dense = dense
        self._bm25 = bm25
        self._embedder = embedder
        self._cfg = cfg
        self._reranker = reranker

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        embedder: QueryEmbedder | None = None,
        reranker: Reranker | None = None,
        load_reranker: bool | None = None,
    ) -> Retriever:
        """Open the indexes from ``just index`` and check they were built from the same facts.

        The cross-encoder is loaded when ``load_reranker`` is true, or when it is None and
        ``retrieval.rerank.enabled`` is set.
        """
        paths = settings.paths
        missing = [p for p in (paths.facts_jsonl, paths.bm25_index, paths.chroma_dir, paths.gazetteer) if not p.exists()]
        if missing:
            raise RetrievalIndexUnavailable(f"missing {', '.join(str(p) for p in missing)}: run `just index`")
        raw = paths.facts_jsonl.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        facts = [Fact.model_validate_json(line) for line in raw.decode("utf-8").splitlines() if line.strip()]

        dense = ChromaDenseIndex.open(paths.chroma_dir, settings.vector_store.collection, settings.vector_store.distance)
        meta = dense.metadata
        if meta.get("facts_digest") != digest:
            raise RetrievalIndexUnavailable(f"Chroma was built from different facts than {paths.facts_jsonl}: run `just index`")
        bm25 = BM25Index.load(paths.bm25_index)
        if set(bm25.fact_ids) != {f.fact_id for f in facts}:
            raise RetrievalIndexUnavailable(f"{paths.bm25_index} was built from different facts: run `just index`")
        gazetteer = load_gazetteer(paths.gazetteer, paths.facts_jsonl)

        if embedder is None:
            from rag_core.retrieval.embeddings import Embedder

            embedder = Embedder.from_settings(settings.embeddings)
        if meta.get("embedding_model") != embedder.model_name:
            raise RetrievalIndexUnavailable(
                f"Chroma holds {meta.get('embedding_model')!r} embeddings but settings use {embedder.model_name!r}: "
                "run `just index`"
            )
        if load_reranker is None:
            load_reranker = settings.retrieval.rerank.enabled
        if reranker is None and load_reranker:
            reranker = CrossEncoderReranker.from_settings(settings.retrieval.rerank)
        return cls(FactCatalog(facts, topic_faculty(gazetteer)), dense, bm25, embedder, settings.retrieval, reranker)

    @property
    def catalog(self) -> FactCatalog:
        return self._catalog

    @property
    def has_reranker(self) -> bool:
        return self._reranker is not None

    def embed_query(self, text: str) -> list[float]:
        """The query vector retrieval uses; pass it back as ``query_vector`` to embed once."""
        return self._embedder.embed_query(text)

    def retrieve(
        self,
        question: str,
        intent: Intent,
        entities: Iterable[Entity | EntityMention] = (),
        *,
        query_vector: Sequence[float] | None = None,
        options: RetrievalOptions | None = None,
    ) -> list[RetrievedFact]:
        return self.search(question, intent, entities, query_vector=query_vector, options=options).facts

    def search(
        self,
        question: str,
        intent: Intent,
        entities: Iterable[Entity | EntityMention] = (),
        *,
        query_vector: Sequence[float] | None = None,
        options: RetrievalOptions | None = None,
    ) -> RetrievalResult:
        """``entities``: pipeline ``EntityMention``s (ambiguity and days of "now" are used) or contract
        ``Entity``s. ``query_vector``: the question's query embedding if already computed (intent
        classification uses the same one), so it isn't embedded twice."""
        opts = options or RetrievalOptions()
        rerank = self._cfg.rerank.enabled if opts.rerank is None else opts.rerank
        if rerank and self._reranker is None:
            raise ValueError("reranking was requested but this retriever has no reranker")
        timings: dict[str, float] = {}
        start = clock = time.perf_counter()

        def lap(step: str) -> None:
            nonlocal clock
            now = time.perf_counter()
            timings[step] = (now - clock) * 1000
            clock = now

        vector = None
        if opts.dense:
            vector = list(query_vector) if query_vector is not None else self._embedder.embed_query(question)
        lap("embed")

        structured = self._structured(intent, entities, vector) if opts.structured else []
        lap("structured")

        categories = self._cfg.categories_for(intent) if opts.category_filter else None
        allowed = None if categories is None else self._catalog.in_categories(categories)
        dense_hits: list[tuple[str, float]] = []
        lexical: list[tuple[str, float]] = []
        if opts.dense and (allowed is None or allowed):
            n = self._cfg.dense_candidates if allowed is None else min(self._cfg.dense_candidates, len(allowed))
            dense_hits = self._dense.query(vector, n, None if categories is None else [c.value for c in categories])
        lap("dense")
        if opts.bm25 and (allowed is None or allowed):
            lexical = self._bm25.search(question, self._cfg.bm25_candidates, allowed)
        lap("bm25")

        pool = structured + self._semantic(dense_hits, lexical, opts, {c.fact_id for c in structured})
        if rerank and pool:
            pool = self._rerank(question, pool)
        lap("rerank")

        facts = [self._fact(c, rank) for rank, c in enumerate(pool[: self._cfg.top_k], start=1)]
        timings["total"] = (time.perf_counter() - start) * 1000
        return RetrievalResult(
            facts=facts,
            signals=RetrievalSignals.from_facts(facts, dense_hits[0][1] if dense_hits else None),
            categories=None if categories is None else sorted(categories),
            latency_ms=timings,
        )

    def _structured(
        self, intent: Intent, entities: Iterable[Entity | EntityMention], vector: Sequence[float] | None
    ) -> list[_Candidate]:
        linked = linked_ids(entities)
        tiers: dict[str, int] = {}
        for route in ROUTES.get(intent, ()):
            if route.fires(linked):
                for fid in self._catalog.route(route, linked):
                    tiers[fid] = min(route.tier, tiers.get(fid, route.tier))
        if not tiers:
            return []
        similarity = self._dense.similarities(vector, list(tiers)) if vector is not None else {}
        order = sorted(tiers, key=lambda fid: (tiers[fid], -similarity.get(fid, 0.0), self._catalog.position(fid)))
        return [_Candidate(fid, "structured", STRUCTURED_SCORE, tier=tiers[fid]) for fid in order]

    def _semantic(
        self,
        dense_hits: list[tuple[str, float]],
        lexical: list[tuple[str, float]],
        opts: RetrievalOptions,
        exclude: set[str],
    ) -> list[_Candidate]:
        in_dense = {fid for fid, _ in dense_hits}
        in_bm25 = {fid for fid, _ in lexical}
        rankings = [[fid for fid, _ in hits] for hits, on in ((dense_hits, opts.dense), (lexical, opts.bm25)) if on]
        out = []
        for fid, score in reciprocal_rank_fusion(rankings, self._cfg.rrf_k):
            if fid in exclude:
                continue
            via: RetrievalVia = "hybrid" if fid in in_dense and fid in in_bm25 else ("dense" if fid in in_dense else "bm25")
            out.append(_Candidate(fid, via, score))
        return out

    def _rerank(self, question: str, pool: list[_Candidate]) -> list[_Candidate]:
        head = pool[: self._cfg.rerank.candidates]
        scores = self._reranker.score(question, [self._catalog.get(c.fact_id).text for c in head])
        for candidate, score in zip(head, scores):
            candidate.rerank = min(1.0, max(0.0, score))
        pinned = sorted((c for c in head if c.via == "structured"), key=lambda c: (c.tier, -c.rerank))
        rest = sorted((c for c in head if c.via != "structured"), key=lambda c: -c.rerank)
        for candidate in rest:
            candidate.score = candidate.rerank
        return pinned + rest

    def _fact(self, candidate: _Candidate, rank: int) -> RetrievedFact:
        fact = self._catalog.get(candidate.fact_id)
        return RetrievedFact(
            fact_id=fact.fact_id,
            text=fact.text,
            metadata=fact.chroma_metadata(),
            score=candidate.score,
            rank=rank,
            via=candidate.via,
        )


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    """Process-wide retriever from :func:`get_settings`. ``get_retriever.cache_clear()`` reloads it."""
    return Retriever.from_settings(get_settings())


def retrieve(question: str, intent: Intent, entities: Iterable[Entity | EntityMention] = ()) -> list[RetrievedFact]:
    """The top ``retrieval.top_k`` facts for a question. Use :meth:`Retriever.search` for the signals."""
    return get_retriever().retrieve(question, intent, entities)
