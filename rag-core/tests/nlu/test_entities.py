"""Entity extraction + linking on the real KB vocabulary (gazetteer built in memory, fixed clock)."""

from datetime import datetime

import pytest

from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings
from rag_core.contracts import Entity, EntityType
from rag_core.kb.facts import FactCategory
from rag_core.nlu.entities import EntityExtractor
from rag_core.nlu.gazetteer import (
    AliasSource,
    GazetteerUnavailable,
    load_gazetteer,
    normalise,
    room_key,
    save_gazetteer,
    tokenize,
)

WEDNESDAY_2PM = datetime(2026, 10, 7, 14, 5)
GEORGES = {"faculty:jossy-p-george", "faculty:naived-george-eapen"}


@pytest.fixture(scope="module")
def settings():
    return load_settings(DEFAULT_SETTINGS_PATH)


@pytest.fixture(scope="module")
def extractor(settings, gazetteer):
    return EntityExtractor.from_settings(settings, gazetteer=gazetteer, clock=lambda: WEDNESDAY_2PM)


def _ids(extractor, text, type_=None, **kw):
    return [m.canonical_id for m in extractor.extract(text, **kw).mentions if type_ is None or m.type == type_]


def _one(extractor, text, type_, **kw):
    found = [m for m in extractor.extract(text, **kw).mentions if m.type == type_]
    assert len(found) == 1, (text, found)
    return found[0]


# --------------------------------------------------------------------------- gazetteer


def test_every_linked_id_exists_in_the_index(gazetteer, entity_kb):
    _, _, facts = entity_kb
    fact_ids = {f.fact_id for f in facts}
    for entry in gazetteer.entries:
        if entry.type == EntityType.FACULTY:
            assert f"{entry.canonical_id}:profile" in fact_ids
        elif entry.type != EntityType.TOPIC:
            assert entry.canonical_id in fact_ids, entry.canonical_id


def test_glossary_todo_is_never_an_alias(gazetteer):
    for entry in gazetteer.entries:
        assert all(a.text.strip().upper() != "TODO" for a in entry.aliases), entry.canonical_id
    c_prog = gazetteer.entry("course:c-prog")
    assert [(a.text, a.source) for a in c_prog.aliases] == [("C PROG", AliasSource.CODE)]


def test_room_ids_come_from_room_facts(gazetteer, entity_kb):
    _, _, facts = entity_kb
    rooms = {f.fact_id for f in facts if f.category == FactCategory.ROOM}
    assert {e.canonical_id for e in gazetteer.entries if e.type == EntityType.ROOM} == rooms


def test_round_trip_and_stale_detection(tmp_path, gazetteer, entity_kb):
    _, _, facts = entity_kb
    path, facts_path = tmp_path / "gazetteer.json", tmp_path / "facts.jsonl"
    with pytest.raises(GazetteerUnavailable, match="just index"):
        load_gazetteer(path)
    save_gazetteer(gazetteer, path)
    facts_path.write_text("".join(f.model_dump_json() + "\n" for f in facts), encoding="utf-8", newline="\n")
    assert load_gazetteer(path, facts_path) == gazetteer
    facts_path.write_text(facts_path.read_text(encoding="utf-8")[:-50], encoding="utf-8", newline="\n")
    with pytest.raises(GazetteerUnavailable, match="different facts"):
        load_gazetteer(path, facts_path)


@pytest.mark.parametrize(
    ("text", "norm"),
    [("Dr. Deepa V. Jose's", "dr deepa v jose"), ("M.Sc AI & ML", "msc ai and ml"), ("ma'am", "maam"), ("607-A", "607 a")],
)
def test_normalise(text, norm):
    assert normalise(text) == norm


def test_possessive_is_outside_the_token_span():
    (token,) = tokenize("Jose's")
    assert (token.raw, token.end) == ("Jose", 4)


@pytest.mark.parametrize(("room", "key"), [("014", "14"), ("332 C", "332C"), ("628-11", "62811"), ("607a", "607A")])
def test_room_key(room, key):
    assert room_key(room) == key


# --------------------------------------------------------------------------- people


@pytest.mark.parametrize(
    ("text", "faculty_id"),
    [
        ("Where is Dr. Deepa V. Jose's cabin?", "faculty:deepa-v-jose"),
        ("Sridevi ma'am", "faculty:sridevi-r"),
        ("where is sri devi", "faculty:sridevi-r"),  # ASR split
        ("Father Jossy", "faculty:jossy-p-george"),
        ("peter augustine sir", "faculty:peter-augustin-d"),  # email spelling of Augustin
        ("Dr Thirunavukarasu", "faculty:thirunavukkarasu-v"),  # typo
        ("Neha Singh madam", "faculty:neha-singhal"),  # two-word fuzzy beats 'Neha' + 'Singh' separately
        ("rupali wagh", "faculty:rupali-sunil-wagh"),
    ],
)
def test_name_variants(extractor, text, faculty_id):
    assert _ids(extractor, text, EntityType.FACULTY) == [faculty_id]


def test_shared_name_is_flagged_not_picked(extractor):
    mention = _one(extractor, "Where is George sir?", EntityType.FACULTY)
    assert mention.ambiguous and mention.canonical_id is None
    assert {c.canonical_id for c in mention.candidates} == GEORGES
    assert {c.name for c in mention.candidates} == {"Dr Jossy P George", "Dr Naived George Eapen"}
    assert mention.to_entity() == Entity(type="faculty", text="George sir", canonical_id=None, score=1.0)


def test_near_equal_fuzzy_matches_are_flagged(extractor):
    mention = _one(extractor, "Where is Dr Neeha?", EntityType.FACULTY)
    assert mention.ambiguous
    assert {c.canonical_id for c in mention.candidates} == {"faculty:neetha-s-s", "faculty:neha-singhal"}


def test_prior_entities_break_a_tie_only_when_exactly_one_candidate_is_known(extractor):
    resolved = _one(extractor, "where is George", EntityType.FACULTY, prior_entities=["faculty:naived-george-eapen", "room:874"])
    assert (resolved.ambiguous, resolved.resolved_by_context, resolved.canonical_id) == (
        False, True, "faculty:naived-george-eapen"
    )
    still = _one(extractor, "where is George", EntityType.FACULTY, prior_entities=sorted(GEORGES))
    assert still.ambiguous and not still.resolved_by_context


def test_unknown_name_after_a_title_is_unlinked(extractor, settings):
    mention = _one(extractor, "Where is Dr Ravi Kumar's cabin?", EntityType.FACULTY)
    assert (mention.text, mention.canonical_id) == ("Dr Ravi Kumar", None)
    assert mention.score == settings.entities.unknown_name_score
    assert _ids(extractor, "my father wants to meet someone", EntityType.FACULTY) == []


@pytest.mark.parametrize(
    ("text", "faculty_ids"),
    [
        ("Where is SU sir?", ["faculty:sudhakar-t"]),
        ("where is DVJ", ["faculty:deepa-v-jose"]),
        ("dr su cabin", ["faculty:sudhakar-t"]),  # any case next to a title
        ("where is su", []),  # lower case alone: could be anything
        ("same as sir said", []),  # "as" is a word: "sir" after it is not enough
        ("Dr as cabin", ["faculty:ashwini-patil"]),
        ("WHERE IS AS", []),  # all-caps input: case says nothing
        ("RM sir cabin", ["faculty:ramamurthy-b"]),
        ("Who teaches RM?", []),  # RM is also a course code: needs the honorific
    ],
)
def test_short_codes(extractor, text, faculty_ids):
    assert _ids(extractor, text, EntityType.FACULTY) == faculty_ids


def test_short_code_score_is_capped(extractor, settings):
    assert _one(extractor, "Where is SU sir?", EntityType.FACULTY).score == settings.entities.short_code_score


def test_surface_text_keeps_honorifics_and_converts_to_the_contract(extractor):
    entities = extractor.extract("Is Dr. Sridevi ma'am free?").entities
    assert entities == [Entity(type="faculty", text="Dr. Sridevi ma'am", canonical_id="faculty:sridevi-r", score=1.0)]


# --------------------------------------------------------------------------- subjects, topics, programmes


def test_subject_name_links_course_and_research_topic_with_glossary_trust(extractor, settings):
    mentions = extractor.extract("Who teaches machine learning?").mentions
    by_type = {m.type: m for m in mentions}
    assert set(by_type) == {EntityType.COURSE, EntityType.TOPIC}
    course = by_type[EntityType.COURSE]
    assert course.canonical_id == "course:ml" and not course.candidates[0].verified
    assert course.score == settings.thresholds.unverified_glossary_weight  # ML -> "Machine Learning" is unverified
    assert by_type[EntityType.TOPIC].canonical_id == "topic:machine-learning"
    assert _one(extractor, "who teaches ML", EntityType.COURSE).score == 1.0  # the code itself is literal


@pytest.mark.parametrize("text", ["who handles M L", "ML", "machine learnin"])
def test_ml_variants(extractor, text):
    assert "course:ml" in _ids(extractor, text, EntityType.COURSE)


def test_deep_learning_is_a_research_area_not_a_course(extractor):
    assert _ids(extractor, "who works on deep learning") == ["topic:deep-learning"]


@pytest.mark.parametrize(
    "text",
    [
        "who teaches c programming",  # C PROG has no verified name; "programming fundamentals" must not match
        "who is teaching web development",  # "development" must not match "API Development"
        "who teaches learning",  # a subset of "deep learning" is not a match
    ],
)
def test_no_link_without_a_real_match(extractor, text):
    assert [m.type for m in extractor.extract(text).mentions] == []


@pytest.mark.parametrize(
    "text", ["MSc AIML", "M.Sc AI and ML", "AIML", "MSc in AI and ML", "M.Sc. in Artificial Intelligence and Machine Learning"]
)
def test_programme_names(extractor, text):
    assert _ids(extractor, text, EntityType.PROGRAMME) == ["programme:msaim"]


def test_exact_case_codes(extractor):
    assert _ids(extractor, "Who teaches SE?", EntityType.COURSE) == ["course:se"]
    assert _ids(extractor, "room 874 se aage", EntityType.COURSE) == []  # Hinglish "se"


@pytest.mark.parametrize(
    ("text", "facilities"),
    [("DBMS lab faculty", []), ("where is the computer lab", ["lab"]), ("MCA lab", ["lab"]), ("staff room 607A", ["staff_room"])],
)
def test_lab_after_a_subject_is_a_class(extractor, text, facilities):
    assert [m.kind for m in extractor.extract(text).of_type(EntityType.FACILITY)] == facilities


# --------------------------------------------------------------------------- rooms


@pytest.mark.parametrize(
    ("text", "room_id"),
    [
        ("Where is room 874?", "room:874"),
        ("how to go to 885", "room:885"),
        ("room no 890 kidhar hai", "room:890"),
        ("room 8 7 9", "room:879"),
        ("8 7 4 kahan hai", "room:874"),
        ("room eight seven seven", "room:877"),
        ("room 14", "room:014"),
        ("staff room 607 A", "room:607a"),
        ("Where is room 999?", None),  # recognised, not in the KB
    ],
)
def test_rooms(extractor, text, room_id):
    assert _ids(extractor, text, EntityType.ROOM) == [room_id]


def test_numbers_that_are_not_rooms(extractor):
    assert _ids(extractor, "meet him at 16", EntityType.ROOM) == []  # a time, not room 16
    assert _ids(extractor, "2nd semester 999 students", EntityType.ROOM) == []


# --------------------------------------------------------------------------- days and times


def test_relative_days_and_now_use_the_clock(extractor):
    mentions = extractor.extract("is she free now or tomorrow, not today").mentions
    assert [m.canonical_id for m in mentions] == ["time:14:05", "day:thursday", "day:wednesday"]
    assert mentions[0].time.day == "Wednesday" and mentions[0].time.date.isoformat() == "2026-10-07"


def test_now_argument_overrides_the_clock(extractor):
    assert _ids(extractor, "today", now="2026-10-05T09:00:00") == ["day:monday"]
    assert _ids(extractor, "right now", now=datetime(2026, 10, 5, 9, 0)) == ["time:09:00"]


@pytest.mark.parametrize(
    ("text", "time_id"),
    [
        ("at 2", "time:14:00"),  # no am/pm: the day window picks 14:00
        ("at 10 tomorrow", "time:10:00"),
        ("2.30 pm", "time:14:30"),
        ("after 3 pm", "time:15:00-17:00"),
        ("before 11", "time:07:30-11:00"),
        ("between 2 and 4", "time:14:00-16:00"),
        ("from 11.45 to 1.45", "time:11:45-13:45"),
        ("after lunch", "time:13:45-17:00"),
        ("on friday afternoon", "time:12:00-17:00"),
        ("12 noon", "time:12:00"),
        ("at 7", None),  # neither 07:00 nor 19:00 is inside the day window: unresolved, not guessed
    ],
)
def test_times(extractor, text, time_id):
    assert _ids(extractor, text, EntityType.TIME) == [time_id]


@pytest.mark.parametrize("text", ["good morning madam", "on the 2nd floor", "1-2 people", "semester 2 to"])
def test_not_times(extractor, text):
    assert _ids(extractor, text, EntityType.TIME) == []


def test_named_periods_are_marked_approximate(extractor):
    assert _one(extractor, "after lunch", EntityType.TIME).time.approximate
    assert not _one(extractor, "at 2 pm", EntityType.TIME).time.approximate


@pytest.mark.parametrize(
    ("text", "day_id"),
    [("Saturday's classes", "day:saturday"), ("on Fri", "day:friday"), ("thurs", "day:thursday"), ("day after tomorrow", "day:friday")],
)
def test_days(extractor, text, day_id):
    assert _ids(extractor, text, EntityType.DAY) == [day_id]


def test_word_abbreviations_need_a_capital(extractor):
    assert _ids(extractor, "I sat in the sun", EntityType.DAY) == []


# --------------------------------------------------------------------------- facilities and events


def test_facilities_and_events_stay_unlinked(extractor):
    mentions = extractor.extract("which event is in the seminar hall, and where is the washroom").mentions
    assert [(m.type, m.kind, m.canonical_id) for m in mentions] == [
        (EntityType.EVENT, "event", None),
        (EntityType.FACILITY, "seminar_hall", None),
        (EntityType.FACILITY, "washroom", None),
    ]
