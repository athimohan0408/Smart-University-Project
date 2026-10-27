"""Typed settings loaded from config/settings.yaml, plus secrets from the environment / .env.

Use :func:`get_settings` in application code. All tunables come from the YAML file; nothing
else in the package should hard-code thresholds, model names, paths or ports.
"""

from __future__ import annotations

import os
from datetime import datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from dotenv import load_dotenv
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    ValidationInfo,
    model_validator,
)

from rag_core.contracts import Intent
from rag_core.kb.facts import FactCategory

# src/rag_core/config.py -> repo root. Valid for the editable install this project uses.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
SETTINGS_PATH_ENV = "RAG_SETTINGS_PATH"

Probability = Annotated[float, Field(ge=0.0, le=1.0)]


def _resolve_against_root(value: Path, info: ValidationInfo) -> Path:
    root = (info.context or {}).get("root", PROJECT_ROOT)
    return value if value.is_absolute() else (Path(root) / value).resolve()


ProjectPath = Annotated[Path, AfterValidator(_resolve_against_root)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PathsSettings(_Section):
    raw_faculty_xlsx: ProjectPath
    glossary: ProjectPath
    entity_lexicon: ProjectPath
    campus_xlsx: ProjectPath
    faculty_json: ProjectPath
    subject_codes_csv: ProjectPath
    data_quality_report: ProjectPath
    facts_jsonl: ProjectPath
    chroma_dir: ProjectPath
    bm25_index: ProjectPath
    gazetteer: ProjectPath
    eval_testset: ProjectPath
    eval_reports_dir: ProjectPath
    eval_baseline: ProjectPath
    confidence_model: ProjectPath
    calibration_report: ProjectPath
    threshold_report: ProjectPath
    query_log: ProjectPath
    intent_train: ProjectPath
    intent_test: ProjectPath
    intent_model: ProjectPath
    intent_report: ProjectPath
    entity_test: ProjectPath
    entity_report: ProjectPath
    retrieval_test: ProjectPath
    retrieval_report: ProjectPath


class KBSettings(_Section):
    faculty_sheet: str
    home_block: str
    home_floor: str
    day_start: time
    day_end: time

    @model_validator(mode="after")
    def _window_disambiguates_am_pm(self) -> KBSettings:
        start = self.day_start.hour * 60 + self.day_start.minute
        end = self.day_end.hour * 60 + self.day_end.minute
        if not 0 < end - start < 12 * 60:
            raise ValueError("kb.day_start must be before kb.day_end, less than 12 h apart")
        return self


class EmbeddingProfile(_Section):
    name: str
    query_prefix: str = ""
    passage_prefix: str = ""


class EmbeddingSettings(_Section):
    model: str = Field(description="Key into `models`; the one-line model switch.")
    models: dict[str, EmbeddingProfile]
    normalize: bool
    batch_size: PositiveInt
    device: str

    @model_validator(mode="after")
    def _model_is_defined(self) -> EmbeddingSettings:
        if self.model not in self.models:
            raise ValueError(f"embeddings.model {self.model!r} is not one of {sorted(self.models)}")
        return self

    @property
    def active(self) -> EmbeddingProfile:
        return self.models[self.model]


class VectorStoreSettings(_Section):
    collection: str
    distance: Literal["cosine", "l2", "ip"]


class RerankSettings(_Section):
    enabled: bool
    model: str
    candidates: PositiveInt
    batch_size: PositiveInt
    device: str


class RetrievalSettings(_Section):
    top_k: PositiveInt
    dense_candidates: PositiveInt
    bm25_candidates: PositiveInt
    rrf_k: PositiveInt
    bm25_k1: PositiveFloat
    bm25_b: Annotated[float, Field(ge=0.0, le=1.0)]
    target_recall: Probability
    intent_categories: dict[Intent, list[FactCategory]]
    rerank: RerankSettings

    @model_validator(mode="after")
    def _pools_cover_top_k(self) -> RetrievalSettings:
        if min(self.dense_candidates, self.bm25_candidates, self.rerank.candidates) < self.top_k:
            raise ValueError("dense_candidates, bm25_candidates and rerank.candidates must be >= top_k")
        return self

    def categories_for(self, intent: Intent) -> frozenset[FactCategory] | None:
        """The semantic-search filter for ``intent``: None = every category, empty = skip the search."""
        categories = self.intent_categories.get(intent)
        return None if categories is None else frozenset(categories)


class ThresholdSettings(_Section):
    entity_match_min: Annotated[float, Field(ge=0, le=100)]
    intent_min_confidence: Probability
    retrieval_min_similarity: Annotated[float, Field(ge=-1.0, le=1.0)]
    answer_min_confidence: Probability
    unverified_glossary_weight: Probability


class FactSettings(_Section):
    include_unverified_expansions: bool


class IntentSettings(_Section):
    c_grid: Annotated[list[PositiveFloat], Field(min_length=1)]
    cv_folds: Annotated[int, Field(ge=2)]
    max_iter: PositiveInt
    class_weight: Literal["balanced"] | None
    seed: int
    top_n: PositiveInt
    rule_confidence: Probability
    llm_fallback_enabled: bool
    llm_fallback_threshold: Probability
    llm_fallback_confidence: Probability
    target_accuracy: Probability


class Period(_Section):
    start: time
    end: time

    @model_validator(mode="before")
    @classmethod
    def _from_pair(cls, data):
        # YAML writes a period as ["07:30", "12:00"].
        if isinstance(data, (list, tuple)) and len(data) == 2:
            return {"start": data[0], "end": data[1]}
        return data

    @model_validator(mode="after")
    def _ordered(self) -> Period:
        if self.start > self.end:
            raise ValueError(f"period start {self.start} is after end {self.end}")
        return self


class PeriodSettings(_Section):
    morning: Period
    afternoon: Period
    evening: Period
    noon: Period
    lunch: Period


class EntitySettings(_Section):
    max_span_tokens: PositiveInt
    fuzzy_min_chars: PositiveInt
    fuzzy_word_min: Annotated[float, Field(ge=0, le=100)]
    ambiguity_margin: Annotated[float, Field(ge=0, le=100)]
    short_code_score: Probability
    unknown_name_score: Probability
    target_f1: Probability
    periods: PeriodSettings


class LLMSettings(_Section):
    provider: Literal["anthropic", "openai", "ollama"]
    model: str
    temperature: Annotated[float, Field(ge=0.0, le=2.0)]
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    anthropic_fallbacks: bool
    max_tokens: PositiveInt
    timeout_s: PositiveFloat
    max_retries: Annotated[int, Field(ge=0)]
    price_input_per_mtok: Annotated[float, Field(ge=0.0)]
    price_output_per_mtok: Annotated[float, Field(ge=0.0)]


class GenerationSettings(_Section):
    mode: Literal["llm", "template"]
    max_names_spoken: PositiveInt
    max_clarification_options: PositiveInt
    safe_message: str = Field(min_length=1)


class ScoringWeights(_Section):
    intent: Probability
    entity: Probability
    retrieval: Probability
    margin: Probability
    structured: Probability
    answerable: Probability
    grounded: Probability

    @model_validator(mode="after")
    def _sum_to_one(self) -> ScoringWeights:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"scoring.weights must sum to 1 (got {total:g})")
        return self


class NLISettings(_Section):
    enabled: bool
    model: str
    max_contradiction: Probability
    device: str


class ScoringSettings(_Section):
    weights: ScoringWeights
    unverified_fact_weight: Probability
    use_calibration: bool
    min_negatives_for_calibration: PositiveInt
    calibration_bins: Annotated[int, Field(ge=2)]
    max_hallucination_rate: Probability
    sweep_step: Annotated[float, Field(gt=0.0, le=0.5)]
    nli: NLISettings


class EvalSettings(_Section):
    recall_at: list[PositiveInt]
    default_now: datetime
    max_regression: Probability
    calibration_c: PositiveFloat


class APISettings(_Section):
    host: str
    port: Annotated[int, Field(ge=1, le=65535)]


class Secrets(_Section):
    """Read from the process environment (after .env is loaded). Never from YAML."""

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    ollama_host: str | None = None

    @classmethod
    def from_env(cls) -> Secrets:
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            ollama_host=os.getenv("OLLAMA_HOST") or None,
        )


class Settings(_Section):
    paths: PathsSettings
    kb: KBSettings
    embeddings: EmbeddingSettings
    vector_store: VectorStoreSettings
    retrieval: RetrievalSettings
    facts: FactSettings
    intent: IntentSettings
    entities: EntitySettings
    thresholds: ThresholdSettings
    llm: LLMSettings
    generation: GenerationSettings
    scoring: ScoringSettings
    eval: EvalSettings
    api: APISettings
    secrets: Secrets = Field(default_factory=Secrets, exclude=True, repr=False)

    @model_validator(mode="after")
    def _recall_within_top_k(self) -> Settings:
        if self.eval.recall_at and max(self.eval.recall_at) > self.retrieval.top_k:
            raise ValueError("eval.recall_at values must be <= retrieval.top_k")
        return self

    @model_validator(mode="after")
    def _periods_within_day(self) -> Settings:
        for name, period in self.entities.periods:
            if period.start < self.kb.day_start or period.end > self.kb.day_end:
                raise ValueError(f"entities.periods.{name} must lie within kb.day_start..kb.day_end")
        return self


def load_settings(path: Path | str | None = None) -> Settings:
    """Load and validate settings. Path precedence: argument, $RAG_SETTINGS_PATH, default."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    settings_path = Path(path or os.getenv(SETTINGS_PATH_ENV) or DEFAULT_SETTINGS_PATH)
    with settings_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if "secrets" in data:
        raise ValueError(f"{settings_path}: secrets must come from .env, not the settings file")
    data["secrets"] = Secrets.from_env()
    return Settings.model_validate(data, context={"root": PROJECT_ROOT})


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings. Call ``get_settings.cache_clear()`` in tests to reload."""
    return load_settings()
