"""Which entities a claim may reference at all.

This is the gate Phase 5.1 exists to feed. The rule is narrow and absolute:

    a claim may reference an entity only where a resolution decision said the
    identity is **canonical**, and the entity store still agrees it is
    claim-eligible.

Both halves matter, and they are checked in different places for a reason.

**The decision, not the id.** An ``entity_id`` on its own says nothing about
whether that identity was established — a provisional row has one too. So
extraction is handed ``Decision`` objects and reads ``decision.canonical``;
there is no path here that accepts a bare id. Requirement: *never treat a raw
entity_id as sufficient evidence for claim eligibility.*

**The store, again, at validation time.** A decision can be older than the
entity's current trust level. Validation therefore re-checks
``claim_eligible`` against the store before anything is staged, so a
demotion takes effect without having to find and rewrite old decisions.

For PERSON in this corpus that means claims are limited to the 8 authoritative
people. The 915 provisional person rows are names, not people, and a claim about
a name would assert something the corpus never established.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EligibleEntity:
    """An entity a claim may reference, as offered to the extractor.

    Deliberately the *only* thing an extractor sees. A model choosing a subject
    picks from these, so it cannot name an entity that was never resolved, and
    cannot reach a provisional identity even if it knows the name.
    """

    entity_id: str
    entity_type: str
    canonical_name: str
    # The surface that actually appeared in this chunk, so the extractor can
    # tie the name in front of it to the identity behind it.
    surface: str

    def as_prompt_row(self) -> dict[str, str]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "name": self.canonical_name,
            "as_written": self.surface,
        }


def eligible_from_decisions(
    decisions: Iterable[Any], index: Any | None = None
) -> list[EligibleEntity]:
    """The claim-eligible entities among a chunk's resolution decisions.

    Filters on ``decision.canonical`` — which is ``AUTO`` and nothing else, so
    ``PROVISIONAL``, ``AMBIGUOUS`` and ``UNRESOLVED`` are all excluded — and
    then on the decision's own ``claim_eligible`` flag. Deduplicated by
    entity id, keeping the first surface seen, so a name repeated in a chunk
    offers one identity rather than three.
    """
    seen: dict[str, EligibleEntity] = {}
    for decision in decisions:
        if not getattr(decision, "canonical", False):
            continue
        if not getattr(decision, "claim_eligible", False):
            continue
        entity_id = decision.entity_id
        if entity_id in seen:
            continue
        canonical_name = decision.surface_text
        if index is not None:
            row = index.entities.get(entity_id)
            if row is not None:
                canonical_name = row["canonical_name"]
        seen[entity_id] = EligibleEntity(
            entity_id=entity_id,
            entity_type=decision.entity_type,
            canonical_name=canonical_name,
            surface=decision.surface_text,
        )
    return list(seen.values())


def is_eligible_in_store(
    entity_id: str, index: Any, predicate: str | None = None
) -> bool:
    """Whether the store *currently* says this entity may carry this claim.

    Re-checked at validation time rather than trusted from the decision, so a
    demotion takes effect immediately instead of waiting for every old decision
    to be rewritten.

    ``predicate`` admits the predicate-scoped trust levels -- today only
    ``author_attested``, which grants ``AUTHORED`` and nothing else. It is
    optional and defaults to None, which asks the broad question alone: a caller
    that does not name a predicate cannot accidentally be granted a scoped one.

    The stored ``claim_eligible`` flag remains the fast path and still means
    "eligible for anything". Only when it is false does the trust level get a
    second, narrower question.
    """
    row = index.entities.get(entity_id)
    if row is None:
        return False
    if bool(row.get("claim_eligible", 0)):
        return True
    if predicate is None:
        return False
    from app.knowledge.seed import is_claim_eligible_for

    return is_claim_eligible_for(str(row.get("trust") or ""), predicate)


def entity_type_of(entity_id: str, index: Any) -> str | None:
    row = index.entities.get(entity_id)
    return row["entity_type"] if row else None


# The eligibility gate for a whole chunk: below this many eligible entities
# there is nothing a relational claim could join, so the expensive extractor is
# never called. A literal-valued predicate needs only one.
MIN_ENTITIES_FOR_RELATIONAL = 2
MIN_ENTITIES_FOR_LITERAL = 1


def chunk_is_extractable(eligible: Sequence[EligibleEntity]) -> bool:
    """Whether a chunk is worth an extraction attempt at all."""
    return len(eligible) >= MIN_ENTITIES_FOR_LITERAL
