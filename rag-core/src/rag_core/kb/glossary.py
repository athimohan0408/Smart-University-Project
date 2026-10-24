"""Glossary of programme and subject codes (data/kb/glossary.yaml), hand-maintained and versioned.

Ingest never writes the glossary; it only reports codes that are missing from it. Expansions are
trusted according to ``verified``:
- ``full_name: TODO``: no expansion. Never expand the code in an answer; say the code as written.
- ``verified: false``: usable, but answers relying on it get a lower confidence
  (``thresholds.unverified_glossary_weight``).
- ``verified: true``: confirmed by the department.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TODO = "TODO"


class GlossaryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    verified: bool = False

    @property
    def is_expanded(self) -> bool:
        return self.full_name.strip().upper() != TODO

    @model_validator(mode="after")
    def _todo_cannot_be_verified(self) -> GlossaryEntry:
        if self.verified and not self.is_expanded:
            raise ValueError("an entry with full_name TODO cannot be verified")
        return self


class Glossary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    programmes: dict[str, GlossaryEntry] = Field(default_factory=dict)
    subjects: dict[str, GlossaryEntry] = Field(default_factory=dict)

    @field_validator("programmes", "subjects")
    @classmethod
    def _keys_unique_ignoring_case(cls, entries: dict[str, GlossaryEntry]) -> dict[str, GlossaryEntry]:
        folded = [k.casefold() for k in entries]
        if dupes := sorted({k for k in entries if folded.count(k.casefold()) > 1}):
            raise ValueError(f"codes differ only by case: {dupes}")
        return entries

    def programme(self, code: str) -> GlossaryEntry | None:
        return _lookup(self.programmes, code)

    def subject(self, code: str) -> GlossaryEntry | None:
        return _lookup(self.subjects, code)


def _lookup(entries: dict[str, GlossaryEntry], code: str) -> GlossaryEntry | None:
    folded = code.strip().casefold()
    return next((entry for key, entry in entries.items() if key.casefold() == folded), None)


def load_glossary(path: Path) -> Glossary:
    with path.open(encoding="utf-8") as fh:
        return Glossary.model_validate(yaml.safe_load(fh) or {})


def confidence_weight(entry: GlossaryEntry | None, unverified_weight: float) -> float:
    """Multiplier for the confidence of an answer that expands a code using this entry."""
    if entry is None or not entry.is_expanded:
        return 0.0
    return 1.0 if entry.verified else unverified_weight
