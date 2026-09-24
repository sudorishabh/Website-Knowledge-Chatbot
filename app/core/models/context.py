from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextBlock:
    """One numbered passage handed to answer generation.

    The shared contract between retrieval (which produces the ordered list via
    ``context_builder.build_context``) and generation (which formats and cites
    it). Lives in the neutral core layer so generation never has to import a
    retrieval implementation module.
    """

    n: int
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    conflict: bool = False
    also_available: list[dict[str, Any]] = field(default_factory=list)
    #: Set when a *newer* block in the same context describes this block's
    #: subject in the past tense — a role that has since changed hands, an
    #: affiliation since left. ``superseded`` marks the older statement,
    #: ``supersedes`` the newer one that dates it. Both are only ever set for a
    #: question about the present; see
    #: ``app.retrieval.context.builder.flag_supersession``.
    #:
    #: Advisory, not a filter. The older block stays in the context and stays
    #: citable — it is still the evidence for what was true then — and the flags
    #: reach the prompt so the answer can say "was" where it would otherwise
    #: have said "is".
    superseded: bool = False
    supersedes: bool = False


# The marker `app.retrieval.graph.facts` puts on its block, and the predicate
# that recognises one. Defined in the neutral core because three layers have to
# agree about it — retrieval builds the block, generation labels it in the
# prompt, and the citation builder describes it to the user — and a copy of the
# literal in any of them is a copy that can drift. The block is deliberately
# given no ``source_type``: it did not come from a document, and every function
# that reads a source kind has to be able to say so.
GRAPH_FACTS_KIND = "graph_facts"


def is_graph_facts(payload: dict[str, Any]) -> bool:
    """Whether this payload is the graph's verified-relationships block."""
    return payload.get("kind") == GRAPH_FACTS_KIND


# The marker `app.retrieval.priority` puts on a block read from the live site at
# question time. Here for the same reason as the graph's: retrieval builds the
# block and generation labels it, and neither may import the other's module to
# agree on a literal. Unlike the facts block it *is* a web page, so it keeps
# ``source_type="website"`` and cites like one.
PRIORITY_PAGE_KIND = "priority_page"


def is_priority_page(payload: dict[str, Any]) -> bool:
    """Whether this payload is a section of a priority page read live."""
    return payload.get("kind") == PRIORITY_PAGE_KIND


# Storage values that all mean "a page on the website". ``website`` is
# canonical; ``article`` is what points indexed before the rename carry. Defined
# here so retrieval and generation share one list — a copy that forgets the alias
# reads a legacy point as a different kind of source than its neighbours do.
WEBSITE_SOURCE_TYPES: tuple[str, ...] = ("website", "article")


def source_kind(payload: dict[str, Any]) -> str | None:
    """The payload's source type with the legacy website alias folded in."""
    source_type = payload.get("source_type")
    return "website" if source_type in WEBSITE_SOURCE_TYPES else source_type


def page_span(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    """The (first, last) page the payload's text covers, or (None, None).

    One definition, because the prompt header and the citation must never
    disagree about which pages a block is standing on. ``page_range`` is
    authoritative when present — the context builder rewrites it to describe the
    text it actually admitted — and a lone ``page_number`` reads as a one-page
    span. Nothing is inferred when neither is set: an unpaginated source has no
    page, and inventing one is worse than showing none.
    """
    span = payload.get("page_range")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        try:
            return int(span[0]), int(span[1])
        except (TypeError, ValueError):
            pass
    page = payload.get("page_number")
    if isinstance(page, int):
        return page, page
    return None, None
