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
    assert m["verified"] is False and len(m["places"]) == 41 and m["start_point"] == [575.0, 250.0]
    assert any(p["id"] == "area:staff-room" and p["label_at"] for p in m["places"])
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


def test_a_room_only_the_floor_map_knows_is_answered_from_the_map(client, settings):
    r = client.post("/api/turn", json={"session_id": "s1", "text": "where is room 815?"}).json()
    t = r["turn"]
    assert t["answer"] == ("Room 815 is here on the 8th floor, on the north-east side of the floor. "
                           "I've shown you the way on the map.")
    assert t["outcome"] == "answered" and t["escalate"] is False and t["options"] == []
    assert t["answered_by"] == "kiosk_floor_map" and t["kb_escalate_reason"] == "not_in_kb"
    assert [x["place_id"] for x in r["routes"]] == ["room:815"]
    assert read_jsonl(settings.logs.ui_events)[-1]["answered_by"] == "kiosk_floor_map"
    lab = client.post("/api/turn", json={"session_id": "s1", "text": "how do I get to room 811"}).json()["turn"]
    assert lab["answer"].startswith("Room 811, the MCA Lab, is here on the 8th floor, on the south-east side")


def test_rooms_not_on_the_map_still_go_to_the_receptionist(client):
    t = client.post("/api/turn", json={"session_id": "s1", "text": "where is room 999?"}).json()["turn"]
    assert t["escalate"] is True and "can't find room 999" in t["answer"]


def test_other_questions_that_mention_a_room_are_not_replaced(client):
    # an escalation about a phone number stays an escalation, even with a room number in it
    t = client.post("/api/turn", json={"session_id": "s1", "text": "phone number for room 815"}).json()["turn"]
    assert t["escalate"] is True and "answered_by" not in t


def test_photos_are_served_and_routes_carry_them(client):
    r = client.get("/api/route/room:874").json()
    assert r["step_photos"][-1] == "room-874.jpg"
    img = client.get("/photos/room-874.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get("/photos/nope.jpg").status_code == 404


def test_speak_endpoint(client):
    # the test settings have the kiosk voice off, so the browser speaks instead
    assert client.get("/api/speak", params={"text": "Turn left and walk about 4 m."}).status_code == 404
    assert client.get("/api/speak", params={"text": ""}).status_code == 422
    assert client.get("/api/speak", params={"text": "x" * 301}).status_code == 422


def test_route_carries_the_walkthrough(client):
    r = client.get("/api/route/room:874").json()
    assert r["step_kinds"] == ["start", "left", "left", "arrive-right"]
    assert r["step_spans"][0][0] == 0 and r["step_spans"][-1][1] == len(r["points"]) - 1


def test_kiosk_page_has_the_walkthrough(client):
    html = client.get("/").text
    for needed in ('id="nav"', 'id="nav-start"', 'id="nav-play"', 'id="nav-map"', 'id="nav-photo"'):
        assert needed in html


def test_routes_from_a_lift_and_the_nearest_washroom(client):
    r = client.get("/api/route/room:874", params={"from": "lift-north"}).json()
    assert r["steps"][0].startswith("Step out of the north lift") and r["start_id"] == "lift-north"
    assert client.get("/api/route/room:874", params={"from": "rooftop"}).status_code == 404
    near = client.get("/api/nearest/gents", params={"from": "lift-west"}).json()
    assert near["place_id"] == "facility:gents-south"
    assert client.get("/api/nearest/helipad").status_code == 404
    m = client.get("/api/map", params={"from": "lift-west"}).json()
    assert m["start_name"] == "the west lift" and m["start_point"] == [155.0, 355.0]
    assert [s["id"] for s in m["starts"]][:2] == ["kiosk", "lift-north"]


def test_place_cards(client):
    places = client.get("/api/places").json()
    assert any(p["id"] == "room:874" and p["people"] == ["Dr Nisha Varghese"] for p in places)
    card = client.get("/api/place/room:874").json()
    assert card["staff"][0]["name"] == "Dr Nisha Varghese" and card["where"].startswith("inside")
    assert client.get("/api/place/room:808").json()["class_teacher"] == "Dr Shivangi Singh"
    assert client.get("/api/place/room:999").status_code == 404


def test_feedback_is_logged_without_personal_data(client, settings):
    assert client.post("/api/feedback", json={"session_id": "s1", "turn_id": "t1", "helpful": False}).json() == {"status": "thanks"}
    [event] = read_jsonl(settings.logs.ui_events)
    assert event["event"] == "feedback" and event["helpful"] is False and event["turn_id"] == "t1"
    assert client.post("/api/feedback", json={"session_id": "s1"}).status_code == 422


def test_phone_page_qr_stickers_and_3d_files_are_served(client):
    go = client.get("/go")
    assert go.status_code == 200 and "Find your way" in go.text and re.search(r"/static/go\.js\?v=\w+", go.text)
    assert "QR stickers" in client.get("/qr").text
    for name in ("go.js", "go.css", "qr.js", "explore.js", "view3d.js", "floor3d.js"):
        assert client.get(f"/static/{name}").status_code == 200, name
    kiosk = client.get("/").text
    for needed in ('id="explore-overlay"', 'id="place-overlay"', 'id="view3d"', 'id="map3d"', 'id="a11y"', "importmap",
                   'id="phone-qr"', 'id="codes-overlay"'):
        assert needed in kiosk, needed
    assert client.get("/api/config").json()["public_url"] is None


def test_qr_codes_use_the_wifi_address_when_the_server_is_on_the_network(settings, voice_memory, monkeypatch):
    import kiosk_ui.server as server

    monkeypatch.setattr(server, "lan_address", lambda: "192.168.1.20")
    on_network = settings.model_copy(update={"api": settings.api.model_copy(update={"host": "0.0.0.0"})})
    with TestClient(create_app(on_network, transport=voice_memory)) as c:
        assert c.get("/api/config").json()["public_url"] == f"http://192.168.1.20:{settings.api.port}"
    fixed = on_network.model_copy(update={"kiosk": settings.kiosk.model_copy(update={"public_url": "http://kiosk.local:8002/"})})
    with TestClient(create_app(fixed, transport=voice_memory)) as c:
        assert c.get("/api/config").json()["public_url"] == "http://kiosk.local:8002"
