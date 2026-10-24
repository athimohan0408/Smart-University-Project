"""Evaluate retrieval on ``paths.retrieval_test`` and write ``paths.retrieval_report``.

Run: ``python -m rag_core.retrieval.eval_retrieval``  (or ``just eval-retrieval``)

Each gold line is ``{"question", "intent", "gold": [fact_id, ...], "now"?}``. ``gold`` is the
smallest set of facts that answers the question, and every id must exist in facts.jsonl. Retrieval
gets the GOLD intent (so intent errors don't hide retrieval errors) and the entities the real
extractor finds, so entity-linking misses do count.

Metrics, averaged over questions:
- Hit@k: a gold fact is in the top k (k from ``eval.recall_at``).
- Recall@N: share of the gold facts in the top N (N = ``retrieval.top_k``).
- MRR: 1 / rank of the first gold fact (0 when none is returned).

The ablation switches stages off: dense only, BM25 only, hybrid (RRF), hybrid + structured routing
(the kiosk default and the target row), and + cross-encoder rerank. The intent category filter
stays on in every row. The questions were written by the same author as the routes: add new
questions for failures instead of bending routes to single items.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from rag_core.config import PROJECT_ROOT, Settings, get_settings
from rag_core.contracts import Intent
from rag_core.nlu.entities import EntityExtractor, EntityMention
from rag_core.nlu.intent import file_sha256
from rag_core.retrieval.retriever import RetrievalOptions, Retriever


class RetrievalExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    intent: Intent
    gold: list[str] = Field(min_length=1)
    now: datetime | None = Field(default=None, description="Clock for 'now', 'today', 'tomorrow'.")


def load_retrieval_examples(path: Path) -> list[RetrievalExample]:
    with path.open(encoding="utf-8") as fh:
        return [RetrievalExample.model_validate_json(line) for line in fh if line.strip()]


ABLATION: dict[str, RetrievalOptions] = {
    "dense only": RetrievalOptions(dense=True, bm25=False, structured=False, rerank=False),
    "BM25 only": RetrievalOptions(dense=False, bm25=True, structured=False, rerank=False),
    "hybrid (RRF)": RetrievalOptions(dense=True, bm25=True, structured=False, rerank=False),
    "hybrid + structured": RetrievalOptions(dense=True, bm25=True, structured=True, rerank=False),
    "+ rerank": RetrievalOptions(dense=True, bm25=True, structured=True, rerank=True),
}
TARGET_CONFIG = "hybrid + structured"


def first_hit_rank(retrieved: Sequence[str], gold: Sequence[str]) -> int | None:
    gold_set = set(gold)
    return next((rank for rank, fid in enumerate(retrieved, start=1) if fid in gold_set), None)


def recall_at(retrieved: Sequence[str], gold: Sequence[str], k: int) -> float:
    return len(set(retrieved[:k]) & set(gold)) / len(set(gold))


@dataclass
class QuestionResult:
    example: RetrievalExample
    retrieved: list[str]
    vias: list[str]

    @property
    def rank(self) -> int | None:
        return first_hit_rank(self.retrieved, self.example.gold)


@dataclass
class ConfigResult:
    name: str
    results: list[QuestionResult] = field(default_factory=list)
    skipped: str | None = None  # why the configuration didn't run

    def metrics(self, hit_ks: Sequence[int], top_k: int, intent: Intent | None = None) -> dict[str, float]:
        rows = [r for r in self.results if intent is None or r.example.intent == intent]
        if not rows:
            return {}
        out = {f"hit@{k}": sum((r.rank or top_k + 1) <= k for r in rows) / len(rows) for k in hit_ks}
        out[f"recall@{top_k}"] = sum(recall_at(r.retrieved, r.example.gold, top_k) for r in rows) / len(rows)
        out["mrr"] = sum(1 / r.rank if r.rank else 0.0 for r in rows) / len(rows)
        out["n"] = len(rows)
        return out


def evaluate(
    retriever: Retriever,
    extractor: EntityExtractor,
    examples: Sequence[RetrievalExample],
    configs: dict[str, RetrievalOptions] | None = None,
) -> dict[str, ConfigResult]:
    """Run every configuration. Rerank rows are skipped when the retriever has no reranker."""
    configs = configs or ABLATION
    mentions: list[list[EntityMention]] = [extractor.extract(ex.question, now=ex.now).mentions for ex in examples]
    vectors = [retriever.embed_query(ex.question) for ex in examples]
    out: dict[str, ConfigResult] = {}
    for name, opts in configs.items():
        result = ConfigResult(name)
        if opts.rerank and not retriever.has_reranker:
            result.skipped = "no cross-encoder loaded"
            out[name] = result
            continue
        for ex, ms, vec in zip(examples, mentions, vectors):
            facts = retriever.retrieve(ex.question, ex.intent, ms, query_vector=vec, options=opts)
            result.results.append(QuestionResult(ex, [f.fact_id for f in facts], [f.via for f in facts]))
        out[name] = result
    return out


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _hit_ks(settings: Settings) -> list[int]:
    return sorted(k for k in set(settings.eval.recall_at) if k < settings.retrieval.top_k) or [1]


def render_report(
    results: dict[str, ConfigResult], *, settings: Settings, n_test: int, rerank_note: str | None = None
) -> str:
    top_k = settings.retrieval.top_k
    hit_ks = _hit_ks(settings)
    target = settings.retrieval.target_recall
    head = [f"Hit@{k}" for k in hit_ks] + [f"Recall@{top_k}", "MRR"]
    keys = [f"hit@{k}" for k in hit_ks] + [f"recall@{top_k}", "mrr"]

    main = results[TARGET_CONFIG].metrics(hit_ks, top_k)
    verdict = "PASS" if main[f"recall@{top_k}"] >= target else "FAIL"
    out = [
        "# Retrieval: evaluation",
        "",
        f"- Test set: `{_display(settings.paths.retrieval_test)}`, {n_test} questions "
        f"(sha256 `{file_sha256(settings.paths.retrieval_test)[:12]}`). Gold intent, extracted entities.",
        f"- Embeddings `{settings.embeddings.active.name}`; candidates dense {settings.retrieval.dense_candidates}, "
        f"BM25 {settings.retrieval.bm25_candidates}; RRF k = {settings.retrieval.rrf_k}; top {top_k} returned.",
        f"- Target: Recall@{top_k} >= {target:.2f} for **{TARGET_CONFIG}**: **{verdict}** "
        f"(Recall@{top_k} {main[f'recall@{top_k}']:.3f}).",
        "",
        "## Ablation",
        "",
        "| configuration | " + " | ".join(head) + " |",
        "|---|" + "---|" * len(head),
    ]
    for name, res in results.items():
        if res.skipped:
            out.append(f"| {name} | " + " | ".join(["n/a"] * len(head)) + f" |")
            continue
        m = res.metrics(hit_ks, top_k)
        bold = name == TARGET_CONFIG
        cells = [f"**{m[k]:.3f}**" if bold else f"{m[k]:.3f}" for k in keys]
        out.append(f"| {'**' + name + '**' if bold else name} | " + " | ".join(cells) + " |")
    skipped = [r for r in results.values() if r.skipped]
    for r in skipped:
        out.append(f"\n`{r.name}` not run: {r.skipped}." + (f" {rerank_note}" if rerank_note else ""))

    intents = [i for i in Intent if any(r.example.intent == i for r in results[TARGET_CONFIG].results)]
    out += [
        "",
        f"## By intent: {TARGET_CONFIG}",
        "",
        "| intent | n | " + " | ".join(head) + " |",
        "|---|---|" + "---|" * len(head),
    ]
    for intent in intents:
        m = results[TARGET_CONFIG].metrics(hit_ks, top_k, intent)
        out.append(f"| {intent.value} | {int(m['n'])} | " + " | ".join(f"{m[k]:.3f}" for k in keys) + " |")

    ran = [r for r in results.values() if not r.skipped]
    out += [
        "",
        f"## Recall@{top_k} by intent and configuration",
        "",
        "| intent | " + " | ".join(r.name for r in ran) + " |",
        "|---|" + "---|" * len(ran),
    ]
    for intent in intents:
        cells = [f"{r.metrics(hit_ks, top_k, intent)[f'recall@{top_k}']:.3f}" for r in ran]
        out.append(f"| {intent.value} | " + " | ".join(cells) + " |")

    misses = [r for r in results[TARGET_CONFIG].results if recall_at(r.retrieved, r.example.gold, top_k) < 1]
    out += ["", f"## Incomplete retrievals ({TARGET_CONFIG}): {len(misses)} questions", ""]
    if misses:
        out += ["| question | intent | missing gold | retrieved (via) |", "|---|---|---|---|"]
        for r in misses:
            missing = "<br>".join(sorted(set(r.example.gold) - set(r.retrieved[:top_k])))
            got = "<br>".join(f"{fid} ({via})" for fid, via in zip(r.retrieved, r.vias))
            out.append(f"| {r.example.question} | {r.example.intent.value} | {missing} | {got} |")
    else:
        out.append("None.")
    out += [
        "",
        "Limits: the gold set and the routes were written by the same author, so this is optimistic. "
        "Label real kiosk questions before trusting it.",
    ]
    return "\n".join(out) + "\n"


def run_evaluation(
    settings: Settings,
    retriever: Retriever | None = None,
    extractor: EntityExtractor | None = None,
    *,
    with_rerank: bool = True,
) -> tuple[Path, dict[str, ConfigResult]]:
    """``with_rerank``: try to load the cross-encoder for the rerank row (downloads it on first use)."""
    rerank_note = None
    if retriever is None:
        try:
            retriever = Retriever.from_settings(settings, load_reranker=with_rerank)
        except OSError as exc:  # model not cached and no network
            rerank_note = f"Loading {settings.retrieval.rerank.model} failed: {exc.__class__.__name__}."
            retriever = Retriever.from_settings(settings, load_reranker=False)
    extractor = extractor or EntityExtractor.from_settings(settings)
    examples = load_retrieval_examples(settings.paths.retrieval_test)
    results = evaluate(retriever, extractor, examples)
    path = settings.paths.retrieval_report
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(results, settings=settings, n_test=len(examples), rerank_note=rerank_note), encoding="utf-8")
    return path, results


def main() -> None:
    settings = get_settings()
    path, results = run_evaluation(settings)
    top_k = settings.retrieval.top_k
    for name, res in results.items():
        if res.skipped:
            print(f"{name:22s} not run: {res.skipped}")
            continue
        m = res.metrics(_hit_ks(settings), top_k)
        hits = ", ".join(f"Hit@{k} {m[f'hit@{k}']:.3f}" for k in _hit_ks(settings))
        print(f"{name:22s} {hits}, Recall@{top_k} {m[f'recall@{top_k}']:.3f}, MRR {m['mrr']:.3f}")
    print(f"report: {_display(path)}")


if __name__ == "__main__":
    main()
