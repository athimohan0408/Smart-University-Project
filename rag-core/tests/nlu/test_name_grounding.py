"""Person resolution: exact names, similar names, ambiguous names, and names that must NOT resolve.

The bug this guards against: "Dr John Mathew" linked Dr Tegil J John through the surname alias
"John", so the kiosk answered with the wrong person's room. A first or last name may still link on
its own ("Dr John"), but a name that goes on with words of nobody's name is unlinked, and the
partial match is kept only as a near miss for "did you mean ...?".
"""

import pytest

from rag_core.contracts import EntityType
from rag_core.nlu.entities import EntityExtractor


@pytest.fixture(scope="module")
def extractor(gazetteer):
    from rag_core.config import DEFAULT_SETTINGS_PATH, load_settings

    return EntityExtractor.from_settings(load_settings(DEFAULT_SETTINGS_PATH), gazetteer=gazetteer)


def faculty(extractor, text):
    return [m for m in extractor.extract(text).mentions if m.type == EntityType.FACULTY]


@pytest.mark.parametrize("text,fid", [
    # exact names and the forms visitors use
    ("Where is Dr Tegil J John's cabin?", "faculty:tegil-j-john"),
    ("Where can I find Dr John?", "faculty:tegil-j-john"),  # a surname alone, one person has it
    ("wheres dr tegil john sitting", "faculty:tegil-j-john"),
    ("Where is Dr Tegil J. John?", "faculty:tegil-j-john"),
    ("Is Dr Deepa Jose free on Friday?", "faculty:deepa-v-jose"),
    ("Where is Dr Nisha Varghese's office?", "faculty:nisha-varghese"),
    ("Father Jossy office", "faculty:jossy-p-george"),
    # similar names must still reach the right person
    ("Dr Nisha Vargese cabin please", "faculty:nisha-varghese"),  # misspelt surname is explained by the name
    ("Where is Dr Kavitha R's room?", "faculty:kavitha-r"),
    ("Where is Dr Rajesh Kanna R?", "faculty:rajesh-kanna-r"),
    ("Is Dr Neetha S S on this floor?", "faculty:neetha-s-s"),
    ("Where is Dr Neha Singhal?", "faculty:neha-singhal"),
    ("where is doctor kavita r", "faculty:kavitha-r"),
    # words next to the name that are not more name
    ("I heard Dr Nisha moved to room 900, can you confirm?", "faculty:nisha-varghese"),
    ("Is Dr Gobi Sir in?", "faculty:gobi-r"),
    ("Dr Sridevi Ma'am please", "faculty:sridevi-r"),
    ("Dr Somnath CS department", "faculty:somnath-sinha"),
    ("Kindly Dr Nisha please", "faculty:nisha-varghese"),
    ("Kindly Nisha ma'am, where is she?", "faculty:nisha-varghese"),
    ("Is Dr Chandra Monday free?", "faculty:chandra-j"),
    ("Dr Nisha Varghese. Where is she?", "faculty:nisha-varghese"),
])
def test_names_that_must_resolve(extractor, text, fid):
    found = faculty(extractor, text)
    assert [m.canonical_id for m in found] == [fid], (text, [(m.text, m.method) for m in found])
    assert not found[0].near_misses


@pytest.mark.parametrize("text,candidates", [
    ("Where is Dr George?", {"faculty:jossy-p-george", "faculty:naived-george-eapen"}),
    ("Where is Dr Neeha?", {"faculty:neetha-s-s", "faculty:neha-singhal"}),
])
def test_ambiguous_names_are_flagged_not_picked(extractor, text, candidates):
    [m] = faculty(extractor, text)
    assert m.ambiguous and m.canonical_id is None and {c.canonical_id for c in m.candidates} == candidates


@pytest.mark.parametrize("text,surface,near,confirm", [
    ("Is Dr John Mathew in today?", "Dr John Mathew", {"faculty:tegil-j-john"}, False),
    ("Where is Dr Mathew John?", "Dr Mathew John", {"faculty:tegil-j-john"}, False),
    ("Where is Dr John K Mathew?", "Dr John K Mathew", {"faculty:tegil-j-john"}, False),
    ("Where is Dr John K. Mathew?", "Dr John K. Mathew", {"faculty:tegil-j-john"}, False),
    ("Where is John Mathew?", "John Mathew", {"faculty:tegil-j-john"}, False),
    ("Where is Dr Nisha Sharma's cabin?", "Dr Nisha Sharma", {"faculty:nisha-varghese"}, False),
    ("What is Dr Kavitha Menon's email?", "Dr Kavitha Menon", {"faculty:kavitha-r"}, False),
    ("Can I meet Dr Deepa Nair today?", "Dr Deepa Nair", {"faculty:deepa-v-jose"}, False),
    ("Where does Dr George Kurian sit?", "Dr George Kurian", {"faculty:jossy-p-george", "faculty:naived-george-eapen"}, False),
    # all-lowercase / all-caps: no case signal, so ask the visitor to confirm instead of declining
    ("where is dr john mathew", "dr john mathew", {"faculty:tegil-j-john"}, True),
    ("where is dr sridevi krishnan", "dr sridevi krishnan", {"faculty:sridevi-r"}, True),
    ("DR JOHN MATHEW", "DR JOHN MATHEW", {"faculty:tegil-j-john"}, True),
])
def test_wrong_person_is_never_linked(extractor, text, surface, near, confirm):
    [m] = faculty(extractor, text)
    assert m.canonical_id is None and not m.ambiguous and not m.candidates
    assert m.to_entity().canonical_id is None  # the contract shows it as unlinked
    assert m.text == surface and m.method == "partial_name"
    assert {c.canonical_id for c in m.near_misses} == near
    assert m.needs_confirmation is confirm


def test_no_fact_text_trips_the_check(extractor, entity_kb):
    """Facts only name real faculty: a partial-name hit there would be a false positive (and answers
    are built from fact text, so it would wrongly fail grounding)."""
    _, _, facts = entity_kb
    hits = [(f.fact_id, m.text) for f in facts for m in extractor.extract(f.text).mentions if m.method == "partial_name"]
    assert hits == []
