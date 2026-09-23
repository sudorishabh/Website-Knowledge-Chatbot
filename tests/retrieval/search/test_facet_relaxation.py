"""Unit tests for facet-filter relaxation in retrieve().

LLM-extracted facet filters (theme / author / source_type) are applied as hard
AND conditions. When they lift terms straight out of the question — a title query
parsed into theme="SDG 7", author="TERI" — those literals rarely equal the stored
metadata and their intersection can be empty even when the corpus plainly answers
the question. retrieve() must then retry once without the facets rather than
returning nothing (which the pipeline turns into a blanket refusal). Relaxation
is precision-preserving: it fires only on a total miss.

A date scope is the exception: it is the user's own constraint rather than a
guess at the corpus's labelling, so it is carried into the retry and an empty
window stays empty. Qdrant is stubbed; no network.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from qdrant_client.models import DatetimeRange, FieldCondition

from app.retrieval import retriever
from app.retrieval.search.hybrid_search import Candidate


def _cand(id, payload=None):
    return Candidate(id=id, score=0.9, payload=payload or {})


def _date_cond(year=2023):
    """The condition `_facet_filters` builds for a single-year scope."""
    return FieldCondition(
        key="effective_start_date",
        range=DatetimeRange(
            gte=datetime(year, 1, 1, tzinfo=timezone.utc),
            lt=datetime(year + 1, 1, 1, tzinfo=timezone.utc),
        ),
    )


def _settings(**overrides):
    base = dict(
        retrieval_top_k=6, retrieval_candidate_k=40, prefer_website_enabled=False,
        multi_query_enabled=False, multi_query_paraphrases=2, rerank_table_boost=0.15,
        keyword_leg_enabled=False, corrective_loop_enabled=False,
        corrective_min_score=0.2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _wire(monkeypatch, *, settings, search_fn):
    monkeypatch.setattr(retriever, "get_settings", lambda: settings)
    monkeypatch.setattr(retriever, "search", search_fn)
    monkeypatch.setattr(retriever, "rerank", lambda q, cands, **kw: list(cands))
    monkeypatch.setattr(
        retriever, "build_context", lambda ranked, *, limit, temporal=None, question="": list(ranked)
    )


def test_zero_under_facets_retries_without_them(monkeypatch):
    calls: list = []

    def fake_search(*a, **k):
        calls.append(k.get("extra_filter"))
        # Empty while facets are applied; the corpus answer only surfaces once
        # they are dropped.
        return [] if k.get("extra_filter") else [_cand("a"), _cand("b")]

    _wire(monkeypatch, settings=_settings(), search_fn=fake_search)
    out = retriever.retrieve("a title query", query_vector=[0.1], filters=["facet"])

    assert calls == [["facet"], None]  # faceted pull, then one relaxed retry
    assert [b.id for b in out] == ["a", "b"]


def test_facet_hits_are_not_relaxed(monkeypatch):
    calls: list = []

    def fake_search(*a, **k):
        calls.append(k.get("extra_filter"))
        return [_cand("scoped")]

    _wire(monkeypatch, settings=_settings(), search_fn=fake_search)
    out = retriever.retrieve("scoped query", query_vector=[0.1], filters=["facet"])

    assert calls == [["facet"]]  # no relaxed retry — the facet pull found matches
    assert [b.id for b in out] == ["scoped"]


def test_no_facets_no_relaxation(monkeypatch):
    calls: list = []

    def fake_search(*a, **k):
        calls.append(k.get("extra_filter"))
        return []

    _wire(monkeypatch, settings=_settings(), search_fn=fake_search)
    out = retriever.retrieve("unanswerable", query_vector=[0.1])

    assert calls == [None]  # a genuine empty pull is not retried
    assert out == []


def test_relaxation_keeps_the_date_scope(monkeypatch):
    """The facets go, the period stays: a 2023 question must not be answered out
    of 2019 just because theme/author matched nothing."""
    date = _date_cond(2023)
    calls: list = []

    def fake_search(*a, **k):
        applied = k.get("extra_filter")
        calls.append(applied)
        # Empty while the theme facet is applied; in-period content exists.
        return [] if applied and "facet" in applied else [_cand("in_period")]

    _wire(monkeypatch, settings=_settings(), search_fn=fake_search)
    out = retriever.retrieve(
        "reports from 2023", query_vector=[0.1], filters=["facet", date]
    )

    assert calls == [["facet", date], [date]]  # retried inside the window
    assert [b.id for b in out] == ["in_period"]


def test_empty_window_is_not_widened(monkeypatch):
    """Nothing published in the window: the retry stays scoped and returns empty,
    leaving the pipeline to refuse rather than answering about other years."""
    date = _date_cond(2023)
    calls: list = []

    def fake_search(*a, **k):
        applied = k.get("extra_filter")
        calls.append(applied)
        # Out-of-period content exists, but only once the date bound is dropped.
        return [] if applied else [_cand("wrong_year")]

    _wire(monkeypatch, settings=_settings(), search_fn=fake_search)
    out = retriever.retrieve(
        "reports from 2023", query_vector=[0.1], filters=["facet", date]
    )

    assert calls == [["facet", date], [date]]  # never retried unfiltered
    assert out == []


def test_date_only_scope_costs_no_retry(monkeypatch):
    """With no facets to drop, a missed date scope adds no second pull — the
    retry would only repeat the query that just came back empty."""
    date = _date_cond(2023)
    calls: list = []

    def fake_search(*a, **k):
        calls.append(k.get("extra_filter"))
        return []

    _wire(monkeypatch, settings=_settings(), search_fn=fake_search)
    out = retriever.retrieve("reports from 2023", query_vector=[0.1], filters=[date])

    assert calls == [[date]]  # one pull, not two
    assert out == []
