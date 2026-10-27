"""Text shown on screen -> text a TTS voice reads clearly.

rag-core's answers are written for the screen ("Dr Nisha Varghese's office is room 874",
"nisha.varghese@christuniversity.in", "2 MSAIM ML"). Read as-is, voices say "doctor" fine but
mangle emails, run codes together as words ("msaim") and read 874 as "eight hundred seventy-four".
Only the spoken copy changes; the screen keeps the original.
"""

from __future__ import annotations

import re

_TITLES = [(re.compile(r"\bDr\.?(?=\s)"), "Doctor"), (re.compile(r"\bFr\.?(?=\s)"), "Father"),
           (re.compile(r"\bProf\.?(?=\s)"), "Professor")]
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
_ROOM = re.compile(r"\b(rooms?|cabin|office)\s+(\d{2,4})([A-Za-z]?)\b", re.I)
_CODE = re.compile(r"\b(?=[A-Z0-9]*[A-Z])[A-Z0-9]{2,6}\b")
_AMPM = re.compile(r"\b(\d{1,2}(?::\d{2})?)\s*(am|pm)\b", re.I)
# Upper-case words a voice already says well: leave them alone.
_SAY_AS_WORD = {"OK", "AI"}


def _spell_email(m: re.Match[str]) -> str:
    def spoken(part: str) -> str:
        return part.replace(".", " dot ").replace("_", " underscore ").replace("-", " dash ")

    return f"{spoken(m[1])} at {spoken(m[2])}".replace("  ", " ")


def speakable(text: str, *, rooms_as_digits: bool = True, spell_codes: bool = True) -> str:
    out = _EMAIL.sub(_spell_email, text)
    for pattern, word in _TITLES:
        out = pattern.sub(word, out)
    if rooms_as_digits:
        out = _ROOM.sub(lambda m: f"{m[1]} {' '.join(m[2])}{(' ' + m[3].upper()) if m[3] else ''}", out)
    out = _AMPM.sub(lambda m: f"{m[1]} {m[2][0].upper()} M", out)
    if spell_codes:
        out = _CODE.sub(lambda m: m[0] if m[0] in _SAY_AS_WORD or m[0].isdigit() else " ".join(m[0]), out)
    out = out.replace(";", ",")
    return re.sub(r"\s+", " ", out).strip()
