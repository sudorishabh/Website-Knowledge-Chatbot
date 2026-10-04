"""Decide which theme groups a question is asking about.

"How many themes are there?" and "what other themes do you have?" are different
questions, and the difference decides whether the answer is the curated thematic
structure or the peripheral vocabulary beside it. This module makes that call.

Deterministic on purpose
------------------------
The classifier that produces the structured slots is a model, and asking it to
also judge Main-versus-Other would put the guarantee — *a generic question never
exposes Other themes* — behind a paraphrase. A short list of explicit markers is
free, inspectable, and testable, and the one thing it must get right is the
default.

Default-safe
------------
Anything unrecognised is :data:`SCOPE_MAIN`. The two failure modes are not
symmetric: answering a generic question with only the main thematic areas is
correct, while volunteering the peripheral ones is the leak this exists to
prevent. So breadth is only ever granted by an explicit request for it.

Scoped by construction
----------------------
This is consulted only once a question has already been routed to the
``list_themes`` operation, so the markers do not have to distinguish a theme
question from any other kind — "other" here is always "other *themes*".
"""
from __future__ import annotations

import re

from app.retrieval.structured.tools import SCOPE_ALL, SCOPE_MAIN, SCOPE_OTHER

__all__ = [
    "asks_for_other", "detect", "mentions_themes", "SCOPE_MAIN", "SCOPE_OTHER",
    "SCOPE_ALL",
]

# Words that make a question *about* themes. `detect` says which group a theme
# question wants; this says whether it is a theme question at all — and the two
# are different jobs. Conflating them applied a Main-theme restriction to every
# count, so "how many authors are there?" quietly excluded every author whose
# documents carry no main theme (955 -> 876) and a plain document count lost
# 2,620 untagged documents.
_ABOUT_THEMES = re.compile(
    r"\b(?:theme|themes|thematic|topic|topics|subject\s+area|focus\s+area)\w*\b",
    re.IGNORECASE,
)


def mentions_themes(question: str | None) -> bool:
    """Whether the question is about themes at all.

    A theme restriction is only ever right for a question that concerns themes.
    For anything else the honest scope is the whole catalog — a document with no
    theme is still a document, and an author with no themed work is still an
    author.
    """
    return bool(_ABOUT_THEMES.search(question or ""))

# An explicit request for the themes *outside* the main structure.
#
# The "…the main" forms come first and deliberately consume the word "main":
# "outside the main areas" and "besides the main ones" are ways of saying *not*
# main, and treating that "main" as a reference to the main structure would read
# them as asking for both sides. Since the matched span is removed before the
# main marker is tested (see `detect`), swallowing it here is what keeps the two
# readings apart.
#
# `\bother\b` does not fire on "another" — the 'o' there follows a word
# character, so there is no boundary.
_OTHER = re.compile(
    r"\b(?:"
    r"(?:outside|besides|beside|beyond|excluding|other\s+than|"
    r"apart\s+from|aside\s+from|not\s+(?:in|part\s+of))"
    r"\s+(?:the\s+)?main\w*"
    r"|non[-\s]?main\w*"
    r"|other|others|additional|remaining|extra|besides|"
    r"apart\s+from|aside\s+from|minor|peripheral|secondary"
    r")\b",
    re.IGNORECASE,
)

# An explicit request for the whole vocabulary, main and other together.
_ALL = re.compile(
    r"\b(?:all|every|entire|complete|full|exhaustive|whole)\b",
    re.IGNORECASE,
)

# An explicit reference to the main structure. Narrow on purpose: its only job
# is to spot a question naming *both* sides ("main and other themes"). Words
# that merely mean "theme" — "thematic area", "focus area" — are excluded,
# because a generic question uses them too and they say nothing about which
# group is wanted.
_MAIN = re.compile(
    r"\b(?:main|primary|core|key|major|principal|top[-\s]?level)\w*\b",
    re.IGNORECASE,
)


def asks_for_other(question: str | None) -> bool:
    """Whether the question names the themes outside the main structure ("other
    themes", "besides the main ones"). For the home page's list of themes this,
    not :func:`detect`, is the test: "tell me about all the thematic areas"
    asks for every theme the page lists, and read as :data:`SCOPE_ALL` it
    followed the page's seven with "Green Shipping" and five more (measured
    2026-10-04)."""
    return bool(_OTHER.search(question or ""))


def detect(question: str | None) -> str:
    """The theme scope a question is asking for.

    >>> detect("What are your main themes?")
    'main'
    >>> detect("What other themes are available?")
    'other'
    >>> detect("List all themes, main and other")
    'all'
    """
    text = question or ""
    if not text.strip():
        return SCOPE_MAIN

    # Remove the "other" markers before looking for a reference to the main
    # structure, so a "main" that belongs to the exclusion itself ("outside the
    # main areas") is not counted as the question naming both sides.
    remainder, wants_other = _OTHER.subn(" ", text)
    wants_main = bool(_MAIN.search(remainder))

    # Naming both sides asks for both, whichever order they appear in.
    if wants_other and wants_main:
        return SCOPE_ALL
    if wants_other:
        return SCOPE_OTHER
    # "all the main themes" is still a Main question — the totality being asked
    # for is the main structure, not the vocabulary around it.
    if _ALL.search(text) and not wants_main:
        return SCOPE_ALL
    return SCOPE_MAIN
