"""The query→answer pipeline.

Shared front-matter (``_prepare``: query understanding, structured/summary
shortcuts, caches, retrieval) feeds both the streaming ``stream_answer`` entry
point and the retrieval-only ``search_blocks``. Answer generation is delegated to
:mod:`app.generation.answerer`; retrieval to :mod:`app.retrieval.retriever`.

Span labels stay ``rag.*`` — they are the stable metric-stage contract, not
import paths.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator

from app.config import get_settings
from app.core.models.context import ContextBlock
from app.generation.answerer import chitchat, generate_answer, generate_stream
from app.generation.prompts import REFUSAL
from app.observability import retrieval_log
from app.observability.metrics import collect_into, component_totals
from app.observability.tracing import record_query_metrics, span
from app.retrieval.context.citations import build_citations
from app.retrieval.understanding.clarify import Clarification
from app.retrieval.understanding.document_request import is_document_request
from app.retrieval.understanding.query_processor import ProcessedQuery, process
from app.retrieval.retriever import retrieve

logger = logging.getLogger(__name__)


def _empty(
    intent: str, answer: str, *, answer_format: str = "default", cached: bool = False
) -> dict[str, Any]:
    return {
        "answer": answer,
        "citations": [],
        "intent": intent,
        "answer_format": answer_format,
        "used_chunks": 0,
        "conflict": False,
        "cached": cached,
    }


@dataclass
class _Generation:
    """What the answer step needs after the shared front-matter (query
    understanding, cache lookups, retrieval) has decided a fresh grounded answer
    must be generated. Carried out of `_prepare` so the buffered and streaming
    entrypoints share one pipeline and differ only in how they emit the answer."""

    pq: ProcessedQuery
    blocks: list[ContextBlock]
    query_vector: list[float]
    top_k: int
    # Deterministic catalog section prefixed onto a combined (database + content)
    # answer; "" for single-source answers.
    db_prefix: str = ""
    # Evidence-coverage directive for a genuinely multi-part question; "" when
    # the question has one part or the extraction step failed. See
    # `app.generation.answer_plan`.
    plan_directive: str = ""
    # The answer-cache fingerprint the answer is stored under: the facet
    # fingerprint plus the content of any priority page it stands on. None means
    # the facet fingerprint alone.
    cache_fingerprint: dict[str, Any] | None = None


# Content capabilities that pair with a database lookup into a combined answer.
_CONTENT_CAPS = frozenset({"qa", "comparison"})


def _trace_understanding(pq: ProcessedQuery, *, top_k: int) -> None:
    """Record what query understanding decided, for the retrieval trace.

    The trace's first question is always "what was actually searched for?" — the
    rewritten search query, the intent that chose the route, and the facet
    filters that narrowed every pull. Costs nothing when logging is off (see
    ``app.observability.retrieval_log``)."""
    retrieval_log.note_query(
        original=pq.original,
        search_query=pq.search_query,
        intent=pq.intent,
        answer_format=pq.answer_format,
        source_type=pq.source_type,
        language=pq.language,
        filters=pq.filters,
        top_k=top_k,
        is_ambiguous=pq.is_ambiguous,
        # Whether this turn asked a question back, and whether it was itself the
        # answer to one — without both, a trace for a clarified turn shows a
        # search query holding words the user never typed and no reason why.
        clarifying=pq.clarification is not None,
        clarified_from=pq.clarified_from,
        capabilities=sorted(_capabilities(pq)),
        intents=[
            {"label": p.label, "confidence": p.confidence, "rationale": p.rationale}
            for p in (pq.understanding.intents if pq.understanding else [])
        ],
        analysis=pq.analysis,
    )


def _clarification_result(
    clarification: Clarification, *, answer_format: str = "default"
) -> dict[str, Any]:
    """The turn that asks the user a question instead of answering.

    Shaped as an ordinary result so every existing consumer — the buffered
    return, the SSE driver, the metrics recorder — handles it without knowing
    what it is. The rendered text carries the state (see
    ``app.retrieval.understanding.clarify``): it ends with the marker the next
    turn recognises, so a client that does nothing but echo the answer back in
    ``history`` already implements the whole protocol.

    ``intent`` is reported as ``clarification`` rather than the route the query
    would have taken. By this point that route is chitchat-or-rescued-qa, which
    describes what the classifier did and not what the user was sent, and the
    metrics that matter here are "how often do we ask" and "does asking help".

    ``clarification`` is an *additive* key. Nothing has to read it — the question
    and its options are already in the answer text — but a client that wants to
    render options as buttons has them structured rather than parsed back out of
    prose.
    """
    retrieval_log.note(
        clarification_kind=clarification.kind,
        clarification_options=len(clarification.options),
    )
    return {
        **_empty("clarification", clarification.render(), answer_format=answer_format),
        "clarification": {
            "question": clarification.question,
            "options": list(clarification.options),
            "kind": clarification.kind,
        },
    }


def _temporal_intent(pq: ProcessedQuery) -> Any | None:
    """The question's temporal intent, or None to retrieve as before.

    The single place `temporal_intent_enabled` is read. `process` computes the
    intent either way — it is a regex over a string the analysis produced anyway,
    and having it on the trace is worth more than the microseconds — but nothing
    downstream sees it unless the flag is set, so OFF leaves both ranking and the
    pre-existing UPCOMING gate byte-identical.
    """
    if not getattr(get_settings(), "temporal_intent_enabled", False):
        return None
    return pq.temporal_intent


def _subquery_plan(pq: ProcessedQuery, requirements_future: Any) -> list[Any]:
    """The multi-part plan for this query, or ``[]`` to retrieve as one query.

    The single place `subquery_planning_enabled` is read. Off, the requirements
    future is never joined here, so retrieval starts without waiting for it and
    the empty plan is a no-op through every leg below.

    Fails to ``[]``: a planning problem must cost the decomposition, never the
    answer — the base pull runs regardless, so an empty plan is simply the
    behaviour this query had before the feature existed.
    """
    settings = get_settings()
    if not getattr(settings, "subquery_planning_enabled", False):
        return []
    from app.retrieval import subqueries as planning

    try:
        plan = planning.plan(
            pq.search_query,
            requirements_future.result(),
            limit=getattr(settings, "subquery_max", 3),
        )
    except Exception:
        logger.warning("Sub-query planning failed; retrieving as one query.",
                       exc_info=True)
        return []
    if plan:
        logger.info("Decomposed into %d parts: %s",
                    len(plan), [s.text for s in plan])
        retrieval_log.note(
            subqueries=[
                {"text": s.text, "requirement": s.requirement,
                 "routes": list(s.routes)}
                for s in plan
            ]
        )
    return plan


# How the answer opens, by how the edition was chosen. `default_latest` means
# the user said "the annual report" and the resolver took the newest, so the
# sentence says which one it picked rather than implying they asked for it.
_EDITION_LEAD: dict[str, str] = {
    "latest": "The latest annual report is",
    "default_latest": "The most recent annual report is",
    "earliest": "The earliest annual report is",
    "named": "The annual report you asked for is",
}


def _document_name(block: ContextBlock, edition: str) -> str:
    """What to call this document in the answer.

    The catalogued title when the chunk carries one — it is the anchor text the
    page uses for each edition ("Annual Report 2024-2025"), which is the only
    place an edition's identity exists as prose. Falls back to the resolved
    edition rather than inventing a title.
    """
    title = str(block.payload.get("title") or "").strip()
    return title or f"the {edition} annual report"


def _document_result(
    pq: ProcessedQuery, blocks: list[ContextBlock]
) -> dict[str, Any]:
    """Answer a request for the document itself, from the document's identity.

    The case this exists for: "give me the latest annual report" resolved
    correctly to the 2024-25 edition and retrieved two chunks from deep inside
    it, and generation then refused — rightly, because pages 148-150 do not say
    which edition is the latest one. No report states that about itself, so no
    amount of retrieval was ever going to ground it. The answer is the
    document's *identity*, which `pq.edition` has held since query understanding.

    Deterministic, and deliberately so: no model call, no new lookup, no second
    source of truth about which document this is. The prose is assembled from
    the resolved edition and the catalogued titles already on the retrieved
    chunks, and the source cards come from `build_citations` — the same function
    that describes every other answer's sources, so a card here is identical to
    the card the same document gets anywhere else.

    Citations cover every retrieved block rather than only the cited ones: they
    are all chunks of the document being named, so each is a true source for the
    claim, and this is also the set the user already sees today.
    """
    resolution = pq.edition
    lead = _EDITION_LEAD.get(resolution.kind, "The annual report you asked for is")

    named: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        key = str(block.payload.get("document_id") or block.payload.get("title") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        named.append(f"{_document_name(block, resolution.edition)} [{block.n}]")

    citations = build_citations(blocks)
    retrieval_log.note(
        document_lookup=True,
        edition=resolution.edition,
        edition_kind=resolution.kind,
        documents=len(seen) or len(blocks),
    )
    return {
        "answer": f"{lead} {', '.join(named)}.",
        "citations": [c.model_dump() for c in citations],
        # Its own label rather than the route the classifier took: the metric
        # worth having is how often a document request is answered as one.
        "intent": "document_lookup",
        "answer_format": pq.answer_format,
        "used_chunks": len(blocks),
        "conflict": any(b.conflict for b in blocks),
        "cached": False,
    }


def _series_blocks(documents: Any) -> list[ContextBlock]:
    """The series' editions as context blocks, so the real citation builder can
    describe them.

    Not a second card format: `build_citations` reads a payload, and these
    payloads carry exactly the keys it reads for a PDF attachment. A card for the
    2024-25 edition here is therefore the same card that edition gets on any
    other answer, built by the same function.
    """
    return [
        ContextBlock(
            n=i,
            text="",
            payload={
                "document_id": doc.document_id,
                "chunk_id": doc.document_id,
                "source_type": "pdf_attachment",
                "bundle": "report",
                "title": doc.title,
                "edition_label": doc.edition,
                "file_url": doc.url,
            },
        )
        for i, doc in enumerate(documents, start=1)
    ]


def _series_result(pq: ProcessedQuery) -> dict[str, Any]:
    """List or count the annual-report series, from the catalogue.

    The second half of the document-discovery gap. `resolve` deliberately
    returns no edition for "list of annual reports" — narrowing a series
    question to its newest member would answer something else — and that None
    used to end the matter, so the question fell through to an *unfiltered*
    semantic search and the model was asked to find a list of editions inside
    two pages of report prose. It refused, and the citation fallback attached
    whatever the unfiltered pull had found, which is where the unrelated cards
    came from.

    Deterministic and terminal: the answer is assembled from catalogued titles
    and the resolved editions, there is no model call, and it returns before
    retrieval — so there are no stray blocks for citations to fall back onto.
    """
    series = pq.series
    documents = series.documents
    blocks = _series_blocks(documents)
    from app.retrieval.understanding.annual_report_editions import COUNT

    if series.kind == COUNT:
        noun = "annual report" if len(documents) == 1 else "annual reports"
        answer = f"There are {len(documents)} {noun} available."
    else:
        lines = "\n".join(
            f"- {doc.title or f'Annual Report {doc.edition}'} [{block.n}]"
            for doc, block in zip(documents, blocks)
        )
        answer = (
            f"There are {len(documents)} annual reports available:\n{lines}"
        )

    retrieval_log.note(
        series_lookup=series.kind,
        editions=list(series.editions),
    )
    return {
        "answer": answer,
        "citations": [c.model_dump() for c in build_citations(blocks)],
        # Its own labels, beside `document_lookup`, so the three discovery
        # shapes stay separable in the metrics rather than blurring into one.
        "intent": f"series_{series.kind}",
        "answer_format": pq.answer_format,
        "used_chunks": len(blocks),
        "conflict": False,
        "cached": False,
    }


def _capabilities(pq: ProcessedQuery) -> set[str]:
    """The detected multi-label intents (empty on the passthrough fallback)."""
    if pq.understanding is None:
        return set()
    return {p.label for p in pq.understanding.intents}


# --------------------------------------------------------------------------- #
# Priority pages (app.retrieval.priority)
# --------------------------------------------------------------------------- #
# Two touch points, both behind `priority_pages_enabled` and both imported
# lazily so the package is never loaded with the feature off:
#
# * before routing, the cheap triggers (a person, a page name, a group) — they
#   decide whether a catalog answer may stand for a question the live page owns;
# * before the answer cache, the pages themselves — their content hashes are
#   part of the cache key, so a changed page cannot be answered from yesterday.

#: Reasons that let a page overrule the catalog route. The theme facet and a
#: description match do not: "how many climate projects" is a count the
#: catalog does exactly, whatever page the theme also has.
_CATALOG_OVERRIDING = frozenset({"person", "name", "group"})
#: Page kinds that can. Theme and centre pages list a sample of their projects,
#: so a count about a theme stays with the catalog. The home page overrules it
#: only for a theme listing: the list of themes is the home page's to give, and
#: a listing that names one theme is that theme's page's.
_CATALOG_OVERRIDING_KINDS = frozenset({"people", "page", "profile", "group"})


def _priority_text(question: str, pq: ProcessedQuery) -> str:
    """The question as the priority triggers read it: the user's own words, plus
    the rewrite that folds in the conversation, so a follow-up still names what
    the earlier turn was about."""
    rewritten = (pq.search_query or "").strip()
    if not rewritten or rewritten.lower() == question.strip().lower():
        return question
    return f"{question}\n{rewritten}"


def _priority_theme(pq: ProcessedQuery) -> str | None:
    return getattr(pq.analysis, "theme", None) if pq.analysis is not None else None


def _lists_themes(pq: ProcessedQuery) -> bool:
    """Whether understanding read the question as a request for the list of
    themes, which the home page gives rather than the catalog's theme map."""
    return pq.analysis is not None and getattr(pq.analysis, "operation", None) == "list_themes"


def _priority_targets(question: str, pq: ProcessedQuery) -> list[Any] | None:
    """The deterministic priority triggers, or None with the feature off or for
    a question about one edition of a series (the edition's own PDF answers it)."""
    if not get_settings().priority_pages_enabled or pq.edition is not None:
        return None
    from app.retrieval.priority.evidence import explicit_targets

    try:
        with span("rag.priority_targets") as s:
            targets = explicit_targets(_priority_text(question, pq), theme=_priority_theme(pq),
                                       themes_listing=_lists_themes(pq))
            s.set("targets", len(targets))
        return targets
    except Exception:  # pragma: no cover - defence in depth; the call never raises
        logger.warning("Priority page triggers failed; continuing without them.",
                       exc_info=True)
        return None


def _priority_overrides_catalog(targets: list[Any] | None, *, themes_listing: bool = False) -> bool:
    """Whether a live page owns this question outright, so the catalog route —
    which cannot see these pages — must not answer it instead."""
    for target in targets or ():
        if themes_listing and target.kind == "theme" and target.reason in ("name", "theme"):
            # "tell me about the climate change thematic" reads as a listing of
            # that theme's sub-themes, and the themes are flat: its page answers.
            return True
        if target.reason not in _CATALOG_OVERRIDING:
            continue
        if target.kind == "group" and getattr(target.group, "kind", None) != "centre":
            continue
        if getattr(target.page, "is_home", False) and not themes_listing:
            continue  # "documents in each thematic area" is a catalog count
        if target.kind in _CATALOG_OVERRIDING_KINDS:
            return True
    return False


def _priority_evidence(
    question: str, pq: ProcessedQuery, query_vector: list[float], targets: list[Any] | None
) -> Any | None:
    """The priority pages this question gets, or None with the feature off."""
    if targets is None:
        return None
    from app.retrieval.priority.evidence import gather

    with span("rag.priority_pages") as s:
        evidence = gather(_priority_text(question, pq), query_vector=query_vector,
                          theme=_priority_theme(pq), explicit=targets)
        s.set("pages", len(evidence.reads))
        s.set("blocks", len(evidence.blocks))
    return evidence


def _db_section(
    pq: ProcessedQuery, question: str, history: list[dict[str, str]] | None
) -> str:
    """Deterministic catalog answer to prefix onto a combined (database + content)
    response, reusing the already-extracted slots (no second LLM parse). '' when
    there is nothing to add. Owns its ``rag.db_section`` span so the timing is
    still recorded when this runs off the main thread (see ``_prepare``)."""
    with span("rag.db_section"):
        if pq.analysis is None or not pq.analysis.operation:
            return ""
        from app.retrieval.structured.answerer import answer_structured

        structured = answer_structured(question, history, analysis=pq.analysis)
        return structured["answer"] if structured else ""


def _catalog_listing(pq: ProcessedQuery, question: str) -> dict[str, Any] | None:
    """The catalog's take on a content question retrieval could not ground, or None
    to refuse as before.

    Framed with `NO_CONTENT_WITH_CATALOG` so a list of titles is never mistaken for
    the substance asked for. Fail-open by construction: this runs on a path that is
    already about to refuse, so a catalog error must degrade to that refusal rather
    than turn it into a 500."""
    from app.generation.prompts import NO_CONTENT_WITH_CATALOG
    from app.retrieval.structured.answerer import catalog_fallback

    with span("rag.catalog_fallback") as s:
        try:
            result = catalog_fallback(question, analysis=pq.analysis)
        except Exception:
            logger.warning("Catalog fallback failed; refusing instead.", exc_info=True)
            return None
        s.set("hit", result is not None)
    if result is None:
        return None
    # `intent` stays the question's own: a qa query answered from catalog rows is
    # still a qa query, and relabelling it would distort the intent metrics.
    return {
        **result,
        "answer": f"{NO_CONTENT_WITH_CATALOG}\n\n{result['answer']}",
        "intent": pq.intent,
        "answer_format": pq.answer_format,
    }


def _graph_generation(pq: ProcessedQuery, *, top_k: int) -> _Generation | None:
    """A graph answer for a query the catalog path is about to answer, or None.

    The same graph leg `retriever.retrieve` runs, reached from the one place that
    returns before retrieval. Contract is identical: blocks or nothing, every
    failure is a `None`, and a `None` leaves the caller's existing behaviour
    exactly as it was.

    The query's scope is handed to the policy layer for the same reason the
    retrieval call site hands it over — a scope no template can honour must
    decline the graph rather than silently drop the constraint.
    """
    from app.retrieval.retriever import graph_blocks_for

    blocks = graph_blocks_for(
        pq.search_query, n=top_k, filters=pq.filters, source_type=pq.source_type
    )
    if not blocks:
        return None
    # Embedded here rather than earlier because this path had no need of a vector
    # until now; it is the same call the qa path makes, and it keeps the answer
    # eligible for the semantic cache like any other.
    from app.core.clients.embeddings import embed_query

    with span("rag.embed_query"):
        query_vector = embed_query(pq.search_query)
    return _Generation(
        pq=pq, blocks=blocks, query_vector=query_vector, top_k=top_k
    )


def _prepare(
    question: str,
    *,
    history: list[dict[str, str]] | None,
    top_k: int | None,
) -> tuple[dict[str, Any] | None, _Generation | None]:
    """Shared front-matter for both answer entrypoints.

    Returns ``(result, None)`` when a complete answer is already available (a
    response- or semantic-cache hit, chit-chat, a structured lookup, or a
    no-context refusal), or ``(None, generation)`` when a grounded answer still
    has to be generated.
    """
    from app.cache import semantic_cache
    from app.core.clients.embeddings import embed_query

    settings = get_settings()
    n = top_k or settings.retrieval_top_k

    with span("rag.query_understanding"):
        pq: ProcessedQuery = process(question, history)
    _trace_understanding(pq, top_k=n)
    # Ahead of the chitchat branch, because that is exactly where an unclear
    # question used to end up: `clarification_needed` is terminal, so
    # `_legacy_intent_and_format` collapses it onto chitchat and the small-talk
    # prompt answers a question it was never given. `process` only sets this when
    # the feature is on and no clarification is already open (see
    # `app.retrieval.understanding.clarify.pending`), so with the flag off this
    # is always None and the line below is unreachable.
    if pq.clarification is not None:
        return _clarification_result(
            pq.clarification, answer_format=pq.answer_format
        ), None
    if pq.intent == "chitchat":
        return _empty("chitchat", chitchat(question, history)), None

    # A request for the annual-report series itself — list it, or count it.
    # Terminal *before* retrieval, unlike the single-document lookup below:
    # that one waits for retrieval because it needs the catalogued title on a
    # retrieved chunk, whereas the series carries its own titles. Returning here
    # is also what removes the unrelated source cards — retrieval never runs, so
    # there is nothing for the citation fallback to pick up.
    #
    # Ahead of the structured branch on purpose. Which route the classifier chose
    # does not change what "list of annual reports" is asking for, and the
    # catalogue route cannot answer it anyway: `list_records` is scoped to
    # website nodes and the editions are attachments.
    if pq.series is not None:
        with span("rag.series_lookup") as s:
            result = _series_result(pq)
            s.set("kind", pq.series.kind)
            s.set("editions", len(pq.series.documents))
        return result, None

    caps = _capabilities(pq)
    # A query that needs both catalog facts and document content: keep the
    # deterministic catalog answer and prefix it onto the grounded content answer.
    combined = "database" in caps and bool(caps & _CONTENT_CAPS)
    chained = False
    # Whether the catalog has already been asked about this query, so the
    # empty-retrieval fallback at the end doesn't re-run a query that just came
    # back with nothing. `combined` asks it via `_db_section` below.
    #
    # Deliberately not set for a scoped_summary that fell through: it returns None
    # both for a scope-less request and for one whose documents held no summarizable
    # text, and in the latter case a listing of those documents is exactly what is
    # worth showing. When the scope was genuinely empty the listing comes back empty
    # too, so the redundant case costs one query on a path that is already refusing.
    db_consulted = combined

    # The priority pages' cheap triggers, ahead of routing: a question naming a
    # page the catalog cannot see ("who is on the governing council", "latest
    # tenders") must reach that page rather than be answered from the catalog.
    priority_targets = _priority_targets(question, pq)
    catalog_route = pq.intent == "structured" and not _priority_overrides_catalog(
        priority_targets, themes_listing=_lists_themes(pq)
    )

    if catalog_route:
        from app.retrieval.structured.answerer import answer_structured
        from app.retrieval.structured.tools import resolve_lookup_chain

        chain_id = resolve_lookup_chain(pq.analysis, question)
        if chain_id is not None:
            # Content question about one named title: answer from that document's
            # chunks (QA path below) instead of title+URL.
            from qdrant_client.models import FieldCondition, MatchValue

            pq.filters.append(
                FieldCondition(key="document_id", match=MatchValue(value=chain_id))
            )
            chained = True
            # The chain came out of a catalog title lookup, so the catalog has
            # already placed this document; only its content is missing.
            db_consulted = True
        elif not combined:
            # Database-only: the deterministic catalog answer is complete.
            structured = answer_structured(question, history, analysis=pq.analysis)
            db_consulted = True
            if structured is not None:
                # ...unless the graph can answer the same question as a
                # relationship. The graph leg lives inside `retriever.retrieve`,
                # which this return never reaches, so a relational question that
                # understanding labelled `structured` used to bypass the graph
                # entirely — measured at 4 of 14 graph-answerable benchmark
                # questions, and every one of them the "which projects did PERSON
                # lead" shape the graph's query-side resolution exists to serve.
                # The catalog answered those with "'projects' matches more than
                # one content type", while the graph held the 14 rows.
                #
                # Attempted only on the branch that would otherwise return here,
                # so the graph is still tried exactly once per query: when
                # `answer_structured` returns None the fall-through reaches
                # `retrieve` and the graph leg there is untouched.
                graph = _graph_generation(pq, top_k=n)
                if graph is not None:
                    return None, graph
                structured.setdefault("answer_format", pq.answer_format)
                return structured, None

    if pq.intent == "scoped_summary":
        from app.pipeline.summarize import summarize_scope

        with span("rag.scoped_summary"):
            summary = summarize_scope(pq.analysis)
        if summary is not None:
            summary.setdefault("answer_format", pq.answer_format)
            return summary, None
        # Empty/unresolvable scope: fall through to plain semantic QA.

    with span("rag.embed_query"):
        query_vector = embed_query(pq.search_query)
    # Read before the cache: an answer built on a live page is only reusable
    # while that page still says the same thing, so its content is in the key.
    priority = _priority_evidence(question, pq, query_vector, priority_targets)
    fingerprint = semantic_cache.facet_fingerprint(pq)
    if priority is not None:
        fingerprint = {**fingerprint, **priority.fingerprint()}
    with span("rag.semantic_cache") as s:
        semantic = semantic_cache.lookup(
            query_vector, top_k=n, answer_format=pq.answer_format,
            fingerprint=fingerprint,
        )
        s.set("hit", semantic is not None)
    if semantic is not None:
        # A cached combined answer already carries its catalog section, so the
        # short-circuit here also skips rebuilding it.
        return {**semantic, "cached": True}, None

    def _run_retrieve(subqueries: list[Any]) -> list[ContextBlock]:
        # `priority` only when the feature is on, so with it off the call is
        # exactly the one it always was.
        extra = {"priority": priority} if priority is not None else {}
        return retrieve(
            pq.search_query,
            filters=pq.filters,
            n=n,
            query_vector=query_vector,
            answer_format=pq.answer_format,
            source_type=pq.source_type,
            capabilities=caps,
            temporal=_temporal_intent(pq),
            subqueries=subqueries,
            **extra,
        )

    # The deterministic catalog section (combined queries only), the answer
    # plan's requirement extraction, and content retrieval share no data until
    # all three are done, so overlap them and pay the slowest rather than the
    # sum. copy_context() keeps each worker's span in this request's stage
    # breakdown. Requirement extraction runs for every query that reaches this
    # point (it is a no-op — see `answer_plan.build_plan` — for the ordinary
    # single-part question), needing only the search query, not the blocks.
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from app.generation.answer_plan import build_plan, extract_requirements, plan_directive

    with ThreadPoolExecutor(max_workers=2) as pool:
        requirements_future = pool.submit(
            copy_context().run, extract_requirements, pq.search_query
        )
        # Sub-query planning is the one thing that needs the requirements
        # *before* retrieval rather than after it, so with the feature on the
        # extraction joins the critical path instead of overlapping it. Off — the
        # default — nothing waits here and the two still run concurrently exactly
        # as they did. `Future.result()` caches, so the join below is free either
        # way. The plan is built here rather than inside `retrieve` because it is
        # derived from `app.generation.answer_plan`, which retrieval may not
        # import (see tests/test_architecture.py).
        subqueries = _subquery_plan(pq, requirements_future)
        if combined and not chained:
            db_future = pool.submit(
                copy_context().run, _db_section, pq, question, history
            )
            blocks = _run_retrieve(subqueries)
            db_prefix = db_future.result()
        else:
            db_prefix = ""
            blocks = _run_retrieve(subqueries)
        requirements = requirements_future.result()

    if not blocks:
        # Combined query whose content retrieval came up empty: still return the
        # deterministic catalog answer rather than a blanket refusal.
        if db_prefix:
            return _empty(pq.intent, db_prefix, answer_format=pq.answer_format), None
        # Retrieval found nothing to ground an answer. Ask the catalog — which
        # indexes titles and facets rather than passages — but only when it hasn't
        # already answered nothing for this query.
        if not db_consulted:
            listing = _catalog_listing(pq, question)
            if listing is not None:
                return listing, None
        return _empty(pq.intent, REFUSAL, answer_format=pq.answer_format), None

    # A request for the document itself, and the document is already resolved.
    # Terminal here rather than in the structured branch above, because that is
    # where the identity becomes answerable: retrieval has just supplied the
    # catalogued title and the blocks the source cards are built from, so the
    # answer and its citations describe the same document by construction and no
    # second lookup is needed. Placed after the empty-retrieval paths, so an
    # edition that resolved but matched no chunks keeps its existing behaviour.
    if pq.edition is not None and is_document_request(question):
        with span("rag.document_lookup") as s:
            result = _document_result(pq, blocks)
            s.set("edition", pq.edition.edition)
            s.set("kind", pq.edition.kind)
        return result, None

    with span("rag.answer_plan") as s:
        plan = build_plan(requirements, blocks)
        directive = plan_directive(plan)
        s.set("requirements", len(plan.requirements))
        s.set("unsupported", len(plan.unsupported))

    return None, _Generation(
        pq=pq, blocks=blocks, query_vector=query_vector,
        top_k=n, db_prefix=db_prefix, plan_directive=directive,
        # Recomputed after retrieval, which may have read further pages whose
        # stored copies it ranked.
        cache_fingerprint=(
            {**semantic_cache.facet_fingerprint(pq), **priority.fingerprint()}
            if priority is not None else None
        ),
    )


def _cited_blocks(body: str, blocks: list[ContextBlock]) -> list[ContextBlock]:
    """The blocks the answer actually cites.

    The sources footer lists what the answer used, not everything retrieval
    pulled: a block the answer left out — an off-topic PDF the model rightly
    dropped, say — must not resurface as a chip contradicting the answer above
    it. Falls back to every block when the answer cites nothing (or cites only
    blocks that are somehow absent), so provenance is never silently lost.
    """
    from app.generation.faithfulness import extract_markers

    cited = extract_markers(body)
    if not cited:
        return blocks
    return [b for b in blocks if b.n in cited] or blocks


def _assemble(answer: str, gen: _Generation) -> dict[str, Any]:
    from app.generation import faithfulness
    from app.generation.sections import strip_tags

    # The block wrappers are presentation, so every pass that reads the answer as
    # content works from the tag-free body.
    body = strip_tags(answer)
    # Deterministic numeric check (~0 ms): observe-only in v1 — flagged and
    # logged, never auto-corrected.
    mismatches = faithfulness.numeric_mismatches(body, gen.blocks)
    if mismatches:
        logger.info("Numeric claims not found in cited blocks: %s", mismatches)
    # The catalog section is deterministic (not from the blocks), so faithfulness
    # and numeric checks run on the grounded content only; compose for display.
    final = f"{gen.db_prefix}\n\n{answer}" if gen.db_prefix else answer
    return {
        "answer": final,
        "citations": [
            c.model_dump() for c in build_citations(_cited_blocks(body, gen.blocks))
        ],
        "intent": gen.pq.intent,
        "answer_format": gen.pq.answer_format,
        "used_chunks": len(gen.blocks),
        "conflict": any(b.conflict for b in gen.blocks),
        "numeric_mismatch": bool(mismatches),
        "cached": False,
    }


def _persist(gen: _Generation, result: dict[str, Any]) -> None:
    from app.cache import semantic_cache

    with span("rag.semantic_cache_store"):
        semantic_cache.store(
            gen.query_vector, result, top_k=gen.top_k,
            answer_format=gen.pq.answer_format,
            fingerprint=(
                gen.cache_fingerprint if gen.cache_fingerprint is not None
                else semantic_cache.facet_fingerprint(gen.pq)
            ),
        )


def _record(
    span_ctx: Any,
    result: dict[str, Any],
    stages: dict[str, float] | None = None,
    trace: Any = None,
) -> None:
    # `trace` is passed explicitly because this runs after the first streamed
    # token, and the SSE driver resumes the generator in a fresh context where
    # the active-trace ContextVar is unset (see app.api.chat._sse). Without it
    # every streamed query recorded an empty outcome.
    retrieval_log.note_outcome(
        trace,
        intent=result.get("intent"),
        answer_format=result.get("answer_format"),
        cached=result.get("cached", False),
        used_chunks=result.get("used_chunks", 0),
        citations=len(result.get("citations") or []),
        conflict=result.get("conflict", False),
        numeric_mismatch=result.get("numeric_mismatch", False),
        answered=result.get("answer") != REFUSAL,
        answer_chars=len(result.get("answer") or ""),
        latency_ms=round(span_ctx.elapsed_ms, 1),
    )
    record_query_metrics(
        latency_ms=span_ctx.elapsed_ms,
        intent=result.get("intent"),
        used_chunks=result.get("used_chunks", 0),
        has_citations=bool(result.get("citations")),
        answered=result.get("answer") != REFUSAL,
        conflict=result.get("conflict", False),
        cached=result.get("cached", False),
        numeric_mismatch=result.get("numeric_mismatch") or None,  # logged only when set
        components=component_totals(stages) if stages else None,
        stages={k: round(v, 1) for k, v in stages.items()} if stages else None,
    )


def _stream_result(result: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Emit a ready-made result dict (cache hit, chit-chat, structured lookup, or
    refusal) as the standard token / sources / done SSE event sequence."""
    yield {"type": "token", "text": result.get("answer", "")}
    sources = {
        "type": "sources",
        "citations": result.get("citations", []),
        "intent": result.get("intent", "qa"),
        "answer_format": result.get("answer_format", "default"),
        "used_chunks": result.get("used_chunks", 0),
        "conflict": result.get("conflict", False),
        "numeric_mismatch": result.get("numeric_mismatch", False),
    }
    # Additive and absent on every other path, so a client that has never heard
    # of clarification sees the event it has always seen. The question and its
    # options are already in the answer text above; this is the same thing
    # structured, for a client that wants to render the options as buttons.
    clarification = result.get("clarification")
    if clarification:
        sources["clarification"] = clarification
    yield sources
    yield {"type": "done"}


def stream_answer(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    top_k: int | None = None,
) -> Iterator[dict[str, Any]]:
    # Spans after the first yield only reach the global aggregates, not this
    # dict — the SSE driver resumes the generator in fresh contexts (see
    # metrics.collect_into) — so the logged breakdown covers the pre-token
    # stages, which is where retrieval time goes.
    stages: dict[str, float] = {}
    with retrieval_log.query_log(
        question,
        entrypoint="chat.stream",
        top_k=top_k,
        history=history,
        # Shared by reference: the trace reports the same per-stage breakdown
        # the `rag_metrics` line does, rather than a second measurement of it.
        stages=stages,
    ) as trace, collect_into(stages), span("rag.stream_answer") as s:
        result, gen = _prepare(question, history=history, top_k=top_k)
        if gen is not None:
            # The rendered context, not just the blocks: `format_context_blocks`
            # is what generation interpolates into the prompt (block headers,
            # source hints, the website/PDF group headings), and it is the same
            # string for the same blocks — so the trace holds what the model was
            # sent rather than a reconstruction of it. Passed as a callable so a
            # disabled trace never pays for the join.
            from app.generation.prompts import format_context_blocks

            retrieval_log.note_context(
                gen.blocks, rendered=lambda: format_context_blocks(gen.blocks)
            )
            retrieval_log.note(
                db_prefix_chars=len(gen.db_prefix),
                plan_directive=bool(gen.plan_directive),
            )
        # Cache hit, chit-chat, structured lookup, or refusal — already complete.
        if result is not None:
            yield from _stream_result(result)
            _record(s, result, stages, trace)
            return

        from app.generation import date_claims, faithfulness
        from app.generation.sections import strip_tags

        parts: list[str] = []
        # Combined answer: stream the deterministic catalog section first, then
        # the grounded content answer.
        if gen.db_prefix:
            yield {"type": "token", "text": gen.db_prefix + "\n\n"}
        for token in generate_stream(
            gen.pq.search_query, gen.blocks,
            history=history, answer_format=gen.pq.answer_format,
            plan_directive=gen.plan_directive,
        ):
            parts.append(token)
            yield {"type": "token", "text": token}
        answer = faithfulness.validate_markers("".join(parts), len(gen.blocks))

        if get_settings().faithfulness_check:
            # Post-hoc verify: tokens streamed at full speed above; an
            # unfaithful answer gets one regeneration emitted as a correction
            # event, and the corrected version is what gets cached below.
            with span("rag.faithfulness") as fs:
                report = faithfulness.verify(strip_tags(answer), gen.blocks)
                fs.set("faithful", report.faithful)
            if not report.faithful:
                logger.info("Streamed answer flagged unfaithful; correcting once.")
                try:
                    retry = generate_answer(
                        gen.pq.search_query, gen.blocks,
                        history=history,
                        correction=report.correction_note(),
                        answer_format=gen.pq.answer_format,
                        plan_directive=gen.plan_directive,
                    )
                    corrected = faithfulness.validate_markers(retry, len(gen.blocks))
                except Exception:
                    logger.warning("Correction regeneration failed; keeping "
                                   "the streamed answer.", exc_info=True)
                    corrected = ""
                if corrected and corrected != answer:
                    answer = corrected
                    yield {
                        "type": "correction",
                        "text": f"{gen.db_prefix}\n\n{corrected}" if gen.db_prefix else corrected,
                        "reason": "faithfulness",
                    }

        # Deterministic publication-date guard. Runs whatever the faithfulness
        # setting: this failure is a specific false claim, not a judgement call,
        # and it survived two rounds of prompt work (4/6 sampled answers still
        # dated the 2024-25 report to the page date). One regeneration, then a
        # mechanical rewrite - the claim must not reach the reader.
        date_report = date_claims.verify_date_claims(strip_tags(answer), gen.blocks)
        if not date_report.clean:
            logger.info(
                'Answer dated a document by its page; correcting once (%d claim(s)).',
                len(date_report.offenders),
            )
            corrected = ''
            try:
                retry = generate_answer(
                    gen.pq.search_query, gen.blocks,
                    history=history,
                    correction=date_report.correction_note(),
                    answer_format=gen.pq.answer_format,
                    plan_directive=gen.plan_directive,
                )
                corrected = faithfulness.validate_markers(retry, len(gen.blocks))
            except Exception:
                logger.warning('Date-claim regeneration failed; falling back to the mechanical rewrite.', exc_info=True)
            recheck = (date_claims.verify_date_claims(strip_tags(corrected), gen.blocks)
                       if corrected else date_report)
            if corrected and recheck.clean:
                answer = corrected
                reason = 'date_claim'
            else:
                # The retry repeated the claim (or never arrived): replace the
                # offending sentences outright.
                answer = date_claims.safe_rewrite(answer, recheck)
                reason = 'date_claim_fallback'
            corrected_text = (
                gen.db_prefix + "\n\n" + answer if gen.db_prefix else answer
            )
            yield {
                "type": "correction",
                "text": corrected_text,
                "reason": reason,
            }

        result = _assemble(answer, gen)
        s.set("answer_chars", len(answer))
        yield {
            "type": "sources",
            "citations": result["citations"],
            "intent": result["intent"],
            "answer_format": result["answer_format"],
            "used_chunks": result["used_chunks"],
            "conflict": result["conflict"],
            "numeric_mismatch": result["numeric_mismatch"],
        }
        yield {"type": "done"}
        _persist(gen, result)
        _record(s, result, stages, trace)


def search_blocks(
    question: str,
    *,
    history: list[dict[str, str]] | None = None,
    top_k: int | None = None,
) -> dict[str, Any]:
    with retrieval_log.query_log(
        question, entrypoint="search", top_k=top_k, history=history
    ):
        return _search_blocks(question, history=history, top_k=top_k)


def _search_blocks(
    question: str,
    *,
    history: list[dict[str, str]] | None,
    top_k: int | None,
) -> dict[str, Any]:
    pq = process(question, history)
    _trace_understanding(pq, top_k=top_k or get_settings().retrieval_top_k)
    # The inspection endpoint shows what generation would be given, so it reads
    # the same priority pages the answer path would.
    extra: dict[str, Any] = {}
    targets = _priority_targets(question, pq)
    if targets is not None:
        from app.core.clients.embeddings import embed_query

        query_vector = embed_query(pq.search_query)
        extra = {"query_vector": query_vector,
                 "priority": _priority_evidence(question, pq, query_vector, targets)}
    blocks = retrieve(
        pq.search_query,
        filters=pq.filters, n=top_k, answer_format=pq.answer_format,
        source_type=pq.source_type, capabilities=_capabilities(pq),
        temporal=_temporal_intent(pq),
        **extra,
    )
    retrieval_log.note_context(blocks)
    retrieval_log.note_outcome(
        intent=pq.intent, answer_format=pq.answer_format,
        used_chunks=len(blocks), answered=bool(blocks),
    )
    return {
        "intent": pq.intent,
        "answer_format": pq.answer_format,
        "search_query": pq.search_query,
        "intents": [
            {"label": p.label, "confidence": p.confidence, "rationale": p.rationale}
            for p in (pq.understanding.intents if pq.understanding else [])
        ],
        "is_ambiguous": pq.is_ambiguous,
        "blocks": [
            {
                "n": b.n,
                "score": round(b.score, 4),
                "conflict": b.conflict,
                "superseded": b.superseded,
                "supersedes": b.supersedes,
                "text": b.text,
                "document_id": b.payload.get("document_id"),
                "source_type": b.payload.get("source_type"),
                "title": b.payload.get("title"),
                "source_url": b.payload.get("source_url"),
                "page_number": b.payload.get("page_number"),
                "section_heading": b.payload.get("section_heading"),
            }
            for b in blocks
        ],
    }
