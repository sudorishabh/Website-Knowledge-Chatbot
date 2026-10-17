"""The contact page's offices: where each is, how to reach it, which centre it is.

The contact page lists every office as a run of lines — its name, its postal
address, then ``Tel:``, ``Email:`` and ``Contact person:`` lines — with nothing
in the markup between one office and the next. So an office ends at its last
contact line, and the next begins at the first line after it that is not one.

An office is matched to the regional centre it is by the centre's own names
("TERI Gram", "Himalayan Centre", "Bengaluru"): first by the office's name, then,
for an office whose name gives no place, by its address. A centre takes the
first office that names it, so a second office in the same city (the Centre for
Himalayan Studies, in Guwahati) is not taken for that city's centre, and an
office that is no centre's (TERI Japan, the TERI School of Advanced Studies) is
left out. The headquarters is the office the page heads "Headquarters".

Nothing here touches the network: :mod:`.evidence` reads the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from app.retrieval.priority.extract import PageContent
from app.retrieval.priority.registry import PriorityPage, normalize_text

#: A contact line: its label, then its value. The word boundary keeps an
#: address line in "Telangana" out.
_FIELD = re.compile(
    r"^(?P<label>tel(?:ephone)?|phone|mobile|fax|e-?mail|contact person)\b\s*[:.]?\s*"
    r"(?P<value>.*)$",
    re.I,
)
_HEADQUARTERS = "headquarters"


@dataclass(frozen=True)
class Office:
    name: str
    address: tuple[str, ...] = ()
    phone: str = ""
    email: str = ""
    contact_person: str = ""

    @property
    def postal_address(self) -> str:
        return ", ".join(line.rstrip(" ,") for line in self.address)

    def details(self) -> list[str]:
        """The office's address and contacts, one labelled line each."""
        fields = (("Address", self.postal_address), ("Tel", self.phone),
                  ("Email", self.email), ("Contact person", self.contact_person))
        return [f"{label}: {value}" for label, value in fields if value]


def _content_lines(content: PageContent) -> list[str]:
    """The page's lines, without the heading each section repeats."""
    lines: list[str] = []
    for section in content.sections:
        rows = [row.strip() for row in section.text.splitlines() if row.strip()]
        if rows and section.heading and rows[0] == section.heading:
            rows = rows[1:]
        lines.extend(rows)
    return lines


def _office(rows: list[str]) -> Office | None:
    """The office one run of lines describes; None for a run with no contact
    line, which is page text rather than an office."""
    place = [row for row in rows if not _FIELD.match(row)]
    fields: dict[str, list[str]] = {}
    for row in rows:
        found = _FIELD.match(row)
        if found and found.group("value").strip():
            label = found.group("label").lower().replace("-", "")
            key = "phone" if label in ("tel", "telephone", "phone", "mobile") else label
            fields.setdefault(key, []).append(found.group("value").strip())
    if not place or not fields:
        return None
    return Office(
        name=place[0],
        address=tuple(place[1:]),
        phone="; ".join(fields.get("phone", [])),
        email="; ".join(fields.get("email", [])),
        contact_person="; ".join(fields.get("contact person", [])),
    )


def offices_on(content: PageContent) -> list[Office]:
    """The offices ``content`` (the contact page) lists, in its order."""
    runs: list[list[str]] = []
    after_contact = True
    for row in _content_lines(content):
        is_contact = _FIELD.match(row) is not None
        if not is_contact and after_contact:
            runs.append([])
        if runs:
            runs[-1].append(row)
        after_contact = is_contact
    return [office for office in map(_office, runs) if office is not None]


def headquarters(offices: Sequence[Office]) -> Office | None:
    return next((o for o in offices if normalize_text(o.name) == _HEADQUARTERS), None)


def _mentions(text: str, phrase: str) -> bool:
    """Whole-word match on normalized text."""
    return bool(phrase) and f" {phrase} " in f" {normalize_text(text)} "


def _centre_named(text: str, centres: Sequence[PriorityPage]) -> PriorityPage | None:
    """The one centre ``text`` names, or None when it names none or several."""
    named = [c for c in centres
             if any(_mentions(text, n) for n in (normalize_text(c.name), *c.aliases))]
    return named[0] if len(named) == 1 else None


def by_centre(offices: Sequence[Office], centres: Sequence[PriorityPage]) -> dict[str, Office]:
    """Each centre's office, keyed by the centre's page key. A centre the page
    lists no office for is absent."""
    found: dict[str, Office] = {}
    taken: set[int] = set()
    for text_of in (lambda o: o.name, lambda o: o.postal_address):
        for index, office in enumerate(offices):
            if index in taken:
                continue
            centre = _centre_named(text_of(office), centres)
            if centre is not None and centre.key not in found:
                found[centre.key] = office
                taken.add(index)
    return found
