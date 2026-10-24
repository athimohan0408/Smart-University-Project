"""Validated KB records produced by ingest. Every parsed value keeps its raw source text nearby."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field, model_validator

Weekday = Literal["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS: tuple[Weekday, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
TIMETABLE_DAYS: tuple[Weekday, ...] = WEEKDAYS[:6]  # the sheet has Monday..Saturday columns

# 24h, zero-padded, so plain string comparison orders times correctly.
HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
Email = Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_fields(cls, data):
        # Computed fields are written to JSON for consumers; ignore them when loading it back.
        if isinstance(data, dict) and cls.model_computed_fields:
            return {k: v for k, v in data.items() if k not in cls.model_computed_fields}
        return data


def _check_order(start: str, end: str) -> None:
    if start >= end:
        raise ValueError(f"start {start} must be before end {end}")


class ClassSlot(_Record):
    """One timetable line, e.g. '11.45 to 1.45 pm - 2 MSAIM ML LAB' on Tuesday."""

    day: Weekday
    start_time: HHMM
    end_time: HHMM
    raw: str
    semester: Annotated[int, Field(ge=1, le=12)] | None = None
    programme: str | None = Field(default=None, description="Upper-cased code: MCA, MSAIM, BCA, BSC, MSC.")
    stream: str | None = Field(default=None, description="BSc/MSc stream token, e.g. 'CS' in '3BSC CS DBMS'.")
    section: str | None = None
    subject_code: str = Field(min_length=1, description="As written in the sheet, e.g. 'ML', 'C PROG', 'IoT'.")
    is_lab: bool = False
    co_teachers: list[str] = Field(
        default_factory=list,
        description="Short codes in parentheses, verbatim, e.g. ['SG', 'JF']. May include this faculty's own code.",
    )

    @model_validator(mode="after")
    def _start_before_end(self) -> ClassSlot:
        _check_order(self.start_time, self.end_time)
        return self


class ConsultationSlot(_Record):
    day: Weekday
    start: HHMM
    end: HHMM

    @model_validator(mode="after")
    def _start_before_end(self) -> ConsultationSlot:
        _check_order(self.start, self.end)
        return self


class ConsultationHours(_Record):
    """Best-effort parse of a free-text cell. ``raw`` is always kept, even when nothing parses."""

    raw: str | None
    slots: list[ConsultationSlot] = Field(default_factory=list)
    unparsed_segments: list[str] = Field(
        default_factory=list,
        description="';'-separated segments not fully understood (no day, no time range, or leftover text).",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> Literal["missing", "parsed", "partial", "unparsed"]:
        if self.raw is None:
            return "missing"
        if not self.slots:
            return "unparsed"
        return "partial" if self.unparsed_segments else "parsed"


class Location(_Record):
    block: str | None
    floor: str | None
    room: str | None = Field(description="Always text: '871', '014', '607A', or a non-room label.")
    on_8th_floor: bool
    raw: str | None = Field(description="Block / Floor / Office Room cells as read, joined with ' / '.")


class Faculty(_Record):
    id: Slug
    source_row: int = Field(description="1-based Excel row number, for tracing back to the sheet.")
    title: str | None = Field(description="Prefix split off the full name, e.g. 'Dr', 'Fr.'.")
    full_name: str = Field(description="Title-cased, without the title, e.g. 'Deepa V Jose'.")
    short_name: str | None
    aliases: list[str]
    department: str | None
    specialization: str | None
    research_areas: list[str]
    research_areas_raw: str | None
    programmes_taught: list[str]
    currently_handling: str | None
    email: Email | None
    gender: str | None
    location: Location
    consultation_hours: ConsultationHours
    weekly_hours: int | None
    timetable: list[ClassSlot]
    timetable_unparsed: list[str] = Field(
        default_factory=list, description="'<Day>: <line>' for lines the parser could not read."
    )
    notes: str | None
    conflicts: list[str] = Field(
        default_factory=list, description="Sentences from notes that contradict a field (surface, never resolve)."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        return f"{self.title} {self.full_name}" if self.title else self.full_name


class FacultyKB(_Record):
    """Top-level shape of data/processed/faculty.json."""

    source: str = Field(description="Source workbook path, relative to the repo root.")
    source_sha256: str
    sheet: str
    faculty: list[Faculty]
