"""The two failure modes that matter more than any other test in this suite.

Both are about the same trade-off, pulled in opposite directions, and a system
that only ever prefers newer evidence passes one and fails the other. Testing
"newer wins" alone would have been worse than testing nothing, because it would
have locked in the bug.

Driven through the real reranker with hand-placed relevance and dates, so each
test states the relationship it is asserting instead of inheriting one from an
embedding model.
"""

from __future__ import annotations

import pytest

from app.retrieval.search import reranker, temporal_gate as tg
from tests.evaluation.harness import doc

TODAY_ISH = "2026-09-14"


@pytest.fixture(autouse=True)
def _deterministic_relevance(monkeypatch):
    """Rank on the scores each test states. See the same fixture in
    tests/retrieval/search/test_temporal_intent.py for why this is needed."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "reranker_provider", "embedding", raising=False)


def _order(candidates, temporal=None):
    return [c.id for c in reranker.rerank("the question", candidates, temporal=temporal)]


# --------------------------------------------------------------------------- #
# Failure mode 1 — new but irrelevant evidence wins
# --------------------------------------------------------------------------- #

def test_recent_but_weakly_relevant_evidence_does_not_win():
    """Old + highly relevant must beat new + weakly relevant.

    This is the failure a freshness score produces, and the reason temporal fit
    is banded *inside* the relevance band rather than blended into a score.
    """
    old_and_relevant = doc("old-relevant", relevance=0.92, start="2011-03-01")
    new_and_weak = doc("new-weak", relevance=0.38, start="2026-06-01")

    assert _order([new_and_weak, old_and_relevant])[0] == "old-relevant"


@pytest.mark.parametrize(
    "mode", [tg.NONE, tg.CURRENT, tg.PAST, tg.POINT_IN_TIME, tg.DATE_RANGE]
)
def test_that_holds_under_every_temporal_intent(mode):
    """Not an artefact of having no temporal intent: no mode may promote weak
    evidence across a relevance band."""
    old_and_relevant = doc("old-relevant", relevance=0.92, start="2011-03-01",
                           end="2011-12-31")
    new_and_weak = doc("new-weak", relevance=0.38, bundle="ongoing_projects",
                       start="2026-06-01")
    intent = tg.TemporalIntent(mode=mode, date_from="2026-01-01",
                               date_to="2027-01-01")

    assert _order([new_and_weak, old_and_relevant], intent)[0] == "old-relevant", mode


# --------------------------------------------------------------------------- #
# Failure mode 2 — old but stale evidence wins a current-state question
# --------------------------------------------------------------------------- #

def test_stale_evidence_loses_a_current_state_question():
    """Two comparably relevant passages, one still valid and one long closed:
    for "what is the current status", the valid one must lead.

    Note what distinguishes them. It is *not* publication date — the closed one
    and the open one start in the same year. It is whether the period is still
    open, which is what `temporal_fit` reads.
    """
    closed = doc("closed-2016", relevance=0.80, bundle="news",
                 start="2016-01-01", end="2016-02-01")
    still_valid = doc("running", relevance=0.80, bundle="ongoing_projects",
                      start="2016-01-01")

    current = tg.TemporalIntent(mode=tg.CURRENT)
    assert _order([closed, still_valid], current)[0] == "running"


def test_the_same_pair_is_untouched_without_a_current_question():
    """The control: the reordering above is caused by the temporal intent, not
    by the two documents' metadata on their own."""
    closed = doc("closed-2016", relevance=0.80, bundle="news",
                 start="2016-01-01", end="2016-02-01")
    still_valid = doc("running", relevance=0.80, bundle="ongoing_projects",
                      start="2016-01-01")

    baseline = _order([closed, still_valid])
    assert _order([closed, still_valid], tg.TemporalIntent(mode=tg.NONE)) == baseline
    assert _order([closed, still_valid], None) == baseline


def test_a_superseded_version_cannot_lead_a_current_question():
    """Defence in depth behind the `is_current` pre-filter."""
    superseded = doc("superseded", relevance=0.80, start="2026-01-01",
                     is_current=False)
    current_version = doc("current", relevance=0.80, start="2025-01-01",
                          end="2027-01-01")

    order = _order([superseded, current_version], tg.TemporalIntent(mode=tg.CURRENT))
    assert order[0] == "current"


# --------------------------------------------------------------------------- #
# The two together
# --------------------------------------------------------------------------- #

def test_relevance_outranks_validity_but_validity_breaks_the_tie():
    """The whole ordering policy in one case.

    Three passages, one current-state question:
      * `weak-valid`     — currently valid, materially less relevant
      * `strong-stale`   — highly relevant, period closed
      * `strong-valid`   — highly relevant, currently valid

    Expected: strong-valid, then strong-stale (same relevance band, worse
    temporal fit), then weak-valid (a relevance band lower, and no amount of
    validity lifts it).
    """
    weak_valid = doc("weak-valid", relevance=0.40, bundle="ongoing_projects",
                     start="2015-01-01")
    strong_stale = doc("strong-stale", relevance=0.88, bundle="news",
                       start="2015-01-01", end="2015-02-01")
    strong_valid = doc("strong-valid", relevance=0.88, bundle="ongoing_projects",
                       start="2015-01-01")

    order = _order([weak_valid, strong_stale, strong_valid],
                   tg.TemporalIntent(mode=tg.CURRENT))
    assert order == ["strong-valid", "strong-stale", "weak-valid"]


def test_a_historical_question_inverts_only_the_tie_break():
    """The mirror: asked about the past, the closed period leads — and the
    weakly relevant passage still comes last."""
    weak_valid = doc("weak-valid", relevance=0.40, bundle="ongoing_projects",
                     start="2015-01-01")
    strong_stale = doc("strong-stale", relevance=0.88, bundle="news",
                       start="2015-01-01", end="2015-02-01")
    strong_valid = doc("strong-valid", relevance=0.88, bundle="ongoing_projects",
                       start="2015-01-01")

    order = _order([weak_valid, strong_stale, strong_valid],
                   tg.TemporalIntent(mode=tg.PAST))
    assert order[0] == "strong-stale"
    assert order[-1] == "weak-valid"
    assert "strong-valid" in order, "history must reorder evidence, never drop it"
