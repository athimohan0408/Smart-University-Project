# kiosk-ui: interface, navigation and analytics (Portion 3)

The face and the dashboard of the 8th-floor receptionist:

- **Kiosk screen** (`/`): greeting, the visitor's question (typed, tapped or spoken), the answer, clarification
  buttons, the "Talk to the receptionist" button, and a details panel with the floor map.
- **Floor map + navigation**: the 8th floor with a highlighted route from reception to the room in the answer,
  step-by-step directions and distance, and (when photos are added) a 360° view along the way.
- **Analytics dashboard** (`/dashboard`): query counts, most-asked and unanswered questions, categories,
  escalation reasons, average response time and questions by hour.
- **Logging** behind the dashboard: the kiosk's own event log, read together with rag-core's and voice-memory's logs.
- **Evaluation**: the 29-turn end-to-end test set and the final report, which compiles the whole team's numbers.

```text
browser (kiosk screen, dashboard)
   |  only talks to this server
   v
kiosk-ui :8002  --POST /turn, /turn/audio, /receptionist, /session/reset-->  voice-memory :8001  -->  rag-core :8000
   |  room_ids -> navigation (data/floor_map.yaml) -> route + directions
   +--appends--> logs/ui_events.jsonl
   +--reads----> ../rag-core/logs/queries.jsonl, ../voice-memory/logs/turns.jsonl  -> /api/analytics
```

## Run it

One-time (from the repo root, venv active, after the setup in [`RUN.md`](../RUN.md)):

```bash
pip install -e "./kiosk-ui[dev]"
```

Three terminals, venv active in each:

```bash
cd rag-core;     just serve                 # :8000
cd voice-memory; python -m voice_memory.api # :8001
cd kiosk-ui;     python -m kiosk_ui         # :8002
```

Open http://127.0.0.1:8002/ for the kiosk and http://127.0.0.1:8002/dashboard for analytics. The microphone
button needs voice-memory's speech extras (`pip install -e "./voice-memory[stt,tts-basic,audio]"`); typed and
tapped questions work without them.

Other commands (from `kiosk-ui/`):

| command | what it does |
|---|---|
| `python -m pytest` | this module's tests: offline, with a fake voice-memory |
| `python -m kiosk_ui.navigation` | checks the map (every room reachable) and prints every route |
| `python -m kiosk_ui.analytics [--days 7] [--json]` | the dashboard numbers as text |
| `python -m kiosk_ui.evaluate` | runs the test set end to end and writes `reports/evaluation_report.md` (needs voice-memory + rag-core running) |

Settings are in [`config/settings.yaml`](config/settings.yaml) (ports, voice-memory URL, idle reset, log paths).
This module has no secrets.

## The floor map: `data/floor_map.yaml`

The layout comes from the department's **8th Floor Layout Plan** board on the floor (outer rooms 808-815 and
the MCA Lab, washrooms, water dispensers, lifts, staircases, CCD café, Faculty Lounge, the Faculty Staff Room and the
open hall), a hand sketch of the inside of the **Faculty Staff Room (C816)** (cabins 871-885, 888 and 890 in two rows
with passages), and the team's markings on the map: the kiosk stands in the north corridor outside the middle of the
staff room, and the staff room's **only three ways in** are open passages on its south side, by room 890, between
888 and 885, and between 884 and 883 (beside the kiosk). There is no door at the east end. 886, 887 and 889 are not
in the sketch and are not drawn. Like the board, the map is **not to scale** (the staff room is drawn taller so the
cabins are readable), so distances are approximate; the kiosk says "Not to scale" above the map.

Routes start at the kiosk, enter the staff room through the nearest of the three passages and run along its
middle aisle ("From the kiosk, go through the Faculty Staff Room entrance C816, between rooms 884 and 883 and walk about
8 m to the middle aisle. Turn left and walk about 4 m. Room 874 is on your right."). A first step names a landmark
when it can; otherwise "left/right" assume the visitor faces the kiosk, i.e. north.

To confirm or correct it:

1. Walk a few routes with the kiosk's directions. Fix coordinates in the file if a turn or door is wrong
   (`viewbox` units: x to the right = east, y down = south). `nodes` are walkable points (a `name` makes one a
   landmark the directions mention), `edges` join them, each place's `door` meets a walkway at node `via`.
2. `python -m kiosk_ui.navigation`: every room must be reachable; read the directions.
3. Set `verified: true` and `verified_by: "<name>, <date>"`.

**The door plates and the directory board disagree with rag-core's faculty sheet for 11 rooms**: see [`reports/room_occupancy_check.md`](reports/room_occupancy_check.md). **Who sits where comes from rag-core's faculty sheet, not from the map** (the map only knows room numbers). The
sketch and the sheet disagree for 873 (sketch: Helen; sheet: Dr Helen K Joy in 885), 877 (Peter; sheet: Dr Deepa V
Jose, Dr Peter Augustin D in 974), 882 (Nizar Banu; sheet: 607A), 885 (Vaidehi; sheet: Dr Neha Singhal and Dr Helen K
Joy) and 888 (Rajesh; sheet: Dr Rajesh Kanna R in 322). Check with the department and update the sheet if needed.

### Walking directions (full screen)

"Show me the way" (under an answer with a route, and on the map) opens a full-screen walkthrough, like a navigation
app: a banner with a turn arrow, the distance and the instruction ("Then: ..." previews the next step); the step's
photo large beside the map; the map zooms to the current stretch while a dot walks it. **Play** reads each step in
the kiosk's voice (`GET /api/speak`), walks it, and moves on by itself to "You have arrived" with the door's photo;
**Back / Next** step through by hand. Each route carries what this needs per step: `step_spans` (the part of
`points` it walks), `step_kinds` (the arrow: start, enter, left, right, straight, around, arrive-left/right/straight),
`step_metres` and `step_photos`.

### Photos along the route

`data/photos/` holds 19 of the team's phone photos (November 2026), resized for the kiosk (about 1024 px, under
200 KB each, no metadata): 15 cabin doors with their name plates (871-874, 876-879, 881-885, 888, 890), the C816
entrance, the Faculty Lounge, the lifts and the staircase. A node or place names its photo (`photo:` in the map
file). The kiosk shows a landmark's photo next to the step that names it ("go through the Faculty Staff Room entrance
C816 ...") and the room's door next to the last step ("Look for this door"); tap to enlarge. Rooms with a photo can
also be tapped on the map. No photo yet: 875, 880, 886, 887, 889, the C817 doorway's place on the map (see below).

To add one: put a JPG in `data/photos/` (landscape or portrait, no people, ideally under 400 KB) and add
`photo: <file>` to the node or place in `data/floor_map.yaml`.

### 360° photos

Put equirectangular 360° photos (JPG, the normal export of a 360 camera or phone panorama) in
`data/panoramas/` and name them on a node or place:

```yaml
nodes:
  reception: {x: 500, y: 250, panorama: reception.jpg}
places:
  - {id: "room:874", ..., panorama: room-874-door.jpg}
```

A route collects the photos along its path (start first, destination last) and the kiosk shows a "360° view"
button with previous/next. The viewer is [Pannellum](https://pannellum.org/) (MIT), loaded from jsDelivr; for an
offline kiosk download `pannellum.js` and `pannellum.css` (2.5.6) into `src/kiosk_ui/static/vendor/` and it is used
first. Large photos: keep each under ~4 MB (e.g. 6000×3000 at JPEG quality 80) so they open quickly.

## Logs and the dashboard

| log | written by | used for |
|---|---|---|
| `../rag-core/logs/queries.jsonl` | rag-core, every `/answer` | questions, intents, escalations, server latency (schema: `rag-core/docs/interface.md`) |
| `../voice-memory/logs/turns.jsonl` | voice-memory, every turn | query completion rate (voice-memory's own definition, reused), turn latency |
| `logs/ui_events.jsonl` | this module | what the visitor waited at the screen, routes shown, buttons, errors |

`ui_events.jsonl`, one JSON object per line: `schema_version` (1), `timestamp` (UTC), `kiosk_id`, `session_id`,
`event` (`turn`, `option_chosen`, `receptionist_button`, `session_reset`, `error`), and for `turn`: `turn_id`,
`question` (as typed/heard), `resolved_question` (after memory), `outcome`, `intent`, `escalate`, `escalate_reason`,
`entities`, `room_ids`, `route_shown`, `options_shown`, `round_trip_ms` (kiosk server to voice-memory and back,
including routing), `upstream_total_ms`. Like the other logs it holds no IP address, device id, audio or name field.

When rag-core's log isn't on this machine, the dashboard counts questions from the kiosk log instead. Sessions from
the evaluation (`eval-...`) are left out, so test runs don't count as visitors.

## What the screen adds to voice-memory's answer

From rag-core's `data/processed/faculty.json` (read-only, `kb.faculty_json` in settings):

- **A route for a person**: "where is Dr X?" comes back with the room only in the answer text. The kiosk adds a route
  to that person's 8th-floor office when the turn was answered, the person is identified (not ambiguous) and the
  answer itself names the room, so the map never shows something the receptionist didn't say.
- **Clarification buttons**: "Which one do you mean: Dr A or Dr B?" comes without `options`; the names in the
  answer become buttons. Buttons under an earlier answer stop working once the visitor asks something else.
- **Rooms only the map knows**: "where is room 815?" (808-815, the MCA Lab, cabins not in the faculty sheet) has no
  answer in rag-core's knowledge base until the campus sheet is filled, so it escalates with `not_in_kb`. When the
  room is on the floor map, the kiosk answers from the map instead ("Room 815 is here on the 8th floor, on the
  north-east side of the floor. I've shown you the way on the map."), spoken in the same voice, with the route. It
  only says where the room is, never what it is or who is in it; rooms on neither stay with the receptionist.
  The turn is logged with `answered_by: kiosk_floor_map`; rag-core's log still counts it as unanswered, so the
  dashboard keeps showing which rooms to add to the campus sheet.
- **Voice**: the answer audio from voice-memory when it sends some (Piper, `tts: backend: piper`, voice file in
  `voice-memory/models/piper/`), otherwise the browser's own speech (silent where the browser has no voices).
- **Two screens**: the page opens on a welcome screen with the robot large in the middle; it greets the visitor
  and, when the greeting ends, the chat screen slides in with the robot small in the corner. "Ask me a question"
  and "Speak to me" skip ahead. "Start over" and the idle reset go back to the welcome screen for the next visitor
  (after an idle reset the greeting waits for a touch, so the kiosk doesn't talk to an empty corridor).
- **The robot**: greets each visitor (slides in, waves, speaks `kiosk.greeting` from the settings with a caption
  that follows the voice), then listens, thinks, talks with lip-sync from the voice's loudness, hops for an answer
  and tilts its head when it hands over to the receptionist. Its eyes follow the visitor's finger or cursor; tapping
  it repeats the greeting; with nobody around it waves every 25 s. The greeting WAV is made once with voice-memory's
  Piper voice (`kiosk_ui/greeting.py`, `GET /api/greeting`), otherwise the browser speaks it.
- **Sound on page load**: browsers block sound until the screen is touched, so the robot greets silently and shows
  "Touch here to start talking to me"; the first touch plays the spoken greeting. On the real kiosk start Chrome with
  `--kiosk --autoplay-policy=no-user-gesture-required http://127.0.0.1:8002/` and it speaks straight away.
- **Hands-free microphone**: one tap, then the kiosk sends by itself after 0.8 s of silence (the same numbers as
  voice-memory's `audio:` settings); nothing heard for 8 s cancels, 12 s is the maximum.

## Evaluation and the final report

[`evaluation/test_set.jsonl`](evaluation/test_set.jsonl) is 26 visitor scenarios, 29 turns: rooms on this floor
(with the expected route), people on other floors (must show no route), consultation hours, timetable, teaching,
research, email, escalations (phone number, fees, unknown person, washroom with no verified data, asking for a
person), a two-person clarification, pronoun follow-ups and a greeting. Expected facts are taken from rag-core's
checked test data (`rag-core/data/eval/qa_test.jsonl`). Each turn says what must be true:

| key | check |
|---|---|
| `outcome` | voice-memory's outcome (`answered`, `clarification`, `escalated`, `handoff`, `social`); a list means any of them |
| `keypoints` | every string appears in the answer (case-insensitive) |
| `route_to` / `route_to_if_answered` | the map shows a route to that room |
| `off_map` / `no_route` | no route is shown |
| `escalate`, `min_options` | escalation flag; at least N buttons |

`python -m kiosk_ui.evaluate` writes `reports/evaluation_report.md`: end-to-end correctness, navigation accuracy,
escalation correctness, response time, results by category, every failed turn, map coverage against the faculty
data, and a copy of rag-core's reports and voice-memory's `voice_metrics.md`. Run `python -m voice_memory.metrics`
and rag-core's evaluations first so their latest numbers are included.

## Not done yet / needs the team

- **Floor map**: drawn from the floor-plan board and the staff room sketch; walk the routes once and set `verified: true` (see above). Resolve the five sketch/sheet room differences with the department.
- **360° photos**: supported; waiting for the photos.
- **Clarification buttons** come from voice-memory's `options`. rag-core's proposed `clarification_options`
  field would make them more reliable (open proposal in `rag-core/docs/interface.md`).
- **voice-memory on Windows**: with `tts: backend: pyttsx3`, the second spoken answer hangs (the Windows voice
  engine called again from a server thread), so `/turn` stops responding and the kiosk shows its "trouble" message
  after 20 s. Reported to Portion 2; until fixed run voice-memory with `tts: backend: none` (the kiosk speaks
  instead) or Piper.
- **Microphone**: the Claude desktop app's browser pane always blocks the microphone; open the kiosk in Chrome or
  Edge. Voice needs voice-memory's `stt` extra (faster-whisper); without it the mic button is greyed out and says
  so. Measured on a development laptop (CPU, `small.en`, int8): about 3-4 s per spoken question, almost all
  speech recognition, vs about 50 ms typed. `base.en` is roughly 3x faster but less accurate with names.
- **Kiosk hardware**: test the microphone, speaker and touch screen on the real kiosk; browsers only allow the
  microphone on `localhost` or HTTPS.
