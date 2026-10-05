"""Query facet → Qdrant filter construction.

Translates the scope fields of a resolved ``QueryAnalysis`` (theme, author,
tags, source type, language, date range) into Qdrant ``FieldCondition`` /
``Filter`` objects for the qa retrieval path.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from app.core.dates import parse_iso_date

if TYPE_CHECKING:
    from app.retrieval.understanding.query_processor import QueryAnalysis

logger = logging.getLogger(__name__)

# The canonical payload fields every date scope is expressed over. Named once
# so the condition builder and `date_conditions` cannot drift apart. No Drupal
# field name appears on this path: a bundle names the field it is dated by at
# ingest, and what reaches the payload is the resolved canonical value.
_DATE_FIELD = "effective_start_date"
_END_FIELD = "effective_end_date"
_START_PRECISION = "start_precision"
_END_PRECISION = "end_precision"
#: Every canonical field a date scope may touch, for `date_conditions`.
_DATE_FIELDS = frozenset({_DATE_FIELD, _END_FIELD, _START_PRECISION, _END_PRECISION})

# Words that make a date phrase about *documents* rather than about a
# relationship. "Reports published between 2005 and 2010" is a publication-date
# scope; "what did X fund between 2005 and 2010" is not.
_PUBLICATION_LANGUAGE = re.compile(
    r"\b(?:publish(?:ed|ing)?|publication|issued|released|"
    r"(?:document|report|paper|article|study|studies|publication)s?\s+"
    r"(?:from|in|of|between|dated)|dated)\b",
    re.IGNORECASE,
)


def _is_relationship_time(analysis: "QueryAnalysis") -> bool:
    """Whether this query's dates bound a *relationship*, not a publication date.

    Both readings arrive here as the same two slots. Applying the document
    reading to the other one is not a near miss: `effective_start_date` is a fact about
    when a page was posted, and on this corpus it holds no value before 2010 at
    all, so scoping "what did the Department of Biotechnology fund between 2005
    and 2010" by it selects almost nothing. It also carries a second cost — a
    scope no graph template expresses makes graph routing fail closed
    (`graph.scope`), so the one path that *can* answer a validity question by
    interval overlap is the path the misreading shuts off. Measured: every
    year-range relational question in the benchmark reached
    `scope_unsupported`.

    The test is deterministic and narrow: the question names an approved
    predicate, and says nothing about publishing. A predicate cue is what makes
    "between 2005 and 2010" modify a relationship — there is one to modify.
    Anything else keeps the existing document scope exactly as it was, which is
    why "which documents were published between 2005 and 2010" is untouched.
    """
    from app.retrieval.understanding.relational import read_relational

    question = getattr(analysis, "search_query", "") or ""
    if not question or _PUBLICATION_LANGUAGE.search(question):
        return False
    return read_relational(question).is_relational


def _parse_bound(value: str | None, *, field: str = "date") -> datetime | None:
    """A Qdrant date bound: UTC-aware, unlike the naive datetimes the MySQL
    catalog takes. `DatetimeRange` compares against tz-aware payload values, so
    the zone has to be attached here."""
    parsed = parse_iso_date(value, field=field)
    return parsed.replace(tzinfo=timezone.utc) if parsed else None


def _theme_condition(theme: str) -> Any:
    """Filter for a theme scope: the documents MySQL says are in it.

    **MySQL is authoritative for theme membership.** The catalog holds the
    hierarchy (``documents_theme.theme_path``), so it is the only thing that can
    answer "and everything beneath it" — a scope of "Energy" has to reach a
    document tagged only "Rural Energy Access". Qdrant's job here is to rank
    passages inside the set the catalog picked, which is the same
    catalog-decides-membership shape ``scoped_retrieval`` already uses.

    This used to match the chunk payload's ``categories`` list by name. That was
    a second, independent copy of theme membership, and it was wrong in two ways
    at once: it was flat, so it never matched a document through its parent
    theme; and it was written at index time, so a rename or reclassification in
    MySQL left it stale until the document happened to be re-ingested. The
    payload field survives as a display/diagnostic value — nothing filters on it.

    Returns ``None`` when the catalog cannot answer, so the caller drops the
    theme scope instead of applying a membership set it does not trust. Failing
    open matches the rest of this path: a MySQL outage degrades retrieval to
    plain semantic search rather than breaking it.

    Also ``None`` when no document carries the theme. The theme vocabulary is
    the set of names documents carry, so a name none carries is not a theme —
    it is a word understanding put in the slot ("WSDS", an event series) — and
    it still reaches search through the question's own words. It used to
    become a filter matching nothing, left to the retriever's retry on an empty
    pull; but the title leg does not take the filters, so one stray title hit
    (a 2021 curtain raiser, for a question about 2026) kept the retry from
    firing and every other leg searched an empty set.
    """
    from qdrant_client.models import FieldCondition, MatchAny

    from app.catalog import queries as catalog

    ids = catalog.theme_document_ids(theme)
    if ids is None:
        logger.warning(
            "Could not resolve the %r theme scope from the catalog; continuing "
            "without a theme filter.", theme,
        )
        return None
    if not ids:
        logger.info("No document carries the theme %r; searching without it.", theme)
        return None
    return FieldCondition(key="document_id", match=MatchAny(any=ids))


def _facet_filters(analysis: "QueryAnalysis") -> list[Any]:
    from qdrant_client.models import FieldCondition, MatchAny, MatchValue

    conditions: list[Any] = []
    if analysis.theme:
        theme_scope = _theme_condition(analysis.theme)
        if theme_scope is not None:
            conditions.append(theme_scope)
    # `analysis.author` is intentionally NOT applied as a filter here. The stored
    # `authors` field is a KEYWORD index (exact-value match, no substring) that is
    # populated on only ~20% of chunks and holds full display names ("Ms Meena
    # Sehgal", "TERI Web Desk"). The understanding LLM extracts a loose form
    # ("TERI", "Sharma") that almost never equals a stored value, so as a hard AND
    # condition it excludes the ~80% of the corpus that has no author at all and
    # then misses the rest — turning strong matches into false refusals. Author
    # scoping stays on `analysis.author` for the structured/catalog path (which
    # LIKE-matches the MySQL facet table); the qa path relies on semantic search,
    # where author names in titles/text already surface author-relevant content.
    if analysis.tags:
        conditions.append(
            FieldCondition(key="tags", match=MatchAny(any=list(analysis.tags)))
        )
    if analysis.source_type == "pdf":
        # "PDFs" includes documents attached to web articles.
        conditions.append(
            FieldCondition(key="source_type", match=MatchAny(any=["pdf", "pdf_attachment"]))
        )
    elif analysis.source_type in ("website", "article"):
        # "website" is canonical; "article" accepted from the LLM and matched in
        # storage for points indexed before the rename.
        conditions.append(
            FieldCondition(key="source_type", match=MatchAny(any=["website", "article"]))
        )
    if analysis.language:
        conditions.append(
            FieldCondition(key="language", match=MatchValue(value=analysis.language))
        )
    lo = _parse_bound(analysis.date_from, field="date_from")
    hi = _parse_bound(analysis.date_to, field="date_to")
    if (lo is not None or hi is not None) and not _is_relationship_time(analysis):
        scope = date_scope_filter(lo, hi)
        if scope is not None:
            conditions.append(scope)
    return conditions


def _floor(value: datetime, precision: str) -> datetime:
    """``value`` rounded down to the start of its year, month or day."""
    if precision == "year":
        return value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if precision == "month":
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(value: datetime) -> str:
    return value.isoformat()


def date_scope_filter(lo: datetime | None, hi: datetime | None) -> Any:
    """A filter matching every document whose period **overlaps** ``[lo, hi)``.

    Not "its start falls in the range". A completed project running 2020-2024 is
    a document *about* 2022 and has to be returned for 2022, which the old
    single-bound condition could not do: it compared only
    ``effective_start_date``, so every period document was treated as a point at
    its start and a four-year project was invisible in three of its five years.

    Overlap is the two ordinary bounds, expressed over the canonical fields:

    * **upper** — the period starts before the range ends
      (``effective_start_date < hi``). The stored start is already the earliest
      instant the document covers, so this needs no precision adjustment.
    * **lower** — the period ends at or after the range begins. Which field
      carries that end, and how far it reaches, depends on the document:

      - a **closed period** (``effective_end_date`` present, e.g.
        ``completed_projects``, ``events``) ends there;
      - an **open-ended period** (a bundle in
        ``app.core.corpus.OPEN_ENDED_BUNDLES`` — ``ongoing_projects``, which
        declares no end field at all) runs to the present, so it has no lower
        bound to fail;
      - a **point** (everything else) ends where its own precision ends.

    Precision is why the lower bound is not one condition. A date stated as
    "2022" is stored as 2022-01-01, and a query for June 2022 must match it: we
    know the document is from 2022 and nothing says it is not from June.
    Comparing the stored 1 January against a June lower bound would assert a day
    the source never gave — the same refusal the answer layer makes when it
    renders "2022 (year only)". So the lower bound is applied against the range
    floored to the document's own precision: for a year-precision document,
    "does its year reach ``lo``"; for a month-precision one, "does its month".

    Boundaries follow the half-open convention the structured planner already
    documents: ``lo`` is included, ``hi`` is excluded. A period ending exactly on
    ``lo`` overlaps by one day and matches; one starting exactly on ``hi`` does
    not.

    Either bound may be None for an open-sided query ("after March 2023",
    "before 2020"). With both None there is no scope and None is returned.
    """
    from qdrant_client.models import (
        DatetimeRange,
        FieldCondition,
        Filter,
        IsEmptyCondition,
        MatchValue,
        PayloadField,
    )
    from app.core.corpus import OPEN_ENDED_BUNDLES

    def absent(key: str) -> Any:
        return IsEmptyCondition(is_empty=PayloadField(key=key))

    must: list[Any] = []
    if hi is not None:
        must.append(FieldCondition(key=_DATE_FIELD, range=DatetimeRange(lt=_iso(hi))))
    if lo is not None:
        reaches: list[Any] = []
        # A closed period: its stated end decides, at the end's own precision.
        for precision, marker in (("day", None), ("month", "month"), ("year", "year")):
            guard = (absent(_END_PRECISION) if marker is None
                     else FieldCondition(key=_END_PRECISION,
                                         match=MatchValue(value=marker)))
            reaches.append(Filter(must=[
                guard,
                FieldCondition(key=_END_FIELD,
                               range=DatetimeRange(gte=_iso(_floor(lo, precision)))),
            ]))
        # An open-ended period runs to the present, so no lower bound can
        # exclude it. `ongoing_projects` declares no end field at all, which
        # stored is indistinguishable from a single-date document — without this
        # branch a project running since 2005 is a point in 2005 and misses
        # every later year it was actually running in.
        if OPEN_ENDED_BUNDLES:
            reaches.append(Filter(should=[
                FieldCondition(key="bundle", match=MatchValue(value=bundle))
                for bundle in sorted(OPEN_ENDED_BUNDLES)
            ]))
        # A point: it ends where its own precision ends.
        for precision, marker in (("day", None), ("month", "month"), ("year", "year")):
            guard = (absent(_START_PRECISION) if marker is None
                     else FieldCondition(key=_START_PRECISION,
                                         match=MatchValue(value=marker)))
            reaches.append(Filter(must=[
                absent(_END_FIELD),
                guard,
                FieldCondition(key=_DATE_FIELD,
                               range=DatetimeRange(gte=_iso(_floor(lo, precision)))),
            ]))
        must.append(Filter(should=reaches))
    return Filter(must=must) if must else None


def _mentions_a_date_field(condition: Any) -> bool:
    """Whether this condition (or anything nested in it) bounds a date field.

    `date_conditions` used to test ``condition.key == _DATE_FIELD``, which was
    enough while a date scope was one ``FieldCondition``. Overlap needs a nested
    ``Filter``, and a nested filter has no ``key`` — so the scope the retry is
    supposed to preserve would have been silently dropped instead.
    """
    if getattr(condition, "key", None) in _DATE_FIELDS:
        return True
    empty = getattr(condition, "is_empty", None)
    if empty is not None and getattr(empty, "key", None) in _DATE_FIELDS:
        return True
    for group in ("must", "should", "must_not"):
        for nested in getattr(condition, group, None) or []:
            if _mentions_a_date_field(nested):
                return True
    return False


def date_conditions(filters: Sequence[Any] | None) -> list[Any]:
    """The subset of ``filters`` that bounds the publication date.

    Lets ``retriever.retrieve`` hold the date scope while dropping the rest on a
    total miss. The distinction is who chose the constraint: theme, author and
    source_type are the understanding LLM's guesses at how the corpus happens to
    be labelled, so discarding them recovers from a bad guess. A period is what
    the user actually asked for, and widening it answers about years they did not
    ask about — silently, because the retry is recorded on the trace span and the
    log, never in the answer text.

    Tolerates entries that aren't ``FieldCondition``s — the date scope returns a
    nested ``Filter`` — by looking for a canonical date field anywhere inside
    them rather than by type."""
    return [c for c in filters or [] if _mentions_a_date_field(c)]
