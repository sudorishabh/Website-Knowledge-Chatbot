"""Database Planner: turn extracted query slots into a validated tool plan.

v1 is deterministic — it maps the operation + facets the intent layer already
extracted onto a single tool call. The interface is ready for a v2 LLM planner
that emits several calls; `execute` already runs a plan's independent calls in
parallel. See docs/database-planner-architecture.md.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.dates import IsoDate, current_date_directive, exclusive_end
from app.retrieval.understanding.catalog_prompt import (
    BEHAVIOR,
    BUNDLE_GLOSSARY,
    BUNDLE_LIST,
    COLLECTIVE_WORD_WARNING,
    FEW_SHOTS,
    OPERATIONS,
    RESOLVE_FIRST,
    VOCABULARY,
    catalog_coverage_directive,
    catalog_inventory_directive,
)
from app.retrieval.structured.tools import (
    THEME_VOCABULARY_LIMIT,
    aggregate_records,
    count_records,
    list_records,
    list_themes,
    lookup_record,
    resolve_entity,
)
from app.retrieval.structured import detail as structured_detail
from app.retrieval.structured import theme_scope
from app.retrieval.structured import topic
from app.retrieval.structured.types import (
    DatabasePlan,
    RecordFilters,
    ResolveType,
    ToolCall,
    ToolName,
    ToolResult,
)

logger = logging.getLogger(__name__)


def _year_dates(year: Any) -> tuple[str | None, str | None]:
    """Calendar-year bounds for the parse fallback's `year` shorthand (the unified
    analysis emits explicit dates, but parse_structured may set only a year)."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None, None
    return f"{y:04d}-01-01", f"{y + 1:04d}-01-01"


def _theme_group_for(question: str | None, *, applies: bool = True) -> str | None:
    """The `theme_group` a question's counts should be restricted to.

    ``applies`` is whether a theme restriction belongs on this query at all.
    Themes scope a question that concerns themes; imposing them on one that does
    not is a silent narrowing, because the filter is a join against
    `documents_theme` and a document carrying no theme then disappears. Measured
    when that was the behaviour: "how many authors are there?" answered 876
    instead of 955, and a plain document count lost 2,620 untagged documents.

    "all" means no restriction rather than a third group, so an explicit request
    for everything counts across main, other and the themes the map has not
    classified — which is what "every theme" means.
    """
    if not applies:
        return None
    scope = theme_scope.detect(question)
    return None if scope == theme_scope.SCOPE_ALL else scope


def _applied_theme(requested: str | None) -> str | None:
    """The theme that will survive resolution, or None if it will be dropped.

    Asks the resolver the same question the tools will ask it, so planning and
    execution agree on which facets are actually doing work. Resolution failures
    return None, which only ever *widens* the residual — the safe direction,
    since an over-constrained list falls through to semantic retrieval while an
    under-constrained one answers wrongly.
    """
    if not requested:
        return None
    try:
        from app.retrieval.structured.filters import resolve_theme

        resolved = resolve_theme(requested)
    except Exception:
        logger.debug("Theme resolution unavailable at plan time.", exc_info=True)
        return None
    return requested if resolved and topic.faithful_theme(requested, resolved) else None


def _tool_call(
    slots: Any, output_format: str, question: str | None = None
) -> ToolCall:
    """Map a slots object (analysis or StructuredQuery — duck-typed on operation,
    bundle, theme, author, title_contains, group_by, date_from, date_to, limit,
    and optionally year/tags) to one tool call.

    `tags` (plural, a list — the query-understanding classifier already extracts
    it for the qa/vector path) maps to `RecordFilters.tag` (singular, the only
    tag scope the catalog tools support today) by taking the first tag; there is
    no multi-tag AND/OR support to map the rest onto.

    `question` is the raw text, used only to decide which theme groups a
    theme listing may expose (see `theme_scope`). Optional so an existing
    caller that has only slots keeps working — it then gets the Main-only
    default, which is the safe side."""
    date_from = getattr(slots, "date_from", None)
    date_to = getattr(slots, "date_to", None)
    if not date_from and not date_to:
        date_from, date_to = _year_dates(getattr(slots, "year", None))
    tags = getattr(slots, "tags", None)
    operation = getattr(slots, "operation", None) or "list"
    # A theme restriction belongs on a query that concerns themes: one whose
    # answer is broken down or counted *by* theme, or one whose wording is about
    # themes. On anything else it would silently drop every untagged document.
    themed = (
        operation == "list_themes"
        or "theme" in (
            getattr(slots, "group_by", None),
            getattr(slots, "secondary_group_by", None),
            getattr(slots, "count_of", None),
        )
        or theme_scope.mentions_themes(question)
    )
    # Subject matter the facets above do not account for. A list is only
    # trustworthy when nothing topical is left unconstrained, so whatever remains
    # becomes an explicit constraint on the rows rather than being dropped (see
    # `app.retrieval.structured.topic`). Computed for the row-returning
    # operations only: a count or a distribution is *about* the facets, and
    # narrowing it by title words would answer a different question.
    topic_terms: tuple[str, ...] = ()
    if (question and operation in ("list", "lookup")
            and topic.enabled()):
        topic_terms = tuple(topic.residual_topic(
            question,
            bundle=getattr(slots, "bundle", None),
            # The *applied* theme, not the requested one. A theme the resolver
            # will drop for being broader than the question covers nothing, and
            # crediting it would leave the question's real subject unconstrained
            # — which is exactly how "reports on climate change adaptation"
            # became "the two most recent reports".
            theme=_applied_theme(getattr(slots, "theme", None)),
            tag=tags[0] if tags else None,
            author=getattr(slots, "author", None),
            title_contains=getattr(slots, "title_contains", None),
        ))
    filters = RecordFilters(
        topic_terms=topic_terms,
        theme=getattr(slots, "theme", None),
        # The same rule the theme *listing* follows, applied to counts: a
        # question that names no theme is answered over the main structure
        # unless it asks otherwise. `resolve_filters` drops this whenever a
        # theme was named, so a named Other theme stays countable.
        theme_group=_theme_group_for(question, applies=themed),
        tag=tags[0] if tags else None,
        author=getattr(slots, "author", None),
        title_contains=getattr(slots, "title_contains", None),
        date_from=date_from,
        date_to=date_to,
    )
    bundle = getattr(slots, "bundle", None)
    limit = getattr(slots, "limit", 10) or 10
    if operation == "count":
        return ToolCall(tool="count_records", entity=bundle, filters=filters,
                        count_of=getattr(slots, "count_of", None) or "records",
                        output_format=output_format)
    if operation == "distribution":
        return ToolCall(tool="aggregate_records", entity=bundle, filters=filters,
                        group_by=getattr(slots, "group_by", None),
                        secondary_group_by=getattr(
                            slots, "secondary_group_by", None),
                        output_format=output_format)
    if operation == "lookup":
        return ToolCall(tool="lookup_record", entity=bundle, filters=filters,
                        title=getattr(slots, "title_contains", None), limit=limit,
                        output_format=output_format)
    if operation == "list_themes":
        # Vocabulary-wide, and explicitly NOT the content-row `limit` above —
        # that defaults to 10, which would truncate the vocabulary and report a
        # wrong total.
        return ToolCall(tool="list_themes", filters=filters,
                        theme_scope=theme_scope.detect(question),
                        limit=THEME_VOCABULARY_LIMIT, output_format=output_format)
    return ToolCall(tool="list_records", entity=bundle, filters=filters, limit=limit,
                    output_format=output_format)


def plan(
    slots: Any, *, output_format: str = "default", question: str | None = None
) -> DatabasePlan:
    """v1 deterministic plan: one tool call derived from the extracted slots."""
    call = _tool_call(slots, output_format, question)
    return DatabasePlan(
        calls=[call], rationale=f"{call.tool} for {call.entity or 'all content'}"
    )


# ── v2: LLM multi-call planner ───────────────────────────────────────────────
# Opt-in via settings.database_multi_call_enabled. Decomposes a compound catalog
# question into several tool calls; any failure returns None so answer_structured
# falls back to the deterministic v1 `plan` above. `execute` already runs the
# calls in parallel.

_MAX_CALLS = 4


class _PlannedCall(BaseModel):
    """One LLM-planned tool call. Mirrors the fields of `ToolCall` that a planner
    can set from natural language; unset fields fall back to tool defaults.

    `ToolCall.offset` is deliberately absent: paging needs a notion of "the next
    page" that this pipeline has no conversation state for, and a hallucinated
    offset silently hides rows rather than failing visibly. It stays settable on
    `ToolCall` for a programmatic caller that is genuinely paging."""

    tool: ToolName
    entity: str | None = Field(
        default=None, description="Content type / bundle, or null to span all content."
    )
    theme: str | None = None
    author: str | None = None
    title_contains: str | None = None
    date_from: IsoDate = Field(
        default=None, description="First date to include (YYYY-MM-DD)."
    )
    date_to_inclusive: IsoDate = Field(
        default=None,
        description=(
            "LAST date to include (YYYY-MM-DD) — copy the end date from the "
            "request as-is; do not add a day to make it exclusive."
        ),
    )
    group_by: Literal["theme", "content_type", "author", "year"] | None = None
    secondary_group_by: Literal[
        "theme", "content_type", "author", "year"
    ] | None = Field(
        default=None,
        description=(
            "A second grouping dimension, for \"which X does which Y\" — the "
            "answer is pairs. Null for an ordinary per-X breakdown."
        ),
    )
    count_of: Literal[
        "records", "theme", "content_type", "author", "year"
    ] = Field(
        default="records",
        description=(
            "What count_records counts. \"records\" (default) counts documents; "
            "name a facet to count its distinct values instead — \"how many "
            "authors work on X\" is count_of=author, not a document count."
        ),
    )
    title: str | None = None
    limit: int = 10
    fields: list[str] | None = Field(
        default=None,
        description=(
            "For list_records: which metadata keys to return per item, from "
            "title, url, effective_start_date, bundle, document_id. Null returns all — "
            "only set it when the user asks for specific fields."
        ),
    )
    # resolve_entity
    query: str | None = Field(
        default=None, description="Free text to resolve, for resolve_entity."
    )
    resolve_type: ResolveType | None = None

    @property
    def date_to(self) -> str | None:
        """Exclusive upper bound derived from the inclusive end (see
        `QueryScope.date_to`)."""
        return exclusive_end(self.date_to_inclusive)


class _MultiPlan(BaseModel):
    calls: list[_PlannedCall] = Field(default_factory=list)
    rationale: str = ""


_PLANNER_SYSTEM = (
    "You plan database queries over a content repository of " + BUNDLE_LIST + ".\n"
    "Decompose the request into one or more catalog tool calls. Emit MULTIPLE "
    "calls for a genuinely compound request — a comparison across periods, "
    "themes, or content types ('reports in 2023 vs 2024'), a count paired with "
    "a list — or when a name needs resolving before the real query (see "
    "resolve_entity below). A simple request naming an exact bundle is a "
    "single call.\n"
    "Tools:\n"
    "- resolve_entity: resolve a free-text author/bundle/theme name to a "
    "canonical id before filtering by it.\n"
    "- count_records: how many items match.\n"
    "- list_records: list matching items (most recent first).\n"
    "- lookup_record: find one item by title.\n"
    "- aggregate_records: counts grouped by group_by "
    "(theme / content_type / author / year).\n"
    "- list_themes: list the themes/topics the collection covers (takes no filters).\n"
    # Glossary before the vocabulary block, which refers back to the everyday
    # words listed "above" for each type.
    + BUNDLE_GLOSSARY + "\n"
    + VOCABULARY + "\n"
    + RESOLVE_FIRST + "\n"
    + OPERATIONS + "\n"
    + BEHAVIOR + "\n"
    "Set only the fields that apply. Dates are the first and last day to include "
    "(date_from / date_to_inclusive), copied from the request without adding or "
    "subtracting a day. entity is one of the listed content types, or null for all. "
    + COLLECTIVE_WORD_WARNING + "\n"
    + FEW_SHOTS
)


def _to_tool_call(
    call: _PlannedCall, output_format: str, question: str | None = None
) -> ToolCall:
    # A vocabulary enumeration must not inherit the LLM's content-row limit
    # (which it habitually leaves at 10) — see THEME_VOCABULARY_LIMIT.
    limit = THEME_VOCABULARY_LIMIT if call.tool == "list_themes" else (call.limit or 10)
    return ToolCall(
        tool=call.tool,
        entity=call.entity,
        filters=RecordFilters(
            theme=call.theme,
            # Not an LLM-settable field: the group restriction comes from
            # the question text by the same rule the v1 planner uses, and
            # only when the query concerns themes at all.
            theme_group=_theme_group_for(
                question,
                applies=(
                    call.tool == "list_themes"
                    or "theme" in (call.group_by, call.secondary_group_by,
                                   call.count_of)
                    or theme_scope.mentions_themes(question)
                ),
            ),
            author=call.author,
            title_contains=call.title_contains,
            date_from=call.date_from,
            date_to=call.date_to,
        ),
        group_by=call.group_by,
        secondary_group_by=call.secondary_group_by,
        count_of=call.count_of,
        title=call.title or call.title_contains,
        limit=limit,
        fields=call.fields or None,
        output_format=output_format,
        query=call.query,
        resolve_type=call.resolve_type,
        # Not a field the LLM may set: which theme groups are exposed is
        # decided from the question text by the same deterministic rule the
        # v1 planner uses, so both planners answer a generic theme question
        # identically.
        theme_scope=theme_scope.detect(question),
    )


def plan_multi(question: str, *, output_format: str = "default") -> DatabasePlan | None:
    """v2 LLM planner: decompose a question into up to ``_MAX_CALLS`` tool calls.

    Returns None on any failure or an empty plan so the caller can fall back to
    the deterministic :func:`plan`."""
    from app.core.clients.llm import get_structured_llm

    try:
        model = get_structured_llm().with_structured_output(_MultiPlan)
        result: _MultiPlan = model.invoke(
            [
                (
                    "system",
                    _PLANNER_SYSTEM
                    + catalog_inventory_directive()
                    + catalog_coverage_directive()
                    + current_date_directive(),
                ),
                ("human", question),
            ]
        )
    except Exception:
        logger.warning("Multi-call planning failed; falling back to v1.", exc_info=True)
        return None
    calls = [
        _to_tool_call(c, output_format, question)
        for c in result.calls[:_MAX_CALLS]
    ]
    if not calls:
        return None
    return DatabasePlan(calls=calls, rationale=result.rationale or "multi-call plan")


def _run(call: ToolCall, question: str | None) -> ToolResult:
    detail = call.detail and structured_detail.enabled()
    if call.tool == "count_records":
        # The question decides whether a zero under a title substring is the
        # answer or a guess to fall through on (see tools._title_guess_zero).
        return count_records(call.entity, call.filters, question=question,
                             count_of=call.count_of, detail=detail)
    if call.tool == "list_records":
        return list_records(call.entity, call.filters, sort=call.sort,
                            limit=call.limit, offset=call.offset,
                            output_format=call.output_format, fields=call.fields)
    if call.tool == "lookup_record":
        return lookup_record(call.entity, call.title, call.filters, limit=call.limit,
                             output_format=call.output_format, question=question)
    if call.tool == "aggregate_records":
        return aggregate_records(call.entity, call.group_by, call.filters,
                                 secondary_group_by=call.secondary_group_by,
                                 aggregation=call.aggregation,
                                 output_format=call.output_format, detail=detail)
    if call.tool == "list_themes":
        return list_themes(scope=call.theme_scope, limit=call.limit,
                           output_format=call.output_format)
    if call.tool == "resolve_entity":
        return resolve_entity(call.query, call.resolve_type)
    return ToolResult(tool=call.tool, entity=call.entity, ok=False,
                      error=f"unknown tool {call.tool!r}")


def execute(db_plan: DatabasePlan, *, question: str | None = None) -> list[ToolResult]:
    """Execute a plan's tool calls; independent calls run in parallel. Each tool is
    fail-open, so partial failures surface as ok=False results, never raises."""
    calls = db_plan.calls
    if not calls:
        return []
    if len(calls) == 1:
        return [_run(calls[0], question)]
    with ThreadPoolExecutor(max_workers=min(len(calls), 4)) as pool:
        return list(pool.map(lambda call: _run(call, question), calls))
