"""Validation: the gate every assertion passes before it is staged.

Deterministic and total. Nothing reaches storage without passing every check
here, and every rejection carries a reason, because "the model produced fewer
claims today" is only diagnosable if the reasons were recorded.

The checks exist in a deliberate order — cheapest and most fundamental first,
so a claim about a non-existent entity is rejected before anyone tries to verify
its quote.

Model output is treated as an untrusted input source throughout. In particular
**offsets a model reports are never used**: the application locates the quote
itself and recomputes them. A quote that cannot be found verbatim is dropped,
which is the property that stops a fabricated citation being storable.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

from app.knowledge.claims import predicates as vocab
from app.knowledge.claims import types as t
from app.knowledge.claims.eligibility import entity_type_of, is_eligible_in_store

logger = logging.getLogger(__name__)

_ISO_DATE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")

# Verbs that open a quote attribution. A literal-valued role beginning with one
# of these is what the subject *did* in a sentence, not what they *are*: the
# model read the predicate of "...says Dr X" or "X acknowledges it was a
# mistake" as the role itself. Measured on 79 staged HAS_ROLE claims, four were
# this and no real title began with any of these words — the titles this corpus
# states are noun phrases ("Senior Fellow, Water Resources Division").
#
# A curated list rather than a part-of-speech test, following
# `app.knowledge.gazetteer._PROSE_MARKERS`: the failure is a narrow, observed
# shape, and a list is auditable where a tagger is a dependency and a guess.
# Deliberately first-token-only, so "commentary author" and "member of the
# research team" — weak and generic, but genuinely descriptions of a role —
# still pass.
_ATTRIBUTION_VERBS = frozenset(
    """
    says said tells told adds added notes noted writes wrote states stated
    explains explained describes described acknowledges acknowledged argues
    argued claims claimed suggests suggested observes observed warns warned
    believes concludes concluded emphasises emphasised emphasizes emphasized
    points pointed provided provides recalls recalled asks asked replies
    replied responds responded reports reported highlights highlighted
    """.split()
)


@dataclass(frozen=True)
class Rejection:
    """Why one assertion did not make it."""

    code: str
    detail: str
    assertion: Any = None


@dataclass
class ValidationResult:
    accepted: list[Any]
    rejected: list[Rejection]

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rejection in self.rejected:
            out[rejection.code] = out.get(rejection.code, 0) + 1
        return out


def parse_iso_date(value: str | None) -> str | None:
    """Normalize a date to ``YYYY-MM-DD``, or None when unusable.

    Accepts a bare year or year-month, which is how documents actually state
    validity ("since 2019"), and pins the missing parts to the start of the
    period rather than guessing a day.
    """
    if not value:
        return None
    match = _ISO_DATE.match(str(value).strip())
    if not match:
        return None
    year = int(match.group(1))
    if not (t.MIN_YEAR <= year <= t.MAX_YEAR):
        return None
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def locate_quote(chunk_text: str, quote: str) -> tuple[int, int] | None:
    """Find a quote in the chunk and return its real offsets.

    Tries the quote as given, then with internal whitespace treated as flexible
    — a PDF line-wraps mid-sentence, so a model quoting a passage faithfully can
    still differ from the stored text by a newline. Anything not found this way
    is not in the chunk, and the assertion is dropped.
    """
    if not quote or not chunk_text:
        return None
    index = chunk_text.find(quote)
    if index >= 0:
        return (index, index + len(quote))
    pattern = re.compile(r"\s+".join(re.escape(part) for part in quote.split()))
    match = pattern.search(chunk_text)
    return (match.start(), match.end()) if match else None


def _validate_one(
    assertion: Any, *, index: Any, chunk_texts: dict[str, str],
    min_confidence: float,
) -> Rejection | None:
    """Return a Rejection, or None when the assertion is acceptable.

    Mutates the assertion only to *correct* it — recomputed offsets, normalized
    dates — never to fill in something the source did not say.
    """
    # --- predicate -----------------------------------------------------------
    predicate = vocab.get(assertion.predicate)
    if predicate is None:
        return Rejection("unknown_predicate", assertion.predicate, assertion)

    # --- subject: exists, and the store still says it may carry claims -------
    subject_type = entity_type_of(assertion.subject_entity_id, index)
    if subject_type is None:
        return Rejection("unknown_subject", assertion.subject_entity_id, assertion)
    # The predicate is passed so a predicate-scoped trust level can answer for
    # itself. An author-attested person clears this for AUTHORED and for nothing
    # else; every other predicate asks the same question it always did.
    if not is_eligible_in_store(
        assertion.subject_entity_id, index, assertion.predicate
    ):
        # A provisional identity. This is the Phase 5.1 guarantee reaching the
        # claim layer: a name-level person may never be a claim subject.
        return Rejection(
            "subject_not_claim_eligible", assertion.subject_entity_id, assertion
        )

    # --- object --------------------------------------------------------------
    object_type: str | None = None
    if predicate.entity_valued:
        if not assertion.object_entity_id:
            return Rejection("missing_object_entity", predicate.name, assertion)
        object_type = entity_type_of(assertion.object_entity_id, index)
        if object_type is None:
            return Rejection(
                "unknown_object", assertion.object_entity_id, assertion
            )
        # No predicate here on purpose. The scoped grant is about being the
        # *author*, not about being something an author-attested person is
        # pointed at, so an object must still clear broad eligibility.
        if not is_eligible_in_store(assertion.object_entity_id, index):
            return Rejection(
                "object_not_claim_eligible", assertion.object_entity_id, assertion
            )
        # A TERI division (seeded from field_division, alongside genuine
        # external sponsors in the same organization pool -- see
        # app.knowledge.seed._ORG_FIELDS) is a valid employer or membership
        # target for a PERSON, but not a peer organizational actor: PARENT_OF,
        # PARTNER_OF and FUNDED_BY all assert a relationship *between*
        # organization-level entities, and a division is not independently one.
        # Piloted: this is exactly the confusion behind excluding PARENT_OF and
        # PARTNER_OF from LLM extraction in document_pipeline.py.
        if object_type == "ORGANIZATION" and "PERSON" not in predicate.domain:
            object_row = index.entities.get(assertion.object_entity_id) or {}
            if object_row.get("source") == "field_division":
                return Rejection(
                    "object_is_internal_division",
                    assertion.object_entity_id, assertion,
                )
        if assertion.object_literal:
            return Rejection("object_literal_on_entity_predicate", predicate.name, assertion)
    else:
        if assertion.object_entity_id:
            return Rejection("object_entity_on_literal_predicate", predicate.name, assertion)
        literal = (assertion.object_literal or "").strip()
        if not literal:
            return Rejection("missing_object_literal", predicate.name, assertion)
        if len(literal) > 255:
            return Rejection("object_literal_too_long", str(len(literal)), assertion)
        if literal.split()[0].lower() in _ATTRIBUTION_VERBS:
            # What the subject *did* in a sentence, not what they *are*. These
            # come from quote-attribution prose ("...says Dr X", "X acknowledges
            # it was a mistake"), where the model reads the verb as the role.
            return Rejection("object_literal_is_an_action", literal[:60], assertion)
        assertion.object_literal = literal

    # --- types compatible with the predicate ---------------------------------
    if not vocab.accepts(predicate.name, subject_type, object_type):
        return Rejection(
            "type_violation",
            f"{subject_type} -{predicate.name}-> {object_type or 'literal'}",
            assertion,
        )

    # --- a claim may not join a thing to itself ------------------------------
    if (
        assertion.object_entity_id
        and assertion.object_entity_id == assertion.subject_entity_id
    ):
        return Rejection("self_reference", assertion.subject_entity_id, assertion)

    # --- provenance ----------------------------------------------------------
    if not assertion.document_id:
        return Rejection("missing_document", "", assertion)
    if assertion.evidence_kind not in t.EVIDENCE_KINDS:
        return Rejection("unknown_evidence_kind", assertion.evidence_kind, assertion)

    if assertion.evidence_kind == t.EVIDENCE_CHUNK:
        if not assertion.chunk_id:
            return Rejection("missing_chunk", "", assertion)
        chunk_text = chunk_texts.get(assertion.chunk_id)
        if chunk_text is None:
            # The chunk must be one we actually hold. A claim pointing at a
            # chunk nobody can fetch is unverifiable by construction.
            return Rejection("chunk_not_found", assertion.chunk_id, assertion)
        quote = (assertion.quote or "").strip()
        if not quote:
            return Rejection("missing_quote", "", assertion)
        if not (t.MIN_QUOTE_CHARS <= len(quote) <= t.MAX_QUOTE_CHARS):
            return Rejection("quote_length", str(len(quote)), assertion)
        located = locate_quote(chunk_text, quote)
        if located is None:
            # Not present verbatim: the evidence does not exist.
            return Rejection("quote_not_in_chunk", quote[:60], assertion)
        # Offsets are computed here and overwrite whatever arrived. Model-
        # supplied offsets are never trusted.
        start, end = located
        assertion.quote_start, assertion.quote_end = start, end
        assertion.quote = chunk_text[start:end]
        if chunk_text[start:end] != assertion.quote:  # pragma: no cover
            return Rejection("offset_mismatch", assertion.chunk_id, assertion)
    else:
        if assertion.quote or assertion.chunk_id:
            # A CMS-field claim has no prose. Carrying a quote would imply an
            # evidence span that does not exist.
            return Rejection("cms_claim_with_quote", assertion.source_field or "", assertion)
        if not assertion.source_field:
            return Rejection("missing_source_field", "", assertion)

    # --- temporal ------------------------------------------------------------
    valid_from = parse_iso_date(assertion.valid_from)
    valid_until = parse_iso_date(assertion.valid_until)
    if assertion.valid_from and valid_from is None:
        return Rejection("bad_valid_from", str(assertion.valid_from), assertion)
    if assertion.valid_until and valid_until is None:
        return Rejection("bad_valid_until", str(assertion.valid_until), assertion)
    if valid_from and valid_until and valid_from > valid_until:
        return Rejection("inverted_validity", f"{valid_from}..{valid_until}", assertion)
    assertion.valid_from, assertion.valid_until = valid_from, valid_until
    if assertion.temporal_basis not in t.TEMPORAL_BASES:
        return Rejection("bad_temporal_basis", assertion.temporal_basis, assertion)
    if not (valid_from or valid_until) and assertion.temporal_basis != t.BASIS_UNKNOWN:
        # Claiming a basis for a window that does not exist.
        assertion.temporal_basis = t.BASIS_UNKNOWN

    # --- extraction metadata --------------------------------------------------
    if assertion.extraction_method not in t.EXTRACTION_METHODS:
        return Rejection("bad_extraction_method", assertion.extraction_method, assertion)
    if assertion.status not in t.STATUSES:
        return Rejection("bad_status", assertion.status, assertion)
    if not 0.0 <= assertion.confidence <= 1.0:
        return Rejection("confidence_out_of_range", str(assertion.confidence), assertion)
    if assertion.confidence < min_confidence:
        return Rejection("low_confidence", f"{assertion.confidence:.2f}", assertion)

    # Identity is recomputed last, from the corrected content, so a claim can
    # never be stored under an id that disagrees with what it says.
    assertion.recompute_id()
    return None


def validate(
    assertions: Iterable[Any], *, index: Any,
    chunk_texts: dict[str, str] | None = None, min_confidence: float = 0.0,
) -> ValidationResult:
    """Validate a batch. Accepted assertions are corrected in place."""
    chunk_texts = chunk_texts or {}
    accepted: list[Any] = []
    rejected: list[Rejection] = []
    for assertion in assertions:
        rejection = _validate_one(
            assertion, index=index, chunk_texts=chunk_texts,
            min_confidence=min_confidence,
        )
        if rejection is None:
            accepted.append(assertion)
        else:
            rejected.append(rejection)
    if rejected:
        logger.info("Rejected %d assertions: %s", len(rejected),
                    ValidationResult([], rejected).counts)
    return ValidationResult(accepted, rejected)


def dedupe(assertions: Sequence[Any]) -> list[Any]:
    """One assertion per claim_id, keeping the most confident.

    Two extractors reading the same sentence produce the same identity by
    design; this is where that becomes one row rather than a conflict.
    """
    best: dict[str, Any] = {}
    for assertion in assertions:
        current = best.get(assertion.claim_id)
        if current is None or assertion.confidence > current.confidence:
            best[assertion.claim_id] = assertion
    return sorted(best.values(), key=lambda a: a.claim_id)
