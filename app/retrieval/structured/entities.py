"""Entity registry for the structured (catalog) query capability.

An "entity" is a content bundle (news, research_papers, events, ...) — all
`source_type='website', entity_type='node'` rows in the catalog. There are no
per-entity tables and no per-entity tools: the bundle is a query parameter, so
registering a content type is a data change here. The bundle list itself comes
from the Drupal source registry (`app.ingestion.extractors.drupal_extractor`) —
this module is what makes the catalog query layer usable without every caller
knowing that origin.

This module is the canonical home for the bundle synonyms and display labels
(the pipeline summarizer and the catalog tools resolve entities through here).
See docs/database-tool-registry.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.corpus import DEFAULT_BUNDLES

# Free-text content types (from the LLM or the user) that plural/singular matching
# won't map to a known bundle.
_BUNDLE_SYNONYMS: dict[str, str] = {
    "person": "people",
    "paper": "research_papers",
    "policy": "policy_brief",
    "brief": "policy_brief",
    "news_article": "news",
    "press": "press_release",
}

# Content words that name several bundles at once, so no single one of them can
# be the answer. Kept deliberately small: a word belongs here only when picking
# any one bundle would misreport the others, which is why "articles" is absent —
# it maps to `article` outright (`feature_articles` needs the word "feature").
#
# "projects" was silently resolving to one project type, answering "0 ongoing
# projects" while 918 completed ones existed. Spanning both in one query is not
# an option either: the catalog tools take a single bundle, so the honest move is
# to ask (§4 — ask on ambiguity, never guess).
_AMBIGUOUS_BUNDLE_WORDS: dict[str, tuple[str, ...]] = {
    "projects": ("completed_projects", "ongoing_projects"),
}

# Display (singular, plural) forms for count/list answers. Bundle names are
# inconsistently pluralized, so map the known ones; anything else is humanized.
_BUNDLE_LABELS: dict[str, tuple[str, str]] = {
    "news": ("news item", "news items"),
    "events": ("event", "events"),
    "feature_articles": ("feature article", "feature articles"),
    "completed_projects": ("completed project", "completed projects"),
    "ongoing_projects": ("ongoing project", "ongoing projects"),
    "press_release": ("press release", "press releases"),
    "research_papers": ("research paper", "research papers"),
    "policy_brief": ("policy brief", "policy briefs"),
    "videos": ("video", "videos"),
    "infographics": ("infographic", "infographics"),
    "services": ("service", "services"),
    "people": ("person", "people"),
    "article": ("article", "articles"),
    "report": ("report", "reports"),
    "page": ("page", "pages"),
    "carousel": ("carousel", "carousels"),
    "items": ("item", "items"),
    "publications": ("publication", "publications"),
}


def entity_label(scope: str, n: int) -> str:
    """Singular/plural display label for a bundle (or any scope word), e.g.
    ('news', 1) -> 'news item'. Unknown scopes are humanized best-effort."""
    forms = _BUNDLE_LABELS.get(scope)
    if forms:
        return forms[0] if n == 1 else forms[1]
    human = scope.replace("_", " ")
    if n == 1:
        return human[:-1] if human.endswith("s") else human
    return human if human.endswith("s") else f"{human}s"


def scope_noun(bundle: str | None, *, by_author: bool) -> str:
    """The scope word for a set of documents: its content type when it has one,
    else "publications" for one author's documents and "items" otherwise. Only
    publications carry authors in the catalog — feature articles, research
    papers, articles, policy briefs and reports (measured 2026-10-04) — so
    everything by a person is one, and "41 items by" undersold it."""
    if bundle:
        return bundle
    return "publications" if by_author else "items"


@dataclass(frozen=True)
class Entity:
    """How a catalog entity is queried. Every entity today is a website node; the
    source_type/entity_type binding lets a future non-Drupal source register with
    a different backing without touching the tools."""

    name: str
    source_type: str = "website"
    entity_type: str | None = "node"

    def label(self, n: int) -> str:
        return entity_label(self.name, n)


# Registered content entities, keyed by canonical bundle name.
_REGISTRY: dict[str, Entity] = {bundle: Entity(name=bundle) for bundle in DEFAULT_BUNDLES}


def normalize_entity(raw: str | None) -> str | None:
    """Map a free-text content type ('event', 'press release', 'person') to a
    canonical bundle name, or None for empty input. An unrecognized type returns
    its cleaned key (callers validate with `is_known` — a bad entity must count as
    zero, not as everything)."""
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if key in _REGISTRY:
        return key
    for variant in (f"{key}s", key.rstrip("s")):
        if variant in _REGISTRY:
            return variant
    return _BUNDLE_SYNONYMS.get(key) or _BUNDLE_SYNONYMS.get(key.rstrip("s"), key)


def is_known(name: str | None) -> bool:
    """True when `name` (already normalized) is a registered content entity."""
    return bool(name) and name in _REGISTRY


def present_bundles() -> tuple[str, ...]:
    """Registered bundles the catalog actually holds documents for.

    Empty means "could not tell" — the catalog was unreachable, or this is a
    caller with no database. It never means "nothing is available", because a
    transient failure must not retract the vocabulary; consumers treat empty as
    "assume the configured list is fine". The underlying query is cached (see
    `app.catalog.queries.available_bundles`)."""
    from app.catalog import queries

    return tuple(b for b in queries.available_bundles() if b in _REGISTRY)


def is_available(name: str | None) -> bool:
    """Whether a registered bundle has any content in *this* catalog.

    Distinct from :func:`is_known`, which only says the type is configured. A
    known-but-absent bundle is what produced confident zeroes: the query layer
    happily filtered on `bundle = 'report'` against a catalog holding no reports
    and answered "0 reports" as though it had counted them.

    True when the inventory is unknown, so a database problem degrades to the
    previous behaviour rather than rejecting every content type."""
    if not is_known(name):
        return False
    present = present_bundles()
    return not present or name in present


def ambiguous_bundles(raw: str | None) -> tuple[str, ...]:
    """The bundles a free-text content word spans when it names more than one,
    else empty.

    Checked only for words that are not themselves a registered bundle, so this
    can never override an exact type the user named. Singular and plural both
    resolve, matching `normalize_entity`'s tolerance."""
    if not raw:
        return ()
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if key in _REGISTRY:
        return ()
    for variant in (key, f"{key}s", key.rstrip("s")):
        spanned = _AMBIGUOUS_BUNDLE_WORDS.get(variant)
        if spanned:
            return spanned
    return ()


def get_entity(name: str | None) -> Entity | None:
    """Resolve a (possibly free-text) entity name to a registered Entity, or None
    when it is empty or unrecognized."""
    canonical = normalize_entity(name)
    return _REGISTRY.get(canonical) if canonical else None
