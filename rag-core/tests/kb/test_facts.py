import pytest
import yaml

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.kb.facts import CATEGORIES_WITHOUT_SOURCE, FactCategory, FactContext, build_facts
from rag_core.kb.glossary import Glossary, load_glossary
from rag_core.kb.ingest_faculty import ingest
from rag_core.kb.models import FacultyKB

METADATA_KEYS = {
    "fact_id", "category", "faculty_id", "room", "floor", "day", "subject_code", "programme", "verified", "source_file",
}


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def kb(settings):
    records = ingest(settings.paths.raw_faculty_xlsx, settings.kb)
    return FacultyKB(source="data/raw/faculty.xlsx", source_sha256="test", sheet=settings.kb.faculty_sheet, faculty=records)


def _ctx(settings, glossary: Glossary | None = None, *, expansions: bool = True) -> FactContext:
    return FactContext(
        glossary=glossary or load_glossary(settings.paths.glossary),
        glossary_source="data/kb/glossary.yaml",
        home_block=settings.kb.home_block,
        home_floor=settings.kb.home_floor,
        include_unverified_expansions=expansions,
    )


@pytest.fixture(scope="module")
def facts(kb, settings):
    return build_facts(kb, _ctx(settings))


@pytest.fixture(scope="module")
def by_id(facts):
    return {f.fact_id: f for f in facts}


def test_every_faculty_has_exactly_one_location_fact(kb, facts):
    locations = [f for f in facts if f.category == FactCategory.FACULTY_LOCATION]
    assert sorted(f.fact_id for f in locations) == sorted(f"faculty:{fac.id}:location" for fac in kb.faculty)


def test_fact_ids_are_unique_and_identical_across_builds(kb, settings, facts):
    again = build_facts(kb, _ctx(settings))
    assert len({f.fact_id for f in facts}) == len(facts)
    assert [(f.fact_id, f.text) for f in again] == [(f.fact_id, f.text) for f in facts]


def test_metadata_is_scalar_with_every_key(facts):
    for fact in facts:
        meta = fact.chroma_metadata()
        assert set(meta) == METADATA_KEYS
        assert all(isinstance(v, (str, bool)) for v in meta.values())
        for key in ("faculty_id", "subject_code", "programme", "day"):
            assert ", " not in meta[key]  # lists are comma-joined without spaces


def test_location_facts(by_id):
    assert by_id["faculty:nisha-varghese:location"].text == (
        "Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor (on this floor)."
    )
    off_floor = by_id["faculty:ashwini-patil:location"].text
    assert "not on this floor" in off_floor and "Block II, First Floor, room 607A" in off_floor
    assert "share this office with Dr Shivangi Singh" in by_id["faculty:neetha-s-s:location"].text
    assert 'listed as "Director, Center for Research (PhD)"' in by_id["faculty:shoney-sebastian:location"].text
    assert by_id["faculty:suresh-k:location"].text == "Dr Suresh K's office location is not listed."


def test_missing_values_are_stated_not_invented(by_id):
    assert by_id["faculty:tegil-j-john:consultation"].text == "Consultation hours for Dr Tegil J John are not listed."
    assert "Email: not listed." in by_id["faculty:kavitha-r:profile"].text
    assert "no classes are listed" in by_id["faculty:nisha-varghese:schedule:wednesday"].text


def test_schedule_fact(by_id):
    assert by_id["faculty:nisha-varghese:schedule:tuesday"].text == (
        "On Tuesday, Dr Nisha Varghese has: 7:30-9:00 5 MSAIM TML; 11:45-13:45 2 MSAIM ML LAB."
    )


def test_unverified_subject_names_are_marked(by_id):
    ml = by_id["faculty:nisha-varghese:teaching:ml"]
    assert 'subject code ML ("Machine Learning", full name not verified)' in ml.text
    assert not ml.verified
    assert "subject code TML (full name not verified)" in by_id["faculty:nisha-varghese:teaching:tml"].text


def test_unverified_expansions_can_be_switched_off(kb, settings):
    facts = {f.fact_id: f for f in build_facts(kb, _ctx(settings, expansions=False))}
    assert "Machine Learning" not in facts["faculty:nisha-varghese:teaching:ml"].text
    assert "subject code ML (full name not verified)" in facts["faculty:nisha-varghese:teaching:ml"].text


def test_verified_glossary_names_are_used_plainly(kb, settings):
    raw = yaml.safe_load(settings.paths.glossary.read_text(encoding="utf-8"))
    raw["subjects"]["ML"]["verified"] = True
    raw["programmes"]["MSAIM"]["verified"] = True
    facts = {f.fact_id: f for f in build_facts(kb, _ctx(settings, Glossary.model_validate(raw)))}
    ml = facts["faculty:nisha-varghese:teaching:ml"]
    assert ml.text.startswith("Dr Nisha Varghese teaches Machine Learning (ML): 2nd semester MSAIM")
    assert ml.verified


def test_conflicts_become_cautions(by_id):
    helen = by_id["faculty:helen-k-joy:location"]
    assert "875" in helen.text and not helen.verified
    assert "Dr Helen K Joy" in by_id["room:875"].text and not by_id["room:875"].verified
    assert "Caution" in by_id["faculty:ashwini-patil:profile"].text


def test_partial_consultation_is_unverified(by_id):
    vijay = by_id["faculty:vijay-arputharaj-j:consultation"]
    assert '"Monday to"' in vijay.text and not vijay.verified


def test_programme_and_course_facts_say_classrooms_are_not_listed(by_id):
    assert "Classroom locations for MSAIM are not listed." in by_id["programme:msaim"].text
    assert "Classroom locations for ML are not listed." in by_id["course:ml"].text


def test_no_invented_events_or_facilities(facts):
    assert not [f for f in facts if f.category in CATEGORIES_WITHOUT_SOURCE]
