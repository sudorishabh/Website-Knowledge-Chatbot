"""Catalog the Drupal pages whose PDFs we hold but whose page we never saved.

Dry run by default::

    python -m scripts.recover_empty_attachment_parents
    python -m scripts.recover_empty_attachment_parents --apply

The shape of the problem
------------------------
A ``completed_projects`` page is, by design, a title, a start and end date and a
link to the project's executive summary. It has no prose. The ingestion
pipeline's empty-extraction gate — which exists so that a blanked body can never
replace a good version — therefore refuses it, and no catalog row is written.
Its PDF is built on a different path, extracts fine, and is saved. The result is
a document with no parent: 85 of them, hanging off 83 pages that are perfectly
healthy in Drupal and simply absent here.

What this does *not* do
-----------------------
**It does not touch a single date.** The recovery report established that all 85
PDFs already carry exactly the date their live parent resolves to, because the
resolver had the real node in hand when they were ingested. This restores
structure — a parent row and an attachment link — and nothing else. The dry run
reports a date-mismatch count precisely so that claim stays checkable rather
than assumed.

**It does not fabricate content.** No synthetic chunk, no placeholder prose, no
vector. The parent is catalogued as ``content_state='metadata_only'``
(:class:`app.catalog.models.StateRecord`), which says the source describes the
document fully without a body. Nothing is written to Qdrant.

**It does not weaken the gate.** The narrow exception lives in
``app.ingestion.pipeline._metadata_only_is_warranted`` and requires a website
record, a bundle in ``METADATA_ONLY_BUNDLES``, at least one attachment, full
identity, and no prior indexable version. This script does not re-implement any
of that: it selects the population and hands it to the ordinary pipeline, so
the code that decides here is the code that decides on every crawl.

Scope
-----
``completed_projects`` only. 21 further pages across four other bundles hit the
same gate, but none of them carries an attachment, so admitting them would buy
nothing and widen an exception whose safety has been argued for exactly one
content type.
"""
from __future__ import annotations

import argparse
import collections
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The one bundle this recovers. Mirrors — and is asserted against —
#: ``app.ingestion.pipeline.METADATA_ONLY_BUNDLES``.
BUNDLE = "completed_projects"


@dataclass
class Candidate:
    """One live Drupal page that should have a catalog row and has none."""

    node: Any
    url: str
    title: str
    files: list[Any] = field(default_factory=list)
    #: The attachment documents we already hold for it, by document id.
    held: list[dict] = field(default_factory=list)
    #: Attachments the node lists that we do not hold as documents.
    missing_children: list[str] = field(default_factory=list)
    #: Held children already linked to some parent.
    already_linked: list[str] = field(default_factory=list)
    #: (document_id, stored_date, resolved_date) where the two disagree.
    date_mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    resolved: Any = None

    @property
    def recoverable(self) -> bool:
        """Enough to write a parent row the invariant will accept."""
        return bool(self.node.uuid and self.title.strip() and self.url
                    and self.files and self.held)


def _orphans() -> dict[str, list[dict]]:
    """Attachment documents with no ``documents_attachment`` row, by parent URL.

    ``documents.url`` on an attachment is its *parent page's* URL — set from
    ``node.url`` when the document was built — which is what makes the parent
    identifiable at all. ``source_key`` is the file's own URL.
    """
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT d.document_id, d.url, d.source_key, d.title, d.bundle, "
            f"       d.effective_start_date, d.effective_end_date, "
            f"       d.start_precision, d.date_source "
            f"FROM `{table}` d "
            f"WHERE d.source_type = 'pdf_attachment' "
            f"  AND NOT EXISTS (SELECT 1 FROM `{table}_attachment` a "
            f"                  WHERE a.file_uuid = d.document_id)"
        )
        rows = list(cur.fetchall())
    by_parent: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_parent[row["url"]].append(row)
    return by_parent


def _catalogued_pages() -> set[str]:
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT document_id FROM `{table}` "
                    f"WHERE source_type = 'website'")
        return {r["document_id"] for r in cur.fetchall()}


def candidates() -> list[Candidate]:
    """Live nodes that own an orphaned PDF and have no catalog row of their own.

    Crawls the bundle through the project's own extractor, so what this sees is
    what an ingestion run sees — including the node's file list, which is the
    only authority on how many PDFs a page has.
    """
    from app.ingestion.bundle_dates import resolve_effective_dates
    from app.ingestion.extractors.drupal_extractor import iter_records

    by_parent = _orphans()
    wanted = set(by_parent)
    catalogued = _catalogued_pages()

    found: list[Candidate] = []
    for node in iter_records([BUNDLE]):
        if node.url not in wanted:
            continue
        if node.uuid in catalogued:
            # The page is already in the catalogue; its PDF is unlinked for some
            # other reason and this script is not the fix.
            continue
        files = list(getattr(node, "files", None) or [])
        held = by_parent[node.url]
        held_by_uuid = {h["document_id"] for h in held}
        candidate = Candidate(
            node=node, url=node.url, title=(node.title or ""),
            files=files, held=held,
            missing_children=[f.uuid for f in files
                              if getattr(f, "uuid", None) not in held_by_uuid],
            resolved=resolve_effective_dates(node.bundle, node.created,
                                             node.metadata),
        )
        for row in held:
            stored = str(row["effective_start_date"])[:10] if row["effective_start_date"] else None
            want = (candidate.resolved.start_value or "")[:10] or None
            if stored != want:
                candidate.date_mismatches.append(
                    (row["document_id"], str(stored), str(want)))
        found.append(candidate)
    return found


def report(found: list[Candidate]) -> dict[str, int]:
    recoverable = [c for c in found if c.recoverable]
    unrecoverable = [c for c in found if not c.recoverable]
    links = sum(len(c.held) for c in recoverable)
    mismatches = sum(len(c.date_mismatches) for c in recoverable)
    missing = sum(len(c.missing_children) for c in found)

    print(f"  {'candidate parent pages':38} {len(found):6}")
    print(f"  {'parent documents that would be created':38} {len(recoverable):6}")
    print(f"  {'attachments that would be linked':38} {links:6}")
    print(f"  {'already-linked relationships':38} "
          f"{sum(len(c.already_linked) for c in found):6}")
    print(f"  {'unrecoverable records':38} {len(unrecoverable):6}")
    print(f"  {'attachments the node lists but we lack':38} {missing:6}")
    print(f"  {'date mismatches':38} {mismatches:6}")

    shape = collections.Counter(len(c.held) for c in recoverable)
    print(f"\n  {'PDFs per recoverable page':38} {'pages':>6}")
    for n, count in sorted(shape.items()):
        print(f"  {'  ' + str(n) + ' attachment(s)':38} {count:6}")

    if mismatches:
        print("\n  date mismatches (stored -> live resolution):")
        for c in recoverable:
            for document_id, stored, want in c.date_mismatches[:10]:
                print(f"    {document_id}  {stored} -> {want}  {c.url[-52:]}")

    if unrecoverable:
        print("\n  unrecoverable, and why:")
        for c in unrecoverable[:10]:
            why = ("no title" if not c.title.strip() else
                   "no url" if not c.url else
                   "node lists no files" if not c.files else
                   "no attachment documents held" if not c.held else "no uuid")
            print(f"    {why:32} {c.url[-52:]}")

    print("\n  sample recoveries:")
    for c in recoverable[:5]:
        print(f"    {c.title[:54]}")
        print(f"      {c.url[-70:]}")
        print(f"      uuid={c.node.uuid}  files={len(c.files)}  held={len(c.held)}")
        print(f"      dates: {c.resolved.start_value} .. {c.resolved.end_value} "
              f"({c.resolved.source}/{c.resolved.rule})")
    return {"pages": len(recoverable), "links": links,
            "mismatches": mismatches, "unrecoverable": len(unrecoverable)}


def apply(found: list[Candidate]) -> dict[str, int]:
    """Hand the verified population to the ordinary ingestion pipeline.

    Deliberately not a direct ``state.upsert``. The pipeline is what decides
    whether an empty page may be catalogued, what its dates are, which facets it
    carries and which attachment links it claims; re-implementing any of that
    here would be the second ingestion path this codebase keeps refusing to grow.

    Each node is presented as a ``NEW`` change record — which is what it is, the
    catalog has never held it — and ``_run`` drives them through the same
    ``_handle`` a crawl uses. The gate's metadata-only branch is what writes the
    row, so if that branch ever stops accepting these pages, this stops
    recovering them, which is the correct coupling.
    """
    from app.ingestion import pipeline
    from app.ingestion.change_detection.base import ChangeRecord, ChangeStatus
    from app.ingestion.change_detection.drupal import _to_unix

    recoverable = [c for c in found if c.recoverable]
    records = [
        ChangeRecord(
            status=ChangeStatus.NEW,
            document_id=c.node.uuid,
            source_type="website",
            # Exactly what `change_detection.drupal` builds for a node it has
            # never seen: the source key is the record's own `source`, the
            # fingerprint is its `changed` stamp, and the mark is that stamp as
            # unix time. Matching it is what lets the next ordinary crawl
            # recognise these pages as UNCHANGED instead of re-ingesting them.
            source_key=c.node.source,
            fingerprint=c.node.changed or "",
            bundle=c.node.bundle,
            changed_mark=_to_unix(c.node.changed),
            prior=None,
            payload=c.node,
            entity_type="node",
        )
        for c in recoverable
    ]
    tally = pipeline._run(iter(records), pipeline._build_drupal_doc)
    return dict(tally)


def verify() -> dict[str, Any]:
    """What the stores hold once the run is over."""
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    out: dict[str, Any] = {}
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) n FROM `{table}` "
                    f"WHERE content_state = 'metadata_only'")
        out["metadata_only_documents"] = cur.fetchall()[0]["n"]
        cur.execute(
            f"SELECT COUNT(*) n FROM `{table}_attachment` a "
            f"JOIN `{table}` p ON p.document_id = a.document_id "
            f"WHERE p.content_state = 'metadata_only'")
        out["links_from_metadata_only_parents"] = cur.fetchall()[0]["n"]
        cur.execute(
            f"SELECT COUNT(*) n FROM `{table}` d "
            f"WHERE d.source_type='pdf_attachment' "
            f"  AND NOT EXISTS (SELECT 1 FROM `{table}_attachment` a "
            f"                  WHERE a.file_uuid = d.document_id)")
        out["attachments_still_parentless"] = cur.fetchall()[0]["n"]
        cur.execute(f"SELECT COUNT(*) n FROM `{table}` "
                    f"WHERE content_state='metadata_only' AND indexed_at IS NOT NULL")
        out["metadata_only_claiming_points"] = cur.fetchall()[0]["n"]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the parent documents and their links")
    parser.add_argument("--expect", type=int, default=-1,
                        help="refuse to apply unless exactly this many pages "
                             "would be recovered")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    # The exception this relies on has to actually be in force, or a dry run
    # would promise a recovery the pipeline would then refuse.
    from app.ingestion.pipeline import METADATA_ONLY_BUNDLES

    if BUNDLE not in METADATA_ONLY_BUNDLES:
        print(f"REFUSING: {BUNDLE!r} is not in "
              f"app.ingestion.pipeline.METADATA_ONLY_BUNDLES, so the pipeline "
              f"would reject every page this script selects.")
        return 1

    mode = "APPLY" if args.apply else "DRY RUN — nothing will be written"
    print(f"=== RECOVER EMPTY ATTACHMENT PARENTS ({mode}) ===\n")
    found = candidates()
    counts = report(found)

    if not args.apply:
        print("\nNo changes written. Re-run with --apply to commit.")
        return 0

    if args.expect >= 0 and counts["pages"] != args.expect:
        print(f"\nREFUSING TO APPLY: {counts['pages']} pages would be recovered, "
              f"expected {args.expect}.")
        return 1
    if counts["mismatches"]:
        print(f"\nREFUSING TO APPLY: {counts['mismatches']} attachment date(s) "
              f"disagree with their live parent. This script restores structure "
              f"and must not be the thing that moves a date; investigate first.")
        return 1

    # The `content_state` column is added by this call, and `verify` reads it.
    # `_run` would do it too, but only once it is already writing — too late for
    # a before-and-after snapshot to be taken around the run. Idempotent DDL.
    from app.catalog import state

    state.ensure_table()

    before = verify()
    tally = apply(found)
    after = verify()
    print(f"\npipeline outcomes: {tally}")
    print("\nstores:")
    for key in after:
        print(f"  {key:34} {before[key]!s:>8} -> {after[key]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
