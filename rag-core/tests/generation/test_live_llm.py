"""Live LLM validation (network, paid). Opt-in twice: a provider key in .env AND RAG_LIVE_TESTS=1.

Run: `$env:RAG_LIVE_TESTS = "1"; python -m pytest -m live`. Without both, it is skipped.
"""

import os

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.generation.llm_client import LLMUnavailable, make_llm_client
from rag_core.generation.validate_llm import run


@pytest.mark.live
def test_live_llm_path():
    if os.getenv("RAG_LIVE_TESTS") != "1":
        pytest.skip("live LLM tests are opt-in: set RAG_LIVE_TESTS=1 (makes paid API calls)")
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    try:
        make_llm_client(settings)
    except LLMUnavailable as exc:
        pytest.skip(f"no LLM provider configured: {exc}")
    code, report = run(settings, limit=25)
    assert code == 0, report
