"""The shared PDF page reader: one definition of how a page becomes text.

Ingestion and web retrieval both read born-digital PDFs through
``app.core.pdf_text``. These tests pin what the web path relies on — page
numbering, the page cap, normalization, and the document's own metadata — and
that the write path still resolves to the very same functions.
"""
from __future__ import annotations

import pytest

from app.core import pdf_text

fitz = pytest.importorskip("fitz")


def _pdf(pages: list[str], *, title: str = "", author: str = "") -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    doc.set_metadata({"title": title, "author": author})
    data = doc.tobytes()
    doc.close()
    return data


def test_pages_are_numbered_from_one_in_document_order():
    result = pdf_text.read_pages(_pdf(["first page body", "second page body"]))
    assert [p.page_number for p in result.pages] == [1, 2]
    assert "first page body" in result.pages[0].text
    assert "second page body" in result.pages[1].text
    assert result.page_count == 2
    assert result.truncated is False


def test_the_page_cap_reads_only_the_opening_pages_and_says_so():
    result = pdf_text.read_pages(_pdf(["one", "two", "three"]), max_pages=2)
    assert [p.page_number for p in result.pages] == [1, 2]
    assert result.page_count == 3
    assert result.truncated is True


def test_page_text_is_normalized_like_ingestion_normalizes_it():
    # A ligature that extraction dropped to a space ("e cient") is repaired by
    # the shared normalizer. ASCII, so the test PDF's base font can draw it.
    result = pdf_text.read_pages(_pdf(["an e cient system"]))
    assert "efficient" in result.pages[0].text


def test_the_documents_own_metadata_is_carried_and_empty_values_dropped():
    result = pdf_text.read_pages(_pdf(["body"], title="Air Report", author=""))
    assert result.metadata.get("title") == "Air Report"
    assert "author" not in result.metadata


def test_ingestion_reads_pages_through_the_same_functions():
    from app.ingestion.extractors import pymupdf_local, text_normalize

    assert pymupdf_local._open is pdf_text.open_pdf
    assert pymupdf_local._page_text is pdf_text.page_text
    from app.core import text_normalize as core_normalize

    assert text_normalize.normalize_page_text is core_normalize.normalize_page_text
    assert text_normalize.strip_running_lines is core_normalize.strip_running_lines
