"""Deterministic claims from CMS metadata.

Free, exact, and the largest source of true relationships this corpus has. The
CMS asserts the relationship directly, so there is nothing to infer and no model
to distrust.

What the fields actually say
----------------------------
``field_completed_sponsors`` (915) and ``field_ongoing_sponsors`` (377) name the
organizations that funded a project. **All 915 completed-sponsor values resolve
to a seeded ORGANIZATION**, which is what makes this worth preferring over any
text extraction.

``field_completed_pi_name`` (497) and ``field_ongoing_pi_name`` (414) name the
principal investigator — a person who leads the project. Most of those names are
*provisional* identities, so most of these claims are refused by the eligibility
gate; that refusal is correct and is the point of the gate.

``field_completed_start_date`` / ``field_completed_end_date`` (1,030 each) give a
completed project a closed period. ``field_ongoing_start_date`` (593) gives an
ongoing one a start and no end — the corpus's own way of saying "current".
Both feed :func:`app.knowledge.claims.temporal.subject_period`.

``field_ongoing_stakeholders`` is deliberately **not** used: its values are
audience categories ("Policy Makers", "Businesses", "Academicians"), not
organizations, so no relationship can be built from it.

Evidence is ``cms_field`` rather than a quote: the fact lives in a metadata
field, not in prose, and inventing a quote would fabricate a span.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from app.knowledge.claims import temporal, types as t
from app.knowledge.normalize import normalize_org, normalize_person

logger = logging.getLogger(__name__)

# v3 emits AUTHORED from the author fields and PARTNER_OF from the partner
# fields. The sponsor and PI rules are unchanged; only the recorded version
# moves, which is what this field is for.
EXTRACTOR_VERSION = "claims-cms-v3"

# CMS field -> (predicate, object entity type). Only fields whose meaning is
# unambiguous: these claims assert at confidence 1.0, so there is no score to
# hedge with and an arguable reading does not belong here.
_FIELD_RULES: tuple[tuple[str, str, str], ...] = (
    ("field_completed_sponsors", "FUNDED_BY", "ORGANIZATION"),
    ("field_ongoing_sponsors", "FUNDED_BY", "ORGANIZATION"),
    ("field_completed_pi_name", "LED_BY", "PERSON"),
    ("field_ongoing_pi_name", "LED_BY", "PERSON"),
    # Partners sit alongside sponsors: same subject (the project this document
    # is about), same object type, and PARTNER_OF already declares exactly
    # PROJECT -> ORGANIZATION. The fields were simply never mapped, so 287
    # projects named their partners in structured metadata and the graph held
    # no PARTNER_OF edge at all.
    #
    # Distinct from FUNDED_BY on purpose: a partner delivered the work with the
    # project, a sponsor paid for it. The CMS keeps them in separate fields and
    # so does the graph.
    #
    # `field_*_stakeholders` remains excluded -- its values are audience
    # categories ("Policy Makers", "Academicians"), not organizations.
    ("field_completed_partners", "PARTNER_OF", "ORGANIZATION"),
    ("field_ongoing_partners", "PARTNER_OF", "ORGANIZATION"),
)

# Author fields, in the order their provenance is recorded. A person named in
# more than one of them on the same document states ONE authorship fact -- 179
# documents carry two or three of these fields and 35 (person, document) pairs
# appear twice -- so the first field that resolves a person is the one recorded
# and the rest are skipped. Without that, `claim_id` would differ (it includes
# `source_field`) and one fact would become two edges.
#
# `field_opinion_ext_auth` is deliberately excluded: its values are lists in one
# string ("Dr Syamal Kumar Sarkar and Dr Snehlata Tigala"), so nothing resolves
# without splitting on conjunctions, and guessing where one name ends is exactly
# the kind of inference that would invent a person.
_AUTHOR_FIELDS: tuple[str, ...] = (
    "field_authors",
    "field_rpaper_author",
    "field_article_authors",
    "field_policybrief_authors",
    "field_external_authors",
    "field_author",
)

# The CMS states these as fact, so they carry full confidence. That is the point
# of preferring them: no model, no interpretation, nothing to review.
CMS_CONFIDENCE = 1.0

PROJECT_BUNDLES = ("completed_projects", "ongoing_projects")
# Retained under the old private name: this module's tests and the corpus driver
# below both referred to it before the per-document path existed.
_PROJECT_BUNDLES = PROJECT_BUNDLES


def is_project_bundle(bundle: str | None) -> bool:
    """Whether a document's bundle can carry CMS project claims at all.

    Lets the per-document path skip the great majority of the corpus — news,
    PDFs, pages — without building an extraction context or reading metadata for
    a document that has no rule to match.
    """
    return bundle in PROJECT_BUNDLES


def _values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return [raw.strip()] if raw.strip() else []
    items = raw if isinstance(raw, list) else [raw]
    return [str(v).strip() for v in items if str(v).strip()]


def _meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw or {}


def _value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class CmsClaimContext:
    """The entity lookups CMS extraction needs, built once from the index.

    Split out so the per-document and whole-corpus paths share one construction
    of it. Building it per document would rebuild three dicts over every entity
    in the store for each document ingested; building it separately in each path
    is how the two would drift.
    """

    # entity_type -> normalized name -> entity_id, for object resolution.
    lookup: dict[str, dict[str, str]]
    # A project node's CMS uuid *is* its document id, so this maps a document to
    # the PROJECT entity that document is about — the claim's subject.
    projects_by_uuid: dict[str, str]
    # entity_type -> normalized *alias* -> entity_id. Consulted only when the
    # canonical lookup misses, and only where the alias is unambiguous; see
    # :func:`_alias_lookup`. Defaults to empty so an index without alias support
    # simply behaves as it did before.
    alias_lookup: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)

    @classmethod
    def from_index(cls, index: Any) -> "CmsClaimContext":
        lookup: dict[str, dict[str, str]] = {"ORGANIZATION": {}, "PERSON": {}}
        projects_by_uuid: dict[str, str] = {}
        for entity_id, row in index.entities.items():
            entity_type = row["entity_type"]
            if entity_type in lookup:
                lookup[entity_type][row["normalized_name"]] = entity_id
            elif entity_type == "PROJECT" and row.get("cms_uuid"):
                projects_by_uuid[row["cms_uuid"]] = entity_id
        return cls(
            lookup=lookup,
            projects_by_uuid=projects_by_uuid,
            alias_lookup=_alias_lookup(index, set(lookup)),
        )

    def subject_for(self, document_id: str) -> str | None:
        """The PROJECT entity this document is about, or None.

        None is the ordinary answer for a project the global seed pass has not
        reached yet. Nothing is minted here — see the module docstring in
        app.knowledge.seed for why entity creation is a deliberate global act.
        """
        return self.projects_by_uuid.get(document_id)

    def object_for(self, object_type: str, value: str) -> str | None:
        """The entity a CMS field value names, by canonical name then by alias.

        The CMS writes whichever form the editor had to hand — "ONGC" in a
        partner field, "Oil and Natural Gas Corporation Limited" as the seeded
        canonical name — so an exact-canonical-only lookup silently drops a
        relationship the CMS stated outright. The alias store already holds the
        other form; this consults it.

        Still exact, on both passes: the value is normalized and looked up. No
        fuzzy or partial matching is introduced, so a value that names nothing
        the store knows continues to resolve to nothing.
        """
        normalizer = _NORMALIZERS.get(object_type)
        if normalizer is None:
            return None
        normalized = normalizer(value)
        canonical = self.lookup.get(object_type, {}).get(normalized)
        if canonical is not None:
            return canonical
        return self.alias_lookup.get(object_type, {}).get(normalized)


def _alias_lookup(index: Any, types: set[str]) -> dict[str, dict[str, str]]:
    """Unambiguous aliases only, as ``entity_type -> normalized -> entity_id``.

    Three protections, all of them the ones the resolver already applies, so
    this path cannot link something per-mention resolution would refuse:

    * ``is_ambiguous`` — the seeder saw this surface form denote more than one
      entity in the corpus, so it identifies nothing on its own.
    * ``autolink=0`` — the alias is recorded but explicitly not safe to link.
    * more than one surviving entity — a collision the ambiguity marker has not
      been applied to yet. Refused rather than arbitrarily picked.

    An index that cannot enumerate aliases yields an empty lookup, which leaves
    :meth:`CmsClaimContext.object_for` exactly as it was.
    """
    groups = getattr(index, "alias_groups", None)
    if groups is None:
        return {}
    out: dict[str, dict[str, str]] = {name: {} for name in types}
    for (entity_type, normalized), candidates in groups():
        if entity_type not in out:
            continue
        safe = {
            c.entity_id
            for c in candidates
            if getattr(c, "autolink", True) and not getattr(c, "is_ambiguous", False)
        }
        if len(safe) == 1:
            out[entity_type][normalized] = safe.pop()
    return out


_NORMALIZERS = {"ORGANIZATION": normalize_org, "PERSON": normalize_person}


def claims_from_meta(
    document_id: str, raw_meta: Any, *, context: CmsClaimContext
) -> list[Any]:
    """Every CMS claim one document states, as unvalidated assertions.

    The single implementation. :func:`extract_cms_claims` drives it over the
    corpus and the per-document knowledge stage calls it directly, so the two
    cannot produce different claims for the same document.

    Returns assertions rather than staging them, so validation still runs. A
    sponsor whose organization was demoted, or a PI who is only a provisional
    identity, must be rejected like anything else rather than trusted for its
    provenance — which is exactly what happens to most PI names here.
    """
    subject = context.subject_for(document_id)
    if subject is None:
        return []
    meta = _meta(raw_meta)
    if not meta:
        return []
    # The project's own CMS-stated period scopes every relationship to it. The
    # single approved inference in the claim layer, and recorded under its own
    # basis so it is never mistaken for a stated relationship date.
    window = temporal.subject_period(meta)

    out: list[Any] = []
    for field_name, predicate, object_type in _FIELD_RULES:
        for value in _values(meta.get(field_name)):
            object_id = context.object_for(object_type, value)
            if object_id is None:
                continue
            out.append(
                t.build(
                    subject_entity_id=subject,
                    predicate=predicate,
                    object_entity_id=object_id,
                    document_id=document_id,
                    evidence_kind=t.EVIDENCE_CMS_FIELD,
                    source_field=field_name,
                    source_value=value[:512],
                    source_value_hash=_value_hash(value),
                    valid_from=window.valid_from,
                    valid_until=window.valid_until,
                    temporal_basis=window.basis,
                    confidence=CMS_CONFIDENCE,
                    extraction_method="cms_field",
                    extractor_version=EXTRACTOR_VERSION,
                )
            )
    return out


def has_author_fields(raw_meta: Any) -> bool:
    """Whether this document names any author at all.

    Cheap dict lookups, so the per-document path can decide to skip building an
    extraction context -- which walks every entity in the store -- for the great
    majority of the corpus that names no author.
    """
    meta = _meta(raw_meta)
    return bool(meta) and any(meta.get(field) for field in _AUTHOR_FIELDS)


def authorship_from_meta(
    document_id: str, raw_meta: Any, *, context: CmsClaimContext
) -> list[Any]:
    """``PERSON -AUTHORED-> this document``, from the CMS author fields.

    Deliberately independent of :func:`claims_from_meta`: that function's
    subject is the PROJECT a document is about, and it returns nothing for a
    document that is not a seeded project. Authorship has a different subject
    (the author) and lives in bundles that have no project at all -- feature
    articles, policy briefs, research papers -- so sharing that path would have
    meant either weakening its project gate or getting no authors.

    The object is the document id as a literal. A document is not one of this
    model's entity types, and the authoritative link to it is the claim's own
    provenance: a CMS claim carries no chunk, so the projection joins it
    ``Claim-[:SUPPORTED_BY]->Document`` and the graph path
    ``Person<-[:SUBJECT]-Claim-[:SUPPORTED_BY]->Document`` is the edge. The
    literal identifies the same document unambiguously and needs no title
    lookup; the readable title already lives on the Document node.

    No entity is ever created here. An author the store does not know is
    skipped, exactly as an unrecognised sponsor is -- the document still
    indexes, and its other authors still land.

    No temporal window: a person authored a document, permanently. Claiming a
    validity period would invent one, so the basis stays unknown rather than
    borrowing the document's date.
    """
    meta = _meta(raw_meta)
    if not meta:
        return []

    out: list[Any] = []
    seen: set[str] = set()
    for field_name in _AUTHOR_FIELDS:
        for value in _values(meta.get(field_name)):
            person = context.object_for("PERSON", value)
            if person is None:
                # Unresolved, ambiguous or simply unknown. Nothing is minted.
                continue
            if person in seen:
                # Already recorded from an earlier field, or listed twice in
                # this one. One authorship fact, one edge.
                continue
            seen.add(person)
            out.append(
                t.build(
                    subject_entity_id=person,
                    predicate="AUTHORED",
                    object_literal=document_id,
                    document_id=document_id,
                    evidence_kind=t.EVIDENCE_CMS_FIELD,
                    source_field=field_name,
                    source_value=value[:512],
                    source_value_hash=_value_hash(value),
                    confidence=CMS_CONFIDENCE,
                    extraction_method="cms_field",
                    extractor_version=EXTRACTOR_VERSION,
                )
            )
    return out


def extract_cms_claims(index: Any, *, limit: int | None = None) -> list[Any]:
    """Every claim the CMS states outright, corpus-wide.

    A driver over :func:`claims_from_meta`, which holds the actual rules.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    context = CmsClaimContext.from_index(index)

    placeholders = ", ".join(["%s"] * len(PROJECT_BUNDLES))
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT document_id, raw_meta FROM `{table}` "
            f"WHERE bundle IN ({placeholders}) AND entity_type = 'node' "
            "AND raw_meta IS NOT NULL",
            PROJECT_BUNDLES,
        )
        rows = cur.fetchall()

    out: list[Any] = []
    for row in rows:
        out.extend(
            claims_from_meta(row["document_id"], row["raw_meta"], context=context)
        )
        if limit is not None and len(out) >= limit:
            break

    logger.info("Built %d CMS-field assertions.", len(out))
    return out


def stale_claim_ids(fresh: list[Any], staged: list[dict[str, Any]]) -> list[str]:
    """Staged CMS claims whose source field no longer supports them.

    The answer to "can an edit to a structured field silently change a claim's
    meaning?" — it cannot, because the field's *value* reaches ``claim_id``
    through the object, so an edit produces a different claim. What an edit
    *can* do is leave the old claim behind, still asserting a sponsor that was
    corrected away.

    So the correct treatment is retraction, not content hashing: any staged
    ``cms_field`` claim for a (document, field) that the current extraction no
    longer produces is stale. Retracted rather than deleted, because the claim
    was true of the source as it stood, and that history is worth keeping.
    """
    fresh_ids = {a.claim_id for a in fresh}
    covered = {
        (a.document_id, a.source_field)
        for a in fresh
        if a.evidence_kind == t.EVIDENCE_CMS_FIELD
    }
    stale: list[str] = []
    for row in staged:
        if row.get("evidence_kind") != t.EVIDENCE_CMS_FIELD:
            continue
        if row.get("status") == t.STATUS_RETRACTED:
            continue
        key = (row.get("document_id"), row.get("source_field"))
        # Only judge a (document, field) the current pass actually looked at;
        # a field absent from this run's scope says nothing about its claims.
        if key not in covered:
            continue
        if row["claim_id"] not in fresh_ids:
            stale.append(row["claim_id"])
    return stale
