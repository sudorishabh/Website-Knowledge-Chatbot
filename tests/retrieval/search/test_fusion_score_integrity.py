"""Regression tests: rank fusion must not corrupt the semantic score.

``rrf`` fuses on *rank*, so its output is on a reciprocal-rank scale
(~0.016-0.033) that has nothing to do with cosine similarity. Several consumers
read a candidate's semantic relevance to compare it against a *cosine-scaled*
threshold:

- ``context_builder`` gates website slots on ``website_chunk_floor`` (0.30) and
  the extra PDF slot on ``pdf_high_confidence_floor`` (0.5);
- ``retriever`` opens the corrective loop below ``corrective_min_score`` (0.2);
- ``reranker`` drops candidates below ``rerank_score_threshold``.

Before the fix these read the fused value, so merely enabling the keyword leg or
multi-query silently starved the website group and pinned the corrective loop
open. The invariant enforced here: ``semantic_score`` is the raw dense/reranker
relevance and survives fusion untouched, while the fused ranking value lives in
``fusion_score`` and orders the candidates. Qdrant and the LLM are stubbed.

Any test here that calls ``rerank`` and asserts a *scale* must pin
``reranker_provider``, because ``rerank`` reads the live Settings: unpinned, the
developer's ``.env`` picks the scorer, and a tree with
``RERANKER_PROVIDER=cross_encoder`` turns a scale assertion into an assertion
about a real model's output. Use
``monkeypatch.setattr(reranker_module, "get_settings", lambda: _settings())`` —
``_settings()`` defaults to ``embedding``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.retrieval import retriever
from app.retrieval.search import strategies
from app.retrieval.context.builder import build_context
from app.retrieval.search.fusion import rrf
from app.retrieval.search.hybrid_search import Candidate, _to_candidate
from app.retrieval.search import reranker as reranker_module
from app.retrieval.search.reranker import rerank

# The two floors this whole file is about, at their production defaults.
WEBSITE_FLOOR = 0.30
CORRECTIVE_MIN = 0.2

# A rank-1/rank-2 RRF contribution, for asserting on the fused scale directly.
RRF_RANK1 = 1 / 61
RRF_RANK2 = 1 / 62


def _point(id_, score, payload=None, vector=None):
    """A Qdrant scored point, as ``search()`` receives it."""
    return SimpleNamespace(
        id=id_, score=score, payload=payload or {}, vector=vector or [1.0, 0.0]
    )


def _searched(id_, score, source_type="website", vector=None):
    """A candidate exactly as the search layer produces it."""
    return _to_candidate(
        _point(
            id_, score,
            payload={
                "source_type": source_type,
                "document_id": id_,
                "chunk_text": f"body text for {id_}",
            },
            vector=vector,
        )
    )


# --------------------------------------------------------------------------- #
# 1. Dense-only retrieval preserves semantic scoring.
# --------------------------------------------------------------------------- #

def test_dense_only_retrieval_preserves_semantic_score(monkeypatch):
    # The provider is pinned because this test asserts the *embedding* contract:
    # that `rerank` hands back the dense score untouched. Unpinned, `rerank`
    # reads the real Settings and so the developer's own .env decides which
    # scorer runs — with RERANKER_PROVIDER=cross_encoder set, this asserted 0.72
    # against a genuine model score of 1.5e-05 and failed for a reason that has
    # nothing to do with fusion. See the note in the module docstring.
    monkeypatch.setattr(reranker_module, "get_settings", lambda: _settings())

    cand = _searched("a", 0.72)
    assert cand.semantic_score == pytest.approx(0.72)
    assert cand.fusion_score == 0.0  # nothing fused this candidate

    ranked = rerank("solar capacity", [cand])
    assert ranked[0].semantic_score == pytest.approx(0.72)
    assert ranked[0].semantic_score >= WEBSITE_FLOOR


# --------------------------------------------------------------------------- #
# 4/6. RRF reorders without touching the semantic scale, and its own ranking
#      behaviour is unchanged.
# --------------------------------------------------------------------------- #

def test_rrf_reorders_without_corrupting_semantic_scores():
    strong = _searched("strong", 0.72)   # better cosine, only in one list
    consensus = _searched("consensus", 0.55)  # weaker cosine, in both lists

    fused = rrf([[strong, consensus], [consensus]])
    by_id = {c.id: c for c in fused}

    # Consensus wins the ranking — that is RRF doing its job.
    assert [c.id for c in fused] == ["consensus", "strong"]
    # ...but neither candidate's semantic relevance moved.
    assert by_id["strong"].semantic_score == pytest.approx(0.72)
    assert by_id["consensus"].semantic_score == pytest.approx(0.55)
    # ...and both still clear a cosine-scaled floor.
    assert min(c.semantic_score for c in fused) >= WEBSITE_FLOOR

    # The fused value is on its own scale, in its own field.
    assert by_id["consensus"].fusion_score == pytest.approx(RRF_RANK2 + RRF_RANK1)
    assert by_id["strong"].fusion_score == pytest.approx(RRF_RANK1)


def test_rrf_ranking_contract_is_unchanged():
    """The pre-existing contract: fused ordering, id-dedup, first-sighting
    payload, deterministic ties, and ``score`` carrying the fused value."""
    a, b, c = _searched("a", 0.9), _searched("b", 0.9), _searched("c", 0.9)
    fused = rrf([[a, b, c], [c, _searched("d", 0.9)]])

    assert [x.id for x in fused][0] == "c"
    assert {x.id for x in fused} == {"a", "b", "c", "d"}
    assert fused[0].score == pytest.approx(1 / 63 + RRF_RANK1)

    first = _searched("x", 0.9)
    first.payload["chunk_text"] = "first"
    second = _searched("x", 0.9)
    second.payload["chunk_text"] = "second"
    deduped = rrf([[first], [second]])
    assert len(deduped) == 1 and deduped[0].payload["chunk_text"] == "first"

    assert [x.id for x in rrf([[_searched("b", 0.9)], [_searched("a", 0.9)]])] == ["a", "b"]


def test_rerank_after_fusion_keeps_the_dense_scale(monkeypatch):
    monkeypatch.setattr(reranker_module, "get_settings", lambda: _settings())

    fused = rrf([[_searched("a", 0.72)], [_searched("b", 0.55)]])
    ranked = rerank("solar capacity", fused)
    assert {c.id: c.semantic_score for c in ranked} == {
        "a": pytest.approx(0.72),
        "b": pytest.approx(0.55),
    }


# --------------------------------------------------------------------------- #
# 2/3. A legitimate website candidate survives the floor when a recall leg is on.
# --------------------------------------------------------------------------- #

def _settings(**overrides):
    base = dict(
        retrieval_top_k=6, retrieval_candidate_k=40, website_candidate_k=20,
        prefer_website_enabled=True, multi_query_enabled=False,
        multi_query_paraphrases=2, multi_query_distinct_threshold=0.92,
        keyword_leg_enabled=False,
        corrective_loop_enabled=False, corrective_min_score=CORRECTIVE_MIN,
        rerank_table_boost=0.15, graph_routing_enabled=False,
        # reranker
        reranker_provider="embedding", rerank_score_threshold=0.0,
        rerank_relevance_tolerance=0.03, rerank_volatile_tolerance_multiplier=2.0,
        rerank_substance_ratio=1.5, rerank_max_candidates=40,
        rerank_max_seq_length=0,
        # context builder
        context_token_budget=9000, dedup_cosine_threshold=0.92,
        website_max_slots=2, website_chunk_floor=WEBSITE_FLOOR,
        pdf_max_slots=2, pdf_high_confidence_floor=0.5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _wire(monkeypatch, settings, *, website, pdfs, extra_leg):
    """Wire ``retrieve`` onto stub pulls but the *real* rrf / rerank / context
    builder, so the floors are exercised end to end."""
    from app.retrieval.context import builder as context_builder
    from app.retrieval.search import reranker

    for module in (retriever, reranker, context_builder):
        monkeypatch.setattr(module, "get_settings", lambda s=settings: s)
    monkeypatch.setattr(retriever, "dual_search", lambda *a, **k: [website] + pdfs)
    monkeypatch.setattr(retriever, "search", lambda *a, **k: [website] + pdfs)
    monkeypatch.setattr(retriever, "_observe_in_shadow", lambda *a, **k: None)
    monkeypatch.setattr(retriever, "keyword_search", lambda *a, **k: extra_leg)
    monkeypatch.setattr(
        retriever, "perspectives",
        lambda q, n, **kw: [strategies.Perspective("p1")],
    )
    monkeypatch.setattr(retriever, "perspective_search", lambda q, **k: extra_leg)


QUERY = "what are the impacts of biofuel adoption on rural incomes"


@pytest.mark.parametrize(
    "leg", ["keyword_leg_enabled", "multi_query_enabled"]
)
def test_recall_leg_does_not_starve_the_website_group(monkeypatch, leg):
    """A website chunk at cosine 0.72 clears the 0.30 floor whether or not a
    second retrieval leg was fused in."""
    settings = _settings(**{leg: True})
    website = _searched("w1", 0.72, source_type="website", vector=[1.0, 0.0])
    pdf = _searched("p1", 0.61, source_type="pdf_attachment", vector=[0.0, 1.0])
    # The extra leg ranks the PDF first, so fusion genuinely reorders.
    _wire(monkeypatch, settings, website=website, pdfs=[pdf], extra_leg=[pdf])

    blocks = retriever.retrieve(QUERY, query_vector=[0.1, 0.2])

    sources = [b.payload.get("source_type") for b in blocks]
    assert "website" in sources, f"website chunk lost to the floor ({leg} on)"
    assert sources[0] == "website"  # segregated context leads with website


def test_dense_only_and_fused_admit_the_same_website_block(monkeypatch):
    """The recall leg must not change *which* groups are representable."""
    website = _searched("w1", 0.72, source_type="website", vector=[1.0, 0.0])
    pdf = _searched("p1", 0.61, source_type="pdf_attachment", vector=[0.0, 1.0])

    _wire(monkeypatch, _settings(), website=website, pdfs=[pdf], extra_leg=[])
    dense_only = retriever.retrieve(QUERY, query_vector=[0.1, 0.2])

    _wire(monkeypatch, _settings(keyword_leg_enabled=True),
          website=website, pdfs=[pdf], extra_leg=[pdf])
    fused = retriever.retrieve(QUERY, query_vector=[0.1, 0.2])

    assert [b.payload["document_id"] for b in dense_only] == \
           [b.payload["document_id"] for b in fused]


def test_website_floor_still_rejects_a_genuinely_weak_chunk(monkeypatch):
    """The fix must not defeat the floor: a 0.05-cosine website chunk stays out."""
    settings = _settings(keyword_leg_enabled=True)
    website = _searched("w1", 0.05, source_type="website", vector=[1.0, 0.0])
    pdf = _searched("p1", 0.61, source_type="pdf_attachment", vector=[0.0, 1.0])
    _wire(monkeypatch, settings, website=website, pdfs=[pdf], extra_leg=[pdf])

    blocks = retriever.retrieve(QUERY, query_vector=[0.1, 0.2])
    assert [b.payload.get("source_type") for b in blocks] == ["pdf_attachment"]


# --------------------------------------------------------------------------- #
# 5. The corrective loop is not tripped merely because fusion ran.
# --------------------------------------------------------------------------- #

def test_corrective_loop_not_triggered_by_fusion(monkeypatch):
    settings = _settings(keyword_leg_enabled=True, corrective_loop_enabled=True)
    website = _searched("w1", 0.72, source_type="website", vector=[1.0, 0.0])
    pdf = _searched("p1", 0.61, source_type="pdf_attachment", vector=[0.0, 1.0])
    _wire(monkeypatch, settings, website=website, pdfs=[pdf], extra_leg=[pdf])

    def _boom(*a, **k):
        raise AssertionError(
            "corrective loop ran: the top semantic score was read off the "
            "fused scale, not the dense one"
        )

    monkeypatch.setattr(retriever, "corrective_requery", _boom)
    assert retriever.retrieve(QUERY, query_vector=[0.1, 0.2])


def test_corrective_loop_still_fires_on_genuinely_weak_results(monkeypatch):
    """The loop must keep working for the case it exists for."""
    settings = _settings(keyword_leg_enabled=True, corrective_loop_enabled=True)
    website = _searched("w1", 0.05, source_type="website", vector=[1.0, 0.0])
    pdf = _searched("p1", 0.08, source_type="pdf_attachment", vector=[0.0, 1.0])
    _wire(monkeypatch, settings, website=website, pdfs=[pdf], extra_leg=[pdf])

    calls: list = []
    monkeypatch.setattr(
        retriever, "corrective_requery",
        lambda q, ranked, **k: calls.append(q) or list(ranked),
    )
    retriever.retrieve(QUERY, query_vector=[0.1, 0.2])
    assert calls == [QUERY]


# --------------------------------------------------------------------------- #
# The floor itself, isolated from retrieval.
# --------------------------------------------------------------------------- #

def test_build_context_floor_reads_the_semantic_score(monkeypatch):
    from app.retrieval.context import builder as context_builder

    settings = _settings()
    monkeypatch.setattr(context_builder, "get_settings", lambda: settings)

    admitted = Candidate(
        id="w", score=RRF_RANK1, semantic_score=0.72, vector=[1.0, 0.0],
        payload={"source_type": "website", "chunk_text": "text", "document_id": "w"},
    )
    blocks = build_context([admitted], limit=6, segregate=True)
    assert [b.payload["document_id"] for b in blocks] == ["w"]


# --------------------------------------------------------------------------- #
# The same invariant for the cross-encoder provider.
#
# A cross-encoder emits an unbounded logit, not a cosine: measured on this
# corpus, about +4 for a passage that answers the query and -11 for one that
# does not. Written through raw it would reach every consumer listed in the
# module docstring on the wrong scale — the mirror image of the RRF defect
# above, and the reason `_cross_encoder_semantic` squashes through a sigmoid.
# --------------------------------------------------------------------------- #

def test_cross_encoder_scores_are_squashed_onto_the_cosine_scale(monkeypatch):
    from app.retrieval.search import reranker

    # Logits as `sentence_transformers.CrossEncoder.predict` returns them.
    logits = {"answers": 4.17, "related": -2.0, "irrelevant": -11.17}
    candidates = [_searched(name, 0.60) for name in logits]
    monkeypatch.setattr(
        reranker, "_load_cross_encoder",
        lambda name: SimpleNamespace(
            predict=lambda pairs: [logits[c] for _, c in
                                   zip(pairs, [x.id for x in candidates])]
        ),
    )

    scores = reranker._cross_encoder_semantic("solar capacity", candidates)

    assert all(0.0 <= s <= 1.0 for s in scores), scores
    # Monotone: squashing must not reorder what the model ranked.
    assert scores == sorted(scores, reverse=True)
    # The strong pair clears a cosine-scaled floor; the irrelevant one does not.
    assert scores[0] >= WEBSITE_FLOOR
    assert scores[-1] < WEBSITE_FLOOR
    # A raw logit would have blown straight past a 0..1 threshold in both
    # directions, which is the regression being pinned.
    assert scores[0] != pytest.approx(4.17)


def test_sigmoid_does_not_overflow_on_extreme_logits():
    from app.retrieval.search.reranker import _sigmoid

    assert _sigmoid(1000.0) == pytest.approx(1.0)
    assert _sigmoid(-1000.0) == pytest.approx(0.0)
    assert _sigmoid(0.0) == pytest.approx(0.5)


def test_uncapped_tail_is_held_behind_the_reranked_head(monkeypatch):
    """The cross-encoder cap must not sort scored against unscored candidates.

    Past `rerank_max_candidates` a candidate keeps its cosine, while the head
    carries a normalised cross-encoder relevance. The two are both 0..1 but are
    not calibrated to each other, so ranking them together would let a candidate
    the first stage put last climb over one the reranker actively judged
    irrelevant. The tail is held behind the head instead.
    """
    from app.retrieval.search import reranker

    settings = _settings(reranker_provider="cross_encoder", rerank_max_candidates=2)
    monkeypatch.setattr(reranker, "get_settings", lambda: settings)
    # The head scores badly, the tail's cosine is high: the ordering is only
    # correct if the cap is positional rather than by score.
    monkeypatch.setattr(
        reranker, "_cross_encoder_semantic", lambda q, c: [0.05] * len(c)
    )

    head = [_searched("h1", 0.10), _searched("h2", 0.11)]
    tail = [_searched("t1", 0.99), _searched("t2", 0.98)]

    ranked = rerank("solar capacity", head + tail)

    assert [c.id for c in ranked[:2]] == ["h1", "h2"]
    assert [c.id for c in ranked[2:]] == ["t1", "t2"]
    # The head was rescored; the tail's cosine survived untouched.
    assert ranked[0].semantic_score == pytest.approx(0.05)
    assert ranked[2].semantic_score == pytest.approx(0.99)


def test_the_cap_is_inert_below_its_limit(monkeypatch):
    from app.retrieval.search import reranker

    settings = _settings(reranker_provider="cross_encoder", rerank_max_candidates=40)
    monkeypatch.setattr(reranker, "get_settings", lambda: settings)
    monkeypatch.setattr(
        reranker, "_cross_encoder_semantic",
        lambda q, c: [0.9 if x.id == "b" else 0.2 for x in c],
    )

    ranked = rerank("solar capacity", [_searched("a", 0.71), _searched("b", 0.52)])

    # Every candidate was scored, so the reranker's judgement decides the order.
    assert [c.id for c in ranked] == ["b", "a"]
