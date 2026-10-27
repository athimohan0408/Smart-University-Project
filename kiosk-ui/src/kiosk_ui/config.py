"""Typed settings from config/settings.yaml. This module has no secrets."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict

MODULE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = MODULE_ROOT / "config" / "settings.yaml"


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (MODULE_ROOT / path).resolve()


ModulePath = Annotated[Path, BeforeValidator(_resolve)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiSettings(_Section):
    host: str
    port: int


class UpstreamSettings(_Section):
    voice_memory_url: str
    timeout_s: float


class KioskSettings(_Section):
    id: str
    idle_reset_s: float
    speak: bool
    greeting: str


class MapSettings(_Section):
    file: ModulePath


class KBSettings(_Section):
    faculty_json: ModulePath


class LogSettings(_Section):
    ui_events: ModulePath
    rag_queries: ModulePath
    voice_turns: ModulePath


class EvaluationSettings(_Section):
    test_set: ModulePath
    report: ModulePath
    results: ModulePath
    rag_reports: ModulePath
    voice_report: ModulePath


class Settings(_Section):
    api: ApiSettings
    upstream: UpstreamSettings
    kiosk: KioskSettings
    map: MapSettings
    kb: KBSettings
    logs: LogSettings
    evaluation: EvaluationSettings


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("KIOSK_SETTINGS_PATH", DEFAULT_SETTINGS_PATH))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
