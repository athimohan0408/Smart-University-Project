"""Calling rag-core: over HTTP (``POST /answer``, one keep-alive connection) or in-process."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from voice_memory.config import RagSettings


class RagUnavailable(RuntimeError):
    """rag-core could not be reached or returned an error: escalate, never guess."""


class RagClient(Protocol):
    def answer(self, question: str, context: dict[str, Any]) -> dict: ...


class HttpRagClient:
    def __init__(self, cfg: RagSettings, client: httpx.Client | None = None):
        # One persistent connection: a new one per request costs ~200 ms on Windows (rag-core README).
        self._client = client or httpx.Client(base_url=cfg.url, timeout=cfg.timeout_s)

    def answer(self, question: str, context: dict[str, Any]) -> dict:
        try:
            response = self._client.post("/answer", json={"question": question, "context": context})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise RagUnavailable(str(exc)) from exc

    def health(self) -> dict:
        try:
            return self._client.get("/health").json()
        except httpx.HTTPError as exc:
            raise RagUnavailable(str(exc)) from exc


class InProcessRagClient:
    """Same Python environment as rag-core (``pip install -e ../rag-core``)."""

    def __init__(self):
        from rag_core.pipeline import warmup

        self._pipeline = warmup()

    def answer(self, question: str, context: dict[str, Any]) -> dict:
        try:
            return self._pipeline.answer(question, context).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - any failure must become an escalation, not a crash
            raise RagUnavailable(str(exc)) from exc


def make_rag_client(cfg: RagSettings) -> RagClient:
    return InProcessRagClient() if cfg.mode == "inprocess" else HttpRagClient(cfg)
