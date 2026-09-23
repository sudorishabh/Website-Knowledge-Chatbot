"""The catalog's URL lookup behind web retrieval's internal-first check.

A URL found on the web is looked up under every spelling the catalog may have
stored for it, in both places an address lives (a document's own url, an
attachment's file url), and only documents with indexed content count. A read
failure is reported as unknown (None), distinct from "none ingested" ({}),
because only the second may be recorded as gaps in the corpus.
"""
from __future__ import annotations

from app.catalog import queries as catalog


class _FakeCursor:
    def __init__(self, results: list[list[dict]]):
        self.results = list(results)
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.results.pop(0) if self.results else []

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


def _serve(monkeypatch, *results):
    cursor = _FakeCursor(list(results))
    monkeypatch.setattr(catalog, "mysql_connection", lambda: _FakeConn(cursor))
    return cursor


ARTICLE = "https://www.teriin.org/article/bridging-gap"
PDF = "https://www.teriin.org/sites/default/files/2023-08/Baseline%20Report.pdf"


def test_every_spelling_the_catalog_may_hold_is_looked_up():
    spellings = catalog._url_spellings(ARTICLE)
    for variant in ("https://teriin.org/article/bridging-gap",
                    "http://www.teriin.org/article/bridging-gap/",
                    "https://www.teriin.org/article/bridging-gap"):
        assert variant in spellings


def test_an_escaped_file_name_is_also_looked_up_unescaped():
    spellings = catalog._url_spellings(PDF)
    assert "https://teriin.org/sites/default/files/2023-08/Baseline Report.pdf" in spellings
    assert "https://teriin.org/sites/default/files/2023-08/Baseline%20Report.pdf" in spellings


def test_page_and_attachment_matches_map_back_to_the_url_given(monkeypatch):
    cursor = _serve(
        monkeypatch,
        [{"document_id": "node-1", "url": "https://teriin.org/article/bridging-gap"}],
        [{"document_id": "file-9",
          "url": "https://teriin.org/sites/default/files/2023-08/Baseline Report.pdf"}],
    )
    found = catalog.indexed_documents_for_urls([ARTICLE, PDF, "https://www.teriin.org/user/1"])
    assert found == {ARTICLE: ["node-1"], PDF: ["file-9"]}
    page_sql, file_sql = (call[0] for call in cursor.calls)
    assert "content_state IS NULL" in page_sql
    assert "_attachment` a JOIN" in file_sql and "d.content_state IS NULL" in file_sql


def test_a_page_and_its_attachments_all_count(monkeypatch):
    _serve(monkeypatch, [
        {"document_id": "node-1", "url": "https://teriin.org/article/bridging-gap"},
        {"document_id": "pdf-2", "url": "https://teriin.org/article/bridging-gap"},
        {"document_id": "node-1", "url": "https://www.teriin.org/article/bridging-gap/"},
    ], [])
    assert catalog.indexed_documents_for_urls([ARTICLE]) == {ARTICLE: ["node-1", "pdf-2"]}


def test_nothing_to_look_up_runs_no_query(monkeypatch):
    cursor = _serve(monkeypatch)
    assert catalog.indexed_documents_for_urls([]) == {}
    assert catalog.indexed_documents_for_urls(["not a url"]) == {}
    assert cursor.calls == []


def test_a_read_failure_is_unknown_not_absent(monkeypatch):
    def boom():
        raise RuntimeError("mysql down")

    monkeypatch.setattr(catalog, "mysql_connection", boom)
    assert catalog.indexed_documents_for_urls([ARTICLE]) is None
