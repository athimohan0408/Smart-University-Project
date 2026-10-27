from __future__ import annotations

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from kiosk_ui.eventlog import read_jsonl
from kiosk_ui.server import SORRY, create_app


@pytest.fixture
def client(settings, voice_memory):
    with TestClient(create_app(settings, transport=voice_memory)) as c:
        yield c


def test_pages_and_static_files_are_served(client):
    assert "8th Floor" in client.get("/").text
    assert "analytics" in client.get("/dashboard").text.lower()
    assert client.get("/static/kiosk.js").status_code == 200
    assert client.get("/static/map.js").status_code == 200


def test_turn_on_this_floor_returns_a_route_and_logs_it(client, settings, voice_memory):
    r = client.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?"}).json()
    assert r["turn"]["outcome"] == "answered"
    assert [x["place_id"] for x in r["routes"]] == ["room:874"]
    assert r["routes"][0]["steps"][-1] == "Room 874 is on your right."
    assert voice_memory.calls[-1] == ("/turn", {"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?",
                                                "speak": settings.kiosk.speak})
    [event] = read_jsonl(settings.logs.ui_events)
    assert event["event"] == "turn" and event["route_shown"] is True and event["room_ids"] == ["room:874"]
    assert event["kiosk_id"] == settings.kiosk.id and event["round_trip_ms"] >= 0
    assert "ip" not in event and "client" not in event


def test_room_on_another_floor_has_no_route(client):
    r = client.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Ashwini Patil?"}).json()
    assert r["routes"] == [] and r["off_map_rooms"] == ["room:607a"]


def test_chosen_option_is_logged(client, settings):
    client.post("/api/turn", json={"session_id": "s1", "text": "Dr Jossy P George", "chosen_option": True})
    events = [e["event"] for e in read_jsonl(settings.logs.ui_events)]
    assert events == ["option_chosen", "turn"]


def test_audio_turn_is_forwarded(client, voice_memory):
    r = client.post("/api/turn/audio", data={"session_id": "s1"},
                    files={"audio": ("speech.wav", b"RIFF....WAVE", "audio/wav")}).json()
    assert r["routes"][0]["place_id"] == "room:874"
    assert voice_memory.calls[-1][0] == "/turn/audio"


def test_voice_memory_down_gives_a_polite_answer(settings):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    with TestClient(create_app(settings, transport=httpx.MockTransport(down))) as c:
        r = c.post("/api/turn", json={"session_id": "s1", "text": "where is room 874"}).json()
        assert r["turn"]["answer"] == SORRY and r["turn"]["escalate"] is True
        assert c.post("/api/receptionist", json={"session_id": "s1"}).json()["status"] == "kiosk_upstream_error"
        assert c.get("/api/health").json()["voice_memory"].startswith("unreachable")
    assert [e["event"] for e in read_jsonl(settings.logs.ui_events)] == ["error", "receptionist_button", "error"]


def test_receptionist_button_and_reset(client, settings, voice_memory):
    assert client.post("/api/receptionist", json={"session_id": "s1"}).json()["ticket_id"] == "T1"
    assert client.post("/api/session/reset", json={"session_id": "s1"}).json() == {"status": "reset"}
    assert [c[0] for c in voice_memory.calls] == ["/receptionist", "/session/reset"]
    assert [e["event"] for e in read_jsonl(settings.logs.ui_events)] == ["receptionist_button", "session_reset"]


def test_map_route_and_config_endpoints(client):
    m = client.get("/api/map").json()
    assert m["verified"] is False and len(m["places"]) == 21 and m["start_point"] == [500.0, 250.0]
    assert client.get("/api/route/room:880").json()["place_id"] == "room:880"
    assert client.get("/api/route/room:607a").status_code == 404
    assert client.get("/api/config").json()["map_verified"] is False


def test_analytics_endpoint_reads_the_kiosk_log(client):
    client.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?"})
    client.post("/api/turn", json={"session_id": "s1", "text": "What is her phone number?"})
    d = client.get("/api/analytics").json()
    assert d["totals"]["questions"] == 2 and d["totals"]["escalated"] == 1 and d["totals"]["routes_shown"] == 1
    assert client.get("/api/analytics?days=0").status_code == 422


def test_empty_question_is_rejected(client):
    assert client.post("/api/turn", json={"session_id": "s1", "text": ""}).status_code == 422


def test_person_on_this_floor_gets_a_route_from_the_answer(client, settings):
    r = client.post("/api/turn", json={"session_id": "s1", "text": "where is dr sudhakar's cabin"}).json()
    assert r["turn"]["room_ids"] == ["room:874"]
    assert [x["place_id"] for x in r["routes"]] == ["room:874"]
    assert read_jsonl(settings.logs.ui_events)[-1]["room_ids"] == ["room:874"]


def test_clarification_gets_buttons(client):
    r = client.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr George?"}).json()
    assert r["turn"]["options"] == ["Dr Jossy P George", "Dr Naived George Eapen"]


def test_analytics_names_people_and_their_offices(client):
    client.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?"})
    [dest] = client.get("/api/analytics").json()["top_destinations"]
    assert dest == {"id": "faculty:nisha-varghese", "count": 1, "name": "Dr Nisha Varghese", "room": "room:874"}


def test_pages_link_versioned_static_files(client):
    html = client.get("/").text
    assert re.search(r'/static/kiosk\.js\?v=[0-9a-f]+"', html) and re.search(r'/static/common\.css\?v=[0-9a-f]+"', html)
    assert client.get("/").headers["cache-control"] == "no-cache"
