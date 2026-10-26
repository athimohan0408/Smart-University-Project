# rag_core: Portion 1 (language + RAG core)

AI receptionist for **Central Block, 8th floor, CHRIST University**. The full problem statement is in
`data/raw/problem 1 - Deepa V. Jose.docx` (the filename has spaces, not underscores).

This repo contains **only** the language/RAG core: intent + entities → retrieval → grounded answer →
confidence/escalation. Teammates own speech (STT/TTS), dialogue/context (coreference, follow-ups),
the kiosk UI, navigation/map and the analytics dashboard. Don't build those here. Integrate through
the contract below.

**Status:** everything in this portion is built: ingest, index, intent, entities, query-time
retrieval (`rag_core.retrieval.retriever`), generation (`rag_core.generation`: LLM client, prompts,
templates), scoring (`rag_core.scoring`: grounding check, confidence, escalation, calibration), the
pipeline (`rag_core.pipeline`), the API (`rag_core.api`), a CLI (`python -m rag_core "..."`) and the
end-to-end eval (`rag_core.eval.run_eval`). The interface for teammates is `docs/interface.md`; the
exact LLM prompt is `docs/prompts.md`.

**Go-live status: NOT PRODUCTION-READY** (2026-10-24; actions in `reports/iterations.md`, "Go-live validation").
The `anthropic` SDK is installed but no key is configured, so the live LLM path is unvalidated
(`reports/llm_validation.md` says NOT RUN), and the human gold-set review hasn't happened (`reports/gold_review.md`
sign-off is empty). Until then the kiosk answers from templates; `/health` reports `llm_ready: false`.
Never report AI review as human review, never merge template-mode and live-LLM metrics, and never edit the
120-question test split (`just eval-integrity` documents how blind it still is).

## Module contract

**Input**
- `question: str`, already **resolved** by the dialogue module. For example, "where is HER office?"
  arrives as "where is Dr Deepa V Jose's office?". Do not do coreference here. If a question still
  has a dangling pronoun or no resolvable entity, treat the entity as missing (low confidence → escalate).
- `context: dict | None`, optional hints. Unknown keys are ignored (documented in `docs/interface.md`):
  - `now`: ISO-8601 local datetime for "is X free now?" questions (default: server clock).
  - `last_entities`: `canonical_id`s from earlier turns, used **only** when the question links no
    KB entity at all, to fill that gap. Never overrides an entity in the question; not coreference.
  - `prior_entities`: list of `canonical_id`s from earlier turns, used **only** to break ties
    between equally good entity matches, never to add facts.
  - `session_id`: copied into the query log. It must never change the answer.
- Over HTTP the body is `RAGRequest(question, context)`.

**Output**: `RAGResult` in [src/rag_core/contracts.py](src/rag_core/contracts.py)

| field | type | meaning |
|---|---|---|
| `answer` | `str` | Text shown on screen and spoken by TTS. On escalation it's a polite "can't verify, please ask the receptionist". |
| `confidence` | `float` 0–1 | Confidence that the answer is correct and fully grounded. |
| `escalate` | `bool` | True ⇒ UI offers the human receptionist. |
| `escalate_reason` | `str \| None` | Required when `escalate`, must be `None` otherwise. One of `user_requested`, `out_of_scope`, `not_in_kb`, `grounding_failed`, `ambiguous_entity`, `llm_error`, `low_confidence` (`scoring.escalation.EscalationReason`). Analytics groups by it. |
| `intent` / `intent_confidence` | `Intent`, `float` 0–1 | One of 13 intents (enum in contracts.py; definitions in the NLU section). |
| `entities` | `list[Entity]` | `type`, surface `text`, `canonical_id` (or `None` if unlinked), `score` 0–1. |
| `sources` | `list[str]` | `fact_id`s **actually cited** in the answer (not everything retrieved). |
| `latency_ms` | `dict[str, float]` | Per-stage wall time. Keys: `nlu`, `retrieval`, `generation`, `scoring`, `total`. |

Invariants:
- `escalate ⇔ escalate_reason` and probabilities in [0, 1] are enforced by the model.
- Contract models reject unknown fields (`extra="forbid"`), so a contract change needs team agreement,
  a change to contracts.py, and an update to this section in the same change.
- `sources` ⊆ the `fact_id`s retrieved for this question, and every factual claim in `answer` is
  supported by a cited source. This is enforced by the scoring stage and measured by eval.
- `canonical_id` format is `<entity_type>:<slug>`, e.g. `faculty:deepa-v-jose`, `room:877`. Slugs are
  derived from the KB, never from `Sl No` (it's a row index). Navigation resolves `room:` ids to the map.
  `fact_id`s extend these: `faculty:deepa-v-jose:location`, `room:877`, `course:ml` (see Retrieval index).
- Entry points: `rag_core.pipeline.answer(question, context=None, now=None) -> RAGResult` (call
  `warmup()` once at start-up) and `POST /answer` in `rag_core.api` (plus `GET /health`,
  `POST /reload-kb`). Every request appends a JSON line to `paths.query_log` (`logs/queries.jsonl`,
  schema in `docs/interface.md`): the question and the system's outputs, nothing else about the visitor.
- On `grounding_failed` the generated answer is never returned: the visitor hears
  `generation.safe_message`. A clarification ("which one do you mean?") is answered, not escalated.

## Golden rule

**Answers come ONLY from retrieved KB facts.**
- If the facts needed are not among the retrieved facts, say the information **can't be verified**,
  set `escalate=True`, and offer the human receptionist. Never give a best guess.
- **Never invent rooms, names, timings or emails.** Do not "complete" a missing value from a pattern
  either: emails in the data don't follow one pattern (see quirks), and a guessed email looks real.
- The LLM only rephrases the facts it's given (temperature 0). Answers are checked against the cited
  facts before returning. A failed grounding check escalates; it is never silently returned.
- If the facts are partial, answer the supported part and say plainly what isn't available.
- If the facts conflict, state both and flag uncertainty. Don't pick one silently. Faculty records
  carry these in `conflicts`.
- **Glossary trust** (`data/kb/glossary.yaml`): a `full_name: TODO` code is never expanded, so say the
  code as written. A `verified: false` expansion may be used, but scale confidence by
  `rag_core.kb.glossary.confidence_weight` (`thresholds.unverified_glossary_weight`). Only the
  department sets `verified: true`.
- Tests and eval must never depend on a live LLM. Use a fake client.

## Metrics this module owns

The problem statement requires six metrics. This module owns four. Response latency and query
completion rate are end-to-end metrics owned by integration/analytics; we supply per-stage `latency_ms`.

1. **Intent classification accuracy**: top-1 accuracy of `intent` against the gold intent, plus a
   per-intent confusion matrix. Measured on the held-out `data/eval/intent_test.jsonl` by
   `just eval-intent` → `reports/intent_eval.md`, with and without the LLM fallback.
2. **Retrieval accuracy**: recall@k of gold `fact_id`s among the retrieved facts (k from
   `eval.recall_at`), plus MRR.
3. **QA accuracy**: for answerable questions, the answer contains every gold key value (room, email,
   time, name, after normalisation) and `escalate=False`. For unanswerable questions, `escalate=True`.
4. **Hallucination/error rate**: share of answers that state a room, name, email or time not
   present in their cited facts, cite a `fact_id` that wasn't retrieved, or answer a question the
   gold set marks unanswerable.

Where the eval lives:
- code in `src/rag_core/eval/` (`qa.py` grading rules, runner `python -m rag_core.eval.run_eval`)
- gold set `data/eval/qa_test.jsonl` with a fixed `split`. **test** = 120 frozen questions (report only, never tune
  on it; a test enforces their fingerprint). **dev** = everything else, including all new regression cases and the
  independent reviewer's `rv*` questions; use it for thresholds, calibration and fix decisions.
- Grading: a keypoint may be a list of alternatives. For an unanswerable question, an escalation or a clarifying
  question that states no facts is correct (policy: ask when a person can't be confidently identified).
  Gap-fact rule: a missing value the visitor needs to act on (email, hours, office) escalates; an empty timetable
  day is itself the answer. Open review findings on the test split: `reports/gold_review.md`.
- Live LLM check: `just validate-llm` (paid; needs a key in `.env`) -> `reports/llm_validation.md`; the SDK-level
  failure tests (refusal, timeout, malformed, 5xx) are `tests/generation/test_anthropic_adapter.py`.
- reports `reports/eval_<timestamp>.md/.json` + `reports/failures.csv` (git-ignored);
  `reports/baseline.json`, `reports/iterations.md`, `reports/data_requests.md` are versioned.
  A model test fails when a headline metric is > `eval.max_regression` worse than the baseline.
- Eval runs generation in **template mode** (no LLM); `--llm` and `--judge` need a key.
- Retrieval has its own gold set and ablation: `data/eval/retrieval_test.jsonl` -> `reports/retrieval_eval.md`.

The gold set covers every intent, off-floor faculty, missing-email faculty, shared rooms, name
variants ("Deepa Jose", "Dr. Sridevi"), ambiguous names, adversarial questions (`forbidden` strings
must not appear), and categories that currently have **no data** (classrooms, labs, events,
facilities), which must escalate. Questions must not copy the intent seed sets (a test checks).

## Commands

The repo is inside OneDrive, so on this machine the venv lives **outside** it, at
`%USERPROFILE%\.venvs\receptionist-rag`. That keeps OneDrive from syncing gigabytes of torch and
avoids file locks during `pip install`. Activate it first:
`& "$env:USERPROFILE\.venvs\receptionist-rag\Scripts\Activate.ps1"`.
`just` comes with the dev extra (`rust-just`) and uses the active `python`, or `$RAG_PYTHON` if set.

| task | just | raw command |
|---|---|---|
| install | `just install` | `pip install -e ".[dev]"`, then add one LLM extra: `pip install -e ".[anthropic]"` / `[openai]` / `[ollama]` |
| ingest (Excel → faculty.json + reports) | `just ingest` | `python -m rag_core.kb.ingest_faculty` |
| build index (runs ingest first) | `just index` | `python -m rag_core.retrieval.build_index` |
| train intent classifier (once after install; then only when intents/training data/embedding model change) | `just train-intent` | `python -m rag_core.nlu.train_intent` |
| held-out intent eval → `reports/intent_eval.md` | `just eval-intent` | `python -m rag_core.nlu.eval_intent` |
| entity eval → `reports/entity_eval.md` (needs `just index`) | `just eval-entities` | `python -m rag_core.nlu.eval_entities` |
| retrieval eval + ablation → `reports/retrieval_eval.md` (needs `just index`) | `just eval-retrieval` | `python -m rag_core.retrieval.eval_retrieval` |
| run tests | `just test` | `python -m pytest` (skip model-backed tests: `python -m pytest -m "not model"`; `just test` can't pass quoted args) |
| end-to-end eval (test split) → `reports/eval_*.md`, `failures.csv`; exit 1 on regression | `just eval` (`make eval`) | `python -m rag_core.eval.run_eval [--split dev] [--llm] [--judge]` |
| refresh `reports/baseline.json` | `just eval-baseline` | `python -m rag_core.eval.run_eval --write-baseline` |
| calibrate confidence + threshold sweep on dev → `reports/calibration.md`, `threshold_sweep.md` | `just calibrate` (`make calibrate`) | `python -m rag_core.scoring.calibrate` |
| regenerate `docs/prompts.md` (a test checks it matches the code) | `just prompts-doc` | `python -m rag_core.generation.prompts` |
| ask one question | `just ask "where is room 874"` | `python -m rag_core "where is room 874" [--now ...] [--json]` |
| run API | `just serve` | `python -m rag_core.api` (reads `api.host`/`api.port`) |

Set `HF_HUB_OFFLINE=1` (in `.env`) once the models are downloaded: without it every model load
checks the Hugging Face Hub, which turned an 11 s start into 85 s here. `make` isn't installed on
this machine; the Makefile only mirrors the main `just` recipes.

The project targets Python ≥ 3.11. This machine only has 3.12, which is fine.

## Config and secrets

- **Every tunable** (model names, paths, top_k, thresholds, LLM provider/model/temperature, API
  host/port) lives in `config/settings.yaml` and is read through `rag_core.config.get_settings()`.
  The settings are typed, frozen, and reject unknown keys. **No magic numbers in code**: add a field
  to settings.yaml and config.py instead.
- Relative paths in the YAML resolve against the repo root. `RAG_SETTINGS_PATH` selects another file.
- **Secrets only come from `.env`** (template in `.env.example`) and are exposed as `Settings.secrets`.
  A YAML file containing `secrets` is rejected.
- The LLM is pluggable: `llm.provider` ∈ {anthropic, openai, ollama}, and each SDK is an optional
  extra. Import a provider SDK lazily inside its own adapter so the others aren't required.

## Knowledge base

`just ingest` reads the faculty sheet and writes to `data/processed/` (git-ignored, regenerate any time):
- `faculty.json`: a `FacultyKB` with one validated `Faculty` per sheet row (models in `kb/models.py`).
  Every parsed value sits next to its raw text: `ClassSlot.raw`, `consultation_hours.raw`,
  `research_areas_raw`, `location.raw`. Cite the raw text; use the parsed fields for matching and
  filtering. Load it with `FacultyKB.model_validate_json`.
- `subject_codes_found.csv`: each timetable subject code with its programmes, slot count and teachers.
- `data_quality_report.md`: missing values, off-floor faculty, shared rooms, unparsed lines, conflicts
  and glossary coverage. Check it after every data change.

Parsing rules (pure functions in `kb/parsing.py`, unit-tested in `tests/kb/`):
- A time without am/pm is placed inside `kb.day_start`–`kb.day_end` (07:30–17:00). An explicit marker
  always wins. If no unique reading fits, the value is left unparsed, never guessed.
- Consultation hours are best effort: `status` ∈ parsed / partial / unparsed / missing, and
  `unparsed_segments` lists what wasn't understood. `raw` is never dropped. Treat `partial` and
  `unparsed` as low confidence and quote the raw text.
- `data/kb/glossary.yaml` is **hand-maintained**, and ingest never writes it. Add any new code
  reported as missing from the glossary by hand, with `full_name: TODO`.

## Retrieval index

`just index` re-runs ingest, then turns `faculty.json` + the glossary into **facts**
(`kb/facts.py`, pure and deterministic) and writes to `data/index/` (git-ignored):
- `facts.jsonl`: every fact. Look up `fact_id`s here when debugging or writing eval gold labels.
- `chroma/`: dense index, one chunk per fact, embedded with the active embedding profile.
- `bm25.pkl`: lexical index over fact text + alias keywords, for hybrid search.
- `gazetteer.json`: the entity-linking vocabulary (see NLU: entities).

All of them carry the same facts digest (Chroma collection metadata `facts_digest`; it equals the
sha256 of `facts.jsonl`, and loading the gazetteer checks it). Rebuilding is idempotent: the
collection is dropped and recreated, and the files are replaced atomically.

Facts: one small, self-contained statement each, naming its subject in full.

| category | fact_id | one per |
|---|---|---|
| faculty_profile / faculty_location / faculty_consultation | `faculty:<id>:profile` / `:location` / `:consultation` | faculty |
| teaching | `faculty:<id>:teaching:<code-slug>` | faculty × subject code |
| schedule | `faculty:<id>:schedule:<day>` | faculty × Monday..Saturday (empty days included) |
| room | `room:<room-slug>` | office room (real room numbers only) |
| programme / course | `programme:<code>` / `course:<code-slug>` | code in the timetable or glossary |
| facility | `facility:<kind>:<slug>` | verified `Places` row of `data/raw/campus_info.xlsx` (`kb/campus.py`) |
| event | `event:<yyyy-mm-dd>:<slug>` | verified `Events` row of the same sheet |

Campus sheet rules (`kb/campus.py`): rows without `Verified By` are skipped and listed by `just campus-check`; kinds
must be lexicon facility/event kinds; hours and times are 24-hour `HH:MM` only (never guessed). With the file missing
or empty, `build_facts` returns exactly the faculty-only facts (a test checks). The committed file is an empty
template. Entities: `facility:<kind>` (every place of a kind, linked from the lexicon words only when that kind has
data), `event:<kind>` and `event:event` (any event); a classroom/lab room number that is no office becomes a
`room:<slug>` entity routed to the place. Programme facts list the classrooms/labs used by the programme instead of
"Classroom locations ... are not listed" when the sheet has them.

- **Gaps are facts too**: "Email: not listed", "Consultation hours … are not listed", "no classes are
  listed", "Classroom locations for MSAIM are not listed". The retriever finds them, so the answer
  says the information isn't available instead of inventing it. These count as verified statements.
- **`verified`** (metadata) is True only when every statement comes from the sheet or a verified
  glossary entry, with no conflict caution and no unreadable part. False facts must lower answer
  confidence. Unverified glossary names appear as `subject code ML ("Machine Learning", full name not
  verified)`; `facts.include_unverified_expansions: false` reduces this to the bare code.
- Metadata keys on every chunk: `fact_id, category, faculty_id, room, floor, day, subject_code,
  programme, verified, source_file`. Values are `""` when not applicable. Multi-valued fields are
  comma-joined without spaces.
- **Embeddings**: `embeddings.model` picks a profile (`bge-small` default, `minilm`). Each profile
  carries its own query/passage prefixes (bge: an instruction on queries only). Always embed through
  `retrieval.embeddings.Embedder` (`embed_query` / `embed_passages`) so the prefixes match the index.
  After switching models, rebuild. The model downloads to the Hugging Face cache on first use.
- BM25 must use `retrieval.bm25.tokenize` at query time. The pickle checks `TOKENIZER_VERSION`.

## NLU: intent

`nlu/intent.py`, `IntentClassifier.predict(text, query_vector=None) -> IntentPrediction`. The
prediction holds the intent, its probability, the top-N distribution, and `source` = rule / model /
llm. It also keeps the model's own answer.
1. **Rules** only fire on unambiguous whole utterances: just a greeting, just thanks/bye, or an
   explicit request for a human. "hi, where is room 874" goes to the model, and so does "talk to
   Dr Nisha". A test checks that no rule contradicts any training label.
2. **Model**: logistic regression on the `Embedder` query vector, the same vector retrieval uses,
   so pass `query_vector` to avoid embedding twice. C is chosen by CV on the train set.
3. **LLM fallback** (`intent.llm_fallback_enabled`, off by default): below
   `intent.llm_fallback_threshold`, ask the LLM for JSON-only `{"intent": ...}`. Any failure or
   invalid label keeps the model's answer.

**Retraining** is needed only when the intents, `intent_train.jsonl` or the embedding model change,
never for KB data. The test set deliberately uses faculty names that don't appear in training. The
artifact (`data/models/intent.joblib`, git-ignored) records its embedding model, and loading refuses
a mismatch or a changed intent list.

**Data discipline**: `data/eval/intent_test.jsonl` is HELD OUT. Never train, tune or pick
thresholds on it, and never copy its questions (or paraphrases) into training. Improve the model from
the CV errors that `just train-intent` prints, which come from the train set. A test enforces that
no held-out question appears in training.

Labelling rules for the boundary cases:
- Subject of the question is a **person**: where/who/profile → `find_person`; free / meet /
  consultation / "is she in her cabin" → `person_availability`; email/phone → `contact_info`;
  classes / timetable / "is she taking class now" → `schedule_query`.
- **Subject → person** ("who teaches ML", "I came to meet the ML faculty") → `find_by_subject`.
- **Where a place is** (room, classroom, lab, washroom, staff room, office) → `find_room`.
  **Amenities and their status** (wifi, water, printing, lift, canteen, charging, waiting area,
  library existence) → `facility_info`, even when phrased "where can I …".
- **Programmes as a topic** (what MSc AIML is, semesters, subjects, which faculty teach in it) →
  `programme_info`. An event that happens to be called a "programme" (orientation, a programme for
  parents) → `event_info`.
- Fees, admissions, hostel, placements, transport, results, general knowledge, homework →
  `out_of_scope`. Jokes and questions about the assistant itself → `smalltalk_or_thanks`.

Known limits: `reports/intent_eval.md` shows 0.985 held-out, but both sets were written by the same
author. Train CV (about 0.90) is the more realistic estimate until real kiosk questions are labelled.
Each retrain re-picks C by CV (currently 1000) and flips a few borderline questions, so judge a data
change on the QA dev split, not on single questions. Avoid putting one faculty name in examples of
only one intent: the classifier learns the name (see `reports/iterations.md`, iteration 2b).
The probabilities are sharp, so `llm_fallback_threshold` and `thresholds.intent_min_confidence`
need calibrating on real data before they mean much.

## NLU: entities

`nlu/entities.py`, `EntityExtractor.extract(text, now=None, prior_entities=()) -> EntityExtraction`.
No NER model: the vocabulary is closed. `nlu/gazetteer.py` builds `data/index/gazetteer.json` at index
time from faculty.json, the facts (so every room/course/programme id exists in the index),
glossary.yaml and the hand-maintained `data/kb/entity_lexicon.yaml` (facility/event words,
honorifics, codes that are everyday words). Edit the lexicon, then `just index`.

Types use the contract enum: a person is `faculty`, a subject `course`, a research area `topic`
(`topic:<slug>` of the research-area text; the gazetteer lists who has it). Ids: `faculty:<id>`,
`course:<code-slug>`, `programme:<code-slug>`, `room:<slug>` (= the room fact id), `topic:<slug>`,
`day:<weekday>`, `time:HH:MM` or `time:HH:MM-HH:MM`, and from the campus sheet `facility:<kind>[:<slug>]`,
`event:<kind>` / `event:<date>:<slug>`. Facility and event mentions of kinds with no campus data are recognised but
**unlinked** (`canonical_id` None, lexicon `kind` on the mention) until verified data exists.

Matching, in order, each stage on words the earlier ones left:
1. Patterns: times, days, rooms. "now"/"today"/"tomorrow" use context `now` or the injectable clock.
   A time without am/pm uses the `kb.day_start`–`kb.day_end` window like ingest; "at 7" fits neither
   reading, so it stays unresolved (unlinked TIME). Named periods ("after lunch", "morning") come from
   `entities.periods` (PROVISIONAL: the data has no lunch break) and are marked `approximate`.
   Rooms need a cue ("room", "cabin") unless the bare number is a known room; spaced digits ("8 7 4")
   and digit words work. An unknown number after a cue is an unlinked ROOM.
2. Short codes (`SU`, `DVJ`) only as written (case-sensitive, not in all-caps input) or next to a
   title/"sir"/"mam". Codes that are common words need the title ("Dr as"), and a short code that is
   also a course code (`RM`) needs an honorific. `exact_case_codes` (SE, CV, …) match only as written.
3. Gazetteer and lexicon phrases, longest span first: exact, normalised, space-insensitive ("sri devi",
   "m l") and function-word-free ("MSc in AI and ML"), then rapidfuzz `token_set_ratio` ≥
   `thresholds.entity_match_min`. **token_set_ratio scores any word subset as 100** ("learning" vs
   "deep learning"), so fuzzy only compares spans and aliases with the same word count, and every
   content word needs a counterpart ≥ `entities.fuzzy_word_min`. A higher word count is allowed only
   as an ASR split, scored on the strings without spaces.
3b. Name completeness (`_reject_partial_names`). A first or last name alone may link ("Dr John" -> Dr Tegil
   J John). But when the match covers only part of a name (initials count: "Dr Sridevi" is part of
   "Dr Sridevi R"), and a word beside it isn't a stopword, an honorific, or listed in the lexicon's
   `name_adjacent_words` (inside, left, moved, call…), the mention is **unlinked** (`method="partial_name"`).
   The candidate is kept only in `near_misses` for "did you mean ...?".
   - A Title-case word in mixed-case text ("Dr John Mathew") declines and escalates `not_in_kb`.
   - A lowercase word ("where is john mathew", with or without a title) sets `needs_confirmation`: the answer
     asks "do you mean …?" and gives no facts.
   - Before the name, a lowercase word only counts in all-lowercase text.
   - Titled unknown names ("dr neta") get near misses by fuzzy name similarity (`entities.fuzzy_word_min`).
   - Tests: the extractor runs over every fact text and none may produce a partial-name mention, and no
     `name_adjacent_words` entry may be part of a faculty name. To stop a needless clarification, add the word
     to `name_adjacent_words`; never loosen the check.
4. "Dr <name not in the KB>" gives an unlinked faculty mention.

Rules that matter downstream:
- **Ambiguity is never resolved by guessing.** All candidates within `entities.ambiguity_margin`
  of the best are returned with `ambiguous=True`, and the contract `Entity` gets `canonical_id=None`.
  Use `candidates[].name` to ask "Did you mean …?". Context `prior_entities` breaks the tie only
  when exactly one candidate is in it (`resolved_by_context`). Real cases: "George" (two faculty),
  "Neeha" (Neetha / Neha).
- A phrase can be both a course and a research topic ("machine learning", "IoT"): both are returned.
  Deep learning, blockchain, NLP etc. are topics only. A `full_name: TODO` code is matched by its code
  only ("c programming" links nothing). A link through an unverified glossary name or alias
  ("machine learning" → ML, "MSc AIML" → MSAIM) has its score multiplied by
  `thresholds.unverified_glossary_weight`, and `Candidate.verified=False`.
- "lab" straight after a subject ("ML lab") is a class, not a FACILITY.
- The contract `Entity` has no candidates/ambiguous field. The pipeline uses `EntityMention`;
  adding candidates to the contract needs team agreement.

Eval: `data/eval/entities_test.jsonl` (gold `id`, `ambiguous: [...]`, or `id: null` + `text` for
unlinked mentions; questions with clock words carry `now`). `just eval-entities` reports P/R/F1 per
type and checks ambiguous names are flagged (`entities.target_f1`). The gold set and the rules have
the same author, so add new questions for failures; don't bend rules to single test items.

## Retrieval, generation, scoring

- **Retrieval** (`retrieval/retriever.py`): structured routes first (exact KB ids from linked
  entities, score 1.0, `via="structured"`, `ROUTES` per intent), then dense + BM25 fused by RRF with
  the intent's category filter (`retrieval.intent_categories`), optional cross-encoder rerank
  (`retrieval.rerank.enabled`, off: it lifts Hit@1 0.84 -> 0.93 but costs latency). Returns
  `RetrievalSignals` for the scorer. Never drops facts on a threshold.
- **Generation** (`generation/`): canned text for greeting/thanks/human/out-of-scope (no LLM);
  otherwise the LLM rephrases the facts into `AnswerDraft` JSON (`prompts.SYSTEM_PROMPT`, structured
  output, one retry on invalid JSON). When the LLM is off, down or times out, `templates.FactComposer`
  answers deterministically from structured facts, or returns None so the pipeline escalates.
  Current Claude models reject `temperature`; the Anthropic adapter sends `llm.effort` instead.
  The composer's parsers follow `kb/facts.py` sentence shapes: change both together (a test checks).
- **Scoring** (`scoring/`): `grounding_check` (cited ⊆ retrieved; every room, time, email, phone,
  name and code in the answer must be in a cited fact; for person intents the answer must be about the
  person asked for, else `wrong_person`; optional NLI), `confidence` (weighted formula
  from `scoring.weights`; a calibrator is used only when `just calibrate` had ≥
  `scoring.min_negatives_for_calibration` wrong dev answers, which the current dev set doesn't),
  `escalation.decide` (reason order documented in the module).

## Layout

```
config/settings.yaml      all tunables
data/raw/                 source data (read-only; never edit by hand)
data/kb/glossary.yaml     programme/subject code expansions (hand-maintained, versioned)
data/kb/entity_lexicon.yaml  facility/event words, honorifics, exact-case codes (hand-maintained, versioned)
data/processed/           ingest outputs: faculty.json, subject_codes_found.csv, data_quality_report.md (git-ignored)
data/index/               index outputs: facts.jsonl, chroma/, bm25.pkl, gazetteer.json (git-ignored)
data/eval/qa_test.jsonl   gold end-to-end QA set with dev/test split (versioned)
data/eval/retrieval_test.jsonl  question -> gold fact_ids (versioned)
data/eval/intent_*.jsonl  intent seed set (train) and HELD-OUT set (test), versioned
data/eval/entities_test.jsonl  gold entities (versioned)
data/models/              trained artifacts: intent.joblib, confidence.joblib (git-ignored)
docs/                     interface.md (teammates' contract + log schema), prompts.md (generated)
logs/queries.jsonl        per-request query log for analytics (git-ignored)
reports/                  generated reports (git-ignored) except baseline.json, iterations.md, data_requests.md
src/rag_core/
  contracts.py            I/O contract (pydantic)
  config.py               typed settings + secrets loader
  pipeline.py, api.py, __main__.py   end-to-end pipeline + query log, FastAPI app, CLI
  kb/                     models, parsing, glossary, report, ingest_faculty (Excel → faculty.json), facts
  nlu/                    intent (rules + LR + LLM fallback), train_intent, eval_intent; gazetteer, entities, eval_entities
  retrieval/              embeddings, bm25, build_index; retriever (routes + RRF + rerank), rerank, eval_retrieval
  generation/             llm_client (anthropic | openai | ollama + fake), prompts, templates, answer
  scoring/                grounding_check, confidence, escalation, calibrate
  eval/                   qa (gold set + grading), run_eval (the 4 metrics + latency, reports, baseline)
tests/
```

## Data quirks: `8th_floor_faculty_final_data.xlsx` (inspected 2026-10-01)

Shape and format:
- One sheet, `8th Floor Faculty`. There are 36 faculty rows (`Sl No` 1–36) and 22 columns: Sl No,
  Full Name, Short Name, Department, Specialization, Block, Floor, Office Room, Programmes Taught,
  Currently Handling, Research Areas, Email, Gender, Consultation Hours, Weekly Hours,
  Monday…Saturday, Notes. The sheet's dimension claims `A1:V997`. pandas + openpyxl currently read
  exactly 36 rows, but keep a defensive all-empty-row drop.
- Read with `dtype=str` (pandas 3.x is installed) and convert `Sl No`/`Weekly Hours` explicitly.
- **Faculty only.** There is no data for programmes, courses, classrooms, labs, facilities, events or
  important locations, which the problem statement requires. Questions about those (e.g. the
  statement's own example, "Where is the MSc AI and ML classroom?") **must escalate** until verified
  data is added under `data/raw/`.

Location:
- **Half the "8th floor" faculty are not on the 8th floor.** 18 rows are Central Block / Eighth Floor.
  17 rows are elsewhere: Block I, Block II, R & D Block, Kuriakose Elias Hall, and Central Block
  Ninth Floor. Dr SURESH K has no Block, Floor or Room at all. Answers for off-floor faculty must say
  they're not on this floor; navigation can't route there. Use `kb.home_block`/`kb.home_floor`.
- **`Office Room` has mixed types.** Rows 1–15 are numbers (871…885) and the rest are text. Always
  read it as `str`. Room `014` relies on being text to keep its leading zero, and `885` appears both
  as a number (Neha Singhal) and as text (Helen K Joy).
- **Some `Office Room` values aren't rooms**: `332 C`, `628-11`, `Insight Zone 2, Centre for AI`, and
  `Director, Center for Research (PhD)`, which is a designation, not a place.
- **Shared rooms**, so a room maps to many people: 871 (Neetha S S, Shivangi Singh), 872 (Binayak
  Dutta, Syam Mohan E), 879 (Somnath Sinha, Thirunavukkarasu V), 885 (Neha Singhal, Helen K Joy),
  607A (Ashwini Patil, Manasa N Kulkarni, Nizar Banu P K), 622 (Limna Das P, Logeshwaran J, Smera C,
  Surabhi Saxena).
- **Conflicting locations, recorded only in `Notes`.** Helen K Joy is listed in 885, but the notes say
  "main list also mentions room 875 (Block 4, 8th floor)", and 875 is Sridevi R's room. For
  Thirunavukkarasu V, 879 is noted as "Central Block (old)". Surface these conflicts, don't resolve
  them silently.

Names and identifiers:
- **Names** are `Dr` (no period) + ALL CAPS, with initials as trailing tokens (`Dr SYAM MOHAN E`,
  `Dr NIZAR BANU P K`). Visitors will say "Dr. Syam Mohan", "Deepa Jose", "Sridevi ma'am", "Father
  Jossy". Normalise and fuzzy-match (rapidfuzz). Don't require exact matches.
- **`Short Name` is a timetable code, not an alias to fuzzy-match.** `AS`, `NE`, `SH`, `DB`, `SS` and
  `RM` collide with English words or course codes. `RM` (Dr Ramamurthy B) is also a course code in
  the timetable (`2 MCA B RM`, `7 BSC RM`). Codes look alike (`RK` Kavitha R vs `RKR` Rajesh Kanna R;
  `KS` Suresh K vs `KNS` Saravanan K N), some are reversed (`JC` = Chandra J), and one is `Fr.J`
  (Jossy P George). Only match an exact, case-sensitive standalone token, if at all.
  The timetable also mentions `SG`, which is not in the sheet.
- **Name/email spelling drift**: Dr PETER AUGUSTIN D → `peter.augustine@`. Don't "correct" either one.

Missing values (say "not available", never fill in):
- **Email** (3): Kavitha R, Nizar Banu P K, Suresh K. The existing emails mix patterns
  (`deepa.v.jose`, `helenk.joy`, `tegil.john`, `rajeshkanna.r`, `frjossy`), so **never synthesise one**.
- **Consultation Hours** (8): Tegil J John, Kavitha R, Jobin Francis, Jossy P George, Limna Das P,
  Nizar Banu P K, Rupali Sunil Wagh, Suresh K.
- **Research Areas** (9): Neetha S S, Shivangi Singh, Syam Mohan E, Somnath Sinha, Jossy P George,
  Limna Das P, Peter Augustin D, Ramamurthy B, Thirunavukkarasu V.

Free-text fields:
- **`Consultation Hours` is free text in a dozen formats**: `.` or `:` as the time separator, am/pm
  often missing, a day with no time (`Monday`), a time with no day (`03:30 pm to 07:00 pm`), broken
  fragments (`Monday to; Wednesday; Friday = 11:45 am to 2:45 pm`, `1:30 PM to 2:30 3:30 to 4:30`),
  a missing separator (`...9.45 amUG Students: ...`) and leaked list numbering (`02:00pm, 2.;`).
  Keep the verbatim text as the citable fact. Any parsing for "is X free now?" must be best-effort
  and return *unknown* (→ escalate) when it can't parse.
- **Timetable (`Monday`…`Saturday`)** cells hold several lines of the form
  `<time range> - <class code>`. The XML stores `\r\n` but openpyxl yields `\n`, so split on any
  line break. am/pm only appears on the end time (`9.45 to 11.45 am`,
  `11.45 to 12.45pm`, `10 to 12 pm`). Class codes look like `<sem> <PROG> [section] <COURSE> [LAB]`
  but with inconsistent spacing and hyphens (`2 MCA A DSA`, `1BCA B-WSD`, `3 BCA A -RPA`, `7BCA-RM`).
  **Course abbreviations (ML, DSA, PP, OS, DAA, TML, SPR, AMI, MOT, MOP, NDM…) aren't expanded
  anywhere** and need a verified glossary, not LLM guesses. Timetable lines have **no room**, so they
  can't answer "where is the class". An empty day can't distinguish "no class" from "not recorded".
- **Programme naming** varies: `MSc AIML` (Programmes Taught), `MSAIM` (timetable codes) and
  "MSc AI and ML" (problem statement). Treat them as aliases only once the department confirms it.
- **BSc lines carry a stream token** before the subject (`3BSC CS DBMS`, `3BSC CM JAVA LAB`,
  `1BSC CS C PROG LAB`), stored as `ClassSlot.stream`. Subject codes can be two words (`C PROG`).
- **Bare and odd codes**: `MDC` and `CPCG` appear with no programme or semester. `MSC CSA` (Sandeep J,
  Saturday) parses as programme MSC + subject CSA, but the notes call it a separate block, so CSA
  may really be a programme. Both cases are listed in the quality report.
- **Research areas split on commas/"and" is lossy** for a few phrases ("MANET and VANET", "AR and VR
  in education and healthcare", "along with …"). Show `research_areas_raw` to visitors.

Columns that need care:
- **`Currently Handling` contradicts the timetable** for many rows. It says UG while the timetable
  shows MCA/MSc AIML classes, and `Notes` says so. Prefer not to answer UG/PG questions from that
  column alone.
- **`Notes` is curator commentary**, e.g. "Added: … location from main list". It refers to a "main
  list" and a "short-names sheet" we don't have. Use it to flag caveats and conflicts. Don't read it
  out to visitors verbatim.
- **Low-signal columns**: `Department` is always "Computer Science". `Specialization` is mostly
  "Computer Science", with odd values (`Transportation`, `School of Computing Sciences`). There are
  typos and truncation in the source (`Predicitve`, `Enginnering`, `...network performance and
  securit`). Store values verbatim and never present a corrected version as fact.
- `Gender` exists. The dialogue team may use it for pronoun resolution, but this module doesn't
  need it.

Ops note: `data/index/` (Chroma's SQLite) sits inside OneDrive. If you see "database is locked",
point `paths.chroma_dir` outside OneDrive.
