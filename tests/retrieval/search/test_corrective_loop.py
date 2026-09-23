"""Unit tests for the one-shot corrective retrieval loop.

Covers the confidence gate in retrieve() (flag off, score above threshold,
score below threshold), the requery composition (reformulate -> search ->
RRF-fuse -> second rerank), and every fail-open path. LLM, embedder, and
Qdrant are stubbed; no network.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.retrieval import retriever
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.search import strategies


def _cand(id, semantic_score=0.5, text="passage text"):
    return Candidate(id=id, score=semantic_score, semantic_score=semantic_score,
                     payload={"chunk_text": text})


def _settings(**overrides):
    base = dict(
        retrieval_top_k=6, retrieval_candidate_k=40, prefer_website_enabled=False,
        multi_query_enabled=False, multi_query_paraphrases=2,
        rerank_table_boost=0.15, keyword_leg_enabled=False,
        corrective_loop_enabled=True, corrective_min_score=0.2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _wire(monkeypatch, *, settings, ranked):
    monkeypatch.setattr(retriever, "get_settings", lambda: settings)
    monkeypatch.setattr(retriever, "search", lambda *a, **k: list(ranked))
    monkeypatch.setattr(retriever, "rerank", lambda q, cands, **kw: list(ranked))
    monkeypatch.setattr(
        retriever, "build_context", lambda cands, *, limit, temporal=None, question="": list(cands)
    )


# --------------------------------------------------------------------------- #
# The confidence gate in retrieve().
# --------------------------------------------------------------------------- #

def test_low_confidence_triggers_corrective(monkeypatch):
    weak = [_cand("a", semantic_score=0.1)]
    _wire(monkeypatch, settings=_settings(), ranked=weak)
    calls: list = []
    monkeypatch.setattr(
        retriever, "corrective_requery",
        lambda q, ranked, **kw: calls.append(q) or [_cand("better", 0.8)],
    )

    out = retriever.retrieve("hard question", query_vector=[0.1])
    assert calls == ["hard question"]
    assert [b.id for b in out] == ["better"]


def test_confident_or_disabled_skips_corrective(monkeypatch):
    def no_requery(*a, **kw):
        raise AssertionError("corrective must not run")

    strong = [_cand("a", semantic_score=0.9)]
    _wire(monkeypatch, settings=_settings(), ranked=strong)
    monkeypatch.setattr(retriever, "corrective_requery", no_requery)
    retriever.retrieve("q", query_vector=[0.1])

    weak = [_cand("a", semantic_score=0.1)]
    _wire(monkeypatch, settings=_settings(corrective_loop_enabled=False), ranked=weak)
    monkeypatch.setattr(retriever, "corrective_requery", no_requery)
    retriever.retrieve("q", query_vector=[0.1])

    _wire(monkeypatch, settings=_settings(), ranked=[])  # nothing ranked at all
    monkeypatch.setattr(retriever, "corrective_requery", no_requery)
    assert retriever.retrieve("q", query_vector=[0.1]) == []


# --------------------------------------------------------------------------- #
# corrective_requery composition.
# --------------------------------------------------------------------------- #

def test_requery_fuses_and_reranks_once(monkeypatch):
    ranked = [_cand("a"), _cand("b")]
    monkeypatch.setattr(strategies, "corrective_query", lambda q, r: "reformulated query")
    monkeypatch.setattr(strategies, "embed_query", lambda q: [0.9])
    searched: list = []

    def fake_search(query, **kw):
        searched.append((query, kw.get("query_vector")))
        return [_cand("b"), _cand("c")]

    monkeypatch.setattr(strategies, "search", fake_search)
    reranked: list = []
    monkeypatch.setattr(
        strategies, "rerank",
        lambda q, cands, **kw: reranked.extend(cands) or list(cands),
    )

    out = strategies.corrective_requery(
        "original", ranked,
        filters=None, limit=40, table_boost=0.0,
    )

    assert searched == [("reformulated query", [0.9])]
    assert {c.id for c in reranked} == {"a", "b", "c"}  # RRF union, deduped
    assert out[0].id == "b"  # consensus of both rankings leads


def test_requery_fail_open_paths(monkeypatch):
    ranked = [_cand("a")]

    # No reformulation -> unchanged, search never runs.
    monkeypatch.setattr(strategies, "corrective_query", lambda q, r: None)

    def no_search(*a, **kw):
        raise AssertionError("search must not run without a reformulation")

    monkeypatch.setattr(strategies, "search", no_search)
    assert strategies.corrective_requery(
        "q", ranked, filters=None,
        limit=40, table_boost=0.0,
    ) is ranked

    # Requery finds nothing new -> unchanged, no second rerank.
    monkeypatch.setattr(strategies, "corrective_query", lambda q, r: "other")
    monkeypatch.setattr(strategies, "embed_query", lambda q: [0.9])
    monkeypatch.setattr(strategies, "search", lambda *a, **kw: [_cand("a")])

    def no_rerank(*a, **kw):
        raise AssertionError("rerank must not run when nothing new arrived")

    monkeypatch.setattr(strategies, "rerank", no_rerank)
    assert strategies.corrective_requery(
        "q", ranked, filters=None,
        limit=40, table_boost=0.0,
    ) is ranked

    # Any exception -> unchanged.
    def boom(*a, **kw):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(strategies, "search", boom)
    assert strategies.corrective_requery(
        "q", ranked, filters=None,
        limit=40, table_boost=0.0,
    ) is ranked


# --------------------------------------------------------------------------- #
# Reformulation call.
# --------------------------------------------------------------------------- #

class _FakeStructured:
    def __init__(self, query):
        self._query = query

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        if isinstance(self._query, Exception):
            raise self._query
        return SimpleNamespace(query=self._query)


def test_corrective_query_returns_reformulation(monkeypatch):
    monkeypatch.setattr(
        strategies, "get_structured_llm", lambda: _FakeStructured("  better query ")
    )
    assert strategies.corrective_query("original", [_cand("a")]) == "better query"


def test_corrective_query_rejects_echo_and_errors(monkeypatch):
    monkeypatch.setattr(
        strategies, "get_structured_llm", lambda: _FakeStructured("ORIGINAL")
    )
    assert strategies.corrective_query("original", []) is None

    monkeypatch.setattr(
        strategies, "get_structured_llm", lambda: _FakeStructured(RuntimeError("down"))
    )
    assert strategies.corrective_query("original", []) is None
