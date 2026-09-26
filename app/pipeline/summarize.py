"""Scoped summarization: map-reduce over a catalog-selected document set.

"Summarize the Climate theme / 2024 publications" cannot be served by
similarity search — the user defined a SET, not a topic. MySQL selects the
set (newest first, capped) and supplies each document's ingest-time abstract;
Qdrant supplies a lead parent chunk only for documents not yet enriched. The
model then summarizes hierarchically: per-document bullets in parallel batches
(map), then one aggregation call (reduce) — though a scope that fits one call
skips the map stage entirely, which is the usual case once abstracts exist.
Citations are document-level catalog rows. Any failure returns None — the
caller falls through to plain semantic RAG.

This is an orchestration use case: it combines retrieval (catalog scope +
lead-parent fetch) with generation (the LLM summary), so it lives in the
pipeline layer rather than inside either feature package.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Sequence

from pydantic import BaseModel, Field

from app.catalog import queries as catalog
from app.core.dates import parse_iso_date
from app.core.corpus import DEFAULT_BUNDLES
from app.retrieval.search import scoped_retrieval
from app.retrieval.structured.entities import normalize_entity
from app.schemas.query import Citation

if TYPE_CHECKING:
    from app.retrieval.understanding.query_processor import QueryAnalysis

logger = logging.getLogger(__name__)

_SCOPE_DOC_CAP = 30      # v1 cap; plan §13.7 raises it with a two-level reduce
# Total estimated tokens that still fit one grounded call, skipping the map
# stage. Sized rather than counted: a scope of ingest-time abstracts (~250
# tokens each) fits comfortably, while the same number of lead parent chunks
# (~2000 each) does not — which is the point of enriching at ingest.
_DIRECT_TOKEN_BUDGET = 12_000
_MAP_BATCH_TOKENS = 6000
_MAP_WORKERS = 4

_DIRECT_SYSTEM = (
    "You summarize a small collection of documents given as numbered context "
    "blocks. Use ONLY the blocks; cite the block number [n] after every claim "
    "it supports; do not add outside facts. Write one cohesive overview of "
    "the set — its main threads and notable specifics — not a per-document "
    "recap."
)

_MAP_SYSTEM = (
    "You summarize documents for a downstream aggregation step. For EACH "
    "input document, produce 2-3 short factual bullets strictly from its own "
    "text — no outside knowledge, no speculation, numbers copied exactly. "
    "Return one entry per document with its document_id copied verbatim.\n"
    "Example input:\n"
    "document_id: abc-1\ntitle: Rooftop solar in Delhi\ntext: The programme "
    "added 1.2 GW of rooftop capacity in 2023, led by commercial "
    "installations across three districts...\n"
    "Example output entry: document_id='abc-1', bullets=['Rooftop programme "
    "added 1.2 GW of capacity in 2023', 'Commercial installations led "
    "adoption across three districts']"
)

_REDUCE_SYSTEM = (
    "You write a thematic overview of a document collection. The input has "
    "one metadata line per document — '[n] title · date' — followed by that "
    "document's summary bullets. Using ONLY those bullets, write a cohesive "
    "summary of the collection's main threads, citing [n] after each claim "
    "it supports. Mention the period covered when the dates make it clear. "
    "Do not add facts."
)


@dataclass
class _Doc:
    document_id: str
    title: str
    url: str | None
    effective_date: str  # ISO date (YYYY-MM-DD) or ""
    text: str
    # Reporting period the document covers, when known. Editions of a series
    # share a title and a effective_start_date; this is what separates them. Last so the
    # dataclass keeps its non-default fields first.
    edition: str = ""


class DocSummary(BaseModel):
    document_id: str
    bullets: list[str] = Field(default_factory=list, description="2-3 short factual bullets.")


class BatchSummaries(BaseModel):
    summaries: list[DocSummary] = Field(default_factory=list)


def _parse_date(value: str | None, *, field: str = "date") -> datetime | None:
    return parse_iso_date(value, field=field)


def _scope_filters(analysis: QueryAnalysis) -> dict[str, Any] | None:
    """Catalog kwargs for the analysis' scope; None when nothing scopes the
    set — a scope-less "summarize" belongs on the QA path. Unknown bundles are
    dropped rather than zeroing the set (a summary scope is soft, unlike the
    count guard); themes fall back to the theme display name."""
    filters: dict[str, Any] = {}
    if analysis.theme:
        # Canonicalize the name; the SQL layer matches it exactly.
        from app.retrieval.structured.filters import resolve_theme

        filters["theme"] = resolve_theme(analysis.theme) or analysis.theme
    bundle = normalize_entity(analysis.bundle)
    if bundle in DEFAULT_BUNDLES:
        filters["bundle"] = bundle
    if analysis.author:
        filters["author"] = analysis.author
    if analysis.title_contains:
        filters["title_contains"] = analysis.title_contains
    lo = _parse_date(analysis.date_from, field="date_from")
    hi = _parse_date(analysis.date_to, field="date_to")
    if lo is not None:
        filters["effective_from"] = lo
    if hi is not None:
        filters["effective_to"] = hi
    return filters or None


def _doc_from_payload(document_id: str, payload: dict[str, Any]) -> _Doc:
    return _Doc(
        document_id=document_id,
        title=str(payload.get("title") or document_id),
        url=payload.get("source_url"),
        effective_date=_effective_date_label(payload.get("effective_start_date"),
                                   payload.get("start_precision")),
        edition=str(payload.get("edition_label") or ""),
        text=str(payload.get("chunk_text") or ""),
    )


def _effective_date_label(value: Any, precision: Any) -> str:
    """The date as it may be shown: a full date, a month, or a bare year.

    A year- or month-precision value holds 1 January / the 1st as a marker for a
    period the source stated without a day. Truncating to ten characters would
    put that day in front of the model, which would then repeat it as the
    publication date.
    """
    text = str(value or "")
    if not text:
        return ""
    if precision == "year":
        return text[:4]
    if precision == "month":
        return text[:7]
    return text[:10]


def _doc_from_catalog(document_id: str, row: dict[str, Any]) -> _Doc:
    return _Doc(
        document_id=document_id,
        title=str(row.get("title") or document_id),
        url=row.get("url"),
        effective_date=_effective_date_label(row.get("effective_start_date"),
                                   row.get("start_precision")),
        edition=str(row.get("edition_label") or ""),
        text=str(row.get("abstract") or ""),
    )


def _collect_docs(ids: Sequence[str]) -> list[_Doc]:
    """One ``_Doc`` per id, preferring the ingest-time abstract.

    An abstract is built from the whole document; the lead parent chunk is only
    its *first section*, which for a long report is the cover page or table of
    contents. Documents not yet enriched keep the lead-chunk fallback, so this
    degrades to the previous behaviour rather than losing them — and only the
    un-enriched ones cost a Qdrant round-trip.

    Catalog order (newest first) is preserved across both sources, since the
    citation numbers follow it.
    """
    enriched = catalog.abstracts_for(ids)
    missing = [i for i in ids if i not in enriched]
    payloads = scoped_retrieval.lead_parents(missing) if missing else {}
    docs: list[_Doc] = []
    for doc_id in ids:
        if doc_id in enriched:
            docs.append(_doc_from_catalog(doc_id, enriched[doc_id]))
        elif doc_id in payloads:
            docs.append(_doc_from_payload(doc_id, payloads[doc_id]))
    return [d for d in docs if d.text.strip()]


def _est_tokens(text: str) -> int:
    # Rough chars/4 is plenty for batch sizing; exactness doesn't matter here.
    return max(1, len(text) // 4)


def _batch_documents(
    docs: Sequence[_Doc], budget: int = _MAP_BATCH_TOKENS
) -> list[list[_Doc]]:
    """Greedy split into groups of ~budget estimated tokens, preserving order.
    An oversized single document still gets its own batch."""
    batches: list[list[_Doc]] = []
    current: list[_Doc] = []
    spent = 0
    for doc in docs:
        cost = _est_tokens(doc.text) + 50  # id/title framing overhead
        if current and spent + cost > budget:
            batches.append(current)
            current, spent = [], 0
        current.append(doc)
        spent += cost
    if current:
        batches.append(current)
    return batches


def _summarize_direct(question: str, docs: list[_Doc]) -> str:
    from app.core.clients.llm import get_llm, reply_text
    from app.core.models.context import ContextBlock
    from app.generation.prompts import format_context_blocks

    blocks = [
        ContextBlock(
            n=i,
            text=doc.text,
            payload={"source_type": "website", "title": doc.title,
                     "effective_start_date": doc.effective_date,
                     "edition_label": doc.edition or None},
        )
        for i, doc in enumerate(docs, start=1)
    ]
    response = get_llm().invoke(
        [
            ("system", _DIRECT_SYSTEM),
            ("human", f"Documents:\n{format_context_blocks(blocks)}\n\n"
                      f"Request: {question}"),
        ]
    )
    return reply_text(response)


def _map_batch(batch: list[_Doc]) -> dict[str, list[str]]:
    from app.core.clients.llm import get_structured_llm

    parts = [
        f"document_id: {doc.document_id}\ntitle: {doc.title}\ntext: {doc.text}"
        for doc in batch
    ]
    model = get_structured_llm().with_structured_output(BatchSummaries)
    result: BatchSummaries = model.invoke(
        [("system", _MAP_SYSTEM), ("human", "\n\n---\n\n".join(parts))]
    )
    return {s.document_id: s.bullets for s in result.summaries if s.bullets}


def _numbered_line(n: int, doc: "_Doc") -> str:
    """One document's header line in the reduce prompt.

    The edition comes before the date, and the date is labelled "page date":
    editions of a series share one, so an unlabelled date invites the model to
    report the page's date as the document's own. It is the page's *effective*
    date — for an event or a project, the date the content is about — which is
    another reason not to let it read as a publication date.
    """
    parts = [f"[{n}] {doc.title}"]
    if doc.edition:
        parts.append(f"edition {doc.edition}")
    if doc.effective_date:
        parts.append(f"page date {doc.effective_date}")
    return " · ".join(parts)


def _summarize_map_reduce(question: str, docs: list[_Doc]) -> str:
    from app.core.clients.llm import get_llm, reply_text

    batches = _batch_documents(docs)
    with ThreadPoolExecutor(max_workers=_MAP_WORKERS) as pool:
        mapped = list(pool.map(_map_batch, batches))
    bullets: dict[str, list[str]] = {}
    for part in mapped:
        bullets.update(part)

    lines: list[str] = []
    for i, doc in enumerate(docs, start=1):
        lines.append(_numbered_line(i, doc))
        lines.extend(f"  - {b}" for b in bullets.get(doc.document_id, []))
    response = get_llm().invoke(
        [
            ("system", _REDUCE_SYSTEM),
            ("human", "Document summaries:\n" + "\n".join(lines)
                      + f"\n\nRequest: {question}"),
        ]
    )
    return reply_text(response)


def summarize_scope(
    analysis: QueryAnalysis | None,
) -> dict[str, Any] | None:
    """Answer a scoped-summary query, or None to fall through to semantic RAG."""
    if analysis is None:
        return None
    try:
        filters = _scope_filters(analysis)
        if not filters:
            return None
        ids = catalog.document_ids_in_scope(limit=_SCOPE_DOC_CAP, **filters)
        if not ids:
            return None
        docs = _collect_docs(ids)
        if not docs:
            return None
        if sum(_est_tokens(d.text) for d in docs) <= _DIRECT_TOKEN_BUDGET:
            answer = _summarize_direct(analysis.search_query, docs)
        else:
            answer = _summarize_map_reduce(analysis.search_query, docs)
        if not answer.strip():
            return None
    except Exception:
        logger.warning("Scoped summary failed; falling back to QA.", exc_info=True)
        return None

    citations = [
        Citation(n=i, type="website", title=d.title, url=d.url, document_id=d.document_id)
        for i, d in enumerate(docs, start=1)
    ]
    return {
        "answer": answer.strip(),
        "citations": [c.model_dump() for c in citations],
        "intent": "scoped_summary",
        "used_chunks": len(docs),
        "conflict": False,
        "cached": False,
    }
