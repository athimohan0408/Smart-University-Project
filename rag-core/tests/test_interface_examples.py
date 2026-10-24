"""The five request/response pairs in docs/interface.md reproduce (every field but latency_ms).

Needs the real index, embedding model and intent model, so it is marked `model`.
"""

import json
import re

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, PROJECT_ROOT, load_settings

DOC = PROJECT_ROOT / "docs" / "interface.md"


def doc_examples():
    text = DOC.read_text(encoding="utf-8")
    pairs = []
    for section in re.split(r"^### Example \d+: ", text, flags=re.MULTILINE)[1:]:
        blocks = re.findall(r"```json\n(.*?)\n```", section, flags=re.DOTALL)
        pairs.append((json.loads(blocks[0]), json.loads(blocks[1])))
    return pairs


def test_doc_has_the_five_examples():
    pairs = doc_examples()
    assert len(pairs) == 5
    assert any(resp["escalate"] for _, resp in pairs)
    assert any("Which one do you mean?" in resp["answer"] for _, resp in pairs)


@pytest.fixture(scope="module")
def client():
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    if not settings.paths.chroma_dir.exists() or not settings.paths.intent_model.exists():
        pytest.skip("no index or intent model: run `just index` and `just train-intent`")
    from fastapi.testclient import TestClient

    from rag_core import pipeline as pipeline_module
    from rag_core.api import app
    from rag_core.pipeline import Pipeline

    # Same settings as the documented run: templates, no query log.
    template = settings.model_copy(update={"generation": settings.generation.model_copy(update={"mode": "template"})})
    pipeline_module._pipeline = Pipeline.from_settings(template, log_queries=False)
    with TestClient(app) as c:
        yield c
    pipeline_module._pipeline = None


@pytest.mark.model
@pytest.mark.parametrize("n", range(5))
def test_documented_example_reproduces(client, n):
    request, expected = doc_examples()[n]
    got = client.post("/answer", json=request)
    assert got.status_code == 200
    got = got.json()
    assert set(got["latency_ms"]) == set(expected["latency_ms"])
    for key in ("confidence", "intent_confidence"):
        assert round(got.pop(key), 2) == round(expected.pop(key), 2), key
    got.pop("latency_ms"), expected.pop("latency_ms")
    assert got == expected


@pytest.mark.model
def test_health_and_validation(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    # template mode: health must not claim the LLM is ready
    assert health["llm_ready"] is False and health["llm"].startswith("disabled")
    assert {"llm_last_success", "llm_last_error"} <= set(health)
    assert client.post("/answer", json={"question": ""}).status_code == 422
    assert client.post("/answer", json={"question": "hi", "extra": 1}).status_code == 422
