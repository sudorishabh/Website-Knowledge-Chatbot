"""The web planner: what a question needs from the web, decided before searching.

The planner runs on every question once web retrieval is on, so it must be free
(no model call) and reproducible (the trace has to explain why a question did or
did not reach the web). These tests pin its reading of the six benchmark
questions, each signal on its own, and the queries it builds.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.retrieval.web import planner
from app.retrieval.web.planner import EXPLICIT, FRESHNESS, WebQuery, plan


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "web_primary_domains", "teriin.org")
    monkeypatch.setattr(s, "web_blocked_domains", "")
    monkeypatch.setattr(s, "web_allow_third_party", True)
    monkeypatch.setattr(s, "web_organisation_names", "TERI, The Energy and Resources Institute")
    return s


@pytest.fixture(autouse=True)
def this_year_is_2026(monkeypatch):
    from datetime import date

    monkeypatch.setattr(planner, "today_utc", lambda: date(2026, 9, 23))


# --- the six benchmark questions ------------------------------------------------

BENCHMARK = {
    "T1": "Who is Mr Sayanta Ghosh at TERI, and how many publications does he have?",
    "T2": "According to TERI, how much do vehicular emissions contribute to Delhi's "
          "particulate pollution?",
    "T3": "What did TERI report about transport's contribution to Delhi air pollution "
          "around 2019, and what did TERI say in 2025?",
    "T4": "Find the TERI article containing a paragraph about seaweed biomass replacing "
          "aquafeed, NPK fertilizer, and Asparagopsis cattle feed.",
    "T5": "What is the latest TERI work on vehicle-related air pollution in Delhi?",
    "T6": "Give me a consolidated overview of TERI's work on vehicle air pollution from "
          "2019 to 2025.",
}


def test_t1_is_a_person_lookup_about_the_organisation():
    p = plan(BENCHMARK["T1"])
    assert p.person == "Sayanta Ghosh"
    assert p.org_scoped and not p.forced
    assert p.queries[0] == WebQuery("Sayanta Ghosh profile", site="teriin.org")


def test_t2_is_an_ordinary_organisation_question_the_corpus_may_answer():
    p = plan(BENCHMARK["T2"])
    assert p.org_scoped
    assert not (p.forced or p.person or p.find_passage or p.multi_document)
    assert "vehicular" in p.terms and "particulate" in p.terms


def test_t3_compares_two_separate_periods():
    p = plan(BENCHMARK["T3"])
    assert p.comparison and p.multi_document
    assert p.years == (2019, 2025)
    assert not p.consolidation and not p.forced
    assert (p.date_from, p.date_to) == (None, None)   # two points, not one range


def test_t4_hunts_for_a_passage_by_its_words():
    p = plan(BENCHMARK["T4"])
    assert p.find_passage and not p.forced
    assert {"seaweed", "aquafeed", "npk", "asparagopsis", "cattle"} <= set(p.terms)
    # The request's scaffolding is not what the passage has to contain.
    assert not {"find", "article", "containing", "paragraph", "teri"} & set(p.terms)
    assert "Asparagopsis" in p.entities
    assert p.queries[0].text == ("seaweed biomass replacing aquafeed npk fertilizer "
                                 "asparagopsis cattle feed")


def test_t5_wants_the_latest_so_the_web_is_forced():
    p = plan(BENCHMARK["T5"])
    assert p.freshness and p.forced_reasons == (FRESHNESS,)
    assert p.org_scoped and "Delhi" in p.entities


def test_t6_consolidates_a_period_across_documents():
    p = plan(BENCHMARK["T6"])
    assert p.consolidation and p.multi_document
    assert (p.date_from, p.date_to) == ("2019-01-01", "2026-01-01")   # end exclusive
    assert not p.comparison and not p.forced
    assert not p.document_request          # "give me an overview" is content


# --- explicit requests ------------------------------------------------------------


@pytest.mark.parametrize("question", [
    "Can you search the web for India's EV policy updates?",
    "Please look up TERI's heavy-duty truck report online",
    "search online for the Delhi clean air plan",
    "What does the internet say about TERI? Do a web search.",
    "Find it on the web: TERI Hindi portal",
])
def test_asking_for_the_web_by_name_forces_it(question):
    p = plan(question)
    assert p.explicit and EXPLICIT in p.forced_reasons


@pytest.mark.parametrize("question", [
    "Find TERI projects on solar energy",
    "Look up the annual report 2024-25",
    "What is TERI's website address?",
])
def test_finding_or_looking_up_alone_is_not_asking_for_the_web(question):
    assert not plan(question).explicit


def test_the_request_to_search_is_not_sent_to_the_search_engine():
    p = plan("Can you search the web for India's EV policy updates?")
    assert {q.text for q in p.queries} == {"India's EV policy updates"}


# --- freshness ----------------------------------------------------------------------


@pytest.mark.parametrize("question", [
    "What are the newest TERI reports?", "Most recent TERI study on trucks",
    "What is TERI working on this year?", "TERI events in 2026",
    "Who currently heads TERI's air quality work?",
])
def test_wanting_the_present_state_is_freshness(question):
    assert plan(question).freshness


def test_asking_about_a_past_year_is_not_freshness():
    assert not plan("What did TERI publish on air quality in 2019?").freshness


# --- passage hunts ----------------------------------------------------------------


@pytest.mark.parametrize("question", [
    "Which report mentions Asparagopsis as cattle feed?",
    "Show me the document that says transport is 23% of winter PM2.5",
    'Where does TERI say "fleet modernisation is essential"?',
    "I need the exact words TERI used about truck bans",
])
def test_looking_for_a_specific_text_is_a_passage_hunt(question):
    assert plan(question).find_passage


def test_a_quoted_phrase_is_kept_whole_and_searched_as_a_phrase():
    p = plan('Where does TERI say "fleet modernisation is essential"?')
    assert p.phrases == ("fleet modernisation is essential",)
    assert p.queries[0].text.startswith('"fleet modernisation is essential"')


def test_asking_about_a_topic_is_not_a_passage_hunt():
    assert not plan("What does TERI say about seaweed farming?").find_passage


# --- people -------------------------------------------------------------------------


@pytest.mark.parametrize("question, person", [
    ("Who is Dr Raghab Ray?", "Raghab Ray"),
    ("who is ajay mathur", "ajay mathur"),
    ("Tell me about Prof. Anju Goel at TERI", "Anju Goel"),
    ("Profile of Sayanta Ghosh", "Sayanta Ghosh"),
])
def test_a_named_person_is_recognised_without_their_honorific(question, person):
    assert plan(question).person == person


@pytest.mark.parametrize("question", [
    "Who is the director general of TERI?",
    "Who is TERI?",
    "Tell me about solar energy at TERI",
    "What do you know about seaweed biomass?",
])
def test_roles_organisations_and_topics_are_not_people(question):
    assert plan(question).person is None


# --- scope and queries -----------------------------------------------------------


def test_an_organisation_question_searches_its_own_site_first():
    queries = plan("What does TERI do on solar energy?").queries
    assert [(q.site, q.purpose, q.fallback) for q in queries] == [
        ("teriin.org", "primary", False), (None, "open", True),
    ]


def test_the_organisation_is_named_on_the_open_web_but_not_on_its_own_site():
    first, second = plan("Who is Dr Raghab Ray?", "Who is Dr Raghab Ray?").queries
    assert first.text == "Raghab Ray profile"
    assert second.text == "Raghab Ray profile"          # not org-scoped: no TERI added
    first, second = plan(BENCHMARK["T1"]).queries
    assert "TERI" not in first.text and second.text.startswith("TERI ")


def test_a_question_not_about_the_organisation_searches_both_side_by_side():
    queries = plan("What is PM2.5?").queries
    assert not plan("What is PM2.5?").org_scoped
    assert [q.fallback for q in queries] == [False, False]


def test_without_third_party_sites_only_the_organisations_site_is_searched(settings, monkeypatch):
    monkeypatch.setattr(settings, "web_allow_third_party", False)
    assert [q.site for q in plan("What is PM2.5?").queries] == ["teriin.org"]


def test_every_primary_domain_gets_its_own_restricted_query(settings, monkeypatch):
    monkeypatch.setattr(settings, "web_primary_domains", "teriin.org, teri.res.in")
    sites = [q.site for q in plan("TERI solar work").queries]
    assert sites == ["teriin.org", "teri.res.in", None]


def test_the_organisations_names_come_from_settings(settings, monkeypatch):
    monkeypatch.setattr(settings, "web_organisation_names", "ACME Institute")
    assert plan("What does ACME Institute publish?").org_scoped
    assert not plan("What does TERI publish?").org_scoped


# --- periods ------------------------------------------------------------------------


def test_understandings_own_window_is_used_when_it_has_one():
    p = plan("air pollution work in the last decade", date_from="2016-01-01",
             date_to="2026-01-01")
    assert (p.date_from, p.date_to) == ("2016-01-01", "2026-01-01")


@pytest.mark.parametrize("question", ["TERI transport studies 2019-2025",
                                      "TERI transport studies between 2019 and 2025"])
def test_written_year_ranges_are_a_window(question):
    p = plan(question)
    assert (p.date_from, p.date_to) == ("2019-01-01", "2026-01-01")
    assert p.consolidation and not p.comparison


def test_the_answer_shape_or_capabilities_can_mark_consolidation():
    assert plan("TERI air pollution work", answer_format="timeline").consolidation
    assert plan("TERI air pollution work", capabilities={"summarization"}).consolidation
    assert plan("TERI and IIT on trucks", capabilities={"comparison"}).comparison


# --- the plan as data ------------------------------------------------------------


def test_the_trace_records_what_fired_and_omits_what_did_not():
    record = plan(BENCHMARK["T5"]).to_trace()
    assert record["freshness"] is True
    assert record["forced_reasons"] == [FRESHNESS]
    assert "person" not in record and "find_passage" not in record


def test_planning_is_deterministic_and_calls_no_model(monkeypatch):
    import app.core.clients.llm as llm

    def fail(*_a, **_k):
        raise AssertionError("the planner must not call a model")

    monkeypatch.setattr(llm, "get_llm", fail)
    monkeypatch.setattr(llm, "get_structured_llm", fail)
    assert plan(BENCHMARK["T4"]) == plan(BENCHMARK["T4"])
