"""Optional cross-encoder rerank of the retriever's candidates (``retrieval.rerank``).

A cross-encoder reads the question and each fact together, so it orders candidates more precisely
than the bi-encoder + BM25 fusion, at the cost of one model call per candidate. Scores are
probabilities (sigmoid of the model's logit), so they are comparable to the other 0-1 scores.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rag_core.config import RerankSettings


class Reranker(Protocol):
    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Relevance of each passage to the query, in [0, 1], in input order."""
        ...


class CrossEncoderReranker:
    def __init__(self, model, batch_size: int):
        self._model = model
        self._batch_size = batch_size

    @classmethod
    def from_settings(cls, cfg: RerankSettings) -> CrossEncoderReranker:
        from sentence_transformers import CrossEncoder  # heavy import; only when reranking

        return cls(CrossEncoder(cfg.model, device=cfg.device), cfg.batch_size)

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        import torch

        # Pass the activation explicitly: the ms-marco models default to raw logits.
        scores = self._model.predict(
            [(query, p) for p in passages],
            batch_size=self._batch_size,
            activation_fn=torch.nn.Sigmoid(),
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(s) for s in scores]
