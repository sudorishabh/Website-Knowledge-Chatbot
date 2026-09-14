"""Phase A: how a clarification reaches the caller.

The seam under test is `_prepare` — that a clarification short-circuits ahead of
chitchat and ahead of retrieval — plus the SSE shape `_stream_result` emits for
it. `process` is patched at the pipeline's module attribute, so no LLM, no
Qdrant, no MySQL.
"""

from __future__ import annotations

import pytest

from app.pipeline import query_pipeline as pipe
from app.retrieval.understanding import clarify
from app.retrieval.understanding import query_processor as qp


def _pq(clarification=None, intent="chitchat", **kw):
    kw.setdefault("original", "show me performance")
    kw.setdefault("search_query", "show me performance")
    return qp.ProcessedQuery(intent=intent, clarification=clarification, **kw)


def _clarification():
    return clarify.Clarification(
        question="Which of these did you mean?",
        options=["Completed Projects", "Ongoing Projects"],
        kind="content_type",
    )


@pytest.fixture
def _no_side_effects(monkeypatch):
    """Fail loudly if a clarification reaches anything downstream of the guard."""

    def _unreachable(*a, **k):
        raise AssertionError("a clarification must not reach this stage")

    monkeypatch.setattr(pipe, "chitchat", _unreachable)
    monkeypatch.setattr(pipe, "retrieve", _unreachable)


def _prepare(monkeypatch, pq):
    monkeypatch.setattr(pipe, "process", lambda q, h: pq)
    return pipe._prepare("show me performance", history=None, top_k=6)


# --------------------------------------------------------------------------- #
# _clarification_result
# --------------------------------------------------------------------------- #

def test_result_carries_the_rendered_question_as_the_answer():
    result = pipe._clarification_result(_clarification())
    assert clarify.is_clarification_turn(result["answer"])
    assert "1. Completed Projects" in result["answer"]


def test_result_reports_a_clarification_intent():
    """Not the route the classifier took — that is chitchat-or-rescued-qa here,
    and it would describe the wrong thing in the metrics."""
    assert pipe._clarification_result(_clarification())["intent"] == "clarification"


def test_result_exposes_the_options_structurally():
    payload = pipe._clarification_result(_clarification())["clarification"]
    assert payload["options"] == ["Completed Projects", "Ongoing Projects"]
    assert payload["kind"] == "content_type"
    assert payload["question"]


def test_result_grounds_nothing():
    result = pipe._clarification_result(_clarification())
    assert result["citations"] == []
    assert result["used_chunks"] == 0
    assert result["cached"] is False


# --------------------------------------------------------------------------- #
# _prepare: the short-circuit
# --------------------------------------------------------------------------- #

def test_prepare_short_circuits_before_chitchat_and_retrieval(
    monkeypatch, _no_side_effects
):
    result, generation = _prepare(monkeypatch, _pq(_clarification()))
    assert generation is None
    assert result is not None
    assert result["intent"] == "clarification"


def test_prepare_still_chitchats_when_there_is_no_clarification(monkeypatch):
    monkeypatch.setattr(pipe, "chitchat", lambda q, h: "Hello!")
    result, generation = _prepare(monkeypatch, _pq(None, intent="chitchat"))
    assert generation is None
    assert result["intent"] == "chitchat"
    assert result["answer"] == "Hello!"


# --------------------------------------------------------------------------- #
# SSE shape
# --------------------------------------------------------------------------- #

def test_stream_emits_the_question_then_a_sources_event_with_options():
    result = pipe._clarification_result(_clarification())
    events = list(pipe._stream_result(result))

    assert [e["type"] for e in events] == ["token", "sources", "done"]
    assert clarify.is_clarification_turn(events[0]["text"])
    assert events[1]["clarification"]["options"] == [
        "Completed Projects", "Ongoing Projects"
    ]


def test_the_clarification_key_is_absent_on_an_ordinary_answer():
    """Additive: a client that has never heard of clarification sees the event
    it has always seen."""
    events = list(pipe._stream_result(pipe._empty("qa", "TERI was founded in 1974.")))
    sources = next(e for e in events if e["type"] == "sources")
    assert "clarification" not in sources
