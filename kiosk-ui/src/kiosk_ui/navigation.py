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


class Start(BaseModel):
    """Somewhere other than the kiosk a visitor can start from: a lift or a staircase (its QR sticker opens the
    phone walkthrough with ``?from=<id>``)."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    node: str
    name: str  # "the north lift"
    facing: str | None = None  # the way a visitor faces on arriving here (out of the lift door); None = unknown
    lead: str | None = None  # how the first step begins: "With the north lift behind you" (default: "From <name>")
    photo: str | None = None  # what it looks like, shown with the first step
    compass: bool = False  # the facing is unknown: the first step names a compass direction (set by `origin`)
    place_id: str | None = None  # the map place this start is ("facility:lift-north"), set by `origin`


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
    spoken: str | None = None  # how directions name it when the label alone is too short ("the gents washroom")
    near: str | None = None  # the walkable node in front of it: a visitor "at the water dispenser" stands here
    aliases: list[str] = Field(default_factory=list)  # what visitors call it ("lift", "elevator"); the first is its noun

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
        if self.spoken:
            return self.spoken[0].upper() + self.spoken[1:]
        if self.kind == "room":
            return f"Room {self.label}"
        if self.near is not None or self.aliases:
            name = self.origin_name  # "the north lift"
            return name[0].upper() + name[1:]
        return f"The {self.label}"

    @property
    def destination(self) -> str:
        """How the screen names it mid-sentence: 'room 874', 'the gents washroom', 'the north lift'."""
        return self.spoken or self.origin_name


KIOSK = "kiosk"  # the start id of the kiosk itself
# "the nearest ..." questions: which places count, by id prefix
NEAREST = {
    "lift": ("facility:lift-",),
    "stairs": ("facility:stairs-",),
    "way-down": ("facility:lift-", "facility:stairs-"),
    "gents": ("facility:gents-",),
    "ladies": ("facility:ladies-",),
    "water": ("facility:water-",),
}


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
    starts: dict[str, Start] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> FloorMap:
        if self.facing not in _FACING:
            raise ValueError(f"facing must be one of {sorted(_FACING)}, not {self.facing!r}")
        if self.start not in self.nodes:
            raise ValueError(f"start node {self.start!r} is not in nodes")
        for sid, s in self.starts.items():
            if sid == KIOSK:
                raise ValueError(f"start id {KIOSK!r} is the kiosk itself (`start`); pick another id")
            if s.node not in self.nodes:
                raise ValueError(f"start {sid}: node {s.node!r} is not in nodes")
            if s.facing is not None and s.facing not in _FACING:
                raise ValueError(f"start {sid}: facing must be one of {sorted(_FACING)}, not {s.facing!r}")
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

    def origin_place(self, origin_id: str | None) -> Place | None:
        """The place a visitor says they are starting from ("facility:lift-north", "room:871"), if the map can start a
        route there (else None)."""
        if not origin_id:
            return None
        place = self.place(origin_id)
        return place if place is not None and place.start_node is not None else None

    def origin(self, start_id: str | None = None) -> Start | None:
        """Where a route starts, or None when the map can't start there:
        - no id or 'kiosk': the kiosk (the visitor's facing is a guess: the first step names a landmark);
        - a lift or staircase in `starts` ('lift-north', or its place id 'facility:lift-north'): the facing out of its
          door is known, so the first step says "turn left / right";
        - any other place a visitor can stand at ('room:871', 'facility:water-west'): the facing is unknown, so the
          first step names a compass direction."""
        key = (start_id or KIOSK).strip().casefold()
        if key == KIOSK:
            return Start(node=self.start, name=self.start_name)
        sid = key.split(":", 1)[1] if key.startswith("facility:") and key.split(":", 1)[1] in self.starts else key
        if sid in self.starts:
            place = self.place(f"facility:{sid}")
            return self.starts[sid].model_copy(update={"place_id": place.id if place else None})
        if (place := self.origin_place(key)) is not None:
            return Start(node=place.start_node, name=place.origin_name, compass=True, place_id=place.id)
        return None

    def start_key(self, start_id: str | None) -> str:
        """The id a route reports as its start: 'kiosk', 'lift-north' (one of `starts`) or a place id."""
        key = (start_id or KIOSK).strip().casefold()
        if key.startswith("facility:") and key.split(":", 1)[1] in self.starts:
            return key.split(":", 1)[1]
        place = self.place(key) if key != KIOSK and key not in self.starts else None
        return place.id if place is not None else key

    def nearest(self, what: str, start_id: str | None = None) -> Route | None:
        """The shortest route to the nearest lift / staircase / washroom / water ('lift', 'way-down', 'gents', ...,
        see NEAREST), from the kiosk or another start. The place you are standing at doesn't count."""
        prefixes = NEAREST.get(what.strip().casefold())
        origin = self.origin(start_id)
        if prefixes is None or origin is None:
            return None
        routes = [r for p in self.places if p.id.startswith(prefixes) and p.id != origin.place_id
                  and (r := self.route(p.id, start_id)) is not None]
        return min(routes, key=lambda r: r.length_m, default=None)

    def route(self, place_id: str, start_id: str | None = None) -> Route | None:
        """Route to a place from the kiosk, or from another start (see `origin`): None when the place isn't on this
        map or can't be reached, or the start is unknown."""
        place = self.place(place_id)
        origin = self.origin(start_id)
        if origin is None:  # a start the map doesn't know: from the kiosk (the API rejects it before this)
            origin, start_id = self.origin(KIOSK), KIOSK
        if place is None or not place.reachable:
            return None
        names = self._shortest_path(origin.node, place.destination_node)
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
        steps, landmark_of_step, spans, kinds = self._directions(points, landmarks, place, origin)
        photo_of = {self.nodes[n].name: self.nodes[n].photo for n in names if self.nodes[n].name and self.nodes[n].photo}
        step_photos = [photo_of.get(lm) if lm else None for lm in landmark_of_step]
        if origin.photo and len(step_photos) > 1 and step_photos[0] is None:
            step_photos[0] = origin.photo  # "Step out of the north lift ...": the lifts
        step_photos[-1] = place.photo  # the last step is "Room 874 is on your right": show its door
        step_metres = [round(sum(math.dist(points[k], points[k + 1]) for k in range(a, b)) * self.metres_per_unit, 1)
                       for a, b in spans]
        return Route(place_id=place.id, label=place.label, destination=place.destination,
                     start_id=self.start_key(start_id), start_name=origin.name, points=points,
                     length_m=round(length * self.metres_per_unit, 1),
                     steps=steps, step_photos=step_photos, step_spans=spans, step_kinds=kinds,
                     step_metres=step_metres, panoramas=panoramas,
                     origin_id=origin.place_id, origin_name=None if origin.place_id is None else origin.name)

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
        place = self.origin_place(origin_id)
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

    def _directions(self, points: list[Point], landmarks: list[str | None], place: Place, origin: Start,
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
        # walking straight past a landmark a couple of metres from the start (the C816 entrance on the way east)
        # is one step, not "walk 2 m towards it, keep going straight"
        if len(legs) > 2 and legs[0][1] * self.metres_per_unit < 3 and legs[0][0] == legs[1][0]:
            legs = [(legs[0][0], legs[0][1] + legs[1][1], legs[1][2], legs[1][3]), *legs[2:]]
        steps: list[str] = []
        # out of a lift the visitor's facing is known (the door); at the kiosk it is a guess; anywhere else unknown
        known = origin.facing is not None
        heading = _FACING[origin.facing or self.facing]
        opening = origin.lead or f"From {origin.name}"
        here = origin.name
        if not legs and origin.place_id is not None:  # the destination is where the visitor stands
            door_vec = (door[0] - walk[-1][0], door[1] - walk[-1][1])
            gap = math.hypot(*door_vec) * self.metres_per_unit
            if place.id == origin.place_id or here == (place.origin_name if place.kind == "room" else None):
                sentence = f"You are already at {here}."
            else:
                where = f"about {max(1, round(gap))} m {_compass(door_vec)} of {here}" if gap > 0.1 else f"right at {here}"
                sentence = f"{place.spoken_name} is {where}."
            return [sentence], [None], [(len(walk) - 1, len(points) - 1)], ["arrive-straight"]
        through = None  # a landmark right next to the start: "go through it" instead of "walk 1 m to it"
        for i, (direction, length, landmark, end) in enumerate(legs):
            metres = max(1, round(length * self.metres_per_unit))
            if landmark and place.spoken and landmark.casefold() == place.spoken.casefold():
                landmark = None  # "... to the north staircase. The north staircase is on your left." says it twice
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
            if first and origin.compass:
                # nobody knows which way a visitor at the water dispenser faces: a compass direction, not "turn left"
                to = f" to {landmark}" if landmark else ""
                steps.append(f"{opening}, head {_compass(direction)} for about {metres} m{to}.")
                seen.append(landmark)
                kinds.append("start")
                heading = direction
                continue
            if first and through:
                to = f" to {landmark}" if landmark else ""
                steps.append(f"{opening}, go through {through} and walk about {metres} m{to}.")
                seen.append(through)  # the entrance is what to look for
                kinds.append("enter")
                heading = direction
                continue
            if first and landmark and not known:
                # the visitor's facing at the kiosk is a guess, so the first step names where to head instead
                steps.append(f"{opening}, walk about {metres} m towards {landmark}.")
                seen.append(landmark)
                kinds.append("start")
                heading = direction
                continue
            if first:
                lead = {"straight": f"{opening}, walk straight ahead",
                        "around": f"{opening}, turn around and walk"}.get(turn, f"{opening}, turn {turn} and walk")
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
    destination: str = Field(default="", description="How to name it mid-sentence: 'room 874', 'the north lift'.")
    start_id: str = Field(default=KIOSK, description="Where it starts: 'kiosk', a lift or staircase ('lift-north') "
                          "or a place id ('room:871').")
    start_name: str = Field(default="the kiosk", description="How the first step names the start.")
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
    for sid, s in fmap.starts.items():
        print(f"from {s.name} ({sid}):")
        for p in fmap.places:
            if not p.routable or p.id == f"facility:{sid}":
                continue
            r = fmap.route(p.id, sid)
            if r is None:
                problems += 1
                print(f"  {p.id}: NOT REACHABLE from {sid}")
            else:
                print(f"  {p.id}: {r.length_m} m  | {r.steps[0]}")
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    main()
