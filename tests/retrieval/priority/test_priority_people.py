"""People on the listings, and which of them a question names."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.retrieval.priority.extract import extract
from app.retrieval.priority.people import Person, holding, named_in, people_on

FIXTURES = Path(__file__).parent / "fixtures"


def _listing(name: str, url: str, label: str) -> list[Person]:
    return people_on(extract((FIXTURES / name).read_text(encoding="utf-8"), url), label)


@pytest.fixture(scope="module")
def people():
    return (
        _listing("committee-of-directors.html",
                 "https://teriin.org/people/committee-of-directors", "Committee of Directors")
        + _listing("governing-council.html",
                   "https://teriin.org/people/governing-council", "Governing Council")
    )


def _names(found):
    return [p.name for p in found]


def test_every_linked_person_is_read_once_with_their_title(people):
    directors = [p for p in people if p.listing == "Committee of Directors"]
    assert len(directors) == len({p.profile_url for p in directors}) == 36
    dg = next(p for p in directors if p.name == "Dr Vibha Dhawan")
    assert dg.profile_url == "https://teriin.org/profile/vibha-dhawan"
    assert dg.title == "Director General"


def test_council_members_keep_their_own_profile_path(people):
    desai = next(p for p in people if p.name == "Mr Nitin Desai")
    assert desai.profile_url == "https://teriin.org/governing-council/mr-nitin-desai"
    assert desai.title == "Chairman - Governing Council"


def test_a_full_name_is_found_whatever_the_case_or_honorific(people):
    assert _names(named_in("who is alekhya datta?", people)) == ["Mr Alekhya Datta"]
    assert _names(named_in("Tell me about Dr. Vibha Dhawan", people)) == ["Dr Vibha Dhawan"]


def test_a_possessive_still_names_the_person(people):
    assert _names(named_in("What is Nitin Desai's background?", people)) == ["Mr Nitin Desai"]


def test_a_surname_after_an_honorific_is_enough_when_it_is_unique(people):
    assert _names(named_in("what does Dr Dhawan do", people)) == ["Dr Vibha Dhawan"]


def test_a_bare_surname_is_not_enough(people):
    assert named_in("dhawan", people) == []


def test_a_shared_surname_never_matches_on_its_own(people):
    shared = [p for p in people if p.parts and p.parts[-1] == "mathur"]
    assert len(shared) >= 2
    assert named_in("what does Dr Mathur work on", people) == []


def test_a_question_naming_nobody_finds_nobody(people):
    assert named_in("what are TERI's main themes", people) == []
    assert named_in("who is the director general", people) == []


def test_initials_are_optional_in_a_name():
    rashmi = Person("Mr R R Rashmi", "https://teriin.org/profile/rr-rashmi", "Committee")
    assert rashmi.parts == ("rashmi",)
    assert named_in("who is RR Rashmi", [rashmi]) == [rashmi]


def test_a_short_one_word_name_is_too_weak_to_match():
    person = Person("Ms Anu", "https://teriin.org/profile/anu", "Committee")
    assert named_in("anu", [person]) == []


def test_a_name_written_as_it_sounds_is_found(people):
    """Measured 2026-10-04: "tell me about PK bhatacharia" read no profile and
    was answered from old event listings that gave a former title."""
    assert _names(named_in("tell me about PK bhatacharia", people)) == ["Dr P K Bhattacharya"]
    assert _names(named_in("who is vibha dhavan", people)) == ["Dr Vibha Dhawan"]
    assert _names(named_in("what does Sunil Pandey work on", people)) == ["Dr Suneel Pandey"]
    assert _names(named_in("what does Dr Dhavan do", people)) == ["Dr Vibha Dhawan"]


def test_a_fuller_name_may_be_a_letter_out(people):
    assert _names(named_in("suneel pande", people)) == ["Dr Suneel Pandey"]
    assert _names(named_in("PK bhatacharaya", people)) == ["Dr P K Bhattacharya"]


def test_a_lone_word_must_sound_like_the_name(people):
    """One word is too little to read past a letter out: without the initials
    "bhatacharaya" names nobody, and "Mr Malik" is not Mr Mullick."""
    assert named_in("bhatacharaya", people) == []
    assert named_in("who is Mr Malik", people) == []


def test_a_listed_spelling_is_not_a_misspelling_of_another(people):
    assert _names(named_in("Dr Bhattacharjya", people)) == ["Mr Souvik Bhattacharjya"]
    assert _names(named_in("Dr Bhattacharya", people)) == ["Dr P K Bhattacharya"]
    assert named_in("bhattacharjya", people) == []


def test_the_reading_that_explains_more_of_the_name_wins(people):
    """"Bhattacharya" alone is Dr P K Bhattacharya, but beside "Souvik" it is
    Mr Souvik Bhattacharjya's surname misspelt."""
    assert _names(named_in("souvik bhattacharya", people)) == ["Mr Souvik Bhattacharjya"]


def test_initials_are_read_together_or_apart():
    pk = Person("Dr P K Bhattacharya", "https://teriin.org/profile/p-k-bhattacharya", "Committee")
    assert pk.initials == "pk"
    for question in ("PK Bhatacharya", "P K Bhatacharya", "Dr P.K. Bhatacharya"):
        assert named_in(question, [pk]) == [pk]
    assert Person("Mr S Vijay Kumar", "https://teriin.org/profile/s", "X").initials == ""


def test_a_title_is_read_as_its_post_and_area(people):
    sethi = next(p for p in people if p.name == "Mr Girish Sethi")
    assert sethi.post == ("senior director", frozenset({"energy"}))
    desai = next(p for p in people if p.name == "Mr Nitin Desai")
    assert desai.post == ("chairman", frozenset({"governing", "council"}))
    dg = next(p for p in people if p.title == "Director General")
    assert dg.post == ("director general", frozenset())


@pytest.mark.parametrize("question, holder", [
    ("who is the current director general of TERI", ["Dr Vibha Dhawan"]),
    ("who is TERI's DG", ["Dr Vibha Dhawan"]),
    # One chairman on the listings, so the council may go unsaid.
    ("who is the chairman of TERI", ["Mr Nitin Desai"]),
    ("who is the director of human resources at TERI", ["Ms Gauri Mathur"]),
    ("who is the executive director of strategy and partnerships",
     ["Mr Sanjay Seth"]),
    # The closest title wins: the senior director, not the director of the area.
    ("who is the senior director of electricity and renewables", ["Mr A K Saxena"]),
    ("who is the director of electricity and renewables", ["Mr Alekhya Datta"]),
    # Two holders of one post: both profiles.
    ("who is the director of industrial energy efficiency",
     ["Dr G R Narsimha Rao", "Mr Prosanto Pal"]),
])
def test_a_post_names_whoever_holds_it(people, question, holder):
    assert _names(holding(question, people)) == holder


@pytest.mark.parametrize("question", [
    # More than two holders: the listing answers it, not their profiles.
    "who are the members of the governing council",
    "who are the directors of TERI",
    # A post several people hold, without the area that tells them apart.
    "who is the senior director",
    "who is the executive director",
    # The listings say who holds a post now, not who held it.
    "who was the first director general of TERI",
    "who served as director general",
    "who stepped down as director general",
    "director general in 2015",
    "what are TERI's themes",
])
def test_a_post_that_names_no_one_person_now_names_nobody(people, question):
    assert holding(question, people) == []


def test_one_person_on_two_listings_is_one_profile():
    a = Person("Mr R R Rashmi", "https://teriin.org/profile/rr-rashmi", "Committee")
    b = Person("Mr R R Rashmi", "https://teriin.org/profile/rr-rashmi", "Fellows")
    assert named_in("R R Rashmi", [a, b]) == [a]


def test_the_director_general_on_two_listings_is_one_person(people):
    listed = [p for p in people if p.name == "Dr Vibha Dhawan"]
    assert len({p.profile_url for p in listed}) == 2
    found = named_in("who is Vibha Dhawan", people)
    assert [p.profile_url for p in found] == ["https://teriin.org/profile/vibha-dhawan"]


def test_only_profile_paths_count_as_people():
    html = (
        '<div class="region region-content"><h1>People</h1>'
        '<h3><a href="/profile/jane-doe">Dr Jane Doe</a></h3><p>Fellow</p>'
        '<a href="/people/governing-council">Governing Council</a>'
        '<a href="/files/cv.pdf">Dr Jane Doe CV</a></div>'
    )
    found = people_on(extract(html, "https://teriin.org/people/x"), "X")
    assert [(p.name, p.profile_url, p.title) for p in found] == [
        ("Dr Jane Doe", "https://teriin.org/profile/jane-doe", "Fellow")
    ]
