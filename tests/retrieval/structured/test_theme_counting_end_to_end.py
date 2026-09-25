"""The theme questions from the requirements, answered by the real SQL.

Every other test in this area asserts the *shape* of a query — which joins
appear, what the parameters are. That catches drift but not arithmetic: a filter
can be spelled exactly as intended and still count the wrong documents. So this
module executes ``app.catalog.queries`` against a small SQLite corpus and checks
the numbers.

SQLite stands in for MySQL only where the two agree on what is being used here:
backtick identifiers, ``COUNT(DISTINCT …)``, ``JOIN``, and text date comparison. Nothing here depends on MySQL
collation behaviour — the case-insensitive facet key is covered by
``test_theme_storage_contract.py``, which tests the writer instead.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.catalog import queries, theme_taxonomy
from app.retrieval.structured import tools
from app.retrieval.structured.types import RecordFilters

_TAXONOMY = [
    {
        "name": "Main Themes",
        "children": [
            {
                "name": "Climate Change",
                "children": [
                    {
                        "name": "Adaptation",
                        "children": [{"name": "Coastal Adaptation"}],
                    },
                    {"name": "Mitigation"},
                ],
            },
            {"name": "Energy", "children": [{"name": "Energy Access"}]},
        ],
    }
]

#: (document_id, bundle, date, [themes]). Deliberately mixed: documents tagged
#: only at a leaf, documents with no theme at all, and one theme the map does
#: not know — the three cases a count has to get right at once.
_CORPUS = [
    ("art-1", "article", "2024-03-01", ["Climate Change"]),
    ("art-2", "article", "2024-06-01", ["Adaptation"]),
    ("art-3", "article", "2025-02-01", ["Coastal Adaptation"]),
    ("art-4", "article", "2023-01-01", ["Mitigation"]),
    ("art-5", "article", "2024-09-01", ["Energy Access"]),
    ("art-6", "article", "2024-11-01", []),
    ("art-7", "article", "2024-12-01", ["Green Hydrogen"]),
    ("evt-1", "events", "2024-04-01", ["Adaptation", "Mitigation"]),
    ("evt-2", "events", "2024-05-01", ["Climate Change"]),
    ("prj-1", "ongoing_projects", "2024-02-01", ["Coastal Adaptation"]),
    ("prj-2", "ongoing_projects", "2024-07-01", ["Energy"]),
]


class _Cursor:
    """Runs MySQL-flavoured SQL on SQLite and returns dict rows.

    Only the two rewrites the catalog's statements actually need: ``%s`` ->
    ``?``, and datetimes to ISO text so a bound date compares against the stored
    one as a string.
    """

    def __init__(self, conn):
        self._cur = conn.cursor()

    def execute(self, sql, params=()):
        params = tuple(
            p.isoformat(sep=" ") if hasattr(p, "isoformat") else p
            for p in (params or ())
        )
        self._cur.execute(sql.replace("%s", "?"), params)
        return self._cur.rowcount

    def _dict(self, row):
        if row is None:
            return None
        return {d[0]: row[i] for i, d in enumerate(self._cur.description)}

    def fetchone(self):
        return self._dict(self._cur.fetchone())

    def fetchall(self):
        return [self._dict(r) for r in self._cur.fetchall()]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _Cursor(self._conn)

    def commit(self):
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    """The corpus above, in SQLite, with `queries` pointed at it."""
    path = tmp_path / "theme_structure.json"
    path.write_text(json.dumps(_TAXONOMY), encoding="utf-8")
    monkeypatch.setattr(theme_taxonomy, "TAXONOMY_PATH", path)
    theme_taxonomy.reload_taxonomy()

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE `documents` (document_id TEXT PRIMARY KEY, source_type TEXT,"
        " entity_type TEXT, bundle TEXT, title TEXT, effective_start_date TEXT,"
        # _row_to_record indexes these directly, so a row without them cannot be
        # read back as a StateRecord.
        " source_key TEXT, fingerprint TEXT)"
    )
    conn.execute(
        "CREATE TABLE `documents_theme` (document_id TEXT, theme TEXT,"
        " theme_type TEXT, parent TEXT, theme_group TEXT, theme_path TEXT,"
        " depth INTEGER)"
    )
    conn.execute("CREATE TABLE `documents_author` (document_id TEXT, author TEXT)")
    conn.execute("CREATE TABLE `documents_tag` (document_id TEXT, tag TEXT)")
    for document_id, bundle, date, themes in _CORPUS:
        conn.execute(
            "INSERT INTO `documents` VALUES (?, 'website', 'node', ?, ?, ?, ?, 'f')",
            (document_id, bundle, document_id.upper(), f"{date} 00:00:00",
             f"https://example.org/{document_id}"),
        )
        for a in theme_taxonomy.classify(themes):
            conn.execute(
                "INSERT INTO `documents_theme` VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, a.name, a.theme_type, a.parent, a.group,
                 a.path, a.depth),
            )
    conn.commit()

    monkeypatch.setattr(queries, "mysql_connection", lambda: _Conn(conn))
    monkeypatch.setattr(queries, "_table", lambda: "documents")
    yield conn
    conn.close()
    theme_taxonomy.reload_taxonomy()


def _count(**kw) -> int:
    return queries.count_documents(source_type="website", entity_type="node", **kw)


# --------------------------------------------------------------------------- #
# "How many <content type> are in <theme>?" — the requirement's headline
# questions, each answered from the documents tagged with that theme.
# --------------------------------------------------------------------------- #

def test_count_articles_in_a_main_theme(catalog):
    """art-1 only: it is the one article tagged Climate Change. art-2, art-3 and
    art-4 carry themes nested under it in the map and are not folded in."""
    assert _count(bundle="article", theme="Climate Change") == 1


def test_count_events_in_a_main_theme(catalog):
    assert _count(bundle="events", theme="Climate Change") == 1  # evt-2


def test_count_ongoing_projects_in_a_main_theme(catalog):
    """None: prj-1 is tagged Coastal Adaptation, prj-2 Energy."""
    assert _count(bundle="ongoing_projects", theme="Climate Change") == 0


def test_count_across_content_types_for_a_theme(catalog):
    """"How many documents are in Climate Change?" — art-1 and evt-2."""
    assert _count(theme="Climate Change") == 2


def test_a_document_is_counted_once_however_many_matching_themes_it_carries(catalog):
    """evt-1 carries two main themes, Adaptation and Mitigation. The group join
    multiplies rows, so without COUNT(DISTINCT document_id) it would be counted
    twice."""
    assert _count(bundle="events", theme_group="main") == 2
    rows = catalog.execute(
        "SELECT COUNT(*) FROM `documents_theme` WHERE document_id = 'evt-1'"
    ).fetchone()
    assert rows[0] == 2


# --------------------------------------------------------------------------- #
# Every theme counts its own documents only, wherever the map nests it.
# --------------------------------------------------------------------------- #

def test_a_nested_theme_counts_only_its_own_documents(catalog):
    """art-2 and evt-1 are tagged Adaptation. art-3 and prj-1 sit below it in
    the map and are not included."""
    assert _count(theme="Adaptation") == 2


def test_count_in_a_leaf_theme(catalog):
    assert _count(theme="Coastal Adaptation") == 2


def test_a_sibling_branch_is_excluded(catalog):
    assert _count(theme="Mitigation") == 2  # art-4, evt-1


def test_a_parent_theme_does_not_include_the_themes_beneath_it(catalog):
    ids = {
        r.document_id
        for r in queries.list_documents(
            source_type="website", entity_type="node",
            theme="Climate Change", limit=50,
        )
    }
    assert ids == {"art-1", "evt-2"}


# --------------------------------------------------------------------------- #
# Sparseness and the open vocabulary, seen from the query side.
# --------------------------------------------------------------------------- #

def test_an_untagged_document_is_in_no_theme_but_still_in_the_corpus(catalog):
    assert _count() == len(_CORPUS)
    for theme in ("Climate Change", "Energy", "Adaptation", "Green Hydrogen"):
        ids = queries.list_documents(
            source_type="website", entity_type="node", theme=theme, limit=50
        )
        assert "art-6" not in [r.document_id for r in ids]


def test_a_theme_absent_from_the_taxonomy_is_still_countable(catalog):
    """The open-vocabulary requirement, from the query side: no edit to
    theme_structure.json was needed for this to work."""
    assert _count(theme="Green Hydrogen") == 1
    assert _count(bundle="article", theme="Green Hydrogen") == 1


def test_an_unknown_theme_does_not_leak_into_a_curated_theme(catalog):
    assert "art-7" not in [
        r.document_id
        for r in queries.list_documents(
            source_type="website", entity_type="node",
            theme="Climate Change", limit=50,
        )
    ]


# --------------------------------------------------------------------------- #
# Combined with the other dimensions.
# --------------------------------------------------------------------------- #

def test_theme_and_date_range_combine(catalog):
    """"How many documents belong to this theme between 2024 and 2025?"

    Of the two documents tagged Coastal Adaptation, prj-1 falls in 2024. art-3
    is 2025-02-01 — outside, because the interval is half-open at the top."""
    from datetime import datetime

    assert _count(
        theme="Coastal Adaptation",
        effective_from=datetime(2024, 1, 1),
        effective_to=datetime(2025, 1, 1),
    ) == 1
    # The date narrows the theme scope rather than replacing it: without the
    # theme, 2024 holds more.
    assert _count(
        effective_from=datetime(2024, 1, 1), effective_to=datetime(2025, 1, 1)
    ) == 9


def test_theme_and_bundle_and_date_combine(catalog):
    from datetime import datetime

    assert _count(
        bundle="article",
        theme="Climate Change",
        effective_from=datetime(2024, 1, 1),
        effective_to=datetime(2025, 1, 1),
    ) == 1  # art-1


# --------------------------------------------------------------------------- #
# Grouping.
# --------------------------------------------------------------------------- #

def test_breakdown_by_content_type_for_a_theme(catalog):
    """"How many events and ongoing projects are associated with Adaptation?"
    in one query."""
    rows = queries.distribution(
        "bundle", source_type="website", entity_type="node", theme="Adaptation",
    )
    assert dict(rows) == {"article": 1, "events": 1}


# --------------------------------------------------------------------------- #
# Through the structured tool, so the chat path is what is exercised.
# --------------------------------------------------------------------------- #

def test_the_count_tool_reports_the_catalog_number(catalog, monkeypatch):
    """Requirement: an exact count comes from the database. The tool's rendered
    answer must carry the catalog's number and nothing approximate."""
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [{"theme": "Climate Change", "theme_type": "primary",
                       "parent": None, "theme_group": "main",
                       "theme_path": "Climate Change", "depth": 1,
                       "documents": 2}],
    )
    monkeypatch.setattr("app.catalog.queries.find_tag", lambda name: None)
    monkeypatch.setattr("app.catalog.queries.distinct_authors", lambda **kw: [])
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: ("article", "events")
    )

    result = tools.count_records("article", RecordFilters(theme="Climate Change"))

    assert result.ok
    assert result.data["count"] == 1
    assert "1" in result.rendered
