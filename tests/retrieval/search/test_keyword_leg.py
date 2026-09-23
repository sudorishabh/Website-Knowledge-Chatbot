"""Unit tests for the full-text keyword leg.

Covers salient-term extraction (quoted phrases, acronyms, years, proper-noun
bigrams, dedup), the MatchText filter on the extra pull, RRF fusion with the
dense pull in ``retrieve()``, and fail-open when the full-text index is
missing. Qdrant is stubbed; no network.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.retrieval import retriever
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.search import strategies


def _cand(id, payload=None):
    return Candidate(id=id, score=0.9, payload=payload or {})


# --------------------------------------------------------------------------- #
# Salient-term extraction.
# --------------------------------------------------------------------------- #

def test_extract_quoted_acronyms_years_bigrams():
    out = strategies.extract_key_terms(
        'what does the "solar mission" report say about GHG output in Tamil Nadu in 2024'
    )
    assert out == ["solar mission", "Tamil Nadu", "GHG", "2024"]


def test_extract_dedupes_case_insensitively():
    out = strategies.extract_key_terms('"solar mission" report of 2024, again 2024')
    assert out == ["solar mission", "2024"]


def test_extract_drops_a_term_subsumed_by_a_longer_one():
    """Terms are OR-ed, and each is itself an AND across its own words. Keeping
    the bare "GHG" alongside "GHG emissions" would therefore match every chunk
    mentioning GHG at all, making the quoted phrase contribute nothing."""
    out = strategies.extract_key_terms('"GHG emissions" and GHG levels in 2024')
    assert out == ["GHG emissions", "2024"]


def test_extract_keeps_the_number_qualifying_an_acronym():
    """"SDG" alone matches every goal; the number is what makes it a lookup."""
    assert "SDG 7" in strategies.extract_key_terms("progress on SDG 7 in rural India")


def test_extract_finds_alphanumeric_codes():
    """PM2.5 matches no word pattern and is blurred by dense embeddings."""
    assert "PM2.5" in strategies.extract_key_terms("PM2.5 annual average concentration")


def test_extract_falls_back_to_content_words():
    """A lowercase title names something exactly without capitalising it.
    Returning None here skipped the leg for the whole exact-term category."""
    out = strategies.extract_key_terms("life cycle analysis of transport modes")
    assert out is not None
    assert "transport" in out and "analysis" in out
    assert "of" not in out  # scaffolding is dropped


def test_extract_fallback_yields_to_precise_terms():
    """Content words must not dilute a query that already named something
    exactly — otherwise ordinary vocabulary outnumbers the real signal."""
    out = strategies.extract_key_terms("what did the 2019 report say about GHG")
    assert out == ["GHG", "2019"]
    assert "report" not in out


def test_extract_none_when_nothing_is_left():
    assert strategies.extract_key_terms("") is None
    assert strategies.extract_key_terms("what are the of and to") is None


# --------------------------------------------------------------------------- #
# keyword_search — MatchText condition and fail-open.
# --------------------------------------------------------------------------- #

def test_keyword_search_builds_matchtext_condition(monkeypatch):
    seen: dict = {}

    def fake_search(query, **kw):
        seen.update(kw)
        return [_cand("k1")]

    monkeypatch.setattr(strategies, "search", fake_search)
    out = strategies.keyword_search(
        "q", ["GHG", "2024"],
        filters=["prior"], query_vector=[0.1], limit=40,
    )

    assert [c.id for c in out] == ["k1"]
    prior, cond = seen["extra_filter"]
    assert prior == "prior"  # existing facet filters preserved
    # One MatchText per term, OR-ed: a single MatchText carrying both words
    # would require one chunk to contain all of them.
    assert [c.match.text for c in cond.should] == ["GHG", "2024"]
    assert all(c.key == "chunk_text" for c in cond.should)
    assert seen["limit"] == 40 and seen["query_vector"] == [0.1]


def test_keyword_search_skips_the_pull_without_terms(monkeypatch):
    def no_search(*a, **kw):
        raise AssertionError("search must not run without terms")

    monkeypatch.setattr(strategies, "search", no_search)
    assert strategies.keyword_search(
        "q", [], filters=None, query_vector=[0.1], limit=40
    ) == []


def test_keyword_search_fails_open_without_index(monkeypatch):
    def boom(query, **kw):
        raise RuntimeError("Index required but not found for chunk_text")

    monkeypatch.setattr(strategies, "search", boom)
    out = strategies.keyword_search(
        "q", ["GHG"],
        filters=None, query_vector=[0.1], limit=40,
    )
    assert out == []


# --------------------------------------------------------------------------- #
# retrieve() wiring — keyword hits fuse with the dense pull.
# --------------------------------------------------------------------------- #

def _settings(**overrides):
    base = dict(
        retrieval_top_k=6, retrieval_candidate_k=40, prefer_website_enabled=False,
        multi_query_enabled=False, multi_query_paraphrases=2, rerank_table_boost=0.15,
        keyword_leg_enabled=True, corrective_loop_enabled=False,
        corrective_min_score=0.2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _wire(monkeypatch, *, settings, base_candidates):
    monkeypatch.setattr(retriever, "get_settings", lambda: settings)
    monkeypatch.setattr(retriever, "search", lambda *a, **k: base_candidates)
    monkeypatch.setattr(retriever, "rerank", lambda q, cands, **kw: cands)
    monkeypatch.setattr(
        retriever, "build_context", lambda ranked, *, limit, temporal=None, question="": list(ranked)
    )


def test_keyword_leg_fuses_with_dense_pull(monkeypatch):
    _wire(monkeypatch, settings=_settings(), base_candidates=[_cand("a"), _cand("b")])
    pulls: list = []
    monkeypatch.setattr(
        retriever, "keyword_search",
        lambda q, terms, **kw: pulls.append(terms) or [_cand("b"), _cand("k")],
    )

    out = retriever.retrieve("what happened at COP in 2024", query_vector=[0.1])

    # The precise pull still happens with exactly the salient terms.
    assert ["COP", "2024"] in pulls
    # A second lexical pull over the query's plain content words now runs beside
    # it (see `extract_content_terms`): the precise pass is skipped whenever any
    # precise pattern matched, which on a corpus with one ubiquitous name left the
    # leg contributing nothing. It is a separate pull on purpose, so the precise
    # terms above are not diluted by ordinary vocabulary.
    assert len(pulls) == 2
    assert ["COP", "2024"] != pulls[-1]
    assert [b.id for b in out][0] == "b"  # consensus candidate leads after RRF
    assert {b.id for b in out} == {"a", "b", "k"}


def test_keyword_leg_empty_hits_keep_dense_only(monkeypatch):
    base = [_cand("a")]
    _wire(monkeypatch, settings=_settings(), base_candidates=base)
    monkeypatch.setattr(retriever, "keyword_search", lambda q, terms, **kw: [])

    out = retriever.retrieve("what happened at COP in 2024", query_vector=[0.1])
    assert [b.id for b in out] == ["a"]


def test_keyword_leg_skipped_without_terms_or_flag(monkeypatch):
    def no_keyword(*a, **kw):
        raise AssertionError("keyword search must not run")

    base = [_cand("a")]

    # Flag on, but the query is pure scaffolding with nothing to match on.
    _wire(monkeypatch, settings=_settings(), base_candidates=base)
    monkeypatch.setattr(retriever, "keyword_search", no_keyword)
    retriever.retrieve("what are the of and to", query_vector=[0.1])

    # Salient terms, but flag off.
    _wire(monkeypatch, settings=_settings(keyword_leg_enabled=False), base_candidates=base)
    monkeypatch.setattr(retriever, "keyword_search", no_keyword)
    retriever.retrieve("what happened at COP in 2024", query_vector=[0.1])
