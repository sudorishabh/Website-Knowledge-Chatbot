"""A group's own listing page: the members it names, as the whole answer to a
request for that list.

The Centres of Excellence page is a grid of cards, each a centre's name linked
to its page, and nothing more (nine names, surveyed 2026-10-22). Asked for the
list, the answer used to be the group's block, written from the page list's
descriptions, followed by whatever the corpus held on any centre of
excellence: a 2009 Urban Development centre, the ICC-TERI CSR centre, 2016
news of the Mahindra centre. That was four to eight sources for a list one
page gives. A request for the list and nothing else
(`match.asks_only_for_group`) is answered with the names the live page gives,
citing that page alone; a question about one centre still reads its own page.

Nothing here touches the network: :func:`app.retrieval.priority.evidence.group_listing`
reads the page.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.retrieval.priority.extract import PageContent
from app.retrieval.priority.registry import normalize_text


@dataclass(frozen=True)
class GroupListing:
    """The members one read of a group's listing page names, and that page."""

    #: The group's name in the page list ("Centres of Excellence").
    group: str
    #: As the page spells them, in its order.
    names: tuple[str, ...]
    url: str
    title: str | None = None


def members_on(content: PageContent) -> list[str]:
    """The members ``content`` (a group's listing page) names, in its order:
    the lines that are a link's label, since each card's title links to its
    member's page. Once each. [] when there are none, which the caller treats
    as an unreadable page."""
    linked = {normalize_text(link.text) for link in content.links}
    names: list[str] = []
    seen: set[str] = set()
    for section in content.sections:
        # A section's first line is its heading, when it has one.
        for line in section.text.splitlines()[1 if section.heading else 0:]:
            key = normalize_text(line)
            if key in linked and key not in seen:
                seen.add(key)
                names.append(line.strip())
    return names


def listing_answer(listing: GroupListing, *, output_format: str = "default") -> str:
    """The whole answer: how many members the page lists and their names, in
    its order, then an offer to say more about any one. '' with no names."""
    names = listing.names
    if not names:
        return ""
    lead = f"TERI has {len(names)} {listing.group}:"
    if output_format == "table":
        rows = [f"| # | {listing.group} |", "| ---: | --- |"]
        rows += [f"| {n} | {name.replace('|', '/')} |" for n, name in enumerate(names, start=1)]
        body = "\n".join(rows)
    else:
        body = "\n".join(f"{n}. {name}" for n, name in enumerate(names, start=1))
    return f"{lead}\n\n{body}\n\nAsk about any one of them to hear more about its work."
