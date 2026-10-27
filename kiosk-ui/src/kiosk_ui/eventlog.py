"""The kiosk's own event log: ``logs/ui_events.jsonl``, one JSON line per screen event.

rag-core logs what it answered and voice-memory logs each turn; this log adds what only the screen
sees: the full round trip the visitor waited for, whether a route was shown, which clarification
button was tapped, the "talk to the receptionist" button, and errors reaching voice-memory.

Like the other logs it records no IP address, device id, audio, face or name field. The question
text is what the visitor asked (needed for "most asked" and "unanswered" on the dashboard).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

SCHEMA_VERSION = 1
EventType = Literal["turn", "option_chosen", "receptionist_button", "session_reset", "error"]


class EventLog:
    def __init__(self, path: Path, kiosk_id: str) -> None:
        self.path = path
        self.kiosk_id = kiosk_id
        self._lock = threading.Lock()

    def append(self, event: EventType, session_id: str, **fields) -> dict:
        record = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kiosk_id": self.kiosk_id,
            "session_id": session_id,
            "event": event,
            **fields,
        }
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never break the screen
        return record


def read_jsonl(path: Path) -> list[dict]:
    """Every parseable line of a JSONL log (a half-written last line is skipped)."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
