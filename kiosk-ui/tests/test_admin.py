from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import openpyxl
import pytest
from fastapi.testclient import TestClient

from kiosk_ui.server import create_app
from kiosk_ui.timetable import nice_name, read_workbook

HEADER = ["Sl No", "Full Name", "Short Name", "Department", "Office Room", "Floor", "Consultation Hours",
          "Weekly Hours", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Notes"]


def workbook(path: Path, rows: list[dict], rooms: list[tuple] | None = None, header=HEADER) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "8th Floor Faculty"
    ws.append(header)
    for i, r in enumerate(rows, start=1):
        ws.append([i if h == "Sl No" else r.get(h) for h in header])
    if rooms is not None:
        rs = wb.create_sheet("Rooms")
        rs.append(["Room No", "Room Name", "Type", "Class Teacher"])
        for room in rooms:
            rs.append(list(room))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


NISHA = {"Full Name": "Dr NISHA VARGHESE", "Office Room": 874, "Floor": "Eighth Floor",
         "Consultation Hours": "Monday 1.45 to 2.45 pm", "Monday": "9.45 to 11.45 am - ML"}
SUDHAKAR = {"Full Name": "Dr SUDHAKAR T", "Office Room": 880, "Floor": "Eighth Floor",
            "Consultation Hours": "Friday 11.45 to 1.45 pm", "Tuesday": "12.45 to 1.45 pm - ML"}


class Rag:
    """Stands in for rag-core's POST /reload-kb: on success it 'rebuilds' faculty.json, like the real one."""

    def __init__(self, faculty_json: Path) -> None:
        self.faculty_json, self.fail, self.calls = faculty_json, False, 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/reload-kb" and request.method == "POST"
            self.calls += 1
            if self.fail:
                return httpx.Response(500, json={"detail": "rebuild failed: bad sheet"})
            data = json.loads(self.faculty_json.read_text(encoding="utf-8"))
            data["faculty"].append({"id": "sudhakar-t", "title": "Dr", "full_name": "Sudhakar T",
                                    "location": {"room": "880", "on_8th_floor": True}})
            self.faculty_json.write_text(json.dumps(data), encoding="utf-8")
            return httpx.Response(200, json={"status": "reloaded", "facts": 10})
        return httpx.MockTransport(handler)


@pytest.fixture
def rag(settings):
    return Rag(settings.kb.faculty_json)


@pytest.fixture
def client(settings, voice_memory, rag):
    workbook(settings.admin.timetable_file, [NISHA], rooms=[(808, "MCA classroom, semester 5, section A", "Classroom", "Dr SHIVANGI SINGH")])
    with TestClient(create_app(settings, transport=voice_memory, rag_transport=rag.transport())) as c:
        yield c


@pytest.fixture
def admin(client):
    r = client.post("/api/admin/setup", json={"username": "office", "name": "CS Office", "password": "correct horse"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    return client


def test_first_account_then_sign_in(client):
    assert client.get("/admin").status_code == 200
    assert client.get("/api/admin/setup").json()["needs_setup"] is True
    assert client.get("/api/admin/me").status_code == 401
    client.post("/api/admin/setup", json={"username": "office", "password": "correct horse"})
    assert client.post("/api/admin/setup", json={"username": "other", "password": "12345678"}).status_code == 409
    client.post("/api/admin/logout")
    assert client.get("/api/admin/timetable").status_code == 401
    assert client.post("/api/admin/login", json={"username": "office", "password": "wrong one"}).status_code == 401
    assert client.post("/api/admin/login", json={"username": "OFFICE", "password": "correct horse"}).status_code == 200
    assert client.get("/api/admin/me").json()["username"] == "office"


def test_wrong_passwords_lock_the_account_for_a_while(admin):
    admin.post("/api/admin/logout")
    for _ in range(5):
        assert admin.post("/api/admin/login", json={"username": "office", "password": "nope nope"}).status_code == 401
    assert admin.post("/api/admin/login", json={"username": "office", "password": "correct horse"}).status_code == 429


def test_passwords_are_not_stored(admin, settings):
    users = (settings.admin.data_dir / "users.json").read_text(encoding="utf-8")
    assert "correct horse" not in users and '"hash"' in users


def test_a_faculty_account_can_set_only_their_own_status(admin):
    r = admin.post("/api/admin/users", json={"username": "nisha", "name": "Nisha", "password": "nisha pass",
                                             "role": "faculty", "faculty_id": "faculty:nisha-varghese"})
    assert r.status_code == 200
    admin.post("/api/admin/logout")
    admin.post("/api/admin/login", json={"username": "nisha", "password": "nisha pass"})
    assert [f["id"] for f in admin.get("/api/admin/faculty").json()] == ["faculty:nisha-varghese"]
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    body = {"state": "meeting", "until_date": tomorrow, "note": "Board meeting"}
    assert admin.put("/api/admin/faculty/faculty:nisha-varghese/status", json=body).status_code == 200
    assert admin.put("/api/admin/faculty/faculty:jossy-p-george/status", json=body).status_code == 403
    for path in ("/api/admin/timetable", "/api/admin/users", "/api/admin/audit", "/api/admin/places"):
        assert admin.get(path).status_code == 403, path


def test_faculty_status_shows_on_the_kiosk(admin):
    friday = date.today() + timedelta(days=3)
    r = admin.put("/api/admin/faculty/faculty:nisha-varghese/status",
                  json={"state": "on_leave", "until_date": friday.isoformat()})
    assert r.status_code == 200 and r.json()["status"].startswith("On leave until")
    [person] = admin.get("/api/place/room:874").json()["staff"]
    assert person["from_office"] is True and person["now"].startswith("On leave until")
    turn = admin.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?"}).json()
    assert any(n.startswith("Dr Nisha Varghese: On leave until") for n in turn["notices"])
    assert admin.delete("/api/admin/faculty/faculty:nisha-varghese/status").status_code == 200
    assert admin.get("/api/place/room:874").json()["staff"][0]["from_office"] is False
    past = {"state": "out", "until_date": (date.today() - timedelta(days=1)).isoformat()}
    assert admin.put("/api/admin/faculty/faculty:nisha-varghese/status", json=past).status_code == 422


def test_announcements(admin):
    assert admin.get("/api/announcements").json() == []
    a = admin.post("/api/admin/announcements", json={"text": "The MCA lab is closed on Friday."}).json()
    later = (date.today() + timedelta(days=5)).isoformat()
    admin.post("/api/admin/announcements", json={"text": "Next week's seminar", "starts": later})
    assert [x["text"] for x in admin.get("/api/announcements").json()] == ["The MCA lab is closed on Friday."]
    assert admin.delete(f"/api/admin/announcements/{a['id']}").status_code == 200
    assert admin.get("/api/announcements").json() == []


def test_closed_rooms_info_edits_and_photos(admin):
    assert admin.put("/api/admin/places/room:811/closed", json={"reason": "practical exam"}).status_code == 200
    assert admin.get("/api/place/room:811").json()["closed"] == "Closed: practical exam"
    turn = admin.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?"}).json()
    assert turn["notices"] == []  # 874 is open
    admin.delete("/api/admin/places/room:811/closed")
    assert admin.get("/api/place/room:811").json()["closed"] is None

    r = admin.put("/api/admin/places/facility:ccd/info", json={"hours": "08:00-18:00", "about": "Coffee and snacks."})
    assert r.status_code == 200
    card = admin.get("/api/place/facility:ccd").json()
    assert card["hours"] == "08:00-18:00" and card["about"] == "Coffee and snacks." and card["name"] == "CCD café"

    jpg = b"\xff\xd8\xff\xe0" + b"0" * 100
    r = admin.post("/api/admin/places/room:875/photo", files={"photo": ("door.jpg", jpg, "image/jpeg")})
    assert r.status_code == 200
    name = r.json()["photo"]
    assert admin.get(f"/photos/{name}").status_code == 200
    assert admin.get("/api/place/room:875").json()["photo"] == name
    assert admin.get("/api/route/room:875").json()["step_photos"][-1] == name
    bad = admin.post("/api/admin/places/room:875/photo", files={"photo": ("x.jpg", b"not a photo", "image/jpeg")})
    assert bad.status_code == 415
    assert admin.post("/api/admin/places/room:999/photo", files={"photo": ("door.jpg", jpg, "image/jpeg")}).status_code == 404
    log = admin.get("/api/admin/audit").json()
    assert {"closed a place", "reopened a place", "edited place info", "uploaded a photo"} <= {e["action"] for e in log}


def test_a_new_timetable_is_checked_previewed_published_and_can_be_rolled_back(admin, settings, rag, tmp_path):
    changed = dict(NISHA, **{"Office Room": 873, "Monday": "10 to 12 pm - ML LAB"})
    new = workbook(tmp_path / "even-sem.xlsx", [changed, SUDHAKAR],
                   rooms=[(808, "MCA classroom, semester 6, section A", "Classroom", "Dr NIZAR BANU P K")])
    with new.open("rb") as fh:
        r = admin.post("/api/admin/timetable/upload", files={"file": ("even-sem.xlsx", fh, "application/octet-stream")}).json()
    assert r["problems"] == []
    c = r["changes"]
    assert c["faculty_before"] == 1 and c["faculty_after"] == 2 and c["added"] == ["Dr Sudhakar T"]
    assert {(x["what"], x["before"], x["after"]) for x in c["changed"]} == {
        ("Office Room", "874", "873"), ("Monday", "9.45 to 11.45 am - ML", "10 to 12 pm - ML LAB")}
    assert c["rooms"][0]["after"] == "MCA classroom, semester 6, section A · Dr Nizar Banu P K"
    assert read_workbook(settings.admin.timetable_file).faculty["Dr NISHA VARGHESE"]["Office Room"] == "874"  # not yet

    p = admin.post("/api/admin/timetable/publish", json={"pending": r["pending"], "note": "Even semester"})
    assert p.status_code == 200 and rag.calls == 1
    assert read_workbook(settings.admin.timetable_file).faculty["Dr NISHA VARGHESE"]["Office Room"] == "873"
    # the kiosk re-read the faculty sheet rag-core rebuilt, and the Rooms sheet's class teacher
    assert [s["name"] for s in admin.get("/api/place/room:880").json()["staff"]] == ["Dr Sudhakar T"]
    assert admin.get("/api/place/room:808").json()["class_teacher"] == "Dr Nizar Banu P K"
    t = admin.get("/api/admin/timetable").json()
    assert [v["id"] for v in t["versions"]][-1] == "original" and t["current"] == p.json()["version"]["id"]
    assert t["faculty"] == 2
    assert admin.get(f"/api/admin/timetable/download/{t['current']}").content == new.read_bytes()

    # roll back to the timetable from before the dashboard
    assert admin.post("/api/admin/timetable/restore", json={"version": "original"}).status_code == 200
    assert read_workbook(settings.admin.timetable_file).faculty["Dr NISHA VARGHESE"]["Office Room"] == "874"
    assert admin.get("/api/admin/timetable").json()["current"] == "original"
    actions = [e["action"] for e in admin.get("/api/admin/audit").json()]
    assert "published a timetable" in actions and "went back to an earlier timetable" in actions


def test_rag_core_failing_keeps_the_old_timetable(admin, settings, rag, tmp_path):
    new = workbook(tmp_path / "new.xlsx", [SUDHAKAR])
    r = admin.post("/api/admin/timetable/upload", files={"file": ("new.xlsx", new.read_bytes(), "x")}).json()
    rag.fail = True
    p = admin.post("/api/admin/timetable/publish", json={"pending": r["pending"]})
    assert p.status_code == 502 and "previous timetable is still in use" in p.json()["detail"]
    assert set(read_workbook(settings.admin.timetable_file).faculty) == {"Dr NISHA VARGHESE"}


def test_a_broken_file_is_stopped_before_publishing(admin, tmp_path):
    bad = workbook(tmp_path / "bad.xlsx", [NISHA, NISHA], header=[h for h in HEADER if h != "Friday"])
    r = admin.post("/api/admin/timetable/upload", files={"file": ("bad.xlsx", bad.read_bytes(), "x")}).json()
    assert any("Friday" in p for p in r["problems"])
    assert admin.post("/api/admin/timetable/publish", json={"pending": r["pending"]}).status_code == 422
    twice = workbook(tmp_path / "twice.xlsx", [NISHA, NISHA])
    r = admin.post("/api/admin/timetable/upload", files={"file": ("twice.xlsx", twice.read_bytes(), "x")}).json()
    assert any("appears twice" in p for p in r["problems"])
    assert admin.post("/api/admin/timetable/upload", files={"file": ("notes.txt", b"hi", "text/plain")}).status_code == 415
    junk = admin.post("/api/admin/timetable/upload", files={"file": ("junk.xlsx", b"not excel", "x")}).json()
    assert junk["problems"] and "Excel" in junk["problems"][0]


def test_names_from_the_sheet_read_naturally():
    assert nice_name("Dr SHIVANGI SINGH") == "Dr Shivangi Singh"
    assert nice_name("Dr NIZAR BANU P K") == "Dr Nizar Banu P K"
    assert nice_name("Dr Helen K Joy") == "Dr Helen K Joy"


def test_a_password_reset_from_the_command_line_works_while_the_kiosk_runs(admin, settings):
    from kiosk_ui.admin_store import AdminStore

    AdminStore(settings.admin.data_dir).set_password("office", "brand new pass")  # what reset-password does
    admin.post("/api/admin/logout")
    assert admin.post("/api/admin/login", json={"username": "office", "password": "correct horse"}).status_code == 401
    assert admin.post("/api/admin/login", json={"username": "office", "password": "brand new pass"}).status_code == 200


def test_forgot_password_on_the_kiosk_computer_only(admin, settings, voice_memory, rag):
    admin.post("/api/admin/logout")
    for _ in range(5):  # locked out
        admin.post("/api/admin/login", json={"username": "office", "password": "forgot it"})
    assert admin.post("/api/admin/reset-local", json={"username": "nobody", "password": "whatever1"}).status_code == 404
    assert admin.post("/api/admin/reset-local", json={"username": "office", "password": "short"}).status_code == 422
    assert admin.post("/api/admin/reset-local", json={"username": "Office", "password": "remembered now"}).status_code == 200
    assert admin.post("/api/admin/login", json={"username": "office", "password": "remembered now"}).status_code == 200
    assert "reset their password on the kiosk computer" in [e["action"] for e in admin.get("/api/admin/audit").json()]
    # from another computer on the Wi-Fi: refused
    with TestClient(create_app(settings, transport=voice_memory, rag_transport=rag.transport()),
                    client=("192.168.1.50", 50000)) as other:
        r = other.post("/api/admin/reset-local", json={"username": "office", "password": "taken over!"})
        assert r.status_code == 403
        assert other.get("/api/admin/setup").json()["local"] is False
