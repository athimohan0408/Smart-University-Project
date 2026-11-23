"""Where the visitor is starting from: "how do I get to Dr Nisha's office from the lift?".

The kiosk draws every route from itself. A visitor who has just stepped out of a lift or is standing by the
staircase wants directions from *there*. This module finds that phrase in the question and:

1. takes it out of the question (rag-core only needs "how do I get to Dr Nisha's office");
2. remembers it for the rest of the conversation, so the follow-ups ("and Dr Deepa's?", "how do I get there?")
   keep the same start point until the visitor says otherwise ("from here", "from the west lift");
3. reports it in ``TurnResult.origin`` (a map place id), which the kiosk uses to draw and read out the route.

The places and their words come from the kiosk's floor map (``memory.origins_file``): a place is a possible
start when it has a ``near`` walkable node or a ``via`` door. "The lift" matches three lifts, so the visitor
is asked which one; "the north lift" or "room 871" is unambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DIRECTIONS = ("north", "south", "east", "west")
_CUE = (
    r"(?:starting\s+(?:from|at)|coming\s+from|come\s+from|came\s+from|from|"
    r"i\s*(?:am|'m|’m)\s+(?:currently\s+)?(?:standing\s+)?(?:at|near|by|next\s+to|beside|outside|in\s+front\s+of)|"
    r"we\s*(?:are|'re|’re)\s+(?:at|near|by|next\s+to)|currently\s+(?:at|near)|standing\s+(?:at|near|by|next\s+to))"
)
_KIOSK_WORDS = r"(?:here|(?:this|the)\s+(?:kiosk|screen|robot)|the\s+(?:front\s+)?(?:reception|desk)|reception)"
_FILLER = re.compile(r"\b(?:and|then|now|ok|okay|so|if|what about|how about|well|yes|actually|no|please|then|also)\b", re.IGNORECASE)


@dataclass(frozen=True)
class OriginPlace:
    id: str  # map place id, e.g. "facility:lift-north"
    name: str  # how to say it: "the north lift", "room 874"
    aliases: tuple[str, ...]  # lower-case phrases that name the kind of place ("lift", "elevator")
    direction: str | None  # north / south / east / west when the id says so


@dataclass(frozen=True)
class OriginMatch:
    start: int
    end: int
    mention: str  # the place words as said, without the cue: "the lift"
    candidates: tuple[OriginPlace, ...]  # one when clear, several when ambiguous, none for the kiosk itself
    kiosk: bool = False  # "from here": back to the default start


@dataclass
class OriginResolver:
    places: dict[str, OriginPlace] = field(default_factory=dict)

    @classmethod
    def load(cls, map_file: Path | None) -> OriginResolver:
        resolver = cls()
        if map_file is None or not Path(map_file).exists():
            return resolver
        raw = yaml.safe_load(Path(map_file).read_text(encoding="utf-8")) or {}
        for place in raw.get("places", []):
            if not (place.get("near") or place.get("via")):
                continue
            pid = place["id"]
            if place.get("kind") == "room":
                number = pid.split(":", 1)[1]
                resolver.places[pid] = OriginPlace(pid, f"room {number}", (number.casefold(),), None)
                continue
            direction = next((d for d in _DIRECTIONS if pid.endswith(f"-{d}")), None)
            aliases = tuple(a.casefold() for a in place.get("aliases") or [place["label"]])
            noun = (place.get("aliases") or [place["label"]])[0]
            name = f"the {direction} {noun}" if direction else f"the {noun}"
            resolver.places[pid] = OriginPlace(pid, name, aliases, direction)
        return resolver

    def __bool__(self) -> bool:
        return bool(self.places)

    def name_of(self, place_id: str) -> str:
        place = self.places.get(place_id)
        return place.name if place else place_id.split(":")[-1].replace("-", " ")

    # ----------------------------------------------------------------------- finding it in a question

    def _pattern(self) -> re.Pattern[str]:
        kinds = sorted({a for p in self.places.values() if p.id.split(":")[0] != "room" for a in p.aliases},
                       key=len, reverse=True)
        kind_re = "|".join(re.escape(k) for k in kinds) or r"(?!x)x"
        direction = r"(?:(?P<dir>north|south|east|west)(?:ern)?\s+(?:side\s+)?)?"
        return re.compile(
            rf"(?P<cue>\b{_CUE})\s+(?P<what>"
            rf"(?P<kiosk>{_KIOSK_WORDS})\b"
            rf"|(?:the\s+)?{direction}(?P<kind>{kind_re})s?\b"
            rf"|(?:(?:the\s+)?room\s+)?(?P<room>\d{{3}}[a-z]?)\b)",
            re.IGNORECASE,
        )

    def find(self, text: str) -> OriginMatch | None:
        if not self.places:
            return None
        if not hasattr(self, "_re"):
            self._re = self._pattern()
        for m in self._re.finditer(text):
            what = m["what"]
            if m["kiosk"]:
                return OriginMatch(m.start(), m.end(), what, (), kiosk=True)
            if m["room"]:
                place = self.places.get(f"room:{m['room'].casefold()}")
                if place is None:
                    continue  # "from 1030": a time, not a room
                return OriginMatch(m.start(), m.end(), what, (place,))
            kind, direction = m["kind"].casefold(), (m["dir"] or "").casefold() or None
            found = [p for p in self.places.values() if kind in p.aliases and (direction is None or p.direction == direction)]
            if found:
                return OriginMatch(m.start(), m.end(), what, tuple(found))
        return None

    @staticmethod
    def strip(text: str, match: OriginMatch) -> str:
        """The question without the start-point phrase."""
        out = f"{text[: match.start]} {text[match.end:]}"
        out = re.sub(r"\s+([?.!,;])", r"\1", out)
        out = re.sub(r"^[\s,;]+|[\s,;]+$", "", out)
        out = re.sub(r"\s{2,}", " ", out)
        out = re.sub(r"([?.!])\s*[,;]\s*", r"\1 ", out)
        return out.strip()

    @staticmethod
    def only_a_start(rest: str) -> bool:
        """After stripping, is nothing left but filler? ("and from the west lift?")"""
        return not re.search(r"\w", _FILLER.sub("", rest))
