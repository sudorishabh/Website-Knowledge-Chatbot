"""Every citation says where its source came from, how it was found, and when.

The provenance fields are additive — a client that ignores them sees the
citation it always saw — and they describe every kind of source by one rule. The
date rules are the part that must not drift: a web page reports the date it
states for itself and when it was read; a corpus source reports neither, because
its only date is the CMS page date, which is not a publication date.
"""
from __future__ import annotations

from app.core.models.context import ContextBlock
from app.retrieval.context.citations import build_citations


def _one(payload: dict, *, score: float = 0.9412, also=()) -> dict:
    block = ContextBlock(n=1, text="t", payload=payload, score=score,
                         also_available=list(also))
    (citation,) = build_citations([block])
    return citation.model_dump()


WEB = {
    "source_type": "web", "document_id": "web:abc", "chunk_id": "web:abc:0",
    "title": "Mr Sayanta Ghosh | TERI", "url": "https://www.teriin.org/user/15680",
    "domain": "teriin.org", "is_primary_source": True, "authors": [],
    "published_date": "2025-03-04", "date_source": "page_markup",
    "retrieved_at": "2026-09-23T06:00:00+00:00", "retrieval_method": "web_search:brave",
    "section_heading": "Profile",
}


def test_a_web_page_is_cited_as_web_with_its_full_provenance():
    c = _one(WEB)
    assert c["type"] == "web"
    assert c["url"] == "https://www.teriin.org/user/15680"
    assert c["domain"] == "teriin.org"
    assert c["is_primary_source"] is True
    assert (c["published_date"], c["date_source"]) == ("2025-03-04", "page_markup")
    assert c["retrieved_at"] == "2026-09-23T06:00:00+00:00"
    assert c["retrieval_method"] == "web_search:brave"
    assert c["chunk_id"] == "web:abc:0"
    assert c["section"] == "Profile"
    assert c["score"] == 0.9412


def test_a_web_pdf_links_to_the_page_the_passage_is_on():
    c = _one({**WEB, "content_type": "pdf", "url": "https://www.teriin.org/f/a.pdf",
              "file_url": "https://www.teriin.org/f/a.pdf", "page_number": 5,
              "page_range": [5, 6]})
    assert c["url"] == "https://www.teriin.org/f/a.pdf#page=5"
    assert (c["page"], c["page_end"]) == (5, 6)


def test_a_third_party_page_says_so():
    c = _one({**WEB, "url": "https://news.example/x", "domain": "news.example",
              "is_primary_source": False})
    assert c["is_primary_source"] is False and c["domain"] == "news.example"


def test_an_undated_web_page_reports_no_publication_date():
    c = _one({k: v for k, v in WEB.items() if k not in ("published_date", "date_source")})
    assert c["published_date"] is None
    assert c["retrieved_at"] == "2026-09-23T06:00:00+00:00"   # when it was read, only


CORPUS = {
    "source_type": "website", "document_id": "node-1", "chunk_id": "c-1",
    "title": "Bridging the Gap", "source_url": "https://www.teriin.org/article/bridging-gap",
    "authors": ["Dr Anju Goel", "Ms Aishwarya Yadav"],
    "effective_start_date": "2025-04-09T00:00:00",
}


def test_a_corpus_source_gains_provenance_without_a_publication_date():
    c = _one(CORPUS)
    assert c["type"] == "website"
    assert c["retrieval_method"] == "corpus"
    assert c["domain"] == "teriin.org"                     # from its link's host
    assert c["authors"] == ["Dr Anju Goel", "Ms Aishwarya Yadav"]
    # Its only date is the CMS page date; it is not reported as a publication date.
    assert c["published_date"] is None and c["retrieved_at"] is None
    assert c["is_primary_source"] is None


def test_the_existing_citation_fields_are_unchanged():
    c = _one(CORPUS)
    assert (c["n"], c["title"], c["url"], c["document_id"]) == (
        1, "Bridging the Gap", "https://www.teriin.org/article/bridging-gap", "node-1")


def test_a_corpus_chunk_found_through_web_search_says_so():
    c = _one({**CORPUS, "retrieval_method": "corpus_via_web_search:brave"})
    assert c["type"] == "website"
    assert c["retrieval_method"] == "corpus_via_web_search:brave"


def test_the_knowledge_graph_is_its_own_retrieval_method():
    c = _one({"kind": "graph_facts", "mode": "history", "claim_ids": ["claim_1"]})
    assert c["type"] == "knowledge_graph"
    assert c["retrieval_method"] == "knowledge_graph"


def test_alternates_are_described_by_the_same_rule():
    alternate = {**CORPUS, "source_type": "pdf_attachment", "file_url":
                 "https://www.teriin.org/f/b.pdf", "page_number": 3, "chunk_id": "c-2"}
    c = _one(CORPUS, also=[alternate])
    (alt,) = c["also_available"]
    assert alt["retrieval_method"] == "corpus"
    assert alt["chunk_id"] == "c-2"
    assert alt["url"] == "https://www.teriin.org/f/b.pdf#page=3"
