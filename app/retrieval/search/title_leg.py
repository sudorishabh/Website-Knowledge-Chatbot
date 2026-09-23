"""Title-anchored retrieval: find the page the question names.

Why a leg of its own
--------------------
Dense retrieval ranks by how a *passage* reads, and an organisation's canonical
pages are frequently the worst-reading passages it has. The Centres of Excellence
hub page is a list of link labels — "CONCOR-TERI Centre of Excellence for Green
and Sustainable Logistics (https://...) Read More (https://...)", nine times over
— so its embedding sits far from "What are TERI's flagship initiatives and
centres of excellence?", while a press release *about* one centre reads like
prose and wins. Measured on the 86-question benchmark: nine questions retrieved
none of their authoritative sources, and the hub page was not even in the top-40
candidate pull.

Neither ranking nor the lexical leg can repair that. Authority reranking only
reorders candidates that were retrieved. The lexical leg filters by ``chunk_text``
and then ranks *within* the match by dense similarity, so a page that matches the
words and reads badly is matched and then buried; and its terms are OR-ed, so
adding the organisation's own name (present in nearly every question and 23% of
this corpus's chunks) dilutes the match back to the corpus.

What survives all of that is the title. A question that wants a canonical page
usually names it — "centres of excellence", "annual reports", "contact",
"mission", "training programmes" — and the title is short, curated and stored in
MySQL where it can be matched by word rather than by embedding. So this leg
resolves *documents* by title overlap in the catalogue, then pulls their chunks
from Qdrant by id and hands them back as one more ranking for RRF to fuse.

Deliberate limits
-----------------
* Read-only against both stores, and no new index: it uses ``documents.title``
  and the existing id-scoped Qdrant search.
* Adds a ranking, never replaces one. RRF decides what it is worth, so a
  question whose title match is coincidental loses to the dense pull as usual.
* Requires two-word overlap, or one word that is both long and rare across the
  title catalogue, so "What is TERI?" does not match every page whose title
  contains "TERI" and "research" does not match every page about research.
* A word that names a *kind* of document ("articles", "reports", "events",
  "projects") never carries a match alone, however rare it is in the titles:
  it says what is wanted, not which page. Two-word agreement still resolves
  the pages such words are part of ("annual reports", "articles publications").
* Website nodes only. An attachment inherits its parent's title, so matching
  titles across attachments would return the same document many times over.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Sequence

logger = logging.getLogger(__name__)

# Question scaffolding that must never drive a title match. Broader than the
# lexical leg's list because a title is short: one stray common word is a much
# larger share of the signal here.
_STOP = frozenset(
    """
    a an and are as at be been by can could did do does for from had has have how
    in into is it its me my of on or say says should show that the their there
    these this those to us was were what when where which who whom whose why will
    with would you your about after before between during over under more most
    tell give find list any some all please kindly need want know like get see
    available offer offers offered provide provides provided conduct conducts
    down mention mentions mentioned mentioning
    """.split()
)

# A word long enough to mean something on its own in a title. "SDG", "LCA" and
# "air" are shorter than this and are handled by the acronym path below.
_DISTINCTIVE_MIN_LEN = 6

_WORD = re.compile(r"[a-z][a-z-]{2,}")
# How long the title table is reused. Titles change only when the CMS does, and
# the leg must not pay a 12k-row scan per question.
_TITLE_TTL_SECONDS = 300.0
_titles_cache: list[tuple[str, str, str | None]] | None = None
_titles_loaded_at = 0.0
_ACRONYM = re.compile(r"\b[A-Z]{2,}\b")

# Titles this long are prose, not names; matching two words inside one is noise.
_MAX_TITLE_WORDS = 14


def _terms(question: str) -> list[str]:
    words = [w for w in _WORD.findall((question or "").lower()) if w not in _STOP]
    # Keep acronyms in their own case so "LCA" is not lost to the lowercase pass.
    acronyms = [a.lower() for a in _ACRONYM.findall(question or "") if a.lower() not in _STOP]
    seen, out = set(), []
    for w in words + acronyms:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _title_words(title: str) -> list[str]:
    """The title's words, lowercased, punctuation stripped.

    Word-level rather than substring: matching "vision" inside "Visionary" put
    "Mr. Darbari Seth: The Visionary Founder of TERI" above "Mission and Goals"
    for a question about the mission and vision.
    """
    return _WORD.findall((title or "").lower())


def _score(title: str, terms: Sequence[str], rare: Sequence[str] | None = None) -> int:
    """Matched-term count, or 0 when the match is too weak to act on.

    ``rare`` is the subset of ``terms`` that occurs in few enough titles to name
    a page on its own; a one-word match from outside it does not count. Passing
    ``None`` treats every term as rare, which is only useful in isolation.
    """
    words = _title_words(title)
    if not words:
        return 0
    if len(words) > _MAX_TITLE_WORDS:
        return 0
    present = set(words)
    # Singular/plural is the one inflection worth crossing: pages are named
    # "Centres of Excellence" and asked about as "centres", but also the reverse
    # ("Annual Reports" vs "annual report").
    hits = [
        t for t in terms
        if t in present or f"{t}s" in present or (t.endswith("s") and t[:-1] in present)
    ]
    if not hits:
        return 0
    if len(hits) >= 2:
        return len(hits)
    # A single hit counts only when the word is long enough to mean something on
    # its own, is a real share of a short title, and is rare enough across the
    # catalogue to identify one page rather than a genre.
    only = hits[0]
    if len(only) < _DISTINCTIVE_MIN_LEN or len(words) > 6:
        return 0
    if rare is not None and only not in rare:
        return 0
    return 1


def title_candidates(question: str, *, limit: int = 12) -> list[str]:
    """Website document ids whose titles overlap the question, best first."""
    terms = _terms(question)
    if len(terms) < 2:
        return []
    try:
        from app.catalog import state
    except Exception:  # pragma: no cover - defence in depth
        return []
    global _titles_cache, _titles_loaded_at
    try:
        if _titles_cache is None or time.monotonic() - _titles_loaded_at > _TITLE_TTL_SECONDS:
            _titles_cache = state.website_titles()
            _titles_loaded_at = time.monotonic()
        rows = _titles_cache
    except Exception:
        logger.warning("Title lookup failed; skipping the title leg.", exc_info=True)
        return []
    counts = _title_frequencies(rows)
    selective = _selective_terms(terms, rows, counts=counts)
    if len(selective) < 1:
        return []
    rare = _rare_terms(selective, rows, counts=counts)
    from app.retrieval.search.reranker import derived_authority

    scored: list[tuple[int, int, str]] = []
    for document_id, title, bundle in rows:
        score = _score(title, selective, rare)
        if not score:
            continue
        # Canonical bundles first at equal score: the point of this leg is the
        # page the organisation maintains, not a news item that shares its words.
        authority = derived_authority({"source_type": "website", "bundle": bundle})
        scored.append((-score, -int(authority * 100), document_id))
    scored.sort()
    return [document_id for _, _, document_id in scored[:limit]]


# A term appearing in more than this share of titles cannot distinguish one page
# from another and is dropped before scoring.
_MAX_TITLE_SHARE = 0.10

# The much tighter bar a term must clear to carry a *one-word* match on its own.
# Measured: "research" is in 1.5% of this catalogue's titles and "training" in
# 1.7%, and each names a genre rather than a page — "research" alone matched the
# grab-bag page "Our Research Focus" for a climate-finance question and pushed
# the answer onto an unrelated spring census. The canonical pages this leg exists
# for are all far rarer: "contact" 0.02%, "reports" 0.12%, "mission" 0.58%,
# "excellence" 0.74%. Two-word matches are unaffected; agreement between two
# terms is evidence a single common word cannot supply.
_MAX_SINGLE_HIT_SHARE = 0.01

# Never treat a term as ubiquitous on the strength of fewer titles than this.
_MIN_DF_CEILING = 25


def _title_frequencies(rows: Sequence[tuple[str, str, str | None]]) -> dict[str, int]:
    """How many titles each word appears in. Computed once per query."""
    counts: dict[str, int] = {}
    for _, title, _bundle in rows:
        for word in set(_title_words(title)):
            counts[word] = counts.get(word, 0) + 1
    return counts


def _df(counts: dict[str, int], term: str) -> int:
    """Document frequency of a term, counting its plural as the same word."""
    return max(counts.get(term, 0), counts.get(f"{term}s", 0))


# Words that name a kind of document rather than a page, loaded once from the
# content-type registry on first use. None until then; a failed load is not
# cached, so a transient import problem self-heals on the next question.
_kind_words_cache: frozenset[str] | None = None


def _kind_words() -> frozenset[str]:
    """Every word that names a content type users ask about by name.

    The bundle names and display labels of each type the glossary describes
    (`catalog_prompt.BUNDLE_MEANINGS`), plus the collective words
    ("publications", "documents"), through the same `topic.bundle_words` the
    structured layer uses to account for them — so a type added to the
    registry is covered without this module knowing it, and the two layers
    cannot disagree about what a type word is.

    Imported lazily for the same reason `title_candidates` loads the
    catalogue lazily: the structured package brings its clients with it, and
    this leg wants only its vocabulary. Fails open to the empty set — today's
    rule, frequency alone — rather than blocking the leg.
    """
    global _kind_words_cache
    if _kind_words_cache is not None:
        return _kind_words_cache
    try:
        from app.retrieval.structured.topic import bundle_words
        from app.retrieval.understanding.catalog_prompt import BUNDLE_MEANINGS

        words = set(bundle_words(None))
        for bundle, _meaning in BUNDLE_MEANINGS:
            words |= bundle_words(bundle)
    except Exception:
        logger.warning(
            "Content-type vocabulary unavailable; a kind word may name a page.",
            exc_info=True,
        )
        return frozenset()
    _kind_words_cache = frozenset(words)
    return _kind_words_cache


def _names_a_kind(term: str) -> bool:
    """Whether a query term names a kind of document, in either number."""
    kinds = _kind_words()
    if not kinds:
        return False
    return (
        term in kinds
        or f"{term}s" in kinds
        or (term.endswith("s") and term[:-1] in kinds)
    )


def _rare_terms(
    terms: Sequence[str], rows: Sequence[tuple[str, str, str | None]],
    *, counts: dict[str, int] | None = None,
) -> list[str]:
    """The terms rare enough in the catalogue to name a page by themselves.

    A word that names a *kind* of document never does, however rare it is in
    the titles. "articles" is in one title of 8,631 — the "Articles &
    Publications" index — so by frequency alone it identified that page
    exactly, and "list the articles where IPCC is mentioned" retrieved the
    index's link labels in place of anything about IPCC. The user was not
    naming the page; they were saying what kind of thing they wanted. The
    same held for "reports" (37 short titles carry "Report"), "projects" (39)
    and "events" (8). Two-word agreement is unaffected, so "annual reports"
    and "articles publications" still resolve their pages.
    """
    named = [t for t in terms if not _names_a_kind(t)]
    if not rows:
        return named
    counts = _title_frequencies(rows) if counts is None else counts
    ceiling = max(_MIN_DF_CEILING, int(len(rows) * _MAX_SINGLE_HIT_SHARE))
    return [t for t in named if _df(counts, t) <= ceiling]


def _selective_terms(
    terms: Sequence[str], rows: Sequence[tuple[str, str, str | None]],
    *, counts: dict[str, int] | None = None,
) -> list[str]:
    """The query terms that actually narrow the title set.

    Plain document frequency over the titles in hand. The organisation's own name
    is in a large share of its page titles, so without this it contributes a hit
    to almost every candidate and the ranking becomes "which title mentions the
    organisation most", which is no ranking at all — measured, it put a biofuel
    conference above "Mission and Goals" for a question about the mission.

    Computed rather than configured so it needs no per-corpus list: whatever word
    is ubiquitous in *this* catalogue is the word that gets dropped.
    """
    if not rows:
        return list(terms)
    counts = _title_frequencies(rows) if counts is None else counts
    # Floor the ceiling so the rule cannot misfire on a small catalogue: at 8
    # titles a 10% share rounds to 0 and *every* term looks ubiquitous, which
    # empties the query rather than sharpening it. Below this many titles there is
    # no meaningful frequency signal and nothing should be dropped.
    ceiling = max(_MIN_DF_CEILING, int(len(rows) * _MAX_TITLE_SHARE))
    kept = [t for t in terms if _df(counts, t) <= ceiling]
    # Never strip the query down to nothing: if every term is ubiquitous the
    # question does not name a page and the leg should simply not fire, which the
    # caller's length check then handles.
    return kept


def title_search(
    question: str, query_vector: Sequence[float], *, limit: int
) -> list[Any]:
    """Chunks of the documents this question names by title. ``[]`` when none."""
    ids = title_candidates(question)
    if not ids:
        return []
    from app.retrieval.search.scoped_retrieval import search_within_documents

    try:
        hits = search_within_documents(
            query_vector, ids, limit=limit, trace_stage="title_leg"
        )
    except Exception:
        logger.warning("Title-scoped search failed.", exc_info=True)
        return []
    if hits:
        logger.debug("Title leg matched %d document(s), %d chunk(s).", len(ids), len(hits))
    return hits
