"""Routes that start somewhere other than the kiosk ("from the lift"), "Where am I?", and the wake settings."""

from __future__ import annotations

import json
import subprocess
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from kiosk_ui.config import MODULE_ROOT
from kiosk_ui.eventlog import read_jsonl
from kiosk_ui.navigation import FloorMap
from kiosk_ui.server import create_app
from tests.conftest import fake_turn

MAP = MODULE_ROOT / "data" / "floor_map.yaml"


@pytest.fixture(scope="module")
def fmap() -> FloorMap:
    return FloorMap.load(MAP)


# ----------------------------------------------------------------------------- navigation


def test_route_from_a_lift_starts_at_the_lift_and_names_a_compass_direction(fmap):
    route = fmap.route("room:874", "facility:lift-north")
    assert route.origin_id == "facility:lift-north" and route.origin_name == "the north lift"
    assert route.steps[0].startswith("From the north lift, head ")  # no "turn left": nobody knows which way they face
    assert route.steps[0].split("head ")[1].split(" ")[0] in ("north", "south", "east", "west")
    assert route.steps[-1].startswith("Room 874 is on your")
    lift = fmap.nodes["lift_n"]
    assert route.points[0] == (lift.x, lift.y)
    assert route.step_spans[0][0] == 0 and route.step_spans[-1][1] == len(route.points) - 1


@pytest.mark.parametrize("origin", ["facility:lift-west", "facility:lift-south", "facility:stairs-north",
                                    "facility:stairs-south", "facility:ccd", "facility:ladies-east", "facility:gents-south",
                                    "place:faculty-lounge", "room:871", "room:811", "room:808"])
def test_every_start_the_map_offers_reaches_every_room(fmap, origin):
    for number in (871, 874, 890, 808, 815):
        route = fmap.route(f"room:{number}", origin)
        assert route is not None and route.steps, (origin, number)
        assert route.steps[0].startswith(f"From {fmap.origin(origin).origin_name}, ") or route.steps[0].startswith("You are")


def test_without_an_origin_nothing_changes(fmap):
    plain = fmap.route("room:874")
    assert plain.origin_id is None and plain.origin_name is None
    assert plain.steps[0].startswith("From the kiosk, ")
    assert fmap.route("room:874", None).steps == plain.steps


def test_an_unknown_or_unstartable_origin_falls_back_to_the_kiosk(fmap):
    assert fmap.origin("room:999") is None and fmap.origin("facility:faculty-washroom") is None
    assert fmap.route("room:874", "room:999").steps == fmap.route("room:874").steps


def test_start_and_destination_the_same_place(fmap):
    route = fmap.route("room:874", "room:874")
    assert route.steps == ["You are already at room 874."]
    neighbour = fmap.route("room:872", "room:888")  # share the doorway node
    assert len(neighbour.steps) == 1 and "room 888" in neighbour.steps[0]


def test_position_of_the_kiosk_and_of_a_named_place(fmap):
    kiosk = fmap.position()
    assert kiosk.origin_id is None and kiosk.name == "the kiosk"
    assert kiosk.point == (fmap.nodes[fmap.start].x, fmap.nodes[fmap.start].y)
    assert kiosk.text.startswith("You are at the kiosk, in Central Block, Eighth Floor. Close by: ")
    assert "staircase" in kiosk.text
    lift = fmap.position("facility:lift-west")
    assert lift.origin_id == "facility:lift-west" and lift.name == "the west lift"
    assert "the west lift" not in lift.text.split("Close by:")[1]  # it doesn't list itself as nearby


# ----------------------------------------------------------------------------- server


def vm_transport(extra: dict | None = None, outcome_for=None):
    """A voice-memory stand-in whose turns carry the given extra fields (an origin, a task outcome ...)."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        turn = fake_turn(body["text"], body["session_id"])
        turn.update(extra or {})
        if outcome_for:
            turn.update(outcome_for(body["text"]))
        return httpx.Response(200, json=turn)

    transport = httpx.MockTransport(handler)
    transport.calls = calls
    return transport


def test_a_start_point_from_voice_memory_moves_the_route(settings):
    origin = {"origin": {"place_id": "facility:lift-north", "name": "the north lift", "changed": True}}
    with TestClient(create_app(settings, transport=vm_transport(origin))) as c:
        r = c.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?"}).json()
    route = r["routes"][0]
    assert route["origin_name"] == "the north lift" and route["steps"][0].startswith("From the north lift, head ")
    assert read_jsonl(settings.logs.ui_events)[-1]["origin"] == "facility:lift-north"


def test_a_sign_qr_hint_is_the_start_unless_the_visitor_says_otherwise(settings):
    with TestClient(create_app(settings, transport=vm_transport())) as c:
        r = c.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?",
                                      "at": "facility:stairs-south"}).json()
        assert r["routes"][0]["origin_name"] == "the south staircase"
    said = {"origin": {"place_id": "facility:lift-west", "name": "the west lift", "changed": False}}
    with TestClient(create_app(settings, transport=vm_transport(said))) as c:
        r = c.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?",
                                      "at": "facility:stairs-south"}).json()
        assert r["routes"][0]["origin_name"] == "the west lift"  # what they said beats the sign
    with TestClient(create_app(settings, transport=vm_transport())) as c:  # an unknown place is ignored
        r = c.post("/api/turn", json={"session_id": "s1", "text": "Where is Dr Nisha Varghese's cabin?",
                                      "at": "room:999"}).json()
        assert r["routes"][0]["origin_name"] is None


@pytest.mark.parametrize("text", ["Where am I?", "where are we", "which floor is this?", "I'm lost", "What floor am I on"])
def test_where_am_i_is_answered_from_the_map(settings, text):
    with TestClient(create_app(settings, transport=vm_transport())) as c:
        r = c.post("/api/turn", json={"session_id": "s1", "text": text}).json()
    turn = r["turn"]
    assert turn["outcome"] == "answered" and turn["escalate"] is False and turn["answered_by"] == "kiosk_position"
    assert turn["answer"].startswith("You are at the kiosk, in Central Block, Eighth Floor.")
    assert r["position"]["name"] == "the kiosk" and r["position"]["origin_id"] is None
    assert r["routes"] == []


def test_where_am_i_after_naming_a_place_or_scanning_a_sign(settings):
    said = {"origin": {"place_id": "facility:lift-west", "name": "the west lift", "changed": False}}
    with TestClient(create_app(settings, transport=vm_transport(said))) as c:
        r = c.post("/api/turn", json={"session_id": "s1", "text": "where am I?"}).json()
    assert r["position"]["origin_id"] == "facility:lift-west" and "the west lift" in r["turn"]["answer"]
    with TestClient(create_app(settings, transport=vm_transport())) as c:
        r = c.post("/api/turn", json={"session_id": "s1", "text": "where am I?", "at": "facility:ccd"}).json()
    assert "the CCD café" in r["turn"]["answer"]


def test_ordinary_questions_are_not_mistaken_for_where_am_i(settings):
    with TestClient(create_app(settings, transport=vm_transport())) as c:
        for text in ("Where is room 815?", "Where is Dr Nisha Varghese's cabin?", "where is this room 874"):
            assert c.post("/api/turn", json={"session_id": "s1", "text": text}).json()["position"] is None


def test_position_endpoint(settings):
    with TestClient(create_app(settings, transport=vm_transport())) as c:
        assert c.get("/api/position").json()["name"] == "the kiosk"
        assert c.get("/api/position", params={"at": "facility:lift-south"}).json()["name"] == "the south lift"
        assert c.get("/api/position", params={"at": "room:999"}).status_code == 404


def test_request_dialogue_text_is_kept_out_of_the_event_log(settings):
    def task_turn(text):
        return {"outcome": "task", "intent": None, "options": ["Skip"], "answer": "May I have your name?"}

    with TestClient(create_app(settings, transport=vm_transport(outcome_for=task_turn))) as c:
        r = c.post("/api/turn", json={"session_id": "s1", "text": "my number is 9876543210"}).json()
    assert r["turn"]["options"] == ["Skip"] and r["routes"] == [] and r["position"] is None
    event = read_jsonl(settings.logs.ui_events)[-1]
    assert event["question"] == "[message or meeting request]" and "9876543210" not in json.dumps(event)


def test_config_carries_the_wake_settings(settings):
    with TestClient(create_app(settings, transport=vm_transport())) as c:
        wake = c.get("/api/config").json()["wake"]
    assert wake["enabled"] is True and wake["hold_ms"] > 0 and 0 < wake["motion_threshold"] < 1


# ----------------------------------------------------------------------------- the wake logic, without a browser

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_wake_logic_in_node():
    script = Path(__file__).parent / "js" / "wake_test.js"
    done = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_map_answer_never_replaces_a_reply_that_holds_several_questions(settings):
    parts = {"parts": [{"question": "where is room 815?", "answer": "x", "outcome": "escalated"},
                       {"question": "who teaches ML?", "answer": "ML is taught by Dr A.", "outcome": "answered"}]}
    with TestClient(create_app(settings, transport=vm_transport(parts))) as c:
        single = TestClient(create_app(settings, transport=vm_transport()))
        assert c.post("/api/turn", json={"session_id": "s", "text": "where is room 815"}).json()["turn"].get("answered_by") is None
        assert single.post("/api/turn", json={"session_id": "s", "text": "where is room 815"}).json()["turn"]["answered_by"] == "kiosk_floor_map"


# ----------------------------------------------------------------------------- facilities answered from the map


def unverified(text):
    """What voice-memory says about a place its knowledge base doesn't hold."""
    return {"answer": "I'm sorry, I can't verify that. Please check with the receptionist at the desk.", "outcome": "escalated",
            "escalate": True, "escalate_reason": "not_in_kb", "intent": "facility_info", "entities": [], "room_ids": []}


def facility(settings, text, **body):
    with TestClient(create_app(settings, transport=vm_transport(outcome_for=unverified))) as c:
        return c.post("/api/turn", json={"session_id": "s", "text": text, **body}).json()


@pytest.mark.parametrize("text,expected_place", [
    ("where is the lift", "facility:lift-north"),               # the nearest of three, from the kiosk
    ("where can I drink water", "facility:water-west"),
    ("where is the staircase", "facility:stairs-north"),
    ("how do I get to the CCD café", "facility:ccd"),
    ("is there a ladies washroom", "facility:ladies-east"),
    ("where is the gents washroom", "facility:gents-south"),
    ("where is the south lift", "facility:lift-south"),         # a direction picks that one
    ("where is the MCA lab", "room:811"),
])
def test_where_is_a_facility_is_answered_from_the_map(settings, text, expected_place):
    r = facility(settings, text)
    assert r["turn"]["outcome"] == "answered" and r["turn"]["escalate"] is False and r["turn"]["answered_by"] == "kiosk_floor_map"
    assert r["turn"]["room_ids"] == [expected_place] and [x["place_id"] for x in r["routes"]] == [expected_place]


def test_the_answer_says_which_is_nearest_how_far_and_what_else_there_is(settings):
    answer = facility(settings, "where is the lift")["turn"]["answer"]
    assert answer.startswith("The nearest lift is the north lift, about ") and "m from the kiosk" in answer
    assert "This floor also has the west lift and the south lift." in answer and "I've shown you the way on the map." in answer
    route = facility(settings, "where is the lift")["routes"][0]
    assert route["destination"] == "the north lift" and route["steps"][-1].startswith("The north lift is ")


def test_the_nearest_is_measured_from_where_the_visitor_is(settings):
    near_west = facility(settings, "where is the lift", at="facility:ccd")
    assert near_west["turn"]["room_ids"] == ["facility:lift-west"] and "from the CCD café" in near_west["turn"]["answer"]
    standing_at_it = facility(settings, "where is the lift", at="facility:lift-north")
    assert standing_at_it["turn"]["room_ids"] != ["facility:lift-north"]  # not sent to the lift they are standing at


def test_a_washroom_with_no_gender_asks_instead_of_guessing(settings):
    r = facility(settings, "where is the washroom")
    assert r["turn"]["outcome"] == "clarification" and r["turn"]["options"] == ["Where is the Gents washroom?", "Where is the Ladies washroom?"]
    assert r["routes"] == []


def test_a_place_with_no_route_yet_is_described_not_routed(settings):
    r = facility(settings, "where is the faculty washroom")
    assert r["turn"]["outcome"] == "answered" and r["routes"] == [] and "can't draw the way there" in r["turn"]["answer"]


@pytest.mark.parametrize("text", ["is there wifi", "what time does the lift close", "tell me about the canteen", "where is the library"])
def test_what_the_map_cannot_say_still_goes_to_the_receptionist(settings, text):
    r = facility(settings, text)
    assert r["turn"]["outcome"] == "escalated" and r["routes"] == []


def test_a_destination_that_is_not_a_room_is_named_for_the_screen(fmap):
    assert fmap.route("room:874").destination == "room 874"
    assert fmap.route("facility:stairs-south").destination == "the south staircase"
    assert fmap.route("place:faculty-lounge").steps[-1].startswith("The Faculty Lounge is ")
    assert fmap.route("area:staff-room") is None and fmap.route("facility:faculty-washroom") is None


def test_only_one_of_something_is_not_called_the_nearest(settings):
    answer = facility(settings, "where is the CCD café")["turn"]["answer"]
    assert answer.startswith("The CCD café is about ") and "nearest" not in answer and "This floor also has" not in answer
