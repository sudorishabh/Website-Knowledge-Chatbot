"""What a catalog answer says beyond its headline (app.retrieval.structured.detail).

The catalog is stubbed throughout; nothing here reaches MySQL. The feature is
off by default in the suite (see tests/conftest.py), so each test that wants it
turns it on.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.ingestion.extractors.drupal_extractor import DEFAULT_BUNDLES
from app.retrieval.structured import answerer, detail, planner, tools
from app.retrieval.structured.types import DatabasePlan, RecordFilters, ToolCall


def _row(i=0, *, bundle="article", date="2026-04-20T11:27:20"):
    return SimpleNamespace(
        document_id=f"d{i}", title=f"Title {i}", url=f"https://teriin.org/a/{i}",
        effective_start_date=date, start_precision="day", bundle=bundle,
    )


@pytest.fixture(autouse=True)
def _offline_catalog(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr("app.catalog.queries.find_tag", lambda name: None)
    monkeypatch.setattr("app.catalog.queries.distinct_authors", lambda **kw: [])
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: DEFAULT_BUNDLES
    )
    monkeypatch.setattr("app.catalog.queries.facets_for", lambda ids: {})


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(get_settings(), "catalog_answer_detail_enabled", True)


@pytest.fixture
def catalog(monkeypatch):
    """A stub catalog: set `.total`, `.years`, `.types`, `.rows`, `.values`."""
    stub = SimpleNamespace(
        total=68, years=[("2018", 10), ("2025", 13), ("2026", 5)],
        types=[], rows=[_row(i) for i in range(3)], values=[], calls=[],
        wider_total=0,
    )

    def count(**kw):
        stub.calls.append(("count", kw))
        widened = ("effective_from" not in kw and stub.calls[0][1].get("effective_from")) \
            or (kw.get("bundle") is None and stub.calls[0][1].get("bundle"))
        return stub.wider_total if widened else stub.total

    def distribution(group_by, **kw):
        stub.calls.append((f"distribution:{group_by}", kw))
        if group_by == "year":
            return list(stub.years)
        if group_by == "bundle":
            return list(stub.types)[: kw.get("limit", 20)]
        return list(stub.values)[: kw.get("limit", 20)]

    def listing(**kw):
        stub.calls.append(("list", kw))
        return list(stub.rows)[: kw.get("limit", 10)]

    monkeypatch.setattr("app.catalog.queries.count_documents", count)
    monkeypatch.setattr("app.catalog.queries.distribution", distribution)
    monkeypatch.setattr("app.catalog.queries.list_documents", listing)
    monkeypatch.setattr(
        "app.catalog.queries.count_distinct_values", lambda dimension, **kw: stub.total
    )
    return stub


def _count(entity="article", **filters):
    return tools.count_records(entity, RecordFilters(**filters), detail=True)


# --------------------------------------------------------------------------- #
# A count of documents.
# --------------------------------------------------------------------------- #

def test_a_count_says_when_its_documents_date_from_and_shows_the_newest(catalog):
    r = _count(theme="Climate Change")
    assert r.rendered.split("\n\n") == [
        "There are **68 articles** on **Climate Change**. "
        "They span 2018 to 2026, and 2025 was the busiest year, with 13.",
        "### Latest articles\n"
        "- [Title 0](https://teriin.org/a/0) — 20 Apr 2026\n"
        "- [Title 1](https://teriin.org/a/1) — 20 Apr 2026\n"
        "- [Title 2](https://teriin.org/a/2) — 20 Apr 2026",
        "*You can ask me to list them, or to break them down by year.*",
    ]
    assert [c["title"] for c in r.citations] == ["Title 0", "Title 1", "Title 2"]
    assert r.data["count"] == 68  # the payload is unchanged


def test_every_detail_query_shares_the_headlines_scope(catalog):
    """Every query beneath the headline uses its filters. The one let go is the
    content type, to say what else the same subject holds."""
    catalog.wider_total = 500
    catalog.types = [("article", 68), ("news", 300), ("events", 132)]
    _count(theme="Climate Change")
    headline = catalog.calls[0][1]
    for name, kw in catalog.calls[1:]:
        shared = {k: v for k, v in kw.items() if k != "limit"}
        if shared["bundle"] is None:
            shared["bundle"] = headline["bundle"]
        assert shared == headline, name


def test_a_count_across_content_types_gives_its_mix(catalog):
    catalog.types = [("article", 12), ("research_papers", 9), ("news", 3)]
    catalog.rows = [_row(0, bundle="news"), _row(1, bundle="research_papers"), _row(2)]
    r = _count(None, author="Suneel Pandey")
    assert r.rendered.split("\n\n")[:2] == [
        "**Suneel Pandey** has **68 publications** on the site. "
        "They span 2018 to 2026, and 2025 was the busiest year, with 13.",
        "### By type\n- Articles: 12\n- Research papers: 9\n- News items: 3",
    ]
    assert "### Latest publications\n" in r.rendered
    assert "— News item · 20 Apr 2026" in r.rendered
    assert r.rendered.endswith(
        "*You can ask me to list them, or to break them down by year or content type.*"
    )


def test_a_persons_count_of_one_type_names_what_else_they_published(catalog):
    catalog.total, catalog.wider_total = 7, 41
    catalog.types = [("feature_articles", 31), ("research_papers", 7),
                     ("policy_brief", 2), ("article", 1)]
    r = _count("research_papers", author="Dr Vibha Dhawan")
    assert r.rendered.split("\n\n")[:3] == [
        "**Dr Vibha Dhawan** has **7 research papers** on the site. "
        "They span 2018 to 2026, and 2025 was the busiest year, with 13.",
        "### All publications by Dr Vibha Dhawan (41)\n"
        "- Feature articles: 31\n- **Research papers: 7**\n- Policy briefs: 2\n- Articles: 1",
        "### Latest research papers\n"
        "- [Title 0](https://teriin.org/a/0) — 20 Apr 2026\n"
        "- [Title 1](https://teriin.org/a/1) — 20 Apr 2026\n"
        "- [Title 2](https://teriin.org/a/2) — 20 Apr 2026",
    ]


def test_a_themes_count_of_one_type_names_what_else_it_holds(catalog, monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [{"theme": "Climate Change", "theme_type": "primary", "parent": None,
                       "theme_group": "main", "documents": 3}],
    )
    catalog.wider_total = 1500
    catalog.types = [("news", 300), ("events", 132), ("feature_articles", 100),
                     ("press_release", 90), ("completed_projects", 80),
                     ("article", 68), ("videos", 30)]
    r = _count(theme="Climate Change")
    # The asked type is shown even below the five largest; the rest is folded.
    assert ("### All items on Climate Change (1,500)\n"
            "- News items: 300\n- Events: 132\n- Feature articles: 100\n"
            "- Press releases: 90\n- Completed projects: 80\n- **Articles: 68**\n"
            "- Other types: 730") in r.rendered


def test_a_type_that_is_all_a_person_wrote_names_nothing_else(catalog):
    catalog.total, catalog.wider_total = 7, 7
    assert "### All" not in _count("research_papers", author="Dr A").rendered


def test_a_type_count_without_a_subject_names_nothing_else(catalog):
    catalog.wider_total = 5000
    catalog.types = [("news", 1667), ("research_papers", 68)]
    assert "### All" not in _count("research_papers").rendered


def test_recent_items_skip_a_page_published_twice(catalog):
    twin = _row(1)
    twin.title, twin.document_id = "Title 0", "d0-copy"
    catalog.rows = [_row(0), twin, _row(2)]
    r = _count()
    assert r.rendered.count("[Title 0]") == 1
    assert "[Title 2]" in r.rendered


def test_recent_items_name_the_co_authors_of_one_persons_work(catalog, monkeypatch):
    monkeypatch.setattr("app.catalog.queries.facets_for", lambda ids: {
        "d0": {"authors": ["Dr Vibha Dhawan", "Mr Sharif Qamar"], "themes": []},
        "d1": {"authors": ["Dr Vibha Dhawan"], "themes": []},
    })
    r = _count("research_papers", author="Dr Vibha Dhawan")
    assert "— 20 Apr 2026 · with Mr Sharif Qamar\n" in r.rendered
    assert "[Title 1](https://teriin.org/a/1) — 20 Apr 2026\n" in r.rendered
    other = _count(theme="Energy")
    assert "· by Dr Vibha Dhawan and Mr Sharif Qamar" in other.rendered


def _article_share(monkeypatch, *, articles, everything):
    """Counts as the catalog gives them: the Article category alone, or every type."""
    monkeypatch.setattr(
        "app.catalog.queries.count_documents",
        lambda **kw: articles if kw.get("bundle") == "article" else everything,
    )


def test_a_widened_count_says_how_many_are_of_the_type_asked_about(catalog, monkeypatch):
    """A person's "articles" spans everything they published; the category
    reading is answered beside it, not dropped."""
    _article_share(monkeypatch, articles=1, everything=41)
    catalog.types = [("feature_articles", 31), ("research_papers", 7),
                     ("policy_brief", 2), ("article", 1)]
    r = tools.count_records(None, RecordFilters(author="Dr Vibha Dhawan"),
                            detail=True, named_type="article")
    assert r.rendered.split("\n\n")[:3] == [
        "**Dr Vibha Dhawan** has **41 publications** on the site. "
        "They span 2018 to 2026, and 2025 was the busiest year, with 13.",
        "That counts every kind of publication; only 1 of them is filed under "
        "*Articles* on the site.",
        "### By type\n- Feature articles: 31\n- Research papers: 7\n"
        "- Policy briefs: 2\n- **Articles: 1**",
    ]


def test_a_widened_count_with_none_of_the_type_says_so(catalog, monkeypatch):
    _article_share(monkeypatch, articles=0, everything=5)
    r = tools.count_records(None, RecordFilters(author="Dr A"), detail=True,
                            named_type="article")
    assert "none of them is filed under *Articles* on the site." in r.rendered


def test_a_count_that_was_not_widened_names_no_category(catalog):
    assert "category" not in _count(None, author="Suneel Pandey").rendered


def test_a_widened_list_says_how_many_are_of_the_type_asked_about(
    catalog, facets, monkeypatch,
):
    _article_share(monkeypatch, articles=1, everything=41)
    catalog.rows = [_row(i, bundle="feature_articles") for i in range(3)]
    r = tools.list_records(None, RecordFilters(author="Dr Vibha Dhawan"), limit=3,
                           detail=True, named_type="article")
    assert r.rendered.startswith(
        "Here are the 3 most recent of **41 publications** by **Dr Vibha Dhawan**:")
    assert ("That counts every kind of publication; only 1 of the 41 is filed under "
            "*Articles* on the site.") in r.rendered


def test_a_persons_list_of_one_type_names_what_else_they_published(catalog, facets):
    catalog.total, catalog.wider_total = 7, 41
    catalog.types = [("feature_articles", 31), ("research_papers", 7),
                     ("policy_brief", 2), ("article", 1)]
    catalog.rows = [_row(i, bundle="research_papers") for i in range(3)]
    r = tools.list_records("research_papers", RecordFilters(author="Dr Vibha Dhawan"),
                           detail=True)
    assert ("### All publications by Dr Vibha Dhawan (41)\n"
            "- Feature articles: 31\n- **Research papers: 7**\n- Policy briefs: 2\n"
            "- Articles: 1") in r.rendered


def test_a_persons_cut_list_across_types_gives_its_mix(catalog, facets):
    catalog.total = 41
    catalog.types = [("feature_articles", 31), ("research_papers", 10)]
    catalog.rows = [_row(i, bundle="feature_articles") for i in range(3)]
    r = tools.list_records(None, RecordFilters(author="Dr Vibha Dhawan"), limit=3,
                           detail=True)
    assert "### By type\n- Feature articles: 31\n- Research papers: 10" in r.rendered


def test_a_list_without_a_person_gets_no_mix(catalog, facets):
    catalog.wider_total = 500
    catalog.types = [("news", 300), ("article", 200)]
    r = tools.list_records("article", RecordFilters(theme="Energy"), limit=3, detail=True)
    assert "### All" not in r.rendered and "### By type" not in r.rendered


def test_a_widened_single_document_shows_its_own_type(catalog, facets, monkeypatch):
    _article_share(monkeypatch, articles=0, everything=1)
    catalog.rows = [_row(0, bundle="feature_articles")]
    r = tools.list_records(None, RecordFilters(author="Dr A"), detail=True,
                           named_type="article")
    assert "Feature article · 20 Apr 2026" in r.rendered
    assert "category" not in r.rendered


def test_a_long_type_mix_folds_the_tail(catalog):
    catalog.total = 100
    catalog.types = [(b, n) for b, n in zip(
        ("news", "article", "report", "events", "videos", "policy_brief"),
        (40, 20, 15, 10, 8, 4),
    )]
    r = _count(None)
    assert ("### By type\n- News items: 40\n- Articles: 20\n- Reports: 15\n"
            "- Events: 10\n- Videos: 8\n- Other types: 7") in r.rendered


def test_a_small_count_shows_every_item_and_offers_nothing_more(catalog):
    catalog.total, catalog.rows = 2, [_row(0), _row(1)]
    catalog.years = [("2024", 1), ("2026", 1)]
    r = _count()
    # A peak of one is no peak.
    assert "They span 2024 to 2026." in r.rendered
    assert "### Both articles\n" in r.rendered
    assert "You can ask" not in r.rendered


def test_a_single_document_is_simply_shown(catalog):
    catalog.total, catalog.rows, catalog.years = 1, [_row(0)], [("2026", 1)]
    r = _count()
    assert r.rendered.split("\n\n") == [
        "There is **1 article** on the site.",
        "### The article\n- [Title 0](https://teriin.org/a/0) — 20 Apr 2026",
    ]


def test_every_item_shown_says_so(catalog):
    catalog.total, catalog.rows = 3, [_row(i) for i in range(3)]
    assert "### All 3 articles\n" in _count().rendered


def test_a_year_the_question_fixed_is_not_repeated(catalog):
    catalog.years = [("2025", 68)]
    r = _count(date_from="2025-01-01", date_to="2026-01-01")
    assert "They span" not in r.rendered
    assert "All of them" not in r.rendered


def test_one_year_is_stated_when_the_question_did_not_fix_it(catalog):
    catalog.years = [("2025", 68)]
    assert "All of them are from 2025." in _count().rendered


def test_a_tied_peak_names_both_years(catalog):
    catalog.years = [("2020", 12), ("2021", 12), ("2022", 1)]
    assert ("and 2020 and 2021 were the busiest years, with 12 each."
            in _count().rendered)


# --------------------------------------------------------------------------- #
# An honest zero.
# --------------------------------------------------------------------------- #

def test_a_zero_in_a_period_says_what_the_other_dates_hold(catalog):
    catalog.total, catalog.wider_total = 0, 1082
    catalog.rows = [_row(0, bundle="events", date="2026-09-09T00:00:00")]
    r = _count("events", date_from="2030-01-01", date_to="2031-01-01")
    assert r.rendered.split("\n\n") == [
        "There are no events on the site in 2030.",
        "Across all dates there are **1,082 events**; the most recent is from 9 Sep 2026.",
    ]


def test_a_zero_under_a_content_type_looks_across_types(catalog, monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [{"theme": "Waste", "theme_type": "primary", "parent": None,
                       "theme_group": "main", "documents": 3}],
    )
    catalog.total, catalog.wider_total = 0, 40
    catalog.types = [("news", 25), ("article", 15)]
    r = _count("events", theme="Waste")
    assert r.rendered.startswith(
        "There are no events on **Waste**.\n\n"
        "### All items on Waste (40)\n- News items: 25\n- Articles: 15\n\n"
        "### Latest items\n")


def test_a_persons_zero_says_what_they_did_publish_and_shows_the_newest(
    catalog, monkeypatch,
):
    """"How many reports by Vibha Dhawan" was "none" and a bare total."""
    from app.retrieval.structured import resolve

    monkeypatch.setattr("app.catalog.queries.distinct_authors",
                        lambda **kw: ["Dr Vibha Dhawan"])
    resolve.reload_authors()
    catalog.total, catalog.wider_total = 0, 41
    catalog.types = [("feature_articles", 31), ("research_papers", 7),
                     ("policy_brief", 2), ("article", 1)]
    catalog.rows = [_row(0, bundle="feature_articles"), _row(1, bundle="research_papers")]
    try:
        r = _count("report", author="Dr Vibha Dhawan")
    finally:
        resolve.reload_authors()
    assert r.rendered.split("\n\n") == [
        "**Dr Vibha Dhawan** has no reports on the site.",
        "### All publications by Dr Vibha Dhawan (41)\n"
        "- Feature articles: 31\n- Research papers: 7\n- Policy briefs: 2\n- Articles: 1",
        "### Latest publications\n"
        "- [Title 0](https://teriin.org/a/0) — Feature article · 20 Apr 2026\n"
        "- [Title 1](https://teriin.org/a/1) — Research paper · 20 Apr 2026",
    ]
    assert len(r.citations) == 2


def test_a_zero_with_nothing_nearby_stays_a_bare_zero(catalog):
    catalog.total, catalog.wider_total = 0, 0
    r = _count("events", date_from="2030-01-01", date_to="2031-01-01")
    assert r.rendered == "There are no events on the site in 2030."


# --------------------------------------------------------------------------- #
# A count of distinct values.
# --------------------------------------------------------------------------- #

def test_an_author_count_names_the_most_frequent_names(catalog):
    catalog.total = 975
    catalog.values = [("Dr R K Pachauri", 270), ("Mr Ajay Shankar", 124),
                      ("Dr Leena Srivastava", 81), ("Dr Syamal Kumar Sarkar", 62),
                      ("Dr Debajit Palit", 60)]
    r = tools.count_records(None, RecordFilters(), count_of="author", detail=True)
    assert r.rendered.split("\n\n") == [
        "There are **975 distinct author names** in the source data.",
        "### Names that appear most often\n"
        "- Dr R K Pachauri: 270\n- Mr Ajay Shankar: 124\n- Dr Leena Srivastava: 81\n"
        "- Dr Syamal Kumar Sarkar: 62\n- Dr Debajit Palit: 60",
        "*Ask for a breakdown by author to see more of them.*",
    ]


def test_a_short_distinct_count_names_every_value(catalog):
    catalog.total = 2
    catalog.types = [("news", 3), ("report", 2)]
    r = tools.count_records(None, RecordFilters(author="A"), count_of="content_type",
                            detail=True)
    assert r.rendered == (
        "**A**'s publications span **2 content types**.\n\n"
        "- News items: 3\n- Reports: 2"
    )


def test_a_year_count_states_its_range(catalog):
    catalog.total = 3
    r = tools.count_records(None, RecordFilters(), count_of="year", detail=True)
    assert r.rendered == (
        "There are **3 years**. "
        "They run from 2018 to 2026, and 2025 was the busiest year, with 13."
    )


# --------------------------------------------------------------------------- #
# Fail-open, and off.
# --------------------------------------------------------------------------- #

def test_a_failing_detail_query_costs_only_its_own_section(catalog, monkeypatch):
    def boom(group_by, **kw):
        raise RuntimeError("db blip")

    monkeypatch.setattr("app.catalog.queries.distribution", boom)
    r = _count()
    assert r.ok
    assert r.rendered.startswith("There are **68 articles** on the site.\n\n"
                                 "### Latest articles\n")


def test_detail_waits_for_the_flag(catalog, monkeypatch):
    """Through the planner, as the pipeline calls it: off, nothing but the count."""
    monkeypatch.setattr(get_settings(), "catalog_answer_detail_enabled", False)
    [r] = planner.execute(DatabasePlan(calls=[ToolCall(tool="count_records",
                                                       entity="article")]))
    assert r.rendered == "There are **68 articles** on the site."
    assert [name for name, _ in catalog.calls] == ["count"]


def test_the_planner_passes_detail_when_on(catalog, on):
    [r] = planner.execute(DatabasePlan(calls=[ToolCall(tool="count_records",
                                                       entity="article")]))
    assert "### Latest articles\n" in r.rendered


def test_detail_is_never_on_when_the_call_says_no(catalog, on):
    call = ToolCall(tool="count_records", entity="article", detail=False)
    [r] = planner.execute(DatabasePlan(calls=[call]))
    assert r.rendered == "There are **68 articles** on the site."


# --------------------------------------------------------------------------- #
# Which plans get detail.
# --------------------------------------------------------------------------- #

def test_one_question_gets_detail():
    calls = [ToolCall(tool="count_records")]
    answerer._gate_detail(calls, allowed=True)
    assert calls[0].detail is True


def test_a_resolve_step_does_not_make_a_plan_compound():
    calls = [ToolCall(tool="resolve_entity"), ToolCall(tool="count_records")]
    answerer._gate_detail(calls, allowed=True)
    assert calls[1].detail is True


def test_a_comparison_stays_a_set_of_headlines():
    calls = [ToolCall(tool="count_records"), ToolCall(tool="count_records")]
    answerer._gate_detail(calls, allowed=True)
    assert [c.detail for c in calls] == [False, False]


def test_a_caller_can_ask_for_the_headline_alone():
    calls = [ToolCall(tool="count_records")]
    answerer._gate_detail(calls, allowed=False)
    assert calls[0].detail is False


def test_join_reads_as_prose():
    assert detail._join([]) == ""
    assert detail._join(["a"]) == "a"
    assert detail._join(["a", "b"]) == "a and b"
    assert detail._join(["a", "b", "c"]) == "a, b and c"


# --------------------------------------------------------------------------- #
# A breakdown.
# --------------------------------------------------------------------------- #

def test_a_content_type_breakdown_names_types_as_people_say_them(catalog):
    catalog.types = [("feature_articles", 1547), ("press_release", 724)]
    r = tools.aggregate_records(None, "content_type", RecordFilters())
    assert r.rendered == (
        "Distribution of items by content type:\n"
        "- Feature articles: 1,547\n- Press releases: 724"
    )
    # The payload keeps the keys a caller filters on.
    assert r.data["groups"] == [["feature_articles", 1547], ["press_release", 724]]


def test_a_year_breakdown_reads_as_a_timeline_and_asks_for_every_year(catalog):
    catalog.years = [("2025", 13), ("2020", 12), ("2018", 10)]
    r = tools.aggregate_records("article", "year", RecordFilters())
    assert r.rendered.endswith("- 2018: 10\n- 2020: 12\n- 2025: 13")
    [(name, kw)] = [c for c in catalog.calls if c[0] == "distribution:year"]
    assert kw["limit"] == 100


def test_a_cut_breakdown_says_so(catalog):
    catalog.values = [(f"Author {i}", 100 - i) for i in range(21)]
    r = tools.aggregate_records(None, "author", RecordFilters())
    assert "Author 19" in r.rendered and "Author 20" not in r.rendered
    assert r.rendered.endswith("Showing the 20 authors with the most items.")
    assert len(r.data["groups"]) == 20


def test_a_whole_breakdown_does_not_claim_a_cut(catalog):
    catalog.values = [(f"Author {i}", 100 - i) for i in range(20)]
    r = tools.aggregate_records(None, "author", RecordFilters())
    assert "Showing" not in r.rendered


def test_a_cut_pair_breakdown_says_so(catalog, monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.cross_distribution",
        lambda first, second, **kw: [(f"A{i}", "Energy", 60 - i)
                                     for i in range(kw["limit"])],
    )
    r = tools.aggregate_records(None, "author", RecordFilters(),
                                secondary_group_by="theme")
    assert r.rendered.endswith("Showing the 50 pairs with the most items.")


def test_a_year_breakdown_with_detail_leads_with_its_total(catalog):
    catalog.years = [("2025", 13), ("2020", 12), ("2018", 10)]
    r = tools.aggregate_records("article", "year", RecordFilters(theme="Climate Change"),
                                detail=True)
    assert r.rendered.split("\n\n") == [
        "Here's how the **68 articles** on **Climate Change** break down by year:\n"
        "- 2018: 10\n- 2020: 12\n- 2025: 13",
        "They span 2018 to 2025, and 2025 was the busiest year, with 13.",
        "*You can ask me to list the items behind any of these.*",
    ]


def test_a_theme_breakdown_with_detail_names_its_leader_and_overlap(catalog):
    catalog.total = 50
    catalog.values = [("Energy", 40), ("Climate Change", 32), ("Water", 12)]
    r = tools.aggregate_records(None, "theme", RecordFilters(), detail=True)
    assert r.rendered.startswith("Here's how the **50 items** break down by theme:")
    assert ("The largest is **Energy** (40), followed by Climate Change (32) and "
            "Water (12). An item can carry more than one theme, so these add up to "
            "more than 50.") in r.rendered


def test_an_author_breakdown_names_who_has_the_most(catalog):
    catalog.values = [("Mr R R Rashmi", 36), ("Dr Shailly Kedia", 22), ("Mr Ajay Shankar", 15)]
    r = tools.aggregate_records(None, "author", RecordFilters(), detail=True)
    assert ("**Mr R R Rashmi** has the most (36), followed by Dr Shailly Kedia (22) and "
            "Mr Ajay Shankar (15).") in r.rendered


def test_a_tied_lead_names_no_leader(catalog):
    catalog.values = [("Energy", 40), ("Climate Change", 40)]
    r = tools.aggregate_records(None, "theme", RecordFilters(), detail=True)
    assert "largest" not in r.rendered


def test_a_breakdown_without_a_total_keeps_its_plain_lead(catalog, monkeypatch):
    def boom(**kw):
        raise RuntimeError("db blip")

    monkeypatch.setattr("app.catalog.queries.count_documents", boom)
    catalog.values = [("Energy", 40), ("Climate Change", 32)]
    r = tools.aggregate_records(None, "theme", RecordFilters(), detail=True)
    assert r.rendered.startswith("Distribution of items by theme:")


# --------------------------------------------------------------------------- #
# A list, and a single document.
# --------------------------------------------------------------------------- #

@pytest.fixture
def facets(monkeypatch):
    found = {}
    monkeypatch.setattr("app.catalog.queries.facets_for",
                        lambda ids: {i: found[i] for i in ids if i in found})
    return found


def test_a_single_document_is_shown_as_a_card(catalog, facets):
    catalog.rows = [_row(0)]
    facets["d0"] = {"authors": ["Dr Raghab Ray"], "themes": ["Climate Change", "Environment"]}
    r = tools.lookup_record("article", "Title 0", RecordFilters(), detail=True)
    assert r.rendered.split("\n\n") == [
        "Found **1 article** with 'Title 0' in the title:",
        "**[Title 0](https://teriin.org/a/0)**\n"
        "Article · 20 Apr 2026\n"
        "By Dr Raghab Ray\n"
        "Themes: Climate Change, Environment",
        "*Ask me what it says, and I'll answer from the document itself.*",
    ]
    assert r.citations[0]["title"] == "Title 0"


def test_a_card_without_facets_still_shows_what_it_is(catalog, facets):
    catalog.rows = [_row(0)]
    r = tools.lookup_record("article", "Title 0", RecordFilters(), detail=True)
    assert "**[Title 0](https://teriin.org/a/0)**\nArticle · 20 Apr 2026" in r.rendered
    assert "By " not in r.rendered and "Themes:" not in r.rendered


def test_listed_items_carry_bylines(catalog, facets):
    catalog.rows = [_row(0), _row(1)]
    facets["d0"] = {"authors": ["& Sharma, A.", "Dr B", "Dr C", "Dr D"], "themes": []}
    r = tools.list_records("article", RecordFilters(), detail=True)
    assert ("- [Title 0](https://teriin.org/a/0) — 20 Apr 2026 · by Dr B, Dr C "
            "and 2 others") in r.rendered
    assert "- [Title 1](https://teriin.org/a/1) — 20 Apr 2026\n" in r.rendered + "\n"


def test_a_cut_list_offers_the_dimensions_still_open(catalog, facets):
    catalog.rows = [_row(i) for i in range(3)]
    r = tools.list_records("article", RecordFilters(theme="Energy"), limit=3,
                           detail=True)
    assert r.rendered.endswith("*You can ask me to narrow these down by year or author.*")


def test_a_whole_list_offers_nothing(catalog, facets):
    catalog.rows = [_row(i) for i in range(3)]
    r = tools.list_records("article", RecordFilters(), limit=10, detail=True)
    assert "You can ask" not in r.rendered


def test_a_failing_facet_read_leaves_the_plain_list(catalog, monkeypatch):
    def boom(ids):
        raise RuntimeError("db blip")

    monkeypatch.setattr("app.catalog.queries.facets_for", boom)
    catalog.rows = [_row(0), _row(1)]
    r = tools.list_records("article", RecordFilters(), detail=True)
    assert r.rendered == (
        "Found **2 articles**:\n"
        "- [Title 0](https://teriin.org/a/0) — 20 Apr 2026\n"
        "- [Title 1](https://teriin.org/a/1) — 20 Apr 2026"
    )


# --------------------------------------------------------------------------- #
# The theme listing.
# --------------------------------------------------------------------------- #

@pytest.fixture
def vocabulary(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [
            {"theme": name, "theme_type": "primary", "parent": None,
             "theme_group": "main", "documents": 1}
            for name in ("Climate Change", "Energy")
        ],
    )


def _newest_by_theme(monkeypatch, **titles):
    """Each theme's newest item; a theme left out has none."""
    def listing(**kw):
        title = titles.get(kw.get("theme"))
        if not title:
            return []
        row = _row(0)
        row.title, row.url = title, f"https://teriin.org/{title.lower().replace(' ', '-')}"
        return [row]

    monkeypatch.setattr("app.catalog.queries.list_documents", listing)


def test_a_theme_listing_says_how_much_each_theme_holds(catalog, vocabulary, monkeypatch):
    catalog.values = [("Climate Change", 1220), ("Energy", 1), ("Air", 359)]
    _newest_by_theme(monkeypatch, **{"Climate Change": "Rooted Resilience"})
    r = tools.list_themes(detail=True)
    assert r.rendered.split("\n\n") == [
        "The collection covers **2 main themes**:",
        "- **Climate Change** — 1,220 items\n"
        "  - Latest: [Rooted Resilience](https://teriin.org/rooted-resilience) · 20 Apr 2026\n"
        "- **Energy** — 1 item",
        "*You can ask me about the work under any of these — for example, how "
        "many reports there are on one, or which are the latest.*",
    ]
    # Counted the way a theme count counts: website content, every group.
    [(_, kw)] = [c for c in catalog.calls if c[0] == "distribution:theme"]
    assert kw["source_type"] == "website" and kw["entity_type"] == "node"
    assert "theme_group" not in kw


def test_a_theme_table_gains_an_items_column(catalog, vocabulary):
    catalog.values = [("Climate Change", 1220), ("Energy", 1154)]
    r = tools.list_themes(output_format="table", detail=True)
    assert "| theme | items | latest |\n| --- | ---: | --- |" in r.rendered
    assert "| Climate Change | 1,220 |" in r.rendered


def test_a_theme_listing_without_counts_is_still_a_listing(catalog, vocabulary, monkeypatch):
    def boom(group_by, **kw):
        raise RuntimeError("db blip")

    monkeypatch.setattr("app.catalog.queries.distribution", boom)
    monkeypatch.setattr("app.catalog.queries.list_documents", boom)
    r = tools.list_themes(detail=True)
    assert r.rendered.startswith("The collection covers **2 main themes**:\n\n"
                                 "- Climate Change\n- Energy")


def test_a_theme_digest_gives_each_themes_figures_and_newest(catalog, vocabulary,
                                                               monkeypatch):
    """What follows the home page's own list of themes."""
    catalog.values = [("Climate Change", 1220), ("Energy", 1154)]
    _newest_by_theme(monkeypatch, **{"Climate Change": "Rooted Resilience",
                                     "Energy": "Storage at POWERGEN"})
    assert tools.theme_digest() == (
        "### What each theme holds\n"
        "- **Climate Change** — 1,220 items\n"
        "  - Latest: [Rooted Resilience](https://teriin.org/rooted-resilience) · 20 Apr 2026\n"
        "- **Energy** — 1,154 items\n"
        "  - Latest: [Storage at POWERGEN](https://teriin.org/storage-at-powergen)"
        " · 20 Apr 2026"
    )


def test_a_theme_digest_without_figures_is_empty(catalog, vocabulary, monkeypatch):
    def boom(group_by, **kw):
        raise RuntimeError("db blip")

    monkeypatch.setattr("app.catalog.queries.distribution", boom)
    assert tools.theme_digest() == ""


def test_a_theme_digest_keeps_to_the_themes_the_answer_listed(catalog, vocabulary,
                                                                monkeypatch):
    """The catalog's main themes include some the home page does not list."""
    catalog.values = [("Climate Change", 1220), ("Energy", 1154)]
    _newest_by_theme(monkeypatch)
    assert tools.theme_digest(names=["Energy", "Water"]) == (
        "### What each theme holds\n- **Energy** — 1,154 items")
    assert tools.theme_digest(names=["Water"]) == ""


def _area(name, description="", url=None):
    return SimpleNamespace(name=name, description=description, url=url)


def test_a_theme_overview_describes_each_theme_and_what_it_holds(catalog, monkeypatch):
    catalog.values = [("Climate Change", 1220), ("Environment & Public Health", 1),
                      ("Water", 30)]
    _newest_by_theme(monkeypatch, **{"Climate Change": "Rooted Resilience"})
    text = tools.theme_overview([
        _area("Climate Change", "Covers climate science.", "https://teriin.org/climate"),
        # "and" for the catalog's "&".
        _area("Environment and Public Health", "Examines health."),
        _area("Sustainable Habitat", "Promotes resilient settlements."),
    ])
    assert text.split("\n\n") == [
        "There are **3 thematic areas**:",
        "- **[Climate Change](https://teriin.org/climate)** — Covers climate science.\n"
        "  - 1,220 items · Latest: [Rooted Resilience](https://teriin.org/rooted-resilience)"
        " · 20 Apr 2026\n"
        "- **Environment and Public Health** — Examines health.\n"
        "  - 1 item\n"
        # Nothing the catalog knows: the description alone.
        "- **Sustainable Habitat** — Promotes resilient settlements.",
        "*You can ask me about the work under any of these — for example, how "
        "many reports there are on one, or which are the latest.*",
    ]


def test_a_theme_overview_table(catalog, monkeypatch):
    catalog.values = [("Energy", 1154)]
    _newest_by_theme(monkeypatch)
    text = tools.theme_overview([_area("Energy", "Covers energy | power.")],
                                output_format="table")
    assert "| theme | what it covers | items | latest |\n| --- | --- | ---: | --- |\n" in text
    assert "| Energy | Covers energy \\| power. | 1,154 |  |" in text


def test_a_theme_overview_of_nothing_is_empty(catalog):
    assert tools.theme_overview([]) == ""
