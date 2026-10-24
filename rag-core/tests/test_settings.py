from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from rag_core.config import DEFAULT_SETTINGS_PATH, PROJECT_ROOT, load_settings


@pytest.fixture
def raw_settings() -> dict:
    return yaml.safe_load(DEFAULT_SETTINGS_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_default_settings_load():
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    assert settings.llm.temperature == 0.0
    assert settings.retrieval.top_k >= 1


def test_relative_paths_resolve_against_project_root():
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    assert settings.paths.raw_faculty_xlsx.is_absolute()
    assert settings.paths.raw_faculty_xlsx.is_relative_to(PROJECT_ROOT)
    assert settings.paths.raw_faculty_xlsx.exists()


def test_unknown_key_is_rejected(tmp_path, raw_settings):
    raw_settings["retrieval"]["topk"] = 3
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, raw_settings))


def test_secrets_in_yaml_are_rejected(tmp_path, raw_settings):
    raw_settings["secrets"] = {"anthropic_api_key": "sk-should-not-be-here"}
    with pytest.raises(ValueError, match="secrets"):
        load_settings(_write(tmp_path, raw_settings))


def test_recall_at_cannot_exceed_top_k(tmp_path, raw_settings):
    raw_settings["eval"]["recall_at"] = [raw_settings["retrieval"]["top_k"] + 1]
    with pytest.raises(ValidationError, match="recall_at"):
        load_settings(_write(tmp_path, raw_settings))


def test_secrets_come_from_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    assert settings.secrets.anthropic_api_key.get_secret_value() == "test-key"
    assert "test-key" not in repr(settings)
    assert "secrets" not in settings.model_dump()
