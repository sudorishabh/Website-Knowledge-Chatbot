"""Web evidence in the one ranking: its authority tier, and merging without rescoring.

Web passages join the corpus's candidates in the same banded ranking, so the
invariants that ranking already guarantees must hold for them too: relevance
first, always; authority only between candidates relevance called equivalent.
Within a band a corpus source leads the organisation's own web page, which leads
a third-party page. And merging new candidates into an already-ranked set must
not ask the provider to score the ranked ones again.

The `embedding` provider is used unless a test says otherwise, so a candidate's
own score is its relevance — no model, no network.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.retrieval.search import reranker
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.search.reranker import derived_authority, merge_ranked, rerank


@pytest.fixture
def settings(monkeypatch):
    cfg = SimpleNamespace(
        reranker_provider="embedding", rerank_score_threshold=0.0,
        rerank_relevance_tolerance=0.03, rerank_volatile_tolerance_multiplier=2.0,
        rerank_substance_ratio=1.5, rerank_max_candidates=40, rerank_max_seq_length=0,
    )
    monkeypatch.setattr(reranker, "get_settings", lambda: cfg)
    return cfg


def _corpus(cid: str, score: float, bundle: str = "policy_brief",
            source_type: str = "website") -> Candidate:
    return Candidate(id=cid, score=score, semantic_score=score, vector=[0.1],
                     payload={"chunk_text": "x" * 400, "bundle": bundle,
                              "source_type": source_type})


def _web(cid: str, score: float, *, primary: bool) -> Candidate:
    return Candidate(id=cid, score=score, semantic_score=score, vector=[0.1],
                     payload={"chunk_text": "x" * 400, "source_type": "web",
                              "is_primary_source": primary})


def _ids(candidates) -> list[str]:
    return [c.id for c in candidates]


# --- the authority tier --------------------------------------------------------


def test_web_sources_sit_below_the_corpus_primary_content():
    corpus_primary = derived_authority({"source_type": "website", "bundle": "policy_brief"})
    web_primary = derived_authority({"source_type": "web", "is_primary_source": True})
    third_party = derived_authority({"source_type": "web", "is_primary_source": False})
    attachment = derived_authority({"source_type": "pdf_attachment", "bundle": "news"})
    assert corpus_primary > web_primary > attachment > third_party
    # Each step is wide enough to be its own authority band.
    assert corpus_primary - web_primary > reranker._AUTHORITY_TOLERANCE
    assert attachment - third_party > reranker._AUTHORITY_TOLERANCE


def test_an_explicit_authority_still_wins_for_a_web_page():
    assert reranker._authority_scores([Candidate(
        id="w", score=0.5, payload={"source_type": "web", "source_authority": 0.95})
    ]) == [0.95]


def test_within_a_band_corpus_then_own_site_then_third_party(settings):
    ranked = rerank("q", [_web("third", 0.80, primary=False),
                          _web("own", 0.80, primary=True),
                          _corpus("corpus", 0.80)])
    assert _ids(ranked) == ["corpus", "own", "third"]


def test_a_clearly_more_relevant_web_page_still_leads(settings):
    # The profile answers "who is X"; the corpus passage only mentions them.
    ranked = rerank("q", [_corpus("mention", 0.34, bundle="page"),
                          _web("profile", 0.72, primary=True)])
    assert _ids(ranked) == ["profile", "mention"]


def test_even_a_third_party_page_leads_when_it_is_the_only_relevant_one(settings):
    ranked = rerank("q", [_corpus("off-topic", 0.30), _web("answer", 0.70, primary=False)])
    assert _ids(ranked)[0] == "answer"


# --- rescoring -----------------------------------------------------------------


@pytest.fixture
def cross_encoder(settings, monkeypatch):
    settings.reranker_provider = "cross_encoder"
    calls: list[list[str]] = []

    def score(query, candidates):
        calls.append([c.id for c in candidates])
        return [0.9 if c.id.startswith("web") else 0.5 for c in candidates]

    monkeypatch.setattr(reranker, "_cross_encoder_semantic", score)
    return calls


def test_rescore_false_bands_on_the_scores_candidates_carry(cross_encoder):
    ranked = rerank("q", [_corpus("a", 0.2), _corpus("b", 0.9)], rescore=False)
    assert _ids(ranked) == ["b", "a"]
    assert cross_encoder == []                    # the provider was not asked


def test_merging_scores_only_the_new_candidates(cross_encoder):
    already = rerank("q", [_corpus("c1", 0.0), _corpus("c2", 0.0)])
    cross_encoder.clear()
    merged = merge_ranked("q", already, [_web("web1", 0.0, primary=True)])
    assert cross_encoder == [["web1"]]
    assert _ids(merged) == ["web1", "c1", "c2"]   # 0.9 from the provider vs 0.5


def test_a_chunk_reached_twice_is_one_candidate(settings):
    already = rerank("q", [_corpus("c1", 0.8)])
    assert merge_ranked("q", already, [_corpus("c1", 0.8)]) == already


def test_nothing_new_leaves_the_ranking_as_it_was(settings):
    already = rerank("q", [_corpus("c1", 0.8), _corpus("c2", 0.5)])
    assert merge_ranked("q", already, []) == already


def test_the_unscored_tail_stays_behind_everything_scored(cross_encoder, settings):
    settings.rerank_max_candidates = 2
    # Four corpus candidates: the provider scores two, the other two form the tail.
    already = rerank("q", [_corpus(f"c{i}", 0.99) for i in range(4)])
    tail = _ids(already)[2:]
    merged = merge_ranked("q", already, [_web("web1", 0.0, primary=True)])
    assert _ids(merged)[-2:] == tail
    assert _ids(merged)[0] == "web1"
