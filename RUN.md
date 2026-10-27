# How to run the whole project (Windows, macOS, Linux)

You need **Python 3.11 or newer** and internet the first time (it downloads PyTorch and two small AI models).
Open this folder in your IDE (Antigravity, VS Code, ...) and use its terminal.

## 1. One-time setup (about 5-10 minutes)

**Windows (PowerShell)**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -e ".\rag-core[dev]"
pip install -e ".\voice-memory[dev]"
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e "./rag-core[dev]"
pip install -e "./voice-memory[dev]"
```

Then build rag-core's knowledge base and intent model (the first run downloads the ~130 MB embedding model):
```bash
cd rag-core
just index            # faculty sheet + campus sheet -> facts -> search index
just train-intent     # about a minute
just test             # rag-core tests: all should pass
cd ..
```
(No `just`? Use `python -m rag_core.retrieval.build_index` after `python -m rag_core.kb.ingest_faculty`,
then `python -m rag_core.nlu.train_intent` and `python -m pytest`.)

## 2. Try it (two terminals, both with the venv activated)

Terminal 1, the brain (rag-core):
```bash
cd rag-core
just serve            # http://127.0.0.1:8000
```

Terminal 2, the receptionist (voice-memory), typed chat:
```bash
cd voice-memory
python -m voice_memory
```
Try a conversation with follow-ups:
```
where is Dr Deepa V Jose's cabin?
is she free on Friday?
how do I get to her office?
and Dr Binayak Dutta?
where is Dr George?
the first one
what is Dr Nisha Varghese's phone number?
yes
```
Type `new` for a new visitor. Afterwards, see the metrics: `python -m voice_memory.metrics`.

## 3. Tests for Portion 2

```bash
cd voice-memory
python -m pytest      # 60 tests, offline
```

## 4. Voice (optional)

```bash
pip install -e "./voice-memory[stt,tts-basic,audio]"
```
In `voice-memory/config/settings.yaml` set `tts: backend: pyttsx3` (uses your computer's own voice, no download),
then `cd voice-memory` and `python -m voice_memory --voice`. Speak after "Listening". The first run downloads the
Whisper speech model (~480 MB for `small.en`; set `stt: model: base.en` for a faster, smaller one).
For the better offline Piper voice instead, see `voice-memory/README.md`, "Speech models".

## 5. HTTP API for the kiosk UI (optional)

With rag-core serving: `cd voice-memory` and `python -m voice_memory.api`, then open
http://127.0.0.1:8001/docs to try `POST /turn` in the browser.

## 6. Kiosk screen, map and dashboard (Portion 3)

One-time: `pip install -e ".\kiosk-ui[dev]"` (Windows) or `pip install -e "./kiosk-ui[dev]"`.

Three terminals, venv active in each: `cd rag-core; just serve`, `cd voice-memory; python -m voice_memory.api`,
`cd kiosk-ui; python -m kiosk_ui`. Then open http://127.0.0.1:8002/ (kiosk) and http://127.0.0.1:8002/dashboard.
If voice-memory won't start because a speech package is missing, set `stt: backend: text` and `tts: backend: none`
in its settings (or a copy named by `VM_SETTINGS_PATH`): the kiosk then speaks answers with the browser's own voice.

Tests: `cd kiosk-ui; python -m pytest` (offline). Final evaluation report, with all three running:
`cd kiosk-ui; python -m kiosk_ui.evaluate` -> `kiosk-ui/reports/evaluation_report.md`.

## Notes

- Classrooms, labs, facilities and events are read from `rag-core/data/raw/campus_info.xlsx`. It is an empty
  template until the department fills it in, so those questions escalate to the receptionist for now (by design).
  After filling it: `cd rag-core`, `just campus-check`, `just index` (no retraining).
- Without an `ANTHROPIC_API_KEY` in `rag-core/.env`, answers come from templates; that is expected and works fine.
- After the first run, put `HF_HUB_OFFLINE=1` in `rag-core/.env` so start-up is faster.
