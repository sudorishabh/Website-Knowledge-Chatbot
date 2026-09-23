"""A web passage expands to the section it carries, as a corpus child to its parent.

A web page has no parent chunk in Qdrant, so a web passage carries its section
inline as ``context_text``. The context builder admits that section — the same
expansion a corpus child gets — without asking Qdrant for a parent that does not
exist, and without the inline text ever overriding a real parent.

Only `_fetch_parents` is stubbed; it is the one call that would reach Qdrant.
"""
from __future__ import annotations

import pytest

from app.retrieval.context import builder as context_builder
from app.retrieval.context.builder import build_context
from app.retrieval.search.hybrid_search import Candidate

CHILD = "He has more than 25 publications to his credit."
SECTION = ("Profile\n\nMr Sayanta Ghosh is an Associate Fellow and Area Convener.\n\n"
           "He has more than 25 publications to his credit.")


@pytest.fixture
def parent_fetches(monkeypatch):
    asked: list[list[str]] = []
    store = {"p1": {"chunk_id": "p1", "chunk_text": "The real parent section.", "is_parent": True}}

    def fetch(ids):
        asked.append(list(ids))
        return {pid: store[pid] for pid in ids if pid in store}

    monkeypatch.setattr(context_builder, "_fetch_parents", fetch)
    return asked


def _web(**extra) -> Candidate:
    payload = {"chunk_id": "web:abc:1", "document_id": "web:abc", "source_type": "web",
               "chunk_text": CHILD, "context_text": SECTION, **extra}
    return Candidate(id="web:abc:1", score=0.7, semantic_score=0.7, payload=payload)


def test_a_web_passage_is_admitted_with_its_section(parent_fetches):
    (block,) = build_context([_web()], limit=3)
    assert block.text == SECTION
    assert block.payload["chunk_text"] == CHILD        # the citation still quotes the child


def test_no_parent_is_requested_for_a_web_passage(parent_fetches):
    build_context([_web()], limit=3)
    assert all(ids == [] for ids in parent_fetches)


def test_a_web_pdf_passage_keeps_its_page_provenance(parent_fetches):
    (block,) = build_context([_web(page_number=5, page_range=[5, 6], content_type="pdf")],
                             limit=3)
    assert block.payload["page_number"] == 5
    assert block.payload["page_range"] == [5, 6]


def test_a_real_parent_wins_over_inline_text(parent_fetches):
    candidate = Candidate(id="c1", score=0.7, semantic_score=0.7, payload={
        "chunk_id": "c1", "document_id": "d1", "source_type": "website",
        "chunk_text": "child", "parent_chunk_id": "p1", "context_text": "ignored",
    })
    (block,) = build_context([candidate], limit=3)
    assert block.text == "The real parent section."


def test_a_corpus_orphan_without_inline_text_is_unchanged(parent_fetches):
    candidate = Candidate(id="c2", score=0.7, semantic_score=0.7, payload={
        "chunk_id": "c2", "document_id": "d2", "source_type": "website",
        "chunk_text": "An orphan's own text."})
    (block,) = build_context([candidate], limit=3)
    assert block.text == "An orphan's own text."


def test_an_excluded_section_is_still_excluded(parent_fetches):
    assert build_context([_web(section_type="references")], limit=3) == []
