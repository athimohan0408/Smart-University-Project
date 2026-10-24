"""Held-out evaluation of the trained intent classifier. Writes reports/intent_eval.md.

Needs the embedding model (Hugging Face cache) and `just train-intent`; skipped without the artifact.
"""

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.nlu.eval_intent import run_evaluation


@pytest.mark.model
def test_held_out_accuracy_without_llm_fallback_and_report():
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    if not settings.paths.intent_model.exists():
        pytest.skip("no trained intent model: run `just train-intent`")

    report_path, results = run_evaluation(settings)
    without_llm = results[0]

    assert report_path.exists()
    assert "## Confusion matrix" in report_path.read_text(encoding="utf-8")
    assert without_llm.setting == "without LLM fallback"
    assert without_llm.accuracy >= settings.intent.target_accuracy, (
        f"held-out accuracy {without_llm.accuracy:.3f} < {settings.intent.target_accuracy}; see {report_path}"
    )
