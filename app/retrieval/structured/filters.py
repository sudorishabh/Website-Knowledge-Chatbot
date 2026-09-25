"""Scope resolver: normalized RecordFilters -> catalog backing kwargs.

Holds the application's structured-scope business rules in one place (previously
duplicated across state, catalog, the structured answerer, and query_processor):

- free-text author and theme names are canonicalized by fuzzy matching against
  what the catalog stores, so a misspelling filters on the real name;
- tags are matched by exact name instead (a long-tail vocabulary where fuzzy
  ranking would flag an ambiguity on almost every query);
- a theme filters on its exact canonical name, never widened to other themes;
- dates are a half-open [from, to) interval;
- resolution failures degrade rather than raise.

Canonicalization happens **here**, not in a separate planner step, because the
plan's tool calls execute in parallel with no data flow between them — a
`resolve_entity` call could never hand its result to a sibling `count_records`.
Resolving on the way to SQL instead means every tool benefits regardless of how
the plan is shaped. `tools.resolve_entity` remains for the one thing this path
cannot do: asking the user which of several close matches they meant.

The resolver produces kwargs the `state.*` catalog readers accept, keyed by name
— themes live in `documents_theme`, tags in `documents_tag`, and taxonomy UUIDs
only in Qdrant payloads (see docs/retire-term-tables-plan.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.core.dates import parse_iso_date
from app.retrieval.structured import topic
from app.retrieval.structured.types import RecordFilters

logger = logging.getLogger(__name__)

# Entity kinds this module canonicalizes. `resolve` advertises author/bundle/
# theme as *tool* types; tag is matched the same way here because it now has its
# own facet table, it just is not offered as a resolve_entity type (see
# docs/database-retrieval-redesign.md §3).
_THEME = "theme"
_TAG = "tag"


def _typed(filters: RecordFilters, kind: str) -> str:
    """The value the user actually typed for `kind` — what a clarification
    question must quote, rather than the candidate it nearly matched."""
    return getattr(filters, kind, None) or ""


@dataclass(frozen=True)
class AmbiguousFilter:
    """A filter whose name matched several catalog entities closely enough that
    picking one would be a guess. Surfaced by the tools as a clarification
    question instead of being silently resolved to the top hit (§4)."""

    kind: str
    query: str
    candidates: list[str]


@dataclass(frozen=True)
class _NameMatch:
    """Outcome of canonicalizing one free-text entity name.

    `name` is always what to filter on: the canonical name when matching found
    one, else the string as typed — a filter is never silently dropped. `band`
    is None when matching was skipped or failed, which callers read as "no
    opinion" rather than as a miss."""

    name: str | None
    band: str | None = None
    candidates: list[str] = field(default_factory=list)


def _resolve_tag_name(value: str) -> _NameMatch:
    """Match a tag by exact name, case-insensitively — never fuzzily.

    Tags are a long-tail freeform vocabulary (thousands of entries, many
    near-duplicates like "Solid waste" / "Urban waste" / "Waste management"), so
    ranking them by similarity would flag an ambiguity on almost every query. A
    hit returns the stored casing; anything else is a miss, which the caller only
    acts on if the query then also finds nothing."""
    from app.catalog import queries
    from app.retrieval.structured import resolve

    try:
        found = queries.find_tag(value)
    except Exception:
        logger.warning("Tag lookup failed; filtering on the name as typed.", exc_info=True)
        return _NameMatch(value)
    return _NameMatch(found, resolve.ACCEPT) if found else _NameMatch(value, resolve.MISS)


def _resolve_name(kind: str, value: str | None) -> _NameMatch:
    """Canonicalize a free-text entity name against what the catalog stores.

    Unconditional, not gated by `entity_resolution_enabled`: theme and tag
    filters match names **exactly** now (see `queries._catalog_filters`), so
    canonicalizing is part of how filtering works rather than an enhancement on
    top of it. The flag gates what the caller *does* with an ambiguous or missing
    match (ask / report vs. carry on), not whether names are matched at all.
    """
    if not value:
        return _NameMatch(None)
    if kind == _TAG:
        return _resolve_tag_name(value)
    from app.retrieval.structured import resolve

    try:
        candidates = resolve.resolve_entity(value, kind)
    except Exception:
        logger.warning(
            "%s name resolution failed; filtering on the name as typed.", kind, exc_info=True
        )
        return _NameMatch(value)
    if not candidates:
        return _NameMatch(value, resolve.MISS)
    top = candidates[0]
    runner_up = candidates[1].score if len(candidates) > 1 else 0.0
    band = resolve.classify_band(top.score, runner_up)
    if band == resolve.MISS:
        return _NameMatch(value, band)
    # Accepted outright, or ambiguous — either way the top candidate is the best
    # available name. Whether an ambiguous match is acted on or quietly taken is
    # the caller's decision (see `resolve_filters`).
    return _NameMatch(
        top.canonical_name, band,
        [c.canonical_name for c in resolve.plausible(candidates)],
    )


def _parse_date(value: str | None, *, field: str = "date") -> datetime | None:
    """Thin alias kept for the tools that render a date scope. See
    :func:`app.core.dates.parse_iso_date`."""
    return parse_iso_date(value, field=field)


def resolve_theme(theme: str | None) -> str | None:
    """The theme name to filter on, canonicalized against `documents_theme`.
    The SQL layer matches it exactly — see `queries._catalog_filters`."""
    return _resolve_name(_THEME, theme).name


def resolve_tag(tag: str | None) -> str | None:
    """The tag name to filter on, canonicalized against `documents_tag`.

    Symmetric with :func:`resolve_theme` — tags have their own facet table, so
    unlike the previous UUID-linked design there is no longer a case where a tag
    has nothing to match against."""
    return _resolve_name(_TAG, tag).name


@dataclass
class ResolvedScope:
    """Backing kwargs for the state.* catalog readers, plus resolution flags.

    `author` / `theme` / `tag` are the names that reach SQL — canonical when
    matching identified one, else as typed. All three are matched the same way
    now that each has its own facet table; there is no longer a filter that has
    nothing to match against.

    `effective` is the same filter set with those canonical names substituted, so
    an answer states the entity it really filtered on rather than the user's
    spelling. `ambiguous` is set when a name matched several entities too closely
    to choose between. `*_missed` means matching found nothing plausible — the
    filter still ran, so the tools only treat it as a miss if the result also
    came back empty.
    """

    author: str | None = None
    title_contains: str | None = None
    topic_terms: tuple[str, ...] = ()
    theme: str | None = None
    theme_group: str | None = None
    tag: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    effective: RecordFilters = field(default_factory=RecordFilters)
    ambiguous: AmbiguousFilter | None = None
    author_missed: bool = False
    theme_missed: bool = False
    tag_missed: bool = False
    # The theme the question's topic would have been widened onto, when one was
    # dropped for being broader than what was asked. Diagnostic only.
    theme_widened: str | None = None

    def as_kwargs(self) -> dict[str, Any]:
        """Filter kwargs shared by count_documents / list_documents /
        distribution (author, theme, tag, dates). `title_contains` and
        `topic_terms` are passed separately by the tools that use them — only the
        row-returning list takes a topic constraint, because a count or a
        breakdown is about the facets and narrowing it by title words would
        answer a different question."""
        kwargs: dict[str, Any] = {
            "author": self.author,
            "theme": self.theme,
            "theme_group": self.theme_group,
            "tag": self.tag,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
        }
        return {key: value for key, value in kwargs.items() if value is not None}


def resolve_filters(filters: RecordFilters) -> ResolvedScope:
    """Turn user-facing RecordFilters into catalog backing kwargs: canonicalize
    the author, theme and tag names against what the catalog stores, and parse
    dates.

    `entity_resolution_enabled` decides what happens to an imperfect match, not
    whether matching runs (see `_resolve_name`). With it off, an ambiguous name
    quietly takes the best candidate and a miss is not reported — the
    pre-existing behaviour of answering with whatever the filter finds.
    """
    from app.retrieval.structured import resolve as _resolve

    strict = get_settings().entity_resolution_enabled
    author = _resolve_name(_resolve.AUTHOR, filters.author)
    theme = _resolve_name(_THEME, filters.theme)
    tag = _resolve_name(_TAG, filters.tag)

    ambiguous: AmbiguousFilter | None = None
    if strict:
        # One clarification at a time, author first: a person's name is the most
        # common source of a genuine near-tie.
        for kind, match in ((_resolve.AUTHOR, author), (_THEME, theme), (_TAG, tag)):
            if match.band == _resolve.AMBIGUOUS:
                ambiguous = AmbiguousFilter(
                    kind=kind, query=_typed(filters, kind), candidates=match.candidates
                )
                break

    # A theme is only a legitimate stand-in for the question's topic when it
    # actually says the same thing. Resolution is fuzzy so that "climate" reaches
    # "Climate Change", but the same fuzziness snapped "Sustainable Development
    # Goals" onto "Resources & Sustainable Development" and "climate change
    # adaptation" onto "Climate Change" — themes an order of magnitude broader
    # than the ask. Filtering on those is worse than not filtering: it looks
    # precise and returns the newest rows of a huge bucket. When the substitution
    # widens the question, drop the theme and let the caller constrain by the
    # words instead (see `topic.residual_topic`).
    theme_name = theme.name
    widened = None
    if (theme_name and topic.enabled()
            and not topic.faithful_theme(filters.theme, theme_name)):
        logger.info(
            "Theme %r resolved to the broader %r; filtering on the topic words "
            "instead so the list is not widened to the whole theme.",
            filters.theme, theme_name,
        )
        widened, theme_name = theme_name, None

    return ResolvedScope(
        author=author.name,
        title_contains=filters.title_contains or None,
        topic_terms=tuple(filters.topic_terms or ()),
        theme=theme_name,
        # A named theme wins: "how many under Green Shipping" must answer even
        # though Green Shipping is an Other theme. The group restriction only
        # shapes questions that name no theme at all.
        theme_group=None if theme_name else filters.theme_group,
        tag=tag.name,
        effective_from=_parse_date(filters.date_from, field="date_from"),
        effective_to=_parse_date(filters.date_to, field="date_to"),
        # `effective` states what was really filtered on, so a dropped theme must
        # not be reported as applied.
        effective=replace(filters, author=author.name, theme=theme_name, tag=tag.name),
        ambiguous=ambiguous,
        # Misses are always *detected* — the flag only decides whether the tools'
        # resulting no-answer is surfaced as a terminal message or falls through
        # to semantic search (see answerer._terminal_result). Detecting them
        # unconditionally is what keeps flag-off behaviour identical to before:
        # an unrecognized filter falls through rather than answering a bare 0.
        author_missed=author.band == _resolve.MISS,
        # A theme that resolved to something too broad is not a *miss* — the name
        # matched, it just did not mean the same thing — so it must not be
        # reported as an unrecognized filter. It is recorded separately.
        theme_missed=theme.band == _resolve.MISS,
        theme_widened=widened,
        tag_missed=tag.band == _resolve.MISS,
    )
