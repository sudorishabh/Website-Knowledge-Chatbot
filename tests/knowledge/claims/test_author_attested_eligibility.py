"""`author_attested`: one predicate, and only one.

The problem this solves is the one `pi_promotion` already solved for LED_BY.
The CMS states outright who wrote a document, but the person it names is usually
a *provisional* identity — a name, not a distinguished person — so every AUTHORED
claim was rejected as `subject_not_claim_eligible`.

The answer is not to weaken PERSON eligibility. It is a separate trust level that
grants exactly `AUTHORED`, because "the CMS says X wrote this" needs no identity
beyond the name, while "X works at Y" asserts something about the person and
still does.
"""
from __future__ import annotations

import pytest

from app.knowledge import seed
from app.knowledge.claims.eligibility import is_eligible_in_store

AUTHOR = "person-author-attested"
PROVISIONAL = "person-provisional"
PI = "person-pi"
ORG = "org-1"


class _Index:
    def __init__(self, entities):
        self.entities = entities


def _index():
    return _Index({
        AUTHOR: {"entity_type": "PERSON", "trust": seed.TRUST_AUTHOR_ATTESTED,
                 "claim_eligible": 0, "canonical_name": "Dr Alice Smith"},
        PROVISIONAL: {"entity_type": "PERSON", "trust": seed.TRUST_PROVISIONAL,
                      "claim_eligible": 0, "canonical_name": "Mr Bob Jones"},
        PI: {"entity_type": "PERSON", "trust": seed.TRUST_PI_ATTESTED,
             "claim_eligible": 1, "canonical_name": "Dr Carol White"},
        ORG: {"entity_type": "ORGANIZATION", "trust": seed.TRUST_DERIVED,
              "claim_eligible": 1, "canonical_name": "TERI"},
    })


# --------------------------------------------------------------------------- #
# The trust vocabulary
# --------------------------------------------------------------------------- #

def test_author_attested_is_not_broadly_claim_eligible():
    """The whole safety property. Adding it to CLAIM_ELIGIBLE_TRUST would make
    these people eligible for every predicate, which is the change this design
    exists to avoid."""
    assert seed.TRUST_AUTHOR_ATTESTED not in seed.CLAIM_ELIGIBLE_TRUST
    assert seed.is_claim_eligible(seed.TRUST_AUTHOR_ATTESTED) is False


def test_the_broad_levels_are_unchanged():
    assert seed.CLAIM_ELIGIBLE_TRUST == frozenset({
        seed.TRUST_AUTHORITATIVE, seed.TRUST_DERIVED, seed.TRUST_PI_ATTESTED})


def test_a_level_is_either_broad_or_scoped_never_both():
    """They are separate tables precisely so they cannot drift into agreeing."""
    assert not (set(seed.PREDICATE_SCOPED_TRUST) & seed.CLAIM_ELIGIBLE_TRUST)


def test_author_attested_grants_authored_and_nothing_else():
    assert seed.PREDICATE_SCOPED_TRUST[seed.TRUST_AUTHOR_ATTESTED] == frozenset(
        {"AUTHORED"})
    assert seed.is_claim_eligible_for(seed.TRUST_AUTHOR_ATTESTED, "AUTHORED")
    for predicate in ("WORKS_AT", "MEMBER_OF", "HAS_ROLE", "FUNDED_BY",
                      "LED_BY", "PARTNER_OF", "PARENT_OF"):
        assert not seed.is_claim_eligible_for(
            seed.TRUST_AUTHOR_ATTESTED, predicate), predicate


def test_an_unnamed_predicate_gets_the_broad_answer_only():
    """A caller that does not know its predicate must not be granted a scoped
    one by accident — the scoped level has to be asked about explicitly."""
    assert seed.is_claim_eligible_for(seed.TRUST_AUTHOR_ATTESTED, None) is False


def test_provisional_gains_nothing():
    for predicate in ("AUTHORED", "WORKS_AT", None):
        assert not seed.is_claim_eligible_for(seed.TRUST_PROVISIONAL, predicate)


def test_the_broad_levels_still_answer_for_every_predicate():
    for trust in (seed.TRUST_AUTHORITATIVE, seed.TRUST_DERIVED,
                  seed.TRUST_PI_ATTESTED):
        for predicate in ("AUTHORED", "WORKS_AT", None):
            assert seed.is_claim_eligible_for(trust, predicate), (trust, predicate)


# --------------------------------------------------------------------------- #
# The store check
# --------------------------------------------------------------------------- #

def test_the_store_admits_an_author_attested_person_for_authored():
    assert is_eligible_in_store(AUTHOR, _index(), "AUTHORED") is True


@pytest.mark.parametrize("predicate", ["WORKS_AT", "MEMBER_OF", "HAS_ROLE",
                                       "FUNDED_BY", "LED_BY", None])
def test_the_store_refuses_them_for_everything_else(predicate):
    assert is_eligible_in_store(AUTHOR, _index(), predicate) is False


def test_a_provisional_person_is_still_refused_for_authored():
    """The promotion is per-identity. Being named in an author field is not
    itself enough — see author_promotion for the tests a name must survive."""
    assert is_eligible_in_store(PROVISIONAL, _index(), "AUTHORED") is False


def test_an_already_eligible_person_is_unaffected():
    for predicate in ("AUTHORED", "WORKS_AT", None):
        assert is_eligible_in_store(PI, _index(), predicate) is True


def test_an_unknown_entity_is_refused():
    assert is_eligible_in_store("nobody", _index(), "AUTHORED") is False


def test_the_default_signature_is_unchanged_for_existing_callers():
    """`predicate` is optional, so every existing call site keeps its meaning."""
    assert is_eligible_in_store(PI, _index()) is True
    assert is_eligible_in_store(AUTHOR, _index()) is False


# --------------------------------------------------------------------------- #
# Validation wiring
# --------------------------------------------------------------------------- #

def _claim(predicate, subject=AUTHOR, **kw):
    from app.knowledge.claims import types as t

    fields = dict(
        subject_entity_id=subject, predicate=predicate, document_id="doc-1",
        evidence_kind=t.EVIDENCE_CMS_FIELD, source_field="field_authors",
        source_value="Dr Alice Smith", confidence=1.0,
        extraction_method="cms_field", extractor_version="test",
    )
    fields.update(kw)
    return t.build(**fields)


def _verdict(claim):
    """(accepted?, rejection code) for one claim through the real validator."""
    from app.knowledge.claims.validate import validate

    result = validate([claim], index=_index(), chunk_texts={},
                      min_confidence=0.0)
    if result.accepted:
        return True, None
    return False, result.rejected[0].code


def test_an_author_attested_person_may_carry_authored():
    """End to end through the real validator, not by reading the source."""
    ok, code = _verdict(_claim("AUTHORED", object_literal="doc-1"))
    assert ok, f"rejected as {code}"


@pytest.mark.parametrize("predicate,obj", [
    ("WORKS_AT", {"object_entity_id": ORG}),
    ("MEMBER_OF", {"object_entity_id": ORG}),
    ("HAS_ROLE", {"object_literal": "Director"}),
])
def test_an_author_attested_person_may_carry_nothing_else(predicate, obj):
    """The safety property, proved through validation rather than asserted."""
    ok, code = _verdict(_claim(predicate, **obj))
    assert not ok
    assert code == "subject_not_claim_eligible", code


def test_a_provisional_person_is_still_refused_for_authored_end_to_end():
    ok, code = _verdict(_claim("AUTHORED", subject=PROVISIONAL,
                               object_literal="doc-1"))
    assert not ok
    assert code == "subject_not_claim_eligible", code


def test_an_author_attested_person_may_not_be_the_object_of_a_claim():
    """The scope is about authorship, not about being pointed at. An object must
    still clear broad eligibility."""
    ok, code = _verdict(_claim("WORKS_AT", subject=PI,
                               object_entity_id=AUTHOR))
    assert not ok
    assert code in ("object_not_claim_eligible", "type_violation"), code
