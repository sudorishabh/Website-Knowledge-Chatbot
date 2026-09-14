"""Document discovery: answering a request *for* a document from its identity.

The bug this covers, exactly as observed in production:

    "give me latest annual report"
      -> annual-report edition: latest -> 2024-25
      -> 2 chunks retrieved from Annual Report 2024-2025, 2 correct source cards
      -> answer: "I don't have information on that in the available sources."
      -> answered=False

The edition resolver was right, retrieval was right, the citations were right,
and generation refused — correctly, because pages 148-150 of a report do not say
which edition is the latest one. The identity was resolved and then discarded.

`test_the_original_production_failure_is_fixed` is the regression assertion for
that exact query. No LLM, no Qdrant, no MySQL.
"""

from __future__ import annotations

import pytest

from app.core.models.context import ContextBlock
from app.generation.prompts import REFUSAL
from app.pipeline import query_pipeline as pipe
from app.retrieval.understanding import query_processor as qp
from app.retrieval.understanding.annual_report_editions import EditionResolution
from app.retrieval.understanding.document_request import is_document_request

LATEST = EditionResolution(
    edition="2024-25",
    document_ids=("doc-2024-25",),
    kind="latest",
    available=("2015-16", "2019-20", "2024-25"),
)
NAMED = EditionResolution(
    edition="2019-20",
    document_ids=("doc-2019-20",),
    kind="named",
    available=("2015-16", "2019-20", "2024-25"),
)


def _block(n, doc_id, title, edition, page=148):
    return ContextBlock(
        n=n,
        text="... interior prose of the report, which names no edition ...",
        payload={
            "document_id": doc_id, "chunk_id": f"{doc_id}-c{n}",
            "source_type": "pdf_attachment", "bundle": "report",
            "title": title, "edition_label": edition,
            "page_number": page, "file_url": f"https://example.org/{doc_id}.pdf",
        },
    )


def _blocks(doc_id="doc-2024-25", title="Annual Report 2024-2025", edition="2024-25"):
    return [
        _block(1, doc_id, title, edition, page=148),
        _block(2, doc_id, title, edition, page=149),
    ]


def _pq(edition=LATEST, **kw):
    kw.setdefault("original", "give me latest annual report")
    kw.setdefault("search_query", "give me latest annual report")
    kw.setdefault("intent", "structured")
    return qp.ProcessedQuery(edition=edition, **kw)


def _prepare(monkeypatch, pq, blocks, question=None):
    """Drive `_prepare` past understanding and retrieval onto the seam.

    `_prepare` imports several of its collaborators *inside* the function, so
    they are patched at their own modules rather than on `pipe` — patching the
    pipeline attribute silently does nothing and the test reaches the real LLM,
    MySQL and Qdrant. Everything stubbed here is upstream of the seam under
    test; the seam itself, the citation builder and the result shape are real.
    """
    from app.cache import semantic_cache
    from app.generation import answer_plan
    from app.retrieval.structured import answerer, tools

    monkeypatch.setattr(pipe, "process", lambda q, h: pq)
    monkeypatch.setattr(pipe, "retrieve", lambda *a, **k: list(blocks))
    monkeypatch.setattr(pipe, "_temporal_intent", lambda p: None)
    monkeypatch.setattr(pipe, "_subquery_plan", lambda p, f: [])
    monkeypatch.setattr(answer_plan, "extract_requirements", lambda q: [])
    # The structured branch: a catalog route that declines, which is what the
    # production trace showed ("annual report" resolves to no single node).
    monkeypatch.setattr(tools, "resolve_lookup_chain", lambda a, q: None)
    monkeypatch.setattr(answerer, "answer_structured", lambda *a, **k: None)
    monkeypatch.setattr(semantic_cache, "lookup", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.core.clients.embeddings.embed_query", lambda t: [0.1]
    )
    return pipe._prepare(question or pq.original, history=None, top_k=6)


# --------------------------------------------------------------------------- #
# The discriminator
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("question", [
    "give me latest annual report",
    "show me the latest annual report",
    "where can I find the 2024-25 annual report?",
    "send me the annual report",
    "do you have the 2019-20 annual report?",
    "link to the latest annual report",
])
def test_a_request_for_the_document_is_recognised(question):
    assert is_document_request(question)


@pytest.mark.parametrize("question", [
    "what does the 2024-25 annual report say about solar?",
    "what are the key findings in the latest annual report?",
    "summarise the latest annual report",
    "show me what the annual report says about emissions",
    "give me the key numbers from the latest annual report",
    "",
])
def test_a_question_about_the_contents_is_not(question):
    """The content cues win ties: a false positive would answer "here is the
    document" to someone who asked what it says."""
    assert not is_document_request(question)


# --------------------------------------------------------------------------- #
# The deterministic result
# --------------------------------------------------------------------------- #

def test_the_original_production_failure_is_fixed(monkeypatch):
    """The regression assertion for the exact reported query."""
    result, generation = _prepare(monkeypatch, _pq(), _blocks())

    assert generation is None, "must not reach content-QA generation"
    assert result["answer"] != REFUSAL
    assert "Annual Report 2024-2025" in result["answer"]
    assert result["citations"], "the source cards must survive"


def test_the_answer_names_the_resolved_edition(monkeypatch):
    result, _ = _prepare(monkeypatch, _pq(), _blocks())
    assert result["answer"] == "The latest annual report is Annual Report 2024-2025 [1]."


def test_a_specific_edition_lookup_returns_that_edition(monkeypatch):
    pq = _pq(NAMED, original="where can I find the 2019-20 annual report?",
             search_query="where can I find the 2019-20 annual report?")
    blocks = _blocks("doc-2019-20", "Annual Report 2019-2020", "2019-20")
    result, generation = _prepare(monkeypatch, pq, blocks)

    assert generation is None
    assert "Annual Report 2019-2020" in result["answer"]
    assert result["answer"].startswith("The annual report you asked for is")
    assert {c["document_id"] for c in result["citations"]} == {"doc-2019-20"}


def test_the_document_is_named_once_however_many_chunks(monkeypatch):
    """Two chunks of one document are one document."""
    result, _ = _prepare(monkeypatch, _pq(), _blocks())
    assert result["answer"].count("Annual Report 2024-2025") == 1
    assert len(result["citations"]) == 2, "but every chunk is still a source"


def test_source_cards_carry_title_url_and_document(monkeypatch):
    """The cards are built by `build_citations`, unchanged — so a card here is
    identical to the one this document gets on any other answer, page anchor
    and all."""
    result, _ = _prepare(monkeypatch, _pq(), _blocks())
    first = result["citations"][0]
    assert first["title"] == "Annual Report 2024-2025"
    assert first["url"].startswith("https://example.org/doc-2024-25.pdf")
    assert first["document_id"] == "doc-2024-25"
    assert first["edition"] == "2024-25"
    assert first["n"] == 1


def test_the_result_is_reported_as_a_document_lookup(monkeypatch):
    result, _ = _prepare(monkeypatch, _pq(), _blocks())
    assert result["intent"] == "document_lookup"
    assert result["used_chunks"] == 2
    assert result["cached"] is False


def test_the_answer_is_counted_as_answered(monkeypatch):
    """`_record` derives `answered` by comparing against REFUSAL — the metric
    that read False in the production trace."""
    result, _ = _prepare(monkeypatch, _pq(), _blocks())
    assert (result["answer"] != REFUSAL) is True


def test_no_llm_is_invoked(monkeypatch):
    """Deterministic by construction: generation is never reached, so nothing
    here can call a model."""
    monkeypatch.setattr(
        pipe, "generate_stream",
        lambda *a, **k: pytest.fail("a document lookup must not generate"),
    )
    monkeypatch.setattr(
        pipe, "generate_answer",
        lambda *a, **k: pytest.fail("a document lookup must not generate"),
    )
    result, generation = _prepare(monkeypatch, _pq(), _blocks())
    assert generation is None and result["answer"]


def test_a_document_without_a_title_falls_back_to_the_edition(monkeypatch):
    """Never invent a title."""
    blocks = _blocks()
    for b in blocks:
        b.payload.pop("title")
    result, _ = _prepare(monkeypatch, _pq(), blocks)
    assert "the 2024-25 annual report" in result["answer"]


# --------------------------------------------------------------------------- #
# Everything that must keep its existing behaviour
# --------------------------------------------------------------------------- #

def test_a_content_question_about_an_edition_still_generates(monkeypatch):
    """The scope guard. The edition is resolved and applied as a filter exactly
    as before, and the answer still comes from the report's prose."""
    pq = _pq(original="what does the 2024-25 annual report say about solar?",
             search_query="what does the 2024-25 annual report say about solar?",
             intent="qa")
    result, generation = _prepare(monkeypatch, pq, _blocks())

    assert result is None
    assert generation is not None, "must reach content-QA generation"
    assert len(generation.blocks) == 2


def test_an_unresolved_edition_keeps_the_existing_path(monkeypatch):
    """A question the resolver declined — no edition, no interception."""
    pq = _pq(edition=None)
    result, generation = _prepare(monkeypatch, pq, _blocks())
    assert result is None and generation is not None


def test_a_resolved_edition_that_retrieved_nothing_keeps_the_existing_path(monkeypatch):
    """Placed after the empty-retrieval branches on purpose: with no blocks
    there is no catalogued title to name, so the existing refusal/catalog
    behaviour stands rather than a document answer with nothing in it."""
    pq = _pq()
    monkeypatch.setattr(pipe, "_catalog_listing", lambda p, q: None)
    result, generation = _prepare(monkeypatch, pq, [])
    assert generation is None
    assert result["answer"] == REFUSAL


def test_a_non_annual_report_structured_lookup_is_untouched(monkeypatch):
    """Nothing outside the annual-report series can reach this path: `edition`
    is None for every other document."""
    pq = _pq(edition=None, original="give me the energy policy brief",
             search_query="give me the energy policy brief")
    assert is_document_request(pq.original), "it is a document request..."
    result, generation = _prepare(monkeypatch, pq, _blocks())
    assert result is None and generation is not None, "...but nothing resolved it"


# --------------------------------------------------------------------------- #
# The resolution survives query understanding
# --------------------------------------------------------------------------- #

def test_process_carries_the_resolution_onto_the_processed_query(monkeypatch):
    """The fix at its source: `_edition` resolves once, and the object survives
    instead of being consumed for a filter and dropped."""
    monkeypatch.setattr(qp, "_edition", lambda q: LATEST)
    monkeypatch.setattr(qp, "_facet_filters", lambda a: [])

    class _Model:
        def with_structured_output(self, _s):
            return self

        def invoke(self, _m):
            return qp.QueryUnderstanding(
                query_rewrite="give me latest annual report",
                intents=[qp.IntentPrediction(label="database", confidence=0.62)],
            )

    monkeypatch.setattr(qp, "get_structured_llm", lambda: _Model())
    pq = qp.process("give me latest annual report")

    assert pq.edition is LATEST
    assert pq.edition.edition == "2024-25"
    assert pq.filters, "and it is still applied as a retrieval filter"


def test_an_edition_still_produces_the_same_retrieval_filter():
    """`_edition_conditions` now takes the resolution rather than the question;
    what it builds is unchanged."""
    conditions = qp._edition_conditions(LATEST)
    assert len(conditions) == 1
    assert conditions[0].key == "document_id"
    assert conditions[0].match.any == ["doc-2024-25"]


def test_no_resolution_means_no_filter():
    assert qp._edition_conditions(None) == []
