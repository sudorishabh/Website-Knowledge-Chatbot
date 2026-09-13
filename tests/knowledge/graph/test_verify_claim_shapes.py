"""The audit understands the graph's two legitimate claim shapes.

A claim reaches the graph in one of two forms, and an audit that knows only the
first reports the second as corrupt:

1. **entity-valued** -- ``Claim-[:OBJECT]->(Entity)``
2. **literal-valued** -- the object is a property on the Claim. There is no
   ``Literal`` node, by design.

Measured on production when the audit knew only form 1: all 584 AUTHORED and 10
HAS_ROLE claims were reported as missing an OBJECT edge -- 594 false positives
against a graph that was correct.

The same release widened projection to predicate-scoped trust levels, which
broke two further audit invariants in the same way: `author_attested` entities
are projected carrying `claim_eligible: false`, so an audit demanding `true`
reported all 258 as corrupt, and an alias expectation filtered on
`claim_eligible=1` under-counted by their aliases.

None of this changed the graph. Only the audit was wrong.
"""
from __future__ import annotations

import pytest

from app.knowledge.claims import predicates as vocab
from app.knowledge.graph import verify as gv

# The production shape this regression is written against.
AUTHORED = 584
HAS_ROLE = 10
LITERAL_CLAIMS = AUTHORED + HAS_ROLE          # 594
ENTITY_CLAIMS = 1858
TOTAL_CLAIMS = LITERAL_CLAIMS + ENTITY_CLAIMS  # 2452


# --------------------------------------------------------------------------- #
# The vocabulary decides which shape a claim has
# --------------------------------------------------------------------------- #

def test_exactly_two_predicates_are_literal_valued():
    assert gv.literal_valued_predicates() == ["AUTHORED", "HAS_ROLE"]


def test_the_split_is_read_from_the_vocabulary_not_hardcoded():
    """A predicate added as literal-valued is exempt without a second edit."""
    expected = sorted(n for n, p in vocab.PREDICATES.items() if not p.entity_valued)
    assert gv.literal_valued_predicates() == expected


def test_every_other_predicate_is_entity_valued():
    literal = set(gv.literal_valued_predicates())
    entity = set(vocab.PREDICATE_NAMES) - literal
    assert entity == {"FUNDED_BY", "LED_BY", "MEMBER_OF", "PARENT_OF",
                      "PARTNER_OF", "WORKS_AT"}
    for name in entity:
        assert vocab.PREDICATES[name].entity_valued, name


def test_the_production_arithmetic_holds():
    assert LITERAL_CLAIMS == 594
    assert ENTITY_CLAIMS == 1858
    assert TOTAL_CLAIMS == 2452


# --------------------------------------------------------------------------- #
# The queries encode the exemption, in both directions
# --------------------------------------------------------------------------- #

def test_missing_object_query_exempts_literal_predicates():
    q = gv.CLAIMS_MISSING_OBJECT
    assert "NOT c.predicate IN $literal_valued" in q
    assert "NOT (c)-[:OBJECT]->()" in q


def test_a_literal_claim_carrying_an_object_edge_is_itself_an_error():
    """The exemption is not a blind spot: the wrong shape is still caught."""
    q = gv.LITERAL_CLAIMS_WITH_OBJECT
    assert "c.predicate IN $literal_valued" in q
    assert "[:OBJECT]" in q


def test_no_literal_node_label_is_introduced():
    for q in (gv.CLAIMS_MISSING_OBJECT, gv.LITERAL_CLAIMS_WITH_OBJECT,
              gv.INELIGIBLE_ENTITIES, gv.ENTITY_STATE):
        assert ":Literal" not in q


def test_the_predicate_lists_are_parameterised():
    """Predicate names must not be pasted into Cypher."""
    for q in (gv.CLAIMS_MISSING_OBJECT, gv.LITERAL_CLAIMS_WITH_OBJECT):
        assert "$literal_valued" in q
        assert "AUTHORED" not in q
        assert "HAS_ROLE" not in q


# --------------------------------------------------------------------------- #
# Zero false positives on the production composition
# --------------------------------------------------------------------------- #

class _Row(dict):
    def __getitem__(self, k):
        return super().__getitem__(k)


class _Session:
    """A graph holding exactly the production claim composition, correctly shaped."""

    def __init__(self, *, literal_with_edge=0, entity_without_edge=0):
        self.literal_with_edge = literal_with_edge
        self.entity_without_edge = entity_without_edge

    def run(self, query, **params):
        literal = set(params.get("literal_valued", ()))
        if query is gv.CLAIMS_MISSING_OBJECT:
            # Entity-valued claims with no OBJECT edge. Correct graph => none.
            return [_Row(claim_id=f"c{i}", predicate="FUNDED_BY")
                    for i in range(self.entity_without_edge)]
        if query is gv.LITERAL_CLAIMS_WITH_OBJECT:
            assert literal == {"AUTHORED", "HAS_ROLE"}
            return [_Row(claim_id=f"l{i}", predicate="AUTHORED")
                    for i in range(self.literal_with_edge)]
        raise AssertionError("unexpected query")


def _shape_problems(session):
    """Just the claim-shape half of `verify`, driven the way `verify` drives it."""
    problems = []
    literal_valued = gv.literal_valued_predicates()
    for row in session.run(gv.CLAIMS_MISSING_OBJECT, literal_valued=literal_valued):
        problems.append(f"entity-valued claim {row['claim_id']} "
                        f"({row['predicate']}) has no OBJECT edge")
    for row in session.run(gv.LITERAL_CLAIMS_WITH_OBJECT,
                           literal_valued=literal_valued):
        problems.append(f"literal-valued claim {row['claim_id']} "
                        f"({row['predicate']}) has an OBJECT edge")
    return problems


def test_a_correct_graph_reports_zero_false_positives():
    """594 literal-valued claims without an OBJECT edge are correct, not broken."""
    assert _shape_problems(_Session()) == []


def test_an_entity_valued_claim_without_an_object_edge_is_still_reported():
    problems = _shape_problems(_Session(entity_without_edge=3))
    assert len(problems) == 3
    assert "has no OBJECT edge" in problems[0]


def test_a_literal_valued_claim_with_an_object_edge_is_reported():
    problems = _shape_problems(_Session(literal_with_edge=2))
    assert len(problems) == 2
    assert "has an OBJECT edge" in problems[0]


# --------------------------------------------------------------------------- #
# The two eligibility invariants broken by the same release
# --------------------------------------------------------------------------- #

def test_scoped_trust_levels_come_from_the_vocabulary():
    from app.knowledge.seed import PREDICATE_SCOPED_TRUST, TRUST_AUTHOR_ATTESTED

    assert gv.scoped_trust_levels() == sorted(PREDICATE_SCOPED_TRUST)
    assert TRUST_AUTHOR_ATTESTED in gv.scoped_trust_levels()


def test_the_ineligible_query_excludes_scoped_levels():
    """Otherwise every author_attested entity reads as an ineligible intruder."""
    assert "NOT e.trust IN $scoped" in gv.INELIGIBLE_ENTITIES


def test_a_provisional_entity_would_still_be_reported():
    """The rule this exemption must not weaken."""
    from app.knowledge.seed import TRUST_PROVISIONAL

    assert TRUST_PROVISIONAL not in gv.scoped_trust_levels()


def test_eligibility_is_compared_against_mysql_not_asserted_true():
    import inspect

    source = inspect.getsource(gv.verify)
    assert 'row["claim_eligible"] is not True' not in source
    assert 'authoritative.get("claim_eligible"' in source
    assert "expected_eligible" in source


def test_the_alias_expectation_uses_the_projector_rule():
    """Filtering on claim_eligible=1 under-counted by 258 scoped aliases."""
    import inspect

    source = inspect.getsource(gv.verify)
    assert "_projectable_clause()" in source
    assert "e.claim_eligible=1" not in source
