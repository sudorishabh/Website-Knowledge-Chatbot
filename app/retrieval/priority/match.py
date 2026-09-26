"""Which priority pages a question needs, and why.

Five triggers, strongest first. The first three are deterministic and cheap;
the fourth reuses the theme query understanding already resolved; the fifth
reuses the query vector the pipeline already computed, so no trigger costs a
model call.

* ``person`` — the question names someone on the people listings; the target
  is that person's profile.
* ``name`` — the question names a page outright: its own name when that is
  more than one word ("climate change", "green shipping"), a theme's name with
  "theme" or "thematic" after it ("the water theme"), or a phrase curated in the
  registry ("director general", "tender", "founder"). The home page is named by
  the phrases that ask for the list of themes ("TERI's thematic areas"), and
  also whenever query understanding read the question as a theme listing; it
  gives way when the question names one theme, since that theme's own page
  answers it.
* ``group`` — the question asks for a group's membership ("regional
  centres"); answered from the list itself, nothing fetched.
* ``theme`` — query understanding resolved a theme facet that is a page on the
  list.
* ``similar`` — the query vector sits close to one page's description, and
  clearly closer than to any other. Calibrated on 2026-09-24 over 20 questions:
  with the organisation's name stripped from the descriptions, most on-topic
  questions scored 0.49–0.75 against the right page, every off-list question
  stayed below 0.46, and the on-topic ones under the bar ("mission of TERI",
  "latest tenders") are caught by a curated name instead.

A sixth reason, ``surfaced``, is assigned later by retrieval itself: ordinary
search ranked the stored copy of a listed page, which is evidence enough that
the live page is relevant.

A seventh, ``staff``, covers the question that asks for the organisation's
people and names none of them ("TERI's leading researchers", "air quality
experts"): the people listings are its answer, and they are read — the two whose
descriptions sit closest to the question, so a question about researchers gets
the directors and fellows while "the governing council" still gets the council
by name. Chosen in :func:`staff_listings`, after the query vector exists.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Callable, Sequence

from app.config import get_settings
from app.retrieval.priority.people import Person, named_in
from app.retrieval.priority.registry import (
    CENTRE,
    PEOPLE,
    THEME,
    PriorityGroup,
    PriorityPage,
    Registry,
    normalize_text,
    normalize_url,
)

logger = logging.getLogger(__name__)

PERSON = "person"
NAME = "name"
GROUP = "group"
THEME_FACET = "theme"
SIMILAR = "similar"
SURFACED = "surfaced"
STAFF = "staff"

#: Lower is stronger. Decides which targets survive the page cap.
STRENGTH = {PERSON: 0, NAME: 1, GROUP: 1, STAFF: 1, THEME_FACET: 2, SIMILAR: 3, SURFACED: 4}
#: Reasons that say the question is *about* the page, so its opening section is
#: admitted whatever it scores.
ABOUT = frozenset({PERSON, NAME, GROUP, STAFF, THEME_FACET, SIMILAR})

#: People listings read for a question that asks for the organisation's people.
MAX_STAFF_LISTINGS = 2

#: The nouns that ask for the organisation's people as a group. Narrower than
#: `app.retrieval.structured.topic.wants_person` on purpose: a bare "who" and
#: "author" ask about one person or a document's byline ("who wrote the
#: net-zero paper?"), which the corpus answers, and reading two staff listings
#: for them would only crowd the right passage out of the context.
_STAFF_WORDS = frozenset(
    """
    researcher researchers scientist scientists expert experts specialist
    specialists staff team teams people fellow fellows director directors
    leader leaders leadership employee employees faculty
    """.split()
)

PROFILE = "profile"
GROUP_KIND = "group"

_ORG = re.compile(r"\b(?:TERI(?:['’]s)?|The Energy and Resources Institute)\b\s*", re.I)


@dataclass(frozen=True)
class Target:
    """One page (or group) a question needs."""

    name: str
    kind: str
    reason: str
    url: str | None = None
    score: float = 1.0
    page: PriorityPage | None = None
    group: PriorityGroup | None = None
    person: Person | None = None

    @property
    def key(self) -> str:
        if self.group is not None:
            return self.group.key
        return normalize_url(self.url)

    def describe(self) -> dict:
        """The trace's view of the decision."""
        return {"name": self.name, "kind": self.kind, "reason": self.reason,
                "url": self.url, "score": round(self.score, 4)}


def _has(words: str, phrase: str) -> bool:
    """Whole-word phrase match, tolerating a plural ("annual reports")."""
    return re.search(rf"(?<!\w){re.escape(phrase)}s?(?!\w)", words) is not None


def ranked(targets: Sequence[Target]) -> list[Target]:
    """Strongest first, one per page, the stronger reason kept."""
    best: dict[str, Target] = {}
    for target in targets:
        current = best.get(target.key)
        if current is None or (STRENGTH[target.reason], -target.score) < (
            STRENGTH[current.reason], -current.score
        ):
            best[target.key] = target
    return sorted(best.values(), key=lambda t: (STRENGTH[t.reason], -t.score))


def explicit(
    question: str,
    *,
    registry: Registry,
    people: Sequence[Person] = (),
    theme: str | None = None,
    themes_listing: bool = False,
) -> list[Target]:
    """The deterministic triggers: people, names, groups, the theme facet.

    ``themes_listing`` says query understanding read the question as a request
    for the list of themes, which names the home page whatever the wording.
    """
    words = normalize_text(question)
    targets: list[Target] = []
    for person in named_in(question, list(people))[:2]:
        targets.append(Target(person.name, PROFILE, PERSON, url=person.profile_url, person=person))
    for group in registry.groups:
        if any(_has(words, alias) for alias in group.aliases):
            targets.append(Target(group.name, GROUP_KIND, GROUP, group=group))
    for page in registry.pages:
        if any(_has(words, alias) for alias in page.aliases):
            targets.append(Target(page.name, page.kind, NAME, url=page.url, page=page))
    home = registry.home
    if themes_listing and home is not None:
        targets.append(Target(home.name, home.kind, NAME, url=home.url, page=home))
    wanted = normalize_text(theme)
    if wanted:
        for page in registry.pages:
            if page.kind in (THEME, CENTRE) and normalize_text(page.topic) == wanted:
                targets.append(Target(page.name, page.kind, THEME_FACET, url=page.url, page=page))
    if any(t.reason == NAME and t.kind == THEME for t in targets):
        # "the climate change thematic area" is about one theme, not the list.
        targets = [t for t in targets if not (t.page is not None and t.page.is_home)]
    return ranked(targets)


# -- description similarity -----------------------------------------------------

_vectors: dict[tuple[str, ...], list[list[float]]] = {}


def _default_embed(texts: list[str]) -> list[list[float]]:
    from app.core.clients.embeddings import get_embeddings

    return get_embeddings().embed_documents(texts)


def description_text(page: PriorityPage) -> str:
    """What a page's description is embedded as. The organisation's own name is
    removed: every description mentions it and most questions do too, and left
    in it pulled "TERI's work on air pollution" towards the Policy page. A theme
    is embedded by its topic, without the file's "Theme": with it, "what are
    TERI's thematic areas" scored 0.56 against the Environment page."""
    return _ORG.sub("", f"{page.topic}. {page.description}").strip()


def description_vectors(
    registry: Registry, embed: Callable[[list[str]], list[list[float]]] | None = None
) -> list[list[float]]:
    """One vector per page, embedded once per process for a given list."""
    texts = tuple(description_text(p) for p in registry.pages)
    if texts not in _vectors:
        _vectors[texts] = list((embed or _default_embed)(list(texts)))
    return _vectors[texts]


def clear_vectors() -> None:
    _vectors.clear()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def asks_for_staff(question: str) -> bool:
    """Whether the question asks for the organisation's people as a group."""
    return bool(set(normalize_text(question).split()) & _STAFF_WORDS)


def staff_listings(
    question: str,
    query_vector: Sequence[float],
    *,
    registry: Registry,
    targets: Sequence[Target] = (),
    embed: Callable[[list[str]], list[list[float]]] | None = None,
) -> list[Target]:
    """The people listings a question for the organisation's people needs.

    Measured 2026-09-25: "List TERI's leading researchers" read no people page
    at all — no curated phrase says "researchers", and the closest listing's
    description scored 0.428 against a 0.48 bar — so the context was the Mission
    and Goals page and three "About TERI" brochures, and the answer was the
    refusal. The Committee of Directors and Distinguished Fellows listings held
    the answer, name by name.

    Nothing when the question names someone (their profile answers it) or
    already names a people listing ("the governing council", "director
    general"). Otherwise the listings ranked by how close their descriptions sit
    to the question, the best `MAX_STAFF_LISTINGS`; with no vectors, the file's
    own order. Never raises.
    """
    if not asks_for_staff(question):
        return []
    if any(t.reason == PERSON or (t.page is not None and t.page.kind == PEOPLE)
           for t in targets):
        return []
    listings = [p for p in registry.pages if p.kind == PEOPLE]
    if not listings:
        return []
    scores: dict[str, float] = {}
    if query_vector:
        try:
            vectors = description_vectors(registry, embed)
            scores = {p.key: _cosine(query_vector, v) for p, v in zip(registry.pages, vectors)}
        except Exception:
            logger.warning("Priority page descriptions could not be embedded; "
                           "taking the people listings in file order.", exc_info=True)
    order = {p.key: i for i, p in enumerate(listings)}
    chosen = sorted(listings, key=lambda p: (-scores.get(p.key, 0.0), order[p.key]))
    return [Target(p.name, p.kind, STAFF, url=p.url, score=scores.get(p.key, 0.0), page=p)
            for p in chosen[:MAX_STAFF_LISTINGS]]


def similar(
    query_vector: Sequence[float],
    *,
    registry: Registry,
    embed: Callable[[list[str]], list[list[float]]] | None = None,
) -> tuple[list[Target], list[dict]]:
    """The one page whose description the query is clearly about, if any, plus
    the top scores for the trace. Never raises: a failed embedding costs this
    trigger, not the question."""
    if not query_vector or not registry.pages:
        return [], []
    settings = get_settings()
    try:
        vectors = description_vectors(registry, embed)
    except Exception:
        logger.warning("Priority page descriptions could not be embedded; "
                       "skipping the similarity trigger.", exc_info=True)
        return [], []
    scored = sorted(
        ((_cosine(query_vector, v), page) for v, page in zip(vectors, registry.pages)),
        key=lambda item: item[0], reverse=True,
    )
    top = [{"name": p.name, "score": round(s, 4)} for s, p in scored[:3]]
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < settings.priority_match_threshold:
        return [], top
    if best_score - runner_up < settings.priority_match_margin:
        return [], top
    return [Target(best.name, best.kind, SIMILAR, url=best.url, score=best_score, page=best)], top
