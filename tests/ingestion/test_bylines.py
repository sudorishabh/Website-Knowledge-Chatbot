"""Author names out of Drupal author fields (app.ingestion.bylines).

Every byline below is copied from the live site (2026-10-04) — the formats the
comma split got wrong, and the ordinary ones it must keep getting right.
"""
from __future__ import annotations

import pytest

from app.ingestion.bylines import authors_from, split_byline
from app.ingestion.canonical import drupal_facets


@pytest.mark.parametrize(
    "byline, people",
    [
        # The corpus's usual form: the comma is the separator.
        ("Pandey Suneel, Khan M Emran", ["Pandey Suneel", "Khan M Emran"]),
        ("Gautam S K, R Suresh, Sharma V P and Sehgal M",
         ["Gautam S K", "R Suresh", "Sharma V P", "Sehgal M"]),
        ("Dr Vibha Dhawan", ["Dr Vibha Dhawan"]),
        # One person written "Surname, Given".
        ("Sehgal, Meena", ["Sehgal Meena"]),
        # "and" / "&" / ";" between people.
        ("Dhup Saumya and Dhawan Vibha", ["Dhup Saumya", "Dhawan Vibha"]),
        ("Kalra Rishu, Conlan Xavier A., Bhat Mamta & Goel Mayurika",
         ["Kalra Rishu", "Conlan Xavier A.", "Bhat Mamta", "Goel Mayurika"]),
        ("Deepa N; Ganguly Shantanu", ["Deepa N", "Ganguly Shantanu"]),
        ("Karthikeya K; Sarma Mrinal Kumar,  Ramkumar N.,   Subudhi Sanjukta",
         ["Karthikeya K", "Sarma Mrinal Kumar", "Ramkumar N.", "Subudhi Sanjukta"]),
        # "Surname, Given" pairs separated by semicolons.
        ("Qamar, Sharif; Dr. Dhawan, Vibha", ["Qamar Sharif", "Dr. Dhawan Vibha"]),
        # Citation style: surname and initials alternate.
        ("Kumar, R., Das, M. M., Das, S., Verma, A., Valsan, G., Tamrakar, A., "
         "Warrier, A. K., Lamba, J., & Sharma, P.",
         ["Kumar R.", "Das M. M.", "Das S.", "Verma A.", "Valsan G.", "Tamrakar A.",
          "Warrier A. K.", "Lamba J.", "Sharma P."]),
        ("Ramaiaha, M.; Varma, R.; D'Souzab, F.; Giriyan, A.",
         ["Ramaiaha M.", "Varma R.", "D'Souzab F.", "Giriyan A."]),
        ("Ghosh, Sayanta, Zaidi, A., Chauhan, P., Soni, A., & Sharma, J. V.",
         ["Ghosh Sayanta", "Zaidi A.", "Chauhan P.", "Soni A.", "Sharma J. V."]),
        ("Ray, R., Primavera-Tirol, Y. H., San Diego-McGlone, M. L., & Nadaoka, K.",
         ["Ray R.", "Primavera-Tirol Y. H.", "San Diego-McGlone M. L.", "Nadaoka K."]),
        # Mixed: one citation-style pair among ordinary names.
        ("Sehgal, M, Suresh R, Sharma VP, and Gautam SK",
         ["Sehgal M", "Suresh R", "Sharma VP", "Gautam SK"]),
        ("Deshmukh SK, Verekar SA, Ganguli, BN",
         ["Deshmukh SK", "Verekar SA", "Ganguli BN"]),
        # Real one-word names stay people of their own.
        ("Snehmani ,Singh Mritunjay Kumar, Gupta, R. D., Bhardwaj Anshuman",
         ["Snehmani", "Singh Mritunjay Kumar", "Gupta R. D.", "Bhardwaj Anshuman"]),
        ("Neha, Kansal Arun", ["Neha", "Kansal Arun"]),
        ("Kaur Mehak , Kusum, Goel Mayurika", ["Kaur Mehak", "Kusum", "Goel Mayurika"]),
        # Acronyms are organisations, never initials to attach.
        ("Mr Karan Mangotra, Ms Swati Agarwal, NRDC, IGSD",
         ["Mr Karan Mangotra", "Ms Swati Agarwal", "NRDC", "IGSD"]),
        # Footnote digits and non-breaking spaces are formatting.
        ("Miranda1 Ana F, Ramkumar Narasimhan", ["Miranda Ana F", "Ramkumar Narasimhan"]),
        ("John O,\xa0Gummidi B,\xa0Tewari A", ["John O", "Gummidi B", "Tewari A"]),
        # "Anand" is not "and".
        ("Anand Kumar, Ferdinand Lee", ["Anand Kumar", "Ferdinand Lee"]),
        # Runs of spaces collapse; accents written as spacing marks survive.
        ("Adholeya  Alok,\tGerber Julien-Franc¸ois, Dum´ee Ludovic F.",
         ["Adholeya Alok", "Gerber Julien-Franc¸ois", "Dum´ee Ludovic F."]),
        # A missing comma cannot be recovered from the text; the run stays whole.
        ("Batra Vidya S.   Balakrishnan Malini", ["Batra Vidya S. Balakrishnan Malini"]),
    ],
)
def test_a_byline_names_each_person_once(byline, people):
    assert split_byline(byline) == people


@pytest.mark.parametrize("empty", ["", "   ", ",", " ; & "])
def test_an_empty_byline_names_nobody(empty):
    assert split_byline(empty) == []


def test_a_list_field_is_taken_item_by_item():
    """The CMS already separated these people; a comma inside one is kept."""
    assert authors_from(["Sehgal, Meena", "Dr Pia Sethi"]) == ["Sehgal, Meena", "Dr Pia Sethi"]


def test_list_items_lose_separators_and_invisible_characters():
    assert authors_from(["David Palchak |", "Ilya Chernyakhovskiy |", "​Rana Masud"]) == [
        "David Palchak", "Ilya Chernyakhovskiy", "Rana Masud",
    ]


def test_email_addresses_are_not_authors():
    assert authors_from(["reetas@teri.res.in", "Dr Pia Sethi"]) == ["Dr Pia Sethi"]
    assert authors_from("shantanu.ganguly@teri.res.in, Deepa N") == ["Deepa N"]


def test_repeats_are_dropped_in_order():
    assert authors_from("Das S, Kumar R, Das S") == ["Das S", "Kumar R"]


def test_no_field_no_authors():
    assert authors_from(None) == []


# --------------------------------------------------------------------------- #
# Through drupal_facets — the one place ingestion reads authors.
# --------------------------------------------------------------------------- #

def test_a_free_text_byline_is_read_as_people():
    facets = drupal_facets({"field_rpaper_authors": "Sehgal, Meena"}, [])
    assert facets["authors"] == ["Sehgal Meena"]


def test_the_field_chosen_is_unchanged():
    """Which field supplies the authors is not this fix's to change: the first
    list-valued field still wins over a free-text byline."""
    facets = drupal_facets({
        "field_rpaper_author": ["Dr Malini Balakrishnan"],
        "field_rpaper_authors": "Alharthi A I, Balakrishnan M",
    }, [])
    assert facets["authors"] == ["Dr Malini Balakrishnan"]


def test_tags_and_themes_still_split_on_commas():
    facets = drupal_facets({"field_tags": "Solar, Wind", "field_theme": "Energy"}, [])
    assert facets["tags"] == ["Solar", "Wind"]
    assert facets["categories"] == ["Energy"]
