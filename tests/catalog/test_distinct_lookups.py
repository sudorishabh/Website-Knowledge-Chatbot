"""Unit tests for the distinct-name catalog readers backing entity resolution
(app.retrieval.structured.resolve). All SQL runs against a scripted fake
cursor; no MySQL needed.
"""

from __future__ import annotations

from app.catalog import queries as state


class _FakeCursor:
    def __init__(self, fetchall_results: list | None = None,
                 fetchone_results: list | None = None):
        self.fetchall_results = list(fetchall_results or [])
        self.fetchone_results = list(fetchone_results or [])
        self.calls: list[tuple[str, object]] = []

    def fetchone(self):
        return self.fetchone_results.pop(0) if self.fetchone_results else None

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
    monkeypatch.setattr(state, "mysql_connection", lambda: _FakeConn(cursor))


def test_distinct_authors_filters_blank_and_returns_names(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[
        {"author": "Rishabh Negi"}, {"author": ""}, {"author": "A Sharma"},
    ]])
    _patch(monkeypatch, cursor)

    names = state.distinct_authors()
    assert names == ["Rishabh Negi", "A Sharma"]
    sql, params = cursor.calls[0]
    assert sql.startswith("SELECT DISTINCT author FROM")
    assert "ORDER BY author ASC" in sql and "LIMIT 2000" in sql
    assert params is None


def test_distinct_authors_clamps_limit(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[], []])
    _patch(monkeypatch, cursor)

    state.distinct_authors(limit=99999)
    state.distinct_authors(limit=-3)

    assert "LIMIT 5000" in cursor.calls[0][0]
    assert "LIMIT 1" in cursor.calls[1][0]


def test_distinct_tags_filters_blank_and_returns_names(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[
        {"tag": "Solid waste"}, {"tag": ""}, {"tag": "Urban waste"},
    ]])
    _patch(monkeypatch, cursor)

    assert state.distinct_tags() == ["Solid waste", "Urban waste"]
    sql, _ = cursor.calls[0]
    assert sql.startswith("SELECT DISTINCT tag FROM")
    assert "_tag` ORDER BY tag ASC LIMIT 5000" in sql


def test_distinct_tags_clamps_limit(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[], []])
    _patch(monkeypatch, cursor)

    state.distinct_tags(limit=99999)
    state.distinct_tags(limit=-3)

    assert "LIMIT 10000" in cursor.calls[0][0]
    assert "LIMIT 1" in cursor.calls[1][0]


def test_find_tag_is_a_targeted_lookup_not_a_vocabulary_scan(monkeypatch):
    """Loading every tag to compare in Python truncates: this corpus has more
    distinct tags (2,364) than any sane row cap."""
    cursor = _FakeCursor(fetchone_results=[{"tag": "Waste management"}])
    _patch(monkeypatch, cursor)

    assert state.find_tag("waste management") == "Waste management"
    sql, params = cursor.calls[0]
    assert "WHERE tag = %s LIMIT 1" in sql  # index hit, no LOWER() on the column
    assert params == ("waste management",)


def test_find_tag_falls_back_to_case_insensitive_only_on_a_miss(monkeypatch):
    cursor = _FakeCursor(fetchone_results=[None, {"tag": "Waste management"}])
    _patch(monkeypatch, cursor)

    assert state.find_tag("WASTE MANAGEMENT") == "Waste management"
    assert len(cursor.calls) == 2
    assert "LOWER(tag) = LOWER(%s)" in cursor.calls[1][0]


def test_find_tag_missing_returns_none(monkeypatch):
    cursor = _FakeCursor(fetchone_results=[None, None])
    _patch(monkeypatch, cursor)
    assert state.find_tag("zzznotatag") is None


def test_find_tag_blank_issues_no_query(monkeypatch):
    cursor = _FakeCursor()
    _patch(monkeypatch, cursor)
    assert state.find_tag("") is None and state.find_tag("   ") is None
    assert cursor.calls == []


def test_a_list_by_several_author_strings_matches_each_exactly(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[]])
    _patch(monkeypatch, cursor)

    state.list_documents(source_type="website", entity_type="node",
                         authors=["Dr Manish Kumar Shrivastava", "Shrivastava M  K"], limit=5)
    sql, params = cursor.calls[0]
    assert sql.startswith("SELECT DISTINCT s.* FROM")
    assert "n.author IN (%s, %s)" in sql and "LIKE" not in sql
    assert list(params) == ["website", "node", "Dr Manish Kumar Shrivastava", "Shrivastava M  K"]
