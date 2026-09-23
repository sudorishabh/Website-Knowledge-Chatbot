"""Which web results the corpus already holds — and which are gaps in it.

Internal first, even after the web has been searched. A result whose address
the catalog already holds is answered from its *ingested* chunks, via the same
id-scoped search the rest of retrieval uses: those chunks were dated by the
ingestion rules, their PDFs OCR'd and their tables extracted, and a page read
again at query time could only be a worse copy of them. Only what the corpus
does not hold is fetched.

What web search finds on the organisation's own site that the corpus does not
hold is recorded as a gap — an expert profile ingestion never crawls, a report
published since the last sweep, a PDF linked from a page outside the ingested
bundles. Recorded, never ingested: the corpus is changed only by ingestion. The
gap reaches the application log, the retrieval trace, the ``web_corpus_gap``
metric, and — when ``web_gap_log_path`` is set — a JSONL file to review.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.catalog.queries import indexed_documents_for_urls
from app.config import get_settings
from app.observability import retrieval_log
from app.observability.metrics import record_event
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.search.scoped_retrieval import search_within_documents
from app.retrieval.web.providers import SearchHit
from app.retrieval.web.safety import policy

logger = logging.getLogger(__name__)

__all__ = ["CorpusSplit", "internal_candidates", "record_gaps", "split_by_corpus"]

# Chunks read per matched document; the reranker decides which are used.
_CHUNKS_PER_DOCUMENT = 3
_MAX_MATCH_CHUNKS = 24


@dataclass
class CorpusSplit:
    """Search hits divided by whether the corpus already holds them.

    ``known`` is False when the catalog could not be read: every hit is then in
    ``missing`` (and will be fetched), but none may be reported as a gap.
    """

    ingested: dict[str, list[str]] = field(default_factory=dict)   # hit url -> document ids
    hits: dict[str, SearchHit] = field(default_factory=dict)       # hit url -> hit
    missing: list[SearchHit] = field(default_factory=list)
    known: bool = True


def split_by_corpus(hits: Sequence[SearchHit]) -> CorpusSplit:
    """Divide ``hits`` into those the corpus holds and those it does not."""
    if not hits:
        return CorpusSplit()
    found = indexed_documents_for_urls([h.url for h in hits])
    if found is None:
        return CorpusSplit(missing=list(hits), known=False)
    split = CorpusSplit()
    for hit in hits:
        if found.get(hit.url):
            split.ingested[hit.url] = found[hit.url]
            split.hits[hit.url] = hit
        else:
            split.missing.append(hit)
    return split


def internal_candidates(split: CorpusSplit, query_vector: Sequence[float]) -> list[Candidate]:
    """The ingested chunks of the documents web search pointed at.

    Ordinary corpus candidates — the payload the corpus wrote, dates and pages
    included — with one addition to their provenance: ``retrieval_method``
    records that web search is how they were reached, and ``search_rank`` where
    it ranked them.
    """
    if not split.ingested:
        return []
    owner: dict[str, SearchHit] = {}
    for url, ids in split.ingested.items():
        for document_id in ids:
            owner.setdefault(document_id, split.hits[url])
    limit = min(_MAX_MATCH_CHUNKS, _CHUNKS_PER_DOCUMENT * len(owner))
    found = search_within_documents(query_vector, list(owner), limit=limit,
                                    trace_stage="web_corpus_match")
    out: list[Candidate] = []
    for candidate in found:
        hit = owner.get(str(candidate.payload.get("document_id")))
        if hit is None:
            out.append(candidate)
            continue
        out.append(replace(candidate, payload={
            **candidate.payload,
            "retrieval_method": f"corpus_via_web_search:{hit.provider}",
            "search_query": hit.query,
            "search_rank": hit.rank,
        }))
    return out


def _gap_record(hit: SearchHit, question: str) -> dict[str, Any]:
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": hit.url,
        "title": hit.title,
        "published": hit.published,
        "provider": hit.provider,
        "query": hit.query,
        "question": question,
    }


def _append(path: str, records: list[dict[str, Any]]) -> None:
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("Could not append to the web gap log.", exc_info=True)


def record_gaps(split: CorpusSplit, *, question: str) -> list[dict[str, Any]]:
    """Record the organisation's own pages web search found and the corpus does
    not hold. Third-party pages are not gaps — the corpus is the organisation's
    own content by design — and nothing is recorded when the catalog could not
    be read, since every hit would then look missing."""
    if not split.known:
        return []
    domain_policy = policy()
    records = [_gap_record(hit, question) for hit in split.missing
               if domain_policy.is_primary(hit.url)]
    if not records:
        return []
    for record in records:
        kind = "pdf" if record["url"].lower().split("?")[0].endswith(".pdf") else "page"
        record_event("web_corpus_gap", kind)
        logger.info("web corpus gap: %s", json.dumps(record, ensure_ascii=False))
    retrieval_log.note(web_corpus_gaps=[r["url"] for r in records])
    path = str(getattr(get_settings(), "web_gap_log_path", "") or "")
    if path:
        _append(path, records)
    return records
