"""A name no facet places, counted and listed by its titles
(app.retrieval.structured.names).

Regression cover for "tell me the count of all the WSDS related events that
happened in 2026", answered "the page does not give a count" while the catalog
held 19. Query understanding put the series in the theme slot, where no theme
has the name, or in the title slot in one spelling, or nowhere at all. The
tests pin the rule that replaces it: the name is matched in titles, word-bounded,
in every spelling titles use, whichever slot it came in.

The title table is stubbed throughout, and the feature is off by default in the
suite (see tests/conftest.py), so each test that wants it turns it on.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.ingestion.extractors.drupal_extractor import DEFAULT_BUNDLES
from app.retrieval.structured import detail, names, planner, tools
from app.retrieval.structured.types import RecordFilters

# Enough ordinary titles that a name in a handful of them is rare, as on the
# site, and the organisation's own name — in one title of eight here, 13% on
# the site — is not.
_FILLER = [f"Workshop on water management in district {i}" for i in range(200)]
_TITLES = _FILLER + [f"TERI annual meeting {i}" for i in range(30)] + [
    "WSDS 2026 Thematic Track: Green Ports as a Gateway to Decarbonization",
    "WSDS 2026 Thematic Track: Accelerating AgriPV in India",
    "CEO Forum at WSDS 2025",
    "World Sustainable Development Summit 2027: Curtain Raiser",
    "World Sustainable Development Summit 2024 concludes",
    "ITEC course on renewable energy",
    "ITEC course on climate finance",
    "Green architecture for cities",
    "COP 30 side event on cooling",
    "COP30 press conference",
    "Darbari Seth Memorial Lecture 2026",
    "Darbari Seth Memorial Lecture 2025",
    "Green Olympiad 2026: Multistakeholders' Meeting",
    "Progress on the Sustainable Development Goals",
    "Localising the Sustainable Development Goals",
    "Sustainable Development Goals and cities",
    "Financing the Sustainable Development Goals",
    "Sustainable Development Goals in practice",
    "Sustainable Development Goals: a review",
    "Sustainable Development Goals for youth",
    "Sustainable Development Goals and water",
    "Sustainable Development Goals and health",
    "Sustainable Development Goals and gender",
    "Sustainable Development in Goa: a case study",
    "Sustainable Development in Goa revisited",
    "SDG 7 and energy access",
    "SDG 13 and climate action",
    "National Workshop on Clean Air Zones for Urban Air Quality",
    "Forest Fire Management and Air Quality in Nepal",
    "World Environment Day 2025: Beat Plastic Pollution",
]


@pytest.fixture
def on(monkeypatch):
    """The feature on, over the stub title table."""
    monkeypatch.setattr(get_settings(), "catalog_title_names_enabled", True)
    monkeypatch.setattr(
        "app.catalog.state.website_titles",
        lambda: [(f"d{i}", title, "events") for i, title in enumerate(_TITLES)],
    )
    for cached in ("_titles", "_acronyms"):
        monkeypatch.setattr(names, cached, None)
    monkeypatch.setattr(names, "_loaded_at", 0.0)


# --------------------------------------------------------------------------- #
# Spellings and the pattern they become.
# --------------------------------------------------------------------------- #

def test_an_acronym_is_spelt_every_way_titles_use(on):
    """The curtain raiser that made 2026 nineteen events, not eighteen, says
    "World Sustainable Development Summit" and never "WSDS"."""
    assert names.spellings("WSDS") == ("WSDS", "World Sustainable Development Summit")


def test_a_name_typed_in_lower_case_takes_the_titles_capitals(on):
    assert names.spellings("wsds")[0] == "WSDS"


def test_a_full_name_gains_the_acronym_titles_use(on):
    assert names.spellings("World Sustainable Development Summit") == (
        "World Sustainable Development Summit", "WSDS",
    )


def test_a_leading_acronym_is_spelt_out_with_the_rest_of_the_name(on):
    assert "World Sustainable Development Summit 2024" in names.spellings("WSDS 2024")


def test_a_rare_coincidence_of_initials_is_not_a_spelling(on):
    """Two titles about "Sustainable Development in Goa" carry SDG's initials;
    ten say "Sustainable Development Goals"."""
    assert names.spellings("SDG") == ("SDG", "Sustainable Development Goals")


def test_the_pattern_is_word_bounded():
    """A substring is wrong for the short names this is for."""
    itec = re.compile(names.pattern(["ITEC"]))
    assert itec.search("ITEC course on renewable energy")
    assert not itec.search("Green architecture for cities")


def test_the_pattern_allows_the_ways_a_name_is_written():
    cop = re.compile(names.pattern(["COP30"]))
    assert all(cop.search(t) for t in ("COP30 press", "COP 30 side event", "cop-30"))
    lecture = re.compile(names.pattern(["Darbari Seth Memorial Lectures"]))
    assert lecture.search("Darbari Seth Memorial Lecture 2026")
    sdg = re.compile(names.pattern(["SDG"]))
    assert sdg.search("Localising the SDGs")


# --------------------------------------------------------------------------- #
# Which name a question gives, and from where.
# --------------------------------------------------------------------------- #

def test_a_name_in_the_theme_slot_is_matched_in_titles(on):
    placed = names.place(
        "tell me the count of all the WSDS related events that happened in 2026",
        theme="WSDS", theme_dropped=True,
    )
    assert placed.slot == "theme"
    assert placed.spellings == ("WSDS", "World Sustainable Development Summit")


def test_a_subject_in_the_theme_slot_is_left_alone(on):
    """The model capitalised the subject; the user did not."""
    assert names.place(
        "how many policy briefs are there on renewable energy",
        theme="Renewable Energy", theme_dropped=True,
    ) is None


def test_a_count_matches_a_dropped_subject_in_titles_too(on):
    """A list keeps a subject by its topic constraint; a count has nothing else
    to narrow it, and counted without it "how many events on air quality in
    2026" was every event of the year."""
    placed = names.place(
        "how many events on air quality were held in 2026",
        theme="air quality", theme_dropped=True, counting=True,
    )
    assert placed.slot == "theme" and placed.name == "air quality"


def test_a_name_matched_to_a_broader_theme_is_still_a_name(on, monkeypatch):
    """"World Environment Day" resolved to Environment, was dropped as broader,
    and the count ran with no subject at all: 1,082 events."""
    monkeypatch.setattr(planner, "_applied_theme", lambda t: None)
    call = planner.plan(
        _slots(theme="World Environment Day", date_from=None, date_to=None),
        question="how many World Environment Day events were there",
    ).calls[0]
    assert call.filters.theme is None
    assert call.filters.title_names == ("World Environment Day",)


def test_a_count_whose_subject_nothing_narrows_falls_through(on, monkeypatch):
    from app.retrieval.structured.filters import ResolvedScope

    monkeypatch.setattr(tools, "resolve_filters", lambda f: ResolvedScope(
        effective=f, theme_widened="Climate Change"))
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: DEFAULT_BUNDLES)
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 1082)
    r = tools.count_records("events", RecordFilters(theme="quantum climate teleportation"))
    assert not r.ok and r.error_kind is None


def test_a_title_guessed_from_a_name_is_matched_every_way(on):
    placed = names.place("how many CEO Forum events have been held", title="CEO Forum")
    assert placed.slot == "title"


def test_counting_the_series_itself_is_left_to_its_page(on):
    """No kind of content is counted: the editions are, which the series' page
    states and the catalog's event pages undercount."""
    assert names.place(
        "how many Darbari Seth Memorial Lectures have there been",
        title="Darbari Seth Memorial Lectures",
    ) is None


def test_a_title_question_keeps_its_exact_title(on):
    assert names.place(
        "how many events are titled WSDS", title="WSDS", covered=["WSDS"],
    ) is None


@pytest.mark.parametrize("question, name", [
    ("list all the WSDS events in 2026", "WSDS"),
    ("list all the wsds events in 2026", "WSDS"),
    ("how many Green Olympiad events happened", "Green Olympiad"),
    ("how many events were held on COP30", "COP30"),
])
def test_a_name_the_question_writes_is_found_when_no_slot_holds_it(on, question, name):
    assert names.place(question).name == name


@pytest.mark.parametrize("question", [
    "how many events did TERI hold in 2026",
    "how many events were held in 2026",
    "How Many Events Were Held In 2026",
])
def test_a_question_naming_nothing_is_planned_as_before(on, question):
    assert names.place(question) is None


def test_a_facet_already_applied_is_not_a_name(on):
    assert names.named_in(
        "how many publications are there by Vibha Dhawan", covered=["Vibha", "Dhawan"],
    ) is None


def test_a_name_no_title_carries_keeps_its_slot(on):
    assert names.place("how many ZYXQ events were held", theme="ZYXQ",
                       theme_dropped=True) is None


def test_nothing_is_read_with_the_flag_off():
    assert names.place("list all the WSDS events in 2026") is None


# --------------------------------------------------------------------------- #
# What a counting question counts.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("question, counted", [
    ("tell me the count of all the WSDS related events that happened in 2026",
     ("events",)),
    ("number of COP28 related events and news in 2023", ("events", "news")),
    ("count the news items about the G20", ("news items",)),
    ("how many WSDS press releases are there", ("press releases",)),
    ("how many people attended WSDS 2025", ()),
    ("how many MW of capacity does the report cite", ()),
    ("how many research staff does TERI have", ()),
    ("how many ITEC training programmes has TERI run", ()),
])
def test_what_a_count_counts(on, question, counted):
    assert names.counted_types(question) == counted


@pytest.mark.parametrize("phrase, bundle", [
    ("press releases", "press_release"),
    ("news items", "news"),
    ("events", "events"),
    ("workshops", "events"),
    ("publications", None),
    ("projects", "projects"),
])
def test_a_counted_phrase_names_its_content_type(phrase, bundle):
    assert names.type_bundle(phrase) == bundle


def test_a_count_read_as_qa_goes_to_the_catalog(on):
    from app.retrieval.understanding.query_processor import (
        QueryAnalysis, _route_catalog_count,
    )

    analysis = QueryAnalysis(intent="qa", search_query="x")
    _route_catalog_count("how many events were held on COP30", analysis)
    assert (analysis.intent, analysis.operation, analysis.bundle) == (
        "structured", "count", "events")


def test_a_count_of_two_types_spans_every_type(on):
    """Understanding set "events" for "COP28 related events and news", and the
    news went uncounted."""
    from app.retrieval.understanding.query_processor import (
        QueryAnalysis, _route_catalog_count,
    )

    analysis = QueryAnalysis(intent="structured", search_query="x",
                             operation="count", bundle="events")
    _route_catalog_count("number of COP28 related events and news in 2023", analysis)
    assert analysis.bundle is None


def test_a_count_of_something_else_stays_qa(on):
    from app.retrieval.understanding.query_processor import (
        QueryAnalysis, _route_catalog_count,
    )

    analysis = QueryAnalysis(intent="qa", search_query="x")
    _route_catalog_count("how many people attended WSDS 2025", analysis)
    assert analysis.intent == "qa"


# --------------------------------------------------------------------------- #
# The plan, the SQL and the answer.
# --------------------------------------------------------------------------- #

def _slots(**over):
    base = dict(
        operation="count", bundle="events", theme=None, tags=[], author=None,
        title_contains=None, group_by=None, secondary_group_by=None,
        count_of="records", limit=10, date_from="2026-01-01", date_to="2026-09-19",
        year=None,
    )
    return SimpleNamespace(**{**base, **over})


def test_the_name_replaces_the_theme_it_was_asked_as(on, monkeypatch):
    monkeypatch.setattr(planner, "_applied_theme", lambda t: None)
    call = planner.plan(
        _slots(theme="WSDS"),
        question="tell me the count of all the WSDS related events that happened in 2026",
    ).calls[0]
    assert call.filters.theme is None
    assert call.filters.title_names == ("WSDS", "World Sustainable Development Summit")


def test_a_list_asked_for_in_full_shows_every_one_up_to_the_cap(on):
    call = planner.plan(
        _slots(operation="list"), question="list all the WSDS events in 2026",
    ).calls[0]
    assert call.limit == detail.ALL_ITEMS


def test_the_name_reaches_sql_as_a_word_bounded_pattern():
    from app.catalog.queries import _catalog_filters

    _, clauses, params, _ = _catalog_filters(
        "website", "events", title_pattern=names.pattern(["WSDS"]),
    )
    assert "s.title REGEXP %s" in clauses
    assert names.pattern(["WSDS"]) in params


def test_every_reader_of_the_scope_gets_the_name(monkeypatch):
    seen = {}

    def count(**kwargs):
        seen.update(kwargs)
        return 19

    monkeypatch.setattr("app.catalog.queries.count_documents", count)
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: DEFAULT_BUNDLES)
    tools.count_records("events", RecordFilters(title_names=("WSDS",)))
    assert seen["title_pattern"] == names.pattern(["WSDS"])


def test_a_count_by_title_name_says_what_it_counted(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 19)
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: DEFAULT_BUNDLES)
    r = tools.count_records("events", RecordFilters(
        title_names=("WSDS", "World Sustainable Development Summit"),
        date_from="2026-01-01", date_to="2027-01-01",
    ))
    assert r.rendered == (
        "There are **19 events** related to **WSDS** in 2026. Each of them names "
        "WSDS or World Sustainable Development Summit in its title."
    )


def test_a_year_cut_at_the_catalogs_newest_date_reads_as_the_year(on, monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.effective_date_range", lambda **kw: ("2001-01-01", "2026-09-18"))
    filters = RecordFilters(date_from="2026-01-01", date_to="2026-09-19")
    assert tools._period_label(filters) == " in 2026"
    assert tools._period_label(RecordFilters(date_from="2026-01-01", date_to="2026-07-01")) \
        == " between 2026-01-01 and 2026-06-30"


# --------------------------------------------------------------------------- #
# A small count, listed whole.
# --------------------------------------------------------------------------- #

def _event(i, date):
    return SimpleNamespace(
        document_id=f"e{i}", title=f"Event {i}", url=f"https://teriin.org/event/{i}",
        effective_start_date=date, start_precision="day", bundle="events",
    )


@pytest.fixture
def nineteen(monkeypatch):
    rows = [_event(0, "2026-09-09")] + [_event(i, "2026-02-25") for i in range(1, 19)]
    monkeypatch.setattr(get_settings(), "catalog_answer_detail_enabled", True)
    monkeypatch.setattr("app.catalog.queries.list_documents",
                        lambda **kw: rows[: kw.get("limit", 10)])
    monkeypatch.setattr("app.catalog.queries.distribution", lambda *a, **kw: [])
    monkeypatch.setattr("app.catalog.queries.facets_for", lambda ids: {})
    return rows


def test_a_small_count_lists_every_item_under_its_month(nineteen):
    d = detail.for_count(19, common={}, bundle="events", filters=RecordFilters(
        date_from="2026-01-01", date_to="2027-01-01"))
    body = d.render_onto("There are **19 events**.")
    assert "### September 2026 (1)" in body and "### February 2026 (18)" in body
    assert body.count("\n- ") == 19
    assert "*You can ask me" not in body


def test_a_large_count_keeps_its_newest_five(nineteen):
    d = detail.for_count(68, common={}, bundle="events", filters=RecordFilters())
    body = d.render_onto("There are **68 events**.")
    assert "### Latest events" in body and body.count("\n- ") == detail.RECENT_ITEMS


def test_a_zero_under_a_type_and_a_period_says_what_else_the_period_holds(monkeypatch):
    """No GRIHA *events* in 2025, while its summit and conclaves that year are
    in the news and the press releases."""
    def count(**kw):
        if kw.get("bundle") is None:
            return 11
        return 0 if kw.get("effective_from") else 21

    monkeypatch.setattr(get_settings(), "catalog_answer_detail_enabled", True)
    monkeypatch.setattr("app.catalog.queries.count_documents", count)
    monkeypatch.setattr("app.catalog.queries.distribution", lambda group_by, **kw: (
        [("news", 7), ("press_release", 4)] if group_by == "bundle" else []))
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **kw: [
        _event(i, "2025-11-04") for i in range(min(kw.get("limit", 10), 3))])
    monkeypatch.setattr("app.catalog.queries.facets_for", lambda ids: {})
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: DEFAULT_BUNDLES)
    r = tools.count_records("events", RecordFilters(
        title_names=("GRIHA",), date_from="2025-01-01", date_to="2026-01-01",
    ), detail=True)
    parts = r.rendered.split("\n\n")
    assert parts[0] == "There are no events related to **GRIHA** in 2025."
    assert parts[1].startswith("Across all dates there are **21 events** related to **GRIHA**")
    assert parts[2] == ("### All items related to GRIHA in 2025 (11)\n"
                        "- News items: 7\n- Press releases: 4")


def test_the_pages_only_the_text_names_are_said_to_be_left_out():
    pages = [{"title": f"Page {i}", "url": f"https://teriin.org/{i}"} for i in range(6)]
    sentence = detail.mentions_sentence(pages, name="WSDS", noun="events", period=" in 2026")
    assert sentence.startswith(
        "6 more events in 2026 name WSDS in their text rather than their title: "
        "[Page 0](https://teriin.org/0), ")
    assert sentence.endswith("and 1 more.")
    one = detail.mentions_sentence(pages[:1], name="WSDS", noun="event")
    assert one == ("1 more event names WSDS in its text rather than its title: "
                   "[Page 0](https://teriin.org/0).")


# --------------------------------------------------------------------------- #
# Passage search, when a question falls through to it.
# --------------------------------------------------------------------------- #

def test_the_title_leg_keeps_the_questions_period(monkeypatch):
    """"WSDS events in 2026" matched a 2021 curtain raiser by title, the one
    candidate of the pull, which kept the empty-pull retry from firing."""
    from app.retrieval.search import scoped_retrieval, title_leg

    seen = {}
    monkeypatch.setattr(title_leg, "title_candidates", lambda q, **kw: ["d1"])

    def within(vector, ids, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(scoped_retrieval, "search_within_documents", within)
    period = [SimpleNamespace(key="effective_start_date")]
    title_leg.title_search("WSDS events in 2026", [0.0], limit=5, date_scope=period)
    assert seen["extra_filter"] == period
