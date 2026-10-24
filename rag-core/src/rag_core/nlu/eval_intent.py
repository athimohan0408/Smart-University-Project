"""Evaluate the intent classifier on the HELD-OUT set and write ``paths.intent_report``.

Run: ``python -m rag_core.nlu.eval_intent``  (or ``just eval-intent``; the pytest suite runs it too)

Reports accuracy, per-intent precision/recall/F1 and the confusion matrix without the LLM fallback,
and with it when the fallback is enabled and an LLM is configured. Never tune anything on these
results: add new, different training examples instead of copying test items.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

from rag_core.config import PROJECT_ROOT, Settings, get_settings
from rag_core.contracts import Intent
from rag_core.nlu.intent import IntentClassifier, IntentExample, file_sha256, load_examples

LABELS = list(Intent)


@dataclass
class IntentEvalResult:
    setting: str
    accuracy: float
    macro_f1: float
    per_intent: list[tuple[Intent, float, float, float, int]]  # intent, precision, recall, f1, support
    confusion: list[list[int]]  # rows gold, columns predicted, both in LABELS order
    errors: list[tuple[str, Intent, Intent, float, str]]  # text, gold, predicted, probability, source
    sources: Counter
    below_threshold: int
    llm_attempted: int


def evaluate(
    classifier: IntentClassifier, examples: Sequence[IntentExample], *, use_llm: bool, threshold: float
) -> IntentEvalResult:
    predictions = classifier.predict_many([e.text for e in examples], use_llm=use_llm)
    gold = [e.intent for e in examples]
    predicted = [p.intent for p in predictions]
    precision, recall, f1, support = precision_recall_fscore_support(
        gold, predicted, labels=LABELS, zero_division=0
    )
    return IntentEvalResult(
        setting="with LLM fallback" if use_llm else "without LLM fallback",
        accuracy=sum(g == p for g, p in zip(gold, predicted)) / len(examples),
        macro_f1=float(f1.mean()),
        per_intent=[
            (intent, float(p), float(r), float(f), int(s))
            for intent, p, r, f, s in zip(LABELS, precision, recall, f1, support)
        ],
        confusion=confusion_matrix(gold, predicted, labels=LABELS).tolist(),
        errors=[
            (e.text, e.intent, p.intent, p.probability, p.source)
            for e, p in zip(examples, predictions)
            if e.intent != p.intent
        ],
        sources=Counter(p.source for p in predictions),
        below_threshold=sum(p.source != "rule" and p.model_probability < threshold for p in predictions),
        llm_attempted=sum(p.llm_attempted for p in predictions),
    )


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def render_report(
    results: Sequence[IntentEvalResult], *, settings: Settings, classifier: IntentClassifier, n_test: int, llm_note: str
) -> str:
    cfg, model, paths = settings.intent, classifier.model, settings.paths
    main = results[0]
    verdict = "PASS" if main.accuracy >= cfg.target_accuracy else "FAIL"
    out = [
        "# Intent classifier: held-out evaluation",
        "",
        f"- Test set: `{_display(paths.intent_test)}`, {n_test} examples (sha256 `{file_sha256(paths.intent_test)[:12]}`), "
        "never used for training or tuning.",
        f"- Model: `{_display(paths.intent_model)}`, logistic regression C={model.c:g}, trained on "
        f"{model.train_examples} examples (train CV accuracy {model.cv_accuracy:.3f}).",
        f"- Embeddings: `{model.embedding_model}` (query prefix as in retrieval).",
        f"- Target: accuracy >= {cfg.target_accuracy:.2f} without the LLM fallback: **{verdict}** ({main.accuracy:.3f}).",
        "",
        "## Summary",
        "",
        "| setting | accuracy | macro F1 | decided by rule / model / LLM | below fallback threshold | LLM calls |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        out.append(
            f"| {r.setting} | {r.accuracy:.3f} | {r.macro_f1:.3f} | "
            f"{r.sources['rule']} / {r.sources['model']} / {r.sources['llm']} | {r.below_threshold} | {r.llm_attempted} |"
        )
    if llm_note:
        out += ["", llm_note]

    out += ["", f"## Per-intent precision / recall ({main.setting})", "", "| intent | precision | recall | F1 | support |", "|---|---|---|---|---|"]
    out += [f"| {i.value} | {p:.2f} | {r:.2f} | {f:.2f} | {s} |" for i, p, r, f, s in main.per_intent]

    out += ["", f"## Confusion matrix ({main.setting})", "", "Rows are the gold intent, columns the predicted intent (numbered as the rows).", ""]
    out.append("| gold \\ predicted | " + " | ".join(str(n) for n in range(1, len(LABELS) + 1)) + " |")
    out.append("|---|" + "---|" * len(LABELS))
    for n, (intent, row) in enumerate(zip(LABELS, main.confusion), start=1):
        cells = [f"**{v}**" if j == n - 1 and v else (str(v) if v else "·") for j, v in enumerate(row)]
        out.append(f"| {n}. {intent.value} | " + " | ".join(cells) + " |")

    for r in results:
        out += ["", f"## Misclassified ({r.setting}): {len(r.errors)}", ""]
        if r.errors:
            out += ["| question | gold | predicted | probability | decided by |", "|---|---|---|---|---|"]
            out += [f"| {t} | {g.value} | {p.value} | {prob:.2f} | {src} |" for t, g, p, prob, src in r.errors]
        else:
            out.append("None.")
    out += [
        "",
        "Do not copy these questions into the training set. That would leak the test set. Add new, "
        "different examples of the same kind instead, then retrain and re-run.",
    ]
    return "\n".join(out) + "\n"


def run_evaluation(settings: Settings, classifier: IntentClassifier | None = None) -> tuple[Path, list[IntentEvalResult]]:
    classifier = classifier or IntentClassifier.from_settings(settings)
    examples = load_examples(settings.paths.intent_test)
    threshold = settings.intent.llm_fallback_threshold
    results = [evaluate(classifier, examples, use_llm=False, threshold=threshold)]

    if settings.intent.llm_fallback_enabled and classifier.has_llm:
        results.append(evaluate(classifier, examples, use_llm=True, threshold=threshold))
        llm_note = ""
    elif settings.intent.llm_fallback_enabled:
        llm_note = "LLM fallback is enabled but no LLM is available (SDK or API key missing), so it was not evaluated."
    else:
        llm_note = "LLM fallback is disabled (`intent.llm_fallback_enabled: false`), so only the setting without it was evaluated."

    report = render_report(results, settings=settings, classifier=classifier, n_test=len(examples), llm_note=llm_note)
    path = settings.paths.intent_report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    return path, results


def main() -> None:
    settings = get_settings()
    path, results = run_evaluation(settings)
    for r in results:
        print(f"{r.setting}: accuracy {r.accuracy:.3f}, macro F1 {r.macro_f1:.3f}, {len(r.errors)} errors")
    print(f"report: {_display(path)}")


if __name__ == "__main__":
    main()
