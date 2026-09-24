"""Page-text normalization for extracted PDFs.

Strips boilerplate that Azure Document Intelligence's Layout markdown (and, to a
lesser extent, local extraction) leaves in page text: HTML layout comments,
``<figure>`` wrappers, and single-cell page-number bars. Cleaning happens in the
extraction layer so the downstream chunker / embedder / payload are untouched.
"""

from __future__ import annotations

import math
import re
from collections import Counter

__all__ = ["normalize_page_text", "strip_running_lines"]

# <!-- PageBreak -->, <!-- PageNumber="22" -->, <!-- PageHeader=... -->, ...
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# <figure>...</figure> — unwrap to inner content (empty ones disappear).
_FIGURE_BLOCK = re.compile(r"<figure>\s*(.*?)\s*</figure>", re.DOTALL | re.IGNORECASE)
_FIGURE_TAG = re.compile(r"</?figure>", re.IGNORECASE)

# A standalone page-number bar: "|  ii  |", "| 14 |" — a single cell holding only
# a page number (roman or arabic). Real multi-cell table rows are left alone.
_PAGE_NUMBER_BAR = re.compile(r"^\s*\|\s*[ivxlcdm\d]+\s*\|\s*$", re.IGNORECASE)

# A markdown separator cell: "---", ":--", "--:" (alignment markers, no content).
_SEP_CELL = re.compile(r"^:?-{2,}:?$")

# Unicode ligature glyphs some PDFs embed literally (ﬀ ﬁ ﬂ ﬃ ﬄ ﬅ ﬆ).
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st",
}
_LIGATURE_RE = re.compile("|".join(map(re.escape, _LIGATURES)))

# Words whose f-ligature dropped to a space on extraction ("e cient" -> "efficient").
# Only non-lexical broken forms are listed, so the replacement can never hit real text.
_DROPPED_LIGATURE = {
    "ine cient": "inefficient", "ine ciency": "inefficiency",
    "e cient": "efficient", "e ciency": "efficiency", "e ciently": "efficiently",
    "su cient": "sufficient", "su ciently": "sufficiently", "insu cient": "insufficient",
    "di cult": "difficult", "di culty": "difficulty", "di culties": "difficulties",
    "signi cant": "significant", "signi cantly": "significantly", "signi cance": "significance",
    "speci c": "specific", "speci cally": "specifically",
    "speci cation": "specification", "speci cations": "specifications",
    "di erent": "different", "di erence": "difference", "di erences": "differences",
    "e ort": "effort", "e orts": "efforts",
    "scienti c": "scientific", "classi cation": "classification",
    "certi cate": "certificate", "certi cates": "certificates", "arti cial": "artificial",
    "tra c": "traffic", "o cial": "official", "o cials": "officials",
    "bene t": "benefit", "bene ts": "benefits", "bene cial": "beneficial",
    "pro t": "profit", "pro ts": "profits", "pro table": "profitable",
}
_DROPPED_RE = re.compile(
    r"(?<![A-Za-z])("
    + "|".join(re.escape(k) for k in sorted(_DROPPED_LIGATURE, key=len, reverse=True))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _fix_dropped(m: re.Match) -> str:
    fixed = _DROPPED_LIGATURE[m.group(1).lower()]
    return fixed[:1].upper() + fixed[1:] if m.group(1)[:1].isupper() else fixed


def _repair_ligatures(text: str) -> str:
    """Restore ligatures lost in extraction: literal glyphs and dropped-to-space gaps."""
    text = _LIGATURE_RE.sub(lambda m: _LIGATURES[m.group()], text)
    return _DROPPED_RE.sub(_fix_dropped, text)


# Formula subscripts that extraction turns into a comma: "CO₂" -> "CO,", "H₂" -> "H,".
# A bare "CO," is also legitimate (carbon monoxide in a list), so each rule is anchored
# to a right-context only a formula carries — never a list. Normalised to ASCII
# ("CO2", "H2") to match how these formulae already read elsewhere in the corpus.
_SUBSCRIPT_FIXES = (
    (re.compile(r"\bMtCO,"), "MtCO2"),
    (re.compile(
        r"\bCO,(?=\s+(?:emission|emissions|source|sources|capture|abatement|intensity|"
        r"equivalent|storage|footprint|removal|sequestration)\b)"
    ), "CO2"),
    (re.compile(r"\bH,(?=\s*(?:DR|DRI|blend\w*|injection|plasma|production)\b)"), "H2"),
)


def _repair_subscripts(text: str) -> str:
    """Restore formula subscripts extraction mangled to commas (CO₂ -> "CO,")."""
    for pattern, repl in _SUBSCRIPT_FIXES:
        text = pattern.sub(repl, text)
    return text

# A "number token": digits plus number punctuation (e.g. "2,020", "-12.5%", "(3)").
_NUM_TOKEN = re.compile(r"^[\d.,%+\-–—()]+$")

# A line ending like a sentence — never treated as a chart label.
_SENTENCE_END = re.compile(r"[.:;!?]$")

_BLANK_RUNS = re.compile(r"\n{3,}")


def _is_number_soup(line: str) -> bool:
    """A bare numeric run on one line (chart axis/data labels), no semantic content."""
    toks = line.split()
    if len(toks) < 4:
        return False
    numeric = sum(1 for t in toks if _NUM_TOKEN.match(t))
    return numeric / len(toks) >= 0.7


def _is_bare_number(line: str) -> bool:
    s = line.strip()
    return bool(s) and bool(_NUM_TOKEN.match(s)) and any(c.isdigit() for c in s)


def _is_chart_label(line: str) -> bool:
    """A short chart axis/category label — a few words, no sentence punctuation
    (e.g. "China", "Japan, South Korea", "RoW"). Table rows are never labels."""
    s = line.strip()
    if not s or s.startswith("|") or _SENTENCE_END.search(s):
        return False
    return len(s) <= 28 and len(s.split()) <= 4


def _drop_number_runs(lines: list[str], min_nums: int = 4) -> list[str]:
    """Drop chart data regions: contiguous blocks of bare-number lines, possibly
    interleaved with short category labels (vertical bar/line chart axes + data).

    A block is dropped when it holds >= ``min_nums`` bare numbers and is at least
    40% numeric, so number-dominated chart soup goes while real short lists (which
    carry few or no bare numbers) are kept.
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        nums = 0
        while j < len(lines) and (_is_bare_number(lines[j]) or _is_chart_label(lines[j])):
            if _is_bare_number(lines[j]):
                nums += 1
            j += 1
        run = j - i
        if run and nums >= min_nums and nums / run >= 0.4:
            i = j  # numeric-dominated block — drop the whole chart region
        elif j > i:
            out.extend(lines[i:j])  # not chart-like enough — keep
            i = j
        else:
            out.append(lines[i])
            i += 1
    return out


def _strip_figures(text: str) -> str:
    text = _FIGURE_BLOCK.sub(lambda m: m.group(1).strip(), text)
    return _FIGURE_TAG.sub("", text)  # drop any unmatched stray tags


def _table_cells(line: str) -> list[str]:
    """Cells of a markdown table row, without the outer pipes' empty edges."""
    parts = [p.strip() for p in line.strip().split("|")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _is_garbage_table(
    block: list[str], *, min_cols: int = 6, max_empty: float = 0.5, max_repeat: float = 0.4
) -> bool:
    """A wide markdown table that is mostly empty cells or one phrase repeated
    across columns — i.e. an infographic/timeline graphic Azure rendered as a
    table, not real tabular data. Narrow tables (< min_cols) are never garbage.
    """
    rows = [_table_cells(ln) for ln in block]
    rows = [r for r in rows if not (r and all(_SEP_CELL.match(c) for c in r))]
    if not rows or max(len(r) for r in rows) < min_cols:
        return False
    cells = [c for r in rows for c in r]
    nonempty = [c for c in cells if c]
    if not cells:
        return False
    if (len(cells) - len(nonempty)) / len(cells) >= max_empty:
        return True
    return bool(nonempty) and Counter(nonempty).most_common(1)[0][1] / len(nonempty) >= max_repeat


def _drop_garbage_tables(lines: list[str]) -> list[str]:
    """Drop contiguous markdown-table blocks that are degenerate infographics."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            if not _is_garbage_table(lines[i:j]):
                out.extend(lines[i:j])
            i = j
        else:
            out.append(lines[i])
            i += 1
    return out


def normalize_page_text(text: str, *, drop_number_soup: bool = True) -> str:
    """Remove layout boilerplate from a single page's text."""
    if not text:
        return text
    text = _repair_ligatures(text)
    text = _repair_subscripts(text)
    text = _HTML_COMMENT.sub("", text)
    text = _strip_figures(text)
    lines = []
    for ln in _drop_garbage_tables(text.splitlines()):
        if "<!--" in ln or "-->" in ln:  # dangling comment half split across a page break
            continue
        if _PAGE_NUMBER_BAR.match(ln):
            continue
        if drop_number_soup and _is_number_soup(ln):
            continue
        lines.append(ln)
    if drop_number_soup:
        lines = _drop_number_runs(lines)
    text = _BLANK_RUNS.sub("\n\n", "\n".join(lines))
    return text.strip()


def _running_key(line: str) -> str:
    """Letters-only key for a line — robust to OCR space/hyphen variation so a
    footer split as "Ma|ritime" and "M|aritime" compares equal once joined."""
    return re.sub(r"[^a-z]", "", line.lower())


def _eligible_lines(lines: list[str], max_words: int) -> list[tuple[int, str]]:
    """(index, key) for short, non-blank, non-table lines — running-line candidates."""
    out: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and not s.startswith("|") and len(s.split()) <= max_words:
            out.append((i, _running_key(s)))
    return out


def strip_running_lines(
    pages: list[str],
    *,
    min_fraction: float = 0.5,
    min_pages: int = 4,
    min_count: int = 3,
    max_window: int = 3,
    min_key_len: int = 12,
    max_key_len: int = 90,
    max_line_words: int = 12,
    side_fraction: float = 0.7,
) -> list[str]:
    """Remove running headers/footers: short lines repeated across most pages.

    Detection joins up to ``max_window`` consecutive candidate lines into a
    letters-only key, so a footer fragmented differently per page (e.g.
    "…for Ma" + "ritime Application-" vs "…for M" + "aritime Application-")
    still matches. A window appearing on >= the page threshold is dropped from
    every page. No-op for short documents or min_fraction <= 0.

    Print layouts routinely put a running head on one side only, so a recto-only
    footer cannot reach half of *all* pages — a 28-page booklet has 14 recto
    pages against a threshold of 14. Such a key is therefore also measured
    against its own side, and only when it is absent from the other side
    entirely: real page furniture alternates strictly, whereas repeated body
    text lands on both sides.
    """
    n = len(pages)
    if n < min_pages or min_fraction <= 0:
        return pages

    def window_keys(elig: list[tuple[int, str]]):
        """Yield (start_pos, span, key) for each joined run of candidate lines."""
        for a in range(len(elig)):
            joined = ""
            for w in range(max_window):
                if a + w >= len(elig):
                    break
                joined += elig[a + w][1]
                if min_key_len <= len(joined) <= max_key_len:
                    yield a, w + 1, joined

    page_counts: Counter = Counter()
    # Keyed by page parity: index 0 is page 1 (recto), index 1 is page 2 (verso).
    side_counts: tuple[Counter, Counter] = (Counter(), Counter())
    for index, text in enumerate(pages):
        elig = _eligible_lines(text.splitlines(), max_line_words)
        keys = {key for _, _, key in window_keys(elig)}
        page_counts.update(keys)
        side_counts[index % 2].update(keys)

    side_totals = ((n + 1) // 2, n // 2)
    threshold = max(min_count, math.ceil(min_fraction * n))

    def is_boilerplate(key: str, count: int) -> bool:
        if count >= threshold:
            return True
        for side, total in enumerate(side_totals):
            here, other = side_counts[side][key], side_counts[1 - side][key]
            if other or not total:
                continue  # appears on both sides — repeated content, not furniture
            if here >= min_count and here / total >= side_fraction:
                return True
        return False

    boilerplate = {key for key, c in page_counts.items() if is_boilerplate(key, c)}
    if not boilerplate:
        return pages

    out: list[str] = []
    for text in pages:
        lines = text.splitlines()
        elig = _eligible_lines(lines, max_line_words)
        drop: set[int] = set()
        for a, span, key in window_keys(elig):
            if key in boilerplate:
                drop.update(elig[a + k][0] for k in range(span))
        kept = [ln for i, ln in enumerate(lines) if i not in drop]
        out.append(_BLANK_RUNS.sub("\n\n", "\n".join(kept)).strip())
    return out
