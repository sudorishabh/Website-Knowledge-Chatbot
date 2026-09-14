"""Turning the question's parts into separately retrievable sub-queries.

What this is, and what it is not
--------------------------------
It is **not** a decomposer. The decomposition already exists:
:func:`app.generation.answer_plan.extract_requirements` splits a question into
the distinct things it asks for, it already runs on every query that reaches
retrieval, and its output is what this module consumes. Nothing here asks a
model anything.

What it is, is the step that was missing between that split and retrieval. Today
the requirements are used only *after* the fact — `build_plan` checks each one
against whatever the single search happened to return, and the prompt is told
which parts went uncovered. That is a report on a gap, not a fix for it: one
embedding of "which customers were affected by Project X and how much revenue
did they lose?" is a blend that matches neither half well, and no amount of
telling the model about the miss puts the missing passage in the context.

The shape of a requirement
--------------------------
`extract_requirements` returns short **noun phrases** ("services",
"certifications", "revenue lost"), not standalone questions — so a requirement
cannot be searched as-is without losing what it is *about*. "Certifications"
alone is a different query from "TERI's certifications".

So each sub-query is the requirement re-anchored to the query's own proper
nouns, taken from :func:`app.retrieval.search.strategies.extract_key_terms` —
the quoted phrases, capitalised names, acronyms and codes that module already
identifies as the terms dense vectors handle worst. "Project X" + "revenue lost"
is a searchable query; "revenue lost" on its own is not.

This mirrors what the content-term leg already does deliberately — pull on the
topical words alone and let RRF promote out of the small, on-subject set it
selects — rather than inventing a new retrieval idea.

Routing
-------
Each sub-query carries the legs it should be offered to. The legs themselves
still decide:

* **semantic** — always. One dense pull, fused into the ranking by the existing
  RRF, which is also what de-duplicates it against every other leg.
* **graph** — when :func:`app.retrieval.understanding.relational.read_relational`
  finds an approved predicate in the sub-query. That is the same deterministic
  probe query understanding already uses to recognise a relational question, and
  it is only a *nomination*: `policy.attempt` applies the real routing rules and
  declines by returning nothing, exactly as it does for the whole question.

The whole question is still routed as it always was — this adds legs, it never
replaces the base pull.

Why MySQL is not routed per part
---------------------------------
The catalog route needs a ``QueryAnalysis`` with an ``operation`` slot; without
one, ``answer_structured`` falls back to ``parse_structured``, which is an LLM
call. Routing each part to MySQL would therefore mean classifying each part —
a second decomposition system, which is the one thing this phase must not build.
The catalog is still consulted for the whole question on exactly the path it
always was (``query_pipeline._db_section``), so a mixed database+content question
keeps its catalog section and gains decomposed content retrieval around it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)

#: A question with one requirement is the ordinary case, and splitting it would
#: re-run the base pull under a different name. The same threshold
#: ``answer_plan.build_plan`` uses to decide a question is genuinely multi-part.
MIN_REQUIREMENTS = 2

SEMANTIC = "semantic"
GRAPH = "graph"

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class SubQuery:
    """One part of the question, and the legs it should be offered to."""

    text: str
    #: The requirement this came from, kept so a trace can say which part of the
    #: question a passage was fetched for.
    requirement: str
    routes: tuple[str, ...] = (SEMANTIC,)

    def goes_to(self, route: str) -> bool:
        return route in self.routes


def _anchors(search_query: str, limit: int = 2) -> list[str]:
    """The query's own proper nouns, to re-attach to a bare requirement.

    Reuses ``strategies.extract_key_terms``, which already isolates exactly the
    terms that carry identity — quoted phrases, capitalised names, acronyms,
    alphanumeric codes, years. Bounded to the first couple: the point is to say
    *what the question is about*, and stacking five of them turns a focused
    sub-query back into the blend it was split out of.

    Fails to ``[]``, which simply leaves the requirement to stand alone.
    """
    try:
        from app.retrieval.search.strategies import extract_key_terms

        return (extract_key_terms(search_query) or [])[:limit]
    except Exception:  # pragma: no cover - a probe must not break planning
        logger.debug("Anchor extraction failed.", exc_info=True)
        return []


def _text(requirement: str, anchors: Sequence[str]) -> str:
    """The searchable form of one requirement: its anchors, then the phrase.

    An anchor already present in the requirement is not repeated — "Project X
    revenue" needs no second "Project X" — so a requirement the extractor
    already qualified is left as it stands.
    """
    lowered = requirement.lower()
    missing = [a for a in anchors if a.lower() not in lowered]
    return _WHITESPACE.sub(" ", " ".join([*missing, requirement])).strip()


def _routes(text: str) -> tuple[str, ...]:
    """Which legs this sub-query is offered to. Semantic always; graph when the
    text names an approved relationship.

    A nomination, not a decision: the graph's own policy layer still applies the
    routing rules and declines whatever it cannot answer.
    """
    try:
        from app.retrieval.understanding.relational import read_relational

        if read_relational(text).is_relational:
            return (SEMANTIC, GRAPH)
    except Exception:  # pragma: no cover - a probe must not break planning
        logger.debug("Relational probe failed for %r.", text, exc_info=True)
    return (SEMANTIC,)


def plan(
    search_query: str, requirements: Sequence[str], *, limit: int = 3
) -> list[SubQuery]:
    """Sub-queries for a genuinely multi-part question, or ``[]``.

    Empty — and therefore a no-op everywhere downstream — for the ordinary
    single-part question, for a failed extraction (``extract_requirements``
    already returns ``[]`` on any error), and for anything that de-duplicates
    down to fewer than two distinct parts. That is what keeps the existing
    single-query path intact rather than merely reachable.

    ``limit`` bounds the fan-out: each sub-query costs one embedding and one
    Qdrant pull, and the legs are fused, so past a handful the marginal ranking
    is noise against real latency.
    """
    cleaned = [r.strip() for r in requirements or [] if r and r.strip()]
    if len(cleaned) < MIN_REQUIREMENTS:
        return []

    anchors = _anchors(search_query)
    out: list[SubQuery] = []
    seen: set[str] = set()
    for requirement in cleaned:
        text = _text(requirement, anchors)
        key = text.lower()
        # A requirement that re-anchors onto the same text as another, or onto
        # the base query itself, would re-run a pull that is already happening.
        if not text or key in seen or key == (search_query or "").strip().lower():
            continue
        seen.add(key)
        out.append(SubQuery(text=text, requirement=requirement, routes=_routes(text)))
        if len(out) >= limit:
            break
    # One sub-query is the single-query path with extra steps.
    return out if len(out) >= MIN_REQUIREMENTS else []


def texts(subqueries: Sequence[SubQuery], route: str) -> list[str]:
    """The sub-query texts offered to one leg, in plan order."""
    return [s.text for s in subqueries if s.goes_to(route)]
