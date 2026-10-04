"""The home page's thematic areas: which it lists, and how each is described."""
from __future__ import annotations

from pathlib import Path

from app.retrieval.priority.extract import PageContent, Section, extract
from app.retrieval.priority.registry import parse
from app.retrieval.priority.themes import ThematicArea, areas_on

FIXTURES = Path(__file__).parent / "fixtures"
REGISTRY = parse([
    {"name": "Home", "page_url": "https://www.teriin.org", "description": "Home."},
    {"name": "Climate Change Theme", "page_url": "https://teriin.org/climate",
     "description": "Covers climate science and policy."},
    {"name": "Environment and Public Health Theme",
     "page_url": "https://teriin.org/environment-and-public-health",
     "description": "Examines environment and health."},
    {"name": "Water Theme", "page_url": "https://teriin.org/water",
     "description": "Water security."},
])


def _home():
    html = (FIXTURES / "home.html").read_text(encoding="utf-8")
    return extract(html, "https://www.teriin.org/")


def _page(*lines, heading="Thematic Areas"):
    return PageContent(title="Home", sections=(Section(heading, "\n".join([heading, *lines])),),
                       links=(), content_hash="h")


def test_the_home_pages_themes_in_its_order():
    names = [area.name for area in areas_on(_home(), REGISTRY)]
    assert names == ["Sustainable Agriculture", "Climate Change", "Energy", "Environment",
                     "Sustainable Habitat", "Environment & Public Health",
                     "Resources & Sustainable Development"]


def test_a_theme_with_a_page_is_described_from_the_page_list():
    """The home page cuts its teasers mid-sentence; the page list's is whole."""
    areas = {area.name: area for area in areas_on(_home(), REGISTRY)}
    assert areas["Climate Change"] == ThematicArea(
        "Climate Change", "Covers climate science and policy.", "https://teriin.org/climate")
    # "&" on the home page, "and" in the page list.
    assert areas["Environment & Public Health"].url == (
        "https://teriin.org/environment-and-public-health")


def test_a_theme_without_a_page_keeps_its_teaser_as_an_ellipsis():
    energy = {area.name: area for area in areas_on(_home(), REGISTRY)}["Energy"]
    assert energy.url is None
    assert energy.description == (
        "A transition to clean energy lies at the heart of India realizing its long-term…")


def test_a_sub_theme_the_home_page_does_not_list_is_not_a_thematic_area():
    assert "Water" not in [area.name for area in areas_on(_home(), REGISTRY)]


def test_a_name_with_no_teaser_still_counts_when_the_page_list_knows_it():
    areas = areas_on(_page("Climate Change", "Energy", "Clean energy for all..."), REGISTRY)
    assert [(a.name, a.description) for a in areas] == [
        ("Climate Change", "Covers climate science and policy."),
        ("Energy", "Clean energy for all…"),
    ]


def test_a_page_without_the_section_lists_no_themes():
    assert areas_on(_page("Climate Change", heading="Key Projects"), REGISTRY) == []
