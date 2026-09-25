"""Unit tests for theme classification and the theme rows it writes.

Two layers: :mod:`app.catalog.theme_taxonomy` turning raw theme names into
primary-tag / sub-theme assignments against ``app/theme_structure.json``, and the
``documents_theme`` writes in :mod:`app.catalog.state` that persist them. The SQL
runs against a scripted fake cursor; the real statements run in
``app/local_tests``.
"""

from __future__ import annotations

import json

import pytest

from app.catalog import state, theme_taxonomy
from app.catalog.models import StateRecord

TABLE = "documents"


# --------------------------------------------------------------------------- #
# theme_taxonomy.classify — the theme_structure.json hierarchy.
# --------------------------------------------------------------------------- #

def _rows(names) -> list[tuple[str, str, str | None, str | None]]:
    return [
        (a.name, a.theme_type, a.parent, a.group) for a in theme_taxonomy.classify(names)
    ]


def _paths(names) -> list[tuple[str, str | None, str, int]]:
    """The ancestry view: (theme, immediate parent, full path, depth). Separate
    from `_rows` because most cases care about type/group and only the hierarchy
    cases care about how deep a theme sits."""
    return [
        (a.name, a.parent, a.path, a.depth) for a in theme_taxonomy.classify(names)
    ]


def test_bucket_child_is_a_primary_tag():
    assert _rows(["Energy"]) == [("Energy", "primary", None, "main")]
    assert _rows(["Climate Change"]) == [
        ("Climate Change", "primary", None, "main")
    ]
    # "Other Themes" children are primary tags too, children or not — but the
    # group distinguishes them from a "Main Themes" primary tag.
    assert _rows(["Green Shipping"]) == [
        ("Green Shipping", "primary", None, "other")
    ]


def test_descendant_is_a_sub_theme_pointing_at_its_primary_tag():
    assert _rows(["Energy Access"]) == [("Energy Access", "sub", "Energy", "main")]
    assert _rows(["Air"]) == [("Air", "sub", "Environment", "main")]
    # Inherits its primary tag's group even though that primary tag sits under
    # "Other Themes", not "Main Themes".
    assert _rows(["Education for Youth Empowerment"]) == [
        (
            "Education for Youth Empowerment",
            "sub",
            "Environment Education",
            "other",
        )
    ]


def test_grouping_buckets_are_never_themes():
    """"Main Themes" / "Other Themes" are containers in data.json — storing them
    would credit every document with a theme it was never tagged with."""
    assert _rows(["Main Themes", "Other Themes"]) == []
    assert _rows(["Energy", "Main Themes"]) == [
        ("Energy", "primary", None, "main")
    ]


def test_blank_values_are_dropped_rather_than_stored_as_placeholders():
    assert _rows([None, "", "   ", "\t\n"]) == []


def test_unknown_theme_is_kept_and_classified_unknown_not_sub():
    """A theme added in the CMS but not yet in the theme map is still recorded —
    it just has no parent or group to point at.

    `unknown` rather than `sub`, which is the distinction that matters: calling
    it a sub-theme asserts it is a child of something, and it also hid the theme
    from `list_themes`, which enumerates primary rows. Its path is its own name,
    so a query for it matches by the same prefix rule as a mapped theme."""
    assert _rows(["Quantum Beekeeping"]) == [
        ("Quantum Beekeeping", "unknown", None, None)
    ]
    assert _paths(["Quantum Beekeeping"]) == [
        ("Quantum Beekeeping", None, "Quantum Beekeeping", 1)
    ]


def test_an_unknown_theme_is_not_given_a_parent():
    """Requirement: never infer a parent. "Energy Storage" shares a word with
    "Energy" and is still parentless until the map says otherwise."""
    assert _paths(["Energy Storage"]) == [
        ("Energy Storage", None, "Energy Storage", 1)
    ]


def test_matching_tolerates_case_and_whitespace_drift():
    assert _rows(["  energy   ACCESS "]) == [
        ("energy ACCESS", "sub", "Energy", "main")
    ]


def test_names_are_deduplicated_case_insensitively_keeping_input_order():
    assert _rows(["Air", "Energy", "AIR", "air"]) == [
        ("Air", "sub", "Environment", "main"),
        ("Energy", "primary", None, "main"),
    ]


def test_a_parent_is_a_reference_not_an_extra_row():
    """Tagging a post with only a sub-theme must not invent the parent's row —
    the document was never tagged with the parent."""
    assert _rows(["Energy Access"]) == [("Energy Access", "sub", "Energy", "main")]


def test_missing_data_file_degrades_instead_of_raising(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(theme_taxonomy, "TAXONOMY_PATH", tmp_path / "nope.json")
    theme_taxonomy.reload_taxonomy()
    try:
        # Every theme becomes unmapped, which is exactly why `require_taxonomy`
        # gates the ingest paths: this succeeds and writes the wrong answer.
        assert _rows(["Energy"]) == [("Energy", "unknown", None, None)]
        assert "Could not read the theme map" in caplog.text
    finally:
        theme_taxonomy.reload_taxonomy()


def _deep_map(tmp_path):
    """A four-level map: Energy > Energy Access > Rural Energy Access, plus a
    sibling branch, written to a temp file."""
    path = tmp_path / "theme_structure.json"
    path.write_text(
        json.dumps(
            [{
                "name": "Main Themes",
                "children": [{
                    "name": "Energy",
                    "children": [
                        {
                            "name": "Energy Access",
                            "children": [{
                                "name": "Rural Energy Access",
                                "children": [{"name": "Mini Grids"}],
                            }],
                        },
                        {"name": "Energy Efficiency"},
                    ],
                }],
            }]
        ),
        encoding="utf-8",
    )
    return path


def test_deeper_nesting_keeps_its_immediate_parent_and_records_the_full_path(
    monkeypatch, tmp_path
):
    """A grandchild hangs off its **immediate** parent, not the primary tag.

    This used to reparent everything below one level onto the primary tag, which
    made "Rural Energy Access" a sibling of its own parent "Energy Access": a
    query for Energy Access could not reach it, and a breakdown of Energy
    Access's children came back empty. The path carries the whole chain, so the
    hierarchy survives at any depth."""
    monkeypatch.setattr(theme_taxonomy, "TAXONOMY_PATH", _deep_map(tmp_path))
    theme_taxonomy.reload_taxonomy()
    try:
        assert _paths(["Rural Energy Access"]) == [
            (
                "Rural Energy Access",
                "Energy Access",
                "Energy > Energy Access > Rural Energy Access",
                3,
            )
        ]
        # A fourth level is no different — depth is not capped.
        assert _paths(["Mini Grids"]) == [
            (
                "Mini Grids",
                "Rural Energy Access",
                "Energy > Energy Access > Rural Energy Access > Mini Grids",
                4,
            )
        ]
        # Every level still inherits the primary tag's bucket, however deep.
        assert [r[3] for r in _rows(["Mini Grids", "Rural Energy Access"])] == [
            "main", "main",
        ]
        # And every level below the first is a sub-theme, never a primary tag.
        assert [r[1] for r in _rows(["Energy Access", "Mini Grids"])] == [
            "sub", "sub",
        ]
    finally:
        theme_taxonomy.reload_taxonomy()


def test_a_primary_tag_is_its_own_single_segment_path(monkeypatch, tmp_path):
    monkeypatch.setattr(theme_taxonomy, "TAXONOMY_PATH", _deep_map(tmp_path))
    theme_taxonomy.reload_taxonomy()
    try:
        assert _paths(["Energy"]) == [("Energy", None, "Energy", 1)]
    finally:
        theme_taxonomy.reload_taxonomy()


def test_two_primary_tags_from_different_buckets_are_distinguishable_by_group():
    """"Energy" and "Green Shipping" are both primary tags with parent=None --
    only theme_group tells them apart as main vs other."""
    energy, shipping = _rows(["Energy"])[0], _rows(["Green Shipping"])[0]
    assert (energy[1], energy[2]) == (shipping[1], shipping[2]) == ("primary", None)
    assert energy[3] == "main"
    assert shipping[3] == "other"
    assert energy[3] != shipping[3]


def test_group_code_matches_on_substring_not_position():
    """A bucket named after "main" maps to "main" wherever it sits in the file;
    anything else falls to "other" rather than raising."""
    assert theme_taxonomy._group_code("Main Themes") == "main"
    assert theme_taxonomy._group_code("MAIN") == "main"
    assert theme_taxonomy._group_code("Other Themes") == "other"
    assert theme_taxonomy._group_code("Emerging Themes") == "other"


# --------------------------------------------------------------------------- #
# theme_taxonomy.group_of / themes_by_group — Main/Other lookup for the theme
# listing (list_themes), which needs to label a theme without a document ever
# carrying it — unlike classify(), which only labels themes a document has.
# --------------------------------------------------------------------------- #

def test_group_of_known_primary_tags():
    assert theme_taxonomy.group_of("Energy") == "main"
    assert theme_taxonomy.group_of("Green Shipping") == "other"


def test_group_of_matches_case_and_whitespace_drift():
    assert theme_taxonomy.group_of("  green   SHIPPING ") == "other"


def test_group_of_sub_theme_inherits_primary_tags_group():
    assert theme_taxonomy.group_of("Air") == "main"  # sub of Environment
    assert theme_taxonomy.group_of("Education for Youth Empowerment") == "other"


def test_group_of_unknown_theme_is_none():
    assert theme_taxonomy.group_of("Quantum Beekeeping") is None


def test_themes_by_group_splits_main_and_other():
    groups = theme_taxonomy.themes_by_group()
    assert set(groups) == {"main", "other"}
    assert "Energy" in groups["main"] and "Air" in groups["main"]
    assert "Green Shipping" in groups["other"]
    assert "Energy" not in groups["other"] and "Green Shipping" not in groups["main"]


# --------------------------------------------------------------------------- #
# state — the documents_theme writes.
# --------------------------------------------------------------------------- #

class _FakeCursor:
    def __init__(self, fetchall_results: list | None = None):
        self.fetchalls = list(fetchall_results or [])
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> int:
        self.calls.append((" ".join(sql.split()), params))
        return 1

    def executemany(self, sql: str, rows: list) -> int:
        self.calls.append((" ".join(sql.split()), rows))
        return len(rows)

    def fetchone(self):
        return {"n": 7}

    def fetchall(self):
        return self.fetchalls.pop(0) if self.fetchalls else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
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


@pytest.fixture
def cursor(monkeypatch):
    cur = _FakeCursor()
    monkeypatch.setattr(state, "mysql_connection", lambda: _FakeConn(cur))
    monkeypatch.setattr(state, "_table", lambda: TABLE)
    return cur


def _theme_sql(cursor: _FakeCursor, verb: str) -> list[tuple[str, object]]:
    return [c for c in cursor.calls if c[0].startswith(verb) and f"{TABLE}_theme" in c[0]]


def _record(**kwargs) -> StateRecord:
    defaults = dict(
        document_id="doc-1",
        source_type="website",
        source_key="https://example.org/brief",
        fingerprint="2024-02-01",
    )
    defaults.update(kwargs)
    return StateRecord(**defaults)


def test_upsert_writes_classified_theme_rows(cursor):
    state.upsert(_record(categories=["Energy", "Air", "Quantum Beekeeping"]))

    inserts = _theme_sql(cursor, "INSERT")
    assert len(inserts) == 1
    sql, rows = inserts[0]
    assert (
        "(document_id, theme, theme_type, parent, theme_group, theme_path, depth)"
        in sql
    )
    assert rows == [
        ("doc-1", "Energy", "primary", None, "main", "Energy", 1),
        ("doc-1", "Air", "sub", "Environment", "main", "Environment > Air", 2),
        (
            "doc-1", "Quantum Beekeeping", "unknown", None, None,
            "Quantum Beekeeping", 1,
        ),
    ]


def test_upsert_inserts_nothing_when_no_valid_theme(cursor):
    """Requirement: no placeholder/NULL theme row. The DELETE still runs, so a
    document that lost its last theme is cleaned up rather than left stale."""
    state.upsert(_record(categories=["", None, "Main Themes"]))

    assert _theme_sql(cursor, "INSERT") == []
    deletes = _theme_sql(cursor, "DELETE")
    assert len(deletes) == 1 and deletes[0][1] == ("doc-1",)


def test_upsert_writes_the_document_row_before_its_theme_rows(cursor):
    """The content record is the primary fact and the FK target — it has to be
    in place before any theme row references it."""
    state.upsert(_record(categories=["Energy"]))

    statements = [sql for sql, _ in cursor.calls]
    doc_insert = next(i for i, s in enumerate(statements) if f"INSERT INTO `{TABLE}` " in s)
    theme_write = next(i for i, s in enumerate(statements) if f"{TABLE}_theme" in s)
    assert doc_insert < theme_write


def test_upsert_with_no_themes_at_all_still_clears_prior_rows(cursor):
    state.upsert(_record())

    assert _theme_sql(cursor, "INSERT") == []
    assert len(_theme_sql(cursor, "DELETE")) == 1


def test_backfill_facets_classifies_too(monkeypatch, cursor):
    monkeypatch.setattr(_FakeCursor, "fetchone", lambda self: {"document_id": "doc-1"})

    assert state.backfill_facets("doc-1", None, ["Jane"], ["Energy Access"]) is True

    _, rows = _theme_sql(cursor, "INSERT")[0]
    assert rows == [
        ("doc-1", "Energy Access", "sub", "Energy", "main",
         "Energy > Energy Access", 2)
    ]


def test_rename_theme_facet_reclassifies_the_new_name(cursor):
    """A term renamed into a known theme picks up that theme's position instead
    of keeping the one the old name had."""
    cursor.fetchalls = [[{"theme": "Atmosphere"}]]

    assert state.rename_theme_facet("doc-1", "Atmosphere", "Air") == ["Air"]

    _, rows = _theme_sql(cursor, "INSERT")[0]
    assert rows == [
        ("doc-1", "Air", "sub", "Environment", "main", "Environment > Air", 2)
    ]


# --------------------------------------------------------------------------- #
# state.reclassify_theme_rows — the one-shot for rows predating the hierarchy.
# --------------------------------------------------------------------------- #

def test_reclassify_updates_known_names_and_drops_non_themes(cursor):
    cursor.fetchalls = [
        [{"theme": "Energy"}, {"theme": "Air"}, {"theme": "Main Themes"}]
    ]

    tally = state.reclassify_theme_rows()

    updates = _theme_sql(cursor, "UPDATE")
    assert [params for _, params in updates] == [
        ("primary", None, "main", "Energy", 1, "Energy"),
        ("sub", "Environment", "main", "Environment > Air", 2, "Air"),
    ]
    deletes = _theme_sql(cursor, "DELETE")
    assert [params for _, params in deletes] == [("Main Themes",)]
    assert tally == {"names": 3, "updated": 2, "deleted": 1}


def test_reclassify_dry_run_writes_nothing(cursor):
    cursor.fetchalls = [[{"theme": "Energy"}, {"theme": "Main Themes"}]]

    tally = state.reclassify_theme_rows(dry_run=True)

    assert _theme_sql(cursor, "UPDATE") == [] and _theme_sql(cursor, "DELETE") == []
    # Counts are rows matching each name (the fake reports 7 per name).
    assert tally == {"names": 2, "updated": 7, "deleted": 7}


def test_reclassify_on_an_empty_table_does_nothing(cursor):
    assert state.reclassify_theme_rows() == {"names": 0, "updated": 0, "deleted": 0}
    assert _theme_sql(cursor, "UPDATE") == [] and _theme_sql(cursor, "DELETE") == []


# --------------------------------------------------------------------------- #
# The shipped theme map must actually load.
#
# These are regression tests for a real outage: `TAXONOMY_PATH` pointed at
# `app/data.json` after the file had been renamed to `app/theme_structure.json`.
# Nothing raised. `_load` logged and returned an empty map, so every theme
# classified as an unparented sub-theme with no group, `themes_by_group()` went
# empty, and the next ingest would have rewritten all 11,138 theme rows with a
# NULL group — erasing the Main/Other split from the data as well as the file.
#
# Every other test in this module supplies its own map via `tmp_path`, which is
# why none of them noticed. These deliberately assert against the *shipped* file.
# --------------------------------------------------------------------------- #


def test_the_shipped_theme_map_exists_and_loads():
    theme_taxonomy.reload_taxonomy()
    assert theme_taxonomy.TAXONOMY_PATH.exists(), (
        f"{theme_taxonomy.TAXONOMY_PATH} is missing — classification silently "
        "degrades to ungrouped themes."
    )
    assert theme_taxonomy.is_loaded()


def test_the_shipped_map_defines_both_groups():
    """An empty side means the bucket names stopped matching `_group_code`."""
    theme_taxonomy.reload_taxonomy()
    groups = theme_taxonomy.themes_by_group()
    assert groups[theme_taxonomy.MAIN], "no Main themes loaded"
    assert groups[theme_taxonomy.OTHER], "no Other themes loaded"


def test_the_shipped_map_groups_a_known_theme_each_way():
    """Pins the contract the theme listing depends on, at the values in the
    file: a Main sub-theme resolves to `main`, an Other primary tag to `other`."""
    theme_taxonomy.reload_taxonomy()
    assert theme_taxonomy.group_of("Energy") == theme_taxonomy.MAIN
    assert theme_taxonomy.group_of("Water") == theme_taxonomy.MAIN
    assert theme_taxonomy.group_of("Green Shipping") == theme_taxonomy.OTHER


def test_a_theme_the_map_does_not_know_has_no_group():
    """Themes discovered in Drupal but absent from the map stay ungrouped, so a
    `theme_group = 'main'` filter cannot surface them."""
    theme_taxonomy.reload_taxonomy()
    assert theme_taxonomy.group_of("A Theme Nobody Has Defined") is None


def test_require_taxonomy_passes_with_the_shipped_map():
    theme_taxonomy.reload_taxonomy()
    theme_taxonomy.require_taxonomy()


def test_require_taxonomy_raises_when_the_map_is_missing(monkeypatch, tmp_path):
    """The preflight ingestion needs: classifying against an empty map does not
    fail, it succeeds and writes the wrong answer for every document."""
    monkeypatch.setattr(
        theme_taxonomy, "TAXONOMY_PATH", tmp_path / "does-not-exist.json"
    )
    theme_taxonomy.reload_taxonomy()
    assert theme_taxonomy.is_loaded() is False
    with pytest.raises(theme_taxonomy.TaxonomyUnavailable, match="theme map"):
        theme_taxonomy.require_taxonomy()
    theme_taxonomy.reload_taxonomy()


def test_require_taxonomy_raises_on_a_malformed_map(monkeypatch, tmp_path):
    bad = tmp_path / "theme_structure.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(theme_taxonomy, "TAXONOMY_PATH", bad)
    theme_taxonomy.reload_taxonomy()
    with pytest.raises(theme_taxonomy.TaxonomyUnavailable):
        theme_taxonomy.require_taxonomy()
    theme_taxonomy.reload_taxonomy()
