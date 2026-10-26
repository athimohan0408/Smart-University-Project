# rag-core: language + RAG core of the receptionist

Takes a visitor's question, already resolved by the dialogue module, and returns a short answer for the screen and
for TTS, plus whether to hand over to the human receptionist. Every answer comes **only** from the faculty knowledge
base; anything it can't verify is escalated, never guessed.

```text
question -> intent + entities -> retrieval (exact KB-id routes + dense/BM25 hybrid) -> answer
         -> grounding check (every room/name/time/email must be in a cited fact) -> confidence -> escalate?
```

> **Status: NOT PRODUCTION-READY.** Everything is built and tested in *template mode*, where answers are composed
> from the facts without an LLM. Two go-live conditions are still open:
> 1. The live LLM path has never run (no API key yet; `reports/llm_validation.md` says NOT RUN).
> 2. The gold sets haven't been reviewed by an independent person (`reports/gold_review.md`, sign-off empty).
>
> Exact remaining steps: `reports/iterations.md`, section "Go-live validation". The module works for integration
> today: without a key it answers from templates, and `/health` reports `llm_ready: false`.

## Quick start

Needs **Python 3.11+** and about 1 GB of disk (PyTorch and the embedding models). If your project folder is synced
by OneDrive/Dropbox/Google Drive, create the virtual environment *outside* it.

**Windows (PowerShell)**
```powershell
cd rag-core
python -m venv $env:USERPROFILE\.venvs\rag-core
& "$env:USERPROFILE\.venvs\rag-core\Scripts\Activate.ps1"
pip install -e ".[dev]"            # add ".[anthropic]" too if you will use the LLM
Copy-Item .env.example .env        # optional: put ANTHROPIC_API_KEY here (never commit .env)
```

**macOS / Linux**
```bash
cd rag-core
python3 -m venv ~/.venvs/rag-core && source ~/.venvs/rag-core/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Then build the generated files (they are not in git) and check everything works:

```bash
just index          # faculty sheet -> facts -> search index (downloads the ~130 MB embedding model the first time)
just train-intent   # trains the question classifier (about a minute)
just test           # the full test suite; everything should pass
just ask "where is Dr Nisha Varghese's cabin?"
just serve          # HTTP API on http://127.0.0.1:8000
```

`just` is installed by the dev extra. Without it, every recipe is a plain `python -m ...` command; see the `justfile`
or the table in `CLAUDE.md`. After the first run, set `HF_HUB_OFFLINE=1` in `.env`: without it every start checks the
Hugging Face Hub, which can add a minute.

## Using it from your module

**HTTP** (`just serve`; host and port in `config/settings.yaml`):

```bash
curl -s http://127.0.0.1:8000/answer -H "Content-Type: application/json" \
  -d '{"question": "where is Dr Nisha Varghese'"'"'s cabin?", "context": {"session_id": "kiosk-01"}}'
```

```json
{"answer": "Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor, here on this floor.",
 "confidence": 0.95, "escalate": false, "escalate_reason": null, "intent": "find_person",
 "entities": [{"type": "faculty", "text": "Dr Nisha Varghese", "canonical_id": "faculty:nisha-varghese", "score": 1.0}],
 "sources": ["faculty:nisha-varghese:location"], "latency_ms": {"nlu": 13.2, "retrieval": 3.8, "...": "..."}}
```

**Python:** `from rag_core.pipeline import answer, warmup`; call `warmup()` once, then `answer("question", context)`.

Other endpoints:
- `GET /health`: is it up, and is the LLM ready?
- `POST /reload-kb`: rebuild the knowledge base after the faculty sheet changes, without a restart.

Read **[`docs/interface.md`](docs/interface.md)** before integrating. It covers:
- **context keys:** `now`, `last_entities`, `prior_entities`, `session_id`;
- **escalation reasons**, and what the visitor hears for each;
- **clarifications:** "Which one do you mean?" is a normal answer, not an escalation;
- **examples:** five worked request/response pairs;
- **query-log schema** for analytics.

Notes for specific modules:
- **Dialogue:** send questions already resolved ("where is Dr X's office?", not "where is her office?").
- **Navigation:** use `entities[].canonical_id` values starting with `room:`.
- **UI:** show the receptionist button when `escalate` is true.
- **HTTP clients:** reuse one HTTP connection. A new connection per request costs about 200 ms on Windows; the
  answer itself takes about 20 ms.

## Configuration and data

| what | where |
|---|---|
| every tunable (models, thresholds, top-k, LLM provider and model, API host and port) | `config/settings.yaml`. No magic numbers in code. |
| secrets (API keys) | `.env` only (git-ignored; template in `.env.example`) |
| source data (read-only) | `data/raw/8th_floor_faculty_final_data.xlsx`, the problem statement `.docx`, and `data/raw/campus_info.xlsx` (classrooms, labs, facilities, events; filled in by the department, `just campus-check` validates it) |
| hand-maintained vocab | `data/kb/glossary.yaml` (subject/programme code names), `data/kb/entity_lexicon.yaml` (facility words, honorifics, words around names) |
| generated (rebuild with `just index` / `just train-intent`) | `data/processed/`, `data/index/`, `data/models/` |
| gold sets | `data/eval/` (QA, retrieval, entities, intent) |

The LLM is optional and pluggable (`llm.provider`: anthropic | openai | ollama). Without a working key, answers are
composed deterministically from the facts.

## Rules for contributors (please read)

1. **Answers come only from KB facts.** Never invent rooms, names, times or emails. If the facts don't cover it,
   escalate. The grounding check enforces this; don't weaken it.
2. **Never answer about the wrong person.** "Dr John Mathew" must not resolve to Dr Tegil J John. Name handling asks
   "do you mean …?" or declines when unsure. To stop a needless clarification, add the word to
   `name_adjacent_words` in `data/kb/entity_lexicon.yaml`; never loosen the check. Zero-tolerance tests guard this.
3. **The 120-question test split is frozen.** `data/eval/qa_test.jsonl`, rows with `"split": "test"`, has its
   fingerprint checked by a test. Don't edit those rows, and don't tune code to make test failures pass. Add every
   new case to the **dev** split. Tune and decide on dev; report test.
4. **Template and live-LLM results are separate.** `just eval` is template mode. `just eval --llm` writes `*_llm.*`
   files; never compare the two as one series.
5. **The API contract changes only with team agreement.** That means `src/rag_core/contracts.py` and
   `docs/interface.md`, updated together.
6. **Before pushing:** run `just test` and `just eval`. `just eval` exits 1 if a headline metric regressed against
   `reports/baseline.json`.

`CLAUDE.md` is the detailed engineering guide: architecture, data quirks of the faculty sheet, NLU labelling rules,
the metrics.

## Evaluation commands

| command | output |
|---|---|
| `just eval` (`--split dev`, `--llm`, `--judge`) | `reports/eval_<time>.md/.json`, `reports/failures.csv` |
| `just eval-retrieval` / `just eval-entities` / `just eval-intent` | per-component reports in `reports/` |
| `just calibrate` | confidence calibration and escalation-threshold sweep (dev split) |
| `just eval-integrity` | how blind the test split is (copies in other gold sets, items seen during development) |
| `just validate-llm` | live LLM check, **paid**; needs a key in `.env` |
| `just review-pack` | spreadsheets for the human gold-set reviewer (`reports/human_review/`) |

Current numbers, template mode, frozen test split of 120 questions:
- **QA accuracy:** 100%, or at least 95.9% at 95% confidence on the 88 items never seen during development.
- **Hallucinations:** 0.
- **Wrong-person answers:** 0.
- **Retrieval Recall@5:** 100%.
- **Intent accuracy:** 91.7%.
- **Latency:** p95 about 20 ms.

All gold sets so far were written by the system's author; see `reports/eval_integrity.md` for what that means for
these numbers.

## Layout

```text
config/settings.yaml   all tunables
data/                  raw/ (source), kb/ (hand-maintained), eval/ (gold sets), processed|index|models/ (generated)
docs/                  interface.md (contract for teammates), prompts.md (exact LLM prompt, generated)
reports/               baseline.json, iterations.md (history + go-live status), gold_review.md, data_requests.md,
                       eval_integrity.md, llm_validation.md, human_review/ (reviewer package)
src/rag_core/          contracts, config, kb/, nlu/, retrieval/, generation/, scoring/, eval/, pipeline.py, api.py
tests/                 pytest suite (model-backed tests skip until `just index` + `just train-intent`)
```

## Known gaps

- **No data yet:** classrooms, labs, events and facilities now load from `data/raw/campus_info.xlsx`
  (`kb/campus.py`), but the committed file is an empty template: no verified rows exist yet, so those questions
  still escalate by design. Phone numbers have no source at all. What to collect: `reports/data_requests.md`.
- **Unverified subject names:** no subject or programme name in `data/kb/glossary.yaml` is department-verified yet,
  so answers say the code ("ML").
- **Intent errors:** the classifier's mistakes are covered by "answer the question asked" rules in the templates,
  not fixed. With the LLM on, check this again.
