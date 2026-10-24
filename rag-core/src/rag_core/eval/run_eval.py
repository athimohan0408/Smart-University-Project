"""End-to-end evaluation of the four metrics this module owns, plus latency for the team.

Run: ``python -m rag_core.eval.run_eval``  (``just eval``; ``--split dev|test|all``, default test)

Writes ``reports/eval_<timestamp>.md`` + ``.json`` and ``reports/failures.csv`` (every wrong case,
with predicted vs gold, retrieved fact ids and the answer). ``--write-baseline`` also stores the
headline metrics in ``reports/baseline.json`` (versioned); the regression test compares to it.

Generation runs in template mode (deterministic, no LLM), as tests and eval must never depend on a
live LLM. ``--llm`` uses the configured provider instead, and ``--judge`` adds an LLM-as-judge
column (never the headline number); both need an API key.

Metrics (definitions in :mod:`rag_core.eval.qa`):
1. Intent: top-1 accuracy, macro F1, confusion matrix.
2. Retrieval: Hit@1, Recall@N, MRR of gold_fact_ids among the facts the pipeline retrieved
   (predicted intent and entities: the real path).
3. QA accuracy: keypoints present and not escalated / escalated for unanswerable questions.
4. Hallucination: share of answered questions with an unsupported claim, and share of unanswerable
   questions that got a confident answer.
Plus escalation precision / recall, coverage, entity linking F1, latency per stage, and the
breakdown by difficulty and category.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rag_core.config import PROJECT_ROOT, Settings, get_settings
from rag_core.contracts import EntityType, Intent
from rag_core.eval.qa import (
    QAExample,
    contradicted_values,
    forbidden_found,
    is_correct,
    load_qa,
    missing_keypoints,
)
from rag_core.nlu.eval_entities import _matches as entity_matches
from rag_core.nlu.intent import file_sha256
from rag_core.pipeline import Pipeline, Trace
from rag_core.retrieval.eval_retrieval import first_hit_rank, recall_at

# name -> True when higher is better. The regression test checks exactly these.
HEADLINE = {
    "intent_accuracy": True,
    "retrieval_recall_at_k": True,
    "qa_accuracy": True,
    "hallucination_rate": False,
    "unanswerable_answered_rate": False,
}
STAGES = ("nlu", "retrieval", "generation", "scoring", "total")
_OTHER_GOLD_TYPES = frozenset({EntityType.ROOM, EntityType.COURSE, EntityType.PROGRAMME, EntityType.TOPIC})


@dataclass
class Row:
    ex: QAExample
    trace: Trace
    correct: bool
    hallucinated: bool
    hallucination_reasons: list[str]
    missing: list[str]
    forbidden: list[str]
    entity_tp: int
    entity_fn: int
    entity_fp: int
    judge: bool | None = None
    judge_reason: str | None = None
    wrong_person: bool = False

    @property
    def result(self):
        return self.trace.result

    @property
    def answered(self) -> bool:
        return not self.result.escalate

    def failure_types(self) -> list[str]:
        out = []
        r = self.result
        if r.intent != self.ex.gold_intent:
            out.append("intent")
        if self.entity_fn or self.entity_fp:
            out.append("entities")
        if self.ex.gold_fact_ids and recall_at(self.trace.retrieved_ids, self.ex.gold_fact_ids, len(self.trace.retrieved_ids) or 1) < 1:
            out.append("retrieval")
        if self.ex.answerable and r.escalate:
            out.append(f"escalated:{r.escalate_reason}")
        if not self.ex.answerable and not r.escalate:
            out.append("answered_unanswerable")
        if self.missing and not r.escalate:
            out.append("missing_keypoints")
        if self.forbidden:
            out.append("forbidden")
        if self.hallucinated:
            out.append("hallucination")
        if self.wrong_person:
            out.append("wrong_person")
        return out


def grade(ex: QAExample, trace: Trace, pipeline: Pipeline) -> Row:
    r = trace.result
    reasons: list[str] = []
    if not r.escalate:
        retrieved = {f.fact_id: f for f in trace.retrieved}
        if any(s not in retrieved for s in r.sources):
            reasons.append("cited_unretrieved")
        cited = [retrieved[s] for s in r.sources if s in retrieved]
        draft = trace.generation.draft.model_copy(update={"answer": r.answer, "cited_fact_ids": r.sources})
        check = pipeline.checker.check(draft, trace.retrieved, question=trace.question, question_mentions=trace.mentions,
                                       intent=r.intent)
        if not check.passed:
            reasons.append("ungrounded:" + ",".join(i.span for i in check.issues))
        if forbidden_found(r.answer, ex.forbidden):
            reasons.append("forbidden")
        gold_text = " ".join(pipeline.retriever.catalog.get(f).text for f in ex.gold_fact_ids if f in pipeline.retriever.catalog)
        keys = [o for k in ex.gold_answer_keypoints for o in (k if isinstance(k, list) else [k])]
        allowed = " ".join([gold_text, *(f.text for f in cited), " ".join(keys)])
        if bad := contradicted_values(r.answer, allowed):
            reasons.append("contradicts:" + ",".join(bad))

    unused = list(trace.mentions)
    tp = fn = 0
    for gold in ex.gold_entities:
        hit = next((m for m in unused if entity_matches(gold, m, ex.question)), None)
        if hit is None:
            fn += 1
        else:
            unused.remove(hit)
            tp += 1
    fp = sum(1 for m in unused if m.method != "context")
    return Row(
        ex=ex, trace=trace, correct=is_correct(ex, r.answer, r.escalate, trace.decision.clarification and not r.sources),
        hallucinated=bool(reasons),
        hallucination_reasons=reasons, missing=missing_keypoints(r.answer, ex.gold_answer_keypoints),
        forbidden=forbidden_found(r.answer, ex.forbidden), entity_tp=tp, entity_fn=fn, entity_fp=fp,
        wrong_person=wrong_person_answer(ex, trace, pipeline),
    )


def wrong_person_answer(ex: QAExample, trace: Trace, pipeline: Pipeline) -> bool:
    """Judged against the GOLD entities, not the system's own checker: an answered (not a clarifying)
    reply that talks about a faculty member other than the one the question is about."""
    r = trace.result
    if r.escalate or trace.decision.clarification:
        return False
    named = {m.canonical_id for m in pipeline.extractor.extract(r.answer).mentions
             if m.type == EntityType.FACULTY and m.canonical_id}
    if not named:
        return False
    faculty = [g for g in ex.gold_entities if g.type == EntityType.FACULTY]
    gold_ids = {g.id for g in faculty if g.id}
    if gold_ids:
        return not named & gold_ids
    if any(g.ambiguous for g in faculty):
        return True  # picked one of several people without asking
    unknown = any(g.id is None and not g.ambiguous for g in faculty)
    other = any(g.type in _OTHER_GOLD_TYPES and g.id for g in ex.gold_entities)
    return unknown and not other


# --------------------------------------------------------------------------- metrics


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def qa_metrics(rows: Sequence[Row], top_k: int) -> dict:
    n = len(rows)
    answered = [r for r in rows if r.answered]
    unanswerable = [r for r in rows if not r.ex.answerable]
    gold_esc = [r for r in rows if not r.ex.answerable]
    pred_esc = [r for r in rows if r.result.escalate]
    tp_esc = sum(1 for r in pred_esc if not r.ex.answerable)
    with_gold = [r for r in rows if r.ex.gold_fact_ids and r.trace.retrieval is not None]
    ranks = [first_hit_rank(r.trace.retrieved_ids, r.ex.gold_fact_ids) for r in with_gold]
    tp_e, fn_e, fp_e = (sum(getattr(r, k) for r in rows) for k in ("entity_tp", "entity_fn", "entity_fp"))
    p_e, r_e = _ratio(tp_e, tp_e + fp_e) or 0.0, _ratio(tp_e, tp_e + fn_e) or 0.0
    judged = [r for r in rows if r.judge is not None]
    return {
        "n": n,
        "intent_accuracy": _ratio(sum(r.result.intent == r.ex.gold_intent for r in rows), n),
        "intent_macro_f1": intent_macro_f1(rows),
        "retrieval_n": len(with_gold),
        "retrieval_hit_at_1": _ratio(sum(rank == 1 for rank in ranks), len(with_gold)),
        "retrieval_recall_at_k": _ratio(sum(recall_at(r.trace.retrieved_ids, r.ex.gold_fact_ids, top_k) for r in with_gold), len(with_gold)),
        "retrieval_mrr": _ratio(sum(1 / rank if rank else 0.0 for rank in ranks), len(with_gold)),
        "qa_accuracy": _ratio(sum(r.correct for r in rows), n),
        "qa_accuracy_answerable": _ratio(sum(r.correct for r in rows if r.ex.answerable), sum(r.ex.answerable for r in rows)),
        "qa_accuracy_unanswerable": _ratio(sum(r.correct for r in unanswerable), len(unanswerable)),
        "hallucination_rate": _ratio(sum(r.hallucinated for r in answered), len(answered)) or 0.0,
        # a clarifying question that states no facts is not a confident answer
        "unanswerable_answered_rate": _ratio(
            sum(r.answered and not (r.trace.decision.clarification and not r.result.sources) for r in unanswerable),
            len(unanswerable)) or 0.0,
        "escalation_precision": _ratio(tp_esc, len(pred_esc)),
        "escalation_recall": _ratio(tp_esc, len(gold_esc)),
        "coverage": _ratio(len(answered), n),
        "accuracy_of_answered": _ratio(sum(r.correct for r in answered), len(answered)),
        "entity_f1": (2 * p_e * r_e / (p_e + r_e)) if p_e + r_e else 0.0,
        "wrong_person_answers": sum(r.wrong_person for r in rows),
        "wrong_person_rate": _ratio(sum(r.wrong_person for r in answered), len(answered)) or 0.0,
        "judge_accuracy": _ratio(sum(bool(r.judge) for r in judged), len(judged)) if judged else None,
    }


def intent_macro_f1(rows: Sequence[Row]) -> float | None:
    labels = sorted({r.ex.gold_intent for r in rows} | {r.result.intent for r in rows})
    f1s = []
    for label in labels:
        tp = sum(r.result.intent == label and r.ex.gold_intent == label for r in rows)
        fp = sum(r.result.intent == label and r.ex.gold_intent != label for r in rows)
        fn = sum(r.result.intent != label and r.ex.gold_intent == label for r in rows)
        if tp + fp + fn == 0:
            continue
        p = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * p * rec / (p + rec) if p + rec else 0.0)
    return sum(f1s) / len(f1s) if f1s else None


def latency_stats(rows: Sequence[Row]) -> dict:
    out = {}
    for stage in STAGES:
        values = sorted(r.result.latency_ms.get(stage, 0.0) for r in rows)
        if not values:
            continue
        p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
        out[stage] = {"mean": statistics.fmean(values), "p50": statistics.median(values), "p95": p95}
    return out


def breakdown(rows: Sequence[Row], key) -> dict[str, dict]:
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return {
        name: {
            "n": len(g),
            "qa_accuracy": sum(r.correct for r in g) / len(g),
            "escalated": sum(r.result.escalate for r in g) / len(g),
            "hallucinated": sum(r.hallucinated for r in g),
            "intent_accuracy": sum(r.result.intent == r.ex.gold_intent for r in g) / len(g),
        }
        for name, g in sorted(groups.items())
    }


def confusion(rows: Sequence[Row]) -> tuple[list[str], dict[tuple[str, str], int]]:
    labels = [i.value for i in Intent if any(r.ex.gold_intent == i or r.result.intent == i for r in rows)]
    counts = Counter((r.ex.gold_intent.value, r.result.intent.value) for r in rows)
    return labels, counts


# --------------------------------------------------------------------------- LLM judge (optional)

JUDGE_SYSTEM = (
    "You grade answers of a university receptionist kiosk. Given the question, the facts the answer "
    "must rely on, the required key points and the answer, decide if the answer is correct, complete "
    "and does not state anything beyond the facts. For a question marked unanswerable, the answer is "
    'correct only if it declines and refers the visitor to the receptionist. Reply with JSON only: '
    '{"correct": true|false, "reason": "<one short sentence>"}'
)


def judge_rows(rows: Sequence[Row], pipeline: Pipeline, settings: Settings) -> str | None:
    from pydantic import BaseModel

    from rag_core.generation.llm_client import LLMError, LLMUnavailable, make_llm_client

    class Verdict(BaseModel):
        correct: bool
        reason: str

    try:
        llm = make_llm_client(settings)
    except LLMUnavailable as exc:
        return f"LLM judge not run: {exc}"
    catalog = pipeline.retriever.catalog
    for r in rows:
        facts = "\n".join(f"- {catalog.get(f).text}" for f in r.ex.gold_fact_ids if f in catalog) or "- (none)"
        user = (f"QUESTION: {r.ex.question}\nANSWERABLE: {r.ex.answerable}\nFACTS:\n{facts}\n"
                f"KEY POINTS: {r.ex.gold_answer_keypoints}\nANSWER: {r.result.answer}")
        try:
            verdict = llm.generate(JUDGE_SYSTEM, user, Verdict)
            r.judge, r.judge_reason = verdict["correct"], verdict["reason"]
        except LLMError as exc:
            r.judge_reason = f"judge error: {exc}"
    return None


# --------------------------------------------------------------------------- report


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


@dataclass
class EvalRun:
    split: str
    rows: list[Row]
    metrics: dict
    latency: dict
    by_difficulty: dict
    by_category: dict
    settings_note: dict = field(default_factory=dict)
    judge_note: str | None = None

    def headline(self) -> dict[str, float]:
        return {k: self.metrics[k] for k in HEADLINE if self.metrics.get(k) is not None}


def render_markdown(run: EvalRun, settings: Settings, *, baseline: dict | None, stamp: str) -> str:
    m, top_k = run.metrics, settings.retrieval.top_k
    out = [
        f"# End-to-end evaluation ({run.split} split)",
        "",
        f"- Run {stamp}. Gold set `{_display(settings.paths.eval_testset)}` (sha256 `{file_sha256(settings.paths.eval_testset)[:12]}`), "
        f"{m['n']} {run.split} questions.",
        f"- **Mode: {run.settings_note.get('mode', 'template').upper()}**. Answer sources: "
        f"{run.settings_note.get('answer_sources', {})}."
        + (f" LLM usage: {run.settings_note['llm_usage']}." if "llm_usage" in run.settings_note else ""),
        f"- Generation: **{run.settings_note['generation']}**; confidence: {run.settings_note['confidence']}; "
        f"escalation threshold {settings.thresholds.answer_min_confidence:.2f}.",
        "",
        "## Headline metrics",
        "",
        "| metric | value | baseline | change |",
        "|---|---|---|---|",
    ]
    names = {
        "intent_accuracy": "1. Intent accuracy (top-1)",
        "retrieval_recall_at_k": f"2. Retrieval Recall@{top_k}",
        "qa_accuracy": "3. QA accuracy",
        "hallucination_rate": "4a. Hallucination rate (answered questions)",
        "unanswerable_answered_rate": "4b. Unanswerable questions answered confidently",
    }
    for key, label in names.items():
        value = m.get(key)
        base = (baseline or {}).get(key)
        change = "" if base is None or value is None else f"{100 * (value - base):+.1f} pts"
        out.append(f"| {label} | {_pct(value)} | {_pct(base) if base is not None else '-'} | {change} |")
    out += [
        "",
        "## All metrics",
        "",
        "| metric | value |",
        "|---|---|",
        f"| intent macro F1 | {m['intent_macro_f1']:.3f} |",
        f"| retrieval Hit@1 / Recall@{top_k} / MRR (n={m['retrieval_n']}) | {m['retrieval_hit_at_1']:.3f} / {m['retrieval_recall_at_k']:.3f} / {m['retrieval_mrr']:.3f} |",
        f"| QA accuracy: answerable / unanswerable | {_pct(m['qa_accuracy_answerable'])} / {_pct(m['qa_accuracy_unanswerable'])} |",
        f"| escalation precision / recall | {_pct(m['escalation_precision'])} / {_pct(m['escalation_recall'])} |",
        f"| coverage (answered) / accuracy of answered | {_pct(m['coverage'])} / {_pct(m['accuracy_of_answered'])} |",
        f"| entity linking micro F1 | {m['entity_f1']:.3f} |",
        f"| wrong-person answers (answered about someone other than the gold person) | {m['wrong_person_answers']} "
        f"({_pct(m['wrong_person_rate'])} of answered) |",
        f"| LLM-as-judge accuracy (not the headline) | {_pct(m['judge_accuracy']) if m['judge_accuracy'] is not None else (run.judge_note or 'not run (--judge)')} |",
        "",
        "## Latency per stage (ms)",
        "",
        "| stage | mean | p50 | p95 |",
        "|---|---|---|---|",
    ]
    for stage, s in run.latency.items():
        out.append(f"| {stage} | {s['mean']:.1f} | {s['p50']:.1f} | {s['p95']:.1f} |")
    for title, table in (("difficulty", run.by_difficulty), ("category", run.by_category)):
        out += ["", f"## By {title}", "", f"| {title} | n | QA accuracy | escalated | hallucinated | intent accuracy |", "|---|---|---|---|---|---|"]
        for name, b in table.items():
            out.append(f"| {name} | {b['n']} | {_pct(b['qa_accuracy'])} | {_pct(b['escalated'])} | {b['hallucinated']} | {_pct(b['intent_accuracy'])} |")
    labels, counts = confusion(run.rows)
    short = {lab: lab.replace("_", " ")[:12] for lab in labels}
    out += ["", "## Intent confusion matrix (rows gold, columns predicted)", "",
            "| gold \\ predicted | " + " | ".join(short[x] for x in labels) + " |", "|---|" + "---|" * len(labels)]
    for g in labels:
        out.append(f"| {g} | " + " | ".join(str(counts.get((g, p), "")) or "" for p in labels) + " |")
    wrong = [r for r in run.rows if not r.correct or r.hallucinated or r.wrong_person]
    out += ["", f"## Wrong answers: {len(wrong)} (details in failures.csv)", ""]
    if wrong:
        out += ["| id | question | failure | answer |", "|---|---|---|---|"]
        for r in wrong:
            answer = r.result.answer.replace("|", "/")
            out.append(f"| {r.ex.id} | {r.ex.question} | {', '.join(r.failure_types())} | {answer} |")
    out += ["", "Limits: the gold set was written by the same author as the rules and templates, so these numbers are "
            "optimistic. Label real kiosk questions before trusting them."]
    return "\n".join(out) + "\n"


def write_failures(rows: Sequence[Row], path: Path) -> None:
    cols = ["id", "split", "category", "difficulty", "question", "failure", "gold_intent", "pred_intent",
            "gold_entities", "pred_entities", "gold_fact_ids", "retrieved_fact_ids", "cited_fact_ids", "answerable",
            "escalate", "escalate_reason", "confidence", "answer_source", "missing_keypoints", "hallucination", "answer",
            "judge", "judge_reason"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            if r.correct and not r.hallucinated and not r.wrong_person:
                continue
            res = r.result
            w.writerow([
                r.ex.id, r.ex.split, r.ex.category, r.ex.difficulty, r.ex.question, ";".join(r.failure_types()),
                r.ex.gold_intent.value, res.intent.value,
                ";".join(g.label() for g in r.ex.gold_entities),
                ";".join(e.canonical_id or f"{e.type.value}:\"{e.text}\" (unlinked)" for e in res.entities),
                ";".join(r.ex.gold_fact_ids), ";".join(r.trace.retrieved_ids), ";".join(res.sources),
                r.ex.answerable, res.escalate, res.escalate_reason or "", f"{res.confidence:.3f}",
                r.trace.generation.source, ";".join(r.missing), ";".join(r.hallucination_reasons), res.answer,
                "" if r.judge is None else r.judge, r.judge_reason or "",
            ])


# --------------------------------------------------------------------------- run


def eval_settings(settings: Settings, *, use_llm: bool) -> Settings:
    if use_llm:
        return settings
    return settings.model_copy(update={"generation": settings.generation.model_copy(update={"mode": "template"})})


def run_examples(pipeline: Pipeline, examples: Sequence[QAExample], default_now: datetime) -> list[Row]:
    rows = []
    for ex in examples:
        trace = pipeline.run(ex.question, {}, ex.now or default_now)
        rows.append(grade(ex, trace, pipeline))
    return rows


def evaluate(
    settings: Settings,
    *,
    split: str = "test",
    use_llm: bool = False,
    judge: bool = False,
    pipeline: Pipeline | None = None,
) -> EvalRun:
    s = eval_settings(settings, use_llm=use_llm)
    pipeline = pipeline or Pipeline.from_settings(s, log_queries=False)
    if use_llm and not pipeline.generator.has_llm:
        # Never let a template run be reported as a live-LLM run.
        raise SystemExit(f"--llm requested but no LLM is available ({pipeline.generator.llm_status}); nothing was run")
    pipeline.warmup()
    examples = load_qa(s.paths.eval_testset, split)
    rows = run_examples(pipeline, examples, s.eval.default_now)
    judge_note = judge_rows(rows, pipeline, s) if judge else None
    note = {
        "mode": "live-llm" if use_llm else "template",
        "generation": f"LIVE LLM ({s.llm.provider} {s.llm.model}, effort {s.llm.effort})" if use_llm
        else "templates (no LLM)",
        "confidence": "calibrated" if pipeline.scorer.calibrated else "weighted formula (not calibrated)",
        "answer_sources": dict(Counter(r.trace.generation.source for r in rows)),
    }
    usage = getattr(getattr(pipeline.generator, "_llm", None), "usage", None)
    if use_llm and usage is not None:
        note["llm_usage"] = {
            "calls": usage.calls, "failures": usage.failures, "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": round(usage.cost_usd(s.llm.price_input_per_mtok, s.llm.price_output_per_mtok), 4),
        }
    return EvalRun(
        split=split, rows=rows, metrics=qa_metrics(rows, s.retrieval.top_k), latency=latency_stats(rows),
        by_difficulty=breakdown(rows, lambda r: r.ex.difficulty), by_category=breakdown(rows, lambda r: r.ex.category),
        settings_note=note, judge_note=judge_note,
    )


def load_baseline(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("headline")


def write_reports(run: EvalRun, settings: Settings) -> tuple[Path, Path, Path]:
    out_dir = settings.paths.eval_reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "_llm" if run.settings_note.get("mode") == "live-llm" else ""  # live-LLM results never mix with template ones
    md, js, fail = out_dir / f"eval_{stamp}{tag}.md", out_dir / f"eval_{stamp}{tag}.json", out_dir / f"failures{tag}.csv"
    md.write_text(render_markdown(run, settings, baseline=load_baseline(settings.paths.eval_baseline), stamp=stamp), encoding="utf-8")
    js.write_text(json.dumps({
        "split": run.split, "timestamp": stamp, "gold_sha256": file_sha256(settings.paths.eval_testset),
        "settings": run.settings_note, "headline": run.headline(), "metrics": run.metrics, "latency_ms": run.latency,
        "by_difficulty": run.by_difficulty, "by_category": run.by_category,
        "questions": [{
            "id": r.ex.id, "correct": r.correct, "hallucinated": r.hallucinated, "wrong_person": r.wrong_person,
            "intent": r.result.intent.value,
            "escalate": r.result.escalate, "escalate_reason": r.result.escalate_reason,
            "confidence": r.result.confidence, "sources": r.result.sources, "retrieved": r.trace.retrieved_ids,
            "answer": r.result.answer, "failure": r.failure_types(),
        } for r in run.rows],
    }, indent=2), encoding="utf-8")
    write_failures(run.rows, fail)
    return md, js, fail


def write_baseline(run: EvalRun, settings: Settings) -> Path:
    path = settings.paths.eval_baseline
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "split": run.split, "gold_sha256": file_sha256(settings.paths.eval_testset),
        "written": datetime.now().isoformat(timespec="seconds"), "settings": run.settings_note,
        "headline": run.headline(),
    }, indent=2) + "\n", encoding="utf-8")
    return path


def regressions(headline: dict[str, float], baseline: dict[str, float], tolerance: float) -> list[str]:
    """Headline metrics worse than the baseline by more than ``tolerance`` (absolute)."""
    out = []
    for key, higher_better in HEADLINE.items():
        if key not in baseline or key not in headline:
            continue
        drop = (baseline[key] - headline[key]) if higher_better else (headline[key] - baseline[key])
        if drop > tolerance + 1e-9:
            out.append(f"{key}: {headline[key]:.3f} vs baseline {baseline[key]:.3f}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_core.eval.run_eval")
    parser.add_argument("--split", default="test", choices=["dev", "test", "all"])
    parser.add_argument("--llm", action="store_true", help="generate with the configured LLM (needs an API key)")
    parser.add_argument("--judge", action="store_true", help="add an LLM-as-judge column (needs an API key)")
    parser.add_argument("--write-baseline", action="store_true", help="store the headline metrics in paths.eval_baseline")
    args = parser.parse_args(argv)

    settings = get_settings()
    run = evaluate(settings, split=args.split, use_llm=args.llm, judge=args.judge)
    md, js, fail = write_reports(run, settings)
    m = run.metrics
    print(f"{run.split}: {m['n']} questions | intent {_pct(m['intent_accuracy'])} | Recall@{settings.retrieval.top_k} "
          f"{_pct(m['retrieval_recall_at_k'])} | QA {_pct(m['qa_accuracy'])} | hallucination {_pct(m['hallucination_rate'])} "
          f"| unanswerable answered {_pct(m['unanswerable_answered_rate'])} | coverage {_pct(m['coverage'])} "
          f"| p95 total {run.latency['total']['p95']:.0f} ms")
    print(f"reports: {_display(md)}, {_display(js)}, {_display(fail)}")
    if args.write_baseline:
        print(f"baseline: {_display(write_baseline(run, settings))}")
    elif (run.split == "test" and not args.llm and (base := load_baseline(settings.paths.eval_baseline))
          and (bad := regressions(run.headline(), base, settings.eval.max_regression))):  # baseline is the test split
        print("REGRESSION vs baseline: " + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
