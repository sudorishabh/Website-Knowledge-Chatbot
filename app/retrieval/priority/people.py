"""Who is on the people pages, and whether a question names one of them.

The three listings (Committee of Directors, Governing Council, Distinguished
Fellows) are read first; a question naming someone on them is answered from
that person's own profile — ``/profile/<slug>`` for staff and fellows,
``/governing-council/<slug>`` for council members, whichever the listing links.
A question naming nobody on them is left to the ingested corpus.

Names are matched conservatively, because a false match puts a stranger's
biography at the head of the answer:

* every significant part of the name must appear as a whole word — honorifics
  are ignored, and single initials ("R R Rashmi") are optional;
* a one-word name ("Mr Anshuman") must be at least five letters long;
* a surname alone matches only after an honorific ("Dr Dhawan") and only when
  no one else on the listings shares it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.retrieval.priority.extract import PageContent
from app.retrieval.priority.registry import normalize_text

_HONORIFICS = ("dr", "mr", "ms", "mrs", "prof", "professor", "shri", "smt", "sir")
#: Profile paths the people pages link to.
_PROFILE_PATH = re.compile(r"^/(?:profile|governing-council)/[^/]+/?$", re.I)
_MIN_SINGLE_NAME = 5


@dataclass(frozen=True)
class Person:
    name: str
    profile_url: str
    #: The listing the person was found on.
    listing: str
    #: The line under the name on the listing ("Director General").
    title: str | None = None

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(
            t for t in normalize_text(self.name).split()
            if t not in _HONORIFICS and len(t) > 1
        )


def people_on(content: PageContent, listing: str) -> list[Person]:
    """Every person a listing links to a profile for, once each, in page order."""
    lines = [line for section in content.sections for line in section.text.splitlines()]
    seen: dict[str, Person] = {}
    for link in content.links:
        if not _PROFILE_PATH.match(urlsplit(link.href).path or ""):
            continue
        if link.href in seen or not normalize_text(link.text):
            continue
        title = None
        if link.text in lines:
            at = lines.index(link.text)
            if at + 1 < len(lines):
                title = lines[at + 1]
        seen[link.href] = Person(link.text, link.href, listing, title)
    return list(seen.values())


def _has(words: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", words) is not None


def named_in(question: str, people: list[Person]) -> list[Person]:
    """The people ``question`` names, best evidence first: a full-name match
    outranks a surname-after-honorific match."""
    words = normalize_text(question)
    # Counted by name, not by profile: the Director General is on the committee
    # *and* the council, with a profile under each, and is one person.
    surnames: dict[str, set[tuple[str, ...]]] = {}
    for person in people:
        if person.parts:
            surnames.setdefault(person.parts[-1], set()).add(person.parts)

    full: list[Person] = []
    partial: list[Person] = []
    for person in people:
        parts = person.parts
        if not parts:
            continue
        if len(parts) == 1:
            if len(parts[0]) >= _MIN_SINGLE_NAME and _has(words, parts[0]):
                full.append(person)
            continue
        if all(_has(words, part) for part in parts):
            full.append(person)
            continue
        surname = parts[-1]
        if len(surnames.get(surname, ())) == 1 and len(surname) >= _MIN_SINGLE_NAME and any(
            _has(words, f"{h} {surname}") for h in _HONORIFICS
        ):
            partial.append(person)
    # One person can be on two listings; the first listing's profile is kept,
    # and the listings are read committee first, whose profiles are the fuller.
    ordered: dict[tuple[str, ...], Person] = {}
    for person in full + partial:
        ordered.setdefault(person.parts, person)
    return list(ordered.values())
