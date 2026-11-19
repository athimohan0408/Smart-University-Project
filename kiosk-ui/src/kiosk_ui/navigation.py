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
import re
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
    photo: str | None = None  # what the landmark looks like, a file name in data/photos/ (shown with its step)
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
    photo: str | None = None  # the door with its name plate, a file name in data/photos/ ("look for this door")
    panorama: str | None = None  # 360-degree photo of the door / inside, a file name in data/panoramas/
    near: str | None = None  # the walkable node in front of it: a visitor "at the lift" stands here (a start, not a destination)
    aliases: list[str] = Field(default_factory=list)  # what visitors call it; the first is how it is spoken

    @property
    def routable(self) -> bool:
        return self.door is not None and self.via is not None

    @property
    def reachable(self) -> bool:
        """Can a route be drawn *to* it: a door (rooms, the lounge) or a walkway node in front of it (lifts, stairs ...)."""
        return self.routable or self.near is not None

    @property
    def destination_node(self) -> str | None:
        return self.via or self.near

    @property
    def start_node(self) -> str | None:
        """The node a route starts from when the visitor says they are at this place."""
        return self.near or self.via

    @property
    def origin_name(self) -> str:
        """How the first step names it: 'room 871', 'the north lift'."""
        if self.kind == "room":
            return f"room {self.id.split(':', 1)[1]}"
        direction = next((d for d in _FACING if self.id.endswith(f"-{d}")), None)
        noun = self.aliases[0] if self.aliases else self.label
        return f"the {direction} {noun}" if direction else f"the {noun}"

    @property
    def spoken_name(self) -> str:
        if self.kind == "room":
            return f"Room {self.label}"
        if self.near is not None or self.aliases:
            name = self.origin_name  # "the north lift"
            return name[0].upper() + name[1:]
        return f"The {self.label}"

    @property
    def destination(self) -> str:
        """How the screen names it: 'room 874', 'the north lift'."""
        return self.origin_name


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
            if p.near is not None and p.near not in self.nodes:
                raise ValueError(f"{p.id}: near node {p.near!r} is not in nodes")
        return self

    @classmethod
    def load(cls, path: Path) -> FloorMap:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def place(self, place_id: str) -> Place | None:
        key = normalise_place_id(place_id)
        return next((p for p in self.places if p.id.casefold() == key), None)

    def origin(self, origin_id: str | None) -> Place | None:
        """The place a visitor says they are starting from, if the map can start a route there (else None)."""
        if not origin_id:
            return None
        place = self.place(origin_id)
        return place if place is not None and place.start_node is not None else None

    def route(self, place_id: str, origin_id: str | None = None) -> Route | None:
        """Route to a place, or None when the place isn't on this map (or not routable). It starts at the kiosk,
        or at ``origin_id`` (a place id such as ``facility:lift-north`` or ``room:871``) when the map can start there."""
        place = self.place(place_id)
        if place is None or not place.reachable:
            return None
        start_place = self.origin(origin_id)
        start_node = start_place.start_node if start_place else self.start
        names = self._shortest_path(start_node, place.destination_node)
        if names is None:
            return None
        end = self.nodes[names[-1]]
        door = place.door or _nearest_point(place.rect, (end.x, end.y))  # a lift has no door: its nearest edge
        points = [(self.nodes[n].x, self.nodes[n].y) for n in names] + [door]
        landmarks = [self.nodes[n].name for n in names]
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        panoramas = [p for n in names if (p := self.nodes[n].panorama)]
        if place.panorama:
            panoramas.append(place.panorama)
        steps, landmark_of_step, spans, kinds = self._directions(
            points, landmarks, place, here=start_place.origin_name if start_place else self.start_name,
            facing_known=start_place is None)
        photo_of = {self.nodes[n].name: self.nodes[n].photo for n in names if self.nodes[n].name and self.nodes[n].photo}
        step_photos = [photo_of.get(lm) if lm else None for lm in landmark_of_step]
        step_photos[-1] = place.photo  # the last step is "Room 874 is on your right": show its door
        step_metres = [round(sum(math.dist(points[k], points[k + 1]) for k in range(a, b)) * self.metres_per_unit, 1)
                       for a, b in spans]
        return Route(place_id=place.id, label=place.label, destination=place.destination, points=points,
                     length_m=round(length * self.metres_per_unit, 1),
                     steps=steps, step_photos=step_photos, step_spans=spans, step_kinds=kinds,
                     step_metres=step_metres, panoramas=panoramas,
                     origin_id=start_place.id if start_place else None,
                     origin_name=start_place.origin_name if start_place else None)

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

    def find_by_name(self, text: str) -> list[Place]:
        """Places the words name ("the lift", "gents washroom", "MCA lab", "north staircase"): every place whose
        longest matching alias is the longest one in the text, narrowed by a direction word when one is said."""
        low = text.casefold()
        best = 0
        found: list[Place] = []
        for place in self.places:
            if place.kind in ("area", "hall"):
                continue
            hits = [a for a in place.aliases if re.search(rf"(?<![\w]){re.escape(a.casefold())}s?(?![\w])", low)]
            if not hits:
                continue
            size = max(len(a) for a in hits)
            if size > best:
                best, found = size, []
            if size == best:
                found.append(place)
        for direction in _FACING:
            if re.search(rf"\b{direction}(?:ern)?\b", low):
                narrowed = [p for p in found if p.id.endswith(f"-{direction}")]
                if narrowed:
                    return narrowed
        return found

    def position(self, origin_id: str | None = None, *, nearby: int = 3) -> Position:
        """Where the visitor is (the kiosk, or the place they said they are at) and what is close, in words."""
        place = self.origin(origin_id)
        node = self.nodes[place.start_node if place else self.start]
        name = place.origin_name if place else self.start_name
        here = (node.x, node.y)
        found: list[tuple[float, str, Point]] = []
        for other in self.places:
            if other is place or other.kind in ("area", "hall"):
                continue
            x, y, w, h = other.rect
            dx = 0.0 if x <= here[0] <= x + w else min(abs(here[0] - x), abs(here[0] - (x + w)))
            dy = 0.0 if y <= here[1] <= y + h else min(abs(here[1] - y), abs(here[1] - (y + h)))
            vec = (x + w / 2 - here[0], y + h / 2 - here[1])
            found.append((math.hypot(dx, dy), other.origin_name, vec))
        found.sort(key=lambda f: f[0])
        facilities = [f for f in found if not f[1].startswith("room ")][: max(1, nearby - 1)]
        rooms = [f for f in found if f[1].startswith("room ")][:1]
        close = []
        for dist, label, vec in sorted([*facilities, *rooms])[:nearby]:
            metres = round(dist * self.metres_per_unit)
            close.append(f"{label}, {f'about {metres} m {_compass(vec)}' if metres >= 1 else 'right beside you'}")
        text = f"You are at {name}, in {self.floor}."
        if close:
            text += " Close by: " + "; ".join(close) + "."
        return Position(origin_id=place.id if place else None, name=name, point=here, text=text)

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

    def _directions(self, points: list[Point], landmarks: list[str | None], place: Place, *, here: str | None = None,
                    facing_known: bool = True,
                    ) -> tuple[list[str], list[str | None], list[tuple[int, int]], list[str]]:
        """The steps and, for each step: the landmark it names (to show that landmark's photo with it), the part of
        `points` it walks (start and end index), and its kind for the turn arrow (start, enter, left, right,
        straight, around, arrive-left, arrive-right, arrive-straight)."""
        *walk, door = points
        seen: list[str | None] = []  # parallel to steps
        spans: list[tuple[int, int]] = []
        kinds: list[str] = []
        span_start = 0
        # once you are in a place ("the middle aisle"), walking on along it doesn't reach it again
        named: list[str | None] = []
        for name in landmarks:
            named.append(None if name is not None and name == next((n for n in reversed(named) if n), None) else name)
        legs = _straight_legs(walk, named)
        steps: list[str] = []
        heading = _FACING[self.facing]
        here = here or self.start_name
        if not legs and not facing_known:  # the destination is where the visitor stands: no facing to turn from
            door_vec = (door[0] - walk[-1][0], door[1] - walk[-1][1])
            gap = math.hypot(*door_vec) * self.metres_per_unit
            if here == (place.origin_name if place.kind == "room" else None):
                sentence = f"You are already at {here}."
            else:
                where = f"about {max(1, round(gap))} m {_compass(door_vec)} of {here}" if gap > 0.1 else f"right at {here}"
                sentence = f"{place.spoken_name} is {where}."
            return [sentence], [None], [(len(walk) - 1, len(points) - 1)], ["arrive-straight"]
        through = None  # a landmark right next to the start: "go through it" instead of "walk 1 m to it"
        for i, (direction, length, landmark, end) in enumerate(legs):
            metres = max(1, round(length * self.metres_per_unit))
            if not steps and length * self.metres_per_unit < 1.5 and i < len(legs) - 1:
                # a step's worth from the start: only worth naming if the route turns into it
                # (the next step's span starts at the kiosk, so the walkthrough still covers it)
                if _turn(direction, legs[i + 1][0]) != "straight":
                    through = landmark or through
                continue
            turn = _turn(heading, direction)
            first = not steps
            spans.append((span_start, end))
            span_start = end
            if first and not facing_known:
                # nobody knows which way a visitor at the lift faces: name a compass direction, not "turn left"
                to = f" to {landmark}" if landmark else ""
                steps.append(f"From {here}, head {_compass(direction)} for about {metres} m{to}.")
                seen.append(landmark)
                kinds.append("start")
                heading = direction
                continue
            if first and through:
                to = f" to {landmark}" if landmark else ""
                steps.append(f"From {here}, go through {through} and walk about {metres} m{to}.")
                seen.append(through)  # the entrance is what to look for
                kinds.append("enter")
                heading = direction
                continue
            if first and landmark:
                # the visitor's facing at the kiosk is a guess, so the first step names where to head instead
                steps.append(f"From {here}, walk about {metres} m towards {landmark}.")
                seen.append(landmark)
                kinds.append("start")
                heading = direction
                continue
            if first:
                lead = {"straight": f"From {here}, walk straight ahead",
                        "around": f"From {here}, turn around and walk"}.get(turn, f"From {here}, turn {turn} and walk")
            else:
                lead = "Keep going straight for" if turn == "straight" else f"Turn {turn} and walk"
            to = f" to {landmark}" if landmark else ""
            steps.append(f"{lead} about {metres} m{to}.")
            seen.append(landmark)
            kinds.append(turn)
            heading = direction
        spur = (door[0] - walk[-1][0], door[1] - walk[-1][1])
        side = _turn(heading, _unit(spur)) if math.hypot(*spur) > _EPS else "straight"
        where = {"left": "on your left", "right": "on your right", "straight": "straight ahead",
                 "around": "behind you"}[side]
        steps.append(f"{place.spoken_name} is {where}.")
        seen.append(None)
        spans.append((len(walk) - 1, len(points) - 1))
        kinds.append(f"arrive-{'straight' if side == 'around' else side}")
        return steps, seen, spans, kinds


class Route(BaseModel):
    place_id: str
    label: str
    destination: str = Field(default="", description="How to name it: 'room 874', 'the north lift'.")
    points: list[tuple[float, float]] = Field(description="Polyline in map units, start first, door last.")
    length_m: float
    steps: list[str]
    step_photos: list[str | None] = Field(default_factory=list,
                                          description="A photo (file in data/photos/) for each step, or None.")
    step_spans: list[tuple[int, int]] = Field(default_factory=list,
                                              description="For each step, the start and end index in points it walks.")
    step_kinds: list[str] = Field(default_factory=list, description="For each step, its arrow: start, enter, left, "
                                  "right, straight, around, arrive-left, arrive-right, arrive-straight.")
    step_metres: list[float] = Field(default_factory=list, description="For each step, how far it walks.")
    panoramas: list[str] = Field(default_factory=list,
                                 description="360-degree photos along the route, start first, destination last.")
    origin_id: str | None = Field(default=None, description="The place the route starts from, when not the kiosk.")
    origin_name: str | None = Field(default=None, description="'the north lift': how the first step names the start.")


class Position(BaseModel):
    """Where a visitor is, for "Where am I?"."""

    origin_id: str | None = Field(description="Place id they said they are at, or None for the kiosk.")
    name: str
    point: tuple[float, float]
    text: str


def _nearest_point(rect: tuple[float, float, float, float], p: Point) -> Point:
    """The point of a rectangle closest to p."""
    x, y, w, h = rect
    return (min(max(p[0], x), x + w), min(max(p[1], y), y + h))


def _compass(v: Point) -> str:
    """north / south / east / west for a map vector (y grows downwards, so north is -y)."""
    return ("east" if v[0] > 0 else "west") if abs(v[0]) >= abs(v[1]) else ("south" if v[1] > 0 else "north")


def normalise_place_id(place_id: str) -> str:
    """'room:874', 'ROOM:874', '874' -> 'room:874' (lower case, for matching)."""
    key = place_id.strip().casefold()
    return key if ":" in key else f"room:{key}"


def _unit(v: Point) -> Point:
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n)


def _straight_legs(points: list[Point], landmarks: list[str | None] | None = None,
                   ) -> list[tuple[Point, float, str | None, int]]:
    """Merge consecutive collinear segments:
    [(unit heading, length, landmark at the leg's end or None, index of the leg's end in points), ...]."""
    landmarks = landmarks or [None] * len(points)
    legs: list[tuple[Point, float, str | None, int]] = []
    for j, ((a, b), name) in enumerate(zip(zip(points, points[1:]), landmarks[1:])):
        length = math.dist(a, b)
        if length < _EPS:
            continue
        h = _unit((b[0] - a[0], b[1] - a[1]))
        # a leg ends at a named landmark: walking on past it is a new step ("keep going straight ...")
        if legs and legs[-1][2] is None and abs(_cross(legs[-1][0], h)) < 1e-3 and _dot(legs[-1][0], h) > 0:
            legs[-1] = (legs[-1][0], legs[-1][1] + length, name, j + 1)
        else:
            legs.append((h, length, name, j + 1))
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
