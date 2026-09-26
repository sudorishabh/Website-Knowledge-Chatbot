"""The priority page list: which pages exist, what they are, what names them.

Read from ``data/priority_crawl_pages.json`` (or ``priority_pages_path``), the
hand-maintained list of the organisation's own pages that the Drupal crawl
cannot reproduce — theme and regional-centre pages are taxonomy terms assembled
from Views, the people listings are Views routes, and their profiles are user
entities — plus the institutional pages whose stored copy is to be ignored in
favour of the live one.

The file mixes two shapes, and the parser keeps them apart:

* an entry with a URL is a **page** (``page_url``, or ``site_url`` on the
  regional centres — both spellings are in the file);
* an entry with children and no URL is a **group** ("Regional centers"). A
  group is not fetched: its own description and its members' names already
  answer "what are the regional centres?".

Pages are flat: a page's own children are not read, and a warning says so. The
themes are flat since 2026-09-25: one top-level page each, named "<theme>
Theme" ("Climate Change Theme"), with no hierarchy between them. The list of
themes itself is the home page's to give — its "Thematic Areas" section — so
the phrases that ask for that list ("TERI's thematic areas") name the home page.

Nothing here touches the network.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from app.config import get_settings

logger = logging.getLogger(__name__)

#: app/retrieval/priority/registry.py -> the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = _REPO_ROOT / "data" / "priority_crawl_pages.json"

THEME = "theme"
CENTRE = "centre"
PEOPLE = "people"
PAGE = "page"

#: How the file marks a theme page: "Climate Change Theme".
_THEME_SUFFIX = re.compile(r"\s+theme$", re.I)


def normalize_url(url: str | None) -> str:
    """Comparison key for a URL: host without ``www.``, path lowercased.

    Scheme, query, fragment and a trailing slash are dropped, so the file's
    ``https://www.teriin.org/policy`` and a stored ``https://teriin.org/policy/``
    are the same page. Lowercased because Drupal path aliases are
    case-insensitive and the file writes ``/CSDR`` and ``/Electricity-and-Renewables``.
    For comparison only; the URL that is fetched keeps its own spelling.
    """
    if not url:
        return ""
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = unquote(parts.path or "").rstrip("/").lower() or "/"
    return f"{host}{path}"


def _path_of(key: str) -> str:
    return key[key.find("/"):] if "/" in key else "/"


_NON_WORD = re.compile(r"[^a-z0-9]+")
_POSSESSIVE = re.compile(r"(?<=\w)['’]s\b")


def normalize_text(text: str | None) -> str:
    """Lowercase words separated by single spaces, for phrase matching.

    ``&`` reads as "and" and a possessive loses its ``'s``, so "TERI's Forest &
    Biodiversity work" and "teri forest and biodiversity" compare equal.
    """
    lowered = _POSSESSIVE.sub("", str(text or "").lower()).replace("&", " and ")
    return _NON_WORD.sub(" ", lowered).strip()


#: Phrases that ask for the list of themes, which the home page gives. Tight on
#: purpose — a page that matches leads the context — so a bare "themes" or
#: "research area" is not one: "which themes have the most publications" is a
#: catalog count, and a question the query understanding reads as a theme
#: listing reaches the home page without any of these (``match.explicit``).
THEMES_OVERVIEW: tuple[str, ...] = (
    "thematic area", "theme area", "themes area", "teri themes", "themes of teri",
    "themes teri", "themes does teri", "themes at teri", "teri research area",
    "research areas of teri", "teri focus area", "focus areas of teri",
    "areas teri works", "areas does teri work", "teri areas of work",
    "areas of work of teri",
)

# Phrases that name a page outright, beyond its own name. Keyed by path, since
# the names in the file are labels ("founder", "annoucements") rather than what
# anyone types. Only distinctive phrases: a bare "policy" or "energy" names a
# topic far more often than it names a page, so those pages are reached through
# the theme facet and the description match instead.
_EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "/": (*THEMES_OVERVIEW, "home page", "homepage"),
    "/history": ("founder", "founded", "history of teri", "teri history", "origin of teri"),
    "/mission-and-goals": (
        "teri mission", "mission of teri", "mission statement", "teri vision",
        "vision of teri", "goals of teri", "teri goals", "mission and vision",
    ),
    "/announcements": (
        "announcement", "tender", "procurement", "expression of interest",
        "request for proposal", "rfp", "eoi",
    ),
    "/fcra-financials": ("fcra", "foreign contribution"),
    "/annual-reports": ("annual report",),
    "/alumni": ("alumni", "alumnus", "alumni association"),
    "/people/committee-of-directors": (
        "committee of directors", "director general", "dg", "executive director",
        "directors of teri", "teri directors", "leadership team", "senior leadership",
    ),
    "/people/governing-council": (
        "governing council", "governing body", "chairman of teri", "teri chairman",
    ),
    "/people/distinguished-fellows": (
        "distinguished fellow", "fellow emeritus", "fellows emeriti",
    ),
    "/centre-of-excellence": (
        "centre of excellence", "center of excellence", "centres of excellence",
        "centers of excellence",
    ),
    "/technologies": ("teri technologies", "technologies developed by teri"),
    "/services": ("teri services", "services offered", "services of teri"),
    "/policy": ("policy research", "policy advisory"),
    "/outreach": ("outreach",),
    "/bengaluru": ("bengaluru", "bangalore"),
    "/goa": ("goa",),
    "/guwahati": ("guwahati", "north east", "northeast", "north eastern"),
    "/photo-story/teri-gram-gwal-pahari": ("gurugram", "gurgaon", "gwal pahari", "teri gram"),
    "/himalayan-centre-nainital": ("mukteshwar", "nainital", "himalayan centre", "himalayan center"),
    "/hyderabad": ("hyderabad",),
    "/mumbai": ("mumbai",),
    "/world-sustainable-development-summit": ("wsds",),
    "/krc": ("krc", "knowledge resource centre", "knowledge resource center"),
    "/teri-corporate-social-responsibility": ("csr",),
    "/teri-council-for-business-sustainability": ("council for business sustainability",),
    "/csdr": ("csdr",),
    "/pbbgc": ("pbbgc", "biochar", "pyrolytic"),
    "/electricity-and-renewables": ("renewable energy", "renewables"),
}

# Phrases that ask for a group's membership rather than any one member.
_GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    CENTRE: ("regional centre", "regional center", "regional office", "centres of teri",
             "centers of teri", "teri centres", "teri centers"),
}


@dataclass(frozen=True)
class PriorityPage:
    """One page on the list."""

    name: str
    url: str
    description: str
    kind: str
    aliases: tuple[str, ...] = ()
    #: The profile URL shape a people listing links to, when the file states one.
    profile_pattern: str | None = None

    @property
    def key(self) -> str:
        return normalize_url(self.url)

    @property
    def is_home(self) -> bool:
        return _path_of(self.key) == "/"

    @property
    def topic(self) -> str:
        """What the page is about: a theme page's name without its "Theme"."""
        return _THEME_SUFFIX.sub("", self.name) if self.kind == THEME else self.name

    @property
    def match_names(self) -> tuple[str, ...]:
        """The normalized forms of this page's own name worth matching on: a
        single common word ("Water", "Policy") names a topic, not the page. A
        theme is also named by its topic with "theme" or "thematic" after it,
        which is how "the water theme" reaches a one-word theme."""
        name = normalize_text(re.sub(r"^people\s*-\s*", "", self.topic, flags=re.I))
        names = [name] if len(name.split()) > 1 else []
        if self.kind == THEME and name:
            names += [f"{name} theme", f"{name} thematic"]
        if name.startswith("teri ") and len(name.split()) > 2:
            names.append(name[len("teri "):])
        return tuple(dict.fromkeys(n for n in names if n))


@dataclass(frozen=True)
class PriorityGroup:
    """A grouping entry with no page of its own."""

    name: str
    description: str
    kind: str
    members: tuple[PriorityPage, ...]
    aliases: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"group:{normalize_text(self.name)}"


@dataclass(frozen=True)
class Registry:
    pages: tuple[PriorityPage, ...] = ()
    groups: tuple[PriorityGroup, ...] = ()

    def by_key(self, key: str) -> PriorityPage | None:
        return _index(self).get(key)

    def page_for(self, url: str | None) -> PriorityPage | None:
        return self.by_key(normalize_url(url))

    @property
    def home(self) -> PriorityPage | None:
        """The home page, which gives the list of themes."""
        return next((p for p in self.pages if p.is_home), None)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(_index(self))

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(k.split("/", 1)[0] for k in _index(self))


@lru_cache(maxsize=4)
def _index(registry: Registry) -> dict[str, PriorityPage]:
    out: dict[str, PriorityPage] = {}
    for page in registry.pages:
        out.setdefault(page.key, page)
    return out


def _group_kind(name: str) -> str:
    lowered = name.lower()
    if "centre" in lowered or "center" in lowered:
        return CENTRE
    return PAGE


def _page_kind(entry: dict[str, Any], group_kind: str | None, url: str) -> str:
    if entry.get("example-of-people-url") or _path_of(normalize_url(url)).startswith("/people/"):
        return PEOPLE
    if _THEME_SUFFIX.search(str(entry.get("name") or "").strip()):
        return THEME
    return group_kind or PAGE


def _aliases(name_forms: tuple[str, ...], key: str) -> tuple[str, ...]:
    extra = _EXTRA_ALIASES.get(_path_of(key), ())
    return tuple(dict.fromkeys(a for a in (*name_forms, *extra) if a))


def _page(entry: Any, *, group_kind: str | None) -> PriorityPage | None:
    """The page an entry describes, or None when it lacks a name or a URL."""
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    url = str(entry.get("page_url") or entry.get("site_url") or "").strip()
    if not (name and url):
        return None
    if entry.get("children"):
        logger.warning("Priority page %r lists child pages; pages are flat, so they "
                       "are not read.", name)
    page = PriorityPage(
        name=name,
        url=url,
        description=str(entry.get("description") or "").strip(),
        kind=_page_kind(entry, group_kind, url),
        profile_pattern=entry.get("example-of-people-url"),
    )
    return replace(page, aliases=_aliases(page.match_names, page.key))


def parse(raw: Any) -> Registry:
    """A registry from the file's parsed JSON. Tolerant: an entry it cannot read
    is skipped, never fatal, because one typo must not cost every other page."""
    pages: list[PriorityPage] = []
    groups: list[PriorityGroup] = []
    for entry in raw if isinstance(raw, list) else ():
        if not isinstance(entry, dict):
            continue
        if entry.get("page_url") or entry.get("site_url"):
            page = _page(entry, group_kind=None)
            if page is not None:
                pages.append(page)
            continue
        if not entry.get("children"):
            continue
        name = str(entry.get("name") or "").strip()
        kind = _group_kind(name)
        members = [m for m in (_page(child, group_kind=kind) for child in entry["children"]) if m]
        pages.extend(members)
        groups.append(PriorityGroup(
            name=name,
            description=str(entry.get("description") or "").strip(),
            kind=kind,
            members=tuple(members),
            aliases=_GROUP_ALIASES.get(kind, ()),
        ))
    return Registry(pages=tuple(pages), groups=tuple(groups))


def configured_path() -> Path:
    configured = (getattr(get_settings(), "priority_pages_path", "") or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_PATH


@lru_cache(maxsize=1)
def load_registry() -> Registry:
    """The registry, parsed once per process. A missing or unreadable file is an
    empty registry — the feature then does nothing — and a loud log line."""
    path = configured_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("Could not read the priority page list at %s; priority pages "
                     "are disabled until it is fixed.", path, exc_info=True)
        return Registry()
    registry = parse(raw)
    logger.info("Priority pages: %d page(s), %d group(s) from %s.",
                len(registry.pages), len(registry.groups), path)
    return registry


def reload_registry() -> None:
    """Drop the cached registry (tests, or after editing the file in place)."""
    load_registry.cache_clear()
