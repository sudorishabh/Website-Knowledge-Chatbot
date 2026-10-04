"""The home page's thematic areas: which they are, and what each is about.

The list of themes is the home page's to give — its "Thematic Areas" section —
but the page shows each theme as a name over a teaser cut mid-sentence ("In the
post-Paris agreement era, accelerating climate action is the biggest..."). The
priority page list describes every theme page in one full sentence, so a theme
is described from there, and from its teaser only when the list has no page
for it.

Nothing here touches the network: :func:`app.retrieval.priority.evidence.thematic_areas`
reads the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.retrieval.priority.extract import PageContent
from app.retrieval.priority.registry import THEME, PriorityPage, Registry, normalize_text

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
