"""Deterministic half of PDF date resolution: keep the page date, drop it, or ask.

**This module can no longer propose a date change.** It returns
``keep_page_date``, ``drop_page_date`` or ``needs_llm``. Every override in the
system originates elsewhere — from the file's own name
(:func:`app.ingestion.date_resolution.title_override`), from a copyright
statement its DocInfo corroborates, or from an explicit publication statement in
the document's text.

``drop_page_date`` is the one addition to that narrowing, and it narrows further
rather than widening: it says the page's date is not this file's to borrow. See
:func:`page_date_is_usable`. The rules themselves are untouched by it — a file
that ended at ``multi_pdf_uploaded_with_page`` still ends there, still for free —
only the value that outcome yields changes, from a shared stamp to nothing.

That is a deliberate narrowing after manual review. The previous version treated
a late upload as a publication date, which conflates two different facts: when a
file was *put on the server* and when the document was *released*. Drupal's
``file.created``, a ``/files/YYYY-MM/`` path, a PDF ``CreationDate``, an event
date, a notification date and an effective date are all evidence of something —
but none of them is, by itself, a publication date. A **year in the filename** is
the one item that has since moved off this list, and only on a page holding
several PDFs, where it is the sole thing distinguishing one file from another;
see :func:`app.ingestion.date_resolution.title_override` for how narrowly.

Upload timing keeps a job: it decides **where it is worth spending money**. A
PDF that Drupal recorded arriving long after its page is a good candidate for
having its own publication date, so it is routed to the LLM to look for one.
If the LLM finds nothing explicit, the page date stands. The upload facts are
carried on the decision as ``supporting_evidence`` so a reviewer can see what
triggered the look, but nothing acts on them.

What survives from the earlier analysis:

- **Single-PDF pages** default to the page date. Measured on the 439 attachments
  whose file arrived days-to-weeks after the node, 76% were authored within 30
  days of the node and 89% read as "written and posted together".
- **The 2017-2018 migration cohort** is real and must never be read as upload
  timing: 1406 of 1545 pre-cutoff files share four timestamps, one of them
  covering 397 files whose nodes span 13.5 years.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from app.ingestion.date_evidence import PdfEvidence, month_start, parse_dt

__all__ = [
    "DateDecision",
    "MIGRATION_END",
    "MIGRATION_START",
    "decide",
    "in_migration_cohort",
    "page_date_is_usable",
]

# The 2017-2018 content migration. `file.created` inside this window is an import
# timestamp, not an upload. Equivalent to fid <= 3760 on this site (verified zero
# overlap with post-cutoff fids), so either test identifies the same cohort.
MIGRATION_START = datetime(2017, 12, 1, tzinfo=timezone.utc)
MIGRATION_END = datetime(2018, 6, 1, tzinfo=timezone.utc)

# Upload divergence large enough to be worth an LLM look on a multi-PDF page.
# Below it the file is part of the page's own publication (measured median 14
# days between page creation and attachment).
SEPARATION_DAYS = 90

# On a single-PDF page the bar for even looking is a year: one PDF on a page is
# overwhelmingly that page's own document.
SINGLE_PDF_LOOK_DAYS = 365

# `decide` returns the first four. "propose_override" exists because the LLM path
# and the two deterministic override rules in `app.ingestion.date_resolution`
# build a DateDecision from evidence this module never sees.
#
# "drop_page_date" is the outcome for a file the system cannot date: the document
# ends up with no date at all rather than a borrowed one. See
# `page_date_is_usable` for when, and why that is the better answer.
Action = Literal[
    "keep_page_date", "drop_page_date", "needs_llm", "needs_manual_review",
    "propose_override",
]
DateType = Literal[
    "publication", "upload", "authoring", "edition", "event",
    "notification", "effective", "unknown",
]


def in_migration_cohort(file_created: str | None) -> bool:
    """Is this file entity a 2017-2018 migration import rather than an upload?"""
    parsed = parse_dt(file_created)
    return parsed is not None and parsed < MIGRATION_END


def page_date_is_usable(page) -> bool:
    """May a file on this page fall back to the page's own date?

    Two conditions have to hold together for the answer to be no, and either one
    alone is fine:

    **Several PDFs on the page.** One file on a page is that page's document and
    inherits its date; several are several documents that accreted over years.

    **The page's date is only its Drupal creation stamp.** When the bundle states
    a date in a configured CMS field — an event's start date, a project's — that
    date really is about the content, and every file on the page belongs to it: an
    event's agenda, concept note and slide deck all happened on the day of the
    event. When it does not, the date is the day somebody typed the node, and on a
    shelf page it is a date about the *shelf*.

    Both together is the shape that was producing plainly wrong dates:
    ``TERI-Annual-Report-2024-25.pdf`` stored as 2022-02-09,
    ``Auditor-Report-2024-25.pdf`` as 2018-04-04, 69 FCRA financial statements
    all sharing one stamp. For those, **no date is the honest answer** — a
    date-scoped query that misses a document is recoverable, one that confidently
    returns it under the wrong year is not.

    Measured on the live corpus this leaves 318 attachments undated, out of 1,523
    on multi-PDF pages: the rest are settled by the file's own title, a
    corroborated copyright year, or a page whose bundle states a real date.
    """
    return not (page.is_multi_pdf and not page.date_from_bundle_field)


@dataclass
class DateDecision:
    """A proposed outcome for one PDF. Never applied by this module."""

    document_id: str
    action: Action
    candidate_start_date: str | None = None
    #: How precise ``candidate_start_date`` is. ``day`` for every rule in this
    #: module and for the LLM path, which only ever quotes a stated day; ``year``
    #: for the copyright rule in :mod:`app.ingestion.date_resolution`, whose
    #: 1 January is a marker for the year and must be read as one.
    candidate_precision: str = "day"
    date_type: DateType = "unknown"
    edition_label: str | None = None
    source: str = "node_effective_date"
    confidence: float = 0.0
    evidence: str = ""
    rule: str = ""
    decided_by: Literal["deterministic", "llm"] = "deterministic"
    #: Facts that justified *looking*, never a date change on their own.
    supporting_evidence: str = ""
    used: list[str] = field(default_factory=list)
    # ------------------------------------------------------------------ #
    # Provenance for a date read from the file's own naming. All three are
    # None for every other path. Deliberately *not* folded into
    # :attr:`date_type` or :attr:`source`, which answer different questions
    # with closed vocabularies of their own (and a test asserts they do not
    # overlap). Together they answer "what exact file metadata gave us this
    # date?" — which the prose in :attr:`evidence` also answers, but not in a
    # form anything can group by.
    # ------------------------------------------------------------------ #
    #: Which of the file's strings answered: ``filename`` |
    #: ``pdf_internal_title`` | ``link_text``.
    title_source: str | None = None
    #: What shape of statement it made: ``full_date`` | ``month_year`` |
    #: ``edition`` | ``bare_year``.
    title_kind: str | None = None
    #: What became of it: ``replaced`` | ``refined`` | ``corroborating`` |
    #: ``rejected``. Recorded even when the name did *not* set the date,
    #: because "the filename clearly says 2024 — why is this dated 2018?" is
    #: exactly the question that needs an answer on the row.
    title_disposition: str | None = None

    @property
    def would_move(self) -> bool:
        return self.action == "propose_override" and bool(self.candidate_start_date)


def _days(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    return (later - earlier).days


def decide(evidence: PdfEvidence) -> DateDecision:
    """Keep the page date, or route to the LLM. Never proposes a change."""
    page = evidence.page
    node_dt = parse_dt(page.node_created)
    file_dt = parse_dt(evidence.file_created)
    pdf_dt = parse_dt(evidence.pdf_created)
    upload_dt = month_start(evidence.upload_month)

    base = {
        "document_id": evidence.document_id,
        "edition_label": evidence.edition,
        "date_type": "publication",
        # The page's *resolved* date, not its creation stamp: that is what a
        # kept decision actually assigns, so it is what the audit row has to
        # record. `node_dt` above stays the creation stamp because the upload-gap
        # arithmetic below is a question about when the page was made.
        "candidate_start_date": page.effective_date,
        "source": "node_effective_date",
    }

    if page.effective_date is None:
        return DateDecision(
            **base, action="needs_manual_review", confidence=0.0,
            rule="no_page_date", used=["drupal"],
            evidence="The page carries no date to fall back on.",
        )

    # ------------------------------------------------------------------ #
    # Case 0 — the page's bundle states its date in a CMS field, and this is
    # the page's *only* PDF.
    #
    # One file on a page is part of that page's own publication, so where the
    # page's date is one the CMS states about this content type — a research
    # paper's year, a press release's date — the page is authoritative and there
    # is nothing to look for: reading the file could only produce a *different*
    # date, which is precisely what must not happen. Checked before every upload
    # heuristic because those exist to decide whether a weak page date is worth
    # questioning, and this page date is not weak.
    #
    # It is also what stops the file's own timestamps mattering: DocInfo,
    # `file.created` and the `/files/YYYY-MM/` month are never read on this path.
    #
    # The PDF-count condition is the whole of the multi-PDF requirement. Several
    # PDFs on one page are several documents: a shelf accretes editions and
    # reports that were published at different times, and an authoritative date
    # for the *page* says nothing about when each *file* on it came out. So a
    # file that shares its page falls through to the evidence path below, where
    # the page's date becomes its fallback rather than its answer. Measured on
    # the live corpus, this branch was answering for 1,042 of the 1,582
    # multi-PDF attachment links without opening one of them.
    # ------------------------------------------------------------------ #
    if page.date_from_bundle_field and not page.is_multi_pdf:
        return DateDecision(
            **base, action="keep_page_date", confidence=1.0,
            rule="parent_bundle_date_field", used=["drupal"],
            evidence=(
                f"The parent {page.bundle} page states its date in "
                f"{page.date_field} ({page.date_field_value!r}); it holds one "
                f"PDF, so that file carries its page's date."
            ),
            supporting_evidence=(
                "File timestamps, upload month and PDF metadata were not read: "
                "the page's own field is authoritative."
            ),
        )

    # Upload divergence: recorded as context, never as a reason to change a date.
    upload_gap = _days(file_dt, node_dt)
    if upload_gap is None and upload_dt is not None:
        upload_gap = _days(upload_dt, node_dt)
    migrated = in_migration_cohort(evidence.file_created)
    if migrated:
        support = ("Drupal file date falls in the 2017-2018 migration import, so it "
                   "records the import, not an upload.")
    elif upload_gap is not None:
        where = "Drupal" if file_dt is not None else "the /files/YYYY-MM/ path"
        support = (f"{where} records the file arriving {upload_gap} days "
                   f"{'after' if upload_gap >= 0 else 'before'} the page was created.")
    else:
        support = "No upload record for this file (in-body link)."

    usable = page_date_is_usable(page)

    def keep(rule: str, why: str, confidence: float = 0.9) -> DateDecision:
        """Fall back to the page's date — or, where that is not the file's to
        borrow, to no date at all. Routed through one helper so every branch
        below inherits the rule without restating it, and so a branch added
        later cannot quietly reintroduce the borrowed date."""
        if usable:
            return DateDecision(
                **base, action="keep_page_date", confidence=confidence, rule=rule,
                evidence=why, supporting_evidence=support, used=["drupal"],
            )
        return DateDecision(
            **{**base, "candidate_start_date": None}, action="drop_page_date",
            confidence=0.0, rule=rule, used=["drupal"],
            evidence=(
                f"{why} One of {page.pdf_count} PDFs on a {page.bundle} page whose "
                f"own date is only its Drupal creation stamp, and the file states "
                f"no date of its own — so it is left undated rather than given the "
                f"day the page was typed."
            ),
            supporting_evidence=support,
        )

    def look(rule: str, why: str) -> DateDecision:
        return DateDecision(
            **base, action="needs_llm", confidence=0.0, rule=rule,
            evidence=why, supporting_evidence=support, used=["drupal", "pdf_meta"],
        )

    # ------------------------------------------------------------------ #
    # Case 1 — single-PDF page.
    # ------------------------------------------------------------------ #
    if not page.is_multi_pdf:
        # A very late upload is worth *looking* at, but the page date stands
        # unless the document itself states a publication date.
        if (
            file_dt is not None
            and not migrated
            and (upload_gap or 0) > SINGLE_PDF_LOOK_DAYS
        ):
            return look(
                "single_pdf_late_upload_review",
                "Only PDF on the page, but it was uploaded more than a year later; "
                "checking the document for an explicit publication date.",
            )
        return keep(
            "single_pdf_page",
            "The page holds exactly one PDF, so the document is part of the page's "
            "own publication.",
        )

    # ------------------------------------------------------------------ #
    # Case 2 — multi-PDF page. Several PDFs does NOT by itself mean each has
    # its own publication date; it only makes that possible.
    # ------------------------------------------------------------------ #
    if file_dt is not None and not migrated:
        if (upload_gap or 0) > SEPARATION_DAYS:
            return look(
                "multi_pdf_late_upload_review",
                f"One of {page.pdf_count} PDFs on the page, uploaded well after it; "
                "checking the document for its own publication date.",
            )
        return keep(
            "multi_pdf_uploaded_with_page",
            f"One of {page.pdf_count} PDFs, uploaded within {SEPARATION_DAYS} days "
            "of the page — part of the page's own publication.",
            confidence=0.85,
        )

    if file_dt is None and upload_dt is not None:
        if (upload_gap or 0) > SEPARATION_DAYS:
            return look(
                "multi_pdf_url_month_review",
                "In-body PDF whose managed path shows it stored well after the page; "
                "checking the document for its own publication date.",
            )
        return keep(
            "multi_pdf_url_month_matches",
            f"Stored under /files/{evidence.upload_month}/, close to the page's date.",
            confidence=0.8,
        )

    if migrated:
        if pdf_dt is None and not evidence.head_text:
            return keep(
                "migration_cohort_no_evidence",
                "File date is a migration import and the PDF offers no readable "
                "evidence; nothing better than the page date.",
                confidence=0.5,
            )
        return look(
            "migration_cohort_review",
            "File date is a migration import, so only the document's own content "
            "could date it.",
        )

    # In-body PDF with no upload signal — the annual-report shape. Every edition
    # shares one page date and the only per-file evidence is textual.
    if pdf_dt is not None or evidence.head_text or evidence.edition or evidence.text_years:
        return look(
            "multi_pdf_textual_only",
            f"One of {page.pdf_count} PDFs with no Drupal upload record; the only "
            "per-document evidence is textual.",
        )

    return keep(
        "multi_pdf_no_evidence",
        "No per-document evidence of any kind; the page date stands.",
        confidence=0.5,
    )
