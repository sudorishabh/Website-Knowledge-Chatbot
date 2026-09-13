"""A date a PDF states about itself in its own title, filename or link text.

The strongest free evidence there is about when a document was made, and until
now the system ignored it. ``TERI-Annual-Report-2024-25.pdf`` sat on a shelf
page whose Drupal node was typed in 2022 and was stored as 2022-02-09;
``newsTRAC-feb21.pdf`` as 2018-09-19; ``Auditor-Report-2024-25.pdf`` as
2018-04-04. Measured across the live corpus, 87 of the 237 multi-PDF
attachments whose title names a year carried a date from a different year, and
the interpreter in :mod:`app.ingestion.date_llm` caught none of them: it accepts
only an explicit publication *statement* in the body text, and a title is not a
sentence.

**Three sources, in order: the filename, the PDF's own DocInfo title, the Drupal
link text.** The filename and the internal title belong to the *file*; the link
label belongs to the *page* that links it, and a page's label is routinely
generic ("Annual Report", "Download") or stale where the file behind it was
swapped for a newer edition. So a clear filename is never overridden by a link
label: ``TERI-Annual-Report-2024-25.pdf`` behind the text "Annual Report" is the
2024-25 edition, and ``Annual_Report_2024.pdf`` behind the text "Report 2022" is
a 2024 document mislabelled on the page.

Link text stays last rather than being dropped, because it is the only source for
a file whose name carries nothing — Phase 0 found it names the edition for 10/10
annual reports, including one whose filename has no year at all.

**A year must be delimited.** This is the whole difference between a date and an
identifier. ``ES2009CE09.pdf`` is a project code — "ES", the scheme year, the
scheme — and reading 2009 out of it moved 137 single-PDF completed-project files
onto a date the file never stated. A four-digit run glued to a letter or digit on
either side is therefore never a year here. ``_2024.`` is; ``IoET_brochure_2026``
is; ``1695701669D2.1`` is not.

**Precision follows the statement.** "09 March 2026" gives a day, "feb21" a
month, "2024-25" a year. The value stored is the first day of the established
period and the precision says how much is actually known, the same contract
:mod:`app.ingestion.bundle_dates` uses for ``field_rpaper_year``. Nothing here
rounds a year up into a day.

Nothing here decides anything — :mod:`app.ingestion.date_resolution` weighs this
against the page's own date.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.core.editions import normalise_edition

__all__ = ["TITLE_SOURCES", "TitleDate", "read_title_date", "title_date"]

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
# Longest first, so "september" is not matched as "sep" leaving "tember".
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

# Not a word boundary: ``\b`` happily matches between a letter and a digit,
# which is exactly the ``ES2009CE09`` case. Alphanumeric on either side
# disqualifies.
_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"
_Y4 = r"(19[89]\d|20[0-3]\d)"
_SEP = r"[-./_]"
_GAP = r"[\s,._-]*"

# "10-02-2018", "01.10.2016", "7.10.2020" — day first, the convention this
# corpus is written in.
_DMY_RE = re.compile(_L + r"(\d{1,2})" + _SEP + r"(\d{1,2})" + _SEP + _Y4 + _R)
# "2024-09-15" — ISO, unambiguous because the year leads.
_YMD_RE = re.compile(_L + _Y4 + _SEP + r"(\d{1,2})" + _SEP + r"(\d{1,2})" + _R)
# "09 March 2026", "31 December 2016", "9 Mar 2026".
_DMONY_RE = re.compile(
    _L + r"(\d{1,2})\s*(?:st|nd|rd|th)?" + _GAP + "(" + _MONTH_RE + ")"
    + _GAP + _Y4 + _R,
    re.IGNORECASE,
)
# "March 09, 2026", "September 9 2024".
_MONDY_RE = re.compile(
    _L + "(" + _MONTH_RE + ")" + _GAP + r"(\d{1,2})(?:st|nd|rd|th)?"
    + _GAP + _Y4 + _R,
    re.IGNORECASE,
)
# "April-June2020", "Oct - Dec 2016" — a quarter. The period opens at the first
# month named, so that is the one taken.
_SPAN_RE = re.compile(
    _L + "(" + _MONTH_RE + r")\s*[-–/]\s*(?:" + _MONTH_RE + ")"
    + _GAP + _Y4 + _R,
    re.IGNORECASE,
)
# "September 2024", "nov 2019".
_MONY_RE = re.compile(_L + "(" + _MONTH_RE + ")" + _GAP + _Y4 + _R, re.IGNORECASE)
# "2024_September" — the order the tender filenames use.
_YMON_RE = re.compile(_L + _Y4 + _GAP + "(" + _MONTH_RE + ")" + _R, re.IGNORECASE)
# "nov19", "feb21", "may-07", "Nov'19". A two-digit year counts only when it is
# glued to the month or joined by a separator a human would not put between a
# month and a day — "Nov 19" with a plain space is far more likely the 19th.
_MONY2_RE = re.compile(
    _L + "(" + _MONTH_RE + r")['_-]?(\d{2})" + _R, re.IGNORECASE,
)
# A bare delimited year, the weakest reading and the last one tried.
_YEAR_RE = re.compile(_L + _Y4 + _R)

_EXTENSION_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip)$", re.IGNORECASE)

# A number introduced by one of these labels is a sequence number, not a day.
# "Issue 8, Nov 2019" is the eighth issue, published in November 2019 — reading
# it as 8 November invents a day the newsletter never claimed. Checked against
# the text immediately preceding a day-precision match, so the month/year
# reading below still gets its turn and the string is read as November 2019.
_SEQUENCE_LABEL_RE = re.compile(
    r"(?:issue|iss|no|num|number|vol|volume|part|chapter|ch|edition|series|page|p)"
    r"\.?\s*$",
    re.IGNORECASE,
)
#: How far back to look for that label. Long enough for "volume", short enough
#: that an unrelated earlier word cannot reach the number.
_LABEL_LOOKBACK = 10

# A year introduced by one of these is a policy *horizon* — a year the document
# is looking towards — not the year it was written. "Post-2015" is the name of a
# development agenda and `Post_2015_bulletin_and_TEDDY_launch.pdf` was released
# on 9 July 2014; "Agenda 2030" and "Vision 2030" are the same shape. Unlike the
# sequence-label guard this refuses the reading outright rather than falling
# through to a coarser one, because there is no weaker true reading underneath:
# the number is not a date at all.
#
# Immediately adjacent only. Widening the window to catch
# "Roadmap-to-India-2030" would start refusing years that merely follow a
# preposition somewhere earlier in a long filename, and that file is anchored by
# its page's stated date in any case.
_HORIZON_RE = re.compile(
    r"(?:post|agenda|vision|beyond|towards?|by|upto|until|till)[\s_\-]*$",
    re.IGNORECASE,
)
#: Enough for "towards" plus a separator.
_HORIZON_LOOKBACK = 9

# The corpus runs 1989-2030; anything outside is a scanner clock or an ID.
_MIN_YEAR = 1989
_MAX_YEAR = 2030


@dataclass(frozen=True)
class TitleDate:
    """A date the file's own naming states, and the words that state it."""

    #: Stored date: the first day of the period the statement establishes.
    normalized_value: date
    #: ``day`` | ``month`` | ``year`` — how much the statement actually says.
    precision: str
    #: The exact substring read, quoted verbatim into the audit row.
    raw_statement: str
    #: Which of the file's strings it came from: ``filename`` |
    #: ``pdf_internal_title`` | ``link_text``. Kept distinct rather than
    #: collapsed into one "title" notion, because the three are not equally
    #: trustworthy and an auditor asking "what exact file metadata gave us this
    #: date?" needs the answer, not a category.
    title_source: str
    #: What shape of statement it was, which is a different question from how
    #: precise it is — and the one that says how much weight it carries.
    #:
    #: ``full_date``, ``month_year`` and ``edition`` are *deliberate*: somebody
    #: wrote a date or a reporting period into the name of the file.
    #: ``bare_year`` is a four-digit number that merely happened to sit between
    #: two delimiters, and the corpus shows what that is worth —
    #: ``Post_2015_bulletin_and_TEDDY_launch.pdf`` is about the post-2015
    #: Development Agenda and was released in July 2014. A caller weighing this
    #: against other evidence must be able to tell the two apart; see
    #: :func:`app.ingestion.date_resolution._reconcile_with_title`.
    title_kind: str = "bare_year"


def _year2(value: str) -> int | None:
    """A two-digit year, read the way this corpus writes them."""
    number = int(value)
    if number <= _MAX_YEAR % 100:
        return 2000 + number
    if number >= _MIN_YEAR % 100:
        return 1900 + number
    return None


def _build(year: int, month: int, day: int, precision: str,
           match: re.Match, source: str, kind: str) -> TitleDate | None:
    """A TitleDate, or None if the numbers do not form a real date in range."""
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        return None
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    return TitleDate(normalized_value=value, precision=precision,
                     raw_statement=" ".join(match.group(0).split()),
                     title_source=source, title_kind=kind)


def read_title_date(text: str | None, source: str = "filename") -> TitleDate | None:
    """The most precise date ``text`` states, or None.

    Tried most-precise-first, so a string carrying both a full date and a stray
    year is read at the precision it actually offers.

    >>> read_title_date("Summary for Policymakers 09 March 2026.pdf").precision
    'day'
    >>> read_title_date("newsTRAC-feb21.pdf").precision
    'month'
    >>> read_title_date("TERI-Annual-Report-2024-25.pdf").precision
    'year'
    >>> read_title_date("ES2009CE09.pdf") is None   # a project code, not a date
    True
    >>> read_title_date("Post_2015_bulletin.pdf").title_kind
    'bare_year'
    """
    if not text:
        return None
    # The extension is never a date, and ".2018" would read as a separator.
    cleaned = _EXTENSION_RE.sub(" ", str(text))

    # Day precision.
    for pattern, order in ((_DMY_RE, (3, 2, 1)), (_YMD_RE, (1, 2, 3)),
                           (_DMONY_RE, (3, "m2", 1)), (_MONDY_RE, (3, "m1", 2))):
        match = pattern.search(cleaned)
        if match is None:
            continue
        if _SEQUENCE_LABEL_RE.search(
            cleaned[max(0, match.start() - _LABEL_LOOKBACK):match.start()]
        ):
            # A sequence number, not a day. Fall through to the month/year
            # reading, which is what the string actually establishes.
            continue
        year_group, month_group, day_group = order
        month = (_MONTHS[match.group(int(month_group[1])).lower()]
                 if isinstance(month_group, str) else int(match.group(month_group)))
        found = _build(int(match.group(year_group)), month,
                       int(match.group(day_group)), "day", match, source,
                       "full_date")
        if found is not None:
            return found

    # Month precision: the 1st is a marker for the month, never a day.
    for pattern, month_group, year_group in (
        (_SPAN_RE, 1, 2), (_MONY_RE, 1, 2), (_YMON_RE, 2, 1),
    ):
        match = pattern.search(cleaned)
        if match is None:
            continue
        found = _build(int(match.group(year_group)),
                       _MONTHS[match.group(month_group).lower()],
                       1, "month", match, source, "month_year")
        if found is not None:
            return found
    match = _MONY2_RE.search(cleaned)
    if match is not None:
        year = _year2(match.group(2))
        if year is not None:
            found = _build(year, _MONTHS[match.group(1).lower()], 1,
                           "month", match, source, "month_year")
            if found is not None:
                return found

    # Year precision. An edition span wins over a bare year because it is the
    # more specific reading of the same characters: "2016-17" is one statement
    # about a period, not two loose years.
    edition = normalise_edition(cleaned)
    if edition is not None:
        year = int(edition[:4])
        if _MIN_YEAR <= year <= _MAX_YEAR:
            return TitleDate(normalized_value=date(year, 1, 1), precision="year",
                             raw_statement=edition, title_source=source,
                             title_kind="edition")
    for match in _YEAR_RE.finditer(cleaned):
        if _HORIZON_RE.search(
            cleaned[max(0, match.start() - _HORIZON_LOOKBACK):match.start()]
        ):
            continue    # a year the document looks towards, not one it was written in
        return _build(int(match.group(1)), 1, 1, "year", match, source, "bare_year")
    return None


#: The sources a file's date may be read from, strongest first. The order is the
#: whole content of the rule, so it lives here as data rather than inline.
TITLE_SOURCES: tuple[str, ...] = ("filename", "pdf_internal_title", "link_text")


def title_date(*, link_text: str | None = None, filename: str | None = None,
               pdf_title: str | None = None) -> TitleDate | None:
    """The date this file states about itself, from the strongest source that
    states one.

    Filename, then the PDF's internal title, then the Drupal link text — see the
    module docstring for why that order. A source that names no date is skipped
    rather than ending the search, so a generic label costs nothing and the next
    source still answers.
    """
    available = {"filename": filename, "pdf_internal_title": pdf_title,
                 "link_text": link_text}
    for source in TITLE_SOURCES:
        found = read_title_date(available[source], source)
        if found is not None:
            return found
    return None
