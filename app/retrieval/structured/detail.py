"""What a catalog answer says beyond its headline.

"How many articles are there on climate change?" used to be answered with one
sentence — "There are 68 articles on 'Climate Change' matching your query." —
correct, and nothing else. The scope behind that number already knows more:
when its documents date from, which kinds they are, and which are newest. This
module reads those from the catalog, over exactly the filters the headline used,
so the lines it adds can never disagree with the number above them.

It also lays them out the way a generated answer is laid out (see
`app.retrieval.structured.rendering`): sentences that continue the headline's
paragraph, then titled sections of one fact per line — "By type", "Latest
publications" — and an offer of what to ask next, in italics.

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
from app.retrieval.structured.rendering import (
    figure, followup, item_line, number, record_date, section, tally,
)
from app.retrieval.structured.types import RecordFilters
from app.schemas.query import Citation

logger = logging.getLogger(__name__)

# Documents shown beneath a count — enough to make the number concrete, few
# enough that the answer stays a count rather than becoming a list. Five since
# 2026-10-04: three read as a teaser beside a type mix and a span of years.
RECENT_ITEMS = 5
# Values a distinct count names beneath its number, and content types a type
# breakdown names before folding the rest into "Other types".
LEADING_VALUES = 5
# Content types read for a breakdown: every one the catalog holds (15), so the
# type a question asked about is found even when it is not among the largest.
_ALL_TYPES = 30

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
    """What follows a headline: sentences that continue its paragraph
    (``lead``), then sections, and citations for any documents named."""

    lead: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)

    def add_lead(self, text: str | None) -> None:
        if text:
            self.lead.append(text)

    def add(self, text: str | None) -> None:
        if text:
            self.sections.append(text)

    def render_onto(self, headline: str) -> str:
        opening = " ".join([headline, *self.lead])
        return "\n\n".join([opening, *self.sections])


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


def _type_name(bundle: str) -> str:
    """A content type as a category: "Feature articles"."""
    return entity_label(bundle, 2).capitalize()


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


def _busiest(years: list[tuple[int, int]]) -> str:
    """", and 2023 was the busiest year, with 9" — or nothing when no year
    stands out: a peak of one, or three or more years tied for it, says nothing."""
    top = max(n for _, n in years)
    tied = [year for year, n in years if n == top]
    if top < 2 or len(tied) > 2:
        return ""
    if len(tied) == 2:
        return f", and {tied[0]} and {tied[1]} were the busiest years, with {number(top)} each"
    return f", and {tied[0]} was the busiest year, with {number(top)}"


def year_sentence(
    years: list[tuple[int, int]], *, total: int, period_fixed: bool
) -> str:
    """When the scope's documents date from: "They span 2003 to 2026, and 2023
    was the busiest year, with 9." Nothing for a single document, and nothing
    for a single year the question already fixed."""
    if not years or total < 2:
        return ""
    if len(years) == 1:
        if period_fixed or sum(n for _, n in years) < total:
            return ""
        return f"All of them are from {years[0][0]}."
    first, last = years[0][0], years[-1][0]
    return f"They span {first} to {last}{_busiest(years)}."


def _type_rows(common: dict[str, Any], *, limit: int = _ALL_TYPES) -> list[tuple[str, int]]:
    """(content type, documents) for the scope, largest first."""
    rows = _safe(
        "types", lambda: state.distribution("bundle", **common, limit=limit), []
    )
    return [(bundle, int(n)) for bundle, n in rows if bundle]


def type_tally(
    rows: Sequence[tuple[str, int]], total: int, *, highlight: str | None = None,
) -> str:
    """The content-type lines of a breakdown: the largest types by name, the
    one the question asked about (``highlight``) always shown and in bold, and
    the rest of ``total`` folded into "Other types"."""
    shown = list(rows[:LEADING_VALUES])
    if highlight and highlight not in {b for b, _ in shown}:
        shown += [(b, n) for b, n in rows if b == highlight]
    lines = [(_type_name(b), n) for b, n in shown]
    rest = total - sum(n for _, n in shown)
    if len(rows) > len(shown) and rest > 0:
        lines.append(("Other types", rest))
    return tally(lines, highlight=_type_name(highlight) if highlight else None)


def type_section(
    common: dict[str, Any], total: int, *, highlight: str | None = None,
) -> str:
    """"### By type" over the content-type mix of a count that spans every
    type: a publication count means little until it says how many are papers
    and how many news. Nothing when there is only one type."""
    rows = _type_rows(common)
    if len(rows) < 2:
        return ""
    return section("By type", type_tally(rows, total, highlight=highlight))


def _has_subject(filters: RecordFilters) -> bool:
    """Whether the scope is about someone or something — an author, a theme or
    a tag — rather than the catalog at large."""
    return bool(filters.author or filters.theme or filters.tag)


def other_types_section(
    total: int, *, common: dict[str, Any], bundle: str | None,
    filters: RecordFilters, scope: str,
) -> str:
    """Everything the same person, theme or tag has, beneath a count or list of
    one content type, with that type in bold: "10 research papers by Suneel
    Pandey" reads better beside the 12 feature articles, 11 articles and 2
    policy briefs that make up the rest of the 35. ``scope`` is the headline's
    scope in plain words (" by Dr Suneel Pandey"). Nothing for an unscoped
    count, or when the type is all there is."""
    if not bundle or not _has_subject(filters):
        return ""
    wider = {**common, "bundle": None}
    everything = _safe("all types", lambda: state.count_documents(**wider), 0)
    if not everything or everything <= total:
        return ""
    rows = _type_rows(wider)
    if not any(b != bundle for b, _ in rows):
        return ""
    noun = entity_label(scope_noun(None, by_author=bool(filters.author)), everything)
    return section(f"All {noun}{scope} ({number(everything)})",
                   type_tally(rows, everything, highlight=bundle))


def named_type_sentence(named_type: str, n: int | None, *, of: str) -> str:
    """That an answer spans more than the type the question's word names, and
    how many it holds of that type. "Article" is both one category on the site
    and everyday English for anything a person writes, so "how many articles by
    Vibha Dhawan" counts all 41 publications and says that 1 is filed as an
    article — the narrow reading answered too, not replaced. ``of`` says what
    the figure is out of ("them", "the 41"). Nothing when the figure is unknown."""
    if n is None:
        return ""
    category = _type_name(named_type)
    held = f"only {number(n)} of {of} is" if n else f"none of {of} is"
    return (f"That counts every kind of publication; {held} filed under "
            f"*{category}* on the site.")


def _latest_title(kind: str, *, total: int, shown: int) -> str:
    """"Latest publications" over a cut; "Both policy briefs" or "All 3
    research papers" when every one is shown; "The article" for one."""
    plural = entity_label(kind, 2)
    if total > shown:
        return f"Latest {plural}"
    if total == 1:
        return f"The {entity_label(kind, 1)}"
    if total == 2:
        return f"Both {plural}"
    return f"All {number(total)} {plural}"


def recent_items(
    common: dict[str, Any], *, total: int, with_type: bool, kind: str,
    listed_by: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The newest documents in the scope, as a "Latest ..." section of linked
    items with their authors and citations — only the co-authors when the scope
    is one author's (``listed_by``). All of them when there are no more than
    `RECENT_ITEMS`. ``kind`` names them ("publications", "research_papers")."""
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
    title = _latest_title(kind, total=total, shown=len(records))
    return section(title, "\n".join(lines)), citations


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
        return followup("You can ask me to list them.")
    return followup(
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
    """Beneath "There are N <items>": when they date from (continuing the
    headline); how many are of the type the question named, when the count was
    widened past it; the type mix (when the count spans types), or everything
    the same person, theme or tag has (when it does not); the most recent few;
    and what to ask next. ``scope`` is the headline's scope in plain words
    (" by Dr Vibha Dhawan in 2024")."""
    detail = Detail()
    if total <= 0:
        return detail
    years = _years(common)
    detail.add_lead(year_sentence(
        years, total=total, period_fixed=bool(filters.date_from or filters.date_to)))
    if named_type and bundle is None:
        n = _safe("named type",
                  lambda: state.count_documents(**{**common, "bundle": named_type}), None)
        detail.add(named_type_sentence(named_type, n, of="them"))
    if bundle is None and total > 1:
        detail.add(type_section(common, total, highlight=named_type))
    detail.add(other_types_section(total, common=common, bundle=bundle,
                                   filters=filters, scope=scope))
    kind = scope_noun(bundle, by_author=bool(filters.author))
    items, citations = recent_items(common, total=total, with_type=bundle is None,
                                    kind=kind, listed_by=filters.author)
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
    type: "no reports by Vibha Dhawan" is better followed by the 41 publications
    there are, by type, and the newest of them. ``scope_without_period`` reads
    in a sentence (names in bold); ``scope_without_type`` heads a section."""
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
                f"Across all dates there {'is' if n == 1 else 'are'} "
                f"{figure(n, noun(n))}{scope_without_period}{tail}"
            )
        return detail
    if bundle and (filters.author or filters.theme or filters.tag
                   or filters.title_contains):
        wider = {**common, "bundle": None}
        n = _safe("all types", lambda: state.count_documents(**wider), 0)
        if n:
            kind = scope_noun(None, by_author=bool(filters.author))
            rows = _type_rows(wider)
            title = f"All {entity_label(kind, n)}{scope_without_type} ({number(n)})"
            detail.add(section(title, type_tally(rows, n)) if rows else "")
            items, citations = recent_items(wider, total=n, with_type=True,
                                            kind=kind, listed_by=filters.author)
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
    """For a list of one author's work: everything they published, when the
    list is of one type ("list research papers by Vibha Dhawan" is 7 of 41), or
    the type mix of a list that spans types and was cut short. Nothing for any
    other list, whose rows answer what was asked."""
    if not filters.author:
        return ""
    if bundle:
        return other_types_section(total or shown, common=common, bundle=bundle,
                                   filters=filters, scope=scope)
    if total and total > shown:
        return type_section(common, total)
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
        detail.add(named_type_sentence(*named, of=f"the {number(total or shown)}"))
    detail.add(mix)
    if shown == 1 and not total:
        detail.add(followup("Ask me what it says, and I'll answer from the document itself."))
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
        detail.add(followup(f"You can ask me to narrow these down by {options}."))
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
    so "Energy — 1,154 items, latest: ..." names one of those 1,154. A theme
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
    detail.add(followup("You can ask me about the work under any of these — for "
                        "example, how many reports there are on one, or which "
                        "are the latest."))
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

    followers = _join([f"{name(value)} ({number(n)})" for value, n in rows[1:3]])
    top, n = name(rows[0][0]), rows[0][1]
    if dimension == "author":
        return f"**{top}** has the most ({number(n)}), followed by {followers}."
    return f"The largest is **{top}** ({number(n)}), followed by {followers}."


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
                             f"add up to more than {number(total)}.")
        detail.add(" ".join(s for s in sentences if s))
    detail.add(followup("You can ask me to list the items behind any of these."))
    return detail


def for_distinct(
    total: int, *, dimension: str, common: dict[str, Any]
) -> Detail:
    """Beneath "There are N themes / author names / content types / years":
    the values themselves, one per line — all of them when few, else the
    largest under a heading that says so."""
    detail = Detail()
    if total <= 0:
        return detail
    if dimension == "year":
        years = _years(common)
        if len(years) > 1:
            detail.add_lead(f"They run from {years[0][0]} to {years[-1][0]}"
                            f"{_busiest(years)}.")
        return detail
    rows = _safe(
        "values",
        lambda: state.distribution(dimension, **common, limit=LEADING_VALUES),
        [],
    )
    if not rows:
        return detail
    lines = tally([
        (_type_name(str(value)) if dimension == "bundle" else str(value), int(n))
        for value, n in rows
    ])
    if total <= len(rows):
        detail.add(lines)
        return detail
    title = "Names that appear most often" if dimension == "author" else "The largest"
    detail.add(section(title, lines))
    # "More", not "all": a breakdown shows its largest groups, not every one.
    detail.add(followup(
        f"Ask for a breakdown by {_DIMENSION_WORDS[dimension]} to see more of them."))
    return detail
