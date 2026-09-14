"""Phase C: per-part routing, result merging, and the flag seam.

Covers the two integration points the planner feeds — the graph leg in
`retriever` and the flag read in `query_pipeline` — plus the property that
matters most: with the flag off, nothing on the retrieval path sees a plan.
No LLM, no Qdrant, no MySQL.
"""

from __future__ import annotations

import pytest

from app.core.models.context import ContextBlock
from app.pipeline import query_pipeline as pipe
from app.retrieval import retriever, subqueries as sq
from app.retrieval.understanding import query_processor as qp


def _sub(text, routes=(sq.SEMANTIC,)):
    return sq.SubQuery(text=text, requirement=text, routes=routes)


def _block(chunk_id, text="t"):
    return ContextBlock(n=1, text=text, payload={"chunk_id": chunk_id})


def _flag(monkeypatch, on, **extra):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "subquery_planning_enabled", on, raising=False)
    for key, value in extra.items():
        monkeypatch.setattr(settings, key, value, raising=False)


class _Future:
    """Stand-in for the requirements future `_prepare` hands the planner."""

    def __init__(self, value):
        self._value = value
        self.joined = 0

    def result(self):
        self.joined += 1
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def _pq(query="what services and certifications does TERI offer?"):
    return qp.ProcessedQuery(original=query, search_query=query)


# --------------------------------------------------------------------------- #
# The flag seam
# --------------------------------------------------------------------------- #

def test_the_flag_defaults_off():
    from app.config import Settings

    assert Settings.model_fields["subquery_planning_enabled"].default is False


def test_flag_off_never_even_joins_the_requirements(monkeypatch):
    """The latency property, not just the behaviour one: with planning off,
    retrieval must not wait for the extraction it used to run beside."""
    _flag(monkeypatch, False)
    future = _Future(["services", "certifications"])
    assert pipe._subquery_plan(_pq(), future) == []
    assert future.joined == 0


def test_flag_on_produces_a_plan(monkeypatch):
    _flag(monkeypatch, True)
    plan = pipe._subquery_plan(_pq(), _Future(["services", "certifications"]))
    assert [s.requirement for s in plan] == ["services", "certifications"]


def test_flag_on_with_a_single_requirement_keeps_one_query(monkeypatch):
    _flag(monkeypatch, True)
    assert pipe._subquery_plan(_pq(), _Future(["mission"])) == []


def test_the_fan_out_limit_is_configurable(monkeypatch):
    _flag(monkeypatch, True, subquery_max=2)
    plan = pipe._subquery_plan(_pq(), _Future(["alpha", "beta", "gamma", "delta"]))
    assert len(plan) == 2


def test_a_planning_failure_costs_the_plan_and_not_the_answer(monkeypatch):
    """The base pull runs regardless, so an empty plan is simply the behaviour
    this query had before the feature existed."""
    _flag(monkeypatch, True)
    assert pipe._subquery_plan(_pq(), _Future(RuntimeError("extraction died"))) == []


# --------------------------------------------------------------------------- #
# Graph routing per part
# --------------------------------------------------------------------------- #

def test_only_nominated_parts_are_offered_to_the_graph(monkeypatch):
    asked: list[str] = []

    def fake(query, *, n, filters, source_type):
        asked.append(query)
        return []

    monkeypatch.setattr(retriever, "graph_blocks_for", fake)
    retriever._graph_subquery_blocks(
        [_sub("services"), _sub("funders of X", (sq.SEMANTIC, sq.GRAPH))],
        [], n=6, filters=None, source_type=None,
    )
    assert asked == ["funders of X"]


def test_graph_blocks_from_parts_are_added_to_the_whole_question_s(monkeypatch):
    monkeypatch.setattr(
        retriever, "graph_blocks_for",
        lambda q, *, n, filters, source_type: [_block("from-part")],
    )
    merged = retriever._graph_subquery_blocks(
        [_sub("funders of X", (sq.SEMANTIC, sq.GRAPH))],
        [_block("from-whole")], n=6, filters=None, source_type=None,
    )
    assert [b.payload["chunk_id"] for b in merged] == ["from-whole", "from-part"]


def test_a_row_the_whole_question_already_returned_is_not_repeated(monkeypatch):
    """The whole question and one of its parts routinely resolve to the same
    rows; de-duplication uses the same key the graph/semantic merge does."""
    monkeypatch.setattr(
        retriever, "graph_blocks_for",
        lambda q, *, n, filters, source_type: [_block("same")],
    )
    merged = retriever._graph_subquery_blocks(
        [_sub("a", (sq.SEMANTIC, sq.GRAPH)), _sub("b", (sq.SEMANTIC, sq.GRAPH))],
        [_block("same")], n=6, filters=None, source_type=None,
    )
    assert len(merged) == 1


def test_a_graph_that_answers_nothing_changes_nothing(monkeypatch):
    monkeypatch.setattr(
        retriever, "graph_blocks_for",
        lambda q, *, n, filters, source_type: [],
    )
    existing = [_block("whole")]
    merged = retriever._graph_subquery_blocks(
        [_sub("x", (sq.SEMANTIC, sq.GRAPH))], existing,
        n=6, filters=None, source_type=None,
    )
    assert [b.payload["chunk_id"] for b in merged] == ["whole"]


def test_an_empty_plan_asks_the_graph_nothing(monkeypatch):
    """The flag-off path: no plan, no extra graph attempts, blocks untouched."""
    called: list[str] = []

    def fake(query, **_kw):
        called.append(query)
        return []

    monkeypatch.setattr(retriever, "graph_blocks_for", fake)
    merged = retriever._graph_subquery_blocks(
        [], [_block("whole")], n=6, filters=None, source_type=None
    )
    assert [b.payload["chunk_id"] for b in merged] == ["whole"]
    assert called == []


# --------------------------------------------------------------------------- #
# Semantic legs: merged and de-duplicated by the existing RRF
# --------------------------------------------------------------------------- #

def test_sub_query_rankings_fuse_and_deduplicate_through_rrf():
    """Merging is not new code: a passage two parts both reach is one candidate
    after `rrf`, and it is promoted for appearing in both."""
    from app.retrieval.search.fusion import rrf
    from app.retrieval.search.hybrid_search import Candidate

    def _c(cid, score=0.5):
        return Candidate(id=cid, score=score, payload={}, vector=None)

    base = [_c("shared"), _c("only-base")]
    part_a = [_c("shared"), _c("only-a")]
    part_b = [_c("only-b"), _c("shared")]

    fused = rrf([base, part_a, part_b])
    assert len(fused) == len({c.id for c in fused}) == 4
    assert fused[0].id == "shared", "a passage every part reaches should lead"


def test_subquery_search_failure_costs_only_its_own_ranking(monkeypatch):
    from app.retrieval.search import strategies

    def boom(*a, **k):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(strategies, "search", boom)
    assert strategies.subquery_search("a part", limit=10) == []


# --------------------------------------------------------------------------- #
# Mixed-source questions
# --------------------------------------------------------------------------- #

def test_a_mixed_question_keeps_its_catalog_section_and_gains_parts(monkeypatch):
    """MySQL is still routed for the whole question on the path it always was
    (`_db_section`), while the content side decomposes. Phase C adds parts
    around the catalog answer; it does not move the catalog answer."""
    _flag(monkeypatch, True)
    understanding = qp.QueryUnderstanding(
        query_rewrite="how many reports are there and what do they cover",
        intents=[
            qp.IntentPrediction(label="database", confidence=0.9),
            qp.IntentPrediction(label="qa", confidence=0.8),
        ],
    )
    pq = qp.ProcessedQuery(
        original="q", search_query="how many reports are there and what do they cover",
        understanding=understanding,
    )
    assert pipe._capabilities(pq) == {"database", "qa"}
    plan = pipe._subquery_plan(pq, _Future(["report count", "report coverage"]))
    assert len(plan) == 2
