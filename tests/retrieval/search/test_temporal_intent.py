"""Phase B: temporal intent as a ranking signal, and the invariant it must not break.

Every candidate here carries explicit metadata and an explicit semantic score, so
each test states exactly which signal it is exercising. `rerank` is driven
directly — no Qdrant, no LLM, no network.

The load-bearing test in this file is
`test_a_newer_but_less_relevant_document_still_loses`: temporal fit is banded
*inside* the relevance band, so it can only ever reorder candidates relevance has
already called equivalent. If that ever stops being true, this file fails.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.retrieval.search import reranker, temporal_gate as tg
from app.retrieval.search.hybrid_search import Candidate

TODAY = date(2026, 9, 14)


@pytest.fixture(autouse=True)
def _deterministic_relevance(monkeypatch):
    """Rank on the scores each test states, not on a model's opinion of its
    placeholder text.

    A developer `.env` may set `reranker_provider = cross_encoder`, which
    re-scores every candidate from its text and discards the `semantic_score`
    the test set. With identical filler text that collapses a deliberate
    0.91-vs-0.40 relevance gap into one band — which is exactly the band
    separation the invariant tests exist to assert. Pinning the provider makes
    these a test of the *ranking keys*, which is what they are for.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "reranker_provider", "embedding", raising=False)


def _cand(cid, score, *, text="a passage about the thing asked about", **payload):
    payload.setdefault("source_type", "website")
    payload.setdefault("bundle", "page")
    payload.setdefault("is_current", True)
    return Candidate(id=cid, score=score, payload=payload, semantic_score=score,
                     vector=None)


def _order(candidates, temporal=None):
    return [c.id for c in reranker.rerank("the question", candidates, temporal=temporal)]


def _intent(mode, **kw):
    return tg.TemporalIntent(mode=mode, **kw)


# --------------------------------------------------------------------------- #
# temporal_fit — the signal itself
# --------------------------------------------------------------------------- #

def test_no_temporal_intent_scores_every_document_alike():
    """Requirement 6/12: a question with no temporal intent must not acquire one."""
    a = {"effective_start_date": "2012-01-01"}
    b = {"effective_start_date": "2026-01-01"}
    none = _intent(tg.NONE)
    assert not none.ranks
    assert tg.temporal_fit(a, none, reference=TODAY) == tg.temporal_fit(b, none, reference=TODAY)


def test_upcoming_does_not_rank_because_it_already_gates():
    assert not _intent(tg.UPCOMING).ranks


@pytest.mark.parametrize("payload, expected", [
    # An open-ended project started long ago IS current; a 2026 news item is not.
    ({"bundle": "ongoing_projects", "effective_start_date": "2005-04-01"}, tg.FIT_MATCH),
    ({"bundle": "news", "effective_start_date": "2026-01-05"}, tg.FIT_MISS),
    # The project's page runs to the present. A PDF attached to it does not: it
    # inherited the bundle, but it was written once, on the date it states.
    ({"bundle": "ongoing_projects", "source_type": "website",
      "effective_start_date": "2019-01-01"}, tg.FIT_MATCH),
    ({"bundle": "ongoing_projects", "source_type": "pdf_attachment",
      "effective_start_date": "2019-01-01", "start_precision": "year"}, tg.FIT_MISS),
    # A closed period covering today.
    ({"effective_start_date": "2024-01-01", "effective_end_date": "2027-01-01"}, tg.FIT_MATCH),
    # Not in force yet.
    ({"effective_start_date": "2030-01-01"}, tg.FIT_MISS),
    # No date at all is unknown, never a miss.
    ({}, tg.FIT_UNKNOWN),
])
def test_current_favours_validity_not_recency(payload, expected):
    """"Current" means *valid now*, not *published recently* — a 2005 project
    still running fits, a January 2026 announcement does not."""
    payload.setdefault("is_current", True)
    assert tg.temporal_fit(payload, _intent(tg.CURRENT), reference=TODAY) == expected


def test_current_rejects_a_superseded_version():
    """Requirement 9. `is_current == True` is already mandatory in
    `build_filter`, so this is defence in depth rather than the only guard."""
    payload = {"effective_start_date": "2026-01-01", "is_current": False}
    assert tg.temporal_fit(payload, _intent(tg.CURRENT), reference=TODAY) == tg.FIT_MISS


def test_the_is_current_prefilter_is_still_mandatory():
    """Phase B must not have weakened the version filter it relies on."""
    from qdrant_client.models import FieldCondition

    from app.retrieval.search.hybrid_search import build_filter

    conditions = {
        c.key: c.match.value for c in build_filter().must
        if isinstance(c, FieldCondition)
    }
    assert conditions["is_current"] is True


def test_past_favours_what_has_ended():
    ended = {"effective_start_date": "2015-01-01", "effective_end_date": "2016-01-01"}
    running = {"bundle": "ongoing_projects", "effective_start_date": "2015-01-01"}
    past = _intent(tg.PAST)
    assert tg.temporal_fit(ended, past, reference=TODAY) == tg.FIT_MATCH
    assert tg.temporal_fit(running, past, reference=TODAY) == tg.FIT_MISS


@pytest.mark.parametrize("payload, expected", [
    # Wholly inside the asked-for year.
    ({"effective_start_date": "2023-04-01", "effective_end_date": "2023-06-01"}, tg.FIT_MATCH),
    # Overlaps it but runs well outside.
    ({"effective_start_date": "2020-01-01", "effective_end_date": "2025-01-01"}, tg.FIT_PARTIAL),
    # Entirely before / after.
    ({"effective_start_date": "2019-01-01", "effective_end_date": "2019-12-31"}, tg.FIT_MISS),
    ({"effective_start_date": "2025-01-01"}, tg.FIT_MISS),
])
def test_date_range_scores_by_degree_of_overlap(payload, expected):
    """Requirement 4: the same interval semantics `filters.date_conditions`
    applies, scored rather than filtered — so a document that merely clips the
    window ranks below one that sits inside it."""
    window = _intent(tg.DATE_RANGE, date_from="2023-01-01", date_to="2024-01-01")
    assert tg.temporal_fit(payload, window, reference=TODAY) == expected


def test_point_in_time_respects_stated_precision():
    """A document stated as "2023" covers all of 2023 — it must not be read as
    1 January and miss a question about June."""
    year_precision = {"effective_start_date": "2023-01-01", "start_precision": "year"}
    asked = _intent(tg.POINT_IN_TIME, date_from="2023-06-01", date_to="2023-07-01")
    assert tg.temporal_fit(year_precision, asked, reference=TODAY) != tg.FIT_MISS


def test_a_mode_with_no_extracted_window_is_neutral():
    """The mode can fire lexically while understanding extracts no bounds.
    Guessing a window here would be the second classifier Phase B must not add."""
    payload = {"effective_start_date": "2019-01-01"}
    assert tg.temporal_fit(payload, _intent(tg.POINT_IN_TIME), reference=TODAY) == tg.FIT_UNKNOWN


@pytest.mark.parametrize("payload", [
    {"effective_start_date": "not-a-date"},
    {"effective_start_date": None},
    {"effective_start_date": "2020-01-01", "effective_end_date": "garbage"},
    {"effective_start_date": "2020-01-01", "start_precision": "decade"},
    {},
])
def test_malformed_temporal_metadata_never_raises(payload):
    """Requirement 11."""
    for mode in (tg.CURRENT, tg.PAST, tg.POINT_IN_TIME, tg.DATE_RANGE):
        intent = _intent(mode, date_from="2023-01-01", date_to="2024-01-01")
        assert 0.0 <= tg.temporal_fit(payload, intent, reference=TODAY) <= 1.0


# --------------------------------------------------------------------------- #
# The invariant: relevance → temporal → authority → substance → recency
# --------------------------------------------------------------------------- #

def test_a_newer_but_less_relevant_document_still_loses():
    """Requirement 7, and the whole point of banding inside relevance.

    The 2026 document is a perfect temporal fit for a "current" question and the
    2011 one is a miss — but it is also materially less relevant, so it stays
    behind. Temporal fit must never become "newest wins".
    """
    relevant_but_old = _cand("old-relevant", 0.91, effective_start_date="2011-01-01")
    fresh_but_weak = _cand(
        "new-irrelevant", 0.40,
        bundle="ongoing_projects", effective_start_date="2024-01-01",
    )
    assert _order([fresh_but_weak, relevant_but_old], _intent(tg.CURRENT))[0] == "old-relevant"


def test_a_similarly_relevant_better_fitting_document_can_lead():
    """Requirement 8: inside one relevance band, temporal fit decides.

    Both score 0.80 — the same band — so the currently-valid project leads the
    news item that merely mentions the subject and ended years ago.
    """
    stale = _cand("ended-2016", 0.80, bundle="news",
                  effective_start_date="2016-01-01", effective_end_date="2016-02-01")
    valid = _cand("running", 0.80, bundle="ongoing_projects",
                  effective_start_date="2015-01-01")
    assert _order([stale, valid], _intent(tg.CURRENT))[0] == "running"
    # ...and with no temporal intent the pair keeps its pre-Phase-B order.
    assert _order([stale, valid]) == _order([stale, valid], _intent(tg.NONE))


def test_temporal_fit_never_overrules_relevance_across_bands():
    """Exhaustive version of the invariant: whatever the mode, the candidate in
    the better relevance band leads."""
    strong = _cand("strong", 0.95, effective_start_date="2001-01-01",
                   effective_end_date="2001-12-31")
    weak = _cand("weak", 0.30, bundle="ongoing_projects",
                 effective_start_date="2020-01-01")
    for mode in (tg.CURRENT, tg.PAST, tg.POINT_IN_TIME, tg.DATE_RANGE, tg.NONE):
        intent = _intent(mode, date_from="2020-01-01", date_to="2021-01-01")
        assert _order([weak, strong], intent)[0] == "strong", mode


def test_historical_questions_keep_historical_evidence():
    """Requirements 2/10: a past-tense question must not be biased toward
    current documents, and the old evidence must still be there."""
    historical = _cand("2015-report", 0.80, bundle="report",
                       effective_start_date="2015-01-01",
                       effective_end_date="2015-12-31")
    current = _cand("running-project", 0.80, bundle="ongoing_projects",
                    effective_start_date="2015-01-01")
    order = _order([current, historical], _intent(tg.PAST))
    assert order[0] == "2015-report"
    assert "running-project" in order, "history must not be dropped, only reordered"


def test_no_temporal_intent_preserves_the_existing_order():
    """Requirement 5/12, stated as an equivalence rather than an assertion about
    one pair: for a mixed set, ranking with no intent is the same list as ranking
    the way the code did before `temporal` existed."""
    candidates = [
        _cand("a", 0.80, bundle="news", effective_start_date="2024-01-01"),
        _cand("b", 0.80, bundle="page", effective_start_date="2011-01-01"),
        _cand("c", 0.79, bundle="ongoing_projects", effective_start_date="2005-01-01"),
        _cand("d", 0.50, bundle="report", effective_start_date="2026-01-01"),
    ]
    baseline = [c.id for c in reranker.rerank("the question", candidates)]
    assert _order(candidates, _intent(tg.NONE)) == baseline
    assert _order(candidates, None) == baseline


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #

def _block(bid, **payload):
    from app.core.models.context import ContextBlock

    payload.setdefault("bundle", "events")
    return ContextBlock(n=1, text="t", payload=payload)


def test_past_gate_drops_occurrences_that_have_not_happened():
    future = _block("f", effective_start_date="2027-01-01")
    done = _block("d", effective_start_date="2020-01-01", effective_end_date="2020-01-02")
    kept = tg.gate_past([future, done], reference=TODAY)
    assert [b.payload["effective_start_date"] for b in kept] == ["2020-01-01"]


def test_past_gate_only_touches_scheduled_bundles():
    """Same narrow scope as the upcoming gate: a report is never an occurrence."""
    report = _block("r", bundle="report", effective_start_date="2027-01-01")
    done = _block("d", effective_start_date="2020-01-01", effective_end_date="2020-01-02")
    assert len(tg.gate_past([report, done], reference=TODAY)) == 2


def test_past_gate_refuses_to_empty_the_context():
    only_future = [_block("f", effective_start_date="2027-01-01")]
    assert tg.gate_past(only_future, reference=TODAY) == only_future


def test_upcoming_gate_is_unchanged_by_the_shared_helper():
    """Requirement 6: the pre-existing behaviour, re-asserted after the refactor
    that gave both gates one body."""
    over = _block("o", effective_start_date="2020-01-01", effective_end_date="2020-01-02")
    ahead = _block("a", effective_start_date="2027-01-01")
    kept = tg.gate_upcoming([over, ahead], reference=TODAY)
    assert [b.payload["effective_start_date"] for b in kept] == ["2027-01-01"]
