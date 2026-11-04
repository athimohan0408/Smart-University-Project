from __future__ import annotations

import pytest

from kiosk_ui.config import MODULE_ROOT
from kiosk_ui.navigation import FloorMap

MAP = MODULE_ROOT / "data" / "floor_map.yaml"
# the cabins in the Faculty Staff Room sketch, and the outer rooms on the department's floor-plan board
CABINS = [*range(871, 886), 888, 890]
OUTER = [808, 809, 810, 811, 812, 813, 814, 815]


@pytest.fixture(scope="module")
def fmap() -> FloorMap:
    return FloorMap.load(MAP)


@pytest.mark.parametrize("number", CABINS + OUTER)
def test_every_room_on_the_floor_is_routable(fmap, number):
    route = fmap.route(f"room:{number}")
    assert route is not None, number
    assert route.points[0] == (fmap.nodes[fmap.start].x, fmap.nodes[fmap.start].y)
    assert route.length_m > 0
    assert route.steps[0].startswith("From the kiosk, ")
    assert route.steps[-1].startswith(f"Room {number}") and route.steps[-1].endswith(("on your left.", "on your right."))


def test_rooms_not_in_the_sketch_or_on_other_floors_have_no_route(fmap):
    for rid in ("room:886", "room:887", "room:889", "room:607a", "room:014"):
        assert fmap.route(rid) is None, rid
    assert fmap.route("area:staff-room") is None  # drawn, not a destination


def test_ids_are_matched_case_insensitively_and_bare_numbers_work(fmap):
    assert fmap.route("ROOM:874").place_id == "room:874"
    assert fmap.route("874").place_id == "room:874"


ENTRANCES = ("the Faculty Staff Room entrance by room 890", "the Faculty Staff Room entrance between rooms 888 and 885",
             "the Faculty Staff Room entrance between rooms 884 and 883")


def test_cabins_are_reached_through_one_of_the_three_entrances(fmap):
    for number in CABINS:
        route = fmap.route(f"room:{number}")
        steps = " ".join(route.steps)
        assert any(e in steps for e in ENTRANCES), number
        # the only ways in are the three open passages on the south side: no route crosses the staff room's
        # south wall anywhere else, and none uses an east door
        assert "east door" not in steps
        for (x1, y1), (x2, y2) in zip(route.points, route.points[1:]):
            if (y1 - 230) * (y2 - 230) < 0:
                assert round(x1) in (235, 390, 565), (number, x1)


def test_directions_from_the_kiosk(fmap):
    # the kiosk stands in the north corridor, right beside the entrance between 884 and 883
    assert fmap.route("room:874").steps == [
        "From the kiosk, go through the Faculty Staff Room entrance between rooms 884 and 883 and walk about 8 m "
        "to the middle aisle.",
        "Turn left and walk about 4 m.",
        "Room 874 is on your right.",
    ]
    assert fmap.route("room:877").steps[-1] == "Room 877 is on your left."
    west = fmap.route("room:890").steps
    assert west[0] == "From the kiosk, walk about 9 m towards the Faculty Staff Room entrance between rooms 888 and 885."
    assert "entrance by room 890" in west[1]
    # walking on along the middle aisle doesn't "reach the middle aisle" twice
    for number in CABINS:
        assert sum("to the middle aisle" in step for step in fmap.route(f"room:{number}").steps) <= 1, number
    # rooms outside the staff room don't pretend to go through an entrance
    assert "go through" not in " ".join(fmap.route("room:813").steps)


def test_no_step_is_a_pointless_one_metre_shuffle(fmap):
    for p in fmap.places:
        if p.routable:
            for step in fmap.route(p.id).steps:
                assert "about 1 m" not in step or "Keep going" in step or "Turn" in step, (p.id, step)


def test_panoramas_are_collected_along_the_route(fmap):
    data = fmap.model_dump()
    data["nodes"]["n565"]["panorama"] = "staff-room-middle-entrance.jpg"
    for p in data["places"]:
        if p["id"] == "room:874":
            p["panorama"] = "room-874.jpg"
    route = FloorMap.model_validate(data).route("room:874")
    assert route.panoramas == ["staff-room-middle-entrance.jpg", "room-874.jpg"]


def test_bad_maps_are_rejected(fmap):
    data = fmap.model_dump()
    data["edges"].append(("here", "nowhere"))
    with pytest.raises(ValueError, match="unknown node"):
        FloorMap.model_validate(data)
    data = fmap.model_dump()
    first_room = next(i for i, p in enumerate(data["places"]) if p["kind"] == "room")
    data["places"][first_room]["via"] = None
    with pytest.raises(ValueError, match="both door and via"):
        FloorMap.model_validate(data)
