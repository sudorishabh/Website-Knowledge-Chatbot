"""The one place a PDF's ``effective_start_date`` is decided.

This is the canonical entry point for the date-resolution behaviour validated in
Phase 0 (see ``reports/phase0/full_corpus_v3_final_report.md``). Every
ingestion path that builds a PDF document calls :func:`resolve`; the rules
themselves live in :mod:`app.ingestion.date_rules` and
:mod:`app.ingestion.date_llm` and are not duplicated anywhere.

The contract:

**One PDF on a page is that page's document.** It inherits the page's date
unopened, whatever the file is called and whatever its timestamps say. Being
uploaded later, having a later ``file.created``, sitting under a later
``/files/YYYY-MM/`` path or carrying a later PDF ``CreationDate`` are all
*supporting signals*: they decide whether a document is worth reading closely,
and never set a date.

**Several PDFs on one page are several documents.** A shelf accretes editions
and reports published years apart, so the page's date is a fact about the shelf.
Two things follow, and together they are what fixed the reported failure —
``TERI-Annual-Report-2024-25.pdf`` stored as 2022-02-09, 69 FCRA statements all
sharing 2018-04-04:

* **the file's own name decides** (:func:`title_override`), because on a shelf it
  is the only thing telling one file from another; and
* **where nothing states a date, the file gets none.** Not the shelf's creation
  stamp. See :func:`app.ingestion.date_rules.page_date_is_usable` for the exact
  condition, and for why a page whose *bundle* states a date — an event, a
  project — still hands that date to every file on it.

**An override needs the document to say so** — in its name, or in its text.
Three paths can propose one, and they are weighed in that order:

:func:`title_override` reads the file's own naming — the filename, then the PDF's
DocInfo title, then the Drupal link text, strongest first, because the first two
belong to the *file* and the label belongs to the *page* that links it. A full
date or a reporting period settles the file outright; a month or a bare year
fixes the *period* and the document's text may sharpen it inside that period, but
nothing may contradict it. See :func:`_reconcile_with_title`.

:func:`copyright_override`, the deterministic text rule, proposes a *year*
(stored as 1 January with ``candidate_precision="year"``) when the front matter
carries a copyright statement **and** the PDF's own DocInfo creation date names
the same year — two independent facts agreeing, and neither of them a Drupal
timestamp.

:mod:`app.ingestion.date_llm` proposes a *date at the precision its evidence
supports* when the verdict survives every gate — a quoted publication statement,
that statement present in the PDF's own text, the statement carrying the proposed
date, publication linkage, and confidence at or above the threshold. A statement
naming a day gives a day; one naming a month gives that month; one naming only a
year gives that year. The value stored is the first day of the established period
and the precision says how much is known, so nothing is invented in either
direction. Anything short of that leaves the page's date where it may be
inherited, no date where it may not, and a review row where a date was seen.

Cost follows the same routing that was measured, with one path added in front of
it: a name that answers settles the file for free. Otherwise the deterministic
pass settles the large majority, only the routed remainder has its text read, and
the model is called only for what survives that. Nothing here downloads
anything — the caller already holds the PDF bytes — and Document Intelligence is
unreachable, because this module does not import
:mod:`app.ingestion.extractors.pdf_extractor`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from app.ingestion.date_evidence import (
    PageContext,
    PdfEvidence,
    copyright_statement,
    parse_dt,
    read_pdf_front_matter,
    read_pdf_head,
)
from app.ingestion.date_rules import DateDecision, decide, page_date_is_usable

logger = logging.getLogger(__name__)

__all__ = [
    "ResolvedDate", "TitleFinding", "build_evidence", "copyright_override",
    "read_title_evidence", "resolve", "title_override",
]


@dataclass
class ResolvedDate:
    """What ingestion should use for one attached file, plus why.

    The four date fields are named exactly as
    :class:`app.ingestion.bundle_dates.EffectiveDate`'s, because they mean the
    same things and a caller holding either should read the same way. Only
    ``start_value`` and ``end_value`` reach the document; ``decision`` carries
    the provenance for the decision table and the review queue and is
    deliberately not part of the chunk payload.
    """

    #: The document's primary date — the parent page's effective start date,
    #: or a day the file's own text states and verified.
    start_value: str | None
    #: Precision of :attr:`start_value`. Inherited from the parent page, so a
    #: file hanging off a research paper is year-precision too and no reader
    #: renders its 1 January as a day. For an override it is the decision's
    #: own: ``day`` from the LLM path, which quotes a stated day, ``year`` from
    #: the copyright rule, which quotes a stated year, and whatever the file's
    #: own name actually established for the title rule. None exactly when
    #: :attr:`start_value` is None — there is no precision for a date that is
    #: not there, and a leftover "day" would read as a claim.
    start_precision: str | None = "day"
    #: The end of the period the parent page's content covers, inherited whole.
    #: None for a single-date page, and None for an override — a quoted
    #: statement gives a day, never a period.
    end_value: str | None = None
    end_precision: str | None = None
    edition_label: str | None = None
    decision: DateDecision | None = None
    #: The model's raw verdict, when one was obtained, for the audit trail.
    llm_raw: dict[str, Any] | None = None
    #: Evidence tiers actually used, for cost accounting.
    used: list[str] = field(default_factory=list)

    @property
    def overridden(self) -> bool:
        return bool(self.decision and self.decision.action == "propose_override")

    @property
    def canonical_source(self) -> str:
        """The value ``documents.date_source`` should carry for this outcome.

        Two vocabularies meet here and neither is wrong. ``DateDecision.source``
        is *provenance*: it names the field, rule or model that produced the
        verdict, and the decision table records it verbatim. ``date_source`` on
        the document is the *canonical* four-then-five value vocabulary the
        query layer reads, and it must never carry a Drupal field name or a
        rule name.

        This is the map between them, and it lives here because this class is
        what the caller holds. An inherited date is ``parent_page``. A quoted
        publication statement is ``document_text``. A corroborated copyright
        year is ``document_copyright`` — which used to be recorded as
        ``document_text``, claiming a verified publication statement for a
        document that had only stated a year. A date the file's own name states
        is ``document_title``. A file the system declined to date is
        ``no_evidence``: there is no value, and the *reason* there is no value is
        worth as much as a value would have been.
        """
        if self.dropped:
            return "no_evidence"
        if not self.overridden:
            return "parent_page"
        source = self.decision.source or ""
        if source in ("document_copyright", "document_title"):
            return source
        return "document_text"

    @property
    def dropped(self) -> bool:
        """Was this file deliberately left undated? See
        :func:`app.ingestion.date_rules.page_date_is_usable`."""
        return bool(self.decision and self.decision.action == "drop_page_date")

    @property
    def needs_review(self) -> bool:
        return bool(self.decision and self.decision.action == "needs_manual_review")


def build_evidence(
    *,
    document_id: str,
    node: Any,
    file: Any,
    page_pdf_count: int | None = None,
    parent_date: Any = None,
) -> PdfEvidence:
    """Adapt a Drupal ``(record, file)`` pair into the evidence model.

    ``page_pdf_count`` is what tells a single-document page from a shelf that
    accreted documents over years. It defaults to the number of files the node
    carries, which is exactly what the crawl already resolved for it.

    ``parent_date`` is the page's :class:`app.ingestion.bundle_dates.
    EffectiveDate`, resolved **once** by the caller and passed in rather than
    re-derived here. That is what makes "every PDF on a page carries the page's
    date" true by construction for a page holding one file or twelve: there is
    only ever one resolution to disagree with. Resolved here when the caller did
    not, so a test or a tool that has a node and a file needs nothing else.
    """
    from app.ingestion.bundle_dates import resolve_effective_dates

    files = getattr(node, "files", None) or []
    count = page_pdf_count if page_pdf_count is not None else max(1, len(files))
    if parent_date is None:
        parent_date = resolve_effective_dates(
            getattr(node, "bundle", None),
            getattr(node, "created", None),
            getattr(node, "metadata", None),
        )
    return PdfEvidence(
        document_id=document_id,
        origin=getattr(file, "origin", "attachment"),
        url=getattr(file, "url", None),
        filename=getattr(file, "filename", None),
        anchor=getattr(file, "description", None) or None,
        file_created=getattr(file, "created", None),
        page=PageContext(
            node_uuid=getattr(node, "uuid", "") or "",
            node_title=getattr(node, "title", "") or "",
            node_created=getattr(node, "created", None),
            node_start_date=parent_date.start_value,
            node_start_precision=parent_date.start_precision,
            node_end_date=parent_date.end_value,
            node_end_precision=parent_date.end_precision,
            date_field=parent_date.start_field,
            date_field_value=parent_date.start_raw,
            end_date_field=parent_date.end_field,
            end_date_field_value=parent_date.end_raw,
            date_source=parent_date.source,
            bundle=getattr(node, "bundle", None),
            url=getattr(node, "url", None),
            pdf_count=count,
        ),
    )


def _read_pdf_signals(evidence: PdfEvidence, content: bytes) -> None:
    """Fill DocInfo and head text from bytes already in hand. PyMuPDF only."""
    from app.ingestion.date_candidates import read_pdf_docinfo

    created, modified = read_pdf_docinfo(content)
    text, title = read_pdf_head(content)
    evidence.pdf_created = created
    evidence.pdf_modified = modified
    evidence.pdf_title = title
    evidence.head_text = text
    evidence.front_text = read_pdf_front_matter(content)



def _same_period(precision: str, title_value, page_value) -> bool:
    """Do a title's date and a page's date name the same period, compared at
    ``precision``? A year-precision comparison asks only about the year."""
    if page_value is None:
        return False
    page_date = page_value.date() if hasattr(page_value, "date") else page_value
    if precision == "year":
        return page_date.year == title_value.year
    if precision == "month":
        return (page_date.year, page_date.month) == (title_value.year,
                                                     title_value.month)
    return page_date == title_value


@dataclass(frozen=True)
class TitleFinding:
    """What the file's own naming said, and what became of it.

    Both halves are needed downstream and for different reasons. ``decision`` is
    the override to apply, when there is one. ``disposition`` is the audit
    answer to "the filename clearly says 2024 — why is this document dated
    2018?", which has to be answerable *especially* in the cases where the name
    did not win.
    """

    #: What the naming stated, or None if it stated nothing usable.
    found: object = None
    #: The override it proposes, or None if it does not.
    decision: DateDecision | None = None
    #: ``replaced`` — the name set the date.
    #: ``refined``  — the name set the period and the document's text sharpened
    #:                it inside that period.
    #: ``corroborating`` — the name agreed with a date the CMS states, so it
    #:                changed nothing and nothing was lost by declining.
    #: ``rejected`` — the name stated something and it was not used: a bare year
    #:                against a stated CMS date, or a reading the document's own
    #:                text contradicted.
    #: ``none``     — the naming stated no date at all.
    disposition: str = "none"


def _reading(found) -> str:
    """The clause that explains a reading a reader would otherwise have to infer.

    Only editions need one, and they need it badly: nothing about the pair
    ``'2024-25'`` and ``2025-01-01`` says on its face which end of the span was
    taken, so "why is this 2025 and not 2024?" would be unanswerable from the
    stored row. Everything else reads straight off its own statement.
    """
    if found.title_kind != "edition":
        return ""
    return (f" — the year the {found.raw_statement} period ends in, which is "
            f"when an edition covering it is published")


def read_title_evidence(evidence: PdfEvidence) -> TitleFinding:
    """The date the file's own naming states, and whether it may be used.

    The cheapest evidence in the system: three strings the crawl already holds —
    the filename, the PDF's DocInfo title, the Drupal link text — read with no
    download, no text extraction and no model call.

    **It applies only where several PDFs share a page**, and that restriction is
    the point rather than a caution. A page holding one file is that file's page:
    its date is a claim about the document, and the document inherits it
    unconditionally — no name is read, nothing is weighed. A page holding twelve
    is a shelf, its date is a claim about the shelf, and the only thing that
    tells one file on it from another is what the file is called. That is the
    case this rule exists for: ``TERI-Annual-Report-2024-25.pdf`` and
    ``Auditor-Report-2024-25.pdf`` both sat on shelves stamped years earlier.

    Restricting it this way is also what keeps the edition/date distinction
    intact where it matters. ``TEDDY-2015-16-press-release.pdf`` is the only file
    on its page and really was released on 2016-03-22; ``2015-16`` is the edition
    TEDDY covers, not a date, and this rule never sees it.

    Two declines, and they mean different things:

    *Corroborating.* A name agreeing with a date the **CMS states** changes
    nothing: replacing a stated day with the period containing it makes the
    record vaguer without making it truer, which is the guard
    :func:`copyright_override` and the LLM year-check apply too.

    *Rejected.* A **bare year** does not displace a date the CMS states.
    ``c-2024.pdf`` on a research paper whose ``field_rpaper_year`` says 2016 is
    not a 2024 paper — a four-digit number that happened to sit between two
    delimiters is not a claim.

    Agreement with a bare **creation stamp** is *not* a reason to stand down, and
    the reason is not that the stamp is worthless — it is that standing down
    there does not preserve it. On a shelf the page's date is not the file's to
    borrow, so declining means the file ends up *undated*, not dated 2018-04-04.
    ``April18-Jun18_new.pdf`` on a page typed on 2018-04-04 would lose the one
    thing it actually states, because one of the 69 files sharing that stamp was
    always going to land in its month.
    """
    from app.ingestion.source_dates import as_stored_date, is_plausible
    from app.ingestion.title_dates import title_date

    page = evidence.page
    if not page.is_multi_pdf:
        return TitleFinding()
    found = title_date(link_text=evidence.anchor, filename=evidence.filename,
                       pdf_title=evidence.pdf_title)
    if found is None or not is_plausible(found.normalized_value):
        return TitleFinding()
    if page.date_from_bundle_field and _same_period(
        found.precision, found.normalized_value, parse_dt(page.effective_date)
    ):
        return TitleFinding(found=found, disposition="corroborating")
    if found.title_kind == "bare_year" and page.date_from_bundle_field:
        return TitleFinding(found=found, disposition="rejected")

    return TitleFinding(
        found=found,
        disposition="replaced",
        decision=DateDecision(
            document_id=evidence.document_id,
            action="propose_override",
            candidate_start_date=as_stored_date(found.normalized_value),
            candidate_precision=found.precision,
            date_type="publication",
            edition_label=evidence.edition,
            source="document_title",
            confidence=0.95,
            evidence=(
                f"The file's {found.title_source.replace('_', ' ')} states "
                f"{found.raw_statement!r}, read as "
                f"{found.normalized_value.isoformat()} at {found.precision} "
                f"precision{_reading(found)}. One of {page.pdf_count} PDFs on "
                f"this page, so the page's {str(page.effective_date)[:10]} is a "
                f"date about the page and not about this file."
            ),
            rule="title_states_date",
            # Which of the file's strings answered, and what shape of statement
            # it made. Both are carried so `_reconcile_with_title` can tell a
            # deliberate edition or month from a four-digit number that merely
            # sat in the filename, and so the audit row can name the exact
            # metadata that produced the date.
            title_source=found.title_source,
            title_kind=found.title_kind,
            title_disposition="replaced",
            decided_by="deterministic",
            supporting_evidence=(
                "Read from the file's own naming only — no file timestamp, "
                "upload month or PDF metadata date was consulted."
            ),
            used=["drupal"],
        ),
    )


def title_override(evidence: PdfEvidence) -> DateDecision | None:
    """The override the file's own naming proposes, or None.

    Thin wrapper over :func:`read_title_evidence` for callers that only want the
    verdict. The resolver uses the fuller form, because a name that did *not*
    win is still something the audit row has to account for.
    """
    return read_title_evidence(evidence).decision


def copyright_override(evidence: PdfEvidence) -> DateDecision | None:
    """A year-precision override from a corroborated copyright statement, or None.

    Fires only when every one of these holds:

    * the front matter names a copyright year (``© … 2020``, ``Ⓒ … 2020``,
      ``(c) 2020``, ``Copyright 2020``);
    * the PDF's DocInfo creation date names the **same** year — a second,
      independent statement by the document about itself. A statement alone
      is the LLM path's business (and a bare year is refused there as
      day-precision); a DocInfo date alone never moves anything
      (``test_a_pdf_creation_date_alone_never_moves_the_page_date``);
    * the year is plausible for this corpus;
    * the year differs from the page's own — agreeing with the page changes
      nothing, and the page's day is the finer value.

    The result is a *year*: 1 January as a marker, ``candidate_precision="year"``,
    exactly how ``research_papers`` store ``field_rpaper_year``. Nothing here
    invents a day, and nothing here reads a Drupal timestamp.

    Applies to any file whose bytes were read — see
    :func:`_wants_document_evidence` — which is every PDF sharing its page and
    every routed one. It is one deterministic rule inside the resolution model,
    not the model itself: a file it cannot settle falls back to the page's date
    or goes on to the interpreter, exactly as before.
    """
    from datetime import date


    from app.ingestion.source_dates import as_stored_date, is_plausible

    found = copyright_statement(evidence.front_text)
    if found is None:
        return None
    statement, year = found
    created = parse_dt(evidence.pdf_created)
    if created is None or created.year != year:
        return None
    if not is_plausible(date(year, 1, 1)):
        return None
    page_date = parse_dt(evidence.page.effective_date)
    if page_date is not None and page_date.year == year:
        return None
    return DateDecision(
        document_id=evidence.document_id,
        action="propose_override",
        candidate_start_date=as_stored_date(date(year, 1, 1)),
        candidate_precision="year",
        date_type="publication",
        edition_label=evidence.edition,
        source="document_copyright",
        confidence=0.9,
        evidence=(
            f"The document's front matter states {statement!r} and its DocInfo "
            f"creation date is {evidence.pdf_created}; both name {year}, which "
            f"differs from the page's {str(evidence.page.effective_date)[:10]}. "
            f"Year precision: 1 January is a marker, not a day."
        ),
        rule="copyright_statement_corroborated",
        decided_by="deterministic",
        supporting_evidence=(
            "In-body file with no Drupal upload record on a page dated by its "
            "creation stamp; the file itself was the only evidence available."
        ),
        used=["drupal", "pdf_meta", "pdf_text"],
    )


def _document_was_read(decision: DateDecision, evidence: PdfEvidence) -> DateDecision:
    """Mark a decision as one taken with the file's own bytes in hand.

    Two things, and an audit needs both.

    The evidence tiers actually used are extended, so the read is not lost when a
    routed decision is re-taken after reading — ``decide`` builds a fresh
    decision each time and would otherwise report ``["drupal"]`` for a file whose
    first page the interpreter has just been shown.

    And where the outcome is still the page's date, the record says the document
    was consulted and stated nothing verifiable. ``evidence`` is the column that
    is persisted, so "why does this file carry its page's date?" has to be
    answerable from it rather than from silence. Only for a file that shares its
    page: a single-PDF branch already gives its own reason, and "one of 1 PDFs"
    would be nonsense.
    """
    used = [*decision.used,
            *(tier for tier in ("pdf_meta", "pdf_text") if tier not in decision.used)]
    already = "read for a date of its own" in (decision.evidence or "")
    if (
        decision.action not in ("keep_page_date", "drop_page_date")
        or already
        or not evidence.page.is_multi_pdf
    ):
        return replace(decision, used=used)
    if decision.action == "drop_page_date":
        return replace(
            decision,
            evidence=(
                f"{decision.evidence} The file itself was read for a date and "
                f"states none that could be verified."
            ).strip(),
            used=used,
        )
    return replace(
        decision,
        evidence=(
            f"{decision.evidence} One of {evidence.page.pdf_count} PDFs on this "
            f"page, so it was read for a date of its own; it states none that "
            f"could be verified, and the page's date stands as this file's "
            f"fallback."
        ).strip(),
        used=used,
    )


def _wants_document_evidence(decision: DateDecision, evidence: PdfEvidence) -> bool:
    """Whether this file's own bytes should be read before its date is settled.

    Two independent reasons, and they are different questions.

    ``needs_llm`` means the deterministic pass has already concluded the document
    is worth reading closely — a late upload, a migration import, a year in the
    link text.

    A **multi-PDF page** is the other, and it is a property of the file's
    situation rather than of any signal about it. Several PDFs on one page are
    several documents, so the page's date is this file's fallback and not its
    answer, and the deterministic rules are entitled to look first.

    A single-PDF page is deliberately absent from both. Its file is part of the
    page's own publication, inherits the page's date without being opened, and
    every single-PDF branch already says so.
    """
    return decision.action == "needs_llm" or evidence.page.is_multi_pdf


def resolve(evidence: PdfEvidence, content: bytes | None = None) -> ResolvedDate:
    """Decide this PDF's ``effective_start_date``, implementing this table:

    ===================================================  =========================
    Situation                                            Result
    ===================================================  =========================
    Single PDF                                           parent effective date
    Multi PDF + full file date                           file date
    Multi PDF + edition/period                           file period
    Multi PDF + month/year                               file month; text may
                                                         sharpen it
    Multi PDF + bare year + strong CMS parent            keep the CMS date
    Multi PDF + bare year + weak creation parent         file year may win
    Multi PDF + verified text date                       file text date
    Multi PDF + no file evidence + strong CMS parent     parent date
    Multi PDF + no file evidence + weak creation parent  undated
    Resolver failure + strong CMS parent                 parent date
    Resolver failure + weak multi-PDF parent             undated
    ===================================================  =========================

    "Strong" means the page's bundle states its date in a configured CMS field;
    "weak" means the page has only its Drupal creation stamp. The single
    predicate behind the last four rows is
    :func:`app.ingestion.date_rules.page_date_is_usable`, asked on the success
    path and on the failure path alike so a crash cannot reintroduce a date the
    rules refused.
    """
    try:
        # Free, and the first thing asked: a file that names its own date.
        #
        # One kind of name settles it outright: a **full date** leaves nothing
        # for anything else to establish, so nothing is read, asked or paid for.
        #
        # Everything else fixes a *period* the document's own text may sharpen
        # within — "2024_December" in the name and "DATED 11-12-2024" in the
        # first line are the same claim, stated twice, and the finer one wins.
        # An edition is included in that: it names the year the period ends in,
        # and a date inside that year is a sharpening rather than a contradiction.
        # What no text may do is move the document *outside* the named period —
        # see `_reconcile_with_title`.
        finding = read_title_evidence(evidence)
        titled = finding.decision
        if titled is not None and titled.title_kind == "full_date":
            return _outcome(titled, evidence, used=list(titled.used),
                            finding=finding)

        decision = decide(evidence)
        used = list(decision.used)

        # Read the bytes at most once, and only where they are owed: a routed
        # decision, or a file that shares its page. PyMuPDF only — no OCR, no
        # Document Intelligence, no model.
        read = False
        if content and _wants_document_evidence(decision, evidence):
            _read_pdf_signals(evidence, content)
            read = True
            # The PDF's internal title only exists now, so the free rule gets a
            # second look with the one source it could not see before.
            reread = read_title_evidence(evidence)
            if reread.decision is not None or finding.found is None:
                finding = reread
                titled = reread.decision or titled
            override = copyright_override(evidence)
            decision = override if override is not None else _document_was_read(
                decision, evidence
            )
            used = list(decision.used)

        if decision.action == "needs_llm":
            if content and not read:
                _read_pdf_signals(evidence, content)
                read = True
            # Reading the document may itself settle the case — an unreadable
            # PDF has nothing to say — so re-run the deterministic pass before
            # paying for a model call.
            decision = decide(evidence)
            if read:
                decision = _document_was_read(decision, evidence)
            used = list(decision.used)

        llm_raw: dict[str, Any] | None = None
        if decision.action == "needs_llm":
            decision, llm_raw = _interpret(evidence, decision)
            used.append("llm")

        return _outcome(decision, evidence, used=used, llm_raw=llm_raw,
                        titled=titled, finding=finding)
    except Exception:
        return _failed(evidence)


def _failed(evidence: PdfEvidence) -> ResolvedDate:
    """What a file gets when resolution raised. Fails **closed**, both ways.

    "Fail closed" used to mean one thing here — keep the page's date, because a
    stale date is recoverable and a wrong one is not. That is still right where
    the page's date is a claim about the content. It is exactly backwards on a
    shelf: a page holding twelve PDFs and dated only by its Drupal creation stamp
    would hand every one of them the day somebody typed the node, which is the
    original bug, silently reintroduced by an unrelated crash in a rule that
    never ran.

    So the same predicate the successful path uses decides this one too
    (:func:`app.ingestion.date_rules.page_date_is_usable`), and a failure on a
    weak multi-PDF page leaves the file undated. A missing date is preferable to
    a confidently wrong one, and the decision row says which happened rather than
    leaving a mystery value.
    """
    page = evidence.page
    usable = page_date_is_usable(page)
    logger.warning(
        "Date resolution failed for %s; %s.", evidence.document_id,
        "keeping the page date" if usable else
        "the page's date is not this file's to borrow, so it is left undated",
        exc_info=True,
    )
    decision = DateDecision(
        document_id=evidence.document_id,
        action="keep_page_date" if usable else "drop_page_date",
        candidate_start_date=page.effective_date if usable else None,
        candidate_precision=page.node_start_precision if usable else "day",
        date_type="unknown",
        edition_label=evidence.edition,
        source="node_effective_date",
        confidence=0.0,
        rule="resolver_failed",
        decided_by="deterministic",
        evidence=(
            "Date resolution raised an unexpected error. "
            + ("The page states its own date, so this file keeps it."
               if usable else
               f"This is one of {page.pdf_count} PDFs on a {page.bundle} page "
               f"dated only by its Drupal creation stamp, so that date is not "
               f"this file's to borrow and it is left undated rather than given "
               f"a date nothing established.")
        ),
        used=["drupal"],
    )
    if not usable:
        return ResolvedDate(start_value=None, start_precision=None,
                            end_value=None, end_precision=None,
                            edition_label=evidence.edition, decision=decision)
    return ResolvedDate(start_value=page.effective_date,
                        start_precision=page.node_start_precision,
                        end_value=page.effective_end,
                        end_precision=page.node_end_precision,
                        edition_label=evidence.edition, decision=decision)


def _reconcile_with_title(
    decision: DateDecision, titled: DateDecision
) -> DateDecision:
    """Settle a date the file's *name* states against one its *text* states.

    Only three things can have happened by the time this runs.

    A text-based override that falls **inside** the named period is a sharpening
    and is kept — that is the tender bulletin whose filename says December 2024
    and whose first line says the 11th. The same holds for an edition: a file
    named ``…2024-25`` is dated to 2025, so a body date in March 2025 sharpens
    it and a body date in March *2024* does not, because 2024 is the year the
    period opened in and not the year the name resolved to.

    A text-based override that falls **outside** it is a disagreement, and the
    name wins: the name is what a person wrote about this file, the text is what
    a model read out of it, and the corpus has the failure mode in both
    directions (a masthead the model reconstructed, a citation year it mistook
    for a publication).

    **Unless the name only offered a bare year**, in which case the text wins.
    ``Post_2015_bulletin_and_TEDDY_launch.pdf`` is about the post-2015
    Development Agenda and was released on 9 July 2014, which its first line
    says in full. A four-digit number that merely sat between two delimiters is
    not a claim about a date, and it must not displace one that is.

    Anything else — a kept page date, a review, a drop — means nothing in the
    text established a date at all, so the named one stands.
    """
    if decision.action != "propose_override":
        return titled
    if titled.title_kind == "bare_year":
        return replace(decision, title_source=titled.title_source,
                       title_kind=titled.title_kind, title_disposition="rejected")
    inside = _same_period(
        titled.candidate_precision,
        date.fromisoformat(str(titled.candidate_start_date)[:10]),
        parse_dt(decision.candidate_start_date),
    )
    if not inside:
        return replace(
            titled,
            evidence=(
                f"{titled.evidence} The document's text proposed "
                f"{str(decision.candidate_start_date)[:10]}, which falls outside "
                f"that period; a disagreement is not a sharpening, so the name "
                f"stands."
            ).strip(),
        )
    return replace(
        decision,
        title_source=titled.title_source,
        title_kind=titled.title_kind,
        title_disposition="refined",
        evidence=(
            f"{decision.evidence} This agrees with the period the file's own name "
            f"states ({titled.candidate_start_date[:10]}, "
            f"{titled.candidate_precision} precision) and is more precise, so it "
            f"stands."
        ).strip(),
    )


def _stamp_title(decision: DateDecision, finding: "TitleFinding | None"):
    """Record what the file's naming said on whatever decision was reached.

    ``_reconcile_with_title`` already stamps the cases where the name competed
    with the document's text. This covers the rest, which are the ones an
    auditor actually asks about: the name was read, it stated something, and the
    document is dated otherwise. Without this the row is silent about the very
    string the question is about — "the filename clearly says 2024, so why is
    this 2018?".
    """
    if finding is None or finding.found is None:
        return decision
    if decision.title_disposition is not None:
        return decision           # already settled by the reconciler
    found = finding.found
    note = {
        "corroborating": (
            f"The file's {found.title_source.replace('_', ' ')} states "
            f"{found.raw_statement!r}, which names the same period the page's "
            f"own field does; it changes nothing and the page's finer date "
            f"stands."
        ),
        "rejected": (
            f"The file's {found.title_source.replace('_', ' ')} contains "
            f"{found.raw_statement!r}, but a bare year is not a claim about a "
            f"date and does not displace the one the page's field states."
        ),
    }.get(finding.disposition)
    return replace(
        decision,
        title_source=found.title_source,
        title_kind=found.title_kind,
        title_disposition=finding.disposition,
        evidence=" ".join(filter(None, (decision.evidence, note))),
    )


def _end_after_override(decision: DateDecision, page: PageContext):
    """The end date a file keeps once its own evidence has set the start.

    **Evidence replaces only the temporal information it actually establishes.**
    Clearing the parent's end used to be unconditional, which destroyed real
    information: a file on a 2020-2025 project named "Annual Report 2024" states
    something about 2024 and nothing whatever about when the project ends.

    So the question is whether the evidence is a *point* or a *period*:

    * ``day`` or ``month`` precision is a point — a date the document was issued
      on. A point-dated document does not cover its parent's five-year range, so
      the inherited end goes with the date it belonged to.
    * ``year`` precision — an edition, a bare year, a copyright line — establishes
      a period and says nothing about an end. The parent's end is left alone.

    With one bound: the result has to read forwards. An override landing after
    the inherited end would store a backwards range, which
    ``reconcile.date_checks.inverted_date_range`` would correctly report as
    something else having written the column. There the end is dropped, because
    the start is the better-evidenced of the two.
    """
    if page.effective_end is None:
        return None, None
    if decision.candidate_precision in ("day", "month"):
        return None, None
    start = str(decision.candidate_start_date or "")[:10]
    if start and start > str(page.effective_end)[:10]:
        logger.info(
            "Document-stated date %s falls after the inherited end %s; the end "
            "is dropped rather than stored backwards.",
            start, str(page.effective_end)[:10],
        )
        return None, None
    return page.effective_end, page.node_end_precision


def _outcome(
    decision: DateDecision,
    evidence: PdfEvidence,
    *,
    used: list[str],
    llm_raw: dict[str, Any] | None = None,
    titled: DateDecision | None = None,
    finding: "TitleFinding | None" = None,
) -> ResolvedDate:
    """Turn a settled decision into the dates the document will carry.

    Three outcomes, and one of them is new. An **override** takes the date the
    document stated about itself. A **drop** takes no date: several PDFs on a
    page whose own date is only a creation stamp, and nothing said otherwise —
    see :func:`app.ingestion.date_rules.page_date_is_usable`. Everything else,
    including a review, inherits the page's date as before.

    The drop is applied here as well as in :func:`app.ingestion.date_rules.decide`
    because the interpreter can hand back ``keep_page_date`` or
    ``needs_manual_review`` for a file on exactly such a page, and a date the
    rules refused must not come back through the model's door. Both paths ask the
    same predicate, so the decision row and the stored value cannot disagree.

    ``titled`` is where the two kinds of self-statement are reconciled, and the
    order between them is: **the file's name fixes the period, its text may
    sharpen that period, and nothing may contradict it.** So a tender bulletin
    named ``…_2024_December.pdf`` whose body reads "ISSUE NO. 22 DATED
    11-12-2024" is dated to the 11th — the text is the same claim, stated more
    precisely. A verdict landing outside the stated period is not a sharpening
    but a disagreement, and there the name wins.

    What an override does **not** do is discard the parent's end date wholesale —
    see :func:`_end_after_override`.
    """
    page = evidence.page
    if titled is not None and titled is not decision:
        decision = _reconcile_with_title(decision, titled)
    decision = _stamp_title(decision, finding)
    if decision.action == "propose_override":
        end_value, end_precision = _end_after_override(decision, page)
        return ResolvedDate(
            start_value=decision.candidate_start_date,
            start_precision=decision.candidate_precision,
            end_value=end_value,
            end_precision=end_precision,
            edition_label=decision.edition_label,
            decision=decision, llm_raw=llm_raw, used=used,
        )

    if not page_date_is_usable(page):
        if decision.action != "drop_page_date":
            decision = replace(
                decision, action="drop_page_date", candidate_start_date=None,
                confidence=0.0,
                evidence=(
                    f"{decision.evidence} The page's date is not this file's to "
                    f"borrow: it is one of {page.pdf_count} PDFs on a "
                    f"{page.bundle} page dated only by its Drupal creation "
                    f"stamp. Left undated."
                ).strip(),
            )
        return ResolvedDate(
            start_value=None, start_precision=None,
            end_value=None, end_precision=None,
            edition_label=decision.edition_label,
            decision=decision, llm_raw=llm_raw, used=used,
        )

    return ResolvedDate(
        start_value=page.effective_date,
        start_precision=page.node_start_precision,
        end_value=page.effective_end,
        end_precision=page.node_end_precision,
        edition_label=decision.edition_label,
        decision=decision, llm_raw=llm_raw, used=used,
    )


def _interpret(
    evidence: PdfEvidence, deferred: DateDecision
) -> tuple[DateDecision, dict[str, Any] | None]:
    """Ask the model, then apply the validated gates to its verdict."""
    from app.ingestion.date_llm import interpret

    page_date = evidence.page.effective_date
    verdict = interpret(evidence)
    if verdict is None:
        # A model outage must never change a date.
        return (
            DateDecision(
                document_id=evidence.document_id, action="keep_page_date",
                candidate_start_date=page_date, date_type="unknown",
                edition_label=evidence.edition, source="node_effective_date",
                confidence=0.0, rule="llm_unavailable", decided_by="llm",
                evidence="Interpretation call failed; the page date was kept.",
                supporting_evidence=deferred.supporting_evidence,
                used=[*deferred.used, "llm"],
            ),
            None,
        )

    action = verdict.safe_action()
    precision = verdict.supported_precision() or "day"
    candidate = verdict.normalized_start_date()

    # A year-only verdict that agrees with the page's own year buys nothing and
    # costs the day the page states. The same guard `copyright_override` applies,
    # for the same reason: replacing 2019-01-11 with "2019, year precision" makes
    # the record vaguer without making it truer. A verdict naming a *different*
    # year is exactly the case worth acting on.
    if action == "override" and precision == "year":
        page_year = str(page_date or "")[:4]
        if candidate and page_year and candidate[:4] == page_year:
            logger.info(
                "Year-only verdict %s matches the page's own year; keeping the "
                "page date, which is more precise.", candidate,
            )
            action = "keep_page_date"

    mapped = {
        "override": "propose_override",
        "review": "needs_manual_review",
        "keep_page_date": "keep_page_date",
    }[action]
    return (
        DateDecision(
            document_id=evidence.document_id,
            action=mapped,
            candidate_start_date=(candidate if action == "override" else page_date),
            candidate_precision=(precision if action == "override" else "day"),
            date_type=verdict.date_type,
            edition_label=verdict.edition_label or evidence.edition,
            source=("llm_publication" if action == "override"
                    else "node_effective_date"),
            confidence=verdict.confidence,
            evidence=verdict.evidence,
            rule="llm_interpreted",
            decided_by="llm",
            supporting_evidence=(verdict.publication_statement or ""),
            used=[*deferred.used, "llm"],
        ),
        verdict.model_dump(),
    )
