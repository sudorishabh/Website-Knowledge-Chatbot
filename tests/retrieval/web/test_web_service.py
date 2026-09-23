"""Web retrieval end to end, with every external edge scripted.

The orchestration is what these tests pin: the organisation's site before the
open web, and the open web only as a fallback for a question about the
organisation; internal first for anything the corpus holds; the organisation's
pages fetched first and within limits; the time budget; the page cache; one copy
of a document reached twice; and every failure costing only its own evidence.
"""
from __future__ import annotations

import time

import pytest

from app.config import get_settings
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.web import cache, corpus, passages, providers, service
from app.retrieval.web.extract import WebBlock, WebDocument
from app.retrieval.web.fetch import FetchedPage, FetchRefused
from app.retrieval.web.planner import plan
from app.retrieval.web.providers import SearchHit

PROFILE = "https://www.teriin.org/user/15680"
ARTICLE = "https://www.teriin.org/article/bridging-gap"
PAPER = "https://www.teriin.org/research-paper/gis-forest-assam"
NEWS = "https://news.example/sayanta-ghosh"

T1 = "Who is Mr Sayanta Ghosh at TERI, and how many publications does he have?"


class _Provider:
    name = "fake"


def _hit(url: str, rank: int, site: str | None = "teriin.org") -> SearchHit:
    return SearchHit(url=url, title=url.rsplit("/", 1)[-1], snippet="", provider="fake",
                     query="q", rank=rank, site=site)


def _document(url: str, text: str, *, canonical: str | None = None) -> WebDocument:
    return WebDocument(url=url, final_url=url, canonical_url=canonical or url,
                       domain="teriin.org", kind="html",
                       fetched_at="2026-09-23T06:00:00+00:00", title=url.rsplit("/", 1)[-1],
                       blocks=[WebBlock(text=text)])


@pytest.fixture(autouse=True)
def world(monkeypatch):
    """A provider, an empty corpus, a page cache and a fake embedder."""
    s = get_settings()
    for name, value in {
        "web_primary_domains": "teriin.org", "web_blocked_domains": "",
        "web_allow_third_party": True, "web_max_fetches": 4, "web_budget_seconds": 5.0,
        "web_passages_per_document": 3, "web_max_candidates": 12, "web_gap_log_path": "",
        "web_organisation_names": "TERI, The Energy and Resources Institute",
    }.items():
        monkeypatch.setattr(s, name, value)
    monkeypatch.setattr(providers, "get_provider", lambda: _Provider())
    monkeypatch.setattr(corpus, "indexed_documents_for_urls", lambda urls: {})
    monkeypatch.setattr(corpus, "search_within_documents", lambda *a, **k: [])
    monkeypatch.setattr(cache, "get_redis", lambda: None)
    store = cache.TTLCache("page", ttl=3600)
    monkeypatch.setattr(service, "page_cache", lambda: store)
    monkeypatch.setattr(passages, "_default_embedder",
                        lambda texts: [[1.0, float(len(t) % 7)] for t in texts])
    return s


def _searches(monkeypatch, results: dict[tuple[str, str | None], list[SearchHit]]) -> list:
    calls: list = []

    def search(text, *, site=None, limit=None):
        calls.append((text, site))
        return list(results.get((text, site), []))

    monkeypatch.setattr(providers, "search", search)
    return calls


def _pages(monkeypatch, texts: dict[str, str], *, refuse: dict[str, str] | None = None,
           slow: dict[str, float] | None = None, canonical: dict[str, str] | None = None) -> list:
    fetched: list = []

    def fake_fetch(url):
        fetched.append(url)
        if url in (slow or {}):
            time.sleep(slow[url])
        if url in (refuse or {}):
            raise FetchRefused(url, refuse[url])
        return FetchedPage(url=url, final_url=url, kind="html", content=b"", content_type="text/html",
                           status=200, fetched_at="2026-09-23T06:00:00+00:00")

    monkeypatch.setattr(service, "fetch", fake_fetch)
    monkeypatch.setattr(service, "extract", lambda page: _document(
        page.url, texts[page.url], canonical=(canonical or {}).get(page.url)))
    return fetched


PROFILE_TEXT = ("Mr Sayanta Ghosh is an Associate Fellow and Area Convener. He has more than "
                "25 publications to his credit in reputed journals.")


# --- no provider ---------------------------------------------------------------


def test_without_a_provider_nothing_is_searched(monkeypatch):
    monkeypatch.setattr(providers, "get_provider", lambda: None)
    calls = _searches(monkeypatch, {})
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert outcome.skipped == "no_provider" and outcome.candidates == []
    assert calls == []


# --- search order ----------------------------------------------------------------


def test_enough_from_the_organisations_site_means_no_open_web(monkeypatch):
    calls = _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                                    [_hit(PROFILE, 1), _hit(PAPER, 2)]})
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT, PAPER: "GIS forest vulnerability in Assam, "
                                                      "a study by Sayanta Ghosh and others."})
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert calls == [("Sayanta Ghosh profile", "teriin.org")]
    assert [q["purpose"] for q in outcome.queries] == ["primary"]


def test_the_open_web_is_the_fallback_when_the_site_comes_back_short(monkeypatch):
    calls = _searches(monkeypatch, {
        ("Sayanta Ghosh profile", "teriin.org"): [_hit(PROFILE, 1)],
        ("TERI Sayanta Ghosh profile", None): [_hit(NEWS, 1, site=None)],
    })
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT, NEWS: "A news story about Sayanta Ghosh "
                                                      "and his GIS work at TERI."})
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert calls == [("Sayanta Ghosh profile", "teriin.org"),
                     ("TERI Sayanta Ghosh profile", None)]
    assert outcome.hits == 2


def test_a_question_not_about_the_organisation_searches_both_at_once(monkeypatch):
    calls = _searches(monkeypatch, {})
    service.gather(plan("What is PM2.5?"), [1.0, 0.0])
    assert {site for _, site in calls} == {"teriin.org", None}


# --- internal first ------------------------------------------------------------


def test_a_result_the_corpus_holds_is_read_from_it_and_not_fetched(monkeypatch):
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                            [_hit(ARTICLE, 1), _hit(PROFILE, 2)]})
    monkeypatch.setattr(corpus, "indexed_documents_for_urls",
                        lambda urls: {ARTICLE: ["node-1"]})
    monkeypatch.setattr(corpus, "search_within_documents", lambda *a, **k: [
        Candidate(id="chunk-1", score=0.8, semantic_score=0.8,
                  payload={"document_id": "node-1", "chunk_text": "corpus text",
                           "source_type": "website"})])
    fetched = _pages(monkeypatch, {PROFILE: PROFILE_TEXT})
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert fetched == [PROFILE]                        # the ingested article never
    assert outcome.in_corpus == {ARTICLE: ["node-1"]}
    assert outcome.corpus_passages == 1
    kinds = {c.payload["source_type"] for c in outcome.candidates}
    assert kinds == {"website", "web"}
    assert outcome.gaps == [PROFILE]                   # the missing TERI page


# --- which pages are read ------------------------------------------------------


def test_the_organisations_pages_are_fetched_first_within_the_limit(world, monkeypatch):
    monkeypatch.setattr(world, "web_max_fetches", 1)
    question = plan("What is PM2.5?")
    by_site = {q.site: q.text for q in question.queries}
    # The open web ranks the third-party page first; the TERI page still wins.
    _searches(monkeypatch, {(by_site[None], None): [_hit(NEWS, 1, site=None)],
                            (by_site["teriin.org"], "teriin.org"): [_hit(PROFILE, 1)]})
    fetched = _pages(monkeypatch, {PROFILE: PROFILE_TEXT, NEWS: "x " * 20})
    outcome = service.gather(question, [1.0, 0.0])
    assert fetched == [PROFILE]
    assert outcome.not_read == {NEWS: service.NOT_READ_LIMIT}


def test_a_refused_or_unreadable_page_is_recorded_and_skipped(monkeypatch):
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                            [_hit(PROFILE, 1), _hit(PAPER, 2)]})
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT}, refuse={PAPER: "robots_disallowed"})
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert outcome.fetched == [PROFILE]
    assert outcome.not_read == {PAPER: "robots_disallowed"}


def test_an_extraction_failure_is_contained(monkeypatch):
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                            [_hit(PROFILE, 1), _hit(PAPER, 2)]})
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT})          # no text for PAPER: extract raises
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert outcome.not_read[PAPER] == service.NOT_READ_EXTRACTION
    assert outcome.web_passages >= 1


def test_a_page_still_loading_when_the_budget_runs_out_is_dropped(monkeypatch):
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                            [_hit(PROFILE, 1), _hit(PAPER, 2)]})
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT, PAPER: "slow " * 20}, slow={PAPER: 1.5})
    started = time.monotonic()
    outcome = service.gather(plan(T1), [1.0, 0.0], budget_seconds=0.5)
    assert time.monotonic() - started < 1.4               # did not wait for the slow page
    assert outcome.budget_exhausted
    assert outcome.not_read[PAPER] == service.NOT_READ_BUDGET
    assert outcome.fetched == [PROFILE]


def test_a_page_read_once_is_served_from_the_cache(monkeypatch):
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                            [_hit(PROFILE, 1), _hit(PAPER, 2)]})
    fetched = _pages(monkeypatch, {PROFILE: PROFILE_TEXT, PAPER: "GIS " * 20})
    service.gather(plan(T1), [1.0, 0.0])
    service.gather(plan(T1), [1.0, 0.0])
    assert sorted(fetched) == sorted([PROFILE, PAPER])   # each fetched once


def test_one_document_reached_through_two_results_is_read_once(monkeypatch):
    alias = "https://www.teriin.org/profile/sayanta-ghosh"
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                            [_hit(PROFILE, 1), _hit(alias, 2)]})
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT, alias: PROFILE_TEXT},
           canonical={alias: PROFILE})
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert outcome.fetched == [PROFILE]
    assert outcome.not_read == {alias: service.NOT_READ_DUPLICATE}


# --- failures and the record ---------------------------------------------------


def test_an_embedding_failure_keeps_the_corpus_candidates(monkeypatch):
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                            [_hit(ARTICLE, 1), _hit(PROFILE, 2)]})
    monkeypatch.setattr(corpus, "indexed_documents_for_urls", lambda urls: {ARTICLE: ["n"]})
    monkeypatch.setattr(corpus, "search_within_documents", lambda *a, **k: [
        Candidate(id="c", score=0.8, semantic_score=0.8,
                  payload={"document_id": "n", "chunk_text": "t", "source_type": "website"})])
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT})

    def broken(texts):
        raise RuntimeError("embedding service down")

    monkeypatch.setattr(passages, "_default_embedder", broken)
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert [c.id for c in outcome.candidates] == ["c"]
    assert outcome.web_passages == 0


def test_gather_never_raises(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(providers, "search", broken)
    outcome = service.gather(plan(T1), [1.0, 0.0])
    assert outcome.candidates == [] and outcome.skipped == "error"


def test_web_candidates_carry_the_web_provenance(monkeypatch):
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"): [_hit(PROFILE, 1)]})
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT})
    (candidate, *_) = service.gather(plan(T1), [1.0, 0.0]).candidates
    assert candidate.payload["source_type"] == "web"
    assert candidate.payload["url"] == PROFILE
    assert candidate.payload["retrieval_method"] == "web_search:fake"


def test_the_trace_holds_one_entry_explaining_the_whole(monkeypatch, world, tmp_path):
    import json

    from app.observability import retrieval_log

    for name, value in {"is_retrieval_log": True, "retrieval_log_dir": str(tmp_path),
                        "retrieval_log_report": False, "retrieval_log_summary": False}.items():
        monkeypatch.setattr(world, name, value, raising=False)
    _searches(monkeypatch, {("Sayanta Ghosh profile", "teriin.org"):
                            [_hit(PROFILE, 1), _hit(PAPER, 2)]})
    _pages(monkeypatch, {PROFILE: PROFILE_TEXT}, refuse={PAPER: "robots_disallowed"})
    with retrieval_log.query_log(T1, entrypoint="test"):
        service.gather(plan(T1), [1.0, 0.0])
    (trace_file,) = [p for p in tmp_path.rglob("trace.json") if "errors" not in p.parts]
    web = json.loads(trace_file.read_text(encoding="utf-8"))["notes"]["web"]
    assert web["provider"] == "fake"
    assert web["queries"] == [{"text": "Sayanta Ghosh profile", "site": "teriin.org",
                               "purpose": "primary", "hits": 2}]
    assert web["fetched"] == [PROFILE]
    assert web["not_read"] == {PAPER: "robots_disallowed"}
    assert web["gaps"] == [PROFILE, PAPER]
