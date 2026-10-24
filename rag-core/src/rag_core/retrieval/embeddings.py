"""Sentence embeddings with the model-specific query/passage prefixes from settings.

bge v1.5 expects an instruction on queries only; passages are embedded as-is. MiniLM uses no
prefixes. Both come from the active profile in ``embeddings.models``, so switching the model in
config switches the prefixes with it. Always embed with this class: a query embedded without its
prefix silently loses recall.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rag_core.config import EmbeddingProfile, EmbeddingSettings

EncodeFn = Callable[[list[str]], Sequence[Sequence[float]]]


class Embedder:
    def __init__(self, profile: EmbeddingProfile, encode: EncodeFn, dimension: int):
        self.profile = profile
        self.dimension = dimension
        self._encode = encode

    @classmethod
    def from_settings(cls, settings: EmbeddingSettings) -> Embedder:
        from sentence_transformers import SentenceTransformer  # heavy import; only when embedding

        profile = settings.active
        model = SentenceTransformer(profile.name, device=settings.device)

        def encode(texts: list[str]) -> list[list[float]]:
            vectors = model.encode(
                texts,
                batch_size=settings.batch_size,
                normalize_embeddings=settings.normalize,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return vectors.tolist()

        # Renamed in sentence-transformers 6; keep the old name for >=3.0 installs.
        dimension = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
        return cls(profile, encode, dimension())

    @property
    def model_name(self) -> str:
        return self.profile.name

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(v) for v in self._encode([self.profile.passage_prefix + t for t in texts])]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(v) for v in self._encode([self.profile.query_prefix + t for t in texts])]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_queries([text])[0]
