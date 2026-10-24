"""Train the intent classifier on ``paths.intent_train`` and save it to ``paths.intent_model``.

Run: ``python -m rag_core.nlu.train_intent``  (or ``just train-intent``)

Model selection uses stratified cross-validation on the TRAIN set only. The held-out test set
(``paths.intent_test``) is never read here. The CV errors printed at the end come from the train
set, so they are safe to learn from when adding training examples.

Retrain when the intents, the training data or the embedding model change. KB data changes
(faculty, rooms, timetable) never require retraining.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict, cross_val_score

from rag_core.config import IntentSettings, get_settings
from rag_core.nlu.intent import IntentExample, IntentModel, QueryEmbedder, file_sha256, load_examples, match_rule


def _make_classifier(c: float, cfg: IntentSettings) -> LogisticRegression:
    return LogisticRegression(C=c, max_iter=cfg.max_iter, class_weight=cfg.class_weight, random_state=cfg.seed)


def train(
    examples: Sequence[IntentExample], embedder: QueryEmbedder, cfg: IntentSettings, *, train_sha256: str = ""
) -> tuple[IntentModel, list[str]]:
    """Pick C by CV, fit on all examples. Returns the model and the CV-predicted label per example."""
    X = np.asarray(embedder.embed_queries([e.text for e in examples]))
    y = np.asarray([e.intent.value for e in examples])
    folds = StratifiedKFold(n_splits=cfg.cv_folds, shuffle=True, random_state=cfg.seed)

    cv_by_c = {c: float(cross_val_score(_make_classifier(c, cfg), X, y, cv=folds).mean()) for c in cfg.c_grid}
    best_c = max(cfg.c_grid, key=lambda c: (cv_by_c[c], -c))  # ties go to the more regularised model
    cv_predicted = cross_val_predict(_make_classifier(best_c, cfg), X, y, cv=folds).tolist()

    model = IntentModel(
        classifier=_make_classifier(best_c, cfg).fit(X, y),
        embedding_model=embedder.profile.name,
        query_prefix=embedder.profile.query_prefix,
        c=best_c,
        cv_accuracy=cv_by_c[best_c],
        cv_by_c=cv_by_c,
        train_examples=len(examples),
        train_sha256=train_sha256,
    )
    return model, cv_predicted


def main() -> None:
    from rag_core.retrieval.embeddings import Embedder

    settings = get_settings()
    paths, cfg = settings.paths, settings.intent
    examples = load_examples(paths.intent_train)
    embedder = Embedder.from_settings(settings.embeddings)
    model, cv_predicted = train(examples, embedder, cfg, train_sha256=file_sha256(paths.intent_train))
    model.save(paths.intent_model)

    # The full classifier lets rules override the model; show CV accuracy with that applied too.
    with_rules = [
        rule.intent.value if (rule := match_rule(e.text)) else pred for e, pred in zip(examples, cv_predicted)
    ]
    rules_cv = sum(p == e.intent.value for p, e in zip(with_rules, examples)) / len(examples)

    print(f"trained on {len(examples)} examples ({paths.intent_train.name}), embeddings: {model.embedding_model}")
    print("CV accuracy by C (model only): " + ", ".join(f"C={c:g}: {acc:.3f}" for c, acc in model.cv_by_c.items()))
    print(f"chosen C={model.c:g}: CV accuracy {model.cv_accuracy:.3f} model only, {rules_cv:.3f} with rules")
    errors = [(e, p) for e, p in zip(examples, with_rules) if p != e.intent.value]
    if errors:
        print(f"CV errors with rules ({len(errors)}), from the TRAIN set:")
        for example, predicted in errors:
            print(f"  {example.intent.value:<20} -> {predicted:<20} {example.text}")
    print(f"saved: {paths.intent_model}")


if __name__ == "__main__":
    main()
