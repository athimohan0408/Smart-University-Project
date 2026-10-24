# Live LLM validation

- Run 2026-10-24T08:53:09; provider `anthropic`, model `claude-opus-5-5`, effort `low`, timeout 15 s, retries 1.
- Status: **NOT RUN** (exit 2). No API call was made; cost $0.00.
- Reason: no Anthropic credentials: put ANTHROPIC_API_KEY in .env (see .env.example) or run `ant auth login`
- Consequence: the live LLM path is **unvalidated**. Every reported metric so far comes from template mode (no LLM). Do not treat the system as production-ready on the LLM path.

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

## Reproduce

1. Put the key in `.env` at the repo root: `ANTHROPIC_API_KEY=...` (git-ignored; never in settings.yaml, code, tickets or chat). Install the extra once: `pip install -e .[anthropic]`.
2. `just validate-llm` -> this report (dev split, live).
3. `just eval --llm` -> `reports/eval_<timestamp>_llm.md/.json` + `failures_llm.csv` (test split, live). Template-mode reports keep their own names; never compare the two as one series.

Estimated cost at $4/$20 per million input/output tokens (`llm.price_*` in settings): validate-llm about $0.86-$5.19 (114 calls), eval --llm about $0.91-$5.46 (120 calls). The upper bound assumes every reply uses `llm.max_tokens`. A real run reports its actual token usage and cost in the status lines at the top.
