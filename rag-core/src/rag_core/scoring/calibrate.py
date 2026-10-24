"""Calibrate the confidence score and pick the escalation threshold, on the QA DEV split only.

Run: ``python -m rag_core.scoring.calibrate``  (``just calibrate``)

1. Run the dev questions with the escalation threshold at 0, so every outcome is what the answer
   would be without a LOW_CONFIDENCE cut, and record the confidence features and whether that
   outcome was correct.
2. Fit a logistic regression (features -> correct) and save it to ``paths.confidence_model``.
   ECE before (the weighted formula) and after (out-of-fold predictions, so the after number isn't
   measured on the rows it was fitted to) go to ``paths.calibration_report``.
3. Sweep the threshold: escalate when confidence < t. For each t: coverage (share answered),
   accuracy of answered, hallucination rate of answered, overall QA accuracy. Pick the t with the
   highest coverage whose hallucination rate is <= ``scoring.max_hallucination_rate``; on equal
   coverage the higher accuracy, then the lower t. Written to ``paths.threshold_report``.

The threshold is NOT written into settings.yaml automatically: copy the recommended value into
``thresholds.answer_min_confidence`` after reading the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from rag_core.config import PROJECT_ROOT, Settings, get_settings
from rag_core.contracts import Intent
from rag_core.eval.qa import load_qa
from rag_core.eval.run_eval import Row, eval_settings, grade
from rag_core.nlu.intent import file_sha256
from rag_core.pipeline import Pipeline
from rag_core.scoring.confidence import FEATURE_ORDER, Calibrator, expected_calibration_error


@dataclass
class SweepPoint:
    threshold: float
    coverage: float
    accuracy_answered: float | None
    hallucination_answered: float
    qa_accuracy: float
    answered: int


def _pipeline(settings: Settings) -> Pipeline:
    s = eval_settings(settings, use_llm=False)
    s = s.model_copy(update={"thresholds": s.thresholds.model_copy(update={"answer_min_confidence": 0.0})})
    return Pipeline.from_settings(s, log_queries=False, use_calibration=False)


def collect(settings: Settings, pipeline: Pipeline | None = None) -> tuple[list[Row], Pipeline]:
    pipeline = pipeline or _pipeline(settings)
    pipeline.warmup()
    rows = []
    for ex in load_qa(settings.paths.eval_testset, "dev"):
        trace = pipeline.run(ex.question, {}, ex.now or settings.eval.default_now)
        rows.append(grade(ex, trace, pipeline))
    return rows, pipeline


def outcome_after_threshold(row: Row, confidence: float, t: float) -> tuple[bool, bool, bool]:
    """(answered, correct, hallucinated) if the escalation threshold were ``t``."""
    escalated = row.result.escalate
    if not escalated and row.trace.features is not None and confidence < t:
        escalated = True  # LOW_CONFIDENCE: the visitor hears the safe message
    if escalated:
        correct = not row.ex.answerable
        return False, correct, False
    return True, row.correct, row.hallucinated


def sweep(rows: list[Row], confidences: list[float], step: float) -> list[SweepPoint]:
    points = []
    for t in np.round(np.arange(0.0, 1.0 + 1e-9, step), 4):
        outcomes = [outcome_after_threshold(r, c, float(t)) for r, c in zip(rows, confidences)]
        answered = [o for o in outcomes if o[0]]
        points.append(SweepPoint(
            threshold=float(t),
            coverage=len(answered) / len(outcomes),
            accuracy_answered=(sum(o[1] for o in answered) / len(answered)) if answered else None,
            hallucination_answered=(sum(o[2] for o in answered) / len(answered)) if answered else 0.0,
            qa_accuracy=sum(o[1] for o in outcomes) / len(outcomes),
            answered=len(answered),
        ))
    return points


def pick(points: list[SweepPoint], max_hallucination: float) -> SweepPoint | None:
    ok = [p for p in points if p.hallucination_answered <= max_hallucination]
    if not ok:
        return None
    # Equal coverage and accuracy: the HIGHER threshold keeps the most safety margin for free.
    return max(ok, key=lambda p: (p.coverage, p.accuracy_answered or 0.0, p.threshold))


def _display(path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def run(settings: Settings) -> dict:
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    rows, pipeline = collect(settings)
    fact_rows = [r for r in rows if r.trace.features is not None]
    X = np.asarray([pipeline.scorer.vector(r.trace.features) for r in fact_rows])
    y = np.asarray([r.correct for r in fact_rows], dtype=int)
    heuristic = [r.trace.heuristic_confidence for r in fact_rows]
    bins = settings.scoring.calibration_bins
    ece_before = expected_calibration_error(heuristic, y.tolist(), bins)

    note = None
    negatives = int(len(y) - y.sum())
    min_neg = settings.scoring.min_negatives_for_calibration
    if negatives < min_neg or len(set(y)) < 2:
        note = (f"Not fitted: only {negatives} wrong dev answers (scoring.min_negatives_for_calibration = {min_neg}). "
                "A regression on so few negatives learns the base rate, not the signals, so the weighted formula stays.")
        oof, model = list(heuristic), None
        settings.paths.confidence_model.unlink(missing_ok=True)  # never leave a stale calibrator behind
    else:
        folds = max(2, min(5, int(np.bincount(y).min())))
        lr = LogisticRegression(C=settings.eval.calibration_c, max_iter=5000)
        oof = cross_val_predict(lr, X, y, cv=StratifiedKFold(folds, shuffle=True, random_state=settings.intent.seed),
                                method="predict_proba")[:, 1].tolist()
        model = LogisticRegression(C=settings.eval.calibration_c, max_iter=5000).fit(X, y)
        Calibrator(model=model, features=FEATURE_ORDER, dev_examples=len(fact_rows)).save(settings.paths.confidence_model)
    ece_after = expected_calibration_error(oof, y.tolist(), bins) if model is not None else None

    # Sweep on the confidence the kiosk will use (out-of-fold calibrated when fitted).
    conf_by_row = {id(r): c for r, c in zip(fact_rows, oof)}
    confidences = [conf_by_row.get(id(r), r.result.confidence) for r in rows]
    points = sweep(rows, confidences, settings.scoring.sweep_step)
    best = pick(points, settings.scoring.max_hallucination_rate)

    stamp = datetime.now().isoformat(timespec="seconds")
    sha = file_sha256(settings.paths.eval_testset)[:12]
    cal = [
        "# Confidence calibration (QA dev split)",
        "",
        f"- {stamp}; gold set sha256 `{sha}`; {len(rows)} dev questions, {len(fact_rows)} on the fact path "
        f"(canned intents keep the intent probability). Label: the outcome without a LOW_CONFIDENCE cut was correct "
        f"({int(y.sum())} correct, {int(len(y) - y.sum())} wrong).",
        f"- Model: logistic regression on {', '.join(FEATURE_ORDER)}; C = {settings.eval.calibration_c:g}. "
        + (f"Saved to `{_display(settings.paths.confidence_model)}`." if model is not None else note),
        "",
        "| confidence | ECE |",
        "|---|---|",
        f"| weighted formula (before) | {ece_before:.3f} |",
        f"| calibrated, out-of-fold (after) | {'not fitted' if ece_after is None else f'{ece_after:.3f}'} |",
        "",
    ]
    if model is not None:
        cal += ["## Coefficients", "", "| feature | weight |", "|---|---|"]
        cal += [f"| {n} | {w:+.3f} |" for n, w in zip(FEATURE_ORDER, model.coef_[0])]
        cal += [f"| (intercept) | {model.intercept_[0]:+.3f} |", ""]
    cal += ["Limits: the dev split is small and was written by the same author as the rules, so the calibration "
            "is rough. Refit on labelled kiosk logs before relying on the probabilities."]
    settings.paths.calibration_report.parent.mkdir(parents=True, exist_ok=True)
    settings.paths.calibration_report.write_text("\n".join(cal) + "\n", encoding="utf-8")

    current = settings.thresholds.answer_min_confidence
    sw = [
        "# Escalation threshold sweep (QA dev split)",
        "",
        f"- {stamp}; {len(rows)} dev questions; confidence = {'calibrated (out-of-fold)' if model is not None else 'weighted formula'}.",
        f"- Rule: highest coverage with hallucination rate of answered <= {100 * settings.scoring.max_hallucination_rate:.0f}%.",
        f"- **Recommended `thresholds.answer_min_confidence`: {best.threshold:.2f}**" if best else "- No threshold meets the hallucination limit.",
        f"- Currently configured: {current:.2f}.",
        "",
        "| threshold | coverage (answered) | accuracy of answered | hallucination of answered | QA accuracy (all) |",
        "|---|---|---|---|---|",
    ]
    for p in points:
        mark = " **<- pick**" if best and p.threshold == best.threshold else ""
        acc = "n/a" if p.accuracy_answered is None else f"{100 * p.accuracy_answered:.1f}%"
        sw.append(f"| {p.threshold:.2f}{mark} | {100 * p.coverage:.1f}% ({p.answered}) | {acc} | "
                  f"{100 * p.hallucination_answered:.1f}% | {100 * p.qa_accuracy:.1f}% |")
    sw += ["", "Coverage vs accuracy of answered:", "", "```text"]
    for p in points:
        bar = "#" * round(40 * p.coverage)
        acc = 0.0 if p.accuracy_answered is None else p.accuracy_answered
        sw.append(f"t={p.threshold:.2f} coverage {bar:<40} {100 * p.coverage:5.1f}%  acc {100 * acc:5.1f}%")
    sw += ["```", "", "Tuned on dev only. The test split is reported by `just eval`."]
    settings.paths.threshold_report.write_text("\n".join(sw) + "\n", encoding="utf-8")
    return {"ece_before": ece_before, "ece_after": ece_after, "best": best, "fitted": model is not None}


def main() -> None:
    settings = get_settings()
    out = run(settings)
    after = "not fitted (too few wrong dev answers)" if out["ece_after"] is None else f"{out['ece_after']:.3f}, calibrator saved"
    print(f"ECE before {out['ece_before']:.3f}, after {after}")
    best = out["best"]
    if best:
        print(f"recommended thresholds.answer_min_confidence = {best.threshold:.2f} "
              f"(coverage {100 * best.coverage:.1f}%, hallucination {100 * best.hallucination_answered:.1f}%)")
    print(f"reports: {_display(settings.paths.calibration_report)}, {_display(settings.paths.threshold_report)}")


if __name__ == "__main__":
    main()
