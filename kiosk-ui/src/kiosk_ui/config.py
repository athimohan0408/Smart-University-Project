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
    # The address phones use to reach this server (e.g. http://192.168.1.20:8002), for the QR codes that open the
    # walkthrough on a phone. None: no "send to phone" QR on the kiosk (127.0.0.1 is only the kiosk itself).
    public_url: str | None = None


class MapSettings(_Section):
    file: ModulePath
    places_info: ModulePath | None = None  # place cards; default: places_info.yaml next to the map file


class KBSettings(_Section):
    faculty_json: ModulePath


class LogSettings(_Section):
    ui_events: ModulePath
    rag_queries: ModulePath
    voice_turns: ModulePath


class AdminSettings(_Section):
    data_dir: ModulePath  # accounts, announcements, statuses, timetable versions, change log (not in git)
    rag_url: str  # rag-core, for POST /reload-kb after a new timetable is published
    timetable_file: ModulePath  # rag-core's faculty workbook, which a published timetable replaces
    reload_timeout_s: float  # rag-core rebuilds its index on reload: give it time


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
    admin: AdminSettings
    evaluation: EvaluationSettings


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("KIOSK_SETTINGS_PATH", DEFAULT_SETTINGS_PATH))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
