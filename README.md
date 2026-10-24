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
| `speech/` | Speech | speech-to-text for the visitor's question, text-to-speech for the answer | to do |
| `dialogue/` | Dialogue / context | follow-ups and pronouns ("where is *her* office?" -> "where is Dr X's office?") before calling rag-core | to do |
| `kiosk-ui/` | Kiosk UI | screen: shows the answer, the "talk to the receptionist" button, clarification choices | to do |
| `navigation/` | Navigation / map | turns `room:` ids from rag-core into directions on the floor map | to do |
| `analytics/` | Analytics dashboard | reads rag-core's query log (`logs/queries.jsonl`) for usage, escalations, response time | to do |

## How the modules talk to each other

```text
visitor --voice--> speech (STT) --text--> dialogue (resolves "her", "that room")
                                              |
                                              v  POST /answer {"question": "...", "context": {...}}
                                          rag-core  --appends--> logs/queries.jsonl --> analytics
                                              |
                                              v  RAGResult: answer, escalate, entities (room:874 ...), sources
                     kiosk-ui (screen) <------+------> speech (TTS) speaks `answer`
                                              +------> navigation (room ids -> map route)
```

The exact contract is in [`rag-core/docs/interface.md`](rag-core/docs/interface.md): request and response fields,
escalation reasons, five worked examples, and the query-log schema for analytics. Changing the contract needs the
whole team's agreement.

## Working in this repo

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
