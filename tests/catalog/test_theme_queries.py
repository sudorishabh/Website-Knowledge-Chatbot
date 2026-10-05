"""Unit tests for the theme-scoped structured queries and distribution path.

Covers the theme vocabulary reader, the theme filter in the catalog SQL (exact
name), the distribution query shape, and the semantic-path theme filter. All SQL runs against scripted fakes; no MySQL, no LLM.
"""

from __future__ import annotations

import pytest

from app.catalog import queries as state
from app.retrieval.understanding import filters as qfilters
from app.retrieval.understanding import query_processor as qp


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

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch(monkeypatch, module, cursor):
    monkeypatch.setattr(module, "mysql_connection", lambda: _FakeConn(cursor))


# --------------------------------------------------------------------------- #
# theme_vocabulary — what themes exist, from documents_theme.
# --------------------------------------------------------------------------- #

def _vocab_row(theme, group="main", theme_type="primary", parent=None, documents=1,
               theme_path=None, depth=None):
    path = theme_path if theme_path is not None else (
        f"{parent} > {theme}" if parent else theme
    )
    return {"theme": theme, "theme_type": theme_type, "parent": parent,
            "theme_group": group, "theme_path": path,
            "depth": depth if depth is not None else len(path.split(" > ")),
            "documents": documents}


def test_theme_vocabulary_reads_the_facet_with_its_hierarchy(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[
        _vocab_row("Climate Change", documents=150),
        _vocab_row("Air", theme_type="sub", parent="Environment", documents=77),
    ]])
    _patch(monkeypatch, state, cursor)

    rows = state.theme_vocabulary()
    assert [r["theme"] for r in rows] == ["Climate Change", "Air"]
    assert rows[1]["parent"] == "Environment" and rows[1]["theme_group"] == "main"
    sql, params = cursor.calls[0]
    assert "_theme`" in sql
    assert "GROUP BY theme, theme_type, parent, theme_group, theme_path, depth" in sql
    assert rows[1]["theme_path"] == "Environment > Air" and rows[1]["depth"] == 2
    assert "theme NOT IN (%s, %s)" in sql  # boolean artefacts excluded in SQL
    assert params == ("False", "True")


def test_theme_vocabulary_collapses_conflicting_hierarchy_variants(monkeypatch):
    """data.json changing between ingests leaves stale rows, so one theme can
    carry two hierarchies. Callers must still see exactly one row per theme —
    the variant the most documents agree on."""
    cursor = _FakeCursor(fetchall_results=[[
        _vocab_row("Energy", theme_type="primary", documents=40),
        _vocab_row("Energy", theme_type="sub", parent="Stale", documents=2),
    ]])
    _patch(monkeypatch, state, cursor)

    rows = state.theme_vocabulary()
    assert len(rows) == 1
    assert rows[0]["theme_type"] == "primary" and rows[0]["documents"] == 40


def test_theme_vocabulary_clamps_the_theme_limit(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[
        [_vocab_row(f"T{i}") for i in range(10)],
    ])
    _patch(monkeypatch, state, cursor)
    assert len(state.theme_vocabulary(limit=3)) == 3


def test_distinct_themes_is_a_names_view_of_the_vocabulary(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[_vocab_row("Air"), _vocab_row("Water")]])
    _patch(monkeypatch, state, cursor)
    assert state.distinct_themes() == ["Air", "Water"]


# --------------------------------------------------------------------------- #
# Catalog SQL — theme/tag scoping and distribution.
# --------------------------------------------------------------------------- #

def test_count_by_theme_matches_the_theme_name_exactly(monkeypatch):
    """Exact name only: a substring match wrongly merged siblings ("Environment"
    sweeping in "Environment Education"), and themes stored beneath it are not
    folded in."""
    cursor = _FakeCursor(fetchone_results=[{"n": 625}])
    _patch(monkeypatch, state, cursor)

    assert state.count_documents(source_type="website", theme="Environment") == 625
    sql, params = cursor.calls[0]
    assert "_theme` c" in sql and "c.theme = %s" in sql
    assert "theme_path" not in sql and "parent" not in sql and "LIKE" not in sql
    assert "COUNT(DISTINCT s.document_id)" in sql
    assert params == ("website", "Environment")


def test_count_by_tag_uses_its_own_facet(monkeypatch):
    cursor = _FakeCursor(fetchone_results=[{"n": 12}])
    _patch(monkeypatch, state, cursor)

    assert state.count_documents(source_type="website", tag="Waste management") == 12
    sql, params = cursor.calls[0]
    assert "_tag` t" in sql and "t.tag = %s" in sql
    assert params == ("website", "Waste management")


def test_theme_and_tag_are_independent_joins(monkeypatch):
    """A document must satisfy both, so they cannot collapse into one condition."""
    cursor = _FakeCursor(fetchone_results=[{"n": 3}])
    _patch(monkeypatch, state, cursor)

    state.count_documents(source_type="website", theme="Energy", tag="solar")
    sql, params = cursor.calls[0]
    assert "_theme` c" in sql and "_tag` t" in sql
    assert sql.count("JOIN") == 2
    assert params == ("website", "Energy", "solar")


def test_count_by_title_contains(monkeypatch):
    """count_documents must take the same title filter list_documents does, or a
    count and a listing of the same query disagree."""
    cursor = _FakeCursor(fetchone_results=[{"n": 4}])
    _patch(monkeypatch, state, cursor)

    assert state.count_documents(source_type="website", title_contains="Solar") == 4
    sql, params = cursor.calls[0]
    assert "s.title LIKE %s" in sql
    assert params == ("website", "%Solar%")


def test_count_scoped_to_entity_type(monkeypatch):
    cursor = _FakeCursor(fetchone_results=[{"n": 5}])
    _patch(monkeypatch, state, cursor)

    assert state.count_documents(source_type="website", entity_type="node") == 5
    sql, params = cursor.calls[0]
    assert "s.entity_type = %s" in sql
    assert params == ("website", "node")


def test_distribution_scoped_to_entity_type(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"k": "report", "n": 8}]])
    _patch(monkeypatch, state, cursor)

    assert state.distribution(
        "bundle", source_type="website", entity_type="node"
    ) == [("report", 8)]
    sql, params = cursor.calls[0]
    assert "s.entity_type = %s" in sql and params == ("website", "node")


def test_distribution_by_theme_groups_on_the_facet(monkeypatch):
    cursor = _FakeCursor(
        fetchall_results=[[{"k": "Climate Change", "n": 12}, {"k": "Energy", "n": 5}]]
    )
    _patch(monkeypatch, state, cursor)

    rows = state.distribution("theme", source_type="website")
    assert rows == [("Climate Change", 12), ("Energy", 5)]
    sql, params = cursor.calls[0]
    # Grouped on the key expression rather than the output alias: for authors
    # the two differ, and MySQL will not group on an aggregate alias.
    assert "GROUP BY gt.theme ORDER BY n DESC" in sql
    assert "_theme` gt" in sql and "gt.theme AS k" in sql
    assert "COUNT(DISTINCT s.document_id)" in sql
    # Same artefact exclusion as theme_vocabulary, so a breakdown and a listing
    # never disagree about which themes exist.
    assert "gt.theme NOT IN (%s, %s)" in sql
    assert params == ("website", "False", "True")


def test_distribution_scoped_by_theme_and_author(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"k": "2024", "n": 4}]])
    _patch(monkeypatch, state, cursor)

    rows = state.distribution(
        "year", source_type="website", theme="Energy", author="Sharma", limit=20
    )
    assert rows == [("2024", 4)]
    sql, params = cursor.calls[0]
    assert "_theme` c" in sql and "c.theme = %s" in sql
    assert "_author` a" in sql and "a.author LIKE %s" in sql
    assert "COUNT(DISTINCT s.document_id)" in sql
    assert params == ("website", "%Sharma%", "Energy")


def test_distribution_by_year_skips_undated(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"k": 2024, "n": 9}]])
    _patch(monkeypatch, state, cursor)

    assert state.distribution("year") == [("2024", 9)]
    sql, _ = cursor.calls[0]
    assert "YEAR(s.effective_start_date)" in sql and "IS NOT NULL" in sql


def test_distribution_rejects_unknown_dimension():
    try:
        state.distribution("acl")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown dimension must raise")


# --------------------------------------------------------------------------- #
# Semantic path — theme filter over the vector search.
# --------------------------------------------------------------------------- #

def test_theme_condition_scopes_by_catalog_document_ids(monkeypatch):
    """MySQL decides who is in the theme; Qdrant only ranks inside that set.

    The previous version matched the chunk payload's `categories` names, which
    was a second copy of theme membership written at index time (so it went
    stale on a rename). The filter now names document ids, and nothing filters
    on `categories` at all."""
    cursor = _FakeCursor(fetchall_results=[[
        {"document_id": "d1"}, {"document_id": "d2"},
    ]])
    _patch(monkeypatch, state, cursor)

    condition = qp._theme_condition("Energy")

    assert condition.key == "document_id"
    assert condition.match.any == ["d1", "d2"]
    sql, params = cursor.calls[0]
    # Exact theme name, never widened to the themes beneath it.
    assert "_theme` c" in sql and "c.theme = %s" in sql
    assert params == ("Energy",)
    # Not restricted to website nodes: an attachment inherits its page's themes
    # so that theme-scoped retrieval can reach the PDF's text.
    assert "s.source_type = %s" not in sql


def test_a_theme_no_document_carries_is_dropped_from_search(monkeypatch):
    """The theme vocabulary is the names documents carry, so a name none carries
    is not a theme — "WSDS", an event series understanding put in the slot. It
    used to become a filter matching nothing, which emptied every leg that takes
    the filters while the title leg's one stray hit kept the empty-pull retry
    from firing. Dropped, the name still reaches search as the question's words."""
    cursor = _FakeCursor(fetchall_results=[[]])
    _patch(monkeypatch, state, cursor)

    assert qp._theme_condition("WSDS") is None


def test_theme_condition_fails_open_when_the_catalog_cannot_answer(monkeypatch):
    """A MySQL outage must degrade retrieval to unscoped semantic search, not
    apply a membership set nobody could verify — and not silently mean
    "nothing matches", which is what an empty list would have said."""
    monkeypatch.setattr(state, "theme_document_ids", lambda *a, **k: None)

    assert qp._theme_condition("Energy") is None


def test_facet_filters_drops_an_unresolvable_theme(monkeypatch):
    """The condition being None must not reach the filter list as a null entry."""
    from types import SimpleNamespace

    monkeypatch.setattr(state, "theme_document_ids", lambda *a, **k: None)
    analysis = SimpleNamespace(
        theme="Energy", tags=None, source_type=None, language=None,
        date_from=None, date_to=None, search_query="",
    )

    assert qfilters._facet_filters(analysis) == []


def test_distribution_applies_no_source_filter_unless_asked(monkeypatch):
    """`source_type` used to default to "website" here while its two sibling
    functions defaulted to None, so a breakdown was silently narrower than a
    count of the same scope. Unset now means unfiltered, as it does for every
    other parameter."""
    cursor = _FakeCursor(fetchall_results=[[{"k": "Energy", "n": 5}]])
    _patch(monkeypatch, state, cursor)

    state.distribution("theme")
    sql, params = cursor.calls[0]
    assert "s.source_type = %s" not in sql
    assert "website" not in params


# --------------------------------------------------------------------------- #
# theme_group: the theme map's Main/Other split, applied to counts.
#
# The listing path learned this earlier; counts had no way to express it, so a
# generic "how many articles per theme" returned all 37 themes — 22 main, 12
# other, 3 the map does not define — and an Other theme with 158 documents
# outranked most of the curated ones.
# --------------------------------------------------------------------------- #


def test_theme_group_filters_by_equality_not_by_excluding_the_other(monkeypatch):
    """A theme the map does not define has a NULL group, and `<> 'other'` is not
    true of NULL. Equality is what keeps uncurated CMS terms out."""
    cursor = _FakeCursor(fetchall_results=[[{"n": 5}]])
    _patch(monkeypatch, state, cursor)

    state.count_documents(theme_group="main")
    sql, params = cursor.calls[0]
    assert "g.theme_group = %s" in sql
    assert "<>" not in sql.split("theme_group")[1][:20]
    assert "main" in params


def test_grouping_by_theme_restricts_the_themes_not_the_documents(monkeypatch):
    """The subtle half.

    As a document scope, `theme_group='main'` only requires a document to carry
    *some* main theme — so one tagged both "Energy" (main) and "Green Shipping"
    (other) would still contribute a row under Green Shipping. Grouping by theme
    has to constrain the grouped rows themselves.
    """
    cursor = _FakeCursor(fetchall_results=[[{"k": "Energy", "n": 5}]])
    _patch(monkeypatch, state, cursor)

    state.distribution("theme", theme_group="main")
    sql, _ = cursor.calls[0]
    assert "gt.theme_group = %s" in sql, "must filter the group join"
    assert " g ON g.document_id" not in sql, "and not add a redundant scope join"


def test_grouping_by_another_dimension_uses_theme_group_as_a_document_scope(
    monkeypatch,
):
    """Per-author over main-theme documents: here the group *is* a document
    scope, so the separate join is the right shape."""
    cursor = _FakeCursor(fetchall_results=[[{"k": "Sharma", "n": 3}]])
    _patch(monkeypatch, state, cursor)

    state.distribution("author", theme_group="main")
    sql, _ = cursor.calls[0]
    assert " g ON g.document_id" in sql and "g.theme_group = %s" in sql


def test_theme_and_theme_group_combine_as_and(monkeypatch):
    """Separate aliases, so "under Energy" and "in a main theme" do not fight
    over one join."""
    cursor = _FakeCursor(fetchall_results=[[{"n": 2}]])
    _patch(monkeypatch, state, cursor)

    state.count_documents(theme="Energy", theme_group="main")
    sql, _ = cursor.calls[0]
    assert "_theme` c" in sql and "_theme` g" in sql


def test_no_theme_group_means_no_restriction(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"n": 9}]])
    _patch(monkeypatch, state, cursor)

    state.count_documents()
    sql, _ = cursor.calls[0]
    assert "theme_group" not in sql


# --------------------------------------------------------------------------- #
# count_distinct_values / cross_distribution
#
# The two questions the count/distribution pair could not express: one about a
# facet rather than the documents ("how many authors under Theme X"), and one
# whose grouping key is a pair ("which authors write about which themes").
# --------------------------------------------------------------------------- #


def test_count_distinct_counts_the_facet_not_the_documents(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[], fetchone_results=[{"n": 264}])
    _patch(monkeypatch, state, cursor)

    assert state.count_distinct_values("author", theme="Energy") == 264
    sql, params = cursor.calls[0]
    assert "COUNT(DISTINCT COALESCE(dv.author_norm, dv.author))" in sql
    assert "Energy" in params


def test_count_distinct_uses_its_own_alias_for_the_counted_facet(monkeypatch):
    """"How many themes does Author X publish in" filters on one facet table and
    counts another. Reusing the filter's alias would count only the rows that
    matched the filter — here, the one theme that matched nothing."""
    cursor = _FakeCursor(fetchall_results=[], fetchone_results=[{"n": 13}])
    _patch(monkeypatch, state, cursor)

    state.count_distinct_values("theme", author="Sharma")
    sql, _ = cursor.calls[0]
    assert "COUNT(DISTINCT dv.theme)" in sql
    assert "_author` a" in sql, "the author filter keeps its own join"
    assert "_theme` dv" in sql, "the counted dimension gets a separate one"


def test_count_distinct_rejects_an_unknown_dimension(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[], fetchone_results=[{"n": 0}])
    _patch(monkeypatch, state, cursor)
    with pytest.raises(ValueError):
        state.count_distinct_values("colour")


def test_count_distinct_by_year_skips_undated(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[], fetchone_results=[{"n": 10}])
    _patch(monkeypatch, state, cursor)

    state.count_distinct_values("year")
    sql, _ = cursor.calls[0]
    assert "YEAR(s.effective_start_date)" in sql and "s.effective_start_date IS NOT NULL" in sql


def test_cross_distribution_groups_on_the_pair(monkeypatch):
    cursor = _FakeCursor(
        fetchall_results=[[{"a": "Sharma", "b": "Waste", "n": 16}]]
    )
    _patch(monkeypatch, state, cursor)

    rows = state.cross_distribution("author", "theme", bundle="article")
    assert rows == [("Sharma", "Waste", 16)]
    sql, _ = cursor.calls[0]
    assert "GROUP BY COALESCE(ga.author_norm, ga.author), gb.theme" in sql
    assert "MIN(ga.author) AS a" in sql and "gb.theme AS b" in sql
    assert "COUNT(DISTINCT s.document_id)" in sql


def test_cross_distribution_applies_theme_group_to_the_theme_side(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"a": "Sharma", "b": "Waste", "n": 3}]])
    _patch(monkeypatch, state, cursor)

    state.cross_distribution("author", "theme", theme_group="main")
    sql, _ = cursor.calls[0]
    assert "gb.theme_group = %s" in sql


def test_cross_distribution_refuses_one_dimension_twice(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[]])
    _patch(monkeypatch, state, cursor)
    with pytest.raises(ValueError):
        state.cross_distribution("theme", "theme")


def test_cross_distribution_clamps_its_limit(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[]])
    _patch(monkeypatch, state, cursor)

    state.cross_distribution("author", "theme", limit=100_000)
    sql, _ = cursor.calls[0]
    assert "LIMIT 500" in sql


def test_counting_themes_restricts_the_counted_themes_not_the_documents(monkeypatch):
    """The same scope-versus-dimension trap `distribution` had, found in the
    Step 5 review on the distinct-count path.

    As a document scope, `theme_group='main'` only requires a document to carry
    *some* main theme — so counting distinct themes over those documents also
    counted their Other themes. "How many main themes are there?" answered 30,
    eight of which were Other themes.
    """
    cursor = _FakeCursor(fetchall_results=[], fetchone_results=[{"n": 22}])
    _patch(monkeypatch, state, cursor)

    state.count_distinct_values("theme", theme_group="main")
    sql, params = cursor.calls[0]
    assert "dv.theme_group = %s" in sql, "must filter the counted alias"
    assert " g ON g.document_id" not in sql, "and not add a document scope join"
    assert "main" in params


def test_counting_a_non_theme_dimension_keeps_theme_group_as_a_document_scope(
    monkeypatch,
):
    """"How many authors work on main-theme documents" — here the group really
    is a property of the documents, so the separate join is the right shape."""
    cursor = _FakeCursor(fetchall_results=[], fetchone_results=[{"n": 5}])
    _patch(monkeypatch, state, cursor)

    state.count_distinct_values("author", theme_group="main")
    sql, _ = cursor.calls[0]
    assert " g ON g.document_id" in sql and "g.theme_group = %s" in sql
    assert "COUNT(DISTINCT COALESCE(dv.author_norm, dv.author))" in sql


def test_cross_distribution_restricts_the_theme_side_whichever_it_is(monkeypatch):
    """Either order must restrict the theme dimension itself."""
    for first, second, alias in (("author", "theme", "gb"), ("theme", "author", "ga")):
        cursor = _FakeCursor(fetchall_results=[[]])
        _patch(monkeypatch, state, cursor)
        state.cross_distribution(first, second, theme_group="main")
        sql, _ = cursor.calls[0]
        assert f"{alias}.theme_group = %s" in sql, (first, second)


# --------------------------------------------------------------------------- #
# cross_distribution argument handling. The two dimensions are positional, so a
# third positional argument reads as a third dimension — and used to bind to
# `source_type` instead, filtering to a source type that does not exist.
# --------------------------------------------------------------------------- #


def test_a_third_positional_argument_is_rejected():
    """It used to filter on a source type called "year", match nothing and
    return [] — a wrong answer wearing an empty one's clothes."""
    with pytest.raises(TypeError):
        state.cross_distribution("author", "theme", "year")  # type: ignore[misc]


def test_scope_arguments_must_be_passed_by_keyword():
    with pytest.raises(TypeError):
        state.cross_distribution(  # type: ignore[misc]
            "author", "theme", "website", "article"
        )


@pytest.mark.parametrize(
    "first,second",
    [("colour", "theme"), ("author", "colour"), ("document", "author")],
)
def test_an_unsupported_dimension_is_rejected(first, second):
    """Refused before any SQL is built, so an unknown name cannot reach the
    database as an empty result."""
    with pytest.raises(ValueError):
        state.cross_distribution(first, second)


def test_the_error_names_the_offending_dimension():
    with pytest.raises(ValueError) as excinfo:
        state.cross_distribution("author", "colour")
    assert "colour" in str(excinfo.value)


def test_valid_keyword_calls_are_unchanged(monkeypatch):
    cursor = _FakeCursor(fetchall_results=[[{"a": "Sharma", "b": "Waste", "n": 4}]])
    _patch(monkeypatch, state, cursor)

    rows = state.cross_distribution(
        "author", "theme", source_type="website", bundle="article"
    )
    assert rows == [("Sharma", "Waste", 4)]


def test_distribution_by_author_groups_on_the_normalized_name(monkeypatch):
    """One name written two ways is one row, labelled with a raw spelling rather
    than the lowercased key it grouped on."""
    cursor = _FakeCursor(fetchall_results=[[{"k": "Dr Jayanta Mitra", "n": 7}]])
    _patch(monkeypatch, state, cursor)

    state.distribution("author")
    sql, _ = cursor.calls[0]
    assert "MIN(f.author) AS k" in sql
    assert "GROUP BY COALESCE(f.author_norm, f.author)" in sql
