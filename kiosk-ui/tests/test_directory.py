from __future__ import annotations

from kiosk_ui.directory import FacultyDirectory

NISHA = {"type": "faculty", "text": "Dr Nisha Varghese", "canonical_id": "faculty:nisha-varghese", "score": 1.0}


def test_loads_names_and_8th_floor_rooms(settings):
    d = FacultyDirectory.load(settings.kb.faculty_json)
    assert d.rooms == {"faculty:nisha-varghese": "874"}  # Jossy is on another floor, Naived has no room
    assert len(d.names) == 3


def test_route_only_when_answered_and_the_answer_names_the_room(settings):
    d = FacultyDirectory.load(settings.kb.faculty_json)
    answered = {"outcome": "answered", "escalate": False, "entities": [NISHA],
                "answer": "Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor."}
    assert d.rooms_in_answer(answered) == ["room:874"]
    # consultation hours answer: no room said, so no route
    assert d.rooms_in_answer({**answered, "answer": "Her consultation hours are 1:45 pm to 2:45 pm."}) == []
    # never for escalations, clarifications or an unlinked name
    assert d.rooms_in_answer({**answered, "outcome": "escalated", "escalate": True}) == []
    assert d.rooms_in_answer({**answered, "outcome": "clarification"}) == []
    assert d.rooms_in_answer({**answered, "entities": [{**NISHA, "canonical_id": None}]}) == []
    # "8741" is not room 874
    assert d.rooms_in_answer({**answered, "answer": "Call extension 8741."}) == []


def test_clarification_buttons_come_from_the_names_in_the_answer(settings):
    d = FacultyDirectory.load(settings.kb.faculty_json)
    turn = {"outcome": "clarification", "options": [],
            "answer": "I found more than one person who matches Dr George: Dr Jossy P George or "
                      "Dr Naived George Eapen. Which one do you mean?"}
    assert d.clarification_options(turn) == ["Dr Jossy P George", "Dr Naived George Eapen"]
    # voice-memory's own options win; one name alone is a confirmation, not a choice
    assert d.clarification_options({**turn, "options": ["Yes", "No"]}) == ["Yes", "No"]
    assert d.clarification_options({**turn, "answer": "Do you mean Dr Jossy P George?"}) == []
    assert d.clarification_options({**turn, "outcome": "answered"}) == []


def test_missing_faculty_file_is_harmless(tmp_path):
    d = FacultyDirectory.load(tmp_path / "nope.json")
    assert d.rooms == {} and d.clarification_options({"outcome": "clarification", "answer": "x"}) == []
