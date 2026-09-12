"""Attached-PDF download + extraction for Drupal nodes.

Network I/O for a node's attached PDF: download (with an http->https upgrade
for hosts that dropped plain-http), extract, and build the canonical PDF
document linked back to its node. Pulled out of the ingestion run coordinator
so that module stays pure orchestration.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.models import CanonicalDocument

if TYPE_CHECKING:
    import requests

    from app.ingestion.change_detection import ChangeRecord

logger = logging.getLogger(__name__)


def fetch_attachment(
    session: "requests.Session", url: str, timeout: float
) -> tuple[bytes, str]:
    """GET an attachment, trying the https:// variant first for http:// URLs.
    Old body HTML still links plain-http PDFs, but teriin.org no longer answers
    on port 80 (the connect attempt hangs until timeout), while the same files
    are served fine over TLS. Falls back to the original URL so hosts that are
    still http-only keep working. Returns (content, url that succeeded)."""
    import requests

    if url.lower().startswith("http://"):
        upgraded = "https://" + url[len("http://"):]
        try:
            response = session.get(upgraded, timeout=timeout)
            response.raise_for_status()
            return response.content, upgraded
        except requests.RequestException:
            logger.info("HTTPS variant failed for %s; retrying original URL.", url)
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content, url


def dead_link_status(exc: "requests.RequestException") -> int | None:
    """The HTTP status if this failure was a client error, else None.

    A 4xx means the server answered and the file is not there: old body HTML
    links tender notices and RFQs that were taken down once they closed, and no
    amount of retrying brings them back. Those are worth one quiet line.
    Timeouts, DNS failures and 5xx can clear on their own, so they keep the
    full traceback that tells you which one it was.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int) and 400 <= status < 500:
        return status
    return None


def _mark_dead(record: "ChangeRecord", url: str, status: int) -> None:
    """Remember a client error so the crawl stops re-fetching this attachment.

    Fails open, like the rest of the pipeline's catalog writes: an unreachable
    database costs one warning and a download retried next sweep, which is what
    happened before the markers existed — never a failed sweep.
    """
    from app.catalog import dead_links

    try:
        dead_links.record(
            record.document_id,
            fingerprint=record.fingerprint,
            url=url,
            status=status,
        )
    except Exception:
        logger.warning(
            "Could not record %s as a dead link; it will be retried.",
            url, exc_info=True,
        )


def _record_date_decision(
    record: "ChangeRecord", node, file, resolved, parent_date
) -> None:
    """Store how this PDF's date was decided, and the evidence behind it.

    Kept out of the document itself: the payload gets the date and the edition
    label, while the confidence, the quoted statement and the rule live in
    ``{state}_date_decision``. That table is also the review queue — a case the
    resolver could not settle safely lands there rather than moving a date.

    ``current_start_date`` records the page's own resolved date, so a row reads
    as "would have been X, assigned Y". The evidence sentence names the whole
    chain — this file, its page, that page's bundle, the configured field and its
    value — because "why does this PDF have the date 2022?" has to be answerable
    from the stored row alone. Fails open like the dead-link markers: an
    unreachable database costs one warning, never an ingestion.
    """
    if resolved.decision is None:
        return
    from app.catalog import date_decisions
    from app.ingestion.bundle_dates import describe
    from app.ingestion.date_llm import prompt_version

    try:
        date_decisions.ensure_table()
        row = date_decisions.from_decision(
            resolved.decision,
            origin=getattr(file, "origin", "attachment"),
            bundle=node.bundle,
            node_uuid=(node.uuid or None),
            page_pdf_count=len(getattr(node, "files", None) or []) or 1,
            current_start_date=parent_date.start_value,
            url=file.url,
            filename=file.filename,
            llm_raw=resolved.llm_raw,
            prompt_version=prompt_version() if resolved.llm_raw else None,
            # The period the file inherited, and any defect in it. Carried
            # from the page rather than re-derived: the file has no dates
            # of its own and a second reading could disagree.
            candidate_end_date=resolved.end_value,
            range_issue=parent_date.range_issue,
        )
        # The inheritance chain, appended rather than replacing the rule's own
        # sentence: a reviewer needs both what the resolver concluded and where
        # the date it kept actually came from.
        row.evidence = " ".join(filter(None, (
            row.evidence,
            describe(parent_date, title=node.title, url=node.url,
                     for_attachment=True),
        )))
        date_decisions.record(row)
    except Exception:
        logger.warning(
            "Could not record the date decision for %s; ingestion continues.",
            record.document_id, exc_info=True,
        )


def build_attachment_doc(
    record: "ChangeRecord", session: "requests.Session"
) -> CanonicalDocument | None:
    """Download a node's attached PDF, extract it, and build a canonical PDF
    document linked back to the node. ``record.payload`` is a (DrupalRecord,
    DrupalFile) pair; ``source_type`` is 'pdf_attachment' so the local on-disk
    PDF pipeline's delete-reconcile never touches these web-sourced docs. The
    ``session`` is shared across the run so downloads reuse the connection pool
    instead of re-handshaking per attachment."""
    import requests

    from app.config import get_settings
    from app.ingestion.canonical import drupal_facets, from_pdf
    from app.ingestion.extractors.pdf_extractor import extract_pdf

    node, file = record.payload
    settings = get_settings()
    try:
        content, fetched_url = fetch_attachment(
            session, file.url, settings.drupal_request_timeout
        )
    except requests.RequestException as exc:
        status = dead_link_status(exc)
        if status is not None:
            logger.warning(
                "Attachment %s is unavailable (HTTP %d); skipping.", file.url, status
            )
            _mark_dead(record, file.url, status)
        else:
            logger.exception("Could not download attachment %s; skipping.", file.url)
        return None
    if not content:
        logger.warning("Empty attachment body for %s; skipping.", file.url)
        return None

    result = extract_pdf(content, file.filename or record.document_id)

    # The parent page's effective date, resolved once from its bundle's
    # configured field. Every PDF on the page inherits *this* — one page holding
    # twelve files produces twelve documents carrying one date, because there is
    # one resolution and it is propagated, not recomputed per file.
    parent_date = resolve_parent_date(node)
    # Then the file-level pass. Its contract is narrow: it may never read a file
    # timestamp, an upload month or PDF metadata *as* a date, and where the page
    # states its own date it does not read the file at all.
    resolved = _resolve_date(record, node, file, content, parent_date)

    # The PDF inherits its node's entity refs and facets so theme-scoped
    # retrieval and per-theme counts reach the attached content too. In-body
    # PDFs linked from several nodes inherit from the first-seen node.
    refs = list(getattr(node, "refs", None) or [])
    extra: dict[str, object] = {"bundle": node.bundle}
    # A reporting period is a label, never a date: "Annual Report 2024-2025"
    # sets this and leaves effective_start_date alone.
    if resolved.edition_label:
        extra["edition_label"] = resolved.edition_label
    doc = from_pdf(
        result,
        document_id=record.document_id,
        source_type="pdf_attachment",
        title=(file.description or node.title or file.filename or None),
        source_url=node.url,
        file_url=fetched_url,
        linked_article_uuid=(node.uuid or None),
        effective_start_date=resolved.start_value,
        # `parent_page` is the ordinary case and says exactly what happened: this
        # file carries the date its Drupal page resolved to. The two override
        # values name which kind of statement the document made about itself —
        # a quoted publication statement, or a corroborated copyright year. The
        # mapping lives on `ResolvedDate.canonical_source` so the document and
        # the decision row cannot disagree about it. The fuller reasoning — which
        # rule fired, the confidence, the quote, the parent's field and value —
        # stays in `{state}_date_decision`; this is the bit that belongs beside
        # the value.
        date_source=resolved.canonical_source,
        # Inherited, not assumed: a file on a research paper is year-precision
        # too, and a reader that renders its 1 January as a day would invent a
        # January publication for the file exactly as it would for the page.
        start_precision=resolved.start_precision,
        # A file on a completed project covers the same period the project did.
        # Inherited whole from the page, never derived from the file.
        effective_end_date=resolved.end_value,
        end_precision=resolved.end_precision,
        date_evidence=_evidence_for(parent_date, resolved),
        extra=extra,
        entity_refs=refs,
        **drupal_facets(node.metadata or {}, refs),
    )
    _record_date_decision(record, node, file, resolved, parent_date)
    return doc


def resolve_parent_date(node):
    """The Drupal page's effective date, from its bundle's configured field.

    The same call `canonical._drupal_document` makes for the page itself, so the
    page and everything attached to it cannot disagree.
    """
    from app.ingestion.bundle_dates import resolve_effective_dates

    return resolve_effective_dates(
        getattr(node, "bundle", None),
        getattr(node, "created", None),
        getattr(node, "metadata", None),
    )


def inherit_date(parent_date):
    """The parent's date as this file's own. See ``bundle_dates.inherited``."""
    from app.ingestion.bundle_dates import inherited

    return inherited(parent_date)


def _evidence_for(parent_date, resolved):
    """Provenance for whichever of the three outcomes happened.

    Inheritance is the ordinary one and is the parent's own resolution, unchanged.
    The other two both displace it, and in both the parent's resolution is still
    recorded — "would have been X" — because the interesting fact about not
    inheriting is what was not inherited.
    """
    if resolved.dropped:
        return _dropped_evidence(parent_date, resolved)
    if resolved.overridden:
        return _overridden_evidence(parent_date, resolved)
    return inherit_date(parent_date)


def _overridden_evidence(parent_date, resolved):
    """Provenance for a file that stated its own date."""
    from dataclasses import replace

    return replace(
        parent_date,
        start_value=resolved.start_value,
        source=resolved.canonical_source,
        # The decision's own precision: a quoted publication statement gives a
        # day, a corroborated copyright statement gives a year, and a title gives
        # whatever it actually named.
        start_precision=resolved.start_precision,
        # A statement about a publication gives a day or a year, not a period;
        # the page's end date belonged to the date it displaced.
        end_value=None,
        end_precision=None,
        rule=(resolved.decision.rule if resolved.decision
              else "document_statement_override"),
    )


def _dropped_evidence(parent_date, resolved):
    """Provenance for a file the system declined to date.

    The whole value of this row is that it is not silence: it records that the
    page *had* a date, that the date was its creation stamp, and that the file
    was one of several sharing it — which is the answer to "why does this PDF
    have no date?" and equally to "why did you not just use 2018-04-04?".
    """
    from dataclasses import replace

    return replace(
        parent_date,
        start_value=None,
        start_precision=None,
        end_value=None,
        end_precision=None,
        source=resolved.canonical_source,
        rule=(resolved.decision.rule if resolved.decision else "drop_page_date"),
    )


def _resolve_date(record: "ChangeRecord", node, file, content: bytes, parent_date):
    """This PDF's date, and the reasoning behind it.

    Delegates to the one canonical resolver. With the feature off, or if
    anything goes wrong, the parent page's date stands — which is now the page's
    *resolved* date rather than its raw creation stamp, so turning the resolver
    off degrades to plain inheritance rather than to a different date.
    """
    from app.config import get_settings
    from app.ingestion.bundle_dates import inherited
    from app.ingestion.date_resolution import (
        ResolvedDate,
        build_evidence,
    )
    from app.ingestion.date_resolution import resolve as resolve_pdf_date

    if not get_settings().date_resolution_enabled:
        carried = inherited(parent_date)
        return ResolvedDate(
            start_value=carried.start_value,
            start_precision=carried.start_precision,
            end_value=carried.end_value,
            end_precision=carried.end_precision,
        )
    evidence = build_evidence(
        document_id=record.document_id, node=node, file=file,
        parent_date=parent_date,
    )
    return resolve_pdf_date(evidence, content)
