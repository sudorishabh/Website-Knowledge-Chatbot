"""End-to-end graph retrieval: question in, context blocks out.

    question -> route -> template -> Neo4j -> ids -> Qdrant -> rerank -> context

Two things this deliberately does **not** do:

* **It does not introduce a generation architecture.** The last two steps are
  the existing ``app.retrieval.search.reranker.rerank`` and
  ``app.retrieval.context.builder.build_context``; a graph answer becomes the
  same ``ContextBlock`` list every other answer is built from.
* **It does not touch the default retrieval path.** Nothing in
  ``app/retrieval/retriever.py`` or ``app/pipeline`` imports this module, and
  ``graph_retrieval_enabled`` is off. With the flag down this code is unreachable
  from a request.

Failure is a value at every step. No route, an unreachable graph, an empty
traversal or a failed hydration all return an empty list, and the caller falls
back to ordinary retrieval — the graph is an enrichment, and no question should
fail because it was unavailable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Row budget for a historical question, where the answer is a timeline rather
# than a present state. Clamped by the registry to `templates.MAX_LIMIT`.
HISTORICAL_LIMIT = 100


@dataclass
class GraphAnswer:
    """A graph-retrieved answer, with the trail that produced it."""

    blocks: list[Any] = field(default_factory=list)
    route: Any = None
    result: Any = None
    hydrated: int = 0
    facts: bool = False
    elapsed_ms: float = 0.0
    stage_ms: dict[str, float] = field(default_factory=dict)
    reason: str = ""

    @property
    def answered(self) -> bool:
        return bool(self.blocks)

    @property
    def disputed(self) -> bool:
        """Whether any supporting claim is contradicted.

        A caller presenting this answer must say so. Current-state templates
        cannot produce it — a disputed claim gets no current edge — so it only
        arises for historical questions, where showing a labelled contradiction
        is better than hiding it.
        """
        return bool(self.result and self.result.has_disputed)

    def summary(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "route": (
                {
                    "template_id": self.route.template_id,
                    "mode": self.route.mode,
                    "entity": self.route.entity_name,
                    "reason": self.route.reason,
                }
                if self.route
                else None
            ),
            "rows": len(self.result.rows) if self.result else 0,
            "hydrated_chunks": self.hydrated,
            "facts_block": self.facts,
            "blocks": len(self.blocks),
            "disputed": self.disputed,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "stage_ms": {k: round(v, 1) for k, v in self.stage_ms.items()},
            "reason": self.reason,
        }


def answer(
    question: str,
    *,
    index: Any = None,
    limit: int | None = None,
    top_k: int | None = None,
    as_of: str | None = None,
    rerank_results: bool = True,
) -> GraphAnswer:
    """Answer one question from the graph, or return empty and explain why."""
    from app.config import get_settings
    from app.retrieval.graph import facts
    from app.retrieval.graph import hydrate as hydration
    from app.retrieval.graph import router as routing
    from app.retrieval.graph import traverse

    started = time.perf_counter()
    out = GraphAnswer()
    stage: dict[str, float] = {}

    def _mark(name: str, since: float) -> float:
        now = time.perf_counter()
        stage[name] = (now - since) * 1000
        return now

    mark = started
    outcome = routing.route(question, index=index, as_of=as_of)
    mark = _mark("route", mark)
    if not outcome.routed:
        out.reason = outcome.reason
        out.stage_ms = stage
        out.elapsed_ms = (time.perf_counter() - started) * 1000
        return out
    out.route = outcome.route

    # History is inherently larger than the present, and the history templates
    # order by recency — so at the current-state default an organization with
    # many records returns its recent rows and almost none of its ended ones.
    # Measured: DBT at limit 25 yields 6 ended relationships of 44, at limit 100
    # all 44. A historical question therefore gets the larger budget.
    effective_limit = limit
    if effective_limit is None and outcome.route.is_historical:
        effective_limit = HISTORICAL_LIMIT

    result = traverse.run_template(
        outcome.route.template_id, outcome.route.parameters,
        limit=effective_limit,
        # The route knows which question was asked; the template only knows
        # where it reads from. See `traverse.run_template`.
        mode=outcome.route.mode,
    )
    mark = _mark("neo4j", mark)
    out.result = result
    if result.error:
        out.reason = f"graph query failed: {result.error}"
        out.stage_ms = stage
        out.elapsed_ms = (time.perf_counter() - started) * 1000
        return out
    if result.empty:
        out.reason = "graph query returned no rows"
        out.stage_ms = stage
        out.elapsed_ms = (time.perf_counter() - started) * 1000
        return out

    candidates = hydration.hydrate(result)
    mark = _mark("qdrant", mark)
    out.hydrated = len(candidates)

    settings = get_settings()
    ranked = candidates
    if candidates and rerank_results:
        from app.retrieval.search.reranker import rerank

        # The existing reranker, unchanged. A graph answer is ranked the same way
        # every other answer is, so nothing new has to be tuned or trusted.
        ranked = rerank(question, candidates)
        mark = _mark("rerank", mark)

    from app.retrieval.context.builder import build_context

    evidence = (
        build_context(ranked, limit=top_k or settings.retrieval_top_k)
        if ranked else []
    )

    # The rows are the answer; the passages are the citation. Hydration returns
    # only the latter, and for a CMS-derived claim the passage frequently does
    # not contain the fact at all - it lived in a structured field. Stating the
    # verified rows first, then the evidence, is what makes a relational answer
    # both correct and checkable.
    facts_block = facts.as_block(result, outcome.route)
    if facts_block is None and not evidence:
        out.reason = "graph rows could not be rendered or hydrated"
        out.stage_ms = stage
        out.elapsed_ms = (time.perf_counter() - started) * 1000
        return out

    blocks = []
    if facts_block is not None:
        blocks.append(facts_block)
    for block in evidence:
        block.n = len(blocks) + 1
        blocks.append(block)
    out.blocks = blocks
    out.facts = facts_block is not None
    _mark("context", mark)
    out.stage_ms = stage
    out.elapsed_ms = (time.perf_counter() - started) * 1000
    out.reason = outcome.route.reason
    logger.info("Graph answer: %s", out.summary())
    return out
