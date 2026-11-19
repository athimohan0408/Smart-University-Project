"""The four new behaviours, end to end through the receptionist (scripted rag-core, no models):
two questions in one, a remembered start point, misheard names, and the message / meeting dialogues."""

import json

import pytest

from tests.conftest import DEEPA, NISHA, ROOM_874, ScriptedRag, result
from voice_memory.metrics import compute, read_log
from voice_memory.receptionist import Receptionist
from voice_memory.speech.stt import TextSTT
from voice_memory.speech.tts import NoTTS, Speaker

RULES = [
    (r"where is Dr Nisha Varghese's (cabin|office)",
     result("Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor, here on this floor.", entities=[NISHA])),
    (r"how do I (get|reach)", result("Room 874 is on this floor, in the Faculty Staff Room.", entities=[NISHA, ROOM_874],
                                     intent="find_room")),
    (r"is Dr Nisha Varghese free",
     result("On Friday, Dr Nisha Varghese's consultation hours are not listed.", entities=[NISHA], intent="person_availability")),
    (r"is Dr Deepa V Jose free", result("Dr Deepa V Jose is free on Friday from 2 to 3 pm.", entities=[DEEPA],
                                        intent="person_availability")),
    (r"where is Dr Deepa V Jose", result("Dr Deepa V Jose's office is room 872.", entities=[DEEPA])),
    (r"phone", result("I'm sorry, I can't verify a phone number for Dr Nisha Varghese. Please check with the receptionist "
                      "at the desk.", entities=[NISHA], intent="contact_info", escalate=True, reason="not_in_kb")),
    (r"where is room 874", result("Room 874 is the office of Dr Nisha Varghese.", entities=[ROOM_874], intent="find_room")),
]


@pytest.fixture
def rag():
    return ScriptedRag(RULES)


@pytest.fixture
def desk(settings, kb, rag):
    return Receptionist(settings, kb=kb, rag=rag, stt=TextSTT(), tts=Speaker(NoTTS(), settings.speech))


def turns_logged(settings):
    return read_log(settings.metrics.turn_log)


# ----------------------------------------------------------------------------- two questions in one


def test_two_questions_are_answered_in_order_and_joined(desk, rag, settings):
    turn = desk.handle_text("k", "where is Dr Nisha Varghese's cabin and is she free on Friday?")
    assert [c[0] for c in rag.calls] == ["where is Dr Nisha Varghese's cabin?", "is Dr Nisha Varghese free on Friday?"]
    assert turn.answer == ("Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor, here on this floor. "
                           "On Friday, Dr Nisha Varghese's consultation hours are not listed.")
    assert turn.speech.startswith("Doctor Nisha Varghese's office is room 8 7 4")  # spoken the way the voice says it
    assert turn.outcome == "answered" and turn.escalate is False
    assert [p.outcome for p in turn.parts] == ["answered", "answered"]
    assert [p.intent for p in turn.parts] == ["find_person", "person_availability"]
    assert turn.rewrite_rules == ["person:female"]  # "she" was resolved from the first answer


def test_each_question_is_logged_for_the_completion_rate(desk, settings):
    desk.handle_text("k", "where is Dr Nisha Varghese's cabin and what is her phone number?")
    records = turns_logged(settings)
    assert [(r["outcome"], r.get("part"), r.get("of")) for r in records] == [("answered", 1, 2), ("escalated", 2, 2)]
    assert "total" in records[0]["latency_ms"] and records[1]["latency_ms"] == {}  # the visitor waited once
    report = compute(records, 120)
    assert (report.completion.completed, report.completion.escalated) == (1, 1)
    assert report.latency["total"].n == 1


def test_an_escalated_part_offers_the_receptionist_once(desk, rag):
    turn = desk.handle_text("k", "where is Dr Nisha Varghese's cabin and what is her phone number?")
    assert turn.outcome == "escalated" and turn.escalate_reason == "not_in_kb"
    assert turn.options == ["Talk to the receptionist", "Ask another question"]
    assert turn.speech.count("Would you like me to call the receptionist for you?") == 1
    assert turn.speech.startswith("Doctor Nisha Varghese's office is room 8 7 4")
    reply = desk.handle_text("k", "yes please")
    assert reply.outcome == "handoff" and reply.ticket is not None
    assert "phone" in reply.ticket.question  # the ticket is about the part that could not be answered


def test_rag_core_down_for_the_second_part(desk, rag):
    desk.handle_text("k", "hello")

    original = rag.answer

    def flaky(question, context):
        if "free" in question:
            from voice_memory.rag_client import RagUnavailable
            raise RagUnavailable("down")
        return original(question, context)

    rag.answer = flaky
    turn = desk.handle_text("k", "where is Dr Nisha Varghese's cabin and is she free on Friday?")
    assert turn.outcome == "escalated" and turn.escalate_reason == "rag_unavailable"
    assert turn.answer.startswith("Dr Nisha Varghese's office is room 874") and "having trouble" in turn.answer


def test_more_questions_than_the_limit_are_answered_up_to_the_limit(desk, rag, settings):
    text = ("where is Dr Nisha Varghese's cabin and is she free on Friday and how do I get there "
            "and where is Dr Deepa V Jose's cabin")
    turn = desk.handle_text("k", text)
    assert len(turn.parts) == settings.memory.max_parts == 3
    assert turn.answer.endswith("Ask me the rest and I'll answer those too.")


def test_a_single_question_looks_exactly_as_before(desk):
    turn = desk.handle_text("k", "where is Dr Nisha Varghese's cabin?")
    assert turn.parts == [] and turn.outcome == "answered" and turn.origin is None


# ----------------------------------------------------------------------------- a name that needs checking first


def test_a_misheard_name_is_checked_and_the_rest_of_the_utterance_is_not_lost(desk, rag):
    turn = desk.handle_text("k", "where is Dr Dipa Josey and is she free on Friday?")
    assert turn.outcome == "clarification" and turn.answer == "I did not catch that name clearly. Did you mean Dr Deepa V Jose?"
    assert turn.options == ["Dr Deepa V Jose"] and rag.calls == []  # nothing was sent to rag-core with a wrong name
    turn = desk.handle_text("k", "yes")
    assert [c[0] for c in rag.calls] == ["where is Dr Deepa V Jose?", "is Dr Deepa V Jose free on Friday?"]
    assert turn.outcome == "answered" and "room 872" in turn.answer and "free on Friday from 2 to 3 pm" in turn.answer


def test_a_clearly_misspelled_name_is_fixed_without_asking(desk, rag):
    turn = desk.handle_text("k", "where is Dr Nisa Varghese's cabin?")
    assert rag.calls[0][0] == "where is Dr Nisha Varghese's cabin?"
    assert turn.outcome == "answered" and turn.rewrite_rules == ["name:corrected"]
    assert turn.transcript == "where is Dr Nisa Varghese's cabin?"  # the screen can show "I understood: ..."


# ----------------------------------------------------------------------------- start points


def test_a_start_point_is_sent_to_the_kiosk_and_kept(desk, rag):
    turn = desk.handle_text("k", "how do I get to Dr Nisha Varghese's office from the north lift?")
    assert rag.calls[0][0] == "how do I get to Dr Nisha Varghese's office?"  # rag-core never sees the start point
    assert turn.origin.place_id == "facility:lift-north" and turn.origin.name == "the north lift" and turn.origin.changed
    assert turn.answer.endswith("I'll show you the way from the north lift.")
    later = desk.handle_text("k", "where is room 874?")
    assert later.origin.place_id == "facility:lift-north" and later.origin.changed is False
    assert "I'll show you the way" not in later.answer  # said once, when it was set


def test_an_ambiguous_lift_asks_with_buttons(desk, rag):
    turn = desk.handle_text("k", "how do I get to Dr Nisha Varghese's office from the lift?")
    assert turn.outcome == "clarification" and rag.calls == []
    assert turn.options == ["the north lift", "the west lift", "the south lift"] and turn.origin is None
    turn = desk.handle_text("k", "the west lift")
    assert turn.outcome == "answered" and turn.origin.place_id == "facility:lift-west"
    assert rag.calls[0][0] == "how do I get to Dr Nisha Varghese's office?"


def test_from_here_returns_to_the_kiosk_and_a_new_visitor_forgets(desk):
    desk.handle_text("k", "where is room 874 from the north lift?")
    assert desk.handle_text("k", "and from here?").origin is None
    desk.handle_text("k", "where is room 874 from the west lift?")
    desk.new_visitor("k")
    assert desk.handle_text("k", "where is room 874?").origin is None


def test_the_start_shows_up_in_the_http_turn_result(settings, kb, rag):
    from fastapi.testclient import TestClient

    from voice_memory import api

    api._state["r"] = Receptionist(settings, kb=kb, rag=rag, stt=TextSTT(), tts=Speaker(NoTTS(), settings.speech))
    try:
        with TestClient(api.app) as client:
            body = client.post("/turn", json={"session_id": "h", "speak": False,
                                              "text": "where is room 874 from the north lift"}).json()
    finally:
        api._state.clear()
    assert body["origin"] == {"place_id": "facility:lift-north", "name": "the north lift", "changed": True}
    assert body["parts"] == [] and body["task"] is None


# ----------------------------------------------------------------------------- message and meeting dialogues


def test_a_message_through_the_receptionist_and_what_is_kept(desk, rag, settings):
    a = desk.handle_text("k", "I want to leave a message for Dr Nisha Varghese")
    assert a.outcome == "task" and a.task == "message" and a.answer == "What would you like me to tell Dr Nisha Varghese?"
    assert rag.calls == []  # a dialogue never reaches rag-core
    desk.handle_text("k", "I will be late for our meeting")
    desk.handle_text("k", "Athira")
    desk.handle_text("k", "9876543210")
    done = desk.handle_text("k", "yes")
    assert done.outcome == "task_done" and done.request_id and done.task == "message"

    # the request has the details ...
    rec = json.loads(settings.tasks.requests.read_text(encoding="utf-8").splitlines()[0])
    assert rec["visitor_name"] == "Athira" and rec["contact"] == "9876543210" and rec["message"] == "I will be late for our meeting"
    # ... and the turn log, which analytics reads, has none of them
    log_text = settings.metrics.turn_log.read_text(encoding="utf-8")
    for secret in ("Athira", "9876543210", "I will be late"):
        assert secret not in log_text
    assert [r["outcome"] for r in turns_logged(settings)] == ["task"] * 4 + ["task_done"]
    assert all(r["question"] == "[message or meeting request]" for r in turns_logged(settings))
    assert compute(turns_logged(settings), 120).completion.completed == 1  # a sent request is a completed query


def test_after_a_message_she_means_the_person_it_was_for(desk, rag):
    for text in ("leave a message for Dr Nisha Varghese saying call me", "Athira", "skip", "yes"):
        desk.handle_text("k", text)
    turn = desk.handle_text("k", "is she free on Friday?")
    assert rag.calls[-1][0] == "is Dr Nisha Varghese free on Friday?" and turn.outcome == "answered"


def test_a_meeting_request_with_buttons_and_a_cancel(desk, settings):
    a = desk.handle_text("k", "book a meeting with Dr Deepa V Jose")
    assert a.task == "meeting" and a.answer == "Which day would you like to meet Dr Deepa V Jose?"
    b = desk.handle_text("k", "friday")
    assert b.answer.startswith("What time on Friday")
    desk.handle_text("k", "3 pm")
    c = desk.handle_text("k", "skip")  # purpose
    assert c.answer.startswith("May I have your name")
    desk.handle_text("k", "Kiran")
    d = desk.handle_text("k", "skip")
    assert d.options == ["Yes, send it", "No, cancel"] and "meeting request for Dr Deepa V Jose" in d.answer
    e = desk.handle_text("k", "No, cancel")
    assert e.outcome == "task_cancelled" and not settings.tasks.requests.exists()


def test_a_new_visitor_abandons_a_half_finished_request(desk, rag):
    desk.handle_text("k", "leave a message for Dr Nisha Varghese")
    desk.new_visitor("k")
    turn = desk.handle_text("k", "where is room 874?")
    assert turn.outcome == "answered" and rag.calls[-1][0] == "where is room 874?"


def test_asking_for_the_receptionist_during_a_dialogue_hands_over(desk):
    from voice_memory.handoff import Handoff  # noqa: F401 - the normal path handles it

    desk.handle_text("k", "leave a message for Dr Nisha Varghese")
    turn = desk.handle_text("k", "I want to talk to a receptionist")
    assert turn.task is None and turn.outcome == "escalated"  # rag stand-in has no rule for it: it escalates
