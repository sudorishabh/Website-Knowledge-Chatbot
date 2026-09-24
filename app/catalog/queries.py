"""Retrieval-facing analytical reads over the document catalog.

Everything here is SELECT-only over the ingest-state tables (see
:mod:`app.catalog.schema` for DDL, :mod:`app.catalog.state` for the write
path). Two families live in this one module because they share the same
filter-building logic and table:

- ``count_documents`` / ``list_documents`` / ``distribution`` answer the
  structured (database-intent) catalog tools;
- ``document_ids_in_scope`` / ``attachments_for`` answer id-scoped retrieval
  (scoped summarization, website-attachment supplementation) — these bake in
  ``source_type='website', entity_type='node'`` since that's the retrieval
  layer's catalog of record.

DB errors fail open: the count/list/distribution functions let real errors
raise (callers already guard), while the id-scope helpers below swallow errors
and return empty, so a MySQL outage degrades retrieval instead of failing it.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Sequence

from app.catalog import theme_taxonomy
from app.catalog.db import state_table as _table
from app.catalog.state import StateRecord, _row_to_record
from app.core.clients import mysql_connection

logger = logging.getLogger(__name__)


# Theme values that are ingestion artefacts rather than themes: a boolean field
# stringified into the theme facet. Filtered in SQL so `limit` applies to real
# themes only. The ingestion-side guard is the actual fix; this keeps the
# artefact out of answers for rows already written.
_NON_THEME_VALUES: tuple[str, ...] = ("False", "True")


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _escape_like(term: str) -> str:
    """LIKE-escape without wrapping in wildcards, for a left-anchored pattern."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _theme_scope_clause(alias: str, name: str) -> tuple[str, list[Any]]:
    """A theme scope covering ``name`` **and every theme beneath it, at any
    depth** — the condition behind "how many documents are in Energy?" when the
    documents are tagged only "Rural Energy Access".

    Matched on the materialized ``theme_path``: a theme is in scope when its
    path *is* the theme's path, or begins with it plus the separator. Left
    anchored, so the prefix index does the work; the separator in the prefix is
    what keeps "Energy" from also matching a sibling called "Energy Storage"
    that happens to share the first six characters.

    The path is looked up from the theme map rather than from the rows, so a
    query for a mid-level theme expands correctly even when no document carries
    that theme itself. A theme the map does not know has no path to expand, so
    it matches by name only — which is exactly right: nothing is known to sit
    beneath it.

    The NULL-path branch is the compatibility path, not a nicety. Rows written
    before ``theme_path`` existed carry NULL until
    ``state.reclassify_theme_rows`` (or a re-ingest) fills them in, and without
    this they would silently drop out of every theme filter — turning a
    migration that has not been run yet into wrong counts rather than old ones.
    It reproduces the previous one-level behaviour exactly: exact name, or
    ``parent`` naming the theme.
    """
    path = theme_taxonomy.path_of(name) or name
    clause = (
        f"({alias}.theme_path = %s OR {alias}.theme_path LIKE %s"
        f" OR ({alias}.theme_path IS NULL AND ({alias}.theme = %s OR {alias}.parent = %s)))"
    )
    prefix = f"{_escape_like(path)}{_escape_like(theme_taxonomy.PATH_SEPARATOR)}%"
    return clause, [path, prefix, name, name]


def _catalog_filters(
    source_type: str | None,
    bundle: str | None,
    *,
    entity_type: str | None = None,
    title_contains: str | None = None,
    topic_terms: Sequence[str] | None = None,
    author: str | None = None,
    theme: str | None = None,
    theme_group: str | None = None,
    tag: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> tuple[str, list[str], list[Any], bool]:
    """Shared JOIN/WHERE assembly for the catalog count/list queries.

    ``entity_type`` scopes to one Drupal entity kind — the query layer passes
    "node" so taxonomy-term and block rows never count as content documents.

    ``theme`` matches a theme **and every theme beneath it, at any depth** — see
    :func:`_theme_scope_clause`. Exact-name rather than substring: the caller
    canonicalizes the name first (see ``app.retrieval.structured.filters``), and
    a substring match both misses sub-themes and wrongly merges siblings —
    "Environment" would sweep in "Environment Education" while missing "Air" and
    "Water".

    ``tag`` joins ``documents_tag`` separately from the theme join, so a theme
    filter and a tag filter combine as AND rather than collapsing into one
    condition. Returns (joins, clauses, params, needs_distinct)."""
    table = _table()
    joins: list[str] = []
    clauses: list[str] = []
    params: list[Any] = []
    distinct = False
    if source_type is not None:
        clauses.append("s.source_type = %s")
        params.append(source_type)
    if bundle is not None:
        clauses.append("s.bundle = %s")
        params.append(bundle)
    if entity_type is not None:
        clauses.append("s.entity_type = %s")
        params.append(entity_type)
    if title_contains:
        clauses.append("s.title LIKE %s")
        params.append(_like(title_contains))
    if topic_terms:
        # OR, not AND: a title rarely carries every word of a topic phrase
        # ("Who is adapting and how?" is a climate-adaptation paper whose title
        # contains neither "climate" nor "adaptation" in full). Requiring all of
        # them returns nothing; requiring one, and letting the caller order by
        # how many matched, puts the squarely-on-topic rows first while still
        # excluding the rest of the bucket. Distinct from `title_contains`,
        # which is a phrase the user named and stays an exact requirement.
        ors = " OR ".join(["s.title LIKE %s"] * len(topic_terms))
        clauses.append(f"({ors})")
        params.extend(_like(term) for term in topic_terms)
    if effective_from is not None:
        clauses.append("s.effective_start_date >= %s")
        params.append(effective_from)
    if effective_to is not None:
        clauses.append("s.effective_start_date < %s")
        params.append(effective_to)
    if author:
        joins.append(f" JOIN `{table}_author` a ON a.document_id = s.document_id")
        clauses.append("a.author LIKE %s")
        params.append(_like(author))
        distinct = True
    if theme:
        joins.append(f" JOIN `{table}_theme` c ON c.document_id = s.document_id")
        clause, args = _theme_scope_clause("c", theme)
        clauses.append(clause)
        params.extend(args)
        distinct = True
    if theme_group:
        # Documents carrying at least one theme from this group. Matched by
        # equality, never as "not the other group": a theme the theme map does
        # not define has a NULL group, and `<> 'other'` is not true of NULL — nor
        # should an uncurated CMS term be counted as part of a curated one.
        # Joined separately from `theme` so "under Energy" and "in a main theme"
        # combine as AND rather than fighting over one alias.
        joins.append(f" JOIN `{table}_theme` g ON g.document_id = s.document_id")
        clauses.append("g.theme_group = %s")
        params.append(theme_group)
        distinct = True
    if tag:
        joins.append(f" JOIN `{table}_tag` t ON t.document_id = s.document_id")
        clauses.append("t.tag = %s")
        params.append(tag)
        distinct = True
    return "".join(joins), clauses, params, distinct


def count_documents(
    source_type: str | None = None,
    bundle: str | None = None,
    *,
    entity_type: str | None = None,
    title_contains: str | None = None,
    topic_terms: Sequence[str] | None = None,
    author: str | None = None,
    theme: str | None = None,
    theme_group: str | None = None,
    tag: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> int:
    """Count catalog documents (not chunks) matching the given filters.

    ``author`` and ``title_contains`` match substrings; ``theme`` and ``tag``
    match names exactly (``theme`` also matching its sub-themes) — see
    :func:`_catalog_filters`. Date bounds are a half-open ``[from, to)`` interval
    over ``effective_start_date``. Takes the same filter set as
    ``list_documents``/``distribution`` so a count and a listing of the same
    query can never disagree."""
    table = _table()
    joins, clauses, params, distinct = _catalog_filters(
        source_type, bundle, entity_type=entity_type, title_contains=title_contains,
        topic_terms=topic_terms, author=author, theme=theme,
        theme_group=theme_group, tag=tag,
        effective_from=effective_from, effective_to=effective_to,
    )
    count_expr = "COUNT(DISTINCT s.document_id)" if distinct else "COUNT(*)"
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT {count_expr} AS n FROM `{table}` s{joins}{where}"
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0


def list_documents(
    source_type: str | None = None,
    bundle: str | None = None,
    *,
    entity_type: str | None = None,
    title_contains: str | None = None,
    topic_terms: Sequence[str] | None = None,
    author: str | None = None,
    theme: str | None = None,
    theme_group: str | None = None,
    tag: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    limit: int = 10,
    offset: int = 0,
) -> list[StateRecord]:
    """List catalog documents matching the filters, most recent first.

    Mirrors ``count_documents`` but returns the matching rows so structured
    list/lookup queries are answered from the local catalog instead of a live
    site fetch. ``limit`` is clamped to [1, 100]; ``offset`` clamps to >= 0 and
    pages through the same ordering (effective_start_date desc, document_id asc)."""
    table = _table()
    joins, clauses, params, needs_distinct = _catalog_filters(
        source_type, bundle, entity_type=entity_type,
        title_contains=title_contains, topic_terms=topic_terms, author=author,
        theme=theme, theme_group=theme_group, tag=tag,
        effective_from=effective_from, effective_to=effective_to,
    )
    distinct = "DISTINCT " if needs_distinct else ""
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    capped = max(1, min(int(limit or 10), 100))
    capped_offset = max(0, int(offset or 0))
    offset_clause = f" OFFSET {capped_offset}" if capped_offset else ""
    # Under a topic constraint, recency alone is the wrong order: the WHERE only
    # requires *one* term to match, so the newest single-term match would lead a
    # list whose best row matches every term. Rank by how much of the topic each
    # title carries, and keep recency as the tie-break — which leaves the
    # no-topic ordering exactly as it was. The extra placeholders go after the
    # WHERE ones because ORDER BY comes later in the statement.
    relevance = ""
    if topic_terms:
        scored = " + ".join(["(s.title LIKE %s)"] * len(topic_terms))
        relevance = f"({scored}) DESC, "
        params = list(params) + [_like(term) for term in topic_terms]
    sql = (
        f"SELECT {distinct}s.* FROM `{table}` s{joins}{where} "
        f"ORDER BY {relevance}s.effective_start_date DESC, s.document_id ASC "
        f"LIMIT {capped}{offset_clause}"
    )
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [_row_to_record(row) for row in cur.fetchall()]


# Dimensions the distribution query can group by. "author" groups on its facet
# table; "theme" groups on the documents_theme facet; "bundle" and "year" come
# off the document row itself.
_DISTRIBUTION_DIMENSIONS = ("bundle", "author", "theme", "year")


def distribution(
    group_by: str,
    source_type: str | None = None,
    bundle: str | None = None,
    *,
    entity_type: str | None = None,
    author: str | None = None,
    theme: str | None = None,
    theme_group: str | None = None,
    tag: str | None = None,
    title_contains: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    limit: int = 20,
) -> list[tuple[str, int]]:
    """Grouped document counts ("how many per theme/type/author/year"),
    largest group first. Returns (group value, count) pairs.

    Applies the same theme/tag/author/title/date scope as ``count_documents``
    and ``list_documents`` (via :func:`_catalog_filters`), so a breakdown can be
    narrowed to one theme, tag, author, period, etc. — ``tag`` is a scope filter
    here, not a groupable dimension (see ``_DISTRIBUTION_DIMENSIONS``). A
    document that fans out across a facet join is counted once per group.

    ``source_type`` defaults to None — no filter — for the same reason every
    other parameter here does, and to match ``count_documents`` and
    ``list_documents``. It previously defaulted to ``"website"``, which made a
    breakdown silently narrower than a count of the same scope: the same author
    was 35 documents here and 46 there. Callers that want one source kind pass
    it, as the tool layer does."""
    if group_by not in _DISTRIBUTION_DIMENSIONS:
        raise ValueError(f"group_by must be one of {_DISTRIBUTION_DIMENSIONS}")
    table = _table()
    # Grouping by theme applies `theme_group` to the *group* join below instead.
    # As a scope filter it would only require the document to carry some theme
    # from the group, so a document tagged both "Energy" (main) and "Green
    # Shipping" (other) would still contribute a row under Green Shipping — the
    # breakdown has to restrict the themes it groups on, not the documents.
    scope_group = None if group_by == "theme" else theme_group
    scope_joins, clauses, params, scoped = _catalog_filters(
        source_type, bundle, entity_type=entity_type,
        title_contains=title_contains, author=author,
        theme=theme, theme_group=scope_group, tag=tag,
        effective_from=effective_from, effective_to=effective_to,
    )

    # `label` is what the row is called; `key` is what it groups on. They
    # differ only for authors, where the grouping key is a normalized name
    # and the label a raw spelling of it.
    label = None
    if group_by == "bundle":
        group_join, key = "", "s.bundle"
    elif group_by == "year":
        group_join, key = "", "YEAR(s.effective_start_date)"
        clauses.append("s.effective_start_date IS NOT NULL")
    elif group_by == "theme":
        # Group on the theme facet, excluding the boolean-literal artefacts
        # `theme_vocabulary` also filters, so a breakdown and a listing of the
        # vocabulary can never disagree about which themes exist.
        group_join = f" JOIN `{table}_theme` gt ON gt.document_id = s.document_id"
        key = "gt.theme"
        placeholders = ", ".join(["%s"] * len(_NON_THEME_VALUES))
        clauses.append(f"gt.theme <> '' AND gt.theme NOT IN ({placeholders})")
        params.extend(_NON_THEME_VALUES)
        if theme_group:
            clauses.append("gt.theme_group = %s")
            params.append(theme_group)
    else:  # author -> multi-valued facet table
        group_join = f" JOIN `{table}_{group_by}` f ON f.document_id = s.document_id"
        # Grouped on the normalized name so spellings of one name are one row,
        # but labelled with a raw spelling so the answer echoes the source
        # rather than a lowercased key. MIN is arbitrary among spellings but
        # deterministic, which is what a repeated query needs.
        key = _author_key("f")
        label = "MIN(f.author)"

    # A facet join — from the scope filters or the group key itself — can repeat
    # a document across rows, so count distinct documents unless the query stays
    # purely on the document row (bundle/year with no scoped join).
    count_expr = (
        "COUNT(*)"
        if group_by in ("bundle", "year") and not scoped
        else "COUNT(DISTINCT s.document_id)"
    )
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    capped = max(1, min(int(limit or 20), 100))
    sql = (
        f"SELECT {label or key} AS k, {count_expr} AS n "
        f"FROM `{table}` s{scope_joins}{group_join}{where} "
        f"GROUP BY {key} ORDER BY n DESC, k ASC LIMIT {capped}"
    )
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [(str(row["k"]), int(row["n"])) for row in rows if row["k"] is not None]


# Author identity for counting and grouping. `author_norm` is the
# formatting-normalized form written beside the raw value (see
# `app.catalog.author_names`); COALESCE keeps a row written before the column
# existed countable under its raw spelling rather than collapsing every such row
# into a single NULL group.
#
# It groups author *names*, not people: two people called "Arun Kumar" share a
# normalized form exactly as they already shared a raw one, and name-order
# variants ("Datta Debajit" / "Debajit Datta") remain separate because deciding
# they are one person is an inference this layer does not make.
def _author_key(alias: str) -> str:
    return f"COALESCE({alias}.author_norm, {alias}.author)"


def _dimension_sql(
    dimension: str, table: str, alias: str
) -> tuple[str, str, str]:
    """(join, group key, display label) for one dimension.

    Key and label are the same expression everywhere except authors, where
    rows group on the normalized name but are labelled with a raw spelling
    so an answer echoes the source rather than a lowercased key.

    Shared by :func:`count_distinct_values` and :func:`cross_distribution` so a
    dimension means the same thing however it is used — the alias is the
    caller's so two dimensions can appear in one query without colliding.
    """
    if dimension == "bundle":
        return "", "s.bundle", "s.bundle"
    if dimension == "year":
        return "", "YEAR(s.effective_start_date)", "YEAR(s.effective_start_date)"
    if dimension in ("author", "theme"):
        join = (
            f" JOIN `{table}_{dimension}` {alias}"
            f" ON {alias}.document_id = s.document_id"
        )
        if dimension == "author":
            return join, _author_key(alias), f"MIN({alias}.author)"
        return join, f"{alias}.{dimension}", f"{alias}.{dimension}"
    raise ValueError(f"dimension must be one of {_DISTRIBUTION_DIMENSIONS}")


def count_distinct_values(
    dimension: str,
    source_type: str | None = None,
    bundle: str | None = None,
    *,
    entity_type: str | None = None,
    title_contains: str | None = None,
    author: str | None = None,
    theme: str | None = None,
    theme_group: str | None = None,
    tag: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> int:
    """How many distinct values of ``dimension`` the filtered scope contains.

    Answers "how many authors are associated with Theme X" and "how many themes
    does Author Y publish in" — questions about the *facet*, where
    :func:`count_documents` counts documents and would report the wrong noun.

    Takes the same filter set as the other three readers, so it can be narrowed
    by anything they can be narrowed by.
    """
    if dimension not in _DISTRIBUTION_DIMENSIONS:
        raise ValueError(f"dimension must be one of {_DISTRIBUTION_DIMENSIONS}")
    table = _table()
    # Counting themes applies `theme_group` to the counted themes, not to the
    # documents — exactly as `distribution` does when grouping by theme. As a
    # document scope it would count every theme on any document that carries at
    # least one main theme, so "how many main themes are there" answered 30,
    # eight of which were Other themes.
    scope_group = None if dimension == "theme" else theme_group
    joins, clauses, params, _ = _catalog_filters(
        source_type, bundle, entity_type=entity_type, title_contains=title_contains,
        author=author, theme=theme, theme_group=scope_group, tag=tag,
        effective_from=effective_from, effective_to=effective_to,
    )
    # A dedicated alias: the scope filters may already join the same facet table
    # for a *different* purpose ("themes that Author X writes in" filters on
    # author and counts themes), and reusing their alias would count only the
    # rows that matched the filter.
    join, key, _label = _dimension_sql(dimension, table, "dv")
    if dimension == "year":
        clauses.append("s.effective_start_date IS NOT NULL")
    if dimension == "theme":
        placeholders = ", ".join(["%s"] * len(_NON_THEME_VALUES))
        clauses.append(f"dv.theme <> '' AND dv.theme NOT IN ({placeholders})")
        params.extend(_NON_THEME_VALUES)
        if theme_group:
            clauses.append("dv.theme_group = %s")
            params.append(theme_group)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT COUNT(DISTINCT {key}) AS n FROM `{table}` s{joins}{join}{where}"
    )
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0


def cross_distribution(
    first: str,
    second: str,
    *,
    source_type: str | None = None,
    bundle: str | None = None,
    entity_type: str | None = None,
    title_contains: str | None = None,
    author: str | None = None,
    theme: str | None = None,
    theme_group: str | None = None,
    tag: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    limit: int = 50,
) -> list[tuple[str, str, int]]:
    """Document counts grouped by two dimensions at once, largest first.

    "Which authors write about which themes" is one question, not a list of
    per-author breakdowns: it needs the pair as the grouping key. Returns
    (first value, second value, count) triples.

    A separate function rather than a second argument to :func:`distribution`
    because the row shape differs, and a caller that gets a triple where it
    expected a pair fails silently in the direction of a wrong number.

    Everything after the two dimensions is keyword-only. The dimensions being
    positional makes a third positional argument read as a third dimension, and
    it used to bind to ``source_type`` instead: ``cross_distribution("author",
    "theme", "year")`` filtered to a source type called "year", matched nothing
    and returned ``[]`` — a wrong answer that looks like an empty one. Now it
    raises ``TypeError``.
    """
    for name, dimension in (("first", first), ("second", second)):
        if dimension not in _DISTRIBUTION_DIMENSIONS:
            raise ValueError(
                f"cross_distribution {name} dimension must be one of "
                f"{_DISTRIBUTION_DIMENSIONS}, got {dimension!r}"
            )
    if first == second:
        raise ValueError("cross_distribution needs two different dimensions")
    table = _table()
    joins, clauses, params, _ = _catalog_filters(
        source_type, bundle, entity_type=entity_type, title_contains=title_contains,
        author=author, theme=theme, theme_group=theme_group, tag=tag,
        effective_from=effective_from, effective_to=effective_to,
    )
    join_a, key_a, label_a = _dimension_sql(first, table, "ga")
    join_b, key_b, label_b = _dimension_sql(second, table, "gb")
    for dimension, alias in ((first, "ga"), (second, "gb")):
        if dimension == "year":
            clauses.append("s.effective_start_date IS NOT NULL")
        elif dimension == "theme":
            placeholders = ", ".join(["%s"] * len(_NON_THEME_VALUES))
            clauses.append(
                f"{alias}.theme <> '' AND {alias}.theme NOT IN ({placeholders})"
            )
            params.extend(_NON_THEME_VALUES)
            if theme_group:
                # Same reasoning as `distribution`: grouping by theme restricts
                # the themes grouped on, not merely the documents scoped.
                clauses.append(f"{alias}.theme_group = %s")
                params.append(theme_group)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    capped = max(1, min(int(limit or 50), 500))
    sql = (
        f"SELECT {label_a} AS a, {label_b} AS b, COUNT(DISTINCT s.document_id) AS n "
        f"FROM `{table}` s{joins}{join_a}{join_b}{where} "
        f"GROUP BY {key_a}, {key_b} ORDER BY n DESC, a ASC, b ASC LIMIT {capped}"
    )
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [
        (str(row["a"]), str(row["b"]), int(row["n"]))
        for row in rows
        if row["a"] is not None and row["b"] is not None
    ]


def distinct_authors(*, limit: int = 2000) -> list[str]:
    """Every distinct author string in the catalog, ordered by name.

    Backs entity resolution's fuzzy author matching (see
    ``app.retrieval.structured.resolve``): the full set is fetched and scored in
    Python rather than narrowed by a SQL ``LIKE`` prefilter, because a genuine
    misspelling ("rishab negi") is not a substring of the stored name
    ("Rishabh Negi") — a prefilter tight enough to be cheap would systematically
    exclude exactly the fuzzy matches this exists to catch. Correct at this
    corpus's scale (low hundreds of distinct authors); revisit if that changes.
    ``limit`` clamps to [1, 5000]."""
    table = _table()
    capped = max(1, min(int(limit or 2000), 5000))
    sql = f"SELECT DISTINCT author FROM `{table}_author` ORDER BY author ASC LIMIT {capped}"
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return [row["author"] for row in cur.fetchall() if row["author"]]


# How long an inventory read is trusted. Short enough that an ingest adding a
# content type — or a newer document — shows up without a restart, long enough
# that the extra query is noise next to the LLM calls on the same request.
_INVENTORY_TTL_SECONDS = 600
_bundle_inventory: tuple[float, tuple[str, ...]] | None = None
_effective_date_range: tuple[float, tuple[str | None, str | None]] | None = None

# The corpus revision is read on every cached query rather than on the
# occasional catalog answer, and it decides whether a cached answer may be
# served at all — so it is trusted for far less time than the inventory reads
# above. This is the ceiling on how long a re-indexed or deleted document can
# still be answered from cache; below the LLM latency it replaces, and one
# cheap aggregate either way.
_CORPUS_REVISION_TTL_SECONDS = 30
_corpus_revision: tuple[float, str | None] | None = None


def corpus_revision(*, refresh: bool = False) -> str | None:
    """An opaque marker of the indexed corpus's current state, or None.

    ``MAX(indexed_at)`` moves whenever a document is actually re-chunked and
    re-indexed (``state.upsert`` COALESCEs the column, so a fingerprint-only
    refresh deliberately does not move it), and the row count moves whenever a
    document is added or deleted. Together they change for every ingestion that
    could alter what retrieval returns, and for nothing else — which is exactly
    the condition under which a cached answer stops being valid.

    ``None`` means *unknown*, and callers must treat it as "do not use the
    cache". Failing open to a placeholder would be the one unsafe direction
    here: it would let a MySQL outage pin the partition key while ingestion
    carried on changing the corpus underneath it.
    """
    global _corpus_revision
    now = time.monotonic()
    if (
        not refresh
        and _corpus_revision is not None
        and now - _corpus_revision[0] < _CORPUS_REVISION_TTL_SECONDS
    ):
        return _corpus_revision[1]

    table = _table()
    sql = (
        f"SELECT MAX(indexed_at) AS latest, COUNT(*) AS documents FROM `{table}`"
    )
    try:
        with mysql_connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        row = rows[0] if rows else {}
        latest = row.get("latest")
        found = (
            f"{latest.isoformat() if isinstance(latest, datetime) else (latest or 'never')}"
            f"|{int(row.get('documents') or 0)}"
        )
    except Exception:
        logger.warning(
            "Corpus revision lookup failed; the semantic cache will be bypassed "
            "until it recovers.", exc_info=True,
        )
        return None
    _corpus_revision = (now, found)
    return found


def available_bundles(*, refresh: bool = False) -> tuple[str, ...]:
    """The bundles this catalog actually holds content documents for.

    ``DEFAULT_BUNDLES`` is the list ingestion *attempts*, not what a given
    deployment ended up with — a source that has no press releases, or a bundle
    that was never crawled, leaves a content type that is advertised to the LLM
    but can only ever match zero rows. Asking the catalog closes that gap.

    Scoped to website nodes, matching what the entity registry considers a
    content document (see ``app.retrieval.structured.entities``); PDF
    attachments reuse their parent's bundle and would otherwise imply a type
    exists as browsable content when it does not.

    Fails open with an empty tuple: callers must read that as "unknown", never
    as "the catalog is empty", or a MySQL blip would retract the whole
    vocabulary.

    Cached here rather than in the callers so the prompt builder and the query
    guard share one inventory (and one query) per request."""
    global _bundle_inventory
    now = time.monotonic()
    if (
        not refresh
        and _bundle_inventory is not None
        and now - _bundle_inventory[0] < _INVENTORY_TTL_SECONDS
    ):
        return _bundle_inventory[1]
    table = _table()
    sql = (
        f"SELECT DISTINCT bundle FROM `{table}` "
        "WHERE bundle IS NOT NULL AND source_type = 'website' AND entity_type = 'node'"
    )
    try:
        with mysql_connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
            found = tuple(sorted(r["bundle"] for r in cur.fetchall() if r["bundle"]))
    except Exception:
        logger.warning("Bundle inventory lookup failed; treating it as unknown.",
                       exc_info=True)
        return ()
    _bundle_inventory = (now, found)
    return found


def _as_iso_date(value: Any) -> str | None:
    """A ``effective_start_date`` bound as a bare ISO date, or None when unusable."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value or "")[:10]
    return text or None


def effective_date_range(*, refresh: bool = False) -> tuple[str | None, str | None]:
    """The oldest and newest effective dates the catalog holds, as ISO dates.

    The date-extracting prompts anchor relative expressions to *today*, which is
    right for reading the user ("last six months" means the last six months) and
    wrong for the corpus: an archive whose newest document is two years old
    answers "what changed this year" with a confident zero that reads as a fact
    about the world rather than about the catalog. Naming the real span lets the
    model scope to a period that can actually match — the same gap
    ``available_bundles`` closes for content types.

    Unscoped, unlike ``available_bundles``: a bundle is a browsable-type concept
    that only applies to website nodes, whereas any indexed document can carry a
    date and be retrieved by one.

    Fails open with ``(None, None)``, which callers must read as "unknown" — a
    MySQL blip must not tell the model the catalog covers nothing."""
    global _effective_date_range
    now = time.monotonic()
    if (
        not refresh
        and _effective_date_range is not None
        and now - _effective_date_range[0] < _INVENTORY_TTL_SECONDS
    ):
        return _effective_date_range[1]
    table = _table()
    sql = (
        f"SELECT MIN(effective_start_date) AS lo, MAX(effective_start_date) AS hi FROM `{table}` "
        "WHERE effective_start_date IS NOT NULL"
    )
    try:
        with mysql_connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        row = rows[0] if rows else {}
        found = (_as_iso_date(row.get("lo")), _as_iso_date(row.get("hi")))
    except Exception:
        logger.warning("Published-range lookup failed; treating it as unknown.",
                       exc_info=True)
        return (None, None)
    _effective_date_range = (now, found)
    return found


#: How many documents a semantic-path theme scope may name. Generous, because
#: this is a narrowing filter over a top-k pull rather than a listing: naming
#: fewer documents than a theme holds costs a little recall, and the retriever
#: drops the facet entirely if the scope comes back empty. Bounded all the same
#: — an unbounded MatchAny reaching Qdrant is the failure this avoids.
THEME_SCOPE_DOC_CAP = 2000


def theme_document_ids(
    theme: str, *, limit: int = THEME_SCOPE_DOC_CAP
) -> list[str] | None:
    """Every document in ``theme``'s scope — the theme and its descendants.

    The membership half of "MySQL decides who is in the theme, Qdrant ranks
    what they say". The semantic path used to answer this from the chunk
    payload's ``categories``, which is a flat list of names: it could not
    express the hierarchy (a chunk tagged only "Energy Access" did not match a
    scope of "Energy", though the catalog said it should), and it drifted
    whenever a theme was renamed or reclassified in MySQL without the payloads
    being rewritten. Two representations of the same fact, disagreeing.

    Unlike :func:`document_ids_in_scope` this does **not** restrict to website
    nodes. An attachment inherits its parent page's themes precisely so
    theme-scoped retrieval reaches the PDF's text, and excluding attachments
    here would put that content out of reach of the very filter meant to select
    it.

    Returns ``None`` when the query fails, which the caller must not confuse
    with ``[]``: no rows means the theme genuinely has no documents, while a
    failure means membership is unknown and the scope should be abandoned rather
    than applied as "nothing matches".
    """
    name = (theme or "").strip()
    if not name:
        return []
    table = _table()
    clause, params = _theme_scope_clause("c", name)
    capped = max(1, min(int(limit or THEME_SCOPE_DOC_CAP), 10_000))
    # Recency-ordered so a truncated scope is the newest slice of the theme
    # rather than an arbitrary one.
    sql = (
        f"SELECT DISTINCT s.document_id, s.effective_start_date"
        f" FROM `{table}` s"
        f" JOIN `{table}_theme` c ON c.document_id = s.document_id"
        f" WHERE {clause}"
        f" ORDER BY s.effective_start_date DESC, s.document_id ASC LIMIT {capped}"
    )
    try:
        with mysql_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            ids = [row["document_id"] for row in cur.fetchall()]
    except Exception:
        logger.warning("Theme scope lookup failed for %r.", name, exc_info=True)
        return None
    if len(ids) >= capped:
        logger.info(
            "Theme scope for %r hit the %d-document cap; the semantic pull sees "
            "the most recent slice only.", name, capped,
        )
    return ids


def theme_vocabulary(*, limit: int = 500) -> list[dict[str, Any]]:
    """The theme vocabulary as the catalog knows it — one row per distinct theme
    with its hierarchy and Main/Other group, ordered by name.

    ``documents_theme`` is the source of truth for what themes exist: it stores
    the theme *name* alongside the ``theme_type`` / ``parent`` / ``theme_group``
    that ``app.catalog.theme_taxonomy`` materialized at ingest. Nothing here
    reads app/theme_structure.json — that file shapes the columns during ingestion, and the
    columns answer queries afterwards.

    A theme carrying more than one hierarchy variant (the theme map changed between
    ingests, so some rows are stale) collapses to the variant the most documents
    agree on, ties broken by name, so callers always see exactly one row per
    theme. ``documents`` is that variant's document count. ``limit`` clamps to
    [1, 2000] and applies to themes, not rows.
    """
    table = _table()
    capped = max(1, min(int(limit or 500), 2000))
    placeholders = ", ".join(["%s"] * len(_NON_THEME_VALUES))
    sql = (
        f"SELECT theme, theme_type, parent, theme_group, theme_path, depth,"
        f" COUNT(DISTINCT document_id) AS documents"
        f" FROM `{table}_theme`"
        f" WHERE theme <> '' AND theme NOT IN ({placeholders})"
        f" GROUP BY theme, theme_type, parent, theme_group, theme_path, depth"
        f" ORDER BY theme ASC, documents DESC, theme_type ASC"
    )
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, _NON_THEME_VALUES)
        rows = cur.fetchall()
    vocabulary: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row["theme"]:
            continue
        # ORDER BY put the best-supported variant first, so the first win sticks.
        vocabulary.setdefault(
            row["theme"],
            {
                "theme": row["theme"],
                "theme_type": row["theme_type"],
                "parent": row["parent"],
                "theme_group": row["theme_group"],
                "theme_path": row["theme_path"],
                "depth": int(row["depth"] or 1),
                "documents": int(row["documents"] or 0),
            },
        )
        if len(vocabulary) >= capped:
            break
    return list(vocabulary.values())


def find_tag(name: str) -> str | None:
    """The stored casing of ``name`` if any document carries that tag, else None.

    A targeted lookup rather than a vocabulary scan, because tags are matched
    **exactly** (they are a long-tail freeform set — see
    docs/database-retrieval-redesign.md §3, and `filters._resolve_tag_name`).
    Loading every tag to compare in Python would also silently truncate: this
    corpus already has more distinct tags than a sane row cap.
    ``idx_val`` on the tag column makes this an index hit."""
    if not name or not name.strip():
        return None
    table = _table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT tag FROM `{table}_tag` WHERE tag = %s LIMIT 1", (name.strip(),)
        )
        row = cur.fetchone()
        if row:
            return row["tag"]
        # Only fall back to a case-insensitive scan when the exact form missed —
        # LOWER() on the column cannot use the index.
        cur.execute(
            f"SELECT tag FROM `{table}_tag` WHERE LOWER(tag) = LOWER(%s) LIMIT 1",
            (name.strip(),),
        )
        row = cur.fetchone()
    return row["tag"] if row else None


def distinct_tags(*, limit: int = 5000) -> list[str]:
    """Every distinct tag name in ``documents_tag``, ordered by name.

    Diagnostics only — matching a tag goes through :func:`find_tag`, which does
    not have to load the vocabulary. ``limit`` clamps to [1, 10000]."""
    table = _table()
    capped = max(1, min(int(limit or 5000), 10000))
    sql = f"SELECT DISTINCT tag FROM `{table}_tag` ORDER BY tag ASC LIMIT {capped}"
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return [row["tag"] for row in cur.fetchall() if row["tag"]]


def distinct_themes(*, limit: int = 500) -> list[str]:
    """Every distinct theme name in ``documents_theme``, ordered by name.

    A names-only view of :func:`theme_vocabulary`, so it inherits the same junk
    filtering and one-row-per-theme guarantee."""
    return [row["theme"] for row in theme_vocabulary(limit=limit)]


# --------------------------------------------------------------------------- #
# Id-scoped reads for retrieval (scoped summarization, attachment
# supplementation). Website nodes (source_type='website', entity_type='node')
# are baked in as the catalog of record; DB errors fail open to empty results
# so callers degrade to the plain semantic pipeline.
# --------------------------------------------------------------------------- #

def document_ids_in_scope(
    *,
    bundle: str | None = None,
    theme: str | None = None,
    tag: str | None = None,
    author: str | None = None,
    title_contains: str | None = None,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    limit: int = 150,
) -> list[str]:
    """Document ids matching a metadata scope, most recent first.

    The id-set selection behind catalog-scoped retrieval: MySQL decides set
    membership, Qdrant ranks content within it. Scoping matches
    :func:`_catalog_filters` — ``theme`` covers its descendants too, ``tag`` by
    exact name. ``limit`` clamps to [1, 300] — honest truncation beats an
    unbounded MatchAny downstream.
    """
    table = _table()
    joins: list[str] = []
    clauses = ["s.source_type = %s", "s.entity_type = %s"]
    params: list[Any] = ["website", "node"]
    distinct = False
    if bundle is not None:
        clauses.append("s.bundle = %s")
        params.append(bundle)
    if title_contains:
        clauses.append("s.title LIKE %s")
        params.append(_like(title_contains))
    if effective_from is not None:
        clauses.append("s.effective_start_date >= %s")
        params.append(effective_from)
    if effective_to is not None:
        clauses.append("s.effective_start_date < %s")
        params.append(effective_to)
    if author:
        joins.append(f" JOIN `{table}_author` a ON a.document_id = s.document_id")
        clauses.append("a.author LIKE %s")
        params.append(_like(author))
        distinct = True
    if theme:
        joins.append(f" JOIN `{table}_theme` c ON c.document_id = s.document_id")
        clause, args = _theme_scope_clause("c", theme)
        clauses.append(clause)
        params.extend(args)
        distinct = True
    if tag:
        joins.append(f" JOIN `{table}_tag` t ON t.document_id = s.document_id")
        clauses.append("t.tag = %s")
        params.append(tag)
        distinct = True

    # effective_start_date is selected alongside the id: MySQL rejects DISTINCT with
    # an ORDER BY column that is not in the select list.
    select = "SELECT DISTINCT" if distinct else "SELECT"
    capped = max(1, min(int(limit or 150), 300))
    sql = (
        f"{select} s.document_id, s.effective_start_date FROM `{table}` s{''.join(joins)}"
        f" WHERE {' AND '.join(clauses)}"
        f" ORDER BY s.effective_start_date DESC, s.document_id ASC LIMIT {capped}"
    )
    try:
        with mysql_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [row["document_id"] for row in cur.fetchall()]
    except Exception:
        logger.warning("Catalog id-scope query failed.", exc_info=True)
        return []


def abstracts_for(document_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Ingest-time abstracts plus display fields, keyed by document_id.

    Joins the documents table to the enrichment cache on ``content_hash`` —
    the cache is keyed by content rather than by document (see
    :mod:`app.catalog.enrichment`), so this is where the two are brought back
    together. Documents with no cached abstract are simply absent from the
    result and the caller falls back to a lead chunk.

    Deliberately does **not** filter on the enrichment version. A version
    mismatch means the abstract predates the current prompt, not that it is
    wrong about the document; serving it still beats the fallback, and the next
    sweep or backfill refreshes it.
    """
    ids = [d for d in document_ids if d]
    if not ids:
        return {}
    table = _table()
    placeholders = ", ".join(["%s"] * len(ids))
    sql = (
        f"SELECT s.document_id, s.title, s.url, s.effective_start_date, e.abstract"
        f" FROM `{table}` s"
        f" JOIN `{table}_enrichment` e ON e.content_hash = s.content_hash"
        f" WHERE s.document_id IN ({placeholders}) AND e.abstract IS NOT NULL"
    )
    try:
        with mysql_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(ids))
            rows = cur.fetchall()
    except Exception:
        # Includes the enrichment table not existing yet — an un-enriched
        # deployment must keep summarizing from lead chunks as before.
        logger.warning("Catalog abstract lookup failed.", exc_info=True)
        return {}
    # A blank abstract counts as absent, not as an empty summary: otherwise the
    # document would be preferred over its own lead chunk and then dropped for
    # having no text, silently vanishing from the scope.
    return {
        row["document_id"]: {
            "abstract": row["abstract"],
            "title": row["title"],
            "url": row["url"],
            "effective_start_date": row["effective_start_date"],
        }
        for row in rows
        if (row["abstract"] or "").strip()
    }


def attachments_for(document_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Attachment rows keyed by document_id — the website→PDF supplementation
    join. Each row: {file_uuid, origin, url, filename}."""
    ids = [d for d in document_ids if d]
    if not ids:
        return {}
    table = _table()
    placeholders = ", ".join(["%s"] * len(ids))
    sql = (
        f"SELECT document_id, file_uuid, origin, url, filename"
        f" FROM `{table}_attachment` WHERE document_id IN ({placeholders})"
    )
    try:
        with mysql_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(ids))
            rows = cur.fetchall()
    except Exception:
        logger.warning("Catalog attachment lookup failed.", exc_info=True)
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row["document_id"], []).append(
            {
                "file_uuid": row["file_uuid"],
                "origin": row["origin"],
                "url": row["url"],
                "filename": row["filename"],
            }
        )
    return out
