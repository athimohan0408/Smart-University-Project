"""Dashboard numbers from the three logs: query counts, most-asked and unanswered questions, categories,
escalation reasons, response time and usage by hour.

Sources (all optional, read-only except the kiosk's own):
- rag-core ``logs/queries.jsonl``: one line per question that reached the knowledge base. The main source
  for questions, intents, escalations and server-side latency.
- voice-memory ``logs/turns.jsonl``: one line per visitor turn. Query completion rate (voice-memory's own
  definition, reused) and turn latency including speech.
- kiosk-ui ``logs/ui_events.jsonl``: what the visitor waited for at the screen, routes shown, buttons.

Run ``python -m kiosk_ui.analytics [--days 7] [--json]`` for a text summary.
"""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from kiosk_ui.eventlog import read_jsonl

_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")
# Not "unanswered": the visitor asked for a person, which the receptionist handles by design.
_NOT_UNANSWERED = {"user_requested"}
# Sessions from `python -m kiosk_ui.evaluate`: test traffic, not visitors.
TEST_SESSION_PREFIX = "eval-"


class Timing(BaseModel):
    n: int
    mean: float
    p50: float
    p95: float
    max: float


class QuestionCount(BaseModel):
    question: str
    count: int
    answered: int = 0
    reasons: dict[str, int] = {}


class Totals(BaseModel):
    questions: int
    answered: int
    escalated: int
    clarifications: int
    sessions: int
    completion_rate: float | None
    routes_shown: int
    receptionist_button: int


class Dashboard(BaseModel):
    generated_at: str
    since: str | None
    sources: dict[str, int]
    totals: Totals
    most_asked: list[QuestionCount]
    unanswered: list[QuestionCount]
    top_destinations: list[dict]
    categories: dict[str, int]
    escalation_reasons: dict[str, int]
    answer_sources: dict[str, int]
    response_time_ms: dict[str, Timing]
    rag_stage_mean_ms: dict[str, float]
    by_hour: list[int]
    by_day: dict[str, int]


def normalise_question(text: str) -> str:
    return _SPACE.sub(" ", _PUNCT.sub(" ", text.casefold().replace("'s", ""))).strip()


def _ts(record: dict) -> datetime | None:
    try:
        ts = datetime.fromisoformat(record["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _since(records: list[dict], since: datetime | None) -> list[dict]:
    if since is None:
        return records
    return [r for r in records if (t := _ts(r)) is not None and t >= since]


def _visitors(records: list[dict]) -> list[dict]:
    return [r for r in records if not str(r.get("session_id") or "").startswith(TEST_SESSION_PREFIX)]


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (same as numpy's default)."""
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def timing(values: Sequence[float]) -> Timing | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    return Timing(n=len(vals), mean=round(sum(vals) / len(vals), 1), p50=round(_percentile(vals, 0.5), 1),
                  p95=round(_percentile(vals, 0.95), 1), max=round(vals[-1], 1))


def _grouped(records: list[dict], top: int, answered_of=None) -> list[QuestionCount]:
    """Group questions by normalised text; show the most common original wording."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if q := (r.get("question") or "").strip():
            groups[normalise_question(q)].append(r)
    out = []
    for rows in groups.values():
        wording = Counter(r["question"].strip() for r in rows).most_common(1)[0][0]
        reasons = Counter(r.get("escalate_reason") for r in rows if r.get("escalate_reason"))
        out.append(QuestionCount(question=wording, count=len(rows),
                                 answered=sum(1 for r in rows if answered_of and answered_of(r)),
                                 reasons=dict(reasons.most_common())))
    out.sort(key=lambda c: (-c.count, c.question.casefold()))
    return out[:top]


def _completion_rate(turns: list[dict]) -> float | None:
    """voice-memory's query completion rate, when that module is installed (one definition for the team)."""
    if not turns:
        return None
    try:
        from voice_memory.config import get_settings as vm_settings
        from voice_memory.metrics import completion
    except ImportError:
        return None
    try:
        idle = vm_settings().memory.session_idle_s
    except Exception:  # noqa: BLE001 - voice-memory's settings missing: use its default idle split
        idle = 120.0
    return completion(turns, idle).rate


def compute(rag: list[dict], turns: list[dict], ui: list[dict], *, days: float | None = None,
            now: datetime | None = None, top: int = 10) -> Dashboard:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None
    rag, turns, ui = (_visitors(_since(x, since)) for x in (rag, turns, ui))
    ui_turns = [e for e in ui if e.get("event") == "turn"]

    # Questions: rag-core's log is the complete record. Without it (rag-core logging elsewhere), use the
    # kiosk's turns so the dashboard still works on a kiosk that only runs this module.
    if rag:
        questions = [r for r in rag if r.get("intent") not in ("greeting", "smalltalk_or_thanks")]
        answered_of = lambda r: not r.get("escalate") and not r.get("clarification")  # noqa: E731
        clarified = sum(1 for r in questions if r.get("clarification"))
    else:
        questions = [e for e in ui_turns if e.get("outcome") not in ("social", "no_speech", None)]
        answered_of = lambda r: r.get("outcome") == "answered"  # noqa: E731
        clarified = sum(1 for r in questions if r.get("outcome") == "clarification")

    escalated = [r for r in questions if r.get("escalate")]
    unanswered = [r for r in escalated if r.get("escalate_reason") not in _NOT_UNANSWERED]

    destinations: Counter[str] = Counter()
    for r in questions:
        for e in r.get("entities") or []:
            cid = e.get("canonical_id") if isinstance(e, dict) else None
            if cid and cid.split(":", 1)[0] in ("faculty", "room", "facility"):
                destinations[cid] += 1

    response: dict[str, Timing] = {}
    for name, values in (
        ("kiosk_round_trip", [e.get("round_trip_ms") for e in ui_turns]),
        ("voice_turn_total", [(t.get("latency_ms") or {}).get("total") for t in turns]),
        ("rag_server_total", [(r.get("latency_ms") or {}).get("total") for r in rag]),
    ):
        if t := timing(values):
            response[name] = t
    stages: dict[str, float] = {}
    for stage in ("nlu", "retrieval", "generation", "scoring", "total"):
        vals = [(r.get("latency_ms") or {}).get(stage) for r in rag]
        vals = [v for v in vals if v is not None]
        if vals:
            stages[stage] = round(sum(vals) / len(vals), 1)

    by_hour = [0] * 24
    by_day: Counter[str] = Counter()
    for r in questions:
        if (t := _ts(r)) is not None:
            local = t.astimezone()
            by_hour[local.hour] += 1
            by_day[local.date().isoformat()] += 1

    sessions = {r.get("session_id") for r in (turns or ui_turns or rag) if r.get("session_id")}
    return Dashboard(
        generated_at=now.isoformat(timespec="seconds"),
        since=since.isoformat(timespec="seconds") if since else None,
        sources={"rag_queries": len(rag), "voice_turns": len(turns), "ui_events": len(ui)},
        totals=Totals(
            questions=len(questions), answered=sum(1 for r in questions if answered_of(r)),
            escalated=len(escalated), clarifications=clarified, sessions=len(sessions),
            completion_rate=_completion_rate(turns),
            routes_shown=sum(1 for e in ui_turns if e.get("route_shown")),
            receptionist_button=sum(1 for e in ui if e.get("event") == "receptionist_button"),
        ),
        most_asked=_grouped(questions, top, answered_of),
        unanswered=_grouped(unanswered, top),
        top_destinations=[{"id": k, "count": v} for k, v in destinations.most_common(top)],
        categories=dict(Counter(r.get("intent") or "unknown" for r in questions).most_common()),
        escalation_reasons=dict(Counter(r.get("escalate_reason") or "unknown" for r in escalated).most_common()),
        answer_sources=dict(Counter(r.get("answer_source") for r in rag if r.get("answer_source")).most_common()),
        response_time_ms=response,
        rag_stage_mean_ms=stages,
        by_hour=by_hour,
        by_day=dict(sorted(by_day.items())),
    )


def load_and_compute(rag_path: Path, turns_path: Path, ui_path: Path, **kwargs) -> Dashboard:
    return compute(read_jsonl(rag_path), read_jsonl(turns_path), read_jsonl(ui_path), **kwargs)


def to_text(d: Dashboard) -> str:
    t = d.totals
    lines = [f"Generated {d.generated_at}" + (f", since {d.since}" if d.since else ""),
             f"Sources: {d.sources}",
             f"Questions {t.questions} | answered {t.answered} | escalated {t.escalated} | "
             f"clarifications {t.clarifications} | sessions {t.sessions} | completion "
             f"{'n/a' if t.completion_rate is None else f'{t.completion_rate:.1%}'}", "", "Most asked:"]
    lines += [f"  {c.count:>4}  {c.question}" for c in d.most_asked] or ["  (none)"]
    lines += ["", "Unanswered:"]
    lines += [f"  {c.count:>4}  {c.question}  {c.reasons}" for c in d.unanswered] or ["  (none)"]
    lines += ["", f"Categories: {d.categories}", f"Escalation reasons: {d.escalation_reasons}", "",
              "Response time (ms):"]
    lines += [f"  {k}: mean {v.mean}, p50 {v.p50}, p95 {v.p95}, max {v.max} (n={v.n})"
              for k, v in d.response_time_ms.items()] or ["  (no data)"]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    from kiosk_ui.config import get_settings

    logs = get_settings().logs
    parser = argparse.ArgumentParser(description="Analytics summary from the rag-core, voice-memory and kiosk logs.")
    parser.add_argument("--days", type=float, default=None, help="only the last N days")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    d = load_and_compute(logs.rag_queries, logs.voice_turns, logs.ui_events, days=args.days)
    print(d.model_dump_json(indent=2) if args.json else to_text(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
