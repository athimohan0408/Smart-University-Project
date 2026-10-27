# rag_core interface

How the speech/dialogue, kiosk UI, navigation and analytics modules talk to the language + RAG core.
The Python models are in `src/rag_core/contracts.py` (the source of truth). Changing them needs team
agreement, a change to contracts.py, and an update to this file and CLAUDE.md in the same change.

## Calling it

| how | call |
|---|---|
| HTTP | `POST /answer`, body `RAGRequest`, response `RAGResult` (`python -m rag_core.api`; host/port from `api` in `config/settings.yaml`, default `127.0.0.1:8000`) |
| Python | `from rag_core.pipeline import answer, warmup`; `warmup()` once at start-up, then `answer(question, context=None, now=None) -> RAGResult` |
| CLI (debugging) | `python -m rag_core "where is Dr Nisha Varghese's cabin?" [--now 2026-10-20T14:30] [--json]` |

Other endpoints:

- `GET /health`: `{"status": "ok", "facts": 526, "facts_digest": "...", "llm": "<status>", "llm_ready": false,
  "llm_last_success": null, "llm_last_error": null, "rerank": false, "calibrated_confidence": false}`.
  - `llm` is the configuration status (`configured`, `disabled (generation.mode: template)`, or
    `unavailable, answering from templates: <reason>`).
  - `llm_ready` is true only when an LLM is configured and the most recent real call (if any) succeeded.
  - `llm_last_error` holds the last failure, with credential values redacted.
  - Every request still tries a configured LLM and falls back to templates on failure, so answers keep flowing
    when `llm_ready` is false.
- `POST /reload-kb`: re-ingests the faculty sheet, rebuilds the index from `data/raw` + `data/kb`, and swaps in the new
  knowledge base without a restart. Requests wait while it runs (a few seconds). Returns `{"status": "reloaded", "facts": N}`.

Models and indexes load once, at start-up (the server calls `warmup()`; allow about 15 s). Set `HF_HUB_OFFLINE=1`
once the models are downloaded: otherwise every start checks the Hugging Face Hub, which can add a minute.
Reuse one HTTP connection (keep-alive): a new connection per request costs about 200 ms on Windows, while the
answer itself takes about 20 ms (p95 about 33 ms server side, without the LLM).

## Request: `RAGRequest`

| field | type | meaning |
|---|---|---|
| `question` | `str`, non-empty | The visitor's question, **already resolved** by the dialogue module: "where is HER office?" arrives as "where is Dr Deepa V Jose's office?". This module does no coreference. |
| `context` | `object \| null` | Optional hints. Unknown keys are ignored. |

Context keys:

| key | type | effect |
|---|---|---|
| `now` | ISO-8601 local datetime | Clock for "now", "today", "tomorrow". Default: the server clock. |
| `last_entities` | list of `canonical_id` strings (or `{"canonical_id": ...}` objects) from earlier turns | Used **only** when the question names no person, room, course, programme or topic at all, to fill that gap ("is she free on Friday?" left unresolved). Never overrides an entity in the question. These entities are not listed in `RAGResult.entities`. |
| `prior_entities` | list of `canonical_id` strings | Breaks a tie between equally good matches ("George" → two people) when exactly one candidate is in the list. |
| `session_id` | string | Copied into the query log for analytics. Never changes the answer. |

Unknown fields at the top level are rejected with HTTP 422, and so is an empty question.

## Response: `RAGResult`

| field | type | meaning |
|---|---|---|
| `answer` | `str` | What to show on screen and speak with TTS: 1-3 short sentences, no markdown, rooms as digits, times like "1:45 pm". On escalation it politely says the information can't be verified and points to the receptionist. |
| `confidence` | `float` 0-1 | Confidence that the answer is correct and grounded. |
| `escalate` | `bool` | `true` ⇒ the UI should offer the human receptionist. |
| `escalate_reason` | `str \| null` | Set exactly when `escalate` is true. Codes below. |
| `intent` / `intent_confidence` | `str`, `float` 0-1 | One of the 13 intents (`find_person`, `person_availability`, `find_by_subject`, `find_room`, `schedule_query`, `programme_info`, `event_info`, `facility_info`, `contact_info`, `greeting`, `smalltalk_or_thanks`, `escalate_to_human`, `out_of_scope`). |
| `entities` | list of `{type, text, canonical_id, score}` | What the question mentioned. `canonical_id` is `<type>:<slug>` (`faculty:deepa-v-jose`, `room:877`, `course:ml`, `day:friday`, `time:14:00`; from the campus sheet `facility:washroom` (any washroom), `facility:lab:<slug>` (one place), `event:seminar`, `event:<yyyy-mm-dd>:<slug>`) or `null` when the mention was recognised but not linked (an unknown name, a room not in the KB, a facility or event kind with no verified campus data, or an **ambiguous** name). Navigation resolves `room:` ids to the map; a place's room is in its fact (`sources`). |
| `sources` | list of `fact_id` | The KB facts the answer cites (`faculty:nisha-varghese:location`, `room:874`). Empty for greetings and for the safe "can't verify" message. |
| `latency_ms` | `{nlu, retrieval, generation, scoring, total}` | Wall time per stage in ms. |

Escalation reasons:

| `escalate_reason` | when | what the visitor hears |
|---|---|---|
| `user_requested` | The visitor asked for a person. | "Of course. Please speak to the receptionist at the desk..." |
| `out_of_scope` | Fees, admissions, hostel, weather, ... | "I can only help with faculty, rooms, timetables and consultation hours..." |
| `not_in_kb` | The facts don't hold the answer: no phone numbers anywhere, an email or consultation hours not listed, classroom/lab/event/facility data that doesn't exist yet, a person or room not in the KB. | A polite sentence saying what isn't available. For a person whose name only partly matches a faculty member ("Dr John Mathew" vs Dr Tegil J John) it adds "Did you mean Dr Tegil J John?" and gives **no** facts about that person. |
| `grounding_failed` | The generated answer stated something the cited facts don't support, cited a fact that wasn't retrieved, or answered about a different person than the one asked for. The answer is discarded. | "I'm sorry, I can't verify that. Please check with the receptionist at the desk." |
| `ambiguous_entity` | Several people match and no clarifying question could be asked. | The safe message. |
| `low_confidence` | Confidence below `thresholds.answer_min_confidence`. | The safe message. |
| `llm_error` | The LLM failed or timed out and no template could answer. | The safe message. |

**Clarifications** (several people match, e.g. "Dr George") are answered, not escalated: `escalate` is false,
`answer` asks "Which one do you mean?" and names the candidates, `sources` is empty, and the ambiguous entity has
`canonical_id: null`. The dialogue module should keep the turn open and send the visitor's choice back as a
resolved question. The same applies to a confirmation when a name can't be confidently identified. In lowercase
speech-to-text such as "where is john mathew" or "where is dr sridevi krishnan", there are no capital letters to
tell a surname from an ordinary word. The answer is then "Just to make sure I have the right person: do you mean
Dr Tegil J John?" (or "…Dr Sridevi R?"), with no facts about that person until the visitor confirms. An unknown
name close to real ones ("dr neta") is asked about the same way ("do you mean Dr Neetha S S or Dr Neha Singhal?"). *Open proposal (needs team agreement before it goes into the contract):* a
`clarification_options: list[str]` field so the UI can show buttons instead of parsing the sentence.

## Examples

These five pairs come from the running API (`python -m rag_core.api`, templates mode, no LLM).
`tests/test_interface_examples.py` replays them and checks every field except `latency_ms`, with `confidence`
compared to two decimals. `latency_ms` differs on every run.

### Example 1: Find a person (answered)

Request:

```json
{
  "question": "where is Dr Nisha Varghese's cabin?",
  "context": {
    "now": "2026-10-20T14:30:00",
    "session_id": "kiosk-01"
  }
}
```

Response:

```json
{
  "answer": "Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor, here on this floor.",
  "confidence": 0.95,
  "escalate": false,
  "escalate_reason": null,
  "intent": "find_person",
  "intent_confidence": 1.0,
  "entities": [
    {
      "type": "faculty",
      "text": "Dr Nisha Varghese",
      "canonical_id": "faculty:nisha-varghese",
      "score": 1.0
    }
  ],
  "sources": [
    "faculty:nisha-varghese:location"
  ],
  "latency_ms": {
    "nlu": 12.6,
    "retrieval": 4.7,
    "generation": 0.3,
    "scoring": 0.8,
    "total": 18.3
  }
}
```

### Example 2: Is someone free on a day (answered)

Request:

```json
{
  "question": "Is Dr Deepa V Jose free on Friday?",
  "context": {
    "now": "2026-10-20T14:30:00",
    "session_id": "kiosk-01"
  }
}
```

Response:

```json
{
  "answer": "On Friday, Dr Deepa V Jose's consultation hours are 1:45 pm to 3:45 pm. They have classes that day starting at 7:30 am, 9:45 am and 2:45 pm.",
  "confidence": 0.949,
  "escalate": false,
  "escalate_reason": null,
  "intent": "person_availability",
  "intent_confidence": 0.993,
  "entities": [
    {
      "type": "faculty",
      "text": "Dr Deepa V Jose",
      "canonical_id": "faculty:deepa-v-jose",
      "score": 1.0
    },
    {
      "type": "day",
      "text": "Friday",
      "canonical_id": "day:friday",
      "score": 1.0
    }
  ],
  "sources": [
    "faculty:deepa-v-jose:consultation",
    "faculty:deepa-v-jose:schedule:friday"
  ],
  "latency_ms": {
    "nlu": 21.8,
    "retrieval": 6.4,
    "generation": 0.3,
    "scoring": 2.5,
    "total": 31.0
  }
}
```

### Example 3: Faculty member on another floor (answered)

Request:

```json
{
  "question": "Where is Dr Ashwini Patil?",
  "context": {
    "now": "2026-10-20T14:30:00"
  }
}
```

Response:

```json
{
  "answer": "Dr Ashwini Patil is not on this floor. Their office is in Block II, First Floor, room 607A. They share this office with Dr Manasa N Kulkarni and Dr Nizar Banu P K.",
  "confidence": 0.948,
  "escalate": false,
  "escalate_reason": null,
  "intent": "find_person",
  "intent_confidence": 0.991,
  "entities": [
    {
      "type": "faculty",
      "text": "Dr Ashwini Patil",
      "canonical_id": "faculty:ashwini-patil",
      "score": 1.0
    }
  ],
  "sources": [
    "faculty:ashwini-patil:location"
  ],
  "latency_ms": {
    "nlu": 11.3,
    "retrieval": 3.3,
    "generation": 0.1,
    "scoring": 2.6,
    "total": 17.3
  }
}
```

### Example 4: Escalation: the KB has no phone numbers

Request:

```json
{
  "question": "What is Dr Nisha Varghese's phone number?",
  "context": {
    "now": "2026-10-20T14:30:00"
  }
}
```

Response:

```json
{
  "answer": "I'm sorry, I can't verify a phone number for Dr Nisha Varghese. Their listed email is nisha.varghese@christuniversity.in. Please check with the receptionist at the desk.",
  "confidence": 0.781,
  "escalate": true,
  "escalate_reason": "not_in_kb",
  "intent": "contact_info",
  "intent_confidence": 0.902,
  "entities": [
    {
      "type": "faculty",
      "text": "Dr Nisha Varghese",
      "canonical_id": "faculty:nisha-varghese",
      "score": 1.0
    }
  ],
  "sources": [
    "faculty:nisha-varghese:profile"
  ],
  "latency_ms": {
    "nlu": 22.4,
    "retrieval": 6.3,
    "generation": 0.2,
    "scoring": 4.7,
    "total": 33.6
  }
}
```

### Example 5: Clarification: two people match

Request:

```json
{
  "question": "Where is Dr George?",
  "context": {
    "now": "2026-10-20T14:30:00"
  }
}
```

Response:

```json
{
  "answer": "I found more than one person who matches Dr George: Dr Jossy P George or Dr Naived George Eapen. Which one do you mean?",
  "confidence": 0.799,
  "escalate": false,
  "escalate_reason": null,
  "intent": "find_person",
  "intent_confidence": 0.994,
  "entities": [
    {
      "type": "faculty",
      "text": "Dr George",
      "canonical_id": null,
      "score": 1.0
    }
  ],
  "sources": [],
  "latency_ms": {
    "nlu": 12.9,
    "retrieval": 3.9,
    "generation": 0.0,
    "scoring": 7.2,
    "total": 24.1
  }
}
```

## Query log for analytics: `logs/queries.jsonl`

One JSON object per line, appended for every `/answer` request (path: `paths.query_log`). It holds the question
and what the system did. Nothing else about the visitor is recorded: no IP address, device id, face, voice or
name field. (The question text is logged as received; the dialogue module should not put personal details in it.)

| field | type | meaning |
|---|---|---|
| `schema_version` | int | `1`. Bumped on any incompatible change. |
| `timestamp` | ISO-8601 UTC string | When the answer was produced. |
| `request_id` | 32-char hex | Unique per request. |
| `session_id` | string or null | From `context.session_id`, as given (an opaque kiosk/session id). Use it for query completion rate. |
| `question` | string | The question after whitespace/quote normalisation. |
| `intent`, `intent_confidence` | string, float | As in `RAGResult`. |
| `entities` | list | As in `RAGResult.entities`. |
| `retrieved_fact_ids` | list of string | Every fact retrieved (up to `retrieval.top_k`), best first. |
| `sources` | list of string | The facts the answer cited. |
| `answer_source` | string | `llm`, `template` (LLM off), `fallback` (LLM failed, template answered), `canned` (greeting/thanks/human/out of scope) or `none` (nothing could answer). |
| `confidence` | float | As in `RAGResult`. |
| `escalate`, `escalate_reason` | bool, string or null | As in `RAGResult`. Group by `escalate_reason`. |
| `grounding_issues` | list of string | Kinds of unsupported claims found (`unsupported_room`, `hallucinated_citation`, `wrong_person`, ...). Non-empty ⇒ `grounding_failed`. |
| `clarification` | bool | The answer asked "which one do you mean?". |
| `latency_ms` | object | `nlu`, `retrieval`, `generation`, `scoring`, `total` in ms (one decimal). |

Example line:

```json
{"schema_version": 1, "timestamp": "2026-10-23T05:02:41.824+00:00", "request_id": "7eaab24d18db4ad280831a4e2117eafa", "session_id": "kiosk-01", "question": "where is Dr Nisha Varghese's cabin?", "intent": "find_person", "intent_confidence": 0.9977, "entities": [{"type": "faculty", "text": "Dr Nisha Varghese", "canonical_id": "faculty:nisha-varghese", "score": 1.0}], "retrieved_fact_ids": ["faculty:nisha-varghese:location", "faculty:nisha-varghese:profile", "room:874", "faculty:suresh-k:location", "faculty:neetha-s-s:location"], "sources": ["faculty:nisha-varghese:location"], "answer_source": "template", "confidence": 0.9495, "escalate": false, "escalate_reason": null, "grounding_issues": [], "clarification": false, "latency_ms": {"nlu": 13.2, "retrieval": 3.8, "generation": 0.3, "scoring": 1.2, "total": 18.4}}
```
