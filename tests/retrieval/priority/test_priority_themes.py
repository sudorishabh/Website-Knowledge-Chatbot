"""The home page's thematic areas: which it lists, and how each is described."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.retrieval.priority.extract import PageContent, Section, extract
from app.retrieval.priority.registry import parse
from app.retrieval.priority.themes import (
    ThematicArea,
    areas_on,
    asks_for_themes,
    asks_only_for_themes,
)

FIXTURES = Path(__file__).parent / "fixtures"
REGISTRY = parse([
    {"name": "Home", "page_url": "https://www.teriin.org", "description": "Home."},
    {"name": "Climate Change Theme", "page_url": "https://teriin.org/climate",
     "description": "Covers climate science and policy."},
    {"name": "Environment and Public Health Theme",
     "page_url": "https://teriin.org/environment-and-public-health",
     "description": "Examines environment and health."},
    {"name": "Water Theme", "page_url": "https://teriin.org/water",
     "description": "Water security."},
])


def _home():
    html = (FIXTURES / "home.html").read_text(encoding="utf-8")
    return extract(html, "https://www.teriin.org/")


def _page(*lines, heading="Thematic Areas"):
    return PageContent(title="Home", sections=(Section(heading, "\n".join([heading, *lines])),),
                       links=(), content_hash="h")


def test_the_home_pages_themes_in_its_order():
    names = [area.name for area in areas_on(_home(), REGISTRY)]
    assert names == ["Sustainable Agriculture", "Climate Change", "Energy", "Environment",
                     "Sustainable Habitat", "Environment & Public Health",
                     "Resources & Sustainable Development"]


def test_a_theme_with_a_page_is_described_from_the_page_list():
    """The home page cuts its teasers mid-sentence; the page list's is whole."""
    areas = {area.name: area for area in areas_on(_home(), REGISTRY)}
    assert areas["Climate Change"] == ThematicArea(
        "Climate Change", "Covers climate science and policy.", "https://teriin.org/climate")
    # "&" on the home page, "and" in the page list.
    assert areas["Environment & Public Health"].url == (
        "https://teriin.org/environment-and-public-health")


def test_a_theme_without_a_page_keeps_its_teaser_as_an_ellipsis():
    energy = {area.name: area for area in areas_on(_home(), REGISTRY)}["Energy"]
    assert energy.url is None
    assert energy.description == (
        "A transition to clean energy lies at the heart of India realizing its long-term…")


def test_a_sub_theme_the_home_page_does_not_list_is_not_a_thematic_area():
    assert "Water" not in [area.name for area in areas_on(_home(), REGISTRY)]


def test_a_name_with_no_teaser_still_counts_when_the_page_list_knows_it():
    areas = areas_on(_page("Climate Change", "Energy", "Clean energy for all..."), REGISTRY)
    assert [(a.name, a.description) for a in areas] == [
        ("Climate Change", "Covers climate science and policy."),
        ("Energy", "Clean energy for all…"),
    ]


def test_a_page_without_the_section_lists_no_themes():
    assert areas_on(_page("Climate Change", heading="Key Projects"), REGISTRY) == []


# Measured 2026-10-17: of these only the "thematic areas" and "themes" wordings
# were answered with the seven areas; "TERI divisions" and "research areas" were
# small talk, and "what are the divisions of TERI" listed a 2017 annual report's
# divisions.
LISTINGS = [
    "What are TERI's research areas?",
    "research areas",
    "What research areas does TERI work in?",
    "What are the divisions of TERI?",
    "TERI divisions",
    "Which research divisions does TERI have?",
    "What are TERI's themes?",
    "What are the main themes of TERI?",
    "What areas does TERI work in?",
    "TERI's areas of expertise",
    "What fields does TERI work in?",
    "What sectors does TERI work in?",
    "TERI departments",
    "thematic areas of TERI",
    "what topics do you cover",
    "How many research areas does TERI have?",
    # Measured 2026-10-22: "are there" was left over, and the count came from
    # a 2013-14 report ("about 20 divisions").
    "How many divisions are there in TERI?",
    "what all divisions are there in teri",
    "What are the names of TERI's research areas?",
]


@pytest.mark.parametrize("question", LISTINGS)
def test_the_themes_are_asked_for_by_many_names(question):
    assert asks_for_themes(question)
    assert asks_only_for_themes(question)


@pytest.mark.parametrize("question", [
    "TERI's research areas and their key projects",
    "Which TERI themes have the most publications?",
    "How many publications are there in each thematic area?",
    "What are the research areas of Dr Vibha Dhawan?",
    "What are the research areas in the climate change theme?",
])
def test_a_listing_that_asks_for_more_is_not_only_a_listing(question):
    assert asks_for_themes(question)
    assert not asks_only_for_themes(question)


@pytest.mark.parametrize("question", [
    "Which themes have the most publications?",   # a catalog count, no organisation
    "tell me about the climate change theme",      # one theme
    "Which division of TERI handles water?",       # one division
    "What has TERI done in coastal areas?",
    "What research has TERI done on air pollution?",
])
def test_other_questions_do_not_ask_for_the_themes(question):
    assert not asks_for_themes(question)



@pytest.mark.parametrize("question", [
    "divisons of teri",
    "TERI's divisons",
    "teri devisions",
    "What departmets does TERI have?",
])
def test_a_typo_in_another_name_for_the_themes_is_forgiven(question):
    """Measured 2026-10-22: "divisons of teri" was answered from four annual
    reports' lists of divisions, with six sources."""
    assert asks_for_themes(question)
    assert asks_only_for_themes(question)


@pytest.mark.parametrize("question", [
    "TERI theses",                                 # one letter from "themes"
    "TERI themed events",
    "Which division of TERI handles water?",       # a singular is not a typo
])
def test_a_word_near_themes_or_a_singular_is_not_a_typo(question):
    assert not asks_for_themes(question)


@pytest.mark.parametrize("question, only", [
    ("themes", True),
    ("Themes?", True),
    ("what are the themes", True),
    ("list all themes", True),
    ("which themes have the most publications", False),
    ("what is the theme", False),
    ("themes of WSDS 2026", False),
])
def test_the_word_themes_alone_asks_only_for_the_themes(question, only):
    """Measured 2026-10-21: the bare "themes" was asked "could you say what
    topic" whenever understanding read it as unclear."""
    assert asks_only_for_themes(question) is only


@pytest.mark.parametrize("question, only", [
    ("divisions", True),
    ("Divisions?", True),
    ("what are all the divisions", True),
    ("list all divisions", True),
    ("how many departments are there", True),
    ("divisons", True),
    ("division", False),                       # one, already in view
    ("what is the division", False),
    ("divisions of WSDS 2026", False),
    ("divisions working on solar energy", False),
])
def test_another_name_for_the_themes_alone_asks_only_for_the_themes(question, only):
    """Measured 2026-10-22: "divisions" and "what are all the divisions" were
    asked "could you say what topic", and "list all divisions" listed the Human
    Resources and IT divisions of 2015 reports."""
    assert asks_only_for_themes(question) is only


@pytest.mark.parametrize("question, only", [
    ("teri division", True),
    ("What is TERI's division?", True),
    ("TERI department", True),
    ("teri divison", True),
    ("Which division of TERI handles water?", False),   # one division
    ("TERI department of policy studies", False),
    ("TERI theme", False),                              # "themes" stays plural
])
def test_a_singular_beside_the_organisation_asks_only_for_the_themes(question, only):
    """Measured 2026-10-22: "teri division" was asked "could you clarify what
    you mean"."""
    assert asks_only_for_themes(question) is only
