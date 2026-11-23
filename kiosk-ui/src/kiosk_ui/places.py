"""Place cards: what a place on the 8th floor is, who sits there and where it is.

Sources, all read-only here:
- ``data/places_info.yaml`` (this module's own file): what a place is, hours, notes, a classroom's class teacher.
- the admin dashboard (AdminStore): the published timetable's Rooms sheet (classroom names and class teachers),
  the office's own edits, uploaded door photos, closed rooms and faculty status ("On leave until Fri 20 Nov"),
  each taking precedence over the files.
- the floor map: the place's label, kind, photo and where on the floor it is ("on the east side of the floor").
- rag-core's faculty sheet (``faculty.json``, via FacultyDirectory): who has their office in a cabin, and what
  the timetable says about them right now ("Consultation hours now, until 12:45").

Used by ``GET /api/places`` (the list for "Explore the floor" and the phone page) and ``GET /api/place/{id}``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict

from kiosk_ui.directory import FacultyDirectory
from kiosk_ui.navigation import FloorMap, normalise_place_id

if TYPE_CHECKING:
    from kiosk_ui.admin_store import AdminStore


class PlaceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str | None = None
    about: str | None = None
    hours: str | None = None
    notes: str | None = None
    class_teacher: str | None = None
    source: str | None = None


class Person(BaseModel):
    name: str
    now: str | None = None  # "Consultation hours now, until 12:45" (from the timetable), or None
    from_office: bool = False  # `now` was set by the department office (on leave, in a meeting ...)
    next_consultation: str | None = None  # "Wednesday 11:45-12:45"


class PlaceSummary(BaseModel):
    id: str
    label: str
    kind: str
    name: str  # what to call it in a list: "Room 874", "MCA Lab (811)", "Gents washroom"
    routable: bool
    people: list[str] = []  # display names, for searching "Nisha" in the list
    group: str  # rooms, cabins, facilities, areas: how the list groups it


class PlaceCard(PlaceSummary):
    about: str | None = None
    hours: str | None = None
    notes: str | None = None
    class_teacher: str | None = None
    source: str | None = None
    where: str | None = None  # "inside the Faculty Staff Room · C816", "on the east side of the floor"
    photo: str | None = None
    closed: str | None = None  # "Closed until Fri 20 Nov: exam in progress" (set by the office)
    staff: list[Person] = []


def load_info(path: Path) -> dict[str, PlaceInfo]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key.casefold(): PlaceInfo.model_validate(value or {}) for key, value in raw.items()}


class PlaceBook:
    def __init__(self, fmap: FloorMap, info: dict[str, PlaceInfo], directory: FacultyDirectory,
                 store: AdminStore | None = None) -> None:
        self.fmap, self.files, self.directory, self.store = fmap, info, directory, store

    @classmethod
    def load(cls, fmap: FloorMap, info_file: Path, directory: FacultyDirectory,
             store: AdminStore | None = None) -> PlaceBook:
        return cls(fmap, load_info(info_file), directory, store)

    def _info(self, place_id: str) -> PlaceInfo:
        """The file's info, then the published timetable's Rooms sheet, then the office's edits, field by field."""
        key = place_id.casefold()
        merged = self.files.get(key, PlaceInfo()).model_dump()
        if self.store is not None:
            for layer in (self.store.state.rooms_sheet.get(key), self.store.state.place_info.get(key)):
                if layer is not None:
                    merged.update({k: v for k, v in layer.model_dump().items() if v})
        return PlaceInfo(**merged)

    def photo(self, place) -> str | None:
        return (self.store.state.photos.get(place.id) if self.store else None) or place.photo

    def person_now(self, cid: str, now: datetime) -> tuple[str | None, bool]:
        """What to say about someone now: the office's status first, else what the timetable says."""
        if self.store is not None and (s := self.store.status_now(cid, now)):
            return s, True
        return self.directory.status(cid, now), False

    def _summary(self, place) -> dict:
        info = self._info(place.id)
        number = place.label.partition("·")[0].strip()
        if place.kind == "room":
            in_staff_room = (self.fmap.whereabouts(place.id) or "").startswith("inside")
            group = "cabins" if in_staff_room else "rooms"
            name = f"{info.name} ({number})" if info.name else f"Room {number}"
            people = [] if not in_staff_room else [self.directory.display[c] for c in self.directory.people_in(number)]
            if not in_staff_room and info.name is None and "·" in place.label:
                name = f"{place.label.partition('·')[2].strip()} ({number})"
        else:
            group = "areas" if place.kind in ("area", "hall") else "facilities"
            name = info.name or place.label
            people = []
        return {"id": place.id, "label": place.label, "kind": place.kind, "name": name,
                "routable": place.routable, "people": people, "group": group}

    def summaries(self) -> list[PlaceSummary]:
        return [PlaceSummary(**self._summary(p)) for p in self.fmap.places]

    def card(self, place_id: str, now: datetime | None = None) -> PlaceCard | None:
        place = self.fmap.place(normalise_place_id(place_id))
        if place is None:
            return None
        now = now or datetime.now()
        info = self._info(place.id)
        base = self._summary(place)
        staff = []
        if base["group"] == "cabins":
            number = place.label.partition("·")[0].strip()
            for c in self.directory.people_in(number):
                said, office = self.person_now(c, now)
                staff.append(Person(name=self.directory.display[c], now=said, from_office=office,
                                    next_consultation=self.directory.next_consultation(c, now)))
        closed = self.store.closure_now(place.id, now.date()) if self.store else None
        closed_text = None
        if closed is not None:
            until = f" until {closed.until:%a %d %b}".replace(" 0", " ") if closed.until else ""
            closed_text = f"Closed{until}: {closed.reason}"
        return PlaceCard(**base, about=info.about, hours=info.hours, notes=info.notes,
                         class_teacher=info.class_teacher, source=info.source,
                         where=self.fmap.whereabouts(place.id), photo=self.photo(place), closed=closed_text,
                         staff=staff)
