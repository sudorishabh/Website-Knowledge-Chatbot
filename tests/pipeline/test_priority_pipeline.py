"""The pipeline's two priority-page touch points: triggers before routing, pages
before the answer cache. Retrieval, embedding and the cache are stubbed."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.catalog.models import StateRecord
from app.config import get_settings
from app.core.models.context import ContextBlock
from app.generation.prompts import publications_note
from app.pipeline import query_pipeline as pipe
from app.retrieval.priority import evidence as ev
from app.retrieval.priority.match import GROUP, NAME, PERSON, THEME_FACET, Target
from app.retrieval.priority.people import Person
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


@pytest.mark.parametrize("kind, overrides", [(CENTRE, True), (THEME, False)])
def test_only_the_regional_centres_group_overrules_the_catalog(wired, monkeypatch, kind, overrides):
    _enable(monkeypatch)
    wired.state.pq = _pq(intent="structured")
    group = PriorityGroup(name="g", description="", kind=kind, members=())
    wired.state.targets = [Target("g", "group", GROUP, group=group)]
    result, _ = pipe._prepare("which ones", history=None, top_k=None)
    assert (wired.log.structured == []) is overrides


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


# -- a named person's work -------------------------------------------------------

SURUCHI = Person("Ms Suruchi Bhadwal", "https://teriin.org/profile/suruchi-bhadwal",
                 "People - committee of directors")


@pytest.fixture
def publications(wired, monkeypatch):
    """The catalog's side, stubbed: who was asked for, and what it holds."""
    state = SimpleNamespace(asked=[], found=[_publication()], total=18, error=None)

    def latest(person, *, limit=5):
        state.asked.append(person)
        if state.error:
            raise state.error
        return list(state.found), state.total

    monkeypatch.setattr("app.retrieval.structured.authored.latest_publications", latest)
    _enable(monkeypatch)
    wired.state.pq = _pq(intent="structured")
    wired.state.targets = [Target(SURUCHI.name, "profile", PERSON,
                                  url=SURUCHI.profile_url, person=SURUCHI)]
    return state


def _publication():
    return StateRecord(document_id="d", source_type="website", source_key="d", fingerprint="f",
                       bundle="policy_brief", title="A Transformative Global Goal on Adaptation",
                       url="https://teriin.org/policy-brief/t", effective_start_date="2024-11-12")


def test_a_named_persons_work_lists_their_publications_after_the_answer(wired, publications):
    """Measured 2026-09-28: her profile overruled the catalog, and the answer
    listed none of her 22 publications."""
    _, gen = pipe._prepare("Suruchi Bhadwal work", history=None, top_k=None)
    assert wired.log.structured == []  # the profile still overrules the catalog
    assert publications.asked == ["Ms Suruchi Bhadwal"]
    assert gen.db_suffix.startswith("### Latest publications by Ms Suruchi Bhadwal\n"
                                    "The 1 most recent of 18:\n"
                                    "- [A Transformative Global Goal on Adaptation]")
    assert gen.notes == (publications_note(["Ms Suruchi Bhadwal"]),)
    assert gen.compose("Her work [1].") == f"Her work [1].\n\n{gen.db_suffix}"


def test_who_a_person_is_is_answered_from_the_profile_alone(wired, publications):
    _, gen = pipe._prepare("who is Suruchi Bhadwal", history=None, top_k=None)
    assert publications.asked == [] and gen.db_suffix == "" and gen.notes == ()


@pytest.mark.parametrize("found, error", [([], None), ([_publication()], RuntimeError("db"))])
def test_no_publications_or_no_catalog_costs_only_the_list(wired, publications, found, error):
    publications.found, publications.error = found, error
    _, gen = pipe._prepare("Suruchi Bhadwal work", history=None, top_k=None)
    assert gen is not None and gen.db_suffix == "" and gen.notes == ()


def test_with_nothing_retrieved_the_publications_still_answer(wired, publications, monkeypatch):
    monkeypatch.setattr(pipe, "retrieve", lambda *a, **kw: [])
    result, gen = pipe._prepare("Suruchi Bhadwal work", history=None, top_k=None)
    assert gen is None
    assert result["answer"].startswith("### Latest publications by Ms Suruchi Bhadwal")
