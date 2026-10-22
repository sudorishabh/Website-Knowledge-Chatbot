"""A group's own listing page: the members it names, and the answer they make."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.retrieval.priority import evidence as ev
from app.retrieval.priority import fetch as fetching
from app.retrieval.priority.extract import extract
from app.retrieval.priority.listings import GroupListing, listing_answer, members_on
from app.retrieval.priority.registry import parse

FIXTURES = Path(__file__).parent / "fixtures"
LISTING_URL = "https://teriin.org/centre-of-excellence"
REGISTRY = parse([
    {"name": "Centres of Excellence", "listing_url": LISTING_URL,
     "description": "Focused centres.", "children": [
         {"name": "Mahindra-TERI Centre of Excellence",
          "page_url": "https://teriin.org/Mahindra-TERI-centre-of-excellence",
          "description": "Sustainable habitat."},
     ]},
    {"name": "Regional centers", "description": "Centres across India.", "children": [
        {"name": "Goa", "site_url": "https://teriin.org/goa", "description": "Coastal work."},
    ]},
])
EXCELLENCE, REGIONAL = REGISTRY.groups

#: The nine centres the live page listed on 2026-10-22, in its order.
CENTRES = [
    "CONCOR–TERI Centre of Excellence for Green and Sustainable Logistics",
    "TERI-Centre of Excellence for Coastal Studies and Resource Management (CoE-CSRM)",
    "NMCG-TERI Centre of Excellence on Water Reuse (NTCoE)",
    "Centre of Excellence for Energy Transition (CoEET)",
    "Mahindra-TERI Centre of Excellence",
    "National Centre of Excellence in Green Ports and Shipping",
    "NCEARAN Centre of Excellence (Supported by DBT)",
    "TERI-CFCL Centre of Excellence (CoE)",
    "Tata Chemicals Ltd.-TERI Centre of Excellence for Biochemicals",
]


def _html() -> str:
    return (FIXTURES / "centre-of-excellence.html").read_text(encoding="utf-8")


def test_the_listing_page_names_every_member_in_its_order():
    assert members_on(extract(_html(), LISTING_URL)) == CENTRES


def test_a_page_without_linked_names_lists_no_one():
    html = "<html><body><div class='region-content'><h1>Centres</h1><p>Coming soon.</p></div></body></html>"
    assert members_on(extract(html, LISTING_URL)) == []


@pytest.fixture
def _pages(monkeypatch):
    calls: list[str] = []

    def fake_fetch(url, *, allowed_hosts, validate=None, now=None):
        calls.append(url)
        if url != LISTING_URL:
            return None
        return fetching.FetchedPage(
            url=url, final_url=url, html=_html(),
            fetched_at=datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(fetching, "fetch", fake_fetch)
    return calls


def test_the_group_listing_is_read_from_the_live_page(_pages):
    listing = ev.group_listing(EXCELLENCE, REGISTRY)
    assert _pages == [LISTING_URL]
    assert listing == GroupListing(group="Centres of Excellence", names=tuple(CENTRES),
                                   url=LISTING_URL, title="Centres of Excellence")


def test_a_group_with_no_listing_page_is_not_read(_pages):
    assert ev.group_listing(REGIONAL, REGISTRY) is None
    assert _pages == []


def test_no_group_listing_when_the_page_cannot_be_read(monkeypatch):
    monkeypatch.setattr(fetching, "fetch", lambda url, **kw: None)
    assert ev.group_listing(EXCELLENCE, REGISTRY) is None


_LISTING = GroupListing(group="Centres of Excellence", names=("CoEET", "Mahindra-TERI"),
                        url=LISTING_URL, title="Centres of Excellence")


def test_the_answer_is_the_pages_names_alone_in_its_order():
    assert listing_answer(_LISTING) == (
        "TERI has 2 Centres of Excellence:\n\n"
        "1. CoEET\n"
        "2. Mahindra-TERI\n\n"
        "Ask about any one of them to hear more about its work."
    )


def test_the_answer_can_be_a_table():
    answer = listing_answer(_LISTING, output_format="table")
    assert "| # | Centres of Excellence |" in answer
    assert "| 2 | Mahindra-TERI |" in answer


def test_no_answer_without_names():
    assert listing_answer(GroupListing(group="Centres of Excellence", names=(), url=LISTING_URL)) == ""
