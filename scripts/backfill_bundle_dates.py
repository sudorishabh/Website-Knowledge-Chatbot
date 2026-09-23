"""Re-date the corpus from the bundle -> date-field mapping. Dry run first.

Changing ingestion code does not re-date anything already ingested. A sweep that
finds a document unchanged returns before the document is even rebuilt
(``pipeline._handle``), so an existing row keeps whatever date the old rule gave
it until something rewrites it. This is that something.

Two passes, in order, because the second depends on the first:

1. **Pages.** Re-resolve every ``website`` document from its stored ``bundle``
   and ``raw_meta`` through :func:`app.ingestion.bundle_dates.resolve` — the same
   function ingestion calls, so a document re-crawled after this runs keeps the
   dates this gave it. A bundle whose mapping declares an end field resolves
   **both** ends here.
2. **Attachments.** Every ``pdf_attachment`` takes its parent page's new dates and
   precisions — start *and* end. Not re-derived per file: the parent's resolution
   is the answer, and the link table is what says which files hang off which page.

It is also the **migration off the publication-date model**. The system no
longer has a "published date": a document has an effective start date and,
where its bundle declares one, an effective end date. Three further phases,
each explicit:

3. **Carry the legacy columns across.** ``published_at`` -> ``effective_start_date``
   and its four siblings, filling only rows whose replacement is still NULL, so
   a re-run cannot overwrite what the new pipeline has written.
4. **Rewrite the Qdrant payload keys.** The same rename on every point, plus the
   deletion of the old keys. Metadata only: no vector is recomputed, because the
   chunk text is untouched.
5. **Drop the legacy columns** (``--drop-legacy``), gated on every value having
   been carried across. Separate from the rest because a drop is the one step
   that cannot be undone.

Nothing is re-extracted, re-chunked or re-embedded: a date is metadata, so the
correction is an ``UPDATE`` plus a ``set_payload``. The content hash is
untouched. ``PIPELINE_VERSION`` **is** bumped (``PAYLOAD``) because payload keys
change — see ``app/ingestion/version.py``; chunk ids and embed inputs do not, so
re-indexed documents reuse their stored vectors and nothing is re-embedded.

    python -m scripts.backfill_bundle_dates                  # show the diff
    python -m scripts.backfill_bundle_dates --limit 20       # scoped trial
    python -m scripts.backfill_bundle_dates --apply
    python -m scripts.backfill_bundle_dates --apply --drop-legacy
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.ingestion.bundle_dates import (
    BUNDLE_DATE_FIELDS,
    EffectiveDate,
    resolve_effective_dates,
)

#: ``date_source`` values this script cannot recompute, and therefore preserves.
#:
#: The line is drawn at **what the evidence needs**, not at "the resolver
#: decided it". Both of these were read out of the PDF's own bytes — a quoted
#: publication statement, a copyright year its DocInfo corroborates — and this
#: script has no bytes. Overwriting them with anything it *can* work out would
#: replace better evidence with worse.
TEXT_DERIVED_SOURCES: frozenset[str] = frozenset({
    "document_text", "document_copyright",
})

#: ``date_source`` values this script **re-derives on every run**.
#:
#: ``document_title`` and ``no_evidence`` come from the file's naming and from
#: the shape of its page — both of which are in the catalogue, so
#: :func:`_attachment_target` reaches them by calling the same functions
#: ingestion calls. Skipping them as "already decided" is what made the
#: ending-year change invisible to 55 documents: the rule moved, the rows did
#: not, and a dry run reported two. A re-derived value that has not changed is
#: not a move, so nothing churns.
RECOMPUTED_SOURCES: frozenset[str] = frozenset({
    "document_title", "no_evidence",
})


@dataclass(frozen=True)
class Move:
    """One document's date correction."""

    document_id: str
    source_type: str
    bundle: str | None
    url: str | None
    old_start: str
    #: ``None`` when the correct outcome is *no date* — a file on a multi-PDF
    #: page dated only by its Drupal creation stamp, which is not that file's to
    #: borrow. Written as SQL NULL, not skipped: leaving the wrong date in place
    #: is the bug.
    new_start: str | None
    start_precision: str | None
    source: str
    rule: str
    field: str | None
    #: The end of the period, as it should be stored. None when the bundle has
    #: no end field, or when the record's end was missing or unusable.
    old_end: str | None = None
    new_end: str | None = None
    end_precision: str | None = None
    #: Set when the two dates the CMS states contradict each other. The start is
    #: still applied; the end is not, and the row lands in the review queue.
    range_issue: str | None = None
    #: The page this attachment inherited from. None for a page.
    parent_id: str | None = None
    #: The resolver's own decision, for an attachment whose date came from its
    #: naming. Carried verbatim rather than re-derived so the audit row records
    #: what actually happened, down to the quoted statement.
    audit: Any = None
    filename: str | None = None
    #: How many PDFs the parent page holds — the fact the whole attachment rule
    #: turns on, and the first thing a reviewer checks.
    page_pdf_count: int = 1
    #: The parent page's own resolved date, so the row reads "would have been X,
    #: assigned Y".
    parent_start: str | None = None

    @property
    def start_changed(self) -> bool:
        return bool(self.old_start) and self.old_start[:10] != (self.new_start or "")[:10]

    @property
    def start_cleared(self) -> bool:
        """Did this move *remove* a date rather than correct one?"""
        return self.new_start is None and bool(self.old_start)

    @property
    def end_changed(self) -> bool:
        return (self.old_end or "")[:10] != (self.new_end or "")[:10]

    @property
    def range_added(self) -> bool:
        return self.new_end is not None and self.old_end is None

    @property
    def days(self) -> int:
        """How many days too late the stored date was. Negative = too early.

        Zero for a move that *removes* a date: there is no new date to measure a
        distance to, and reporting one would put a cleared document in a
        "shifted by N days" bucket it does not belong in.
        """
        if not self.old_start or not self.new_start:
            return 0
        old = datetime.fromisoformat(self.old_start[:19]).date()
        new = datetime.fromisoformat(self.new_start[:19]).date()
        return (old - new).days


def _metadata(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _iso(value: Any) -> str | None:
    """A stored datetime as an ISO string, **with its UTC offset**.

    MySQL hands back a naive datetime because the column is a DATETIME and
    `state._to_datetime` normalises to naive UTC on the way in. Passing that
    through verbatim produces `2017-12-28T08:58:09`, which is the same string
    ingestion would have written as `...+00:00` — and an offset-less value read
    by a consumer in another zone shifts the calendar date, which is the whole
    class of bug this migration exists to correct. The value is UTC; this says so
    rather than leaving the reader to assume it.
    """
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.isoformat()


def _readable(cur, table: str, new: str, legacy: str, alias: str = "") -> str:
    """A SQL expression for ``new``, falling back to ``legacy`` where it exists.

    The migration has three states and this has to read correctly in all of them:
    before the copy (only ``legacy`` holds anything), after the copy (both do,
    and they agree), and after the drop (``legacy`` is gone). ``COALESCE`` covers
    the first two; the column check covers the third.

    Without this the dry run reads the empty new columns, concludes every page
    already agrees with itself, and reports only attachment moves — which is
    exactly the shape of a half-migrated database and exactly the wrong answer.
    """
    from app.catalog.schema import _column_exists

    prefix = f"{alias}." if alias else ""
    if _column_exists(cur, table, legacy):
        return f"COALESCE({prefix}`{new}`, {prefix}`{legacy}`)"
    return f"{prefix}`{new}`"


def created_stamps() -> dict[str, str]:
    """Each website document's *original* creation stamp, where it is recoverable.

    Needed because ``documents.effective_start_date`` is no longer necessarily the
    creation stamp: a previous run of ``scripts.backfill_source_dates`` moved
    ~1,000 rows onto a CMS field, and ``raw_meta`` does not carry ``created`` (the
    extractor keeps only ``field_*`` attributes).

    Two recoverable cases, and one that is not:

    * ``date_source`` is ``created`` or NULL — the stored value *is* the
      creation stamp, which is what that label means.
    * a ``{state}_date_decision`` row exists — its ``current_start_date`` is
      the creation stamp, recorded there by the write path that moved the row.
    * neither — the original is genuinely lost, and this run reports the document
      rather than guessing. Re-ingesting it restores the stamp.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    stamps: dict[str, str] = {}
    with mysql_connection() as conn, conn.cursor() as cur:
        start = _readable(cur, table, "effective_start_date", "published_at")
        source = _readable(cur, table, "date_source", "published_at_source")
        cur.execute(
            f"SELECT document_id, {start} AS effective_start_date, "
            f"       {source} AS date_source "
            f"FROM `{table}` WHERE source_type = 'website'"
        )
        for row in cur.fetchall():
            if row["date_source"] in (None, "", "created"):
                stamp = _iso(row["effective_start_date"])
                if stamp:
                    stamps[row["document_id"]] = stamp
        try:
            decisions = f"{table}_date_decision"
            current = _readable(cur, decisions, "current_start_date",
                                "current_published_at")
            cur.execute(
                f"SELECT document_id, {current} AS current_start_date "
                f"FROM `{decisions}` WHERE origin = 'website'"
            )
            for row in cur.fetchall():
                stamp = _iso(row["current_start_date"])
                if stamp:
                    # Wins over the inference above: it is a recorded fact rather
                    # than a deduction from a label.
                    stamps[row["document_id"]] = stamp
        except Exception:
            # The table may not exist on a deployment that never ran the
            # resolver. The first branch still covers every unmoved row.
            pass
    return stamps


def page_moves() -> tuple[list[Move], dict[str, EffectiveDate], list[str]]:
    """``(moves, resolution per page, documents whose creation stamp is lost)``.

    The resolution map is returned whole — not just the moves — because an
    attachment inherits its parent's date whether or not that date *changed*.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    stamps = created_stamps()
    with mysql_connection() as conn, conn.cursor() as cur:
        table = state_table()
        start = _readable(cur, table, "effective_start_date", "published_at")
        precision = _readable(cur, table, "start_precision",
                              "published_at_precision")
        end = _readable(cur, table, "effective_end_date", "published_until")
        end_precision = _readable(cur, table, "end_precision",
                                  "published_until_precision")
        cur.execute(
            f"SELECT document_id, bundle, url, raw_meta, "
            f"       {start} AS effective_start_date, "
            f"       {precision} AS start_precision, "
            f"       {end} AS effective_end_date, "
            f"       {end_precision} AS end_precision "
            f"FROM `{table}` "
            f"WHERE source_type = 'website' "
            # Ordered so `--limit` selects the same subset every run. Without it
            # a scoped trial cannot be verified: the rows inspected beforehand
            # would not be the rows written.
            f"ORDER BY document_id"
        )
        rows = list(cur.fetchall())

    moves: list[Move] = []
    resolutions: dict[str, EffectiveDate] = {}
    unrecoverable: list[str] = []
    for row in rows:
        document_id = row["document_id"]
        stored = _iso(row["effective_start_date"])
        created = stamps.get(document_id)
        # The same single decision ingestion makes, from the metadata rather than
        # from the current value — so the run is idempotent and cannot compound.
        resolved = resolve_effective_dates(
            row["bundle"], created, _metadata(row["raw_meta"]))
        # A lost creation stamp only matters when the answer depends on it. A
        # `news` page whose `field_news_date` is intact resolves from the CMS
        # field and never consults `created`, so skipping it would drop a
        # document we can date exactly — the earlier version did that to 2,770
        # pages. Only a resolution that actually fell back to the stamp is
        # unrecoverable, and those are reported rather than guessed at.
        if created is None and resolved.source != "cms_field":
            unrecoverable.append(document_id)
            continue
        resolutions[document_id] = resolved
        if not resolved.start_value or not stored:
            continue
        stored_end = _iso(row["effective_end_date"])
        if (resolved.start_value[:10] == stored[:10]
                and resolved.start_precision == (row["start_precision"] or "day")
                and (resolved.end_value or "")[:10] == (stored_end or "")[:10]):
            continue
        moves.append(Move(
            document_id=document_id, source_type="website", bundle=row["bundle"],
            url=row["url"], old_start=stored, new_start=resolved.start_value,
            start_precision=resolved.start_precision, source=resolved.source,
            rule=resolved.rule, field=resolved.start_field,
            old_end=stored_end, new_end=resolved.end_value,
            end_precision=resolved.end_precision,
            range_issue=resolved.range_issue,
        ))
    return moves, resolutions, unrecoverable


def attachment_moves(resolutions: dict[str, EffectiveDate]) -> list[Move]:
    """Every attached file that does not currently carry its page's date.

    An attachment reached from more than one page — 84 of them are — takes the
    first parent by document id, deterministically, which is the same rule the
    crawl's per-run dedup applies.

    **This does not mean "copy the parent's date onto every file".** It means
    "apply what ingestion would apply", and for a file sharing its page that is
    not inheritance. Two of the resolver's rules need no PDF bytes — the file's
    own naming and the weak-shelf drop — so both are applied here by calling the
    *same functions* ingestion calls
    (:func:`app.ingestion.date_resolution.read_title_evidence`,
    :func:`app.ingestion.date_rules.page_date_is_usable`). A second
    implementation of either would drift, and the drift would look exactly like
    the bug this script exists to correct: a sweep quietly re-stamping 300
    documents with the day their shelf page was typed.

    The rule that *does* need bytes is not re-run here; its verdicts are
    recognised and left alone (:data:`TEXT_DERIVED_SOURCES`):

    * ``document_text`` — a publication statement verified inside the PDF's text.
    * ``document_copyright`` — a copyright year its own DocInfo corroborates.

    Everything else is **re-derived, not preserved** (:data:`RECOMPUTED_SOURCES`).
    A row already reading ``document_title`` is not evidence that the current
    rule would still produce that date — when the edition rule moved from the
    opening year of a span to the closing one, 55 documents needed exactly this
    path and a skip list would have hidden every one of them.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        start = _readable(cur, table, "effective_start_date", "published_at", "d")
        precision = _readable(cur, table, "start_precision",
                              "published_at_precision", "d")
        end = _readable(cur, table, "effective_end_date", "published_until", "d")
        end_precision = _readable(cur, table, "end_precision",
                                  "published_until_precision", "d")
        source = _readable(cur, table, "date_source", "published_at_source", "d")
        cur.execute(
            f"SELECT a.document_id AS parent_id, a.file_uuid AS document_id, "
            f"       {start} AS effective_start_date, "
            f"       {precision} AS start_precision, "
            f"       {end} AS effective_end_date, "
            f"       {end_precision} AS end_precision, "
            f"       {source} AS date_source, "
            f"       d.url, d.bundle, d.title, a.filename, a.origin, "
            f"       p.title AS parent_title "
            f"FROM `{table}_attachment` a "
            f"JOIN `{table}` d ON d.document_id = a.file_uuid "
            f"LEFT JOIN `{table}` p ON p.document_id = a.document_id "
            f"WHERE d.source_type = 'pdf_attachment' "
            f"ORDER BY a.file_uuid, a.document_id"
        )
        rows = list(cur.fetchall())
        # How many PDFs each page holds — the single fact that decides whether a
        # file is that page's document or one of a shelf of them. Counted the
        # same way the crawl counts it: distinct files per node.
        cur.execute(
            f"SELECT document_id, COUNT(DISTINCT file_uuid) AS n "
            f"FROM `{table}_attachment` GROUP BY document_id"
        )
        pdf_counts = {r["document_id"]: int(r["n"]) for r in cur.fetchall()}

    seen: set[str] = set()
    moves: list[Move] = []
    for row in rows:
        document_id = row["document_id"]
        if document_id in seen:
            continue
        seen.add(document_id)
        if row["date_source"] in TEXT_DERIVED_SOURCES:
            continue
        parent = resolutions.get(row["parent_id"])
        if parent is None or not parent.start_value:
            continue
        stored = _iso(row["effective_start_date"])
        stored_end = _iso(row["effective_end_date"])
        target = _attachment_target(
            row, parent, pdf_count=pdf_counts.get(row["parent_id"], 1))
        # "Already correct" has to include "already correctly undated", or every
        # run re-proposes the same 321 no-op writes. A NULL precision means
        # `day` only where there is a date for it to describe; with no date
        # there is no precision, and reading one in makes an undated row look
        # different from an undated target forever.
        stored_precision = row["start_precision"] or ("day" if stored else None)
        same_day = (
            (stored or None) == (target.start or None)
            or bool(stored and target.start and target.start[:10] == stored[:10])
        )
        if (same_day
                and target.precision == stored_precision
                and (target.end or "")[:10] == (stored_end or "")[:10]):
            continue
        moves.append(Move(
            document_id=document_id, source_type="pdf_attachment",
            bundle=parent.bundle or row["bundle"], url=row["url"],
            old_start=stored or "", new_start=target.start,
            start_precision=target.precision, source=target.source,
            rule=target.rule, field=parent.start_field,
            old_end=stored_end, new_end=target.end,
            end_precision=target.end_precision,
            range_issue=parent.range_issue if target.source == "parent_page" else None,
            parent_id=row["parent_id"],
            audit=target.decision, filename=row.get("filename"),
            page_pdf_count=pdf_counts.get(row["parent_id"], 1),
            parent_start=parent.start_value,
        ))
    return moves


@dataclass(frozen=True)
class _Target:
    """What one attachment's date should be, once the byteless rules have run."""

    start: str | None
    precision: str | None
    end: str | None
    end_precision: str | None
    source: str
    rule: str
    #: The resolver's own :class:`~app.ingestion.date_rules.DateDecision`, where
    #: one was produced. None for plain inheritance, which the page's own
    #: decision row already explains.
    decision: Any = None


def _attachment_target(row: dict, parent: EffectiveDate, *, pdf_count: int) -> _Target:
    """The date this attachment should carry — by asking ingestion, not by
    restating it.

    A :class:`~app.ingestion.date_evidence.PdfEvidence` is assembled from the
    catalog row and handed to the very functions the ingestion path uses, so
    there is one set of rules and this script cannot disagree with a crawl.
    Only the rules that need no PDF bytes can run here; everything else falls
    through to inheritance, which is what a file with no self-statement gets on
    the ingestion path too.
    """
    from app.ingestion.date_evidence import PageContext, PdfEvidence
    from app.ingestion.date_resolution import read_title_evidence
    from app.ingestion.date_rules import page_date_is_usable

    # A link label equal to the page's own title is the page's, not the file's —
    # `canonical.from_pdf` falls back to `node.title` when a file has no
    # description, so using it as link text would read the shelf's name as the
    # file's.
    anchor = row.get("title")
    if anchor and anchor == row.get("parent_title"):
        anchor = None
    page = PageContext(
        node_uuid=row["parent_id"] or "",
        node_title=row.get("parent_title") or "",
        node_created=parent.start_value,
        node_start_date=parent.start_value,
        node_start_precision=parent.start_precision,
        node_end_date=parent.end_value,
        node_end_precision=parent.end_precision,
        date_field=parent.start_field,
        date_source=parent.source,
        bundle=parent.bundle,
        pdf_count=max(1, pdf_count),
    )
    evidence = PdfEvidence(
        document_id=row["document_id"], origin=row.get("origin") or "attachment",
        url=row.get("url"), filename=row.get("filename"), anchor=anchor,
        page=page,
    )

    titled = read_title_evidence(evidence).decision
    if titled is not None:
        # An override replaces only the temporal information it establishes —
        # the same rule `date_resolution._end_after_override` applies.
        point = titled.candidate_precision in ("day", "month")
        keeps_order = (
            parent.end_value is None
            or str(titled.candidate_start_date)[:10] <= parent.end_value[:10]
        )
        keep_end = (not point) and keeps_order
        return _Target(
            start=titled.candidate_start_date,
            precision=titled.candidate_precision,
            end=parent.end_value if keep_end else None,
            end_precision=parent.end_precision if keep_end else None,
            source="document_title", rule="title_states_date",
            decision=titled,
        )

    if not page_date_is_usable(page):
        from app.ingestion.date_rules import DateDecision

        return _Target(
            start=None, precision=None, end=None, end_precision=None,
            source="no_evidence", rule="multi_pdf_no_evidence",
            decision=DateDecision(
                document_id=row["document_id"], action="drop_page_date",
                candidate_start_date=None, date_type="unknown",
                source="node_effective_date", confidence=0.0,
                rule="multi_pdf_no_evidence", decided_by="deterministic",
                evidence=(
                    f"One of {page.pdf_count} PDFs on a {page.bundle} page dated "
                    f"only by its Drupal creation stamp "
                    f"({str(page.effective_date)[:10]}), and the file states no "
                    f"date of its own. Left undated rather than given a date "
                    f"nothing established."
                ),
                used=["drupal"],
            ),
        )

    return _Target(start=parent.start_value, precision=parent.start_precision,
                   end=parent.end_value, end_precision=parent.end_precision,
                   source="parent_page", rule="inherited_from_parent")


def invariants() -> dict[str, Any]:
    """Everything this run must leave alone, in a form that can be compared.

    The content-hash checksum is the important one: this run is metadata-only,
    and a change there means something re-chunked the corpus.
    """
    from app.catalog.db import state_table
    from app.config import get_settings
    from app.core.clients import get_qdrant_client, mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) n FROM `{table}`")
        documents = int(cur.fetchall()[0]["n"])
        cur.execute(
            f"SELECT document_id, content_hash, doc_version FROM `{table}` "
            f"ORDER BY document_id"
        )
        digest = hashlib.sha256()
        for row in cur.fetchall():
            digest.update(
                f"{row['document_id']}|{row['content_hash']}|{row['doc_version']}\n"
                .encode())
        cur.execute(f"SELECT COUNT(*) n FROM `{table}` WHERE effective_start_date IS NULL")
        undated = int(cur.fetchall()[0]["n"])

    points = None
    try:
        client = get_qdrant_client()
        collection = get_settings().qdrant_collection
        if client.collection_exists(collection):
            points = client.count(collection_name=collection, exact=True).count
    except Exception:
        points = "unreadable"

    return {
        "documents": documents,
        "content_checksum": digest.hexdigest()[:16],
        "documents_without_a_date": undated,
        "qdrant_points": points,
    }


def preflight(moves: list[Move], *, expect: int) -> list[str]:
    """Reasons to refuse, computed before the first write."""
    problems: list[str] = []
    if expect >= 0 and len(moves) != expect:
        problems.append(
            f"{len(moves)} documents would move, expected {expect}. Re-review the "
            f"dry run and pass --expect if the corpus has legitimately changed."
        )
    mapped = {f for fields in BUNDLE_DATE_FIELDS.values() for f in fields}
    stray = {m.field for m in moves} - mapped - {None}
    if stray:
        problems.append(f"field(s) {sorted(stray)} are not in the bundle mapping")
    stray_sources = ({m.source for m in moves}
                     - {"created", "cms_field", "parent_page", "document_title",
                        "no_evidence"})
    if stray_sources:
        problems.append(f"unexpected provenance {sorted(stray_sources)}")
    # A cleared date is a deliberate outcome, not a value: every check below
    # asks about the *shape* of a value, so a move that removes one is exempt
    # from all of them and gets its own two instead.
    cleared = [m for m in moves if m.new_start is None]
    valued = [m for m in moves if m.new_start is not None]
    mislabelled = [m for m in cleared if m.source != "no_evidence"]
    if mislabelled:
        problems.append(
            f"{len(mislabelled)} move(s) clear the date without recording "
            f"`no_evidence` as the reason")
    with_leftovers = [m for m in cleared
                      if m.new_end is not None or m.start_precision is not None]
    if with_leftovers:
        problems.append(
            f"{len(with_leftovers)} cleared move(s) still carry an end date or a "
            f"precision; both would read as claims about a date that is not there")
    # A year-start_precision value is stored as 1 January *as a marker for the year*.
    # Any other day would mean the value and its start_precision disagree about what is
    # known, which is what `year_precision_not_january` watches for.
    off_january = [m for m in valued
                   if m.start_precision == "year"
                   and not m.new_start.startswith(m.new_start[:4] + "-01-01")]
    if off_january:
        problems.append(
            f"{len(off_january)} year-start_precision value(s) are not 1 January")
    if any(not m.new_start.endswith("+00:00") and "+" not in m.new_start[10:]
           for m in valued):
        problems.append("a value carries no timezone; the calendar date would shift")
    # An end before its start must never reach the column. `bundle_dates` drops
    # an inverted end rather than resolving it, so this is a belt-and-braces
    # assertion that nothing downstream reassembled one.
    backwards = [m for m in valued
                 if m.new_end and m.new_end[:10] < m.new_start[:10]]
    if backwards:
        problems.append(
            f"{len(backwards)} range(s) would be stored end-before-start")
    if any(m.new_end and not m.new_end.endswith("+00:00") for m in moves):
        problems.append("an end value is not stored as UTC")
    return problems


def apply(moves: list[Move], *, progress_every: int = 200) -> dict[str, int]:
    """Write the corrections to MySQL and Qdrant, and record why.

    Both stores, in that order. MySQL alone would be reverted the next time
    anyone ran ``app.ingestion.backfill`` — it lifts ``effective_start_date`` out of the
    chunk payloads and writes it back with a bare SET.
    """
    from qdrant_client import models as qm

    from app.catalog.db import state_table
    from app.config import get_settings
    from app.core.clients import get_qdrant_client, mysql_connection

    table = state_table()
    client = get_qdrant_client()
    collection = get_settings().qdrant_collection
    tally: Counter = Counter()

    with mysql_connection() as conn, conn.cursor() as cur:
        cur.executemany(
            f"UPDATE `{table}` SET effective_start_date = %s, date_source = %s, "
            f"start_precision = %s, effective_end_date = %s, "
            f"end_precision = %s, updated_at = NOW() "
            f"WHERE document_id = %s",
            # updated_at moves; indexed_at deliberately does not. That column
            # means "was re-chunked and re-indexed", which has not happened —
            # and `corpus_revision` reads MAX(indexed_at), so claiming it here
            # would be both false and a silent cache invalidation.
            # The end is written as NULL when there is none, not skipped. A
            # record whose CMS end date was cleared, or whose range turned out to
            # be inverted, has to lose the stored end rather than keep a value
            # the source no longer supports.
            [(m.new_start[:19].replace("T", " ") if m.new_start else None,
              m.source, m.start_precision,
              m.new_end[:19].replace("T", " ") if m.new_end else None,
              m.end_precision, m.document_id) for m in moves],
        )
        tally["mysql_rows"] = cur.rowcount
        conn.commit()

    for index, move in enumerate(moves, start=1):
        # Precision goes with the value, not after it. A year-start_precision date is
        # 1 January standing in for a year, and the payload is the only place the
        # answer layer can learn that — set the date alone and the model reports
        # a January publication nobody stated. Written only for "year", and
        # explicitly cleared otherwise: a document that *was* year-start_precision and
        # is no longer would keep a stale marker.
        payload: dict[str, Any] = {
            "effective_start_date": move.new_start,
            "start_precision": ("year" if move.start_precision == "year" else None),
            "effective_end_date": move.new_end,
            "end_precision": ("year" if move.end_precision == "year"
                                          else None),
        }
        selector = qm.Filter(must=[qm.FieldCondition(
            key="document_id", match=qm.MatchValue(value=move.document_id))])
        client.set_payload(collection_name=collection,
                           payload={k: v for k, v in payload.items()
                                    if v is not None},
                           points=selector)
        # A key whose new value is absent is deleted, not left alone: a point
        # carrying a year marker or an end date the source no longer states would
        # otherwise keep it forever.
        stale = [key for key, value in payload.items() if value is None]
        if stale:
            client.delete_payload(collection_name=collection, keys=stale,
                                  points=selector)
        tally["payloads_set"] += 1
        if index % progress_every == 0:
            print(f"    payloads rewritten: {index}/{len(moves)}")

    return dict(tally)


#: ``old payload key -> new`` on every point in the collection. The values are
#: unchanged; only the names move. Written as data so the dry run can report it
#: and the test can assert the two ends agree with the schema map.
#: The date keys a chunk payload carries under the effective-date model.
_DATE_PAYLOAD_KEYS: tuple[str, ...] = (
    "effective_start_date", "start_precision",
    "effective_end_date", "end_precision",
)

LEGACY_PAYLOAD_KEYS: dict[str, str] = {
    "published_at": "effective_start_date",
    "published_until": "effective_end_date",
    "published_at_precision": "start_precision",
    "published_until_precision": "end_precision",
    # Modelled once, never populated by any path, and removed rather than
    # renamed: every point carries it as absent or NULL.
    "document_published_at": "",
}


def migrate_payload_keys(*, batch: int = 512) -> dict[str, int]:
    """Rename the date keys on every stored point. Values are carried, not derived.

    Scrolls the collection once, and for each point that still carries a legacy
    key writes the new key with the same value and deletes the old one. Points
    already migrated cost one read and no write, so a second run is nearly free
    — which is what makes it safe to run after an interrupted first.

    **Order-independent, and that is load-bearing.** `apply()` runs first and
    writes corrected dates under the *new* key while leaving the legacy key on
    the point. A version of this that read only the legacy names carried the
    stale value straight back over every correction — MySQL right, Qdrant
    silently reverted on all 5,154 moved documents. So it reads both names and
    **never overwrites a new key that already holds a value**: the legacy key is
    a fallback for points nothing has corrected, never a source of truth.

    No vector is touched. `set_payload`/`delete_payload` do not re-embed, and the
    chunk text this collection was built from has not changed.
    """
    from qdrant_client import models as qm

    from app.config import get_settings
    from app.core.clients import get_qdrant_client

    client = get_qdrant_client()
    collection = get_settings().qdrant_collection
    tally: Counter = Counter()
    if not client.collection_exists(collection):
        return {"collection": "absent"}

    legacy = list(LEGACY_PAYLOAD_KEYS)
    wanted = legacy + [new for new in LEGACY_PAYLOAD_KEYS.values() if new]
    offset: Any = None
    while True:
        points, offset = client.scroll(
            collection_name=collection, limit=batch, offset=offset,
            with_payload=wanted, with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            carried = {
                new: payload[old]
                for old, new in LEGACY_PAYLOAD_KEYS.items()
                # Only where the new key has nothing. A point `apply()` has
                # already corrected keeps its correction; the legacy value is
                # dropped, not promoted over it.
                if new and payload.get(old) not in (None, "")
                and payload.get(new) in (None, "")
            }
            stale = [k for k in legacy if k in payload]
            if not stale:
                continue
            selector = qm.PointIdsList(points=[point.id])
            if carried:
                client.set_payload(collection_name=collection, payload=carried,
                                   points=selector)
            client.delete_payload(collection_name=collection, keys=stale,
                                  points=selector)
            tally["points_migrated"] += 1
        if offset is None:
            break
    return dict(tally)


def catalog_dates() -> dict[str, tuple[str | None, str | None, str | None, str | None]]:
    """``document_id -> (start, start_precision, end, end_precision)`` from MySQL.

    The authority for the repair. After `copy_legacy_date_columns()` and
    `apply()` have both run, every row holds its resolved value — the corrected
    ones from the write, the rest carried verbatim because their resolution did
    not change them.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT document_id, effective_start_date, start_precision, "
            f"       effective_end_date, end_precision FROM `{state_table()}`"
        )
        return {
            row["document_id"]: (
                _iso(row["effective_start_date"]), row["start_precision"],
                _iso(row["effective_end_date"]), row["end_precision"],
            )
            for row in cur.fetchall()
        }


def _wanted_payload(entry: tuple) -> dict[str, Any]:
    """The date keys a point for this document should carry.

    Mirrors `chunking.payload.build_payload`: a precision is written **only**
    when it is ``"year"``, so absent means "a full date" — which is what every
    reader assumes. A key whose value is None here is deleted from the point
    rather than stored as null.
    """
    start, start_precision, end, end_precision = entry
    return {
        "effective_start_date": start,
        "start_precision": start_precision if start_precision == "year" else None,
        "effective_end_date": end,
        "end_precision": end_precision if end_precision == "year" else None,
    }


#: Backoff in seconds between retries. Long enough at the tail to sit out a
#: container restart: this run has been interrupted three times, once by the
#: whole compose stack being stopped and started underneath it (Qdrant and Neo4j
#: began 2ms apart, with unchanged `created` timestamps and RestartCount 0 — an
#: external `docker compose`, not a crash). A schedule that gave up after twelve
#: seconds turned a thirty-second outage into a half-finished migration.
_RETRY_BACKOFF: tuple[int, ...] = (2, 5, 15, 30, 60)


def _with_retry(what: str, call):
    """Run a Qdrant call, waiting out a server that has gone away.

    Two failure shapes have actually occurred here, and both are survivable:

    * `zstd decompressor error: Allocation error` — intermittent, client-side,
      against a container using a tenth of its memory. Not a state the next
      attempt inherits.
    * `RemoteProtocolError: Server disconnected` — the server genuinely went
      down and came back. That needs *waiting*, not just repeating.

    Every call this wraps is idempotent, so a retry can only repeat work, never
    compound it. That is what makes waiting the right response rather than a
    gamble.
    """
    import time

    for attempt, pause in enumerate(_RETRY_BACKOFF, start=1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - the client wraps several
            print(f"    {what} failed ({type(exc).__name__}: "
                  f"{str(exc)[:80]}); waiting {pause}s "
                  f"[{attempt}/{len(_RETRY_BACKOFF)}]")
            time.sleep(pause)
    # One last attempt, unguarded, so the caller sees the real exception.
    return call()


def _write_documents(client, collection, catalog, ordered, absent, tally,
                     progress_every: int) -> str:
    """Set each document's date payload. Returns the last id that landed."""
    from qdrant_client import models as qm

    last = ""
    for index, document_id in enumerate(ordered, start=1):
        wanted = _wanted_payload(catalog[document_id])
        keep = {k: v for k, v in wanted.items() if v is not None}
        drop = tuple(sorted(k for k, v in wanted.items() if v is None))
        if keep:
            selector = qm.Filter(must=[qm.FieldCondition(
                key="document_id", match=qm.MatchValue(value=document_id))])
            _with_retry(f"set {document_id}", lambda: client.set_payload(
                collection_name=collection, payload=keep, points=selector))
            tally["documents_written"] += 1
        if drop:
            absent.setdefault(drop, []).append(document_id)
        last = document_id
        if index % progress_every == 0:
            print(f"    documents written: {index}/{len(ordered)}  (last {last})")
    return last


def push_catalog_dates_to_qdrant(
    *, progress_every: int = 500, start_after: str | None = None
) -> dict[str, Any]:
    """Write every document's catalogue dates onto its points. No scroll.

    Ordered by document id and idempotent, so an interrupted run resumes with
    ``--start-after <the last id printed>`` and a full re-run is merely wasteful
    rather than wrong.

    Three shapes of call, in order:

    1. one ``delete_payload`` for the legacy keys across the whole collection —
       their values are superseded by the catalogue for every document, so there
       is nothing to decide per point;
    2. one ``set_payload`` per document carrying the values it should have;
    3. batched ``delete_payload`` calls for the keys a document must *not* carry
       (a precision marker is written only for ``year``, and absent means "a full
       date"), grouped by which keys those are — four combinations, so a few
       dozen calls rather than twelve thousand.
    """
    from qdrant_client import models as qm

    from app.config import get_settings
    from app.core.clients import get_qdrant_client

    client = get_qdrant_client()
    collection = get_settings().qdrant_collection
    if not client.collection_exists(collection):
        return {"collection": "absent"}

    catalog = catalog_dates()
    legacy = list(LEGACY_PAYLOAD_KEYS)
    tally: Counter = Counter()

    def _for(document_id: str) -> qm.Filter:
        return qm.Filter(must=[qm.FieldCondition(
            key="document_id", match=qm.MatchValue(value=document_id))])

    ordered = sorted(catalog)
    if start_after:
        ordered = [d for d in ordered if d > start_after]
        print(f"  resuming after {start_after}: {len(ordered)} document(s) left")

    absent: dict[tuple, list[str]] = {}
    last = ""
    try:
        last = _write_documents(client, collection, catalog, ordered, absent,
                                tally, progress_every)
    except Exception:
        # The resume point is the last id that actually landed, so a re-run does
        # not have to redo six thousand documents to reach the failure.
        print("\n  INTERRUPTED. Resume with:")
        print(f"    python -m scripts.backfill_bundle_dates --repair-qdrant "
              f"--apply --start-after {last or '<nothing landed yet>'}")
        raise


    # The keys a document must not carry, grouped by which they are so this is a
    # few dozen calls rather than one per document.
    for keys, ids in absent.items():
        for chunk in range(0, len(ids), 200):
            batch = ids[chunk:chunk + 200]
            _with_retry("absent-key delete", lambda: client.delete_payload(
                collection_name=collection, keys=list(keys),
                points=qm.Filter(must=[qm.FieldCondition(
                    key="document_id", match=qm.MatchAny(any=batch))])))
            tally["absent_key_calls"] += 1

    # The legacy keys go LAST, and only on a run that reached the end. Deleting
    # them first would open a window in which a point carried neither the old key
    # nor the new one — strictly worse than the state this is recovering from,
    # and the runs here have crashed twice. Ordered this way, every point holds a
    # usable date at every moment: the legacy value until its document is
    # written, the catalogue value afterwards.
    #
    # Skipped on a resumed run, because the documents before `--start-after` were
    # written by an earlier pass and this pass cannot confirm that. Finish with
    # one full pass, or drop the keys with a verify run afterwards.
    if start_after is None:
        print(f"  dropping the legacy keys collection-wide: {', '.join(legacy)}")
        _with_retry("legacy delete", lambda: client.delete_payload(
            collection_name=collection, keys=legacy, points=qm.Filter()))
        tally["legacy_delete_calls"] += 1
    else:
        print("  legacy keys left in place: this was a resumed pass, so it "
              "cannot vouch for the documents before --start-after")

    return {
        "documents": len(ordered),
        "documents_written": tally["documents_written"],
        "absent_key_calls": tally["absent_key_calls"],
        "legacy_keys_dropped": bool(tally["legacy_delete_calls"]),
        "last_document_id": last,
    }


def repair_qdrant_from_catalog(
    *, batch: int = 400, apply_writes: bool = False
) -> dict[str, Any]:
    """Reconcile every point's date keys to its document's MySQL row.

    Reads MySQL, scrolls the collection once, and for each point compares what
    it carries to what the catalogue says. Points that already agree cost one
    read and no write, so re-running after an interruption is cheap and safe.

    ``apply_writes=False`` (the default) reports what it would change and writes
    nothing — the same dry-run discipline the rest of this script uses.

    The scroll batch is deliberately small: the run this exists to recover from
    died inside the client's zstd decompression with an allocation error, and a
    smaller response is the cheapest way not to repeat that.
    """
    from qdrant_client import models as qm

    from app.config import get_settings
    from app.core.clients import get_qdrant_client

    client = get_qdrant_client()
    collection = get_settings().qdrant_collection
    if not client.collection_exists(collection):
        return {"collection": "absent"}

    catalog = catalog_dates()
    legacy = [k for k in LEGACY_PAYLOAD_KEYS]
    fields = ["document_id", *_DATE_PAYLOAD_KEYS, *legacy]
    tally: Counter = Counter()
    pending: dict[tuple, list] = {}
    orphans: set[str] = set()

    def flush() -> None:
        for payload_items, ids in pending.items():
            payload = dict(payload_items)
            drop = [k for k, v in payload.items() if v is None]
            keep = {k: v for k, v in payload.items() if v is not None}
            selector = qm.PointIdsList(points=ids)
            if apply_writes:
                if keep:
                    client.set_payload(collection_name=collection, payload=keep,
                                       points=selector)
                if drop:
                    client.delete_payload(collection_name=collection, keys=drop,
                                          points=selector)
            tally["points_written"] += len(ids)
        pending.clear()

    offset: Any = None
    scanned = 0
    while True:
        points, offset = client.scroll(
            collection_name=collection, limit=batch, offset=offset,
            with_payload=fields, with_vectors=False,
        )
        for point in points:
            scanned += 1
            current = point.payload or {}
            document_id = current.get("document_id")
            entry = catalog.get(document_id) if document_id else None
            if entry is None:
                # A point whose document the catalogue does not know. Left
                # exactly as it is: reconciliation never invents a date, and an
                # orphaned point is a different problem with its own report.
                orphans.add(str(document_id))
                continue
            if any(key in current for key in legacy):
                tally["points_with_a_legacy_key"] += 1
            wanted = _wanted_payload(entry)
            if all(current.get(k) == v or (v is None and k not in current)
                   for k, v in wanted.items()):
                tally["already_correct"] += 1
                continue
            tally["points_to_write"] += 1
            pending.setdefault(tuple(sorted(wanted.items())), []).append(point.id)
        if len(pending) >= 64 or offset is None:
            flush()
        if offset is None:
            break

    # The legacy keys are dropped in one collection-wide call rather than per
    # point. Their values are superseded by the catalogue for every document, so
    # there is nothing to decide per point — and folding the deletion into the
    # per-point diff made every one of the 152,833 points "need a write", which
    # both hid how many actually carried a wrong date and turned a recovery into
    # ~24,000 API calls against a client that had just died of an allocation
    # error.
    if apply_writes and tally["points_with_a_legacy_key"]:
        client.delete_payload(collection_name=collection, keys=legacy,
                              points=qm.Filter())
        tally["legacy_keys_dropped"] = tally["points_with_a_legacy_key"]

    return {
        "scanned": scanned,
        "already_correct": tally["already_correct"],
        "points_needing_a_date_write": tally["points_to_write"],
        "points_written": tally["points_written"] if apply_writes else 0,
        "points_carrying_a_legacy_key": tally["points_with_a_legacy_key"],
        "legacy_keys_dropped": tally["legacy_keys_dropped"] if apply_writes else 0,
        "orphaned_points": len(orphans),
        "wrote": apply_writes,
    }


#: Properties the graph projection used to write and no longer does. Only one:
#: `writer.py` sets `d.effective_start_date` now, and nothing has ever written
#: the range fields onto a node.
LEGACY_GRAPH_PROPERTIES: tuple[str, ...] = ("published_at",)


def push_catalog_dates_to_graph(*, apply_writes: bool = False) -> dict[str, Any]:
    """Bring every ``:Document`` node's date into line with the catalogue.

    The third store, and the one this script never touched. Neo4j is a
    *projection* — ``app.knowledge.graph.project`` reads
    ``documents.effective_start_date`` and writes it onto the node — so a full
    re-projection would also fix it. This exists because a re-projection rebuilds
    claims, chunks and relationships to correct a date, which is a great deal of
    work and risk for a metadata change, and because the projection is not run on
    a schedule.

    **MySQL is read first and the graph is written from it**, never the reverse:
    the catalogue is the system of record and the graph is rebuildable from it.

    Two properties of Cypher matter here and both are load-bearing:

    * ``SET d.prop = NULL`` **removes** the property. That is exactly right for a
      document that is now deliberately undated — the node stops matching a date
      filter instead of matching the wrong year — but it means this write is how
      an undated document becomes undated in the graph, not an afterthought.
    * ``MATCH`` (not ``MERGE``) so a document the graph has never projected is
      left alone rather than conjured as a bare node with a date and nothing else.

    The node carries ``effective_start_date`` only; there is no end-date property
    on ``:Document`` and nothing reads one, so none is invented here.

    An unreachable graph is reported as skipped rather than failed, which is how
    the rest of the codebase treats Neo4j.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection
    from app.core.clients.graph import read_session, write_session

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT document_id, effective_start_date FROM `{table}` "
            f"ORDER BY document_id"
        )
        wanted = {r["document_id"]: _iso(r["effective_start_date"])
                  for r in cur.fetchall()}

    result: dict[str, Any] = {"catalog_documents": len(wanted)}
    try:
        with read_session() as session:
            rows = session.run(
                "MATCH (d:Document) "
                "RETURN d.document_id AS document_id, "
                "       d.effective_start_date AS effective_start_date"
            ).data()
    except Exception as exc:  # noqa: BLE001 - an optional store
        result["graph"] = f"unreachable ({type(exc).__name__}); skipped"
        return result

    result["graph_documents"] = len(rows)
    stale = []
    orphans = 0
    for row in rows:
        document_id = row["document_id"]
        if document_id not in wanted:
            orphans += 1
            continue
        if (row["effective_start_date"] or None) != wanted[document_id]:
            stale.append({"document_id": document_id,
                          "effective_start_date": wanted[document_id]})
    result["already_correct"] = len(rows) - len(stale) - orphans
    result["would_change" if not apply_writes else "changed"] = len(stale)
    result["nodes_not_in_the_catalogue"] = orphans
    result["losing_their_date"] = sum(1 for r in stale
                                      if r["effective_start_date"] is None)
    if not apply_writes or not stale:
        return result

    written = 0
    with write_session() as session:
        for start in range(0, len(stale), 500):
            batch = stale[start:start + 500]
            session.run(
                "UNWIND $rows AS row "
                "MATCH (d:Document {document_id: row.document_id}) "
                "SET d.effective_start_date = row.effective_start_date",
                rows=batch,
            )
            written += len(batch)
    result["changed"] = written
    return result


def drop_legacy_graph_property(*, apply_writes: bool = False) -> dict[str, Any]:
    """Remove the superseded date property from every ``:Document`` node.

    Tidying rather than a fix: nothing reads ``d.published_at`` any more. It is
    still guarded the same way as the other two drops — refused unless every node
    already carries ``effective_start_date`` — because the failure mode if that
    is not true is a node with no date at all, and a graph that silently matches
    nothing is exactly how this migration started.

    Treats an unreachable graph as skipped rather than failed, which is how the
    rest of the codebase treats Neo4j: it is a projection that can be rebuilt
    from MySQL, never the system of record.
    """
    from app.core.clients.graph import read_session, write_session

    result: dict[str, Any] = {}
    try:
        with read_session() as session:
            row = session.run(
                "MATCH (d:Document) RETURN count(d) AS documents, "
                "count(d.effective_start_date) AS dated, "
                "count(d.published_at) AS legacy"
            ).single()
    except Exception as exc:  # noqa: BLE001 - an optional store
        return {"graph": f"unreachable ({type(exc).__name__}); skipped"}

    documents, dated, legacy = row["documents"], row["dated"], row["legacy"]
    result.update(documents=documents, with_effective_start_date=dated,
                  carrying_legacy_property=legacy)

    if documents and dated < documents:
        result["refused"] = (
            f"{documents - dated} node(s) have no effective_start_date. "
            f"Re-run the graph projection first; removing the legacy property "
            f"now would leave them with no date at all."
        )
        return result
    if not legacy:
        result["nothing_to_do"] = "no node carries the legacy property"
        return result
    if not apply_writes:
        result["would_remove"] = list(LEGACY_GRAPH_PROPERTIES)
        return result

    removed = []
    with write_session() as session:
        for prop in LEGACY_GRAPH_PROPERTIES:
            session.run(
                f"MATCH (d:Document) WHERE d.`{prop}` IS NOT NULL "
                f"REMOVE d.`{prop}`"
            )
            removed.append(prop)
    result["removed"] = removed
    return result


def drop_legacy_payload_keys(*, apply_writes: bool = False) -> dict[str, Any]:
    """Delete the legacy date keys from every point. One call, guarded.

    Refuses unless the collection already agrees with the catalogue — because
    after this the legacy value is gone from Qdrant, and a point that never
    received its new value would be left with no date at all. MySQL still holds
    everything, so the situation would be recoverable, but only by re-running the
    repair, and the failure would be silent in the meantime.

    Deliberately *not* a full clean pass. Rewriting 12,003 documents to achieve
    one delete is twelve thousand writes of exposure to buy nothing.
    """
    from qdrant_client import models as qm

    from app.config import get_settings
    from app.core.clients import get_qdrant_client

    client = get_qdrant_client()
    collection = get_settings().qdrant_collection
    if not client.collection_exists(collection):
        return {"collection": "absent"}

    check = repair_qdrant_from_catalog(apply_writes=False)
    outstanding = check.get("points_needing_a_date_write", 0)
    carrying = check.get("points_carrying_a_legacy_key", 0)
    result: dict[str, Any] = {
        "points_checked": check.get("scanned"),
        "points_already_correct": check.get("already_correct"),
        "points_needing_a_date_write": outstanding,
        "points_carrying_a_legacy_key": carrying,
    }
    if outstanding:
        result["refused"] = (
            f"{outstanding} point(s) do not yet carry their catalogue value. "
            f"Run --repair-qdrant --apply first; dropping now would leave them "
            f"with no date at all."
        )
        return result
    if not carrying:
        result["nothing_to_do"] = "no point carries a legacy date key"
        return result
    if not apply_writes:
        result["would_drop"] = list(LEGACY_PAYLOAD_KEYS)
        return result

    # `wait=False`, and deliberately NOT wrapped in `_with_retry`.
    #
    # A mutation across 150k points takes about a minute server-side, which is
    # the client's default request timeout — so with `wait=True` the client gives
    # up at the exact moment the server succeeds, and a retry reissues the whole
    # thing. Six 200 OK responses at ~60.0s each is what that looked like in
    # practice, and the redundant work is what made the collection unresponsive
    # afterwards.
    #
    # So: hand it to the server, do not block on it, and confirm by reading back
    # — the same idiom `vector_store.ensure_payload_indexes` uses for an index
    # build that outlives the timeout. The operation is idempotent, so an
    # unconfirmed issue is safe to re-issue later if the read-back says it did
    # not land.
    try:
        client.delete_payload(
            collection_name=collection, keys=list(LEGACY_PAYLOAD_KEYS),
            points=qm.Filter(), wait=False,
        )
        result["issued"] = list(LEGACY_PAYLOAD_KEYS)
    except Exception as exc:  # noqa: BLE001 - the client wraps several
        # Still not a failure signal: the request may well have reached the
        # server. Say what is actually known rather than guessing either way.
        result["issued_unconfirmed"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    result["confirm_with"] = (
        "python -m scripts.backfill_bundle_dates --drop-legacy-payload  "
        "(expect points_carrying_a_legacy_key 0; allow a few minutes for the "
        "collection to finish optimising first)"
    )
    return result


def legacy_report() -> dict[str, Any]:
    """What the publication-date migration still has to do. Reads nothing else."""
    from app.catalog import schema

    out: dict[str, Any] = {}
    try:
        out["columns_present"] = schema.legacy_date_columns_present()
        out["rows_not_yet_carried"] = schema.unmigrated_legacy_rows()
    except Exception as exc:  # noqa: BLE001
        out["columns_present"] = f"unreadable ({type(exc).__name__})"
    return out


def record_decisions(
    resolutions: dict[str, EffectiveDate], urls: dict[str, str | None]
) -> int:
    """Rewrite the audit rows so the review queue matches the applied dates."""
    from app.catalog import date_decisions

    date_decisions.ensure_table()
    written = 0
    for document_id, resolved in resolutions.items():
        row = date_decisions.from_effective_date(
            document_id=document_id, url=urls.get(document_id),
            # `created` is only used to decide whether the field moved the date;
            # the value written is the resolution itself.
            created=(resolved.start_value if resolved.source == "created" else None),
            resolved=resolved,
        )
        if row is not None:
            date_decisions.record(row)
            written += 1
    return written


def record_attachment_decisions(moves: list[Move]) -> int:
    """Audit rows for the attachments this run re-decided.

    The page pass writes rows from an ``EffectiveDate``; an attachment's answer
    is a ``DateDecision``, because for a file sharing its page the question is
    not "what does the CMS say" but "what does this file say about itself". The
    decision is carried on the move rather than re-derived, so the row records
    what was actually applied — including the quoted statement, which is the
    whole point of the column.

    Only moves the resolver decided get a row. Plain inheritance is already
    explained by the parent page's own row, and writing a near-duplicate per
    file would bury the ones a reviewer needs to see.
    """
    from app.catalog import date_decisions

    decided = [m for m in moves if m.audit is not None]
    if not decided:
        return 0
    date_decisions.ensure_table()
    written = 0
    for move in decided:
        row = date_decisions.from_decision(
            move.audit,
            origin="attachment",
            bundle=move.bundle,
            node_uuid=move.parent_id,
            page_pdf_count=move.page_pdf_count,
            current_start_date=move.parent_start,
            url=move.url,
            filename=move.filename,
            candidate_end_date=move.new_end,
            range_issue=move.range_issue,
        )
        date_decisions.record(row)
        written += 1
    return written


def clear_answer_cache() -> str:
    """Drop the semantic cache so pre-correction answers stop being served.

    Necessary because the cache's partition key is
    ``retrieval settings + top_k + answer_format + corpus revision``, and the
    corpus revision is ``MAX(indexed_at) + COUNT(*)`` — neither of which a date
    correction moves. Without this, a question asked in the previous 24 hours
    replays its old answer, with the old date, and the lookup happens *before*
    retrieval so nothing about the fix is consulted.
    """
    from app.config import get_settings
    from app.core.clients import get_qdrant_client

    name = get_settings().semantic_cache_collection
    try:
        client = get_qdrant_client()
        if not client.collection_exists(name):
            return "already absent"
        count = client.count(collection_name=name, exact=True).count
        client.delete_collection(name)
        return f"dropped ({count} cached answers)"
    except Exception as exc:
        return f"FAILED ({type(exc).__name__}) — clear it manually or wait out the TTL"


#: Bundles whose mapping declares an end field, so "no end stated" is worth
#: reporting for them and meaningless for everything else.
_RANGE_BUNDLES = frozenset(
    b for b, fields in BUNDLE_DATE_FIELDS.items() if len(fields) > 1
)


#: How an attachment's `date_source` reads in the summary. Ordered as the
#: resolver weighs them, strongest evidence first, so the shape of the corpus is
#: legible at a glance rather than alphabetical.
_ATTACHMENT_SOURCES: tuple[tuple[str, str], ...] = (
    ("document_title", "title-derived"),
    ("document_text", "text-derived"),
    ("document_copyright", "copyright-derived"),
    ("parent_page", "parent-CMS-derived"),
    ("no_evidence", "intentionally undated"),
)


def summary(moves: list[Move]) -> dict[str, int]:
    """The headline counts, for the line printed immediately before a write."""
    files = [m for m in moves if m.source_type == "pdf_attachment"]
    return {
        "documents_changing": len(moves),
        "attachments_becoming_undated": sum(1 for m in files if m.start_cleared),
        "title_derived": sum(1 for m in files if m.source == "document_title"),
        "range_changes": sum(1 for m in moves if m.end_changed),
        "review_rows": sum(1 for m in moves if m.range_issue),
    }


def report(moves: list[Move], unrecoverable: list[str]) -> None:
    pages = [m for m in moves if m.source_type == "website"]
    files = [m for m in moves if m.source_type == "pdf_attachment"]
    print(f"documents whose date would change: {len(moves)} "
          f"({len(pages)} pages, {len(files)} attachments)\n")

    # The two source types are answered by different rules and have different
    # failure modes, so they are never added together.
    print(f"  {'website documents':34} {'documents':>9}")
    print(f"  {'  start date changed':34} "
          f"{sum(1 for m in pages if m.start_changed):9}")
    print(f"  {'  end date changed':34} "
          f"{sum(1 for m in pages if m.end_changed):9}")
    print(f"  {'  newly undated':34} "
          f"{sum(1 for m in pages if m.start_cleared):9}")

    print(f"\n  {'PDF attachments':34} {'documents':>9}")
    by_source: Counter = Counter(m.source for m in files)
    for key, label in _ATTACHMENT_SOURCES:
        print(f"  {'  ' + label:34} {by_source[key]:9}")

    print(f"  {'bundle':24} {'pages':>7} {'files':>7}")
    by_bundle: Counter = Counter(m.bundle for m in pages)
    file_bundle: Counter = Counter(m.bundle for m in files)
    for bundle, _ in (by_bundle + file_bundle).most_common():
        print(f"  {str(bundle):24} {by_bundle[bundle]:7} {file_bundle[bundle]:7}")

    print(f"\n  {'source field':30} {'documents':>9}")
    for field, n in Counter(m.field for m in moves).most_common():
        print(f"  {str(field):30} {n:9}")

    # The six populations a reviewer has to sign off separately. A start moving
    # and an end appearing are different risks and are never added together.
    print(f"\n  {'population':34} {'documents':>9}")
    for label, selected in (
        ("start date changed", [m for m in moves if m.start_changed]),
        ("end date changed", [m for m in moves if m.end_changed]),
        ("range newly added", [m for m in moves if m.range_added]),
        ("bundle has an end field, none stated",
         [m for m in moves if m.new_end is None and m.bundle in _RANGE_BUNDLES]),
        ("invalid range (start applied, end dropped)",
         [m for m in moves if m.range_issue]),
        ("falling back to the created stamp",
         [m for m in moves if m.source == "created"]),
    ):
        print(f"  {label:34} {len(selected):9}")

    issues = Counter(m.range_issue for m in moves if m.range_issue)
    if issues:
        print(f"\n  {'range issue':30} {'documents':>9}")
        for issue, n in issues.most_common():
            print(f"  {issue:30} {n:9}")
        print("  (each lands in the review queue; the start date is still applied)")

    print(f"\n  {'rule':30} {'documents':>9}")
    for rule, n in Counter(m.rule for m in moves).most_common():
        print(f"  {rule:30} {n:9}")

    cleared = [m for m in moves if m.start_cleared]
    if cleared:
        print(f"\n  {len(cleared)} document(s) would LOSE their date entirely — "
              f"a shelf page's creation stamp is not any one file's date. "
              f"The first few:")
        for move in cleared[:6]:
            print(f"    {move.old_start[:10]} -> (none)  [{move.bundle}] "
                  f"{str(move.filename or move.url)[-46:]}")

    dated = [m for m in moves if m.old_start and m.new_start]
    later = [m for m in dated if m.days < 0]
    print(f"\n  direction: {len(dated) - len(later)} earlier, {len(later)} later")
    buckets: Counter = Counter()
    for move in dated:
        gap = abs(move.days)
        buckets["1 day" if gap == 1 else
                "2-7 days" if gap <= 7 else
                "8-30 days" if gap <= 30 else
                "1-6 months" if gap <= 183 else
                "6-12 months" if gap <= 365 else
                "1-3 years" if gap <= 1095 else "over 3 years"] += 1
    print(f"\n  {'shift':16} {'documents':>9}")
    for label in ("1 day", "2-7 days", "8-30 days", "1-6 months", "6-12 months",
                  "1-3 years", "over 3 years"):
        if buckets[label]:
            print(f"  {label:16} {buckets[label]:9}")

    print("\n  largest corrections:")
    for move in sorted(dated, key=lambda m: -abs(m.days))[:10]:
        span = f" .. {move.new_end[:10]}" if move.new_end else ""
        print(f"    {move.old_start[:10]} -> {move.new_start[:10]}{span} "
              f"({-move.days:+d}d) [{move.bundle}] {str(move.url)[-46:]}")

    if unrecoverable:
        print(f"\n  {len(unrecoverable)} document(s) whose original creation stamp "
              f"is not recoverable from the catalogue and were skipped. Re-ingest "
              f"them to restore it; the first few:")
        for document_id in unrecoverable[:10]:
            print(f"    {document_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="Commit the changes. Omit for a dry run.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Apply only the first N moves (a scoped trial).")
    parser.add_argument("--expect", type=int, default=-1,
                        help="Refuse to apply unless this many documents move. "
                             "-1 (the default) disables the check; set it to what "
                             "a reviewed dry run showed.")
    parser.add_argument("--pages-only", action="store_true",
                        help="Skip the attachment pass.")
    parser.add_argument("--repair-qdrant", action="store_true",
                        help="Reconcile every Qdrant point's date keys to its "
                             "MySQL row and exit. The recovery path for a run "
                             "that wrote MySQL and died partway through Qdrant: "
                             "idempotent, driven by the catalogue rather than by "
                             "a move set, and a dry run unless --apply is given.")
    parser.add_argument("--start-after", default=None, metavar="DOCUMENT_ID",
                        help="Resume a --repair-qdrant --apply run after this "
                             "document id (the last one it printed). The run is "
                             "idempotent, so this only saves time.")
    parser.add_argument("--drop-legacy-graph", action="store_true",
                        help="Remove the superseded published_at property from "
                             "every Neo4j :Document node and exit. Refused "
                             "unless every node already carries "
                             "effective_start_date. Dry run unless --apply.")
    parser.add_argument("--drop-legacy-payload", action="store_true",
                        help="Delete the legacy date keys from every Qdrant "
                             "point and exit. One collection-wide call, refused "
                             "unless every point already carries its catalogue "
                             "value. Dry run unless --apply is given.")
    parser.add_argument("--drop-legacy", action="store_true",
                        help="After the dates are written, drop the legacy "
                             "published_* columns. Refuses unless every value "
                             "has been carried across first.")
    parser.add_argument("--keep-cache", action="store_true",
                        help="Do not drop the semantic cache. Old answers will "
                             "then be served for up to its TTL.")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.drop_legacy_graph:
        mode = "APPLY" if args.apply else "DRY RUN — nothing will be written"
        print(f"=== DROP LEGACY GRAPH PROPERTY ({mode}) ===\n")
        outcome = drop_legacy_graph_property(apply_writes=args.apply)
        for key, value in outcome.items():
            print(f"  {key:30} {value}")
        if "refused" in outcome:
            return 1
        if not args.apply and "would_remove" in outcome:
            print("\nNo changes written. Re-run with --apply to commit.")
        return 0

    if args.drop_legacy_payload:
        mode = "APPLY" if args.apply else "DRY RUN — nothing will be written"
        print(f"=== DROP LEGACY PAYLOAD KEYS ({mode}) ===\n")
        outcome = drop_legacy_payload_keys(apply_writes=args.apply)
        for key, value in outcome.items():
            print(f"  {key:30} {value}")
        if "refused" in outcome:
            return 1
        if not args.apply and "nothing_to_do" not in outcome:
            print("\nNo changes written. Re-run with --apply to commit.")
        return 0

    if args.repair_qdrant:
        if args.apply:
            # The write path deliberately does NOT scroll. Reading the
            # collection is the call that has failed twice, and the catalogue
            # already holds the answer for every document.
            print("=== QDRANT REPAIR (APPLY) ===\n")
            result = push_catalog_dates_to_qdrant(start_after=args.start_after)
            for key, value in result.items():
                print(f"  {key:24} {value}")
            print("\nRe-run without --apply to verify; expect "
                  "already_correct to equal the point count and "
                  "points_carrying_a_legacy_key to be 0.")
            return 0
        print("=== QDRANT REPAIR (DRY RUN — nothing will be written) ===\n")
        result = repair_qdrant_from_catalog(apply_writes=False)
        for key, value in result.items():
            print(f"  {key:26} {value}")
        print("\nNo changes written. Re-run with --apply to commit.")
        return 0

    pages, resolutions, unrecoverable = page_moves()
    files = [] if args.pages_only else attachment_moves(resolutions)
    moves = pages + files
    examined = invariants()["documents"]

    mode = "APPLY" if args.apply else "DRY RUN — nothing will be written"
    print(f"=== {mode} ===\n")
    report(moves, unrecoverable)

    legacy = legacy_report()
    present = legacy.get("columns_present")
    print("\n  legacy publication-date columns still in the database:")
    if not present:
        print("    none — this database is already on the effective-date model")
    elif isinstance(present, str):
        print(f"    {present}")
    else:
        for table, columns in present.items():
            print(f"    {table}: {', '.join(columns)}")
        if legacy.get("rows_not_yet_carried"):
            print(f"    rows not yet carried across: "
                  f"{legacy['rows_not_yet_carried']}")

    graph = push_catalog_dates_to_graph(apply_writes=False)
    print("\n  graph (Neo4j) against the catalogue:")
    for key, value in graph.items():
        print(f"    {key:30} {value}")

    if not args.apply:
        counts = summary(moves)
        print("\n  ---- summary ----")
        print(f"    Documents examined:            {examined}")
        print(f"    Documents changing:            {counts['documents_changing']}")
        print(f"    Attachments becoming undated:  "
              f"{counts['attachments_becoming_undated']}")
        print(f"    Title-derived:                 {counts['title_derived']}")
        print(f"    Range changes:                 {counts['range_changes']}")
        print(f"    Review/error rows:             {counts['review_rows']}")
        print("\nNo changes written. Re-run with --apply to commit.")
        return 0

    problems = preflight(moves, expect=args.expect)
    if problems:
        print("\nREFUSING TO APPLY:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\npre-flight OK: {len(moves)} documents, every field in the bundle "
          f"mapping, year values all 1 January")

    selected = moves[:args.limit] if args.limit else moves
    if args.limit:
        print(f"--limit {args.limit}: applying {len(selected)} of {len(moves)}")

    # Carry the legacy columns first: `apply` writes the new columns, and a
    # copy that ran afterwards would overwrite them with the old values.
    from app.catalog import schema

    copied = schema.copy_legacy_date_columns()
    if copied:
        print(f"\ncarried across from the legacy columns: {copied}")

    before = invariants()
    tally = apply(selected)
    urls = {m.document_id: m.url for m in pages}
    tally["decisions_recorded"] = record_decisions(
        {k: v for k, v in resolutions.items()
         if not args.limit or k in {m.document_id for m in selected}},
        urls,
    )
    tally["attachment_decisions_recorded"] = record_attachment_decisions(selected)
    after = invariants()

    print(f"\napplied: {tally}")
    print("\ninvariants (must be identical):")
    ok = True
    for key in before:
        if key == "documents_without_a_date":
            continue
        same = before[key] == after[key]
        ok = ok and same
        print(f"  {key:30} {before[key]!s:>20} -> {after[key]!s:<20}"
              + ("OK" if same else "*** CHANGED ***"))
    if not ok:
        print("\nAn invariant moved. Investigate before trusting this run.")
        return 1

    payloads = migrate_payload_keys()
    print(f"\npayload keys migrated: {payloads}")

    # The third store. Written from MySQL after MySQL is correct, so a failure
    # here leaves a stale projection rather than a catalogue disagreeing with
    # itself — and the projection can always be rebuilt.
    graph = push_catalog_dates_to_graph(apply_writes=True)
    print("\ngraph dates:")
    for key, value in graph.items():
        print(f"  {key:30} {value}")

    if args.drop_legacy:
        try:
            dropped = schema.drop_legacy_date_columns()
            print(f"\nlegacy columns dropped: {dropped or 'none were left'}")
        except RuntimeError as exc:
            print(f"\nlegacy columns NOT dropped: {exc}")
            return 1
    else:
        remaining = schema.legacy_date_columns_present()
        if remaining:
            print(f"\nlegacy columns left in place: {remaining}\n"
                  f"  Re-run with --drop-legacy once you have verified the "
                  f"carried values.")

    if args.keep_cache:
        print("\nsemantic cache: left alone (--keep-cache); old answers may "
              "still be served")
    else:
        print(f"\nsemantic cache: {clear_answer_cache()}")
    print("\nRe-run `python -m scripts.audit_dates --compare <baseline>` to "
          "measure the change.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
