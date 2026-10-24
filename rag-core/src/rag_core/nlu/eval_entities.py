"""Evaluate entity extraction + linking on ``paths.entity_test`` and write ``paths.entity_report``.

Run: ``python -m rag_core.nlu.eval_entities``  (or ``just eval-entities``; the pytest suite runs it too)

A predicted mention matches a gold entity of the same type when:
- gold ``id``: the mention is linked (not ambiguous) to exactly that id;
- gold ``ambiguous``: the mention is flagged ambiguous with exactly those candidates;
- gold ``id: null``: the mention is unlinked and overlaps the gold ``text`` in the question.
Matching is one-to-one per question. Precision, recall and F1 are micro-averaged per type and
overall. Scores and surface spans of linked mentions are not graded.

There is no training, but the rules were written by the same author as the gold set: add new
questions for failures instead of bending rules to individual test items.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag_core.config import PROJECT_ROOT, Settings, get_settings
from rag_core.contracts import EntityType
from rag_core.nlu.entities import EntityExtractor, EntityMention
from rag_core.nlu.intent import file_sha256


class GoldEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EntityType
    id: str | None = None
    ambiguous: list[str] | None = Field(default=None, min_length=2)
    text: str | None = Field(default=None, description="Required for an unlinked entity (id null).")

    @model_validator(mode="after")
    def _one_kind(self) -> GoldEntity:
        if self.ambiguous is not None and self.id is not None:
            raise ValueError("give either id or ambiguous, not both")
        if self.ambiguous is None and self.id is None and not self.text:
            raise ValueError("an unlinked gold entity (id null) needs its text")
        return self

    def label(self) -> str:
        if self.ambiguous:
            return f"{self.type.value}[ambiguous: {' | '.join(self.ambiguous)}]"
        return self.id or f'{self.type.value} "{self.text}" (unlinked)'


class EntityExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    now: datetime | None = Field(default=None, description="Clock for 'now', 'today', 'tomorrow'.")
    entities: list[GoldEntity]

    @model_validator(mode="after")
    def _unlinked_text_occurs(self) -> EntityExample:
        for gold in self.entities:
            if gold.text and gold.text.casefold() not in self.text.casefold():
                raise ValueError(f"gold text {gold.text!r} does not occur in {self.text!r}")
        return self


def load_entity_examples(path: Path) -> list[EntityExample]:
    with path.open(encoding="utf-8") as fh:
        return [EntityExample.model_validate_json(line) for line in fh if line.strip()]


def mention_label(m: EntityMention) -> str:
    if m.ambiguous:
        return f"{m.type.value}[ambiguous: {' | '.join(c.canonical_id for c in m.candidates)}]"
    return m.canonical_id or f'{m.type.value} "{m.text}" (unlinked)'


def _matches(gold: GoldEntity, pred: EntityMention, question: str) -> bool:
    if gold.type != pred.type:
        return False
    if gold.ambiguous is not None:
        return pred.ambiguous and {c.canonical_id for c in pred.candidates} == set(gold.ambiguous)
    if gold.id is not None:
        return not pred.ambiguous and pred.canonical_id == gold.id
    start = question.casefold().find(gold.text.casefold())
    end = start + len(gold.text)
    return pred.canonical_id is None and not pred.ambiguous and pred.start < end and pred.end > start


@dataclass
class ExampleResult:
    example: EntityExample
    predicted: list[EntityMention]
    missed: list[GoldEntity]
    spurious: list[EntityMention]
    matched: list[tuple[GoldEntity, EntityMention]]


@dataclass
class EntityEvalResult:
    examples: list[ExampleResult]
    tp: Counter = field(default_factory=Counter)
    fp: Counter = field(default_factory=Counter)
    fn: Counter = field(default_factory=Counter)

    @staticmethod
    def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
        p = tp / (tp + fp) if tp + fp else 1.0
        r = tp / (tp + fn) if tp + fn else 1.0
        return p, r, (2 * p * r / (p + r) if p + r else 0.0)

    def per_type(self, type_: EntityType) -> tuple[float, float, float]:
        return self._prf(self.tp[type_], self.fp[type_], self.fn[type_])

    @property
    def micro(self) -> tuple[float, float, float]:
        return self._prf(sum(self.tp.values()), sum(self.fp.values()), sum(self.fn.values()))

    @property
    def types(self) -> list[EntityType]:
        seen = set(self.tp) | set(self.fp) | set(self.fn)
        return [t for t in EntityType if t in seen]

    @property
    def ambiguity(self) -> tuple[int, int, int]:
        """(gold ambiguous mentions, flagged with the right candidates, silently linked to one of them)."""
        total = flagged = silent = 0
        for r in self.examples:
            for gold in (g for g in r.example.entities if g.ambiguous):
                total += 1
                flagged += any(g is gold for g, _ in r.matched)
                silent += any(
                    p.type == gold.type and not p.ambiguous and p.canonical_id in gold.ambiguous for p in r.predicted
                )
        return total, flagged, silent


def evaluate(extractor: EntityExtractor, examples: Sequence[EntityExample]) -> EntityEvalResult:
    result = EntityEvalResult(examples=[])
    for ex in examples:
        predicted = extractor.extract(ex.text, now=ex.now).mentions
        unused = list(predicted)
        matched, missed = [], []
        for gold in ex.entities:
            pred = next((p for p in unused if _matches(gold, p, ex.text)), None)
            if pred is None:
                missed.append(gold)
                result.fn[gold.type] += 1
            else:
                unused.remove(pred)
                matched.append((gold, pred))
                result.tp[gold.type] += 1
        for pred in unused:
            result.fp[pred.type] += 1
        result.examples.append(ExampleResult(ex, predicted, missed, unused, matched))
    return result


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def render_report(result: EntityEvalResult, *, settings: Settings, n_test: int) -> str:
    target = settings.entities.target_f1
    p, r, f1 = result.micro
    total, flagged, silent = result.ambiguity
    verdict = "PASS" if f1 >= target and flagged == total and not silent else "FAIL"
    out = [
        "# Entity extraction + linking: evaluation",
        "",
        f"- Test set: `{_display(settings.paths.entity_test)}`, {n_test} questions "
        f"(sha256 `{file_sha256(settings.paths.entity_test)[:12]}`).",
        f"- Fuzzy cut-off `thresholds.entity_match_min` = {settings.thresholds.entity_match_min:g}, "
        f"ambiguity margin {settings.entities.ambiguity_margin:g} points.",
        f"- Target: micro F1 >= {target:.2f} and every ambiguous name flagged: **{verdict}** "
        f"(F1 {f1:.3f}; ambiguous flagged {flagged}/{total}, silently resolved {silent}).",
        "",
        "## Precision / recall / F1 by type",
        "",
        "| type | precision | recall | F1 | gold | predicted |",
        "|---|---|---|---|---|---|",
    ]
    for t in result.types:
        tp, fp, fn = result.tp[t], result.fp[t], result.fn[t]
        tp_, tr_, tf_ = result.per_type(t)
        out.append(f"| {t.value} | {tp_:.2f} | {tr_:.2f} | {tf_:.2f} | {tp + fn} | {tp + fp} |")
    out.append(
        f"| **all (micro)** | **{p:.3f}** | **{r:.3f}** | **{f1:.3f}** | {sum(result.tp.values()) + sum(result.fn.values())} "
        f"| {sum(result.tp.values()) + sum(result.fp.values())} |"
    )

    errors = [x for x in result.examples if x.missed or x.spurious]
    out += ["", f"## Errors: {len(errors)} questions", ""]
    if errors:
        out += ["| question | missed (gold) | spurious (predicted) |", "|---|---|---|"]
        for x in errors:
            missed = "<br>".join(g.label() for g in x.missed) or "·"
            spurious = "<br>".join(mention_label(m) for m in x.spurious) or "·"
            out.append(f"| {x.example.text} | {missed} | {spurious} |")
    else:
        out.append("None.")
    out += [
        "",
        "Limits: the gold set and the rules were written by the same author, so this is optimistic. "
        "Label real kiosk questions before trusting it. Add new questions for failures instead of "
        "bending rules to individual test items.",
    ]
    return "\n".join(out) + "\n"


def run_evaluation(settings: Settings, extractor: EntityExtractor | None = None) -> tuple[Path, EntityEvalResult]:
    extractor = extractor or EntityExtractor.from_settings(settings)
    examples = load_entity_examples(settings.paths.entity_test)
    result = evaluate(extractor, examples)
    path = settings.paths.entity_report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(result, settings=settings, n_test=len(examples)), encoding="utf-8")
    return path, result


def main() -> None:
    settings = get_settings()
    path, result = run_evaluation(settings)
    p, r, f1 = result.micro
    total, flagged, silent = result.ambiguity
    print(f"entities: precision {p:.3f}, recall {r:.3f}, F1 {f1:.3f}")
    print(f"ambiguous names: {flagged}/{total} flagged, {silent} silently resolved")
    print(f"report: {_display(path)}")


if __name__ == "__main__":
    main()
