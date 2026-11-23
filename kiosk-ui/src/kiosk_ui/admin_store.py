"""What the department office changes from the admin dashboard, kept in ``data/admin/`` (not in git):

- ``users.json``: who can sign in. Passwords are stored only as salted PBKDF2-SHA256 hashes.
- ``state.json``: announcements, faculty status (on leave, in a meeting ...), closed rooms, place info edits,
  door photos, the class teachers read from the timetable's Rooms sheet, and the timetable versions.
- ``audit.jsonl``: the change log, one line per change: who, when, what.

Roles: ``admin`` (the department office: everything) and ``faculty`` (can set only their own status).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["admin", "faculty"]
STATUS_LABELS = {"on_leave": "On leave", "out": "Out of office", "meeting": "In a meeting", "in": "In the cabin"}
_ITERATIONS = 200_000
SESSION_S = 8 * 3600  # a working day
MAX_FAILS, LOCK_S = 5, 300  # five wrong passwords lock that account for five minutes


class User(BaseModel):
    username: str
    name: str
    role: Role
    faculty_id: str | None = None  # "faculty:nisha-varghese" for a faculty account
    salt: str
    hash: str
    created_at: str


class Announcement(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=300)
    starts: date
    ends: date | None = None
    created_by: str
    created_at: str

    def active(self, today: date) -> bool:
        return self.starts <= today and (self.ends is None or today <= self.ends)


class FacultyStatus(BaseModel):
    state: Literal["on_leave", "out", "meeting", "in"]
    note: str | None = Field(default=None, max_length=200)
    until: datetime  # local time; the status ends then
    set_by: str
    set_at: str

    def active(self, now: datetime) -> bool:
        return now < self.until


class Closure(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
    until: date | None = None  # None: until reopened
    set_by: str
    set_at: str

    def active(self, today: date) -> bool:
        return self.until is None or today <= self.until


class InfoEdit(BaseModel):
    name: str | None = None
    about: str | None = None
    hours: str | None = None
    notes: str | None = None
    class_teacher: str | None = None


class TimetableVersion(BaseModel):
    id: str
    file: str  # in data/admin/timetables/
    original_name: str
    uploaded_by: str
    uploaded_at: str
    faculty: int
    note: str | None = None


class State(BaseModel):
    announcements: list[Announcement] = []
    faculty_status: dict[str, FacultyStatus] = {}
    closed: dict[str, Closure] = {}
    place_info: dict[str, InfoEdit] = {}
    photos: dict[str, str] = {}
    rooms_sheet: dict[str, InfoEdit] = {}  # 808-815 from the published timetable's Rooms sheet
    timetable_versions: list[TimetableVersion] = []
    timetable_current: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()


class AdminStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "timetables").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._sessions: dict[str, tuple[str, float]] = {}
        self._fails: dict[str, tuple[int, float]] = {}
        self.users: dict[str, User] = {}
        self._users_mtime = -1.0
        self._refresh_users()
        self.state = State.model_validate(self._read("state.json", dict))

    # ---------- files ----------
    def _read(self, name: str, default):
        path = self.folder / name
        if not path.exists():
            return default()
        return json.loads(path.read_text(encoding="utf-8") or "null") or default()

    def _write(self, name: str, data) -> None:
        path = self.folder / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)  # never a half-written file

    def save(self) -> None:
        with self._lock:
            self._write("state.json", self.state.model_dump(mode="json"))

    def _save_users(self) -> None:
        self._write("users.json", {k: u.model_dump() for k, u in self.users.items()})
        self._users_mtime = (self.folder / "users.json").stat().st_mtime

    def _refresh_users(self) -> None:
        """Re-read users.json when it changed on disk (a password reset from the command line while the kiosk runs)."""
        path = self.folder / "users.json"
        mtime = path.stat().st_mtime if path.exists() else 0.0
        if mtime != self._users_mtime:
            self.users = {k: User.model_validate(v) for k, v in self._read("users.json", dict).items()}
            self._users_mtime = mtime

    def log(self, user: str, action: str, detail: str = "") -> None:
        record = {"timestamp": _now(), "user": user, "action": action, "detail": detail}
        with self._lock, (self.folder / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def audit(self, limit: int = 200) -> list[dict]:
        path = self.folder / "audit.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in reversed(lines) if line.strip()]

    # ---------- accounts ----------
    def add_user(self, username: str, name: str, password: str, role: Role, faculty_id: str | None = None) -> User:
        username = username.strip().casefold()
        if not username or not username.replace(".", "").replace("-", "").replace("_", "").isalnum():
            raise ValueError("a username is letters, digits, dots, dashes or underscores")
        if len(password) < 8:
            raise ValueError("the password needs at least 8 characters")
        if role == "faculty" and not faculty_id:
            raise ValueError("a faculty account needs the faculty member it belongs to")
        with self._lock:
            if username in self.users:
                raise ValueError(f"there is already an account called {username}")
            salt = secrets.token_hex(16)
            user = User(username=username, name=name.strip() or username, role=role,
                        faculty_id=faculty_id if role == "faculty" else None,
                        salt=salt, hash=_hash(password, salt), created_at=_now())
            self.users[username] = user
            self._save_users()
        return user

    def set_password(self, username: str, password: str) -> None:
        if len(password) < 8:
            raise ValueError("the password needs at least 8 characters")
        with self._lock:
            user = self.users[username]
            salt = secrets.token_hex(16)
            self.users[username] = user.model_copy(update={"salt": salt, "hash": _hash(password, salt)})
            self._save_users()
            self._sessions = {t: s for t, s in self._sessions.items() if s[0] != username}  # sign out everywhere

    def unlock(self, username: str) -> None:
        """Forget wrong-password tries (after a password reset)."""
        self._fails.pop(username.strip().casefold(), None)

    def remove_user(self, username: str) -> None:
        with self._lock:
            if self.users[username].role == "admin" and sum(u.role == "admin" for u in self.users.values()) == 1:
                raise ValueError("this is the last admin account; add another admin first")
            del self.users[username]
            self._save_users()
            self._sessions = {t: s for t, s in self._sessions.items() if s[0] != username}

    def check_password(self, username: str, password: str) -> bool:
        user = self.users.get(username)
        return user is not None and hmac.compare_digest(user.hash, _hash(password, user.salt))

    def sign_in(self, username: str, password: str) -> str | None:
        """A session token, or None. Raises PermissionError while the account is locked."""
        username = username.strip().casefold()
        with self._lock:
            self._refresh_users()
        fails, locked_until = self._fails.get(username, (0, 0.0))
        if locked_until > time.time():
            raise PermissionError("too many wrong passwords: try again in a few minutes")
        user = self.users.get(username)
        ok = user is not None and hmac.compare_digest(user.hash, _hash(password, user.salt))
        if not ok:
            fails += 1
            self._fails[username] = (0, time.time() + LOCK_S) if fails >= MAX_FAILS else (fails, 0.0)
            return None
        self._fails.pop(username, None)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (username, time.time() + SESSION_S)
        return token

    def session_user(self, token: str | None) -> User | None:
        if not token or (s := self._sessions.get(token)) is None:
            return None
        username, expires = s
        if expires < time.time() or username not in self.users:
            self._sessions.pop(token, None)
            return None
        return self.users[username]

    def sign_out(self, token: str | None) -> None:
        self._sessions.pop(token or "", None)

    # ---------- what the kiosk shows ----------
    def announcements_now(self, today: date) -> list[Announcement]:
        return [a for a in self.state.announcements if a.active(today)]

    def status_now(self, cid: str, now: datetime) -> str | None:
        """'On leave until Fri 20 Nov' etc., while an admin-set status lasts."""
        s = self.state.faculty_status.get(cid)
        if s is None or not s.active(now):
            return None
        label = STATUS_LABELS[s.state]
        same_day = s.until.date() == now.date()
        until = s.until.strftime("%H:%M") if same_day else s.until.strftime("%a %d %b").replace(" 0", " ")
        if s.until.hour == 23 and s.until.minute == 59:
            until = "the end of today" if same_day else until
        text = f"{label} until {until}"
        return f"{text}: {s.note}" if s.note else text

    def closure_now(self, place_id: str, today: date) -> Closure | None:
        c = self.state.closed.get(place_id.casefold())
        return c if c is not None and c.active(today) else None
