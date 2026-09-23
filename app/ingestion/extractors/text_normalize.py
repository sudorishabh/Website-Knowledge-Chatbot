"""Page-text normalization for extracted PDFs — re-exported from the core.

The rules moved to :mod:`app.core.text_normalize` so web retrieval can read a
fetched PDF with exactly the cleaning ingestion applies, without the read path
importing a write-path extractor. This name stays so every existing import on
the write path keeps working unchanged.
"""

from __future__ import annotations

from app.core.text_normalize import normalize_page_text, strip_running_lines

__all__ = ["normalize_page_text", "strip_running_lines"]
