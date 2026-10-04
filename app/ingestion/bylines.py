"""Author names out of a Drupal author field — one name per person.

Most author fields are lists the CMS already split (people references, the
external-author fields), and each item is one person. The free-text ones —
``field_rpaper_authors`` on research papers, ``field_author`` on policy briefs —
are bylines typed by hand, and those used to be split on every comma. That is
right for the corpus's usual form and wrong for the others:

=====================================================  =========================
Byline                                                 Comma-split produced
=====================================================  =========================
``Pandey Suneel, Khan M Emran``                        two people (correct)
``Sehgal, Meena``                                      ``Sehgal`` and ``Meena``
``Kumar, R., Das, M. M., … & Sharma, P.``              ``Kumar``, ``R.``, ``Das``,
                                                       ``M. M.`` … ``& Sharma``
``Dhup Saumya and Dhawan Vibha``                       one "person"
``Deepa N; Ganguly Shantanu``                          one "person"
=====================================================  =========================

Measured on the live site (2026-10-04): of 682 free-text bylines, 37 use "and"
or "&" between people, 9 use semicolons, and about a dozen are written
citation-style, "Surname, Initials".

The rules below are deliberately narrow. Separators are ``,`` ``;`` ``&`` and
the word "and". A part that is nothing but initials ("R.", "M. M.", "BN") is
the second half of the name before it. Two single-word parts are joined into one
person only where the byline's own shape says so — the whole byline is exactly
two words ("Sehgal, Meena"), a semicolon chunk is ("Qamar, Sharif"), or the
byline is citation-style, where parts alternate surname and given name. A lone
single word anywhere else is a real one-word name ("Snehmani", "Neha", "Kusum"
all occur) and is left alone. Name order is never changed: a joined pair is
written "Surname Given", the corpus's prevailing form.

Nothing here decides who is who. Like :mod:`app.catalog.author_names`, it only
undoes formatting.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app.catalog.author_names import TITLES

# Zero-width characters editors paste in with a name; they make two identical
# names differ.
_INVISIBLE = re.compile("[​‌‍⁠﻿]")
_WS = re.compile(r"\s+")
# Between two people. "and" only as a whole word, so "Anand" survives.
_SEPARATOR = re.compile(r"\s*(?:,|;|&|\band\b)\s*", re.IGNORECASE)
# A footnote digit glued to a name ("Miranda1 Ana F").
_FOOTNOTE = re.compile(r"(?<=[^\W\d_])\d+\b")
# Characters that are never the edge of a name ("David Palchak |").
_EDGE_JUNK = " |*-–—"
# Initials alone: dotted ("R.", "M. M.", "S.A.", "J. V") or up to two bare
# capitals ("M", "BN"). Bare runs of three or more are acronyms ("NRDC").
_INITIALS = re.compile(r"^(?:(?:[A-Z]\.\s*)+[A-Z]?|[A-Z]{1,2})$")
_TITLE = re.compile(r"^(?:" + "|".join(TITLES) + r")\.?\s+", re.IGNORECASE)


def _tidy(text: Any) -> str:
    # Whitespace runs of any kind (tabs, non-breaking spaces) become one space.
    # Not NFKC: that splits a spacing accent into space + combining mark, so
    # "Julien-Franc¸ois" would come out as "Franc ̧ois".
    text = _INVISIBLE.sub("", str(text or ""))
    return _WS.sub(" ", text).strip(_EDGE_JUNK).strip()


def _is_initials(part: str) -> bool:
    return bool(_INITIALS.match(part))


def _one_word(part: str) -> bool:
    """A single capitalised word, courtesy title aside, that is not an acronym."""
    bare = _TITLE.sub("", part)
    return " " not in bare and not bare.isupper() and not _is_initials(bare)


def _people(parts: list[str], *, citation: bool) -> list[str]:
    """Join the parts that are halves of one name; keep the rest whole."""
    people: list[str] = []
    joinable = False  # whether the last person can still take a second half
    for part in parts:
        if people and joinable and _is_initials(part):
            people[-1] = f"{people[-1]} {part}"
            joinable = False
        elif people and joinable and citation and _one_word(people[-1]) and _one_word(part):
            people[-1] = f"{people[-1]} {part}"
            joinable = False
        else:
            people.append(part)
            joinable = not _is_initials(part)
    return people


def _chunk(text: str, *, citation: bool) -> list[str]:
    parts = [p for p in (_tidy(p) for p in _SEPARATOR.split(text)) if p]
    if len(parts) == 2 and all(_one_word(p) for p in parts):
        return [f"{parts[0]} {parts[1]}"]  # "Sehgal, Meena" is one person
    return _people(parts, citation=citation)


def split_byline(byline: str) -> list[str]:
    """The people a free-text byline names, in byline order."""
    text = _FOOTNOTE.sub("", _tidy(byline))
    if not text:
        return []
    parts = [p for p in (_tidy(p) for p in _SEPARATOR.split(text)) if p]
    # Citation style: some part is initials alone, following a name.
    citation = any(_is_initials(p) for p in parts[1:])
    if ";" in text and not citation:
        # Semicolons separate people; a comma inside a chunk is "Surname, Given".
        return [name for chunk in text.split(";") for name in _chunk(chunk, citation=False)]
    return _chunk(text, citation=citation)


def authors_from(value: Any) -> list[str]:
    """Author names from one Drupal author field, cleaned and de-duplicated.

    A list is taken item by item — the CMS already separated the people — and a
    string is read as a byline. Email addresses are dropped: they are account
    names some author references resolve to, never a name to show."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        names: Iterable[str] = (_tidy(_FOOTNOTE.sub("", str(v))) for v in value)
    else:
        names = split_byline(str(value))
    seen: dict[str, None] = {}
    for name in names:
        if name and "@" not in name:
            seen.setdefault(name, None)
    return list(seen)
