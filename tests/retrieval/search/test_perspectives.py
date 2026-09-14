"""Phase D: retrieval perspectives, and the guard that keeps them honest.

The point of this file is not that the generated strings look different — a
prompt can be judged by eye. It is that the *acceptance rules* reject a
reworded question and that a passage only a secondary perspective can reach
survives fusion. `test_a_passage_only_a_perspective_finds_survives_fusion` is
the one that demonstrates recall.

LLM and embeddings are stubbed; no network.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from app.retrieval import retriever
from app.retrieval.search import strategies
from app.retrieval.search.fusion import rrf
from app.retrieval.search.hybrid_search import Candidate

QUERY = "What issues affected Project X?"

# Hand-placed vectors: axis 0 is "the original question's neighbourhood", axis 1
# is "a different aspect". Cosine against the original is then something the
# test states outright rather than something an embedding model decides.
_VECTORS = {
    QUERY.lower(): [1.0, 0.0],
    # Synonym swaps: different words, same neighbourhood.
    "which problems were associated with project x?": [0.999, 0.044],
    "what problems affected project x?": [0.999, 0.044],
    # Genuinely different angles.
    "project x delays and blockers": [0.6, 0.8],
    "project x customer complaints": [0.1, 0.995],
    "project x quality problems": [0.55, 0.835],
    # Four well-separated angles, for the cap test: pairwise cosine 0.77 at
    # worst, comfortably under any threshold these tests use.
    "project x alpha aspect": [0.0, 1.0, 0.0],
    "project x beta aspect": [0.0, 0.766, 0.643],
    "project x gamma aspect": [0.0, 0.174, 0.985],
    "project x delta aspect": [0.0, -0.5, 0.866],
}


class _FakeLLM:
    def __init__(self, queries):
        self._queries = queries
        self.calls = 0

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(queries=self._queries)


@pytest.fixture
def _embeddings(monkeypatch):
    """Deterministic query embeddings.

    An unlisted string gets its own direction, orthogonal to the original's
    axis — so it is distinct from the question *and* from every other unlisted
    string, rather than colliding with them and tripping the pairwise guard for
    the wrong reason.
    """
    def fake(text):
        key = text.strip().lower()
        if key in _VECTORS:
            return _VECTORS[key]
        angle = (sum(ord(c) for c in key) % 100) / 100 * (math.pi / 2)
        return [0.0, math.cos(angle), math.sin(angle)]

    monkeypatch.setattr(strategies, "embed_query", fake)
    return fake


def _generate(monkeypatch, queries):
    llm = _FakeLLM(queries)
    monkeypatch.setattr(strategies, "get_llm", lambda **kw: llm)
    return llm


def _perspectives(monkeypatch, queries, n=3, vector=None, **settings):
    _generate(monkeypatch, queries)
    base = dict(multi_query_distinct_threshold=0.92)
    base.update(settings)
    monkeypatch.setattr(strategies, "get_settings", lambda: SimpleNamespace(**base))
    return strategies.perspectives(QUERY, n, query_vector=vector)


# --------------------------------------------------------------------------- #
# What counts as a perspective rather than a rewording
# --------------------------------------------------------------------------- #

def test_different_aspects_are_accepted(monkeypatch, _embeddings):
    out = _perspectives(
        monkeypatch,
        ["Project X delays and blockers", "Project X customer complaints"],
        vector=_VECTORS[QUERY.lower()],
    )
    assert [p.text for p in out] == [
        "Project X delays and blockers", "Project X customer complaints",
    ]


def test_a_rewording_is_rejected_without_needing_an_embedding(monkeypatch, _embeddings):
    """The free half of the guard: every content word is already in the original,
    so the leg would contribute no new vocabulary."""
    assert strategies._is_rewording("What issues did Project X have?", QUERY)
    assert not strategies._is_rewording("Project X delays and blockers", QUERY)
    out = _perspectives(monkeypatch, ["What issues did Project X have?"], vector=None)
    assert out == []


def test_a_synonym_swap_is_rejected_by_the_semantic_guard(monkeypatch, _embeddings):
    """"Which problems were associated with Project X?" introduces new words, so
    word overlap cannot see it — but it sits on top of the original in embedding
    space, which is what makes it a duplicate leg."""
    candidate = "Which problems were associated with Project X?"
    assert not strategies._is_rewording(candidate, QUERY)
    out = _perspectives(monkeypatch, [candidate], vector=_VECTORS[QUERY.lower()])
    assert out == [], "a near-identical vector must not earn its own leg"


def test_perspectives_too_close_to_each_other_are_dropped(monkeypatch, _embeddings):
    """Two angles that land in the same neighbourhood are one leg, not two."""
    out = _perspectives(
        monkeypatch,
        ["Project X delays and blockers", "Project X quality problems"],
        vector=_VECTORS[QUERY.lower()],
        multi_query_distinct_threshold=0.98,
    )
    assert [p.text for p in out] == ["Project X delays and blockers"]


def test_exact_duplicates_and_the_echoed_query_are_dropped(monkeypatch, _embeddings):
    out = _perspectives(
        monkeypatch,
        [QUERY, "Project X customer complaints", "project x customer complaints", ""],
        vector=_VECTORS[QUERY.lower()],
    )
    assert [p.text for p in out] == ["Project X customer complaints"]


def test_the_count_is_bounded(monkeypatch, _embeddings):
    out = _perspectives(
        monkeypatch,
        ["Project X alpha aspect", "Project X beta aspect", "Project X gamma aspect",
         "Project X delta aspect"],
        n=2, vector=_VECTORS[QUERY.lower()],
    )
    assert len(out) == 2


# --------------------------------------------------------------------------- #
# Fallback, and the absence of a loop
# --------------------------------------------------------------------------- #

def test_all_duplicates_falls_back_to_the_base_query(monkeypatch, _embeddings):
    """The explicit requirement: rather than adding noisy legs, add none. The
    base query is always its own leg, so this is the retrieval the query would
    have had."""
    out = _perspectives(
        monkeypatch,
        ["What problems affected Project X?", "What issues did Project X have?"],
        vector=_VECTORS[QUERY.lower()],
    )
    assert out == []


def test_a_generation_failure_falls_back_to_the_base_query(monkeypatch):
    def boom(**_kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(strategies, "get_llm", boom)
    assert strategies.perspectives(QUERY, 3) == []


@pytest.mark.parametrize("payload", [None, "not a list", 42, {"queries": 1}])
def test_malformed_generation_output_falls_back(monkeypatch, payload):
    """A structured response that is not a list of strings reads as "nothing
    generated", never as a crash."""
    monkeypatch.setattr(
        strategies, "get_llm",
        lambda **kw: _FakeLLM(payload) if payload is not None
        else SimpleNamespace(
            with_structured_output=lambda _s: SimpleNamespace(
                invoke=lambda _m: SimpleNamespace()
            )
        ),
    )
    assert strategies.perspectives(QUERY, 3) == []


def test_generation_runs_exactly_once(monkeypatch, _embeddings):
    """No retry, no recursion: one call per query, whatever it returns."""
    llm = _generate(monkeypatch, ["What problems affected Project X?"])
    monkeypatch.setattr(
        strategies, "get_settings",
        lambda: SimpleNamespace(multi_query_distinct_threshold=0.92),
    )
    assert strategies.perspectives(QUERY, 3, query_vector=[1.0, 0.0]) == []
    assert llm.calls == 1


def test_an_embedding_failure_keeps_the_perspective(monkeypatch):
    """A missing vector is a missing *check*, not evidence of duplication — the
    lexical guard still applies and the pull embeds as it always did."""
    def boom(_text):
        raise RuntimeError("embeddings down")

    monkeypatch.setattr(strategies, "embed_query", boom)
    out = _perspectives(
        monkeypatch, ["Project X customer complaints"], vector=[1.0, 0.0]
    )
    assert [p.text for p in out] == ["Project X customer complaints"]
    assert out[0].vector is None


def test_an_accepted_perspective_carries_its_vector(monkeypatch, _embeddings):
    """So the pull does not embed the same string a second time."""
    out = _perspectives(
        monkeypatch, ["Project X customer complaints"],
        vector=_VECTORS[QUERY.lower()],
    )
    assert out[0].vector == _VECTORS["project x customer complaints"]


def test_the_carried_vector_is_what_the_pull_searches_with(monkeypatch):
    seen = {}

    def fake_search(query, *, limit, trace_stage, query_vector):
        seen["vector"] = query_vector
        seen["stage"] = trace_stage
        return []

    monkeypatch.setattr(strategies, "search", fake_search)
    monkeypatch.setattr(
        strategies, "embed_query",
        lambda t: pytest.fail("the pull must reuse the carried vector"),
    )
    strategies.perspective_search("a perspective", limit=10, query_vector=[0.3, 0.4])
    assert seen["vector"] == [0.3, 0.4]
    # The trace label is a stable contract; Phase D changed the content, not it.
    assert seen["stage"] == "multi_query_leg"


# --------------------------------------------------------------------------- #
# Recall: the test that has to be about evidence, not wording
# --------------------------------------------------------------------------- #

def _settings(**overrides):
    base = dict(
        retrieval_top_k=6, retrieval_candidate_k=40, prefer_website_enabled=False,
        multi_query_enabled=True, multi_query_paraphrases=2,
        multi_query_distinct_threshold=0.92, rerank_table_boost=0.15,
        keyword_leg_enabled=False, corrective_loop_enabled=False,
        corrective_min_score=0.2, graph_routing_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_a_passage_only_a_perspective_finds_survives_fusion(monkeypatch):
    """Recall, demonstrated rather than asserted about wording.

    The base query retrieves A. A useful perspective — and only that
    perspective — retrieves B. B must be in the final context, and RRF must be
    what put it there.
    """
    settings = _settings()
    only_base = Candidate(id="A", score=0.9, payload={}, semantic_score=0.9)
    only_perspective = Candidate(id="B", score=0.9, payload={}, semantic_score=0.9)

    monkeypatch.setattr(retriever, "get_settings", lambda: settings)
    monkeypatch.setattr(retriever, "search", lambda *a, **k: [only_base])
    monkeypatch.setattr(retriever, "title_search", lambda *a, **k: [])
    monkeypatch.setattr(retriever, "_observe_in_shadow", lambda *a, **k: None)
    # The real rrf and a pass-through rerank: fusion is what is under test.
    monkeypatch.setattr(retriever, "rerank", lambda q, cands, **kw: list(cands))
    monkeypatch.setattr(
        retriever, "build_context",
        lambda ranked, *, limit, segregate: list(ranked),
    )
    monkeypatch.setattr(
        retriever, "perspectives",
        lambda q, n, **kw: [strategies.Perspective("Project X customer complaints",
                                                  vector=[0.1, 0.995])],
    )
    monkeypatch.setattr(
        retriever, "perspective_search",
        lambda q, **kw: [only_perspective],
    )

    out = retriever.retrieve(
        "what issues affected Project X during rollout", query_vector=[1.0, 0.0]
    )

    ids = [c.id for c in out]
    assert "B" in ids, "evidence only the perspective could reach was lost"
    assert "A" in ids, "the base query's own evidence must not be displaced"


def test_without_the_perspective_that_passage_is_not_retrieved(monkeypatch):
    """The control for the test above: with generation returning nothing, B is
    absent — so the recall gain is attributable to the perspective leg."""
    settings = _settings()
    only_base = Candidate(id="A", score=0.9, payload={}, semantic_score=0.9)

    monkeypatch.setattr(retriever, "get_settings", lambda: settings)
    monkeypatch.setattr(retriever, "search", lambda *a, **k: [only_base])
    monkeypatch.setattr(retriever, "title_search", lambda *a, **k: [])
    monkeypatch.setattr(retriever, "_observe_in_shadow", lambda *a, **k: None)
    monkeypatch.setattr(retriever, "rerank", lambda q, cands, **kw: list(cands))
    monkeypatch.setattr(
        retriever, "build_context",
        lambda ranked, *, limit, segregate: list(ranked),
    )
    monkeypatch.setattr(retriever, "perspectives", lambda q, n, **kw: [])
    monkeypatch.setattr(
        retriever, "perspective_search",
        lambda q, **kw: pytest.fail("no perspective, no leg"),
    )

    out = retriever.retrieve(
        "what issues affected Project X during rollout", query_vector=[1.0, 0.0]
    )
    assert [c.id for c in out] == ["A"]


def test_fusion_promotes_a_passage_both_legs_reach():
    """RRF is untouched and still does the merging: a candidate every leg finds
    leads, and nothing is duplicated."""
    def _c(cid):
        return Candidate(id=cid, score=0.9, payload={})

    fused = rrf([[_c("A"), _c("shared")], [_c("shared"), _c("B")]])
    assert [c.id for c in fused][0] == "shared"
    assert sorted(c.id for c in fused) == ["A", "B", "shared"]
