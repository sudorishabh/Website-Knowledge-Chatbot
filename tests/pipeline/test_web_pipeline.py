"""The pipeline's side of web retrieval: the flag seam and the cache rules.

`web_search_enabled` is read in exactly one place, `_web_plan`, so this file pins
that place: off hands retrieval None (byte-identical to before web retrieval
existed) and builds nothing; on hands it a plan built from what understanding
extracted. And the two cache rules: a question forcing the web is never answered
from a stored answer, and an answer built on web evidence is never stored.
"""
from __future__ import annotations

import pytest

from app.cache import cache_keys, semantic_cache
from app.config import Settings, get_settings
from app.core.models.context import ContextBlock
from app.pipeline import query_pipeline as pipe
from app.retrieval import retriever
from app.retrieval.understanding import query_processor as qp


def _pq(question: str, **kw) -> qp.ProcessedQuery:
    analysis = kw.pop("analysis", qp.QueryAnalysis(search_query=question))
    labels = kw.pop("labels", [("qa", 0.9)])
    understanding = qp.QueryUnderstanding(
        query_rewrite=question,
        intents=[qp.IntentPrediction(label=lbl, confidence=c) for lbl, c in labels],
    )
    return qp.ProcessedQuery(original=question, search_query=kw.pop("search_query", question),
                             analysis=analysis, understanding=understanding, **kw)


class _Plan:
    """What `_web_plan` needs back from the builder: a plan it can record."""

    forced = False

    def to_trace(self):
        return {"forced_reasons": []}


@pytest.fixture
def flag(monkeypatch):
    settings = get_settings()

    def set_flag(on: bool) -> None:
        monkeypatch.setattr(settings, "web_search_enabled", on, raising=False)

    set_flag(False)
    return set_flag


# --- the flag seam ------------------------------------------------------------


def test_the_flag_defaults_off():
    assert Settings.model_fields["web_search_enabled"].default is False


def test_off_hands_retrieval_nothing_and_builds_nothing(flag, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("no plan is built with the web off")

    monkeypatch.setattr(retriever, "web_plan_for", fail)
    assert pipe._web_plan(_pq("What is the latest TERI report?")) is None


def test_on_builds_the_plan_from_what_understanding_extracted(flag, monkeypatch):
    flag(True)
    seen = {}

    def build(question, search_query, **kwargs):
        seen.update(question=question, search_query=search_query, **kwargs)
        return _Plan()

    monkeypatch.setattr(retriever, "web_plan_for", build)
    pq = _pq("TERI transport studies", labels=[("qa", 0.9), ("comparison", 0.8)],
             answer_format="table",
             analysis=qp.QueryAnalysis(search_query="TERI transport studies",
                                       date_from="2019-01-01", date_to="2026-01-01"))
    pipe._web_plan(pq)
    assert seen == {"question": "TERI transport studies", "search_query": "TERI transport studies",
                    "capabilities": {"qa", "comparison"}, "answer_format": "table",
                    "date_from": "2019-01-01", "date_to": "2026-01-01"}


def test_in_a_conversation_the_standalone_rewrite_is_planned(flag, monkeypatch):
    flag(True)
    seen = {}

    def build(question, search_query, **kwargs):
        seen["q"] = question
        return _Plan()

    monkeypatch.setattr(retriever, "web_plan_for", build)
    pq = _pq("and who is he?", search_query="Who is Dr Raghab Ray at TERI?")
    pipe._web_plan(pq, history=[{"role": "user", "content": "the seaweed article"}])
    assert seen["q"] == "Who is Dr Raghab Ray at TERI?"


def test_the_plan_is_recorded_on_the_trace(flag, monkeypatch):
    flag(True)
    notes = {}
    monkeypatch.setattr(pipe.retrieval_log, "note", lambda **kw: notes.update(kw))
    plan = pipe._web_plan(_pq("What is the latest TERI work on trucks?"))
    assert plan is not None and plan.forced
    assert notes["web_plan"]["forced_reasons"] == ["freshness"]


def test_a_planning_failure_costs_the_web_not_the_answer(flag, monkeypatch):
    flag(True)

    def broken(*a, **k):
        raise RuntimeError("planner bug")

    monkeypatch.setattr(retriever, "web_plan_for", broken)
    assert pipe._web_plan(_pq("anything")) is None


# --- the cache rules --------------------------------------------------------------


@pytest.fixture
def drive(monkeypatch):
    """Run `_prepare` with understanding, embedding, cache and retrieval scripted."""
    state = {"lookups": 0, "retrieve_web": "unset"}

    def run(question: str, *, blocks=None):
        monkeypatch.setattr(pipe, "process", lambda q, h: _pq(question))
        monkeypatch.setattr("app.core.clients.embeddings.embed_query", lambda text: [0.1])

        def lookup(*a, **k):
            state["lookups"] += 1
            return None

        monkeypatch.setattr(semantic_cache, "lookup", lookup)

        def fake_retrieve(*a, web=None, **k):
            state["retrieve_web"] = web
            return list(blocks or [ContextBlock(n=1, text="t", payload={"source_type": "website"})])

        monkeypatch.setattr(pipe, "retrieve", fake_retrieve)
        monkeypatch.setattr("app.generation.answer_plan.extract_requirements", lambda q: [])
        return pipe._prepare(question, history=None, top_k=3)

    return run, state


def test_a_forced_question_is_never_answered_from_the_cache(flag, drive):
    flag(True)
    run, state = drive
    run("What is the latest TERI work on vehicle pollution in Delhi?")
    assert state["lookups"] == 0
    assert state["retrieve_web"].forced


def test_an_ordinary_question_still_uses_the_cache(flag, drive):
    flag(True)
    run, state = drive
    run("What does TERI do on solar energy?")
    assert state["lookups"] == 1
    assert state["retrieve_web"] is not None and not state["retrieve_web"].forced


def test_with_the_web_off_retrieval_gets_no_plan_and_the_cache_is_used(flag, drive):
    run, state = drive
    run("What is the latest TERI work on vehicle pollution in Delhi?")
    assert state["retrieve_web"] is None
    assert state["lookups"] == 1


def _generation(source_type: str) -> pipe._Generation:
    return pipe._Generation(pq=_pq("q"), query_vector=[0.1], top_k=3,
                            blocks=[ContextBlock(n=1, text="t",
                                                 payload={"source_type": source_type})])


def test_an_answer_built_on_web_evidence_is_never_stored(monkeypatch):
    stored = []
    monkeypatch.setattr(semantic_cache, "store", lambda *a, **k: stored.append(1))
    pipe._persist(_generation("web"), {"answer": "a"})
    assert stored == []


def test_a_corpus_answer_is_stored_as_before(monkeypatch):
    stored = []
    monkeypatch.setattr(semantic_cache, "store", lambda *a, **k: stored.append(1))
    pipe._persist(_generation("website"), {"answer": "a"})
    assert stored == [1]


def test_switching_the_web_changes_the_cache_partition(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_search_enabled", False, raising=False)
    off = cache_keys._pref_fingerprint()
    monkeypatch.setattr(settings, "web_search_enabled", True, raising=False)
    on = cache_keys._pref_fingerprint()
    monkeypatch.setattr(settings, "web_search_provider", "tavily", raising=False)
    other_provider = cache_keys._pref_fingerprint()
    assert len({off, on, other_provider}) == 3


# --- /search -------------------------------------------------------------------


def test_search_blocks_hands_retrieval_the_same_plan(flag, monkeypatch):
    flag(True)
    seen = {}
    monkeypatch.setattr(pipe, "process", lambda q, h: _pq(q))

    def fake_retrieve(*a, web=None, **k):
        seen["web"] = web
        return []

    monkeypatch.setattr(pipe, "retrieve", fake_retrieve)
    pipe.search_blocks("Search the web for TERI truck emissions")
    assert seen["web"] is not None and seen["web"].explicit
