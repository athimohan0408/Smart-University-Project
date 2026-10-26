"""Typed settings from config/settings.yaml (all tunables) and .env (secrets only)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

MODULE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = MODULE_ROOT / "config" / "settings.yaml"


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (MODULE_ROOT / path).resolve()


ModulePath = Annotated[Path, BeforeValidator(_resolve)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RagSettings(_Section):
    mode: Literal["http", "inprocess"]
    url: str
    timeout_s: float


class KBSettings(_Section):
    faculty_json: ModulePath
    gazetteer: ModulePath


class MemorySettings(_Section):
    session_idle_s: float
    max_turns: int
    send_last_entities: bool
    send_prior_entities: bool
    max_prior_entities: int
    option_match_min: float


class HandoffSettings(_Section):
    option_label: str
    ask_again_label: str
    offer_prompt: str
    confirmed_message: str
    declined_message: str
    unavailable_message: str
    tickets: ModulePath
    webhook_timeout_s: float


class SpeechSettings(_Section):
    language: str
    rooms_as_digits: bool
    spell_codes: bool


class STTSettings(_Section):
    backend: Literal["faster_whisper", "text"]
    model: str
    device: str
    compute_type: str
    beam_size: int
    bias_with_kb_names: bool
    max_prompt_names: int


class TTSSettings(_Section):
    backend: Literal["piper", "pyttsx3", "none"]
    piper_voice: ModulePath
    pyttsx3_rate: int


class AudioSettings(_Section):
    sample_rate: int
    silence_threshold: float
    end_silence_s: float
    max_utterance_s: float
    start_timeout_s: float


class MetricsSettings(_Section):
    turn_log: ModulePath
    report: ModulePath
    target_latency_p95_ms: float
    target_completion_rate: float = Field(ge=0, le=1)


class ApiSettings(_Section):
    host: str
    port: int


class Secrets(_Section):
    receptionist_webhook_url: str | None = None


class Settings(_Section):
    rag: RagSettings
    kb: KBSettings
    memory: MemorySettings
    handoff: HandoffSettings
    speech: SpeechSettings
    stt: STTSettings
    tts: TTSSettings
    audio: AudioSettings
    metrics: MetricsSettings
    api: ApiSettings
    secrets: Secrets = Secrets()


def load_settings(path: Path | None = None, env_file: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("VM_SETTINGS_PATH", DEFAULT_SETTINGS_PATH))
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "secrets" in raw:
        raise ValueError(f"{path}: secrets belong in .env, not in the settings file")
    env = {**dotenv_values(env_file or MODULE_ROOT / ".env"), **os.environ}
    raw["secrets"] = {"receptionist_webhook_url": env.get("RECEPTIONIST_WEBHOOK_URL") or None}
    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
