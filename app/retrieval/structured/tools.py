"""The catalog tools the Database Planner invokes.

Each tool wraps an existing read function in `app.catalog.queries` and renders
a uniform `ToolResult`. An unknown entity, an unresolvable theme or tag, or an
empty result yields `ok=False` so the caller can fall through to semantic
search. `resolve_entity` is the exception: it wraps
`app.retrieval.structured.resolve` (fuzzy name matching), not a catalog read.

See docs/database-tool-registry.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime
from typing import Any, Sequence

from app.config import get_settings
from app.catalog import queries as state
from app.catalog import theme_taxonomy
from app.catalog.models import StateRecord
from app.core.dates import inclusive_end
from app.retrieval.structured.entities import (
    ambiguous_bundles,
    entity_label,
    get_entity,
    is_available,
    is_known,
    normalize_entity,
    scope_noun,
)
from app.retrieval.structured import detail as structured_detail
from app.retrieval.structured import topic
from app.retrieval.structured.filters import AmbiguousFilter, _parse_date, resolve_filters
from app.retrieval.structured.rendering import card, item_line, md_link, record_date
from app.retrieval.structured.types import GroupBy, RecordFilters, ToolResult
from app.schemas.query import Citation

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Rendering helpers.
# --------------------------------------------------------------------------- #

def _md_cell(text: str) -> str:
    """'|' inside a value would break a markdown table row."""
    return text.replace("|", "\\|")


def _period_label(filters: RecordFilters) -> str:
    """Human phrase for a date scope. A whole-calendar-year range reads as
    'in YYYY' rather than raw bounds.

    `date_to` is exclusive, so a two-ended range names the last day it actually
    covers — echoing the raw bound would claim a day the query excludes ("between
    2020-01-01 and 2022-01-01" for a 2020-2021 range). 'before' is left as the
    raw bound, which is already what an exclusive end means."""
    df, dt = filters.date_from, filters.date_to
    lo, hi = _parse_date(df), _parse_date(dt)
    if lo and hi and (lo.month, lo.day) == (1, 1) and hi == datetime(lo.year + 1, 1, 1):
        return f" in {lo.year}"
    if df and dt:
        last = inclusive_end(dt) or dt
        return f" on {df}" if last == df else f" between {df} and {last}"
    if df:
        return f" since {df}"
    if dt:
        return f" before {dt}"
    return ""


def _scope_phrase(filters: RecordFilters) -> str:
    """Human phrase naming every active filter (author, theme, tag, title,
    period) — so an answer states its own interpretation rather than a bare
    number a wrong match could hide behind. Kept in step with
    `_applied_filters`, which echoes the same set structurally.

    The bundle/content-type itself is already named as the sentence's noun (see
    `entity_label`), so it is not repeated here. Callers pass `scope.effective`,
    whose author/theme are the canonical names resolution matched — so the
    phrase names the entity actually filtered on, not the user's spelling."""
    parts = []
    if filters.author:
        parts.append(f" by {filters.author}")
    if filters.theme:
        parts.append(f" on '{filters.theme}'")
    if filters.tag:
        parts.append(f" tagged '{filters.tag}'")
    if filters.title_contains:
        parts.append(f" with '{filters.title_contains}' in the title")
    parts.append(_period_label(filters))
    return "".join(parts)


def _unresolved_miss(tool: str, entity: str | None, kind: str, value: str | None) -> ToolResult:
    """The terminal "no <kind> matching X found" result.

    Checked *after* querying, never before: a name that matching could not place
    is still used as a filter, so the query may well find rows anyway — matching
    works from the names documents carry, and it being unsure is not proof of
    absence. Only an empty result leaves "unknown name" and "genuinely no
    documents" indistinguishable, and then the miss is the honest answer."""
    return ToolResult(
        tool=tool, entity=entity, ok=False,
        error=f"{kind} did not resolve to a known entity", error_kind="unresolved",
        rendered=f"No {kind} matching '{value}' found.",
    )


def _ambiguous_result(
    tool: str, entity: str | None, amb: Any, *, error_kind: str = "ambiguous"
) -> ToolResult:
    """The terminal clarification for a name that matched several catalog entities
    too closely to choose between — asked, never guessed (§4).

    Takes the `AmbiguousFilter` itself rather than the enclosing scope so the
    content-type guard can reuse it: an ambiguous *bundle* is decided before any
    filter is resolved, so there is no scope to read it from."""
    options = "\n".join(f"{i}. {name}" for i, name in enumerate(amb.candidates, start=1))
    return ToolResult(
        tool=tool, entity=entity, ok=False,
        error=f"ambiguous {amb.kind}", error_kind=error_kind,
        rendered=(
            f"'{amb.query}' matches more than one {amb.kind}:\n{options}\n"
            "Which did you mean?"
        ),
    )


def _entity_guard(tool: str, entity: str | None) -> ToolResult | None:
    """Pre-query guard on the content type: a word naming several bundles asks
    which was meant; an unrecognized one falls through to semantic search as
    before. Returns None when `entity` is empty or a known bundle.

    Ambiguity is terminal (`error_kind`), because collapsing "projects" onto one
    project type reports that type's total as if it were every project — and
    leaving the type off instead counts articles and papers as projects.

    It reports as `ambiguous_entity` rather than `ambiguous` so it stays terminal
    with `entity_resolution_enabled` off: that flag guards *fuzzy* name matching,
    which can misfire and wants an eval first, while this is a curated list of
    words that name more than one bundle and cannot resolve to a single one."""
    if not entity or is_known(normalize_entity(entity)):
        return None
    spanned = ambiguous_bundles(entity)
    if spanned:
        return _ambiguous_result(
            tool, entity,
            AmbiguousFilter(
                kind="content type", query=entity,
                candidates=[entity_label(bundle, 2) for bundle in spanned],
            ),
            error_kind="ambiguous_entity",
        )
    return ToolResult(tool=tool, entity=entity, ok=False,
                      error=f"unknown entity {entity!r}")


def _scope_guard(tool: str, entity: str | None, scope: Any) -> ToolResult | None:
    """The one pre-query guard shared by the filtered tools: a name that matched
    several entities too closely to choose between. Returns None to proceed."""
    if scope.ambiguous is not None:
        return _ambiguous_result(tool, entity, scope.ambiguous)
    return None


def _empty_result_miss(tool: str, entity: str | None, scope: Any) -> ToolResult | None:
    """Post-query: whether an empty result should be reported as something other
    than a genuine zero. Returns None when every filter placed, leaving the
    caller to answer an honest zero.

    Author, theme and tag are treated identically — each has its own facet table,
    so each filters by name and each can only be judged a miss once the query
    comes back empty.

    `entity` is judged the same way and for the same reason. A bundle can be
    configured (`DEFAULT_BUNDLES`) yet have no rows in this deployment, and
    filtering on it then reports "0 reports" — a statement about the corpus when
    the truth is about the vocabulary. Checked here rather than before querying
    so the tools stay independent of the catalog's inventory whenever the query
    found something anyway."""
    for kind in ("author", "theme", "tag"):
        if getattr(scope, f"{kind}_missed", False):
            return _unresolved_miss(tool, entity, kind, getattr(scope.effective, kind))
    if entity and not is_available(entity):
        logger.info(
            "Bundle %r is registered but absent from the catalog; falling "
            "through instead of reporting zero.", entity,
        )
        # No error_kind: this must fall through to semantic search like an
        # unrecognized type, not terminate the way an unresolved name does.
        return ToolResult(
            tool=tool, entity=entity, ok=False,
            error=f"no {entity!r} content in this catalog",
        )
    return None


# Words showing the user asked about titles as such, rather than a subject the
# intent layer funnelled into `title_contains`.
_TITLE_QUESTION = re.compile(r"\b(titles?|titled|called|named|headlines?)\b", re.I)
# A quoted phrase names a title verbatim ("the report called “Solar India”").
# Double quotes only: an apostrophe is ordinary prose ("India's energy mix").
_QUOTED_PHRASE = re.compile(r"[\"“”].+?[\"“”]")


def _title_guess_zero(question: str | None, scope: Any) -> bool:
    """Whether an empty count came from a *guessed* title filter rather than a
    real absence, so the caller should fall through to semantic search instead of
    reporting a corpus-wide zero.

    `title_contains` is `title LIKE '%…%'` over one column, so zero under it means
    "no title holds this phrase" — never "the corpus holds nothing on this
    subject". The intent layer fills the slot from whatever the question is about,
    so "how many reports about quantum teleportation" arrives as a title
    substring, and the body text it does not search is exactly where a subject
    lives. Answering 0 there states the corpus is silent on a topic when only its
    titles are, and unlike the author/theme/tag misses above there is no
    resolution step to catch it: any phrase is a valid substring.

    Not a guess when the question is about titles ("reports titled X", a quoted
    phrase) — then the zero is what was asked for, and prose from semantic search
    would be the worse answer. An absent `question` cannot establish that, so it
    counts as a guess: falling through costs one semantic pull, while a wrong zero
    costs the answer."""
    if not scope.effective.title_contains:
        return False
    if not question:
        return True
    return not (_TITLE_QUESTION.search(question) or _QUOTED_PHRASE.search(question))


def _applied_filters(bundle: str | None, filters: RecordFilters) -> dict[str, str]:
    """The filters actually in effect, keyed by name — the structured
    counterpart to `_scope_phrase`, so a caller can check the interpretation
    programmatically rather than only by reading prose. Unset filters are
    omitted rather than included as null."""
    applied = {
        "entity": bundle,
        "author": filters.author,
        "theme": filters.theme,
        "tag": filters.tag,
        "title_contains": filters.title_contains,
        "date_from": filters.date_from,
        "date_to": filters.date_to,
    }
    return {key: value for key, value in applied.items() if value}


def _render_list_table(records: Sequence[StateRecord]) -> str:
    lines = ["| Title | Published | Type |", "| --- | --- | --- |"]
    for r in records:
        cell = _md_cell(md_link(r.title or r.document_id, r.url))
        kind = entity_label(r.bundle, 1) if r.bundle else ""
        lines.append(f"| {cell} | {record_date(r)} | {kind} |")
    return "\n".join(lines)


def _render_list_timeline(records: Sequence[StateRecord]) -> str:
    """Year-grouped chronology; expects records already sorted newest-first."""
    lines: list[str] = []
    year = ""
    for r in records:
        y = (r.effective_start_date or "")[:4] or "Undated"
        if y != year:
            if lines:
                lines.append("")
            lines.append(f"{y}:")
            year = y
        label = record_date(r) or "n.d."
        lines.append(f"- {label}: {md_link(r.title or r.document_id, r.url)}")
    return "\n".join(lines)


def _topic_phrase(topic_terms: Sequence[str]) -> str:
    """The title words a topic-constrained list was narrowed by, stated so the
    reader can see why these rows and not others (any one word qualifies)."""
    if not topic_terms:
        return ""
    words = " or ".join(f"'{term}'" for term in topic_terms)
    return f" mentioning {words} in the title"


def _list_lead(
    shown: int, total: int | None, bundle: str | None, filters: RecordFilters,
    *, ranked: bool,
) -> str:
    """The sentence above a list: how many rows, of how many, in which scope.

    When the page is a cut of a larger set the total leads, rather than trailing
    the list where it was easy to miss. Rows ranked by topic match are not
    "the most recent", so they are not called that."""
    scope = _scope_phrase(filters) + _topic_phrase(filters.topic_terms)
    kind = scope_noun(bundle, by_author=bool(filters.author))
    if total and total > shown:
        noun = entity_label(kind, total)
        if ranked:
            return f"Here are {shown} of the {total} {noun}{scope}, closest matches first:"
        return f"Here are the {shown} most recent of {total} {noun}{scope}:"
    return f"Found {shown} {entity_label(kind, shown)}{scope}:"


def _render_records(
    records: Sequence[StateRecord], output_format: str, *, bundle: str | None,
    filters: RecordFilters, total: int | None = None, ranked: bool = False,
    facets: dict[str, dict[str, list[str]]] | None = None,
) -> tuple[str, list[dict], list[dict]]:
    """Body + structured records + citations, in one consistent order (timeline
    sorts newest-first; citations follow the rendered order).

    ``facets`` (authors and themes per document) is set when the answer may
    carry detail: one document is then shown as a card, several with bylines."""
    separator = "\n"
    if output_format == "timeline":
        ordered = sorted(records, key=lambda r: r.effective_start_date or "", reverse=True)
        body = _render_list_timeline(ordered)
    elif output_format == "table":
        ordered = list(records)
        body = _render_list_table(ordered)
    elif facets is not None and len(records) == 1:
        ordered = list(records)
        found = facets.get(ordered[0].document_id, {})
        body = card(ordered[0], authors=found.get("authors", ()),
                    themes=found.get("themes", ()))
        separator = "\n\n"
    else:
        ordered = list(records)
        # The kind of each item is only news when the list spans several kinds.
        body = "\n".join(
            item_line(r, with_type=bundle is None,
                      authors=(facets or {}).get(r.document_id, {}).get("authors", ()),
                      listed_by=filters.author)
            for r in ordered
        )
    citations = [
        Citation(
            n=i, type="website", title=r.title, url=r.url,
            document_id=r.document_id or None,
        ).model_dump()
        for i, r in enumerate(ordered, start=1)
    ]
    data = [
        {
            "document_id": r.document_id, "title": r.title, "url": r.url,
            "effective_start_date": r.effective_start_date, "bundle": r.bundle,
        }
        for r in ordered
    ]
    # Named the scope actually matched, not a generic "here is what I found" —
    # the same information `count_records` already states in its own sentence,
    # so a list answer is no less specific than a count of the same query.
    lead = _list_lead(len(ordered), total, bundle, filters, ranked=ranked)
    return lead + separator + body, data, citations


def _latest_phrase(record: Any) -> str:
    """"[Title](url), 27 Apr 2026" — a theme's newest item, linked and dated."""
    when = record_date(record)
    link = md_link(record.title or record.document_id, record.url)
    return f"{link}, {when}" if when else link


def _theme_section(
    label: str, names: list[str], output_format: str,
    counts: dict[str, int] | None = None,
    latest: dict[str, Any] | None = None,
) -> str:
    """One block of a theme listing. An empty `label` renders the bare list, for
    the case where the surrounding sentence already names what these are.
    ``counts`` (items per theme) adds a column, or a figure beside each name;
    ``latest`` (each theme's newest item) adds that too."""
    latest = latest or {}
    if output_format == "table":
        header, rule = ["theme"], ["---"]
        if counts:
            header.append("items")
            rule.append("---")
        if latest:
            header.append("latest")
            rule.append("---")
        lines = [f"| {' | '.join(header)} |", f"| {' | '.join(rule)} |"]
        for n in names:
            cells = [_md_cell(n)]
            if counts:
                cells.append(str(counts.get(n, 0)))
            if latest:
                cells.append(_md_cell(_latest_phrase(latest[n])) if n in latest else "")
            lines.append(f"| {' | '.join(cells)} |")
        rows = "\n".join(lines)
        return f"**{label}**\n{rows}" if label else rows
    lines = []
    for n in names:
        line = f"- {n}"
        if counts:
            line += f" — {counts.get(n, 0)} {entity_label('items', counts.get(n, 0))}"
        if n in latest:
            line += f"; latest: {_latest_phrase(latest[n])}"
        lines.append(line)
    body = "\n".join(lines)
    return f"{label}:\n{body}" if label else body


def _project_fields(
    records: list[dict[str, Any]], fields: Sequence[str] | None
) -> list[dict[str, Any]]:
    """Keep only the requested metadata keys per record. Projects the structured
    payload only — `rendered` is a natural-language answer already shaped by
    `output_format`, not a strict field projection, so a field list narrows what
    a caller reads from `data` without changing the human-readable text.

    Unknown keys are dropped rather than honored, and a list naming *only*
    unknown keys projects nothing away: an LLM-supplied field name that does not
    exist should cost the caller some extra keys, not silently empty every
    record."""
    if not fields or not records:
        return records
    allowed = {f for f in fields if f in records[0]}
    if not allowed:
        logger.warning("Ignoring unknown list_records fields %s.", sorted(set(fields)))
        return records
    return [{k: v for k, v in r.items() if k in allowed} for r in records]


# --------------------------------------------------------------------------- #
# Tools.
# --------------------------------------------------------------------------- #

def count_records(
    entity: str | None, filters: RecordFilters, *, question: str | None = None,
    count_of: str = "records", detail: bool = False, named_type: str | None = None,
) -> ToolResult:
    """How many catalog documents match. Unknown entity returns ok=False (fall
    through, never a misleading zero). Names are canonicalized first, so the
    answer states the entity actually filtered on; a name too ambiguous to pick
    or a tag that resolves to nothing stops before querying, and an unresolved
    author/theme becomes a miss only if the query then comes back empty. A
    resolved filter matching no rows is an honest 0 — except under a guessed title
    substring, which is a claim about titles and not about the corpus
    (`question` decides which was asked; see `_title_guess_zero`).

    ``count_of`` changes *what* is counted, not what is filtered: "records"
    counts documents, any other dimension counts distinct values of that
    facet within the same scope. "How many authors work on Energy" and "how
    many articles are under Energy" share every filter and differ only
    here — and answering one with the other's number is the failure this
    parameter exists to prevent.

    ``detail`` follows the headline with what the same scope shows (see
    `app.retrieval.structured.detail`); off, the headline stands alone.
    ``named_type`` is the type the question's word named when the count was
    widened past it; the detail then says how many are of that type."""
    guarded = _entity_guard("count_records", entity)
    if guarded is not None:
        return guarded
    ent = get_entity(entity) if entity else None
    scope = resolve_filters(filters)
    guarded = _scope_guard("count_records", entity, scope)
    if guarded is not None:
        return guarded
    bundle = ent.name if ent else None
    dimension, valid = _dimension_or_reject(count_of, allow_records=True)
    if not valid:
        # Refuse rather than guess. Falling through to semantic search cannot
        # fabricate a total (see the grounded prompt's rule 8), whereas
        # defaulting to a document count would answer a different question in a
        # sentence that looks right.
        logger.warning(
            "count_records got an unsupported count_of %r; expected one of %s.",
            count_of, sorted(VALID_COUNT_OF),
        )
        return ToolResult(
            tool="count_records", entity=entity, ok=False,
            error=f"unsupported count_of {count_of!r}",
        )
    common = dict(
        source_type=ent.source_type if ent else "website",
        bundle=bundle,
        entity_type=ent.entity_type if ent else "node",
        title_contains=scope.title_contains,
        **scope.as_kwargs(),
    )
    try:
        total = (
            state.count_distinct_values(dimension, **common)
            if dimension
            else state.count_documents(**common)
        )
    except Exception:
        logger.warning("count_records query failed.", exc_info=True)
        return ToolResult(tool="count_records", entity=bundle, ok=False, error="query failed")
    if not total:
        missed = _empty_result_miss("count_records", bundle, scope)
        if missed is not None:
            return missed
        if _title_guess_zero(question, scope):
            logger.info(
                "Zero count under the guessed title %r; falling through to "
                "semantic search instead of reporting a corpus-wide zero.",
                scope.effective.title_contains,
            )
            # No error_kind: this falls through like an unrecognized type rather
            # than terminating the way an unresolved name does.
            return ToolResult(
                tool="count_records", entity=bundle, ok=False,
                error="zero under a guessed title filter",
            )
    phrase = _scope_phrase(scope.effective)
    verb = "is" if total == 1 else "are"
    source_labels = count_of in _SOURCE_LABEL_DIMENSIONS
    kind = scope_noun(bundle, by_author=bool(scope.effective.author))
    if dimension:
        singular, plural = _COUNT_OF_NOUNS[count_of]
        noun = singular if total == 1 else plural
    else:
        noun = entity_label(kind, total)
    # A count of labels says where the labels come from. "955 authors" claims an
    # identity resolution nobody has done; "955 distinct author names recorded
    # in the source data" is what the query actually established.
    tail = "recorded in the source data." if source_labels else "matching your query."
    rendered = f"There {verb} {total or 'no'} {noun}{phrase} {tail}"
    citations: list[dict[str, Any]] = []
    if detail:
        if dimension:
            extra = structured_detail.for_distinct(
                total, dimension=dimension, common=common
            )
        elif total:
            extra = structured_detail.for_count(
                total, common=common, bundle=bundle, filters=scope.effective,
                named_type=named_type, scope=phrase,
            )
        else:
            extra = structured_detail.for_zero(
                common=common, bundle=bundle, filters=scope.effective,
                noun=lambda n: entity_label(kind, n),
                scope_without_period=_scope_phrase(
                    replace(scope.effective, date_from=None, date_to=None)),
                scope_without_type=phrase,
            )
        rendered = extra.render_onto(rendered)
        citations = extra.citations
    data: dict[str, Any] = {
        "count": total, "applied": _applied_filters(bundle, scope.effective),
    }
    if dimension:
        # Only when it is not the default, so a document count's payload is
        # exactly what it has always been and no consumer has to learn a new key
        # to keep reading it.
        data["count_of"] = count_of
        # What the number is a count of, spelled out for any consumer that
        # renders it themselves rather than using `rendered`.
        data["counts"] = (
            "distinct_author_names_in_source" if source_labels
            else f"distinct_{count_of}"
        )
    return ToolResult(
        tool="count_records", entity=bundle, ok=True, data=data,
        citations=citations, rendered=rendered,
    )


def _scope_total(ent: Any, bundle: str | None, scope: Any) -> int | None:
    """How many documents the list's own filters match in total, or None.

    Same filters, same tool call — so "showing 10 of 594" cannot contradict the
    rows above it. Failure is not an error: the list is still a good answer
    without a total, so a counting problem degrades to saying nothing.
    """
    try:
        return state.count_documents(
            source_type=ent.source_type if ent else "website",
            bundle=bundle,
            entity_type=ent.entity_type if ent else "node",
            title_contains=scope.title_contains,
            topic_terms=scope.topic_terms or None,
            **scope.as_kwargs(),
        )
    except Exception:
        logger.debug("Could not total the list scope.", exc_info=True)
        return None


def list_records(
    entity: str | None,
    filters: RecordFilters,
    *,
    sort: str = "recent",
    limit: int = 10,
    offset: int = 0,
    output_format: str = "default",
    fields: Sequence[str] | None = None,
    detail: bool = False,
    named_type: str | None = None,
) -> ToolResult:
    """List matching documents, most recent first (the only backing sort today).
    Empty result returns ok=False. Filter-resolution semantics match
    `count_records` (see `_scope_guard` / `_empty_result_miss`). `fields` narrows
    the metadata keys in `data["records"]`; `rendered` is unaffected (see
    `_project_fields`). ``detail`` adds bylines (a card for a single document)
    and a follow-up (see `app.retrieval.structured.detail.for_list`), and with
    ``named_type`` says how many of the listed scope are of that type."""
    guarded = _entity_guard("list_records", entity)
    if guarded is not None:
        return guarded
    ent = get_entity(entity) if entity else None
    scope = resolve_filters(filters)
    guarded = _scope_guard("list_records", entity, scope)
    if guarded is not None:
        return guarded
    bundle = ent.name if ent else None
    try:
        records = state.list_documents(
            source_type=ent.source_type if ent else "website",
            bundle=bundle,
            entity_type=ent.entity_type if ent else "node",
            title_contains=scope.title_contains,
            topic_terms=scope.topic_terms or None,
            limit=limit,
            offset=offset,
            **scope.as_kwargs(),
        )
    except Exception:
        logger.warning("list_records query failed.", exc_info=True)
        return ToolResult(tool="list_records", entity=bundle, ok=False, error="query failed")
    if not records:
        missed = _empty_result_miss("list_records", bundle, scope)
        if missed is not None:
            return missed
        return ToolResult(tool="list_records", entity=bundle, ok=False,
                          error="no matching records")
    # A list cut off at `limit` says nothing about how much it cut off, so a
    # user cannot tell ten from six hundred. Count the same scope and say so
    # whenever the page is full — the count is over exactly the filters that
    # produced the rows, so the two can never disagree.
    total = None
    if len(records) >= limit and topic.enabled():
        total = _scope_total(ent, bundle, scope)
    rendered, data, citations = _render_records(
        records, output_format, bundle=bundle, filters=scope.effective, total=total,
        # A topic ranks rows by match before recency, and a later page is not
        # the most recent either.
        ranked=bool(scope.topic_terms) or offset > 0,
        facets=structured_detail.facets(records) if detail else None,
    )
    if detail:
        named = None
        if named_type and bundle is None and (named_ent := get_entity(named_type)):
            named = (named_type, _scope_total(named_ent, named_type, scope))
        mix = ""
        # Title words narrow the rows past what a type count over the facets
        # would describe, so a topic-constrained list or a title lookup gets no
        # mix.
        if not scope.topic_terms and not scope.title_contains:
            mix = structured_detail.author_list_mix(
                len(records), total=total, bundle=bundle, filters=scope.effective,
                scope=_scope_phrase(scope.effective),
                common=dict(
                    source_type=ent.source_type if ent else "website", bundle=bundle,
                    entity_type=ent.entity_type if ent else "node",
                    title_contains=scope.title_contains, **scope.as_kwargs(),
                ),
            )
        rendered = structured_detail.for_list(
            len(records), total=total, filters=scope.effective, named=named, mix=mix,
        ).render_onto(rendered)
    return ToolResult(
        tool="list_records", entity=bundle, ok=True,
        data={"records": _project_fields(data, fields),
              "total_matching": total,
              "applied": _applied_filters(bundle, scope.effective)},
        citations=citations, rendered=rendered,
    )


# Words signalling the user wants what a document SAYS, not just its catalog entry
# ("what does X say" reads; "show the article titled X" browses).
_CONTENT_QUESTION = re.compile(
    r"\b(what|how|why|when|where|who|does|do|did|is|are|was|were"
    r"|explain|describe|summar\w*|tell)\b",
    re.IGNORECASE,
)


def _resolve_chain(title: str | None, question: str | None, output_format: str) -> str | None:
    """Document id for a title lookup that should chain into content QA: a content
    question (or summary/detailed shape) naming a title that matches exactly one
    catalog document."""
    if not title:
        return None
    is_content = output_format in ("summary", "detailed") or bool(
        question and _CONTENT_QUESTION.search(question)
    )
    if not is_content:
        return None
    try:
        records = state.list_documents(
            source_type="website", entity_type="node", title_contains=title, limit=3,
        )
    except Exception:
        logger.warning("lookup chain resolution failed.", exc_info=True)
        return None
    if len(records) == 1 and records[0].document_id:
        return records[0].document_id
    return None


def lookup_record(
    entity: str | None,
    title: str | None,
    filters: RecordFilters,
    *,
    limit: int = 10,
    output_format: str = "default",
    question: str | None = None,
    detail: bool = False,
) -> ToolResult:
    """Find a specific document by title. Returns the list rendering AND a
    `chain_document_id` when the lookup uniquely identifies a document for a
    content question (the caller may then route into the QA path)."""
    chain_id = _resolve_chain(title, question, output_format)
    scoped = replace(filters, title_contains=title or filters.title_contains)
    result = list_records(entity, scoped, limit=limit, output_format=output_format,
                          detail=detail)
    return ToolResult(
        tool="lookup_record",
        entity=result.entity,
        ok=result.ok,
        data={**result.data, "chain_document_id": chain_id},
        citations=result.citations,
        rendered=result.rendered,
        error=result.error,
        # Without this the clarification a guard produced arrives as a plain
        # ok=False and falls through to semantic search, so "which did you mean?"
        # is replaced by a guess at the very question it was asked about.
        error_kind=result.error_kind,
    )


def resolve_lookup_chain(analysis: Any, question: str) -> str | None:
    """The lookup->content-QA chain decision used by the query pipeline: the
    document id when `analysis` is a lookup naming a title that a content
    question matches to exactly one catalog document, else None. Duck-typed on
    operation / title_contains / answer_format."""
    if analysis is None or getattr(analysis, "operation", None) != "lookup":
        return None
    return _resolve_chain(
        getattr(analysis, "title_contains", None),
        question,
        getattr(analysis, "answer_format", "default"),
    )


# aggregate group_by -> (catalog dimension, display label).
_GROUP_DIMENSIONS: dict[str, tuple[str, str]] = {
    "theme": ("theme", "theme"),
    "content_type": ("bundle", "content type"),
    "author": ("author", "author"),
    "year": ("year", "year"),
}

# Plural nouns for a distinct-facet count, so the answer names what was actually
# counted. Getting this wrong is the whole risk of the operation: "264 articles
# work on Energy" would be a confident, wrong sentence built from a right number.
#
# "author" is deliberately **not** "authors". Drupal stores authors as free
# text; there is no author id, email or reference anywhere in the payload, and
# the knowledge graph's person entities for authors are all provisional. So the
# catalog can count distinct author *names* and cannot count people: two people
# called "Arun Kumar" are one name, and one person written "Datta Debajit" and
# "Debajit Datta" is two. Saying "authors" would assert an identity resolution
# that has not been done — see reports/knowledge/ Step 6.
_COUNT_OF_NOUNS: dict[str, tuple[str, str]] = {
    "theme": ("theme", "themes"),
    "content_type": ("content type", "content types"),
    "author": ("distinct author name", "distinct author names"),
    "year": ("year", "years"),
}

# Dimensions whose count is a count of *labels in the source*, not of the real
# things behind them. The answer says so rather than leaving the reader to
# assume otherwise.
_SOURCE_LABEL_DIMENSIONS: frozenset[str] = frozenset({"author"})

# The only value of `count_of` that means "the documents themselves".
COUNT_RECORDS = "records"

# Every dimension a count or a grouping may name. Derived from the mapping
# rather than restated, so the allow-list cannot drift from what the tool can
# actually dispatch.
VALID_DIMENSIONS: frozenset[str] = frozenset(_GROUP_DIMENSIONS)
VALID_COUNT_OF: frozenset[str] = VALID_DIMENSIONS | {COUNT_RECORDS}


# Groups a single-dimension breakdown shows, pairs a two-dimension one shows,
# and the ceiling for years (all of them; the corpus spans about 35).
_GROUP_LIMIT = 20
_PAIR_LIMIT = 50
_YEAR_LIMIT = 100


def _group_value(dimension: str | None, value: Any) -> str:
    """A group's display name: a content type reads as what it is called
    ("feature articles"), not as its bundle key ("feature_articles")."""
    return entity_label(str(value), 2) if dimension == "bundle" else str(value)


def _dimension_or_reject(value: Any, *, allow_records: bool) -> tuple[str | None, bool]:
    """Resolve a dimension name to its catalog column. Returns (column, ok).

    Unset means the default; **unrecognised means refuse**. Collapsing the two
    is what made `count_of="document"` — a plausible spelling, and the one an
    unfamiliar caller reaches for first — answer "62 articles" to "how many
    authors work on Energy". A wrong noun on a right number is a confident wrong
    answer, which is worse than no answer.
    """
    if value is None or value == "" or (allow_records and value == COUNT_RECORDS):
        return None, True
    mapped = _GROUP_DIMENSIONS.get(value)
    if mapped is None:
        return None, False
    return mapped[0], True


def aggregate_records(
    entity: str | None,
    group_by: GroupBy | None,
    filters: RecordFilters,
    *,
    secondary_group_by: GroupBy | None = None,
    aggregation: str = "count",
    output_format: str = "default",
    detail: bool = False,
) -> ToolResult:
    """Grouped counts (per theme / content type / author / year). Only the
    'count' aggregation is backed today. Filter-resolution semantics match
    `count_records` (see `_scope_guard` / `_empty_result_miss`).

    ``secondary_group_by`` makes the grouping key the *pair* of dimensions:
    "which authors write about which themes" is one question whose answer is
    a set of author-theme pairs, not a per-author breakdown repeated. Ignored
    when it names the same dimension as ``group_by`` — a pair of one thing is
    the single-dimension question, and refusing would be pedantry.

    ``detail`` leads with the scope's total and follows the groups with what
    they show (see `app.retrieval.structured.detail.for_breakdown`)."""
    # Unset groups by theme, which is the documented default. An unrecognised
    # name is refused instead: "group_by='Author'" quietly becoming a theme
    # breakdown is a wrong answer wearing a right one's shape.
    if group_by is not None and group_by not in _GROUP_DIMENSIONS:
        logger.warning(
            "aggregate_records got an unsupported group_by %r; expected one of %s.",
            group_by, sorted(VALID_DIMENSIONS),
        )
        return ToolResult(
            tool="aggregate_records", entity=entity, ok=False,
            error=f"unsupported group_by {group_by!r}",
        )
    dimension, label = _GROUP_DIMENSIONS.get(group_by or "theme", ("theme", "theme"))

    # The secondary is optional, so an unusable one degrades to the
    # single-dimension breakdown — a narrower *correct* answer, unlike the cases
    # above which would have been wrong ones. It is logged, and `dimensions`
    # below reports what was actually grouped on rather than what was asked for.
    second, second_label = None, None
    if secondary_group_by:
        mapped = _GROUP_DIMENSIONS.get(secondary_group_by)
        if mapped is None:
            logger.warning(
                "aggregate_records ignoring unsupported secondary_group_by %r.",
                secondary_group_by,
            )
        elif mapped[0] == dimension:
            # A pair of one thing is the single-dimension question.
            pass
        else:
            second, second_label = mapped
    guarded = _entity_guard("aggregate_records", entity)
    if guarded is not None:
        return guarded
    ent = get_entity(entity) if entity else None
    scope = resolve_filters(filters)
    guarded = _scope_guard("aggregate_records", entity, scope)
    if guarded is not None:
        return guarded
    bundle = ent.name if ent else None
    common = dict(
            # Taken from the entity, exactly as `count_records` does. Hardcoding
            # "website"/"node" here agreed with it only because every registered
            # bundle happens to be a website node today; the first bundle that
            # is not would make a breakdown disagree with a count of the same
            # scope, silently and in the direction of under-reporting.
        source_type=ent.source_type if ent else "website",
        bundle=bundle,
        entity_type=ent.entity_type if ent else "node",
        title_contains=scope.title_contains,
        **scope.as_kwargs(),
    )
    # Years are few and read as a timeline, so all of them come back; any other
    # dimension shows its largest groups, and one row past the cap says whether
    # the cap cut anything off.
    if second:
        cap: int | None = _PAIR_LIMIT
    else:
        cap = None if dimension == "year" else _GROUP_LIMIT
    try:
        rows = (
            state.cross_distribution(dimension, second, **common, limit=cap + 1)
            if second
            else state.distribution(dimension, **common,
                                    limit=_YEAR_LIMIT if cap is None else cap + 1)
        )
    except Exception:
        logger.warning("aggregate_records query failed.", exc_info=True)
        return ToolResult(tool="aggregate_records", entity=bundle, ok=False, error="query failed")
    if not rows:
        missed = _empty_result_miss("aggregate_records", bundle, scope)
        if missed is not None:
            return missed
        return ToolResult(tool="aggregate_records", entity=bundle, ok=False,
                          error="no matching records")
    truncated = cap is not None and len(rows) > cap
    rows = rows[:cap] if cap is not None else rows
    if dimension == "year" and not second:
        rows = sorted(rows, key=lambda row: str(row[0]))
    if second:
        if output_format == "table":
            body = "\n".join(
                [f"| {label} | {second_label} | count |", "| --- | --- | --- |"]
                + [
                    f"| {_md_cell(_group_value(dimension, a))} | "
                    f"{_md_cell(_group_value(second, b))} | {n} |"
                    for a, b, n in rows
                ]
            )
        else:
            body = "\n".join(
                f"- {_group_value(dimension, a)} — {_group_value(second, b)}: {n}"
                for a, b, n in rows
            )
        by = f"by {label} and {second_label}"
        groups = [[a, b, n] for a, b, n in rows]
        dimensions = [group_by or "theme", secondary_group_by]
    else:
        if output_format == "table":
            body = "\n".join(
                [f"| {label} | count |", "| --- | --- |"]
                + [f"| {_md_cell(_group_value(dimension, value))} | {n} |"
                   for value, n in rows]
            )
        else:
            body = "\n".join(f"- {_group_value(dimension, value)}: {n}" for value, n in rows)
        by = f"by {label}"
        groups = [[value, n] for value, n in rows]
        # What was grouped on, not what was requested: a dropped secondary must
        # not leave a caller parsing pairs out of single-dimension rows.
        dimensions = [group_by or "theme"]
    if truncated:
        body += (f"\n\nShowing the {cap} {'pairs' if second else label + 's'} "
                 "with the most items.")
    scope_text = _scope_phrase(scope.effective)
    kind = scope_noun(bundle, by_author=bool(scope.effective.author))
    total = structured_detail.scope_total(common) if detail else None
    if total:
        noun = entity_label(kind, total)
        verb = "breaks" if total == 1 else "break"
        rendered = f"Here's how the {total} {noun}{scope_text} {verb} down {by}:\n" + body
    else:
        rendered = (
            f"Distribution of {entity_label(kind, 2)}{scope_text} "
            f"{by}:\n" + body
        )
    if detail and not second:
        rendered = structured_detail.for_breakdown(
            rows, dimension=dimension, label=label, total=total,
        ).render_onto(rendered)
    return ToolResult(
        tool="aggregate_records", entity=bundle, ok=True,
        data={
            "groups": groups,
            "dimensions": dimensions,
            "applied": _applied_filters(bundle, scope.effective),
        },
        rendered=rendered,
    )


# How many themes a vocabulary enumeration may return. Deliberately NOT the
# list/lookup row limit (`ToolCall.limit`, default 10): that one answers "how
# many items should I show", while this one has to cover the whole vocabulary or
# "how many themes are there?" reports a truncated count as if it were the total.
# Callers pass this explicitly rather than relying on the default (see
# planner._tool_call) so the two limits can never be confused again.
THEME_VOCABULARY_LIMIT = 200


# What a theme listing is allowed to expose. `main` is the default because a
# generic "what themes do you cover?" is a question about the organisation's
# thematic areas, not an inventory of every vocabulary term the CMS happens to
# hold; Other themes are real but peripheral, and answer a different question.
SCOPE_MAIN = "main"
SCOPE_OTHER = "other"
SCOPE_ALL = "all"
THEME_SCOPES = (SCOPE_MAIN, SCOPE_OTHER, SCOPE_ALL)


def _split_by_group(top_level: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Top-level themes bucketed by `theme_group`, as a positive allow-list.

    Matching each group by equality rather than testing `!= 'main'` matters: a
    theme the map does not know carries `NULL`, and `NULL != 'main'` would file
    it under Other — presenting a term discovered in Drupal as part of a curated
    structure it was never added to. Unclassified themes get their own bucket so
    they can be reported without being dressed up as something they are not.
    That bucket is where a theme classified `unknown` lands — it carries no
    group by definition, and it is the reason the bucket is not dead code.
    """
    buckets: dict[str, list[str]] = {SCOPE_MAIN: [], SCOPE_OTHER: [], "unclassified": []}
    for row in top_level:
        group = row.get("theme_group")
        if group == SCOPE_MAIN:
            buckets[SCOPE_MAIN].append(row["theme"])
        elif group == SCOPE_OTHER:
            buckets[SCOPE_OTHER].append(row["theme"])
        else:
            buckets["unclassified"].append(row["theme"])
    return buckets


def list_themes(
    *,
    scope: str = SCOPE_MAIN,
    limit: int = THEME_VOCABULARY_LIMIT,
    output_format: str = "default",
    detail: bool = False,
) -> ToolResult:
    """Enumerate the collection's themes — the **top-level themes only**
    (`theme_type='primary'`, plus themes the map does not know). Rows stored as
    `sub` are never listed: mixing "Air" and "Waste" in with "Climate Change"
    and "Energy" would overstate the count.

    ``scope`` selects which groups are exposed, and defaults to Main:

    * ``"main"`` — Main themes only, and the reported total counts only those.
      A generic theme question gets the curated thematic areas and nothing else.
    * ``"other"`` — Other themes only, for a question that explicitly asks for
      them.
    * ``"all"`` — every group, including themes the map has not classified,
      which are labelled as such rather than folded into Other.

    Reads `documents_theme`, so it lists what documents actually carry, with the
    grouping `app.catalog.theme_taxonomy` materialized at ingest. An empty result
    or a query failure returns ok=False so the caller can fall through to
    semantic search."""
    if scope not in THEME_SCOPES:
        scope = SCOPE_MAIN
    try:
        rows = state.theme_vocabulary(limit=limit)
    except Exception:
        logger.warning("list_themes query failed.", exc_info=True)
        return ToolResult(tool="list_themes", ok=False, error="query failed")
    if not rows:
        return ToolResult(tool="list_themes", ok=False, error="no themes found")

    # Top-level entries: the curated primary tags, plus themes the theme map
    # does not know. An unknown theme *is* top-level — nothing is known to sit
    # above it — and excluding it here is what used to make a theme discovered
    # in the CMS unlistable until someone edited the map. `_split_by_group`
    # keeps it out of the curated groups, so it only ever surfaces under
    # `scope="all"`, labelled as unclassified.
    top_level = [
        r for r in rows
        if r["theme_type"] in ("primary", theme_taxonomy.UNKNOWN)
    ]
    if not top_level:
        return ToolResult(tool="list_themes", ok=False, error="no themes found")

    buckets = _split_by_group(top_level)
    main, other = buckets[SCOPE_MAIN], buckets[SCOPE_OTHER]
    unclassified = buckets["unclassified"]

    if scope == SCOPE_MAIN:
        sections = [("Main themes", main)]
    elif scope == SCOPE_OTHER:
        sections = [("Other themes", other)]
    else:
        sections = [("Main themes", main), ("Other themes", other),
                    ("Unclassified themes", unclassified)]
    sections = [(label, names) for label, names in sections if names]
    if not sections:
        # The requested group is empty — "show me the other themes" when there
        # are none. ok=False so the caller falls through rather than rendering a
        # heading over nothing.
        return ToolResult(
            tool="list_themes", ok=False, error=f"no {scope} themes found"
        )

    listed = [name for _, names in sections for name in names]
    total = len(listed)

    data: dict[str, Any] = {
        "themes": listed, "scope": scope,
        "main_themes": main if scope in (SCOPE_MAIN, SCOPE_ALL) else [],
        "other_themes": other if scope in (SCOPE_OTHER, SCOPE_ALL) else [],
    }
    # A single-group answer needs no group heading; the sentence already says
    # which themes these are, and a lone "Main themes:" label implies a second
    # section that is deliberately absent.
    labelled = len(sections) > 1
    counts = structured_detail.theme_counts() if detail else None
    latest = structured_detail.theme_latest(listed) if detail else None
    body = "\n\n".join(
        _theme_section(label if labelled else "", names, output_format, counts, latest)
        for label, names in sections
    )

    noun = {
        SCOPE_MAIN: "main theme", SCOPE_OTHER: "other theme", SCOPE_ALL: "theme",
    }[scope] + ("" if total == 1 else "s")
    rendered = f"The collection covers {total} {noun}:\n\n{body}"
    if detail:
        rendered = structured_detail.for_themes().render_onto(rendered)
    return ToolResult(
        tool="list_themes", ok=True, data=data, rendered=rendered,
    )


def theme_digest(*, scope: str = SCOPE_MAIN, output_format: str = "default") -> str:
    """How many items each theme holds and its newest one, as a section to
    follow an answer that lists the themes from elsewhere — the home page, which
    describes the themes but cannot say what is in them. '' when the themes or
    their figures cannot be read, leaving that answer as it was."""
    result = list_themes(scope=scope, output_format=output_format)
    names = result.data.get("themes", []) if result.ok else []
    counts = structured_detail.theme_counts() if names else {}
    if not counts:
        return ""
    latest = structured_detail.theme_latest(names)
    return _theme_section("What each theme holds", names, output_format, counts, latest)


def resolve_entity(query: str | None, type: str | None = None) -> ToolResult:
    """Resolve a free-text name to a ranked catalog entity. A confident top
    match accepts; a genuine near-tie asks the user to pick rather than
    silently choosing one (§4 — never guess on ambiguity); nothing plausible is
    reported explicitly as a miss, never as a misleading zero. See
    `app.retrieval.structured.resolve` for the scoring and banding this wraps."""
    from app.retrieval.structured import resolve

    if not query or not query.strip():
        # `ToolCall.query` defaults to None, so a planned call that omits it
        # reaches here — fall through rather than rendering the missing value
        # into a user-facing "No entity matching 'None' found."
        return ToolResult(tool="resolve_entity", entity=type, ok=False,
                          error="no query to resolve")
    try:
        candidates = resolve.resolve_entity(query, type)
    except ValueError as exc:
        return ToolResult(tool="resolve_entity", entity=type, ok=False, error=str(exc))
    except Exception:
        logger.warning("resolve_entity query failed.", exc_info=True)
        return ToolResult(tool="resolve_entity", entity=type, ok=False, error="query failed")

    label = type or "entity"
    data: dict[str, Any] = {
        "candidates": [
            {"id": c.id, "canonical_name": c.canonical_name, "type": c.type,
             "score": round(c.score, 3)}
            for c in candidates
        ]
    }
    if not candidates:
        return ToolResult(tool="resolve_entity", entity=type, ok=False, data=data,
                          error="no matching entity", error_kind="unresolved",
                          rendered=f"No {label} matching '{query}' found.")

    top = candidates[0]
    runner_up_score = candidates[1].score if len(candidates) > 1 else 0.0
    band = resolve.classify_band(top.score, runner_up_score)

    if band == resolve.MISS:
        return ToolResult(tool="resolve_entity", entity=type, ok=False, data=data,
                          error="no confident match", error_kind="unresolved",
                          rendered=f"No {label} matching '{query}' found.")

    if band == resolve.AMBIGUOUS:
        shown = resolve.plausible(candidates)
        options = "\n".join(f"{i}. {c.canonical_name}" for i, c in enumerate(shown, start=1))
        return ToolResult(
            tool="resolve_entity", entity=type, ok=False, data=data,
            error="ambiguous match", error_kind="ambiguous",
            rendered=f"'{query}' matches more than one {label}:\n{options}\nWhich did you mean?",
        )

    data["resolved"] = {"id": top.id, "canonical_name": top.canonical_name,
                        "type": top.type, "score": round(top.score, 3)}
    return ToolResult(
        tool="resolve_entity", entity=type, ok=True, data=data,
        rendered=f"'{query}' resolves to {top.canonical_name} ({top.type}).",
    )
