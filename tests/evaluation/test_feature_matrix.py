"""Phase E: the feature matrix — each flag independently switchable.

One query, six configurations, and an assertion per row about what that row is
supposed to change. The point is not that each row works (the phase suites
already say so) but that the flags are **independent**: turning one on must not
turn another on, and turning all four on must not be different from the sum of
their effects.
"""

from __future__ import annotations

import pytest

from app.retrieval.search import temporal_gate as tg
from tests.evaluation.harness import (
    BASELINE, CLARIFIED, DECOMPOSED, FLAGS, FULL_STACK, MATRIX, PERSPECTIVE,
    TEMPORAL_ONLY, Features, Harness, doc,
)

# One query that every feature has something to say about: it is multi-part
# ("status" and "funding"), temporal ("current"), and open-ended enough to be
# worth extra perspectives.
QUERY = "what is the current status and funding of the rollout programme?"
REWRITE = "what is the current status and funding of the rollout programme"

_STALE = doc("stale", relevance=0.80, bundle="news",
             start="2016-01-01", end="2016-02-01")
_VALID = doc("valid", relevance=0.80, bundle="ongoing_projects", start="2016-01-01")
_VIA_PERSPECTIVE = doc("via-perspective", relevance=0.78)
_VIA_PART = doc("via-part", relevance=0.78)


def _run(monkeypatch, features: Features):
    return (
        Harness(monkeypatch, features)
        .understanding(rewrite=REWRITE)
        .requirements("current status", "funding received")
        .generated_perspectives("rollout programme delays and blockers")
        .corpus(
            [_STALE, _VALID],
            per_perspective={
                "rollout programme delays and blockers": [_VIA_PERSPECTIVE]
            },
            per_subquery={"current status": [_VIA_PART]},
        )
        .run(QUERY)
    )


# --------------------------------------------------------------------------- #
# The matrix
# --------------------------------------------------------------------------- #

def test_the_matrix_covers_every_flag_independently():
    """Each flag is on in at least one row and off in at least one row, and the
    full-stack row has them all on. A row added without this staying true would
    make the table decorative."""
    for flag in FLAGS:
        values = {row.as_settings()[flag] for row in MATRIX}
        assert values == {True, False}, flag
    assert all(FULL_STACK.as_settings().values())
    assert not any(BASELINE.as_settings().values())


def test_row_1_baseline_is_the_single_query_path(monkeypatch):
    record = _run(monkeypatch, BASELINE)
    assert not record.clarified
    assert record.temporal_mode == tg.CURRENT, "classified, but not acted on"
    assert record.subqueries == []
    assert record.perspectives == []
    assert set(record.evidence_ids) == {"stale", "valid"}


def test_row_2_clarification_only(monkeypatch):
    """A clear question is unaffected — clarification changes nothing until a
    question is actually unclear, which is the design."""
    record = _run(monkeypatch, CLARIFIED)
    baseline = _run(monkeypatch, BASELINE)
    assert not record.clarified
    assert record.evidence_ids == baseline.evidence_ids
    assert record.subqueries == [] and record.perspectives == []


def test_row_3_temporal_only(monkeypatch):
    record = _run(monkeypatch, TEMPORAL_ONLY)
    assert record.leads() == "valid", "currently valid evidence now leads"
    assert record.subqueries == [] and record.perspectives == []


def test_row_4_decomposition_only(monkeypatch):
    record = _run(monkeypatch, DECOMPOSED)
    assert record.subqueries == ["current status", "current status funding received"]
    assert "via-part" in record.evidence_ids, "a part reached evidence the base missed"
    assert record.perspectives == []


def test_row_5_perspectives_only(monkeypatch):
    record = _run(monkeypatch, PERSPECTIVE)
    assert record.perspectives == ["rollout programme delays and blockers"]
    assert "via-perspective" in record.evidence_ids
    assert record.subqueries == []


def test_row_6_full_stack_is_the_union_of_the_effects(monkeypatch):
    """Every individual effect is present together, and nothing is lost in the
    combination — the property a stack of independent flags has to have."""
    record = _run(monkeypatch, FULL_STACK)

    assert record.temporal_mode == tg.CURRENT
    assert record.leads() == "valid", "temporal tie-break still applies"
    assert record.subqueries, "decomposition still ran"
    assert record.perspectives, "perspectives still ran"
    assert record.recalled("valid", "stale", "via-part", "via-perspective")
    assert len(record.evidence_ids) == len(set(record.evidence_ids)), "no duplicates"


# --------------------------------------------------------------------------- #
# Independence
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "features, expected",
    [
        (BASELINE, {"subqueries": False, "perspectives": False}),
        (TEMPORAL_ONLY, {"subqueries": False, "perspectives": False}),
        (DECOMPOSED, {"subqueries": True, "perspectives": False}),
        (PERSPECTIVE, {"subqueries": False, "perspectives": True}),
        (FULL_STACK, {"subqueries": True, "perspectives": True}),
    ],
    ids=lambda v: v.label if isinstance(v, Features) else "",
)
def test_one_flag_never_switches_on_another(monkeypatch, features, expected):
    record = _run(monkeypatch, features)
    assert bool(record.subqueries) is expected["subqueries"]
    assert bool(record.perspectives) is expected["perspectives"]


def test_the_full_stack_recalls_at_least_as_much_as_the_baseline(monkeypatch):
    """The headline measurement of this file: strictly more evidence reaches the
    answer, and none of the baseline's evidence is displaced.

    Stated as a superset rather than a count, because "more blocks" is not by
    itself an improvement — losing a passage the baseline had would be a
    regression however many new ones arrived.
    """
    baseline = _run(monkeypatch, BASELINE)
    full = _run(monkeypatch, FULL_STACK)

    assert set(baseline.evidence_ids) <= set(full.evidence_ids)
    assert len(set(full.evidence_ids)) > len(set(baseline.evidence_ids))


def test_every_matrix_row_is_runnable(monkeypatch):
    """A smoke pass over the declared matrix, so a row that stops working is a
    failure here rather than a gap in the report."""
    for features in MATRIX:
        record = _run(monkeypatch, features)
        assert record.features == features.label
        assert record.normalized == REWRITE
