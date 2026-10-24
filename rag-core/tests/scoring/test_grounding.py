"""Grounding check: every claim in the answer must be in a cited, retrieved fact."""

import pytest

from rag_core.contracts import RetrievedFact
from rag_core.generation.prompts import AnswerDraft
from rag_core.nlu.entities import EntityExtractor
from rag_core.scoring.grounding_check import GroundingChecker, answer_times, fact_times

LOCATION = RetrievedFact(
    fact_id="faculty:nisha-varghese:location",
    text="Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor (on this floor).",
    metadata={"category": "faculty_location", "faculty_id": "nisha-varghese", "room": "874", "verified": True},
    score=1.0, rank=1, via="structured",
)
PROFILE = RetrievedFact(
    fact_id="faculty:nisha-varghese:profile",
    text="Dr Nisha Varghese (short name NV) is a faculty member in the Computer Science department. "
         "Email: nisha.varghese@christuniversity.in. Weekly teaching hours: 10.",
    metadata={"category": "faculty_profile", "faculty_id": "nisha-varghese", "verified": True},
    score=1.0, rank=2, via="structured",
)
CONSULT = RetrievedFact(
    fact_id="faculty:nisha-varghese:consultation",
    text='Dr Nisha Varghese\'s consultation hours, as listed: "Monday 1.45 - 2.45PM". In 24-hour time: Monday 13:45-14:45.',
    metadata={"category": "faculty_consultation", "faculty_id": "nisha-varghese", "verified": True},
    score=1.0, rank=3, via="structured",
)
FACTS = [LOCATION, PROFILE, CONSULT]


def draft(answer, cited, answerable=True):
    return AnswerDraft(answer=answer, answerable=answerable, cited_fact_ids=cited,
                       needs_clarification=False, clarification_options=[])


@pytest.fixture(scope="module")
def checker(settings, gazetteer):
    return GroundingChecker(EntityExtractor.from_settings(settings, gazetteer=gazetteer))


def test_supported_answer_passes(checker):
    r = checker.check(draft("Dr Nisha Varghese's office is room 874, here on this floor.", [LOCATION.fact_id]), FACTS)
    assert r.passed and r.claims_checked >= 2


@pytest.mark.parametrize("answer,cited,kind,span", [
    ("Dr Nisha Varghese's office is room 875.", ["faculty:nisha-varghese:location"], "unsupported_room", "room 875"),
    ("Dr Nisha Varghese's office is room 874.", ["faculty:nisha-varghese:secret"], "hallucinated_citation", "faculty:nisha-varghese:secret"),
    ("Her email is nisha.v@christuniversity.in.", ["faculty:nisha-varghese:profile"], "unsupported_email", "nisha.v@christuniversity.in"),
    ("Her phone number is 080 4012 9100.", ["faculty:nisha-varghese:profile"], "unsupported_phone", "080 4012 9100"),
    ("She meets students on Monday from 3:45 pm.", ["faculty:nisha-varghese:consultation"], "unsupported_time", "3:45 pm"),
    ("Dr Sridevi R sits in room 874.", ["faculty:nisha-varghese:location"], "unsupported_name", "Dr Sridevi R"),
    ("Dr Nisha Varghese teaches DAA.", ["faculty:nisha-varghese:location"], "unsupported_code", "DAA"),
    ("Dr Nisha Varghese's office is room 874, and room 900 is next door.", ["faculty:nisha-varghese:location"], "unsupported_room", "room 900"),
])
def test_injected_faults_are_caught(checker, answer, cited, kind, span):
    r = checker.check(draft(answer, cited), FACTS)
    assert not r.passed
    assert (kind, span) in [(i.kind, i.span) for i in r.issues]


def test_claims_need_a_citation(checker):
    r = checker.check(draft("Dr Nisha Varghese's office is room 874.", []), FACTS)
    assert not r.passed  # true, but nothing cited supports it


def test_times_match_across_formats():
    assert {"13:45", "14:45"} <= fact_times(CONSULT.text)
    assert answer_times("from 1:45 pm to 2:45 pm") == [("1:45 pm", {"13:45"}), ("2:45 pm", {"14:45"})]


def test_a_decline_may_echo_the_visitors_words(checker, settings, gazetteer):
    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    q = "where is room 900"
    r = checker.check(draft("I'm sorry, I can't find room 900 in my records.", [], answerable=False), [],
                      question=q, question_mentions=extractor.extract(q).mentions)
    assert r.passed
    # ...but an answer that agrees with a false premise doesn't get the same pass
    r = checker.check(draft("Yes, Dr Nisha Varghese moved to room 900.", [LOCATION.fact_id]), FACTS,
                      question="did Dr Nisha move to room 900")
    assert not r.passed


def test_unverified_glossary_expansion_is_flagged(checker):
    course = RetrievedFact(
        fact_id="course:ml",
        text='Course: subject code ML ("Machine Learning", full name not verified). Taught by: Dr Nisha Varghese.',
        metadata={"category": "course", "subject_code": "ML", "faculty_id": "nisha-varghese", "verified": False},
        score=1.0, rank=1, via="structured",
    )
    by_code = checker.check(draft("ML is taught by Dr Nisha Varghese.", ["course:ml"]), [course])
    by_name = checker.check(draft("Machine Learning is taught by Dr Nisha Varghese.", ["course:ml"]), [course])
    assert by_code.passed and not by_code.uses_unverified_glossary
    assert by_name.passed and by_name.uses_unverified_glossary


TEGIL = RetrievedFact(
    fact_id="faculty:tegil-j-john:location",
    text="Dr Tegil J John's office is room 881, Central Block, Eighth Floor (on this floor).",
    metadata={"category": "faculty_location", "faculty_id": "tegil-j-john", "room": "881", "verified": True},
    score=0.9, rank=1, via="bm25",
)


def test_answer_about_a_near_miss_is_a_wrong_person(checker, settings, gazetteer):
    from rag_core.contracts import Intent

    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    q = "Is Dr John Mathew in today?"
    mentions = extractor.extract(q).mentions
    wrong = draft("Dr Tegil J John's office is room 881.", [TEGIL.fact_id])
    r = checker.check(wrong, [TEGIL], question=q, question_mentions=mentions, intent=Intent.FIND_PERSON)
    assert not r.passed and [i.kind for i in r.issues] == ["wrong_person"]  # grounded, but the wrong person
    decline = draft("I'm sorry, I can't find anyone called Dr John Mathew in the faculty list. Did you mean "
                    "Dr Tegil J John? Please check with the receptionist at the desk.", [], answerable=False)
    assert checker.check(decline, [TEGIL], question=q, question_mentions=mentions, intent=Intent.FIND_PERSON).passed


def test_answer_about_someone_else_than_the_linked_person(checker, settings, gazetteer):
    from rag_core.contracts import Intent

    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    q = "where is Dr Nisha Varghese's cabin?"
    mentions = extractor.extract(q).mentions
    other = draft("Dr Tegil J John's office is room 881.", [TEGIL.fact_id])
    r = checker.check(other, [LOCATION, TEGIL], question=q, question_mentions=mentions, intent=Intent.FIND_PERSON)
    assert "wrong_person" in [i.kind for i in r.issues]
    right = draft("Dr Nisha Varghese's office is room 874.", [LOCATION.fact_id])
    assert checker.check(right, [LOCATION, TEGIL], question=q, question_mentions=mentions, intent=Intent.FIND_PERSON).passed
    # without the intent (or for a non-person intent) the person check doesn't apply
    assert "wrong_person" not in [i.kind for i in checker.check(other, [TEGIL], question=q, question_mentions=mentions).issues]


class FakeNLI:
    def contradiction(self, premise, hypotheses):
        return [0.9 if "closed" in h else 0.1 for h in hypotheses]


def test_optional_nli_flags_contradicted_sentences(settings, gazetteer):
    checker = GroundingChecker(EntityExtractor.from_settings(settings, gazetteer=gazetteer), FakeNLI(), 0.5)
    r = checker.check(draft("Room 874 is Dr Nisha Varghese's office. It is closed today.", [LOCATION.fact_id]), FACTS)
    assert [i.kind for i in r.issues] == ["nli_contradiction"]
