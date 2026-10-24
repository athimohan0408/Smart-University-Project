"""The grounded prompt, its docs copy, canned answers and the LLM-free fact composer."""

import re
from datetime import datetime

import pytest

from rag_core.contracts import Entity, EntityType, Intent, RetrievedFact
from rag_core.generation.prompts import PROMPTS_DOC, SYSTEM_PROMPT, build_user_message, render_prompts_doc
from rag_core.generation.templates import (
    FactComposer,
    canned_answer,
    consultation_slots,
    schedule_entries,
    spoken_time,
)
from rag_core.kb.facts import FactCategory
from rag_core.nlu.entities import EntityExtractor

NOW = datetime(2026, 10, 20, 14, 30)  # a Tuesday


def test_docs_prompts_md_matches_the_code():
    assert PROMPTS_DOC.read_text(encoding="utf-8") == render_prompts_doc(), (
        "docs/prompts.md is stale: run `python -m rag_core.generation.prompts`"
    )


def test_system_prompt_carries_the_grounding_rules():
    for phrase in ("SPOKEN ALOUD", "room 874", "1:45 pm", "ONLY the numbered FACTS", "cited_fact_ids",
                   '"answerable" to false', "NOT on this floor", "not verified", "at most 3"):
        assert phrase in SYSTEM_PROMPT, phrase


def test_user_message_lists_facts_with_ids():
    fact = RetrievedFact(fact_id="room:874", text="Room 874 is in Central Block.", score=1.0, rank=1, via="structured")
    msg = build_user_message("where is room 874", NOW, Intent.FIND_ROOM, [], [fact])
    assert "1. [room:874] Room 874 is in Central Block." in msg
    assert "Tuesday 20 October 2026, 2:30 pm" in msg and "DETECTED INTENT: find_room" in msg


def test_spoken_times():
    assert [spoken_time(t) for t in ("13:45", "09:00", "12:00", "07:30")] == ["1:45 pm", "9 am", "12 pm", "7:30 am"]


def test_canned_answers_cover_the_no_llm_intents():
    for intent in (Intent.GREETING, Intent.SMALLTALK_OR_THANKS, Intent.ESCALATE_TO_HUMAN, Intent.OUT_OF_SCOPE):
        assert canned_answer(intent, "hi")
    assert canned_answer(Intent.SMALLTALK_OR_THANKS, "thank you") != canned_answer(Intent.SMALLTALK_OR_THANKS, "who are you")
    assert canned_answer(Intent.FIND_PERSON, "where is room 874") is None


def test_every_fact_shape_still_parses(entity_kb):
    """The composer's parsers follow kb/facts.py sentences; a change there must show up here."""
    _, _, facts = entity_kb
    for f in facts:
        if f.category == FactCategory.FACULTY_CONSULTATION and "In 24-hour time" in f.text:
            assert consultation_slots(f.text), f.fact_id
        if f.category == FactCategory.SCHEDULE and " has: " in f.text:
            assert schedule_entries(f.text), f.fact_id
        if f.category == FactCategory.FACULTY_PROFILE:
            assert re.search(r"Email: (\S+@\S+?|not listed)\.", f.text), f.fact_id
        if f.category == FactCategory.COURSE:
            assert "Taught by: " in f.text, f.fact_id


@pytest.fixture(scope="module")
def compose(settings, gazetteer, make_retriever):
    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    retriever = make_retriever()
    composer = FactComposer(settings.generation)

    def run(question, intent):
        mentions = extractor.extract(question, now=NOW).mentions
        facts = retriever.retrieve(question, intent, mentions)
        return composer.compose(question, intent, mentions, facts, NOW), {f.fact_id for f in facts}

    return run


@pytest.mark.parametrize("question,intent,expect,answerable", [
    ("where is Dr Nisha Varghese's cabin?", Intent.FIND_PERSON, ["874", "on this floor"], True),
    ("where is Dr Ashwini Patil", Intent.FIND_PERSON, ["not on this floor", "Block II", "607A"], True),
    ("who teaches ML", Intent.FIND_BY_SUBJECT, ["ML is taught by", "Dr Nisha Varghese"], True),
    ("is Deepa Jose free on Friday", Intent.PERSON_AVAILABILITY, ["1:45 pm to 3:45 pm"], True),
    ("what is Dr Nisha Varghese's phone number?", Intent.CONTACT_INFO, ["can't verify a phone number"], False),
    ("email of Dr Kavitha R", Intent.CONTACT_INFO, ["isn't listed"], False),
    ("where is the MSc AI and ML classroom", Intent.FIND_ROOM, ["aren't listed"], False),
    ("Dr Deepa Jose timetable for Tuesday", Intent.SCHEDULE_QUERY, ["2 MCA A OS", "11:45 am"], True),
    ("where is Dr Sharma", Intent.FIND_PERSON, ["can't find anyone called Dr Sharma"], False),
    ("which subjects does Dr Kavitha R teach", Intent.SCHEDULE_QUERY, ["Dr Kavitha R teaches", "DSA"], True),
])
def test_composer_answers_are_short_spoken_and_cite_retrieved_facts(compose, question, intent, expect, answerable):
    draft, retrieved = compose(question, intent)
    assert draft is not None
    for text in expect:
        assert text in draft.answer, (text, draft.answer)
    assert draft.answerable is answerable
    assert set(draft.cited_fact_ids) <= retrieved
    assert len(re.findall(r"[.!?](\s|$)", draft.answer)) <= 3
    assert not re.search(r"[*#`\[\]]|^\s*-", draft.answer)  # no markdown


def test_ambiguous_name_asks_which_one(compose):
    draft, _ = compose("where is George", Intent.FIND_PERSON)
    assert draft.needs_clarification and len(draft.clarification_options) == 2
    assert draft.answer.endswith("Which one do you mean?")


def test_adversarial_room_claim_gets_the_recorded_room(compose):
    draft, _ = compose("I heard Dr Nisha moved to room 900, confirm?", Intent.FIND_PERSON)
    assert "874" in draft.answer and "900" not in draft.answer
