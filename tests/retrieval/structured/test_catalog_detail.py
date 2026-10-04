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
        "There are 68 articles on 'Climate Change' matching your query.",
        "They date from 2018 to 2026, with the most (13) in 2025.",
        "The most recent:\n"
        "- [Title 0](https://teriin.org/a/0) — 20 Apr 2026\n"
        "- [Title 1](https://teriin.org/a/1) — 20 Apr 2026\n"
        "- [Title 2](https://teriin.org/a/2) — 20 Apr 2026",
        "You can ask me to list them, or to break them down by year.",
    ]
    assert [c["title"] for c in r.citations] == ["Title 0", "Title 1", "Title 2"]
    assert r.data["count"] == 68  # the payload is unchanged


def test_every_detail_query_shares_the_headlines_scope(catalog):
    _count(theme="Climate Change")
    headline = catalog.calls[0][1]
    for name, kw in catalog.calls[1:]:
        shared = {k: v for k, v in kw.items() if k != "limit"}
        assert shared == headline, name


def test_a_count_across_content_types_gives_its_mix(catalog):
    catalog.types = [("article", 12), ("research_papers", 9), ("news", 3)]
    catalog.rows = [_row(0, bundle="news"), _row(1, bundle="research_papers"), _row(2)]
    r = _count(None, author="Suneel Pandey")
    assert ("They date from 2018 to 2026, with the most (13) in 2025. "
            "By type: 12 articles, 9 research papers and 3 news items.") in r.rendered
    assert "— news item, 20 Apr 2026" in r.rendered
    assert r.rendered.endswith(
        "You can ask me to list them, or to break them down by year or content type."
    )


def test_a_long_type_mix_folds_the_tail(catalog):
    catalog.total = 100
    catalog.types = [(b, n) for b, n in zip(
        ("news", "article", "report", "events", "videos", "policy_brief"),
        (40, 20, 15, 10, 8, 4),
    )]
    r = _count(None)
    assert ("By type: 40 news items, 20 articles, 15 reports, 10 events, 8 videos "
            "and 7 of other types.") in r.rendered


def test_a_small_count_shows_every_item_and_offers_nothing_more(catalog):
    catalog.total, catalog.rows = 2, [_row(0), _row(1)]
    catalog.years = [("2024", 1), ("2026", 1)]
    r = _count()
    # A peak of one is no peak.
    assert "They date from 2024 to 2026." in r.rendered
    assert "Here they are:" in r.rendered
    assert "You can ask" not in r.rendered


def test_a_single_document_is_simply_shown(catalog):
    catalog.total, catalog.rows, catalog.years = 1, [_row(0)], [("2026", 1)]
    r = _count()
    assert r.rendered.split("\n\n") == [
        "There is 1 article matching your query.",
        "Here it is:\n- [Title 0](https://teriin.org/a/0) — 20 Apr 2026",
    ]


def test_a_year_the_question_fixed_is_not_repeated(catalog):
    catalog.years = [("2025", 68)]
    r = _count(date_from="2025-01-01", date_to="2026-01-01")
    assert "date from" not in r.rendered
    assert "All of them" not in r.rendered


def test_one_year_is_stated_when_the_question_did_not_fix_it(catalog):
    catalog.years = [("2025", 68)]
    assert "All of them date from 2025." in _count().rendered


def test_a_tied_peak_names_both_years(catalog):
    catalog.years = [("2020", 12), ("2021", 12), ("2022", 1)]
    assert "with the most (12 each) in 2020 and 2021." in _count().rendered


# --------------------------------------------------------------------------- #
# An honest zero.
# --------------------------------------------------------------------------- #

def test_a_zero_in_a_period_says_what_the_other_dates_hold(catalog):
    catalog.total, catalog.wider_total = 0, 1082
    catalog.rows = [_row(0, bundle="events", date="2026-09-09T00:00:00")]
    r = _count("events", date_from="2030-01-01", date_to="2031-01-01")
    assert r.rendered.split("\n\n") == [
        "There are no events in 2030 matching your query.",
        "Across all dates there are 1082 events; the most recent is from 9 Sep 2026.",
    ]


def test_a_zero_under_a_content_type_looks_across_types(catalog, monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [{"theme": "Waste", "theme_type": "primary", "parent": None,
                       "theme_group": "main", "documents": 3}],
    )
    catalog.total, catalog.wider_total = 0, 40
    r = _count("events", theme="Waste")
    assert r.rendered.endswith("Across all content types there are 40 items on 'Waste'.")


def test_a_zero_with_nothing_nearby_stays_a_bare_zero(catalog):
    catalog.total, catalog.wider_total = 0, 0
    r = _count("events", date_from="2030-01-01", date_to="2031-01-01")
    assert r.rendered == "There are no events in 2030 matching your query."


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
        "There are 975 distinct author names recorded in the source data.",
        "The names that appear most often are Dr R K Pachauri (270), Mr Ajay "
        "Shankar (124), Dr Leena Srivastava (81), Dr Syamal Kumar Sarkar (62) "
        "and Dr Debajit Palit (60).",
        "Ask for a breakdown by author to see more of them.",
    ]


def test_a_short_distinct_count_names_every_value(catalog):
    catalog.total = 2
    catalog.types = [("news", 3), ("report", 2)]
    r = tools.count_records(None, RecordFilters(author="A"), count_of="content_type",
                            detail=True)
    assert r.rendered.endswith("They are news items (3) and reports (2).")


def test_a_year_count_states_its_range(catalog):
    catalog.total = 3
    r = tools.count_records(None, RecordFilters(), count_of="year", detail=True)
    assert r.rendered.endswith("They run from 2018 to 2026, with the most (13) in 2025.")


# --------------------------------------------------------------------------- #
# Fail-open, and off.
# --------------------------------------------------------------------------- #

def test_a_failing_detail_query_costs_only_its_own_section(catalog, monkeypatch):
    def boom(group_by, **kw):
        raise RuntimeError("db blip")

    monkeypatch.setattr("app.catalog.queries.distribution", boom)
    r = _count()
    assert r.ok
    assert r.rendered.startswith("There are 68 articles matching your query.\n\n"
                                 "The most recent:")


def test_detail_waits_for_the_flag(catalog, monkeypatch):
    """Through the planner, as the pipeline calls it: off, nothing but the count."""
    monkeypatch.setattr(get_settings(), "catalog_answer_detail_enabled", False)
    [r] = planner.execute(DatabasePlan(calls=[ToolCall(tool="count_records",
                                                       entity="article")]))
    assert r.rendered == "There are 68 articles matching your query."
    assert [name for name, _ in catalog.calls] == ["count"]


def test_the_planner_passes_detail_when_on(catalog, on):
    [r] = planner.execute(DatabasePlan(calls=[ToolCall(tool="count_records",
                                                       entity="article")]))
    assert "The most recent:" in r.rendered


def test_detail_is_never_on_when_the_call_says_no(catalog, on):
    call = ToolCall(tool="count_records", entity="article", detail=False)
    [r] = planner.execute(DatabasePlan(calls=[call]))
    assert r.rendered == "There are 68 articles matching your query."


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
        "- feature articles: 1547\n- press releases: 724"
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
        "Here's how the 68 articles on 'Climate Change' break down by year:\n"
        "- 2018: 10\n- 2020: 12\n- 2025: 13",
        "They date from 2018 to 2025, with the most (13) in 2025.",
        "You can ask me to list the items behind any of these.",
    ]


def test_a_theme_breakdown_with_detail_names_its_leader_and_overlap(catalog):
    catalog.total = 50
    catalog.values = [("Energy", 40), ("Climate Change", 32), ("Water", 12)]
    r = tools.aggregate_records(None, "theme", RecordFilters(), detail=True)
    assert r.rendered.startswith("Here's how the 50 items break down by theme:")
    assert ("The largest is Energy (40), followed by Climate Change (32) and "
            "Water (12). An item can carry more than one theme, so these add up to "
            "more than 50.") in r.rendered


def test_an_author_breakdown_names_who_has_the_most(catalog):
    catalog.values = [("Mr R R Rashmi", 36), ("Dr Shailly Kedia", 22), ("Mr Ajay Shankar", 15)]
    r = tools.aggregate_records(None, "author", RecordFilters(), detail=True)
    assert ("Mr R R Rashmi has the most (36), followed by Dr Shailly Kedia (22) and "
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
        "Found 1 article with 'Title 0' in the title:",
        "**[Title 0](https://teriin.org/a/0)**\n"
        "Article · 20 Apr 2026\n"
        "By Dr Raghab Ray\n"
        "Themes: Climate Change, Environment",
        "Ask me what it says, and I'll answer from the document itself.",
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
    assert r.rendered.endswith("You can ask me to narrow these down by year or author.")


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
        "Found 2 articles:\n"
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


def test_a_theme_listing_says_how_much_each_theme_holds(catalog, vocabulary):
    catalog.values = [("Climate Change", 1220), ("Energy", 1), ("Air", 359)]
    r = tools.list_themes(detail=True)
    assert r.rendered.split("\n\n") == [
        "The collection covers 2 main themes:",
        "- Climate Change — 1220 items\n- Energy — 1 item",
        "You can ask me about the work under any of these — for example, how "
        "many reports there are on one, or which are the latest.",
    ]
    # Counted the way a theme count counts: website content, every group.
    [(_, kw)] = [c for c in catalog.calls if c[0] == "distribution:theme"]
    assert kw["source_type"] == "website" and kw["entity_type"] == "node"
    assert "theme_group" not in kw


def test_a_theme_table_gains_an_items_column(catalog, vocabulary):
    catalog.values = [("Climate Change", 1220), ("Energy", 1154)]
    r = tools.list_themes(output_format="table", detail=True)
    assert "| theme | items |" in r.rendered
    assert "| Climate Change | 1220 |" in r.rendered


def test_a_theme_listing_without_counts_is_still_a_listing(catalog, vocabulary, monkeypatch):
    def boom(group_by, **kw):
        raise RuntimeError("db blip")

    monkeypatch.setattr("app.catalog.queries.distribution", boom)
    r = tools.list_themes(detail=True)
    assert r.rendered.startswith("The collection covers 2 main themes:\n\n"
                                 "- Climate Change\n- Energy")
