"""Unit tests for the scoped-summary route.

Covers scope-filter derivation from the unified analysis, the map-batch
packing math, direct-vs-map-reduce path selection, fall-through on empty or
unresolvable scopes, and the result/citation shape. Catalog, Qdrant, and LLM
calls are all stubbed; no network.
"""

from __future__ import annotations

from datetime import datetime

from app.pipeline import summarize as sm
from app.retrieval.understanding import query_processor as qp


def _analysis(**kw):
    kw.setdefault("search_query", "summarize the Climate theme")
    kw.setdefault("intent", "scoped_summary")
    return qp.QueryAnalysis(**kw)


def _payload(doc_id, text="Some content.", title=None, published="2024-03-15T00:00:00"):
    return {
        "document_id": doc_id,
        "title": title or f"Title {doc_id}",
        "source_url": f"https://t/{doc_id}",
        "effective_start_date": published,
        "chunk_text": text,
    }


# --------------------------------------------------------------------------- #
# Scope filters — what the analysis translates to in catalog kwargs.
# --------------------------------------------------------------------------- #

def test_scope_filters_theme_is_canonicalized_by_name(monkeypatch):
    """The scope carries a single canonical name, which SQL matches exactly."""
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [{"theme": "Climate Change", "theme_type": "primary",
                       "parent": None, "theme_group": "main", "documents": 3}],
    )
    filters = sm._scope_filters(_analysis(theme="climate"))
    assert filters == {"theme": "Climate Change"}


def test_scope_filters_unmatched_theme_keeps_the_name_as_typed(monkeypatch):
    """A summary scope is soft: an unrecognized theme still narrows the set
    rather than being dropped."""
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    filters = sm._scope_filters(_analysis(theme="Oceans"))
    assert filters == {"theme": "Oceans"}


def test_scope_filters_unknown_bundle_dropped_dates_kept(monkeypatch):
    filters = sm._scope_filters(
        _analysis(bundle="widgets", date_from="2024-01-01", date_to="2025-01-01")
    )
    # A summary scope is soft: an unknown bundle must not zero the set.
    assert filters == {
        "effective_from": datetime(2024, 1, 1),
        "effective_to": datetime(2025, 1, 1),
    }


def test_scope_filters_bundle_author_title(monkeypatch):
    filters = sm._scope_filters(
        _analysis(bundle="paper", author="Sharma", title_contains="solar")
    )
    assert filters == {
        "bundle": "research_papers",
        "author": "Sharma",
        "title_contains": "solar",
    }


def test_scope_filters_no_scope_returns_none():
    assert sm._scope_filters(_analysis()) is None


# --------------------------------------------------------------------------- #
# Map-batch packing.
# --------------------------------------------------------------------------- #

def test_batch_documents_packs_by_token_budget():
    docs = [
        sm._Doc(document_id=f"d{i}", title="t", url=None, effective_date="", text="x" * 8000)
        for i in range(5)
    ]  # ~2050 est. tokens each -> 2 per 6k batch
    batches = sm._batch_documents(docs)
    assert [len(b) for b in batches] == [2, 2, 1]
    assert [d.document_id for b in batches for d in b] == [f"d{i}" for i in range(5)]


def test_batch_documents_oversized_doc_gets_own_batch():
    docs = [
        sm._Doc(document_id="big", title="t", url=None, effective_date="", text="x" * 60000),
        sm._Doc(document_id="small", title="t", url=None, effective_date="", text="x" * 100),
    ]
    assert [[d.document_id for d in b] for b in sm._batch_documents(docs)] == [
        ["big"], ["small"]
    ]


# --------------------------------------------------------------------------- #
# summarize_scope — path selection, fall-through, result shape.
# --------------------------------------------------------------------------- #

def _stub_scope(monkeypatch, ids, payloads, abstracts=None):
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr(
        sm.catalog, "document_ids_in_scope", lambda **kw: ids
    )
    monkeypatch.setattr(
        sm.catalog, "abstracts_for", lambda i: dict(abstracts or {})
    )
    monkeypatch.setattr(
        sm.scoped_retrieval, "lead_parents", lambda i, **kw: payloads
    )


def _abstract(doc_id, text="A whole-document abstract.", published="2024-03-15"):
    return {
        "abstract": text,
        "title": f"Title {doc_id}",
        "url": f"https://t/{doc_id}",
        "effective_start_date": published,
    }


def test_a_scope_that_fits_one_call_skips_the_map_stage(monkeypatch):
    ids = ["d1", "d2"]
    _stub_scope(monkeypatch, ids, {i: _payload(i) for i in ids})
    calls = []
    monkeypatch.setattr(
        sm, "_summarize_direct", lambda q, docs: calls.append(len(docs)) or "Overview [1][2]."
    )

    def no_map(q, docs):
        raise AssertionError("map-reduce must not run for a scope that fits")

    monkeypatch.setattr(sm, "_summarize_map_reduce", no_map)

    out = sm.summarize_scope(_analysis(theme="Climate"))

    assert calls == [2]
    assert out["answer"] == "Overview [1][2]."
    assert out["intent"] == "scoped_summary"
    assert out["used_chunks"] == 2
    assert [c["document_id"] for c in out["citations"]] == ["d1", "d2"]
    assert out["citations"][0]["type"] == "website"
    assert out["citations"][0]["url"] == "https://t/d1"


def test_a_scope_too_large_for_one_call_maps_and_reduces(monkeypatch):
    """Selection is by total size, not document count: eight lead parent chunks
    do not fit one call, which is exactly what the map stage is for."""
    ids = [f"d{i}" for i in range(8)]
    _stub_scope(
        monkeypatch, ids, {i: _payload(i, text="x" * 8000) for i in ids}
    )

    def no_direct(q, docs):
        raise AssertionError("direct path must not run for an oversized scope")

    monkeypatch.setattr(sm, "_summarize_direct", no_direct)
    monkeypatch.setattr(sm, "_summarize_map_reduce", lambda q, docs: "Thematic summary.")

    out = sm.summarize_scope(_analysis(theme="Climate"))
    assert out["answer"] == "Thematic summary."
    assert out["used_chunks"] == 8


# --------------------------------------------------------------------------- #
# Ingest-time abstracts — preferred over the lead-chunk stand-in.
# --------------------------------------------------------------------------- #

def test_abstracts_are_preferred_and_skip_the_qdrant_fetch(monkeypatch):
    ids = ["d1", "d2"]
    _stub_scope(monkeypatch, ids, {}, abstracts={i: _abstract(i) for i in ids})

    def no_lead(i, **kw):
        raise AssertionError("no lead-chunk fetch when every document is enriched")

    monkeypatch.setattr(sm.scoped_retrieval, "lead_parents", no_lead)
    seen: list[list[str]] = []
    monkeypatch.setattr(
        sm, "_summarize_direct", lambda q, docs: seen.append([d.text for d in docs]) or "S."
    )

    out = sm.summarize_scope(_analysis(theme="Climate"))

    assert seen == [["A whole-document abstract."] * 2]
    assert out["citations"][0]["title"] == "Title d1"


def test_unenriched_documents_fall_back_to_the_lead_chunk(monkeypatch):
    """A half-enriched corpus must still summarize the whole scope."""
    ids = ["d1", "d2"]
    asked: list[list[str]] = []
    _stub_scope(monkeypatch, ids, {}, abstracts={"d1": _abstract("d1")})
    monkeypatch.setattr(
        sm.scoped_retrieval, "lead_parents",
        lambda i, **kw: asked.append(list(i)) or {"d2": _payload("d2", text="Lead chunk.")},
    )
    seen: list[list[str]] = []
    monkeypatch.setattr(
        sm, "_summarize_direct", lambda q, docs: seen.append([d.text for d in docs]) or "S."
    )

    out = sm.summarize_scope(_analysis(theme="Climate"))

    # Only the un-enriched id is fetched from Qdrant, and catalog order holds.
    assert asked == [["d2"]]
    assert seen == [["A whole-document abstract.", "Lead chunk."]]
    assert [c["document_id"] for c in out["citations"]] == ["d1", "d2"]


def test_a_blank_abstract_is_treated_as_absent_by_the_catalog_read():
    """Filtered in abstracts_for, so a blank abstract can never be preferred
    over the document's own lead chunk and then dropped for having no text —
    which would silently remove the document from the scope."""
    rows = [
        {"document_id": "d1", "abstract": "   ", "title": "T", "url": None,
         "effective_start_date": None},
        {"document_id": "d2", "abstract": "Real.", "title": "T", "url": None,
         "effective_start_date": None},
    ]
    kept = {r["document_id"] for r in rows if (r["abstract"] or "").strip()}
    assert kept == {"d2"}


def test_a_document_with_a_blank_abstract_still_uses_its_lead_chunk(monkeypatch):
    ids = ["d1"]
    _stub_scope(
        monkeypatch, ids,
        {"d1": _payload("d1", text="Lead chunk.")},
        abstracts={},  # abstracts_for filtered the blank one out
    )
    seen: list[list[str]] = []
    monkeypatch.setattr(
        sm, "_summarize_direct", lambda q, docs: seen.append([d.text for d in docs]) or "S."
    )

    out = sm.summarize_scope(_analysis(theme="Climate"))

    assert seen == [["Lead chunk."]]
    assert out["used_chunks"] == 1


def test_empty_scope_falls_through(monkeypatch):
    _stub_scope(monkeypatch, [], {})
    assert sm.summarize_scope(_analysis(theme="Climate")) is None


def test_no_scope_falls_through(monkeypatch):
    assert sm.summarize_scope(_analysis()) is None
    assert sm.summarize_scope(None) is None


def test_blank_texts_fall_through(monkeypatch):
    ids = ["d1"]
    _stub_scope(monkeypatch, ids, {"d1": _payload("d1", text="   ")})
    assert sm.summarize_scope(_analysis(theme="Climate")) is None


def test_llm_failure_falls_through(monkeypatch):
    ids = ["d1"]
    _stub_scope(monkeypatch, ids, {"d1": _payload("d1")})

    def boom(q, docs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(sm, "_summarize_direct", boom)
    assert sm.summarize_scope(_analysis(theme="Climate")) is None


def test_docs_missing_payloads_are_skipped(monkeypatch):
    ids = ["d1", "d2", "d3"]
    _stub_scope(monkeypatch, ids, {"d1": _payload("d1"), "d3": _payload("d3")})
    monkeypatch.setattr(sm, "_summarize_direct", lambda q, docs: "S.")

    out = sm.summarize_scope(_analysis(theme="Climate"))
    assert out["used_chunks"] == 2
    assert [c["document_id"] for c in out["citations"]] == ["d1", "d3"]
