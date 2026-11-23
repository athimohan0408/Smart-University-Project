# kiosk-ui: interface, navigation and analytics (Portion 3)

The face and the dashboard of the 8th-floor receptionist:

- **Kiosk screen** (`/`): greeting, the visitor's question (typed, tapped or spoken), the answer, clarification
  buttons, the "Talk to the receptionist" button, and a details panel with the floor map.
- **Floor map + navigation**: the 8th floor with a highlighted route from reception to the room in the answer,
  step-by-step directions and distance, and (when photos are added) a 360° view along the way.
- **Analytics dashboard** (`/dashboard`): query counts, most-asked and unanswered questions, categories,
  escalation reasons, average response time and questions by hour.
- **Logging** behind the dashboard: the kiosk's own event log, read together with rag-core's and voice-memory's logs.
- **Evaluation**: the 33-turn end-to-end test set and the final report, which compiles the whole team's numbers.

```text
browser (kiosk screen, dashboard)
   |  only talks to this server
   v
kiosk-ui :8002  --POST /turn, /turn/audio, /receptionist, /session/reset-->  voice-memory :8001  -->  rag-core :8000
   |  room_ids -> navigation (data/floor_map.yaml) -> route + directions
   |  origin (from the lift) -> routes start there; "Where am I?" -> answered from the map
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
888 and 885, and between 883 and 882 (beside the kiosk). There is no door at the east end. 886, 887 and 889 are not
in the sketch and are not drawn. Like the board, the map is **not to scale** (the staff room is drawn taller so the
cabins are readable), so distances are approximate; the kiosk says "Not to scale" above the map.

Routes start at the kiosk, enter the staff room through the nearest of the three passages and run along its
middle aisle ("From the kiosk, walk about 2 m towards the Faculty Staff Room entrance C816, between rooms 883 and 882. Turn
left and walk about 8 m to the middle aisle. Turn left and walk about 7 m. Room 874 is on your right."). A first step names a landmark
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
file). The kiosk shows a landmark's photo next to the step that names it ("... the Faculty Staff Room entrance
C816 ...") and the room's door next to the last step ("Look for this door"); tap to enlarge. Rooms with a photo can
also be tapped on the map. No photo yet: 875, 880, 886, 887, 889, the C817 doorway's place on the map (see below).

To add one: put a JPG in `data/photos/` (landscape or portrait, no people, ideally under 400 KB) and add
`photo: <file>` to the node or place in `data/floor_map.yaml`.

**23 November photos:** doors of all eight classrooms 808-815 (read from their C8xx tags), the south Gents and
Ladies washrooms (beside the Placement Office and the MCA Lab), the west lift and water dispenser, the south lift
(and the view stepping out of it), the CCD café, the open hall, and five **corridor views**. Faces were blurred
(pixelated, not reversible) before resizing; no phone metadata is kept. A corridor view sits on a node with the way
the camera faced (`view:` + `view_facing:`) and is shown with a step that walks that way past the node, so a visitor
sees what is ahead of them and never a view of the corridor behind them. Still without a photo: cabins 875, 880,
886, 887, 889; the north lift; the corridor views K, L, H, G and P from that set (where each was taken and which way
it faces isn't known yet).

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
- **Facilities and labs, from the map**: "where is the lift / the staircase / drinking water / the CCD café / the Ladies
  washroom / the MCA lab?" has no answer in rag-core's knowledge base until the campus sheet is filled, so it escalates
  with `not_in_kb`. When the map has the place (matched by its `aliases`), the kiosk answers where it is and draws the
  route: "The nearest lift is the north lift, about 8 m from the kiosk, on the north-east side of the floor. ... This
  floor also has the west lift and the south lift." The nearest is measured from where the visitor is (the kiosk, a
  start they named, or a sign's QR code). "Where is the washroom?" asks "Gents or Ladies?" (buttons) rather than
  guessing. A place whose entrance isn't on the drawings (the faculty washroom, the north Gents) is described
  ("on the north-west side of the floor") with no route. It only says *where*: never whether it is open, working or
  free, so "is there wifi?" or "when does the lift close?" still go to the receptionist.
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

## Exploring the floor, lifts, phones and 3D

- **Routes from the lifts and staircases.** `starts:` in `data/floor_map.yaml` adds five more places a route can
  begin: `lift-north`, `lift-south`, `lift-west`, `stairs-north`, `stairs-south`. Each knows which way a visitor
  faces stepping out, so the first step says which way to turn ("Step out of the north lift, turn left and walk about
  6 m."). `GET /api/route/{place}?from=lift-north`; the kiosk is still the default (`from=kiosk`).
- **Lifts, stairs, washrooms, water and the CCD café are destinations** (they have a `door`), so "the nearest ..."
  works: `GET /api/nearest/{lift|stairs|way-down|gents|ladies|water}?from=...`. The faculty washroom has no route.
- **Place cards** (`GET /api/place/{id}`, list at `GET /api/places`): tap any room on the map, or open **Explore the
  floor** (search by room, person or place; one-tap nearest washroom / lift / water). A card says what the place is
  (`data/places_info.yaml`, this module's own file: classrooms 808-815 from the dataset's Rooms sheet, with class
  teachers), where it is, and for a cabin who sits there (rag-core's faculty sheet) with what the timetable says
  right now ("Consultation hours now, until 12:45", "Teaching now (ML) until 11:45") and the next consultation
  hours. The timetable says where someone should be, not where they are, and the card says so.
- **Phone walkthrough** `/go?from=lift-north&to=room:874`: the same directions on a phone, one step at a time.
  **`/qr`** prints a QR sticker for each lift and staircase. For phones to reach the kiosk, set `kiosk.public_url`
  (the kiosk's address on the campus Wi-Fi) and `api.host: 0.0.0.0` in `config/settings.yaml`; then place cards
  also show a "scan to take the directions on your phone" QR. QR codes are drawn with qrcode-generator (MIT, from
  jsDelivr, or `static/vendor/qrcode.js` for an offline kiosk).
- **3D view** (on by default, "2D map" switches back, remembered per kiosk): the floor raised into 3D with Three.js
  0.160 (MIT, from jsDelivr via the import map in `kiosk.html`), the route as a glowing tube, and a camera that flies
  along it to the highlighted room. Drag to turn, pinch to zoom, tap a room for its card. Without WebGL or without
  network for Three.js the button says "3D unavailable" and the 2D map stays.
- **Easy view** (masthead): larger text, darker colours, bigger buttons, slower speech and walkthrough. It resets
  for the next visitor. All routes on this floor are step-free; "Nearest lift" avoids stairs between floors.
- **Was this helpful?** Thumbs up / down under an answer: a `feedback` event in `logs/ui_events.jsonl`
  (`turn_id`, `helpful`), shown on the dashboard as "Rated helpful".

## Admin dashboard (`/admin`)

For the department office, so the receptionist stays right when things change, without editing files:

- **Timetable each semester.** Download the timetable in use, change it in Excel (the *8th Floor Faculty* sheet:
  rooms, consultation hours, a column per day; the *Rooms* sheet: classrooms and class teachers), upload it.
  The dashboard checks it (missing columns, a person twice, rows without names: these stop it; missing rooms or
  hours: warnings) and shows **what will change** (people added / removed, every changed room, hours and day)
  before anything happens. **Publish** saves it as rag-core's `data/raw/8th_floor_faculty_with_subjects.xlsx` and
  calls rag-core's own `POST /reload-kb` (no restart, no rag-core code changed); the kiosk then re-reads
  `faculty.json` and the Rooms sheet's class teachers. If rag-core can't load it, the old file is put back.
  Every version is kept (the one in use before the first publish too): **Use this version** goes back.
- **Faculty status:** on leave / out of office / in a meeting / in the cabin, until a date and time, with a note.
  Shown on place cards and under answers about that person ("Dr X: On leave until Fri 20 Nov (from the department
  office)") instead of what the timetable says, until it ends.
- **Announcements** with start and end dates: a notice bar on every kiosk screen.
- **Rooms and places:** edit what a place is, hours, class teacher, a note; **mark a room closed** (with a reason,
  until a date: shown on its card and under answers that route there); **upload a door photo** (shown on the card
  and as the last step's photo).
- **Accounts:** `admin` (the office: everything) and `faculty` (their own status only). The first admin account is
  created on the kiosk computer itself (`/admin` on 127.0.0.1). Passwords are stored only as salted PBKDF2 hashes;
  five wrong passwords lock an account for five minutes. Locked out:
  `python -m kiosk_ui.admin_api reset-password <username>` (or `add-admin`, `list`).
- **Change log:** who changed what, and when.

Everything is kept in `data/admin/` (settings: `admin:`), which is not in git: back it up with the kiosk.

## Start points, "Where am I?" and waking on approach

**Routes from where the visitor is.** Routes normally start at the kiosk. When voice-memory reports an `origin` for
the turn ("how do I get to Dr Nisha's office from the north lift?", and every follow-up until the visitor changes
it), or the page was opened from a sign's QR code (below), the route starts there:

- A place can be a start when the map gives it a `near` node (a lift, staircase, washroom, water point, CCD, in front
  of it) or a door (`via`: any room, the lounge). `aliases` are the words visitors use; the first is how it is spoken.
  Cabins and rooms start at their door. A start the map doesn't know is ignored and the route starts at the kiosk.
- At the five lifts and staircases in `starts:` the way a visitor faces stepping out is known, so the first step says
  which way to turn ("Step out of the north lift, turn left and walk about 6 m."). Anywhere else (a room, a water
  point, a washroom) nobody knows which way the visitor faces, so the first step names a **compass direction**
  ("From room 871, head south for about 2 m ..."); the rest is as before. The map
  moves the "You are here" marker to the start ("You are at the north lift") and the walkthrough starts there.
- Start and destination the same place: "You are already at room 874."

**"Where am I?"** ("where are we", "which floor is this", "I'm lost") is answered from the map, whatever voice-memory
made of it: "You are at the kiosk, in Central Block, Eighth Floor. Close by: the north staircase, about 2 m south; room
883, about 4 m north; the north lift, about 8 m east." It names the kiosk, or the place the visitor said they are
at (`answered_by: kiosk_position`), marks it on the map, and never guesses a position. Distances are rough: the
drawings are not to scale.

**Signs with a QR code.** Open `http://<kiosk-address>:8002/?at=facility:lift-north` (any place id from the map) on a
phone: that page treats the visitor as standing there (map marker, "Where am I?", every route), and does not try to
wake like a kiosk. `GET /api/position?at=<place id>` returns the same description. (A phone needs the kiosk's
network address; the microphone and camera only work over HTTPS or on the kiosk itself.)

**Wake on approach** (`kiosk.wake` in `config/settings.yaml`, `static/wake.js`). While nobody is at the kiosk the
welcome screen dims; when someone walks up it brightens, the robot waves and greets (spoken if the browser allows
sound, otherwise "Touch the screen to hear me"). Someone is "there" when, best signal first:

1. the browser's `FaceDetector` API sees a face at least `min_face_ratio` of the picture wide (the bigger, the
   closer: a passer-by down the corridor doesn't wake it), or
2. without that API, `motion_threshold` of the picture changes between two samples,

for at least `hold_ms`. The same person standing still is not greeted again (`absent_s`, and 30 s between greetings);
a person who leaves and a new one who arrives is a new approach. Touch, mouse and keyboard always count, so with no
webcam, no permission, or `camera: false` it still dims and wakes. **Privacy**: pictures are compared in the page
on an 80x60 grey canvas and thrown away; nothing is recorded, stored or sent. A "Watching for visitors" badge shows
in the header while the camera is on, with that explanation as its tooltip. Set `wake.enabled: false` to switch it all off.
The numbers depend on the camera and where it points: tune them on the real kiosk.

Tests: `tests/test_origin_and_position.py` (routes from every start the map offers, "Where am I?", QR hint, the
event log keeping request text out) and `tests/js/wake_test.js` (the wake logic, run by pytest when `node` exists).
The wake logic was also run in headless Chrome with a fake camera feed (camera on, presence detected, greeting
triggered, no script errors); that needs a real camera and kiosk screen to tune, which has not been done.

## Evaluation and the final report

[`evaluation/test_set.jsonl`](evaluation/test_set.jsonl) is 29 visitor scenarios, 33 turns: rooms on this floor
(with the expected route), people on other floors (must show no route), consultation hours, timetable, teaching,
research, email, escalations (phone number, fees, unknown person, wifi with no verified data, asking for a
person), facilities answered from the map (lift, washroom, MCA lab), a two-person clarification, pronoun follow-ups and a greeting. Expected facts are taken from rag-core's
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

- **Wake on approach** needs tuning on the real camera and screen (thresholds, where the camera points); the logic
  is tested, but a real passer-by has not been.
- **Start points**: the faculty washroom and the north gents, whose entrances aren't on the drawings, have no `near`
  node yet, so "from the faculty washroom" is not understood. Add `near:` when the entrances are known.
- **Floor map**: drawn from the floor-plan board and the staff room sketch; walk the routes once and set `verified: true` (see above). Resolve the five sketch/sheet room differences with the department.
- **360° photos**: supported; waiting for the photos.
- **Lift doors**: the map assumes the north and south lifts open onto the hall walkway and the west lift onto the
  west corridor, and reads washroom door sides off the board. Walk them once and fix `starts:` / `door:` if needed.
- **Phones**: set `kiosk.public_url` before printing the `/qr` stickers (the page warns while it points at 127.0.0.1).
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
