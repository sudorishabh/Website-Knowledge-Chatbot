"""Web retrieval for one question, start to finish: search, sort, fetch, read.

:func:`gather` is the package's one entry point. Given a :class:`WebPlan` and the
question's embedding, it returns a :class:`WebOutcome` — the candidates to rank
beside the corpus's, and a record of everything done to get them:

1. **Search** the plan's queries: the organisation's own site first, side by
   side with the open web or, for a question about the organisation, the open
   web only when its own site came back short.
2. **Sort** the results against the corpus (:mod:`.corpus`). What the corpus
   already holds is read from its ingested chunks; the organisation's pages it
   lacks are recorded as gaps.
3. **Fetch** what remains — the organisation's pages first, at most
   ``web_max_fetches`` — in parallel, each page read from the page cache when it
   has been read before.
4. **Read** each fetched document into passages scored on the corpus's own
   relevance scale (:mod:`.passages`).

Everything is bounded by ``web_budget_seconds``, and everything fails open: a
provider outage, a slow site, an unreadable PDF, an embedding failure each cost
the evidence they would have produced and nothing else. The worst case is an
empty outcome, which leaves the question answered from the corpus alone.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import get_settings
from app.observability import retrieval_log
from app.observability.tracing import span
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.web import corpus, passages, providers
from app.retrieval.web.cache import page_cache
from app.retrieval.web.extract import WebDocument, extract
from app.retrieval.web.fetch import FetchRefused, fetch
from app.retrieval.web.planner import WebPlan, WebQuery
from app.retrieval.web.providers import SearchHit
from app.retrieval.web.safety import policy, url_key

logger = logging.getLogger(__name__)

__all__ = ["WebOutcome", "gather"]

# How many distinct results from the organisation's own site make a fallback to
# the open web unnecessary for a question about the organisation.
_ENOUGH_PRIMARY_HITS = 2
# Why a search result was not read, beyond the fetcher's own reasons.
NOT_READ_BUDGET = "budget_exhausted"
NOT_READ_LIMIT = "beyond_fetch_limit"
NOT_READ_EXTRACTION = "unreadable"
NOT_READ_DUPLICATE = "duplicate_document"


@dataclass
class WebOutcome:
    """What web retrieval produced for one question, and how.

    ``candidates`` are ready for ranking: web passages carrying ``source_type
    web`` and corpus chunks reached through web search. The rest is the record
    the trace keeps — which queries ran and what came back, which pages were
    read, which were not and why, which were already in the corpus, which are
    gaps in it.
    """

    candidates: list[Candidate] = field(default_factory=list)
    provider: str | None = None
    queries: list[dict[str, Any]] = field(default_factory=list)
    hits: int = 0
    in_corpus: dict[str, list[str]] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)
    not_read: dict[str, str] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    web_passages: int = 0
    corpus_passages: int = 0
    budget_exhausted: bool = False
    latency_ms: float = 0.0
    skipped: str | None = None

    def to_trace(self) -> dict[str, Any]:
        record = {
            "provider": self.provider, "skipped": self.skipped,
            "queries": self.queries, "hits": self.hits,
            "in_corpus": self.in_corpus, "fetched": self.fetched,
            "not_read": self.not_read, "gaps": self.gaps,
            "web_passages": self.web_passages, "corpus_passages": self.corpus_passages,
            "budget_exhausted": self.budget_exhausted,
            "latency_ms": round(self.latency_ms, 1),
        }
        return {k: v for k, v in record.items() if v not in (None, [], {}, 0, False)}


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + max(0.0, seconds)

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())


# --- 1. search ------------------------------------------------------------------


def _run_queries(queries: Sequence[WebQuery], outcome: WebOutcome) -> list[SearchHit]:
    """Every query at once; results in query order, each page once."""
    if not queries:
        return []
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(pool.map(
            retrieval_log.bound(lambda q: providers.search(q.text, site=q.site)), queries,
        ))
    hits: list[SearchHit] = []
    for query, found in zip(queries, results):
        outcome.queries.append({"text": query.text, "site": query.site,
                                "purpose": query.purpose, "hits": len(found)})
        hits.extend(found)
    return hits


def _unique(hits: Sequence[SearchHit]) -> list[SearchHit]:
    seen: set[str] = set()
    out: list[SearchHit] = []
    for hit in hits:
        key = url_key(hit.url)
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def _search(plan: WebPlan, outcome: WebOutcome) -> list[SearchHit]:
    first = [q for q in plan.queries if not q.fallback]
    fallback = [q for q in plan.queries if q.fallback]
    hits = _unique(_run_queries(first, outcome))
    if fallback and len(hits) < _ENOUGH_PRIMARY_HITS:
        hits = _unique(hits + _run_queries(fallback, outcome))
    return hits


# --- 3. fetch ---------------------------------------------------------------------


def _to_fetch(missing: Sequence[SearchHit], outcome: WebOutcome) -> list[SearchHit]:
    """Which results the corpus lacks are worth reading: the organisation's own
    pages first, then in search order, up to ``web_max_fetches``."""
    settings = get_settings()
    domain_policy = policy()
    ordered = sorted(
        enumerate(missing),
        key=lambda item: (not domain_policy.is_primary(item[1].url), item[0]),
    )
    limit = max(0, int(settings.web_max_fetches))
    chosen: list[SearchHit] = []
    for _, hit in ordered:
        if len(chosen) < limit:
            chosen.append(hit)
        else:
            outcome.not_read[hit.url] = NOT_READ_LIMIT
    return chosen


def _read(hit: SearchHit) -> WebDocument:
    """One result as a document, from the page cache when it has been read.

    Cached by the address searched *and* the one it resolved to, so a result
    reached through a redirect is not fetched again under either name.
    """
    cache = page_cache()
    cached = cache.get(url_key(hit.url))
    if isinstance(cached, dict):
        return WebDocument.from_dict(cached)
    document = extract(fetch(hit.url))
    stored = document.to_dict()
    for address in {hit.url, document.final_url, document.canonical_url}:
        cache.set(url_key(address), stored)
    return document


def _fetch_all(hits: Sequence[SearchHit], deadline: _Deadline, outcome: WebOutcome
               ) -> list[tuple[WebDocument, SearchHit]]:
    if not hits:
        return []
    documents: list[tuple[WebDocument, SearchHit]] = []
    pool = ThreadPoolExecutor(max_workers=min(4, len(hits)))
    futures: dict[Future, SearchHit] = {
        pool.submit(retrieval_log.bound(_read), hit): hit for hit in hits
    }
    pending = set(futures)
    try:
        while pending:
            remaining = deadline.remaining()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                hit = futures[future]
                try:
                    documents.append((future.result(), hit))
                    outcome.fetched.append(hit.url)
                except FetchRefused as exc:
                    outcome.not_read[hit.url] = exc.reason
                except Exception:
                    logger.warning("Could not read %s; skipping it.", hit.url, exc_info=True)
                    outcome.not_read[hit.url] = NOT_READ_EXTRACTION
    finally:
        # Pages still loading are abandoned, not awaited: the answer is built
        # from what arrived, and the threads finish (or time out) on their own.
        for future in pending:
            future.cancel()
            outcome.not_read[futures[future].url] = NOT_READ_BUDGET
        outcome.budget_exhausted = bool(pending)
        pool.shutdown(wait=False, cancel_futures=True)
    # Search order, not completion order; and one copy of a document reached
    # through two results.
    order = {hit.url: i for i, hit in enumerate(hits)}
    documents.sort(key=lambda pair: order[pair[1].url])
    unique: list[tuple[WebDocument, SearchHit]] = []
    seen: set[str] = set()
    for document, hit in documents:
        key = url_key(document.canonical_url)
        if key in seen:
            outcome.not_read[hit.url] = NOT_READ_DUPLICATE
            outcome.fetched.remove(hit.url)
            continue
        seen.add(key)
        unique.append((document, hit))
    return unique


# --- the whole ----------------------------------------------------------------------


def gather(
    plan: WebPlan,
    query_vector: Sequence[float],
    *,
    budget_seconds: float | None = None,
) -> WebOutcome:
    """Web evidence for one planned question. Never raises; see the module
    docstring for what each failure costs."""
    started = time.perf_counter()
    outcome = WebOutcome()
    try:
        _gather(plan, query_vector, outcome, budget_seconds)
    except Exception:  # pragma: no cover - defence in depth
        logger.warning("Web retrieval failed; answering from the corpus.", exc_info=True)
        outcome.candidates = []
        outcome.skipped = outcome.skipped or "error"
    outcome.latency_ms = (time.perf_counter() - started) * 1000.0
    retrieval_log.note(web=outcome.to_trace())
    return outcome


def _gather(plan: WebPlan, query_vector: Sequence[float], outcome: WebOutcome,
            budget_seconds: float | None) -> None:
    settings = get_settings()
    provider = providers.get_provider()
    if provider is None:
        outcome.skipped = "no_provider"
        return
    outcome.provider = provider.name
    deadline = _Deadline(budget_seconds if budget_seconds is not None
                         else float(settings.web_budget_seconds))

    with span("rag.web_search") as s:
        hits = _search(plan, outcome)
        outcome.hits = len(hits)
        s.set("hits", len(hits))
    if not hits:
        return

    split = corpus.split_by_corpus(hits)
    outcome.in_corpus = dict(split.ingested)
    corpus_found = corpus.internal_candidates(split, query_vector)
    outcome.gaps = [g["url"] for g in corpus.record_gaps(split, question=plan.question)]

    with span("rag.web_fetch") as s:
        documents = _fetch_all(_to_fetch(split.missing, outcome), deadline, outcome)
        s.set("fetched", len(documents))
        s.set("budget_exhausted", outcome.budget_exhausted)

    web_found: list[Candidate] = []
    if documents:
        with span("rag.web_passages") as s:
            try:
                web_found = passages.to_candidates(documents, plan.search_query, query_vector)
            except Exception:
                logger.warning("Web passages could not be embedded; using none.",
                               exc_info=True)
                retrieval_log.note_error("web_passages", "embedding failed")
            s.set("passages", len(web_found))

    outcome.corpus_passages = len(corpus_found)
    outcome.web_passages = len(web_found)
    outcome.candidates = corpus_found + web_found
