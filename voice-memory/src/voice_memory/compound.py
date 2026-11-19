"""Two questions in one: "where is Dr Nisha's cabin and is she free on Friday?".

rag-core answers one question at a time, so the receptionist splits the utterance, answers the parts in
order (the second part's "she" is resolved from the first part's answer, exactly like a follow-up) and joins the
answers.

The split is conservative. It only happens where a conjunction or sentence end is followed by something that
*starts a question* ("where", "who", "is", "can", "how do I", "tell me" ...), and both halves must be able to
stand on their own. Everything else stays in one piece, because rag-core copes with it:

- "is she free on Monday and Friday?"            (the second half is just a day)
- "what is her phone number and email?"          (the second half is just a noun)
- "where is Dr Nisha and Dr Deepa?"              (a list of names, not two questions)
"""

from __future__ import annotations

import re

_STARTERS = (
    r"where|who|whom|whose|what|when|which|why|how|is|are|was|were|can|could|may|does|do|did|will|would|should|"
    r"tell me|show me|give me|take me|guide me|i want to|i need to|i would like to|i'd like to|i wanna|"
    r"please tell|please show"
)
_START = re.compile(rf"^\s*(?:{_STARTERS})\b", re.IGNORECASE)
# a conjunction (or a sentence end) followed by the start of another question
_JOINER = re.compile(
    rf"(?:\s*[?;]+\s*(?:(?:and\s+then|and\s+also|and|also|plus|then)\s+)?"
    rf"|\s*,?\s+(?:and\s+then|and\s+also|and|also|plus|then|next)\s+)(?=(?:{_STARTERS})\b)",
    re.IGNORECASE,
)
# "who teaches ML and DSA" / "where is X and who is Y": the first half must be a question or a request itself
_MIN_WORDS = 2


def _clean(part: str) -> str:
    part = part.strip(" ,;")
    if part and part[-1] not in "?.!":
        part += "?"
    return part


def split_questions(text: str) -> list[str]:
    """The questions in ``text``, in the order asked. One element when it isn't a compound question."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return [text]
    pieces = _JOINER.split(text)
    parts = [p for p in (x.strip() for x in pieces) if p and re.search(r"\w", p)]
    if len(parts) < 2:
        return [text]
    # every part must be able to stand alone: it starts a question (or, for the first, contains one)
    if not all(_START.match(p) for p in parts[1:]) or len(parts[0].split()) < _MIN_WORDS:
        return [text]
    if not (_START.match(parts[0]) or re.search(rf"\b(?:{_STARTERS})\b", parts[0], re.IGNORECASE)):
        return [text]
    if any(len(p.split()) < _MIN_WORDS for p in parts):
        return [text]
    return [_clean(p) for p in parts]
