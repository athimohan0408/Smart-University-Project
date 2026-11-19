"""The semester timetable, as the department office changes it from the admin dashboard.

The timetable lives in rag-core's faculty workbook (``8th_floor_faculty_with_subjects.xlsx``: the "8th Floor
Faculty" sheet with a column per weekday, consultation hours and office rooms, plus a "Rooms" sheet with each
classroom's class teacher). rag-core's own way to take a new workbook is: save it as its
``data/raw/8th_floor_faculty_with_subjects.xlsx`` and call ``POST /reload-kb``, which re-reads it and rebuilds the
answers without a restart. This module does exactly that, after checking the file and showing what will change,
and keeps every published version so a bad upload can be undone.

Upload -> check (problems that stop it, warnings that don't) -> preview of changes -> publish -> roll back.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from kiosk_ui.admin_store import AdminStore, InfoEdit, TimetableVersion

SHEET = "8th Floor Faculty"
ROOMS_SHEET = "Rooms"
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
REQUIRED = ("Full Name", "Office Room", "Consultation Hours", *DAYS)
COMPARED = ("Office Room", "Floor", "Consultation Hours", *DAYS)
_TITLES = {"dr", "fr", "prof", "mr", "ms", "mrs"}


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def nice_name(raw: str) -> str:
    """'Dr SHIVANGI SINGH' -> 'Dr Shivangi Singh' (initials like 'P K' stay)."""
    out = []
    for w in raw.split():
        if w.casefold() in _TITLES or (len(w) > 1 and w.isupper()):
            out.append(w.capitalize())
        else:
            out.append(w)  # initials ("P K") and words already in mixed case
    return " ".join(out)


@dataclass
class Workbook:
    faculty: dict[str, dict[str, str]] = field(default_factory=dict)  # full name -> column -> value
    rooms: dict[str, InfoEdit] = field(default_factory=dict)  # "room:808" -> name, class teacher
    problems: list[str] = field(default_factory=list)  # stop publishing
    warnings: list[str] = field(default_factory=list)  # worth a look, don't stop it


def read_workbook(path: Path) -> Workbook:
    import openpyxl  # rag-core's dependency too; only needed here

    wb_out = Workbook()
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable upload is the same problem for the office
        wb_out.problems.append(f"This isn't an Excel workbook that can be opened ({type(exc).__name__}).")
        return wb_out
    try:
        if SHEET not in wb.sheetnames:
            wb_out.problems.append(f'The workbook has no "{SHEET}" sheet (it has: {", ".join(wb.sheetnames)}).')
            return wb_out
        rows = wb[SHEET].iter_rows(values_only=True)
        header = [_clean(h) for h in next(rows, ())]
        missing = [c for c in REQUIRED if c not in header]
        if missing:
            wb_out.problems.append(f'The "{SHEET}" sheet is missing the column(s): {", ".join(missing)}.')
            return wb_out
        col = {name: header.index(name) for name in header if name}
        for n, row in enumerate(rows, start=2):
            values = {name: _clean(row[i]) if i < len(row) else "" for name, i in col.items()}
            name = values["Full Name"]
            if not name:
                if any(values[d] for d in DAYS) or values["Office Room"]:
                    wb_out.problems.append(f"Row {n} has a timetable or room but no name.")
                continue
            if name in wb_out.faculty:
                wb_out.problems.append(f"{name} appears twice (row {n}).")
                continue
            wb_out.faculty[name] = values
            if not values["Office Room"]:
                wb_out.warnings.append(f"{name} has no office room: the kiosk can't show the way to them.")
            if not values["Consultation Hours"]:
                wb_out.warnings.append(f"{name} has no consultation hours.")
        if not wb_out.faculty:
            wb_out.problems.append(f'The "{SHEET}" sheet has no faculty rows.')
        if ROOMS_SHEET in wb.sheetnames:
            rows = wb[ROOMS_SHEET].iter_rows(values_only=True)
            header = [_clean(h) for h in next(rows, ())]
            if "Room No" in header:
                at = {name: header.index(name) for name in header if name}
                for row in rows:
                    get = lambda c: _clean(row[at[c]]) if c in at and at[c] < len(row) else ""  # noqa: E731
                    if number := get("Room No"):
                        teacher = get("Class Teacher")
                        wb_out.rooms[f"room:{number.casefold()}"] = InfoEdit(
                            name=get("Room Name") or None, class_teacher=nice_name(teacher) if teacher else None)
        else:
            wb_out.warnings.append(f'No "{ROOMS_SHEET}" sheet: classroom names and class teachers stay as they are.')
    finally:
        wb.close()
    return wb_out


def compare(old: Workbook | None, new: Workbook) -> dict:
    """What publishing `new` changes: faculty added / removed, and each changed room, consultation hours and day."""
    before = old.faculty if old else {}
    added = sorted(set(new.faculty) - set(before))
    removed = sorted(set(before) - set(new.faculty))
    changed = []
    for name in sorted(set(new.faculty) & set(before)):
        for column in COMPARED:
            a, b = before[name].get(column, ""), new.faculty[name].get(column, "")
            if a != b:
                changed.append({"name": nice_name(name), "what": column, "before": a, "after": b})
    rooms = []
    for rid in sorted(set(new.rooms) | set(old.rooms if old else {})):
        a = (old.rooms.get(rid) if old else None) or InfoEdit()
        b = new.rooms.get(rid) or InfoEdit()
        if (a.name, a.class_teacher) != (b.name, b.class_teacher):
            rooms.append({"room": rid.split(":", 1)[1], "before": " · ".join(x for x in (a.name, a.class_teacher) if x),
                          "after": " · ".join(x for x in (b.name, b.class_teacher) if x)})
    return {"faculty_before": len(before), "faculty_after": len(new.faculty),
            "added": [nice_name(n) for n in added], "removed": [nice_name(n) for n in removed],
            "changed": changed, "rooms": rooms}


class Timetables:
    """Uploads waiting to be published, the published versions, and publishing itself."""

    def __init__(self, store: AdminStore, target: Path, reload: Callable[[], None]) -> None:
        self.store, self.target, self.reload = store, target, reload
        self.folder = store.folder / "timetables"

    def _version_path(self, version_id: str) -> Path:
        v = next((v for v in self.store.state.timetable_versions if v.id == version_id), None)
        if v is None:
            raise KeyError(version_id)
        return self.folder / v.file

    def current(self) -> Workbook | None:
        return read_workbook(self.target) if self.target.exists() else None

    def stage(self, data: bytes, original_name: str) -> tuple[str, Workbook, dict]:
        """Keep an upload aside (not published yet): its id, the check, and what would change."""
        pending = f"pending-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        path = self.folder / f"{pending}.xlsx"
        path.write_bytes(data)
        (self.folder / f"{pending}.name").write_text(original_name, encoding="utf-8")
        book = read_workbook(path)
        return pending, book, compare(self.current(), book)

    def _keep_original(self) -> None:
        """Before the first publish from the dashboard, keep the workbook rag-core has now as a version."""
        if self.store.state.timetable_versions or not self.target.exists():
            return
        book = read_workbook(self.target)
        version = TimetableVersion(id="original", file="original.xlsx", original_name=self.target.name,
                                   uploaded_by="(before the admin dashboard)",
                                   uploaded_at=datetime.fromtimestamp(self.target.stat().st_mtime, timezone.utc)
                                   .isoformat(timespec="seconds"), faculty=len(book.faculty))
        shutil.copy2(self.target, self.folder / version.file)
        self.store.state.timetable_versions.append(version)
        self.store.state.timetable_current = version.id

    def _install(self, source: Path, book: Workbook) -> None:
        """Copy `source` over rag-core's workbook and have rag-core rebuild; put the old one back if that fails."""
        backup = self.folder / "previous.xlsx"
        had_old = self.target.exists()
        if had_old:
            shutil.copy2(self.target, backup)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, self.target)
        try:
            self.reload()
        except Exception:
            if had_old:
                shutil.copy2(backup, self.target)
                try:
                    self.reload()
                except Exception:  # noqa: BLE001 - the old file is back; rag-core will pick it up on its next reload
                    pass
            raise
        self.store.state.rooms_sheet = dict(book.rooms)

    def publish(self, pending: str, user: str, note: str | None = None) -> TimetableVersion:
        path = self.folder / f"{pending}.xlsx"
        if not pending.startswith("pending-") or not path.exists():
            raise KeyError(pending)
        book = read_workbook(path)
        if book.problems:
            raise ValueError("This file has problems to fix first: " + " ".join(book.problems))
        name_file = self.folder / f"{pending}.name"
        original = name_file.read_text(encoding="utf-8") if name_file.exists() else path.name
        with self.store._lock:
            self._keep_original()
            self._install(path, book)
            vid = datetime.now().strftime("%Y%m%d-%H%M%S")
            final = self.folder / f"{vid}.xlsx"
            path.replace(final)
            name_file.unlink(missing_ok=True)
            version = TimetableVersion(id=vid, file=final.name, original_name=original, uploaded_by=user,
                                       uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                       faculty=len(book.faculty), note=note or None)
            self.store.state.timetable_versions.append(version)
            self.store.state.timetable_current = vid
            self.store.save()
        return version

    def restore(self, version_id: str) -> TimetableVersion:
        path = self._version_path(version_id)
        book = read_workbook(path)
        with self.store._lock:
            self._install(path, book)
            self.store.state.timetable_current = version_id
            self.store.save()
        return next(v for v in self.store.state.timetable_versions if v.id == version_id)

    def discard(self, pending: str) -> None:
        if pending.startswith("pending-"):
            (self.folder / f"{pending}.xlsx").unlink(missing_ok=True)
            (self.folder / f"{pending}.name").unlink(missing_ok=True)

    def file(self, version_id: str) -> Path:
        return self._version_path(version_id)
