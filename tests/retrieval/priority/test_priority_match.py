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
from app.retrieval.priority.registry import (
    DEFAULT_PATH,
    PriorityGroup,
    PriorityPage,
    normalize_text,
    one_edit_apart,
    parse,
)


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
    ("TERI contact details", "Contact Us"),
    ("what is the phone number of TERI", "Contact Us"),
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


@pytest.mark.parametrize("question", [
    "What is TERI's pan India presence?",
    "pan-india presence",
    "Tell me about TERI's national presence",
    "TERI's presence across the country",
    "Where are TERI's offices in India?",
    "List all TERI branches and centres in India",
    "Which centres does TERI have?",
    "Does TERI have offices in other cities?",
    "Where is TERI located?",
    "where are your offices",
    "TERI campuses",
])
def test_the_centres_are_asked_for_by_presence_offices_and_branches(reg, question):
    """Measured 2026-10-17: of these only the "regional centres" wording reached
    the list; the rest were answered from an annual report's one line, which
    predates the Hyderabad centre."""
    assert ("Regional centers", GROUP) in _hits(question, reg)


@pytest.mark.parametrize("question", [
    "regional cenres",
    "TERI regional centrs",
    "regoinal centers",
    "pan india presense",
])
def test_a_typo_in_a_group_phrase_is_forgiven(reg, question):
    """Measured 2026-10-17: "regional cenres" was read as small talk and the
    user was asked what they meant."""
    assert ("Regional centers", GROUP) in _hits(question, reg)


@pytest.mark.parametrize("a, b, close", [
    ("cenres", "centres", True),      # a letter dropped
    ("centeres", "centres", True),    # a letter added
    ("centrse", "centres", True),     # neighbours swapped
    ("centros", "centres", True),     # a letter changed
    ("cntrs", "centres", False),
    ("gender", "tender", True),       # why page names are matched exactly
])
def test_one_edit_apart(a, b, close):
    assert one_edit_apart(a, b) is close


def test_a_typo_is_not_forgiven_in_a_short_word(reg):
    assert ("Regional centers", GROUP) not in _hits("tery centres", reg)
    assert ("Regional centers", GROUP) not in _hits("pun india presence", reg)


_MAHINDRA = PriorityPage(
    name="Mahindra-TERI Centre of Excellence", description="Habitats.", kind="excellence",
    url="https://teriin.org/Mahindra-TERI-centre-of-excellence",
    aliases=("mahindra teri centre of excellence", "mahindra teri"),
)
_REGIONAL = PriorityGroup(
    name="Regional centers", description="", kind="centre", members=(),
    aliases=("regional centre", "teri centres"), member_words=frozenset({"centres"}),
)
_EXCELLENCE = PriorityGroup(
    name="Centres of Excellence", description="", kind="excellence", members=(_MAHINDRA,),
    aliases=("centre of excellence", "centres of excellence"),
)


def _groups(question, named=()):
    return [g.name for g in match._groups_asked_for(
        (_REGIONAL, _EXCELLENCE), normalize_text(question), list(named))]


@pytest.mark.parametrize("question, groups", [
    # The longest phrase is the group asked for, and uses its words up.
    ("TERI centres of excellence", ["Centres of Excellence"]),
    ("regional centres and centres of excellence", ["Centres of Excellence", "Regional centers"]),
    ("which centre of excellence works on water reuse", ["Centres of Excellence"]),
    ("which centres does TERI have", ["Regional centers"]),
    # The question and its rewrite, read together, say it twice (2026-10-17).
    ("List all centers of excellence at TERI\nList all centers of excellence at TERI.",
     ["Centres of Excellence"]),
])
def test_the_most_specific_group_is_the_one_asked_for(question, groups):
    assert _groups(question) == groups


@pytest.mark.parametrize("question", [
    "Tell me about TERI centre of excellence",
    "What are TERI's centres of excellence?",
    "List all centers of excellence at TERI",
    "What does each TERI centre of excellence work on?",
    "centre of excellance",
    "Which centre of excellence works on water reuse?",
])
def test_the_centres_of_excellence_are_asked_for_as_a_group(reg, question):
    """Measured 2026-10-17: these read the live page's bare list of names, and
    the answers described one to four centres of nine."""
    assert _hits(question, reg) == [("Centres of Excellence", GROUP)]


@pytest.mark.parametrize("question, page", [
    ("What is CoEET?", "Centre of Excellence for Energy Transition (CoEET)"),
    ("Tell me about the Mahindra-TERI Centre of Excellence", "Mahindra-TERI Centre of Excellence"),
    ("What does the CONCOR-TERI Centre of Excellence do?",
     "CONCOR–TERI Centre of Excellence for Green and Sustainable Logistics"),
    ("What is NTCoE?", "NMCG-TERI Centre of Excellence on Water Reuse (NTCoE)"),
    ("TERI-CFCL Centre of Excellence", "TERI-CFCL Centre of Excellence (CoE)"),
])
def test_one_centre_of_excellence_is_its_own_page(reg, question, page):
    assert _hits(question, reg) == [(page, NAME)]


def test_a_question_naming_one_member_is_about_that_member():
    assert _groups("tell me about the Mahindra-TERI Centre of Excellence", [_MAHINDRA]) == []


def test_a_full_name_is_cut_out_before_a_short_name_inside_it():
    assert "excellence" not in match._without(
        "mahindra teri centre of excellence", ["mahindra teri", "mahindra teri centre of excellence"])


@pytest.mark.parametrize("question", [
    "TERI centres of excellence",
    "what does TERI's Goa centre work on",
    "how much water does a data centre use",
    "TERI's presence at COP28",
    "what is the TERI Centre for Sustainable Development Research",
])
def test_one_centre_or_another_page_is_not_the_centres(reg, question):
    assert ("Regional centers", GROUP) not in _hits(question, reg)


def test_the_theme_facet_names_its_page(reg):
    assert _hits("what does TERI do here", reg, theme="Water") == [("Water Theme", THEME_FACET)]
    assert _hits("what does TERI do here", reg, theme="climate change") == [
        ("Climate Change Theme", THEME_FACET)
    ]


def test_a_theme_facet_off_the_list_names_nothing(reg):
    assert _hits("what does TERI do here", reg, theme="Solar") == []


BIOFUELS = [
    "Advanced Biofuels & Biochemicals Theme",
    "Pyrolytic Biofuels, Biochar & Green Chemicals Theme",
    "Microbial Biofuels and Biochemicals Theme",
]


@pytest.mark.parametrize("question, page", [
    ("tell me about the marine theme", "Marine and Coastal Theme"),
    ("what is the city theme", "Cities Theme"),
    ("tell me about the microbial theme", "Microbial Biofuels and Biochemicals Theme"),
])
def test_part_of_a_themes_name_names_its_page(reg, question, page):
    """Measured 2026-10-21: none of 25 such questions reached a theme page."""
    assert _hits(question, reg) == [(page, NAME)]


def test_part_of_a_name_several_themes_share_names_each_of_them(reg):
    assert _hits("tell me about biofuel theme", reg) == [(name, NAME) for name in BIOFUELS]


def test_part_of_a_themes_name_takes_the_place_of_the_list(reg):
    """"thematic area" is an overview phrase, but "the biofuel thematic area"
    is about the biofuel themes."""
    hits = _hits("what is the biofuel thematic area about", reg)
    assert hits == [(name, NAME) for name in BIOFUELS]


def test_a_theme_facet_that_is_part_of_a_name_names_its_pages(reg):
    assert _hits("summarize TERI's research here", reg, theme="biofuel") == [
        (name, THEME_FACET) for name in BIOFUELS
    ]


@pytest.mark.parametrize("reply, chosen", [
    ("2", [BIOFUELS[1]]),
    ("microbial", [BIOFUELS[2]]),
    ("all", BIOFUELS),
])
def test_a_reply_chooses_among_the_themes_a_name_fits(reg, reply, chosen):
    assert _hits("tell me about biofuel theme", reg, reply=reply) == [
        (name, NAME) for name in chosen
    ]


def test_a_named_person_leads_with_their_profile(reg):
    person = Person("Dr Vibha Dhawan", "https://teriin.org/profile/vibha-dhawan", "Committee")
    targets = explicit("what has Vibha Dhawan said about the director general role",
                       registry=reg, people=[person])
    assert targets[0].reason == PERSON
    assert targets[0].url == "https://teriin.org/profile/vibha-dhawan"
    assert ("People - committee of directors", NAME) in [(t.name, t.reason) for t in targets]


def test_a_post_leads_with_its_holder_profile(reg):
    """Measured 2026-10-04: "who is the current director general of TERI" read
    the committee listing alone and was answered with her name and a line about
    an annual report; her profile held her biography."""
    dg = Person("Dr Vibha Dhawan", "https://teriin.org/profile/vibha-dhawan", "Committee",
                "Director General")
    targets = explicit("who is the current director general of TERI", registry=reg, people=[dg])
    assert targets[0].reason == PERSON
    assert targets[0].url == "https://teriin.org/profile/vibha-dhawan"
    assert ("People - committee of directors", NAME) in [(t.name, t.reason) for t in targets]


def test_a_named_person_outranks_a_post(reg):
    dg = Person("Dr Vibha Dhawan", "https://teriin.org/profile/vibha-dhawan", "Committee",
                "Director General")
    sethi = Person("Mr Girish Sethi", "https://teriin.org/profile/girish-sethi", "Committee",
                   "Senior Director, Energy")
    targets = explicit("did Girish Sethi meet the director general", registry=reg,
                       people=[dg, sethi])
    assert [t.name for t in targets if t.reason == PERSON] == ["Mr Girish Sethi"]


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

    @staticmethod
    def _themes():
        return parse([
            {"name": "Advanced Biofuels & Biochemicals Theme", "page_url": "https://teriin.org/a",
             "description": "Advanced."},
            {"name": "Pyrolytic Biofuels, Biochar & Green Chemicals Theme",
             "page_url": "https://teriin.org/p", "description": "Pyrolytic."},
            {"name": "Microbial Biofuels and Biochemicals Theme", "page_url": "https://teriin.org/m",
             "description": "Microbial."},
            {"name": "Air Theme", "page_url": "https://teriin.org/air", "description": "Air."},
        ])

    @staticmethod
    def _theme_embed(texts):
        axes = {"Advanced": [1.0, 0, 0, 0], "Pyrolytic": [0, 1.0, 0, 0],
                "Microbial": [0, 0, 1.0, 0], "Air": [0, 0, 0, 1.0]}
        return [next(v for k, v in axes.items() if t.startswith(k)) for t in texts]

    def test_a_tie_between_themes_sharing_the_questions_word_reads_each(self):
        """Measured 2026-10-21: "TERI's work on biofuels" sat at 0.500, 0.488
        and 0.456 against the three biofuel themes and read none of them."""
        targets, _ = similar([0.50, 0.46, 0.49, 0.0], registry=self._themes(),
                             embed=self._theme_embed,
                             question="tell me about TERI's work on biofuels")
        assert [(t.name, t.reason) for t in targets] == [
            ("Advanced Biofuels & Biochemicals Theme", SIMILAR),
            ("Pyrolytic Biofuels, Biochar & Green Chemicals Theme", SIMILAR),
            ("Microbial Biofuels and Biochemicals Theme", SIMILAR),
        ]

    @pytest.mark.parametrize("question, vector", [
        ("tell me about TERI's work", [0.50, 0.46, 0.49, 0.0]),
        ("biofuels and air quality", [0.60, 0.0, 0.0, 0.58]),
    ])
    def test_a_tie_the_questions_words_do_not_explain_reads_nothing(self, question, vector):
        targets, _ = similar(vector, registry=self._themes(), embed=self._theme_embed,
                             question=question)
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

    @pytest.mark.parametrize("question, team", [
        ("climate change theme team", True),
        ("who are the members of the water theme", True),
        ("top climate change theme team", False),   # a choice, by seniority
        ("who leads the climate change team", False),
        ("climate change researchers", False),
    ])
    def test_a_team_as_it_stands_is_told_from_a_choice_among_people(self, question, team):
        assert match.asks_for_team(question) is team

    def test_staff_is_an_about_reason(self):
        # So each listing's opening section is admitted whatever it scores.
        assert match.STAFF in match.ABOUT
