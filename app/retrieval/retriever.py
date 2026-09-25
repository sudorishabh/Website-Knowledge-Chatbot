"""Retrieval orchestration.

Turns a processed search query into the final ordered ``ContextBlock`` list:
picks the base pull (plain or website-biased dual), fans out the optional
recall-expansion legs (multi-query, keyword) and fuses them, reranks, runs the
optional corrective loop, builds context, and supplements attachments for
detailed answers. Lifted out of the old ``app.rag`` god module.

Span labels stay ``rag.*`` — they are the stable metric-stage contract consumed
by observability/metrics (see docs/operations.md), not import paths.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from app.config import get_settings
from app.core.clients.embeddings import embed_query
from app.core.models.context import ContextBlock, is_priority_page
from app.observability import retrieval_log
from app.observability.tracing import span
from app.retrieval.context.builder import build_context
from app.retrieval.search.fusion import rrf
from app.retrieval.search.hybrid_search import search
from app.retrieval.search.reranker import rerank
from app.retrieval.search.strategies import (
    corrective_requery,
    dual_search,
    extract_content_terms,
    extract_key_terms,
    keyword_search,
    perspective_search,
    perspectives,
    subquery_search,
)
from app.retrieval.search.title_leg import title_search
from app.retrieval.understanding.filters import date_conditions

logger = logging.getLogger(__name__)

# `retrieve` is the retrieval engine's main public entry point; the stages it
# composes here (and in app.retrieval.search.strategies) are internal and free
# to change. The rest of the app depends only on these names.
#
# `graph_blocks_for` is exported alongside it because one caller legitimately
# needs the graph leg without the rest: `app.pipeline.query_pipeline` returns a
# deterministic catalog answer for some queries *before* it ever calls
# `retrieve`, and a relational question that lands there would otherwise never
# see the graph at all. Exposing the leg is what keeps that call site running the
# same code with the same fallback contract, rather than a second copy of it.
__all__ = ["retrieve", "graph_blocks_for"]

# Content capabilities (from query understanding) whose open-ended search
# benefits from multi-query recall expansion; a pure `database` lookup does not.
_MULTI_QUERY_INTENTS = frozenset({"qa", "comparison"})


def _supplement_attachments(
    blocks: list[ContextBlock],
    ranked: list[Any],
    *,
    search_query: str,
    query_vector: list[float],
    n: int,
    temporal: Any | None = None,
) -> list[ContextBlock]:
    """Detailed answers: when admitted website blocks have attached PDFs that
    contributed nothing to the context, pull those attachments' chunks once and
    let rerank decide admission. Bounded to one extra Qdrant query; any failure
    keeps the original blocks."""
    from app.catalog import queries as catalog
    from app.retrieval.search.scoped_retrieval import search_within_documents

    try:
        website_ids = {
            b.payload.get("document_id")
            for b in blocks
            if b.payload.get("source_type") == "website" and b.payload.get("document_id")
        }
        if not website_ids:
            return blocks
        attachments = catalog.attachments_for(sorted(website_ids))
        if not attachments:
            return blocks
        # An attachment document's id is its file_uuid; it is "represented"
        # when any admitted block is that document or links to it.
        represented = {
            v
            for b in blocks
            for v in (b.payload.get("document_id"), b.payload.get("linked_pdf_id"))
            if v
        }
        file_uuids = list(dict.fromkeys(
            a["file_uuid"]
            for rows in attachments.values()
            for a in rows
            if a.get("file_uuid") and a["file_uuid"] not in represented
        ))
        if not file_uuids:
            return blocks
        extra = search_within_documents(
            query_vector, file_uuids, limit=10, trace_stage="attachment_pull"
        )
        seen = {c.id for c in ranked}
        new = [c for c in extra if c.id not in seen]
        if not new:
            return blocks
        reranked = rerank(search_query, list(ranked) + new, temporal=temporal)
        return build_context(reranked, limit=n, temporal=temporal,
                             question=search_query)
    except Exception:
        logger.warning("Attachment supplementation failed; keeping original blocks.",
                       exc_info=True)
        return blocks


def graph_blocks_for(
    search_query: str,
    *,
    n: int,
    filters: list[Any] | None = None,
    source_type: str | None = None,
) -> list[ContextBlock]:
    """Blocks from the graph, or ``[]`` to carry on with existing retrieval.

    The whole contract in one line: an empty list means "fall back", and every
    outcome that is not a useful graph answer produces one — including a query
    scoped in a way no graph template can honour. The policy layer catches its
    own exceptions, so the ``except`` here is defence in depth for the import
    and the flag read: a graph problem must never reach a caller that was only
    asking a question.
    """
    if not getattr(get_settings(), "graph_routing_enabled", False):
        return []
    try:
        from app.retrieval.graph import policy

        with span("rag.graph_route") as s:
            attempt = policy.attempt(
                search_query, top_k=n, filters=filters, source_type=source_type
            )
            s.set("outcome", attempt.outcome)
            s.set("class", attempt.query_class or "-")
            s.set("rows", attempt.rows)
            s.set("scope", attempt.scope)
        return attempt.blocks if attempt.used else []
    except Exception:  # pragma: no cover - defence in depth
        logger.warning("Graph routing hook failed; using retrieval.", exc_info=True)
        return []


def _graph_subquery_blocks(
    subqueries: Sequence[Any],
    existing: list[ContextBlock],
    *,
    n: int,
    filters: list[Any] | None,
    source_type: str | None,
) -> list[ContextBlock]:
    """``existing`` plus whatever the graph can answer for the nominated parts.

    Same contract as the whole-question leg it reuses: every part is offered to
    `graph_blocks_for`, which returns blocks or nothing, so a part the graph
    cannot serve costs one declined attempt and changes nothing. De-duplicated
    against the blocks already held — the whole question and one of its parts
    routinely resolve to the same rows — by the key the graph/semantic merge
    already uses, so a row cannot be printed twice.
    """
    from app.retrieval.subqueries import GRAPH

    merged = list(existing)
    seen = {_block_key(b) for b in merged}
    for sub in subqueries:
        if not sub.goes_to(GRAPH):
            continue
        for block in graph_blocks_for(
            sub.text, n=n, filters=filters, source_type=source_type
        ):
            key = _block_key(block)
            if key not in seen:
                seen.add(key)
                merged.append(block)
    if len(merged) != len(existing):
        logger.info("Graph answered %d extra block(s) for question parts.",
                    len(merged) - len(existing))
    return merged


# Context slots kept for ordinary retrieval whenever the graph has also
# answered. The graph states *that* something is true and cites the documents
# its claims were read from; those documents frequently do not describe the
# subject at all, so without a reserved share the prose that does is never
# fetched.
#
# Two is the smallest number that helps and the largest that is free: with the
# default top_k of 6 the graph still keeps a facts block and three evidence
# passages, which is more evidence than any measured graph answer used.
SEMANTIC_MIN_SLOTS = 2


def _block_key(block: ContextBlock) -> str:
    """Identity for de-duplication across the two legs.

    The parent is preferred because the context builder admits by parent: the
    graph can hydrate one child of a parent while the semantic pull admits a
    different child of the same parent, and printing both is printing the same
    passage twice.
    """
    payload = block.payload
    return str(
        payload.get("parent_chunk_id")
        or payload.get("chunk_id")
        or payload.get("document_id")
        or id(block)
    )


def _merge_graph_and_retrieval(
    graph_blocks: list[ContextBlock],
    semantic_blocks: list[ContextBlock],
    *,
    limit: int,
    token_budget: int,
) -> list[ContextBlock]:
    """One context from both legs: the graph's answer, and the corpus's prose.

    The graph used to *replace* retrieval — ``if graph_blocks: return`` — which
    is right when the rows are the whole answer and wrong the moment they are
    only part of it. Measured: "a brief history of TERI" routes to
    ``entity_timeline``, the graph answers with eleven funding and partnership
    rows, and the six evidence passages are the project pages those claims came
    from. None of them says when TERI was founded. The Annual Report chunk that
    opens "TERI was established in 1974" sits at 0.77 similarity and was never
    fetched, because the semantic leg never ran. The answer was the refusal —
    correctly, since nothing in the context supported one.

    Order is meaning here. The facts block leads because it is what the graph
    verified; graph evidence follows because it is the provenance for those
    rows; ordinary retrieval fills the rest. Both legs are de-duplicated against
    each other, and the token budget is now shared rather than spent twice —
    which also closes the older gap where the facts block was appended after
    ``build_context`` had already spent its allowance.
    """
    from app.core.models.context import is_graph_facts
    from app.retrieval.context.builder import _count_tokens

    leading = graph_blocks[0] if graph_blocks else None
    facts = leading if leading is not None and is_graph_facts(leading.payload) else None
    graph_evidence = graph_blocks[1:] if facts is not None else list(graph_blocks)

    merged: list[ContextBlock] = []
    seen: set[str] = set()
    spent = 0

    def admit(block: ContextBlock) -> bool:
        key = _block_key(block)
        if key in seen or len(merged) >= limit:
            return False
        cost = _count_tokens(block.text)
        # The first block is admitted whatever it costs: a context of nothing is
        # worse than a context slightly over budget.
        if merged and spent + cost > token_budget:
            return False
        seen.add(key)
        merged.append(block)
        return True

    if facts is not None and admit(facts):
        spent += _count_tokens(facts.text)

    # Reserve the tail for prose before the graph's own evidence is allowed to
    # fill the context, but never reserve slots nothing can occupy.
    reserved = min(SEMANTIC_MIN_SLOTS, len(semantic_blocks))
    graph_allowance = max(0, limit - len(merged) - reserved)
    for block in graph_evidence[:graph_allowance]:
        if admit(block):
            spent += _count_tokens(block.text)

    for block in semantic_blocks:
        if admit(block):
            spent += _count_tokens(block.text)

    # Anything the reservation left unused goes back to the graph.
    for block in graph_evidence[graph_allowance:]:
        if admit(block):
            spent += _count_tokens(block.text)

    for i, block in enumerate(merged, start=1):
        block.n = i
    return merged


def _observe_in_shadow(search_query: str, blocks: list[ContextBlock]) -> None:
    """Hand the question to graph shadow mode, if it is enabled.

    Deliberately the only contact production retrieval has with the graph, and
    it is one-way: the call returns nothing, runs its work on a background
    thread, and swallows every error, so neither the blocks above nor the
    latency of this request can be affected by it.

    The import is local so that with the flag off — the default — the graph
    package is never even loaded.
    """
    from app.config import get_settings

    if not getattr(get_settings(), "graph_shadow_enabled", False):
        return
    try:
        from app.retrieval.graph import shadow

        shadow.observe(search_query, blocks)
    except Exception:  # pragma: no cover - defence in depth
        logger.warning("Graph shadow hook failed.", exc_info=True)


def retrieve(
    search_query: str,
    *,
    filters: list[Any] | None = None,
    n: int | None = None,
    query_vector: list[float] | None = None,
    answer_format: str | None = None,
    source_type: str | None = None,
    capabilities: set[str] | None = None,
    temporal: Any | None = None,
    subqueries: Sequence[Any] | None = None,
    priority: Any | None = None,
) -> list[ContextBlock]:
    """``priority`` is the question's `PriorityEvidence` (see
    :mod:`app.retrieval.priority`), or None — the feature off — to behave exactly
    as before it existed. With it, the stored copy of every listed page is
    dropped from the ranking (its live copy is the one that counts), a listed page
    whose stored copy ranked is read live instead, and the live sections lead the
    context.

    ``temporal`` is the question's `TemporalIntent`, or None to behave exactly
    as before it existed: no temporal ranking, and the UPCOMING gate detecting
    its own mode. The caller passes it only when `temporal_intent_enabled` is
    set, so the flag is honoured in one place rather than three.

    ``subqueries`` is the plan for a multi-part question (see
    :mod:`app.retrieval.subqueries`), or empty for the single-query path every
    ordinary question still takes. Each one contributes at most one more ranking
    to the same RRF every other leg goes through, and may nominate itself to the
    graph; neither can remove a candidate the base pull found. Planned in
    `app.pipeline` rather than here because the requirements it is built from
    come from `app.generation.answer_plan`, which retrieval may not import."""
    settings = get_settings()
    n = n or settings.retrieval_top_k

    # The graph leg. It answers or it declines; every outcome that is not a
    # useful answer — not routed, an empty graph, a scope it cannot honour, an
    # error, a timeout — leaves this empty and the retrieval below is unchanged.
    #
    # What it no longer does is *replace* retrieval. A graph answer used to
    # return from here, so a question the graph could answer only partly lost
    # the corpus entirely: "a brief history of TERI" got eleven funding rows and
    # the project pages behind them, while the Annual Report chunk beginning
    # "TERI was established in 1974" was never fetched. Both legs now run and
    # `_merge_graph_and_retrieval` composes one context from them.
    #
    # The query's scope is handed to the policy layer rather than checked here,
    # so a scope dimension added later cannot be forgotten at this call site.
    graph_blocks = graph_blocks_for(
        search_query, n=n, filters=filters, source_type=source_type
    )
    # A part of the question may route to the graph even when the whole question
    # does not: "which customers were affected by Project X and how much revenue
    # did they lose?" blends a relationship lookup with a measurement, and the
    # policy layer declines the blend. Each nominated part is offered separately,
    # still through the same `graph_blocks_for` contract — blocks or nothing —
    # and de-duplicated against what the whole question already returned.
    if subqueries:
        graph_blocks = _graph_subquery_blocks(
            subqueries, graph_blocks, n=n, filters=filters, source_type=source_type
        )

    # Prefer website content only when the feature is on, the user didn't pin a
    # source (explicit intent → honor their filter with a single pull, else the
    # PDF pull's "not website" would contradict a website filter), and the answer
    # isn't a table (tables live in PDFs — don't force a website lead).
    dual = bool(settings.prefer_website_enabled) and not source_type and answer_format != "table"
    # Multi-query only where recall expansion helps: an open-ended content
    # search (not a pure structured lookup), no explicit scope already narrowing
    # the pull, and enough words that perspectives can actually diverge (short
    # factoids are already unambiguous). The capabilities come from query
    # understanding; an empty set (the degraded passthrough) is treated as QA.
    content_search = not capabilities or bool(capabilities & _MULTI_QUERY_INTENTS)
    multi = (
        bool(settings.multi_query_enabled)
        and content_search
        and not source_type
        and not filters
        and len(search_query.split()) >= 5
    )

    if query_vector is None:
        with span("rag.embed_query"):
            query_vector = embed_query(search_query)

    def _base_search(active_filters: list[Any] | None, *, use_dual: bool) -> list[Any]:
        if use_dual:
            return dual_search(
                search_query, filters=active_filters,
                query_vector=query_vector, settings=settings,
            )
        return search(
            search_query,
            limit=settings.retrieval_candidate_k,
            extra_filter=active_filters or None,
            query_vector=query_vector,
        )

    keyword_terms = (
        extract_key_terms(search_query) if settings.keyword_leg_enabled else None
    )
    # A second lexical pull over the query's plain content words, fused as its own
    # ranking. `extract_key_terms` skips its content-word pass whenever any precise
    # pattern matched, and the organisation's acronym matches nearly every question
    # asked of it — so the precise list collapsed to ['TERI'] and, OR-ed, selected
    # most of the corpus. Kept separate rather than merged: OR-ing a ubiquitous
    # term with a selective one keeps the ubiquitous match, whereas a pull over
    # {initiatives, centres, excellence} alone lands on the hub page the dense
    # vectors miss because its text is mostly link labels. Skipped when it would
    # only repeat the precise terms.
    content_terms = (
        extract_content_terms(search_query) if settings.keyword_leg_enabled else None
    )
    if content_terms and keyword_terms and {
        c.lower() for c in content_terms
    } <= {k.lower() for k in keyword_terms}:
        content_terms = None

    # Title-anchored leg. Neither ranking nor the lexical legs can retrieve a
    # canonical page whose *text* is a list of link labels — see
    # `app.retrieval.search.title_leg`. Runs whenever the question names something
    # specific enough to match a page title; contributes one more ranking and
    # nothing else, so RRF decides what it is worth. Skipped for a pinned source
    # type, whose single filtered pull the caller has already narrowed.
    use_title_leg = not source_type

    # The semantic half of decomposition: one dense pull per part, fused like
    # every other leg. Deliberately not gated the way `multi` is — a multi-part
    # question is multi-part whether or not it is long, scoped or filtered, and
    # the planner has already decided there is more than one thing being asked.
    from app.retrieval.subqueries import SEMANTIC, texts as subquery_texts

    subquery_legs = subquery_texts(subqueries or [], SEMANTIC)

    with span("rag.search") as s:
        if multi or keyword_terms or content_terms or use_title_leg or subquery_legs:
            from concurrent.futures import ThreadPoolExecutor

            rankings: list[list[Any]] = []
            # Paraphrase generation and the keyword pull overlap the base
            # pull, so the added wall-clock is only the perspective searches
            # that follow the generation step.
            with ThreadPoolExecutor(max_workers=4) as pool:
                base_future = pool.submit(
                    retrieval_log.bound(_base_search), filters, use_dual=dual
                )
                keyword_future = (
                    pool.submit(
                        retrieval_log.bound(keyword_search),
                        search_query, keyword_terms,
                        filters=filters, query_vector=query_vector,
                        limit=settings.retrieval_candidate_k,
                    )
                    if keyword_terms
                    else None
                )
                content_future = (
                    pool.submit(
                        retrieval_log.bound(keyword_search),
                        search_query, content_terms,
                        filters=filters, query_vector=query_vector,
                        limit=settings.retrieval_candidate_k,
                        trace_stage="content_term_leg",
                    )
                    if content_terms
                    else None
                )
                title_future = (
                    pool.submit(
                        retrieval_log.bound(title_search),
                        search_query, query_vector,
                        limit=settings.retrieval_candidate_k,
                    )
                    if use_title_leg
                    else None
                )
                if multi:
                    with span("rag.multi_query") as mq:
                        # The query vector goes in so the generator can measure
                        # each angle against the question it came from, and the
                        # accepted ones come back carrying their own vectors —
                        # so a perspective is embedded once, not once to judge it
                        # and again to search it.
                        angles = pool.submit(
                            perspectives, search_query,
                            settings.multi_query_paraphrases,
                            query_vector=query_vector,
                        ).result()
                        rankings.extend(
                            r
                            for r in pool.map(
                                retrieval_log.bound(
                                    lambda p: perspective_search(
                                        p.text, limit=settings.retrieval_candidate_k,
                                        query_vector=p.vector,
                                    )
                                ),
                                angles,
                            )
                            if r
                        )
                        mq.set("perspectives", len(angles))
                        retrieval_log.note(
                            perspectives=[p.text for p in angles]
                        )
                if subquery_legs:
                    with span("rag.subquery_legs") as sq:
                        rankings.extend(
                            r
                            for r in pool.map(
                                retrieval_log.bound(
                                    lambda q: subquery_search(
                                        q, limit=settings.retrieval_candidate_k,
                                    )
                                ),
                                subquery_legs,
                            )
                            if r
                        )
                        sq.set("legs", len(subquery_legs))
                if keyword_future is not None:
                    with span("rag.keyword_leg") as kw:
                        keyword_hits = keyword_future.result()
                        kw.set("hits", len(keyword_hits))
                    if keyword_hits:
                        rankings.append(keyword_hits)
                if content_future is not None:
                    with span("rag.content_term_leg") as ct:
                        content_hits = content_future.result()
                        ct.set("hits", len(content_hits))
                        ct.set("terms", len(content_terms or []))
                    if content_hits:
                        rankings.append(content_hits)
                if title_future is not None:
                    with span("rag.title_leg") as tl:
                        title_hits = title_future.result()
                        tl.set("hits", len(title_hits))
                    if title_hits:
                        rankings.append(title_hits)
                base = base_future.result()
            candidates = rrf([base] + rankings) if rankings else base
        else:
            candidates = _base_search(filters, use_dual=dual)
        s.set("candidates", len(candidates))
        # Which legs ran and what the fused candidate set came to — the two
        # facts no single Qdrant event can state, since each one sees only its
        # own pull.
        retrieval_log.note(
            search_query=search_query,
            candidates=len(candidates),
            legs={
                "dual": dual,
                "multi_query": bool(multi),
                "keyword": bool(keyword_terms),
                "content_terms": bool(content_terms),
                "title": bool(use_title_leg),
                "subqueries": len(subquery_legs),
            },
        )

    # Facet filters (theme / author / source_type) are LLM-extracted and applied
    # as hard AND conditions. When they lift terms straight out of the question —
    # a title query parsed into theme="SDG 7", author="TERI" — those literals
    # rarely equal the stored metadata, and their intersection can be empty even
    # when the corpus plainly answers the question. A total miss under facets is
    # never better than the plain semantic pull, so retry once without them
    # rather than refusing. Precision-preserving: only fires on zero, so a
    # non-empty facet-scoped result is left exactly as-is.
    #
    # A date scope survives the retry (see `date_conditions`): the facets are
    # guesses at the corpus's labelling, but the period is the user's own
    # constraint, and answering "reports from 2023" out of 2019 is worse than
    # answering nothing — the more so because the widening is invisible, recorded
    # on the span below and never in the answer. When the window genuinely holds
    # no chunks, empty is honest and the pipeline's refusal path is correct —
    # which is also why an all-dates filter set skips the retry outright: it would
    # re-run the pull that just came back empty.
    kept = date_conditions(filters)
    if not candidates and filters and len(kept) < len(filters):
        with span("rag.search_relaxed") as s:
            relaxed_dual = bool(settings.prefer_website_enabled) and answer_format != "table"
            candidates = _base_search(kept or None, use_dual=relaxed_dual)
            s.set("candidates", len(candidates))
            s.set("relaxed", True)
            s.set("kept_date_scope", bool(kept))
            retrieval_log.note(
                facets_relaxed=True,
                relaxed_candidates=len(candidates),
                kept_date_scope=bool(kept),
            )
        logger.info(
            "Facet filters matched no chunks; retried without facets%s (%d candidates).",
            " but within the date scope" if kept else "", len(candidates),
        )

    with span("rag.rerank") as s:
        table_boost = settings.rerank_table_boost if answer_format == "table" else 0.0
        ranked = rerank(
            search_query, candidates, table_boost=table_boost, temporal=temporal
        )
        s.set("survivors", len(ranked))
        retrieval_log.note(rerank_survivors=len(ranked))
    if (
        bool(settings.corrective_loop_enabled)
        and ranked
        and ranked[0].semantic_score < settings.corrective_min_score
    ):
        with span("rag.corrective") as s:
            score_before = ranked[0].semantic_score
            ranked = corrective_requery(
                search_query, ranked,
                filters=filters, limit=settings.retrieval_candidate_k,
                table_boost=table_boost, temporal=temporal,
            )
            score_after = ranked[0].semantic_score if ranked else 0.0
            # Did the retry actually lift the top result? Recorded so we can
            # later judge whether the corrective loop earns its extra LLM +
            # search cost before tuning or removing it.
            s.set("survivors", len(ranked))
            s.set("score_before", round(score_before, 4))
            s.set("score_after", round(score_after, 4))
            s.set("improved", score_after > score_before)
            logger.info(
                "corrective loop: top score %.4f -> %.4f (%s)",
                score_before, score_after,
                "improved" if score_after > score_before else "no gain",
            )
    if priority is not None:
        # Before anything is built from the ranking: a listed page's stored copy
        # must not be admitted, and where it ranked, the live page is read.
        with span("rag.priority_stored_copies") as s:
            before = len(ranked)
            ranked = priority.drop_stored_copies(ranked, top_n=n)
            s.set("dropped", before - len(ranked))
    if not ranked:
        # Nothing from the corpus. A graph answer still stands on its own, which
        # is the behaviour this leg has always had when retrieval came up empty —
        # and so do the priority pages.
        _observe_in_shadow(search_query, [])
        if priority is not None and priority.blocks:
            return priority.merge(list(graph_blocks), limit=n,
                                  token_budget=settings.context_token_budget)
        return list(graph_blocks)
    with span("rag.context_build"):
        blocks = build_context(ranked, limit=n, temporal=temporal,
                               question=search_query)
    if answer_format == "detailed" and blocks:
        with span("rag.attachment_pull"):
            blocks = _supplement_attachments(
                blocks, ranked, search_query=search_query, query_vector=query_vector,
                n=n, temporal=temporal,
            )
    if graph_blocks:
        # Merged last, so attachment supplementation above still operates on the
        # retrieval blocks alone — it rebuilds context from `ranked` and would
        # otherwise drop the facts block it knows nothing about.
        with span("rag.graph_merge") as s:
            blocks = _merge_graph_and_retrieval(
                graph_blocks, blocks, limit=n,
                token_budget=settings.context_token_budget,
            )
            s.set("graph_blocks", len(graph_blocks))
            s.set("merged", len(blocks))
            retrieval_log.note(
                graph_blocks=len(graph_blocks), merged_blocks=len(blocks)
            )
    if priority is not None and priority.blocks:
        # Last of the merges, so the live pages lead whatever the corpus and the
        # graph composed between them; the corpus keeps its reserved slots.
        with span("rag.priority_merge") as s:
            blocks = priority.merge(blocks, limit=n,
                                    token_budget=settings.context_token_budget)
            s.set("priority_blocks", sum(1 for b in blocks if is_priority_page(b.payload)))
    # Temporal gate, last of all: an "upcoming" question must not be answered
    # from events that have already happened. Applied here rather than as a
    # vector pre-filter because an event's own start date lives in the CMS
    # (`documents.raw_meta`) and is not in the Qdrant payload — the only date on
    # a chunk is `effective_start_date`, which is when the page went up, not when the
    # event runs. Removal-only and it never empties the context, so the worst it
    # can do is leave the context exactly as it was.
    blocks = _gate_temporal(search_query, blocks, temporal=temporal)
    _observe_in_shadow(search_query, blocks)
    return blocks


def _gate_temporal(
    search_query: str,
    blocks: list[ContextBlock],
    *,
    temporal: Any | None = None,
) -> list[ContextBlock]:
    """Apply the question's temporal scope to the finished context.

    ``temporal`` carries the mode query understanding already classified. It is
    detected on the same string this function would have detected on, so passing
    it changes nothing about which mode is chosen — it only saves the second
    regex pass and makes the mode inspectable upstream. With it absent the
    function detects for itself, exactly as it always did.

    Only the two *scheduled-occurrence* modes gate. A point-in-time or
    date-range question is already scoped by `filters.date_conditions` before the
    search runs, and re-applying it here would be a second, weaker copy of that
    logic; those modes reach ranking instead.
    """
    if not blocks:
        return blocks
    try:
        from app.retrieval.search import temporal_gate

        if temporal is None:
            # Exactly the pre-Phase-B path: detect here, and only UPCOMING gates.
            mode = temporal_gate.detect_mode(search_query)
            gate = (temporal_gate.gate_upcoming
                    if mode == temporal_gate.UPCOMING else None)
        else:
            mode = temporal.mode
            gate = {
                temporal_gate.UPCOMING: temporal_gate.gate_upcoming,
                temporal_gate.PAST: temporal_gate.gate_past,
            }.get(mode)
        if gate is None:
            return blocks
        with span("rag.temporal_gate") as s:
            gated = gate(blocks)
            s.set("mode", mode)
            s.set("dropped", len(blocks) - len(gated))
        return gated
    except Exception:  # pragma: no cover - defence in depth
        # A temporal filter must never cost an answer.
        logger.warning("Temporal gate failed; using the ungated context.", exc_info=True)
        return blocks
