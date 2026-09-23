"""The six web-retrieval benchmarks, end to end against the configured deployment.

Marked ``llm``: it calls the real LLM, embeddings, Qdrant and MySQL, and — for the
questions that need the web — the configured search provider, so the
deterministic suite (``-m "not llm"``) never runs it. Run it with::

    pytest -m llm tests/evaluation/test_web_benchmark_live.py

What it asserts about each delivered answer (the text after any correction):

* every required fact is stated — for T2, all four seasonal figures;
* at least one expected source is cited;
* a question the benchmark says needs the web reached it (a web citation, or a
  corpus source found through web search), and one it says does not, did not.

A benchmark that needs the web is skipped when no search provider is
configured; one that does not is run either way.
"""
from __future__ import annotations

import re

import pytest

from app.config import get_settings
from app.retrieval.web.safety import url_key
from tests.evaluation.web_benchmark import BENCHMARKS

pytestmark = pytest.mark.llm

IDS = [b.id for b in BENCHMARKS]


def _answer_and_citations(question: str) -> tuple[str, list[dict]]:
    from app.pipeline.query_pipeline import stream_answer

    text, citations = "", []
    for event in stream_answer(question):
        if event["type"] == "token":
            text += event["text"]
        elif event["type"] == "correction":
            text = event["text"]                  # a correction replaces the draft
        elif event["type"] == "sources":
            citations = event.get("citations") or []
    return text, citations


def _web_used(citations: list[dict]) -> bool:
    return any(c.get("type") == "web"
               or str(c.get("retrieval_method") or "").startswith("corpus_via_web_search")
               for c in citations)


@pytest.mark.parametrize("benchmark", BENCHMARKS, ids=IDS)
def test_the_delivered_answer_meets_the_benchmark(benchmark, monkeypatch):
    settings = get_settings()
    if not (settings.azure_openai_endpoint and settings.azure_openai_model):
        pytest.skip("no LLM deployment configured")
    has_provider = bool(settings.web_search_provider and settings.web_search_api_key)
    if benchmark.web_should_trigger and not has_provider:
        pytest.skip("needs the web and no search provider is configured")
    monkeypatch.setattr(settings, "web_search_enabled", has_provider, raising=False)
    # A stored answer would measure yesterday's pipeline, not this one.
    monkeypatch.setattr(settings, "semantic_cache_enabled", False, raising=False)

    answer, citations = _answer_and_citations(benchmark.question)

    missing = [f for f in benchmark.facts if not re.search(f, answer, re.IGNORECASE)]
    assert missing == [], f"{benchmark.id} answer lacks {missing}:\n{answer}"

    cited = {url_key(c["url"].split("#")[0]) for c in citations if c.get("url")}
    expected = {url_key(s.url) for s in benchmark.sources}
    assert cited & expected, (
        f"{benchmark.id} cites none of {sorted(expected)}; cited {sorted(cited)}"
    )

    if not has_provider:
        return
    used = _web_used(citations)
    if not benchmark.web_should_trigger:
        assert not used, f"{benchmark.id} used the web though the corpus suffices"
    elif benchmark.web_reason != "freshness":
        # A freshness search is made whatever the corpus holds, and may find only
        # what the corpus already answers with — so only the others must show it.
        assert used, f"{benchmark.id} needed the web and did not use it"
