"""The annual-report series as a whole: listing it, and counting it.

The bug, as reported:

    "list of annual reports"
      -> "I don't have information on that in the available sources."
      -> plus unrelated WEB PAGE and PDF source cards

`resolve` recognises a whole-series question and returns None so retrieval is
not narrowed to one edition — correct, and then the verdict was discarded. The
question fell through to an *unfiltered* semantic search, the model was asked to
find a list of editions in report prose, and the citation fallback attached
whatever that pull happened to return.

Three operations are kept apart here, and the boundaries between them are what
these tests are mostly about:

    single-document lookup   "give me latest annual report"      (test_document_lookup.py)
    series listing           "list of annual reports"
    series count             "how many annual reports are there?"

No LLM, no Qdrant, no MySQL.
"""

from __future__ import annotations

import pytest

from app.generation.prompts import REFUSAL
from app.pipeline import query_pipeline as pipe
from app.retrieval.understanding import annual_report_editions as are
from app.retrieval.understanding import query_processor as qp

EDITIONS = [f"{y}-{str(y + 1)[2:]}" for y in range(2015, 2025)]
SERIES = {e: (f"doc-{e}",) for e in EDITIONS}
DOCUMENTS = tuple(
    are.SeriesDocument(
        edition=e,
        document_id=f"doc-{e}",
        title=f"Annual Report {e[:4]}-20{e[5:]}",
        url=f"https://teri.res.in/ar-{e}.pdf",
    )
    for e in sorted(EDITIONS, reverse=True)
)


@pytest.fixture(autouse=True)
def _catalogue(monkeypatch):
    """Stand in for the catalogue read, so detection and rendering are exercised
    against a known series without a database."""
    monkeypatch.setattr(are, "_series", lambda: SERIES)
    monkeypatch.setattr(are, "series_documents", lambda: DOCUMENTS)


def _request(question):
    return are.series_request(question)


def _pq(question, series=None, edition=None):
    return qp.ProcessedQuery(
        original=question, search_query=question, intent="structured",
        series=series, edition=edition,
    )


def _prepare(monkeypatch, pq):
    """Drive `_prepare`, failing loudly if anything downstream of the seam runs."""
    monkeypatch.setattr(pipe, "process", lambda q, h: pq)
    monkeypatch.setattr(
        pipe, "retrieve",
        lambda *a, **k: pytest.fail("a series request must not reach retrieval"),
    )
    monkeypatch.setattr(
        pipe, "chitchat",
        lambda *a, **k: pytest.fail("a series request is not chit-chat"),
    )
    monkeypatch.setattr(
        "app.core.clients.embeddings.embed_query",
        lambda t: pytest.fail("a series request must not embed"),
    )
    return pipe._prepare(pq.original, history=None, top_k=6)


# --------------------------------------------------------------------------- #
# Which operation a question is
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("question", [
    "list of annual reports",
    "list the annual reports",
    "show me the annual reports",
    "all annual reports",
    "what annual reports do you have",
    "which annual reports are available",
])
def test_a_series_question_is_a_listing(question):
    request = _request(question)
    assert request is not None and request.kind == are.LIST


@pytest.mark.parametrize("question", [
    "how many annual reports are there?",
    "how many annual reports do you have",
    "what is the number of annual reports",
])
def test_a_series_question_that_counts_is_a_count(question):
    request = _request(question)
    assert request is not None and request.kind == are.COUNT


@pytest.mark.parametrize("question", [
    # Single-document lookups — singular, and/or an edition named.
    "give me latest annual report",
    "show me the latest annual report",
    "where can I find the 2024-25 annual report?",
    "give me the annual report 2023-24",
    # Content questions about the reports.
    "what does the latest annual report say about solar?",
    "how many annual reports discuss solar?",
    "how many annual reports mention biofuel?",
    "annual reports on water",
    # Analysis across the series — plural, but about the substance.
    "compare the annual reports over the years",
    "trends across annual reports",
    "how have the annual reports changed",
])
def test_everything_else_is_not_a_series_operation(question):
    assert _request(question) is None


def test_plural_show_me_lists_rather_than_resolving_to_the_latest():
    """The scope decision that motivated separating the two: "show me the annual
    reports" must not be answered with one report.

    The edition resolver still returns its own verdict for this wording — it is
    untouched — so the guarantee comes from the pipeline consulting the series
    first, which `test_the_series_branch_wins_over_a_resolved_edition` pins.
    """
    assert _request("show me the annual reports").kind == are.LIST
    assert _request("show me the latest annual report") is None
    assert are.resolve("show me the latest annual report").kind == "latest"


def test_counting_documents_is_not_counting_their_contents():
    """The distinction the brief called out: one counts the series, the other
    counts documents by what is inside them."""
    assert _request("how many annual reports are there?").kind == are.COUNT
    assert _request("how many annual reports mention solar?") is None


def test_a_named_edition_is_never_a_series_request():
    assert _request("list the annual reports for 2019") is None


def test_an_unreadable_catalogue_is_not_a_series_request(monkeypatch):
    """Fails to None, leaving the question on the path it takes today."""
    monkeypatch.setattr(are, "_series", lambda: {})
    assert _request("list of annual reports") is None


def test_no_documents_means_no_series_request(monkeypatch):
    monkeypatch.setattr(are, "series_documents", lambda: ())
    assert _request("list of annual reports") is None


# --------------------------------------------------------------------------- #
# The series verdict must not become a retrieval filter
# --------------------------------------------------------------------------- #

def test_a_series_request_adds_no_qdrant_condition():
    """`conditions_for` is unchanged and takes an edition, not a series. The
    two are separate objects precisely so a series can never be narrowed to a
    document_id filter by accident."""
    assert are.resolve("list of annual reports") is None
    assert are.conditions_for(None) == []
    assert not hasattr(_request("list of annual reports"), "document_ids")


# --------------------------------------------------------------------------- #
# The deterministic listing
# --------------------------------------------------------------------------- #

def test_the_listing_returns_every_edition(monkeypatch):
    result, generation = _prepare(
        monkeypatch, _pq("list of annual reports", series=_request("list of annual reports"))
    )
    assert generation is None
    assert result["intent"] == "series_list"
    assert len(result["citations"]) == 10
    for edition in EDITIONS:
        assert f"Annual Report {edition[:4]}-20{edition[5:]}" in result["answer"]


def test_the_listing_is_not_a_refusal(monkeypatch):
    """The exact reported symptom."""
    result, _ = _prepare(
        monkeypatch, _pq("list of annual reports", series=_request("list of annual reports"))
    )
    assert result["answer"] != REFUSAL


def test_the_listing_carries_correct_titles_and_urls(monkeypatch):
    result, _ = _prepare(
        monkeypatch, _pq("list of annual reports", series=_request("list of annual reports"))
    )
    first = result["citations"][0]
    assert first["title"] == "Annual Report 2024-2025"
    assert first["url"] == "https://teri.res.in/ar-2024-25.pdf"
    assert first["edition"] == "2024-25"
    assert first["document_id"] == "doc-2024-25"
    assert first["type"] == "pdf_attachment"


def test_no_unrelated_source_cards(monkeypatch):
    """Every card is an edition of the series — the unrelated web page and PDF
    cards came from an unfiltered pull that no longer happens."""
    result, _ = _prepare(
        monkeypatch, _pq("list of annual reports", series=_request("list of annual reports"))
    )
    assert {c["document_id"] for c in result["citations"]} == {
        f"doc-{e}" for e in EDITIONS
    }


def test_the_newest_edition_leads(monkeypatch):
    result, _ = _prepare(
        monkeypatch, _pq("list of annual reports", series=_request("list of annual reports"))
    )
    assert result["citations"][0]["edition"] == "2024-25"
    assert result["answer"].index("2024-2025") < result["answer"].index("2015-2016")


# --------------------------------------------------------------------------- #
# The deterministic count
# --------------------------------------------------------------------------- #

def test_the_count_states_the_number(monkeypatch):
    question = "how many annual reports are there?"
    result, generation = _prepare(monkeypatch, _pq(question, series=_request(question)))
    assert generation is None
    assert result["answer"] == "There are 10 annual reports available."
    assert result["intent"] == "series_count"


def test_the_count_cites_what_it_counted(monkeypatch):
    question = "how many annual reports are there?"
    result, _ = _prepare(monkeypatch, _pq(question, series=_request(question)))
    assert len(result["citations"]) == 10


def test_a_single_edition_series_is_not_pluralised():
    pq = _pq("how many annual reports are there?", series=are.SeriesRequest(
        kind=are.COUNT, documents=DOCUMENTS[:1]))
    assert pipe._series_result(pq)["answer"] == "There are 1 annual report available."


# --------------------------------------------------------------------------- #
# Terminal: no retrieval, no model
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("question", [
    "list of annual reports", "how many annual reports are there?",
])
def test_a_series_request_never_retrieves_or_generates(monkeypatch, question):
    """The `_prepare` stub fails the test if retrieval or embedding runs; this
    adds generation to that."""
    monkeypatch.setattr(
        pipe, "generate_stream",
        lambda *a, **k: pytest.fail("a series request must not generate"),
    )
    result, generation = _prepare(monkeypatch, _pq(question, series=_request(question)))
    assert generation is None and result["answer"]


def test_the_series_branch_wins_over_a_resolved_edition(monkeypatch):
    """"show me the annual reports" resolves to an edition *and* to a series —
    the resolver is untouched, so both verdicts exist. The series must win, and
    the edition filter must never be applied."""
    question = "show me the annual reports"
    pq = _pq(question, series=_request(question), edition=are.resolve(question))
    assert pq.edition is not None, "the edition resolver still has its own opinion"

    result, generation = _prepare(monkeypatch, pq)
    assert generation is None
    assert result["intent"] == "series_list"
    assert len(result["citations"]) == 10


# --------------------------------------------------------------------------- #
# Nothing else moved
# --------------------------------------------------------------------------- #

def test_a_question_with_no_series_verdict_is_untouched(monkeypatch):
    """The default path: `series` is None for every question that is not about
    the series, and `_prepare` proceeds exactly as before."""
    pq = _pq("what does the latest annual report say about solar?")
    assert pq.series is None

    monkeypatch.setattr(pipe, "process", lambda q, h: pq)
    monkeypatch.setattr(
        pipe, "retrieve", lambda *a, **k: pytest.raises and []
    )
    monkeypatch.setattr(pipe, "_catalog_listing", lambda p, q: None)
    monkeypatch.setattr(
        "app.core.clients.embeddings.embed_query", lambda t: [0.1]
    )
    from app.cache import semantic_cache
    from app.generation import answer_plan
    from app.retrieval.structured import answerer, tools

    monkeypatch.setattr(semantic_cache, "lookup", lambda *a, **k: None)
    monkeypatch.setattr(answer_plan, "extract_requirements", lambda q: [])
    monkeypatch.setattr(tools, "resolve_lookup_chain", lambda a, q: None)
    monkeypatch.setattr(answerer, "answer_structured", lambda *a, **k: None)

    result, generation = pipe._prepare(pq.original, history=None, top_k=6)
    # Retrieval returned nothing, so the pre-existing refusal path stands.
    assert generation is None and result["answer"] == REFUSAL


def test_process_carries_the_series_verdict(monkeypatch):
    """The fix at its source, mirroring `edition`."""
    monkeypatch.setattr(qp, "_facet_filters", lambda a: [])
    monkeypatch.setattr(qp, "_edition", lambda q: None)

    class _Model:
        def with_structured_output(self, _s):
            return self

        def invoke(self, _m):
            return qp.QueryUnderstanding(
                query_rewrite="list of annual reports",
                intents=[qp.IntentPrediction(label="database", confidence=0.62)],
            )

    monkeypatch.setattr(qp, "get_structured_llm", lambda: _Model())
    pq = qp.process("list of annual reports")

    assert pq.series is not None and pq.series.kind == are.LIST
    assert len(pq.series.documents) == 10
    assert pq.filters == [], "a series verdict adds no retrieval filter"


def test_a_processed_query_defaults_to_no_series():
    assert qp.ProcessedQuery(original="q", search_query="q").series is None
