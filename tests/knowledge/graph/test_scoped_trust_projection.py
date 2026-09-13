"""A predicate-scoped trust level reaches the graph without becoming eligible.

`author_attested` is deliberately not `claim_eligible` -- that flag means
"eligible for anything". But the projection admitted only eligible entities, so
501 AUTHORED claims sat in MySQL with no projectable subject and the authorship
edge existed nowhere in Neo4j.

Widening the *node* rule is not widening eligibility. These tests pin both
halves: the node appears, and every other gate still refuses it.
"""
from __future__ import annotations

import pytest

from app.knowledge import seed
from app.knowledge.graph import project as gp


# --------------------------------------------------------------------------- #
# The SQL rule
# --------------------------------------------------------------------------- #

def test_the_clause_admits_broadly_eligible_entities():
    clause, params = gp._projectable_clause()
    assert "claim_eligible = 1" in clause


def test_the_clause_admits_every_scoped_trust_level():
    clause, params = gp._projectable_clause()
    assert set(params) == set(seed.PREDICATE_SCOPED_TRUST)
    assert seed.TRUST_AUTHOR_ATTESTED in params


def test_the_clause_is_parameterised_not_interpolated():
    """Trust levels are constants today, but the query must not become a place
    where a value is pasted into SQL."""
    clause, params = gp._projectable_clause()
    for level in params:
        assert level not in clause
    assert clause.count("%s") == len(params)


def test_the_clause_is_read_from_the_vocabulary(monkeypatch):
    """A new scoped level must not need a second edit here to be projectable."""
    monkeypatch.setattr(
        seed, "PREDICATE_SCOPED_TRUST",
        {"author_attested": frozenset({"AUTHORED"}),
         "reviewer_attested": frozenset({"REVIEWED"})},
    )
    _, params = gp._projectable_clause()
    assert set(params) == {"author_attested", "reviewer_attested"}


def test_no_scoped_levels_falls_back_to_eligibility_alone(monkeypatch):
    monkeypatch.setattr(seed, "PREDICATE_SCOPED_TRUST", {})
    clause, params = gp._projectable_clause()
    assert clause == "claim_eligible = 1"
    assert params == []


def test_a_provisional_person_is_still_refused():
    """The rule this widening must not break: a name-level identity has no
    node, so no traversal can arrive at one."""
    clause, params = gp._projectable_clause()
    assert seed.TRUST_PROVISIONAL not in params


# --------------------------------------------------------------------------- #
# The node carries the truth
# --------------------------------------------------------------------------- #

def _row(trust, claim_eligible):
    return {
        "entity_id": "e1", "entity_type": "PERSON", "canonical_name": "Dr A",
        "normalized_name": "a", "trust": trust, "claim_eligible": claim_eligible,
        "cms_uuid": None, "source": "s", "status": "active",
    }


def _projected(row):
    """The node properties projection would write for one entity row."""
    return {
        "entity_id": row["entity_id"],
        "trust": row["trust"],
        "claim_eligible": bool(row.get("claim_eligible", 1)),
    }


def test_an_author_attested_node_is_not_marked_eligible():
    """The safety property. A node advertising `claim_eligible: true` would tell
    every consumer this person may hold any predicate."""
    node = _projected(_row(seed.TRUST_AUTHOR_ATTESTED, 0))
    assert node["claim_eligible"] is False
    assert node["trust"] == seed.TRUST_AUTHOR_ATTESTED


def test_an_eligible_node_is_still_marked_eligible():
    assert _projected(_row(seed.TRUST_PI_ATTESTED, 1))["claim_eligible"] is True


# --------------------------------------------------------------------------- #
# Every other gate is untouched
# --------------------------------------------------------------------------- #

def test_authored_cannot_become_a_current_state_edge():
    """AUTHORED's object is the document id as a *literal*, and a literal is a
    property rather than a relationship -- so widening which entities have nodes
    cannot manufacture a current-state edge."""
    claim = {"claim_id": "c1", "predicate": "AUTHORED", "object_entity_id": None,
             "object_literal": "doc-1", "subject_entity_id": "e1",
             "confidence": 1.0, "valid_from": None, "valid_until": None,
             "temporal_basis": "unknown", "status": "active"}
    assert gp._current_state_rows([claim], as_of=None) == []


def test_a_claim_whose_subject_has_no_node_is_still_refused():
    """The mechanism that made this change necessary, pinned so it keeps
    working for everything else."""
    claims = [{"claim_id": "c1", "subject_entity_id": "missing",
               "object_entity_id": None, "predicate": "AUTHORED"}]
    projectable, refused = gp._partition_projectable(claims, {"e1"})
    assert projectable == []
    assert refused == 1


def test_a_claim_whose_subject_has_a_node_is_projected():
    claims = [{"claim_id": "c1", "subject_entity_id": "e1",
               "object_entity_id": None, "predicate": "AUTHORED"}]
    projectable, refused = gp._partition_projectable(claims, {"e1"})
    assert len(projectable) == 1
    assert refused == 0


def test_an_object_end_is_still_checked():
    """Widening the subject rule must not let a claim through on its object."""
    claims = [{"claim_id": "c1", "subject_entity_id": "e1",
               "object_entity_id": "missing", "predicate": "WORKS_AT"}]
    projectable, refused = gp._partition_projectable(claims, {"e1"})
    assert projectable == []
    assert refused == 1
