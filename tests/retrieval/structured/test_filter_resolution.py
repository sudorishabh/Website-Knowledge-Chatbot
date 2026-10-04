"""Unit tests for fuzzy name canonicalization in the filter path.

This is where entity resolution actually affects results: names are resolved on
the way to SQL, so a misspelling filters on the name the catalog stores no matter
which tool runs or how the plan is shaped (the plan's calls execute in parallel
and cannot pass values to each other). Candidate sourcing/scoring itself lives in
test_entity_resolution*.py. No DB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.retrieval.structured import filters as F
from app.retrieval.structured import resolve, tools
from app.retrieval.structured.types import RecordFilters

AUTHORS = ["Rishabh Negi", "Rishab Nigam", "A K Sharma"]


@pytest.fixture
def resolution_on(monkeypatch):
    """entity_resolution_enabled=True plus a known author pool."""
    monkeypatch.setattr(
        F, "get_settings",
        lambda: SimpleNamespace(entity_resolution_enabled=True,
                                database_multi_call_enabled=False),
    )
    monkeypatch.setattr("app.catalog.queries.distinct_authors", lambda **kw: list(AUTHORS))
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr("app.catalog.queries.find_tag", lambda name: None)
    resolve.reload_authors()
    yield
    resolve.reload_authors()


@pytest.fixture
def resolution_off(monkeypatch):
    """Same known pools, flag off — matching still runs, reporting does not."""
    monkeypatch.setattr(
        F, "get_settings",
        lambda: SimpleNamespace(entity_resolution_enabled=False,
                                database_multi_call_enabled=False),
    )
    monkeypatch.setattr("app.catalog.queries.distinct_authors", lambda **kw: list(AUTHORS))
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr("app.catalog.queries.find_tag", lambda name: None)
    resolve.reload_authors()
    yield
    resolve.reload_authors()


# --------------------------------------------------------------------------- #
# resolve.plausible — what a clarification may offer.
# --------------------------------------------------------------------------- #

def _cand(name, score):
    return resolve.EntityCandidate(id=name, canonical_name=name, type="author", score=score)


def test_plausible_drops_candidates_below_the_ambiguity_floor():
    """A blind top-N slice would offer an unrelated name as a choice."""
    ranked = [_cand("Rishabh Negi", 0.75), _cand("Rishab Nigam", 0.75),
              _cand("A K Sharma", 0.375)]
    assert [c.canonical_name for c in resolve.plausible(ranked)] == [
        "Rishabh Negi", "Rishab Nigam",
    ]


def test_plausible_caps_the_list():
    ranked = [_cand(f"Author {i}", 0.9) for i in range(10)]
    assert len(resolve.plausible(ranked)) == 3


# --------------------------------------------------------------------------- #
# _resolve_name — canonicalization. Runs unconditionally: theme and tag filters
# match names exactly in SQL, so matching is part of how filtering works. The
# feature flag only gates what a caller *does* with an imperfect match.
# --------------------------------------------------------------------------- #

def test_confident_match_becomes_the_canonical_name(resolution_on):
    match = F._resolve_name(resolve.AUTHOR, "rishab negi")
    assert match.name == "Rishabh Negi"
    assert match.band == resolve.ACCEPT


def test_matching_runs_even_with_the_flag_disabled(resolution_off):
    """Exact SQL matching would fail on the raw spelling, so the name is still
    canonicalized — the flag decides reporting, not matching."""
    assert F._resolve_name(resolve.AUTHOR, "rishab negi").name == "Rishabh Negi"


def test_near_tie_offers_candidates_and_takes_the_best_name(resolution_on):
    match = F._resolve_name(resolve.AUTHOR, "rishab")
    assert match.band == resolve.AMBIGUOUS
    # The best candidate is carried so a non-strict caller can proceed; whether
    # to ask instead is resolve_filters' decision.
    assert match.name in {"Rishabh Negi", "Rishab Nigam"}
    assert match.candidates == ["Rishabh Negi", "Rishab Nigam"]


def test_miss_keeps_the_typed_name_so_filtering_still_happens(resolution_on):
    match = F._resolve_name(resolve.AUTHOR, "Zzz Nonexistent")
    assert match.name == "Zzz Nonexistent"
    assert match.band == resolve.MISS and match.candidates == []


def test_blank_name_resolves_to_nothing(resolution_on):
    assert F._resolve_name(resolve.AUTHOR, None).name is None
    assert F._resolve_name(resolve.AUTHOR, "").name is None


def test_resolution_failure_degrades_to_the_typed_name(monkeypatch, resolution_on):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.retrieval.structured.resolve.resolve_entity", boom)
    match = F._resolve_name(resolve.AUTHOR, "rishab negi")
    assert match.name == "rishab negi" and match.band is None


# --------------------------------------------------------------------------- #
# resolve_filters — the effective filter set the tools render from.
# --------------------------------------------------------------------------- #

def test_effective_filters_carry_the_canonical_author(resolution_on):
    scope = F.resolve_filters(RecordFilters(author="rishab negi"))
    assert scope.author == "Rishabh Negi"          # what reaches SQL
    assert scope.effective.author == "Rishabh Negi"  # what the answer states
    assert scope.ambiguous is None and scope.author_missed is False


def test_effective_filters_preserve_unrelated_fields(resolution_on):
    scope = F.resolve_filters(
        RecordFilters(author="rishab negi", title_contains="grid", date_from="2024-01-01")
    )
    assert scope.effective.title_contains == "grid"
    assert scope.effective.date_from == "2024-01-01"


def test_author_missed_is_flagged(resolution_on):
    scope = F.resolve_filters(RecordFilters(author="Zzz Nonexistent"))
    assert scope.author_missed is True
    assert scope.author == "Zzz Nonexistent"  # still filters; may match nothing


def test_ambiguity_is_carried_on_the_scope(resolution_on):
    scope = F.resolve_filters(RecordFilters(author="rishab"))
    assert scope.ambiguous is not None and scope.ambiguous.kind == "author"
    assert scope.ambiguous.query == "rishab"  # quotes what the user typed


def test_ambiguity_is_not_raised_when_the_flag_is_off(resolution_off):
    """Legacy behaviour: take the best candidate and answer, never interrupt."""
    scope = F.resolve_filters(RecordFilters(author="rishab"))
    assert scope.ambiguous is None
    assert scope.author in {"Rishabh Negi", "Rishab Nigam"}


def _theme_vocab(*names):
    return [{"theme": n, "theme_type": "primary", "parent": None,
             "theme_group": "main", "documents": 3} for n in names]


def test_misspelled_theme_canonicalizes_to_the_stored_name(monkeypatch, resolution_on):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Environment", "Energy"),
    )
    scope = F.resolve_filters(RecordFilters(theme="enviroment"))
    assert scope.effective.theme == "Environment"
    assert scope.theme == "Environment"  # what reaches SQL


def test_tag_is_matched_exactly_not_fuzzily(monkeypatch, resolution_on):
    """A long-tail vocabulary: an exact hit resolves (with the stored casing),
    a near-miss does not silently become a different tag."""
    stored = {"waste management": "Waste management", "solid waste": "Solid waste"}
    monkeypatch.setattr(
        "app.catalog.queries.find_tag", lambda name: stored.get(name.strip().lower())
    )
    assert F.resolve_filters(RecordFilters(tag="waste MANAGEMENT")).tag == "Waste management"
    scope = F.resolve_filters(RecordFilters(tag="waste managment"))  # typo
    assert scope.tag == "waste managment" and scope.tag_missed is True


# --------------------------------------------------------------------------- #
# End-to-end through the tools — the behaviour the feature promises.
# --------------------------------------------------------------------------- #

def test_misspelled_author_reaches_sql_as_the_canonical_name(monkeypatch, resolution_on):
    seen = {}
    monkeypatch.setattr(
        "app.catalog.queries.count_documents", lambda **kw: seen.update(kw) or 12
    )
    r = tools.count_records(None, RecordFilters(author="rishab negi"))
    assert seen["author"] == "Rishabh Negi"
    assert r.rendered == "**Rishabh Negi** has **12 publications** on the site."
    assert r.data["applied"]["author"] == "Rishabh Negi"


def test_ambiguous_author_asks_without_querying(monkeypatch, resolution_on):
    def forbid(**kw):
        raise AssertionError("must not query on an ambiguous filter")

    monkeypatch.setattr("app.catalog.queries.count_documents", forbid)
    r = tools.count_records(None, RecordFilters(author="rishab"))
    assert r.ok is False and r.error_kind == "ambiguous"
    assert r.rendered == (
        "'rishab' matches more than one author:\n"
        "1. Rishabh Negi\n2. Rishab Nigam\nWhich did you mean?"
    )


def test_missing_author_with_no_rows_is_a_terminal_miss(monkeypatch, resolution_on):
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 0)
    r = tools.count_records(None, RecordFilters(author="Zzz Nonexistent"))
    assert r.ok is False and r.error_kind == "unresolved"
    assert r.rendered == "No author matching 'Zzz Nonexistent' found."


def test_missing_author_that_still_matches_rows_is_answered(monkeypatch, resolution_on):
    """A fuzzy miss is not proof of absence — if the substring filter still
    found documents, the honest answer is the count."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 3)
    r = tools.count_records(None, RecordFilters(author="Negi"))
    assert r.ok is True and r.data["count"] == 3


def test_list_records_applies_the_same_canonicalization(monkeypatch, resolution_on):
    seen = {}
    monkeypatch.setattr(
        "app.catalog.queries.list_documents", lambda **kw: seen.update(kw) or []
    )
    tools.list_records(None, RecordFilters(author="rishab negi"))
    assert seen["author"] == "Rishabh Negi"


def test_aggregate_records_applies_the_same_canonicalization(monkeypatch, resolution_on):
    seen = {}
    monkeypatch.setattr(
        "app.catalog.queries.distribution",
        lambda group_by, **kw: seen.update(kw) or [("news", 2)],
    )
    r = tools.aggregate_records(None, "content_type", RecordFilters(author="rishab negi"))
    assert seen["author"] == "Rishabh Negi"
    assert "by **Rishabh Negi**" in r.rendered


def test_disabled_flag_still_canonicalizes_but_reports_no_miss(monkeypatch, resolution_off):
    """The rollback path: matching still runs (exact SQL needs it), but an
    unrecognized name falls through instead of producing a terminal message —
    which `answerer` gates on the flag."""
    seen = {}
    monkeypatch.setattr(
        "app.catalog.queries.count_documents", lambda **kw: seen.update(kw) or 12
    )
    r = tools.count_records(None, RecordFilters(author="rishab negi"))
    assert seen["author"] == "Rishabh Negi"
    assert r.ok is True and r.data["count"] == 12
