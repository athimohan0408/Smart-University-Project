# voice-memory: voice, conversation memory and receptionist hand-off (Portion 2)

The ears, mouth and memory of the 8th-floor receptionist. It sits between the visitor and
[`rag-core`](../rag-core/) (Portion 1, the brain):

```text
visitor --voice--> STT --text--> memory.rewrite --standalone question--> rag-core POST /answer
                                   ^                                        |
                                   +------------ memory.observe <-----------+ RAGResult
                                                                            |
          kiosk UI <-- answer, options, room_ids <-- hand-off (escalate?) <-+--> TTS --voice--> visitor
```

What this portion delivers:

| deliverable | where |
|---|---|
| **The knowledge base**, loaded from `8th_floor_faculty_final_data.xlsx` and extended with classrooms, labs, facilities and events, updatable without retraining | built and served by rag-core: the faculty sheet plus the new campus sheet `rag-core/data/raw/campus_info.xlsx` ([below](#the-knowledge-base-faculty--campus-sheet)) |
| **Voice in / voice out** | `speech/stt.py` (offline Faster-Whisper, biased with KB names), `speech/tts.py` (offline Piper, or the OS voice), `speech/speakable.py`, `speech/audio.py` (kiosk mic + speaker) |
| **Follow-up -> standalone question** | `memory.py`: `ConversationMemory.rewrite(session_id, text)` |
| **Escalation message + "contact the receptionist" option** | `handoff.py` |
| One full turn, for the UI | `receptionist.py` (`Receptionist.handle_text` / `handle_audio`), HTTP: `api.py` |
| **Metrics: response latency, query completion rate** | `metrics.py` (`python -m voice_memory.metrics`) |

## Quick start

Same Python 3.11+ environment as rag-core is simplest (needed for `rag.mode: inprocess` and the
integration tests):

```bash
cd voice-memory
pip install -e ".[dev]"                 # memory, hand-off, API, metrics
pip install -e ".[stt,tts,audio]"       # + speech (faster-whisper, piper, sounddevice)
cp .env.example .env                    # optional: RECEPTIONIST_WEBHOOK_URL for desk alerts
python -m pytest                        # 60 tests, offline
```

Run it (rag-core must be serving: `cd ../rag-core && just serve`):

```bash
python -m voice_memory                  # type questions; follow-ups are resolved
python -m voice_memory --voice          # kiosk mode: microphone in, speaker out
python -m voice_memory --wav q.wav      # answer one recorded question
python -m voice_memory.api              # HTTP API for the kiosk UI on 127.0.0.1:8001
python -m voice_memory.metrics          # latency + completion report -> reports/voice_metrics.md
```

### Speech models (downloaded once)

- **STT**: Faster-Whisper downloads `stt.model` (default `small.en`, about 480 MB; `base.en` is faster
  on a slow kiosk) from Hugging Face the first time. Then set `HF_HUB_OFFLINE=1` in `.env`.
- **TTS**: download a Piper voice (`.onnx` + `.onnx.json`) into `models/piper/`, e.g. `en_US-lessac-medium`
  from <https://huggingface.co/rhasspy/piper-voices> (`en/en_US/lessac/medium/`). Indian English
  voices: `en_IN` is not in Piper yet; `en_GB-alba-medium` is a clear alternative. Or set `tts.backend:
  pyttsx3` to use the operating system's voice (no download).

## Conversation memory (`memory.py`)

rag-core's contract says questions arrive *resolved*. Memory keeps a small per-visitor state (last
person, place, event, subject; the last question; an open clarification) and rewrites:

| visitor says (after...) | sent to rag-core |
|---|---|
| "where is **her** office?" (after Dr Nisha Varghese) | "where is Dr Nisha Varghese's office?" |
| "is **she** free on Friday?" / "is **ma'am** free now?" | "is Dr Nisha Varghese free on Friday?" |
| "where is **his** cabin?" (after Dr Binayak Dutta) / "is **sir** in today?" | "where is Dr Binayak Dutta's cabin?" |
| "how do I get **there**?" (after a person) | "how do I get to Dr Nisha Varghese's office?" |
| "how do I get to **that room**?" (after room 874) | "how do I get to room 874?" |
| "when is **it** open?" (after a lab) | "when is Sample AIML Lab open?" |
| "**and Dr Binayak Dutta?**" (after "where is Dr Nisha Varghese's cabin?") | "where is Dr Binayak Dutta's cabin?" |
| "**what about Friday?**" (after "is Dr X free on Monday?") | "is Dr X free on Friday?" |
| "**the first one**" / "**Jossy**" (after "Dr Jossy P George or Dr Naived George Eapen?") | "Where is Dr Jossy P George?" |
| "**yes**" (after "do you mean Dr Tegil J John?") | "where is Dr Tegil J John" |

Safety rules (tested):
- **Gender comes from the sheet's Gender column.** "her" after a man is left unresolved, never guessed.
- A pronoun next to a name ("where is Dr Deepa V Jose and is she free") is about that name: no rewrite.
- Nothing to refer to => the question goes through unchanged and rag-core escalates (no entity), so a
  visitor is never told about the wrong person.
- An unrelated follow-up never inherits a person: `memory.send_last_entities` is **off**, because rag-core
  would otherwise attach the last person to "where is the washroom?" (`find_room` needs an entity).
  `prior_entities` is sent, so rag-core can break "Dr George" ties with people already discussed.
- Sessions expire after `memory.session_idle_s` (a new visitor); the UI can also call `/session/reset`.

## Escalation and the receptionist (`handoff.py`)

When rag-core escalates, its `answer` already says what couldn't be verified. This module adds:
- the spoken offer: *"Would you like me to call the receptionist for you?"*, and the buttons
  **Talk to the receptionist** / **Ask another question**;
- "yes" (or the button) -> a ticket in `logs/handoffs.jsonl` and, if `RECEPTIONIST_WEBHOOK_URL` is set, a
  POST to the desk; "no" -> "No problem. You can ask me something else."; anything else is the next question;
- a visitor who asks for a person (`user_requested`) is handed over at once, without the extra question;
- rag-core unreachable -> the same polite offer (`escalate_reason: rag_unavailable`), never a guess.

Tickets hold the question, the reason and the answer given. Nothing else about the visitor.

## HTTP API for the kiosk UI (`api.py`)

| call | body | returns |
|---|---|---|
| `POST /turn` | `{"session_id": "kiosk-01", "text": "where is her office?", "speak": true}` | `TurnResult` |
| `POST /turn/audio` | multipart `session_id`, `audio` (16-bit WAV, any sample rate) | `TurnResult` |
| `POST /receptionist` | `{"session_id": ...}` (the button) | `Ticket` |
| `POST /session/reset` | `{"session_id": ...}` | `{"status": "reset"}` |
| `GET /health` | | this module + rag-core `/health` |

`TurnResult`: `transcript`, `question` (what rag-core was asked), `answer` (screen), `speech` (what is
spoken), `audio_wav_base64`, `outcome` (`answered` / `clarification` / `escalated` / `handoff` /
`declined` / `social` / `no_speech`), `escalate`, `escalate_reason`, `options` (buttons), `ticket`,
`intent`, `confidence`, `entities`, `room_ids` (for **navigation**), `latency_ms`.

## Metrics this module owns (`metrics.py`)

Every turn appends a line to `logs/turns.jsonl`:
`{schema_version, timestamp, session_id, turn_id, input: voice|text, question, rewritten, rewrite_rules,
outcome, offer_reply, intent, escalate_reason, latency_ms: {stt, memory, rag, handoff, tts, total}, rag_server_ms}`.

- **Response latency**: `latency_ms.total`, from the end of the visitor's speech (or a typed question) to
  the answer audio being ready. Reported p50 / p95 / max, per stage and per input type. Target:
  `metrics.target_latency_p95_ms` (3000 ms, provisional).
- **Query completion rate**: completed / (completed + escalated + abandoned). One query can span turns: a
  clarification followed by an answer is one completed query; an escalation ends a query uncompleted; a
  clarification left open when the visitor walks away is abandoned. Greetings, thanks and yes/no replies
  to the hand-off offer are not queries. Target: `metrics.target_completion_rate` (85%, provisional).

`python -m voice_memory.metrics` writes `reports/voice_metrics.md`. rag-core's own `logs/queries.jsonl`
has the per-request detail (retrieved facts, grounding issues) for the analytics dashboard.

## The knowledge base: faculty + campus sheet

The KB is built by rag-core, so there is one source of truth:
1. **Faculty**: `rag-core/data/raw/8th_floor_faculty_final_data.xlsx` (36 faculty), as before.
2. **Campus** (new): `rag-core/data/raw/campus_info.xlsx`, sheets `Places` (classrooms, labs,
   washrooms, wifi, printing, lift, seminar hall, ...) and `Events` (seminars, workshops, ...). The file in
   the repo is an **empty template with instructions in its README sheet**: there is no verified data for
   these yet, and the receptionist must not invent any. Rows without `Verified By` are not loaded.

**Updating needs no retraining**: edit the sheet, then `just campus-check` (lists rows that won't load and
why) and `just index`, or `POST /reload-kb` on a running rag-core. Only facts, search index and entity
vocabulary are rebuilt; the intent classifier never sees KB data. Place kinds must be one of the
facility kinds in `rag-core/data/kb/entity_lexicon.yaml` (that's how "toilet", "restroom" and "washroom"
all find the washroom rows).

Until rows are added, questions about classrooms, labs, facilities and events escalate exactly as before.

## Tests

`python -m pytest` (offline): memory rules, speakable text, audio helpers, hand-off and webhook, full turns
with a scripted rag-core, the turn log and both metrics, the HTTP API, and `tests/test_with_rag_core.py`,
which runs memory against the **real** rag-core pipeline (entity linking, routes, templates, grounding,
escalation) in memory, with rag-core's offline stand-ins for the embedding model and a keyword stub for
the intent classifier. That file is skipped if rag-core isn't installed.

## Not done yet / needs the team

- **Real campus data**: the department needs to fill `campus_info.xlsx` (see `rag-core/reports/data_requests.md`).
- **Speech on the real kiosk**: STT/TTS are built and unit-tested with fakes, but have not been run here
  with the real models (the cloud environment that built this could not reach Hugging Face). Measure
  latency on the kiosk hardware, then choose `stt.model` (`base.en` vs `small.en`).
- **Indian-English accuracy**: record 30-50 real questions at the kiosk and check the transcripts,
  especially names. The KB-name prompt helps Whisper with names; it doesn't replace testing.
- **Targets**: 3 s p95 and 85% completion are placeholders until the team agrees on them.
