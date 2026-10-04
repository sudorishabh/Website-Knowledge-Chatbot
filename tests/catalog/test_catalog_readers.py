"""Unit tests for the retrieval-side MySQL catalog readers.

Covers the id-set scope selection (joins, params, DISTINCT, clamping), author
disambiguation lookup, and the attachment join. All SQL runs against scripted
fakes; no MySQL, Qdrant, or LLM needed.
"""

from __future__ import annotations

from datetime import datetime

from app.catalog import queries as catalog


class _FakeCursor:
    def __init__(self, fetchall_results: list | None = None):
        self.fetchall_results = list(fetchall_results or [])
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> int:
        self.calls.append((" ".join(sql.split()), params))
        return 1

    def fetchall(self):
        return self.fetchall_results.pop(0) if self.fetchall_results else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch, cursor):
    monkeypatch.setattr(catalog, "mysql_connection", lambda: _FakeConn(cursor))


# --------------------------------------------------------------------------- #
# document_ids_in_scope — membership selection for id-scoped retrieval.
# --------------------------------------------------------------------------- #

def test_ids_in_scope_bakes_in_website_nodes(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"document_id": "d1"}, {"document_id": "d2"}]])
    _patch(monkeypatch, cursor)

    ids = catalog.document_ids_in_scope(bundle="news")

    assert ids == ["d1", "d2"]
    sql, params = cursor.calls[0]
    assert "s.source_type = %s" in sql and "s.entity_type = %s" in sql
    assert "ORDER BY s.effective_start_date DESC" in sql and "LIMIT 150" in sql
    assert "DISTINCT" not in sql  # no facet join -> no duplicate rows
    assert params == ("website", "node", "news")


def test_ids_in_scope_theme_join_distinct_and_param_order(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"document_id": "d1"}]])
    _patch(monkeypatch, cursor)

    catalog.document_ids_in_scope(
        theme="Environment",
        author="Sharma",
        effective_from=datetime(2024, 1, 1),
        limit=30,
    )

    sql, params = cursor.calls[0]
    assert sql.startswith("SELECT DISTINCT s.document_id, s.effective_start_date")
    assert "_theme` c" in sql and "c.theme = %s" in sql
    assert "_author` a" in sql and "a.author LIKE %s" in sql
    assert "LIMIT 30" in sql
    assert params == (
        "website", "node", datetime(2024, 1, 1), "%Sharma%", "Environment",
    )


def test_ids_in_scope_theme_and_tag_join_independently(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[]])
    _patch(monkeypatch, cursor)

    catalog.document_ids_in_scope(theme="Climate", tag="solar")

    sql, _ = cursor.calls[0]
    # Theme and tag are independent joins, so a document must satisfy both.
    assert "_theme` c" in sql and "_tag` t" in sql


def test_ids_in_scope_clamps_limit(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[], []])
    _patch(monkeypatch, cursor)

    catalog.document_ids_in_scope(limit=9999)
    catalog.document_ids_in_scope(limit=-3)

    assert "LIMIT 300" in cursor.calls[0][0]
    assert "LIMIT 1" in cursor.calls[1][0]


def test_ids_in_scope_fails_open(monkeypatch):
    def boom():
        raise RuntimeError("mysql down")

    monkeypatch.setattr(catalog, "mysql_connection", boom)
    assert catalog.document_ids_in_scope(bundle="news") == []


# --------------------------------------------------------------------------- #
# attachments_for — website -> attached-PDF join.
# --------------------------------------------------------------------------- #

def test_attachments_for_keys_rows_by_document(monkeypatch):
    rows = [
        {"document_id": "d1", "file_uuid": "f1", "origin": "attachment",
         "url": "https://t/f1.pdf", "filename": "f1.pdf"},
        {"document_id": "d1", "file_uuid": "f2", "origin": "inbody",
         "url": "https://t/f2.pdf", "filename": "f2.pdf"},
        {"document_id": "d2", "file_uuid": "f3", "origin": "attachment",
         "url": None, "filename": None},
    ]
    cursor = _FakeCursor(fetchall_results=[rows])
    _patch(monkeypatch, cursor)

    out = catalog.attachments_for(["d1", "d2"])

    assert [a["file_uuid"] for a in out["d1"]] == ["f1", "f2"]
    assert out["d2"][0]["origin"] == "attachment"
    sql, params = cursor.calls[0]
    assert "_attachment` WHERE document_id IN (%s, %s)" in sql
    assert params == ("d1", "d2")


def test_attachments_for_empty_ids_skip_query(monkeypatch):
    cursor = _FakeCursor()
    _patch(monkeypatch, cursor)
    assert catalog.attachments_for([]) == {}
    assert catalog.attachments_for(["", None]) == {}
    assert cursor.calls == []


def test_attachments_for_fails_open(monkeypatch):
    def boom():
        raise RuntimeError("mysql down")

    monkeypatch.setattr(catalog, "mysql_connection", boom)
    assert catalog.attachments_for(["d1"]) == {}


# --------------------------------------------------------------------------- #
# effective_date_range — the span the date-extracting prompts are told about.
# --------------------------------------------------------------------------- #

def _no_cache(monkeypatch):
    """Drop the TTL cache so each case queries. The cache is process-global, so
    a leftover entry would otherwise answer the next test."""
    monkeypatch.setattr(catalog, "_effective_date_range", None)


def test_effective_date_range_returns_iso_dates(monkeypatch):
    _no_cache(monkeypatch)
    cursor = _FakeCursor(fetchall_results=[[
        {"lo": datetime(2011, 3, 4, 9, 30), "hi": datetime(2024, 11, 30, 18, 5)}
    ]])
    _patch(monkeypatch, cursor)

    assert catalog.effective_date_range() == ("2011-03-04", "2024-11-30")
    sql, _ = cursor.calls[0]
    assert "MIN(effective_start_date) AS lo, MAX(effective_start_date) AS hi" in sql
    assert "effective_start_date IS NOT NULL" in sql


def test_effective_date_range_spans_every_source_type(monkeypatch):
    """Unlike the bundle inventory, it is not scoped to website nodes — any
    indexed document can carry a date and be retrieved by one."""
    _no_cache(monkeypatch)
    cursor = _FakeCursor(fetchall_results=[[{"lo": None, "hi": None}]])
    _patch(monkeypatch, cursor)

    catalog.effective_date_range()

    sql, _ = cursor.calls[0]
    assert "source_type" not in sql
    assert "entity_type" not in sql


def test_effective_date_range_reads_an_empty_catalog_as_unknown(monkeypatch):
    _no_cache(monkeypatch)
    _patch(monkeypatch, _FakeCursor(fetchall_results=[[]]))
    assert catalog.effective_date_range() == (None, None)


def test_effective_date_range_fails_open(monkeypatch):
    """A MySQL blip must not tell the prompt the catalog covers nothing."""
    _no_cache(monkeypatch)

    def boom():
        raise RuntimeError("mysql down")

    monkeypatch.setattr(catalog, "mysql_connection", boom)
    assert catalog.effective_date_range() == (None, None)


def test_effective_date_range_is_cached_between_calls(monkeypatch):
    _no_cache(monkeypatch)
    cursor = _FakeCursor(fetchall_results=[[
        {"lo": datetime(2011, 3, 4), "hi": datetime(2024, 11, 30)}
    ]])
    _patch(monkeypatch, cursor)

    assert catalog.effective_date_range() == catalog.effective_date_range()
    assert len(cursor.calls) == 1
    assert catalog.effective_date_range(refresh=True) != () and len(cursor.calls) == 2


# --------------------------------------------------------------------------- #
# facets_for — authors and top-level themes for documents an answer shows.
# --------------------------------------------------------------------------- #

def test_facets_for_groups_authors_and_primary_themes_by_document(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[
        [{"document_id": "d1", "author": "A"}, {"document_id": "d1", "author": "B"},
         {"document_id": "d2", "author": "C"}],
        [{"document_id": "d1", "theme": "Energy"}],
    ])
    _patch(monkeypatch, cursor)

    facets = catalog.facets_for(["d1", "d2", ""])

    assert facets == {
        "d1": {"authors": ["A", "B"], "themes": ["Energy"]},
        "d2": {"authors": ["C"], "themes": []},
    }
    author_sql, author_params = cursor.calls[0]
    assert "_author`" in author_sql and "ORDER BY author" in author_sql
    assert author_params == ("d1", "d2")
    theme_sql, theme_params = cursor.calls[1]
    # Sub-themes are not shown at question time; neither are boolean artefacts.
    assert "theme_type = 'primary'" in theme_sql
    assert theme_params == ("d1", "d2", "False", "True")


def test_facets_for_nothing_asks_nothing(monkeypatch):
    cursor = _FakeCursor()
    _patch(monkeypatch, cursor)
    assert catalog.facets_for([]) == {}
    assert cursor.calls == []


def test_facets_for_fails_open(monkeypatch):
    def boom():
        raise RuntimeError("mysql down")

    monkeypatch.setattr(catalog, "mysql_connection", boom)
    assert catalog.facets_for(["d1"]) == {}
