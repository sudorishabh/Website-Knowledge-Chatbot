"""Candidate generation: which canonical entities could this mention denote?

Generation is deliberately separate from scoring. Its job is *recall* over a
bounded set — find everything worth considering, decide nothing — and its
contract is that a candidate's presence says only "this was worth looking at".

Bounded and deterministic:

* every source is an index lookup, never a scan over all entities;
* the shortlist is capped (``MAX_CANDIDATES``), and when the cap bites the
  mention is marked so the resolver can refuse rather than pick from a
  truncated list;
* the same mention and index always produce the same candidates in the same
  order, which is what makes repeated resolution idempotent.

Blocking, for PERSON, is by *initials* rather than by name similarity. That is
the widest net that is still cheap ("r k pachauri" and "rajendra kumar pachauri"
share ``rkp``), and it is explicitly not a match — scoring decides, and for
PERSON it decides conservatively.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from app.knowledge.normalize import initials_of, normalize_for
from app.knowledge.seed import PROJECT_CODE_SCHEME

logger = logging.getLogger(__name__)

# How a candidate was found, strongest first. The order is the tier order: an
# identifier match is a database invariant, an exact canonical name is near
# certain, an alias is asserted, and a blocking hit is merely plausible.
CANDIDATE_SOURCES = ("identifier", "exact_name", "alias", "blocked")

# A shortlist longer than this means the surface is too common to be evidence of
# anything. Truncating and then choosing would turn "too ambiguous to decide"
# into an arbitrary pick, so the cap is recorded and the resolver refuses.
MAX_CANDIDATES = 25


@dataclass(frozen=True)
class Candidate:
    """One entity a mention might denote, with how it was found."""

    entity_id: str
    entity_type: str
    canonical_name: str
    normalized_name: str
    trust: str
    source: str
    # False for a provisional identity — a name the corpus attests but has not
    # shown to denote one real thing. Such a candidate may still be *linked*,
    # to group sightings by name, but nothing downstream may treat that link as
    # a canonical identity (see seed.is_claim_eligible).
    claim_eligible: bool = True
    # False when the alias that produced this candidate is shared with another
    # entity, or is too generic to link on its own.
    autolink: bool = True
    is_ambiguous: bool = False
    alias_type: str | None = None


@dataclass
class ResolutionContext:
    """What the surrounding document and chunk say, for corroboration.

    Corroboration is what separates "the name matches" from "the name matches
    and everything around it agrees". For PERSON it is the difference between a
    safe link and a false merge, which is why the resolver requires it.
    """

    document_id: str = ""
    # Names this document's own CMS metadata asserts, normalized per type.
    cms_names: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # Other entity types' normalized mentions in the same chunk, for
    # co-occurrence: a person beside their employer is corroborated.
    co_mentions: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def asserts(self, entity_type: str, normalized: str) -> bool:
        return normalized in self.cms_names.get(entity_type, set())

    def co_mentioned(self, entity_type: str, normalized: str) -> bool:
        return normalized in self.co_mentions.get(entity_type, set())


class EntityIndex:
    """In-memory lookup over the canonical entity store.

    Built once and held: resolution runs per mention over millions of mentions,
    so a per-lookup query would dominate. Follows the gazetteer's precedent.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.entities: dict[str, dict[str, Any]] = payload["entities"]
        self.identifiers: dict[tuple[str, str], str] = payload["identifiers"]
        self._by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._by_alias: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self._by_initials: dict[tuple[str, str], list[str]] = defaultdict(list)

        for entity_id, row in self.entities.items():
            key = (row["entity_type"], row["normalized_name"])
            self._by_name[key].append(entity_id)
            if row["entity_type"] == "PERSON":
                self._by_initials[
                    ("PERSON", initials_of(row["normalized_name"]))
                ].append(entity_id)

        for alias in payload["aliases"]:
            entity = self.entities.get(alias["entity_id"])
            if entity is None:
                continue
            self._by_alias[(entity["entity_type"], alias["normalized"])].append(alias)

        for bucket in (self._by_name, self._by_initials):
            for value in bucket.values():
                value.sort()

    @classmethod
    def load(cls) -> "EntityIndex":
        from app.catalog import entities as store

        return cls(store.load_index())

    def _candidate(self, entity_id: str, source: str, alias: dict | None = None) -> Candidate | None:
        row = self.entities.get(entity_id)
        if row is None:
            return None
        return Candidate(
            entity_id=entity_id,
            entity_type=row["entity_type"],
            canonical_name=row["canonical_name"],
            normalized_name=row["normalized_name"],
            trust=row["trust"],
            source=source,
            claim_eligible=bool(row.get("claim_eligible", 1)),
            autolink=bool(alias["autolink"]) if alias else True,
            is_ambiguous=bool(alias["is_ambiguous"]) if alias else False,
            alias_type=alias["alias_type"] if alias else None,
        )

    # ----------------------------------------------------------------- #
    # Per-source lookups. Each is bounded by an index, never a scan.
    # ----------------------------------------------------------------- #

    def by_identifier(self, scheme: str, value: str) -> Candidate | None:
        entity_id = self.identifiers.get((scheme, value))
        return self._candidate(entity_id, "identifier") if entity_id else None

    def by_exact_name(self, entity_type: str, normalized: str) -> list[Candidate]:
        return [
            c
            for entity_id in self._by_name.get((entity_type, normalized), ())
            if (c := self._candidate(entity_id, "exact_name")) is not None
        ]

    def by_alias(self, entity_type: str, normalized: str) -> list[Candidate]:
        return [
            c
            for alias in self._by_alias.get((entity_type, normalized), ())
            if (c := self._candidate(alias["entity_id"], "alias", alias)) is not None
        ]

    def alias_groups(self) -> Iterable[tuple[tuple[str, str], list[Candidate]]]:
        """Every ``(entity_type, normalized)`` alias group, as candidates.

        :meth:`by_alias` answers one key at a time, which is what per-mention
        resolution wants. A consumer that needs to build its own lookup over
        *all* aliases would otherwise have to reach into the private index, so
        this is the supported way to enumerate them.
        """
        for key in self._by_alias:
            yield key, self.by_alias(*key)

    def by_initials(self, normalized_person: str) -> list[Candidate]:
        key = ("PERSON", initials_of(normalized_person))
        return [
            c
            for entity_id in self._by_initials.get(key, ())
            if (c := self._candidate(entity_id, "blocked")) is not None
        ]


@dataclass
class CandidateSet:
    """The bounded shortlist for one mention, with why it looks like this."""

    candidates: list[Candidate]
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.candidates)

    def __iter__(self) -> Iterable[Candidate]:
        return iter(self.candidates)

    @property
    def best_source(self) -> str | None:
        for source in CANDIDATE_SOURCES:
            if any(c.source == source for c in self.candidates):
                return source
        return None


def _dedupe(candidates: Iterable[Candidate]) -> list[Candidate]:
    """One candidate per entity, keeping the strongest source that found it."""
    rank = {source: i for i, source in enumerate(CANDIDATE_SOURCES)}
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        current = best.get(candidate.entity_id)
        if current is None or rank[candidate.source] < rank[current.source]:
            best[candidate.entity_id] = candidate
    return sorted(
        best.values(), key=lambda c: (rank[c.source], c.entity_id)
    )


def generate(mention: Any, index: EntityIndex) -> CandidateSet:
    """Every entity worth considering for this mention, bounded and ordered.

    PROJECT is the one type whose surface may be an identifier: a project code
    is looked up first and, when it hits, nothing else is considered — the
    ``(scheme, value)`` key is a database invariant, not an inference, so there
    is nothing for scoring to weigh.
    """
    entity_type = mention.entity_type
    normalized = mention.normalized_text
    found: list[Candidate] = []

    if entity_type == "PROJECT":
        by_code = index.by_identifier(PROJECT_CODE_SCHEME, mention.surface_text.strip())
        if by_code is not None:
            return CandidateSet([by_code])

    found.extend(index.by_exact_name(entity_type, normalized))
    found.extend(index.by_alias(entity_type, normalized))

    # Blocking only for PERSON, and only when nothing exact was found. It exists
    # to surface "R K Pachauri" against "Rajendra Kumar Pachauri"; running it
    # when an exact match already exists would only add noise for scoring to
    # discard.
    if entity_type == "PERSON" and not found:
        found.extend(index.by_initials(normalized))

    deduped = _dedupe(found)
    if len(deduped) > MAX_CANDIDATES:
        return CandidateSet(deduped[:MAX_CANDIDATES], truncated=True)
    return CandidateSet(deduped)


@lru_cache(maxsize=1)
def get_entity_index() -> EntityIndex:
    """The process-wide entity index, loaded once.

    ``EntityIndex.load`` reads the whole entity table. The corpus builder pays
    that once per run and holds the result; the per-document knowledge stage
    runs once per ingested document and cannot, so the cache lives here rather
    than in either caller. Follows ``app.knowledge.gazetteer.get_gazetteer``,
    which exists for exactly the same reason on the same hot path.

    Staleness is bounded by process lifetime and is safe in the conservative
    direction: an index that has not yet seen an entity yields
    ``unknown_subject`` or ``UNRESOLVED``, never a wrong link. Seeding is a
    global act (see :mod:`app.knowledge.seed`), so the corpus builder calls
    :func:`reload_entity_index` after it writes.
    """
    return EntityIndex.load()


def reload_entity_index() -> EntityIndex:
    """Forget the cached index and rebuild it.

    Called after seeding, promotion or any other write to the entity store,
    because everything downstream resolves against what those just wrote.
    """
    get_entity_index.cache_clear()
    return get_entity_index()


def context_for_document(
    document_id: str,
    raw_meta: dict[str, Any] | None,
    *,
    authors: Iterable[str] = (),
) -> ResolutionContext:
    """Corroboration drawn from a document's own CMS metadata and facets.

    ``authors`` is supplied by the caller rather than read here, and both halves
    of that matter.

    *Supplied*, because author names no longer live in the metadata blob. They
    were moved to the ``documents_author`` facet, which holds 1,860 rows while
    ``raw_meta.field_authors`` holds **none** — so reading only the metadata
    left ``PERSON`` corroboration permanently empty. That failed in the
    expensive direction and silently: PERSON is the one type the resolver
    requires corroboration for, so a uniquely-matching name landed on
    ``AMBIGUOUS`` — "unique name match but no corroborating context" — instead
    of ``AUTO``, and no count said why.

    *By the caller*, because this function stays pure. It is called once per
    document inside the extraction loop, and the callers already differ in
    where the names are: ingestion has them in memory on the canonical
    document, while a CLI or retry pass reads them from the facet with
    ``state.authors_for``. A query in here would impose one of those on both.

    ``field_authors`` is still read, so a corpus that repopulates the metadata
    key keeps working; the two sources are unioned rather than either winning.
    """
    context = ResolutionContext(document_id=document_id)
    fields = {
        "PERSON": ("field_authors",),
        "ORGANIZATION": (
            "field_completed_sponsors", "field_news_source", "field_division",
        ),
    }
    if raw_meta:
        for entity_type, names in fields.items():
            for field_name in names:
                value = raw_meta.get(field_name)
                items = value if isinstance(value, list) else [value] if value else []
                for item in items:
                    text = str(item).strip()
                    if text:
                        context.cms_names[entity_type].add(
                            normalize_for(entity_type, text)
                        )
    for author in authors or ():
        text = str(author).strip()
        if text:
            context.cms_names["PERSON"].add(normalize_for("PERSON", text))
    return context
