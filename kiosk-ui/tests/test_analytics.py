from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kiosk_ui.analytics import compute, normalise_question, timing

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def q(question, *, intent="find_person", escalate=False, reason=None, clarification=False, total=20.0,
      ago_h=1.0, entities=(), session="kiosk-01"):
    return {"schema_version": 1, "timestamp": (NOW - timedelta(hours=ago_h)).isoformat(), "session_id": session,
            "question": question, "intent": intent, "escalate": escalate, "escalate_reason": reason,
            "clarification": clarification, "answer_source": "template",
            "entities": [{"type": e.split(":")[0], "text": e, "canonical_id": e, "score": 1.0} for e in entities],
            "latency_ms": {"nlu": 5.0, "retrieval": 4.0, "generation": 1.0, "scoring": 1.0, "total": total}}


def test_normalise_groups_wording_variants():
    assert normalise_question("Where is Dr Nisha Varghese's cabin?") == normalise_question("where is dr nisha varghese cabin")


def test_counts_most_asked_unanswered_and_categories():
    rag = [
        q("Where is Dr Nisha Varghese's cabin?", entities=["faculty:nisha-varghese"]),
        q("where is dr nisha varghese's cabin", entities=["faculty:nisha-varghese"]),
        q("Where is room 874?", intent="find_room", entities=["room:874"]),
        q("What is Dr Nisha Varghese's phone number?", intent="contact_info", escalate=True, reason="not_in_kb"),
        q("What is Dr Nisha Varghese's phone number?", intent="contact_info", escalate=True, reason="not_in_kb"),
        q("Can I talk to a person?", intent="escalate_to_human", escalate=True, reason="user_requested"),
        q("Where is Dr George?", clarification=True),
        q("Hello", intent="greeting"),  # not a question
    ]
    d = compute(rag, [], [], now=NOW)
    assert d.totals.questions == 7
    assert d.totals.escalated == 3
    assert d.totals.answered == 3
    assert d.totals.clarifications == 1
    assert d.most_asked[0].count == 2
    assert d.most_asked[0].answered in (0, 2)
    top = {c.question.casefold(): c for c in d.most_asked}
    assert top["where is dr nisha varghese's cabin?"].answered == 2
    # the visitor asking for a person is not an "unanswered question"
    assert [u.question for u in d.unanswered] == ["What is Dr Nisha Varghese's phone number?"]
    assert d.unanswered[0].reasons == {"not_in_kb": 2}
    assert d.categories["find_person"] == 3
    assert d.escalation_reasons == {"not_in_kb": 2, "user_requested": 1}
    assert d.top_destinations[0] == {"id": "faculty:nisha-varghese", "count": 2}
    assert d.response_time_ms["rag_server_total"].mean == 20.0
    assert sum(d.by_hour) == 7


def test_days_filter_drops_old_records():
    rag = [q("old question", ago_h=24 * 10), q("new question", ago_h=2)]
    d = compute(rag, [], [], now=NOW, days=7)
    assert d.totals.questions == 1
    assert d.most_asked[0].question == "new question"


def test_falls_back_to_kiosk_events_without_the_rag_log():
    ui = [{"timestamp": NOW.isoformat(), "event": "turn", "session_id": "a", "question": "Where is room 874?",
           "outcome": "answered", "intent": "find_room", "escalate": False, "route_shown": True, "round_trip_ms": 80.0},
          {"timestamp": NOW.isoformat(), "event": "turn", "session_id": "a", "question": "phone number?",
           "outcome": "escalated", "intent": "contact_info", "escalate": True, "escalate_reason": "not_in_kb",
           "round_trip_ms": 120.0},
          {"timestamp": NOW.isoformat(), "event": "receptionist_button", "session_id": "a"}]
    d = compute([], [], ui, now=NOW)
    assert d.totals.questions == 2
    assert d.totals.answered == 1
    assert d.totals.routes_shown == 1
    assert d.totals.receptionist_button == 1
    assert d.response_time_ms["kiosk_round_trip"].mean == 100.0


def test_empty_logs_give_an_empty_dashboard():
    d = compute([], [], [], now=NOW)
    assert d.totals.questions == 0
    assert d.most_asked == [] and d.response_time_ms == {}


def test_timing_percentiles_match_numpy_linear():
    t = timing([10, 20, 30, 40, 50])
    assert (t.p50, t.p95, t.max, t.mean) == (30.0, 48.0, 50.0, 30.0)
    assert timing([]) is None


def test_evaluation_sessions_are_not_counted_as_visitors():
    rag = [q("Where is room 874?", session="kiosk-01-abc"), q("Where is room 874?", session="eval-e10-120000-ab12")]
    assert compute(rag, [], [], now=NOW).totals.questions == 1
