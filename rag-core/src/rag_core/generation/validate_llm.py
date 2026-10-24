"""Validate the LIVE LLM path against the configured provider (``llm.provider``; key in ``.env``).

Run: ``python -m rag_core.generation.validate_llm [--limit N]``  (``just validate-llm``)

1. Connectivity: one structured-output call.
2. Dev questions through the pipeline with the real LLM, graded like the eval: QA accuracy,
   hallucinations, wrong-person answers, answers blocked by grounding, fallbacks to templates,
   latency, real token usage and cost. Only the DEV split is used (the test split is for reporting).
3. Near-miss battery: every similar_name and ambiguous dev question (includes the lowercase
   speech-to-text names); zero wrong-person answers allowed.
4. Timeout: the same question with a tiny timeout must fall back to the templates.

Refusals, malformed output, 5xx and 401 can't be provoked from a live model on demand; they are
verified with the real SDK and a mock HTTP transport in ``tests/generation/test_anthropic_adapter.py``
(simulated, and reported as such).

Writes ``reports/llm_validation.md`` every time, including when nothing could run. Exit code: 0 PASS,
1 FAIL, 2 NOT RUN (no provider/credentials). PASS needs: connectivity, no wrong-person answers, no
hallucinations, no unexpected fallbacks (the LLM really answered), and the forced timeout falling back.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import datetime

from pydantic import BaseModel

from rag_core.config import PROJECT_ROOT, Settings, get_settings
from rag_core.eval.qa import load_qa
from rag_core.eval.run_eval import qa_metrics, run_examples
from rag_core.generation.llm_client import LLMError, LLMUnavailable, make_llm_client
from rag_core.generation.prompts import SYSTEM_PROMPT
from rag_core.pipeline import Pipeline

REPORT = PROJECT_ROOT / "reports" / "llm_validation.md"
_TYPICAL_USER_CHARS = 2000  # question + entities + 5 facts, measured on dev questions (about 1.5-2.5k chars)
_MIN_OUTPUT_TOKENS = 150     # a 1-3 sentence JSON answer with little thinking at effort "low"

SIMULATED = """\
## Verified only in simulation (real SDK + mock HTTP transport, no network)

`tests/generation/test_anthropic_adapter.py` drives the installed `anthropic` SDK and our adapter through scripted
HTTP responses. These pass, but they are **simulated**: they prove our handling of each response type, not how the
live API behaves.

| behaviour | result |
|---|---|
| request shape (model, structured output schema, effort, refusal fallbacks, no temperature) | passes (simulated) |
| malformed JSON -> one retry with the error -> success / second failure -> error | passes (simulated) |
| `stop_reason: refusal` / `max_tokens` -> error -> templates answer | passes (simulated) |
| timeout -> one SDK retry -> error -> templates answer | passes (simulated) |
| 5xx -> retried and recovered; 529 overloaded -> templates answer; 401 -> no retry | passes (simulated) |
| LLM answer about the wrong person (near miss) -> blocked as `grounding_failed` | passes (simulated) |
| near miss marked "a DIFFERENT person" in the prompt; prompt rule 11 present | passes (simulated) |
| a fake API key absent from results, query log, health status, settings repr | passes (simulated) |

Verified for real on this machine, without credentials: the installed SDK resolves no credentials, the running API's
`/health` reports `llm_ready: false` with the reason, and answers come from the templates (2026-10-24).
"""


class _Ping(BaseModel):
    ok: bool


def _llm_settings(settings: Settings, **llm) -> Settings:
    return settings.model_copy(update={
        "generation": settings.generation.model_copy(update={"mode": "llm"}),
        "llm": settings.llm.model_copy(update=llm),
    })


def _p(values, q):
    values = sorted(v for v in values if v is not None)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))] if values else None


def cost_estimate(settings: Settings, calls: int) -> tuple[float, float]:
    """(low, high) USD for ``calls`` answer calls, from prompt size and llm.max_tokens."""
    cfg = settings.llm
    input_tokens = calls * (len(SYSTEM_PROMPT) + _TYPICAL_USER_CHARS) / 4
    low = input_tokens / 1e6 * cfg.price_input_per_mtok + calls * _MIN_OUTPUT_TOKENS / 1e6 * cfg.price_output_per_mtok
    high = input_tokens / 1e6 * cfg.price_input_per_mtok + calls * cfg.max_tokens / 1e6 * cfg.price_output_per_mtok
    return low, high


def _footer(settings: Settings, n_dev: int) -> list[str]:
    v_low, v_high = cost_estimate(settings, n_dev + 2)
    e_low, e_high = cost_estimate(settings, 120)
    return [
        "",
        SIMULATED,
        "## Reproduce",
        "",
        "1. Put the key in `.env` at the repo root: `ANTHROPIC_API_KEY=...` (git-ignored; never in settings.yaml, code, "
        "tickets or chat). Install the extra once: `pip install -e .[anthropic]`.",
        "2. `just validate-llm` -> this report (dev split, live).",
        "3. `just eval --llm` -> `reports/eval_<timestamp>_llm.md/.json` + `failures_llm.csv` (test split, live). "
        "Template-mode reports keep their own names; never compare the two as one series.",
        "",
        f"Estimated cost at ${settings.llm.price_input_per_mtok:g}/${settings.llm.price_output_per_mtok:g} per million "
        f"input/output tokens (`llm.price_*` in settings): validate-llm about ${v_low:.2f}-${v_high:.2f} "
        f"({n_dev + 2} calls), eval --llm about ${e_low:.2f}-${e_high:.2f} (120 calls). The upper bound assumes every "
        "reply uses `llm.max_tokens`. A real run reports its actual token usage and cost in the status lines at the top.",
    ]


def run(settings: Settings, limit: int | None = None) -> tuple[int, str]:
    s = _llm_settings(settings)
    stamp = datetime.now().isoformat(timespec="seconds")
    n_dev = len(load_qa(s.paths.eval_testset, "dev")) if not limit else limit
    head = ["# Live LLM validation", "",
            f"- Run {stamp}; provider `{s.llm.provider}`, model `{s.llm.model}`, effort `{s.llm.effort}`, "
            f"timeout {s.llm.timeout_s:g} s, retries {s.llm.max_retries}."]
    try:
        client = make_llm_client(s)
    except LLMUnavailable as exc:
        text = head + [
            "- Status: **NOT RUN** (exit 2). No API call was made; cost $0.00.",
            f"- Reason: {exc}",
            "- Consequence: the live LLM path is **unvalidated**. Every reported metric so far comes from template "
            "mode (no LLM). Do not treat the system as production-ready on the LLM path.",
        ] + _footer(settings, n_dev)
        return 2, "\n".join(text) + "\n"

    t = time.perf_counter()
    try:
        ok = client.generate("Reply with JSON only.", 'Return {"ok": true}.', _Ping)["ok"]
        ping = f"ok ({(time.perf_counter() - t) * 1000:.0f} ms)" if ok else "unexpected reply"
    except LLMError as exc:
        usage = getattr(client, "usage", None)
        text = head + [f"- Status: **FAIL** (exit 1): connectivity failed: {exc}",
                       f"- API calls attempted: {usage.calls + usage.failures if usage else 'unknown'}."] + _footer(settings, n_dev)
        return 1, "\n".join(text) + "\n"

    pipeline = Pipeline.from_settings(s, llm=client, log_queries=False)
    examples = load_qa(s.paths.eval_testset, "dev")
    if limit:
        risky = [e for e in examples if e.difficulty in ("similar_name", "ambiguous", "adversarial")]
        examples = (risky + [e for e in examples if e not in risky])[:limit]
    rows = run_examples(pipeline, examples, s.eval.default_now)
    m = qa_metrics(rows, s.retrieval.top_k)
    sources = Counter(r.trace.generation.source for r in rows)
    unexpected_fallbacks = [r for r in rows if r.trace.generation.llm_error]
    blocked = [r for r in rows if r.result.escalate_reason == "grounding_failed"]
    llm_ms = [r.trace.generation.llm_ms for r in rows if r.trace.generation.source == "llm"]
    total_ms = [r.result.latency_ms.get("total") for r in rows if r.trace.generation.source == "llm"]
    near = [r for r in rows if r.ex.difficulty in ("similar_name", "ambiguous")]
    near_wrong = [r for r in near if r.wrong_person]
    lower = [r for r in rows if r.ex.question == r.ex.question.lower() and r.ex.difficulty == "similar_name"]

    slow = _llm_settings(settings, timeout_s=0.001, max_retries=0)
    timeout_client = make_llm_client(slow)
    timeout_pipe = Pipeline.from_settings(slow, llm=timeout_client, log_queries=False)
    trace = timeout_pipe.run("where is Dr Nisha Varghese's cabin?")
    timeout_ok = trace.generation.source == "fallback" and "874" in trace.result.answer

    u, tu = client.usage, timeout_client.usage
    calls, fails = u.calls + tu.calls, u.failures + tu.failures
    cost = u.cost_usd(s.llm.price_input_per_mtok, s.llm.price_output_per_mtok) + tu.cost_usd(
        s.llm.price_input_per_mtok, s.llm.price_output_per_mtok)

    checks = {
        "connectivity": ok,
        "no wrong-person answers": m["wrong_person_answers"] == 0,
        "no hallucinations": m["hallucination_rate"] == 0.0,
        "LLM really answered (no unexpected fallbacks)": not unexpected_fallbacks,
        "forced timeout falls back": timeout_ok,
    }
    passed = all(checks.values())
    out = head + [
        f"- Status: **{'PASS' if passed else 'FAIL'}** (exit {0 if passed else 1}). Mode: **LIVE LLM**, dev split.",
        f"- Real API usage: {calls} successful calls, {fails} failed calls, {u.input_tokens + tu.input_tokens} input / "
        f"{u.output_tokens + tu.output_tokens} output tokens; cost about **${cost:.2f}**.",
        "",
        "| gate | result |", "|---|---|",
        *(f"| {k} | {'yes' if v else '**NO**'} |" for k, v in checks.items()),
        "",
        "| measurement (live) | value |", "|---|---|",
        f"| connectivity (structured output) | {ping} |",
        f"| dev questions run | {len(rows)} |",
        f"| QA accuracy | {100 * m['qa_accuracy']:.1f}% |",
        f"| hallucination rate (answered) | {100 * m['hallucination_rate']:.1f}% |",
        f"| wrong-person answers | {m['wrong_person_answers']} |",
        f"| unanswerable answered confidently | {100 * m['unanswerable_answered_rate']:.1f}% |",
        f"| LLM answers blocked by grounding (`grounding_failed`) | {len(blocked)} |",
        f"| answer sources | {dict(sources)} |",
        f"| unexpected LLM failures (fell back) | {len(unexpected_fallbacks)} |",
        f"| near-miss / ambiguous battery | {len(near)} questions ({len(lower)} lowercase similar names), "
        f"{len(near_wrong)} wrong-person |",
        f"| LLM call latency p50 / p95 | {_p(llm_ms, 0.5) or 0:.0f} / {_p(llm_ms, 0.95) or 0:.0f} ms |",
        f"| end-to-end latency p50 / p95 (LLM answers) | {_p(total_ms, 0.5) or 0:.0f} / {_p(total_ms, 0.95) or 0:.0f} ms |",
        f"| forced timeout (0.001 s) falls back to templates | {'yes' if timeout_ok else 'NO'} ({trace.generation.llm_error}) |",
        "| refusal, malformed JSON, 5xx, 401 | not provokable live; see the simulated table below |",
        "",
    ]
    wrong = list(dict.fromkeys([r for r in rows if not r.correct or r.hallucinated or r.wrong_person] + blocked
                               + unexpected_fallbacks))
    out += [f"## Failures and blocked answers: {len(wrong)}", ""]
    if wrong:
        out += ["| id | question | failure | answer source | answer |", "|---|---|---|---|---|"]
        for r in wrong:
            issues = ",".join(i.kind for i in r.trace.grounding.issues)
            out.append(f"| {r.ex.id} | {r.ex.question} | {','.join(r.failure_types())} {issues} "
                       f"| {r.trace.generation.source} | {r.result.answer} |")
    else:
        out.append("None.")
    return (0 if passed else 1), "\n".join(out + _footer(settings, len(examples))) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rag_core.generation.validate_llm")
    parser.add_argument("--limit", type=int, default=None, help="at most N dev questions (risky ones first)")
    args = parser.parse_args(argv)
    code, text = run(get_settings(), args.limit)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    status = {0: "PASS", 1: "FAIL", 2: "NOT RUN"}[code]
    print(f"live LLM validation: {status} (exit {code}); report: {REPORT.relative_to(PROJECT_ROOT).as_posix()}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
