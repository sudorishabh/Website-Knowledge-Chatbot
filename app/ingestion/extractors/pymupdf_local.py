"""PyMuPDF (fitz) page classification + local text extraction.

This module owns the *local* side of the hybrid router (see ``pdf_extractor``):

* ``classify_document`` inspects every page with PyMuPDF and reports, per page,
  whether it looks scanned or carries a table (``PageSignal``) — the signals the
  router uses to pick a per-page extractor: scanned/image -> Azure OCR,
  born-digital table -> Camelot, everything else -> local text.
* ``extract_local`` / ``extract_local_pages`` extract born-digital text only and
  emit the same canonical ``ExtractionResult`` the rest of the pipeline expects.

Tables are never *kept* from the local path — on a born-digital table page the
router pairs this module's page text with Camelot's table Markdown (see
``camelot_tables``).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass

from app.config import get_settings

# Reuse the frozen public types so both extraction paths emit one structure.
from app.ingestion.extractors.pdf_extractor import (
    ExtractedVia,
    ExtractionResult,
    PageContent,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PageSignal",
    "classify_document",
    "extract_local",
    "extract_local_pages",
]


@dataclass
class PageSignal:
    """Per-page routing signals derived with PyMuPDF.

    ``text`` is the page's born-digital text. The scanned check already extracts
    it during classification, so carrying it here lets the hybrid router reuse it
    for local/table pages instead of re-opening the PDF to extract it again.
    """

    page_number: int
    char_count: int
    scanned: bool
    has_table: bool
    text: str = ""

    @property
    def route(self) -> str:
        """Per-page extractor: scanned/image -> Azure OCR, born-digital table ->
        Camelot, everything else -> local PyMuPDF text. Scanned wins over table
        because Camelot cannot read an image."""
        if self.scanned:
            return "azure"
        if self.has_table:
            return "camelot"
        return "local"


def _open(content: bytes):
    import fitz  # PyMuPDF

    return fitz.open(stream=content, filetype="pdf")


def _page_text(page) -> str:
    return (page.get_text("text") or "").strip()


def _grid_line_counts(page) -> tuple[int, int]:
    """Distinct horizontal and vertical ruling-line positions on the page.

    A real bordered table forms a grid: several horizontal AND several vertical
    lines. Decorative page furniture (header/footer underlines, a logo) is
    almost always horizontal-only or a lone box, so requiring *both* axes keeps
    it from being mistaken for a table.
    """
    tol = 2  # quantize positions to a 2pt grid
    h_positions: set[int] = set()
    v_positions: set[int] = set()
    for drawing in page.get_drawings():
        for item in drawing.get("items", ()):
            op = item[0] if item else ""
            if op == "l":  # straight line segment: ("l", p1, p2)
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) <= tol and abs(p1.x - p2.x) > tol:
                    h_positions.add(round(p1.y / tol) * tol)
                elif abs(p1.x - p2.x) <= tol and abs(p1.y - p2.y) > tol:
                    v_positions.add(round(p1.x / tol) * tol)
            elif op == "re":  # rectangle: ("re", rect, ...) — 2 h + 2 v edges
                rect = item[1]
                h_positions.add(round(rect.y0 / tol) * tol)
                h_positions.add(round(rect.y1 / tol) * tol)
                v_positions.add(round(rect.x0 / tol) * tol)
                v_positions.add(round(rect.x1 / tol) * tol)
    return len(h_positions), len(v_positions)


def _has_aligned_columns(page, min_rows: int, min_cols: int) -> bool:
    """Borderless-table heuristic: several lines share >= min_cols word-start columns.

    Requires multiple *internal* columns (not just a shared left margin), so
    ordinary prose — which only lines up at the left edge — does not qualify.
    """
    words = page.get_text("words")
    if not words:
        return False

    lines: dict[tuple, list[int]] = defaultdict(list)
    for w in words:
        x0 = int(round(w[0] / 5.0)) * 5  # quantize x-start to a 5pt grid
        block_no, line_no = w[5], w[6]
        lines[(block_no, line_no)].append(x0)

    column_positions: Counter = Counter()
    multi_column_lines = 0
    for xs in lines.values():
        distinct = sorted(set(xs))
        if len(distinct) >= min_cols:  # a line spanning several columns
            multi_column_lines += 1
            for x in distinct:
                column_positions[x] += 1

    if multi_column_lines < min_rows:
        return False
    # At least min_cols column positions each shared by >= min_rows lines.
    shared = [x for x, c in column_positions.items() if c >= min_rows]
    return len(shared) >= min_cols


def _page_has_table(page, settings) -> bool:
    # (a) PyMuPDF's own table finder.
    try:
        finder = page.find_tables()
        if getattr(finder, "tables", None):
            return True
    except Exception:
        logger.debug("find_tables failed on a page; ignoring.", exc_info=True)

    # (b) Optional ruled-grid heuristic (off by default — noisy on designed PDFs).
    if settings.pdf_detect_ruled_grid:
        try:
            h_lines, v_lines = _grid_line_counts(page)
            if h_lines >= settings.pdf_table_min_grid_lines and v_lines >= settings.pdf_table_min_grid_lines:
                return True
        except Exception:
            logger.debug("get_drawings failed on a page; ignoring.", exc_info=True)

    # (c) Optional borderless-table detection via whitespace-column alignment.
    if settings.pdf_detect_borderless_tables:
        try:
            if _has_aligned_columns(
                page,
                settings.pdf_borderless_min_aligned_rows,
                settings.pdf_borderless_min_columns,
            ):
                return True
        except Exception:
            logger.debug("borderless detection failed on a page; ignoring.", exc_info=True)

    return False


def classify_document(content: bytes) -> list[PageSignal]:
    """Classify every page; pages drive the document-level routing decision."""
    settings = get_settings()
    signals: list[PageSignal] = []
    doc = _open(content)
    try:
        for i, page in enumerate(doc, start=1):
            text = _page_text(page)
            scanned = len(text) < settings.pdf_scanned_char_threshold
            has_table = _page_has_table(page, settings)
            signals.append(
                PageSignal(
                    page_number=i,
                    char_count=len(text),
                    scanned=scanned,
                    has_table=has_table,
                    text=text,
                )
            )
    finally:
        doc.close()
    return signals


def extract_local_pages(
    content: bytes, page_numbers: list[int] | None = None
) -> dict[int, PageContent]:
    """Text-only PyMuPDF extraction of specific pages (1-based; None => all).

    Returns a {page_number: PageContent} map so the router can stitch local and
    Azure pages together in page order. Never keeps tables.
    """
    wanted = set(page_numbers) if page_numbers is not None else None
    out: dict[int, PageContent] = {}
    doc = _open(content)
    try:
        for i, page in enumerate(doc, start=1):
            if wanted is not None and i not in wanted:
                continue
            text = _page_text(page)
            via = ExtractedVia.TEXT if text else ExtractedVia.EMPTY
            out[i] = PageContent(page_number=i, text=text, extracted_via=via, tables=[])
    finally:
        doc.close()
    return out


def extract_local(content: bytes, filename: str) -> ExtractionResult:
    """Extract a whole born-digital document as text only (no tables kept)."""
    pages_map = extract_local_pages(content, None)
    pages = [pages_map[n] for n in sorted(pages_map)]
    return ExtractionResult(source=filename, pages=pages, metadata={"engine": "pymupdf"})
