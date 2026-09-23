"""Which annual-report edition is this question asking for?

"Give me the latest annual report" cannot be answered by ranking, and the reason
is structural rather than a tuning problem. Every edition of the series is an
in-body attachment on one Drupal page, so all ten carry that page's
``effective_start_date`` — 2022-02-09. So:

* **relevance cannot separate them** — ten near-identical documents, and the
  breadcrumb the embedder saw names the page ("Annual Reports"), not the edition;
* **recency cannot separate them either** — "newest by date" is a ten-way tie,
  so the reranker's tie-break has nothing to break. The observed failure was
  page 148 of the 2020-21 edition, chosen by a hair of cosine noise.

The edition label is the only field that distinguishes them, and nothing read
it at query time. This module does, resolving the question to specific documents
*before* retrieval so the newest edition cannot simply be absent from the
candidate set.

Three properties keep it from disturbing anything else:

**It fails silent and returns ``[]``.** Every question that does not name the
series, or names it without saying *which* edition, resolves to ``None`` and
leaves retrieval byte-identical — including annual-report *content* questions
("what does the annual report say about solar"), which are about the series
rather than one edition of it.

**It never guesses.** An edition the corpus does not hold, a question naming
both ends ("first and latest"), or two competing series all resolve to ``None``.
A wrong scope is worse than no scope: it would answer confidently out of the
wrong document.

**A miss cannot starve retrieval.** The condition is a facet, not a date scope,
so ``retriever.retrieve`` drops it and retries unfiltered when it matches
nothing (see ``date_conditions``). The worst case is the behaviour that exists
today.

Filtering is by ``document_id`` rather than by ``edition_label``, deliberately:
the ids come from the catalogue, so the filter cannot be defeated by a label
spelled ``2023-2024`` in one store and ``2023-24`` in another.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.core.editions import EDITION_RE, find_editions, normalise_edition

logger = logging.getLogger(__name__)

__all__ = [
    "COUNT", "LIST", "EditionResolution", "SeriesDocument", "SeriesRequest",
    "conditions_for", "reset_cache", "resolve", "series_documents",
    "series_request",
]

# The question has to name the series itself. "TERI's latest report" is not this,
# and scoping it to an annual report would answer the wrong question.
_SERIES = re.compile(r"\bannual\s+reports?\b", re.IGNORECASE)

# Asks for the single oldest edition. Distinct from the "older/past" cues in
# `_WHOLE_SERIES`: "the oldest annual report" names one document, "older annual
# reports" does not.
_EARLIEST = re.compile(r"\b(?:earliest|oldest|first)\b", re.IGNORECASE)

# Asks for the newest edition explicitly. Resolves identically to the default —
# this only decides which `kind` is recorded, so a trace distinguishes "the user
# asked for the latest" from "the user did not say and we assumed it".
_LATEST = re.compile(
    r"\b(?:latest|newest|most\s+recent|recent-?most|current|this\s+year'?s?)\b",
    re.IGNORECASE,
)

# Asks about the series as a whole — a count, a list, a trend, or older editions
# without saying which. Narrowing any of these to one edition would answer a
# different question than the one asked, so they stay unfiltered.
_WHOLE_SERIES = re.compile(
    r"\b(?:all|every|each|both|list|listing|enumerate|how\s+many|count|"
    r"number\s+of|across|throughout|over\s+the\s+years|over\s+time|trend|"
    r"trends|evolution|timeline|history|historical|year[-\s]on[-\s]year|"
    r"year[-\s]over[-\s]year|since|compare|comparison|"
    r"older|previous|prior|past|earlier|archive|archived)\b",
    re.IGNORECASE,
)

# A bare year is only read as an edition when it sits against the series name —
# "annual report 2018", "2018 annual report". Anywhere else in the sentence it is
# far more likely to be part of the subject ("what the annual report says about
# the 2015 Paris Agreement"), and reading that as an edition would silently
# answer out of the wrong document.
_YEAR_AFTER = re.compile(
    r"\bannual\s+reports?\b(?:\s+(?:for|of|from|in))?\s+(20\d{2})\b", re.IGNORECASE
)
_YEAR_BEFORE = re.compile(r"\b(20\d{2})\s+annual\s+reports?\b", re.IGNORECASE)

# How a single edition is titled in the catalogue. The title is the anchor text
# the page uses for each link ("Annual Report 2024-2025"), which is the only
# place one edition's identity exists at all — the PDFs share a page, a date and
# (for several of them) a filename that names no year.
_TITLE_PREFIX = "Annual Report%"

# The series changes only when ingestion runs, so a per-question catalogue read
# would be waste. Short enough that a newly ingested edition appears without a
# restart.
_CACHE_TTL_SECONDS = 300.0

_cache: tuple[float, dict[str, tuple[str, ...]]] | None = None


@dataclass(frozen=True)
class EditionResolution:
    """One resolved edition, and enough context to explain the choice in a log."""

    #: Canonical ``YYYY-YY``, or several joined by "+" when the question named
    #: more than one (a comparison).
    edition: str
    #: The catalogued documents for those editions.
    document_ids: tuple[str, ...]
    #: How it was chosen. ``default_latest`` is kept distinct from ``latest`` so
    #: a log line says whether the user asked for the newest edition or simply
    #: did not say which one they wanted.
    kind: str
    #: Every edition the series holds, for the log line.
    available: tuple[str, ...] = ()

    def describe(self) -> str:
        return (
            f"{self.kind} -> {self.edition} "
            f"({len(self.document_ids)} document(s); series holds "
            f"{len(self.available)}: {', '.join(self.available)})"
        )


def reset_cache() -> None:
    """Forget the cached series.

    For tests. Production does not need it: the TTL is what picks up a newly
    ingested edition, so no ingestion path has to know this cache exists.
    """
    global _cache, _documents_cache
    _cache = None
    _documents_cache = None


def _read_series_rows() -> list[dict[str, Any]]:
    """Catalogued attachments whose title names an annual-report edition.

    A seam: the only function here that touches a store, so the resolution logic
    is testable without a database.

    Reads the catalogue rather than the decision table on purpose.
    ``documents_date_decision`` holds confidence scores and model verdicts and is
    documented as never being read back into retrieval; the catalogue's title is
    the same fact without the baggage.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT document_id, title, url FROM `{state_table()}` "
            "WHERE source_type = 'pdf_attachment' AND title LIKE %s",
            (_TITLE_PREFIX,),
        )
        return list(cur.fetchall())


def _series() -> dict[str, tuple[str, ...]]:
    """``{canonical edition: document ids}`` for the annual-report series.

    Grouped by the page each attachment hangs off, because "the series" is a
    page holding several editions — and the corpus contains unrelated documents
    whose titles begin the same way. The page holding the most editions wins,
    and only if it wins outright: a tie means two plausible series and no way to
    tell which one "the latest annual report" means, so nothing is resolved.
    """
    global _cache
    if _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_TTL_SECONDS:
        return _cache[1]

    try:
        rows = _read_series_rows()
    except Exception:
        logger.warning(
            "Could not read the annual-report series; retrieval proceeds "
            "unfiltered.", exc_info=True,
        )
        return {}

    by_page: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        edition = normalise_edition(row.get("title"))
        if edition is None:
            continue
        page = str(row.get("url") or "")
        by_page.setdefault(page, {}).setdefault(edition, []).append(
            str(row["document_id"])
        )

    series: dict[str, tuple[str, ...]] = {}
    if by_page:
        ranked = sorted(by_page.values(), key=len, reverse=True)
        if len(ranked) == 1 or len(ranked[0]) > len(ranked[1]):
            series = {e: tuple(sorted(ids)) for e, ids in ranked[0].items()}
        else:
            logger.info(
                "Two or more pages hold %d annual-report editions each; the "
                "series is ambiguous, so no edition is resolved.", len(ranked[0]),
            )

    _cache = (time.monotonic(), series)
    return series


def _requested(
    text: str, series: dict[str, tuple[str, ...]]
) -> tuple[list[str], bool]:
    """``(editions the series holds, whether one was pointed at)``.

    The second value is what stops a silent substitution. A question naming an
    edition the corpus does not have must leave retrieval alone, not fall
    through to the newest — answering "the 2012-13 annual report" out of the
    2024-25 edition is the worst outcome available here.

    Three ways a question points at an edition, in precedence order:

    1. a canonical span — "2024-25", "2019-2020", "2020/21";
    2. a **span shape that maps to no single edition** — "2019-2024" is a period
       and "2031-32" is outside the corpus. Both point at something specific
       that is not one edition, so neither may be narrowed or defaulted;
    3. a bare year against the series name — "annual report 2018". Only here,
       never elsewhere in the sentence (see :data:`_YEAR_AFTER`).
    """
    spans = find_editions(text)
    if spans:
        return [e for e in spans if e in series], True
    if EDITION_RE.search(text) is not None:
        return [], True
    years = dict.fromkeys(_YEAR_AFTER.findall(text) + _YEAR_BEFORE.findall(text))
    if years:
        return [e for year in years
                for e in series if e.startswith(f"{year}-")], True
    return [], False


def resolve(question: str) -> EditionResolution | None:
    """The edition(s) this question is about, or None to leave retrieval alone.

    **An unqualified "annual report" resolves to the newest edition.** That is
    the intended default, and it is a correction rather than a preference: the
    editions share a title and a date, so leaving the question unscoped does not
    search them even-handedly — it lets whichever chunk scores a hair higher
    decide, which is how "the latest annual report" answered out of the 2020-21
    edition. When a user says "the annual report" they mean the current one, and
    when they mean an older one they say so.

    The question therefore has to *earn* an unfiltered search, by asking about
    the series as a whole (a count, a list, a trend, or older editions without
    naming one). Those cues are listed in :data:`_WHOLE_SERIES`.

    Lexical and deterministic throughout. This is a scope decision applied
    before search, so it must be reproducible and free — an LLM call here would
    add latency and variance to every question that mentions the series.
    """
    text = question or ""
    if not _SERIES.search(text):
        return None

    series = _series()
    if not series:
        return None

    named, pointed = _requested(text, series)
    if pointed:
        if not named:
            logger.info(
                "Question points at an annual-report edition the series does "
                "not hold as a single edition (%s); retrieval stays unfiltered "
                "rather than answering out of another one.", ", ".join(sorted(series)),
            )
            return None
        return EditionResolution(
            edition="+".join(named),
            document_ids=tuple(i for e in named for i in series[e]),
            kind="named",
            available=tuple(sorted(series)),
        )

    # No edition named. A question about the series as a whole must not be
    # narrowed to one of them; anything else defaults to the newest.
    whole = _WHOLE_SERIES.search(text)
    if whole is not None:
        logger.info(
            "Question is about the annual-report series as a whole (%r); "
            "retrieval stays unfiltered.", whole.group(0),
        )
        return None

    if _EARLIEST.search(text) is not None:
        edition, kind = min(series), "earliest"
    else:
        edition = max(series)
        kind = "latest" if _LATEST.search(text) else "default_latest"

    return EditionResolution(
        edition=edition,
        document_ids=series[edition],
        kind=kind,
        available=tuple(sorted(series)),
    )


def conditions_for(resolution: EditionResolution | None) -> list[Any]:
    """Qdrant conditions scoping retrieval to the resolved edition.

    By ``document_id``, so the scope holds regardless of how a label happens to
    be spelled in the payload. The parent page is deliberately excluded: it
    lists every edition, so admitting it re-introduces exactly the confusion the
    filter exists to remove.
    """
    if resolution is None or not resolution.document_ids:
        return []
    from qdrant_client.models import FieldCondition, MatchAny

    return [
        FieldCondition(
            key="document_id", match=MatchAny(any=list(resolution.document_ids))
        )
    ]


# --------------------------------------------------------------------------- #
# The series as a whole: listing it, and counting it
# --------------------------------------------------------------------------- #
# `resolve` above answers "which edition?" and deliberately returns None when the
# question is about the series rather than one of its editions — narrowing
# "list the annual reports" to the newest one would answer a different question.
#
# That None was the whole answer, and it threw away the fact that produced it.
# "list of annual reports" therefore fell through to semantic search, which is
# unfiltered for exactly this reason, and the model was asked to find a list of
# editions in two pages of report prose. It refused, correctly, and the citation
# fallback attached whatever chunks the unfiltered pull had found.
#
# So the verdict is reported instead of discarded. It is kept apart from
# `EditionResolution` on purpose: a series request must NOT become a Qdrant
# filter, and giving it document ids in the same shape as an edition resolution
# is the one mistake that would make that happen by accident.


@dataclass(frozen=True)
class SeriesDocument:
    """One edition, with what a source card needs to describe it."""

    edition: str
    document_id: str
    title: str
    url: str | None = None


@dataclass(frozen=True)
class SeriesRequest:
    """A question about the series itself: list it, or count it."""

    #: ``list`` or ``count``.
    kind: str
    documents: tuple[SeriesDocument, ...] = ()

    @property
    def editions(self) -> tuple[str, ...]:
        return tuple(d.edition for d in self.documents)

    def describe(self) -> str:
        return f"{self.kind} -> {len(self.documents)} edition(s)"


LIST = "list"
COUNT = "count"

# Asking how many there are. Kept apart from the list cues because the answer
# shape differs, not because the detection does.
_SERIES_COUNT = re.compile(
    r"\bhow\s+many\b|\bnumber\s+of\b|\bcount\s+of\b", re.IGNORECASE
)

# Asking to see them enumerated. Deliberately *narrower* than `_WHOLE_SERIES`:
# that pattern also catches "trends", "over the years", "compare" and "history",
# which are analytical questions about the series' contents and must stay on the
# content path. Its job is to stop a question being narrowed to one edition; it
# is not a claim that the question wants a list.
_SERIES_ENUMERATE = re.compile(
    r"\ball\b|\bevery\b|\beach\b|\blist\b|\blisting\b|\benumerate\b"
    r"|\bwhich\s+ones\b|\bavailable\b",
    re.IGNORECASE,
)

# Analytical readings that name the series in the plural but want its substance.
# "compare the annual reports" is a content question wearing a plural noun.
_SERIES_ANALYSIS = re.compile(
    r"\bcompare\b|\bcomparison\b|\btrends?\b|\bevolution\b|\btimeline\b"
    r"|\bhistor(?:y|ical)\b|\bover\s+the\s+years\b|\bover\s+time\b"
    r"|\bacross\b|\bthroughout\b|\bchanged?\b|\bchanges\b"
    r"|\byear[-\s]on[-\s]year\b|\byear[-\s]over[-\s]year\b",
    re.IGNORECASE,
)

# The plural form. "the annual reports" names the series; "the annual report"
# names one of them, which is what keeps "show me the latest annual report" on
# the single-document path.
_SERIES_PLURAL = re.compile(r"\bannual\s+reports\b", re.IGNORECASE)

_documents_cache: "tuple[float, tuple[SeriesDocument, ...]] | None" = None


def series_documents() -> tuple[SeriesDocument, ...]:
    """Every edition of the series, newest first, with title and URL.

    An accessor over rows :func:`_read_series_rows` already reads — it selects
    ``document_id, title, url`` and :func:`_series` keeps only the ids, so the
    card metadata was being fetched and dropped on every call.

    Which page's editions count is not re-decided here: :func:`_series` owns
    that (the page holding the most editions wins, and only outright), and this
    filters the rows down to the ids it returned. One reader of that rule, not
    two.

    Returns ``()`` on any failure, which leaves every caller on the path it
    would have taken before this existed.
    """
    global _documents_cache
    if (
        _documents_cache is not None
        and (time.monotonic() - _documents_cache[0]) < _CACHE_TTL_SECONDS
    ):
        return _documents_cache[1]

    series = _series()
    if not series:
        return ()
    edition_of = {doc_id: edition for edition, ids in series.items() for doc_id in ids}
    try:
        rows = _read_series_rows()
    except Exception:
        logger.warning("Could not read the annual-report series documents.",
                       exc_info=True)
        return ()

    documents = tuple(sorted(
        (
            SeriesDocument(
                edition=edition_of[str(row["document_id"])],
                document_id=str(row["document_id"]),
                title=str(row.get("title") or "").strip(),
                url=(str(row["url"]).strip() or None) if row.get("url") else None,
            )
            for row in rows
            if str(row.get("document_id") or "") in edition_of
        ),
        key=lambda d: d.edition,
        reverse=True,
    ))
    _documents_cache = (time.monotonic(), documents)
    return documents


def series_request(question: str) -> SeriesRequest | None:
    """Whether this question asks for the series itself, and in what shape.

    ``None`` for everything else, which is every question that reaches this
    today — so a miss leaves behaviour exactly as it is.

    Four conditions, and all of them have to hold:

    1. the question names the series **in the plural** ("the annual reports"),
       which is what separates it from "the latest annual report";
    2. it does not point at a particular edition (:func:`_requested`), so
       "annual reports 2019 and 2020" stays a two-edition question;
    3. it asks to enumerate or to count — a bare plural is not enough on its
       own, because "annual reports on water" is a topic, not a request for the
       index. ``show me the annual reports`` qualifies through the locate cues
       :mod:`app.retrieval.understanding.document_request` already defines;
    4. it is not asking about the *contents* of the reports, or about how they
       changed. "how many annual reports discuss solar" counts documents by
       what is inside them and belongs on the content path, not here.
    """
    from app.retrieval.understanding.document_request import (
        _ABOUT_CONTENT,
        _LOCATE,
    )

    text = question or ""
    if not _SERIES_PLURAL.search(text):
        return None

    counting = bool(_SERIES_COUNT.search(text))
    # The counting phrase is the *operation*, so it is removed before the
    # question is read for content cues. Without this "what is the number of
    # annual reports" is rejected by `_ABOUT_CONTENT`'s own "numbers?" — which
    # is there to catch "the key numbers in the report", a different thing.
    # Removing only the matched phrase keeps the rest of the sentence readable:
    # "how many annual reports discuss solar" still has "discuss", and is still
    # a content question.
    subject = _SERIES_COUNT.sub(" ", text)
    if _ABOUT_CONTENT.search(subject) or _SERIES_ANALYSIS.search(subject):
        return None

    if not (counting or _SERIES_ENUMERATE.search(text) or _LOCATE.search(text)):
        return None

    series = _series()
    if not series:
        return None
    if _requested(text, series)[1]:
        return None

    documents = series_documents()
    if not documents:
        return None
    request = SeriesRequest(kind=COUNT if counting else LIST, documents=documents)
    logger.info("annual-report series: %s", request.describe())
    return request
