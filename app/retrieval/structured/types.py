"""Data contracts for the Database Planner and its tools.

The planner emits a `DatabasePlan` (a list of `ToolCall`s); executing each call
produces a `ToolResult`. `RecordFilters` is the normalized, entity-agnostic filter
set the Scope Resolver (see filters.py) turns into catalog query kwargs. See
docs/database-tool-registry.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ToolName = Literal[
    "count_records", "list_records", "lookup_record", "aggregate_records", "list_themes",
    "resolve_entity",
]

# resolve_entity's advertised entity kinds — author | bundle | theme, not tag
# (see app.retrieval.structured.resolve for why).
ResolveType = Literal["author", "bundle", "theme"]

# Grouping dimensions for aggregate_records (mapped to catalog columns/facets by
# the tool: theme->theme, content_type->bundle, author, year).
GroupBy = Literal["theme", "content_type", "author", "year"]

# What a count counts. "records" is the documents themselves; the others
# count distinct values of that facet, which is a different question with a
# different noun in the answer — "264 authors work on Energy" is not "264
# articles". Same vocabulary as GroupBy so one dimension means one thing
# whether it is counted or grouped on.
CountOf = Literal["records", "theme", "content_type", "author", "year"]


@dataclass
class RecordFilters:
    """Normalized filter scope — only the columns the catalog actually supports.

    `theme` and `tag` are display names resolved to taxonomy UUIDs by the Scope
    Resolver; dates are a half-open [from, to) interval over effective_start_date.
    """

    theme: str | None = None
    # Restrict to themes from one bucket of the theme map ("main"/"other").
    # Set from the question by `theme_scope.detect`, and left None whenever
    # the user named a theme — a named Other theme must stay countable.
    theme_group: str | None = None
    tag: str | None = None
    author: str | None = None
    title_contains: str | None = None
    # A name no facet places — a series, programme or acronym ("WSDS") — in
    # every spelling titles use ("WSDS", "World Sustainable Development
    # Summit"). The title must carry one of them, word-bounded. Unlike
    # `topic_terms` it narrows counts and breakdowns too: it stands in for the
    # facet the name was asked as. See `app.retrieval.structured.names`.
    title_names: tuple[str, ...] = ()
    # Subject words the closed facet set cannot express, kept so a list can still
    # be constrained by what the question is actually about. Any one of them must
    # appear in the title; rows matching more of them rank higher. See
    # `app.retrieval.structured.topic` for why a list without this degenerates
    # into "the newest N rows of a large bucket".
    topic_terms: tuple[str, ...] = ()
    date_from: str | None = None
    date_to: str | None = None


@dataclass
class ToolCall:
    """One planned catalog operation. `entity` is a canonical bundle name (or None
    to span all content); operation-specific fields are ignored by tools that
    don't use them."""

    tool: ToolName
    entity: str | None = None
    filters: RecordFilters = field(default_factory=RecordFilters)
    # aggregate_records
    group_by: GroupBy | None = None
    # A second grouping dimension, making the key the *pair*: "which authors
    # write about which themes" is one question, not a breakdown per author.
    secondary_group_by: GroupBy | None = None
    aggregation: str = "count"
    # count_records: count distinct values of this facet instead of documents.
    count_of: CountOf = "records"
    # lookup_record
    title: str | None = None
    # list_records / lookup_record
    sort: str = "recent"
    limit: int = 10
    # list_records: page offset, and the metadata keys to keep in the returned
    # records (None keeps them all).
    offset: int = 0
    fields: list[str] | None = None
    # list_themes: which theme groups may be exposed ("main"/"other"/"all").
    # Decided deterministically from the question by
    # `app.retrieval.structured.theme_scope`, and carried on the call so a plan
    # states what it will expose rather than leaving it to the tool's default.
    theme_scope: str = "main"
    # rendering shape for list/aggregate output (table/timeline/list/default)
    output_format: str = "default"
    # Whether the answer may follow its headline with detail sections (see
    # `app.retrieval.structured.detail`). Cleared for a part of a multi-call
    # plan or a combined answer, where the parts or the prose say the rest.
    detail: bool = True
    # count_records / list_records: the content type the question's own word
    # also names when the call was widened past it ("articles" by a person spans
    # everything they published), so the detail can say how many are of it.
    named_type: str | None = None
    # resolve_entity
    query: str | None = None
    resolve_type: ResolveType | None = None


@dataclass
class DatabasePlan:
    """An ordered set of tool calls for one query. Independent calls execute in
    parallel; `rationale` is for logging/debugging."""

    calls: list[ToolCall] = field(default_factory=list)
    rationale: str = ""


@dataclass
class ToolResult:
    """A tool's structured output plus a deterministic rendering.

    `data` holds the shape for the tool: {"count": int} | {"records": [...]} |
    {"groups": [[value, count], ...]}. `rendered` is the LLM-free answer for the
    single-capability path; `data` + `citations` are the evidence for multi-label
    synthesis. `ok=False` signals a guarded no-answer (e.g. unknown entity) so the
    caller can fall through to semantic search — unless `error_kind` marks it
    terminal (see below), in which case `rendered` is the answer instead.
    """

    tool: str
    entity: str | None = None
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    citations: list[dict[str, Any]] = field(default_factory=list)
    rendered: str = ""
    error: str | None = None
    # "unresolved" | "ambiguous" -> terminal: a filter was understood but could
    # not be answered honestly, so `rendered` is shown as the answer rather than
    # a cue to fall through to semantic search. Both come from fuzzy name
    # matching, so they are terminal only while `entity_resolution_enabled` is on.
    # "ambiguous_entity" -> terminal unconditionally: a content word naming
    # several bundles, decided from a curated list rather than by similarity.
    # "no_records" | "unknown_entity" | "query_failed" | None -> today's
    # behaviour: ok=False here still means "fall through".
    error_kind: str | None = None
