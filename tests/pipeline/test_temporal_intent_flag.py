"""Phase B: the flag seam, and that the intent survives normalization.

`temporal_intent_enabled` is read in exactly one place — `_temporal_intent` —
so this file pins that place: OFF hands retrieval `None`, which is what makes
ranking and the pre-existing UPCOMING gate byte-identical to before Phase B.
"""

from __future__ import annotations

import pytest

from app.pipeline import query_pipeline as pipe
from app.retrieval.search import temporal_gate as tg
from app.retrieval.understanding import query_processor as qp


def _pq(mode=tg.CURRENT, **kw):
    kw.setdefault("original", "what is the current status")
    kw.setdefault("search_query", "what is the current status")
    return qp.ProcessedQuery(temporal_intent=tg.TemporalIntent(mode=mode), **kw)


def _flag(monkeypatch, on):
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "temporal_intent_enabled", on, raising=False
    )


def test_the_flag_defaults_off():
    from app.config import Settings

    assert Settings.model_fields["temporal_intent_enabled"].default is False


def test_flag_off_hands_retrieval_nothing(monkeypatch):
    _flag(monkeypatch, False)
    assert pipe._temporal_intent(_pq()) is None


def test_flag_on_hands_retrieval_the_intent(monkeypatch):
    _flag(monkeypatch, True)
    intent = pipe._temporal_intent(_pq())
    assert intent is not None and intent.mode == tg.CURRENT


def test_a_processed_query_always_carries_a_temporal_intent():
    """Defaulted, so every existing construction site stays valid and nothing
    downstream has to test for absence."""
    pq = qp.ProcessedQuery(original="q", search_query="q")
    assert pq.temporal_intent.mode == tg.NONE
    assert not pq.temporal_intent.ranks


# --------------------------------------------------------------------------- #
# Promotion through `process`
# --------------------------------------------------------------------------- #

@pytest.fixture
def _understanding(monkeypatch):
    holder: dict = {}

    class _Model:
        def with_structured_output(self, _schema):
            return self

        def invoke(self, _messages):
            return holder["result"]

    monkeypatch.setattr(qp, "get_structured_llm", lambda: _Model())
    monkeypatch.setattr(qp, "_facet_filters", lambda analysis: [])
    monkeypatch.setattr(qp, "_edition_conditions", lambda question: [])
    return holder


def test_process_promotes_the_mode_and_the_window(_understanding):
    """The window is understanding's own extraction, not a second parse."""
    _understanding["result"] = qp.QueryUnderstanding(
        query_rewrite="what changed between 2023 and 2024",
        intents=[qp.IntentPrediction(label="qa", confidence=0.9)],
        scope=qp.QueryScope(date_from="2023-01-01", date_to_inclusive="2023-12-31"),
    )
    pq = qp.process("what changed between 2023 and 2024")
    assert pq.temporal_intent.mode == tg.DATE_RANGE
    assert pq.temporal_intent.date_from == "2023-01-01"
    # Exclusive upper bound, as everywhere else on the read path.
    assert pq.temporal_intent.date_to == "2024-01-01"


def test_the_promoted_mode_matches_what_the_gate_would_detect(_understanding):
    """The property that makes UPCOMING provably unchanged: the mode is detected
    on `analysis.search_query`, which is the exact string `retrieve` receives and
    `_gate_temporal` has always detected on itself."""
    _understanding["result"] = qp.QueryUnderstanding(
        query_rewrite="are there any upcoming training programmes",
        intents=[qp.IntentPrediction(label="qa", confidence=0.9)],
    )
    pq = qp.process("any upcoming ones?")
    assert pq.temporal_intent.mode == tg.detect_mode(pq.search_query) == tg.UPCOMING


def test_a_question_with_no_temporal_wording_promotes_none(_understanding):
    _understanding["result"] = qp.QueryUnderstanding(
        query_rewrite="what does TERI do about air quality",
        intents=[qp.IntentPrediction(label="qa", confidence=0.9)],
    )
    assert qp.process("what does TERI do about air quality").temporal_intent.mode == tg.NONE


# --------------------------------------------------------------------------- #
# The gate seam
# --------------------------------------------------------------------------- #

def _event(start, end=None):
    from app.core.models.context import ContextBlock

    payload = {"bundle": "events", "effective_start_date": start}
    if end:
        payload["effective_end_date"] = end
    return ContextBlock(n=1, text="t", payload=payload)


def test_past_gating_is_unreachable_without_an_intent():
    """With `temporal=None` the gate behaves exactly as it did before Phase B:
    only UPCOMING selects a gate, whatever the wording says."""
    from app.retrieval import retriever

    blocks = [_event("2030-01-01"), _event("2020-01-01", "2020-01-02")]
    assert retriever._gate_temporal("past workshops we have run", list(blocks)) == blocks


def test_past_gating_engages_with_an_intent():
    from app.retrieval import retriever

    blocks = [_event("2030-01-01"), _event("2020-01-01", "2020-01-02")]
    gated = retriever._gate_temporal(
        "past workshops we have run", list(blocks),
        temporal=tg.TemporalIntent(mode=tg.PAST),
    )
    assert [b.payload["effective_start_date"] for b in gated] == ["2020-01-01"]
