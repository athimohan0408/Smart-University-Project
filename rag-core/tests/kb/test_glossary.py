import pytest
from pydantic import ValidationError

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.kb.glossary import Glossary, GlossaryEntry, confidence_weight, load_glossary
from rag_core.kb.ingest_faculty import ingest

STANDARD_SUBJECTS = {
    "ML": "Machine Learning",
    "DSA": "Data Structures and Algorithms",
    "OS": "Operating Systems",
    "DBMS": "Database Management Systems",
    "SE": "Software Engineering",
    "RM": "Research Methodology",
    "CN": "Computer Networks",
    "IoT": "Internet of Things",
}


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def glossary(settings):
    return load_glossary(settings.paths.glossary)


def test_nothing_is_verified_yet(glossary):
    entries = [*glossary.programmes.values(), *glossary.subjects.values()]
    assert entries and not any(e.verified for e in entries)


def test_only_standard_subject_expansions_are_prefilled(glossary):
    expanded = {code: e.full_name for code, e in glossary.subjects.items() if e.is_expanded}
    assert expanded == STANDARD_SUBJECTS


def test_programme_expansions(glossary):
    assert glossary.programme("MSAIM").full_name == "MSc Artificial Intelligence and Machine Learning"
    assert {c for c, e in glossary.programmes.items() if e.is_expanded} == {"MSAIM", "MCA", "BCA", "BSC"}


def test_every_code_in_the_timetable_is_in_the_glossary(settings, glossary):
    slots = [s for f in ingest(settings.paths.raw_faculty_xlsx, settings.kb) for s in f.timetable]
    assert [s.subject_code for s in slots if glossary.subject(s.subject_code) is None] == []
    assert [s.programme for s in slots if s.programme and glossary.programme(s.programme) is None] == []


def test_lookup_ignores_case(glossary):
    assert glossary.subject("iot") is glossary.subject("IoT")


def test_unverified_entries_lower_confidence(settings):
    weight = settings.thresholds.unverified_glossary_weight
    verified = GlossaryEntry(full_name="Machine Learning", verified=True)
    unverified = GlossaryEntry(full_name="Machine Learning")
    todo = GlossaryEntry(full_name="TODO")
    assert confidence_weight(verified, weight) == 1.0
    assert confidence_weight(unverified, weight) == weight < 1.0
    assert confidence_weight(todo, weight) == confidence_weight(None, weight) == 0.0


def test_todo_entry_cannot_be_verified():
    with pytest.raises(ValidationError):
        GlossaryEntry(full_name="TODO", verified=True)


def test_codes_differing_only_by_case_are_rejected():
    with pytest.raises(ValidationError):
        Glossary(subjects={"IoT": {"full_name": "TODO"}, "IOT": {"full_name": "TODO"}})
