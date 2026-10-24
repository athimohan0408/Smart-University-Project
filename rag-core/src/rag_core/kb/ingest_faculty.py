"""Faculty sheet -> validated records in data/processed/faculty.json.

Also writes subject_codes_found.csv and data_quality_report.md (paths in config/settings.yaml).
The glossary (data/kb/glossary.yaml) is read for the coverage report but never written.

Run: ``python -m rag_core.kb.ingest_faculty``  (or ``just ingest``)
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from rag_core.config import PROJECT_ROOT, KBSettings, get_settings
from rag_core.kb.glossary import Glossary, load_glossary
from rag_core.kb.models import TIMETABLE_DAYS, Faculty, FacultyKB, Location
from rag_core.kb.parsing import (
    DayWindow,
    build_aliases,
    note_conflicts,
    normalise_room,
    parse_consultation_hours,
    parse_timetable_line,
    slugify,
    split_research_areas,
    split_semicolon_list,
    split_title,
)
from rag_core.kb.report import build_quality_report, subject_code_usage, write_subject_codes_csv

DAY_COLUMNS = TIMETABLE_DAYS
EXPECTED_COLUMNS = (
    "Sl No", "Full Name", "Short Name", "Department", "Specialization", "Block", "Floor",
    "Office Room", "Programmes Taught", "Currently Handling", "Research Areas", "Email", "Gender",
    "Consultation Hours", "Weekly Hours", *DAY_COLUMNS, "Notes",
)
HEADER_ROWS = 1  # sheet row of the header; data starts on the next row


def _clean(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    number = float(value)
    if not number.is_integer():
        raise ValueError(f"expected a whole number, got {value!r}")
    return int(number)


def _same(a: str | None, b: str) -> bool:
    return a is not None and " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def read_sheet(xlsx_path: Path, sheet: str) -> pd.DataFrame:
    # dtype=str keeps rooms like '014' intact and stops pandas guessing types per column.
    frame = pd.read_excel(xlsx_path, sheet_name=sheet, dtype=str)
    if missing := [c for c in EXPECTED_COLUMNS if c not in frame.columns]:
        raise ValueError(f"{xlsx_path.name} [{sheet}] is missing columns: {missing}")
    return frame.dropna(how="all")


def build_faculty(row: dict[str, str | None], source_row: int, kb: KBSettings, window: DayWindow) -> Faculty:
    if row["Full Name"] is None:
        raise ValueError("Full Name is empty")
    title, full_name = split_title(row["Full Name"])

    block, floor, room_raw = row["Block"], row["Floor"], row["Office Room"]
    raw_location = "; ".join(
        f"{label}: {value}" for label, value in (("Block", block), ("Floor", floor), ("Office Room", room_raw)) if value
    )

    timetable, unparsed = [], []
    for day in DAY_COLUMNS:
        for line in (row[day] or "").splitlines():
            if not line.strip():
                continue
            slot = parse_timetable_line(line, day, window)
            if slot is None:
                unparsed.append(f"{day}: {line.strip()}")
            else:
                timetable.append(slot)

    return Faculty(
        id=slugify(full_name),
        source_row=source_row,
        title=title,
        full_name=full_name,
        short_name=row["Short Name"],
        aliases=build_aliases(full_name, title, row["Short Name"]),
        department=row["Department"],
        specialization=row["Specialization"],
        research_areas=split_research_areas(row["Research Areas"]),
        research_areas_raw=row["Research Areas"],
        programmes_taught=split_semicolon_list(row["Programmes Taught"]),
        currently_handling=row["Currently Handling"],
        email=row["Email"].lower() if row["Email"] else None,
        gender=row["Gender"],
        location=Location(
            block=block,
            floor=floor,
            room=normalise_room(room_raw),
            on_8th_floor=_same(block, kb.home_block) and _same(floor, kb.home_floor),
            raw=raw_location or None,
        ),
        consultation_hours=parse_consultation_hours(row["Consultation Hours"], window),
        weekly_hours=_to_int(row["Weekly Hours"]),
        timetable=timetable,
        timetable_unparsed=unparsed,
        notes=row["Notes"],
        conflicts=note_conflicts(row["Notes"]),
    )


def ingest(xlsx_path: Path, kb: KBSettings) -> list[Faculty]:
    """Exactly one validated Faculty per non-empty sheet row, in sheet order."""
    frame = read_sheet(xlsx_path, kb.faculty_sheet)
    window = DayWindow.from_times(kb.day_start, kb.day_end)
    records = []
    for index, values in frame.iterrows():
        source_row = int(index) + HEADER_ROWS + 1
        row = {col: _clean(values[col]) for col in EXPECTED_COLUMNS}
        try:
            records.append(build_faculty(row, source_row, kb, window))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"sheet row {source_row} ({row['Full Name']}): {exc}") from exc

    ids = [r.id for r in records]
    if dupes := sorted({i for i in ids if ids.count(i) > 1}):
        raise ValueError(f"duplicate faculty ids (two rows with the same name?): {dupes}")
    return records


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(
    records: Sequence[Faculty],
    *,
    xlsx_path: Path,
    sheet: str,
    faculty_json: Path,
    subject_codes_csv: Path,
    report_path: Path,
    glossary: Glossary | None,
    glossary_path: Path,
) -> None:
    sha256 = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()
    source = _display_path(xlsx_path)

    kb = FacultyKB(source=source, source_sha256=sha256, sheet=sheet, faculty=list(records))
    faculty_json.parent.mkdir(parents=True, exist_ok=True)
    faculty_json.write_text(kb.model_dump_json(indent=2) + "\n", encoding="utf-8")

    write_subject_codes_csv(subject_code_usage(records), subject_codes_csv)

    report = build_quality_report(
        records,
        source=source,
        source_sha256=sha256,
        sheet=sheet,
        glossary=glossary,
        glossary_path=_display_path(glossary_path),
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")


def main() -> None:
    settings = get_settings()
    paths = settings.paths
    records = ingest(paths.raw_faculty_xlsx, settings.kb)
    glossary = load_glossary(paths.glossary) if paths.glossary.exists() else None
    write_outputs(
        records,
        xlsx_path=paths.raw_faculty_xlsx,
        sheet=settings.kb.faculty_sheet,
        faculty_json=paths.faculty_json,
        subject_codes_csv=paths.subject_codes_csv,
        report_path=paths.data_quality_report,
        glossary=glossary,
        glossary_path=paths.glossary,
    )

    parsed = sum(len(r.timetable) for r in records)
    unparsed = sum(len(r.timetable_unparsed) for r in records)
    statuses = [r.consultation_hours.status for r in records]
    print(f"faculty records:    {len(records)} -> {_display_path(paths.faculty_json)}")
    print(f"timetable lines:    {parsed} parsed, {unparsed} unparsed")
    print(
        "consultation hours: "
        + ", ".join(f"{statuses.count(s)} {s}" for s in ("parsed", "partial", "unparsed", "missing"))
    )
    print(f"subject codes:      {_display_path(paths.subject_codes_csv)}")
    print(f"quality report:     {_display_path(paths.data_quality_report)}")
    if glossary is None:
        print(f"WARNING: glossary not found at {_display_path(paths.glossary)}")
    if unparsed:
        print(f"WARNING: {unparsed} timetable lines could not be parsed; see the quality report")


if __name__ == "__main__":
    main()
