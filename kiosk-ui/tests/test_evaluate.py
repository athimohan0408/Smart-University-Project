from __future__ import annotations

from fastapi.testclient import TestClient

from kiosk_ui.config import MODULE_ROOT
from kiosk_ui.eventlog import read_jsonl
from kiosk_ui.evaluate import check_turn, map_coverage, run, to_markdown
from kiosk_ui.navigation import FloorMap
from kiosk_ui.server import create_app


def result(outcome="answered", answer="", routes=(), escalate=False, options=()):
    return {"turn": {"outcome": outcome, "answer": answer, "escalate": escalate, "options": list(options)},
            "routes": [{"place_id": r} for r in routes]}


def test_check_turn_passes_and_fails_each_rule():
    good = result(answer="Room 880, Central Block", routes=["room:880"])
    assert check_turn({"outcome": "answered", "keypoints": ["880"], "route_to": "room:880"}, good) == []
    assert check_turn({"outcome": ["escalated", "handoff"]}, good)[0].startswith("outcome")
    assert "missing" in check_turn({"keypoints": ["881"]}, good)[0]
    assert "no route" in check_turn({"route_to": "room:881"}, good)[0]
    assert "where none should be" in check_turn({"off_map": True}, good)[0]
    assert "escalate" in check_turn({"escalate": True}, good)[0]
    assert "option buttons" in check_turn({"min_options": 2}, good)[0]
    # route_to_if_answered only applies when the system answered
    assert check_turn({"route_to_if_answered": "room:885"}, result(outcome="clarification")) == []


def test_test_set_is_well_formed():
    rows = read_jsonl(MODULE_ROOT / "evaluation" / "test_set.jsonl")
    assert 20 <= sum(len(r["turns"]) for r in rows) <= 30
    assert len({r["id"] for r in rows}) == len(rows)
    fmap = FloorMap.load(MODULE_ROOT / "data" / "floor_map.yaml")
    for r in rows:
        for t in r["turns"]:
            assert t["say"].strip() and isinstance(t["expect"], dict)
            for key in ("route_to", "route_to_if_answered"):
                if key in t["expect"]:
                    assert fmap.route(t["expect"][key]) is not None, (r["id"], t["expect"][key])


def test_run_and_report_end_to_end_with_a_fake_voice_memory(settings, voice_memory, tmp_path):
    scenarios = [
        {"id": "a", "category": "location", "turns": [
            {"say": "Where is Dr Nisha Varghese's cabin?", "expect": {"outcome": "answered", "keypoints": ["874"],
                                                                       "route_to": "room:874"}}]},
        {"id": "b", "category": "escalation", "turns": [
            {"say": "What is Dr Nisha Varghese's phone number?", "expect": {"outcome": "escalated", "escalate": True}}]},
        {"id": "c", "category": "clarification", "turns": [
            {"say": "Where is Dr George?", "expect": {"outcome": "clarification", "min_options": 2}},
            {"say": "Dr Ashwini Patil", "expect": {"outcome": "answered", "off_map": True, "keypoints": ["999"]}}]},
    ]
    with TestClient(create_app(settings, transport=voice_memory)) as client:
        records = run(scenarios, lambda path, body: client.post(path, json=body).json())
    assert [r["passed"] for r in records] == [True, True, True, False]
    assert "missing ['999']" in records[-1]["fails"][0]

    fmap = FloorMap.load(settings.map.file)
    md = to_markdown(records, settings, fmap, map_coverage(fmap, tmp_path / "missing.json"))
    assert "| turns correct | 75.0% (3/4) |" in md
    assert "| navigation: correct route shown | 100.0% (1/1) |" in md
    assert "NO (placeholder layout)" in md
    assert "**c turn 2**" in md
