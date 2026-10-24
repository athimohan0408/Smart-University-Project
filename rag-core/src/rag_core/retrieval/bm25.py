"""Lexical (BM25) index over the same facts as the vector index, for hybrid search.

Pickled to ``paths.bm25_index``. Only load pickles this project built: unpickling runs code.
Build and query must use the same :func:`tokenize`; bump ``TOKENIZER_VERSION`` when it changes.
"""

from __future__ import annotations

import os
import pickle
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rank_bm25 import BM25Okapi

TOKENIZER_VERSION = 1

_TOKEN = re.compile(r"[a-z0-9]+")
# Function words that would otherwise dominate short visitor questions ("where is the ...").
_STOPWORDS = frozenset(
    "a an the is are was were be of to in on at for and or with by from as it its this that "
    "what where who whom when which how does do did can could i me my you your his her their "
    "there please tell".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.casefold()) if t not in _STOPWORDS]


@dataclass
class BM25Index:
    fact_ids: list[str]
    k1: float
    b: float
    model: BM25Okapi = field(repr=False)
    tokenizer_version: int = TOKENIZER_VERSION

    @classmethod
    def build(cls, fact_ids: Sequence[str], documents: Sequence[str], k1: float, b: float) -> BM25Index:
        if len(fact_ids) != len(documents):
            raise ValueError("fact_ids and documents must have the same length")
        return cls(list(fact_ids), k1, b, BM25Okapi([tokenize(d) for d in documents], k1=k1, b=b))

    def search(self, query: str, n: int, allowed: Collection[str] | None = None) -> list[tuple[str, float]]:
        """Top ``n`` (fact_id, score) pairs with a positive score, best first; ties by fact_id.

        ``allowed`` restricts the result to those fact ids (the retriever's category filter).
        """
        pairs = zip(self.fact_ids, self.model.get_scores(tokenize(query)))
        if allowed is not None:
            pairs = ((fid, score) for fid, score in pairs if fid in allowed)
        ranked = sorted(pairs, key=lambda pair: (-pair[1], pair[0]))
        return [(fid, float(score)) for fid, score in ranked[:n] if score > 0]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(self, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> BM25Index:
        with path.open("rb") as fh:
            index = pickle.load(fh)
        if not isinstance(index, cls) or index.tokenizer_version != TOKENIZER_VERSION:
            raise ValueError(f"{path} was built with a different tokenizer; rebuild with `just index`")
        return index
