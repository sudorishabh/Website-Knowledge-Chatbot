"""Catalog schema: table DDL and idempotent migrations.

Kept apart from the read/write model code (state.py / log.py): this module only
ever CREATEs or ALTERs tables, never touches rows. Called once per process via
each ``ensure_*`` function (state.py / log.py wrap these under their historical
names so callers are unaffected).

Table names were simplified from their legacy ``ingest_state*`` forms to
``documents*``; a deployment with existing data must run
``scripts.rename_catalog_tables`` once before/at deploy so the old tables become
the new ones instead of being recreated empty.

The taxonomy-term tables (``terms``, ``term_aliases``, ``documents_term``) were
retired and dropped: the catalog is keyed by name, so themes live in
``documents_theme`` and tags in ``documents_tag``. Taxonomy no longer reaches
storage at all — terms are not crawled as documents, and the term uuids that
once rode every chunk payload were removed once nothing filtered on them. See
docs/retire-term-tables-plan.md for the original retirement.

The theme facet was likewise renamed from ``category``. That one is handled here
rather than by the script, in ``migrate_renamed_facets``, because it also has to
rename the child table's *value column* and must work for whatever
``ingest_state_table`` prefix the process is configured with.

The theme facet also grew from a flat (document, value) list into a primary-tag /
sub-theme hierarchy; ``migrate_theme_hierarchy`` adds those columns to a table
that predates them.
"""
from __future__ import annotations

import logging
from typing import Any

from app.catalog.db import log_table, state_table
from app.core.clients import mysql_connection

logger = logging.getLogger(__name__)

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    document_id  VARCHAR(255)  NOT NULL,
    source_type  VARCHAR(32)   NOT NULL,
    source_key   VARCHAR(1024) NOT NULL,
    bundle       VARCHAR(128)  NULL,
    entity_type  VARCHAR(32)   NULL,
    fingerprint  VARCHAR(128)  NOT NULL,
    content_hash VARCHAR(64)   NOT NULL DEFAULT '',
    doc_version  INT           NOT NULL DEFAULT 1,
    -- Which pipeline produced the indexed content (app.ingestion.version).
    -- Nullable: a row written before this column existed has no answer, and
    -- "unknown" must read as "not current" so it gets rebuilt rather than
    -- assumed fresh. Indexed because the corpus reprocessor's whole query is
    -- "which documents are not on the current version".
    pipeline_version VARCHAR(32) NULL,
    changed_mark BIGINT        NULL,
    -- NULL for a document with indexed content; 'metadata_only' for one whose
    -- source states identity and attachments but no body. See
    -- app.catalog.models.StateRecord.content_state.
    content_state VARCHAR(16) NULL,
    effective_start_date DATETIME      NULL,
    title        VARCHAR(1024) NULL,
    url          VARCHAR(1024) NULL,
    indexed_at   DATETIME      NULL,
    updated_at   DATETIME      NOT NULL,
    PRIMARY KEY (document_id),
    KEY idx_source_type (source_type),
    KEY idx_bundle (source_type, bundle),
    KEY idx_pipeline_version (pipeline_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Multi-valued facets stored one row per (document, value) so they count exactly
# via COUNT(DISTINCT document_id). Rows cascade-delete with their parent. The
# facet name doubles as the child table's suffix and its value column.
# Themes are such a facet too, but they carry hierarchy, so they have their own
# DDL (_STATE_THEME_DDL) instead of the generic one.
STATE_FACETS: tuple[str, ...] = ("author", "tag")

# Facets renamed after deployments already had data. Because the facet name is
# both the table suffix and the column name, both have to be carried forward --
# and scripts.rename_catalog_tables only ever renamed tables, so a deployment
# can sit on `documents_theme` while its value column is still `category`.
# {current facet: previous facet}
_RENAMED_FACETS: dict[str, str] = {"theme": "category"}

_STATE_CHILD_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_{facet}` (
    document_id VARCHAR(255) NOT NULL,
    {facet}     VARCHAR(255) NOT NULL,
    -- One row per (document, value). The facet is a set, and nothing but this
    -- said so: with only the two lookup keys it replaces, a writer that emitted
    -- the same pair twice was simply believed, and every COUNT over the table
    -- was wrong by the duplication. `documents_theme` has had the equivalent as
    -- its primary key from the start.
    --
    -- It also subsumes the old `idx_doc`: document_id is its leftmost column, so
    -- per-document lookups and the foreign key below are served by this alone.
    UNIQUE KEY uq_{facet} (document_id, {facet}),
    KEY idx_val ({facet}),
    CONSTRAINT `fk_{table}_{facet}` FOREIGN KEY (document_id)
        REFERENCES `{table}` (document_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# The theme facet, with the taxonomy shape a flat facet has no room for: a
# document's top-level themes are stored as primary tags and every deeper theme
# as a sub-theme naming its immediate `parent`. `parent` is NULL for a primary
# tag and for a theme the map does not know. `theme_group` is which top-level
# theme_structure.json bucket ("main" / "other", from _group_code) the theme
# traces back to -- tracked separately from theme_type/parent because two
# primary tags (e.g. "Energy" and "Green Shipping") can have the same
# theme_type/parent (primary, NULL) while coming from different buckets; a
# sub-theme inherits its primary tag's group.
#
# `theme_path` is the whole ancestor chain ("Energy > Energy Access > Rural
# Energy Access") and `depth` its segment count. `parent` alone answers exactly
# one level, so a parent-theme query could never reach a grandchild; the
# materialized path turns "this theme and everything under it" into an indexed
# prefix match at any depth. Both are NULL on rows written before the columns
# existed, and every reader falls back to `theme`/`parent` for those -- see
# `queries._theme_scope_clause`.
#
# Values are classified by app.catalog.theme_taxonomy against the theme map;
# only themes the document is actually tagged with get a row -- a parent is a
# reference, never its own row -- and a document with no theme gets no row at
# all. `theme_type` includes 'unknown' for a theme absent from the map: it is
# stored and countable, but claiming it is a `sub` would assert a parent nothing
# supports and hide it from the primary-tag listing.
#: Named once so the CREATE and the ADD/MODIFY COLUMN migration below cannot
#: disagree about which values are permitted.
_THEME_TYPE_DDL = (
    "theme_type ENUM('primary', 'sub', 'unknown') NOT NULL DEFAULT 'sub'"
)

_STATE_THEME_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_theme` (
    document_id VARCHAR(255) NOT NULL,
    theme       VARCHAR(255) NOT NULL,
    {theme_type},
    parent      VARCHAR(255) NULL,
    theme_group ENUM('main', 'other') NULL,
    theme_path  VARCHAR(1024) NULL,
    depth       TINYINT UNSIGNED NOT NULL DEFAULT 1,
    PRIMARY KEY (document_id, theme),
    KEY idx_val (theme),
    KEY idx_parent (parent),
    KEY idx_group (theme_group),
    -- Prefix-indexed: the descendant expansion is `theme_path LIKE 'X > %'`,
    -- which a left-anchored pattern can range-scan. 255 chars is well past any
    -- real chain and keeps the key inside InnoDB's limit.
    KEY idx_path (theme_path(255)),
    CONSTRAINT `fk_{table}_theme` FOREIGN KEY (document_id)
        REFERENCES `{table}` (document_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Node -> attached PDF links. Composite key: one in-body PDF can be linked
# from several nodes. The PDF's own catalog row is keyed by file_uuid.
_STATE_ATTACHMENT_LINK_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_attachment` (
    file_uuid   VARCHAR(255)  NOT NULL,
    document_id VARCHAR(255)  NOT NULL,
    origin      VARCHAR(16)   NOT NULL,
    url         VARCHAR(1024) NULL,
    filename    VARCHAR(255)  NULL,
    PRIMARY KEY (file_uuid, document_id),
    KEY idx_doc (document_id),
    CONSTRAINT `fk_{table}_attachment` FOREIGN KEY (document_id)
        REFERENCES `{table}` (document_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _table_exists(cur: Any, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.TABLES "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return cur.fetchone() is not None


def _column_exists(cur: Any, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.COLUMNS "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


def _column_type(cur: Any, table: str, column: str) -> str:
    """A column's full type text ("enum('primary','sub')"), lowercased; empty
    when the column does not exist. Lets a migration widen an ENUM only when it
    is actually still narrow — a MODIFY, unlike an ADD COLUMN, has no
    IF NOT EXISTS form and rewrites the table when it runs."""
    cur.execute(
        "SELECT COLUMN_TYPE AS t FROM information_schema.COLUMNS "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    row = cur.fetchone()
    if not row:
        return ""
    value = row["t"] if isinstance(row, dict) else row[0]
    return str(value or "").lower()


def _has_primary_key(cur: Any, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.STATISTICS "
        "WHERE table_schema = DATABASE() AND table_name = %s "
        "AND index_name = 'PRIMARY'",
        (table,),
    )
    return cur.fetchone() is not None


def _ensure_column(cur: Any, table: str, column: str, ddl: str) -> None:
    """Add a column to an existing table only if it is missing (idempotent
    migration for deployments created before the column existed)."""
    if not _column_exists(cur, table, column):
        cur.execute(f"ALTER TABLE `{table}` ADD COLUMN {ddl}")


def _widen_varchar(cur: Any, table: str, column: str, length: int) -> bool:
    """Grow a VARCHAR that is narrower than ``length``. Idempotent, lossless.

    Widening cannot truncate: MySQL keeps every existing value and only raises
    the ceiling. Narrowing could, so this refuses to do it — a column that is
    already wide enough is left exactly as it is.

    Exists because a *closed vocabulary* column outgrew its width.
    ``documents.date_source`` was sized for four values and now carries five;
    the fifth, ``document_copyright``, is 18 characters. Storing it truncated
    would be worse than not storing it, because a silently shortened
    provenance label still reads as a provenance label.
    """
    if not _column_exists(cur, table, column):
        return False
    cur.execute(
        "SELECT CHARACTER_MAXIMUM_LENGTH AS n, IS_NULLABLE AS nullable "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    row = cur.fetchone()
    current = None
    if row:
        current = row["n"] if isinstance(row, dict) else row[0]
    if current is None or int(current) >= length:
        return False
    cur.execute(
        f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` VARCHAR({length}) NULL"
    )
    return True


def _index_exists(cur: Any, table: str, index: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.STATISTICS "
        "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
        (table, index),
    )
    return cur.fetchone() is not None


def _ensure_index(cur: Any, table: str, index: str, columns: str) -> None:
    """Add an index to an existing table only if it is missing.

    The fresh-install DDL declares it; this carries it to a table that predates
    it. Best-effort — an index is a performance property, and failing to add one
    must not stop ``ensure_state_table`` from doing everything else.
    """
    if _index_exists(cur, table, index):
        return
    try:
        cur.execute(f"ALTER TABLE `{table}` ADD KEY `{index}` {columns}")
    except Exception:
        logger.warning(
            "Could not add index %s on `%s`; queries that use it will be slower.",
            index, table, exc_info=True,
        )


def migrate_renamed_facets(cur: Any, table: str, *, dry_run: bool = False) -> list[str]:
    """Carry a renamed facet's child table *and* value column forward.

    Two independent steps, because a deployment can be part-way through: the
    table rename (``documents_category`` -> ``documents_theme``) may already have
    been done by ``scripts.rename_catalog_tables``, which leaves the value column
    named after the old facet. Renaming in place preserves the existing rows --
    the child DDL below cannot, since ``CREATE TABLE IF NOT EXISTS`` silently
    no-ops against the old table.

    Must run *before* the facet DDL: creating the new facet's table first would
    shadow the still-populated old one with an empty table.

    Idempotent -- each step only fires while the old name is the one present.
    Returns the statements applied (or, under ``dry_run``, the ones that would be).
    """
    applied: list[str] = []
    for facet, old in _RENAMED_FACETS.items():
        old_table, new_table = f"{table}_{old}", f"{table}_{facet}"
        # The table still holding the rows: under dry_run nothing moves, so a
        # pending table rename means the column lives on the old table.
        holder = new_table
        if _table_exists(cur, old_table) and not _table_exists(cur, new_table):
            stmt = f"RENAME TABLE `{old_table}` TO `{new_table}`"
            applied.append(stmt)
            if dry_run:
                holder = old_table
            else:
                cur.execute(stmt)
        if (
            _table_exists(cur, holder)
            and _column_exists(cur, holder, old)
            and not _column_exists(cur, holder, facet)
        ):
            stmt = f"ALTER TABLE `{new_table}` RENAME COLUMN `{old}` TO `{facet}`"
            applied.append(stmt)
            if not dry_run:
                cur.execute(stmt)
    return applied


def migrate_author_names(cur: Any, table: str, *, dry_run: bool = False) -> list[str]:
    """Add the derived `author_norm` column to ``documents_author``.

    The raw Drupal value stays in `author`, untouched; `author_norm` holds its
    formatting-normalized form (see :mod:`app.catalog.author_names`) so a count
    of distinct author *names* is not inflated by spacing, punctuation, case or
    a courtesy title. Exactly the arrangement `documents_theme` already uses:
    the value as the CMS wrote it, plus what classification derived from it.

    Nullable and unindexed-until-populated: existing rows keep NULL until
    something fills them (``scripts.backfill_author_names``, or the document's
    next ingest), so adding the column changes no answer on its own.

    Idempotent. Returns the statements applied (or, under ``dry_run``, the ones
    that would be).
    """
    author_table = f"{table}_author"
    applied: list[str] = []
    if not _table_exists(cur, author_table):
        return applied
    if _column_exists(cur, author_table, "author_norm"):
        return applied
    stmt = (
        f"ALTER TABLE `{author_table}` ADD COLUMN author_norm VARCHAR(255) NULL, "
        "ADD KEY idx_author_norm (author_norm)"
    )
    applied.append(stmt)
    if not dry_run:
        cur.execute(stmt)
    return applied


def migrate_facet_uniqueness(
    cur: Any, table: str, facet: str, *, dry_run: bool = False
) -> list[str]:
    """Collapse duplicate (document, value) rows, then forbid them.

    The write path de-duplicated before truncating, so two values differing only
    past character 255 became two identical stored rows — and with no unique key
    the table accepted them. Adding the key to a table that already holds such
    rows fails, so they are collapsed first.

    Collapsed by deleting each duplicated pair outright and re-inserting it once.
    A facet row has no primary key or surrogate id, so there is no way to say
    "delete all but one" in a single statement; the caller's transaction is what
    makes the pair of statements atomic. Only pairs with more than one row are
    touched, so a clean table is read and left alone.

    Idempotent — the key's presence is the guard. Returns the statements applied
    (or, under ``dry_run``, the ones that would be).
    """
    child = f"{table}_{facet}"
    applied: list[str] = []
    if not _table_exists(cur, child) or _index_exists(cur, child, f"uq_{facet}"):
        return applied

    extra = "author_norm" if facet == "author" else None
    cur.execute(
        f"SELECT document_id, `{facet}` AS value, COUNT(*) AS copies"
        + (f", MIN(`{extra}`) AS extra" if extra else "")
        + f" FROM `{child}` GROUP BY document_id, `{facet}` HAVING COUNT(*) > 1"
    )
    duplicates = cur.fetchall()
    if duplicates:
        lost = sum(int(row["copies"]) - 1 for row in duplicates)
        applied.append(
            f"-- collapse {len(duplicates)} duplicated pair(s) in `{child}` "
            f"({lost} redundant row(s))"
        )
        logger.info(
            "Collapsing %d duplicated (document_id, %s) pair(s) in `%s`; %d "
            "redundant row(s) removed.", len(duplicates), facet, child, lost,
        )
        if not dry_run:
            for row in duplicates:
                cur.execute(
                    f"DELETE FROM `{child}` WHERE document_id = %s AND `{facet}` = %s",
                    (row["document_id"], row["value"]),
                )
                columns = f"document_id, `{facet}`" + (f", `{extra}`" if extra else "")
                values = "%s, %s" + (", %s" if extra else "")
                params = [row["document_id"], row["value"]]
                if extra:
                    params.append(row.get("extra"))
                cur.execute(
                    f"INSERT INTO `{child}` ({columns}) VALUES ({values})", tuple(params)
                )

    stmt = f"ALTER TABLE `{child}` ADD UNIQUE KEY `uq_{facet}` (document_id, `{facet}`)"
    applied.append(stmt)
    if not dry_run:
        try:
            cur.execute(stmt)
        except Exception:
            logger.warning(
                "Could not add the unique key to `%s`; duplicates remain possible "
                "there. The table still works — collapse whatever blocked it and "
                "re-run.", child, exc_info=True,
            )
    return applied


def migrate_date_decision_columns(
    cur: Any, table: str, *, dry_run: bool = False
) -> list[str]:
    """Carry the date-decision table's pre-rename column names forward.

    ``documents_date_decision`` predates the replacement of the publication-date
    vocabulary by the effective-date one, so a deployment can still hold
    ``current_published_at``, ``candidate_date`` and ``candidate_source`` while
    :func:`app.catalog.date_decisions.record` writes ``current_start_date``,
    ``candidate_start_date`` and ``date_source``. Every INSERT then fails with
    MySQL 1054, and because both call sites fail open the only symptom is one
    warning per document and a review queue that never advances — measured on
    the live database, which holds 4,933 rows under the old names and none of the
    new columns.

    Renamed in place rather than copied. ``CREATE TABLE IF NOT EXISTS`` cannot do
    it (it no-ops against the existing table) and ``copy_legacy_date_columns``
    will not (it skips a mapping whose replacement column does not exist). The
    rename moves every existing value under the name the code reads, loses
    nothing and leaves no NULLs to backfill. :func:`migrate_renamed_facets`
    renames a value column for exactly the same reason.

    Each step only fires while the old name is the one present, so a part-way
    deployment is carried the rest of the way and a migrated one is a no-op. A
    table where *both* names exist is left alone: that is the copy-then-drop case
    and :func:`copy_legacy_date_columns` owns it.

    Nothing is dropped here. Returns the statements applied (or, under
    ``dry_run``, the ones that would be).
    """
    applied: list[str] = []
    if not _table_exists(cur, table):
        return applied
    for old, new in LEGACY_DATE_COLUMNS["_date_decision"].items():
        if not _column_exists(cur, table, old):
            continue
        if _column_exists(cur, table, new):
            continue
        stmt = f"ALTER TABLE `{table}` RENAME COLUMN `{old}` TO `{new}`"
        applied.append(stmt)
        if not dry_run:
            cur.execute(stmt)
    return applied


def migrate_theme_hierarchy(cur: Any, table: str, *, dry_run: bool = False) -> list[str]:
    """Bring a pre-hierarchy ``documents_theme`` up to the current shape.

    The flat facet table held only (document_id, theme) with no primary key.
    Existing rows keep their theme and take the column default -- an unparented
    sub-theme -- until something reclassifies them
    (``scripts.reclassify_theme_rows``, or the document's next ingest).

    ``theme_path``/``depth`` arrive the same way and stay NULL/1 on legacy rows.
    They are deliberately **not** backfilled here: deriving a path needs the
    theme map, which this module does not read, and every reader already falls
    back to ``theme``/``parent`` when the path is NULL. So an un-reclassified
    deployment keeps its previous one-level behaviour rather than losing theme
    scoping outright, and ``reclassify_theme_rows`` upgrades it in place.

    ``theme_type`` is widened to include ``'unknown'`` before any row can use
    it. Widening an ENUM only adds a permitted value, so it cannot invalidate a
    stored one -- but it has to happen before the first write that classifies a
    theme as unknown, or MySQL coerces that write to '' (or rejects it in strict
    mode).

    The key is added last and its failure is non-fatal: a legacy table can hold
    duplicate (document_id, theme) pairs, and the table works without the key
    anyway (every write replaces a document's rows wholesale), so a duplicate is
    logged rather than allowed to fail ``ensure_state_table`` for everything else.

    Idempotent. Returns the statements applied (or, under ``dry_run``, the ones
    that would be).
    """
    theme_table = f"{table}_theme"
    applied: list[str] = []
    if not _table_exists(cur, theme_table):
        return applied

    for column, ddl in (
        ("theme_type", _THEME_TYPE_DDL),
        ("parent", "parent VARCHAR(255) NULL"),
        ("theme_group", "theme_group ENUM('main', 'other') NULL"),
        ("theme_path", "theme_path VARCHAR(1024) NULL"),
        ("depth", "depth TINYINT UNSIGNED NOT NULL DEFAULT 1"),
    ):
        if _column_exists(cur, theme_table, column):
            continue
        stmt = f"ALTER TABLE `{theme_table}` ADD COLUMN {ddl}"
        applied.append(stmt)
        if not dry_run:
            cur.execute(stmt)

    # Widen an existing theme_type that predates the 'unknown' value. Guarded on
    # the stored type rather than run unconditionally: MODIFY COLUMN rewrites the
    # table, and `ensure_state_table` runs at the start of every ingest.
    if "'unknown'" not in _column_type(cur, theme_table, "theme_type"):
        stmt = f"ALTER TABLE `{theme_table}` MODIFY COLUMN {_THEME_TYPE_DDL}"
        applied.append(stmt)
        if not dry_run:
            cur.execute(stmt)

    if not _index_exists(cur, theme_table, "idx_path") and _column_exists(
        cur, theme_table, "theme_path"
    ):
        stmt = f"ALTER TABLE `{theme_table}` ADD KEY `idx_path` (theme_path(255))"
        applied.append(stmt)
        if not dry_run:
            cur.execute(stmt)

    if not _has_primary_key(cur, theme_table):
        stmt = f"ALTER TABLE `{theme_table}` ADD PRIMARY KEY (document_id, theme)"
        applied.append(stmt)
        if not dry_run:
            try:
                cur.execute(stmt)
            except Exception:
                logger.warning(
                    "Could not add the primary key to `%s` -- duplicate "
                    "(document_id, theme) rows from before it existed? The table "
                    "still works; collapse the duplicates and re-run to get the "
                    "key.", theme_table, exc_info=True,
                )
    return applied


#: ``legacy column -> its replacement``, for the two tables that carried the
#: publication-date vocabulary. The system no longer has a "published date"
#: concept at all: a document has an **effective date** — the business/content
#: date its Drupal bundle's configured field states — and optionally an end to
#: the period that date opens.
#:
#: These are not aliases and no query reads the left-hand side. They exist only so
#: a database created before the rename can be carried across without losing the
#: values it already holds, and so the drop is explicit and reviewable rather
#: than a silent ``ALTER``.
#:
#: The two halves are carried across differently, and the difference is forced by
#: the state each table is actually in. On ``documents`` both names exist and the
#: pipeline has already written the new one, so which value wins is a real
#: question and the answer is copy, verify, then drop
#: (:func:`copy_legacy_date_columns`). On ``documents_date_decision`` the
#: replacement columns were never added, so there is no second value to choose
#: between and the honest operation is a rename in place
#: (:func:`migrate_date_decision_columns`).
LEGACY_DATE_COLUMNS: dict[str, dict[str, str]] = {
    "": {
        "published_at": "effective_start_date",
        "published_until": "effective_end_date",
        "published_at_source": "date_source",
        "published_at_precision": "start_precision",
        "published_until_precision": "end_precision",
    },
    "_date_decision": {
        "current_published_at": "current_start_date",
        "candidate_date": "candidate_start_date",
        "candidate_source": "date_source",
    },
}

#: A column that was modelled, never written by any path, and is being dropped
#: rather than renamed. ``document_published_at`` was "the date the document
#: states about itself"; no ingestion path, script or backfill ever assigned it,
#: so every row is NULL and there is nothing to carry across.
DROPPED_DATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "": ("document_published_at",),
    "_date_decision": (),
}


def legacy_date_columns_present() -> dict[str, list[str]]:
    """Which legacy date columns still exist. ``{table: [column, ...]}``.

    Read-only. Empty for a database created after the rename, and empty again
    once :func:`drop_legacy_date_columns` has run.
    """
    base = state_table()
    found: dict[str, list[str]] = {}
    with mysql_connection() as conn, conn.cursor() as cur:
        for suffix, mapping in LEGACY_DATE_COLUMNS.items():
            table = f"{base}{suffix}"
            names = list(mapping) + list(DROPPED_DATE_COLUMNS.get(suffix, ()))
            present = [c for c in names if _column_exists(cur, table, c)]
            if present:
                found[table] = present
    return found


def copy_legacy_date_columns() -> dict[str, int]:
    """Carry each legacy column's values into its replacement. Idempotent.

    Only fills rows whose replacement is still NULL, so a re-run cannot overwrite
    a value the new pipeline has since written — which is what makes it safe to
    run before *and* after a sweep.

    Nothing is dropped here. Copy, verify, then drop is three steps on purpose:
    a rename that loses data is unrecoverable, and the verification in between is
    the whole point of separating them.
    """
    base = state_table()
    copied: dict[str, int] = {}
    with mysql_connection() as conn, conn.cursor() as cur:
        for suffix, mapping in LEGACY_DATE_COLUMNS.items():
            table = f"{base}{suffix}"
            for old, new in mapping.items():
                if not _column_exists(cur, table, old):
                    continue
                if not _column_exists(cur, table, new):
                    continue
                cur.execute(
                    f"UPDATE `{table}` SET `{new}` = `{old}` "
                    f"WHERE `{new}` IS NULL AND `{old}` IS NOT NULL"
                )
                if cur.rowcount:
                    copied[f"{table}.{old}"] = cur.rowcount
        conn.commit()
    return copied


def unmigrated_legacy_rows() -> dict[str, int]:
    """Rows where a legacy column holds a value its replacement does not.

    The gate on dropping. Non-zero means the copy has not finished — or that
    something wrote the old column after it ran — and the drop must not proceed.
    """
    base = state_table()
    stuck: dict[str, int] = {}
    with mysql_connection() as conn, conn.cursor() as cur:
        for suffix, mapping in LEGACY_DATE_COLUMNS.items():
            table = f"{base}{suffix}"
            for old, new in mapping.items():
                if not (_column_exists(cur, table, old)
                        and _column_exists(cur, table, new)):
                    continue
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM `{table}` "
                    f"WHERE `{old}` IS NOT NULL AND NOT (`{new}` <=> `{old}`)"
                )
                n = int(cur.fetchall()[0]["n"])
                if n:
                    stuck[f"{table}.{old}"] = n
    return stuck


def drop_legacy_date_columns() -> list[str]:
    """Remove the publication-date columns. Refuses while data is unmigrated.

    The last step of the migration, and the one that makes the old concept
    genuinely gone rather than merely unread. Guarded by
    :func:`unmigrated_legacy_rows` so a half-copied database cannot be truncated
    by a mistimed run.
    """
    stuck = unmigrated_legacy_rows()
    if stuck:
        raise RuntimeError(
            f"Refusing to drop legacy date columns: {stuck} row(s) still hold a "
            f"value their replacement does not. Run copy_legacy_date_columns() "
            f"first and re-check."
        )
    base = state_table()
    dropped: list[str] = []
    with mysql_connection() as conn, conn.cursor() as cur:
        for suffix in LEGACY_DATE_COLUMNS:
            table = f"{base}{suffix}"
            names = (list(LEGACY_DATE_COLUMNS[suffix])
                     + list(DROPPED_DATE_COLUMNS.get(suffix, ())))
            for column in names:
                if _column_exists(cur, table, column):
                    cur.execute(f"ALTER TABLE `{table}` DROP COLUMN `{column}`")
                    dropped.append(f"{table}.{column}")
        conn.commit()
    return dropped


def ensure_state_table() -> None:
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_STATE_DDL.format(table=table))
        _ensure_column(cur, table, "effective_start_date", "effective_start_date DATETIME NULL")
        # Where `effective_start_date` came from, and how precise it is. Everything
        # ranks, filters and orders on that column, and until now a bare value
        # could not be told apart from a placeholder: an import timestamp shared
        # by 646 documents and a date the publisher stated read identically.
        #
        # `created` = the source record's creation stamp, which is what the
        # column held for every row before these existed. `cms_field` = a date
        # the source states about the document. `parent_page` = the resolved
        # date of the Drupal page a file hangs on. `document_text` = a
        # publication statement the document itself makes, quoted and verified
        # in its own text. `document_copyright` = a copyright year the document
        # states, corroborated by its own DocInfo creation year.
        #
        # The last two are deliberately separate values. Both are the document
        # speaking about itself, but one is a publication statement carrying a
        # day and the other is a copyright year carrying a year, and collapsing
        # them would make the stronger claim on behalf of the weaker evidence.
        #
        # NULL means *not recorded*, not `created`: the four PDFs whose date came
        # from a verified publication statement would be mislabelled by a blanket
        # backfill, so legacy rows are left unclaimed and
        # `{state}_date_decision.date_source` remains the record for those.
        _ensure_column(cur, table, "date_source",
                       "date_source VARCHAR(32) NULL")
        # Sized for four values originally; `document_copyright` is 18 chars.
        _widen_varchar(cur, table, "date_source", 32)
        # `year` | `month` | `day`. A source holding only "2016" supports the
        # year and nothing finer, so a consumer that reads the day without
        # reading this invents a January publication — the same refusal
        # `DateInterpretation.statement_is_year_only` makes on the PDF path.
        _ensure_column(cur, table, "start_precision",
                       "start_precision VARCHAR(8) NULL")
        # The end of the period the content covers, for the bundles whose
        # mapping declares an end field (`completed_projects`, `events`). NULL
        # for every single-date document, and NULL is never a claim that a
        # period ended — only that none was stated.
        #
        # `effective_start_date` remains the effective date every ranking, ordering and
        # filtering path reads; this is stored beside it as business metadata
        # and for date-range questions. Nothing filters on it yet.
        _ensure_column(cur, table, "effective_end_date",
                       "effective_end_date DATETIME NULL")
        _ensure_column(cur, table, "end_precision",
                       "end_precision VARCHAR(8) NULL")
        _ensure_column(cur, table, "title", "title VARCHAR(1024) NULL")
        _ensure_column(cur, table, "url", "url VARCHAR(1024) NULL")
        _ensure_column(cur, table, "raw_meta", "raw_meta JSON NULL")
        _ensure_column(cur, table, "entity_type", "entity_type VARCHAR(32) NULL")
        # NULL on every existing row, which is exactly right: nothing already
        # indexed was produced by a pipeline that stamped a version, so all of it
        # reads as stale and is rebuilt as it is next crawled.
        _ensure_column(
            cur, table, "pipeline_version", "pipeline_version VARCHAR(32) NULL"
        )
        _ensure_index(cur, table, "idx_pipeline_version", "(pipeline_version)")
        # NULL on every existing row, which is the right default: everything
        # already catalogued got there by producing chunks. Only the narrow
        # metadata-only path in `app.ingestion.pipeline` ever sets it.
        _ensure_column(cur, table, "content_state", "content_state VARCHAR(16) NULL")
        migrate_renamed_facets(cur, table)
        for facet in STATE_FACETS:
            cur.execute(_STATE_CHILD_DDL.format(table=table, facet=facet))
        migrate_author_names(cur, table)
        # After the author column exists: collapsing a duplicated author pair
        # has to carry `author_norm` with it.
        for facet in STATE_FACETS:
            migrate_facet_uniqueness(cur, table, facet)
        # Create then migrate: a fresh install gets the hierarchy from the DDL
        # and the migration no-ops; a legacy table survives CREATE IF NOT EXISTS
        # untouched and gets its columns from the migration.
        cur.execute(
            _STATE_THEME_DDL.format(table=table, theme_type=_THEME_TYPE_DDL)
        )
        migrate_theme_hierarchy(cur, table)
        cur.execute(_STATE_ATTACHMENT_LINK_DDL.format(table=table))
        conn.commit()


# Ingest-time enrichment (LLM-derived per-document output), keyed by the content
# hash it was derived from rather than by document_id — deliberately NOT a child
# table of `documents` and deliberately without a foreign key:
#
#   * it has to survive a state-table reset, which is the usual way to force a
#     reindex and exactly when re-paying for enrichment hurts most;
#   * documents whose body text is identical (the same PDF reached by two URLs,
#     or linked from several nodes) then share one row and enrich once;
#   * nothing may cascade-delete it when a document row goes away, because the
#     same content may come back under a different id.
#
# The trade is that orphan rows have to be pruned rather than cascaded. They are
# small and act as a cache for re-added content, so pruning is a maintenance
# task, not a correctness one.
_ENRICHMENT_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_enrichment` (
    content_hash VARCHAR(64) NOT NULL,
    version      VARCHAR(64) NOT NULL,
    abstract     TEXT        NULL,
    attempts     INT         NOT NULL DEFAULT 0,
    last_error   TEXT        NULL,
    updated_at   DATETIME    NOT NULL,
    PRIMARY KEY (content_hash),
    KEY idx_version (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_enrichment_table() -> None:
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_ENRICHMENT_DDL.format(table=table))
        conn.commit()


# Attachment URLs the site answers 4xx for. Like the enrichment table this is
# deliberately not a child of `documents` and carries no foreign key: a dead
# link never becomes a document row, so there is no parent to hang off.
#
# Keyed by document_id (the attachment's file uuid) and qualified by the
# fingerprint that was current when the download failed, so the marker expires
# exactly when the thing it describes could have changed: edit the node and its
# real attachments are retried, edit the body link and the in-body PDF's
# URL-derived id changes into a row that was never marked dead.
_DEAD_LINK_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_dead_link` (
    document_id VARCHAR(255)  NOT NULL,
    fingerprint VARCHAR(128)  NOT NULL,
    url         VARCHAR(1024) NULL,
    status      SMALLINT      NOT NULL,
    attempts    INT           NOT NULL DEFAULT 1,
    first_seen  DATETIME      NOT NULL,
    updated_at  DATETIME      NOT NULL,
    PRIMARY KEY (document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_dead_link_table() -> None:
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_DEAD_LINK_DDL.format(table=table))
        conn.commit()


# Documents a run reached but did not index, and the crawl position each one
# sits at. The incremental cursor is derived from `documents` — MAX(changed_mark)
# over rows that exist, and a row exists only on success — so a failure leaves no
# trace and the next run's cursor advances straight past it. These rows are that
# trace: the crawl floors its cursor at the earliest one per bundle.
#
# Deliberately NOT a row in `documents`. A placeholder there would count as a
# catalogued document in every analytical read (bundle counts, list_documents,
# theme distributions) — a document that was never indexed showing up as one that
# was. `changed_mark` mirrors the column it is compared against; `bundle` is what
# the cursor is computed per.
_RETRY_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_retry` (
    document_id  VARCHAR(255) NOT NULL,
    source_type  VARCHAR(32)  NOT NULL,
    bundle       VARCHAR(128) NULL,
    changed_mark BIGINT       NULL,
    outcome      VARCHAR(16)  NOT NULL,
    attempts     INT          NOT NULL DEFAULT 1,
    error        TEXT         NULL,
    first_seen   DATETIME     NOT NULL,
    updated_at   DATETIME     NOT NULL,
    PRIMARY KEY (document_id),
    KEY idx_retry_floor (bundle, changed_mark)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_retry_table() -> None:
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_RETRY_DDL.format(table=table))
        conn.commit()


# Shadow output of the evidence-based resolver (deterministic rules + LLM
# interpretation). Separate from both `documents` and `{table}_date_candidate`:
# the first must not be touched at all, and the second records the simpler
# node/file/DocInfo comparison it supersedes. Nothing reads this back into
# ingestion or retrieval — it exists to be reviewed.
_DATE_DECISION_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_date_decision` (
    document_id     VARCHAR(255)  NOT NULL,
    origin          VARCHAR(16)   NOT NULL,
    bundle          VARCHAR(128)  NULL,
    node_uuid       VARCHAR(255)  NULL,
    page_pdf_count  INT           NOT NULL DEFAULT 1,
    current_start_date DATETIME NULL,
    candidate_start_date  DATETIME      NULL,
    candidate_end_date DATETIME   NULL,
    range_issue     VARCHAR(24)   NULL,
    date_type       VARCHAR(16)   NOT NULL,
    edition_label   VARCHAR(64)   NULL,
    date_source VARCHAR(32)  NOT NULL,
    confidence      DECIMAL(4,3)  NOT NULL DEFAULT 0,
    action          VARCHAR(24)   NOT NULL,
    rule            VARCHAR(48)   NOT NULL,
    decided_by      VARCHAR(16)   NOT NULL,
    evidence        TEXT          NULL,
    llm_raw         JSON          NULL,
    prompt_version  VARCHAR(32)   NULL,
    url             VARCHAR(1024) NULL,
    filename        VARCHAR(512)  NULL,
    -- Provenance for a date read from the file's own naming; NULL otherwise.
    title_source      VARCHAR(24) NULL,
    title_kind        VARCHAR(16) NULL,
    title_disposition VARCHAR(16) NULL,
    updated_at      DATETIME      NOT NULL,
    PRIMARY KEY (document_id),
    KEY idx_action (action),
    KEY idx_decided_by (decided_by),
    KEY idx_rule (rule)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_date_decision_table() -> None:
    table = f"{state_table()}_date_decision"
    with mysql_connection() as conn, conn.cursor() as cur:
        # Before the DDL, for the reason migrate_renamed_facets gives: the CREATE
        # is IF NOT EXISTS, so against an existing table it is a no-op and cannot
        # rename anything. A table that does not exist yet is created below with
        # the current names and this is a no-op instead.
        migrate_date_decision_columns(cur, table)
        cur.execute(_DATE_DECISION_DDL.format(table=state_table()))
        # Carried to deployments whose table predates the range-aware model.
        # The CREATE above is IF NOT EXISTS, so on an existing table it is a
        # no-op and these are the only thing that adds the columns.
        _ensure_column(cur, table, "candidate_end_date",
                       "candidate_end_date DATETIME NULL")
        # Why a range is not usable, when it is not: `inverted` |
        # `end_invalid` | `end_without_start`. NULL means either a well-formed
        # range or no range at all — the two cases nobody has to look at.
        _ensure_column(cur, table, "range_issue", "range_issue VARCHAR(24) NULL")
        # Which of the file's own strings gave it a date, what shape of
        # statement that was, and what became of it. NULL for every path that
        # did not read the naming, which is most of them.
        _ensure_column(cur, table, "title_source",
                       "title_source VARCHAR(24) NULL")
        _ensure_column(cur, table, "title_kind", "title_kind VARCHAR(16) NULL")
        _ensure_column(cur, table, "title_disposition",
                       "title_disposition VARCHAR(16) NULL")
        conn.commit()


_LOG_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    id             BIGINT        NOT NULL AUTO_INCREMENT,
    run_id         VARCHAR(64)   NULL,
    document_id    VARCHAR(255)  NOT NULL,
    source_type    VARCHAR(32)   NOT NULL,
    source_path    VARCHAR(1024) NULL,
    source_url     VARCHAR(1024) NULL,
    bundle         VARCHAR(128)  NULL,
    tags           VARCHAR(1024) NULL,
    title          VARCHAR(512)  NULL,
    status         VARCHAR(32)   NOT NULL,
    doc_version    INT           NULL,
    chunks_indexed INT           NULL,
    fingerprint    VARCHAR(128)  NULL,
    content_hash   VARCHAR(64)   NULL,
    error_message  TEXT          NULL,
    event_time     DATETIME      NOT NULL,
    PRIMARY KEY (id),
    KEY idx_document (document_id),
    KEY idx_source_type (source_type),
    KEY idx_event_time (event_time),
    KEY idx_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_log_table() -> None:
    table = log_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_LOG_DDL.format(table=table))
        conn.commit()


# Entity mentions: one row per sighting of a name in a chunk. An append-heavy
# audit log — this is the largest table the knowledge layer adds — which is why
# it stays relational rather than becoming nodes in the graph.
#
# Deliberately NOT a child of `documents`:
#   * chunk ids are version-scoped, so a re-index replaces a document's whole
#     mention set anyway, by (document_id, doc_version);
#   * the same guard the enrichment table documents applies — content that comes
#     back under a different id must not lose its rows to a cascade.
#
# UNIQUE(chunk_id, start_offset, end_offset, normalized_text) is what makes
# repeated extraction idempotent: re-running writes the same rows, so retries
# and re-sweeps cannot duplicate knowledge. No entity_id column exists here —
# a mention is a sighting, and resolution owns identity.
_ENTITY_MENTION_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_entity_mention` (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    chunk_id          VARCHAR(64)  NOT NULL,
    document_id       VARCHAR(255) NOT NULL,
    doc_version       INT          NULL,
    start_offset      INT          NOT NULL,
    end_offset        INT          NOT NULL,
    surface_text      VARCHAR(512) NOT NULL,
    normalized_text   VARCHAR(512) NOT NULL,
    entity_type       VARCHAR(32)  NOT NULL,
    extraction_method VARCHAR(32)  NOT NULL,
    extractor_version VARCHAR(64)  NOT NULL,
    confidence        FLOAT        NOT NULL,
    created_at        DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_span (chunk_id, start_offset, end_offset, normalized_text),
    KEY idx_document (document_id, doc_version),
    KEY idx_normalized (entity_type, normalized_text),
    KEY idx_method (extraction_method)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# The extraction cost cache, modelled on `{table}_enrichment`: keyed by the
# chunk's own content hash (not its id) so a re-index whose paragraphs are
# unchanged still hits, and qualified by a key covering the extractor version
# and the gazetteer, so newer code never serves output it would not produce.
_ENTITY_EXTRACTION_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_entity_extraction` (
    content_hash      VARCHAR(64) NOT NULL,
    extraction_key    VARCHAR(64) NOT NULL,
    extractor_version VARCHAR(64) NOT NULL,
    mention_count     INT         NOT NULL DEFAULT 0,
    attempts          INT         NOT NULL DEFAULT 0,
    last_error        TEXT        NULL,
    updated_at        DATETIME    NOT NULL,
    PRIMARY KEY (content_hash),
    KEY idx_key (extraction_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_entity_tables() -> None:
    """Create the mention log and its extraction cache. Idempotent."""
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_ENTITY_MENTION_DDL.format(table=table))
        cur.execute(_ENTITY_EXTRACTION_DDL.format(table=table))
        conn.commit()


# ---------------------------------------------------------------------------
# Canonical entities and the resolution audit trail.
#
# Entities are the things mentions may resolve *to*. They are seeded from CMS
# records, so most carry a `cms_uuid` and are authoritative; a few are created
# provisionally from text. `entity_id` is opaque and derived deterministically
# from the seed source, so re-seeding a clean corpus reproduces the same ids
# rather than minting new ones.
#
# Nothing here cascades from `documents`: deleting one news item must not
# destroy the identity of a person named in three hundred PDFs. Orphaned
# entities are reportable and prunable, never cascaded.
# ---------------------------------------------------------------------------
_ENTITY_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_entity` (
    entity_id       VARCHAR(64)  NOT NULL,
    entity_type     VARCHAR(32)  NOT NULL,
    canonical_name  VARCHAR(512) NOT NULL,
    normalized_name VARCHAR(512) NOT NULL,
    source          VARCHAR(64)  NOT NULL,
    cms_uuid        VARCHAR(255) NULL,
    trust           VARCHAR(16)  NOT NULL DEFAULT 'derived',
    status          VARCHAR(16)  NOT NULL DEFAULT 'active',
    -- Whether this entity may be the subject or object of a claim, and a target
    -- for graph retrieval. 0 for a *provisional* identity: a name the corpus
    -- attests but has not shown to denote exactly one real-world thing. The
    -- author facet is full of these -- two different people called "Arun Kumar"
    -- are one row here -- so linking a mention to such a row groups sightings
    -- by name and asserts nothing about identity.
    claim_eligible  TINYINT(1)   NOT NULL DEFAULT 1,
    merged_into     VARCHAR(64)  NULL,
    created_at      DATETIME     NOT NULL,
    updated_at      DATETIME     NOT NULL,
    PRIMARY KEY (entity_id),
    UNIQUE KEY uq_cms_uuid (cms_uuid),
    KEY idx_normalized (entity_type, normalized_name),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Every surface form that may denote an entity. `autolink` carries the Phase 4
# eligibility rule forward: a surface too short, too generic, or attested for
# more than one entity is a resolution *candidate* but never an automatic match.
_ENTITY_ALIAS_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_entity_alias` (
    entity_id    VARCHAR(64)  NOT NULL,
    normalized   VARCHAR(512) NOT NULL,
    surface      VARCHAR(512) NOT NULL,
    alias_type   VARCHAR(32)  NOT NULL,
    autolink     TINYINT(1)   NOT NULL DEFAULT 1,
    is_ambiguous TINYINT(1)   NOT NULL DEFAULT 0,
    source       VARCHAR(64)  NOT NULL,
    PRIMARY KEY (entity_id, normalized, alias_type),
    KEY idx_normalized (normalized),
    KEY idx_ambiguous (is_ambiguous)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Exact identifiers. PRIMARY KEY (scheme, value) states "this identifier denotes
# exactly one entity" as a database invariant - the strongest correctness
# guarantee in the entity layer, and what makes Tier 0 a lookup rather than an
# inference. Project codes live here.
_ENTITY_IDENTIFIER_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_entity_identifier` (
    scheme    VARCHAR(32)  NOT NULL,
    value     VARCHAR(255) NOT NULL,
    entity_id VARCHAR(64)  NOT NULL,
    source    VARCHAR(64)  NOT NULL,
    PRIMARY KEY (scheme, value),
    KEY idx_entity (entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# One row per resolution attempt: what was decided, and everything needed to
# explain why. `candidates` holds the scored shortlist as JSON so a decision can
# be re-read without re-running the resolver. Deliberately append-only.
_ENTITY_DECISION_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_entity_resolution_decision` (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    chunk_id          VARCHAR(64)  NOT NULL,
    start_offset      INT          NOT NULL,
    end_offset        INT          NOT NULL,
    surface_text      VARCHAR(512) NOT NULL,
    normalized_text   VARCHAR(512) NOT NULL,
    entity_type       VARCHAR(32)  NOT NULL,
    decision          VARCHAR(16)  NOT NULL,
    entity_id         VARCHAR(64)  NULL,
    -- Whether the linked identity may carry claims. Denormalized onto the
    -- decision so a consumer never has to join back to the entity to find out,
    -- and so the log stays readable after a later promotion changes the entity.
    claim_eligible    TINYINT(1)   NOT NULL DEFAULT 1,
    tier              VARCHAR(24)  NOT NULL,
    score             FLOAT        NULL,
    margin            FLOAT        NULL,
    reason            VARCHAR(255) NOT NULL,
    candidates        JSON         NULL,
    resolver_version  VARCHAR(64)  NOT NULL,
    created_at        DATETIME     NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_mention (chunk_id, start_offset, end_offset, normalized_text),
    KEY idx_decision (entity_type, decision),
    KEY idx_entity (entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_resolution_tables() -> None:
    """Create the canonical entity store and the decision log. Idempotent."""
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_ENTITY_DDL.format(table=table))
        cur.execute(_ENTITY_ALIAS_DDL.format(table=table))
        cur.execute(_ENTITY_IDENTIFIER_DDL.format(table=table))
        cur.execute(_ENTITY_DECISION_DDL.format(table=table))
        # Added after the table shipped: a deployment created before the
        # provisional-identity distinction existed has every entity implicitly
        # claim-eligible, which is the unsafe default the column exists to fix.
        _ensure_column(
            cur, f"{table}_entity", "claim_eligible",
            "claim_eligible TINYINT(1) NOT NULL DEFAULT 1",
        )
        _ensure_column(
            cur, f"{table}_entity_resolution_decision", "claim_eligible",
            "claim_eligible TINYINT(1) NOT NULL DEFAULT 1",
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Assertion staging.
#
# Claims live here first and only here: projection to Neo4j is a separate,
# retryable pass, so a graph outage costs a retry rather than a re-extraction,
# and no transaction ever has to span two databases.
#
# `claim_id` is the primary key and is derived from what the source *states*
# (evidence + subject + predicate + object) and nothing about how it was read.
# Re-extracting the same chunk therefore updates the row rather than forking it
# -- see app.knowledge.claims.types for why validity and confidence are
# deliberately excluded from the identity.
# ---------------------------------------------------------------------------
_ASSERTION_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_assertion` (
    claim_id           VARCHAR(64)  NOT NULL,
    subject_entity_id  VARCHAR(64)  NOT NULL,
    predicate          VARCHAR(64)  NOT NULL,
    object_entity_id   VARCHAR(64)  NULL,
    object_literal     VARCHAR(255) NULL,
    document_id        VARCHAR(255) NOT NULL,
    chunk_id           VARCHAR(64)  NULL,
    evidence_kind      VARCHAR(16)  NOT NULL,
    source_field       VARCHAR(64)  NULL,
    -- The literal CMS value that produced a cms_field claim, and a hash of it.
    -- NOT part of claim identity: the value already reaches the id through the
    -- object, so an edited field yields a *different* claim rather than
    -- silently changing this one's meaning. Recorded for explainability ("why
    -- does the system believe this?") and so a re-extraction can tell a value
    -- that was edited from one that was removed.
    source_value       VARCHAR(512) NULL,
    source_value_hash  VARCHAR(64)  NULL,
    quote              TEXT         NULL,
    quote_start        INT          NULL,
    quote_end          INT          NULL,
    valid_from         DATE         NULL,
    valid_until        DATE         NULL,
    temporal_basis     VARCHAR(16)  NOT NULL DEFAULT 'unknown',
    confidence         FLOAT        NOT NULL DEFAULT 0,
    status             VARCHAR(16)  NOT NULL DEFAULT 'active',
    extraction_method  VARCHAR(32)  NOT NULL,
    extractor_version  VARCHAR(64)  NOT NULL,
    vocabulary_version VARCHAR(64)  NOT NULL,
    model              VARCHAR(128) NULL,
    prompt_version     VARCHAR(64)  NULL,
    asserted_at        DATETIME     NOT NULL,
    created_at         DATETIME     NOT NULL,
    updated_at         DATETIME     NOT NULL,
    PRIMARY KEY (claim_id),
    KEY idx_subject (subject_entity_id, predicate),
    KEY idx_object (object_entity_id),
    KEY idx_document (document_id),
    KEY idx_chunk (chunk_id),
    KEY idx_status (status),
    KEY idx_predicate (predicate)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# Why an assertion was refused. Append-only and deliberately separate from the
# staged claims: "the model produced fewer claims today" is only diagnosable if
# the refusals were recorded, and a rejected claim must never sit in the same
# table as an accepted one.
_ASSERTION_REJECTION_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_assertion_rejection` (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    code              VARCHAR(48)  NOT NULL,
    detail            VARCHAR(255) NULL,
    subject_entity_id VARCHAR(64)  NULL,
    predicate         VARCHAR(64)  NULL,
    document_id       VARCHAR(255) NULL,
    chunk_id          VARCHAR(64)  NULL,
    extraction_method VARCHAR(32)  NULL,
    created_at        DATETIME     NOT NULL,
    PRIMARY KEY (id),
    KEY idx_code (code),
    KEY idx_chunk (chunk_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


# Directed links between claims: one supersedes another, or two contradict.
# A separate table rather than columns because a claim may contradict several
# others, and because a contradiction is a fact worth inspecting on its own
# rather than something implied by two status flags.
_ASSERTION_LINK_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_assertion_link` (
    from_claim_id VARCHAR(64)  NOT NULL,
    to_claim_id   VARCHAR(64)  NOT NULL,
    kind          VARCHAR(16)  NOT NULL,
    reason        VARCHAR(255) NULL,
    detector      VARCHAR(64)  NOT NULL,
    created_at    DATETIME     NOT NULL,
    PRIMARY KEY (from_claim_id, to_claim_id, kind),
    KEY idx_to (to_claim_id),
    KEY idx_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_assertion_tables() -> None:
    """Create the assertion staging tables. Idempotent."""
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_ASSERTION_DDL.format(table=table))
        cur.execute(_ASSERTION_REJECTION_DDL.format(table=table))
        cur.execute(_ASSERTION_LINK_DDL.format(table=table))
        # Added after the table shipped.
        for column, ddl in (
            ("source_value", "source_value VARCHAR(512) NULL"),
            ("source_value_hash", "source_value_hash VARCHAR(64) NULL"),
        ):
            _ensure_column(cur, f"{table}_assertion", column, ddl)
        conn.commit()


# ---------------------------------------------------------------------------
# Pending relationship candidates.
#
# An extractor may propose a predicate the closed vocabulary does not contain
# ("COLLABORATED_WITH"). Before this table the evidence was thrown away twice
# over: app.knowledge.claims.extract_llm dropped the proposal, and
# app.knowledge.claims.validate rejected it with a code and no quote. So the one
# question the vocabulary needs answered -- "what relationship does this corpus
# keep asserting that we cannot express?" -- had no data behind it.
#
# A candidate is evidence, never a claim and never an edge. It carries the same
# verified quote a claim would, so a reviewer can read the sentence that
# proposed it, and it cannot become real without a source-code change to
# app.knowledge.claims.predicates and a VOCABULARY_VERSION bump. Nothing at
# runtime can widen the graph vocabulary.
#
# `candidate_id` is built by the same hash construction as `claim_id`, so a
# retry upserts rather than duplicating -- the identity argument in
# app.knowledge.claims.types applies here unchanged.
# ---------------------------------------------------------------------------
_PREDICATE_CANDIDATE_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_predicate_candidate` (
    candidate_id         VARCHAR(64)  NOT NULL,
    predicate_surface    VARCHAR(128) NOT NULL,
    predicate_normalized VARCHAR(128) NOT NULL,
    subject_entity_id    VARCHAR(64)  NOT NULL,
    object_entity_id     VARCHAR(64)  NULL,
    object_literal       VARCHAR(255) NULL,
    document_id          VARCHAR(255) NOT NULL,
    chunk_id             VARCHAR(64)  NULL,
    evidence_kind        VARCHAR(16)  NOT NULL,
    quote                TEXT         NULL,
    quote_start          INT          NULL,
    quote_end            INT          NULL,
    confidence           FLOAT        NOT NULL DEFAULT 0,
    extraction_method    VARCHAR(32)  NOT NULL,
    extractor_version    VARCHAR(64)  NOT NULL,
    vocabulary_version   VARCHAR(64)  NOT NULL,
    model                VARCHAR(128) NULL,
    prompt_version       VARCHAR(64)  NULL,
    status               VARCHAR(16)  NOT NULL DEFAULT 'pending',
    observations         INT          NOT NULL DEFAULT 1,
    first_seen_at        DATETIME     NOT NULL,
    last_seen_at         DATETIME     NOT NULL,
    PRIMARY KEY (candidate_id),
    KEY idx_predicate (predicate_normalized, status),
    KEY idx_document (document_id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_predicate_candidate_table() -> None:
    """Create the pending-predicate table. Idempotent."""
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_PREDICATE_CANDIDATE_DDL.format(table=table))
        conn.commit()


# ---------------------------------------------------------------------------
# Per-document knowledge runs.
#
# One row per (document_id, doc_version), upserted -- not append-only. History
# of *versions* is what matters here; history of *retries* is `attempts` plus
# `last_error`, which is the shape `{table}_enrichment` and `{table}_dead_link`
# already use. An append-only log would grow one row per sweep per document and
# answer no question the counters do not.
#
# The counters are scalar columns rather than one JSON blob because they are
# what an operator aggregates ("how many documents staged nothing this week");
# JSON is reserved for the two genuinely open-ended maps, the rejection tally
# and the error list.
#
# A document with no row here is one whose stage never ran or crashed before it
# could report -- which is exactly what the catch-up sweep looks for, so the
# absence is load-bearing and the row is written last.
# ---------------------------------------------------------------------------
_KNOWLEDGE_RUN_DDL = """
CREATE TABLE IF NOT EXISTS `{table}_knowledge_run` (
    document_id          VARCHAR(255) NOT NULL,
    doc_version          INT          NOT NULL,
    run_id               VARCHAR(64)  NULL,
    status               VARCHAR(16)  NOT NULL,
    attempts             INT          NOT NULL DEFAULT 0,
    seconds              FLOAT        NOT NULL DEFAULT 0,
    chunks_seen          INT          NOT NULL DEFAULT 0,
    chunks_cached        INT          NOT NULL DEFAULT 0,
    mentions             INT          NOT NULL DEFAULT 0,
    entities_auto        INT          NOT NULL DEFAULT 0,
    entities_provisional INT          NOT NULL DEFAULT 0,
    entities_ambiguous   INT          NOT NULL DEFAULT 0,
    entities_unresolved  INT          NOT NULL DEFAULT 0,
    claims_built         INT          NOT NULL DEFAULT 0,
    claims_staged        INT          NOT NULL DEFAULT 0,
    claims_rejected      INT          NOT NULL DEFAULT 0,
    claims_retracted     INT          NOT NULL DEFAULT 0,
    pending_predicates   INT          NOT NULL DEFAULT 0,
    conflicts_disputed   INT          NOT NULL DEFAULT 0,
    conflicts_superseded INT          NOT NULL DEFAULT 0,
    projection_status    VARCHAR(16)  NOT NULL DEFAULT 'skipped',
    projection_version   VARCHAR(64)  NULL,
    projection_edges     INT          NOT NULL DEFAULT 0,
    rejection_counts     JSON         NULL,
    errors               JSON         NULL,
    last_error           TEXT         NULL,
    knowledge_version    VARCHAR(128) NOT NULL,
    created_at           DATETIME     NOT NULL,
    updated_at           DATETIME     NOT NULL,
    PRIMARY KEY (document_id, doc_version),
    KEY idx_status (status, attempts),
    KEY idx_updated (updated_at),
    KEY idx_document (document_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_knowledge_run_table() -> None:
    """Create the per-document knowledge run table. Idempotent."""
    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(_KNOWLEDGE_RUN_DDL.format(table=table))
        conn.commit()
