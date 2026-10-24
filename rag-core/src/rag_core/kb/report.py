"""Human-facing outputs of faculty ingest: the subject-code list (CSV) and the data-quality report (Markdown)."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from rag_core.kb.glossary import Glossary, GlossaryEntry
from rag_core.kb.models import Faculty
from rag_core.kb.parsing import is_room_number

MISSING = "—"


@dataclass(frozen=True)
class SubjectCodeUsage:
    subject_code: str
    programmes: list[str]
    count: int
    faculty: list[str]


def subject_code_usage(records: Sequence[Faculty]) -> list[SubjectCodeUsage]:
    counts: Counter[str] = Counter()
    programmes: dict[str, set[str]] = defaultdict(set)
    teachers: dict[str, dict[str, None]] = defaultdict(dict)  # dict keeps first-seen order
    for fac in records:
        for slot in fac.timetable:
            counts[slot.subject_code] += 1
            if slot.programme:
                programmes[slot.subject_code].add(slot.programme)
            teachers[slot.subject_code][fac.display_name] = None
    usages = [
        SubjectCodeUsage(code, sorted(programmes[code]), n, list(teachers[code]))
        for code, n in counts.items()
    ]
    return sorted(usages, key=lambda u: (-u.count, u.subject_code.casefold()))


def write_subject_codes_csv(usages: Iterable[SubjectCodeUsage], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["subject_code", "programmes", "count", "faculty"])
        for u in usages:
            writer.writerow([u.subject_code, "; ".join(u.programmes), u.count, "; ".join(u.faculty)])


# --------------------------------------------------------------------------- markdown helpers


def _cell(value: object) -> str:
    text = MISSING if value is None or value == "" else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return lines


def _section(title: str, headers: Sequence[str], rows: Sequence[Sequence[object]], intro: str = "") -> list[str]:
    lines = ["", f"## {title} ({len(rows)})", ""]
    if intro:
        lines += [intro, ""]
    lines += _table(headers, rows) if rows else ["None."]
    return lines


# --------------------------------------------------------------------------- report


def _glossary_coverage(found: set[str], lookup, label: str) -> tuple[list[object], list[str]]:
    entries: dict[str, GlossaryEntry | None] = {code: lookup(code) for code in found}
    missing = sorted((c for c, e in entries.items() if e is None), key=str.casefold)
    present = [e for e in entries.values() if e is not None]
    row = [
        label,
        len(found),
        len(missing),
        sum(not e.is_expanded for e in present),
        sum(e.is_expanded and not e.verified for e in present),
        sum(e.verified for e in present),
    ]
    return row, missing


def build_quality_report(
    records: Sequence[Faculty],
    *,
    source: str,
    source_sha256: str,
    sheet: str,
    glossary: Glossary | None,
    glossary_path: str,
) -> str:
    slots = [(f, s) for f in records for s in f.timetable]
    unparsed_lines = [(f.display_name, line) for f in records for line in f.timetable_unparsed]
    status_counts = Counter(f.consultation_hours.status for f in records)

    missing_email = [(f.display_name, f.source_row) for f in records if f.email is None]
    missing_hours = [(f.display_name, f.source_row) for f in records if f.consultation_hours.raw is None]
    off_floor = [
        (f.display_name, f.location.block, f.location.floor, f.location.room)
        for f in records
        if not f.location.on_8th_floor
    ]

    by_room: dict[tuple, list[str]] = defaultdict(list)
    for f in records:
        if f.location.room:
            by_room[(f.location.room, f.location.block, f.location.floor)].append(f.display_name)
    shared_rooms = [
        (room, f"{block or MISSING}, {floor or MISSING}", ", ".join(names))
        for (room, block, floor), names in sorted(by_room.items(), key=lambda kv: kv[0][0])
        if len(names) > 1
    ]
    non_rooms = [
        (f.display_name, f.location.room)
        for f in records
        if f.location.room and not is_room_number(f.location.room)
    ]

    incomplete_slots = []
    for f, s in slots:
        missing = [name for name, value in (("programme", s.programme), ("semester", s.semester)) if value is None]
        if missing:
            incomplete_slots.append((f.display_name, s.day, s.raw, ", ".join(missing)))

    hours_issues = [
        (f.display_name, f.consultation_hours.status, f.consultation_hours.raw, "; ".join(f.consultation_hours.unparsed_segments))
        for f in records
        if f.consultation_hours.status in ("partial", "unparsed")
    ]
    conflicts = [(f.display_name, sentence) for f in records for sentence in f.conflicts]

    short_names = {f.short_name for f in records if f.short_name}
    unknown_co = sorted(
        {(code, f.display_name, s.raw) for f, s in slots for code in s.co_teachers if code not in short_names}
    )

    alias_owners: dict[str, set[str]] = defaultdict(set)
    for f in records:
        for alias in f.aliases:
            alias_owners[alias.casefold()].add(f.display_name)
    shared_aliases = sorted((a, ", ".join(sorted(o))) for a, o in alias_owners.items() if len(o) > 1)

    out = [
        "# Data quality report: faculty sheet",
        "",
        f"Source: `{source}`, sheet `{sheet}`, sha256 `{source_sha256[:12]}`. "
        "Regenerated by `python -m rag_core.kb.ingest_faculty` on every ingest; do not edit by hand.",
        "",
        "## Summary",
        "",
        *_table(
            ["check", "count"],
            [
                ("faculty records", len(records)),
                ("timetable lines parsed", len(slots)),
                ("timetable lines unparsed", len(unparsed_lines)),
                (
                    "consultation hours parsed / partial / unparsed / missing",
                    " / ".join(str(status_counts[s]) for s in ("parsed", "partial", "unparsed", "missing")),
                ),
                ("missing emails", len(missing_email)),
                ("not on the 8th floor (incl. unknown location)", len(off_floor)),
                ("shared rooms", len(shared_rooms)),
                ("conflicting note sentences", len(conflicts)),
            ],
        ),
    ]
    out += _section(
        "Missing emails", ["faculty", "sheet row"], missing_email,
        "Answer 'not available'. Never construct an address: the existing ones follow no single pattern.",
    )
    out += _section("Missing consultation hours", ["faculty", "sheet row"], missing_hours)
    out += _section(
        "Not on the 8th floor", ["faculty", "block", "floor", "room"], off_floor,
        "Answers must say these faculty are not on this floor; navigation cannot route to them.",
    )
    out += _section("Shared rooms", ["room", "location", "faculty"], shared_rooms)
    out += _section("Office Room values that are not room numbers", ["faculty", "value"], non_rooms)
    out += _section("Unparsed timetable lines", ["faculty", "line"], unparsed_lines)
    out += _section(
        "Timetable lines parsed without a programme or semester", ["faculty", "day", "line", "missing"],
        incomplete_slots,
        "Parsed by the code grammar but worth confirming, e.g. whether `MSC CSA` is a subject or a programme.",
    )
    out += _section(
        "Unparsed or partially parsed consultation hours", ["faculty", "status", "raw", "not understood"],
        hours_issues,
        "The raw text is kept in faculty.json. Only the listed segments were not turned into slots.",
    )
    out += _section(
        "Conflicting notes", ["faculty", "note"], conflicts,
        "Detected by cue words ('also mentions', 'but', '(old)'). Surface these to visitors as uncertainty.",
    )
    out += _section("Co-teacher codes that match no faculty short name", ["code", "faculty", "line"], unknown_co)
    out += _section("Aliases shared by more than one faculty", ["alias", "faculty"], shared_aliases)

    out += ["", "## Glossary coverage", ""]
    if glossary is None:
        out.append(f"Glossary not found at `{glossary_path}`. No code has a verified expansion.")
    else:
        prog_row, prog_missing = _glossary_coverage(
            {s.programme for _, s in slots if s.programme}, glossary.programme, "programmes"
        )
        subj_row, subj_missing = _glossary_coverage({s.subject_code for _, s in slots}, glossary.subject, "subjects")
        out += [f"Glossary: `{glossary_path}`.", ""]
        out += _table(
            ["codes", "found in timetable", "missing from glossary", "TODO", "unverified", "verified"],
            [prog_row, subj_row],
        )
        for label, missing in (("programme", prog_missing), ("subject", subj_missing)):
            if missing:
                out += ["", f"Missing {label} codes: " + ", ".join(f"`{c}`" for c in missing)]
    return "\n".join(out) + "\n"
