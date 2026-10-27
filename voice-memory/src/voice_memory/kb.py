"""Read-only view of the knowledge base that rag-core builds, for names, genders and display names.

The KB itself (faculty sheet + campus sheet -> facts -> index) is built and served by rag-core
(``just index`` / ``POST /reload-kb``). Memory only needs to know who people are: their display
name ("Dr Deepa V Jose"), the words visitors use for them, and the sheet's Gender column, so
"her office" can be resolved to the right person. Nothing here is a source of answers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
# Words that are also name parts in the sheet but are ordinary English: never treat them alone as a name.
_COMMON = frozenset({"joy", "john", "rose", "grace", "hope", "may", "jose", "das", "k", "r", "j", "p", "s", "v", "e", "d", "b", "t"})


def norm_words(text: str) -> list[str]:
    return _WORD.findall(text.casefold().replace("'s", ""))


@dataclass(frozen=True)
class Person:
    canonical_id: str  # faculty:<id>
    name: str  # display name, e.g. "Dr Deepa V Jose"
    gender: str | None  # "Female" / "Male" / None, as in the sheet
    name_words: tuple[str, ...]  # lower-case words of the name without the title


@dataclass
class KBView:
    people: dict[str, Person] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # any canonical id -> what to call it
    _name_index: dict[str, set[str]] = field(default_factory=dict)  # name word -> faculty ids

    @classmethod
    def load(cls, faculty_json: Path, gazetteer: Path | None = None) -> KBView:
        view = cls()
        if faculty_json.exists():
            data = json.loads(faculty_json.read_text(encoding="utf-8"))
            for fac in data.get("faculty", []):
                title = fac.get("title")
                name = f"{title} {fac['full_name']}" if title else fac["full_name"]
                view.add_person(f"faculty:{fac['id']}", name, fac.get("gender"), fac.get("aliases", []))
        if gazetteer is not None and gazetteer.exists():
            for entry in json.loads(gazetteer.read_text(encoding="utf-8")).get("entries", []):
                view.names.setdefault(entry["canonical_id"], entry["name"])
        return view

    def add_person(self, canonical_id: str, name: str, gender: str | None, aliases: list[str] = ()) -> None:
        words = tuple(w for w in norm_words(name) if w not in {"dr", "fr", "prof"})
        self.people[canonical_id] = Person(canonical_id, name, gender, words)
        self.names[canonical_id] = name
        for w in {*words, *(x for a in aliases for x in norm_words(a))}:
            if len(w) > 2 and w not in _COMMON:
                self._name_index.setdefault(w, set()).add(canonical_id)

    def name_of(self, canonical_id: str) -> str:
        """What to call an entity in a rewritten question: 'Dr Deepa V Jose', 'room 874', 'ML'."""
        kind, _, rest = canonical_id.partition(":")
        if canonical_id in self.names:
            name = self.names[canonical_id]
            # a kind-level id ("facility:washroom") names a kind, not one place: "the washroom"
            return f"the {name}" if kind in ("facility", "event") and ":" not in rest else name
        if kind == "room":
            return f"room {rest.upper()}"
        if kind in ("course", "programme"):
            return rest.upper().replace("-", " ")
        return rest.split(":")[-1].replace("-", " ")

    def gender_of(self, canonical_id: str) -> str | None:
        person = self.people.get(canonical_id)
        return person.gender.casefold() if person and person.gender else None

    def mentions_person(self, text: str) -> bool:
        """Does the text name someone ("Dr Sridevi", "deepa ma'am")? Then its pronouns are about them."""
        words = norm_words(text)
        if any(w in self._name_index for w in words):
            return True
        return bool(re.search(r"\b(dr|doctor|prof|professor|fr|father)\.?\s+[a-z]", text, re.IGNORECASE))

    def people_in(self, text: str) -> list[Person]:
        """Faculty whose full display name appears in the text (for parsing clarification questions)."""
        found = [(text.find(p.name), p) for p in self.people.values() if p.name in text]
        return [p for _, p in sorted(found, key=lambda pair: pair[0])]
