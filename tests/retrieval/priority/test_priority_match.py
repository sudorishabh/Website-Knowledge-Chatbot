"""Which priority pages a question needs: the deterministic triggers on the
shipped list, and the description-similarity trigger on stub vectors."""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.retrieval.priority import match
from app.retrieval.priority.match import (
    GROUP,
    NAME,
    PERSON,
    SIMILAR,
    THEME_FACET,
    Target,
    explicit,
    ranked,
    similar,
)
from app.retrieval.priority.people import Person
from app.retrieval.priority.registry import DEFAULT_PATH, parse


@pytest.fixture(scope="module")
def reg():
    return parse(json.loads(DEFAULT_PATH.read_text(encoding="utf-8")))


def _hits(question, reg, **kw):
    return [(t.name, t.reason) for t in explicit(question, registry=reg, **kw)]


@pytest.mark.parametrize("question, page", [
    ("tell me about climate change theme", "Climate Change Theme"),
    ("Like me about climate change thematic", "Climate Change Theme"),
    ("tell me about the water theme", "Water Theme"),
    ("TERI's energy thematic area", "Energy Theme"),
    ("Who is on TERI's governing council?", "people - governing council"),
    ("who is the director general of TERI", "People - committee of directors"),
    ("what does the Goa centre work on", "Goa"),
    ("latest tenders at TERI", "annoucements"),
    ("who founded TERI", "founder"),
    ("TERI FCRA receipts", "FCRA Financials"),
    ("what is green shipping", "Green Shipping Theme"),
    ("tell me about the WSDS", "World Sustainable Development Summit Theme"),
    ("Forest & Biodiversity work", "Forest & Biodiversity Theme"),
])
def test_a_named_page_is_found(reg, question, page):
    assert (page, NAME) in _hits(question, reg)


@pytest.mark.parametrize("question", [
    "Themes area Teri works on",
    "Teri thematic areas",
    "What are TERI's research areas?",
    "which themes does TERI work on",
])
def test_asking_for_the_themes_names_the_home_page(reg, question):
    assert _hits(question, reg) == [("Home", NAME)]


def test_a_theme_listing_names_the_home_page_whatever_the_wording(reg):
    assert _hits("what are the main themes", reg) == []
    assert _hits("what are the main themes", reg, themes_listing=True) == [("Home", NAME)]


def test_one_named_theme_takes_the_place_of_the_list(reg):
    assert _hits("tell me about the climate change thematic area", reg,
                 themes_listing=True) == [("Climate Change Theme", NAME)]


def test_a_count_over_the_themes_does_not_name_the_home_page(reg):
    assert _hits("which themes have the most publications", reg) == []


@pytest.mark.parametrize("question", [
    "how much water does a data centre use",
    "what is the policy on leave",
    "explain the energy transition",
])
def test_a_single_common_word_names_no_page(reg, question):
    assert _hits(question, reg) == []


def test_a_group_is_asked_for_by_what_it_holds(reg):
    assert ("Regional centers", GROUP) in _hits("which regional centres does TERI have", reg)


def test_the_theme_facet_names_its_page(reg):
    assert _hits("what does TERI do here", reg, theme="Water") == [("Water Theme", THEME_FACET)]
    assert _hits("what does TERI do here", reg, theme="climate change") == [
        ("Climate Change Theme", THEME_FACET)
    ]


def test_a_theme_facet_off_the_list_names_nothing(reg):
    assert _hits("what does TERI do here", reg, theme="Microbes") == []


def test_a_named_person_leads_with_their_profile(reg):
    person = Person("Dr Vibha Dhawan", "https://teriin.org/profile/vibha-dhawan", "Committee")
    targets = explicit("what has Vibha Dhawan said about the director general role",
                       registry=reg, people=[person])
    assert targets[0].reason == PERSON
    assert targets[0].url == "https://teriin.org/profile/vibha-dhawan"
    assert ("People - committee of directors", NAME) in [(t.name, t.reason) for t in targets]


def test_one_page_is_kept_once_with_its_strongest_reason(reg):
    targets = explicit("climate change theme", registry=reg, theme="Climate Change")
    assert [(t.name, t.reason) for t in targets] == [("Climate Change Theme", NAME)]


def test_ranking_orders_by_strength_then_score():
    weak = Target("A", "theme", SIMILAR, url="https://teriin.org/a", score=0.9)
    strong = Target("B", "page", NAME, url="https://teriin.org/b")
    other = Target("C", "theme", SIMILAR, url="https://teriin.org/c", score=0.6)
    assert [t.name for t in ranked([other, weak, strong])] == ["B", "A", "C"]


class TestSimilarity:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        match.clear_vectors()
        monkeypatch.setattr(get_settings(), "priority_match_threshold", 0.48)
        monkeypatch.setattr(get_settings(), "priority_match_margin", 0.06)
        yield
        match.clear_vectors()

    @staticmethod
    def _registry():
        return parse([
            {"name": "Air", "page_url": "https://teriin.org/air", "description": "Air quality."},
            {"name": "Water", "page_url": "https://teriin.org/water", "description": "Water."},
            {"name": "Land", "page_url": "https://teriin.org/land", "description": "Land."},
        ])

    @staticmethod
    def _embed(texts):
        # Air along x, Water along y, Land along z; w is off every page.
        axes = {"Air": [1.0, 0.0, 0.0, 0.0], "Water": [0.0, 1.0, 0.0, 0.0],
                "Land": [0.0, 0.0, 1.0, 0.0]}
        return [axes[t.split(".")[0]] for t in texts]

    def test_a_clear_best_page_is_matched(self):
        targets, top = similar([0.9, 0.3, 0.1, 0.0], registry=self._registry(), embed=self._embed)
        assert [(t.name, t.reason) for t in targets] == [("Air", SIMILAR)]
        assert top[0]["name"] == "Air"

    def test_a_score_under_the_threshold_matches_nothing(self):
        targets, top = similar([0.3, 0.1, 0.05, 0.95], registry=self._registry(),
                               embed=self._embed)
        assert targets == []
        assert top[0]["name"] == "Air" and top[0]["score"] < 0.48

    def test_a_close_runner_up_matches_nothing(self):
        targets, _ = similar([0.7, 0.68, 0.0, 0.0], registry=self._registry(), embed=self._embed)
        assert targets == []

    def test_descriptions_are_embedded_once(self):
        calls = []

        def embed(texts):
            calls.append(len(texts))
            return self._embed(texts)

        similar([1.0, 0.0, 0.0, 0.0], registry=self._registry(), embed=embed)
        similar([0.0, 1.0, 0.0, 0.0], registry=self._registry(), embed=embed)
        assert calls == [3]

    def test_an_embedding_failure_costs_only_this_trigger(self):
        def boom(texts):
            raise RuntimeError("embedding service down")

        assert similar([1.0, 0.0, 0.0, 0.0], registry=self._registry(), embed=boom) == ([], [])

    def test_the_organisation_name_is_left_out_of_what_is_embedded(self):
        page = parse([{"name": "Policy", "page_url": "https://teriin.org/policy",
                       "description": "Presents TERI's policy research by The Energy and "
                                      "Resources Institute."}]).pages[0]
        assert "TERI" not in match.description_text(page)
        assert "Energy and Resources Institute" not in match.description_text(page)

    def test_a_theme_is_embedded_by_its_topic_without_theme(self):
        page = parse([{"name": "Environment Theme", "page_url": "https://teriin.org/environment",
                       "description": "Air, water and land."}]).pages[0]
        assert match.description_text(page) == "Environment. Air, water and land."


class TestStaffListings:
    """A question for the organisation's people that names none of them.

    Measured 2026-09-25: "List TERI's leading researchers" read no people page —
    no curated phrase says "researchers" and the best listing's description
    scored 0.428 under a 0.48 bar — so the answer was the refusal, although the
    Committee of Directors and Distinguished Fellows listings held it.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        match.clear_vectors()
        yield
        match.clear_vectors()

    @staticmethod
    def _registry():
        return parse([
            {"name": "Air", "page_url": "https://teriin.org/air", "description": "Air quality."},
            {"name": "People - committee of directors",
             "page_url": "https://teriin.org/people/committee-of-directors",
             "description": "Directors."},
            {"name": "people - governing council",
             "page_url": "https://teriin.org/people/governing-council",
             "description": "Council."},
            {"name": "people - distinguished fellows",
             "page_url": "https://teriin.org/people/distinguished-fellows",
             "description": "Fellows."},
        ])

    @staticmethod
    def _embed(texts):
        # Directors along x, Fellows along y, Council along z, Air along w.
        axes = {"Air": [0, 0, 0, 1.0], "committee of directors": [1.0, 0, 0, 0],
                "distinguished fellows": [0, 1.0, 0, 0], "governing council": [0, 0, 1.0, 0]}
        return [next(v for k, v in axes.items() if k in t) for t in texts]

    def _staff(self, question, vector=(0.8, 0.6, 0.1, 0.0), targets=()):
        found = match.staff_listings(question, list(vector), registry=self._registry(),
                                     targets=targets, embed=self._embed)
        return [(t.name, t.reason) for t in found]

    @pytest.mark.parametrize("question", [
        "List TERI's leading researchers.", "TERI top researchers", "air quality experts",
        "TERI climate change team", "who are the scientists at TERI",
    ])
    def test_a_question_for_people_reads_the_closest_two_listings(self, question):
        assert self._staff(question) == [
            ("People - committee of directors", match.STAFF),
            ("people - distinguished fellows", match.STAFF),
        ]

    def test_the_listings_follow_the_question_not_the_file(self):
        # A question closest to the council gets the council.
        assert self._staff("which people sit on the council", vector=(0.1, 0.2, 0.9, 0.0))[0] == (
            "people - governing council", match.STAFF)

    @pytest.mark.parametrize("question", [
        "who wrote the net-zero paper?",   # one document's byline: the corpus answers it
        "authors of the 2024 energy report",
        "what is blended finance",
    ])
    def test_a_question_not_for_the_organisations_people_reads_none(self, question):
        assert self._staff(question) == []

    def test_a_named_person_or_listing_is_left_to_its_own_page(self):
        reg = self._registry()
        committee = next(p for p in reg.pages if "committee" in p.name)
        named = Target(committee.name, committee.kind, NAME, url=committee.url, page=committee)
        assert self._staff("who are the directors", targets=[named]) == []
        person = Target("Dr A", "profile", PERSON, url="https://teriin.org/profile/a")
        assert self._staff("researchers working with Dr A", targets=[person]) == []

    def test_without_vectors_the_file_order_decides(self):
        def boom(texts):
            raise RuntimeError("embedding service down")

        found = match.staff_listings("TERI researchers", [1.0, 0, 0, 0],
                                     registry=self._registry(), embed=boom)
        assert [t.name for t in found] == ["People - committee of directors",
                                           "people - governing council"]

    @pytest.mark.parametrize("question, people, staff", [
        ("who are the members of the climate change theme", True, False),
        ("climate change theme team", True, True),
        ("top climate change researchers", True, True),
        ("what does the climate change theme work on", False, False),
    ])
    def test_members_ask_for_a_team_but_not_for_the_staff_listings(self, question, people, staff):
        assert match.asks_for_people(question) is people
        assert match.asks_for_staff(question) is staff

    def test_staff_is_an_about_reason(self):
        # So each listing's opening section is admitted whatever it scores.
        assert match.STAFF in match.ABOUT
