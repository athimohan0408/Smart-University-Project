"""Two questions in one utterance, and "from the lift" start points."""

import pytest

from tests.conftest import NISHA, ROOM_874, result
from voice_memory.compound import split_questions
from voice_memory.memory import ConversationMemory
from voice_memory.origins import OriginResolver

# ----------------------------------------------------------------------------- splitting


@pytest.mark.parametrize("text,parts", [
    ("where is Dr Nisha's cabin and is she free on Friday?", ["where is Dr Nisha's cabin?", "is she free on Friday?"]),
    ("who teaches ML and where is the MCA lab", ["who teaches ML?", "where is the MCA lab?"]),
    ("Where is Dr Nisha? Is she free on Friday?", ["Where is Dr Nisha?", "Is she free on Friday?"]),
    ("where is the washroom, and how do I get there", ["where is the washroom?", "how do I get there?"]),
    ("can I meet Dr Deepa and is she in her cabin", ["can I meet Dr Deepa?", "is she in her cabin?"]),
    ("where is room 874 and then how do I reach room 871", ["where is room 874?", "how do I reach room 871?"]),
    ("what is her email and what is her phone number", ["what is her email?", "what is her phone number?"]),
    ("where is Dr Nisha? Is she free? And how do I get there?", ["where is Dr Nisha?", "Is she free?", "how do I get there?"]),
])
def test_two_questions_are_split(text, parts):
    assert split_questions(text) == parts


@pytest.mark.parametrize("text", [
    "is she free on Monday and Friday?",           # the second half is only a day
    "what is her phone number and email?",         # ... only a noun
    "where is Dr Nisha and Dr Deepa?",             # a list of names
    "where is room 874?",
    "who teaches data structures and algorithms",
    "tell me about MSc AIML",
    "yes",
    "",
])
def test_one_question_stays_in_one_piece(text):
    assert split_questions(text) == [text]


# ----------------------------------------------------------------------------- start points


@pytest.fixture
def origins(settings):
    return OriginResolver.load(settings.memory.origins_file)


@pytest.fixture
def memory(kb, settings, origins):
    return ConversationMemory(kb, settings.memory, names=settings.names, origins=origins)


def test_resolver_knows_places_with_a_start_node(origins):
    assert "facility:lift-north" in origins.places and "room:871" in origins.places
    assert "facility:faculty-washroom" not in origins.places  # no near/via: a visitor can't start there
    assert origins.name_of("facility:lift-north") == "the north lift" and origins.name_of("room:871") == "room 871"
    assert origins.name_of("facility:ccd") == "the CCD café"


@pytest.mark.parametrize("text,place", [
    ("how do I get to room 874 from the north lift", "facility:lift-north"),
    ("directions to room 874 starting from the west lift", "facility:lift-west"),
    ("I'm at the north staircase, where is room 874", "facility:stairs-north"),
    ("i am near room 871 how do i reach room 874", "room:871"),
    ("from the CCD café how do I get to 874", "facility:ccd"),
    ("coming from the south elevator", "facility:lift-south"),
])
def test_a_clear_start_is_found(origins, text, place):
    match = origins.find(text)
    assert match is not None and [c.id for c in match.candidates] == [place]


@pytest.mark.parametrize("text", [
    "is Dr Nisha free from 3 to 4", "free from monday to friday", "from 1030 am", "where is the lift",
    "where is room 874", "from room 999",  # not a room on the map
    "what time is it", "how do I get to the lift",
])
def test_things_that_are_not_a_start_are_not_found(origins, text):
    assert origins.find(text) is None


def test_a_start_is_taken_out_of_the_question_and_remembered(memory):
    rw = memory.rewrite("s", "how do I get to Dr Nisha Varghese's office from the north lift?")
    assert rw.question == "how do I get to Dr Nisha Varghese's office?"
    assert (rw.origin, rw.origin_name, rw.origin_changed) == ("facility:lift-north", "the north lift", True)
    assert rw.rules[0] == "origin:set"


def test_the_start_is_kept_for_the_follow_ups(memory):
    memory.rewrite("s", "where is room 874 from the north lift?")
    rw = memory.rewrite("s", "and where is room 871?")
    assert rw.origin == "facility:lift-north" and rw.origin_changed is False
    assert memory.origin_of("s") == ("facility:lift-north", "the north lift")


def test_a_lift_is_ambiguous_so_it_asks_and_the_pick_goes_through_the_pipeline(memory):
    rw = memory.rewrite("s", "how do I get to Dr Nisha Varghese's office from the lift?")
    assert rw.options == ["the north lift", "the west lift", "the south lift"]
    assert rw.local_reply == ("There is more than one lift on this floor. "
                              "Which one are you at: the north lift, the west lift or the south lift?")
    assert rw.origin is None  # nothing is assumed until they answer
    rw = memory.rewrite("s", "the west one")
    assert rw.question == "how do I get to Dr Nisha Varghese's office?" and rw.origin == "facility:lift-west"
    assert rw.local_reply is None


def test_a_direction_makes_it_unambiguous(memory):
    assert memory.rewrite("s", "from the south lift where is room 874").origin == "facility:lift-south"


def test_from_here_goes_back_to_the_kiosk(memory):
    memory.rewrite("s", "where is room 874 from the north lift")
    rw = memory.rewrite("s", "and from here?")
    assert rw.origin is None and rw.origin_changed is True and "origin:kiosk" in rw.rules
    assert memory.origin_of("s") is None


def test_only_a_new_start_repeats_the_last_question_from_there(memory):
    rw = memory.rewrite("s", "where is room 874?")
    memory.observe("s", rw, result("Room 874 is the office of Dr Nisha Varghese.", entities=[ROOM_874], intent="find_room"))
    rw = memory.rewrite("s", "and from the north lift?")
    assert rw.question == "where is room 874?" and rw.origin == "facility:lift-north"
    assert rw.rules == ["origin:set", "origin:again"]


def test_saying_where_you_are_with_no_question_just_acknowledges(memory):
    rw = memory.rewrite("s", "I'm at the north staircase")
    assert rw.local_reply == "Okay, I'll start from the north staircase. Where would you like to go?"
    assert rw.origin == "facility:stairs-north"


def test_a_new_visitor_starts_from_the_kiosk(memory):
    memory.rewrite("s", "where is room 874 from the north lift")
    memory.reset("s")
    assert memory.origin_of("s") is None


def test_no_map_means_no_start_points(kb, settings):
    memory = ConversationMemory(kb, settings.memory, names=settings.names)  # no resolver
    rw = memory.rewrite("s", "how do I get to room 874 from the north lift")
    assert rw.origin is None and rw.question.endswith("from the north lift")


def test_a_start_and_a_pronoun_together(memory):
    rw = memory.rewrite("s", "where is Dr Nisha Varghese's cabin?")
    memory.observe("s", rw, result("Dr Nisha Varghese's office is room 874.", entities=[NISHA]))
    rw = memory.rewrite("s", "how do I get to her office from the west lift?")
    assert rw.question == "how do I get to Dr Nisha Varghese's office?" and rw.origin == "facility:lift-west"
