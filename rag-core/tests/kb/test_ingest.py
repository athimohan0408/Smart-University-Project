import csv
from datetime import time

import openpyxl
import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.kb.ingest_faculty import EXPECTED_COLUMNS, ingest, write_outputs
from rag_core.kb.models import FacultyKB
from rag_core.kb.parsing import (
    DayWindow,
    build_aliases,
    parse_class_code,
    parse_consultation_hours,
    parse_time_range,
    parse_timetable_line,
    split_research_areas,
    split_title,
)

# The window from the spec ("classes run between 7:30 and 17:00"), fixed here so these unit
# tests don't drift if config/settings.yaml is retuned.
WINDOW = DayWindow.from_times(time(7, 30), time(17, 0))


# --------------------------------------------------------------------------- time ranges


def test_noon_crossing_time():
    slot = parse_timetable_line("11.45 to 1.45 pm - 2 MSAIM ML LAB", "Tuesday", WINDOW)
    assert (slot.start_time, slot.end_time) == ("11:45", "13:45")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9.45 to 11.45 am", ("09:45", "11:45")),
        ("10 to 12 pm", ("10:00", "12:00")),  # '12 pm' is noon
        ("7.30 to 9 am", ("07:30", "09:00")),  # window start is inclusive
        ("12.45 to 1.45 pm", ("12:45", "13:45")),
        ("3.45 to 4.45 pm", ("15:45", "16:45")),
        ("1:45-4:00", ("13:45", "16:00")),  # no marker at all: window decides
        ("9.15- 11.00", ("09:15", "11:00")),
        ("03:30 pm to 07:00 pm", ("15:30", "19:00")),  # explicit markers beat the window
    ],
)
def test_time_ranges(text, expected):
    assert parse_time_range(text, WINDOW) == expected


def test_time_outside_window_is_not_guessed():
    assert parse_time_range("5 to 6", WINDOW) is None


# --------------------------------------------------------------------------- class codes


def _code(code: str) -> tuple:
    f = parse_class_code(code)
    return f["semester"], f["programme"], f["section"], f["subject_code"], f["is_lab"]


def test_no_space_code_matches_spaced_code():
    assert _code("2MCA A RM") == _code("2 MCA A RM") == (2, "MCA", "A", "RM", False)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("3BCA A-CN", (3, "BCA", "A", "CN", False)),
        ("7BCA-RM LAB", (7, "BCA", None, "RM", True)),
        ("3 BCA A -RPA", (3, "BCA", "A", "RPA", False)),
        ("1BCA B-C PROG", (1, "BCA", "B", "C PROG", False)),
        ("5 MCA AMI LAB", (5, "MCA", None, "AMI", True)),
        ("5 MCA A IoT", (5, "MCA", "A", "IoT", False)),
        ("7BSc PRJ", (7, "BSC", None, "PRJ", False)),
        ("CPCG", (None, None, None, "CPCG", False)),
        ("MDC", (None, None, None, "MDC", False)),
    ],
)
def test_class_code_variants(code, expected):
    assert _code(code) == expected


def test_bsc_stream_is_separated_from_subject():
    fields = parse_class_code("1BSC CS C PROG LAB")
    assert (fields["programme"], fields["stream"], fields["subject_code"], fields["is_lab"]) == (
        "BSC", "CS", "C PROG", True,
    )


def test_co_teachers():
    slot = parse_timetable_line("12.45 to 1.45 pm - 7 BCA BI (SG/JF)", "Monday", WINDOW)
    assert slot.co_teachers == ["SG", "JF"]
    assert slot.subject_code == "BI"


@pytest.mark.parametrize(
    "line",
    ["TBA", "9.45 to 11.45 am - see the office", "sometime - 2 MCA A RM", "9.45 to 11.45 am -"],
)
def test_unreadable_lines_are_rejected_not_guessed(line):
    assert parse_timetable_line(line, "Monday", WINDOW) is None


# --------------------------------------------------------------------------- consultation hours


def test_consultation_hours_parsed():
    hours = parse_consultation_hours("Monday (11:45 to 1:45); Friday (11:45 to 1:45)", WINDOW)
    assert hours.status == "parsed"
    assert [(s.day, s.start, s.end) for s in hours.slots] == [
        ("Monday", "11:45", "13:45"),
        ("Friday", "11:45", "13:45"),
    ]


def test_several_ranges_on_one_day():
    hours = parse_consultation_hours("Wednesday 11.45 to 12.45pm, 12.45 to 1.45 pm", WINDOW)
    assert [(s.start, s.end) for s in hours.slots] == [("11:45", "12:45"), ("12:45", "13:45")]


@pytest.mark.parametrize("raw", ["Monday", "03:30 pm to 07:00 pm", "Monday - 11:45"])
def test_unparseable_consultation_hours_keep_raw(raw):
    hours = parse_consultation_hours(raw, WINDOW)
    assert hours.status == "unparsed"
    assert hours.raw == raw
    assert hours.slots == []
    assert hours.unparsed_segments == [raw]


def test_partial_consultation_hours_flag_what_was_not_understood():
    raw = "Monday to; Wednesday 1.30 to 2.30 PM; Friday 1.30 to 2.30 PM"
    hours = parse_consultation_hours(raw, WINDOW)
    assert hours.status == "partial"
    assert [(s.day, s.start) for s in hours.slots] == [("Wednesday", "13:30"), ("Friday", "13:30")]
    assert hours.unparsed_segments == ["Monday to"]


def test_leftover_text_is_flagged_even_when_times_parse():
    segment = "Tuesday: 09:00 - 03:00 pm BANGALORE CENTRAL CAMPUS"
    hours = parse_consultation_hours(segment, WINDOW)
    assert [(s.start, s.end) for s in hours.slots] == [("09:00", "15:00")]
    assert hours.unparsed_segments == [segment]


# --------------------------------------------------------------------------- names and lists


def test_title_split_and_aliases():
    title, name = split_title("Dr DEEPA V JOSE")
    assert (title, name) == ("Dr", "Deepa V Jose")
    assert build_aliases(name, title, "DVJ") == ["Deepa", "Jose", "Deepa Jose", "DVJ"]


def test_priest_aliases_from_short_name():
    aliases = build_aliases("Jossy P George", "Dr", "Fr.J")
    assert {"Fr. Jossy", "Father Jossy", "Jossy George", "Fr.J"} <= set(aliases)


def test_research_areas_split_on_commas_and_and():
    raw = "deep learning, cloud computing, and blockchain."
    assert split_research_areas(raw) == ["deep learning", "cloud computing", "blockchain"]


# --------------------------------------------------------------------------- whole sheet


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def sheet_rows(settings):
    """Read the sheet independently of pandas: {excel row number: {column: value}}."""
    wb = openpyxl.load_workbook(settings.paths.raw_faculty_xlsx, read_only=True)
    rows = wb[settings.kb.faculty_sheet].iter_rows(values_only=True)
    header = list(next(rows))
    data = {
        n: dict(zip(header, values))
        for n, values in enumerate(rows, start=2)
        if any(v is not None for v in values)
    }
    wb.close()
    return data


@pytest.fixture(scope="module")
def records(settings):
    return ingest(settings.paths.raw_faculty_xlsx, settings.kb)


@pytest.fixture(scope="module")
def by_id(records):
    return {r.id: r for r in records}


def test_every_sheet_row_produces_exactly_one_record(records, sheet_rows):
    assert len(records) == len(sheet_rows) == 36
    assert sorted(r.source_row for r in records) == sorted(sheet_rows)
    assert len({r.id for r in records}) == len(records)
    for r in records:
        assert r.display_name.upper() == " ".join(sheet_rows[r.source_row]["Full Name"].split()).upper()


def test_sheet_has_expected_columns(sheet_rows):
    assert set(EXPECTED_COLUMNS) <= set(next(iter(sheet_rows.values())))


def test_rooms_are_always_strings(by_id):
    assert by_id["neetha-s-s"].location.room == "871"  # numeric cell in the sheet
    assert by_id["ashwini-patil"].location.room == "607A"
    assert by_id["jossy-p-george"].location.room == "014"  # leading zero survives
    assert by_id["shoney-sebastian"].location.room == "Director, Center for Research (PhD)"


def test_missing_email_is_none_not_guessed(records, by_id):
    assert by_id["kavitha-r"].email is None
    assert sorted(r.id for r in records if r.email is None) == ["kavitha-r", "nizar-banu-p-k", "suresh-k"]


def test_on_8th_floor(records, by_id):
    assert sum(r.location.on_8th_floor for r in records) == 18
    assert by_id["deepa-v-jose"].location.on_8th_floor
    assert not by_id["peter-augustin-d"].location.on_8th_floor  # Central Block, but Ninth Floor
    suresh = by_id["suresh-k"].location
    assert (suresh.block, suresh.floor, suresh.room, suresh.raw, suresh.on_8th_floor) == (None, None, None, None, False)


def test_zero_unparsed_timetable_lines(records, sheet_rows):
    days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
    sheet_lines = sum(
        1 for row in sheet_rows.values() for d in days for ln in str(row[d] or "").splitlines() if ln.strip()
    )
    assert [line for r in records for line in r.timetable_unparsed] == []
    assert sum(len(r.timetable) for r in records) == sheet_lines


def test_consultation_raw_is_never_dropped(records, sheet_rows):
    for r in records:
        sheet_value = sheet_rows[r.source_row]["Consultation Hours"]
        assert r.consultation_hours.raw == (sheet_value.strip() if sheet_value else None)


def test_conflicting_notes_are_surfaced(by_id):
    assert any("875" in c for c in by_id["helen-k-joy"].conflicts)
    assert any("(old)" in c for c in by_id["thirunavukkarasu-v"].conflicts)
    assert by_id["vijay-arputharaj-j"].conflicts == []


def test_write_outputs(tmp_path, settings, records):
    paths = settings.paths
    out = {"faculty_json": tmp_path / "faculty.json", "subject_codes_csv": tmp_path / "codes.csv", "report_path": tmp_path / "report.md"}
    write_outputs(
        records,
        xlsx_path=paths.raw_faculty_xlsx,
        sheet=settings.kb.faculty_sheet,
        glossary=None,
        glossary_path=paths.glossary,
        **out,
    )

    kb = FacultyKB.model_validate_json(out["faculty_json"].read_text(encoding="utf-8"))
    assert kb.faculty == records

    with out["subject_codes_csv"].open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert set(rows[0]) == {"subject_code", "programmes", "count", "faculty"}
    assert sum(int(r["count"]) for r in rows) == sum(len(f.timetable) for f in records)

    report = out["report_path"].read_text(encoding="utf-8")
    assert "## Unparsed timetable lines (0)" in report
    assert "## Missing emails (3)" in report
    assert "Main list also mentions room 875" in report
