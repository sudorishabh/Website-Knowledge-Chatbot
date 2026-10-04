"""How catalog rows read in an answer: dates, links, one-line items, list leads."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.ingestion.extractors.drupal_extractor import DEFAULT_BUNDLES
from app.retrieval.structured import rendering, tools
from app.retrieval.structured.types import RecordFilters


def _row(title="Clean Air Starts at Home", url="https://teriin.org/article/clean-air",
         date="2026-04-20T11:27:20", precision="day", bundle="article", document_id="d1"):
    return SimpleNamespace(title=title, url=url, effective_start_date=date,
                           start_precision=precision, bundle=bundle,
                           document_id=document_id)


@pytest.fixture(autouse=True)
def _offline_catalog(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr("app.catalog.queries.find_tag", lambda name: None)
    monkeypatch.setattr("app.catalog.queries.distinct_authors", lambda **kw: [])
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: DEFAULT_BUNDLES
    )


# --------------------------------------------------------------------------- #
# display_date — never more precise than the source.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "value, precision, expected",
    [
        ("2026-04-20T11:27:20", "day", "20 Apr 2026"),
        ("2026-04-20T11:27:20", None, "20 Apr 2026"),
        ("2026-04-01T00:00:00", "month", "Apr 2026"),
        ("2026-01-01T00:00:00", "year", "2026"),
        (datetime(2024, 5, 1), None, "1 May 2024"),
        (None, None, ""),
        ("", "day", ""),
    ],
)
def test_display_date_keeps_the_sources_precision(value, precision, expected):
    assert rendering.display_date(value, precision) == expected


def test_display_date_passes_through_what_it_cannot_parse():
    assert rendering.display_date("sometime in 2024") == "sometime i"


# --------------------------------------------------------------------------- #
# md_link — safe for the UI's markdown.
# --------------------------------------------------------------------------- #

def test_md_link_links_the_title():
    assert rendering.md_link("Solar", "https://teriin.org/s") == "[Solar](https://teriin.org/s)"


def test_md_link_keeps_brackets_and_parentheses_from_ending_the_link_early():
    link = rendering.md_link("Report [Draft]", "https://teriin.org/files/a (1).pdf")
    assert link == "[Report (Draft)](https://teriin.org/files/a%20%281%29.pdf)"


def test_md_link_never_links_a_non_web_address():
    assert rendering.md_link("Solar", "javascript:alert(1)") == "Solar"
    assert rendering.md_link("Solar", None) == "Solar"


def test_md_link_collapses_whitespace_in_the_title():
    assert rendering.md_link("Two\n  lines", "https://a.org") == "[Two lines](https://a.org)"


# --------------------------------------------------------------------------- #
# item_line
# --------------------------------------------------------------------------- #

def test_item_line_links_and_dates():
    assert rendering.item_line(_row()) == (
        "- [Clean Air Starts at Home](https://teriin.org/article/clean-air) — 20 Apr 2026"
    )


def test_item_line_names_the_kind_only_when_asked():
    line = rendering.item_line(_row(bundle="research_papers"), with_type=True)
    assert line.endswith("— Research paper · 20 Apr 2026")


def test_figures_are_bold_and_grouped_by_thousands():
    assert rendering.number(1220) == "1,220"
    assert rendering.figure(41, "publications") == "**41 publications**"


def test_a_section_is_a_titled_block_and_vanishes_when_empty():
    assert rendering.section("By type", "- News items: 3") == "### By type\n- News items: 3"
    assert rendering.section("By type", "") == ""


def test_a_tally_bolds_what_was_asked_about():
    assert rendering.tally([("Feature articles", 31), ("Articles", 1)],
                           highlight="Articles") == (
        "- Feature articles: 31\n- **Articles: 1**")


def test_a_followup_is_set_apart():
    assert rendering.followup("Ask me more.") == "*Ask me more.*"
    assert rendering.followup("") == ""


def test_item_line_without_a_date_or_link():
    assert rendering.item_line(_row(url=None, date=None)) == "- Clean Air Starts at Home"


def test_a_list_by_one_author_names_only_the_co_authors():
    """Every line of "list articles by Vibha Dhawan" said "by Dr Vibha Dhawan"."""
    both = ["Dr Vibha Dhawan", "Dr Pushplata Singh"]
    line = rendering.item_line(_row(), authors=both, listed_by="Dr Vibha Dhawan")
    assert line.endswith("— 20 Apr 2026 · with Dr Pushplata Singh")
    alone = rendering.item_line(_row(), authors=["Dr Vibha Dhawan"],
                                listed_by="Dr Vibha Dhawan")
    assert alone.endswith("— 20 Apr 2026")
    assert rendering.item_line(_row(), authors=both).endswith(
        "· by Dr Pushplata Singh and Dr Vibha Dhawan")


# --------------------------------------------------------------------------- #
# list_records leads and shapes
# --------------------------------------------------------------------------- #

def test_a_list_spanning_content_types_names_each_items_kind(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents",
                        lambda **k: [_row(bundle="news"), _row(bundle="report")])
    r = tools.list_records(None, RecordFilters(author="A"))
    assert "— News item · 20 Apr 2026" in r.rendered
    assert "— Report · 20 Apr 2026" in r.rendered


def test_a_list_of_one_content_type_does_not_repeat_it(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_row()])
    r = tools.list_records("article", RecordFilters())
    assert "Article ·" not in r.rendered


def test_a_cut_list_leads_with_its_total(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents",
                        lambda **k: [_row(document_id=f"d{i}") for i in range(3)])
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 68)
    r = tools.list_records("article", RecordFilters(theme="Climate Change"), limit=3)
    assert r.rendered.startswith(
        "Here are the 3 most recent of **68 articles** on **Climate Change**:"
    )
    assert "Showing" not in r.rendered


def test_a_topic_ranked_list_is_not_called_the_most_recent(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents",
                        lambda **k: [_row(document_id=f"d{i}") for i in range(2)])
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 57)
    r = tools.list_records(
        "research_papers", RecordFilters(topic_terms=("air", "pollution")), limit=2,
    )
    assert r.rendered.startswith(
        "Here are 2 of the **57 research papers** mentioning 'air' or 'pollution' "
        "in the title, closest matches first:"
    )


def test_a_whole_list_says_found(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_row()])
    r = tools.list_records("article", RecordFilters(topic_terms=("air",)), limit=10)
    assert r.rendered.startswith("Found **1 article** mentioning 'air' in the title:")


def test_the_table_names_the_kind_and_dates_precisely(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.list_documents",
        lambda **k: [_row(bundle="policy_brief", date="2024-01-01", precision="year")],
    )
    r = tools.list_records(None, RecordFilters(), output_format="table")
    assert "| 2024 | policy brief |" in r.rendered
    assert "[Clean Air Starts at Home](https://teriin.org/article/clean-air)" in r.rendered


# --------------------------------------------------------------------------- #
# names and card
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "people, most, expected",
    [
        ([], 2, ""),
        (["A"], 2, "A"),
        (["B", "A"], 2, "A and B"),
        (["A", "B", "C"], 2, "A, B and 1 other"),
        (["A", "B", "C", "D"], 2, "A, B and 2 others"),
        (["A", "B", "C", "D"], 6, "A, B, C and D"),
        (["& Sharma, A.", "Sharma, A.", " "], 2, "Sharma, A."),
    ],
)
def test_names_reads_as_a_byline(people, most, expected):
    assert rendering.names(people, most=most) == expected


def test_card_states_one_fact_per_line():
    text = rendering.card(_row(bundle="research_papers"), authors=["B", "A"],
                          themes=["Energy"])
    assert text == (
        "**[Clean Air Starts at Home](https://teriin.org/article/clean-air)**\n"
        "Research paper · 20 Apr 2026\nBy A and B\nThemes: Energy"
    )
