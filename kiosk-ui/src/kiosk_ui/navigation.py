"""Floor map + route finder: ``room:874`` -> highlighted path from reception and spoken-style directions.

The map (``data/floor_map.yaml``) is a small graph: corridor nodes joined by edges, and places (rooms,
landmarks) whose door joins one node. A route is the shortest path (Dijkstra) from the start node to
the place's node, then out to its door. Directions are worked out from the path's geometry, so they
stay right when the coordinates are corrected to the real floor plan.

Run ``python -m kiosk_ui.navigation`` to check the map (every room reachable) and print each route.
"""

from __future__ import annotations

import heapq
import math
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_FACING = {"north": (0.0, -1.0), "south": (0.0, 1.0), "east": (1.0, 0.0), "west": (-1.0, 0.0)}
_EPS = 1e-6

Point = tuple[float, float]


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    x: float
    y: float
    name: str | None = None  # a landmark the directions can name ("the Faculty Staff Room entrance")
    panorama: str | None = None  # 360-degree photo taken here, a file name in data/panoramas/


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    label: str
    kind: str
    rect: tuple[float, float, float, float]
    door: tuple[float, float] | None = None
    via: str | None = None
    label_at: tuple[float, float] | None = None  # where to draw the label (default: the middle of rect)
    panorama: str | None = None  # 360-degree photo of the door / inside, a file name in data/panoramas/

    @property
    def routable(self) -> bool:
        return self.door is not None and self.via is not None

    @property
    def spoken_name(self) -> str:
        return f"Room {self.label}" if self.kind == "room" else f"The {self.label}"


class FloorMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verified: bool
    verified_by: str | None = None
    source: str | None = None  # what the layout was drawn from
    floor: str
    metres_per_unit: float = Field(gt=0)
    facing: str
    viewbox: tuple[float, float, float, float]
    start: str
    start_name: str = "the kiosk"  # how the first step names where the visitor stands
    corridor: list[tuple[float, float, float, float]] = Field(default_factory=list)
    nodes: dict[str, Node]
    edges: list[tuple[str, str]]
    places: list[Place]

    @model_validator(mode="after")
    def _check(self) -> FloorMap:
        if self.facing not in _FACING:
            raise ValueError(f"facing must be one of {sorted(_FACING)}, not {self.facing!r}")
        if self.start not in self.nodes:
            raise ValueError(f"start node {self.start!r} is not in nodes")
        for a, b in self.edges:
            for n in (a, b):
                if n not in self.nodes:
                    raise ValueError(f"edge {a}-{b}: unknown node {n!r}")
        seen: set[str] = set()
        for p in self.places:
            if p.id in seen:
                raise ValueError(f"duplicate place id {p.id!r}")
            seen.add(p.id)
            if (p.door is None) != (p.via is None):
                raise ValueError(f"{p.id}: give both door and via, or neither")
            if p.via is not None and p.via not in self.nodes:
                raise ValueError(f"{p.id}: via node {p.via!r} is not in nodes")
        return self

    @classmethod
    def load(cls, path: Path) -> FloorMap:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def place(self, place_id: str) -> Place | None:
        key = normalise_place_id(place_id)
        return next((p for p in self.places if p.id.casefold() == key), None)

    def route(self, place_id: str) -> Route | None:
        """Route from the start to a place, or None when the place isn't on this map (or not routable)."""
        place = self.place(place_id)
        if place is None or not place.routable:
            return None
        names = self._shortest_path(self.start, place.via)
        if names is None:
            return None
        points = [(self.nodes[n].x, self.nodes[n].y) for n in names] + [place.door]
        landmarks = [self.nodes[n].name for n in names]
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        panoramas = [p for n in names if (p := self.nodes[n].panorama)]
        if place.panorama:
            panoramas.append(place.panorama)
        return Route(place_id=place.id, label=place.label, points=points,
                     length_m=round(length * self.metres_per_unit, 1),
                     steps=self._directions(points, landmarks, place), panoramas=panoramas)

    def whereabouts(self, place_id: str) -> str | None:
        """Where a place is on the floor, in words: 'inside the Faculty Staff Room · C816', 'on the east side'."""
        place = self.place(place_id)
        if place is None:
            return None
        x, y, w, h = place.rect
        cx, cy = x + w / 2, y + h / 2
        for area in self.places:
            ax, ay, aw, ah = area.rect
            if area.kind == "area" and area is not place and ax <= cx <= ax + aw and ay <= cy <= ay + ah:
                return f"inside the {area.label}"
        vx, vy, vw, vh = self.viewbox
        ew = "west" if cx < vx + vw / 3 else "east" if cx > vx + 2 * vw / 3 else None
        ns = "north" if cy < vy + vh / 3 else "south" if cy > vy + 2 * vh / 3 else None
        side = "-".join(part for part in (ns, ew) if part)  # "north-east", "south", "west"
        return f"on the {side} side of the floor" if side else "in the middle of the floor"

    def _shortest_path(self, src: str, dst: str) -> list[str] | None:
        adj: dict[str, list[tuple[str, float]]] = {n: [] for n in self.nodes}
        for a, b in self.edges:
            d = math.dist((self.nodes[a].x, self.nodes[a].y), (self.nodes[b].x, self.nodes[b].y))
            adj[a].append((b, d))
            adj[b].append((a, d))
        dist = {src: 0.0}
        prev: dict[str, str] = {}
        queue = [(0.0, src)]
        while queue:
            d, n = heapq.heappop(queue)
            if n == dst:
                path = [n]
                while n in prev:
                    n = prev[n]
                    path.append(n)
                return path[::-1]
            if d > dist.get(n, math.inf):
                continue
            for m, w in adj[n]:
                if d + w < dist.get(m, math.inf):
                    dist[m] = d + w
                    prev[m] = n
                    heapq.heappush(queue, (d + w, m))
        return None

    def _directions(self, points: list[Point], landmarks: list[str | None], place: Place) -> list[str]:
        *walk, door = points
        # once you are in a place ("the middle aisle"), walking on along it doesn't reach it again
        named: list[str | None] = []
        for name in landmarks:
            named.append(None if name is not None and name == next((n for n in reversed(named) if n), None) else name)
        legs = _straight_legs(walk, named)
        steps: list[str] = []
        heading = _FACING[self.facing]
        here = self.start_name
        through = None  # a landmark right next to the start: "go through it" instead of "walk 1 m to it"
        for i, (direction, length, landmark) in enumerate(legs):
            metres = max(1, round(length * self.metres_per_unit))
            if not steps and length * self.metres_per_unit < 1.5 and i < len(legs) - 1:
                # a step's worth from the start: only worth naming if the route turns into it
                if _turn(direction, legs[i + 1][0]) != "straight":
                    through = landmark or through
                continue
            turn = _turn(heading, direction)
            first = not steps
            if first and through:
                to = f" to {landmark}" if landmark else ""
                steps.append(f"From {here}, go through {through} and walk about {metres} m{to}.")
                heading = direction
                continue
            if first and landmark:
                # the visitor's facing at the kiosk is a guess, so the first step names where to head instead
                steps.append(f"From {here}, walk about {metres} m towards {landmark}.")
                heading = direction
                continue
            if first:
                lead = {"straight": f"From {here}, walk straight ahead",
                        "around": f"From {here}, turn around and walk"}.get(turn, f"From {here}, turn {turn} and walk")
            else:
                lead = "Keep going straight for" if turn == "straight" else f"Turn {turn} and walk"
            to = f" to {landmark}" if landmark else ""
            steps.append(f"{lead} about {metres} m{to}.")
            heading = direction
        spur = (door[0] - walk[-1][0], door[1] - walk[-1][1])
        side = _turn(heading, _unit(spur)) if math.hypot(*spur) > _EPS else "straight"
        where = {"left": "on your left", "right": "on your right", "straight": "straight ahead",
                 "around": "behind you"}[side]
        steps.append(f"{place.spoken_name} is {where}.")
        return steps


class Route(BaseModel):
    place_id: str
    label: str
    points: list[tuple[float, float]] = Field(description="Polyline in map units, start first, door last.")
    length_m: float
    steps: list[str]
    panoramas: list[str] = Field(default_factory=list,
                                 description="360-degree photos along the route, start first, destination last.")


def normalise_place_id(place_id: str) -> str:
    """'room:874', 'ROOM:874', '874' -> 'room:874' (lower case, for matching)."""
    key = place_id.strip().casefold()
    return key if ":" in key else f"room:{key}"


def _unit(v: Point) -> Point:
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n)


def _straight_legs(points: list[Point], landmarks: list[str | None] | None = None) -> list[tuple[Point, float, str | None]]:
    """Merge consecutive collinear segments: [(unit heading, length, landmark at the leg's end or None), ...]."""
    landmarks = landmarks or [None] * len(points)
    legs: list[tuple[Point, float, str | None]] = []
    for (a, b), name in zip(zip(points, points[1:]), landmarks[1:]):
        length = math.dist(a, b)
        if length < _EPS:
            continue
        h = _unit((b[0] - a[0], b[1] - a[1]))
        # a leg ends at a named landmark: walking on past it is a new step ("keep going straight ...")
        if legs and legs[-1][2] is None and abs(_cross(legs[-1][0], h)) < 1e-3 and _dot(legs[-1][0], h) > 0:
            legs[-1] = (legs[-1][0], legs[-1][1] + length, name)
        else:
            legs.append((h, length, name))
    return legs


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _turn(heading: Point, direction: Point) -> str:
    """Relative turn on a y-down map: a positive cross product is clockwise, i.e. to the right."""
    c, d = _cross(heading, direction), _dot(heading, direction)
    if abs(c) < 1e-3:
        return "straight" if d > 0 else "around"
    return "right" if c > 0 else "left"


def main() -> None:
    from kiosk_ui.config import get_settings

    fmap = FloorMap.load(get_settings().map.file)
    print(f"{fmap.floor}: {len(fmap.places)} places, verified={fmap.verified}")
    problems = 0
    for p in fmap.places:
        if not p.routable:
            continue
        r = fmap.route(p.id)
        if r is None:
            problems += 1
            print(f"  {p.id}: NOT REACHABLE from {fmap.start}")
        else:
            print(f"  {p.id}: {r.length_m} m  | " + " ".join(r.steps))
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
