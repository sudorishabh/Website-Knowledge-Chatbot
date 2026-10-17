"""The contact page's offices: how they are read, and which centre each is."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.retrieval.priority.extract import PageContent, Section, extract
from app.retrieval.priority.offices import Office, by_centre, headquarters, offices_on
from app.retrieval.priority.registry import CENTRE, DEFAULT_PATH, parse

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def centres():
    groups = parse(json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))).groups
    (group,) = [g for g in groups if g.kind == CENTRE]
    return group.members


@pytest.fixture(scope="module")
def offices():
    html = (FIXTURES / "contact.html").read_text(encoding="utf-8")
    return offices_on(extract(html, "https://www.teriin.org/contact"))


def _page(*lines):
    return PageContent(title="Contact Us",
                       sections=(Section("Contact Us", "\n".join(["Contact Us", *lines]), lead=True),),
                       links=(), content_hash="h")


def test_the_contact_page_lists_every_office_in_its_order(offices):
    assert [o.name for o in offices] == [
        "Headquarters", "TERI Gram", "TERI North-Eastern Regional Centre, Guwahati",
        "Centre for Himalayan Studies", "TERI Southern Regional Centre, Bengaluru",
        "TERI Southern Regional Centre, Hyderabad", "TERI Western Regional Centre, Mumbai",
        "TERI Western Regional Centre, Goa", "TERI Himalayan Centre", "TERI Japan",
        "TERI School of Advanced Studies",
    ]


def test_an_office_keeps_its_address_and_contacts(offices):
    bengaluru = next(o for o in offices if o.name.endswith("Bengaluru"))
    assert bengaluru.postal_address == (
        "4th Main, 2nd Cross, Domlur II Stage, Bengaluru - 560 071, Karnataka"
    )
    assert bengaluru.phone == "(+91 80) 2535 6590-94 (4 lines)"
    assert bengaluru.email == "terisrc@teri.res.in"
    assert bengaluru.contact_person == "Dr G Rudra Narsimha Rao"


def test_an_address_in_telangana_is_not_a_telephone_line(offices):
    hyderabad = next(o for o in offices if o.name.endswith("Hyderabad"))
    assert hyderabad.address[-1] == "Telangana"
    assert hyderabad.phone == "+91 99499 96210"


def test_details_are_labelled_lines_without_the_empty_ones():
    office = Office("TERI Japan", ("Tokyo, Japan",), email="teri@iges.or.jp")
    assert office.details() == ["Address: Tokyo, Japan", "Email: teri@iges.or.jp"]


def test_every_centre_gets_its_own_office(offices, centres):
    found = by_centre(offices, centres)
    assert {c.name: found[c.key].name for c in centres} == {
        "Bengaluru": "TERI Southern Regional Centre, Bengaluru",
        "Goa": "TERI Western Regional Centre, Goa",
        "guwahati": "TERI North-Eastern Regional Centre, Guwahati",
        "Gurugram": "TERI Gram",
        "Mukteshwar - Nainital": "TERI Himalayan Centre",
        "Hyderabad": "TERI Southern Regional Centre, Hyderabad",
        "Mumbai": "TERI Western Regional Centre, Mumbai",
    }


def test_an_office_named_for_no_centre_is_matched_by_its_address(centres):
    offices = [Office("Gwal Pahari campus", ("Gurugram - Faridabad Road", "Gurugram - 122 003"),
                      email="a@teri.res.in")]
    assert by_centre(offices, centres)["teriin.org/photo-story/teri-gram-gwal-pahari"] == offices[0]


def test_the_headquarters_is_the_office_headed_so(offices):
    office = headquarters(offices)
    assert office is not None
    assert "New Delhi - 110 003" in office.postal_address
    assert office.email == "mailbox@teri.res.in"


def test_text_with_no_contact_line_is_not_an_office():
    page = _page("Headquarters", "New Delhi", "Email: mailbox@teri.res.in",
                 "Join our knowledge network")
    assert [o.name for o in offices_on(page)] == ["Headquarters"]
    assert offices_on(_page("Reach us at any of our offices.")) == []
