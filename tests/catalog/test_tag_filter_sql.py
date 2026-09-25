"""Unit tests for the tag scope in the catalog SQL layer.

Covers the `documents_tag` join in isolation and, most importantly, that a tag
filter and a theme filter combine as two independent joins (AND) rather than
one merged condition. All SQL runs against a scripted fake cursor; no MySQL.
"""

from __future__ import annotations

from app.catalog import queries as state


class _FakeCursor:
    def __init__(self, fetchall_results: list | None = None,
                 fetchone_results: list | None = None):
        self.fetchall_results = list(fetchall_results or [])
        self.fetchone_results = list(fetchone_results or [])
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> int:
        self.calls.append((" ".join(sql.split()), params))
        return 1

    def fetchall(self):
        return self.fetchall_results.pop(0) if self.fetchall_results else []

    def fetchone(self):
        return self.fetchone_results.pop(0) if self.fetchone_results else None

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
    monkeypatch.setattr(state, "mysql_connection", lambda: _FakeConn(cursor))


def test_count_by_tag_joins_the_tag_facet(monkeypatch):
    cursor = _FakeCursor(fetchone_results=[{"n": 4}])
    _patch(monkeypatch, cursor)

    total = state.count_documents(
        source_type="website", bundle="news", tag="Waste management"
    )
    assert total == 4
    sql, params = cursor.calls[0]
    assert "COUNT(DISTINCT s.document_id)" in sql
    assert "_tag` t" in sql and "t.tag = %s" in sql
    assert params == ("website", "news", "Waste management")


def test_tag_and_theme_are_two_separate_joins(monkeypatch):
    """The regression that matters: theme and tag must AND — a document has to
    satisfy both filters, not either."""
    cursor = _FakeCursor(fetchone_results=[{"n": 1}])
    _patch(monkeypatch, cursor)

    state.count_documents(source_type="website", theme="Energy", tag="solar")
    sql, params = cursor.calls[0]
    assert "_theme` c" in sql and "c.theme = %s" in sql
    assert "_tag` t" in sql and "t.tag = %s" in sql
    assert sql.count("JOIN") == 2  # two independent joins
    assert params == ("website", "Energy", "solar")


def test_list_documents_applies_tag_scope(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[]])
    _patch(monkeypatch, cursor)

    state.list_documents(source_type="website", tag="solar")
    sql, params = cursor.calls[0]
    assert "_tag` t" in sql and "t.tag = %s" in sql
    assert "DISTINCT" in sql
    assert params == ("website", "solar")


def test_distribution_scoped_by_tag_is_not_a_group_dimension(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"k": "report", "n": 2}]])
    _patch(monkeypatch, cursor)

    rows = state.distribution("bundle", source_type="website", tag="solar")
    assert rows == [("report", 2)]
    sql, params = cursor.calls[0]
    assert "_tag` t" in sql and "t.tag = %s" in sql
    assert params == ("website", "solar")

    try:
        state.distribution("tag")
    except ValueError:
        pass
    else:
        raise AssertionError("tag is a scope filter, not a groupable dimension")


def test_no_tag_filter_omits_the_join(monkeypatch):
    cursor = _FakeCursor(fetchone_results=[{"n": 9}])
    _patch(monkeypatch, cursor)

    state.count_documents(source_type="website")
    sql, _ = cursor.calls[0]
    assert "_tag`" not in sql
