# Smart-University-Project

AI-Powered Intelligent Robotic Receptionist for a Smart University Floor (Central Block, 8th floor, CHRIST University).

Visitors walk up to a kiosk/robot on the 8th floor and ask, by voice or touch, where a faculty member sits, when
they can be met, who teaches a subject, or where a room is. The receptionist answers only from verified
department data. When it can't verify something, it hands over to the human receptionist instead of guessing.

## Modules

Each part of the system lives in its own top-level folder with its own README. Add yours the same way.

| folder | module | what it does | status |
|---|---|---|---|
| [`rag-core/`](rag-core/) | Language + RAG core | question -> intent + entities -> retrieval from the faculty knowledge base -> grounded spoken answer -> confidence and hand-over decision. HTTP API: `POST /answer`. | built, tested; **not production-ready yet** (see its README) |
| [`voice-memory/`](voice-memory/) | Voice, memory + knowledge base (Portion 2) | speech-to-text and text-to-speech; conversation memory that rewrites follow-ups ("where is *her* office?" -> "where is Dr X's office?"); the escalation message and "talk to the receptionist" hand-off; response-latency and query-completion metrics. HTTP API for the UI: `POST /turn`. The KB extension (classrooms, labs, facilities, events) is `rag-core/data/raw/campus_info.xlsx`. | built, tested offline; speech models not yet run on the kiosk (see its README) |
| [`kiosk-ui/`](kiosk-ui/) | Kiosk UI, navigation / map, analytics dashboard, evaluation | screen: greeting, question, answer, clarification buttons, "talk to the receptionist"; the 8th-floor map with a route and directions to the room; the dashboard over all three logs; the end-to-end test set and final report. Web app on `:8002` (`/` and `/dashboard`). | built, tested; **map layout is a placeholder** until checked on the floor (see its README) |

## How the modules talk to each other

```text
visitor --voice--> voice-memory: STT --text--> memory (resolves "her", "that room")
                                              |
                                              v  POST /answer {"question": "...", "context": {...}}
                                          rag-core  --appends--> logs/queries.jsonl --> analytics
                                              |
                                              v  RAGResult: answer, escalate, entities (room:874 ...), sources
                         voice-memory: hand-off offer if escalate, TTS speaks the answer
                                              |  POST /turn -> TurnResult (answer, options, room_ids, latency)
                     kiosk-ui (screen) <------+------> navigation (room ids -> map route)
                                              +------> logs/turns.jsonl --> analytics (latency, completion rate)
```

The exact contract is in [`rag-core/docs/interface.md`](rag-core/docs/interface.md): request and response fields,
escalation reasons, five worked examples, and the query-log schema for analytics. Changing the contract needs the
whole team's agreement.

## Working in this repo

- **Run everything:** see [`RUN.md`](RUN.md).
- **Clone:** `git clone https://github.com/athimohan0408/Smart-University-Project.git`
- **Branches:** work on a branch (`git checkout -b <your-name>/<what>`), push it, and open a pull request into
  `main`. Don't push straight to `main` once others depend on it.
- **Your module:** put it in its own folder with a README (what it does, how to run it, how it connects to the
  others). Keep its dependencies inside that folder.
- **Never commit secrets** (API keys, passwords). Each module keeps them in its own git-ignored `.env`; commit a
  `.env.example` with empty values instead.
- **Don't commit generated files** (indexes, trained models, virtual environments, caches): each module's
  `.gitignore` lists them, and its README says how to rebuild them.
- **Data note:** this repository is public. `rag-core/data/raw/` contains the faculty sheet (names, office rooms,
  emails, consultation hours) used as the knowledge base.
