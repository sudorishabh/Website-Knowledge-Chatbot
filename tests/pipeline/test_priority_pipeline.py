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
from app.retrieval.priority.listings import GroupListing
from app.retrieval.priority.match import GROUP, NAME, PERSON, THEME_FACET, Target
from app.retrieval.priority.people import Person
from app.retrieval.priority.registry import CENTRE, PAGE, THEME, PriorityGroup, PriorityPage
from app.retrieval.priority.themes import ThematicArea, ThemeListing
from app.retrieval.understanding import query_processor as qp
from app.retrieval.understanding.clarify import Clarification

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
                          listing=[], replies=[])
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

    def fake_targets(question, *, theme=None, themes_listing=False, reply=None, registry=None):
        log.targets.append((question, theme))
        log.listing.append(themes_listing)
        log.replies.append(reply)
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


COE_LISTING = GroupListing(
    group="Centres of Excellence", names=("CoEET", "Mahindra-TERI Centre of Excellence"),
    url="https://teriin.org/centre-of-excellence", title="Centres of Excellence",
)


@pytest.fixture
def coe_page(monkeypatch):
    """The Centres of Excellence listing page, stubbed; records each group read."""
    calls = SimpleNamespace(read=[], listing=COE_LISTING)

    def fake_group_listing(group, registry=None):
        calls.read.append(group.name)
        return calls.listing

    monkeypatch.setattr(ev, "group_listing", fake_group_listing)
    return calls


@pytest.mark.parametrize("question", [
    "list all centres of excellence",
    "tell me about all the Centres of Excellence",
    "What are TERI's centres of excellence?",
])
def test_the_centres_of_excellence_are_listed_from_their_live_page_alone(
    wired, monkeypatch, coe_page, question,
):
    """Measured 2026-10-22: these were answered from the group's block and
    corpus passages on a 2009 Urban Development centre, the ICC-TERI CSR centre
    and 2016 Mahindra news, citing four to eight sources."""
    _enable(monkeypatch)
    wired.state.pq = _pq(intent="structured")
    result, gen = pipe._prepare(question, history=None, top_k=None)
    assert gen is None
    assert result["answer"].startswith("TERI has 2 Centres of Excellence:\n\n1. CoEET\n")
    assert [(c["title"], c["url"]) for c in result["citations"]] == [
        ("Centres of Excellence", "https://teriin.org/centre-of-excellence")]
    assert coe_page.read == ["Centres of Excellence"]
    assert wired.log.retrieve == [] and wired.log.structured == [] and wired.log.gather == []


@pytest.mark.parametrize("pq", [
    _pq(intent="chitchat"),
    _pq(clarification=Clarification(question="Could you say which topic?")),
])
def test_a_list_taken_for_small_talk_or_unclear_is_still_listed(wired, monkeypatch, coe_page, pq):
    _enable(monkeypatch)
    wired.state.pq = pq
    result, gen = pipe._prepare("centre of excellance", history=None, top_k=None)
    assert gen is None and result["citations"][0]["url"] == COE_LISTING.url


@pytest.mark.parametrize("question", [
    "tell me about the Mahindra-TERI Centre of Excellence",
    "Which centre of excellence works on water reuse?",
    "regional centres and centres of excellence",
])
def test_one_centre_or_more_than_the_list_is_left_to_the_pages(wired, monkeypatch, coe_page,
                                                              question):
    _enable(monkeypatch)
    _, gen = pipe._prepare(question, history=None, top_k=None)
    assert coe_page.read == []
    assert gen is not None and wired.log.retrieve


def test_an_unreadable_listing_page_leaves_the_list_to_the_pages(wired, monkeypatch, coe_page):
    _enable(monkeypatch)
    coe_page.listing = None
    _, gen = pipe._prepare("list all centres of excellence", history=None, top_k=None)
    assert coe_page.read == ["Centres of Excellence"]
    assert gen is not None and wired.log.retrieve


def test_the_listing_page_is_not_read_with_the_feature_off(wired, coe_page):
    pipe._prepare("list all centres of excellence", history=None, top_k=None)
    assert coe_page.read == []


@pytest.mark.parametrize("rewrite, listed", [
    ("What are TERI's centres of excellence?", True),
    ("Which of TERI's centres of excellence work on water reuse?", False),
])
def test_a_follow_up_is_listed_only_when_its_rewrite_asks_for_the_list(
    wired, monkeypatch, coe_page, rewrite, listed,
):
    _enable(monkeypatch)
    wired.state.pq = _pq(search_query=rewrite)
    result, gen = pipe._prepare("and its centres of excellence?",
                                history=[{"role": "user", "content": "x"}], top_k=None)
    assert (gen is None) is listed
    assert coe_page.read == (["Centres of Excellence"] if listed else [])


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


HOME_THEMES = ThemeListing(
    areas=(ThematicArea("Energy", "Covers energy systems.", "https://teriin.org/energy"),
           ThematicArea("Climate Change", "Covers climate science.", "https://teriin.org/climate")),
    url=HOME.url, title="TERI home",
)


@pytest.fixture
def digest(monkeypatch):
    """Catalog detail on, the home page's themes and the theme figures stubbed.
    Records each digest as (scope, format, names) and each overview as the
    names it was given."""
    monkeypatch.setattr(get_settings(), "catalog_answer_detail_enabled", True)
    calls = SimpleNamespace(digest=[], overview=[], listing=HOME_THEMES)

    def fake_digest(*, scope, output_format, names=None):
        calls.digest.append((scope, output_format, names))
        return "What each theme holds:\n- Energy — 1154 items"

    def fake_overview(areas, *, output_format):
        calls.overview.append([a.name for a in areas])
        return "There are **2 thematic areas**: ..."

    monkeypatch.setattr("app.retrieval.structured.tools.theme_digest", fake_digest)
    monkeypatch.setattr("app.retrieval.structured.tools.theme_overview", fake_overview)
    monkeypatch.setattr(ev, "thematic_areas", lambda registry=None: calls.listing)
    return calls


def _home_listing(wired, pq):
    wired.state.pq = pq
    wired.state.targets = [Target("Home", PAGE, NAME, url=HOME.url, page=HOME)]


def test_a_plain_theme_listing_is_answered_theme_by_theme(wired, monkeypatch, digest):
    """Written by the model from the page, one of seven themes was described."""
    _enable(monkeypatch)
    _home_listing(wired, _listing_pq("list_themes"))
    result, gen = pipe._prepare("tell me about all the thematic areas", history=None,
                                top_k=None)
    assert gen is None and result["answer"] == "There are **2 thematic areas**: ..."
    assert digest.overview == [["Energy", "Climate Change"]]
    assert [(c["title"], c["url"]) for c in result["citations"]] == [("TERI home", HOME.url)]
    assert wired.log.retrieve == [] and wired.log.structured == []


def test_an_unreadable_home_page_leaves_the_listing_to_the_page(wired, monkeypatch, digest):
    _enable(monkeypatch)
    digest.listing = None
    _home_listing(wired, _listing_pq("list_themes"))
    _, gen = pipe._prepare("tell me about all the thematic areas", history=None, top_k=None)
    assert gen is not None and wired.log.retrieve
    assert digest.overview == [] and gen.db_suffix == ""


def test_the_other_themes_are_not_the_home_pages(wired, monkeypatch, digest):
    _enable(monkeypatch)
    _home_listing(wired, _listing_pq("list_themes"))
    _, gen = pipe._prepare("what are the other themes", history=None, top_k=None)
    assert digest.overview == []
    assert digest.digest == [("other", "default", None)]


def test_a_theme_listing_asking_for_more_is_followed_by_each_themes_figures(
    wired, monkeypatch, digest,
):
    """The page says what each theme is about, not what it holds — and the
    figures are for the themes it lists, not the catalog's other main themes."""
    _enable(monkeypatch)
    _home_listing(wired, _combined_pq("list_themes"))
    _, gen = pipe._prepare("what are the thematic areas and their key projects",
                           history=None, top_k=None)
    assert digest.overview == []
    assert gen.db_suffix == "What each theme holds:\n- Energy — 1154 items"
    assert gen.db_prefix == ""
    # Every theme the page lists, whichever group the catalog files it under.
    assert digest.digest == [("all", "default", ["Energy", "Climate Change"])]
    assert gen.compose("The themes are ...") == (
        "The themes are ...\n\nWhat each theme holds:\n- Energy — 1154 items")


def test_no_figures_follow_when_the_home_page_cannot_be_read(wired, monkeypatch, digest):
    _enable(monkeypatch)
    digest.listing = None
    _home_listing(wired, _combined_pq("list_themes"))
    _, gen = pipe._prepare("what are the thematic areas and their key projects",
                           history=None, top_k=None)
    assert gen.db_suffix == "" and digest.digest == []


def test_no_figures_follow_with_catalog_detail_off(wired, monkeypatch, digest):
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "catalog_answer_detail_enabled", False)
    _home_listing(wired, _listing_pq("list_themes"))
    _, gen = pipe._prepare("what themes do you cover", history=None, top_k=None)
    assert gen.db_suffix == "" and digest.digest == [] and digest.overview == []


def test_a_listing_naming_one_theme_gets_no_figures(wired, monkeypatch, digest):
    _enable(monkeypatch)
    wired.state.pq = _listing_pq("list_themes")
    wired.state.targets = [Target("Climate Change Theme", THEME, NAME,
                                  url="https://teriin.org/climate")]
    _, gen = pipe._prepare("Tell me about climate change thematic", history=None, top_k=None)
    assert gen.db_suffix == "" and digest.digest == [] and digest.overview == []


def test_an_ordinary_answer_gets_no_figures(wired, monkeypatch, digest):
    _enable(monkeypatch)
    _, gen = pipe._prepare("tell me about climate change", history=None, top_k=None)
    assert gen.db_suffix == "" and digest.digest == [] and digest.overview == []
    assert gen.compose("Body") == "Body"


@pytest.mark.parametrize("reason", [NAME, THEME_FACET])
def test_a_theme_listing_naming_one_theme_is_answered_from_its_page(wired, monkeypatch, reason):
    _enable(monkeypatch)
    wired.state.pq = _listing_pq("list_themes")
    wired.state.targets = [Target("Climate Change Theme", THEME, reason,
                                  url="https://teriin.org/climate")]
    result, gen = pipe._prepare("Tell me about climate change thematic", history=None, top_k=None)
    assert wired.log.structured == []
    assert gen is not None and wired.log.retrieve


def _passage_pq():
    """A question understanding labelled a passage question, as it labels "TERI's
    research areas" and "what are the divisions of TERI"."""
    pq = _pq(intent="qa", analysis=qp.QueryAnalysis(search_query="q", intent="qa"))
    pq.understanding = qp.QueryUnderstanding(query_rewrite="q", intents=[
        qp.IntentPrediction(label="qa", confidence=0.9, rationale="")])
    return pq


@pytest.mark.parametrize("question", [
    "What are TERI's research areas?",
    "What are the divisions of TERI?",
    "What sectors does TERI work in?",
])
def test_the_themes_asked_for_by_another_name_get_the_overview(wired, monkeypatch, digest,
                                                                question):
    """Measured 2026-10-17: these were answered from a Policy page's topics, a
    2017 annual report's divisions and a list of programmes."""
    _enable(monkeypatch)
    _home_listing(wired, _passage_pq())
    result, gen = pipe._prepare(question, history=None, top_k=None)
    assert gen is None and result["answer"] == "There are **2 thematic areas**: ..."
    assert wired.log.retrieve == [] and wired.log.structured == []


def test_the_themes_asked_for_with_more_are_the_page_and_the_models(wired, monkeypatch,
                                                                    digest):
    _enable(monkeypatch)
    _home_listing(wired, _passage_pq())
    _, gen = pipe._prepare("TERI's research areas and their key projects", history=None,
                           top_k=None)
    assert digest.overview == [] and gen is not None and wired.log.retrieve


def test_the_wording_decides_nothing_with_the_feature_off(wired, monkeypatch, digest):
    wired.state.pq = _passage_pq()
    _, gen = pipe._prepare("What are TERI's research areas?", history=None, top_k=None)
    assert digest.overview == [] and gen is not None


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



# -- a theme named by part of a name several themes share ------------------------

BIOFUEL_THEMES = [
    "Advanced Biofuels & Biochemicals",
    "Pyrolytic Biofuels, Biochar & Green Chemicals",
    "Microbial Biofuels and Biochemicals",
]


def _biofuel_pq(intent="scoped_summary", **kw):
    analysis = qp.QueryAnalysis(search_query="Tell me about the biofuel theme.", intent=intent,
                                theme="biofuel", operation=kw.pop("operation", None))
    return _pq(intent=intent, original="tell me about biofuel theme",
               search_query="Tell me about the biofuel theme.", analysis=analysis, **kw)


@pytest.fixture
def clarifying(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "clarification_enabled", True)


def test_a_theme_several_themes_share_is_asked_about(wired, clarifying, summaries):
    """Measured 2026-10-21: summarised from one catalog document instead."""
    wired.state.pq = _biofuel_pq()
    result, gen = pipe._prepare("tell me about biofuel theme", history=None, top_k=None)
    assert gen is None and result["intent"] == "clarification"
    assert result["clarification"]["options"] == BIOFUEL_THEMES
    assert '"biofuel"' in result["clarification"]["question"]
    assert summaries == [] and wired.log.retrieve == []


def test_a_catalog_question_on_such_a_theme_is_not_asked_about(wired, clarifying):
    wired.state.pq = _biofuel_pq(intent="structured", operation="count")
    result, _ = pipe._prepare("how many projects are in the biofuel theme",
                              history=None, top_k=None)
    assert result["answer"] == "catalog answer"


def test_without_clarification_every_theme_that_fits_is_read(wired, monkeypatch, summaries):
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "clarification_enabled", False)
    wired.state.pq = _biofuel_pq(intent="qa")
    result, gen = pipe._prepare("tell me about biofuel theme", history=None, top_k=None)
    assert result is None and wired.log.retrieve[0]["priority"] is wired.state.evidence


def test_the_reply_chooses_among_the_themes_of_the_question_it_answers(wired, clarifying):
    wired.state.pq = _biofuel_pq(intent="qa", clarified_from="tell me about biofuel theme")
    result, gen = pipe._prepare("2", history=None, top_k=None)
    assert result is None
    assert wired.log.targets[0][0] == "tell me about biofuel theme\nTell me about the biofuel theme."
    assert wired.log.replies == ["2"]



# -- "themes" alone ---------------------------------------------------------------

@pytest.mark.parametrize("unclear", [True, False])
def test_themes_alone_gets_the_overview_however_understanding_read_it(
    wired, monkeypatch, digest, unclear
):
    """Measured 2026-10-21: "themes" was asked "could you say what topic" when
    understanding read it as unclear, and got the overview when it read it as
    a theme listing."""
    from app.retrieval.understanding import clarify

    _enable(monkeypatch)
    asked = clarify.Clarification(question="Could you say what topic?") if unclear else None
    _home_listing(wired, _pq(intent="chitchat", original="themes", search_query="themes",
                             clarification=asked))
    monkeypatch.setattr(pipe, "chitchat", lambda q, h: pytest.fail("answered as small talk"))
    result, gen = pipe._prepare("themes", history=None, top_k=None)
    assert gen is None and result["answer"] == "There are **2 thematic areas**: ..."
    assert wired.log.listing == [True]


def test_a_follow_up_asking_for_something_elses_themes_is_not_the_overview(
    wired, monkeypatch, digest
):
    _enable(monkeypatch)
    wired.state.pq = _pq(original="and its themes?",
                         search_query="What are the themes of WSDS 2026?")
    history = [{"role": "user", "content": "tell me about WSDS 2026"},
               {"role": "assistant", "content": "WSDS 2026 was held in New Delhi."}]
    result, gen = pipe._prepare("and its themes?", history=history, top_k=None)
    assert digest.overview == [] and gen is not None
    assert wired.log.listing == [False]


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
