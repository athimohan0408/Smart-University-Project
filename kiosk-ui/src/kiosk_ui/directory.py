"""Read-only view of rag-core's faculty data, for two things the screen adds to a turn.

1. **Routes for people and places.** voice-memory's ``room_ids`` only carry rooms the question named ("room 874").
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
from datetime import datetime
from pathlib import Path

_TITLE = r"(?:(Dr|Fr|Prof|Mr|Ms|Mrs)\.?\s+)?"
_PLACE_ROOM = re.compile(r"\bis in room (\d{3,4}[A-Z]?)\b")  # rag-core's place sentence; not "office room 871"


@dataclass
class FacultyDirectory:
    names: dict[str, str] = field(default_factory=dict)  # canonical id -> full name without title
    rooms: dict[str, str] = field(default_factory=dict)  # canonical id -> 8th-floor office room ("874")
    display: dict[str, str] = field(default_factory=dict)  # canonical id -> "Dr Nisha Varghese"
    classes: dict[str, list[dict]] = field(default_factory=dict)  # canonical id -> timetable rows (day, start, end)
    consultation: dict[str, list[dict]] = field(default_factory=dict)  # canonical id -> consultation slots

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
            d.classes[cid] = [{"day": t["day"], "start": t["start_time"], "end": t["end_time"],
                               "what": t.get("subject_code") or "a class"}
                              for t in fac.get("timetable") or [] if t.get("day") and t.get("start_time") and t.get("end_time")]
            d.consultation[cid] = [s for s in (fac.get("consultation_hours") or {}).get("slots") or []
                                   if s.get("day") and s.get("start") and s.get("end")]
        return d

    def people_in(self, room: str) -> list[str]:
        """Canonical ids of the faculty whose 8th-floor office is this room ('874'), in name order."""
        key = room.strip().casefold()
        return sorted((cid for cid, r in self.rooms.items() if r.casefold() == key), key=lambda c: self.names[c])

    def status(self, cid: str, now: datetime) -> str | None:
        """What the faculty sheet's timetable says about this person right now, in words, or None when it says
        nothing (no class, no consultation hours now). It is the timetable, not where they actually are."""
        day, hhmm = now.strftime("%A"), now.strftime("%H:%M")
        for s in self.consultation.get(cid, []):
            if s["day"] == day and _hm(s["start"]) <= hhmm < _hm(s["end"]):
                return f"Consultation hours now, until {_hm(s['end'])}"
        for c in self.classes.get(cid, []):
            if c["day"] == day and _hm(c["start"]) <= hhmm < _hm(c["end"]):
                return f"Teaching now ({c['what']}) until {_hm(c['end'])}, so may not be in the cabin"
        return None

    def next_consultation(self, cid: str, now: datetime) -> str | None:
        """The next consultation slot from now within a week: 'Wednesday 11:45-12:45' ('today 14:00-15:00')."""
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        today, hhmm = now.weekday(), now.strftime("%H:%M")
        best: tuple[int, str, dict] | None = None
        for s in self.consultation.get(cid, []):
            if s["day"] not in days:
                continue
            ahead = (days.index(s["day"]) - today) % 7
            if ahead == 0 and _hm(s["start"]) <= hhmm:
                ahead = 7
            key = (ahead, _hm(s["start"]), s)
            if best is None or key[:2] < best[:2]:
                best = key
        if best is None:
            return None
        ahead, _, s = best
        when = "today" if ahead == 0 else "tomorrow" if ahead == 1 else s["day"]
        return f"{when} {_hm(s['start'])}-{_hm(s['end'])}"

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
        # A place from the dataset's Rooms sheet ("where is the MCA lab?", "which room is 5th sem MCA section A?"):
        # rag-core answers "MCA Lab is in room 811". The class teacher's "office room 871" never matches.
        place_q = turn.get("intent") == "find_room" or any(
            e.get("type") == "facility" and e.get("canonical_id") for e in turn.get("entities") or [])
        if place_q:
            for m in _PLACE_ROOM.finditer(answer):
                rid = f"room:{m.group(1).casefold()}"
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


def _hm(value: str) -> str:
    """'9:45' / '09:45' / '9.45' -> '09:45', so times compare as strings."""
    h, _, m = value.strip().replace(".", ":").partition(":")
    return f"{int(h):02d}:{int(m or 0):02d}"
