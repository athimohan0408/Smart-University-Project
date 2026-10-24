"""Go-live safety checks: LLM readiness reporting, secret hygiene, and live/template separation."""

import re
from pathlib import Path

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, PROJECT_ROOT, Secrets, load_settings
from rag_core.generation.answer import AnswerGenerator, redact
from rag_core.generation.llm_client import FakeLLMClient, LLMError, LLMUnavailable

FAKE_KEY = "sk-ant-api03-abcdefghijklmnop1234567890"
GOOD = '{"answer": "Room 874.", "answerable": true, "cited_fact_ids": [], "needs_clarification": false, "clarification_options": []}'


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


def test_redact_strips_values_but_keeps_setting_names():
    assert FAKE_KEY not in redact(f"AuthenticationError: invalid key {FAKE_KEY}")
    assert redact("Authorization: Bearer abc.def-123") == "Authorization: Bearer [redacted]"
    assert redact("x-api-key: s3cr3t-value") == "x-api-key: [redacted]"
    assert redact("api_key=s3cr3t-value given") == "api_key=[redacted] given"
    # the operator instruction must stay readable
    msg = "no Anthropic credentials: put ANTHROPIC_API_KEY in .env (see .env.example)"
    assert redact(msg) == msg


def test_health_reports_real_call_outcomes(settings):
    from datetime import datetime

    from rag_core.contracts import Intent

    state = {"fail": True}

    def reply(system, user):
        if state["fail"]:
            raise LLMError(f"Anthropic call failed: AuthenticationError: bad key {FAKE_KEY}")
        return GOOD

    gen = AnswerGenerator(settings.generation, FakeLLMClient(reply))
    assert gen.health()["llm_ready"] is True and gen.llm_status == "configured"  # configured, nothing tried yet
    from rag_core.contracts import RetrievedFact

    fact = RetrievedFact(fact_id="room:874", text="Room 874 is in Central Block.", score=1.0, rank=1, via="structured")
    gen.generate("where is room 874", Intent.FIND_ROOM, [], [fact], datetime(2026, 10, 20, 11))
    h = gen.health()
    assert h["llm_ready"] is False and "AuthenticationError" in h["llm_last_error"]
    assert FAKE_KEY not in str(h)  # redacted
    state["fail"] = False
    gen.generate("where is room 874", Intent.FIND_ROOM, [], [fact], datetime(2026, 10, 20, 11))
    assert gen.health()["llm_ready"] is True and gen.health()["llm_last_success"]


def test_template_mode_and_missing_credentials_are_not_ready(settings, monkeypatch):
    template = settings.generation.model_copy(update={"mode": "template"})
    assert AnswerGenerator(template).health()["llm_ready"] is False
    import rag_core.generation.answer as answer_module

    def unavailable(_settings):
        raise LLMUnavailable("no Anthropic credentials: put ANTHROPIC_API_KEY in .env")

    monkeypatch.setattr(answer_module, "make_llm_client", unavailable)
    gen = AnswerGenerator.from_settings(settings.model_copy(update={"secrets": Secrets()}))
    assert gen.health()["llm_ready"] is False and gen.llm_status.startswith("unavailable")
    assert "ANTHROPIC_API_KEY" in gen.llm_status  # tells the operator what to set


def test_validation_report_when_not_run_costs_nothing(settings, monkeypatch):
    import rag_core.generation.validate_llm as v

    def unavailable(_settings):  # never let this test reach a real provider
        raise LLMUnavailable("no Anthropic credentials")

    monkeypatch.setattr(v, "make_llm_client", unavailable)
    code, report = v.run(settings)
    assert code == 2
    assert "**NOT RUN**" in report and "cost $0.00" in report and "unvalidated" in report
    assert "Verified only in simulation" in report  # simulated results are labelled as simulated


def test_no_credentials_in_the_repository():
    """Nothing that looks like a real API key in tracked-style text files (tests use obvious fakes)."""
    pattern = re.compile(r"sk-ant-(?!test-|api03-abcdefghijklmnop)[A-Za-z0-9_\-]{20,}|sk-proj-[A-Za-z0-9_\-]{20,}")
    roots = ["src", "config", "data/kb", "data/eval", "docs", "reports", "tests", "logs"]
    hits = []
    for root in roots:
        for path in (PROJECT_ROOT / root).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".yaml", ".yml", ".json", ".jsonl", ".csv", ".txt", ".toml"}:
                if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                    hits.append(str(path.relative_to(PROJECT_ROOT)))
    for name in ("pyproject.toml", "justfile", "Makefile", ".env.example", "CLAUDE.md"):
        p = PROJECT_ROOT / name
        if p.exists() and pattern.search(p.read_text(encoding="utf-8", errors="ignore")):
            hits.append(name)
    assert hits == []


def test_env_file_is_git_ignored():
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^ANTHROPIC_API_KEY=\s*$", example, re.MULTILINE)  # the template holds no value


@pytest.mark.model
def test_eval_refuses_llm_mode_without_an_llm(settings, monkeypatch):
    """`just eval --llm` must never report template answers as a live-LLM run."""
    import rag_core.generation.answer as answer_module
    from rag_core.eval.run_eval import evaluate

    if not settings.paths.chroma_dir.exists():
        pytest.skip("no index")

    def unavailable(_settings):
        raise LLMUnavailable("no credentials")

    monkeypatch.setattr(answer_module, "make_llm_client", unavailable)
    with pytest.raises(SystemExit, match="no LLM is available"):
        evaluate(settings, split="dev", use_llm=True)
