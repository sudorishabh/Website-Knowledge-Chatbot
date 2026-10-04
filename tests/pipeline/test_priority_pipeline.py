"""The pipeline's two priority-page touch points: triggers before routing, pages
before the answer cache. Retrieval, embedding and the cache are stubbed."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.core.models.context import ContextBlock
from app.pipeline import query_pipeline as pipe
from app.retrieval.priority import evidence as ev
from app.retrieval.priority.match import GROUP, NAME, THEME_FACET, Target
from app.retrieval.priority.registry import CENTRE, PAGE, THEME, PriorityGroup, PriorityPage
from app.retrieval.understanding import query_processor as qp

BLOCK = ContextBlock(n=1, text="corpus text", payload={"source_type": "website", "title": "Doc"})


def _pq(intent="qa", **kw):
    kw.setdefault("original", "q")
    kw.setdefault("search_query", "q")
    kw.setdefault("analysis", qp.QueryAnalysis(search_query="q", intent=intent, theme="Climate Change"))
    return qp.ProcessedQuery(intent=intent, **kw)


class _Evidence:
    def __init__(self, fingerprint=None):
        self.reads: list = []
        self.blocks: list = []
        self._fp = fingerprint or {"priority": ["teriin.org/climate#abc"]}

    def fingerprint(self):
        return dict(self._fp)


@pytest.fixture
def wired(monkeypatch):
    log = SimpleNamespace(retrieve=[], lookup=[], gather=[], structured=[], targets=[],
                          listing=[])
    state = SimpleNamespace(pq=_pq(), targets=[], evidence=_Evidence())

    monkeypatch.setattr(pipe, "process", lambda q, h: state.pq)

    def fake_retrieve(*a, **kw):
        log.retrieve.append(kw)
        return [BLOCK]

    monkeypatch.setattr(pipe, "retrieve", fake_retrieve)
    monkeypatch.setattr("app.core.clients.embeddings.embed_query", lambda q: [0.1])
    # A model call otherwise; `[]` is its own no-op result.
    monkeypatch.setattr("app.generation.answer_plan.extract_requirements", lambda q: [])

    def fake_lookup(*a, **kw):
        log.lookup.append(kw["fingerprint"])
        return None

    monkeypatch.setattr("app.cache.semantic_cache.lookup", fake_lookup)

    def fake_targets(question, *, theme=None, themes_listing=False, registry=None):
        log.targets.append((question, theme))
        log.listing.append(themes_listing)
        return list(state.targets)

    def fake_gather(question, *, query_vector, theme=None, explicit=None, **kw):
        log.gather.append({"question": question, "theme": theme, "explicit": explicit,
                           "vector": query_vector})
        return state.evidence

    monkeypatch.setattr(ev, "explicit_targets", fake_targets)
    monkeypatch.setattr(ev, "gather", fake_gather)

    def fake_structured(question, history, *, analysis):
        log.structured.append(question)
        return {"answer": "catalog answer", "citations": [], "intent": "structured",
                "used_chunks": 0, "conflict": False, "cached": False}

    monkeypatch.setattr("app.retrieval.structured.answerer.answer_structured", fake_structured)
    monkeypatch.setattr("app.retrieval.structured.tools.resolve_lookup_chain", lambda a, q: None)
    monkeypatch.setattr(pipe, "_graph_generation", lambda pq, *, top_k: None)
    return SimpleNamespace(log=log, state=state)


def _enable(monkeypatch):
    monkeypatch.setattr(get_settings(), "priority_pages_enabled", True)


def test_with_the_feature_off_nothing_changes(wired):
    result, gen = pipe._prepare("tell me about climate change", history=None, top_k=None)
    assert "priority" not in wired.log.retrieve[0]
    assert wired.log.targets == [] and wired.log.gather == []
    assert "priority" not in wired.log.lookup[0]
    assert gen.cache_fingerprint is None


def test_pages_are_read_before_the_cache_and_key_it(wired, monkeypatch):
    _enable(monkeypatch)
    result, gen = pipe._prepare("tell me about climate change", history=None, top_k=None)
    assert wired.log.gather[0]["theme"] == "Climate Change"
    assert wired.log.gather[0]["vector"] == [0.1]
    assert wired.log.lookup[0]["priority"] == ["teriin.org/climate#abc"]
    assert wired.log.retrieve[0]["priority"] is wired.state.evidence
    assert gen.cache_fingerprint["priority"] == ["teriin.org/climate#abc"]


def test_the_answer_is_stored_under_the_fingerprint_retrieval_left(wired, monkeypatch):
    _enable(monkeypatch)
    stored = []
    monkeypatch.setattr("app.cache.semantic_cache.store",
                        lambda *a, **kw: stored.append(kw["fingerprint"]))
    _, gen = pipe._prepare("tell me about climate change", history=None, top_k=None)
    wired.state.evidence._fp = {"priority": ["teriin.org/climate#abc", "teriin.org/policy#def"]}
    gen.cache_fingerprint = {**gen.cache_fingerprint, **wired.state.evidence.fingerprint()}
    pipe._persist(gen, {"answer": "a"})
    assert stored[0]["priority"] == ["teriin.org/climate#abc", "teriin.org/policy#def"]


def test_a_named_institutional_page_overrules_the_catalog(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _pq(intent="structured")
    wired.state.targets = [Target("people - governing council", "people", NAME,
                                  url="https://teriin.org/people/governing-council")]
    result, gen = pipe._prepare("how many members are on the governing council",
                                history=None, top_k=None)
    assert wired.log.structured == []
    assert gen is not None and wired.log.retrieve


def test_a_theme_facet_does_not_overrule_the_catalog(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _pq(intent="structured")
    wired.state.targets = [Target("Climate Change", THEME, THEME_FACET,
                                  url="https://teriin.org/climate")]
    result, gen = pipe._prepare("how many climate change projects are there",
                                history=None, top_k=None)
    assert wired.log.structured and result["answer"] == "catalog answer"


def test_a_theme_page_named_outright_does_not_overrule_a_count(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _pq(intent="structured")
    wired.state.targets = [Target("Climate Change", THEME, NAME, url="https://teriin.org/climate")]
    result, _ = pipe._prepare("how many climate change projects", history=None, top_k=None)
    assert result["answer"] == "catalog answer"


def _person_pq(operation):
    analysis = qp.QueryAnalysis(search_query="q", intent="structured",
                                operation=operation, author="Suneel Pandey")
    return _pq(intent="structured", analysis=analysis)


PERSON_TARGET = Target("Dr Suneel Pandey", "profile", "person",
                       url="https://teriin.org/profile/suneel-pandey")


@pytest.mark.parametrize("operation", ["count", "distribution"])
def test_a_persons_page_does_not_overrule_a_count_of_their_publications(
    wired, monkeypatch, operation,
):
    """Measured: the profile page took "how many publications by Suneel Pandey",
    listed none, and the answer was a refusal while the catalog holds 35."""
    _enable(monkeypatch)
    wired.state.pq = _person_pq(operation)
    wired.state.targets = [PERSON_TARGET]
    result, _ = pipe._prepare("how many publications by Suneel Pandey",
                              history=None, top_k=None)
    assert result["answer"] == "catalog answer"


@pytest.mark.parametrize("operation, question", [
    ("list", "list articles by Vibha Dhawan"),
    ("list", "show me the research papers Suneel Pandey wrote"),
    ("lookup", "find Suneel Pandey's policy brief on air quality"),
])
def test_a_persons_page_does_not_overrule_a_list_of_their_writing(
    wired, monkeypatch, operation, question,
):
    """Measured: the profile page took "list articles by Vibha Dhawan", listed
    none, and the answer was a refusal while the catalog holds 41."""
    _enable(monkeypatch)
    wired.state.pq = _person_pq(operation)
    wired.state.targets = [PERSON_TARGET]
    result, _ = pipe._prepare(question, history=None, top_k=None)
    assert result["answer"] == "catalog answer"


def test_a_persons_page_still_owns_a_question_about_them(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _person_pq("list")
    wired.state.targets = [PERSON_TARGET]
    result, gen = pipe._prepare("what does Suneel Pandey work on",
                                history=None, top_k=None)
    assert wired.log.structured == []
    assert gen is not None


def test_the_regional_centres_group_overrules_the_catalog(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _pq(intent="structured")
    group = PriorityGroup(name="Regional centers", description="", kind=CENTRE, members=())
    wired.state.targets = [Target("Regional centers", "group", GROUP, group=group)]
    result, _ = pipe._prepare("which regional centres does TERI have", history=None, top_k=None)
    assert wired.log.structured == []


HOME = PriorityPage(name="Home", url="https://www.teriin.org", description="", kind=PAGE)


def _listing_pq(operation):
    analysis = qp.QueryAnalysis(search_query="q", intent="structured", operation=operation)
    return _pq(intent="structured", analysis=analysis)


def test_a_theme_listing_is_answered_from_the_home_page(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _listing_pq("list_themes")
    wired.state.targets = [Target("Home", PAGE, NAME, url=HOME.url, page=HOME)]
    result, gen = pipe._prepare("Teri thematic areas", history=None, top_k=None)
    assert wired.log.listing == [True]
    assert wired.log.structured == []
    assert gen is not None and wired.log.retrieve


@pytest.mark.parametrize("reason", [NAME, THEME_FACET])
def test_a_theme_listing_naming_one_theme_is_answered_from_its_page(wired, monkeypatch, reason):
    _enable(monkeypatch)
    wired.state.pq = _listing_pq("list_themes")
    wired.state.targets = [Target("Climate Change Theme", THEME, reason,
                                  url="https://teriin.org/climate")]
    result, gen = pipe._prepare("Tell me about climate change thematic", history=None, top_k=None)
    assert wired.log.structured == []
    assert gen is not None and wired.log.retrieve


def test_the_home_page_does_not_overrule_a_count_over_the_themes(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _listing_pq("distribution")
    wired.state.targets = [Target("Home", PAGE, NAME, url=HOME.url, page=HOME)]
    result, _ = pipe._prepare("how many documents are in each thematic area",
                              history=None, top_k=None)
    assert wired.log.listing == [False]
    assert result["answer"] == "catalog answer"


def test_a_question_about_one_edition_skips_priority_pages(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _pq()
    wired.state.pq.edition = SimpleNamespace(edition="2023-24", kind="annual_report")
    pipe._prepare("what does the 2023-24 annual report say", history=None, top_k=None)
    assert wired.log.targets == [] and "priority" not in wired.log.retrieve[0]


def test_the_triggers_read_the_question_and_its_rewrite(wired, monkeypatch):
    _enable(monkeypatch)
    wired.state.pq = _pq(search_query="TERI governing council members")
    pipe._prepare("and who chairs it?", history=[{"role": "user", "content": "x"}], top_k=None)
    question, _ = wired.log.targets[0]
    assert question == "and who chairs it?\nTERI governing council members"


def test_search_blocks_reads_the_same_pages(wired, monkeypatch):
    _enable(monkeypatch)
    pipe.search_blocks("tell me about climate change")
    assert wired.log.retrieve[0]["priority"] is wired.state.evidence
    assert wired.log.retrieve[0]["query_vector"] == [0.1]


CLIMATE = Target("Climate Change Theme", THEME, NAME, url="https://teriin.org/climate")


@pytest.fixture
def summaries(monkeypatch):
    calls = []

    def fake_summary(analysis):
        calls.append(analysis)
        return {"answer": "summary of documents", "citations": [], "intent": "scoped_summary",
                "used_chunks": 3, "conflict": False, "cached": False}

    monkeypatch.setattr("app.pipeline.summarize.summarize_scope", fake_summary)
    return calls


def _summary_pq(**scope):
    analysis = qp.QueryAnalysis(search_query="q", intent="scoped_summary", **scope)
    return _pq(intent="scoped_summary", analysis=analysis)


@pytest.mark.parametrize("question", [
    "Summarize the climate change theme",
    "Give me an overview of TERI's climate change thematic area",
    "Summarize TERI's work on climate change",
])
def test_a_summary_of_one_theme_is_answered_from_its_page(wired, monkeypatch, summaries,
                                                           question):
    _enable(monkeypatch)
    wired.state.pq = _summary_pq(theme="climate change")
    wired.state.targets = [CLIMATE]
    result, gen = pipe._prepare(question, history=None, top_k=None)
    assert summaries == []
    assert result is None and wired.log.retrieve[0]["priority"] is wired.state.evidence


@pytest.mark.parametrize("question, scope", [
    ("Summarize TERI's publications on climate change", {}),
    ("Summarize the policy briefs on climate change", {}),
    ("Summarize climate change work from 2023", {"date_from": "2023-01-01"}),
    ("Summarize Dr Bhadwal's climate change work", {"author": "Suruchi Bhadwal"}),
])
def test_a_summary_of_documents_on_a_theme_stays_a_summary(wired, monkeypatch, summaries,
                                                           question, scope):
    _enable(monkeypatch)
    wired.state.pq = _summary_pq(theme="climate change", **scope)
    wired.state.targets = [CLIMATE]
    result, _ = pipe._prepare(question, history=None, top_k=None)
    assert result["answer"] == "summary of documents"


def test_a_summary_no_theme_page_owns_stays_a_summary(wired, monkeypatch, summaries):
    _enable(monkeypatch)
    wired.state.pq = _summary_pq(theme="Microbes")
    result, _ = pipe._prepare("Summarize the microbes work", history=None, top_k=None)
    assert result["answer"] == "summary of documents"


def _combined_pq(operation):
    pq = _listing_pq(operation)
    pq.understanding = qp.QueryUnderstanding(query_rewrite="q", intents=[
        qp.IntentPrediction(label=label, confidence=0.9, rationale="")
        for label in ("database", "qa")
    ])
    return pq


@pytest.fixture
def db_sections(monkeypatch):
    calls = []

    def fake_db_section(pq, question, history):
        calls.append(question)
        return "The collection covers 7 main themes: ..."

    monkeypatch.setattr(pipe, "_db_section", fake_db_section)
    return calls


def test_a_theme_listing_that_asks_for_content_takes_no_catalog_list(wired, monkeypatch,
                                                                     db_sections):
    _enable(monkeypatch)
    wired.state.pq = _combined_pq("list_themes")
    wired.state.targets = [Target("Home", PAGE, NAME, url=HOME.url, page=HOME)]
    _, gen = pipe._prepare("List TERI's themes and explain each one", history=None, top_k=None)
    assert db_sections == []
    assert gen.db_prefix == ""


def test_other_combined_questions_keep_their_catalog_section(wired, monkeypatch, db_sections):
    _enable(monkeypatch)
    wired.state.pq = _combined_pq("count")
    wired.state.targets = [CLIMATE]
    _, gen = pipe._prepare("How many climate change projects are there and what do they cover?",
                           history=None, top_k=None)
    assert db_sections and gen.db_prefix.startswith("The collection covers")
