"""From matched pages to context blocks: which sections lead, the cache
fingerprint, dropping stored copies, and merging with the corpus."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.core.models.context import PRIORITY_PAGE_KIND, ContextBlock, is_priority_page
from app.retrieval.priority import evidence as ev
from app.retrieval.priority import fetch as fetching
from app.retrieval.priority import match
from app.retrieval.priority.registry import parse

FIXTURES = Path(__file__).parent / "fixtures"
PAGES = {
    "https://www.teriin.org": "home.html",
    "https://teriin.org/climate": "climate.html",
    "https://teriin.org/people/committee-of-directors": "committee-of-directors.html",
    "https://teriin.org/profile/alekhya-datta": "profile-alekhya-datta.html",
    "https://teriin.org/fcra-financials": "fcra-financials.html",
}
REGISTRY = parse([
    {"name": "Home", "page_url": "https://www.teriin.org",
     "description": "The landing page, with the thematic areas."},
    {"name": "FCRA Financials", "page_url": "https://teriin.org/fcra-financials",
     "description": "FCRA disclosures."},
    {"name": "People - committee of directors",
     "page_url": "https://teriin.org/people/committee-of-directors",
     "description": "Directors.", "example-of-people-url": "https://teriin.org/profile/[name]"},
    {"name": "Regional centers", "description": "Centres across India.", "children": [
        {"name": "Goa", "site_url": "https://teriin.org/goa", "description": "Coastal work."},
        {"name": "Mumbai", "site_url": "https://teriin.org/mumbai", "description": "Western work."},
    ]},
    {"name": "Climate Change Theme", "page_url": "https://teriin.org/climate",
     "description": "Climate science and policy.", "children": []},
])
QUERY = [1.0, 0.0]


def _embed_on(*words):
    """Sections containing any of ``words`` score 1.0 against QUERY, others 0."""
    def embed(texts):
        return [[1.0, 0.0] if any(w in t for w in words) else [0.0, 1.0] for t in texts]
    return embed


@pytest.fixture(autouse=True)
def _pages(monkeypatch):
    calls: list[str] = []

    def fake_fetch(url, *, allowed_hosts, validate=None, now=None):
        calls.append(url)
        name = PAGES.get(url)
        if name is None:
            return None
        return fetching.FetchedPage(
            url=url, final_url=url, html=(FIXTURES / name).read_text(encoding="utf-8"),
            fetched_at=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(fetching, "fetch", fake_fetch)
    monkeypatch.setattr(get_settings(), "priority_max_pages", 3)
    monkeypatch.setattr(get_settings(), "priority_max_blocks", 3)
    monkeypatch.setattr(get_settings(), "priority_section_floor", 0.40)
    monkeypatch.setattr(get_settings(), "priority_match_threshold", 0.48)
    monkeypatch.setattr(get_settings(), "priority_match_margin", 0.06)
    ev.clear_vectors()
    match.clear_vectors()
    yield calls
    ev.clear_vectors()
    match.clear_vectors()


def _gather(question, embed=None, **kw):
    return ev.gather(question, query_vector=QUERY, registry=REGISTRY,
                     embed=embed or _embed_on("\0never"), **kw)


def test_a_named_page_leads_with_its_opening_section():
    got = _gather("tell me about the climate change theme")
    assert [(t.name, t.reason) for t in got.targets] == [("Climate Change Theme", match.NAME)]
    lead = got.blocks[0]
    assert "post-Paris agreement era" in lead.text
    assert is_priority_page(lead.payload)
    assert lead.payload["kind"] == PRIORITY_PAGE_KIND
    assert lead.payload["source_type"] == "website"
    assert lead.payload["source_authority"] == 1.0
    assert lead.payload["source_url"] == "https://teriin.org/climate"
    assert lead.payload["section_heading"] is None
    assert lead.payload["fetched_at"] == "2026-09-24T10:00:00+00:00"


def test_other_sections_are_admitted_only_above_the_floor():
    got = _gather("who works on climate change", embed=_embed_on("Suruchi Bhadwal"))
    headings = [b.payload.get("section_heading") for b in got.blocks]
    assert headings[0] is None
    assert "Team" in headings
    assert "Events" not in headings
    assert len(got.blocks) <= 3


def test_blocks_are_capped():
    got = _gather("climate change", embed=_embed_on("Climate"))
    assert len(got.blocks) == 3


def test_a_question_for_the_organisations_people_reads_the_people_listing():
    """Measured: "List TERI's leading researchers" read no people page and was
    refused, although the Committee of Directors listing held the answer."""
    got = _gather("yes leading researchers\nList TERI's leading researchers.")
    assert [(t.name, t.reason) for t in got.targets] == [
        ("People - committee of directors", match.STAFF)
    ]
    assert got.blocks and "Vibha Dhawan" in got.blocks[0].text


@pytest.mark.parametrize("question", [
    "climate change theme team",
    "who are the members of the climate change theme",
    "top climate change theme researchers",
])
def test_a_people_question_about_a_theme_leads_with_its_team(question):
    """Measured 2026-09-28: the theme's introduction and the listings' openings
    filled the three blocks, and the Team section never reached the answer."""
    got = _gather(question)
    lead = got.blocks[0]
    assert lead.payload["priority_page"] == "Climate Change Theme"
    assert lead.payload["section_heading"] == "Team"
    assert "Suruchi Bhadwal" in lead.text and "K Venkatramana" in lead.text
    assert not any("post-Paris agreement era" in b.text for b in got.blocks)


def test_a_team_question_is_answered_by_the_team_alone():
    """The page's Team section is the team; the staff listings would pad it
    with people who are not on it."""
    got = _gather("climate change theme team")
    assert [(t.name, t.reason) for t in got.targets] == [("Climate Change Theme", match.NAME)]
    assert got.blocks[0].payload["team_of"] == "Climate Change"


def test_a_question_for_the_top_people_reads_the_listings_beside_the_team():
    got = _gather("top climate change theme researchers")
    assert [(t.name, t.reason) for t in got.targets] == [
        ("Climate Change Theme", match.NAME),
        ("People - committee of directors", match.STAFF),
    ]
    assert got.blocks[0].payload["team_of"] == "Climate Change"
    assert "Vibha Dhawan" in got.blocks[1].text
    assert "team_of" not in got.blocks[1].payload


def test_a_team_question_on_a_page_with_no_team_reads_the_listings(monkeypatch):
    serve = fetching.fetch

    def fetch(url, **kw):
        if url != "https://teriin.org/goa":
            return serve(url, **kw)
        return fetching.FetchedPage(
            url=url, final_url=url, fetched_at=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
            html="<html><body><div class='region-content'><h1>Goa</h1><p>The Western "
                 "Regional Centre works on coastal ecology, marine resources and water "
                 "management.</p></div></body></html>",
        )

    monkeypatch.setattr(fetching, "fetch", fetch)
    got = _gather("goa team")
    assert [(t.name, t.reason) for t in got.targets] == [
        ("Goa", match.NAME), ("People - committee of directors", match.STAFF),
    ]
    assert "Vibha Dhawan" in got.blocks[1].text


def test_a_people_listing_is_read_as_one_block_past_the_section_cut(monkeypatch):
    """The Committee of Directors listing was 2,353 characters against a 2,400
    cut; split, its second half would reach the answer only by score."""
    people = "".join(f"<h4><a href='/profile/p{i}'>Dr Person Number {i}</a></h4>"
                     f"<div>Director, Area of Research Number {i}</div>" for i in range(60))
    html = f"<div class='region-content'><h1>Committee of Directors</h1>{people}</div>"
    monkeypatch.setattr(fetching, "fetch", lambda url, **kw: fetching.FetchedPage(
        url=url, final_url=url, html=html,
        fetched_at=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)))
    committee = next(p for p in REGISTRY.pages if p.name.startswith("People"))
    read = ev._read(match.Target(committee.name, committee.kind, match.STAFF,
                                 url=committee.url, page=committee), REGISTRY)
    assert len(read.content.text) > ev.MAX_SECTION_CHARS
    assert len(read.content.sections) == 1
    assert "Dr Person Number 59" in read.content.sections[0].text


def test_a_question_about_a_theme_itself_still_leads_with_its_introduction():
    got = _gather("what does the climate change theme work on")
    assert "post-Paris agreement era" in got.blocks[0].text


def test_a_page_with_no_team_keeps_its_introduction():
    content = ev.extract("<html><body><h1>Goa</h1><p>Coastal ecology work.</p></body></html>",
                         "https://teriin.org/goa")
    goa = next(p for p in REGISTRY.pages if p.name == "Goa")
    read = ev.PageRead(match.Target(goa.name, goa.kind, match.NAME, url=goa.url, page=goa),
                       goa.url, content=content)
    assert ev.team_index(content) is None
    assert ev._opening(read, people=True) == 0


def test_a_question_about_nothing_on_the_list_reads_nothing(_pages):
    got = _gather("what is blended finance")
    # Only the people listing is read, to know whose name to look for.
    assert _pages == ["https://teriin.org/people/committee-of-directors"]
    assert got.targets == [] and got.blocks == [] and got.fingerprint() == {}


def test_a_named_person_is_answered_from_their_profile(_pages):
    got = _gather("who is Alekhya Datta")
    assert got.targets[0].reason == match.PERSON
    assert "https://teriin.org/profile/alekhya-datta" in _pages
    assert "Electrical engineer" in got.blocks[0].text
    assert got.blocks[0].payload["source_url"] == "https://teriin.org/profile/alekhya-datta"


def test_a_group_is_answered_from_the_list_without_a_fetch(_pages):
    got = _gather("which regional centres are there")
    block = got.blocks[0]
    assert block.payload["title"] == "Regional centers"
    assert "- Goa: Coastal work. (https://teriin.org/goa)" in block.text
    assert "https://teriin.org/goa" not in _pages


def test_the_list_of_themes_is_answered_from_the_home_page(_pages):
    got = _gather("what are TERI's thematic areas")
    assert [(t.name, t.reason) for t in got.targets] == [("Home", match.NAME)]
    lead = got.blocks[0]
    assert lead.text.startswith("Thematic Areas\nSustainable Agriculture")
    assert lead.payload["source_url"] == "https://www.teriin.org"
    assert lead.payload["title"] == "TERI: Innovative Solutions for Sustainable Development - India"


def test_a_theme_listing_reads_the_home_page_whatever_the_wording(_pages):
    got = _gather("what are the main themes", themes_listing=True)
    assert [(t.name, t.reason) for t in got.targets] == [("Home", match.NAME)]
    assert "https://www.teriin.org" in _pages


def test_a_theme_listing_adds_no_page_by_description(_pages):
    # Every description scores 1.0 here; the list of themes still reads only the home page.
    got = _gather("what are TERI's thematic areas", embed=lambda texts: [[1.0, 0.0]] * len(texts))
    assert [t.name for t in got.targets] == ["Home"]
    assert got.similar_top == []


def test_one_named_theme_is_answered_from_its_own_page_not_the_list(_pages):
    got = _gather("tell me about the climate change thematic area", themes_listing=True)
    assert [t.name for t in got.targets] == ["Climate Change Theme"]
    assert "https://www.teriin.org" not in _pages


def test_a_page_that_cannot_be_read_contributes_nothing():
    registry = parse([{"name": "Green Shipping", "page_url": "https://teriin.org/green-shipping",
                       "description": "Maritime."}])
    got = ev.gather("tell me about green shipping", query_vector=QUERY, registry=registry,
                    embed=_embed_on("x"))
    assert got.blocks == []
    assert got.describe()["reads"][0]["ok"] is False


def test_linked_documents_arrive_as_links():
    got = _gather("FCRA financials", embed=_embed_on("\0never"))
    assert "(https://teriin.org/sites/default/files/files/fcra-receipts/" in got.blocks[0].text


def test_the_fingerprint_names_each_page_by_its_content():
    got = _gather("tell me about the climate change theme")
    (entry,) = got.fingerprint()["priority"]
    url, digest = entry.split("#")
    assert url == "teriin.org/climate"
    assert digest == got.blocks[0].payload["content_hash"][:16]


def test_an_embedding_failure_still_admits_the_opening_section():
    def boom(texts):
        raise RuntimeError("down")

    got = _gather("tell me about the climate change theme", embed=boom)
    assert len(got.blocks) == 1 and "post-Paris" in got.blocks[0].text


def test_gather_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ev, "_read_all", boom)
    assert _gather("tell me about the climate change theme").blocks == []


def _candidate(url, source_type="website"):
    return SimpleNamespace(payload={"source_url": url, "source_type": source_type})


def test_stored_copies_of_listed_pages_are_dropped_but_their_pdfs_stay():
    got = _gather("what is blended finance")
    candidates = [
        _candidate("https://teriin.org/fcra-financials/"),
        _candidate("https://teriin.org/fcra-financials", "pdf_attachment"),
        _candidate("https://teriin.org/article/something"),
    ]
    kept = got.drop_stored_copies(candidates, top_n=6)
    assert [c.payload["source_type"] for c in kept] == ["pdf_attachment", "website"]
    assert got.suppressed == 1


def test_a_listed_page_whose_stored_copy_ranked_is_read_live():
    got = ev.gather("FCRA receipts", query_vector=QUERY, registry=REGISTRY,
                    embed=_embed_on("Auditor"), explicit=[])
    assert got.blocks == []
    got.drop_stored_copies([_candidate("https://teriin.org/fcra-financials")], top_n=6)
    assert [t.reason for t in got.targets] == [match.SURFACED]
    assert got.blocks and "Auditor" in got.blocks[0].text


def test_a_stored_copy_ranked_below_the_cut_is_dropped_but_not_read():
    got = ev.gather("x", query_vector=QUERY, registry=REGISTRY, embed=_embed_on("Auditor"),
                    explicit=[])
    candidates = [_candidate(f"https://teriin.org/n{i}") for i in range(6)]
    candidates.append(_candidate("https://teriin.org/fcra-financials"))
    kept = got.drop_stored_copies(candidates, top_n=6)
    assert len(kept) == 6 and got.targets == []


def _block(text):
    return ContextBlock(n=0, text=text, payload={"source_type": "website"})


def test_merge_puts_priority_first_and_keeps_two_corpus_slots(monkeypatch):
    monkeypatch.setattr(get_settings(), "priority_own_slots", False)
    got = _gather("climate change", embed=_embed_on("Climate"))
    corpus = [_block(f"corpus {i}") for i in range(5)]
    merged = got.merge(corpus, limit=4, token_budget=100_000)
    assert [is_priority_page(b.payload) for b in merged] == [True, True, False, False]
    assert [b.n for b in merged] == [1, 2, 3, 4]


def test_with_own_slots_the_corpus_keeps_every_slot_it_had(monkeypatch):
    """Measured 2026-09-25: three live pages left "TERI top researchers" three
    corpus passages of six, in a 1,900-token context of a 9,000 budget."""
    monkeypatch.setattr(get_settings(), "priority_own_slots", True)
    got = _gather("climate change", embed=_embed_on("Climate"))
    assert len(got.blocks) == 3
    corpus = [_block(f"corpus {i}") for i in range(6)]
    merged = got.merge(corpus, limit=6, token_budget=100_000)
    assert [is_priority_page(b.payload) for b in merged] == [True] * 3 + [False] * 6
    assert [b.text for b in merged[3:]] == [f"corpus {i}" for i in range(6)]
    assert [b.n for b in merged] == list(range(1, 10))


def test_with_own_slots_the_pages_do_not_spend_the_corpus_budget(monkeypatch):
    from app.retrieval.context.builder import _count_tokens

    monkeypatch.setattr(get_settings(), "priority_own_slots", True)
    got = _gather("climate change", embed=_embed_on("Climate"))
    corpus = [_block("word " * 50) for _ in range(2)]
    corpus[1] = _block("other " * 50)
    budget = sum(_count_tokens(b.text) for b in corpus)
    merged = got.merge(corpus, limit=6, token_budget=budget)
    # Both corpus blocks fit the corpus's own budget, pages or no pages.
    assert sum(1 for b in merged if not is_priority_page(b.payload)) == 2
    # Shared, the same budget leaves room for the pages and not the corpus.
    monkeypatch.setattr(get_settings(), "priority_own_slots", False)
    shared = got.merge(corpus, limit=6, token_budget=budget)
    assert sum(1 for b in shared if not is_priority_page(b.payload)) < 2


def test_merge_gives_every_slot_to_priority_when_the_corpus_has_none():
    got = _gather("climate change", embed=_embed_on("Climate"))
    assert len(got.merge([], limit=6, token_budget=100_000)) == 3


def test_merge_skips_a_corpus_block_repeating_a_priority_block():
    got = _gather("tell me about the climate change theme")
    copy = _block(got.blocks[0].text)
    merged = got.merge([copy, _block("other")], limit=6, token_budget=100_000)
    assert [b.text for b in merged][1:] == ["other"]
