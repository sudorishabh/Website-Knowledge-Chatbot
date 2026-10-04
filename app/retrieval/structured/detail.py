"""What a catalog answer says beyond its headline.

"How many articles are there on climate change?" used to be answered with one
sentence — "There are 68 articles on 'Climate Change' matching your query." —
correct, and nothing else. The scope behind that number already knows more:
when its documents date from, which kinds they are, and which are newest. This
module reads those from the catalog, over exactly the filters the headline used,
so the lines it adds can never disagree with the number above them.

Deterministic and fail-open: no model call, and a query that fails costs its own
section and nothing else. With `catalog_answer_detail_enabled` off the headline
stands alone, as it always did.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from app.catalog import queries as state
from app.retrieval.structured.entities import entity_label
from app.retrieval.structured.rendering import item_line, record_date
from app.retrieval.structured.types import RecordFilters
from app.schemas.query import Citation

logger = logging.getLogger(__name__)

# Documents shown beneath a count — enough to make the number concrete, few
# enough that the answer stays a count rather than becoming a list.
RECENT_ITEMS = 3
# Values a distinct count names beneath its number.
LEADING_VALUES = 5

# What each distinct-count dimension is called when offering its breakdown.
_DIMENSION_WORDS = {
    "author": "author", "theme": "theme", "bundle": "content type", "year": "year",
}


def enabled() -> bool:
    """Whether detail sections are switched on. Read with a default, as
    `topic.enabled` is, so a test's partial settings stub gets the shipped
    behaviour rather than an AttributeError."""
    from app.config import get_settings

    return bool(getattr(get_settings(), "catalog_answer_detail_enabled", True))


@dataclass
class Detail:
    """Sections to follow a headline, and citations for any documents named."""

    sections: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)

    def add(self, text: str | None) -> None:
        if text:
            self.sections.append(text)

    def render_onto(self, headline: str) -> str:
        return "\n\n".join([headline, *self.sections])


def _safe(label: str, read: Callable[[], Any], default: Any) -> Any:
    try:
        return read()
    except Exception:
        logger.debug("Catalog detail %r failed; leaving it out.", label, exc_info=True)
        return default


def _join(parts: Sequence[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


# --------------------------------------------------------------------------- #
# The pieces.
# --------------------------------------------------------------------------- #

def _years(common: dict[str, Any]) -> list[tuple[int, int]]:
    """(year, documents) for the scope, oldest first; undated rows excluded."""
    rows = _safe("years", lambda: state.distribution("year", **common, limit=100), [])
    years: list[tuple[int, int]] = []
    for value, n in rows:
        try:
            years.append((int(value), int(n)))
        except (TypeError, ValueError):
            continue
    return sorted(years)


def _peak(years: list[tuple[int, int]]) -> str:
    """", with the most (13) in 2025" — or nothing when no year stands out:
    a peak of one, or three or more years tied for it, says nothing."""
    top = max(n for _, n in years)
    tied = [year for year, n in years if n == top]
    if top < 2 or len(tied) > 2:
        return ""
    if len(tied) == 2:
        return f", with the most ({top} each) in {tied[0]} and {tied[1]}"
    return f", with the most ({top}) in {tied[0]}"


def year_sentence(
    years: list[tuple[int, int]], *, total: int, period_fixed: bool
) -> str:
    """When the scope's documents date from. Nothing for a single document, and
    nothing for a single year the question already fixed."""
    if not years or total < 2:
        return ""
    if len(years) == 1:
        if period_fixed or sum(n for _, n in years) < total:
            return ""
        return f"All of them date from {years[0][0]}."
    first, last = years[0][0], years[-1][0]
    return f"They date from {first} to {last}{_peak(years)}."


def type_sentence(common: dict[str, Any], total: int) -> str:
    """The content-type mix of a count that spans every type: "a publication
    count" means little until it says how many are papers and how many news."""
    rows = _safe(
        "types",
        lambda: state.distribution("bundle", **common, limit=LEADING_VALUES + 1),
        [],
    )
    rows = [(bundle, n) for bundle, n in rows if bundle]
    if len(rows) < 2:
        return ""
    shown = rows[:LEADING_VALUES]
    parts = [f"{n} {entity_label(bundle, n)}" for bundle, n in shown]
    rest = total - sum(n for _, n in shown)
    if len(rows) > LEADING_VALUES and rest > 0:
        parts.append(f"{rest} of other types")
    return f"By type: {_join(parts)}."


def recent_items(
    common: dict[str, Any], *, total: int, with_type: bool
) -> tuple[str, list[dict[str, Any]]]:
    """The newest documents in the scope, as linked items with citations. All
    of them when there are no more than `RECENT_ITEMS`."""
    records = _safe(
        "recent", lambda: state.list_documents(**common, limit=RECENT_ITEMS), []
    )
    if not records:
        return "", []
    if total <= len(records):
        lead = "Here it is:" if total == 1 else "Here they are:"
    else:
        lead = "The most recent:"
    lines = [item_line(r, with_type=with_type) for r in records]
    citations = [
        Citation(
            n=i, type="website", title=r.title, url=r.url,
            document_id=r.document_id or None,
        ).model_dump()
        for i, r in enumerate(records, start=1)
    ]
    return lead + "\n" + "\n".join(lines), citations


def _count_followup(
    total: int, *, years: list[tuple[int, int]], bundle: str | None,
    filters: RecordFilters,
) -> str:
    """What to ask next. Only follow-ups the catalog route answers from the
    conversation are offered — "list them" and "break them down by year" were
    checked live against the turn before them — and only dimensions that vary."""
    if total <= RECENT_ITEMS:
        return ""
    dimensions = []
    if len(years) > 1:
        dimensions.append("year")
    if not bundle:
        dimensions.append("content type")
    elif not filters.theme:
        dimensions.append("theme")
    if not dimensions:
        return "You can ask me to list them."
    return (
        "You can ask me to list them, or to break them down by "
        f"{' or '.join(dimensions[:2])}."
    )


# --------------------------------------------------------------------------- #
# Entry points, one per headline shape.
# --------------------------------------------------------------------------- #

def for_count(
    total: int, *, common: dict[str, Any], bundle: str | None, filters: RecordFilters
) -> Detail:
    """Beneath "There are N <items>": when they date from, their type mix (when
    the count spans types), the most recent few, and what to ask next."""
    detail = Detail()
    if total <= 0:
        return detail
    years = _years(common)
    sentences = [
        year_sentence(years, total=total,
                      period_fixed=bool(filters.date_from or filters.date_to)),
        type_sentence(common, total) if bundle is None and total > 1 else "",
    ]
    detail.add(" ".join(s for s in sentences if s))
    items, citations = recent_items(common, total=total, with_type=bundle is None)
    detail.add(items)
    detail.citations = citations
    detail.add(_count_followup(total, years=years, bundle=bundle, filters=filters))
    return detail


def for_zero(
    *, common: dict[str, Any], bundle: str | None, filters: RecordFilters,
    noun: Callable[[int], str], scope_without_period: str, scope_without_type: str,
) -> Detail:
    """Beneath an honest zero: the nearest scope that is not empty.

    A zero under a period most often means the period, not the subject — "no
    events in 2030" is better followed by how many there are at all and how
    recent the newest is. Failing that, a zero under a content type may be the
    type: "no events on Waste" can still have articles on it."""
    detail = Detail()
    if filters.date_from or filters.date_to:
        wider = {k: v for k, v in common.items()
                 if k not in ("effective_from", "effective_to")}
        n = _safe("all dates", lambda: state.count_documents(**wider), 0)
        if n:
            latest = _safe("latest", lambda: state.list_documents(**wider, limit=1), [])
            when = record_date(latest[0]) if latest else ""
            tail = f"; the most recent is from {when}." if when else "."
            detail.add(
                f"Across all dates there {'is' if n == 1 else 'are'} {n} "
                f"{noun(n)}{scope_without_period}{tail}"
            )
        return detail
    if bundle and (filters.author or filters.theme or filters.tag
                   or filters.title_contains):
        n = _safe("all types",
                  lambda: state.count_documents(**{**common, "bundle": None}), 0)
        if n:
            detail.add(
                f"Across all content types there {'is' if n == 1 else 'are'} {n} "
                f"{entity_label('items', n)}{scope_without_type}."
            )
    return detail


def scope_total(common: dict[str, Any]) -> int | None:
    """How many documents a breakdown's scope holds, or None when unknown —
    the total a breakdown leads with, counted over the same filters."""
    return _safe("total", lambda: state.count_documents(**common), None) or None


def _leader(rows: Sequence[tuple[Any, int]], dimension: str) -> str:
    """"Energy is the largest (40), followed by ..." — nothing when the top two
    tie, since then nothing leads. An author "has the most" rather than being
    the largest of anything."""
    if len(rows) < 2 or rows[0][1] == rows[1][1]:
        return ""

    def name(value: Any) -> str:
        return entity_label(str(value), 2) if dimension == "bundle" else str(value)

    followers = _join([f"{name(value)} ({n})" for value, n in rows[1:3]])
    top, n = name(rows[0][0]), rows[0][1]
    if dimension == "author":
        return f"{top} has the most ({n}), followed by {followers}."
    return f"The largest is {top} ({n}), followed by {followers}."


def for_breakdown(
    rows: Sequence[tuple[Any, int]], *, dimension: str, label: str,
    total: int | None,
) -> Detail:
    """Beneath a one-dimension breakdown: what it shows at a glance — the span
    and peak of a year breakdown, the leading group of any other — and, for a
    facet a document can carry several of, why the groups outnumber the total."""
    detail = Detail()
    if not rows:
        return detail
    if dimension == "year":
        years: list[tuple[int, int]] = []
        for value, n in rows:
            try:
                years.append((int(value), int(n)))
            except (TypeError, ValueError):
                continue
        detail.add(year_sentence(sorted(years), total=total or sum(n for _, n in years),
                                 period_fixed=False))
    else:
        sentences = [_leader(rows, dimension)]
        if (total and dimension in ("theme", "author")
                and sum(n for _, n in rows) > total):
            sentences.append(f"An item can carry more than one {label}, so these "
                             f"add up to more than {total}.")
        detail.add(" ".join(s for s in sentences if s))
    detail.add("You can ask me to list the items behind any of these.")
    return detail


def for_distinct(
    total: int, *, dimension: str, common: dict[str, Any]
) -> Detail:
    """Beneath "There are N themes / author names / content types / years":
    the values themselves — all of them when few, else the largest."""
    detail = Detail()
    if total <= 0:
        return detail
    if dimension == "year":
        years = _years(common)
        if len(years) > 1:
            detail.add(f"They run from {years[0][0]} to {years[-1][0]}{_peak(years)}.")
        return detail
    rows = _safe(
        "values",
        lambda: state.distribution(dimension, **common, limit=LEADING_VALUES),
        [],
    )
    if not rows:
        return detail
    if dimension == "bundle":
        named = [f"{entity_label(value, 2)} ({n})" for value, n in rows]
    else:
        named = [f"{value} ({n})" for value, n in rows]
    if total <= len(rows):
        lead = "They are"
    elif dimension == "author":
        lead = "The names that appear most often are"
    else:
        lead = "The largest are"
    detail.add(f"{lead} {_join(named)}.")
    if total > len(rows):
        # "More", not "all": a breakdown shows its largest groups, not every one.
        detail.add(
            f"Ask for a breakdown by {_DIMENSION_WORDS[dimension]} to see more of them."
        )
    return detail
