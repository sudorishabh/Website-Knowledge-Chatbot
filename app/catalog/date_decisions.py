"""Shadow storage for evidence-based PDF date decisions.

One row per PDF, holding what the resolver *would* propose and why: the
candidate, what kind of date it is, which rule or model produced it, the
confidence and the evidence sentence. Deliberately its own table, and
deliberately not ``CanonicalDocument.extra`` — ``build_payload`` does
``payload.update(m.extra)``, so anything parked there would flow straight into
Qdrant chunk payloads. Nothing reads this back into ingestion or retrieval.

Writes fail open at the call site, matching the rest of the ingestion path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.catalog import schema
from app.catalog.db import now as _now
from app.catalog.db import state_table as _table
from app.core.clients import mysql_connection
from app.core.dates import parse_iso_date

if TYPE_CHECKING:
    from app.ingestion.date_rules import DateDecision

__all__ = ["DecisionRow", "ensure_table", "record", "load"]

_ensured = False


def ensure_table() -> None:
    """Create the table if missing, at most once per process."""
    global _ensured
    if _ensured:
        return
    schema.ensure_date_decision_table()
    _ensured = True


@dataclass
class DecisionRow:
    document_id: str
    origin: str
    bundle: str | None = None
    node_uuid: str | None = None
    page_pdf_count: int = 1
    current_start_date: str | None = None
    candidate_start_date: str | None = None
    #: The end of the period, for a bundle whose mapping declares an end field
    #: and whose record stated a usable one. None otherwise, and never derived
    #: from :attr:`candidate_start_date`.
    candidate_end_date: str | None = None
    #: What is wrong with the range, when something is: ``inverted`` |
    #: ``end_invalid`` | ``end_without_start``. None for a well-formed range and
    #: for a document that has no range at all.
    range_issue: str | None = None
    date_type: str = "unknown"
    edition_label: str | None = None
    date_source: str = "node_created"
    confidence: float = 0.0
    action: str = "keep_page_date"
    rule: str = ""
    decided_by: str = "deterministic"
    evidence: str | None = None
    llm_raw: dict[str, Any] | None = None
    prompt_version: str | None = None
    url: str | None = None
    filename: str | None = None
    # ------------------------------------------------------------------ #
    # Where a date read from the file's own naming came from. All three are
    # NULL for every other path. `evidence` says the same thing in prose;
    # these say it in a form a query can group by, which is what turns "why
    # is this document dated 2018 when the filename says 2024?" from a
    # one-row read into a corpus-wide answer.
    # ------------------------------------------------------------------ #
    #: ``filename`` | ``pdf_internal_title`` | ``link_text``.
    title_source: str | None = None
    #: ``full_date`` | ``month_year`` | ``edition`` | ``bare_year``.
    title_kind: str | None = None
    #: ``replaced`` | ``refined`` | ``corroborating`` | ``rejected``.
    title_disposition: str | None = None


def from_decision(
    decision: "DateDecision",
    *,
    origin: str,
    bundle: str | None,
    node_uuid: str | None,
    page_pdf_count: int,
    current_start_date: str | None,
    url: str | None,
    filename: str | None,
    llm_raw: dict[str, Any] | None = None,
    prompt_version: str | None = None,
    candidate_end_date: str | None = None,
    range_issue: str | None = None,
) -> DecisionRow:
    return DecisionRow(
        document_id=decision.document_id,
        origin=origin,
        bundle=bundle,
        node_uuid=node_uuid,
        page_pdf_count=page_pdf_count,
        current_start_date=current_start_date,
        candidate_start_date=decision.candidate_start_date,
        candidate_end_date=candidate_end_date,
        range_issue=range_issue,
        date_type=decision.date_type,
        edition_label=decision.edition_label,
        date_source=decision.source,
        confidence=decision.confidence,
        action=decision.action,
        rule=decision.rule,
        decided_by=decision.decided_by,
        evidence=decision.evidence,
        llm_raw=llm_raw,
        prompt_version=prompt_version,
        url=url,
        filename=filename,
        title_source=decision.title_source,
        title_kind=decision.title_kind,
        title_disposition=decision.title_disposition,
    )


def from_effective_date(
    *,
    document_id: str,
    url: str | None,
    created: str | None,
    resolved: Any,
    title: str | None = None,
) -> DecisionRow | None:
    """A decision row for a *website* document, or None when there is nothing to say.

    ``resolved`` is the :class:`app.ingestion.bundle_dates.EffectiveDate` the
    write path actually applied. It is passed in rather than re-derived from the
    metadata, so the row cannot disagree with the value on the document.

    **A row is written when the document's bundle maps to a real CMS date
    field** — whether the field supplied the date, was empty, or held something
    unusable. That is the population an auditor asks about, and the last two
    cases are exactly the ones worth reviewing.

    No row for a bundle that maps to ``created`` or has no mapping at all. Their
    provenance is already complete without one: ``documents.bundle`` plus
    ``date_source='created'`` says "this content type takes its creation
    stamp", which is the whole answer. A row per document saying so would cost an
    INSERT and a commit each across thousands of documents to store a fact two
    columns already carry.
    """
    if resolved is None or resolved.start_field in (None, "created"):
        return None

    from app.ingestion.bundle_dates import describe

    moved = (bool(resolved.start_value) and bool(created)
             and resolved.start_value[:10] != str(created)[:10])
    if resolved.range_issue is not None:
        # The two dates the CMS states about this record contradict each
        # other, or the end is unusable, or there is an end and no start.
        # None of those can be settled from here — the start is still
        # applied, and a person gets to see why the range was not.
        action, rule = "needs_manual_review", f"range_{resolved.range_issue}"
    elif resolved.rule == "field_empty":
        action, rule = "keep_page_date", "bundle_field_empty"
    elif resolved.rule == "field_invalid":
        # Worth a person's attention: the CMS holds something in the field this
        # content type is dated by, and it is not a date.
        action, rule = "needs_manual_review", "bundle_field_invalid"
    elif moved:
        action, rule = "propose_override", "bundle_date_field"
    else:
        action, rule = "keep_page_date", "bundle_field_matches_created"

    return DecisionRow(
        document_id=document_id,
        # Not "attachment" or "inbody": this document is a page, not a file
        # reached from one.
        origin="website",
        bundle=resolved.bundle,
        # A source record is its own page, so the join every report already
        # makes on node_uuid resolves rather than dangling.
        node_uuid=document_id,
        page_pdf_count=1,
        # The record's own creation stamp — so a row reads as "would have been
        # X, assigned Y" exactly as the PDF rows do.
        current_start_date=created,
        candidate_start_date=resolved.start_value,
        candidate_end_date=resolved.end_value,
        range_issue=resolved.range_issue,
        # What the *source field* is, not what the date was used for. A
        # completed project's start date is applied as that document's date and
        # the field is still a `range_start`; flattening that away would erase
        # the one thing an auditor needs to see.
        #
        # This column carries a value from one of two vocabularies, told apart by
        # `origin`. A `website` row holds a
        # `source_dates.FieldRole` — what the Drupal field is. An `attachment` /
        # `inbody` row holds a `date_rules.DateType` — what the model judged a
        # date found inside the PDF's text to be, which is a different question
        # about a different thing. They were never one enumeration, and no query
        # should group across the two without filtering `origin` first.
        date_type=(resolved.field_role if resolved.source == "cms_field"
                   else "unknown"),
        date_source=(resolved.start_field if action == "propose_override"
                          else "node_effective_date"),
        # A transcription of what the source states, not an inference from it —
        # unlike the PDF path, where the same value is a model's judgement about
        # evidence and is capped accordingly.
        confidence=1.0 if action == "propose_override" else 0.5,
        action=action,
        rule=rule,
        decided_by="deterministic",
        evidence=describe(resolved, title=title, url=url),
        url=url,
    )


def record(row: DecisionRow) -> None:
    """Store one decision, replacing any earlier reading for the document."""
    if not row.document_id:
        return
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO `{_table()}_date_decision` "
            "(document_id, origin, bundle, node_uuid, page_pdf_count,"
            " current_start_date, candidate_start_date, candidate_end_date,"
            " range_issue, date_type, edition_label,"
            " date_source, confidence, action, rule, decided_by, evidence,"
            " llm_raw, prompt_version, url, filename,"
            " title_source, title_kind, title_disposition, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  origin = VALUES(origin), bundle = VALUES(bundle),"
            "  node_uuid = VALUES(node_uuid),"
            "  page_pdf_count = VALUES(page_pdf_count),"
            "  current_start_date = VALUES(current_start_date),"
            "  candidate_start_date = VALUES(candidate_start_date),"
            "  candidate_end_date = VALUES(candidate_end_date),"
            "  range_issue = VALUES(range_issue),"
            "  date_type = VALUES(date_type),"
            "  edition_label = VALUES(edition_label),"
            "  date_source = VALUES(date_source),"
            "  confidence = VALUES(confidence), action = VALUES(action),"
            "  rule = VALUES(rule), decided_by = VALUES(decided_by),"
            "  evidence = VALUES(evidence), llm_raw = VALUES(llm_raw),"
            "  prompt_version = VALUES(prompt_version), url = VALUES(url),"
            "  filename = VALUES(filename),"
            "  title_source = VALUES(title_source),"
            "  title_kind = VALUES(title_kind),"
            "  title_disposition = VALUES(title_disposition),"
            "  updated_at = VALUES(updated_at)",
            (
                row.document_id, row.origin, row.bundle, row.node_uuid,
                int(row.page_pdf_count),
                parse_iso_date(row.current_start_date, field="current effective_start_date"),
                parse_iso_date(row.candidate_start_date, field="candidate date"),
                parse_iso_date(row.candidate_end_date,
                               field="candidate end date"),
                row.range_issue,
                row.date_type, row.edition_label, row.date_source,
                round(float(row.confidence), 3), row.action, row.rule, row.decided_by,
                row.evidence,
                json.dumps(row.llm_raw, ensure_ascii=False) if row.llm_raw else None,
                row.prompt_version, row.url, row.filename,
                row.title_source, row.title_kind, row.title_disposition, _now(),
            ),
        )
        conn.commit()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def load() -> list[DecisionRow]:
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT * FROM `{_table()}_date_decision`")
        return [
            DecisionRow(
                document_id=row["document_id"],
                origin=row.get("origin") or "",
                bundle=row.get("bundle"),
                node_uuid=row.get("node_uuid"),
                page_pdf_count=int(row.get("page_pdf_count") or 1),
                current_start_date=_iso(row.get("current_start_date")),
                candidate_start_date=_iso(row.get("candidate_start_date")),
                candidate_end_date=_iso(row.get("candidate_end_date")),
                range_issue=row.get("range_issue"),
                date_type=row.get("date_type") or "unknown",
                edition_label=row.get("edition_label"),
                date_source=row.get("date_source") or "",
                confidence=float(row.get("confidence") or 0),
                action=row.get("action") or "",
                rule=row.get("rule") or "",
                decided_by=row.get("decided_by") or "",
                evidence=row.get("evidence"),
                prompt_version=row.get("prompt_version"),
                url=row.get("url"),
                filename=row.get("filename"),
            )
            for row in cur.fetchall()
        ]
