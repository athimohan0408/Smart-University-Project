"""HTTP API for the RAG core. Run: ``python -m rag_core.api`` (or ``just serve``) on api.host:api.port.

- ``POST /answer``: body ``RAGRequest`` -> ``RAGResult`` (contract in docs/interface.md).
- ``GET /health``: readiness, the LLM status and which facts are loaded.
- ``POST /reload-kb``: re-ingest the faculty sheet, rebuild the index from data/raw + data/kb, and
  swap in a fresh pipeline without restarting. Requests wait while it runs.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

from rag_core import pipeline as pipeline_module
from rag_core.config import get_settings
from rag_core.contracts import RAGRequest, RAGResult

_reload_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(pipeline_module.warmup)  # load models + indexes before the first request
    yield


app = FastAPI(title="rag_core: CHRIST University 8th floor receptionist", version="0.1.0", lifespan=lifespan)


def _answer(request: RAGRequest) -> RAGResult:
    with _reload_lock:  # don't answer from a half-rebuilt index
        pipe = pipeline_module.get_pipeline()
    return pipe.answer(request.question, request.context)


@app.post("/answer", response_model=RAGResult)
async def answer(request: RAGRequest) -> RAGResult:
    return await run_in_threadpool(_answer, request)


@app.get("/health")
async def health() -> dict:
    pipe = pipeline_module.get_pipeline()
    facts = pipe.settings.paths.facts_jsonl
    return {
        "status": "ok",
        "facts": len(pipe.retriever.catalog),
        "facts_digest": hashlib.sha256(facts.read_bytes()).hexdigest()[:12] if facts.exists() else None,
        # llm (status text), llm_ready (configured AND the last real call, if any, succeeded),
        # llm_last_success, llm_last_error (redacted). Every request still tries a configured LLM and falls
        # back to templates on failure; llm_ready says whether the most recent attempt worked.
        **pipe.generator.health(),
        "rerank": pipe.retriever.has_reranker,
        "calibrated_confidence": pipe.scorer.calibrated,
    }


def _reload_kb() -> dict:
    from rag_core.kb import ingest_faculty
    from rag_core.retrieval import build_index

    with _reload_lock:
        try:
            ingest_faculty.main()
            build_index.main()
        except SystemExit as exc:  # the CLIs exit with a message on bad data
            raise HTTPException(status_code=500, detail=f"rebuild failed: {exc}") from exc
        fresh = pipeline_module.reload()
        fresh.warmup()
    return {"status": "reloaded", "facts": len(fresh.retriever.catalog)}


@app.post("/reload-kb")
async def reload_kb() -> dict:
    return await run_in_threadpool(_reload_kb)


def main() -> None:
    import uvicorn

    cfg = get_settings().api
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
