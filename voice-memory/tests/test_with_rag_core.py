"""voice-memory against the real rag-core pipeline (entity linking, retrieval routes, templates,
grounding, escalation), built offline in memory: rag-core's own test stand-ins replace the embedding
model and Chroma, and a keyword stub replaces the trained intent classifier. The campus sheet is the
SAMPLE data from rag-core/tests/kb/test_campus.py, never real data.

Skipped when rag-core isn't installed in this environment.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

rag_core = pytest.importorskip("rag_core")
RAG_TESTS = Path(rag_core.__file__).resolve().parents[2] / "tests"


def _rag_test_module(name: str, relpath: str):
    """rag-core's test helpers by file path: both modules have a `tests` package, so no plain import."""
    spec = importlib.util.spec_from_file_location(name, RAG_TESTS / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings  # noqa: E402
from rag_core.contracts import Intent  # noqa: E402
from rag_core.generation.answer import AnswerGenerator  # noqa: E402
from rag_core.kb.campus import load_campus  # noqa: E402
from rag_core.kb.facts import FactContext, build_facts  # noqa: E402
from rag_core.kb.glossary import load_glossary  # noqa: E402
from rag_core.kb.ingest_faculty import ingest  # noqa: E402
from rag_core.kb.models import FacultyKB  # noqa: E402
from rag_core.nlu.entities import EntityExtractor  # noqa: E402
from rag_core.nlu.gazetteer import build_gazetteer, load_lexicon  # noqa: E402
from rag_core.nlu.intent import IntentPrediction, IntentScore  # noqa: E402
from rag_core.pipeline import Pipeline  # noqa: E402
from rag_core.retrieval.bm25 import BM25Index  # noqa: E402
from rag_core.retrieval.retriever import FactCatalog, Retriever, topic_faculty  # noqa: E402
from rag_core.scoring.confidence import ConfidenceScorer  # noqa: E402
from rag_core.scoring.grounding_check import GroundingChecker  # noqa: E402

from voice_memory.kb import KBView  # noqa: E402
from voice_memory.receptionist import Receptionist  # noqa: E402
from voice_memory.speech.stt import TextSTT  # noqa: E402
from voice_memory.speech.tts import NoTTS, Speaker  # noqa: E402

pytestmark = pytest.mark.ragcore
NOW = datetime(2026, 10, 20, 11, 0)  # a Tuesday

# Keyword stand-in for the trained classifier (that one needs the embedding model).
_INTENTS = [
    (r"\b(talk to|speak to)\b.*\b(someone|person|human|receptionist)\b", Intent.ESCALATE_TO_HUMAN),
    (r"^(hi|hello)\b", Intent.GREETING),
    (r"\b(email|phone|number|contact)\b", Intent.CONTACT_INFO),
    (r"\b(free|available|meet|consultation)\b", Intent.PERSON_AVAILABILITY),
    (r"\b(event|events|seminar|workshop)\b", Intent.EVENT_INFO),
    (r"\b(open|wifi)\b", Intent.FACILITY_INFO),
    (r"\b(washroom|lab|classroom)\b|\broom \d", Intent.FIND_ROOM),
    (r"\b(teach|teaches|timetable|classes)\b", Intent.SCHEDULE_QUERY),
]


class KeywordIntent:
    def predict(self, text, query_vector=None):
        intent = next((i for p, i in _INTENTS if re.search(p, text, re.I)), Intent.FIND_PERSON)
        return IntentPrediction(intent=intent, probability=0.95, top=[IntentScore(intent=intent, probability=0.95)],
                                source="model", model_intent=intent, model_probability=0.95)


class InProcess:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def answer(self, question, context):
        return self.pipeline.answer(question, context, now=NOW).model_dump(mode="json")


@pytest.fixture(scope="module")
def rag_pipeline(tmp_path_factory):
    rag_conftest = _rag_test_module("rag_core_tests_conftest", "conftest.py")  # offline stand-ins
    FakeDense, FakeEmbedder = rag_conftest.FakeDense, rag_conftest.FakeEmbedder
    ours = sys.modules.get("tests.conftest")
    sys.modules["tests.conftest"] = rag_conftest  # rag-core's test_campus imports it under this name
    try:
        _write_sample = _rag_test_module("rag_core_tests_campus", "kb/test_campus.py")._write_sample
    finally:
        if ours is not None:
            sys.modules["tests.conftest"] = ours

    settings = load_settings(DEFAULT_SETTINGS_PATH)
    settings = settings.model_copy(update={"generation": settings.generation.model_copy(update={"mode": "template"})})
    records = ingest(settings.paths.raw_faculty_xlsx, settings.kb)
    kb = FacultyKB(source="data/raw/faculty.xlsx", source_sha256="test", sheet=settings.kb.faculty_sheet, faculty=records)
    glossary = load_glossary(settings.paths.glossary)
    lexicon = load_lexicon(settings.paths.entity_lexicon)
    campus = load_campus(_write_sample(tmp_path_factory.mktemp("c") / "campus.xlsx"), set(lexicon.facilities),
                         set(lexicon.events), "sample")
    ctx = FactContext(glossary, "glossary", settings.kb.home_block, settings.kb.home_floor, True)
    facts = build_facts(kb, ctx, campus)
    gazetteer = build_gazetteer(kb, glossary, lexicon, facts)
    bm25 = BM25Index.build([f.fact_id for f in facts], [" ".join([f.text, *f.keywords]) for f in facts],
                           k1=settings.retrieval.bm25_k1, b=settings.retrieval.bm25_b)
    extractor = EntityExtractor.from_settings(settings, gazetteer=gazetteer)
    pipeline = Pipeline(
        settings,
        classifier=KeywordIntent(),
        extractor=extractor,
        retriever=Retriever(FactCatalog(facts, topic_faculty(gazetteer)), FakeDense(facts), bm25, FakeEmbedder(),
                            settings.retrieval),
        generator=AnswerGenerator.from_settings(settings),
        checker=GroundingChecker(extractor),
        scorer=ConfidenceScorer(settings.scoring, settings.thresholds.unverified_glossary_weight, None),
        gazetteer=gazetteer,
    )
    names = KBView()
    for fac in records:
        names.add_person(f"faculty:{fac.id}", fac.display_name, fac.gender, fac.aliases)
    for entry in gazetteer.entries:
        names.names.setdefault(entry.canonical_id, entry.name)
    return pipeline, names


@pytest.fixture
def desk(rag_pipeline, settings):
    pipeline, names = rag_pipeline
    return Receptionist(settings, kb=names, rag=InProcess(pipeline), stt=TextSTT(), tts=Speaker(NoTTS(), settings.speech))


def test_pronoun_follow_ups_get_real_answers(desk):
    first = desk.handle_text("v1", "where is Dr Deepa V Jose's cabin?")
    assert first.outcome == "answered" and "877" in first.answer
    office = desk.handle_text("v1", "how do I get to her office?")
    assert office.question == "how do I get to Dr Deepa V Jose's office?" and "877" in office.answer
    free = desk.handle_text("v1", "is she free on Friday?")
    assert free.question == "is Dr Deepa V Jose free on Friday?"
    assert free.outcome == "answered" and "1:45 pm to 3:45 pm" in free.answer


def test_ellipsis_swaps_the_person(desk):
    desk.handle_text("v2", "where is Dr Nisha Varghese's cabin?")
    turn = desk.handle_text("v2", "and Dr Binayak Dutta?")
    assert turn.question == "where is Dr Binayak Dutta's cabin?" and "872" in turn.answer


def test_clarification_reply_is_answered(desk):
    ask = desk.handle_text("v3", "Where is Dr George?")
    assert ask.outcome == "clarification" and "Dr Jossy P George" in ask.answer
    pick = desk.handle_text("v3", "the second one")
    assert pick.question == "Where is Dr Naived George Eapen?" and pick.outcome == "answered"


def test_missing_data_escalates_with_the_receptionist_option(desk):
    turn = desk.handle_text("v4", "what is Dr Nisha Varghese's phone number?")
    assert turn.escalate and turn.escalate_reason == "not_in_kb"
    assert "Talk to the receptionist" in turn.options
    assert desk.handle_text("v4", "yes").ticket is not None


def test_campus_places_and_that_lab(desk):
    lab = desk.handle_text("v5", "where is the sample ai lab")
    assert lab.outcome == "answered" and "806" in lab.answer and "room:806" not in lab.room_ids
    open_ = desk.handle_text("v5", "when is that lab open?")
    assert "Sample AIML Lab" in open_.question and "8 am to 12:45 pm" in open_.answer
    canteen = desk.handle_text("v5", "where is the canteen")
    assert canteen.escalate  # no verified canteen row


def test_unresolved_pronoun_escalates_instead_of_guessing(desk):
    turn = desk.handle_text("v6", "where is her cabin?")
    assert turn.question == "where is her cabin?" and turn.escalate
