"""The admin dashboard's API, under ``/api/admin`` (the page is ``/admin``). Every call needs a signed-in account
(an HttpOnly session cookie); most need the ``admin`` role, and a ``faculty`` account can set only its own status.

The very first admin account is created from the kiosk computer itself (``/admin`` on 127.0.0.1 offers it while
there are no accounts). Forgotten password? "Forgot password?" on the sign-in page, again only on the kiosk computer
itself (from anywhere else it is refused), or ``python -m kiosk_ui.admin_api reset-password <username>`` there.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from kiosk_ui.admin_store import STATUS_LABELS, AdminStore, Announcement, Closure, FacultyStatus, InfoEdit, Role, User
from kiosk_ui.timetable import Timetables

COOKIE = "kiosk_admin"
LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}  # "testclient": FastAPI's TestClient
MAX_XLSX = 15 * 1024 * 1024
MAX_PHOTO = 6 * 1024 * 1024
_PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class LocalReset(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=200)


class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=100)
    password: str = Field(min_length=8, max_length=200)
    role: Role = "admin"
    faculty_id: str | None = None


class Password(BaseModel):
    password: str = Field(min_length=8, max_length=200)
    current: str | None = None


class StatusIn(BaseModel):
    state: str
    until_date: date
    until_time: time | None = None  # None: until the end of that day
    note: str | None = Field(default=None, max_length=200)


class AnnouncementIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    starts: date | None = None
    ends: date | None = None


class ClosureIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
    until: date | None = None


class PublishIn(BaseModel):
    pending: str
    note: str | None = Field(default=None, max_length=200)


class VersionIn(BaseModel):
    version: str


def _clean(text: str | None) -> str | None:
    text = " ".join((text or "").split())
    return text or None


def create_admin_router(*, store: AdminStore, settings, kb, fmap, refresh_kiosk: Callable[[], None],
                        rag_transport: httpx.BaseTransport | None = None) -> APIRouter:
    """kb: the server's live faculty directory (``kb.directory``); refresh_kiosk: re-read faculty.json after
    rag-core rebuilt it."""
    router = APIRouter(prefix="/api/admin", include_in_schema=False)
    photos = settings.admin.data_dir / "photos"  # uploads stay with the admin data (not in git); served at /photos/

    def reload_rag() -> None:
        with httpx.Client(base_url=settings.admin.rag_url, timeout=settings.admin.reload_timeout_s,
                          transport=rag_transport) as client:
            try:
                r = client.post("/reload-kb")
            except httpx.HTTPError as exc:
                raise RuntimeError(f"rag-core isn't reachable at {settings.admin.rag_url} "
                                   f"({type(exc).__name__}). Start rag-core and try again.") from exc
            if r.status_code != 200:
                detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text
                raise RuntimeError(f"rag-core couldn't load this timetable: {detail}")
        refresh_kiosk()

    timetables = Timetables(store, settings.admin.timetable_file, reload_rag)

    # ---------- who is asking ----------
    def current_user(request: Request) -> User:
        user = store.session_user(request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(status_code=401, detail="Please sign in.")
        return user

    def admin_only(user: User = Depends(current_user)) -> User:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="Only the department office (an admin account) can do this.")
        return user

    def me(user: User) -> dict:
        return {"username": user.username, "name": user.name, "role": user.role, "faculty_id": user.faculty_id}

    # ---------- signing in ----------
    @router.get("/setup")
    def setup_needed(request: Request) -> dict:
        return {"needs_setup": not store.users, "local": (request.client.host if request.client else "") in LOCAL}

    @router.post("/setup")
    def setup(body: NewUser, request: Request, response: Response) -> dict:
        if store.users:
            raise HTTPException(status_code=409, detail="The first account already exists: please sign in.")
        if (request.client.host if request.client else "") not in LOCAL:
            raise HTTPException(status_code=403, detail="Create the first account on the kiosk computer itself.")
        try:
            user = store.add_user(body.username, body.name, body.password, "admin")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.log(user.username, "created the first admin account")
        return login(Login(username=body.username, password=body.password), response)

    @router.post("/reset-local")
    def reset_local(body: LocalReset, request: Request) -> dict:
        """Forgotten password: a new one, set on the kiosk computer itself (like the first account). From any other
        computer this is refused: there, an admin sets a new password from the Accounts tab."""
        if (request.client.host if request.client else "") not in LOCAL:
            raise HTTPException(status_code=403, detail="Reset a password on the kiosk computer itself, "
                                "or ask the department office to set a new one for you.")
        username = body.username.strip().casefold()
        if username not in store.users:
            raise HTTPException(status_code=404, detail="There's no account with that username.")
        store.set_password(username, body.password)
        store.unlock(username)
        store.log(username, "reset their password on the kiosk computer")
        return {"status": "changed"}

    @router.post("/login")
    def login(body: Login, response: Response) -> dict:
        try:
            token = store.sign_in(body.username, body.password)
        except PermissionError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if token is None:
            raise HTTPException(status_code=401, detail="That username and password don't match.")
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=8 * 3600, path="/")
        user = store.session_user(token)
        store.log(user.username, "signed in")
        return me(user)

    @router.post("/logout")
    def logout(request: Request, response: Response) -> dict:
        store.sign_out(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"status": "signed out"}

    @router.get("/me")
    def whoami(user: User = Depends(current_user)) -> dict:
        return me(user)

    @router.post("/me/password")
    def own_password(body: Password, user: User = Depends(current_user)) -> dict:
        if not body.current or not store.check_password(user.username, body.current):
            raise HTTPException(status_code=401, detail="Your current password isn't right.")
        store.set_password(user.username, body.password)
        store.log(user.username, "changed their password")
        return {"status": "changed: please sign in again"}

    # ---------- accounts ----------
    @router.get("/users")
    def users(_: User = Depends(admin_only)) -> list[dict]:
        return [{"username": u.username, "name": u.name, "role": u.role, "faculty_id": u.faculty_id,
                 "faculty_name": kb.directory.display.get(u.faculty_id or "")} for u in store.users.values()]

    @router.post("/users")
    def add_user(body: NewUser, user: User = Depends(admin_only)) -> dict:
        if body.role == "faculty" and body.faculty_id not in kb.directory.display:
            raise HTTPException(status_code=422, detail="Pick the faculty member this account belongs to.")
        try:
            new = store.add_user(body.username, body.name, body.password, body.role, body.faculty_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.log(user.username, "added an account", f"{new.username} ({new.role})")
        return {"username": new.username}

    @router.post("/users/{username}/password")
    def reset_password(username: str, body: Password, user: User = Depends(admin_only)) -> dict:
        if username not in store.users:
            raise HTTPException(status_code=404, detail="No such account.")
        store.set_password(username, body.password)
        store.log(user.username, "set a new password", username)
        return {"status": "changed"}

    @router.delete("/users/{username}")
    def remove_user(username: str, user: User = Depends(admin_only)) -> dict:
        if username not in store.users:
            raise HTTPException(status_code=404, detail="No such account.")
        try:
            store.remove_user(username)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store.log(user.username, "removed an account", username)
        return {"status": "removed"}

    # ---------- faculty status ----------
    @router.get("/faculty")
    def faculty(user: User = Depends(current_user)) -> list[dict]:
        now = datetime.now()
        ids = sorted(kb.directory.display, key=lambda c: kb.directory.names[c])
        if user.role == "faculty":
            ids = [c for c in ids if c == user.faculty_id]
        out = []
        for cid in ids:
            s = store.state.faculty_status.get(cid)
            active = s is not None and s.active(now)
            out.append({"id": cid, "name": kb.directory.display[cid], "room": kb.directory.rooms.get(cid),
                        "status": store.status_now(cid, now), "state": s.state if active else None,
                        "timetable_now": kb.directory.status(cid, now),
                        "next_consultation": kb.directory.next_consultation(cid, now)})
        return out

    @router.put("/faculty/{cid}/status")
    def set_status(cid: str, body: StatusIn, user: User = Depends(current_user)) -> dict:
        if user.role != "admin" and user.faculty_id != cid:
            raise HTTPException(status_code=403, detail="You can set only your own status.")
        if cid not in kb.directory.display:
            raise HTTPException(status_code=404, detail="No such faculty member.")
        if body.state not in STATUS_LABELS:
            raise HTTPException(status_code=422, detail=f"state is one of {', '.join(STATUS_LABELS)}")
        until = datetime.combine(body.until_date, body.until_time or time(23, 59))
        if until <= datetime.now():
            raise HTTPException(status_code=422, detail="That time has already passed.")
        store.state.faculty_status[cid] = FacultyStatus(state=body.state, note=_clean(body.note), until=until,
                                                        set_by=user.username, set_at=_utc())
        store.save()
        store.log(user.username, "set a faculty status",
                  f"{kb.directory.display[cid]}: {STATUS_LABELS[body.state]} until {until:%Y-%m-%d %H:%M}")
        return {"status": store.status_now(cid, datetime.now())}

    @router.delete("/faculty/{cid}/status")
    def clear_status(cid: str, user: User = Depends(current_user)) -> dict:
        if user.role != "admin" and user.faculty_id != cid:
            raise HTTPException(status_code=403, detail="You can set only your own status.")
        if store.state.faculty_status.pop(cid, None) is not None:
            store.save()
            store.log(user.username, "cleared a faculty status", kb.directory.display.get(cid, cid))
        return {"status": None}

    # ---------- announcements ----------
    @router.get("/announcements")
    def announcements(_: User = Depends(admin_only)) -> list[Announcement]:
        return sorted(store.state.announcements, key=lambda a: (a.starts, a.created_at), reverse=True)

    @router.post("/announcements")
    def add_announcement(body: AnnouncementIn, user: User = Depends(admin_only)) -> Announcement:
        starts = body.starts or date.today()
        if body.ends and body.ends < starts:
            raise HTTPException(status_code=422, detail="It ends before it starts.")
        a = Announcement(id=uuid.uuid4().hex[:10], text=" ".join(body.text.split()), starts=starts, ends=body.ends,
                         created_by=user.username, created_at=_utc())
        store.state.announcements.append(a)
        store.save()
        store.log(user.username, "added an announcement", a.text)
        return a

    @router.delete("/announcements/{aid}")
    def remove_announcement(aid: str, user: User = Depends(admin_only)) -> dict:
        before = len(store.state.announcements)
        gone = [a for a in store.state.announcements if a.id == aid]
        store.state.announcements = [a for a in store.state.announcements if a.id != aid]
        if len(store.state.announcements) == before:
            raise HTTPException(status_code=404, detail="No such announcement.")
        store.save()
        store.log(user.username, "removed an announcement", gone[0].text)
        return {"status": "removed"}

    # ---------- places: info, closed, photos ----------
    def place_or_404(pid: str):
        if (place := fmap.place(pid)) is None:
            raise HTTPException(status_code=404, detail="That place isn't on the map.")
        return place

    @router.get("/places")
    def places(_: User = Depends(admin_only)) -> list[dict]:
        today = date.today()
        out = []
        for p in fmap.places:
            if p.kind == "hall":
                continue
            card = kb.book.card(p.id)
            closed = store.closure_now(p.id, today)
            out.append({"id": p.id, "label": p.label, "name": card.name, "group": card.group,
                        "info": {k: getattr(card, k) for k in ("name", "about", "hours", "notes", "class_teacher")},
                        "edited": p.id in store.state.place_info, "photo": card.photo,
                        "closed": closed.model_dump(mode="json") if closed else None})
        return out

    @router.put("/places/{pid}/info")
    def edit_info(pid: str, body: InfoEdit, user: User = Depends(admin_only)) -> dict:
        place = place_or_404(pid)
        edit = InfoEdit(**{k: _clean(v) for k, v in body.model_dump().items()})
        store.state.place_info[place.id] = edit
        store.save()
        store.log(user.username, "edited place info", place.id)
        return {"status": "saved"}

    @router.delete("/places/{pid}/info")
    def reset_info(pid: str, user: User = Depends(admin_only)) -> dict:
        place = place_or_404(pid)
        if store.state.place_info.pop(place.id, None) is not None:
            store.save()
            store.log(user.username, "reset place info", place.id)
        return {"status": "reset"}

    @router.put("/places/{pid}/closed")
    def close_place(pid: str, body: ClosureIn, user: User = Depends(admin_only)) -> dict:
        place = place_or_404(pid)
        store.state.closed[place.id] = Closure(reason=" ".join(body.reason.split()), until=body.until,
                                               set_by=user.username, set_at=_utc())
        store.save()
        store.log(user.username, "closed a place", f"{place.id}: {body.reason}")
        return {"status": "closed"}

    @router.delete("/places/{pid}/closed")
    def reopen_place(pid: str, user: User = Depends(admin_only)) -> dict:
        place = place_or_404(pid)
        if store.state.closed.pop(place.id, None) is not None:
            store.save()
            store.log(user.username, "reopened a place", place.id)
        return {"status": "open"}

    @router.post("/places/{pid}/photo")
    async def upload_photo(pid: str, photo: UploadFile = File(...), user: User = Depends(admin_only)) -> dict:
        place = place_or_404(pid)
        ext = _PHOTO_TYPES.get(photo.content_type or "")
        if ext is None:
            raise HTTPException(status_code=415, detail="Send a JPG, PNG or WebP photo.")
        data = await photo.read()
        if len(data) > MAX_PHOTO:
            raise HTTPException(status_code=413, detail="That photo is too large (6 MB at most).")
        if not _looks_like_image(data):
            raise HTTPException(status_code=415, detail="That file isn't a photo.")
        name = f"admin-{re.sub(r'[^a-z0-9]+', '-', place.id.casefold())}-{datetime.now():%Y%m%d%H%M%S}{ext}"
        photos.mkdir(parents=True, exist_ok=True)
        (photos / name).write_bytes(data)
        store.state.photos[place.id] = name
        store.save()
        store.log(user.username, "uploaded a photo", f"{place.id}: {name}")
        return {"photo": name}

    @router.delete("/places/{pid}/photo")
    def remove_photo(pid: str, user: User = Depends(admin_only)) -> dict:
        place = place_or_404(pid)
        if store.state.photos.pop(place.id, None) is not None:
            store.save()
            store.log(user.username, "removed an uploaded photo", place.id)
        return {"status": "removed"}

    # ---------- the timetable ----------
    @router.get("/timetable")
    def timetable(_: User = Depends(admin_only)) -> dict:
        target = settings.admin.timetable_file
        current = timetables.current()
        return {"file": target.name, "exists": target.exists(),
                "modified": datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
                if target.exists() else None,
                "faculty": len(current.faculty) if current else 0,
                "current": store.state.timetable_current,
                "versions": [v.model_dump() for v in reversed(store.state.timetable_versions)]}

    @router.post("/timetable/upload")
    async def upload_timetable(file: UploadFile = File(...), _: User = Depends(admin_only)) -> dict:
        if not (file.filename or "").lower().endswith(".xlsx"):
            raise HTTPException(status_code=415, detail="Upload the timetable as an Excel .xlsx workbook.")
        data = await file.read()
        if len(data) > MAX_XLSX:
            raise HTTPException(status_code=413, detail="That file is too large (15 MB at most).")
        pending, book, changes = timetables.stage(data, Path(file.filename).name)
        return {"pending": pending, "problems": book.problems, "warnings": book.warnings, "changes": changes}

    @router.post("/timetable/publish")
    def publish(body: PublishIn, user: User = Depends(admin_only)) -> dict:
        try:
            version = timetables.publish(body.pending, user.username, body.note)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="That upload isn't waiting any more: upload it again.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            store.log(user.username, "tried to publish a timetable", f"failed: {exc}")
            raise HTTPException(status_code=502, detail=f"{exc} The previous timetable is still in use.") from exc
        store.log(user.username, "published a timetable", f"{version.original_name} ({version.faculty} faculty)")
        return {"status": "published", "version": version.model_dump()}

    @router.post("/timetable/discard")
    def discard(body: PublishIn, _: User = Depends(admin_only)) -> dict:
        timetables.discard(body.pending)
        return {"status": "discarded"}

    @router.post("/timetable/restore")
    def restore(body: VersionIn, user: User = Depends(admin_only)) -> dict:
        try:
            version = timetables.restore(body.version)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="No such version.") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=f"{exc} The timetable didn't change.") from exc
        store.log(user.username, "went back to an earlier timetable", f"{version.original_name} ({version.id})")
        return {"status": "restored", "version": version.model_dump()}

    @router.get("/timetable/download/{version_id}")
    def download(version_id: str, _: User = Depends(admin_only)) -> FileResponse:
        try:
            path = timetables.file(version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="No such version.") from exc
        v = next(v for v in store.state.timetable_versions if v.id == version_id)
        return FileResponse(path, filename=v.original_name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---------- the change log ----------
    @router.get("/audit")
    def audit(_: User = Depends(admin_only)) -> list[dict]:
        return store.audit()

    return router


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _looks_like_image(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass

    from kiosk_ui.config import get_settings

    parser = argparse.ArgumentParser(description="Admin accounts for the kiosk's admin dashboard.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the accounts")
    add = sub.add_parser("add-admin", help="add an admin account")
    add.add_argument("username")
    add.add_argument("--name", default="")
    reset = sub.add_parser("reset-password", help="set a new password for an account")
    reset.add_argument("username")
    args = parser.parse_args(argv)
    store = AdminStore(get_settings().admin.data_dir)
    if args.cmd == "list":
        for u in store.users.values():
            print(f"{u.username:20} {u.role:8} {u.name}")
        return 0
    password = getpass.getpass("New password (8+ characters): ")
    if args.cmd == "add-admin":
        store.add_user(args.username, args.name, password, "admin")
        store.log("(command line)", "added an account", f"{args.username} (admin)")
    else:
        if args.username not in store.users:
            print(f"No account called {args.username}.")
            return 1
        store.set_password(args.username, password)
        store.log("(command line)", "set a new password", args.username)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
