"""Campus places (classrooms, labs, facilities) and events: the KB beyond the faculty sheet.

Source: ``paths.campus_xlsx`` (default ``data/raw/campus_info.xlsx``), filled in by the department.
Two data sheets, ``Places`` and ``Events``, plus a ``README`` sheet that explains the columns.
Write an empty template with ``python -m rag_core.kb.campus --write-template <path>``.

Golden rule, as for faculty: only verified data becomes a fact. A row without ``verified_by`` is
skipped and reported, never half-loaded. Nothing is completed from a pattern: a missing room, hours
or time stays missing, and the fact says so.

Updating needs no retraining. Edit the sheet, then ``just index`` or ``POST /reload-kb``. Only the
facts, the search index and the gazetteer are rebuilt; the intent classifier never sees KB data.

Ids (pure functions of the data, so rebuilds give the same ids):
- place fact and entity: ``facility:<kind>:<slug>``; every place of a kind is also reachable through
  the kind-level entity ``facility:<kind>`` ("where is the washroom" -> every washroom);
- event fact and entity: ``event:<yyyy-mm-dd>:<slug>``; kind-level ``event:<kind>``, and the generic
  ``event:event`` ("any events today?") reaches every event.
Kinds must be keys of ``facilities`` / ``events`` in ``data/kb/entity_lexicon.yaml``, so the
extractor recognises the words visitors use for them.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from rag_core.kb.facts import Fact, FactCategory
from rag_core.kb.parsing import slugify

PLACES_SHEET = "Places"
EVENTS_SHEET = "Events"
README_SHEET = "README"

PLACE_COLUMNS = {
    "Name": "What visitors call it, e.g. 'Ladies washroom', 'AI and ML Lab 1', 'MSc AIML classroom'. Required.",
    "Kind": "One kind from the allowed list below (washroom, classroom, lab, ...). Required.",
    "Aliases": "Other names visitors use, separated by ';' (e.g. 'AIML lab; lab 1'). Optional.",
    "Block": "e.g. 'Central Block'. Required.",
    "Floor": "Written like the faculty sheet, e.g. 'Eighth Floor'. Required.",
    "Room": "Room number or label, e.g. '806'. Leave empty if it has none.",
    "Directions": "One short sentence from the kiosk, e.g. 'Turn left from the lift; it is the second door on the right.'",
    "Hours": "Opening hours as 24-hour HH:MM-HH:MM, several separated by ';' (e.g. '08:00-17:00'). Empty = not listed.",
    "Programmes": "Programme codes that use it, as in the timetable (e.g. 'MSAIM; MCA'). Optional.",
    "Notes": "Anything visitors should know (e.g. 'Wheelchair accessible'). Spoken as written.",
    "Verified By": "Who in the department confirmed this row. Rows without it are NOT loaded.",
    "Verified On": "Date of the confirmation (YYYY-MM-DD).",
}
EVENT_COLUMNS = {
    "Title": "Event name as announced. Required.",
    "Kind": "One kind from the allowed list below (seminar, workshop, ...). Required.",
    "Date": "YYYY-MM-DD (or an Excel date). Required.",
    "Start": "24-hour HH:MM. Empty = not listed.",
    "End": "24-hour HH:MM. Empty = not listed.",
    "Venue": "Where it happens. Use the exact Name of a row in Places to link the two.",
    "Organiser": "Department, club or person organising it. Optional.",
    "Audience": "Who it is for (e.g. 'MSc AIML students', 'open to all'). Optional.",
    "Description": "One sentence, spoken as written. Optional.",
    "Verified By": "Who in the department confirmed this row. Rows without it are NOT loaded.",
    "Verified On": "Date of the confirmation (YYYY-MM-DD).",
}

_HHMM = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
_RANGE = re.compile(r"^\s*(\S+)\s*(?:-|to)\s*(\S+)\s*$", re.IGNORECASE)


# --------------------------------------------------------------------------- models


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(description="Fact id and canonical id: facility:<kind>:<slug>.")
    name: str
    kind: str
    aliases: list[str] = Field(default_factory=list)
    block: str
    floor: str
    room: str | None = None
    directions: str | None = None
    hours: list[tuple[str, str]] = Field(default_factory=list, description="(HH:MM, HH:MM) opening ranges.")
    programmes: list[str] = Field(default_factory=list)
    notes: str | None = None
    verified_by: str
    verified_on: str | None = None
    row: int = Field(description="Spreadsheet row, for error messages.")


class CampusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(description="Fact id and canonical id: event:<yyyy-mm-dd>:<slug>.")
    title: str
    kind: str
    date: date
    start: str | None = None
    end: str | None = None
    venue: str | None = None
    place_id: str | None = Field(default=None, description="The Places row named in Venue, if any.")
    organiser: str | None = None
    audience: str | None = None
    description: str | None = None
    verified_by: str
    verified_on: str | None = None
    row: int


class CampusKB(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    places: list[Place] = Field(default_factory=list)
    events: list[CampusEvent] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list, description="Rows not loaded, with the reason.")

    @property
    def is_empty(self) -> bool:
        return not (self.places or self.events)


# --------------------------------------------------------------------------- cell helpers


def _cell(row: pd.Series, column: str) -> str | None:
    value = row.get(column)
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # a room typed as a number: 806.0 -> "806"
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(";") if v.strip()]


def parse_hhmm(value: object) -> str | None:
    """'9:00', '09.30', datetime.time(14, 0) -> 'HH:MM'. Anything else (am/pm, '2-4') -> None: never guessed."""
    if isinstance(value, (time, datetime)):
        return f"{value.hour:02d}:{value.minute:02d}"
    match = _HHMM.match(str(value).strip()) if value is not None else None
    return f"{int(match[1]):02d}:{match[2]}" if match else None


def parse_hours(value: str | None) -> list[tuple[str, str]] | None:
    """'08:00-12:45; 13:45-17:00' -> ranges. None when any part is unreadable."""
    out = []
    for part in _list(value):
        match = _RANGE.match(part)
        start, end = (parse_hhmm(match[1]), parse_hhmm(match[2])) if match else (None, None)
        if not (start and end and start < end):
            return None
        out.append((start, end))
    return out


def parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- loading


def _read_sheet(path: Path, sheet: str, columns: Iterable[str]) -> pd.DataFrame:
    try:
        df = pd.read_excel(path, sheet_name=sheet, dtype=object)
    except ValueError:  # sheet missing
        return pd.DataFrame(columns=list(columns))
    df.columns = [str(c).strip() for c in df.columns]
    if missing := [c for c in columns if c not in df.columns]:
        raise ValueError(f"{path.name} sheet {sheet!r} is missing columns: {', '.join(missing)}")
    return df.dropna(how="all")


def load_places(df: pd.DataFrame, kinds: Collection[str], skipped: list[str]) -> list[Place]:
    places: list[Place] = []
    seen: set[str] = set()
    for index, row in df.iterrows():
        line = int(index) + 2  # header is row 1
        name, kind = _cell(row, "Name"), (_cell(row, "Kind") or "").lower().replace(" ", "_")
        where = f"Places row {line}" + (f" ({name})" if name else "")
        if not _cell(row, "Verified By"):
            skipped.append(f"{where}: no 'Verified By', not loaded")
            continue
        problems = [c for c in ("Name", "Kind", "Block", "Floor") if not _cell(row, c)]
        if problems:
            skipped.append(f"{where}: missing {', '.join(problems)}")
            continue
        if kind not in kinds:
            skipped.append(f"{where}: kind {kind!r} is not in the lexicon (allowed: {', '.join(sorted(kinds))})")
            continue
        hours = parse_hours(_cell(row, "Hours"))
        if hours is None:
            skipped.append(f"{where}: Hours {_cell(row, 'Hours')!r} is not HH:MM-HH:MM (24-hour)")
            continue
        pid = f"facility:{kind}:{slugify(name)}"
        if pid in seen:
            skipped.append(f"{where}: duplicate of an earlier {kind} with the same name")
            continue
        seen.add(pid)
        places.append(Place(
            id=pid, name=name, kind=kind, aliases=_list(_cell(row, "Aliases")),
            block=_cell(row, "Block"), floor=_cell(row, "Floor"), room=_cell(row, "Room"),
            directions=_cell(row, "Directions"), hours=hours, programmes=_list(_cell(row, "Programmes")),
            notes=_cell(row, "Notes"), verified_by=_cell(row, "Verified By"),
            verified_on=_cell(row, "Verified On"), row=line,
        ))
    return places


def load_events(df: pd.DataFrame, kinds: Collection[str], places: Sequence[Place], skipped: list[str]) -> list[CampusEvent]:
    by_name = {slugify(p.name): p for p in places}
    events: list[CampusEvent] = []
    seen: set[str] = set()
    for index, row in df.iterrows():
        line = int(index) + 2
        title, kind = _cell(row, "Title"), (_cell(row, "Kind") or "").lower().replace(" ", "_")
        where = f"Events row {line}" + (f" ({title})" if title else "")
        if not _cell(row, "Verified By"):
            skipped.append(f"{where}: no 'Verified By', not loaded")
            continue
        if not title or not kind:
            skipped.append(f"{where}: missing Title or Kind")
            continue
        if kind not in kinds:
            skipped.append(f"{where}: kind {kind!r} is not in the lexicon (allowed: {', '.join(sorted(kinds))})")
            continue
        day = parse_date(row.get("Date"))
        if day is None:
            skipped.append(f"{where}: Date {_cell(row, 'Date')!r} is not YYYY-MM-DD")
            continue
        start = parse_hhmm(row.get("Start")) if _cell(row, "Start") else None
        end = parse_hhmm(row.get("End")) if _cell(row, "End") else None
        if (_cell(row, "Start") and not start) or (_cell(row, "End") and not end) or (start and end and start >= end):
            skipped.append(f"{where}: Start/End must be 24-hour HH:MM with Start before End")
            continue
        eid = f"event:{day.isoformat()}:{slugify(title)}"
        if eid in seen:
            skipped.append(f"{where}: duplicate of an earlier event with the same title and date")
            continue
        seen.add(eid)
        venue = _cell(row, "Venue")
        place = by_name.get(slugify(venue)) if venue else None
        events.append(CampusEvent(
            id=eid, title=title, kind=kind, date=day, start=start, end=end, venue=venue,
            place_id=place.id if place else None, organiser=_cell(row, "Organiser"),
            audience=_cell(row, "Audience"), description=_cell(row, "Description"),
            verified_by=_cell(row, "Verified By"), verified_on=_cell(row, "Verified On"), row=line,
        ))
    return sorted(events, key=lambda e: (e.date, e.start or "", e.id))


def load_campus(path: Path | None, facility_kinds: Collection[str], event_kinds: Collection[str], source: str) -> CampusKB:
    """Read the campus sheet. A missing file is an empty KB (questions about places then escalate)."""
    if path is None or not path.exists():
        return CampusKB(source=source)
    skipped: list[str] = []
    places = load_places(_read_sheet(path, PLACES_SHEET, PLACE_COLUMNS), facility_kinds, skipped)
    events = load_events(_read_sheet(path, EVENTS_SHEET, EVENT_COLUMNS), event_kinds, places, skipped)
    return CampusKB(source=source, places=places, events=events, skipped=skipped)


# --------------------------------------------------------------------------- facts


def _hm(hhmm: str) -> str:
    return f"{int(hhmm[:2])}:{hhmm[3:]}"


def _on_floor(block: str, floor: str, home_block: str, home_floor: str) -> bool:
    return block.casefold() == home_block.casefold() and floor.casefold() == home_floor.casefold()


def place_fact(place: Place, home_block: str, home_floor: str, source: str) -> Fact:
    """Sentence shapes are parsed by generation.templates (change both together)."""
    kind = place.kind.replace("_", " ")
    where = ", ".join(p for p in (f"room {place.room}" if place.room else None, place.block, place.floor) if p)
    here = _on_floor(place.block, place.floor, home_block, home_floor)
    text = f"{place.name} ({kind}) is in {where} ({'on this floor' if here else 'not on this floor'})."
    if place.directions:
        text += f" Directions: {place.directions.rstrip('.')}."
    if place.hours:
        text += f" Open hours, 24-hour time: {'; '.join(f'{_hm(s)}-{_hm(e)}' for s, e in place.hours)}."
    else:
        text += f" Opening hours for {place.name} are not listed."
    if place.programmes:
        text += f" Used by: {', '.join(place.programmes)}."
    if place.notes:
        text += f" Note: {place.notes.rstrip('.')}."
    return Fact(
        fact_id=place.id,
        category=FactCategory.FACILITY,
        text=text,
        room=place.room or "",
        floor=place.floor,
        programme=",".join(place.programmes),
        verified=True,
        source_file=source,
        keywords=[place.name, *place.aliases, kind],
    )


def event_fact(event: CampusEvent, source: str) -> Fact:
    kind = event.kind.replace("_", " ")
    when = f"{event.date.strftime('%A')}, {event.date.day} {event.date.strftime('%B %Y')}"
    text = f"Event: {event.title} ({kind}) on {when}"
    if event.start and event.end:
        text += f", {_hm(event.start)}-{_hm(event.end)} (24-hour time)"
    elif event.start:
        text += f", starting {_hm(event.start)} (24-hour time)"
    text += f", at {event.venue}." if event.venue else ". The venue is not listed."
    if not event.start:
        text += " The time is not listed."
    if event.organiser:
        text += f" Organised by: {event.organiser.rstrip('.')}."
    if event.audience:
        text += f" For: {event.audience.rstrip('.')}."
    if event.description:
        text += f" About: {event.description.rstrip('.')}."
    return Fact(
        fact_id=event.id,
        category=FactCategory.EVENT,
        text=text,
        day=event.date.strftime("%A"),
        verified=True,
        source_file=source,
        keywords=[event.title, kind],
    )


def build_campus_facts(campus: CampusKB, home_block: str, home_floor: str) -> list[Fact]:
    return [
        *(place_fact(p, home_block, home_floor, campus.source) for p in campus.places),
        *(event_fact(e, campus.source) for e in campus.events),
    ]


def programme_places(campus: CampusKB | None) -> dict[str, list[Place]]:
    """Classrooms and labs per programme code, for the programme facts."""
    out: dict[str, list[Place]] = {}
    for place in campus.places if campus else ():
        if place.kind in ("classroom", "lab"):
            for code in place.programmes:
                out.setdefault(code, []).append(place)
    return out


def kind_of(fact_id: str) -> str | None:
    """'facility:washroom:ladies-washroom' -> 'washroom'; 'event:2026-11-05:x' -> None (events: see Fact text)."""
    parts = fact_id.split(":")
    return parts[1] if len(parts) == 3 and parts[0] == "facility" else None


def event_date(fact_id: str) -> date | None:
    parts = fact_id.split(":")
    if len(parts) == 3 and parts[0] == "event":
        try:
            return date.fromisoformat(parts[1])
        except ValueError:
            return None
    return None


def event_kind(fact: Fact) -> str | None:
    match = re.match(r"Event: .*? \(([^()]+)\) on ", fact.text)
    return match[1].replace(" ", "_") if match else None


# --------------------------------------------------------------------------- template + CLI


def write_template(path: Path, facility_kinds: Iterable[str], event_kinds: Iterable[str]) -> None:
    """An empty workbook with the right headers and a README sheet. No example rows: the data must be real."""
    readme = [("Sheet", "Column", "What to write")]
    readme += [(PLACES_SHEET, c, d) for c, d in PLACE_COLUMNS.items()]
    readme += [(EVENTS_SHEET, c, d) for c, d in EVENT_COLUMNS.items()]
    readme += [
        ("", "", ""),
        ("Allowed", "Place kinds", ", ".join(sorted(facility_kinds))),
        ("Allowed", "Event kinds", ", ".join(sorted(event_kinds))),
        ("", "", ""),
        ("How to update", "", "Save this file as rag-core/data/raw/campus_info.xlsx, then run `just index` "
         "or POST /reload-kb. No retraining is needed."),
        ("Check", "", "`just campus-check` lists rows that were not loaded and why."),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(columns=list(PLACE_COLUMNS)).to_excel(writer, sheet_name=PLACES_SHEET, index=False)
        pd.DataFrame(columns=list(EVENT_COLUMNS)).to_excel(writer, sheet_name=EVENTS_SHEET, index=False)
        pd.DataFrame(readme[1:], columns=list(readme[0])).to_excel(writer, sheet_name=README_SHEET, index=False)


def load_campus_from_settings(settings, lexicon=None) -> CampusKB:
    from rag_core.nlu.gazetteer import load_lexicon

    lexicon = lexicon or load_lexicon(settings.paths.entity_lexicon)
    path = settings.paths.campus_xlsx
    return load_campus(path, set(lexicon.facilities), set(lexicon.events), _display(path))


def _display(path: Path) -> str:
    from rag_core.config import PROJECT_ROOT

    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: Sequence[str] | None = None) -> int:
    from rag_core.config import get_settings
    from rag_core.nlu.gazetteer import load_lexicon

    parser = argparse.ArgumentParser(description="Check the campus places/events sheet, or write an empty template.")
    parser.add_argument("--write-template", type=Path, metavar="XLSX", help="write an empty template workbook and exit")
    args = parser.parse_args(argv)
    settings = get_settings()
    lexicon = load_lexicon(settings.paths.entity_lexicon)
    if args.write_template:
        write_template(args.write_template, lexicon.facilities, lexicon.events)
        print(f"wrote {args.write_template}")
        return 0
    campus = load_campus_from_settings(settings, lexicon)
    if not settings.paths.campus_xlsx.exists():
        print(f"{campus.source} not found: place and event questions will escalate.")
        return 0
    kinds = Counter(p.kind for p in campus.places)
    print(f"{campus.source}: {len(campus.places)} places, {len(campus.events)} events loaded")
    for kind, n in sorted(kinds.items()):
        print(f"  {kind:<18}{n:>4}")
    for line in campus.skipped:
        print(f"  SKIPPED {line}")
    return 1 if campus.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
