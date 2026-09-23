"""A word in the question is a filter only when the user chose it as one.

Covers `content_scope` and the two processor guards built on it:

* `_widen_content_question` — a question that asks for documents by what they
  *say* spans every content type, whichever bundle the model set, and leaves
  the catalog route for the content one. A question about the catalog itself
  keeps its type word literal.
* `_drop_implicit_tags` — a tag the model extracted is a hard filter only when
  the question names the tag facet ("tagged 'policy'"); otherwise the word is
  matched as content.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.retrieval.understanding import content_scope
from app.retrieval.understanding import query_processor as qp

# --------------------------------------------------------------------------- #
# The predicate.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("question", [
    "list down all the articles where IPCC mentioned",
    "List down all the articles where IPCC is mentioned.",
    "which reports discuss green hydrogen?",
    "news items that refer to COP28",
    "events talking about the AR6 report",
    "papers containing the word adaptation",
    "projects in which the Paris Agreement appears",
    "policy briefs referencing the NDCs",
    "press releases mentioning Dr Pachauri",
])
def test_a_verb_about_the_text_conditions_on_it(question):
    assert content_scope.conditions_on_text(question)


@pytest.mark.parametrize("question", [
    "how many articles were published in 2023?",
    "list all news since March",
    "latest 5 reports under Climate Change",
    "how many completed projects are there?",
    "what themes do you cover?",
    # A topic is not a predicate: the structured layer accounts for it.
    "list the articles about IPCC",
    "reports on climate change adaptation",
    "news regarding COP28",
    # Instruction verbs aimed at the assistant say nothing about the documents.
    "list all 2023 news and cite the sources",
    "",
])
def test_a_catalog_shaped_question_does_not(question):
    assert not content_scope.conditions_on_text(question)


def test_a_blank_or_missing_question_never_raises():
    assert content_scope.conditions_on_text("") is False
    assert content_scope.conditions_on_text(None) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The guard: whichever type the model set, a content question spans them all.
# --------------------------------------------------------------------------- #

def _analysis(**kw) -> qp.QueryAnalysis:
    kw.setdefault("search_query", "q")
    return qp.QueryAnalysis(**kw)


@pytest.mark.parametrize("bundle", [
    "article", "news", "report", "events", "research_papers",
    "completed_projects", "policy_brief", "press_release", "feature_articles",
])
def test_every_bundle_becomes_descriptive_on_a_content_question(bundle):
    a = _analysis(intent="structured", operation="list", bundle=bundle)
    qp._widen_content_question(f"list the {bundle} where IPCC is mentioned", a)
    assert a.bundle is None
    assert a.intent == "qa"


@pytest.mark.parametrize("bundle", ["article", "news", "report", "events"])
def test_a_catalog_question_keeps_its_type_word(bundle):
    a = _analysis(intent="structured", operation="count", bundle=bundle)
    qp._widen_content_question(f"how many {bundle} were published in 2023?", a)
    assert a.bundle == bundle
    assert a.intent == "structured"


def test_a_topic_beside_a_type_word_is_still_literal():
    """A type word next to a topic ("news about COP28") is often meant
    literally; only a verb about the text widens. The topic itself is the
    structured layer's business."""
    a = _analysis(intent="structured", operation="list", bundle="news")
    qp._widen_content_question("list the news about COP28", a)
    assert a.bundle == "news"
    assert a.intent == "structured"


def test_a_counting_question_on_the_text_leaves_the_catalog():
    """The catalog cannot count mentions; a number it produced would be about
    something else (tagged rows, title matches) and read as the answer."""
    a = _analysis(intent="structured", operation="count", bundle="article")
    qp._widen_content_question("how many articles mention IPCC?", a)
    assert a.bundle is None
    assert a.intent == "qa"


def test_a_content_question_with_no_type_word_still_leaves_the_catalog():
    a = _analysis(intent="structured", operation="list", bundle=None,
                  title_contains="IPCC")
    qp._widen_content_question("list all the data where IPCC is mentioned", a)
    assert a.intent == "qa"
    assert a.title_contains == "IPCC"  # other facets are untouched


@pytest.mark.parametrize("intent", ["qa", "scoped_summary"])
def test_the_bundle_is_cleared_on_every_route(intent):
    """The qa path's catalog fallback and the scoped summary plan from the same
    slots, so a type filter is as wrong for them as for the catalog answer."""
    a = _analysis(intent=intent, bundle="report")
    qp._widen_content_question("summarize the reports that discuss hydrogen", a)
    assert a.bundle is None
    assert a.intent == intent


@pytest.mark.parametrize("operation", ["list_themes", "distribution"])
def test_facet_operations_are_not_disturbed(operation):
    """A theme listing ("what topics are discussed?") is about the vocabulary,
    not about documents; a distribution is about the facets."""
    a = _analysis(intent="structured", operation=operation, bundle="article")
    qp._widen_content_question("what topics are discussed in the articles?", a)
    assert a.bundle == "article"
    assert a.intent == "structured"


def test_other_facets_survive_widening():
    a = _analysis(intent="structured", operation="list", bundle="news",
                  theme="Climate Change", author="Dr Ajay Mathur",
                  date_from="2023-01-01")
    qp._widen_content_question("news by Ajay Mathur mentioning IPCC since 2023", a)
    assert a.bundle is None
    assert (a.theme, a.author, a.date_from) == (
        "Climate Change", "Dr Ajay Mathur", "2023-01-01"
    )


# --------------------------------------------------------------------------- #
# Wiring: process() applies the guard after the legacy derivation.
# --------------------------------------------------------------------------- #

class _One:
    def __init__(self, obj):
        self._obj = obj

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        return self._obj


def _understanding(**kw) -> qp.QueryUnderstanding:
    kw.setdefault("query_rewrite", "List down all the articles where IPCC is mentioned.")
    kw.setdefault("intents", [qp.IntentPrediction(
        label="database", confidence=0.78,
        rationale="list items from the catalog (articles) matching a keyword",
    )])
    return qp.QueryUnderstanding(**kw)


def test_process_widens_the_traced_question(monkeypatch):
    """The live trace of 2026-09-18: database 0.78, bundle=article, list."""
    u = _understanding(operation="list", bundle="article")
    monkeypatch.setattr(qp, "get_structured_llm", lambda: _One(u))
    monkeypatch.setattr(qp, "get_settings", lambda: SimpleNamespace(analysis_votes=1))

    pq = qp.process("list down all the articles where IPCC mentioned")

    assert pq.intent == "qa"
    assert pq.analysis is not None and pq.analysis.bundle is None


def test_process_leaves_a_catalog_question_alone(monkeypatch):
    u = _understanding(query_rewrite="how many articles were published in 2023?",
                       operation="count", bundle="article")
    monkeypatch.setattr(qp, "get_structured_llm", lambda: _One(u))
    monkeypatch.setattr(qp, "get_settings", lambda: SimpleNamespace(analysis_votes=1))

    pq = qp.process("how many articles were published in 2023?")

    assert pq.intent == "structured"
    assert pq.analysis is not None and pq.analysis.bundle == "article"


# --------------------------------------------------------------------------- #
# Tags: a filter only when the user asked for tagged content.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("question", [
    "how many posts are tagged 'policy'?",
    "list the news with the tag COP28",
    "documents tagged IPCC",
    "which tags does the Climate theme carry?",
    "show everything tagging Solid waste",
])
def test_naming_the_tag_facet(question):
    assert content_scope.names_tag_facet(question)


@pytest.mark.parametrize("question", [
    "list down all the articles where IPCC mentioned",
    "list the articles about IPCC",
    "where the keyword IPCC appears",   # a content word, not the facet
    "news labelled climate",            # not this facet's name
    "",
])
def test_a_subject_word_does_not_name_the_tag_facet(question):
    assert not content_scope.names_tag_facet(question)


def test_an_implicit_tag_is_dropped():
    """The live trace: tags=[IPCC] from "where IPCC mentioned" kept 2 documents."""
    a = _analysis(intent="qa", tags=["IPCC"])
    qp._drop_implicit_tags("list down all the articles where IPCC mentioned", a)
    assert a.tags == []


@pytest.mark.parametrize("tags", [["policy"], ["CoP28", "Climate change"]])
def test_an_explicit_tag_is_kept(tags):
    a = _analysis(intent="structured", operation="count", tags=tags)
    qp._drop_implicit_tags("how many posts are tagged with these?", a)
    assert a.tags == tags


def test_no_tags_is_a_no_op():
    a = _analysis(intent="qa")
    qp._drop_implicit_tags("where IPCC is mentioned", a)
    assert a.tags == []


def _tag_conditions(pq):
    return [c for c in pq.filters if getattr(c, "key", None) == "tags"]


def test_process_applies_no_tag_filter_for_the_traced_question(monkeypatch):
    """Both slot values from the 2026-09-18 trace: bundle=article, tags=[IPCC]."""
    u = _understanding(operation="list", bundle="article",
                       scope=qp.QueryScope(tags=["IPCC"]))
    monkeypatch.setattr(qp, "get_structured_llm", lambda: _One(u))
    monkeypatch.setattr(qp, "get_settings", lambda: SimpleNamespace(analysis_votes=1))

    pq = qp.process("list down all the articles where IPCC mentioned")

    assert pq.intent == "qa"
    assert pq.analysis is not None and pq.analysis.tags == []
    assert _tag_conditions(pq) == []


def test_process_keeps_the_tag_filter_the_user_asked_for(monkeypatch):
    u = _understanding(query_rewrite="how many posts are tagged 'policy'?",
                       operation="count", scope=qp.QueryScope(tags=["policy"]))
    monkeypatch.setattr(qp, "get_structured_llm", lambda: _One(u))
    monkeypatch.setattr(qp, "get_settings", lambda: SimpleNamespace(analysis_votes=1))

    pq = qp.process("how many posts are tagged 'policy'?")

    assert pq.analysis is not None and pq.analysis.tags == ["policy"]
    assert len(_tag_conditions(pq)) == 1
