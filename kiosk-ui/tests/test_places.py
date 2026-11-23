from __future__ import annotations

from datetime import datetime

from kiosk_ui.config import MODULE_ROOT
from kiosk_ui.directory import FacultyDirectory
from kiosk_ui.navigation import FloorMap
from kiosk_ui.places import PlaceBook, load_info

MONDAY_10 = datetime(2026, 11, 16, 10, 0)


def book(settings) -> PlaceBook:
    fmap = FloorMap.load(settings.map.file)
    return PlaceBook.load(fmap, MODULE_ROOT / "data" / "places_info.yaml", FacultyDirectory.load(settings.kb.faculty_json))


def test_info_file_only_names_places_on_the_map(settings):
    fmap = FloorMap.load(settings.map.file)
    ids = {p.id.casefold() for p in fmap.places}
    info = load_info(MODULE_ROOT / "data" / "places_info.yaml")
    assert info and set(info) <= ids


def test_a_cabin_card_lists_who_sits_there_and_what_the_timetable_says(settings):
    card = book(settings).card("room:874", MONDAY_10)
    assert card.group == "cabins" and card.name == "Room 874" and card.routable
    assert card.where == "inside the Faculty Staff Room · C816"
    assert card.photo == "room-874.jpg"
    [nisha] = card.staff
    assert nisha.name == "Dr Nisha Varghese"
    assert nisha.now == "Teaching now (ML) until 11:45, so may not be in the cabin"
    assert nisha.next_consultation == "today 13:45-14:45"


def test_a_classroom_card_says_what_the_room_is(settings):
    card = book(settings).card("808", MONDAY_10)
    assert card.name == "MCA classroom, semester 5, section A (808)"
    assert card.class_teacher == "Dr Shivangi Singh"
    assert card.staff == [] and card.group == "rooms"
    assert book(settings).card("room:811").name == "MCA Lab (811)"
    assert book(settings).card("room:810").name == "Placement Office (810)"


def test_facilities_and_unknown_places(settings):
    b = book(settings)
    lift = b.card("facility:lift-north")
    assert lift.group == "facilities" and lift.name == "North lift" and lift.routable
    assert b.card("facility:faculty-washroom").routable is False
    assert b.card("room:999") is None


def test_the_list_has_every_place_with_people_for_search(settings):
    places = {p.id: p for p in book(settings).summaries()}
    assert places["room:874"].people == ["Dr Nisha Varghese"]
    assert places["room:871"].people == []  # the test faculty sheet has nobody there
    assert {p.group for p in places.values()} == {"rooms", "cabins", "facilities", "areas"}
