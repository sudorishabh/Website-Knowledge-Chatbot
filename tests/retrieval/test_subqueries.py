"""Phase C: turning already-extracted requirements into retrievable sub-queries.

The planner is pure — no LLM, no stores — so these are plain function tests. The
routing probe and the anchor extractor are the two things it reaches out to, and
both are patched at their own modules where a test needs to pin them.
"""

from __future__ import annotations

import pytest

from app.retrieval import subqueries as sq


def _plan(query, requirements, **kw):
    return sq.plan(query, requirements, **kw)


# --------------------------------------------------------------------------- #
# When to decompose at all
# --------------------------------------------------------------------------- #

def test_a_single_requirement_keeps_the_single_query_path():
    """The ordinary question. Splitting it would re-run the base pull under a
    different name."""
    assert _plan("what is TERI's mission?", ["mission"]) == []


def test_no_requirements_is_a_no_op():
    """`extract_requirements` already returns [] on any failure, so this is also
    the degraded-LLM path."""
    assert _plan("anything", []) == []
    assert _plan("anything", None) == []


def test_blank_requirements_are_discarded_before_counting():
    assert _plan("q", ["  ", "", "mission"]) == []


def test_two_requirements_decompose():
    plan = _plan("what services and certifications does TERI offer?",
                 ["services", "certifications"])
    assert [s.requirement for s in plan] == ["services", "certifications"]


def test_the_fan_out_is_bounded():
    plan = _plan("q about TERI", ["a one", "b two", "c three", "d four", "e five"],
                 limit=3)
    assert len(plan) == 3


def test_duplicate_parts_are_retrieved_once(monkeypatch):
    """Two requirements that re-anchor onto the same text are one pull — and one
    pull is the single-query path, so nothing is decomposed."""
    monkeypatch.setattr(sq, "_anchors", lambda q, limit=2: [])
    assert _plan("q", ["services", "services"]) == []
    assert len(_plan("q", ["services", "services", "certifications"])) == 2


def test_a_part_identical_to_the_base_query_is_dropped(monkeypatch):
    """It would re-run the pull that is already happening. With only one part
    left after the drop, the query keeps the single-query path."""
    monkeypatch.setattr(sq, "_anchors", lambda q, limit=2: [])
    assert _plan("mission", ["mission", "vision"]) == []
    assert [s.requirement for s in _plan("mission", ["mission", "vision", "values"])] == [
        "vision", "values",
    ]


# --------------------------------------------------------------------------- #
# Sub-query text: re-anchoring a bare noun phrase
# --------------------------------------------------------------------------- #

def test_a_requirement_is_anchored_to_the_query_s_proper_nouns(monkeypatch):
    """`extract_requirements` returns noun phrases, so "revenue lost" alone has
    lost what it is about. The anchors come from the existing key-term extractor."""
    monkeypatch.setattr(sq, "_anchors", lambda q, limit=2: ["Project X"])
    plan = _plan("which customers were affected by Project X and what revenue was lost?",
                 ["affected customers", "revenue lost"])
    assert [s.text for s in plan] == [
        "Project X affected customers", "Project X revenue lost",
    ]


def test_an_anchor_already_in_the_requirement_is_not_repeated(monkeypatch):
    monkeypatch.setattr(sq, "_anchors", lambda q, limit=2: ["Project X"])
    plan = _plan("q", ["Project X delays", "budget"])
    assert plan[0].text == "Project X delays"


def test_no_anchors_leaves_the_requirement_to_stand_alone(monkeypatch):
    monkeypatch.setattr(sq, "_anchors", lambda q, limit=2: [])
    plan = _plan("q", ["services", "certifications"])
    assert [s.text for s in plan] == ["services", "certifications"]


def test_anchor_extraction_failure_does_not_break_planning(monkeypatch):
    from app.retrieval.search import strategies

    def boom(_q):
        raise RuntimeError("nope")

    monkeypatch.setattr(strategies, "extract_key_terms", boom)
    plan = _plan("q", ["services", "certifications"])
    assert len(plan) == 2


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

def test_every_part_goes_to_the_semantic_leg(monkeypatch):
    monkeypatch.setattr(sq, "_routes", lambda text: (sq.SEMANTIC,))
    plan = _plan("q", ["services", "certifications"])
    assert all(s.goes_to(sq.SEMANTIC) for s in plan)
    assert not any(s.goes_to(sq.GRAPH) for s in plan)


def test_a_relational_part_is_also_nominated_to_the_graph(monkeypatch):
    """Reuses `relational.read_relational` — the same deterministic probe query
    understanding already uses. A nomination only: the graph's policy layer
    still decides."""
    from app.retrieval.understanding import relational

    class _Intent:
        is_relational = True

    monkeypatch.setattr(relational, "read_relational", lambda t: _Intent())
    plan = _plan("q", ["funders of Project X", "outcomes"])
    assert all(s.goes_to(sq.GRAPH) for s in plan)


def test_a_failing_relational_probe_falls_back_to_semantic_only(monkeypatch):
    from app.retrieval.understanding import relational

    def boom(_t):
        raise RuntimeError("vocab unavailable")

    monkeypatch.setattr(relational, "read_relational", boom)
    plan = _plan("q", ["services", "certifications"])
    assert [s.routes for s in plan] == [(sq.SEMANTIC,), (sq.SEMANTIC,)]


def test_texts_selects_the_parts_for_one_leg():
    plan = [
        sq.SubQuery(text="a", requirement="a", routes=(sq.SEMANTIC,)),
        sq.SubQuery(text="b", requirement="b", routes=(sq.SEMANTIC, sq.GRAPH)),
    ]
    assert sq.texts(plan, sq.SEMANTIC) == ["a", "b"]
    assert sq.texts(plan, sq.GRAPH) == ["b"]
    assert sq.texts([], sq.SEMANTIC) == []
