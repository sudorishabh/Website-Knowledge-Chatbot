"""How a catalog row reads in an answer: its date, its link, its one-line item.

Shared by the tools' renderings and the detail sections that follow a headline,
so a document reads the same way wherever a catalog answer shows it. Nothing
here queries; every function is a pure formatting of values already on hand.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

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


def _tidy_name(name: Any) -> str:
    # Free-text author fields carry separators from the byline they were cut
    # out of ("& Sharma, A."); a trailing full stop is an initial and stays.
    return " ".join(str(name or "").split()).strip("&,;:/|- ").rstrip(",;&/|-")


def names(people: Sequence[str], *, most: int = 2) -> str:
    """"A", "A and B", "A, B and 3 others" — a byline that stays one line.
    Names are tidied of stray separators and listed alphabetically."""
    people = sorted({tidy for p in people if (tidy := _tidy_name(p))}, key=str.casefold)
    if len(people) > most:
        rest = len(people) - most
        return f"{', '.join(people[:most])} and {rest} other{'s' if rest > 1 else ''}"
    if len(people) <= 1:
        return "".join(people)
    return f"{', '.join(people[:-1])} and {people[-1]}"


def item_line(
    record: Any, *, with_type: bool = False, authors: Sequence[str] = (),
    listed_by: str | None = None,
) -> str:
    """One bulleted document: its linked title, then what kind of item it is
    (when the list spans several kinds), its date and who wrote it.

    In a list of one author's work (``listed_by``) that author goes without
    saying on every line, so only the co-authors are named — "with ..."."""
    title = getattr(record, "title", None) or getattr(record, "document_id", "")
    bundle = getattr(record, "bundle", None)
    kind = entity_label(bundle, 1) if with_type and bundle else ""
    meta = ", ".join(part for part in (kind, record_date(record)) if part)
    byline = ""
    if listed_by:
        others = [a for a in authors if listed_by.casefold() not in a.casefold()]
        byline = f"with {names(others)}" if others else ""
    elif authors:
        byline = f"by {names(authors)}"
    if byline:
        meta = f"{meta} · {byline}" if meta else byline
    head = md_link(title, getattr(record, "url", None))
    return f"- {head} — {meta}" if meta else f"- {head}"


def card(record: Any, *, authors: Sequence[str] = (), themes: Sequence[str] = ()) -> str:
    """One document on its own: the linked title, then what it is and when,
    who wrote it, and the themes it is filed under — one fact per line."""
    title = getattr(record, "title", None) or getattr(record, "document_id", "")
    bundle = getattr(record, "bundle", None)
    kind = entity_label(bundle, 1).capitalize() if bundle else ""
    lines = [f"**{md_link(title, getattr(record, 'url', None))}**"]
    meta = " · ".join(part for part in (kind, record_date(record)) if part)
    if meta:
        lines.append(meta)
    if authors:
        lines.append(f"By {names(authors, most=6)}")
    if themes:
        lines.append(f"Themes: {', '.join(themes)}")
    return "\n".join(lines)
