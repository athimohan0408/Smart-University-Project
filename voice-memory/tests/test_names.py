"""Misheard and misspelled names: fixed quietly, asked about, or left alone."""

import pytest

from tests.conftest import NISHA, result
from voice_memory.memory import ConversationMemory
from voice_memory.names import NameMatcher, phonetic_key


@pytest.fixture
def matcher(kb, settings):
    n = settings.names
    return NameMatcher.from_kb(kb, auto_min=n.auto_min, ask_min=n.ask_min, margin=n.margin, max_options=n.max_options)


@pytest.fixture
def memory(kb, settings):
    return ConversationMemory(kb, settings.memory, names=settings.names)


@pytest.mark.parametrize("a,b", [("Dutta", "Datta"), ("Deepa", "Dipa"), ("Naived", "Naveed"), ("Jossy", "Josie"),
                                 ("Sridevi", "Sridhevi"), ("Rajesh", "Raajesh"), ("Shyam", "Syam"), ("Joseph", "Josef")])
def test_names_that_sound_alike_share_a_key(a, b):
    assert phonetic_key(a) == phonetic_key(b)


@pytest.mark.parametrize("a,b", [("Dutta", "Nisha"), ("George", "Deepa"), ("Tegil", "Jossy")])
def test_different_names_do_not(a, b):
    assert phonetic_key(a) != phonetic_key(b)


@pytest.mark.parametrize("said,fixed", [
    ("where is Dr Nisa Varghese's cabin", "where is Dr Nisha Varghese's cabin"),
    ("is Dr Nisha Vargese free", "is Dr Nisha Varghese free"),
    ("where is Dr Naveed George", "where is Dr Naived George"),
    ("is binayak datta free", "is binayak Dutta free"),       # a surname after a known first name
    ("where is binayak dutt", "where is binayak Dutta"),      # no title needed once it is nearly the same spelling
])
def test_a_clearly_meant_name_is_fixed_quietly(matcher, said, fixed):
    fixes = matcher.scan(said)
    assert not any(f.options for f in fixes)
    assert matcher.apply(said, fixes) == fixed


def test_the_possessive_is_kept(matcher):
    said = "where is Dr Nisa Varghese's cabin"
    assert matcher.apply(said, matcher.scan(said)).endswith("Varghese's cabin")


def test_a_name_that_is_close_but_not_certain_is_asked_about(matcher):
    fixes = matcher.scan("where is Dr Dipa Josey's cabin")
    assert len(fixes) == 1 and fixes[0].replacement is None  # "Dipa Josey" is one name, asked about once
    assert fixes[0].options == ("Dr Deepa V Jose",) and fixes[0].heard == "Dr Dipa Josey"


def test_two_people_who_sound_equally_close_are_both_offered(matcher):
    [fix] = matcher.scan("is Dr Josi in")
    assert fix.replacement is None and set(fix.options) >= {"Dr Deepa V Jose", "Dr Jossy P George"}


@pytest.mark.parametrize("text", [
    "where is the washroom", "who teaches machine learning", "where is Dr Nisha Varghese sitting",
    "how many semesters in MCA", "get me a human", "what is the weather today", "who shares room 871",
    "where is Dr Nisha Varghese", "is she free on Friday", "where is Dr George", "talk to someone",
])
def test_ordinary_words_and_known_names_are_never_touched(matcher, text):
    assert matcher.scan(text) == []


def test_an_unknown_but_different_name_is_not_forced_onto_someone(matcher):
    assert matcher.scan("where is Dr Zubair Khan") == []  # nothing is close: rag-core escalates it as not found


# ----------------------------------------------------------------------------- in a conversation


def test_memory_fixes_and_says_so(memory):
    rw = memory.rewrite("s", "is binayak datta free on friday?")
    assert rw.question == "is binayak Dutta free on friday?" and rw.rules == ["name:corrected"] and rw.local_reply is None


def test_memory_asks_and_a_yes_continues_with_the_right_name(memory):
    rw = memory.rewrite("s", "where is Dr Dipa Josey's cabin?")
    assert rw.local_reply == "I did not catch that name clearly. Did you mean Dr Deepa V Jose?"
    assert rw.options == ["Dr Deepa V Jose"] and rw.rules == ["name:ask"]
    rw = memory.rewrite("s", "yes")
    assert rw.question == "where is Dr Deepa V Jose's cabin?" and rw.local_reply is None


def test_a_no_does_not_guess(memory):
    memory.rewrite("s", "where is Dr Dipa Josey's cabin?")
    rw = memory.rewrite("s", "no")
    assert rw.local_reply and "full name" in rw.local_reply


def test_asking_about_two_candidates_accepts_the_second(memory):
    rw = memory.rewrite("s", "is Dr Josi in?")
    assert rw.options and len(rw.options) >= 2
    pick = rw.options.index("Dr Jossy P George") + 1
    rw = memory.rewrite("s", {1: "the first one", 2: "the second one", 3: "the third one"}[pick])
    assert "Dr Jossy P George" in rw.question


def test_a_fixed_name_still_gets_pronouns_resolved_afterwards(memory):
    rw = memory.rewrite("s", "where is Dr Nisa Varghese?")
    memory.observe("s", rw, result("Dr Nisha Varghese's office is room 874.", entities=[NISHA]))
    assert memory.rewrite("s", "is she free on Friday?").question == "is Dr Nisha Varghese free on Friday?"


def test_the_feature_can_be_switched_off(kb, settings):
    off = settings.names.model_copy(update={"enabled": False})
    rw = ConversationMemory(kb, settings.memory, names=off).rewrite("s", "is binayak datta free")
    assert rw.question == "is binayak datta free"
