"""The six web-retrieval benchmarks, deterministically.

For each question in :mod:`tests.evaluation.web_benchmark`: the fixture is
complete; the planner raises the signals the question calls for; and, given the
internal evidence retrieval actually returned (a recorded snapshot), the web is
consulted exactly when the benchmark says it should be, for the reason it says.
The facts each answer must state are checked against the evidence the answer
would be built from, so the fixture cannot demand a fact no source holds.

No model, no network. :mod:`tests.evaluation.test_web_benchmark_live` runs the
same six end to end against the configured deployment.
"""
from __future__ import annotations

import re

import pytest

from app.config import get_settings
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.web import planner
from app.retrieval.web.planner import plan
from app.retrieval.web.sufficiency import assess, decide
from tests.evaluation.web_benchmark import BENCHMARKS, CORPUS, WEB, Benchmark

IDS = [b.id for b in BENCHMARKS]


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    s = get_settings()
    for name, value in {
        "web_primary_domains": "teriin.org", "web_allow_third_party": True,
        "web_blocked_domains": "", "retrieval_top_k": 6,
        "web_organisation_names": "TERI, The Energy and Resources Institute",
        "web_subject_min_coverage": 0.5, "web_passage_min_coverage": 0.8,
        "web_min_internal_relevance": 0.0, "web_on_freshness": True,
        "web_fallback_enabled": True,
    }.items():
        monkeypatch.setattr(s, name, value)
    from datetime import date

    monkeypatch.setattr(planner, "today_utc", lambda: date(2026, 9, 23))
    return s


def _candidates(benchmark: Benchmark) -> list[Candidate]:
    out = []
    for i, e in enumerate(benchmark.evidence):
        payload = {"chunk_text": e.text, "title": e.title, "bundle": e.bundle,
                   "source_type": e.source_type}
        if e.date:
            payload["effective_start_date"] = f"{e.date}T00:00:00"
        out.append(Candidate(id=f"{benchmark.id}-{i}", score=0.8, semantic_score=0.8,
                             payload=payload))
    return out


@pytest.mark.parametrize("benchmark", BENCHMARKS, ids=IDS)
def test_every_benchmark_records_what_the_evaluation_needs(benchmark):
    assert benchmark.question and benchmark.sources and benchmark.facts
    assert benchmark.date_handling
    assert isinstance(benchmark.web_should_trigger, bool)
    assert benchmark.web_should_trigger == (benchmark.web_reason is not None)
    assert all(s.held_by in (CORPUS, WEB) and s.url.startswith("https://")
               and s.verified for s in benchmark.sources)
    assert benchmark.evidence


@pytest.mark.parametrize("benchmark", BENCHMARKS, ids=IDS)
def test_the_planner_reads_each_question_as_the_benchmark_expects(benchmark):
    p = plan(benchmark.question)
    for name, expected in benchmark.signals.items():
        assert getattr(p, name) == expected, f"{benchmark.id}: {name}"
    assert p.multi_document == benchmark.multi_document


@pytest.mark.parametrize("benchmark", BENCHMARKS, ids=IDS)
def test_the_web_is_consulted_exactly_when_the_benchmark_says(benchmark):
    p = plan(benchmark.question)
    decision = decide(p, assess(p, _candidates(benchmark)))
    assert decision.search == benchmark.web_should_trigger, decision.to_trace()
    if benchmark.web_reason:
        assert benchmark.web_reason in decision.reasons


@pytest.mark.parametrize("benchmark", BENCHMARKS, ids=IDS)
def test_every_required_fact_is_held_by_the_evidence_the_answer_rests_on(benchmark):
    available = " ".join([e.title + " " + e.text for e in benchmark.evidence]
                         + [benchmark.web_evidence])
    missing = [fact for fact in benchmark.facts
               if not re.search(fact, available, re.IGNORECASE)
               and not _about_dates(fact, benchmark)]
    assert missing == [], f"{benchmark.id}: no recorded evidence states {missing}"


def _about_dates(fact: str, benchmark: Benchmark) -> bool:
    """A date fact is held by a source's recorded date rather than its text."""
    dates = " ".join([s.published or "" for s in benchmark.sources]
                     + [e.date or "" for e in benchmark.evidence])
    return bool(re.search(fact, dates, re.IGNORECASE))


def test_only_the_question_the_corpus_cannot_answer_needs_a_fetched_page():
    fetched = {b.id for b in BENCHMARKS if any(s.held_by == WEB for s in b.sources)}
    assert fetched == {"T1"}


def test_the_seasonal_figures_are_four_separate_facts():
    t2 = next(b for b in BENCHMARKS if b.id == "T2")
    # One fact per season-and-pollutant pair: an answer saying only "28%" fails three.
    assert len(t2.facts) == 4
    assert not re.search(t2.facts[2], "vehicles contribute 28% of PM2.5", re.IGNORECASE)
