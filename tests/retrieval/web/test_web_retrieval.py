"""The web leg inside `retriever.retrieve`: when it runs, and what it changes.

Driven through the real `retrieve` — real planning, sufficiency, ranking and
context building — with the corpus's Qdrant pulls and the web's `gather`
scripted. The properties pinned: no plan means no web and no change; a
sufficient corpus is not supplemented, and the trace says so; an insufficient
one is; a forced question starts the web early; an empty corpus is answered
from the web; a web failure leaves the corpus's answer; and with the switch off
the web package is never even loaded.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.retrieval import retriever
from app.retrieval.context import builder as context_builder
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.web import planner, service
from app.retrieval.web.planner import plan
from app.retrieval.web.service import WebOutcome

T1 = "Who is Mr Sayanta Ghosh at TERI, and how many publications does he have?"
T2 = ("According to TERI, how much do vehicular emissions contribute to Delhi's "
      "particulate pollution?")
T5 = "What is the latest TERI work on vehicle-related air pollution in Delhi?"


def _corpus(cid: str, text: str, score: float = 0.6, title: str = "") -> Candidate:
    return Candidate(id=cid, score=score, semantic_score=score, vector=[1.0, 0.0, float(len(cid))],
                     payload={"chunk_id": cid, "document_id": f"doc-{cid}",
                              "source_type": "website", "bundle": "news", "title": title,
                              "chunk_text": text})


def _web(cid: str, text: str, score: float = 0.72) -> Candidate:
    return Candidate(id=cid, score=score, semantic_score=score, vector=[0.0, 1.0, float(len(cid))],
                     payload={"chunk_id": cid, "document_id": "web:1", "source_type": "web",
                              "is_primary_source": True, "chunk_text": text,
                              "context_text": f"Profile\n\n{text}",
                              "url": "https://www.teriin.org/user/15680"})


PROFILE = _web("web:1:0", "Mr Sayanta Ghosh is an Associate Fellow with more than 25 "
                          "publications to his credit.")


@pytest.fixture(autouse=True)
def corpus(monkeypatch):
    """A corpus whose pulls return `corpus.hits`, with every other leg off."""
    s = get_settings()
    for name, value in {
        "prefer_website_enabled": False, "multi_query_enabled": False,
        "keyword_leg_enabled": False, "corrective_loop_enabled": False,
        "graph_routing_enabled": False, "graph_shadow_enabled": False,
        "reranker_provider": "embedding", "rerank_score_threshold": 0.0,
        "retrieval_top_k": 6, "web_primary_domains": "teriin.org",
        "web_allow_third_party": True, "web_on_freshness": True, "web_fallback_enabled": True,
        "web_subject_min_coverage": 0.5, "web_passage_min_coverage": 0.8,
        "web_min_internal_relevance": 0.0,
        "web_organisation_names": "TERI, The Energy and Resources Institute",
    }.items():
        monkeypatch.setattr(s, name, value, raising=False)

    class Corpus:
        hits: list[Candidate] = []

    monkeypatch.setattr(retriever, "search", lambda *a, **k: list(Corpus.hits))
    monkeypatch.setattr(retriever, "title_search", lambda *a, **k: [])
    monkeypatch.setattr(retriever, "embed_query", lambda text: [1.0, 0.0, 0.0])
    monkeypatch.setattr(context_builder, "_fetch_parents", lambda ids: {})
    from datetime import date

    monkeypatch.setattr(planner, "today_utc", lambda: date(2026, 9, 23))
    return Corpus


@pytest.fixture
def web(monkeypatch):
    """A scripted `gather`, recording every call."""
    calls: list = []

    class Web:
        candidates: list[Candidate] = [PROFILE]
        raises = False

    def gather(p, query_vector, **kwargs):
        calls.append(p.question)
        if Web.raises:
            raise RuntimeError("web down")
        return WebOutcome(candidates=list(Web.candidates), provider="fake")

    monkeypatch.setattr(service, "gather", gather)
    Web.calls = calls
    return Web


def _texts(blocks) -> list[str]:
    return [b.text for b in blocks]


def test_without_a_plan_the_web_is_never_consulted(corpus, web):
    corpus.hits = [_corpus("a", "Unrelated text.")]
    retriever.retrieve(T1, n=3)
    assert web.calls == []


def test_sufficient_internal_evidence_is_not_supplemented(corpus, web, monkeypatch):
    corpus.hits = [_corpus("a", "Delhi's vehicular emissions contribute 24% of PM10 in winter.")]
    notes: dict = {}
    monkeypatch.setattr(retriever.retrieval_log, "note", lambda **kw: notes.update(kw))
    blocks = retriever.retrieve(T2, n=3, web=plan(T2))
    assert web.calls == []
    assert all(b.payload["source_type"] == "website" for b in blocks)
    assert notes["web_decision"]["search"] is False
    assert notes["web_decision"]["internal"]["sufficient"] is True


def test_insufficient_internal_evidence_is_supplemented_and_ranked(corpus, web):
    corpus.hits = [_corpus("a", "Bionote: Sayanta Ghosh co-authored a GIS study.", score=0.34,
                           title="Bionotes")]
    blocks = retriever.retrieve(T1, n=3, web=plan(T1))
    assert web.calls == [T1]
    assert blocks[0].payload["source_type"] == "web"          # the profile leads
    assert blocks[0].text.startswith("Profile\n\n")            # with its section
    assert any(b.payload["source_type"] == "website" for b in blocks)


def test_a_forced_question_starts_the_web_before_ranking(corpus, web, monkeypatch):
    corpus.hits = [_corpus("a", "Delhi truck emissions study.")]
    order: list[str] = []
    real_rerank = retriever.rerank

    def rerank(*a, **k):
        order.append("rerank")
        return real_rerank(*a, **k)

    def gather(p, query_vector, **kwargs):
        order.append("gather")
        return WebOutcome(candidates=[PROFILE], provider="fake")

    monkeypatch.setattr(retriever, "rerank", rerank)
    monkeypatch.setattr(service, "gather", gather)
    retriever.retrieve(T5, n=3, web=plan(T5))
    assert order.index("gather") < order.index("rerank")
    assert order.count("gather") == 1


def test_an_empty_corpus_is_answered_from_the_web(corpus, web):
    corpus.hits = []
    blocks = retriever.retrieve(T1, n=3, web=plan(T1))
    assert [b.payload["source_type"] for b in blocks] == ["web"]


def test_a_web_failure_leaves_the_corpus_answer(corpus, web):
    corpus.hits = [_corpus("a", "Bionote: Sayanta Ghosh co-authored a GIS study.",
                           title="Bionotes")]
    web.raises = True
    blocks = retriever.retrieve(T1, n=3, web=plan(T1))
    assert _texts(blocks) == ["Bionote: Sayanta Ghosh co-authored a GIS study."]


def test_an_empty_web_outcome_changes_nothing(corpus, web):
    corpus.hits = [_corpus("a", "Bionote: Sayanta Ghosh co-authored a GIS study.",
                           title="Bionotes")]
    web.candidates = []
    blocks = retriever.retrieve(T1, n=3, web=plan(T1))
    assert _texts(blocks) == ["Bionote: Sayanta Ghosh co-authored a GIS study."]


# --- isolation ---------------------------------------------------------------------


def _repo_root():
    import pathlib

    here = pathlib.Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "app").is_dir() and (candidate / "tests").is_dir():
            return candidate
    raise AssertionError(f"could not locate the repository root from {here}")


def test_importing_production_retrieval_does_not_load_the_web_package():
    """The switch is a policy; this is the structural guarantee behind it.

    A subprocess, so the answer cannot depend on what the rest of the suite has
    imported. Every reference to the web package from production code sits inside
    a function reached only with a plan, so importing the read path — retriever,
    pipeline, API — must not load it.
    """
    import os
    import subprocess
    import sys

    program = (
        "import sys;"
        "import app.retrieval.retriever, app.pipeline.query_pipeline, app.api.chat;"
        "leaked=[m for m in sys.modules if m.startswith('app.retrieval.web')];"
        "print(','.join(leaked))"
    )
    env = {**os.environ, "WEB_SEARCH_ENABLED": "false", "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run([sys.executable, "-c", program], capture_output=True,
                               text=True, timeout=300, cwd=str(_repo_root()), env=env)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == "", (
        f"web package loaded by production retrieval: {completed.stdout.strip()}"
    )


def test_only_the_retriever_references_the_web_package_from_production_code():
    """One doorway. Web retrieval is reached through `retrieve` and nowhere else
    on the read path, so the switch and the fail-open wrapper cannot be bypassed."""
    root = _repo_root()
    offenders = []
    for folder in ("retrieval", "pipeline", "generation", "api"):
        for path in (root / "app" / folder).rglob("*.py"):
            if "web" in path.relative_to(root / "app").parts[:2]:
                continue
            if "app.retrieval.web" in path.read_text(encoding="utf-8") and path.name != "retriever.py":
                offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"web retrieval referenced from: {offenders}"
