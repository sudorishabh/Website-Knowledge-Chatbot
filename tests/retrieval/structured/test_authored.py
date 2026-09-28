"""A person's latest publications: which author strings are theirs, and how the
section reads."""
from __future__ import annotations

import pytest

from app.catalog.models import StateRecord
from app.retrieval.structured import authored

AUTHORS = [
    "Dr Manish Kumar Shrivastava", "Mr Manish Shrivastava", "Shrivastava M  K",
    "Shrivastava Manish Kumar", "Dr Manish Kumar", "Mr Alekhya Datta", "Dr Arindam Datta",
    "Datta A", "Mr R R Rashmi", "Ms Rashmi Murali",
]


def test_one_persons_variants_are_merged():
    """Measured 2026-09-28: 18 of his documents were under his full name, and
    five more under three other spellings of it."""
    assert authored.author_names("Dr Manish Kumar Shrivastava", AUTHORS) == [
        "Dr Manish Kumar Shrivastava", "Mr Manish Shrivastava", "Shrivastava M  K",
        "Shrivastava Manish Kumar",
    ]


def test_a_variant_that_fits_two_people_is_neithers():
    assert authored.author_names("Mr Alekhya Datta", AUTHORS) == ["Mr Alekhya Datta"]
    assert authored.author_names("Dr Arindam Datta", AUTHORS) == ["Dr Arindam Datta"]


def test_a_persons_own_name_is_theirs_beside_a_fuller_one():
    assert authored.author_names("Mr Manish Shrivastava", AUTHORS) == ["Mr Manish Shrivastava"]


def test_a_shared_word_is_not_a_shared_name():
    # "Rashmi" is R R Rashmi's surname and Rashmi Murali's first name.
    assert authored.author_names("Mr R R Rashmi", AUTHORS) == ["Mr R R Rashmi"]
    assert authored.author_names("Nobody Known", AUTHORS) == []


def _record(title, bundle="policy_brief", day="2026-05-14", url="https://teriin.org/x"):
    return StateRecord(document_id=title, source_type="website", source_key=title,
                       fingerprint="f", bundle=bundle, title=title, url=url,
                       effective_start_date=day)


def test_the_latest_are_the_persons_website_pages(monkeypatch):
    seen = {}

    def list_documents(**kw):
        seen.update(kw)
        return [_record("Modeling for Climate Finance")]

    monkeypatch.setattr(authored, "known_authors", lambda: tuple(AUTHORS))
    monkeypatch.setattr(authored.queries, "list_documents", list_documents)
    monkeypatch.setattr(authored.queries, "count_documents", lambda **kw: 15)
    records, total = authored.latest_publications("Dr Manish Kumar Shrivastava")
    assert [r.title for r in records] == ["Modeling for Climate Finance"] and total == 15
    assert seen["source_type"] == "website" and seen["entity_type"] == "node"
    assert seen["limit"] == authored.LATEST
    assert "Shrivastava Manish Kumar" in seen["authors"]


def test_a_name_the_catalog_does_not_hold_queries_nothing(monkeypatch):
    monkeypatch.setattr(authored, "known_authors", lambda: tuple(AUTHORS))
    monkeypatch.setattr(authored.queries, "list_documents", pytest.fail)
    assert authored.latest_publications("Dr Prasoon Singh") == ([], 0)


def test_the_section_lists_each_with_its_type_and_date():
    records = [_record("Modeling for Climate Finance", url="https://teriin.org/policy-brief/m"),
               _record("COP30: Who is the enemy?", bundle="feature_articles", day="2025-11-22")]
    section = authored.publications_section("Dr Manish Kumar Shrivastava", records, 15)
    assert section.splitlines() == [
        "### Latest publications by Dr Manish Kumar Shrivastava",
        "The 2 most recent of 15:",
        "- [Modeling for Climate Finance](https://teriin.org/policy-brief/m) — policy brief, 14 May 2026",
        "- [COP30: Who is the enemy?](https://teriin.org/x) — feature article, 22 November 2025",
    ]


def test_the_section_says_nothing_of_a_total_it_shows_whole_or_of_none():
    section = authored.publications_section("Dr A", [_record("One")], 1)
    assert "most recent" not in section
    assert authored.publications_section("Dr A", [], 0) == ""
