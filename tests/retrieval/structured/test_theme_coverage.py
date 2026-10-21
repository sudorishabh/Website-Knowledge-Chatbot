"""A theme named by part of its name stands for a topic only when it holds it.

The title table and the theme counts are stubbed, so nothing here touches MySQL.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.retrieval.structured import filters, names, planner
from app.retrieval.structured.types import RecordFilters

#: Pages tagged with each theme, as the catalog stood on 2026-10-21.
TAGGED = {
    "Advanced Biofuels & Biochemicals": 1,
    "Marine and Coastal": 3,
    "Climate Change": 1220,
    "Buildings": 208,
}

TITLES = [
    *(f"Biofuels from waste, part {i}" for i in range(30)),
    *(f"A biofuel policy note {i}" for i in range(12)),
    *(f"Marine plastics survey {i}" for i in range(30)),
    *(f"Climate finance brief {i}" for i in range(926)),
    *(f"Green buildings rating {i}" for i in range(251)),
]


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(get_settings(), "catalog_title_names_enabled", True)
    monkeypatch.setattr(names, "_load", lambda: (TITLES, {}))
    monkeypatch.setattr("app.catalog.state.website_titles",
                        lambda: [(str(i), title, None) for i, title in enumerate(TITLES)])
    asked: list[str] = []

    def count_documents(**kw):
        asked.append(kw["theme"])
        return TAGGED[kw["theme"]]

    monkeypatch.setattr("app.catalog.queries.count_documents", count_documents)
    monkeypatch.setattr(filters, "_theme_sizes", {})
    return asked


def test_titles_are_counted_by_every_spelling(catalog):
    assert names.titled(["biofuels"]) == 42
    assert names.titled([]) == 0


@pytest.mark.parametrize("requested, resolved", [
    ("biofuels", "Advanced Biofuels & Biochemicals"),
    ("marine", "Marine and Coastal"),
])
def test_a_theme_tagged_on_fewer_pages_than_titles_name_the_topic_is_not_it(
    catalog, requested, resolved
):
    """Measured 2026-10-21: "how many publications on biofuels" counted 1."""
    assert not filters.covers_topic(requested, resolved)
    assert not filters.stands_for_topic(requested, resolved)


def test_a_theme_holding_the_topic_stands_for_it(catalog):
    assert filters.covers_topic("climate", "Climate Change")
    assert filters.stands_for_topic("climate", "Climate Change")


def test_a_theme_named_in_full_is_what_was_asked_for(catalog):
    assert filters.covers_topic("building", "Buildings")
    assert filters.covers_topic("climate change", "Climate Change")
    assert catalog == []  # nothing to measure


def test_a_theme_that_drops_part_of_the_ask_never_stands_for_it(catalog):
    assert not filters.stands_for_topic("climate change adaptation", "Climate Change")


def test_the_theme_count_is_reused(catalog):
    filters.covers_topic("biofuels", "Advanced Biofuels & Biochemicals")
    filters.covers_topic("biofuel", "Advanced Biofuels & Biochemicals")
    assert catalog == ["Advanced Biofuels & Biochemicals"]


def test_without_titles_to_fall_back_on_the_theme_stands(catalog, monkeypatch):
    monkeypatch.setattr(get_settings(), "catalog_title_names_enabled", False)
    assert filters.covers_topic("biofuels", "Advanced Biofuels & Biochemicals")


def test_a_catalog_that_cannot_count_leaves_the_theme_as_it_was(catalog, monkeypatch):
    def broken(**kw):
        raise RuntimeError("MySQL down")

    monkeypatch.setattr("app.catalog.queries.count_documents", broken)
    assert filters.covers_topic("biofuels", "Advanced Biofuels & Biochemicals")


# -- planning and resolving ------------------------------------------------------

RESOLVED = {
    "biofuels": "Advanced Biofuels & Biochemicals",
    "marine": "Marine and Coastal",
    "climate": "Climate Change",
}

_SLOTS = dict(
    operation="count", bundle=None, theme=None, tags=[], author=None,
    title_contains=None, group_by=None, secondary_group_by=None,
    count_of="records", limit=10, date_from=None, date_to=None, year=None,
)


@pytest.fixture
def resolving(catalog, monkeypatch):
    monkeypatch.setattr(filters, "resolve_theme", lambda name: RESOLVED.get((name or "").lower()))
    monkeypatch.setattr(filters, "_resolve_name", lambda kind, value: filters._NameMatch(
        RESOLVED.get((value or "").lower()) if kind == "theme" else value, "accept"))
    return catalog


def _plan(question, **slots):
    return planner.plan(SimpleNamespace(**{**_SLOTS, **slots}), question=question).calls[0]


def test_a_count_on_a_topic_its_theme_holds_a_corner_of_is_counted_by_title(resolving):
    call = _plan("how many publications on biofuels", theme="biofuels")
    assert call.filters.theme is None
    assert call.filters.title_names == ("biofuels",)


def test_a_list_on_such_a_topic_is_constrained_by_its_words(resolving):
    call = _plan("list publications on marine", operation="list", theme="marine")
    assert "marine" in call.filters.topic_terms


def test_a_count_on_a_topic_its_theme_holds_keeps_the_theme(resolving):
    call = _plan("how many publications on climate", theme="climate")
    assert call.filters.theme == "climate" and call.filters.title_names == ()


def test_the_tools_drop_the_theme_the_planner_did(resolving):
    scope = filters.resolve_filters(RecordFilters(theme="marine"))
    assert scope.theme is None and scope.theme_widened == "Marine and Coastal"
    assert filters.resolve_filters(RecordFilters(theme="climate")).theme == "Climate Change"
