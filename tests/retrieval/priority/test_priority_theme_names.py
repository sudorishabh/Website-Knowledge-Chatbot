"""Themes named by part of their name, on the shipped page list."""
from __future__ import annotations

import json

import pytest

from app.retrieval.priority.registry import DEFAULT_PATH, parse
from app.retrieval.priority.theme_names import MAX_FITS, mentions, same_word, to_ask_about

BIOFUELS = [
    "Advanced Biofuels & Biochemicals Theme",
    "Pyrolytic Biofuels, Biochar & Green Chemicals Theme",
    "Microbial Biofuels and Biochemicals Theme",
]


@pytest.fixture(scope="module")
def reg():
    return parse(json.loads(DEFAULT_PATH.read_text(encoding="utf-8")))


def _pages(text, reg, **kw):
    return [[p.name for p in m.pages] for m in mentions(text, reg, **kw)]


@pytest.mark.parametrize("a, b, same", [
    ("biofuel", "biofuels", True),
    ("city", "cities", True),
    ("agricultural", "agriculture", True),
    ("environmental", "environment", True),
    ("biofule", "biofuel", True),
    ("biochar", "biochemicals", False),
    ("community", "communication", False),
    ("air", "aim", False),
])
def test_same_word(a, b, same):
    assert same_word(a, b) is same


@pytest.mark.parametrize("question, page", [
    ("tell me about the microbial theme", "Microbial Biofuels and Biochemicals Theme"),
    ("what is the marine theme", "Marine and Coastal Theme"),
    ("tell me about coastal theme", "Marine and Coastal Theme"),
    ("what does the biotechnology theme do", "Industrial Biotechnology Theme"),
    ("tell me about the forest theme", "Forest & Biodiversity Theme"),
    ("tell me about the agricultural theme", "Sustainable Agriculture Theme"),
    ("what is TERI's public health theme", "Environment and Public Health Theme"),
    ("what is the city theme", "Cities Theme"),
    ("tell me about the building theme", "Buildings Theme"),
    ("what is the climate theme", "Climate Change Theme"),
    ("tell me about the advanced biofuels theme", "Advanced Biofuels & Biochemicals Theme"),
    ("the theme on microbial biofuels", "Microbial Biofuels and Biochemicals Theme"),
])
def test_part_of_a_name_fits_one_theme(reg, question, page):
    assert _pages(question, reg) == [[page]]


def test_a_name_that_is_exactly_the_words_is_the_one_meant(reg):
    assert _pages("tell me about the environmental theme", reg) == [["Environment Theme"]]
    assert _pages("tell me about the energy theme", reg) == [["Energy Theme"]]


@pytest.mark.parametrize("question", [
    "tell me about biofuel theme",
    "what does TERI's biofuels theme work on",
    "what is the biofuel thematic area about",
    "tell me about the theme on biofuels",
])
def test_part_of_a_name_may_fit_several_themes(reg, question):
    found = mentions(question, reg)
    assert [[p.name for p in m.pages] for m in found] == [BIOFUELS]
    assert to_ask_about(found) is found[0]


def test_themes_asks_for_every_theme_that_fits(reg):
    found = mentions("tell me about the biofuel themes", reg)
    assert [[p.name for p in m.pages] for m in found] == [BIOFUELS]
    assert to_ask_about(found) is None


def test_understandings_theme_alone_is_read_but_not_asked_about(reg):
    found = mentions("summarize TERI's biofuel research", reg, facet="biofuel")
    assert [[p.name for p in m.pages] for m in found] == [BIOFUELS]
    assert not found[0].named
    assert to_ask_about(found) is None


def test_one_theme_picked_elsewhere_settles_the_question(reg):
    found = mentions("tell me about biofuel theme\nthe microbial theme", reg)
    assert to_ask_about(found) is None


@pytest.mark.parametrize("question", [
    "what are TERI's research themes",
    "which themes have the most publications",
    "what is the theme of the report",
    "what is TERI's mission",
    "tell me about the sustainable theme",
])
def test_no_theme_is_named(reg, question):
    assert mentions(question, reg) == []


def test_too_many_fits_name_nothing(reg):
    assert MAX_FITS == 4
    assert mentions("sustainable", reg, facet="sustainable") == []


def test_names_joined_by_and_are_read_apart(reg):
    assert _pages("the climate change and energy themes", reg) == [
        ["Climate Change Theme"], ["Energy Theme"],
    ]


@pytest.mark.parametrize("reply, chosen", [
    ("2", ["Pyrolytic Biofuels, Biochar & Green Chemicals Theme"]),
    ("the third one", ["Microbial Biofuels and Biochemicals Theme"]),
    ("Microbial Biofuels and Biochemicals", ["Microbial Biofuels and Biochemicals Theme"]),
    ("the pyrolytic one", ["Pyrolytic Biofuels, Biochar & Green Chemicals Theme"]),
    ("all of them", BIOFUELS),
    ("not sure", BIOFUELS),
])
def test_a_reply_chooses_among_the_themes(reg, reply, chosen):
    (mention,) = mentions("tell me about biofuel theme", reg)
    assert [p.name for p in mention.narrowed(reply).pages] == chosen
