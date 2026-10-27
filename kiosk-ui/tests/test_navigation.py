from __future__ import annotations

import pytest

from kiosk_ui.config import MODULE_ROOT
from kiosk_ui.navigation import FloorMap

MAP = MODULE_ROOT / "data" / "floor_map.yaml"


@pytest.fixture(scope="module")
def fmap() -> FloorMap:
    return FloorMap.load(MAP)


def test_every_room_871_to_890_is_routable(fmap):
    for n in range(871, 891):
        route = fmap.route(f"room:{n}")
        assert route is not None, n
        assert route.points[0] == (fmap.nodes[fmap.start].x, fmap.nodes[fmap.start].y)
        assert route.length_m > 0
        assert route.steps[-1].startswith(f"Room {n} is on your")


def test_ids_are_matched_case_insensitively_and_bare_numbers_work(fmap):
    assert fmap.route("ROOM:874").place_id == "room:874"
    assert fmap.route("874").place_id == "room:874"


def test_rooms_on_other_floors_have_no_route(fmap):
    assert fmap.route("room:607a") is None
    assert fmap.route("room:014") is None
    assert fmap.route("landmark:reception") is None  # drawn, not a destination


def test_directions_follow_the_geometry(fmap):
    # placeholder layout: reception mid-corridor, visitor faces north; 874 is west, on the north side
    west_north = fmap.route("room:874").steps
    assert west_north[0].startswith("From the reception desk, turn left")
    assert west_north[-1] == "Room 874 is on your right."
    # 884 is east, north side
    east_north = fmap.route("room:884").steps
    assert east_north[0].startswith("From the reception desk, turn right")
    assert east_north[-1] == "Room 884 is on your left."


def test_shortest_path_is_straight_along_the_corridor(fmap):
    route = fmap.route("room:871")
    xs = [x for x, _ in route.points[:-1]]
    assert xs == sorted(xs, reverse=True)  # only ever walks west
    assert len(route.steps) == 2  # one corridor leg + "is on your ..."


def test_panoramas_are_collected_along_the_route(fmap):
    data = fmap.model_dump()
    data["nodes"]["c340"]["panorama"] = "corridor-west.jpg"
    for p in data["places"]:
        if p["id"] == "room:874":
            p["panorama"] = "room-874.jpg"
    route = FloorMap.model_validate(data).route("room:874")
    assert route.panoramas == ["corridor-west.jpg", "room-874.jpg"]


def test_bad_maps_are_rejected(fmap):
    data = fmap.model_dump()
    data["edges"].append(("c100", "nowhere"))
    with pytest.raises(ValueError, match="unknown node"):
        FloorMap.model_validate(data)
    data = fmap.model_dump()
    data["places"][1]["via"] = None
    with pytest.raises(ValueError, match="both door and via"):
        FloorMap.model_validate(data)
