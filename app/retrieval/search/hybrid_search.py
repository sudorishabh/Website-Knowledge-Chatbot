from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import get_settings
from app.core.clients import get_embeddings, get_qdrant_client
from app.observability import retrieval_log

logger = logging.getLogger(__name__)

# Section types that extract cleanly but pollute retrieval (tables of contents,
# bibliographies, glossaries) — excluded from search via the query filter.
_NON_SEARCHABLE_SECTIONS = ("toc", "references", "glossary")

# Collections confirmed to exist this process. A missing collection is a
# bootstrap/error state, not a per-query concern, so we verify it once and then
# skip the extra round-trip — steady state is a single query_points per search.
_verified_collections: set[str] = set()


def _collection_ready(client: Any, name: str) -> bool:
    if name in _verified_collections:
        return True
    if client.collection_exists(name):
        _verified_collections.add(name)
        return True
    return False


@dataclass
class Candidate:
    """One retrieved chunk, carrying three scores that must not be conflated.

    ``score``          the *current ranking* value, whatever stage produced it:
                       the dense similarity out of Qdrant, then the fused value
                       after ``fusion.rrf``, then the banded relevance after
                       ``reranker.rerank``. Ordering only — never compare it to
                       a configured threshold.
    ``semantic_score`` the *raw semantic relevance*, on the scale the active
                       scorer works in (Qdrant cosine, or the reranker
                       provider's own 0-1 score). Set once at search time and
                       preserved through fusion, because every configured
                       threshold — ``corrective_min_score``,
                       ``rerank_score_threshold`` — is calibrated against it.
    ``fusion_score``   the reciprocal-rank value from ``fusion.rrf``, on its own
                       ~0.016-0.033 scale; 0.0 when no fusion ran.

    Keeping these apart is the fix for a real defect: ``rrf`` used to overwrite
    ``score`` and the cosine-scaled thresholds read it, so merely enabling the
    keyword or multi-query leg put every candidate an order of magnitude below
    them — emptying the website group through an admission floor since retired,
    and pinning the corrective loop open through one that has not
    (see tests/retrieval/search/test_fusion_score_integrity.py).
    """

    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)
    vector: list[float] = field(default_factory=list)
    semantic_score: float = 0.0
    fusion_score: float = 0.0

    @property
    def parent_id(self) -> str | None:
        return self.payload.get("parent_chunk_id")

    @property
    def text(self) -> str:
        return self.payload.get("chunk_text", "")


def build_filter(
    *,
    extra: Sequence[Any] | None = None,
    extra_must_not: Sequence[Any] | None = None,
    exclude_non_searchable: bool = True,
) -> Any:
    """The mandatory shape filter, plus caller conditions.

    The corpus is public: every caller may see all of it, so there is no tenant
    or ACL leg here. What remains mandatory is what keeps retrieval pointed at
    the *searchable* view of the corpus — current child chunks, since parents
    hold no vector of their own and superseded versions are not the answer.

    ``exclude_non_searchable`` drops toc/references/glossary chunks and is on
    for every search. Fetches that must return *something* for a document —
    rather than the best thing to search — turn it off as a last resort; see
    :func:`app.retrieval.search.scoped_retrieval.lead_parents`.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    must: list[Any] = [
        FieldCondition(key="is_parent", match=MatchValue(value=False)),
        FieldCondition(key="is_current", match=MatchValue(value=True)),
    ]
    if extra:
        must.extend(extra)
    must_not: list[Any] = []
    if exclude_non_searchable:
        must_not.append(
            FieldCondition(
                key="section_type", match=MatchAny(any=list(_NON_SEARCHABLE_SECTIONS))
            )
        )
    if extra_must_not:
        must_not.extend(extra_must_not)
    return Filter(must=must, must_not=must_not or None)


def search(
    query: str,
    *,
    limit: int | None = None,
    extra_filter: Sequence[Any] | None = None,
    extra_must_not: Sequence[Any] | None = None,
    query_vector: Sequence[float] | None = None,
    with_vectors: bool = True,
    trace_stage: str = "dense_pull",
) -> list[Candidate]:
    """One dense pull. ``trace_stage`` names the leg for the retrieval log only
    (every leg in ``retriever.retrieve`` calls this function, and a trace that
    could not tell the website pull from the keyword pull would not be much of a
    trace); it has no effect on retrieval."""
    settings = get_settings()
    limit = limit or settings.retrieval_candidate_k

    client = get_qdrant_client()
    if not _collection_ready(client, settings.qdrant_collection):
        logger.warning("Collection %r does not exist; no results.", settings.qdrant_collection)
        return []

    vector = list(query_vector) if query_vector is not None else get_embeddings().embed_query(query)
    query_filter = build_filter(extra=extra_filter, extra_must_not=extra_must_not)

    with retrieval_log.qdrant_call(
        "vector_search",
        stage=trace_stage,
        # A callable: with logging off this dictionary is never built, and with
        # it on the filter is serialized once, here, rather than at every leg.
        request=lambda: {
            "collection": settings.qdrant_collection,
            "query_text": query,
            "limit": limit,
            "filter": query_filter,
            "vector_dimensions": len(vector),
            "vector_supplied": query_vector is not None,
            "with_vectors": with_vectors,
        },
    ) as call:
        response = client.query_points(
            collection_name=settings.qdrant_collection,
            query=vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=with_vectors,
        )
        call.qdrant_results(response.points)
    return [_to_candidate(p) for p in response.points]


def _to_candidate(point: Any) -> Candidate:
    raw = getattr(point, "vector", None)
    if isinstance(raw, dict):
        raw = raw.get("dense") or next(iter(raw.values()), None)
    vector = [float(x) for x in raw] if isinstance(raw, (list, tuple)) else []
    score = float(point.score or 0.0)
    return Candidate(
        id=str(point.id),
        score=score,
        payload=point.payload or {},
        vector=vector,
        # The dense similarity is the semantic relevance until a reranker
        # provider replaces it. Stamped here — the one place a candidate is born
        # from a real search — so it is already on the payload before any fusion
        # can rewrite `score`.
        semantic_score=score,
    )
