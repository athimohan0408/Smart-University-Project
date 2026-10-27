"""Follow-up -> standalone question."""

import pytest

from tests.conftest import BINAYAK, DEEPA, NISHA, ROOM_874, result
from voice_memory.memory import ConversationMemory


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def memory(kb, settings, clock):
    return ConversationMemory(kb, settings.memory, clock=clock)


def turn(memory, text, res, sid="s1"):
    rw = memory.rewrite(sid, text)
    memory.observe(sid, rw, res)
    return rw


def after_nisha(memory):
    turn(memory, "where is Dr Nisha Varghese's cabin?", result("Dr Nisha Varghese's office is room 874.", entities=[NISHA]))


@pytest.mark.parametrize("follow_up,expected", [
    ("where is her office?", "where is Dr Nisha Varghese's office?"),
    ("is she free on Friday?", "is Dr Nisha Varghese free on Friday?"),
    ("can I meet her today", "can I meet Dr Nisha Varghese today"),
    ("what is her email id", "what is Dr Nisha Varghese's email id"),
    ("when are her consultation hours", "when are Dr Nisha Varghese's consultation hours"),
    ("is ma'am free now", "is Dr Nisha Varghese free now"),
    ("is the file hers", "is the file Dr Nisha Varghese's"),
])
def test_female_pronouns_resolve_to_the_last_person(memory, follow_up, expected):
    after_nisha(memory)
    rw = memory.rewrite("s1", follow_up)
    assert rw.question == expected and rw.changed and rw.rules == ["person:female"]


def test_male_pronouns_and_sir(memory):
    turn(memory, "who is Dr Binayak Dutta", result("Dr Binayak Dutta ...", entities=[BINAYAK]))
    assert memory.rewrite("s1", "where is his cabin").question == "where is Dr Binayak Dutta's cabin"
    assert memory.rewrite("s1", "is sir in today").question == "is Dr Binayak Dutta in today"
    assert memory.rewrite("s1", "can I see him after lunch").question == "can I see Dr Binayak Dutta after lunch"


def test_gender_mismatch_is_left_unresolved(memory):
    turn(memory, "who is Dr Binayak Dutta", result("...", entities=[BINAYAK]))
    rw = memory.rewrite("s1", "where is her office")
    assert rw.question == "where is her office" and not rw.changed and rw.unresolved == ["her"]


def test_nothing_to_resolve_without_history(memory):
    rw = memory.rewrite("s1", "is she in her cabin?")
    assert not rw.changed and set(rw.unresolved) == {"her", "she"}


def test_pronoun_next_to_a_name_is_about_that_name(memory):
    after_nisha(memory)
    q = "where is Dr Deepa V Jose and is she free"
    assert memory.rewrite("s1", q).question == q
    assert memory.rewrite("s1", "is Deepa ma'am in her cabin").question == "is Deepa ma'am in her cabin"


def test_latest_person_wins(memory):
    after_nisha(memory)
    turn(memory, "and where is Dr Deepa V Jose?", result("Dr Deepa V Jose's office is room 877.", entities=[DEEPA]))
    assert memory.rewrite("s1", "is she free on Monday").question == "is Dr Deepa V Jose free on Monday"


def test_rooms_places_events_and_there(memory):
    turn(memory, "who sits in room 874", result("Room 874 is the office of Dr Nisha Varghese.", entities=[ROOM_874],
                                                 intent="find_room"))
    assert memory.rewrite("s1", "how do I get to that room").question == "how do I get to room 874"
    lab = ("facility", "the AIML lab", "facility:lab:sample-aiml-lab")
    turn(memory, "where is the AIML lab", result("Sample AIML Lab is in room 806.", entities=[lab], intent="find_room"))
    assert memory.rewrite("s1", "when is it open").question == "when is Sample AIML Lab open"
    assert memory.rewrite("s1", "how do I go there").question == "how do I go to Sample AIML Lab"
    ev = ("event", "the python workshop", "event:2026-10-23:sample-python-workshop")
    turn(memory, "when is the python workshop", result("Sample Python Workshop ...", entities=[ev], intent="event_info"))
    assert memory.rewrite("s1", "where is that workshop").question == "where is Sample Python Workshop"


def test_there_after_a_person_means_their_office(memory):
    after_nisha(memory)
    assert memory.rewrite("s1", "how do I get there").question == "how do I get to Dr Nisha Varghese's office"


def test_that_cabin_after_a_person(memory):
    after_nisha(memory)
    assert memory.rewrite("s1", "is that cabin on this floor").question == "is Dr Nisha Varghese's cabin on this floor"


def test_unrelated_question_does_not_inherit_the_person(memory):
    after_nisha(memory)
    rw = memory.rewrite("s1", "where is the washroom")
    assert rw.question == "where is the washroom" and "last_entities" not in rw.context


def test_ellipsis_swaps_the_person_or_the_day(memory):
    after_nisha(memory)
    rw = memory.rewrite("s1", "and Dr Deepa V Jose?")
    assert rw.question == "where is Dr Deepa V Jose's cabin?" and rw.rules == ["ellipsis:person"]
    turn(memory, "is Dr Binayak Dutta free on Monday?", result("On Monday ...", entities=[BINAYAK]))
    assert memory.rewrite("s1", "what about Friday?").question == "is Dr Binayak Dutta free on Friday?"
    assert memory.rewrite("s1", "and tomorrow").question == "is Dr Binayak Dutta free tomorrow?"


def test_clarification_pick_by_ordinal_name_or_yes(memory):
    q = "Where is Dr George?"
    ask = result("I found more than one person who matches Dr George: Dr Jossy P George or Dr Naived George Eapen. "
                 "Which one do you mean?", entities=[("faculty", "Dr George", None)])
    turn(memory, q, ask)
    assert memory.rewrite("s1", "the first one").question == "Where is Dr Jossy P George?"
    turn(memory, q, ask)
    assert memory.rewrite("s1", "Naived").question == "Where is Dr Naived George Eapen?"
    turn(memory, q, ask)
    assert memory.rewrite("s1", "the second").question == "Where is Dr Naived George Eapen?"

    confirm = result("Just to make sure I have the right person: do you mean Dr Tegil J John?",
                     entities=[("faculty", "john mathew", None)])
    turn(memory, "where is john mathew", confirm)
    rw = memory.rewrite("s1", "yes")
    assert rw.question == "where is Dr Tegil J John" and rw.rules == ["clarification:picked"]


def test_clarification_declined_or_replaced_by_a_new_question(memory):
    ask = result("I found more than one person who matches Dr George: Dr Jossy P George or Dr Naived George Eapen. "
                 "Which one do you mean?", entities=[("faculty", "Dr George", None)])
    turn(memory, "Where is Dr George?", ask)
    rw = memory.rewrite("s1", "no")
    assert rw.local_reply and "full name" in rw.local_reply
    turn(memory, "Where is Dr George?", ask)
    rw = memory.rewrite("s1", "where is the washroom on this floor")
    assert rw.question == "where is the washroom on this floor" and memory.session("s1").pending is None


def test_new_visitor_after_idle(memory, clock, settings):
    after_nisha(memory)
    clock.t += settings.memory.session_idle_s + 1
    assert not memory.rewrite("s1", "where is her office").changed


def test_sessions_are_separate_and_prior_entities_are_sent(memory):
    after_nisha(memory)
    assert not memory.rewrite("other-kiosk", "where is her office").changed
    rw = memory.rewrite("s1", "is she free")
    assert rw.context == {"session_id": "s1", "prior_entities": ["faculty:nisha-varghese"]}


def test_real_kb_genders_load(settings):
    """With rag-core's generated faculty.json (after `just ingest`), every person has a gender."""
    from voice_memory.kb import KBView

    if not settings.kb.faculty_json.exists():
        pytest.skip("run `just ingest` in rag-core first")
    kb = KBView.load(settings.kb.faculty_json)
    assert len(kb.people) == 36
    assert kb.gender_of("faculty:deepa-v-jose") == "female" and kb.gender_of("faculty:binayak-dutta") == "male"
