"""Canonical form for a reporting-period label like ``2024-25``.

An edition label names the period a document *covers*. It is never a date: an
annual report for 2024-25 was not published on 2024-01-01, and the whole date
resolver exists to keep those two facts apart (see
:mod:`app.ingestion.date_rules`). This module owns only the label's *spelling*.

It exists because the same rule was written four times — in
``app.ingestion.date_evidence``, in ``scripts.eval_date_resolution``, in
``scripts.backfill_edition_and_titles`` and implicitly wherever a label was
compared — and the corpus shows what that costs: ``2024-25``, ``2019-2020``,
``2020/21`` and ``FY 20-21`` all stored for the same kind of value, which makes
the field unusable for matching or ordering.

**Canonical form is ``YYYY-YY``**, zero-padded and fixed width, so plain string
comparison orders editions correctly and ``max()`` is "the newest". That is why
no separate sort key is needed.

**Only consecutive spans are editions.** ``2024-25`` is one; ``2019-2024`` is a
range and ``Report 2 - 3`` is nothing. A value that names no consecutive span is
not normalised into one — the caller gets ``None`` and decides what to do, which
for a stored label means keeping what the source actually said rather than
silently discarding it.
"""
from __future__ import annotations

import re

__all__ = ["EDITION_RE", "edition_end_year", "find_editions", "normalise_edition"]

# A fiscal/edition span: "2024-25", "2024-2025", "2024_25", "20-21", "2020/21".
EDITION_RE = re.compile(r"(?<!\d)(20\d{2}|\d{2})\s*[-_/–]\s*(\d{2,4})(?!\d)")


def edition_end_year(label: str) -> int:
    """The year a canonical ``YYYY-YY`` edition *ends* in.

    ``2024-25`` ends in 2025. This is arithmetic rather than parsing, and
    exactly: :func:`find_editions` only produces a label for a **consecutive**
    span, so the end is always the start plus one. A non-consecutive pair never
    becomes a label in the first place.

    It lives here, beside the spelling rule, because "what does 2024-25 mean" is
    the same kind of question as "how is 2024-25 written", and the corpus has
    already paid once for that knowledge being in four places.

    >>> edition_end_year("2024-25")
    2025
    >>> edition_end_year("2019-20")
    2020
    """
    return int(label[:4]) + 1


def find_editions(value: str | None) -> list[str]:
    """Every canonical edition named in ``value``, in the order they appear.

    Deduplicated while keeping first-seen order, so a question naming two
    editions ("compare 2019-20 and 2024-25") yields both — which is what lets a
    comparison be scoped to exactly the documents it is about.

    >>> find_editions("compare 2019-20 with the 2024-2025 report")
    ['2019-20', '2024-25']
    >>> find_editions("2019-2024")   # a range, not an edition
    []
    """
    found: list[str] = []
    for start, end in EDITION_RE.findall(str(value or "")):
        start_year = int(start) if len(start) == 4 else 2000 + int(start)
        end_short = int(end) % 100
        if 2000 <= start_year <= 2030 and (end_short - start_year % 100) % 100 == 1:
            label = f"{start_year}-{end_short:02d}"
            if label not in found:
                found.append(label)
    return found


def normalise_edition(value: str | None) -> str | None:
    """The canonical ``YYYY-YY`` edition named in ``value``, or None.

    The first consecutive year span, rewritten. A two-digit start is read as
    20xx, and the end is compared modulo a century so ``2019-20`` and
    ``1999-00`` are both consecutive.

    >>> normalise_edition("Annual Report 2024-2025")
    '2024-25'
    >>> normalise_edition("2020/21")
    '2020-21'
    >>> normalise_edition("FY 20-21")
    '2020-21'
    >>> normalise_edition("2019-2024") is None   # a range, not an edition
    True
    """
    found = find_editions(value)
    return found[0] if found else None
