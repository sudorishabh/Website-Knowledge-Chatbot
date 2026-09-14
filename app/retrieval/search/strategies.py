"""Recall-expansion retrieval strategies.

Each function is one optional pull layered on top of the base dense search, fused
back in by ``retriever.retrieve``. They call the shared LLM / embedding gateways
in :mod:`app.core.clients` directly — retrieval never depends on the generation
package for these.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Sequence

from app.config import get_settings

from app.core.clients.embeddings import embed_query
from app.core.clients.llm import get_llm, get_structured_llm
from app.retrieval.search.fusion import rrf
from app.retrieval.search.hybrid_search import search
from app.retrieval.search.reranker import rerank

logger = logging.getLogger(__name__)


def dual_search(
    search_query: str,
    *,
    filters: list[Any] | None,
    query_vector: list[float] | None,
    settings: Any,
) -> list[Any]:
    """Two pulls sharing one query vector: website (source_type == website) and
    "not website". Preserves any non-source filters (language / date) on both.
    Their union guarantees the website's best chunks are fetched even though PDFs
    dominate the corpus (see docs/website-preference-retrieval.md)."""
    from qdrant_client.models import FieldCondition, MatchValue

    base = list(filters or [])
    website_cond = FieldCondition(key="source_type", match=MatchValue(value="website"))
    website = search(
        search_query,
        limit=settings.website_candidate_k,
        extra_filter=base + [website_cond],
        query_vector=query_vector,
        trace_stage="website_pull",
    )
    others = search(
        search_query,
        limit=settings.retrieval_candidate_k,
        extra_filter=base or None,
        extra_must_not=[website_cond],
        query_vector=query_vector,
        trace_stage="not_website_pull",
    )
    return website + others


_PERSPECTIVE_SYSTEM = (
    "A search over a document corpus is about to run for the user's query. "
    "Propose alternative queries that would surface passages the original "
    "wording would miss.\n"
    "Each one must look at a DIFFERENT ASPECT of the subject — a different kind "
    "of thing that would be written about it — not the same question in "
    "different words. Ask what *sorts of documents* would answer the query, and "
    "name one of those per line.\n"
    "Keep the subject exactly as the user named it. Add no facts, no entities, "
    "no constraints and no dates the query did not contain.\n"
    "Example. Query: 'What issues affected Project X?'\n"
    "  GOOD - different aspects: 'Project X incidents', 'Project X delays and "
    "blockers', 'Project X quality problems', 'Project X unresolved risks'\n"
    "  BAD - the same question reworded: 'What problems affected Project X?', "
    "'What issues did Project X have?', 'Which problems were associated with "
    "Project X?'\n"
    "Return only the alternative queries."
)

# Content words, for the lexical half of the distinctness guard.
_PERSPECTIVE_WORD = re.compile(r"[a-z][a-z'-]{2,}")


@dataclass(frozen=True)
class Perspective:
    """One retrieval perspective, and the vector its pull will use.

    The vector is carried rather than recomputed: the distinctness guard has to
    embed a perspective to judge it, and the pull would otherwise embed the same
    string again — a second network round trip per accepted leg for a value
    already in hand. ``None`` when embedding was unavailable, in which case the
    pull embeds as it always did.
    """

    text: str
    vector: list[float] | None = None


def _content_words(text: str) -> set[str]:
    """The query's own vocabulary, minus the scaffolding of a question."""
    return {
        w for w in _PERSPECTIVE_WORD.findall((text or "").lower())
        if w not in _STOPWORDS
    }


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


def _is_rewording(candidate: str, original: str) -> bool:
    """Whether ``candidate`` only rearranges words the original already had.

    The free half of the guard, needing no embedding: a query whose content
    words are all present in the original contributes nothing new to the lexical
    legs and very little to the dense one. "What issues did Project X have?" is
    exactly this; "Project X delays and blockers" is not.
    """
    words = _content_words(candidate)
    return not words or words <= _content_words(original)


def _distinct(
    search_query: str,
    candidates: Sequence[Any],
    *,
    n: int,
    query_vector: Sequence[float] | None,
) -> list[Perspective]:
    """The candidates worth their own retrieval leg, in order, capped at ``n``.

    An embedding failure degrades to the lexical guard alone rather than
    dropping the perspective: a missing vector is a missing *check*, not
    evidence of duplication.
    """
    threshold = float(getattr(get_settings(), "multi_query_distinct_threshold", 0.92))
    seen: set[str] = {(search_query or "").strip().lower()}
    kept: list[Perspective] = []
    for candidate in candidates:
        text = str(candidate or "").strip()
        key = text.lower()
        if not text or key in seen or _is_rewording(text, search_query):
            continue
        vector: list[float] | None = None
        if query_vector is not None:
            try:
                vector = embed_query(text)
            except Exception:
                logger.debug("Could not embed perspective %r.", text, exc_info=True)
            if vector is not None:
                # Too close to the question itself, or to an angle already
                # taken: either way this leg would re-pull a neighbourhood the
                # ranking already covers.
                if _cosine(vector, query_vector) >= threshold:
                    continue
                if any(
                    p.vector is not None and _cosine(vector, p.vector) >= threshold
                    for p in kept
                ):
                    continue
        seen.add(key)
        kept.append(Perspective(text=text, vector=vector))
        if len(kept) >= n:
            break
    return kept


def perspectives(
    search_query: str, n: int, *, query_vector: Sequence[float] | None = None
) -> list[Perspective]:
    """Up to ``n`` genuinely different angles on the query; ``[]`` on failure.

    Replaces the paraphrase generator this used to be. Paraphrasing was the
    wrong instrument: re-asking one question in three ways retrieves three
    near-identical neighbourhoods, so the extra pulls cost latency and return
    what the base pull already had. What raises recall is asking about a
    *different aspect* of the same subject — the incidents, the delays, the
    complaints — because those get written up in different documents.

    This uses the multi-query LLM call that was already being made. It asks for
    something else; it adds no call of its own.

    Returning ``[]`` is a success, not a failure. The base query is always a leg
    in its own right (``retriever.retrieve`` fuses ``[base] + rankings``), so
    falling back to it is exactly the retrieval this query would have had — which
    is the right outcome when every generated angle turned out to be the question
    again in other words.
    """
    from pydantic import BaseModel

    class _Generated(BaseModel):
        queries: list[str] = []

    try:
        # Diversity is the point here, so temperature ~0.7 (not the pinned
        # parsing temperature).
        model = get_llm(temperature=0.7).with_structured_output(_Generated)
        result: _Generated = model.invoke(
            [
                ("system", _PERSPECTIVE_SYSTEM),
                ("human", f"Give {n} alternative queries for: {search_query}"),
            ]
        )
        # Tolerant of a malformed structured response: a missing or non-list
        # `queries` reads as "nothing generated", which is the base-query path.
        raw = getattr(result, "queries", None)
        raw = list(raw) if isinstance(raw, (list, tuple)) else []
    except Exception:
        logger.warning("Perspective generation failed; base query only.", exc_info=True)
        return []

    kept = _distinct(search_query, raw, n=n, query_vector=query_vector)
    if raw and not kept:
        logger.info("All %d generated perspective(s) restated the query; "
                    "retrieving with the base query alone.", len(raw))
    return kept


def _leg_search(
    query: str,
    *,
    limit: int,
    trace_stage: str,
    query_vector: Sequence[float] | None = None,
) -> list[Any]:
    """One derived query's dense pull (cached embed); [] on failure.

    Shared by every leg that searches a *rewritten* query rather than the user's
    own: the paraphrases below, and the sub-queries of a decomposed question.
    They differ only in what the trace should call them, and a leg that fails
    must cost its own ranking and nothing else."""
    try:
        return search(
            query, limit=limit, trace_stage=trace_stage,
            query_vector=(list(query_vector) if query_vector is not None
                          else embed_query(query)),
        )
    except Exception:
        logger.warning("%s failed for %r.", trace_stage, query, exc_info=True)
        return []


def perspective_search(
    query: str, *, limit: int, query_vector: Sequence[float] | None = None
) -> list[Any]:
    """One perspective's dense pull; [] on failure.

    The trace stage stays ``multi_query_leg``: that name is the stable label in
    the retrieval trace and the metrics, and what Phase D changed is what the
    leg searches *for*, not that there is a leg."""
    return _leg_search(query, limit=limit, trace_stage="multi_query_leg",
                       query_vector=query_vector)


def subquery_search(query: str, *, limit: int) -> list[Any]:
    """One sub-query's dense pull (cached embed); [] on failure.

    The retrieval half of question decomposition: the ranking this returns is
    fused with the base pull by the same RRF every other leg goes through, which
    is also what de-duplicates a passage several parts of the question reach."""
    return _leg_search(query, limit=limit, trace_stage="subquery_leg")


_CORRECTIVE_SYSTEM = (
    "The retrieved passages may not answer the question. Suggest ONE "
    "reformulated search query targeting the missing information: change the "
    "wording or angle toward what the passages lack; keep the original "
    "meaning; do not add facts.\n"
    "Example: question 'what did the report say about coastal erosion "
    "funding' with passages about erosion science but nothing on budgets -> "
    "'budget allocation for coastal erosion protection programmes'."
)


def corrective_query(search_query: str, ranked: list[Any]) -> str | None:
    """One structured reformulation aimed at what the weak results missed;
    None when it fails or merely echoes the original."""
    from pydantic import BaseModel

    class Reformulation(BaseModel):
        query: str = ""

    snippets = "\n".join(f"- {c.text[:200]}" for c in ranked[:3] if c.text)
    try:
        result: Reformulation = (
            get_structured_llm()
            .with_structured_output(Reformulation)
            .invoke(
                [
                    ("system", _CORRECTIVE_SYSTEM),
                    ("human", f"Question: {search_query}\n\n"
                              f"Best passages so far:\n{snippets or '(none)'}"),
                ]
            )
        )
    except Exception:
        logger.warning("Corrective reformulation failed.", exc_info=True)
        return None
    query = (result.query or "").strip()
    if not query or query.lower() == search_query.lower():
        return None
    return query


def corrective_requery(
    search_query: str,
    ranked: list[Any],
    *,
    filters: list[Any] | None,
    limit: int,
    table_boost: float,
    temporal: Any | None = None,
) -> list[Any]:
    """One-shot corrective retrieval: reformulate, search once, RRF-fuse with
    the current ranking, rerank once more. Strictly one iteration; any failure
    or empty gain keeps the original ranking.

    ``temporal`` is passed straight through to the rerank so the corrective pass
    orders on the same keys as the pass it replaces; None reproduces the
    previous behaviour."""
    try:
        reformulated = corrective_query(search_query, ranked)
        if not reformulated:
            return ranked

        extra = search(
            reformulated, limit=limit, extra_filter=filters or None,
            query_vector=embed_query(reformulated),
            trace_stage="corrective_pull",
        )
        seen = {c.id for c in ranked}
        if not any(c.id not in seen for c in extra):
            return ranked
        return rerank(search_query, rrf([ranked, extra]),
                      table_boost=table_boost, temporal=temporal)
    except Exception:
        logger.warning("Corrective requery failed; keeping original ranking.",
                       exc_info=True)
        return ranked


# Salient-term patterns for the keyword leg: the query features dense vectors
# handle worst — exact phrases, acronyms, alphanumeric codes, years, proper nouns.
_QUOTED = re.compile(r"\"([^\"]{2,})\"|'([^']{2,})'")
_CAP_BIGRAM = re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)+)\b")
# An acronym, optionally carrying the number that qualifies it: SDG 7, COP26.
# The number is part of the term because "SDG" alone matches every one of them.
_ACRONYM = re.compile(r"\b([A-Z]{2,}(?:[ -]?\d+)?)\b")
# A token mixing letters and digits — PM2.5, CO2, 1.5C. Dense embeddings blur
# these into their nearest word; they are exact by nature.
_ALNUM = re.compile(r"\b([A-Za-z]+\d+(?:\.\d+)?[A-Za-z]*)\b")
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")

# Words that carry no retrieval signal. Small and deliberately conservative:
# this list only has to strip the scaffolding of a question, since the content
# fallback below is a last resort rather than the primary source of terms.
_STOPWORDS = frozenset(
    """
    a an and are as at be been by can could did do does for from had has have how
    in into is it its me my of on or say says should show that the their there
    these this those to us was were what when where which who whom whose why will
    with would you your about after before between during over under more most
    tell give find list
    """.split()
)
# Lowercase content words, used only when nothing more precise was found. Three
# characters is the floor rather than four: "air" and "gas" name whole domains
# in this corpus, and dropping them lost the point of the phrase.
_CONTENT = re.compile(r"\b([a-z][a-z-]{2,})\b")


def extract_key_terms(query: str) -> list[str] | None:
    """Deterministic salient terms for the keyword pull; None when the query has
    none at all.

    Ordered most to least precise. The lowercase content-word pass is a
    *fallback*, reached only when no quoted phrase, proper noun, acronym, code
    or year was found — because a query like "life cycle analysis of transport
    modes" names something exactly without capitalising any of it, and skipping
    the leg entirely there was the single biggest hole in the lexical path.
    Running it alongside the precise patterns instead would drown them in
    ordinary vocabulary.
    """
    terms: list[str] = [m.group(1) or m.group(2) for m in _QUOTED.finditer(query)]
    terms.extend(_CAP_BIGRAM.findall(query))
    terms.extend(_ALNUM.findall(query))
    terms.extend(_ACRONYM.findall(query))
    terms.extend(_YEAR.findall(query))
    if not terms:
        terms = [
            word for word in _CONTENT.findall(query.lower())
            if word not in _STOPWORDS
        ]
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = term.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            unique.append(cleaned)
    # Drop any term that is merely the opening of another one already kept. The
    # patterns overlap by design — "PM2.5" is also read as the acronym "PM2",
    # and "SDG 7" as the acronym "SDG" — and the shorter form is always the
    # less selective of the two.
    return [
        term
        for term in unique
        if not any(
            other != term and other.lower().startswith(term.lower())
            for other in unique
        )
    ] or None


def extract_content_terms(query: str) -> list[str] | None:
    """Lowercase content words of a query, unconditionally.

    The companion to :func:`extract_key_terms`, and the answer to a hole that
    function has on any corpus with one ubiquitous name. Its content-word pass is
    a *fallback*, skipped whenever a precise pattern matched — and the
    organisation's own acronym matches almost every question asked of it. So
    "What are TERI's flagship initiatives and centres of excellence?" yielded the
    single term ``['TERI']``, which under the OR semantics of
    :func:`keyword_search` matches nearly the whole corpus and leaves the lexical
    leg contributing nothing the dense pull did not already have. Measured: every
    organisational question in the 86-question benchmark extracted exactly
    ``['TERI']``.

    Diluting the precise list by merging content words into it would be the wrong
    repair — OR-ing a ubiquitous term with a selective one keeps the ubiquitous
    match. Instead this is pulled as its *own* ranking and fused, so the topical
    words ("initiatives", "centres", "excellence") select a small, on-subject set
    that RRF can promote out of, while the precise pull keeps doing its own job.

    Returns None when the query has no content words, so callers can skip the leg.
    """
    words = [
        word for word in _CONTENT.findall((query or "").lower())
        if word not in _STOPWORDS
    ]
    unique: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word not in seen:
            seen.add(word)
            unique.append(word)
    return unique or None


def keyword_search(
    search_query: str,
    terms: list[str],
    *,
    filters: list[Any] | None,
    query_vector: list[float],
    limit: int,
    trace_stage: str = "keyword_leg",
) -> list[Any]:
    """One MatchText-filtered pull (dense ranking within keyword matches).

    The terms are OR-ed, one ``MatchText`` each. A single ``MatchText`` carrying
    several words is an AND across all of them, which made the leg brittle in
    exactly the case it exists for: "Emission Inventorisation for Faridabad
    Town" returned nothing, because no one chunk held all four words. OR-ing
    degrades toward the dense pull instead of collapsing to zero, and the
    ranking within the matched set is still dense similarity.

    Fails open to [] — notably while the chunk_text full-text index doesn't
    exist yet (scripts/create_fulltext_index.py).
    """
    if not terms:
        return []
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchText

        cond = Filter(
            should=[
                FieldCondition(key="chunk_text", match=MatchText(text=term))
                for term in terms
            ]
        )
        return search(
            search_query, limit=limit,
            extra_filter=list(filters or []) + [cond], query_vector=query_vector,
            trace_stage=trace_stage,
        )
    except Exception:
        logger.debug("Keyword leg unavailable; dense-only.", exc_info=True)
        return []
