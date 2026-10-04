"""Ingest-state write model: the document catalog's source of truth.

Schema/migrations live in :mod:`app.catalog.schema`; the retrieval-facing
analytical reads (count/list/distribution) live in :mod:`app.catalog.queries`.
This module owns the per-document write path (upsert / delete / facet & link
replacement) and the handful of point reads ingestion itself needs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from app.catalog import author_names, schema, theme_taxonomy
from app.catalog.db import now as _now
from app.catalog.db import state_table as _table
from app.catalog.models import AttachmentLink, StateRecord
from app.core.clients import mysql_connection

logger = logging.getLogger(__name__)

__all__ = [
    "AttachmentLink",
    "StateRecord",
    "ensure_table",
    "load",
    "get",
    "upsert",
    "delete",
    "clear_change_markers",
    "attachment_ids_for",
    "orphaned_attachments",
    "backfill_facets",
    "rename_theme_facet",
    "reclassify_theme_rows",
]


def ensure_table() -> None:
    schema.ensure_state_table()


def _to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


# Facet value columns are VARCHAR(255); anything longer is stored truncated.
_FACET_WIDTH = 255


def _stored_values(values: Iterable[str], width: int = _FACET_WIDTH) -> list[str]:
    """The distinct values as they will actually be stored.

    Truncate first, de-duplicate second. The other order de-duplicates strings
    the database will never hold: two tags differing only past character 255 are
    distinct as read and identical as written, so the old order emitted two rows
    the table then had no constraint to reject. 144 duplicate (document, tag)
    pairs came from exactly that.

    Order is preserved — it is the order the source listed them in, and a facet
    list that reshuffles itself between ingests is noise in every diff.
    """
    return list(dict.fromkeys(v[:width] for v in values if v))


# Every facet table keys on (document_id, value), and MySQL compares that key
# under the column collation — utf8mb4_0900_ai_ci here, which folds case and
# accents. `_stored_values` de-duplicates in Python, which folds neither, so a
# source that tags one document both "Climate Variability" and "climate
# variability" offers two values the index accepts as one. Without this the
# second row raises 1062 and takes the whole document's transaction down with
# it — the document does not persist at all over a repeated tag.
#
# Letting the database decide which values are the same string is the point:
# Python cannot restate that rule without reimplementing Unicode collation, and
# would drift from it the moment the column is altered. First spelling wins,
# and `_stored_values` preserves source order, so which one that is stays
# stable across ingests.
#
# `document_id = document_id` rather than INSERT IGNORE: this absorbs a
# duplicate key and nothing else. IGNORE would equally downgrade a foreign-key
# violation or an over-long value to a warning, hiding real corruption.
_KEEP_FIRST = " ON DUPLICATE KEY UPDATE document_id = document_id"


def _replace_facet(
    cur: Any, table: str, facet: str, document_id: str, values: Iterable[str]
) -> None:
    cur.execute(f"DELETE FROM `{table}_{facet}` WHERE document_id = %s", (document_id,))
    rows = [(document_id, value) for value in _stored_values(values)]
    if rows:
        cur.executemany(
            f"INSERT INTO `{table}_{facet}` (document_id, {facet}) "
            f"VALUES (%s, %s){_KEEP_FIRST}",
            rows,
        )


def _replace_authors(
    cur: Any, table: str, document_id: str, values: Iterable[str]
) -> None:
    """Rewrite a document's author rows, each with its normalized form beside it.

    `author` is exactly what Drupal sent and is never rewritten — it is what an
    answer displays and what makes a count traceable to the source.
    `author_norm` is the formatting-normalized form (see
    :mod:`app.catalog.author_names`), which is what a *distinct name* count
    should group on: "Dr Jayanta Mitra" and "Dr. Jayanta Mitra" are one name
    written two ways.

    It is emphatically not a person id. Two people called "Arun Kumar" share a
    normalized form here, exactly as they already share a raw one.
    """
    cur.execute(f"DELETE FROM `{table}_author` WHERE document_id = %s", (document_id,))
    # De-duplicated on the stored (truncated) spelling for the same reason tags
    # are, and normalized from that same stored value so the pair in one row
    # always describes one string.
    rows = [
        (document_id, value, author_names.normalize(value)[:_FACET_WIDTH] or None)
        for value in _stored_values(values)
    ]
    if rows:
        cur.executemany(
            f"INSERT INTO `{table}_author` (document_id, author, author_norm) "
            f"VALUES (%s, %s, %s){_KEEP_FIRST}",
            rows,
        )


#: `theme_path` is VARCHAR(1024); a chain longer than that is stored truncated.
#: Wide enough that no real hierarchy reaches it — four 255-char segments — so
#: this is a storage guard, not a policy.
_PATH_WIDTH = 1024


def _replace_themes(
    cur: Any, table: str, document_id: str, names: Iterable[str]
) -> None:
    """Rewrite a document's theme rows: each theme with its type, its immediate
    parent, the top-level bucket ("main" / "other") it traces back to, and its
    full ancestor path and depth.

    Only the themes the document itself carries are written — a parent is
    recorded as a reference, never materialized as an extra row, so a document
    is never credited with a theme it wasn't tagged with. A document with no
    valid theme (all values empty, or only grouping-bucket names) gets no row at
    all rather than a placeholder.

    DELETE-then-INSERT rather than an upsert, which is what makes re-ingestion
    idempotent: a theme the document no longer carries has to *disappear*, and
    an upsert keyed on (document_id, theme) can only ever add or update. The
    DELETE runs unconditionally, so a document that lost its last theme is left
    with no rows rather than its previous ones.

    See :mod:`app.catalog.theme_taxonomy` for the classification."""
    cur.execute(f"DELETE FROM `{table}_theme` WHERE document_id = %s", (document_id,))
    rows = [
        (
            document_id,
            a.name[:_FACET_WIDTH],
            a.theme_type,
            a.parent[:_FACET_WIDTH] if a.parent else None,
            a.group,
            a.path[:_PATH_WIDTH],
            a.depth,
        )
        for a in theme_taxonomy.classify(names)
    ]
    if rows:
        cur.executemany(
            f"INSERT INTO `{table}_theme` "
            "(document_id, theme, theme_type, parent, theme_group, theme_path, depth) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s){_KEEP_FIRST}",
            rows,
        )


def _replace_attachment_links(
    cur: Any, table: str, document_id: str, links: Iterable[AttachmentLink]
) -> None:
    cur.execute(f"DELETE FROM `{table}_attachment` WHERE document_id = %s", (document_id,))
    # First link wins per file: an explicit attachment ref carries url/filename
    # and outranks a later in-body sighting of the same PDF.
    seen: dict[str, AttachmentLink] = {}
    for link in links:
        if link.file_uuid and link.file_uuid not in seen:
            seen[link.file_uuid] = link
    rows = [
        (l.file_uuid, document_id, l.origin[:16], l.url, l.filename)
        for l in seen.values()
    ]
    if rows:
        cur.executemany(
            f"INSERT INTO `{table}_attachment` "
            "(file_uuid, document_id, origin, url, filename) "
            "VALUES (%s, %s, %s, %s, %s)",
            rows,
        )


def _row_to_record(row: dict) -> StateRecord:
    indexed = row.get("indexed_at")
    start = row.get("effective_start_date")
    until = row.get("effective_end_date")
    return StateRecord(
        document_id=row["document_id"],
        source_type=row["source_type"],
        source_key=row["source_key"],
        fingerprint=row["fingerprint"],
        content_hash=row.get("content_hash") or "",
        doc_version=int(row.get("doc_version") or 1),
        pipeline_version=row.get("pipeline_version") or None,
        bundle=row.get("bundle"),
        entity_type=row.get("entity_type"),
        changed_mark=row.get("changed_mark"),
        title=row.get("title"),
        url=row.get("url"),
        indexed_at=indexed.isoformat() if isinstance(indexed, datetime) else indexed,
        effective_start_date=start.isoformat() if isinstance(start, datetime) else start,
        date_source=row.get("date_source"),
        start_precision=row.get("start_precision"),
        effective_end_date=until.isoformat() if isinstance(until, datetime) else until,
        end_precision=row.get("end_precision"),
        content_state=row.get("content_state") or None,
    )


def load(source_type: str) -> dict[str, StateRecord]:
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM `{table}` WHERE source_type = %s", (source_type,)
        )
        return {row["document_id"]: _row_to_record(row) for row in cur.fetchall()}


def get(document_id: str) -> StateRecord | None:
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM `{table}` WHERE document_id = %s", (document_id,)
        )
        row = cur.fetchone()
    return _row_to_record(row) if row else None


def raw_meta_for(document_id: str) -> dict | None:
    """One document's CMS metadata, decoded.

    A reader of its own rather than a field on :class:`StateRecord`, and
    deliberately so. ``_row_to_record`` does not carry ``raw_meta``, because
    :func:`load` builds a record for *every* document of a source type and the
    metadata blob is by far the largest column — inflating that map with it
    would cost the change-detection pass a great deal of memory for something
    almost nothing reads.

    So callers that genuinely need the metadata for one document ask for it,
    and pay only for that document. The knowledge layer is the caller this
    exists for: CMS claims are derived from these fields, so reading them back
    is the whole job when a document is processed outside ingestion.
    """
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT raw_meta FROM `{table}` WHERE document_id = %s",
            (document_id,),
        )
        row = cur.fetchone()
    if not row or row.get("raw_meta") is None:
        return None
    raw = row["raw_meta"]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Could not decode raw_meta for %s.", document_id)
            return None
    return raw if isinstance(raw, dict) else None


def website_titles() -> list[tuple[str, str, str | None]]:
    """``(document_id, title, bundle)`` for every website node that has a title.

    Serves the title-anchored retrieval leg (``app.retrieval.search.title_leg``), which
    needs to find the page a question names when that page's *text* is a list of
    link labels no embedding matches.

    The whole set rather than a filtered slice, deliberately. A ``LIKE`` per term
    looks cheaper but is not usable here: the organisation's own name appears in
    thousands of titles, so any row budget is spent on it before the page actually
    being asked for is reached — the first version of this returned two rows for
    "annual reports" and neither was the Annual Reports page. Titles are short and
    there are ~12k website nodes, so one scan is a few hundred KB and the caller
    caches it; matching and scoring then happen in Python where word boundaries
    can be respected (``LIKE '%vision%'`` matches "Visionary").

    Website nodes only: an attachment inherits its parent's title, so including
    them would return one page many times over. Read-only.
    """
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT document_id, title, bundle FROM `{table}` "
            f"WHERE source_type = 'website' AND title IS NOT NULL AND title <> ''"
        )
        rows = cur.fetchall() or []
    return [
        (str(r["document_id"]), str(r.get("title") or ""), r.get("bundle"))
        for r in rows
    ]


def authors_for(document_id: str) -> list[str]:
    """A document's author names, as the source wrote them.

    The companion to :func:`raw_meta_for`, and it exists for the same reason:
    ``_row_to_record`` carries no facets, so a caller that needs one document's
    authors has nowhere to read them from.

    The knowledge layer is that caller. Author names used to live in
    ``raw_meta.field_authors`` and now live here — ``documents_author`` holds
    1,860 rows while that metadata key holds none — so anything still reading
    the metadata for them silently gets nothing.

    Returns the raw ``author`` value, not ``author_norm``: the knowledge layer
    applies its own :mod:`app.knowledge.normalize` fold, which is a different
    normalization from the facet's display-oriented one, and folding twice
    through two schemes would not round-trip.
    """
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT author FROM `{table}_author` WHERE document_id = %s",
            (document_id,),
        )
        return [r["author"] for r in cur.fetchall() if r.get("author")]


def attachment_ids_for(document_id: str) -> list[str]:
    """The attachments this document links to.

    Read *before* the document is deleted: its link rows cascade away with it,
    which is exactly the information :func:`orphaned_attachments` then needs a
    candidate list for.
    """
    if not document_id:
        return []
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT file_uuid FROM `{_table()}_attachment` WHERE document_id = %s",
            (document_id,),
        )
        return [row["file_uuid"] for row in cur.fetchall()]


def orphaned_attachments(
    file_uuids: Iterable[str], *, ignoring_parents: Iterable[str] = ()
) -> list[str]:
    """Which of these attachments no longer hang off any document at all.

    An attachment is shared: 84 of them are reachable from more than one page,
    so losing one parent is not losing the attachment. The link table is the
    whole truth about that, so the question is asked of it directly — after the
    parent's rows have gone, an attachment with no rows left has no parent left.

    ``ignoring_parents`` answers the same question a step earlier: treat these
    parents as though they were already gone. That is what lets a dry run report
    the attachments a deletion *would* orphan without deleting anything to find
    out, using this query rather than a second copy of its reasoning.

    Restricted to ids that are attachment documents in their own right, so a
    file that was linked but never successfully ingested costs no delete call.
    """
    ids = [f for f in dict.fromkeys(file_uuids) if f]
    if not ids:
        return []
    ignored = [p for p in dict.fromkeys(ignoring_parents) if p]
    table = _table()
    params = list(ids)
    unless_gone = ""
    if ignored:
        unless_gone = f" AND a.document_id NOT IN ({', '.join(['%s'] * len(ignored))})"
        params.extend(ignored)
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT d.document_id FROM `{table}` d "
            f"WHERE d.document_id IN ({', '.join(['%s'] * len(ids))}) "
            f"  AND d.source_type = 'pdf_attachment' "
            f"  AND NOT EXISTS (SELECT 1 FROM `{table}_attachment` a "
            f"                  WHERE a.file_uuid = d.document_id{unless_gone})",
            tuple(params),
        )
        return [row["document_id"] for row in cur.fetchall()]


def upsert(record: StateRecord, *, mark_indexed: bool = True) -> None:
    table = _table()
    now = _now()
    indexed_at = now if mark_indexed else None
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO `{table}`
                (document_id, source_type, source_key, bundle, entity_type,
                 fingerprint, content_hash, doc_version, pipeline_version,
                 changed_mark, content_state, effective_start_date,
                 date_source, start_precision,
                 effective_end_date, end_precision, title, url,
                 raw_meta, indexed_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                source_type  = VALUES(source_type),
                source_key   = VALUES(source_key),
                bundle       = VALUES(bundle),
                entity_type  = COALESCE(VALUES(entity_type), entity_type),
                fingerprint  = VALUES(fingerprint),
                content_hash = VALUES(content_hash),
                doc_version  = VALUES(doc_version),
                -- COALESCE, like entity_type and indexed_at: only a write that
                -- actually re-chunked the document may claim its pipeline
                -- version. A fingerprint refresh (`indexed=False`) passes NULL
                -- and keeps the stored one, so a document that has not been
                -- rebuilt still reads as stale and is rebuilt later.
                pipeline_version = COALESCE(VALUES(pipeline_version), pipeline_version),
                changed_mark = VALUES(changed_mark),
                -- VALUES, not COALESCE: a page that gains a body stops being
                -- metadata-only, and the row has to be able to say so. The
                -- write that indexes it passes NULL and clears the marker.
                content_state = VALUES(content_state),
                effective_start_date = VALUES(effective_start_date),
                -- VALUES, not COALESCE. These describe
                -- `effective_start_date`, which is itself overwritten
                -- outright; a provenance that outlived the value it describes
                -- would be worse than none, because it would read as evidence
                -- for a date it was never about.
                date_source    = VALUES(date_source),
                start_precision = VALUES(start_precision),
                -- VALUES, like effective_start_date itself: the end of the range is
                -- re-derived from the source on every write, so a record whose
                -- CMS end date was cleared must lose it here too. COALESCE
                -- would make a deleted end date immortal.
                effective_end_date           = VALUES(effective_end_date),
                end_precision = VALUES(end_precision),
                title        = VALUES(title),
                url          = VALUES(url),
                raw_meta     = COALESCE(VALUES(raw_meta), raw_meta),
                indexed_at   = COALESCE(VALUES(indexed_at), indexed_at),
                updated_at   = VALUES(updated_at)
            """,
            (
                record.document_id,
                record.source_type,
                record.source_key,
                record.bundle,
                record.entity_type,
                record.fingerprint,
                record.content_hash,
                record.doc_version,
                record.pipeline_version,
                record.changed_mark,
                record.content_state,
                _to_datetime(record.effective_start_date),
                record.date_source,
                record.start_precision,
                _to_datetime(record.effective_end_date),
                record.end_precision,
                record.title,
                record.url,
                json.dumps(record.raw_meta, ensure_ascii=False, default=str)
                if record.raw_meta is not None
                else None,
                indexed_at,
                now,
            ),
        )
        _replace_authors(cur, table, record.document_id, record.authors)
        _replace_facet(cur, table, "tag", record.document_id, record.tags)
        _replace_themes(cur, table, record.document_id, record.categories)
        _replace_attachment_links(cur, table, record.document_id, record.attachments)
        conn.commit()


def delete(document_ids: Iterable[str]) -> int:
    ids = [d for d in document_ids if d]
    if not ids:
        return 0
    table = _table()
    placeholders = ", ".join(["%s"] * len(ids))
    with mysql_connection() as conn, conn.cursor() as cur:
        removed = cur.execute(
            f"DELETE FROM `{table}` WHERE document_id IN ({placeholders})", tuple(ids)
        )
        conn.commit()
    return int(removed or 0)


def clear_change_markers(document_id: str) -> bool:
    """Make the next crawl treat this document as changed — without deleting it.

    Two independent signals gate re-indexing, and anything asking for a document
    to be rebuilt has to clear both:

    * ``fingerprint`` is the *crawl's* change test (:func:`compute_status`).
      While it still matches the source's ``changed`` value the record resolves
      UNCHANGED and the document is never even built, so the content hash below
      would never be consulted.
    * ``content_hash`` is the *pipeline's* test, which decides between refreshing
      the fingerprint and re-indexing for real.

    ``changed_mark`` is deliberately left in place. It is the document's position
    in the crawl, and the retry marker written alongside this call needs it to
    pull the window back far enough to reach the document at all — clearing it
    would strand exactly the document being repaired.

    Nothing else is touched: the row, its facets, its attachments links and its
    indexed vectors all survive, so the document stays searchable and correctly
    catalogued right up until its replacement is indexed.

    Returns False when no such document is catalogued.
    """
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM `{table}` WHERE document_id = %s", (document_id,))
        if cur.fetchone() is None:
            return False
        cur.execute(
            f"UPDATE `{table}` SET fingerprint = '', content_hash = '', "
            f"updated_at = %s WHERE document_id = %s",
            (_now(), document_id),
        )
        conn.commit()
    return True


def backfill_facets(
    document_id: str,
    effective_start_date: str | None,
    authors: Iterable[str],
    categories: Iterable[str],
    *,
    title: str | None = None,
    url: str | None = None,
) -> bool:
    """Set the date/author/theme facets (and optionally title/url) for an
    already-cataloged document (e.g. one indexed before these columns existed).
    title/url only overwrite when a value is supplied (COALESCE), so rows already
    populated at ingest are left intact. Returns False when no catalog row exists
    for the id, leaving child rows untouched (FK safety).

    ``effective_start_date`` is lifted from a chunk payload, which carries the value and
    not where it came from — so the provenance columns are **cleared** rather
    than left standing. A stale ``date_source`` would read as evidence
    for a date it was never about, which is worse than recording nothing."""
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM `{table}` WHERE document_id = %s", (document_id,))
        if cur.fetchone() is None:
            return False
        cur.execute(
            f"UPDATE `{table}` SET effective_start_date = %s, "
            f"date_source = NULL, start_precision = NULL, "
            f"title = COALESCE(%s, title), url = COALESCE(%s, url) "
            f"WHERE document_id = %s",
            (_to_datetime(effective_start_date), title, url, document_id),
        )
        _replace_authors(cur, table, document_id, authors)
        _replace_themes(cur, table, document_id, categories)
        conn.commit()
    return True


def replace_authors(document_id: str, authors: Iterable[str]) -> bool:
    """Rewrite one cataloged document's author rows, nothing else.

    For a correction to how authors were *read* (see
    ``scripts.backfill_author_bylines``): the document's content, dates and
    other facets are untouched. Returns False when no catalog row exists for
    the id, leaving the child table alone (FK safety), as `backfill_facets` does.
    """
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM `{table}` WHERE document_id = %s", (document_id,))
        if cur.fetchone() is None:
            return False
        _replace_authors(cur, table, document_id, authors)
        conn.commit()
    return True


def reclassify_theme_rows(*, dry_run: bool = False) -> dict[str, int]:
    """Re-apply the theme map to theme rows already in the table, in place.

    For deployments whose rows predate the hierarchy columns
    (:func:`app.catalog.schema.migrate_theme_hierarchy` gives them the column
    default — an unparented sub-theme, with a NULL ``theme_path``). Keyed on the
    distinct theme *names*, not on documents, since the classification depends
    only on the name: a whole corpus is a few dozen statements. Names that are
    not themes at all (grouping buckets, blanks) have their rows deleted.

    This is the backfill for ``theme_path``/``depth``, and the way a deployment
    picks up a deepened theme map without re-ingesting: the map is re-read, so
    a theme moved to a new position in the hierarchy gets its new path here.

    Requires a loadable theme map (:func:`theme_taxonomy.require_taxonomy`).
    Without it every name would classify as unmapped and this would rewrite the
    whole hierarchy to NULL — the exact damage it exists to repair.

    Idempotent — a second run reports 0 updated. Under ``dry_run`` nothing is
    written and the counts are rows that *match* each name, not rows that would
    actually change. Returns ``{'names', 'updated', 'deleted'}``.
    """
    theme_taxonomy.require_taxonomy()
    table = _table()
    tally = {"names": 0, "updated": 0, "deleted": 0}
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT DISTINCT theme FROM `{table}_theme`")
        for name in [row["theme"] for row in cur.fetchall()]:
            tally["names"] += 1
            assignment = next(iter(theme_taxonomy.classify([name])), None)
            bucket = "deleted" if assignment is None else "updated"
            if dry_run:
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM `{table}_theme` WHERE theme = %s",
                    (name,),
                )
                tally[bucket] += int(cur.fetchone()["n"])
                continue
            if assignment is None:
                logger.info("Dropping theme rows for non-theme value %r", name)
                affected = cur.execute(
                    f"DELETE FROM `{table}_theme` WHERE theme = %s", (name,)
                )
            else:
                affected = cur.execute(
                    f"UPDATE `{table}_theme` SET theme_type = %s, parent = %s, "
                    "theme_group = %s, theme_path = %s, depth = %s WHERE theme = %s",
                    (
                        assignment.theme_type,
                        assignment.parent,
                        assignment.group,
                        assignment.path[:_PATH_WIDTH],
                        assignment.depth,
                        name,
                    ),
                )
            tally[bucket] += int(affected or 0)
        if not dry_run:
            conn.commit()
    return tally


def rename_theme_facet(document_id: str, old: str, new: str) -> list[str]:
    """Replace ``old`` with ``new`` in a document's theme facet, collapsing
    duplicates; returns the resulting theme list (payload-refresh input).

    The rewrite re-classifies, so a theme renamed into (or out of) the theme map
    picks up its correct primary/sub position rather than keeping the old one."""
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT theme FROM `{table}_theme` WHERE document_id = %s",
            (document_id,),
        )
        themes = [row["theme"] for row in cur.fetchall()]
        updated = list(dict.fromkeys(new if c == old else c for c in themes))
        if updated != themes:
            _replace_themes(cur, table, document_id, updated)
            conn.commit()
    return updated
