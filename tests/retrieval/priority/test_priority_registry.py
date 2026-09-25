"""The priority page list: parsing its shapes, and the shipped file."""
from __future__ import annotations

import json

import pytest

from app.retrieval.priority import registry
from app.retrieval.priority.registry import (
    CENTRE,
    PAGE,
    PEOPLE,
    THEME,
    normalize_text,
    normalize_url,
    parse,
)

SAMPLE = [
    {"name": "Home", "page_url": "https://www.teriin.org", "description": "Landing page."},
    {"name": "Policy", "page_url": "https://www.teriin.org/policy", "description": "Policy work."},
    {"name": "founder", "page_url": "https://teriin.org/history", "description": "History."},
    {
        "name": "People - committee of directors",
        "page_url": "https://teriin.org/people/committee-of-directors",
        "description": "Directors.",
        "example-of-people-url": "https://teriin.org/profile/[name-of-person]",
    },
    {
        "name": "Regional centers",
        "description": "Centres across India.",
        "children": [{"name": "Goa", "site_url": "https://teriin.org/goa", "description": "Goa."}],
    },
    {
        "name": "Energy Theme",
        "page_url": "https://teriin.org/energy",
        "description": "Energy.",
        # The file allows a page's children; the themes no longer use them.
        "children": [
            {"name": "Energy Efficiency Theme", "page_url": "https://teriin.org/energy-efficiency",
             "description": "Efficiency."},
        ],
    },
    {"name": "Climate Change Theme", "page_url": "https://teriin.org/climate",
     "description": "Climate.", "children": []},
]


def _page(reg, path):
    return reg.by_key(f"teriin.org{path}")


def test_urls_compare_without_scheme_www_case_or_trailing_slash():
    assert normalize_url("https://www.teriin.org/CSDR/") == normalize_url("http://teriin.org/csdr")
    assert normalize_url("https://teriin.org/policy?x=1#top") == "teriin.org/policy"
    assert normalize_url(None) == ""


def test_text_normalization_reads_ampersand_as_and_and_drops_possessives():
    assert normalize_text("TERI's Forest & Biodiversity work") == "teri forest and biodiversity work"


def test_pages_groups_and_kinds_are_told_apart():
    reg = parse(SAMPLE)
    assert {p.key for p in reg.pages} == {
        "teriin.org/", "teriin.org/policy", "teriin.org/history",
        "teriin.org/people/committee-of-directors", "teriin.org/goa", "teriin.org/energy",
        "teriin.org/energy-efficiency", "teriin.org/climate",
    }
    assert _page(reg, "/policy").kind == PAGE
    assert _page(reg, "/people/committee-of-directors").kind == PEOPLE
    assert _page(reg, "/goa").kind == CENTRE
    assert _page(reg, "/climate").kind == THEME
    assert _page(reg, "/energy-efficiency").kind == THEME
    assert [g.name for g in reg.groups] == ["Regional centers"]


def test_site_url_is_read_like_page_url():
    assert _page(parse(SAMPLE), "/goa").url == "https://teriin.org/goa"


def test_a_child_page_knows_its_parent_and_group():
    reg = parse(SAMPLE)
    assert _page(reg, "/energy-efficiency").parent == "Energy Theme"
    assert _page(reg, "/goa").group == "Regional centers"
    assert _page(reg, "/climate").group is None


def test_a_group_lists_only_its_direct_members():
    (centres,) = parse(SAMPLE).groups
    assert [m.name for m in centres.members] == ["Goa"]


def test_a_single_common_word_is_not_a_name_that_matches():
    reg = parse(SAMPLE)
    assert "policy" not in _page(reg, "/policy").aliases
    assert "climate change" in _page(reg, "/climate").aliases


def test_a_theme_is_named_by_its_topic_with_or_without_theme_after_it():
    reg = parse(SAMPLE)
    climate, energy = _page(reg, "/climate"), _page(reg, "/energy")
    assert climate.topic == "Climate Change" and energy.topic == "Energy"
    assert {"climate change", "climate change theme", "climate change thematic"} <= set(climate.aliases)
    # One word names a topic, not the page — unless "theme" follows it.
    assert "energy" not in energy.aliases
    assert {"energy theme", "energy thematic"} <= set(energy.aliases)


def test_the_home_page_is_named_by_the_phrases_that_ask_for_the_themes():
    reg = parse(SAMPLE)
    assert reg.home is not None and reg.home.key == "teriin.org/" and reg.home.kind == PAGE
    assert set(registry.THEMES_OVERVIEW) <= set(reg.home.aliases)
    assert not any(p.is_home for p in reg.pages if p is not reg.home)


def test_curated_aliases_name_pages_the_file_labels_differently():
    reg = parse(SAMPLE)
    assert "founder" in _page(reg, "/history").aliases
    assert "director general" in _page(reg, "/people/committee-of-directors").aliases
    assert "committee of directors" in _page(reg, "/people/committee-of-directors").aliases


def test_a_malformed_entry_is_skipped_not_fatal():
    reg = parse([{"name": "No url"}, "junk", {"page_url": "https://teriin.org/x"}, *SAMPLE])
    assert len(reg.pages) == 8


def test_a_missing_file_is_an_empty_registry(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "priority_pages_path", str(tmp_path / "missing.json"))
    registry.reload_registry()
    try:
        assert registry.load_registry().pages == ()
    finally:
        monkeypatch.undo()
        registry.reload_registry()


@pytest.fixture(scope="module")
def shipped():
    return parse(json.loads(registry.DEFAULT_PATH.read_text(encoding="utf-8")))


def test_the_shipped_list_has_60_pages_with_unique_urls(shipped):
    assert len(shipped.pages) == 60
    assert len({p.key for p in shipped.pages}) == 60


def test_every_shipped_page_is_on_the_organisations_site(shipped):
    assert shipped.hosts == frozenset({"teriin.org"})


def test_the_shipped_list_has_only_the_regional_centres_group(shipped):
    assert [g.name for g in shipped.groups] == ["Regional centers"]
    assert len(shipped.groups[0].members) == 7


def test_the_shipped_themes_are_flat_and_the_home_page_is_listed(shipped):
    themes = [p for p in shipped.pages if p.kind == THEME]
    assert len(themes) == 38
    assert all(p.parent is None and p.group is None for p in themes)
    assert shipped.home is not None and shipped.home.url == "https://www.teriin.org"
