"""Seed canonical entities, aliases and identifiers from CMS records.

Resolution needs things to resolve *to*. This builds them, and only from
sources the CMS actually asserts — never from prose, and never from taxonomy.

Sources per type, and what each is worth
----------------------------------------
PROJECT       ``completed_projects`` / ``ongoing_projects`` nodes (~1,623).
              936 of them carry ``field_completed_project_code`` (932 distinct,
              e.g. ``2004BS22``), which becomes an **identifier** — the one
              Tier-0 signal in this corpus.
ORGANIZATION  ``field_completed_sponsors``, ``field_news_source``,
              ``field_division``. Plain text in ``raw_meta``, not taxonomy
              references, so unaffected by taxonomy removal. No CMS uuid, so
              these are ``trust='derived'`` and keyed by their normalized name.
PERSON        the ``people`` bundle (**8** nodes, ``trust='authoritative'``),
              then ``field_authors``, the two ``field_*_pi_name`` fields and the
              ``documents_author`` facet — all seeded ``provisional``, because
              the corpus supplies names rather than people. A PI name earns
              *consideration* for promotion (app.knowledge.pi_promotion), never
              eligibility on its own.

``entity_id`` is derived deterministically from the seed source, so re-seeding a
clean corpus reproduces the same ids instead of minting new ones. That is what
makes the whole layer rebuildable, which the re-ingestion plan depends on.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from app.knowledge.normalize import (
    is_initials_only,
    normalize_org,
    normalize_person,
    normalize_project,
    split_joined_org_names,
)

logger = logging.getLogger(__name__)

SEEDER_VERSION = "entity-seed-v1"

_PREFIX = {"PERSON": "person", "ORGANIZATION": "org", "PROJECT": "project"}
_NORMALIZE = {
    "PERSON": normalize_person,
    "ORGANIZATION": normalize_org,
    "PROJECT": normalize_project,
}

# `entity_id` shape, checked before an id is ever used in a query.
ENTITY_ID_RE = re.compile(r"^(?:person|org|project)_[0-9a-f]{12}$")

# The identifier scheme for a TERI project code.
PROJECT_CODE_SCHEME = "teri_project_code"
_PROJECT_CODE_RE = re.compile(r"^(?:19|20)\d{2}[A-Z]{2}\d{2}$")

_PROJECT_BUNDLES = ("completed_projects", "ongoing_projects")
_ORG_FIELDS = ("field_completed_sponsors", "field_news_source", "field_division")


def entity_id_for(entity_type: str, seed_key: str) -> str:
    """A stable, opaque id derived from what the entity was seeded from.

    Deterministic rather than sequential so a rebuild is a no-op: the same CMS
    record yields the same id, and nothing has to remember a counter.
    """
    digest = hashlib.sha256(f"{entity_type}\x1f{seed_key}".encode("utf-8")).hexdigest()
    return f"{_PREFIX[entity_type]}_{digest[:12]}"


# Identity confidence, and what each level licenses.
#
# AUTHORITATIVE  a CMS record asserts this thing exists and is distinct. Safe to
#                carry claims and to answer graph queries about.
# DERIVED        no CMS record of its own, but the name is discriminative enough
#                that one name reliably denotes one thing. Organizations sit
#                here: "Ministry of External Affairs" names one ministry.
# PROVISIONAL    a name the corpus attests, which has NOT been shown to denote
#                exactly one real-world thing. Person names from the author
#                facet are the case this exists for: two different people called
#                "Arun Kumar" are one row, so the row is a *name*, not a person.
TRUST_AUTHORITATIVE = "authoritative"
TRUST_DERIVED = "derived"
TRUST_PROVISIONAL = "provisional"

# Only these may be a claim subject/object or a graph-retrieval target. A
# provisional identity is deliberately excluded: attaching a claim to it would
# assert something about a person the corpus has not distinguished.
# A name the CMS names as a project's principal investigator, which then
# survived every discriminating test in app.knowledge.pi_promotion. Stronger
# than a bare facet name, weaker than a CMS person record — kept as its own
# level so a promotion stays auditable and reversible.
TRUST_PI_ATTESTED = "pi_attested"

# A name an authoritative CMS author field states wrote a document, which then
# survived every discriminating test in app.knowledge.author_promotion. Its own
# level for the same reason as pi_attested -- a promotion stays auditable and
# reversible -- but weaker: a PI field is a curated assertion about one coded
# project, while an author facet accumulates every name printed on anything.
#
# Deliberately NOT in CLAIM_ELIGIBLE_TRUST. This level grants one predicate,
# AUTHORED, and nothing else: the CMS states outright who wrote a document, so
# that fact needs no identity beyond the name, whereas "X works at Y" asserts
# something about the person and still requires an identity the corpus has
# distinguished. See `is_claim_eligible_for`.
TRUST_AUTHOR_ATTESTED = "author_attested"

CLAIM_ELIGIBLE_TRUST = frozenset(
    {TRUST_AUTHORITATIVE, TRUST_DERIVED, TRUST_PI_ATTESTED}
)

#: Trust levels that grant only specific predicates, and which ones. Kept beside
#: CLAIM_ELIGIBLE_TRUST so the two cannot drift: a level appears in one or the
#: other, never both.
PREDICATE_SCOPED_TRUST: dict[str, frozenset[str]] = {
    TRUST_AUTHOR_ATTESTED: frozenset({"AUTHORED"}),
}


def is_claim_eligible(trust: str) -> bool:
    """Whether an identity at this trust level may carry claims of any kind."""
    return trust in CLAIM_ELIGIBLE_TRUST


def is_claim_eligible_for(trust: str, predicate: str | None) -> bool:
    """Whether this trust level may carry a claim under this predicate.

    Broad eligibility first, then the scoped grants. A caller that does not know
    its predicate gets the broad answer only, so an unscoped check can never be
    widened by accident -- the scoped level has to be asked about explicitly.
    """
    if is_claim_eligible(trust):
        return True
    if predicate is None:
        return False
    return predicate in PREDICATE_SCOPED_TRUST.get(trust, frozenset())


@dataclass
class SeedEntity:
    entity_id: str
    entity_type: str
    canonical_name: str
    normalized_name: str
    source: str
    cms_uuid: str | None = None
    trust: str = TRUST_DERIVED
    aliases: list[tuple[str, str, str]] = field(default_factory=list)  # surface, type, source
    identifiers: list[tuple[str, str]] = field(default_factory=list)   # scheme, value

    @property
    def claim_eligible(self) -> bool:
        return is_claim_eligible(self.trust)


def _is_name_like(normalized: str) -> bool:
    """Whether every token of a normalized name starts with a letter."""
    tokens = normalized.split()
    return bool(tokens) and all(token[:1].isalpha() for token in tokens)


def _json_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if str(v).strip()]


def _seed_projects(cur, table: str) -> list[SeedEntity]:
    """Project nodes. Keyed by CMS uuid, with the project code as identifier."""
    placeholders = ", ".join(["%s"] * len(_PROJECT_BUNDLES))
    cur.execute(
        f"SELECT document_id, title, raw_meta FROM `{table}` "
        f"WHERE bundle IN ({placeholders}) AND entity_type = 'node' "
        "AND title IS NOT NULL AND title <> ''",
        _PROJECT_BUNDLES,
    )
    out: list[SeedEntity] = []
    for row in cur.fetchall():
        title = (row["title"] or "").strip()
        normalized = normalize_project(title)
        if not normalized:
            continue
        uuid = row["document_id"]
        entity = SeedEntity(
            entity_id=entity_id_for("PROJECT", uuid),
            entity_type="PROJECT",
            canonical_name=title,
            normalized_name=normalized,
            source="cms_project_node",
            cms_uuid=uuid,
            trust=TRUST_AUTHORITATIVE,
            aliases=[(title, "title", "cms_project_node")],
        )
        raw = row["raw_meta"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            meta = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            meta = {}
        code = next(
            (
                str(meta.get(f) or "").strip()
                for f in ("field_completed_project_code", "field_ongoing_project_code")
                if str(meta.get(f) or "").strip()
            ),
            "",
        )
        if _PROJECT_CODE_RE.match(code):
            entity.identifiers.append((PROJECT_CODE_SCHEME, code))
            # The code is also a surface people write in text ("TERI Report No.
            # 2004RP23"), so it is an alias as well as an identifier.
            entity.aliases.append((code, "code", "cms_project_code"))
        out.append(entity)
    return out


def _seed_people(cur, table: str) -> list[SeedEntity]:
    """The 8 authoritative people nodes, then the far larger derived facets."""
    out: list[SeedEntity] = []
    cur.execute(
        f"SELECT document_id, title FROM `{table}` "
        "WHERE bundle = 'people' AND entity_type = 'node' AND title IS NOT NULL"
    )
    for row in cur.fetchall():
        name = (row["title"] or "").strip()
        normalized = normalize_person(name)
        if not normalized:
            continue
        out.append(
            SeedEntity(
                entity_id=entity_id_for("PERSON", row["document_id"]),
                entity_type="PERSON",
                canonical_name=name,
                normalized_name=normalized,
                source="cms_people_node",
                cms_uuid=row["document_id"],
                trust=TRUST_AUTHORITATIVE,
                aliases=[(name, "full_name", "cms_people_node")],
            )
        )

    # Everything below is PROVISIONAL, and the distinction is the whole point.
    #
    # The author facet gives *names*, not people. Keying by normalized name
    # means one row per name, so two different people called "Arun Kumar" are
    # one row — and that conflation is unavoidable, because the corpus carries
    # nothing that would tell them apart. What can be avoided is *lying about
    # it*: these rows are marked provisional, so nothing downstream may treat a
    # name-level grouping as a canonical person.
    #
    # An author name that matches an authoritative people-node becomes an alias
    # of that node instead, which is how the 8 real identities absorb their own
    # spelling variants.
    seen: dict[str, SeedEntity] = {e.normalized_name: e for e in out}
    for source, values in _author_sources(cur, table):
        for name in values:
            normalized = normalize_person(name)
            # An initials-only or single-token author names nobody in
            # particular; seeding it would create an entity that attracts
            # false merges for the rest of the corpus's life.
            if not normalized or is_initials_only(normalized):
                continue
            if len(normalized.split()) < 2:
                continue
            # The facet holds fragments as well as names — "& Sharma" is a real
            # value, left behind by a split on "and". A name every one of whose
            # tokens does not begin with a letter is not a person, and seeding
            # it would create an entity for other mentions to collide with.
            if not _is_name_like(normalized):
                continue
            existing = seen.get(normalized)
            if existing is not None:
                existing.aliases.append((name, "full_name", source))
                continue
            entity = SeedEntity(
                entity_id=entity_id_for("PERSON", normalized),
                entity_type="PERSON",
                canonical_name=name,
                normalized_name=normalized,
                source=source,
                trust=TRUST_PROVISIONAL,
                aliases=[(name, "full_name", source)],
            )
            seen[normalized] = entity
            out.append(entity)
    return out


def _author_sources(cur, table: str) -> Iterable[tuple[str, list[str]]]:
    for field_name in ("field_authors", "field_completed_pi_name",
                       "field_ongoing_pi_name"):
        cur.execute(
            f"SELECT JSON_EXTRACT(raw_meta, %s) AS v FROM `{table}` "
            "WHERE JSON_EXTRACT(raw_meta, %s) IS NOT NULL",
            (f"$.{field_name}", f"$.{field_name}"),
        )
        values: list[str] = []
        for row in cur.fetchall():
            values.extend(_json_values(row["v"]))
        yield field_name, values

    cur.execute(f"SELECT DISTINCT author FROM `{table}_author`")
    yield "documents_author", [
        r["author"].strip() for r in cur.fetchall() if (r["author"] or "").strip()
    ]


def _seed_organizations(cur, table: str) -> list[SeedEntity]:
    """Sponsors, publications and divisions. No CMS uuid, so keyed by name."""
    seen: dict[str, SeedEntity] = {}
    out: list[SeedEntity] = []
    for field_name in _ORG_FIELDS:
        cur.execute(
            f"SELECT JSON_EXTRACT(raw_meta, %s) AS v FROM `{table}` "
            "WHERE JSON_EXTRACT(raw_meta, %s) IS NOT NULL",
            (f"$.{field_name}", f"$.{field_name}"),
        )
        for row in cur.fetchall():
            for value in _json_values(row["v"]):
                # One value may name several sponsors joined into one string;
                # see normalize.split_joined_org_names. A value that names one
                # organization comes back unchanged.
                for name in split_joined_org_names(value):
                    normalized = normalize_org(name)
                    if not normalized:
                        continue
                    existing = seen.get(normalized)
                    if existing is not None:
                        existing.aliases.append((name, "full_name", field_name))
                        continue
                    entity = SeedEntity(
                        entity_id=entity_id_for("ORGANIZATION", normalized),
                        entity_type="ORGANIZATION",
                        canonical_name=name,
                        normalized_name=normalized,
                        source=field_name,
                        aliases=[(name, "full_name", field_name)],
                    )
                    seen[normalized] = entity
                    out.append(entity)
    return out


def build_seed_entities() -> list[SeedEntity]:
    """Every canonical entity the CMS supports, with aliases and identifiers."""
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        entities = (
            _seed_projects(cur, table)
            + _seed_organizations(cur, table)
            + _seed_people(cur, table)
        )
    logger.info("Seeded %d canonical entities.", len(entities))
    return entities


# --------------------------------------------------------------------------- #
# Acronym aliases mined from the corpus
# --------------------------------------------------------------------------- #

# Minimum times a gloss must be observed before its acronym becomes an alias.
# One sighting can be an OCR artifact or a one-off coinage; a form the corpus
# writes repeatedly is a name it actually uses.
MIN_GLOSS_OBSERVATIONS = 2


def mine_acronym_aliases(*, limit_chunks: int | None = None) -> list[tuple[str, str, int]]:
    """Find "Full Name (ACR)" glosses in chunk text and pair them to entities.

    Organizations in this corpus are referred to by acronym far more often than
    by full name — TERI, IOCL, CPCB, MNRE — and no CMS field lists those forms.
    The gloss is where the corpus itself declares them, so it is evidence rather
    than guesswork: the expansion must match a seeded entity's own name, and the
    pairing must be observed more than once.

    Returns ``(entity_id, acronym, observations)``; writing them is the caller's
    job so this stays testable without a database.
    """
    from collections import Counter

    from app.catalog import entities as store
    from app.config import get_settings
    from app.core.clients import get_qdrant_client
    from app.knowledge.extract import _ORG_ACRONYM_GLOSS
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    index = store.load_index()
    by_name: dict[str, str] = {
        row["normalized_name"]: entity_id
        for entity_id, row in index["entities"].items()
        if row["entity_type"] == "ORGANIZATION"
    }
    # A gloss pairs two names, and either side may be the one the CMS seeded.
    # TERI itself is the case that matters here: the sponsor field lists it as
    # "TERI", so the *acronym* is the canonical name and the full name is the
    # form that needs an alias. Matching only expansion->entity left "The Energy
    # and Resources Institute" resolving to nothing at all.
    by_acronym: dict[str, str] = {
        row["normalized_name"]: entity_id
        for entity_id, row in index["entities"].items()
        if row["entity_type"] == "ORGANIZATION"
        and row["canonical_name"].isupper()
        and 2 <= len(row["canonical_name"]) <= 8
    }

    client = get_qdrant_client()
    collection = get_settings().qdrant_collection
    # (entity_id, surface_to_add) -> times the gloss was seen.
    observed: Counter[tuple[str, str]] = Counter()
    scanned = 0
    next_page = None
    while True:
        points, next_page = client.scroll(
            collection_name=collection,
            scroll_filter=Filter(
                must=[FieldCondition(key="is_parent", match=MatchValue(value=False))]
            ),
            limit=1000, offset=next_page,
            with_payload=["chunk_text"], with_vectors=False,
        )
        for point in points:
            text = (point.payload or {}).get("chunk_text") or ""
            scanned += 1
            for match in _ORG_ACRONYM_GLOSS.finditer(text):
                expansion = " ".join(match.group(1).split())
                acronym = match.group(2)
                # Whichever side the CMS seeded, the other side becomes the
                # alias. Checked in this order so a corpus that seeds both keeps
                # the fuller name as canonical.
                entity_id = by_name.get(normalize_org(expansion))
                if entity_id:
                    observed[(entity_id, acronym)] += 1
                    continue
                entity_id = by_acronym.get(normalize_org(acronym))
                if entity_id:
                    observed[(entity_id, expansion)] += 1
        if next_page is None or (limit_chunks and scanned >= limit_chunks):
            break

    logger.info("Scanned %d chunks for acronym glosses.", scanned)
    return [
        (entity_id, acronym, count)
        for (entity_id, acronym), count in sorted(observed.items())
        if count >= MIN_GLOSS_OBSERVATIONS
    ]
