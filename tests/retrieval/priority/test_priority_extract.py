"""Cutting rendered pages into sections, on real pages saved from the site
(scripts and styles stripped), and keeping linked documents as links."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.retrieval.priority.extract import MAX_SECTION_CHARS, extract

FIXTURES = Path(__file__).parent / "fixtures"


def _page(name: str, url: str):
    return extract((FIXTURES / name).read_text(encoding="utf-8"), url)


@pytest.fixture(scope="module")
def climate():
    return _page("climate.html", "https://teriin.org/climate")


def test_a_theme_page_opens_with_its_title_and_introduction(climate):
    lead = climate.sections[0]
    assert lead.lead and lead.heading == "Climate Change"
    assert "post-Paris agreement era" in lead.text
    assert not any(s.lead for s in climate.sections[1:])


def test_the_page_own_section_titles_open_sections(climate):
    headings = [s.heading for s in climate.sections]
    for expected in ("NEW IN CLIMATE CHANGE", "Projects", "Articles", "Events", "Services", "Team"):
        assert expected in headings


def test_item_titles_stay_inside_their_section(climate):
    articles = next(s for s in climate.sections if s.heading == "Articles")
    assert "Rooted Resilience" in articles.text


def test_menus_and_footer_are_left_out(climate):
    text = climate.text
    assert "Thematic Areas" not in text
    assert "mailbox@teri.res.in" not in text
    assert "Custom menu" not in text


def test_link_only_chrome_is_dropped(climate):
    assert not any(line.strip() == "Read more" for line in climate.text.splitlines())


def test_the_team_section_names_people_with_their_titles(climate):
    team = next(s for s in climate.sections if s.heading == "Team")
    assert "Ms Suruchi Bhadwal" in team.text
    assert "Director, Climate Change and Air Quality" in team.text


def test_a_document_behind_read_more_is_attached_to_its_item(climate):
    url = "https://teriin.org/files/A_Transformative_Global_Goal_New_File.pdf"
    assert f"A Transformative Global Goal on Adaptation: Scope, Science and Policy ({url})" in climate.text
    assert [(d.text, d.href) for d in climate.documents] == [
        ("A Transformative Global Goal on Adaptation: Scope, Science and Policy", url)
    ]


def test_every_document_on_a_listing_page_is_kept_as_a_link():
    fcra = _page("fcra-financials.html", "https://teriin.org/fcra-financials")
    assert len(fcra.documents) == 69
    assert all(d.href.lower().endswith(".pdf") for d in fcra.documents)
    assert "Auditor's Report (https://teriin.org/sites/default/files/files/fcra-receipts/" \
           "Auditor-Report-2024-25.pdf)" in fcra.text


def test_a_long_page_is_cut_into_sections_that_each_name_their_heading():
    fcra = _page("fcra-financials.html", "https://teriin.org/fcra-financials")
    assert len(fcra.sections) > 1
    for section in fcra.sections:
        assert len(section.text) <= MAX_SECTION_CHARS
        assert section.text.startswith("FCRA Financials")


def test_a_people_listing_drops_its_tab_strip_and_keeps_profile_links():
    listing = _page("committee-of-directors.html", "https://teriin.org/people/committee-of-directors")
    assert listing.sections[0].heading == "Committee of Directors"
    assert "Dr Vibha Dhawan\nDirector General" in listing.sections[0].text
    hrefs = {link.href: link.text for link in listing.links}
    assert hrefs["https://teriin.org/profile/vibha-dhawan"] == "Dr Vibha Dhawan"


def test_the_governing_council_links_its_own_profile_path():
    council = _page("governing-council.html", "https://teriin.org/people/governing-council")
    hrefs = {link.href: link.text for link in council.links}
    assert hrefs["https://teriin.org/governing-council/mr-nitin-desai"] == "Mr Nitin Desai"


def test_a_profile_does_not_repeat_the_name_under_its_title():
    profile = _page("profile-alekhya-datta.html", "https://teriin.org/profile/alekhya-datta")
    lines = profile.sections[0].text.splitlines()
    assert lines[:2] == ["Mr Alekhya Datta", "Director, Electricity & Renewables"]
    assert "Electrical engineer" in profile.text


def test_a_document_reached_only_through_an_image_is_named_by_its_file():
    html = (
        '<div class="region region-content"><h1 class="page-header">Brochures</h1>'
        '<p>Our brochures.</p><a href="/files/Green%20Shipping.pdf"><img src="x.jpg"></a>'
        '</div><div class="region region-footer">footer</div>'
    )
    page = extract(html, "https://teriin.org/brochures")
    assert page.sections[-1].heading == "Documents linked on this page"
    assert "Green Shipping.pdf (https://teriin.org/files/Green%20Shipping.pdf)" in page.text


def test_tables_keep_their_cells_apart():
    html = (
        '<div class="region region-content"><h1>Figures</h1><table>'
        '<tr><th>Year</th><th>Receipts</th></tr><tr><td>2024</td><td>12</td></tr>'
        '</table></div>'
    )
    assert "Year | Receipts\n2024 | 12" in extract(html, "https://teriin.org/f").text


def test_the_content_hash_moves_only_with_the_text():
    html = '<div class="region region-content"><h1>A</h1><p>one</p></div>'
    same = extract(html + "<!-- build 2 -->", "https://teriin.org/a")
    assert extract(html, "https://teriin.org/a").content_hash == same.content_hash
    changed = extract(html.replace("one", "two"), "https://teriin.org/a")
    assert changed.content_hash != same.content_hash


def test_a_page_with_no_content_has_no_sections():
    assert extract("<html><body></body></html>", "https://teriin.org/x").sections == ()
