"""Theme hierarchy: the primary-tag / sub-theme map behind a document's themes.

The theme map (``app/theme_structure.json`` by default, overridable with the
``theme_taxonomy_path`` setting — see :func:`_configured_path`) is the authority
for which themes are **Primary Tags** and which are **Sub-Themes** hanging off
one. Its top level ("Main Themes" / "Other Themes") is a grouping bucket rather
than a theme, so:

* a bucket's children are Primary Tags (``parent`` is NULL);
* anything below a Primary Tag is a Sub-Theme whose ``parent`` is its
  **immediate** parent;
* a bucket name itself is never stored as a theme.

Depth is not capped. ``parent`` names one hop, and ``path`` carries the whole
ancestor chain from the primary tag down ("Energy > Energy Access > Rural Energy
Access"), which is what makes a parent-theme query reach a grandchild: one
``parent`` column can only ever answer one level, and rolling a deep tree up
through repeated self-joins is both slower and unbounded. ``depth`` is the number
of segments in that path, so a primary tag is 1.

The bucket a theme originates from is still tracked, as ``group`` (``"main"`` /
``"other"``) — every entry records which top-level bucket it traces back to (a
sub-theme inherits its primary tag's bucket), so "Main Themes" and "Other Themes"
stay distinguishable downstream even though ``theme_type`` (primary/sub) is about
depth within a bucket, not which bucket. The bucket's display name is matched
down to one of the two fixed codes (see ``_group_code``); it does not store the
bucket's literal name.

Deliberately a static file rather than the crawled Drupal tree: classification
has to stay stable however a vocabulary happens to be nested in the CMS, and the
same map has to apply to the ref-less export/upload paths, which have no
taxonomy to read. The crawled tree still lives in ``terms.parent_uuid`` and is
what uuid-based scoping expands over — this map only shapes the theme rows.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, NamedTuple

from app.config import get_settings

logger = logging.getLogger(__name__)

PRIMARY = "primary"
SUB = "sub"
#: A theme the map has no entry for at all. Distinct from ``SUB`` on purpose: an
#: unmapped name is not a child of anything, and calling it a sub-theme claims a
#: hierarchy position nothing supports — it also hid new themes from the theme
#: listing, which enumerates ``primary`` rows. The row is still written; only its
#: classification is honest about being absent from the map.
UNKNOWN = "unknown"

MAIN = "main"
OTHER = "other"

#: Separator between the segments of a theme ``path``. A theme name containing
#: this would break the segmentation, so it is deliberately a spaced form that
#: no CMS label uses; :func:`_clean` collapses whitespace, which keeps a name's
#: own spacing from ever producing it by accident.
PATH_SEPARATOR = " > "

# Values that reach the theme facet as strings but are not themes: a boolean or
# null from some upstream field, already stringified before it gets here (a real
# `False` is falsy and drops out in `_clean`, but `"False"` does not). Dropped in
# `classify` so no such row is ever written — the catalog once held 404 rows
# whose theme was the literal string "False".
_NOT_A_THEME: frozenset[str] = frozenset({"false", "true", "none", "null", "nan"})

# app/theme_structure.json — a sibling of the app package root, not of this
# module.
_DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "theme_structure.json"


def _configured_path() -> Path:
    """The theme map's location: the ``theme_taxonomy_path`` setting when set,
    otherwise the shipped default.

    Configurable because the map is load-bearing and there has been more than
    one copy of it on disk. Naming the authoritative file in configuration makes
    "which of these is the real one" answerable without reading a path
    expression, and lets a deployment relocate it without a code edit.

    Settings failures fall back to the default rather than raising: this runs at
    import time, and a module that cannot be imported is a worse failure than
    one that reads the file next to itself. An unreadable map is still caught —
    loudly, by :func:`require_taxonomy`.
    """
    try:
        configured = (get_settings().theme_taxonomy_path or "").strip()
    except Exception:  # pragma: no cover - defensive, see docstring
        logger.warning("Could not read theme_taxonomy_path; using the default.")
        return _DEFAULT_TAXONOMY_PATH
    return Path(configured).expanduser() if configured else _DEFAULT_TAXONOMY_PATH


#: Read at call time by :func:`_load`, so tests (and operators, via the setting
#: above) can point it elsewhere and :func:`reload_taxonomy` to pick it up.
TAXONOMY_PATH = _configured_path()

_WHITESPACE = re.compile(r"\s+")


class _Entry(NamedTuple):
    """One mapped theme: its display name and its position in the hierarchy."""

    name: str
    theme_type: str
    parent: str | None
    group: str | None
    path: str
    depth: int


@dataclass(frozen=True)
class ThemeAssignment:
    """One theme row for a document: the theme, whether it is the primary tag, a
    sub-theme or unmapped, its immediate parent, which top-level bucket
    (``"main"`` / ``"other"``) it traces back to, and its full ancestry.

    ``parent`` is None for a primary tag and for an unmapped theme — both store
    NULL. ``group`` is the bucket the theme's primary tag belongs to
    (inherited), so it is set independently of ``parent``; it is None only when
    the map has no entry for the name at all.

    ``path`` is the ancestor chain from the primary tag down to this theme,
    joined by :data:`PATH_SEPARATOR`, and ``depth`` is its segment count. An
    unmapped theme is its own single-segment path rather than NULL, so a query
    for it matches by exactly the same prefix rule as a mapped one — a NULL path
    would need every reader to carry a second code path for the open-vocabulary
    case."""

    name: str
    theme_type: str
    parent: str | None
    group: str | None
    path: str
    depth: int


def _clean(value: Any) -> str:
    """Display form: whitespace-collapsed and trimmed. For str patterns \\s
    is Unicode-aware, so the non-breaking spaces Drupal labels pick up go too."""
    return _WHITESPACE.sub(" ", str(value or "")).strip()


_AMPERSAND = re.compile(r"\s*&\s*")


def _key(value: Any) -> str:
    """Match key — case-insensitive on top of :func:`_clean`, so CMS display
    drift ("energy  access") still resolves against the map.

    "&" and "and" are one key. The site spells the same theme both ways — its
    menu has "Energy Assessment & Modelling", its terms "Energy Assessment and
    Modelling" — and the CMS term "Environment & Public Health" (195 documents)
    stayed unmapped against a map that wrote "and"."""
    return _clean(_AMPERSAND.sub(" and ", _clean(value))).casefold()


def _group_code(bucket_name: str) -> str:
    """Fixed ``"main"``/``"other"`` code for a top-level bucket's display name.

    Matched on substring rather than position, so reordering the two buckets in
    the theme map doesn't flip which is which. Any bucket not named after "main"
    (a third bucket added later, a rename) falls to ``"other"`` rather than
    raising — the two-value split is deliberately the ceiling here; a document
    is never denied a theme row over an unrecognized bucket label."""
    return MAIN if "main" in bucket_name.casefold() else OTHER


def _walk(
    nodes: Any,
    ancestors: tuple[str, ...],
    group: str | None,
    out: dict[str, _Entry],
) -> None:
    """Collect ``nodes`` into ``out``. ``ancestors`` is the chain of theme names
    above them, innermost last, and empty when they *are* the primary tags
    (bucket children). ``group`` is the top-level bucket's fixed code
    (``"main"``/``"other"``), carried unchanged through the whole recursion —
    depth changes the ancestry, never the group.

    Descends past unnamed nodes rather than dropping their subtree, and keeps
    the first entry per key so an accidental duplicate in the file is stable.

    Every level below the first is a sub-theme of its **immediate** parent, and
    the full chain is recorded as ``path``. This used to reparent everything
    deeper than one level onto the primary tag, which made a grandchild a
    sibling of its own parent: "Energy > Renewables > Rooftop Solar" was stored
    as a child of Energy, so a query for Renewables could not find it and a
    breakdown of Renewables' children came back empty."""
    for node in nodes or ():
        if not isinstance(node, dict):
            continue
        name = _clean(node.get("name"))
        children = node.get("children")
        if not name:
            _walk(children, ancestors, group, out)
            continue
        chain = ancestors + (name,)
        out.setdefault(
            _key(name),
            _Entry(
                name=name,
                theme_type=PRIMARY if len(chain) == 1 else SUB,
                parent=ancestors[-1] if ancestors else None,
                group=group,
                path=PATH_SEPARATOR.join(chain),
                depth=len(chain),
            ),
        )
        _walk(children, chain, group, out)


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, _Entry], frozenset[str]]:
    """Parse the theme map once per process into (theme map, bucket keys).

    A missing or malformed file is logged, not raised: themes then all fall
    through to unparented sub-themes, which keeps ingestion running instead of
    failing every document on a data-file problem.

    That tolerance is a liability during ingestion, which is why
    :func:`require_taxonomy` exists. An unreadable map does not merely lose the
    hierarchy for one run — it rewrites every theme row with a NULL group, and
    the Main/Other split is then gone from the data as well as the file. The log
    is CRITICAL for the same reason: this degradation is silent, cheap to cause
    (renaming the file is enough) and expensive to notice."""
    try:
        raw = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.critical(
            "Could not read the theme map at %s. Every theme now classifies as "
            "an unparented sub-theme with no Main/Other group; re-ingesting in "
            "this state will overwrite the stored hierarchy. Fix the file before "
            "ingesting.",
            TAXONOMY_PATH,
            exc_info=True,
        )
        return {}, frozenset()

    mapping: dict[str, _Entry] = {}
    buckets: set[str] = set()
    for bucket in raw if isinstance(raw, list) else ():
        if not isinstance(bucket, dict):
            continue
        name = _clean(bucket.get("name"))
        if name:
            buckets.add(_key(name))
        _walk(bucket.get("children"), (), _group_code(name) if name else None, mapping)
    # A name used both as a bucket and as a real theme stays a theme.
    return mapping, frozenset(buckets - set(mapping))


def reload_taxonomy() -> None:
    """Drop the cached map (tests / after editing the theme map in place)."""
    _load.cache_clear()


class TaxonomyUnavailable(RuntimeError):
    """The theme map could not be loaded, so classification would be wrong."""


def is_loaded() -> bool:
    """Whether the theme map parsed into at least one theme."""
    mapping, _ = _load()
    return bool(mapping)


def require_taxonomy() -> None:
    """Raise unless the theme map is usable. Call before writing theme rows.

    The preflight that turns a silent data problem into a loud one. Classifying
    against an empty map does not fail — it succeeds and writes the wrong
    answer for every document, which is the worse outcome and the one that
    actually happened: the map's filename changed and nothing complained until
    the Main/Other split was already missing from every runtime lookup.

    Deliberately not called from :func:`classify`. Per-document classification
    stays tolerant, so a mid-run problem degrades one document rather than
    aborting a long ingest; this is for the start of a run, where refusing costs
    nothing and prevents a rewrite.
    """
    if not is_loaded():
        raise TaxonomyUnavailable(
            f"No themes could be read from {TAXONOMY_PATH}. Ingesting now would "
            "clear the Main/Other grouping on every theme row. Fix or restore "
            "the theme map first."
        )


def classify(names: Iterable[str] | None) -> list[ThemeAssignment]:
    """Theme rows for one document's themes, in input order, de-duplicated.

    Only the names passed in are returned — a sub-theme's parent is recorded as
    a reference, never materialized as an extra row, so a document is never
    credited with a theme it was not tagged with.

    Empty values and grouping-bucket names are dropped, so a document with no
    valid theme yields ``[]`` and no row is written for it. That is the whole
    sparseness guarantee: "this document has no theme" is the absence of rows,
    never a row saying so.

    A theme the map does not know is kept — classified :data:`UNKNOWN`, with no
    parent and no group, and its own name as its one-segment path. So a theme
    newly added in the CMS is countable and listable immediately, and editing
    the map is never a precondition for storing it."""
    mapping, buckets = _load()
    seen: dict[str, ThemeAssignment] = {}
    for raw in names or ():
        name = _clean(raw)
        if not name:
            continue
        key = _key(name)
        if key in buckets or key in seen or key in _NOT_A_THEME:
            continue
        known = mapping.get(key)
        # The supplied display name is stored as-is (rename handling lives in
        # state.rename_theme_facet); the parent, group and path come from the
        # map, which is the only place any of them is named. An unmapped theme's
        # path is its own name — see ThemeAssignment.
        seen[key] = (
            ThemeAssignment(
                name, known.theme_type, known.parent, known.group,
                known.path, known.depth,
            )
            if known
            else ThemeAssignment(name, UNKNOWN, None, None, name, 1)
        )
    return list(seen.values())


def group_of(name: str) -> str | None:
    """The top-level bucket (``"main"`` / ``"other"``) ``name`` traces back to,
    or ``None`` when the map has no entry for it — a theme the CMS has but
    the theme map does not yet know has no group to report, the same as
    ``classify`` leaving its ``group`` unset. Looks up by the same
    case-insensitive match key ``classify`` uses, so CMS display drift resolves
    the same way. Used by the theme listing to split Main from Other without
    depending on any document actually carrying the theme."""
    mapping, _ = _load()
    entry = mapping.get(_key(name))
    return entry.group if entry else None


def themes_by_group() -> dict[str, list[str]]:
    """Every mapped theme name (primary tags and sub-themes), split by which
    top-level bucket it traces back to, each in file order. A diagnostics/docs
    helper mirroring ``primary_tags`` — the DB-backed theme listing looks up
    each name individually via ``group_of`` instead, since it must also cover
    themes the theme map does not know about."""
    mapping, _ = _load()
    result: dict[str, list[str]] = {MAIN: [], OTHER: []}
    for entry in mapping.values():
        if entry.group in result:
            result[entry.group].append(entry.name)
    return result
