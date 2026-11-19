"""'Leave a message' and 'book a meeting': the question-by-question dialogues."""

import json
from datetime import date, datetime

import httpx
import pytest

from voice_memory.names import NameMatcher
from voice_memory.tasks import TaskManager, parse_day, parse_time

MONDAY = datetime(2026, 11, 16, 10, 0)  # a Monday, 10:00


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(kb, settings, clock):
    def build(*, focus=None, webhook=None, client=None, tasks=None, now=MONDAY):
        cfg = tasks or settings.tasks
        n = settings.names
        matcher = NameMatcher.from_kb(kb, auto_min=n.auto_min, ask_min=n.ask_min, margin=n.margin, max_options=n.max_options)
        return TaskManager(cfg, kb, matcher=matcher, focus_person=focus, webhook_url=webhook, client=client,
                           clock=clock, now=lambda: now)
    return build


@pytest.fixture
def tm(make):
    return make()


def say(tm, text, sid="s"):
    return tm.handle(sid, text)


def records(settings):
    path = settings.tasks.requests
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ----------------------------------------------------------------------------- parsing days and times


@pytest.mark.parametrize("text,expected", [
    ("tomorrow", date(2026, 11, 17)), ("today", date(2026, 11, 16)), ("day after tomorrow", date(2026, 11, 18)),
    ("friday", date(2026, 11, 20)), ("on Friday please", date(2026, 11, 20)), ("next monday", date(2026, 11, 23)),
    ("monday", date(2026, 11, 16)), ("wed", date(2026, 11, 18)), ("the 20th", date(2026, 11, 20)),
    ("20 november", date(2026, 11, 20)), ("november 25", date(2026, 11, 25)), ("25/11", date(2026, 11, 25)),
])
def test_days(text, expected):
    day, reason = parse_day(text, MONDAY.date(), ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"])
    assert (day, reason) == (expected, None)


@pytest.mark.parametrize("text,reason", [
    ("sunday", "Sunday isn't available: meeting requests are for Monday to Saturday."),
    ("22 november", "Sunday isn't available: meeting requests are for Monday to Saturday."),
    ("31 november", "That date doesn't exist."),
    ("10 november", "That day has already passed."),
])
def test_days_that_cannot_be_used_say_why(text, reason):
    day, got = parse_day(text, MONDAY.date(), ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"])
    assert day is None and got == reason


def test_no_day_is_not_an_error():
    assert parse_day("hello there", MONDAY.date(), ["monday"]) == (None, None)


@pytest.mark.parametrize("text,value", [
    ("3 pm", "15:00"), ("3:30 pm", "15:30"), ("3.30pm", "15:30"), ("15:30", "15:30"), ("10 am", "10:00"),
    ("at 3", "15:00"), ("noon", "12:00"), ("half past three", "15:30"), ("four thirty", "16:30"),
    ("quarter to four", "15:45"), ("9 o'clock", "09:00"), ("morning", "morning"), ("in the afternoon", "afternoon"),
])
def test_times_inside_office_hours(text, value):
    parsed = parse_time(text, "07:30", "17:00", allow_bare=True)
    assert parsed.value == value and parsed.reason is None


def test_a_bare_number_in_a_date_is_not_a_time():
    assert parse_time("on 20 november", "07:30", "17:00", allow_bare=False).value is None
    assert parse_time("tomorrow at 3", "07:30", "17:00", allow_bare=False).value == "15:00"


@pytest.mark.parametrize("text", ["7 pm", "6 am", "7", "11 pm", "evening"])
def test_times_outside_office_hours_are_refused(text):
    parsed = parse_time(text, "07:30", "17:00", allow_bare=True)
    assert parsed.value is None and "between 7:30 am and 5:00 pm" in parsed.reason


def test_am_or_pm_is_asked_only_when_both_fit():
    wide = parse_time("7", "06:00", "20:00", allow_bare=True)
    assert wide.ask_ampm == (7, 0) and wide.value is None
    assert parse_time("pm", "06:00", "20:00", allow_bare=True, ampm_for=(7, 0)).value == "19:00"
    assert parse_time("in the morning", "06:00", "20:00", allow_bare=True, ampm_for=(7, 0)).value == "07:00"
    assert parse_time("er", "06:00", "20:00", allow_bare=True, ampm_for=(7, 0)).ask_ampm == (7, 0)


# ----------------------------------------------------------------------------- leave a message


def test_a_message_from_start_to_finish(tm, settings):
    r = say(tm, "I want to leave a message for Dr Nisha Varghese")
    assert r.outcome == "task" and r.kind == "message" and r.answer == "What would you like me to tell Dr Nisha Varghese?"
    assert r.person_id == "faculty:nisha-varghese"
    r = say(tm, "tell her I will be ten minutes late for our meeting")
    assert r.answer == "May I have your name, so Dr Nisha Varghese knows who it is from?"
    r = say(tm, "my name is athira mohan")
    assert r.answer.startswith("How can Dr Nisha Varghese reach you?") and r.options == ["Skip"]
    r = say(tm, "9 8 7 6 5 4 3 2 1 0")
    assert r.options == ["Yes, send it", "No, cancel"]
    assert r.answer == ('Let me check: a message for Dr Nisha Varghese: "I will be ten minutes late for our meeting", '
                        "from Athira Mohan, contact 9876543210. Shall I send it?")
    r = say(tm, "yes")
    assert r.outcome == "task_done" and r.request_id and "left your message for Dr Nisha Varghese" in r.answer
    [rec] = records(settings)
    assert rec["type"] == "message" and rec["status"] == "pending" and rec["recipient_id"] == "faculty:nisha-varghese"
    assert rec["message"] == "I will be ten minutes late for our meeting" and rec["visitor_name"] == "Athira Mohan"
    assert rec["contact"] == "9876543210" and rec["request_id"] == r.request_id
    assert not tm.active("s")


def test_everything_said_up_front_is_not_asked_again(tm):
    r = say(tm, "leave a message for Dr Nisha Varghese saying I will be late today")
    assert r.answer.startswith("May I have your name")  # recipient and message were in the sentence


def test_the_contact_can_be_skipped(tm, settings):
    say(tm, "leave a message for Dr Binayak Dutta saying please call me back")
    say(tm, "Ravi")
    r = say(tm, "skip")
    assert r.answer == 'Let me check: a message for Dr Binayak Dutta: "please call me back", from Ravi. Shall I send it?'
    say(tm, "yes")
    assert records(settings)[0]["contact"] is None


def test_a_spoken_email_is_understood(tm):
    say(tm, "leave a message for Dr Binayak Dutta saying please call me back")
    say(tm, "Ravi")
    r = say(tm, "r a v i at gmail dot com")
    assert "contact ravi@gmail.com" in r.answer


def test_her_means_the_person_just_discussed(make):
    tm = make(focus=lambda sid: "faculty:deepa-v-jose")
    r = say(tm, "I'd like to leave a message for her")
    assert r.person_id == "faculty:deepa-v-jose" and r.answer == "What would you like me to tell Dr Deepa V Jose?"


def test_an_ambiguous_name_asks_which(tm):
    r = say(tm, "leave a message for Dr George")
    assert r.options == ["Dr Jossy P George", "Dr Naived George Eapen"]
    assert r.answer == "Which one do you mean: Dr Jossy P George or Dr Naived George Eapen?"
    r = say(tm, "the second one")
    assert r.person_id == "faculty:naived-george-eapen" and r.answer.startswith("What would you like me to tell")


def test_a_misheard_name_is_checked_with_the_visitor(tm):
    r = say(tm, "I want to leave a message for Dr Dipa Josey")
    assert r.answer == "Did you mean Dr Deepa V Jose?" and r.options == ["Yes", "No"]
    r = say(tm, "yes")
    assert r.person_id == "faculty:deepa-v-jose" and r.answer.startswith("What would you like me to tell")


def test_a_name_that_matches_nobody_is_asked_again_then_given_up(tm):
    say(tm, "I want to leave a message")
    for _ in range(2):
        r = say(tm, "Dr Zubair Khan")
        assert r.outcome == "task" and "couldn't find anyone" in r.answer
    r = say(tm, "Dr Zubair Khan")
    assert r.outcome == "task_cancelled" and "stopped the request" in r.answer and not tm.active("s")


def test_a_bad_contact_is_asked_again_then_given_up(tm):
    say(tm, "leave a message for Dr Binayak Dutta saying call me back")
    say(tm, "Ravi")
    assert "doesn't look like a phone number" in say(tm, "tomorrow").answer
    assert "doesn't look like a phone number" in say(tm, "later").answer
    assert say(tm, "whenever").outcome == "task_cancelled"


def test_no_at_the_end_cancels_and_nothing_is_stored(tm, settings):
    say(tm, "leave a message for Dr Binayak Dutta saying call me back")
    say(tm, "Ravi")
    say(tm, "skip")
    r = say(tm, "No, cancel")
    assert r.outcome == "task_cancelled" and r.answer == "No problem, I've cancelled that. You can ask me something else."
    assert records(settings) == []


@pytest.mark.parametrize("word", ["cancel", "never mind", "stop"])
def test_cancel_works_at_any_point(tm, word):
    say(tm, "leave a message for Dr Binayak Dutta")
    assert say(tm, word).outcome == "task_cancelled"


def test_a_long_message_that_starts_with_stop_is_still_a_message(tm):
    say(tm, "leave a message for Dr Binayak Dutta")
    r = say(tm, "stop by my lab when you are free because the demo is ready")
    assert r.outcome == "task" and r.answer.startswith("May I have your name")


def test_asking_for_the_receptionist_leaves_the_dialogue(tm):
    say(tm, "leave a message for Dr Binayak Dutta")
    assert say(tm, "I want to talk to the receptionist") is None
    assert not tm.active("s")


def test_a_forgotten_dialogue_expires(tm, clock):
    say(tm, "leave a message for Dr Binayak Dutta saying call me back")
    clock.t += 500
    assert say(tm, "yes") is None  # not taken as the answer to a question asked ages ago


@pytest.mark.parametrize("text", [
    "can I meet Dr Nisha tomorrow", "where is the meeting room", "when is the next staff meeting", "what is her message",
    "is she free on Friday", "who teaches ML", "hello",
])
def test_ordinary_questions_do_not_start_a_dialogue(tm, text):
    assert say(tm, text) is None


# ----------------------------------------------------------------------------- book a meeting


def test_a_meeting_with_everything_in_one_sentence(tm, settings):
    r = say(tm, "book a meeting with Dr Deepa V Jose tomorrow at 3 pm about my project")
    assert r.kind == "meeting" and r.answer.startswith("May I have your name")
    say(tm, "Athira")
    r = say(tm, "skip")
    assert r.answer == ("Let me check: a meeting request for Dr Deepa V Jose on Tuesday 17 November at 3:00 pm, "
                        "about my project, from Athira. Shall I send it?")
    r = say(tm, "send it")
    assert r.outcome == "task_done" and "nothing is booked until they do" in r.answer
    [rec] = records(settings)
    assert (rec["type"], rec["meeting_date"], rec["meeting_day"], rec["meeting_time"], rec["purpose"]) == (
        "meeting", "2026-11-17", "tuesday", "15:00", "my project")


def test_a_meeting_asks_for_what_is_missing(tm, settings):
    r = say(tm, "I want to book an appointment with Dr Binayak Dutta")
    assert r.answer == "Which day would you like to meet Dr Binayak Dutta?"
    r = say(tm, "sunday")
    assert r.answer == "Sunday isn't available: meeting requests are for Monday to Saturday. Which day would you like?"
    r = say(tm, "friday")
    assert r.answer == "What time on Friday 20 November would suit you?"
    r = say(tm, "7 pm")
    assert "between 7:30 am and 5:00 pm" in r.answer
    r = say(tm, "10")
    assert r.answer == "What is the meeting about? You can say skip." and r.options == ["Skip"]
    r = say(tm, "to discuss my thesis")
    assert r.answer.startswith("May I have your name")
    say(tm, "Kiran")
    say(tm, "kiran@example.com")
    r = say(tm, "yes")
    rec = records(settings)[0]
    assert rec["meeting_time"] == "10:00" and rec["purpose"] == "to discuss my thesis" and rec["contact"] == "kiran@example.com"
    assert r.outcome == "task_done"


def test_a_time_that_has_already_passed_today_is_refused(tm):
    r = say(tm, "book a meeting with Dr Binayak Dutta today")
    assert r.answer == "What time on Monday 16 November would suit you?"
    r = say(tm, "9 am")
    assert r.answer == "That time has already passed today. What time would suit you?"
    r = say(tm, "2 pm")
    assert r.answer.startswith("What is the meeting about")


def test_a_vague_time_of_day_is_accepted_as_it_is(tm, settings):
    say(tm, "book a meeting with Dr Binayak Dutta on friday")
    r = say(tm, "in the afternoon")
    assert r.answer.startswith("What is the meeting about")
    say(tm, "skip")
    say(tm, "Kiran")
    r = say(tm, "skip")
    assert "on Friday 20 November, afternoon" in r.answer


def test_am_or_pm_is_asked_when_the_hours_allow_both(make, settings):
    wide = settings.tasks.model_copy(update={"meeting_earliest": "06:00", "meeting_latest": "20:00"})
    tm = make(tasks=wide)
    say(tm, "book a meeting with Dr Binayak Dutta on friday")
    r = say(tm, "at 7")
    assert r.answer == "Is that 7 am or 7 pm?" and r.options == ["7 am", "7 pm"]
    r = say(tm, "7 pm")
    assert r.answer.startswith("What is the meeting about")


def test_the_dialogue_makes_the_person_the_one_she_means(tm):
    r = say(tm, "leave a message for Dr Nisha Varghese saying thanks a lot")
    assert r.person_id == "faculty:nisha-varghese"


# ----------------------------------------------------------------------------- the request goes to the desk


def test_the_webhook_gets_the_request_when_one_is_configured(make, settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    tm = make(webhook="http://desk.example/hook", client=httpx.Client(transport=httpx.MockTransport(handler)))
    say(tm, "leave a message for Dr Binayak Dutta saying call me back")
    say(tm, "Ravi")
    say(tm, "skip")
    say(tm, "yes")
    assert seen and seen[0]["type"] == "message" and seen[0]["recipient"] == "Dr Binayak Dutta"


def test_a_webhook_failure_does_not_lose_the_request(make, settings):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    tm = make(webhook="http://desk.example/hook", client=httpx.Client(transport=httpx.MockTransport(handler)))
    say(tm, "leave a message for Dr Binayak Dutta saying call me back")
    say(tm, "Ravi")
    say(tm, "skip")
    assert say(tm, "yes").outcome == "task_done" and len(records(settings)) == 1


def test_the_feature_can_be_switched_off(make, settings):
    off = settings.tasks.model_copy(update={"enabled": False})
    assert say(make(tasks=off), "leave a message for Dr Binayak Dutta") is None
