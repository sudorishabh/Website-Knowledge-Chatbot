"""Id-scoped Qdrant reads: search within a catalog-selected document set.

Inverts the usual vector-search-plus-facet-filter flow for queries where
MySQL is authoritative for set membership (theme/author/period/title scopes):
the catalog picks the ids, Qdrant ranks or fetches content strictly inside
them. Read-only against the documents collection; every path fails open.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from app.config import get_settings
from app.core.clients import get_qdrant_client
from app.observability import retrieval_log
from app.retrieval.search.hybrid_search import Candidate, build_filter, search

logger = logging.getLogger(__name__)

# Safety cap on MatchAny id sets (callers pre-cap via the catalog limit, but
# an unbounded id filter must never reach Qdrant).
_MAX_IDS = 150


def search_within_documents(
    query_vector: Sequence[float],
    document_ids: Sequence[str],
    *,
    limit: int,
    trace_stage: str = "scoped_pull",
    extra_filter: Sequence[Any] | None = None,
) -> list[Candidate]:
    """Dense search constrained to the given document ids, and to
    ``extra_filter`` when given."""
    from qdrant_client.models import FieldCondition, MatchAny

    ids = [d for d in document_ids if d][:_MAX_IDS]
    if not ids:
        return []
    try:
        return search(
            "",  # query text unused when query_vector is supplied
            limit=limit,
            extra_filter=[FieldCondition(key="document_id", match=MatchAny(any=ids)),
                          *(extra_filter or ())],
            query_vector=query_vector,
            trace_stage=trace_stage,
        )
    except Exception:
        logger.warning("Id-scoped search failed.", exc_info=True)
        return []


# How far into a document to look for a lead chunk once the first one turns out
# to be unusable. Front matter (a cover page, a multi-page table of contents)
# can occupy several chunks.
_LEAD_SCAN_CHUNKS = 5

# Tried in order, each against only the documents still without a lead:
# the usual single-point fetch; then past the front matter; then, for a
# document that is *entirely* front matter, its opening chunk regardless.
# Escalating this way keeps the common case at exactly one point per document.
_LEAD_STRATEGIES: tuple[tuple[tuple[int, ...], bool], ...] = (
    ((0,), True),
    (tuple(range(1, _LEAD_SCAN_CHUNKS)), True),
    ((0,), False),
)


def _scroll_leads(
    ids: Sequence[str],
    indices: Sequence[int],
    *,
    exclude_non_searchable: bool,
) -> list[dict[str, Any]]:
    """Child payloads for ``ids`` restricted to the given chunk indices."""
    from qdrant_client.models import FieldCondition, MatchAny

    settings = get_settings()
    scroll_filter = build_filter(
        extra=[
            FieldCondition(key="document_id", match=MatchAny(any=list(ids))),
            FieldCondition(key="chunk_index", match=MatchAny(any=list(indices))),
        ],
        exclude_non_searchable=exclude_non_searchable,
    )
    with retrieval_log.qdrant_call(
        "scroll",
        stage="lead_child_scroll",
        request=lambda: {
            "collection": settings.qdrant_collection,
            "filter": scroll_filter,
            "limit": len(ids) * len(indices),
            "document_ids": list(ids),
            "chunk_indices": list(indices),
        },
    ) as call:
        try:
            points, _ = get_qdrant_client().scroll(
                collection_name=settings.qdrant_collection,
                scroll_filter=scroll_filter,
                limit=len(ids) * len(indices),
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            call.fail(exc)
            logger.warning("Lead-child scroll failed.", exc_info=True)
            return []
        call.qdrant_results(points)
    return [p.payload or {} for p in points]


def _earliest_per_document(
    payloads: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        doc_id = payload.get("document_id")
        if not doc_id:
            continue
        current = best.get(doc_id)
        if current is None or (payload.get("chunk_index") or 0) < (
            current.get("chunk_index") or 0
        ):
            best[doc_id] = payload
    return best


def lead_parents(
    document_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """document_id -> the best single payload to represent the document.

    A document's lead parent chunk (~1600 tokens max) is its best one-block
    representation for summarization. Children carry ``chunk_index`` and
    parents don't, so find each document's earliest usable child and hop to its
    ``parent_chunk_id`` in one batched retrieve. Documents whose lead child has
    no parent fall back to the child payload itself (single-child documents
    skip parent emission).

    "Usable" is why this escalates rather than simply taking ``chunk_index ==
    0``. The mandatory filter excludes toc/references/glossary chunks, so a
    report whose first chunk is its table of contents used to match nothing and
    disappear from the caller's scope entirely — silently, since the caller
    only sees a shorter dict. It now looks past the front matter, and as a last
    resort takes the opening chunk even if that is all the document has.
    """
    ids = [d for d in document_ids if d][:_MAX_IDS]
    if not ids:
        return {}

    children: dict[str, dict[str, Any]] = {}
    for indices, exclude_non_searchable in _LEAD_STRATEGIES:
        missing = [i for i in ids if i not in children]
        if not missing:
            break
        children.update(
            _earliest_per_document(
                _scroll_leads(
                    missing, indices,
                    exclude_non_searchable=exclude_non_searchable,
                )
            )
        )

    parent_ids = [
        c["parent_chunk_id"] for c in children.values() if c.get("parent_chunk_id")
    ]
    parents: dict[str, dict[str, Any]] = {}
    if parent_ids:
        try:
            records = get_qdrant_client().retrieve(
                collection_name=get_settings().qdrant_collection,
                ids=parent_ids,
                with_payload=True,
                with_vectors=False,
            )
            parents = {str(r.id): (r.payload or {}) for r in records}
        except Exception:
            logger.warning("Parent retrieve failed; using child payloads.", exc_info=True)

    return {
        doc_id: parents.get(str(child.get("parent_chunk_id") or "")) or child
        for doc_id, child in children.items()
    }
