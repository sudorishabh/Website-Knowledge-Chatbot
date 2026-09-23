"""How a born-digital PDF page becomes text — one definition for both paths.

Ingestion reads every PDF page through here (``pymupdf_local`` imports
:func:`open_pdf` and :func:`page_text` under its old private names), and web
retrieval reads a PDF it fetched at query time through :func:`read_pages`. Same
library call, same flags, same normalization, so a TERI report retrieved from
the web is read exactly as it would have been read had it been ingested.

What this deliberately does *not* do is the ingestion router's per-page choice
between PyMuPDF, Azure OCR and Camelot (``app.ingestion.extractors.
pdf_extractor``). That routing costs seconds per scanned page and money per OCR
call, which is the right trade for a document indexed once and read forever, and
the wrong one for a document fetched for a single answer. A scanned page simply
comes back empty here, and an empty page contributes no evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.text_normalize import normalize_page_text, strip_running_lines

logger = logging.getLogger(__name__)

__all__ = ["PdfPage", "PdfText", "open_pdf", "page_text", "read_pages"]


def open_pdf(content: bytes):
    import fitz  # PyMuPDF

    return fitz.open(stream=content, filetype="pdf")


def page_text(page) -> str:
    return (page.get_text("text") or "").strip()


@dataclass
class PdfPage:
    page_number: int
    text: str


@dataclass
class PdfText:
    """A PDF's normalized page text plus the metadata its own dictionary states.

    ``metadata`` is the PDF's document-information dictionary (title, author,
    creationDate, ...) exactly as the file states it. It is a weak signal —
    authoring tools stamp it and nobody reads it — which is why a reader that
    dates a document from it has to say so.
    """

    pages: list[PdfPage] = field(default_factory=list)
    page_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False


def read_pages(
    content: bytes,
    *,
    max_pages: int | None = None,
    drop_number_soup: bool = True,
    running_header_min_fraction: float = 0.5,
) -> PdfText:
    """Normalized text of the first ``max_pages`` pages (1-based numbering).

    The same two passes ingestion's ``_normalize_result`` applies, in the same
    order: per-page boilerplate first, then lines repeated across pages (running
    headers and footers). Raises what PyMuPDF raises on an unreadable file; the
    caller decides what an unreadable document is worth.
    """
    doc = open_pdf(content)
    try:
        total = doc.page_count
        limit = total if not max_pages else min(total, max_pages)
        raw: list[PdfPage] = []
        for index in range(limit):
            text = page_text(doc[index])
            raw.append(PdfPage(page_number=index + 1, text=text))
        metadata = {k: v for k, v in (doc.metadata or {}).items() if v}
    finally:
        doc.close()

    for page in raw:
        page.text = normalize_page_text(page.text, drop_number_soup=drop_number_soup)
    cleaned = strip_running_lines(
        [p.text for p in raw], min_fraction=running_header_min_fraction
    )
    for page, text in zip(raw, cleaned):
        page.text = text
    return PdfText(
        pages=raw, page_count=total, metadata=metadata, truncated=limit < total
    )
