"""The tests an author name must survive to hold AUTHORED.

Reuses `pi_promotion`'s thresholds rather than inventing new ones, because the
failure mode is identical: a name that denotes more than one human. The rules
here are the discriminators; the *grant* they earn is narrow, and
`test_author_attested_eligibility` covers that half.
"""
from __future__ import annotations

import pytest

from app.knowledge import author_promotion as ap
from app.knowledge import pi_promotion as pi


def _evidence(normalized="alice smith", surface="Dr Alice Smith", *,
              documents=3, years=(2019, 2020), divisions=("Energy",),
              fields=("field_authors",)):
    e = ap.AuthorEvidence(normalized=normalized, surface=surface)
    e.document_ids = {f"doc-{i}" for i in range(documents)}
    e.years = set(years)
    e.divisions = set(divisions)
    e.fields_seen = set(fields)
    return e


def _decide(entry, surnames=None, ambiguous=frozenset()):
    return ap.decide(entry, surnames=surnames or {}, ambiguous=set(ambiguous))


# --------------------------------------------------------------------------- #
# Thresholds are shared, not re-invented
# --------------------------------------------------------------------------- #

def test_the_thresholds_come_from_the_pi_pattern():
    """Imported rather than copied, so the two cannot drift apart."""
    assert ap.MIN_NAME_TOKENS is pi.MIN_NAME_TOKENS
    assert ap.MAX_SHARED_SURNAME is pi.MAX_SHARED_SURNAME
    assert ap.MAX_CAREER_YEARS is pi.MAX_CAREER_YEARS
    assert ap.MAX_DIVISION_AREAS is pi.MAX_DIVISION_AREAS


def test_the_author_fields_are_the_authoritative_ones():
    from app.knowledge.claims.extract_cms import _AUTHOR_FIELDS

    assert set(ap.AUTHOR_FIELDS) == set(_AUTHOR_FIELDS)


# --------------------------------------------------------------------------- #
# A clean name passes
# --------------------------------------------------------------------------- #

def test_a_clean_name_is_promoted():
    d = _decide(_evidence())
    assert d.promote is True
    assert "author of 3 document(s)" in d.reason
    assert d.evidence["documents"] == 3


def test_the_decision_carries_auditable_evidence():
    d = _decide(_evidence(documents=5, years=(2018, 2021), divisions=("Energy",)))
    assert d.evidence == {"documents": 5, "publishing_years": 3,
                          "divisions": ["Energy"], "fields": ["field_authors"]}


def test_a_single_year_span_is_zero_not_an_error():
    assert _evidence(years=(2020,)).publishing_years == 0


# --------------------------------------------------------------------------- #
# Each discriminating rule
# --------------------------------------------------------------------------- #

def test_initials_only_is_refused():
    d = _decide(_evidence(normalized="a k", surface="A K"))
    assert not d.promote and d.reason == "initials only"


def test_a_single_token_name_is_refused():
    """"Dr Neha" is a real author value in this corpus and identifies nobody."""
    d = _decide(_evidence(normalized="neha", surface="Dr Neha"))
    assert not d.promote and "fewer than 2 name tokens" in d.reason


def test_an_ambiguous_name_is_refused():
    """The strongest refusal: the resolver saw the collision in the corpus."""
    d = _decide(_evidence(), ambiguous={"alice smith"})
    assert not d.promote and d.reason == "name is marked ambiguous"


def test_a_crowded_surname_is_refused_for_a_two_token_name():
    d = _decide(_evidence(normalized="arun kumar", surface="Arun Kumar"),
                surnames={"kumar": 25})
    assert not d.promote and "surname shared with 24 other people" in d.reason


def test_a_crowded_surname_is_tolerated_with_three_tokens():
    """A middle name is additional discrimination, so the guard is aimed only at
    the bare two-token shape."""
    d = _decide(_evidence(normalized="arun p kumar", surface="Arun P Kumar"),
                surnames={"kumar": 25})
    assert d.promote is True


def test_an_implausible_publishing_span_is_refused():
    d = _decide(_evidence(years=(1970, 2025)))
    assert not d.promote and "publishing span of 55 years" in d.reason


def test_several_division_areas_are_refused():
    d = _decide(_evidence(divisions=("Energy", "Water", "Transport")))
    assert not d.promote and "spans 3 division areas" in d.reason


def test_no_division_recorded_is_not_a_refusal():
    """Absence of a signal is not evidence against the name."""
    assert _decide(_evidence(divisions=())).promote is True


def test_the_first_failure_is_the_reason_recorded():
    """Ordered cheapest-and-most-decisive first, so the reason is actionable."""
    d = _decide(_evidence(normalized="a k", surface="A K",
                          divisions=("A", "B", "C")),
                ambiguous={"a k"})
    assert d.reason == "initials only"


# --------------------------------------------------------------------------- #
# The promotion never widens anything
# --------------------------------------------------------------------------- #

def test_apply_does_not_set_claim_eligible():
    """The safety property of the whole change. `claim_eligible` means
    "eligible for anything"; setting it here would grant WORKS_AT, MEMBER_OF,
    HAS_ROLE and every LLM-proposed predicate."""
    import inspect

    source = inspect.getsource(ap.apply_promotions)
    statement = source[source.index("UPDATE"):source.index("WHERE")]
    assert "claim_eligible" not in statement, (
        "the promotion must set trust only, never the broad eligibility flag"
    )


def test_apply_only_touches_provisional_people():
    import inspect

    source = inspect.getsource(ap.apply_promotions)
    assert "trust='provisional'" in source
    assert "entity_type='PERSON'" in source


def test_apply_is_a_no_op_without_promotions():
    assert ap.apply_promotions([]) == 0
    refused = [ap.PromotionDecision("x y", "X Y", False, "initials only", {})]
    assert ap.apply_promotions(refused) == 0


def test_the_trust_level_is_the_scoped_one():
    from app.knowledge.seed import TRUST_AUTHOR_ATTESTED, CLAIM_ELIGIBLE_TRUST
    import inspect

    assert TRUST_AUTHOR_ATTESTED in inspect.getsource(ap.apply_promotions)
    assert TRUST_AUTHOR_ATTESTED not in CLAIM_ELIGIBLE_TRUST


# --------------------------------------------------------------------------- #
# Evidence gathering
# --------------------------------------------------------------------------- #

def test_nested_drupal_values_are_flattened():
    assert ap._values([{"value": "Dr A"}, "Mr B", {"target_id": "Ms C"}]) == [
        "Dr A", "Mr B", "Ms C"]


def test_empty_and_malformed_values_are_dropped():
    assert ap._values(None) == []
    assert ap._values([{"nothing": "x"}, "", "   "]) == []


def test_affected_documents_covers_only_promoted_names(monkeypatch):
    entry = _evidence(documents=2)
    monkeypatch.setattr(ap, "collect_author_evidence",
                        lambda: {"alice smith": entry})
    promoted = [ap.PromotionDecision("alice smith", "Dr Alice Smith", True, "ok", {})]
    refused = [ap.PromotionDecision("alice smith", "Dr Alice Smith", False, "no", {})]
    assert ap.affected_documents(promoted) == entry.document_ids
    assert ap.affected_documents(refused) == set()
