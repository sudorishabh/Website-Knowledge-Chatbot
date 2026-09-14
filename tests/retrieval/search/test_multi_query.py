"""Unit tests for multi-query retrieval and reciprocal-rank fusion.

Covers the RRF math (id-dedup, first-sighting payload, deterministic ties),
the generated-query cleaner, and the per-query gates in ``retrieve()`` (flag off,
short query, explicit filters/source, non-content capability). LLM and Qdrant
are stubbed; no network.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.retrieval import retriever
from app.retrieval.search.fusion import rrf
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.search import strategies


def _cand(id, payload=None):
    return Candidate(id=id, score=0.9, payload=payload or {})


# --------------------------------------------------------------------------- #
# RRF math.
# --------------------------------------------------------------------------- #

def test_rrf_consensus_outranks_single_list():
    base = [_cand("a"), _cand("b"), _cand("c")]
    other = [_cand("c"), _cand("d")]
    fused = rrf([base, other])

    ids = [c.id for c in fused]
    assert ids[0] == "c"  # in both lists, beats every single-list candidate
    assert set(ids) == {"a", "b", "c", "d"}  # id-dedup, nothing lost
    assert fused[0].score == (1 / 63) + (1 / 61)


def test_rrf_keeps_first_sighting_payload():
    first = _cand("x", payload={"chunk_text": "first"})
    second = _cand("x", payload={"chunk_text": "second"})
    fused = rrf([[first], [second]])
    assert len(fused) == 1 and fused[0].payload["chunk_text"] == "first"


def test_rrf_ties_break_on_id():
    fused = rrf([[_cand("b")], [_cand("a")]])  # same rank in different lists
    assert [c.id for c in fused] == ["a", "b"]


def test_rrf_empty_rankings():
    assert rrf([]) == []
    assert [c.id for c in rrf([[], [_cand("a")]])] == ["a"]


# --------------------------------------------------------------------------- #
# Paraphrase generation — cleaning and fail-open.
# --------------------------------------------------------------------------- #

class _FakeLLM:
    def __init__(self, queries):
        self._queries = queries

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        return SimpleNamespace(queries=self._queries)


def test_generated_queries_cleaned_deduped_capped(monkeypatch):
    """Unchanged contract, on the generator that replaced paraphrasing: strip,
    drop the echoed base query, cap at n. See test_perspectives.py for the
    distinctness rules Phase D added."""
    monkeypatch.setattr(
        strategies, "get_llm",
        lambda **kw: _FakeLLM(
            ["  Alt one ", "", "BASE QUERY", "alt two", "alt three"]
        ),
    )
    out = strategies.perspectives("base query", 2)
    assert [p.text for p in out] == ["Alt one", "alt two"]


def test_generation_fails_open(monkeypatch):
    def boom(**kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(strategies, "get_llm", boom)
    assert strategies.perspectives("base query", 2) == []


# --------------------------------------------------------------------------- #
# retrieve() gates and fusion wiring.
# --------------------------------------------------------------------------- #

def _settings(**overrides):
    base = dict(
        retrieval_top_k=6, retrieval_candidate_k=40, prefer_website_enabled=False,
        multi_query_enabled=True, multi_query_paraphrases=2,
        multi_query_distinct_threshold=0.92, rerank_table_boost=0.15,
        keyword_leg_enabled=False, corrective_loop_enabled=False,
        corrective_min_score=0.2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _wire(monkeypatch, *, settings, base_candidates):
    monkeypatch.setattr(retriever, "get_settings", lambda: settings)
    monkeypatch.setattr(retriever, "search", lambda *a, **k: base_candidates)
    monkeypatch.setattr(retriever, "rerank", lambda q, cands, **kw: cands)
    monkeypatch.setattr(
        retriever, "build_context",
        lambda ranked, *, limit, segregate: list(ranked),
    )


def test_multi_query_fuses_perspective_pulls(monkeypatch):
    _wire(monkeypatch, settings=_settings(), base_candidates=[_cand("a"), _cand("b")])
    monkeypatch.setattr(
        retriever, "perspectives",
        lambda q, n, **kw: [strategies.Perspective("p1"), strategies.Perspective("p2")],
    )
    pulls: list = []
    monkeypatch.setattr(
        retriever, "perspective_search",
        lambda q, **kw: pulls.append(q) or [_cand("b"), _cand("c")],
    )

    out = retriever.retrieve("what are the impacts of biofuel adoption", query_vector=[0.1])

    assert sorted(pulls) == ["p1", "p2"]
    assert [b.id for b in out][0] == "b"  # consensus candidate leads after RRF
    assert {b.id for b in out} == {"a", "b", "c"}


def test_multi_query_no_perspectives_uses_base(monkeypatch):
    base = [_cand("a")]
    _wire(monkeypatch, settings=_settings(), base_candidates=base)
    monkeypatch.setattr(retriever, "perspectives", lambda q, n, **kw: [])

    out = retriever.retrieve("what are the impacts of biofuel adoption", query_vector=[0.1])
    assert [b.id for b in out] == ["a"]


def test_multi_query_gates(monkeypatch):
    def no_generation(q, n, **kw):
        raise AssertionError("perspective generation must not run")

    base = [_cand("a")]

    # Flag off.
    _wire(monkeypatch, settings=_settings(multi_query_enabled=False), base_candidates=base)
    monkeypatch.setattr(retriever, "perspectives", no_generation)
    retriever.retrieve("what are the impacts of biofuel adoption", query_vector=[0.1])

    # Short query, explicit source, filters, non-content capability.
    _wire(monkeypatch, settings=_settings(), base_candidates=base)
    monkeypatch.setattr(retriever, "perspectives", no_generation)
    retriever.retrieve("biofuel impacts", query_vector=[0.1])
    retriever.retrieve("what are the impacts of biofuel adoption",
                       query_vector=[0.1], source_type="pdf")
    retriever.retrieve("what are the impacts of biofuel adoption",
                       query_vector=[0.1], filters=["cond"])
    retriever.retrieve("what are the impacts of biofuel adoption",
                       query_vector=[0.1], capabilities={"database"})
