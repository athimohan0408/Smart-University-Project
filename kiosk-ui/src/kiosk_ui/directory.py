"""Read-only view of rag-core's faculty data, for two things the screen adds to a turn.

1. **Routes for people.** voice-memory's ``room_ids`` only carry rooms the question named ("room 874").
   For "where is Dr Nisha Varghese?" the room is in the answer text. A faculty member's 8th-floor office
   becomes a route only when the turn was *answered*, the entity is linked (not ambiguous), and the answer
   itself names that room: the map never shows anything the receptionist didn't say.
2. **Clarification buttons.** "Which one do you mean: Dr A or Dr B?" arrives with no ``options``; the
   names in the answer become buttons, in the order spoken.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_TITLE = r"(?:(Dr|Fr|Prof|Mr|Ms|Mrs)\.?\s+)?"


@dataclass
class FacultyDirectory:
    names: dict[str, str] = field(default_factory=dict)  # canonical id -> full name without title
    rooms: dict[str, str] = field(default_factory=dict)  # canonical id -> 8th-floor office room ("874")
    display: dict[str, str] = field(default_factory=dict)  # canonical id -> "Dr Nisha Varghese"

    @classmethod
    def load(cls, faculty_json: Path) -> FacultyDirectory:
        d = cls()
        if not faculty_json.exists():
            return d
        for fac in json.loads(faculty_json.read_text(encoding="utf-8")).get("faculty", []):
            cid = f"faculty:{fac['id']}"
            d.names[cid] = fac["full_name"]
            d.display[cid] = f"{fac['title']} {fac['full_name']}" if fac.get("title") else fac["full_name"]
            loc = fac.get("location") or {}
            if isinstance(loc, dict) and loc.get("on_8th_floor") and loc.get("room"):
                d.rooms[cid] = str(loc["room"])
        return d

    def rooms_in_answer(self, turn: dict) -> list[str]:
        if turn.get("outcome") != "answered" or turn.get("escalate"):
            return []
        answer = turn.get("answer") or ""
        out = []
        for e in turn.get("entities") or []:
            room = self.rooms.get(e.get("canonical_id") or "")
            if room and re.search(rf"\b{re.escape(room)}\b", answer, re.IGNORECASE):
                rid = f"room:{room.casefold()}"
                if rid not in out:
                    out.append(rid)
        return out

    def clarification_options(self, turn: dict) -> list[str]:
        if turn.get("outcome") != "clarification" or turn.get("options"):
            return list(turn.get("options") or [])
        answer = turn.get("answer") or ""
        found: list[tuple[int, int, str]] = []
        for name in sorted(self.names.values(), key=len, reverse=True):  # longest first: no partial overlaps
            for m in re.finditer(_TITLE + re.escape(name) + r"\b", answer):
                if not any(s <= m.start() < e for s, e, _ in found):
                    found.append((m.start(), m.end(), m.group(0)))
        found.sort()
        return [text for _, _, text in found] if len(found) >= 2 else []
