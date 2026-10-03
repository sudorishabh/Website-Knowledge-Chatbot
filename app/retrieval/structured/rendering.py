"""How a catalog row reads in an answer: its date, its link, its one-line item.

Shared by the tools' renderings and the detail sections that follow a headline,
so a document reads the same way wherever a catalog answer shows it. Nothing
here queries; every function is a pure formatting of values already on hand.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.retrieval.structured.entities import entity_label

# Spelled out rather than taken from strftime("%b"), which follows the process
# locale and would render a month in whatever language the server runs in.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value or "").strip()[:10]).date()
    except ValueError:
        return None


def display_date(value: Any, precision: str | None = None) -> str:
    """A catalog date at exactly the precision the source established:
    "27 Apr 2026", "Apr 2026" or "2026"; "" when there is none.

    A year- or month-precision value is stored as the 1st (of January) because
    the column holds a full date, so showing the day would assert one nobody
    stated. An unrecorded precision is shown in full, as it always was."""
    parsed = _as_date(value)
    if parsed is None:
        return str(value or "").strip()[:10]
    if precision == "year":
        return str(parsed.year)
    if precision == "month":
        return f"{_MONTHS[parsed.month - 1]} {parsed.year}"
    return f"{parsed.day} {_MONTHS[parsed.month - 1]} {parsed.year}"


def md_link(title: Any, url: Any) -> str:
    """`[title](url)`, safe for the chat UI's markdown: brackets in the title
    and spaces or parentheses in the URL would otherwise end the link early.
    A URL that is not http(s) is never linked; the title stands alone."""
    label = " ".join(str(title or "").split()).replace("[", "(").replace("]", ")")
    href = str(url or "").strip()
    if not href.startswith(("http://", "https://")):
        return label
    href = href.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[{label}]({href})"


def record_date(record: Any) -> str:
    return display_date(getattr(record, "effective_start_date", None),
                        getattr(record, "start_precision", None))


def item_line(record: Any, *, with_type: bool = False) -> str:
    """One bulleted document: its linked title, then what kind of item it is
    (when the list spans several kinds) and its date."""
    title = getattr(record, "title", None) or getattr(record, "document_id", "")
    bundle = getattr(record, "bundle", None)
    kind = entity_label(bundle, 1) if with_type and bundle else ""
    meta = ", ".join(part for part in (kind, record_date(record)) if part)
    head = md_link(title, getattr(record, "url", None))
    return f"- {head} — {meta}" if meta else f"- {head}"
