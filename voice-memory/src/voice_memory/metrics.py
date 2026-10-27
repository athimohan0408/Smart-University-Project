"""The two metrics this module owns, from the turn log: response latency and query completion rate.

Run: ``python -m voice_memory.metrics [--log logs/turns.jsonl] [--out reports/voice_metrics.md]``

**Response latency**: wall time of a turn, from the end of the visitor's speech (or a typed
question) to the answer audio being ready: ``latency_ms.total`` = stt + memory + rag + hand-off +
tts. Reported as p50 / p95 / max, per stage and per input (voice / text).

**Query completion rate**: share of the visitors' information requests the receptionist answered
itself. A *query* is one information need and may span turns:
- an ``answered`` turn completes a query (a clarification followed by an answer is ONE completed query);
- an ``escalated`` or ``handoff`` turn ends the query without completing it;
- a ``clarification`` left open when the session ends counts as ``abandoned``;
- greetings/thanks (``social``), yes/no replies to the hand-off offer (``offer_reply``) and ``no_speech``
  are not queries.
completion rate = completed / (completed + escalated + abandoned). Sessions split on
``memory.session_idle_s`` of silence, since a kiosk reuses one session id for many visitors.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

import numpy as np
from pydantic import BaseModel

STAGES = ("stt", "memory", "rag", "handoff", "tts", "total")


class LatencyStats(BaseModel):
    n: int
    p50: float
    p95: float
    max: float


class Completion(BaseModel):
    queries: int
    completed: int
    escalated: int
    abandoned: int
    rate: float | None
    sessions: int
    sessions_fully_completed: int
    escalations_by_reason: dict[str, int]


class MetricsReport(BaseModel):
    turns: int
    latency: dict[str, LatencyStats]
    latency_by_input: dict[str, LatencyStats]
    completion: Completion
    rewritten_share: float | None


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stats(values: Sequence[float]) -> LatencyStats | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    return LatencyStats(n=len(arr), p50=round(float(np.percentile(arr, 50)), 1),
                        p95=round(float(np.percentile(arr, 95)), 1), max=round(float(arr.max()), 1))


def sessions(records: Iterable[dict], idle_s: float) -> list[list[dict]]:
    by_id: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_id[r.get("session_id") or "?"].append(r)
    out = []
    for turns in by_id.values():
        turns.sort(key=lambda r: r["timestamp"])
        current: list[dict] = []
        last = None
        for r in turns:
            ts = datetime.fromisoformat(r["timestamp"])
            if current and last is not None and (ts - last).total_seconds() > idle_s:
                out.append(current)
                current = []
            current.append(r)
            last = ts
        if current:
            out.append(current)
    return out


def completion(records: Sequence[dict], idle_s: float) -> Completion:
    completed = escalated = abandoned = 0
    full = counted = 0
    reasons: Counter[str] = Counter()
    for session in sessions(records, idle_s):
        open_query = False
        s_done = s_fail = 0
        for r in session:
            outcome = r["outcome"]
            if r.get("offer_reply"):
                continue  # "yes, call the receptionist": the query was already counted when it escalated
            if outcome == "answered":
                completed += 1
                s_done += 1
                open_query = False
            elif outcome == "clarification":
                open_query = True
            elif outcome in ("escalated", "handoff"):
                escalated += 1
                s_fail += 1
                reasons[r.get("escalate_reason") or "unknown"] += 1
                open_query = False
        if open_query:
            abandoned += 1
            s_fail += 1
        if s_done or s_fail:
            counted += 1
            full += s_fail == 0
    queries = completed + escalated + abandoned
    return Completion(
        queries=queries, completed=completed, escalated=escalated, abandoned=abandoned,
        rate=round(completed / queries, 4) if queries else None, sessions=counted, sessions_fully_completed=full,
        escalations_by_reason=dict(reasons.most_common()),
    )


def compute(records: Sequence[dict], idle_s: float) -> MetricsReport:
    latency = {}
    for stage in STAGES:
        if stats := _stats([r["latency_ms"][stage] for r in records if stage in r.get("latency_ms", {})]):
            latency[stage] = stats
    by_input = {}
    for source in sorted({r.get("input", "text") for r in records}):
        if stats := _stats([r["latency_ms"]["total"] for r in records if r.get("input", "text") == source]):
            by_input[source] = stats
    queries = [r for r in records if r["outcome"] not in ("social", "no_speech") and not r.get("offer_reply")]
    rewritten = [r for r in queries if r.get("rewritten")]
    return MetricsReport(
        turns=len(records), latency=latency, latency_by_input=by_input, completion=completion(records, idle_s),
        rewritten_share=round(len(rewritten) / len(queries), 4) if queries else None,
    )


def to_markdown(report: MetricsReport, target_p95: float, target_rate: float, source: str) -> str:
    lines = [f"# Voice + memory metrics\n\nSource: `{source}`, {report.turns} turns.\n", "## Response latency (ms)\n",
             "| stage | n | p50 | p95 | max |", "|---|---|---|---|---|"]
    for stage, s in report.latency.items():
        lines.append(f"| {stage} | {s.n} | {s.p50} | {s.p95} | {s.max} |")
    for source_, s in report.latency_by_input.items():
        lines.append(f"| total ({source_} input) | {s.n} | {s.p50} | {s.p95} | {s.max} |")
    total = report.latency.get("total")
    if total:
        verdict = "meets" if total.p95 <= target_p95 else "MISSES"
        lines.append(f"\np95 total {total.p95} ms {verdict} the target of {target_p95:.0f} ms.\n")
    c = report.completion
    lines += ["## Query completion rate\n", "| | count |", "|---|---|", f"| queries | {c.queries} |",
              f"| completed by the receptionist | {c.completed} |", f"| escalated / handed off | {c.escalated} |",
              f"| abandoned at a clarification | {c.abandoned} |",
              f"| sessions fully completed | {c.sessions_fully_completed} of {c.sessions} |"]
    if c.rate is not None:
        verdict = "meets" if c.rate >= target_rate else "MISSES"
        lines.append(f"\nCompletion rate **{c.rate:.1%}**, which {verdict} the target of {target_rate:.0%}.\n")
    if c.escalations_by_reason:
        lines += ["Escalations by reason:\n", *(f"- `{k}`: {v}" for k, v in c.escalations_by_reason.items()), ""]
    if report.rewritten_share is not None:
        lines.append(f"Follow-ups rewritten by memory: {report.rewritten_share:.1%} of queries.\n")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from voice_memory.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(description="Response latency and query completion rate from the turn log.")
    parser.add_argument("--log", type=Path, default=settings.metrics.turn_log)
    parser.add_argument("--out", type=Path, default=settings.metrics.report)
    args = parser.parse_args(argv)
    records = read_log(args.log)
    if not records:
        print(f"no turns in {args.log}")
        return 1
    report = compute(records, settings.memory.session_idle_s)
    text = to_markdown(report, settings.metrics.target_latency_p95_ms, settings.metrics.target_completion_rate,
                       str(args.log))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
