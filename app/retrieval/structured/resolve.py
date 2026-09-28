"""Entity resolution: free-text query -> ranked canonical entity candidates.

Maps loose, synonym-heavy user phrasing ("rishab negi", "env theme") onto the
catalog's known authors, bundles, and themes. No new infrastructure — plain
normalization plus `difflib`, scored in Python over each type's small candidate
set (16 bundles, ~200 themes, low hundreds of authors), per the
no-new-dependency constraint in docs/database-retrieval-redesign.md §3, §4.

See docs/database-retrieval-redesign.md §4 for the design and the worked
examples the thresholds below were tuned against.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

logger = logging.getLogger(__name__)

ACCEPT = "accept"
AMBIGUOUS = "ambiguous"
MISS = "miss"

# resolve_entity's advertised types. Deliberately author | bundle | theme, not
# tag — a dev-DB sample found ~237 freeform tag terms over ~224 tagged
# documents (roughly 3 docs/tag, many near-duplicates like "Solid waste" /
# "Urban waste" / "Waste management"), the shape of long-tail CMS tagging
# rather than a curated vocabulary fuzzy matching could usefully rank. See
# docs/database-retrieval-redesign.md §3, §4.
AUTHOR = "author"
BUNDLE = "bundle"
THEME = "theme"

# A near-exact score always accepts regardless of the runner-up; a moderate
# score only accepts when it clearly dominates every alternative (no real
# competition) — otherwise it is ambiguous down to the floor, and a miss below
# it. Tuned against docs/database-retrieval-redesign.md §4's worked examples:
# "climate" -> Climate Change must accept (dominant, if not near-exact), while
# "rishab" -> Rishabh Negi / Rishab Nigam must not (a genuine tie).
_ACCEPT_SCORE = 0.90
_ACCEPT_FLOOR = 0.60
_ACCEPT_MARGIN = 0.30
_AMBIGUOUS_FLOOR = 0.60

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

# Domain-generic descriptor words a user adds around the entity name itself
# ("env THEME", "policy BRIEF", "the THEME of waste") rather than being part of
# it — stripped from the query before scoring so they cannot dilute a match
# against the one word that actually names the entity. Never applied to the
# candidate side, and never strips a query down to nothing (see
# `_content_tokens`).
_FILLER_WORDS = frozenset(
    {"theme", "themes", "bundle", "bundles", "tag", "tags", "type", "category"}
)


def _normalize(value: str) -> str:
    """Casefold and collapse whitespace, treating punctuation as a word
    boundary rather than deleting it — "Rishabh-Negi" must tokenize the same
    as "Rishabh Negi", not merge into one word ("rishabhnegi") that no longer
    matches at all."""
    text = _PUNCT.sub(" ", value or "").casefold()
    return _WHITESPACE.sub(" ", text).strip()


def _content_tokens(tokens: list[str]) -> list[str]:
    """Query tokens with filler words removed, unless that would strip every
    token — an all-filler query still compares as typed rather than as empty."""
    kept = [t for t in tokens if t not in _FILLER_WORDS]
    return kept or tokens


def _token_set_ratio(a: str, b: str) -> float:
    """Word-order-insensitive similarity: sort each side's tokens before
    comparing, so "negi rishabh" and "Rishabh Negi" compare as equal."""
    ta = sorted(a.split())
    tb = sorted(b.split())
    return difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()


def _prefix_score(query_tokens: list[str], candidate_tokens: list[str]) -> float:
    """A single strong abbreviation/prefix match ("env" -> "Environment"),
    discounted by how much of the candidate that one token represents — an
    exact hit on the first word of a four-word candidate must not outscore an
    exact hit on a one-word candidate, or "environment" would tie "Environment"
    against "Environment and Public Health" instead of preferring the exact
    name. Only fires for a single-token query: with more than one token, a lone
    strong pair match says nothing about whether the OTHER tokens also
    correspond (two different people can share a first name) — see
    `_token_set_ratio` / the whole-string ratio in `score` for the multi-token
    case instead."""
    if len(query_tokens) != 1:
        return 0.0
    qt = query_tokens[0]
    if len(qt) < 3 or not candidate_tokens:
        return 0.0
    coverage = 1.0 / len(candidate_tokens)
    best = 0.0
    for ct in candidate_tokens:
        if qt == ct:
            best = max(best, 0.5 + 0.5 * coverage)
        elif ct.startswith(qt) or qt.startswith(ct):
            shorter, longer = sorted((len(qt), len(ct)))
            best = max(best, (0.5 + 0.5 * coverage) * (0.6 + 0.4 * (shorter / longer)))
    return best


def score(query: str, candidate: str) -> float:
    """Similarity in [0, 1] between a free-text query and one candidate name —
    the max of a whole-string ratio, a word-order-insensitive ratio, a
    single-token prefix/abbreviation match, and a length-aware substring boost.
    An empty query or candidate never matches (0.0), and a query that equals
    the candidate after normalization always scores 1.0."""
    q_raw, c = _normalize(query), _normalize(candidate)
    if not q_raw or not c:
        return 0.0
    q_tokens = _content_tokens(q_raw.split())
    q = " ".join(q_tokens)
    if q == c:
        return 1.0
    candidates = [
        difflib.SequenceMatcher(None, q, c).ratio(),
        _token_set_ratio(q, c),
        _prefix_score(q_tokens, c.split()),
    ]
    if q in c or c in q:
        shorter, longer = (q, c) if len(q) <= len(c) else (c, q)
        candidates.append(0.5 + 0.5 * (len(shorter) / len(longer)))
    return max(candidates)


def classify_band(top_score: float, runner_up_score: float = 0.0) -> str:
    """ACCEPT / AMBIGUOUS / MISS from the best score and the next-best score.

    ACCEPT is either a near-exact top score on its own, or a moderate score
    with a clear lead over the runner-up (no real competition) — a moderate
    score with an equally moderate second guess is ambiguous, not a guess to
    make silently. See docs/database-retrieval-redesign.md §4 for the worked
    examples these thresholds were tuned against."""
    dominant = top_score >= _ACCEPT_FLOOR and (top_score - runner_up_score) >= _ACCEPT_MARGIN
    if top_score >= _ACCEPT_SCORE or dominant:
        return ACCEPT
    if top_score >= _AMBIGUOUS_FLOOR:
        return AMBIGUOUS
    return MISS


@dataclass(frozen=True)
class EntityCandidate:
    """One ranked match for a free-text entity name.

    `id` is the catalog identifier, which for every type is now the name itself
    — the catalog keys themes, tags, authors and bundles by name (bundles by
    their canonical key). It is kept distinct from `canonical_name` so a future
    type with a separate identifier does not need the shape to change.
    """

    id: str
    canonical_name: str
    type: str
    score: float


def plausible(candidates: list[EntityCandidate], limit: int = 3) -> list[EntityCandidate]:
    """The candidates worth offering the user in a clarification — those scoring
    at or above the ambiguity floor, best first.

    A blind top-N slice is wrong here: with a small candidate pool the 3rd-best
    match can be an unrelated name (scoring ~0.38 against a 0.75 tie), and
    offering it as a choice implies a similarity that does not exist."""
    return [c for c in candidates if c.score >= _AMBIGUOUS_FLOOR][:limit]


def _bundle_candidates(query: str) -> list[EntityCandidate]:
    """A recognized bundle name/synonym/plural (`entities.get_entity`) is a
    sure thing — return it alone at score 1.0 rather than also fuzzy-ranking
    the other 15 bundles against it. Otherwise fuzzy-score the query against
    every bundle's display label, since raw bundle keys ("policy_brief") don't
    normalize against free text the way their labels ("policy briefs") do."""
    from app.retrieval.structured import entities

    exact = entities.get_entity(query)
    if exact is not None:
        return [
            EntityCandidate(
                id=exact.name, canonical_name=entities.entity_label(exact.name, 2),
                type=BUNDLE, score=1.0,
            )
        ]
    return [
        EntityCandidate(
            id=name, canonical_name=entities.entity_label(name, 2),
            type=BUNDLE, score=score(query, entities.entity_label(name, 2)),
        )
        for name in entities.DEFAULT_BUNDLES
    ]


def _theme_candidates(query: str) -> list[EntityCandidate]:
    """Themes from `documents_theme` — the names documents actually carry, with
    their Main/Other group. A lookup failure degrades to no candidates for this
    type rather than raising; `resolve_entity` still ranks whatever the other
    types found."""
    try:
        from app.catalog import queries

        rows = queries.theme_vocabulary()
    except Exception:
        logger.warning("Theme candidate lookup failed.", exc_info=True)
        return []
    return [
        EntityCandidate(
            id=row["theme"], canonical_name=row["theme"],
            type=THEME, score=score(query, row["theme"]),
        )
        for row in rows
    ]


@lru_cache(maxsize=1)
def _cached_author_names() -> tuple[str, ...]:
    from app.catalog import queries

    return tuple(queries.distinct_authors())


def known_authors() -> tuple[str, ...]:
    """Every distinct author string in the catalog, fetched once per process
    (see `reload_authors`)."""
    return _cached_author_names()


def reload_authors() -> None:
    """Drop the cached author list (tests / after ingesting new authors). A
    failed fetch is never cached — `functools.lru_cache` does not cache
    exceptions — so a transient DB outage self-heals on the next call without
    needing this."""
    _cached_author_names.cache_clear()


def _author_candidates(query: str) -> list[EntityCandidate]:
    """Every distinct author, fuzzy-scored — see `queries.distinct_authors` for
    why this is a full scored comparison rather than a SQL-narrowed one."""
    try:
        names = _cached_author_names()
    except Exception:
        logger.warning("Author candidate lookup failed.", exc_info=True)
        return []
    return [
        EntityCandidate(id=name, canonical_name=name, type=AUTHOR, score=score(query, name))
        for name in names
    ]


_SOURCES: dict[str, Callable[[str], list[EntityCandidate]]] = {
    BUNDLE: _bundle_candidates,
    THEME: _theme_candidates,
    AUTHOR: _author_candidates,
}


def resolve_entity(query: str, type: str | None = None, *, limit: int = 5) -> list[EntityCandidate]:
    """Ranked entity candidates for a free-text query, highest score first.

    `type` narrows to one entity kind (`author` | `bundle` | `theme`); omit it
    to merge and rank across all three, so a query like "climate" is answerable
    without the caller knowing which kind of entity it names. This function
    only ranks — callers decide what to do with the result (accept the top hit,
    ask the user to disambiguate, or report a miss) via `classify_band` on the
    top one or two scores.
    """
    if not query or not query.strip():
        return []
    if type is not None and type not in _SOURCES:
        raise ValueError(f"type must be one of {sorted(_SOURCES)} or None, got {type!r}")
    capped = max(1, min(int(limit or 5), 20))
    sources = [_SOURCES[type]] if type else list(_SOURCES.values())
    candidates = [c for source in sources for c in source(query)]
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:capped]
