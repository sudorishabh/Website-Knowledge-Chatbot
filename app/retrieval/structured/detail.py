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
from app.retrieval.structured.entities import entity_label, scope_noun
from app.retrieval.structured.rendering import item_line, record_date
from app.retrieval.structured.types import RecordFilters
from app.schemas.query import Citation

logger = logging.getLogger(__name__)

# Documents shown beneath a count — enough to make the number concrete, few
# enough that the answer stays a count rather than becoming a list. Five since
# 2026-10-04: three read as a teaser beside a type mix and a span of years.
RECENT_ITEMS = 5
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


def _type_rows(common: dict[str, Any], *, limit: int) -> list[tuple[str, int]]:
    """(content type, documents) for the scope, largest first."""
    rows = _safe(
        "types", lambda: state.distribution("bundle", **common, limit=limit), []
    )
    return [(bundle, int(n)) for bundle, n in rows if bundle]


def _mix(rows: Sequence[tuple[str, int]], total: int) -> str:
    """"31 feature articles, 7 research papers and 2 policy briefs" — the
    largest types by name, the remainder of ``total`` folded into one figure."""
    shown = rows[:LEADING_VALUES]
    parts = [f"{n} {entity_label(bundle, n)}" for bundle, n in shown]
    rest = total - sum(n for _, n in shown)
    if len(rows) > LEADING_VALUES and rest > 0:
        parts.append(f"{rest} of other types")
    return _join(parts)


def type_sentence(common: dict[str, Any], total: int) -> str:
    """The content-type mix of a count that spans every type: "a publication
    count" means little until it says how many are papers and how many news."""
    rows = _type_rows(common, limit=LEADING_VALUES + 1)
    if len(rows) < 2:
        return ""
    return f"By type: {_mix(rows, total)}."


def _has_subject(filters: RecordFilters) -> bool:
    """Whether the scope is about someone or something — an author, a theme or
    a tag — rather than the catalog at large."""
    return bool(filters.author or filters.theme or filters.tag)


def other_types_sentence(
    total: int, *, common: dict[str, Any], bundle: str | None,
    filters: RecordFilters, scope: str,
) -> str:
    """What else the same person, theme or tag has, beneath a count or list of
    one content type: "10 research papers by Suneel Pandey" reads better beside
    the 12 feature articles, 11 articles and 2 policy briefs that make up the
    rest of the 35. ``scope`` is the headline's phrase (" by Dr Suneel Pandey").
    Nothing for an unscoped count, or when the type is all there is."""
    if not bundle or not _has_subject(filters):
        return ""
    wider = {**common, "bundle": None}
    everything = _safe("all types", lambda: state.count_documents(**wider), 0)
    if not everything or everything <= total:
        return ""
    others = [(b, n) for b, n in _type_rows(wider, limit=LEADING_VALUES + 2) if b != bundle]
    if not others:
        return ""
    rest = everything - total
    noun = entity_label(scope_noun(None, by_author=bool(filters.author)), everything)
    return (f"Beyond these, there {'is' if rest == 1 else 'are'} {_mix(others, rest)}"
            f"{scope} — {everything} {noun} in all.")


def named_type_sentence(named_type: str, n: int | None, *, of: str) -> str:
    """That an answer spans more than the type the question's word names, and
    how many it holds of that type. "Article" is both one category on the site
    and everyday English for anything a person writes, so "how many articles by
    Vibha Dhawan" counts all 41 publications and says that 1 is filed as an
    article — the narrow reading answered too, not replaced. ``of`` says what
    the figure is out of ("them", "the 41"). Nothing when the figure is unknown."""
    if n is None:
        return ""
    category = entity_label(named_type, 1).capitalize()
    return (f"That spans every type of publication, not only the site's {category} "
            f"category, which holds {n or 'none'} of {of}.")


def recent_items(
    common: dict[str, Any], *, total: int, with_type: bool,
    listed_by: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The newest documents in the scope, as linked items with their authors
    and citations — only the co-authors when the scope is one author's
    (``listed_by``). All of them when there are no more than `RECENT_ITEMS`."""
    # A few spare rows, because the site publishes some pages twice under one
    # title (71 titles, the copy's URL ending "-0"), and a short list naming
    # the same paper twice reads as a mistake.
    fetched = _safe(
        "recent", lambda: state.list_documents(**common, limit=RECENT_ITEMS + 3), []
    )
    seen: set[str] = set()
    records = []
    for r in fetched:
        key = (r.title or r.document_id or "").casefold()
        if key not in seen:
            seen.add(key)
            records.append(r)
    records = records[:RECENT_ITEMS]
    if not records:
        return "", []
    if total <= len(records):
        lead = "Here it is:" if total == 1 else "Here they are:"
    else:
        lead = "The most recent:"
    found = facets(records)
    lines = [
        item_line(r, with_type=with_type, listed_by=listed_by,
                  authors=found.get(r.document_id, {}).get("authors", ()))
        for r in records
    ]
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
    total: int, *, common: dict[str, Any], bundle: str | None, filters: RecordFilters,
    named_type: str | None = None, scope: str = "",
) -> Detail:
    """Beneath "There are N <items>": how many are of the type the question
    named, when the count was widened past it; when they date from; their type
    mix (when the count spans types), or what else the same person, theme or
    tag has (when it does not); the most recent few; and what to ask next.
    ``scope`` is the headline's own scope phrase (" by Dr Vibha Dhawan in 2024")."""
    detail = Detail()
    if total <= 0:
        return detail
    years = _years(common)
    named = ""
    if named_type and bundle is None:
        n = _safe("named type",
                  lambda: state.count_documents(**{**common, "bundle": named_type}), None)
        named = named_type_sentence(named_type, n, of="them")
    sentences = [
        named,
        year_sentence(years, total=total,
                      period_fixed=bool(filters.date_from or filters.date_to)),
        type_sentence(common, total) if bundle is None and total > 1 else "",
        other_types_sentence(total, common=common, bundle=bundle, filters=filters,
                             scope=scope),
    ]
    detail.add(" ".join(s for s in sentences if s))
    items, citations = recent_items(common, total=total, with_type=bundle is None,
                                    listed_by=filters.author)
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
        wider = {**common, "bundle": None}
        n = _safe("all types", lambda: state.count_documents(**wider), 0)
        if n:
            # What the scope does hold, and the newest of it: "no reports by
            # Vibha Dhawan" is better followed by the 41 publications there are.
            kind = scope_noun(None, by_author=bool(filters.author))
            rows = _type_rows(wider, limit=LEADING_VALUES + 1)
            mix = f": {_mix(rows, n)}" if rows else ""
            detail.add(
                f"Across all content types there {'is' if n == 1 else 'are'} {n} "
                f"{entity_label(kind, n)}{scope_without_type}{mix}."
            )
            items, citations = recent_items(wider, total=n, with_type=True,
                                            listed_by=filters.author)
            detail.add(items)
            detail.citations = citations
    return detail


def facets(records: Sequence[Any]) -> dict[str, dict[str, list[str]]]:
    """Authors and themes for the documents a list shows; {} on failure, which
    leaves the list exactly as it renders without detail."""
    ids = [getattr(r, "document_id", None) for r in records]
    return _safe("facets", lambda: state.facets_for([i for i in ids if i]), {})


def author_list_mix(
    shown: int, *, total: int | None, common: dict[str, Any], bundle: str | None,
    filters: RecordFilters, scope: str,
) -> str:
    """For a list of one author's work: what else they published, when it is
    one type ("list research papers by Vibha Dhawan" is 7 of 41), or the type
    mix of a list that spans types and was cut short. Nothing for any other list,
    whose rows answer what was asked."""
    if not filters.author:
        return ""
    if bundle:
        return other_types_sentence(total or shown, common=common, bundle=bundle,
                                    filters=filters, scope=scope)
    if total and total > shown:
        return type_sentence(common, total)
    return ""


def for_list(
    shown: int, *, total: int | None, filters: RecordFilters,
    named: tuple[str, int | None] | None = None, mix: str = "",
) -> Detail:
    """Beneath a list: how many are of the type the question named, when the
    list was widened past it (``named`` is that type and its count); ``mix``
    (see `author_list_mix`); for a single document, that its content can be
    asked about; for a page cut from a larger set, how to narrow it. Only the
    dimensions the question has not already fixed are offered."""
    detail = Detail()
    # One document already shows its own type.
    if named is not None and (total or shown) > 1:
        detail.add(named_type_sentence(*named, of=f"the {total or shown}"))
    detail.add(mix)
    if shown == 1 and not total:
        detail.add("Ask me what it says, and I'll answer from the document itself.")
        return detail
    if not total or total <= shown:
        return detail
    dimensions = [word for word, fixed in (
        ("year", filters.date_from or filters.date_to),
        ("theme", filters.theme),
        ("author", filters.author),
    ) if not fixed]
    if dimensions:
        options = (dimensions[0] if len(dimensions) == 1
                   else f"{', '.join(dimensions[:-1])} or {dimensions[-1]}")
        detail.add(f"You can ask me to narrow these down by {options}.")
    return detail


def theme_counts() -> dict[str, int]:
    """Items per theme, counted exactly as "how many items on <theme>" counts
    them — website content, the theme by exact name — so the figure beside a
    theme in a listing is the one a follow-up count will give."""
    rows = _safe(
        "theme counts",
        lambda: state.distribution("theme", source_type="website",
                                   entity_type="node", limit=100),
        [],
    )
    return {str(theme): int(n) for theme, n in rows}


def theme_latest(names: Sequence[str]) -> dict[str, Any]:
    """Each theme's newest item, over the same scope `theme_counts` counts —
    so "Energy — 1154 items, latest: ..." names one of those 1154. A theme
    whose read fails or holds nothing is left out, and its line shows the count
    alone."""
    latest: dict[str, Any] = {}
    for name in names:
        rows = _safe(
            f"latest on {name}",
            lambda name=name: state.list_documents(
                source_type="website", entity_type="node", theme=name, limit=1),
            [],
        )
        if rows:
            latest[name] = rows[0]
    return latest


def for_themes() -> Detail:
    """Beneath a theme listing: what can be asked about any one of them."""
    detail = Detail()
    detail.add("You can ask me about the work under any of these — for example, "
               "how many reports there are on one, or which are the latest.")
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
