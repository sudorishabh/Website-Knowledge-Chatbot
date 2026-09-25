"""`retrieve` with priority evidence: stored copies dropped before the context
is built, live sections leading it, and nothing changed without it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.models.context import ContextBlock, is_priority_page
from app.retrieval import retriever
from app.retrieval.search.hybrid_search import Candidate


def _settings():
    return SimpleNamespace(
        retrieval_top_k=6, retrieval_candidate_k=40, prefer_website_enabled=False,
        multi_query_enabled=False, multi_query_paraphrases=2, rerank_table_boost=0.15,
        keyword_leg_enabled=False, corrective_loop_enabled=False, corrective_min_score=0.2,
        context_token_budget=100_000,
    )


def _cand(name, url, source_type="website"):
    return Candidate(id=name, score=0.9, semantic_score=0.9,
                     payload={"title": name, "source_url": url, "source_type": source_type,
                              "chunk_text": f"text of {name}"})


def _priority_block(name):
    return ContextBlock(n=0, text=f"live {name}",
                        payload={"kind": "priority_page", "source_type": "website", "title": name})


class _Evidence:
    """Stands in for PriorityEvidence: records what retrieval asked of it."""

    def __init__(self, blocks, listed=("https://teriin.org/policy",)):
        self.blocks = list(blocks)
        self.listed = set(listed)
        self.dropped: list[str] = []

    def drop_stored_copies(self, candidates, *, top_n):
        kept = []
        for c in candidates:
            if c.payload.get("source_type") == "website" and c.payload["source_url"] in self.listed:
                self.dropped.append(c.payload["title"])
            else:
                kept.append(c)
        return kept

    def merge(self, blocks, *, limit, token_budget):
        merged = [*self.blocks, *blocks][:limit]
        for n, b in enumerate(merged, start=1):
            b.n = n
        return merged


@pytest.fixture
def corpus(monkeypatch):
    built: list[list[str]] = []

    def build_context(ranked, *, limit, temporal=None, question=""):
        built.append([c.payload["title"] for c in ranked])
        return [ContextBlock(n=i, text=c.text, payload=dict(c.payload))
                for i, c in enumerate(ranked[:limit], start=1)]

    monkeypatch.setattr(retriever, "get_settings", _settings)
    monkeypatch.setattr(retriever, "graph_blocks_for", lambda *a, **kw: [])
    monkeypatch.setattr(retriever, "title_search", lambda *a, **kw: [])
    monkeypatch.setattr(retriever, "rerank", lambda q, cands, **kw: list(cands))
    monkeypatch.setattr(retriever, "build_context", build_context)
    results = {"hits": [
        _cand("Policy page", "https://teriin.org/policy"),
        _cand("Policy brief PDF", "https://teriin.org/policy", "pdf_attachment"),
        _cand("A news story", "https://teriin.org/news/1"),
    ]}
    monkeypatch.setattr(retriever, "search", lambda *a, **kw: list(results["hits"]))
    return SimpleNamespace(built=built, results=results)


def _titles(blocks):
    return [b.payload.get("title") for b in blocks]


def test_without_evidence_retrieval_is_unchanged(corpus):
    blocks = retriever.retrieve("policy", n=6, query_vector=[1.0])
    assert _titles(blocks) == ["Policy page", "Policy brief PDF", "A news story"]


def test_the_stored_copy_is_dropped_before_the_context_is_built(corpus):
    evidence = _Evidence([_priority_block("Policy (live)")])
    retriever.retrieve("policy", n=6, query_vector=[1.0], priority=evidence)
    assert evidence.dropped == ["Policy page"]
    assert corpus.built == [["Policy brief PDF", "A news story"]]


def test_live_sections_lead_the_context(corpus):
    evidence = _Evidence([_priority_block("Policy (live)")])
    blocks = retriever.retrieve("policy", n=6, query_vector=[1.0], priority=evidence)
    assert _titles(blocks) == ["Policy (live)", "Policy brief PDF", "A news story"]
    assert is_priority_page(blocks[0].payload)
    assert [b.n for b in blocks] == [1, 2, 3]


def test_live_sections_stand_alone_when_the_corpus_has_nothing(corpus):
    corpus.results["hits"] = [_cand("Policy page", "https://teriin.org/policy")]
    evidence = _Evidence([_priority_block("Policy (live)")])
    blocks = retriever.retrieve("policy", n=6, query_vector=[1.0], priority=evidence)
    assert _titles(blocks) == ["Policy (live)"]


def test_evidence_with_no_blocks_still_drops_stored_copies(corpus):
    evidence = _Evidence([])
    blocks = retriever.retrieve("policy", n=6, query_vector=[1.0], priority=evidence)
    assert _titles(blocks) == ["Policy brief PDF", "A news story"]
