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

A part may be misspelt, since users write names as they hear them ("PK
Bhatacharia" for Dr P K Bhattacharya). A word is read as a name part when it
starts with the same letter and is spelt the way the part sounds — "h" after a
consonant, doubled letters, "y"/"i" and the like set aside. When the question
gives more than one word of the name, or the person's initials ("PK"), each
part may also be a letter out (two, for a long name); a lone word must sound
the same, so "Mr Malik" is not read as Mr Mullick. A misspelling never
outranks a name spelt as listed: a misspelt reading is dropped when an exactly
spelt name, or a reading explaining more of the question, covers the same
words. "Dr Bhattacharjya" is therefore Mr Souvik Bhattacharjya, not a misspelt
Dr Bhattacharya, while "Souvik Bhattacharya" still names him.
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
#: How long a name must sound before one letter, or two, may be out.
_ONE_LETTER_OUT = 5
_TWO_LETTERS_OUT = 9
#: The cost of a word spelt the way the name sounds (see `_cost`): as far as
#: one word of a name, on its own, may be from the listed spelling.
_SAME_SOUND = 1

#: Spellings of one sound, folded before names are compared ("Suneel"/"Sunil",
#: "Dhawan"/"Dhavan", "Laxmi"/"Lakshmi").
_SOUNDS = (("ee", "i"), ("oo", "u"), ("ck", "k"), ("x", "ks"), ("q", "k"),
           ("z", "j"), ("w", "v"), ("y", "i"))
#: An "h" after a consonant ("Bhattacharya", "Bhatacharya").
_ASPIRATE = re.compile(r"(?<=[b-df-hj-np-tv-z])h")
#: A doubled letter ("Bhattacharya", "Bhatacharya").
_DOUBLED = re.compile(r"(.)\1+")


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

    @property
    def initials(self) -> str:
        """The name's single initials run together ("pk"), or "" with fewer
        than two of them — one letter is too common to say anything."""
        letters = "".join(t for t in normalize_text(self.name).split() if len(t) == 1)
        return letters if len(letters) > 1 else ""


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


def _sound(word: str) -> str:
    """``word`` as it sounds, near enough: one spelling per sound, no "h" after
    a consonant, no doubled letters."""
    for spelling, sound in _SOUNDS:
        word = word.replace(spelling, sound)
    return _DOUBLED.sub(r"\1", _ASPIRATE.sub("", word))


def _edits(a: str, b: str) -> int:
    """The letters added, dropped or changed to turn ``a`` into ``b``."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def _cost(part: str, word: str) -> int | None:
    """How far ``word`` is from the name part ``part``: 0 spelt as listed, 1
    spelt as it sounds, 2 or 3 a letter or two out, None another word."""
    if word == part:
        return 0
    a, b = _sound(part), _sound(word)
    if not a or not b or a[0] != b[0]:
        return None
    if a == b:
        return 1
    shorter = min(len(a), len(b))
    allowed = 2 if shorter >= _TWO_LETTERS_OUT else 1 if shorter >= _ONE_LETTER_OUT else 0
    if not allowed:
        return None
    edits = _edits(a, b)
    return 1 + edits if edits <= allowed else None


@dataclass(frozen=True)
class _Reading:
    """One way of reading some of the question's words as a listed name.

    ``person`` is None for a word spelt exactly as someone's name part, which
    names nobody on its own but holds that word against a misspelling."""

    person: Person | None
    words: frozenset[str]
    cost: int
    partial: bool = False

    @property
    def rank(self) -> tuple[bool, bool, int]:
        """Spelt as listed first, then a full name before a surname alone."""
        return (self.cost > 0, self.partial, self.cost)

    def outread_by(self, other: _Reading) -> bool:
        """Whether ``other`` reads the same words better: it explains more of
        the question, or the same words with fewer letters out."""
        return other is not self and self.words <= other.words and (
            len(other.words) > len(self.words) or other.cost < self.cost
        )


def _closest(part: str, words: set[str]) -> tuple[int, str] | None:
    costs = [(cost, word) for word in words if (cost := _cost(part, word)) is not None]
    return min(costs) if costs else None


def _initials_in(tokens: list[str], initials: str) -> bool:
    """Whether the question gives ``initials``, run together ("PK") or apart
    ("P K", "P.K.")."""
    if not initials:
        return False
    letters = list(initials)
    return initials in tokens or any(
        tokens[i:i + len(letters)] == letters for i in range(len(tokens))
    )


def _reading(
    person: Person, tokens: list[str], words: set[str], people: list[Person]
) -> _Reading | None:
    """How the question names ``person``, if it does: every part of the name,
    or the surname after an honorific when no one else's is as close."""
    parts = person.parts
    if not parts or (len(parts) == 1 and len(parts[0]) < _MIN_SINGLE_NAME):
        return None
    initialled = _initials_in(tokens, person.initials)
    closest = [_closest(part, words) for part in parts]
    if all(closest):
        cost = sum(c for c, _ in closest)
        if len(parts) == 1 and not initialled and cost > _SAME_SOUND:
            return None
        named = {w for _, w in closest} | ({person.initials} if initialled else set())
        return _Reading(person, frozenset(named), cost)
    if len(parts) == 1 or len(parts[-1]) < _MIN_SINGLE_NAME:
        return None
    surname = parts[-1]
    for honorific, word in zip(tokens, tokens[1:]):
        cost = _cost(surname, word) if honorific in _HONORIFICS else None
        if cost is None or cost > _SAME_SOUND:
            continue
        # Counted by name, not by profile: the Director General is on the
        # committee *and* the council, with a profile under each, and is one
        # person.
        rivals = {p.parts for p in people
                  if p.parts and (c := _cost(p.parts[-1], word)) is not None and c <= cost}
        if rivals == {parts}:
            return _Reading(person, frozenset({word}), cost, partial=True)
    return None


def named_in(question: str, people: list[Person]) -> list[Person]:
    """The people ``question`` names, best evidence first: a name spelt as
    listed outranks a misspelt one, and a full-name match a surname after an
    honorific."""
    tokens = normalize_text(question).split()
    words = {t for t in tokens if t not in _HONORIFICS}
    readings = [r for person in people if (r := _reading(person, tokens, words, people))]
    listed = {part for person in people for part in person.parts}
    readings += [_Reading(None, frozenset({word}), 0) for word in words & listed]
    kept = sorted((r for r in readings
                   if r.person is not None and not any(r.outread_by(o) for o in readings)),
                  key=lambda r: r.rank)
    # One person can be on two listings; the first listing's profile is kept,
    # and the listings are read committee first, whose profiles are the fuller.
    ordered: dict[tuple[str, ...], Person] = {}
    for reading in kept:
        ordered.setdefault(reading.person.parts, reading.person)
    return list(ordered.values())
