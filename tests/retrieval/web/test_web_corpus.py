"""Internal first after a web search, and the gaps it reveals.

A web result the corpus already holds must be answered from its ingested
chunks — never fetched — and those chunks keep the corpus's own payload with
their web provenance added. The organisation's own pages the corpus lacks are
recorded as gaps; third-party pages are not, and nothing is when the catalog
could not be read.
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.observability import metrics
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.web import corpus
from app.retrieval.web.corpus import internal_candidates, record_gaps, split_by_corpus
from app.retrieval.web.providers import SearchHit

ARTICLE = "https://www.teriin.org/article/bridging-gap"
PROFILE = "https://www.teriin.org/user/15680"
REPORT = "https://www.teriin.org/sites/default/files/2018-08/AQM-SA_0.pdf"
NEWS = "https://news.example/teri-trucks"


def _hit(url: str, rank: int = 1) -> SearchHit:
    return SearchHit(url=url, title=url.rsplit("/", 1)[-1], snippet="", provider="brave",
                     query="q", rank=rank, site=None, published="2026-06-29")


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "web_primary_domains", "teriin.org")
    monkeypatch.setattr(s, "web_blocked_domains", "")
    monkeypatch.setattr(s, "web_allow_third_party", True)
    monkeypatch.setattr(s, "web_gap_log_path", "")
    return s


@pytest.fixture
def catalog(monkeypatch):
    holdings = {ARTICLE: ["node-1", "pdf-2"]}
    monkeypatch.setattr(corpus, "indexed_documents_for_urls",
                        lambda urls: {u: holdings[u] for u in urls if u in holdings})
    return holdings


# --- splitting -------------------------------------------------------------------


def test_hits_the_corpus_holds_are_separated_from_those_it_does_not(catalog):
    split = split_by_corpus([_hit(ARTICLE), _hit(PROFILE, 2), _hit(NEWS, 3)])
    assert split.known
    assert split.ingested == {ARTICLE: ["node-1", "pdf-2"]}
    assert [h.url for h in split.missing] == [PROFILE, NEWS]


def test_an_unreadable_catalog_leaves_every_hit_to_fetch_but_unknown(monkeypatch):
    monkeypatch.setattr(corpus, "indexed_documents_for_urls", lambda urls: None)
    split = split_by_corpus([_hit(ARTICLE), _hit(PROFILE)])
    assert not split.known
    assert [h.url for h in split.missing] == [ARTICLE, PROFILE]


def test_no_hits_asks_the_catalog_nothing(monkeypatch):
    def fail(urls):
        raise AssertionError("no lookup without hits")

    monkeypatch.setattr(corpus, "indexed_documents_for_urls", fail)
    assert split_by_corpus([]).ingested == {}


# --- answering from the corpus ------------------------------------------------


def test_ingested_documents_are_read_from_their_own_chunks(catalog, monkeypatch):
    calls = []

    def scoped(query_vector, ids, *, limit, trace_stage):
        calls.append((list(ids), limit, trace_stage))
        return [Candidate(id="chunk-7", score=0.8, semantic_score=0.8, payload={
            "document_id": "node-1", "chunk_text": "24% of PM10 in winter",
            "source_type": "website", "effective_start_date": "2025-04-09T00:00:00",
        })]

    monkeypatch.setattr(corpus, "search_within_documents", scoped)
    split = split_by_corpus([_hit(ARTICLE, rank=2)])
    (candidate,) = internal_candidates(split, [0.1, 0.2])
    assert calls == [(["node-1", "pdf-2"], 6, "web_corpus_match")]
    # The corpus's own payload, dates included, with how it was reached added.
    assert candidate.payload["source_type"] == "website"
    assert candidate.payload["effective_start_date"] == "2025-04-09T00:00:00"
    assert candidate.payload["retrieval_method"] == "corpus_via_web_search:brave"
    assert candidate.payload["search_rank"] == 2
    assert candidate.id == "chunk-7"


def test_nothing_ingested_means_no_corpus_read(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("no scoped read without matches")

    monkeypatch.setattr(corpus, "search_within_documents", fail)
    assert internal_candidates(corpus.CorpusSplit(), [0.1]) == []


def test_the_corpus_read_is_bounded(monkeypatch):
    split = corpus.CorpusSplit(ingested={f"u{i}": [f"d{i}"] for i in range(20)},
                               hits={f"u{i}": _hit(f"https://teriin.org/{i}") for i in range(20)})
    limits = []

    def scoped(query_vector, ids, *, limit, trace_stage):
        limits.append(limit)
        return []

    monkeypatch.setattr(corpus, "search_within_documents", scoped)
    internal_candidates(split, [0.1])
    assert limits == [corpus._MAX_MATCH_CHUNKS]


# --- gaps ---------------------------------------------------------------------------


def test_the_organisations_own_missing_pages_are_gaps_and_third_party_are_not(catalog):
    before = metrics.events().get("web_corpus_gap", {}).get("counts", {})
    split = split_by_corpus([_hit(ARTICLE), _hit(PROFILE), _hit(REPORT), _hit(NEWS)])
    records = record_gaps(split, question="Who is Mr Sayanta Ghosh?")
    assert [r["url"] for r in records] == [PROFILE, REPORT]
    assert records[0]["question"] == "Who is Mr Sayanta Ghosh?"
    after = metrics.events()["web_corpus_gap"]["counts"]
    assert after.get("page", 0) - before.get("page", 0) == 1
    assert after.get("pdf", 0) - before.get("pdf", 0) == 1


def test_no_gaps_are_recorded_when_the_catalog_could_not_be_read(monkeypatch):
    monkeypatch.setattr(corpus, "indexed_documents_for_urls", lambda urls: None)
    assert record_gaps(split_by_corpus([_hit(PROFILE)]), question="q") == []


def test_gaps_are_appended_to_the_configured_file(catalog, settings, monkeypatch, tmp_path):
    path = tmp_path / "gaps" / "web_gaps.jsonl"
    monkeypatch.setattr(settings, "web_gap_log_path", str(path))
    record_gaps(split_by_corpus([_hit(PROFILE)]), question="first")
    record_gaps(split_by_corpus([_hit(REPORT)]), question="second")
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(r["url"], r["question"]) for r in lines] == [(PROFILE, "first"), (REPORT, "second")]


def test_an_unwritable_gap_file_costs_only_the_file(catalog, settings, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "web_gap_log_path", str(blocker / "gaps.jsonl"))
    assert record_gaps(split_by_corpus([_hit(PROFILE)]), question="q")   # still returned
