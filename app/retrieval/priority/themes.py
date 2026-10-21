"""The home page's thematic areas: which they are, and what each is about.

The list of themes is the home page's to give — its "Thematic Areas" section —
but the page shows each theme as a name over a teaser cut mid-sentence ("In the
post-Paris agreement era, accelerating climate action is the biggest..."). The
priority page list describes every theme page in one full sentence, so a theme
is described from there, and from its teaser only when the list has no page
for it.

People ask for the themes by many names — research areas, divisions,
departments, the fields or sectors the organisation works in — and
:func:`asks_for_themes` hears them all, so that every one of them reaches the
home page's list.

Nothing here touches the network: :func:`app.retrieval.priority.evidence.thematic_areas`
reads the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.retrieval.priority.extract import PageContent
from app.retrieval.priority.registry import (
    THEME,
    THEMES_OVERVIEW,
    THEMES_OVERVIEW_WORDS,
    PriorityPage,
    Registry,
    normalize_text,
)

#: The home page section that lists the themes, normalized.
SECTION = "thematic areas"
#: The end of a teaser the home page cut short.
_CUT = re.compile(r"\s*(?:\.{3}|…)\s*$")
#: Longest line read as the name of a theme the page list does not know.
_MAX_NAME_WORDS = 8


@dataclass(frozen=True)
class ThematicArea:
    #: As the home page spells it.
    name: str
    #: One sentence on what the theme covers; "" when there is none.
    description: str
    #: The theme's own page, when the page list has one.
    url: str | None = None


@dataclass(frozen=True)
class ThemeListing:
    """The themes one read of the home page lists, and that page."""

    areas: tuple[ThematicArea, ...]
    url: str
    title: str | None = None


def _theme_pages(registry: Registry) -> dict[str, PriorityPage]:
    return {normalize_text(p.topic): p for p in registry.pages if p.kind == THEME}


def _section_lines(content: PageContent) -> list[str]:
    """The lines of the "Thematic Areas" section, without its heading. A long
    section is split into pieces that each repeat the heading."""
    lines: list[str] = []
    for section in content.sections:
        if normalize_text(section.heading) != SECTION:
            continue
        lines.extend(line.strip() for line in section.text.splitlines()[1:] if line.strip())
    return lines


def _teaser(text: str) -> str:
    """A teaser as a description: a cut one ends in an ellipsis, not "..."."""
    return f"{_CUT.sub('', text).rstrip(' ,;:')}…" if _CUT.search(text) else text


def areas_on(content: PageContent, registry: Registry) -> list[ThematicArea]:
    """The thematic areas ``content`` (the home page) lists, in its order.

    A line is a theme's name when the page list has a page for that theme, or
    when it is short and the next line is a teaser — so a theme added to the
    home page is listed before anyone adds its page. [] when the section is
    missing, which the caller treats as an unreadable page."""
    pages = _theme_pages(registry)
    lines = _section_lines(content)

    def is_name(i: int) -> bool:
        line = lines[i]
        if normalize_text(line) in pages:
            return True
        following = lines[i + 1] if i + 1 < len(lines) else ""
        return (bool(_CUT.search(following)) and not _CUT.search(line)
                and len(line.split()) <= _MAX_NAME_WORDS)

    names = [is_name(i) for i in range(len(lines))]
    areas: list[ThematicArea] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        key = normalize_text(line)
        if not names[i] or key in seen:
            continue
        seen.add(key)
        page = pages.get(key)
        teaser = lines[i + 1] if i + 1 < len(lines) and not names[i + 1] else ""
        description = (page.description if page is not None and page.description
                       else _teaser(teaser))
        areas.append(ThematicArea(name=line, description=description,
                                  url=page.url if page is not None else None))
    return areas


# -- questions that ask for the themes ---------------------------------------------

#: The organisation in normalized text, or the assistant standing for it ("what
#: divisions do you have").
_ORG = re.compile(r"(?<!\w)(?:teri|your?|(?:the )?energy and resources institute)(?!\w)")
#: "What sectors does TERI work in", "the areas TERI works on": the fields the
#: organisation works in are its themes.
_WORKS_IN = re.compile(
    r"(?<!\w)(?:areas|fields|sectors|domains|disciplines|topics|subjects)(?:\s+\w+){0,3}?"
    r"\s+(?:teri|you)\s+(?:works?|working|focus(?:es|ed)?|operates?|operating|"
    r"speciali[sz](?:e|es|ed)|research(?:es)?|covers?|covering)(?!\w)"
)
#: The words a request for a list is made of, whatever it lists, and the verbs
#: it asks what the organisation does with ("what research areas does TERI
#: work in").
_QUESTION_WORDS = frozenset(
    """
    what which whats are is was were be the a an of do does did have has had list
    show tell give name me us about all its it their main major key core different
    various current currently primary please kind kinds type types in on at to for
    by and or can could would i we want know how many number
    work works working focus focuses cover covers
    """.split()
)


def _phrase(phrase: str) -> str:
    """Whole-word pattern for ``phrase``, tolerating a plural."""
    return rf"(?<!\w){re.escape(phrase)}s?(?!\w)"


def asks_for_themes(question: str) -> bool:
    """Whether ``question`` asks for the organisation's themes, by any of the
    names people give them: a phrase that asks for them anywhere ("research
    areas", "areas of expertise"), a plural word that does beside the
    organisation's name ("TERI divisions", "which departments does TERI
    have"), or the fields it works in ("what sectors does TERI work in")."""
    words = normalize_text(question)
    if any(re.search(_phrase(p), words) for p in THEMES_OVERVIEW):
        return True
    if _WORKS_IN.search(words):
        return True
    return bool(THEMES_OVERVIEW_WORDS & set(words.split())) and _ORG.search(words) is not None


def asks_only_for_themes(question: str) -> bool:
    """Whether ``question`` asks for the list of themes and for nothing else:
    once the request, the organisation's name and the words any question is
    made of are taken out, nothing is left.

    Measured 2026-10-17: "TERI's research areas", "TERI divisions" and "what
    fields does TERI work in" were read as passage questions or small talk, and
    answered with the themes mixed into a Policy page's topics, with the
    divisions of a 2017 annual report, or not at all. Read as a listing, each
    gets the home page's seven areas. "TERI's research areas and their key
    projects" leaves "projects", and "which TERI themes have the most
    publications" leaves "most publications": both are answered as before.
    """
    if not asks_for_themes(question):
        return False
    words = normalize_text(question)
    for phrase in THEMES_OVERVIEW:
        words = re.sub(_phrase(phrase), " ", words)
    words = _ORG.sub(" ", _WORKS_IN.sub(" ", words))
    return all(w in _QUESTION_WORDS or w in THEMES_OVERVIEW_WORDS for w in words.split())
