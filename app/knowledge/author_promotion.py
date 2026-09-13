"""Promoting CMS author names to an authorship-only claim identity.

The problem this solves
-----------------------
The CMS states outright who wrote a document, in six authoritative author
fields. The person it names is almost always a *provisional* PERSON — a name the
seeder created from a facet, not an identity the corpus has distinguished — so
every ``AUTHORED`` claim was refused as ``subject_not_claim_eligible``. Measured
before this existed: 2,761 AUTHORED claims built, 802 staged, **1,959 rejected**
for that one reason.

Why this is not "weaken the PERSON rule"
----------------------------------------
The provisional level exists for exactly this population, and for a good reason
stated in :mod:`app.knowledge.seed`: *two different people called "Arun Kumar"
are one row, so the row is a name, not a person*. So the risk in an AUTHORED edge
is **not** weak provenance — Drupal is authoritative that someone of that name
wrote the document — but identity conflation.

That is why the grant is scoped rather than broad. "The CMS says X wrote this"
needs no identity beyond the name; "X works at Y" asserts something *about the
person* and still requires one. :data:`app.knowledge.seed.PREDICATE_SCOPED_TRUST`
carries that distinction, and this module decides who may hold it.

The tests a name must survive
-----------------------------
The same six that :mod:`app.knowledge.pi_promotion` applies, reused rather than
reinvented, because they target the same failure mode — a name that denotes more
than one human. Thresholds are imported from that module so the two cannot drift.

Measured on the corpus this was designed against: of 443 distinct author names
that resolve, 200 passed and 243 failed — 118 already marked ambiguous, 88
spanning several division areas, 37 on a crowded surname. That is the point:
**most rejections are correct**, and this recovers the minority that are not.

What this does NOT do
---------------------
It does not set ``claim_eligible``. That flag means "eligible for anything", and
setting it here would grant WORKS_AT, MEMBER_OF, HAS_ROLE and every LLM-proposed
predicate — the exact widening this design exists to avoid. Eligibility for
AUTHORED comes from the *trust level* being consulted per predicate.

    python -m scripts.promote_authors             # dry run, prints the decisions
    python -m scripts.promote_authors --apply
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.knowledge.normalize import is_initials_only, normalize_person
from app.knowledge.pi_promotion import (
    MAX_CAREER_YEARS,
    MAX_DIVISION_AREAS,
    MAX_SHARED_SURNAME,
    MIN_NAME_TOKENS,
    surname_frequency,
)
from app.knowledge.seed import TRUST_AUTHOR_ATTESTED

logger = logging.getLogger(__name__)

PROMOTION_VERSION = "author-promotion-v1"

#: The authoritative author fields, mirroring
#: :data:`app.knowledge.claims.extract_cms._AUTHOR_FIELDS`. Named here rather
#: than imported so this module states independently what it treats as
#: authoritative: a field added to extraction should be a deliberate decision
#: here too, not an inherited one.
AUTHOR_FIELDS = (
    "field_authors",
    "field_rpaper_author",
    "field_article_authors",
    "field_policybrief_authors",
    "field_external_authors",
    "field_author",
)

#: Where a document records the division it came out of. The coherence signal:
#: one person's writing tends to sit in one division area, and a name whose
#: documents scatter across several may be covering more than one person.
DIVISION_FIELDS = (
    "field_division",
    "field_farticles_division_area",
    "field_article_division",
    "field_policybrief_division_area",
    "field_division_area",
    "field_completed_division_area",
    "field_ongoing_division_area",
)


@dataclass
class AuthorEvidence:
    """Everything the CMS says about one author name, gathered across documents."""

    normalized: str
    surface: str
    document_ids: set[str] = field(default_factory=set)
    years: set[int] = field(default_factory=set)
    divisions: set[str] = field(default_factory=set)
    fields_seen: set[str] = field(default_factory=set)

    @property
    def publishing_years(self) -> int:
        """Span between the first and last document, or 0 for a single year."""
        return (max(self.years) - min(self.years)) if len(self.years) > 1 else 0

    def as_audit(self) -> dict[str, Any]:
        return {
            "documents": len(self.document_ids),
            "publishing_years": self.publishing_years,
            "divisions": sorted(self.divisions)[:6],
            "fields": sorted(self.fields_seen),
        }


@dataclass
class PromotionDecision:
    normalized: str
    surface: str
    promote: bool
    reason: str
    evidence: dict[str, Any]


def _values(raw: Any) -> list[str]:
    """Scalar strings out of Drupal's nested value shapes."""
    out: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                out.append(text)
        elif isinstance(value, dict):
            for key in ("value", "target_id", "title", "name"):
                if key in value:
                    walk(value[key])
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(raw)
    return out


def collect_author_evidence() -> dict[str, AuthorEvidence]:
    """Gather, per normalized author name, what the CMS records across documents."""
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT document_id, raw_meta, effective_start_date FROM `{table}` "
            "WHERE raw_meta IS NOT NULL"
        )
        rows = cur.fetchall()

    evidence: dict[str, AuthorEvidence] = {}
    for row in rows:
        raw = row["raw_meta"]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue

        divisions = {v for f in DIVISION_FIELDS for v in _values(meta.get(f))}
        started = row["effective_start_date"]
        year = int(str(started)[:4]) if started and str(started)[:4].isdigit() else None

        for author_field in AUTHOR_FIELDS:
            for surface in _values(meta.get(author_field)):
                normalized = normalize_person(surface)
                if not normalized:
                    continue
                entry = evidence.setdefault(
                    normalized,
                    AuthorEvidence(normalized=normalized, surface=surface),
                )
                entry.document_ids.add(row["document_id"])
                entry.fields_seen.add(author_field)
                if year is not None:
                    entry.years.add(year)
                entry.divisions.update(divisions)
    return evidence


def decide(
    entry: AuthorEvidence, *, surnames: dict[str, int], ambiguous: set[str],
) -> PromotionDecision:
    """Whether this author name may hold AUTHORED. Every test must pass.

    Ordered cheapest and most decisive first, so the reason recorded is the
    *first* thing wrong rather than an arbitrary one.
    """
    def refuse(reason: str) -> PromotionDecision:
        return PromotionDecision(
            entry.normalized, entry.surface, False, reason, entry.as_audit()
        )

    tokens = entry.normalized.split()

    # A name that identifies nobody cannot be made to identify someone.
    if is_initials_only(entry.normalized):
        return refuse("initials only")
    if len(tokens) < MIN_NAME_TOKENS:
        return refuse(f"fewer than {MIN_NAME_TOKENS} name tokens")

    # Already known to denote more than one thing. The strongest refusal here:
    # the resolver saw the collision in the corpus itself.
    if entry.normalized in ambiguous:
        return refuse("name is marked ambiguous")

    # The "Arun Kumar" guard: a two-token name on a crowded surname is a poor
    # identity however good the rest of the evidence is.
    shared = surnames.get(tokens[-1], 0) - 1
    if len(tokens) == MIN_NAME_TOKENS and shared >= MAX_SHARED_SURNAME:
        return refuse(f"surname shared with {shared} other people")

    # Contextual coherence — the two signals that would betray a conflated name.
    if entry.publishing_years > MAX_CAREER_YEARS:
        return refuse(f"publishing span of {entry.publishing_years} years")
    if len(entry.divisions) > MAX_DIVISION_AREAS:
        return refuse(f"spans {len(entry.divisions)} division areas")

    return PromotionDecision(
        entry.normalized, entry.surface, True,
        f"author of {len(entry.document_ids)} document(s), "
        f"span {entry.publishing_years}y, {len(entry.divisions)} division(s)",
        entry.as_audit(),
    )


def evaluate_promotions() -> list[PromotionDecision]:
    """Decide every author name against the current entity store. No writes."""
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT normalized_name, trust FROM `{table}_entity` "
            "WHERE entity_type='PERSON'"
        )
        people = {r["normalized_name"]: r["trust"] for r in cur.fetchall()}
        cur.execute(
            f"SELECT DISTINCT normalized FROM `{table}_entity_alias` "
            "WHERE is_ambiguous = 1"
        )
        ambiguous = {r["normalized"] for r in cur.fetchall()}

    surnames = surname_frequency(list(people))
    decisions: list[PromotionDecision] = []
    for normalized, entry in sorted(collect_author_evidence().items()):
        if normalized not in people:
            # An author the seeder never created an entity for. Unresolvable
            # rather than provisional, and nothing here may mint one.
            continue
        if people[normalized] != "provisional":
            # Already carries a stronger identity; promotion would be a
            # downgrade or a no-op.
            continue
        decisions.append(decide(entry, surnames=surnames, ambiguous=ambiguous))
    return decisions


def apply_promotions(decisions: list[PromotionDecision]) -> int:
    """Raise promoted names to ``author_attested``. Returns rows changed.

    ``claim_eligible`` is deliberately **not** set. That flag means "eligible for
    anything", and setting it would grant WORKS_AT, MEMBER_OF, HAS_ROLE and every
    LLM-proposed predicate. Eligibility for AUTHORED comes from the trust level
    being consulted per predicate — see
    :func:`app.knowledge.claims.eligibility.is_eligible_in_store`.

    Only ever promotes a **provisional** person, so a stronger identity is left
    alone and nothing is ever demoted.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    promote = [d.normalized for d in decisions if d.promote]
    if not promote:
        return 0
    table = state_table()
    changed = 0
    with mysql_connection() as conn, conn.cursor() as cur:
        for start in range(0, len(promote), 500):
            batch = promote[start : start + 500]
            placeholders = ", ".join(["%s"] * len(batch))
            cur.execute(
                f"UPDATE `{table}_entity` SET trust=%s "
                f"WHERE entity_type='PERSON' AND trust='provisional' "
                f"AND normalized_name IN ({placeholders})",
                [TRUST_AUTHOR_ATTESTED, *batch],
            )
            changed += cur.rowcount
        conn.commit()
    logger.info("Promoted %d author names to %s.", changed, TRUST_AUTHOR_ATTESTED)
    return changed


def affected_documents(decisions: list[PromotionDecision]) -> set[str]:
    """Documents authored by a promoted name — the targeted re-run scope."""
    promoted = {d.normalized for d in decisions if d.promote}
    if not promoted:
        return set()
    return {
        document_id
        for normalized, entry in collect_author_evidence().items()
        if normalized in promoted
        for document_id in entry.document_ids
    }
