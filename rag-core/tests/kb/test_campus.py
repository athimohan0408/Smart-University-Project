"""Campus places and events (kb/campus.py): loading rules, facts, linking, retrieval and spoken answers.

The sheet here is SAMPLE data written for the tests (made-up names, rooms and dates). It is never
copied into data/raw/: real rows must come from the department with a 'Verified By'.
"""

from datetime import datetime

import pandas as pd
import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.contracts import EntityType, Intent
from rag_core.generation.templates import FactComposer
from rag_core.kb.campus import (
    EVENT_COLUMNS,
    PLACE_COLUMNS,
    load_campus,
    parse_hhmm,
    parse_hours,
    write_template,
)
from rag_core.kb.facts import FactCategory, FactContext, build_facts
from rag_core.nlu.entities import EntityExtractor
from rag_core.nlu.gazetteer import build_gazetteer, load_lexicon
from rag_core.retrieval.bm25 import BM25Index
from rag_core.retrieval.retriever import FactCatalog, Retriever, topic_faculty
from rag_core.scoring.grounding_check import GroundingChecker
from tests.conftest import FakeDense, FakeEmbedder

NOW = datetime(2026, 10, 20, 11, 0)  # a Tuesday

SAMPLE_PLACES = [
    # Name, Kind, Aliases, Block, Floor, Room, Directions, Hours, Programmes, Notes, Verified By, Verified On
    ["Sample Ladies Washroom", "washroom", "", "Central Block", "Eighth Floor", "", "Turn right from the lift", "", "", "", "test", "2026-10-01"],
    ["Sample Gents Washroom", "washroom", "", "Central Block", "Eighth Floor", "", "Turn left from the lift", "", "", "", "test", "2026-10-01"],
    ["Sample AIML Lab", "lab", "sample ai lab", "Central Block", "Eighth Floor", "806", "Third door past the reception",
     "08:00-12:45; 13:45-17:00", "MSAIM", "Bring your ID card", "test", "2026-10-01"],
    ["Sample MSAIM Classroom", "classroom", "", "Central Block", "Ninth Floor", "905", "", "", "MSAIM", "", "test", "2026-10-01"],
    ["Sample Floor Wifi", "wifi", "", "Central Block", "Eighth Floor", "", "", "", "", "Network name SAMPLE-NET", "test", "2026-10-01"],
    ["Sample Seminar Hall", "seminar_hall", "", "Central Block", "Eighth Floor", "890", "", "", "", "", "test", "2026-10-01"],
    # rows that must NOT load
    ["Sample Unverified Printer", "printing", "", "Central Block", "Eighth Floor", "", "", "", "", "", "", ""],
    ["Sample Swimming Pool", "pool", "", "Central Block", "Eighth Floor", "", "", "", "", "", "test", ""],
    ["Sample Canteen", "canteen", "", "Central Block", "Ground Floor", "", "", "9 to 5", "", "", "test", ""],
]
SAMPLE_EVENTS = [
    # Title, Kind, Date, Start, End, Venue, Organiser, Audience, Description, Verified By, Verified On
    ["Sample Talk on Graphs", "seminar", "2026-10-20", "10:00", "11:30", "Sample Seminar Hall", "Sample Club", "Open to all", "", "test", ""],
    ["Sample Python Workshop", "workshop", "2026-10-23", "14:00", "16:00", "Sample AIML Lab", "", "MSAIM students", "", "test", ""],
    ["Sample Old Seminar", "seminar", "2026-09-01", "10:00", "11:00", "Sample Seminar Hall", "", "", "", "test", ""],
    ["Sample Unverified Fest", "fest", "2026-10-21", "", "", "", "", "", "", "", ""],
]


def _write_sample(path):
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(SAMPLE_PLACES, columns=list(PLACE_COLUMNS)).to_excel(writer, sheet_name="Places", index=False)
        pd.DataFrame(SAMPLE_EVENTS, columns=list(EVENT_COLUMNS)).to_excel(writer, sheet_name="Events", index=False)
    return path


@pytest.fixture(scope="module")
def lexicon():
    return load_lexicon(load_settings(DEFAULT_SETTINGS_PATH).paths.entity_lexicon)


@pytest.fixture(scope="module")
def campus(tmp_path_factory, lexicon):
    path = _write_sample(tmp_path_factory.mktemp("campus") / "campus_info.xlsx")
    return load_campus(path, set(lexicon.facilities), set(lexicon.events), "tests/campus_info.xlsx")


@pytest.fixture(scope="module")
def stack(entity_kb, campus, lexicon):
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    kb, glossary, _ = entity_kb
    ctx = FactContext(glossary, "data/kb/glossary.yaml", settings.kb.home_block, settings.kb.home_floor,
                      settings.facts.include_unverified_expansions)
    facts = build_facts(kb, ctx, campus)
    gazetteer = build_gazetteer(kb, glossary, lexicon, facts)
    bm25 = BM25Index.build([f.fact_id for f in facts], [" ".join([f.text, *f.keywords]) for f in facts],
                           k1=settings.retrieval.bm25_k1, b=settings.retrieval.bm25_b)
    retriever = Retriever(FactCatalog(facts, topic_faculty(gazetteer)), FakeDense(facts), bm25, FakeEmbedder(),
                          settings.retrieval)
    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    composer = FactComposer(settings.generation)
    checker = GroundingChecker(extractor)

    def ask(question, intent):
        mentions = extractor.extract(question, now=NOW).mentions
        retrieved = retriever.retrieve(question, intent, mentions)
        draft = composer.compose(question, intent, mentions, retrieved, NOW)
        if draft is None:  # no template: the pipeline escalates
            return None, None, mentions
        grounding = checker.check(draft, retrieved, question=question, question_mentions=mentions, intent=intent)
        return draft, grounding, mentions

    return {"facts": facts, "ask": ask, "extractor": extractor}


# --------------------------------------------------------------------------- loading


def test_only_verified_well_formed_rows_load(campus):
    names = {p.name for p in campus.places}
    assert "Sample AIML Lab" in names and len(campus.places) == 6
    assert not names & {"Sample Unverified Printer", "Sample Swimming Pool", "Sample Canteen"}
    reasons = " | ".join(campus.skipped)
    assert "no 'Verified By'" in reasons and "'pool' is not in the lexicon" in reasons and "HH:MM-HH:MM" in reasons
    assert [e.title for e in campus.events] == ["Sample Old Seminar", "Sample Talk on Graphs", "Sample Python Workshop"]
    lab = next(p for p in campus.places if p.name == "Sample AIML Lab")
    assert lab.id == "facility:lab:sample-aiml-lab" and lab.hours == [("08:00", "12:45"), ("13:45", "17:00")]
    assert next(e for e in campus.events if e.title == "Sample Python Workshop").place_id == lab.id


def test_time_parsing_never_guesses():
    assert parse_hhmm("9.30") == "09:30" and parse_hhmm("14:00") == "14:00"
    assert parse_hhmm("2 pm") is None and parse_hhmm("2-4") is None
    assert parse_hours("") == [] and parse_hours("17:00-08:00") is None


def test_missing_or_empty_sheet_leaves_faculty_facts_unchanged(entity_kb, tmp_path, lexicon):
    kb, glossary, faculty_only = entity_kb
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    ctx = FactContext(glossary, "data/kb/glossary.yaml", settings.kb.home_block, settings.kb.home_floor,
                      settings.facts.include_unverified_expansions)
    template = tmp_path / "template.xlsx"
    write_template(template, lexicon.facilities, lexicon.events)
    for path in (template, tmp_path / "missing.xlsx"):
        campus = load_campus(path, set(lexicon.facilities), set(lexicon.events), "x")
        assert campus.is_empty and not campus.skipped
        assert build_facts(kb, ctx, campus) == faculty_only


def test_committed_template_is_empty_and_loads(lexicon):
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    campus = load_campus(settings.paths.campus_xlsx, set(lexicon.facilities), set(lexicon.events), "x")
    assert campus.skipped == []
    assert set(pd.ExcelFile(settings.paths.campus_xlsx).sheet_names) >= {"Places", "Events", "README"}


# --------------------------------------------------------------------------- facts + linking


def test_campus_facts_and_programme_classrooms(stack):
    facts = {f.fact_id: f for f in stack["facts"]}
    lab = facts["facility:lab:sample-aiml-lab"]
    assert lab.category == FactCategory.FACILITY and lab.room == "806" and lab.programme == "MSAIM"
    assert "(on this floor)" in lab.text and "8:00-12:45; 13:45-17:00" in lab.text
    assert "(not on this floor)" in facts["facility:classroom:sample-msaim-classroom"].text
    assert facts["event:2026-10-23:sample-python-workshop"].day == "Friday"
    assert "Classrooms and labs used by MSAIM: Sample AIML Lab (room 806" in facts["programme:msaim"].text
    assert "Classroom locations for MSAIM are not listed" not in facts["programme:msaim"].text


def test_words_link_to_kinds_with_data_only(stack):
    extract = stack["extractor"].extract
    washroom = next(m for m in extract("where is the washroom", now=NOW).mentions if m.type == EntityType.FACILITY)
    assert washroom.canonical_id == "facility:washroom"
    canteen = next(m for m in extract("where is the canteen", now=NOW).mentions if m.type == EntityType.FACILITY)
    assert canteen.canonical_id is None  # no verified canteen row: still unlinked
    named = next(m for m in extract("where is the sample ai lab", now=NOW).mentions if m.type == EntityType.FACILITY)
    assert named.canonical_id == "facility:lab:sample-aiml-lab"
    room = next(m for m in extract("where is room 806", now=NOW).mentions if m.type == EntityType.ROOM)
    assert room.canonical_id == "room:806"
    assert not [m for m in extract("when is the ML lab", now=NOW).mentions if m.type == EntityType.FACILITY]


# --------------------------------------------------------------------------- answers


@pytest.mark.parametrize("question,intent,expect,cited", [
    ("where is the washroom", Intent.FIND_ROOM, ["Sample Ladies Washroom", "Sample Gents Washroom", "here on this floor"],
     {"facility:washroom:sample-ladies-washroom", "facility:washroom:sample-gents-washroom"}),
    ("where is the sample ai lab", Intent.FIND_ROOM, ["room 806", "Third door past the reception", "Bring your ID card"],
     {"facility:lab:sample-aiml-lab"}),
    ("when is the sample ai lab open", Intent.FACILITY_INFO, ["8 am to 12:45 pm and 1:45 pm to 5 pm"],
     {"facility:lab:sample-aiml-lab"}),
    ("where is room 806", Intent.FIND_ROOM, ["Sample AIML Lab", "room 806"], {"facility:lab:sample-aiml-lab"}),
    ("is there wifi here", Intent.FACILITY_INFO, ["Sample Floor Wifi", "SAMPLE-NET"], {"facility:wifi:sample-floor-wifi"}),
    ("where is the MSc AIML classroom", Intent.FIND_ROOM, ["Sample MSAIM Classroom", "not on this floor"], None),
    ("any events today", Intent.EVENT_INFO, ["Sample Talk on Graphs", "10 am to 11:30 am", "Sample Seminar Hall"],
     {"event:2026-10-20:sample-talk-on-graphs"}),
    ("is there a workshop on Friday", Intent.EVENT_INFO, ["Sample Python Workshop", "Friday, 23 October", "2 pm to 4 pm"],
     {"event:2026-10-23:sample-python-workshop"}),
])
def test_campus_answers_are_grounded(stack, question, intent, expect, cited):
    draft, grounding, _ = stack["ask"](question, intent)
    assert draft is not None and draft.answerable, draft
    for text in expect:
        assert text in draft.answer, (text, draft.answer)
    if cited is not None:
        assert set(draft.cited_fact_ids) == cited
    assert grounding.passed, grounding.issues


def test_upcoming_events_skip_past_ones(stack):
    draft, grounding, _ = stack["ask"]("what events are coming up", Intent.EVENT_INFO)
    assert "Sample Old Seminar" not in draft.answer
    assert "Sample Talk on Graphs" in draft.answer and "Sample Python Workshop" in draft.answer
    assert grounding.passed


@pytest.mark.parametrize("question,intent", [
    ("where is the canteen", Intent.FIND_ROOM),
    ("is there a fest tomorrow", Intent.EVENT_INFO),
    ("any seminar on Saturday", Intent.EVENT_INFO),
    ("where can I take a printout", Intent.FACILITY_INFO),
])
def test_no_verified_data_declines(stack, question, intent):
    draft, _, _ = stack["ask"](question, intent)
    assert draft is None or not draft.answerable
