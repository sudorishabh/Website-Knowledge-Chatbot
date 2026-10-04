"""state.replace_authors — rewrite one document's author rows and nothing else."""
from __future__ import annotations

from app.catalog import state

TABLE = "documents"


class _Cursor:
    def __init__(self, exists: bool):
        self.exists = exists
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def executemany(self, sql, rows):
        self.calls.append((" ".join(sql.split()), rows))

    def fetchone(self):
        return {"1": 1} if self.exists else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch, exists):
    cursor = _Cursor(exists)
    conn = _Conn(cursor)
    monkeypatch.setattr(state, "mysql_connection", lambda: conn)
    monkeypatch.setattr(state, "_table", lambda: TABLE)
    return cursor, conn


def test_the_author_rows_are_replaced_with_their_normalised_forms(monkeypatch):
    cursor, conn = _patch(monkeypatch, exists=True)

    assert state.replace_authors("d1", ["Sehgal Meena", "Dr Pia Sethi"]) is True

    statements = [sql for sql, _ in cursor.calls]
    assert f"DELETE FROM `{TABLE}_author` WHERE document_id = %s" in statements
    [(_, rows)] = [c for c in cursor.calls if c[0].startswith("INSERT")]
    assert rows == [("d1", "Sehgal Meena", "sehgal meena"),
                    ("d1", "Dr Pia Sethi", "pia sethi")]
    # Nothing but the author table is written.
    assert all(f"`{TABLE}_author`" in sql for sql in statements[1:])
    assert conn.commits == 1


def test_no_authors_clears_the_rows(monkeypatch):
    cursor, _ = _patch(monkeypatch, exists=True)
    assert state.replace_authors("d1", []) is True
    assert not [c for c in cursor.calls if c[0].startswith("INSERT")]


def test_an_unknown_document_is_left_alone(monkeypatch):
    cursor, conn = _patch(monkeypatch, exists=False)
    assert state.replace_authors("nope", ["A"]) is False
    assert len(cursor.calls) == 1 and conn.commits == 0
