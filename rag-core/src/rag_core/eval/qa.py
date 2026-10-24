"""The end-to-end QA gold set (``paths.eval_testset``) and how one answer is graded.

Each line: id, question, gold_intent, gold_entities (as in entities_test.jsonl), gold_fact_ids,
gold_answer_keypoints, answerable, category, difficulty, split (dev | test), and optionally
``forbidden`` (strings a correct answer must NOT contain, for adversarial questions) and ``now``.

Grading (all string matching is on :func:`normalise`d text, with clock times reduced to 24h):
- answerable: correct = every keypoint in the answer, no forbidden string, and not escalated.
  For an ambiguous name the keypoints are the candidates, so the right answer is the question.
  A keypoint may be a list of acceptable alternatives (any one counts), so a value can be required
  without dictating its wording; plain-string keypoints (all of the frozen test split) grade as before.
- unanswerable: correct = escalated (any reason) or a clarifying question that states no facts
  ("do you mean Dr X?", the documented policy for a name that can't be confidently identified),
  and no forbidden string.
- hallucinated (answered questions only): the final answer fails the grounding check against its
  cited facts, cites a fact that wasn't retrieved, contains a forbidden string, or states an email
  or room number that is in neither the gold facts nor the cited facts (keypoint contradiction).

The split is fixed in the file (30% dev / 70% test, stratified by category, seed 13). Tune
thresholds and calibration on dev only; report test.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rag_core.contracts import Intent
from rag_core.nlu.eval_entities import GoldEntity
from rag_core.scoring.grounding_check import _EMAIL, _NUMBER, _ROOM

# similar_name: a name that shares a first or last name with a faculty member ("Dr Nisha Sharma"), or a
# real name that could be confused with another; the answer must never be about the wrong person.
Difficulty = Literal[
    "easy", "paraphrase", "misspelt", "asr_noise", "multi_entity", "off_floor", "ambiguous", "adversarial", "similar_name",
]


class QAExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str = Field(min_length=1)
    gold_intent: Intent
    gold_entities: list[GoldEntity] = Field(default_factory=list)
    gold_fact_ids: list[str] = Field(default_factory=list)
    gold_answer_keypoints: list[str | list[str]] = Field(default_factory=list)
    answerable: bool
    category: str
    difficulty: Difficulty
    split: Literal["dev", "test"]
    forbidden: list[str] = Field(default_factory=list)
    now: datetime | None = None


def load_qa(path: Path, split: str | None = None) -> list[QAExample]:
    with path.open(encoding="utf-8") as fh:
        rows = [QAExample.model_validate_json(line) for line in fh if line.strip()]
    return [r for r in rows if split in (None, "all") or r.split == split]


_TIME = re.compile(r"(?<![\d.:])(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s?m\b\.?", re.IGNORECASE)
_PUNCT = re.compile(r"[\"'’,;!?()\[\]]")


def normalise(text: str) -> str:
    """casefold, '1:45 pm' -> '13:45', drop quotes/commas/brackets, single spaces."""

    def to24(m: re.Match[str]) -> str:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12:
            return m.group(0)
        hour = hour % 12 + (12 if m.group(3).lower() == "p" else 0)
        return f"{hour:02d}:{minute:02d}"

    text = _TIME.sub(to24, text)
    text = re.sub(r"(?<![\d:])(\d):(\d{2})(?!\d)", r"0\1:\2", text)  # 9:15 -> 09:15
    text = _PUNCT.sub(" ", text.casefold())
    return re.sub(r"\s+", " ", text).strip().rstrip(".")


def contains(answer: str, keypoint: str) -> bool:
    a, k = normalise(answer), normalise(keypoint)
    return re.search(rf"(?<![\w@]){re.escape(k)}(?![\w@])", a) is not None


def missing_keypoints(answer: str, keypoints: list[str | list[str]]) -> list[str]:
    """Keypoints not found; a list keypoint is satisfied by any of its alternatives."""
    out = []
    for k in keypoints:
        options = k if isinstance(k, list) else [k]
        if not any(contains(answer, o) for o in options):
            out.append(" | ".join(options))
    return out


def forbidden_found(answer: str, forbidden: list[str]) -> list[str]:
    return [f for f in forbidden if contains(answer, f)]


def is_correct(example: QAExample, answer: str, escalated: bool, clarification: bool = False) -> bool:
    if forbidden_found(answer, example.forbidden):
        return False
    if example.answerable:
        return not escalated and not missing_keypoints(answer, example.gold_answer_keypoints)
    return escalated or clarification


def contradicted_values(answer: str, allowed_text: str) -> list[str]:
    """Emails and room numbers stated in the answer that appear nowhere in ``allowed_text``."""
    allowed = allowed_text.casefold()
    out = [e for e in _EMAIL.findall(answer) if e.casefold() not in allowed]
    rooms = set(_ROOM.findall(answer)) | set(_NUMBER.findall(_EMAIL.sub(" ", answer)))
    out += [r for r in rooms if not re.search(rf"\b{re.escape(r.casefold())}\b", allowed)]
    return out
