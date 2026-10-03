"""Unit tests for the structured (database-intent) answer path and its inputs.

Covers answer_structured's delegation to the Database Planner (analysis vs the
parse fallback, bundle normalization, format passthrough, fall-through), the
query_processor facet filters (semantic-path DatetimeRange / tags), the
generation format directives, and the ProcessedQuery contract. The catalog tools
themselves are covered by test_database_tools; the SQL by app/local_tests. No
MySQL, Qdrant, LLM, or network.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.catalog import queries as state
from app.retrieval.structured import answerer as dr
from app.retrieval.structured import planner
from app.retrieval.structured.types import ToolResult
from app.retrieval.understanding import query_processor as qp


# --------------------------------------------------------------------------- #
# answer_structured — delegation, normalization, fall-through, format.
# --------------------------------------------------------------------------- #

def _forbid_count(**kw):
    raise AssertionError("count_documents must not be called")


def _rec(title="Solar in India", document_id="d1"):
    from app.catalog.models import StateRecord

    return StateRecord(
        document_id=document_id, source_type="website", source_key="k",
        fingerprint="f", title=title, url="http://a",
        effective_start_date="2024-05-01T00:00:00", bundle="news",
    )


# --------------------------------------------------------------------------- #
# catalog_fallback — the catalog's answer when retrieval grounded nothing.
# --------------------------------------------------------------------------- #

def test_catalog_fallback_without_analysis_is_none():
    assert dr.catalog_fallback("q", analysis=None) is None


def test_catalog_fallback_needs_a_subject_facet(monkeypatch):
    """A date or bundle alone would list the most recent documents, which answers
    nothing about a subject — refuse instead of implying relevance."""
    monkeypatch.setattr(
        planner, "execute",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not query")),
    )
    analysis = qp.QueryAnalysis(
        search_query="what happened in 2023?", intent="qa",
        date_from="2023-01-01", date_to="2024-01-01", bundle="news",
    )
    assert dr.catalog_fallback("what happened in 2023?", analysis=analysis) is None


def test_catalog_fallback_lists_the_scope(monkeypatch):
    monkeypatch.setattr(state, "list_documents", lambda **kw: [_rec()])
    analysis = qp.QueryAnalysis(
        search_query="what does the solar report say?", intent="qa",
        title_contains="Solar",
    )
    out = dr.catalog_fallback("what does the solar report say?", analysis=analysis)
    assert "Solar in India" in out["answer"]
    assert out["citations"][0]["title"] == "Solar in India"


def test_catalog_fallback_is_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(state, "list_documents", lambda **kw: [])
    analysis = qp.QueryAnalysis(
        search_query="what does the solar report say?", intent="qa",
        title_contains="Solar",
    )
    assert dr.catalog_fallback("q", analysis=analysis) is None


def test_catalog_fallback_forces_a_listing(monkeypatch):
    """Whatever the classifier's operation, the fallback lists: a count answers
    nothing for a question that wanted content."""
    seen = {}

    def fake_execute(db_plan, *, question=None):
        seen["tool"] = db_plan.calls[0].tool
        return []

    monkeypatch.setattr(planner, "execute", fake_execute)
    analysis = qp.QueryAnalysis(
        search_query="how many solar reports?", intent="qa",
        operation="count", title_contains="Solar",
    )
    assert dr.catalog_fallback("how many solar reports?", analysis=analysis) is None
    assert seen["tool"] == "list_records"


def test_catalog_fallback_never_spends_an_llm_parse(monkeypatch):
    """The facets are already extracted; a parse on the refusal path would buy
    nothing and cost a call."""
    monkeypatch.setattr(
        dr, "parse_structured",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not parse")),
    )
    monkeypatch.setattr(state, "list_documents", lambda **kw: [_rec()])
    analysis = qp.QueryAnalysis(
        search_query="q", intent="qa", title_contains="Solar",
    )
    assert dr.catalog_fallback("q", analysis=analysis) is not None


def test_answer_structured_unknown_bundle_falls_through(monkeypatch):
    monkeypatch.setattr(state, "count_documents", _forbid_count)
    analysis = qp.QueryAnalysis(
        search_query="table of emissions by sector",
        intent="structured",
        operation="count",
        bundle="emission",  # normalizes to no known bundle
    )
    assert dr.answer_structured("emissions by sector?", analysis=analysis) is None


def _enable_entity_resolution(monkeypatch):
    monkeypatch.setattr(
        dr, "get_settings",
        lambda: SimpleNamespace(database_multi_call_enabled=False,
                                entity_resolution_enabled=True),
    )


def test_answer_structured_zero_under_a_guessed_title_falls_through(monkeypatch):
    """End-to-end wiring: the question must reach count_records through the planner,
    so a zero under a title the classifier guessed from a subject phrase falls
    through to semantic search rather than claiming the corpus has nothing."""
    monkeypatch.setattr(state, "count_documents", lambda **kw: 0)
    question = "how many reports about quantum teleportation?"
    analysis = qp.QueryAnalysis(
        search_query=question, intent="structured", operation="count",
        title_contains="quantum teleportation",
    )
    assert dr.answer_structured(question, analysis=analysis) is None


def test_answer_structured_zero_for_a_title_question_is_answered(monkeypatch):
    """The same zero, asked about titles, is the answer — not a fall-through."""
    monkeypatch.setattr(state, "count_documents", lambda **kw: 0)
    question = "how many reports are titled Solar?"
    analysis = qp.QueryAnalysis(
        search_query=question, intent="structured", operation="count",
        title_contains="Solar",
    )
    out = dr.answer_structured(question, analysis=analysis)
    assert out["answer"] == (
        "There are no items with 'Solar' in the title matching your query."
    )


def test_answer_structured_unresolved_theme_falls_through_by_default(monkeypatch):
    """entity_resolution_enabled defaults to False — the rollback path: a theme
    that resolves to nothing AND matches no rows falls through exactly as it did
    before this feature existed, with no settings override needed."""
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr(state, "count_documents", lambda **kw: 0)
    analysis = qp.QueryAnalysis(
        search_query="how many events under Mystery?",
        intent="structured", operation="count", bundle="events", theme="Mystery",
    )
    assert dr.answer_structured("how many events under Mystery?", analysis=analysis) is None


def test_answer_structured_unresolved_theme_is_terminal_not_a_fallthrough(monkeypatch):
    """With the flag on, a theme that resolves to no term AND matches no rows is
    understood-but-unanswerable — the answer names it explicitly rather than
    silently falling through to a vague semantic-search guess
    (docs/database-retrieval-redesign.md §7)."""
    _enable_entity_resolution(monkeypatch)
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr(state, "count_documents", lambda **kw: 0)
    analysis = qp.QueryAnalysis(
        search_query="how many events under Mystery?",
        intent="structured", operation="count", bundle="events", theme="Mystery",
    )
    out = dr.answer_structured("how many events under Mystery?", analysis=analysis)
    assert out["answer"] == "No theme matching 'Mystery' found."


def test_answer_structured_real_theme_is_counted(monkeypatch):
    """A theme documents actually carry is counted and named — the vocabulary
    comes from documents_theme, so no taxonomy crawl is involved."""
    _enable_entity_resolution(monkeypatch)
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [{"theme": "Environment", "theme_type": "primary",
                       "parent": None, "theme_group": "main", "documents": 32}],
    )
    monkeypatch.setattr(state, "count_documents", lambda **kw: 32)
    analysis = qp.QueryAnalysis(
        search_query="how many posts under Environment?",
        intent="structured", operation="count", theme="Environment",
    )
    out = dr.answer_structured("how many posts under Environment?", analysis=analysis)
    assert out["answer"] == "There are 32 items on 'Environment' matching your query."


def test_answer_structured_unresolved_tag_is_terminal_not_a_fallthrough(monkeypatch):
    _enable_entity_resolution(monkeypatch)
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr("app.catalog.queries.find_tag",
                        lambda name: "policy" if name.lower() == "policy" else None)
    monkeypatch.setattr(state, "count_documents", lambda **kw: 0)
    analysis = qp.QueryAnalysis(
        search_query="how many posts are tagged nonexistent?",
        intent="structured", operation="count", tags=["nonexistent"],
    )
    out = dr.answer_structured(
        "how many posts are tagged nonexistent?", analysis=analysis
    )
    assert out["answer"] == "No tag matching 'nonexistent' found."


def test_answer_structured_normalizes_bundle_for_count(monkeypatch):
    monkeypatch.setattr(
        dr, "parse_structured",
        lambda q, h=None: dr.StructuredQuery(operation="count", bundle="event"),
    )
    seen: dict = {}
    monkeypatch.setattr(state, "count_documents", lambda **kw: seen.update(kw) or 3)

    out = dr.answer_structured("how many events?")

    assert seen["bundle"] == "events"  # normalized before the catalog query
    assert out["answer"] == "There are 3 events matching your query."


def test_answer_structured_generic_publications_spans_all_types(monkeypatch):
    # "publications" is a collective word; the classifier may collapse it onto the
    # research_papers bundle, which would under-count a person's total output. The
    # bundle must be dropped so the count spans every content type.
    seen: dict = {}
    monkeypatch.setattr(state, "count_documents", lambda **kw: seen.update(kw) or 21)
    analysis = qp.QueryAnalysis(
        search_query="how many publications from Dr Suneel Pandey",
        intent="structured", operation="count",
        bundle="research_papers", author="Dr Suneel Pandey",
    )
    out = dr.answer_structured(
        "tell me overall number of publications from Dr Suneel Pandey", analysis=analysis
    )
    assert seen["bundle"] is None  # spans all content types, not just papers
    assert out["answer"] == "There are 21 items by Dr Suneel Pandey matching your query."


def test_answer_structured_named_type_keeps_bundle(monkeypatch):
    # When the user actually names the type, the bundle is honored even though the
    # word "publications" is also present.
    seen: dict = {}
    monkeypatch.setattr(state, "count_documents", lambda **kw: seen.update(kw) or 10)
    analysis = qp.QueryAnalysis(
        search_query="how many research paper publications from Dr Suneel Pandey",
        intent="structured", operation="count",
        bundle="research_papers", author="Dr Suneel Pandey",
    )
    dr.answer_structured(
        "how many research paper publications from Dr Suneel Pandey", analysis=analysis
    )
    assert seen["bundle"] == "research_papers"


def test_spans_all_content_helper():
    # Generic term, bundle words absent from the question -> span all types.
    assert dr._spans_all_content("how many publications from Dr X", "research_papers")
    # The named type appears -> keep the bundle.
    assert not dr._spans_all_content("how many research papers from Dr X", "research_papers")
    # No generic term -> keep the bundle.
    assert not dr._spans_all_content("how many articles from Dr X", "article")
    # No bundle to begin with -> nothing to clear.
    assert not dr._spans_all_content("how many publications from Dr X", None)


def test_answer_structured_skips_parse_when_analysis_provided(monkeypatch):
    def no_parse(q, h=None):
        raise AssertionError("parse_structured must not be called")

    monkeypatch.setattr(dr, "parse_structured", no_parse)
    seen: dict = {}
    monkeypatch.setattr(state, "count_documents", lambda **kw: seen.update(kw) or 5)

    analysis = qp.QueryAnalysis(
        search_query="how many events in 2024",
        intent="structured",
        operation="count",
        bundle="event",
        date_from="2024-01-01",
        date_to="2025-01-01",
    )
    out = dr.answer_structured("how many events in 2024?", analysis=analysis)

    assert seen["bundle"] == "events"  # normalized before the catalog query
    assert seen["effective_from"] == datetime(2024, 1, 1)
    assert seen["effective_to"] == datetime(2025, 1, 1)
    assert out["answer"] == "There are 5 events in 2024 matching your query."


def _count_distinct_spy(monkeypatch, n=7):
    seen: dict = {}

    def fake(dimension, **kw):
        seen.update(kw, dimension=dimension)
        return n

    monkeypatch.setattr(state, "count_distinct_values", fake)
    monkeypatch.setattr(state, "count_documents", _forbid_count)
    return seen


def test_how_many_authors_counts_author_names_not_documents(monkeypatch):
    """Measured: the analysis carried count_of="records" for this question, so
    it would have been answered with the number of documents."""
    seen = _count_distinct_spy(monkeypatch, n=955)
    analysis = qp.QueryAnalysis(
        search_query="How many authors are there?", intent="structured",
        operation="count",
    )
    out = dr.answer_structured("How many authors are there?", analysis=analysis)
    assert seen["dimension"] == "author"
    assert out["answer"].startswith(
        "There are 955 distinct author names recorded in the source data."
    )


def test_how_many_themes_is_answered_from_the_theme_list(monkeypatch):
    """Measured: "How many themes are there?" was answered "There are 5524
    items". A distinct count over the theme facet would say 26, because it
    includes sub-theme rows; the listing says 7, as the home page does."""
    _count_distinct_spy(monkeypatch)
    monkeypatch.setattr(
        state, "theme_vocabulary",
        lambda **kw: [
            {"theme": name, "theme_type": "primary", "parent": None,
             "theme_group": "main", "documents": 3}
            for name in ("Climate Change", "Energy")
        ] + [{"theme": "Air", "theme_type": "sub", "parent": "Environment",
              "theme_group": "main", "documents": 3}],
    )
    analysis = qp.QueryAnalysis(
        search_query="How many themes are there?", intent="structured",
        operation="count",
    )
    out = dr.answer_structured("How many themes are there?", analysis=analysis)
    assert out["answer"].startswith("The collection covers 2 main themes")
    assert "Air" not in out["answer"]


def test_a_scoped_theme_count_counts_distinct_themes(monkeypatch):
    seen = _count_distinct_spy(monkeypatch, n=4)
    question = "How many themes does Meena Sehgal publish in?"
    analysis = qp.QueryAnalysis(
        search_query=question, intent="structured", operation="count",
        author="Meena Sehgal",
    )
    out = dr.answer_structured(question, analysis=analysis)
    assert seen["dimension"] == "theme"
    # The name is whatever author resolution canonicalised it to.
    assert out["answer"].startswith("There are 4 themes by ")


def test_a_counted_noun_does_not_override_a_deliberate_count_of(monkeypatch):
    seen = _count_distinct_spy(monkeypatch)
    analysis = qp.QueryAnalysis(
        search_query="q", intent="structured", operation="count", count_of="year",
    )
    dr.answer_structured("How many themes cover years of work?", analysis=analysis)
    assert seen["dimension"] == "year"


def test_counted_noun_reads_only_facet_nouns():
    assert dr._counted_noun("How many distinct authors wrote on energy?") == "author"
    assert dr._counted_noun("how many main themes are there") == "theme"
    assert dr._counted_noun("How many thematic areas does TERI have?") == "theme"
    assert dr._counted_noun("How many content types are there?") == "content_type"
    # Documents, and nouns the catalog does not record as a facet, stay as they were.
    assert dr._counted_noun("How many articles on climate change?") is None
    assert dr._counted_noun("How many people work at TERI?") is None
    assert dr._counted_noun("How many years of reports are there?") is None


def test_a_year_the_classifier_dropped_is_restored(monkeypatch):
    """Measured: "How many events were held in 2030?" arrived without dates — the
    coverage directive tells the classifier to drop a period the catalog does not
    reach — and was answered with the all-time total of 1082 events."""
    seen: dict = {}
    monkeypatch.setattr(state, "count_documents", lambda **kw: seen.update(kw) or 0)
    analysis = qp.QueryAnalysis(
        search_query="How many events were held in 2030?", intent="structured",
        operation="count", bundle="events",
    )
    out = dr.answer_structured("How many events were held in 2030?", analysis=analysis)
    assert seen["effective_from"] == datetime(2030, 1, 1)
    assert seen["effective_to"] == datetime(2031, 1, 1)
    assert "in 2030" in out["answer"]


def test_a_year_restores_through_the_parse_fallback(monkeypatch):
    monkeypatch.setattr(
        dr, "parse_structured",
        lambda q, h=None: dr.StructuredQuery(operation="count", bundle="events"),
    )
    seen: dict = {}
    monkeypatch.setattr(state, "count_documents", lambda **kw: seen.update(kw) or 0)
    dr.answer_structured("How many events were held during 2019?")
    assert seen["effective_from"] == datetime(2019, 1, 1)


def test_a_year_that_names_a_subject_is_not_a_period(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(state, "count_documents", lambda **kw: seen.update(kw) or 4)
    for question in ("How many reports are there on the 2030 Agenda?",
                     "How many reports compare 2020 with 2030?",
                     "How many reports are titled Energy in 2030?"):
        seen.clear()
        analysis = qp.QueryAnalysis(
            search_query=question, intent="structured", operation="count",
            bundle="report",
            title_contains="Energy in 2030" if "titled" in question else None,
        )
        dr.answer_structured(question, analysis=analysis)
        assert "effective_from" not in seen, question


def test_a_year_the_classifier_kept_is_left_alone(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(state, "count_documents", lambda **kw: seen.update(kw) or 5)
    analysis = qp.QueryAnalysis(
        search_query="q", intent="structured", operation="count", bundle="events",
        date_from="2024-03-01", date_to="2024-04-01",
    )
    dr.answer_structured("How many events were held in 2024?", analysis=analysis)
    assert seen["effective_from"] == datetime(2024, 3, 1)


def test_answer_structured_falls_back_to_parse_without_operation(monkeypatch):
    monkeypatch.setattr(
        dr, "parse_structured",
        lambda q, h=None: dr.StructuredQuery(operation="count", bundle="events"),
    )
    monkeypatch.setattr(state, "count_documents", lambda **kw: 4)

    analysis = qp.QueryAnalysis(search_query="x", intent="structured")  # no operation
    out = dr.answer_structured("how many events?", analysis=analysis)
    assert out["answer"] == "There are 4 events matching your query."


def test_answer_structured_passes_format_from_analysis(monkeypatch):
    monkeypatch.setattr(state, "distribution", lambda *a, **k: [("Climate", 2)])
    analysis = qp.QueryAnalysis(
        search_query="articles per theme as a table",
        intent="structured", operation="distribution", answer_format="table",
    )
    out = dr.answer_structured("articles per theme as a table", analysis=analysis)
    assert "| theme | count |" in out["answer"]


def test_answer_structured_prefers_multi_plan_when_enabled(monkeypatch):
    sentinel = object()
    seen: dict = {}

    def forbid_v1(*a, **k):
        raise AssertionError("v1 plan must not run when the multi-call plan succeeds")

    monkeypatch.setattr(
        dr, "get_settings", lambda: SimpleNamespace(database_multi_call_enabled=True)
    )
    monkeypatch.setattr(planner, "plan_multi", lambda q, *, output_format: sentinel)
    monkeypatch.setattr(planner, "plan", forbid_v1)
    monkeypatch.setattr(
        planner, "execute",
        lambda db_plan, *, question=None: seen.update(plan=db_plan)
        or [ToolResult(tool="count_records", ok=True, rendered="R")],
    )
    analysis = qp.QueryAnalysis(search_query="x", intent="structured", operation="count")
    out = dr.answer_structured("2023 vs 2024?", analysis=analysis)

    assert seen["plan"] is sentinel  # the v2 plan was executed
    assert out["answer"] == "R"


def test_answer_structured_partial_success_drops_terminal_failure(monkeypatch):
    """A terminal failure alongside a successful call in the same multi-call
    plan is dropped exactly like any other ok=False result — the terminal
    check only kicks in when EVERY call failed (see _terminal_result)."""
    sentinel = object()
    monkeypatch.setattr(
        dr, "get_settings", lambda: SimpleNamespace(database_multi_call_enabled=True)
    )
    monkeypatch.setattr(planner, "plan_multi", lambda q, *, output_format: sentinel)
    monkeypatch.setattr(
        planner, "execute",
        lambda db_plan, *, question=None: [
            ToolResult(tool="count_records", ok=True, rendered="3 items."),
            ToolResult(tool="count_records", ok=False, error_kind="unresolved",
                      rendered="No theme matching 'X' found."),
        ],
    )
    analysis = qp.QueryAnalysis(search_query="x", intent="structured", operation="count")
    out = dr.answer_structured("x", analysis=analysis)
    assert out["answer"] == "3 items."


def test_terminal_result_finds_first_terminal_failure():
    results = [
        ToolResult(tool="count_records", ok=False, error_kind=None),
        ToolResult(tool="count_records", ok=False, error_kind="unresolved",
                  rendered="No theme matching 'X' found."),
    ]
    terminal = dr._terminal_result(results, strict=True)
    assert terminal is not None and terminal.rendered == "No theme matching 'X' found."


def test_terminal_result_none_when_no_failure_is_terminal():
    results = [ToolResult(tool="count_records", ok=False, error_kind="no_records")]
    assert dr._terminal_result(results, strict=True) is None


def test_terminal_result_none_for_empty_results():
    assert dr._terminal_result([], strict=True) is None


def test_fuzzy_match_failures_are_terminal_only_when_strict():
    """`entity_resolution_enabled` off keeps the old fall-through for the
    outcomes fuzzy matching produces."""
    for kind in ("unresolved", "ambiguous"):
        results = [ToolResult(tool="count_records", ok=False, error_kind=kind,
                              rendered="msg")]
        assert dr._terminal_result(results, strict=True) is not None, kind
        assert dr._terminal_result(results, strict=False) is None, kind


def test_ambiguous_content_type_is_terminal_regardless_of_the_flag():
    """A word naming several bundles is decided from a curated list, not by
    similarity, so the flag holding fuzzy matching back does not apply. Falling
    through would answer "how many projects" from prose."""
    results = [ToolResult(tool="count_records", ok=False, error_kind="ambiguous_entity",
                          rendered="'projects' matches more than one content type:")]
    for strict in (True, False):
        assert dr._terminal_result(results, strict=strict) is not None, strict


def test_answer_structured_surfaces_an_ambiguous_content_type_with_the_flag_off(
    monkeypatch,
):
    """End-to-end shape of the above: the clarification is the answer, not None."""
    monkeypatch.setattr(
        dr, "get_settings",
        lambda: SimpleNamespace(database_multi_call_enabled=False,
                                entity_resolution_enabled=False),
    )
    monkeypatch.setattr(
        planner, "execute",
        lambda db_plan, *, question=None: [
            ToolResult(tool="count_records", ok=False, error_kind="ambiguous_entity",
                       rendered="'projects' matches more than one content type:\n"
                                "1. completed projects\n2. ongoing projects\n"
                                "Which did you mean?"),
        ],
    )
    analysis = qp.QueryAnalysis(search_query="x", intent="structured",
                                operation="count", bundle="projects")
    out = dr.answer_structured("how many projects are there", analysis=analysis)
    assert out is not None
    assert "Which did you mean?" in out["answer"]


def test_answer_structured_falls_back_to_v1_when_multi_none(monkeypatch):
    monkeypatch.setattr(
        dr, "get_settings", lambda: SimpleNamespace(database_multi_call_enabled=True)
    )
    monkeypatch.setattr(planner, "plan_multi", lambda q, *, output_format: None)
    monkeypatch.setattr(state, "count_documents", lambda **kw: 2)

    analysis = qp.QueryAnalysis(
        search_query="x", intent="structured", operation="count", bundle="events"
    )
    out = dr.answer_structured("how many events?", analysis=analysis)
    assert out["answer"] == "There are 2 events matching your query."


def test_compose_stacks_sections_and_renumbers_citations():
    r1 = ToolResult(
        tool="list_records", ok=True, data={"records": [{"title": "A"}]},
        citations=[{"n": 1, "title": "A"}], rendered="Section one",
    )
    r2 = ToolResult(
        tool="list_records", ok=True,
        data={"records": [{"title": "B"}, {"title": "C"}]},
        citations=[{"n": 1, "title": "B"}, {"n": 2, "title": "C"}],
        rendered="Section two",
    )
    out = dr._compose([r1, r2])
    assert out["answer"] == "Section one\n\nSection two"
    # Second section's citations shift up so numbering is unique across sections.
    assert [(c["n"], c["title"]) for c in out["citations"]] == [
        (1, "A"), (2, "B"), (3, "C"),
    ]
    assert out["used_chunks"] == 3  # 1 + 2 records
    assert out["intent"] == "structured" and out["conflict"] is False


# --------------------------------------------------------------------------- #
# Semantic path — dates / author / tags become query_processor facet filters.
# --------------------------------------------------------------------------- #

def test_facet_filters_builds_an_overlapping_date_scope():
    """A date range is a period query now, not a bound on the start date.

    The scope is one nested filter: an upper bound on ``effective_start_date``
    and a group of lower-bound branches, one per document shape (closed period,
    open-ended period, and a point at each precision). The old single
    ``DatetimeRange(gte, lt)`` on the start could not express "a 2020-2024
    project is about 2022".
    """
    from app.retrieval.understanding.filters import date_conditions

    analysis = qp.QueryAnalysis(search_query="x", date_from="2024-03-01",
                                date_to="2024-04-01")
    conds = qp._facet_filters(analysis)
    scope = date_conditions(conds)
    assert len(scope) == 1, "exactly one date scope, whatever its internal shape"

    def bounds(condition, out):
        rng = getattr(condition, "range", None)
        if rng is not None:
            out.append((getattr(condition, "key", None),
                        getattr(rng, "gte", None), getattr(rng, "lt", None)))
        for group in ("must", "should", "must_not"):
            for nested in getattr(condition, group, None) or []:
                bounds(nested, out)
        return out

    found = bounds(scope[0], [])
    hi = qp._parse_bound("2024-04-01")
    # The upper bound is exclusive, on the start, and stated once.
    assert [b for b in found if b[2] == hi] == [("effective_start_date", None, hi)]
    # The lower bound appears floored to each precision the branches cover.
    lows = {b[1] for b in found if b[1] is not None}
    assert qp._parse_bound("2024-03-01") in lows, "day precision"
    assert qp._parse_bound("2024-01-01") in lows, "floored for a year-precision date"


def test_facet_filters_no_dates_no_condition():
    conds = qp._facet_filters(qp.QueryAnalysis(search_query="x"))
    assert not any(getattr(c, "key", None) == "effective_start_date" for c in conds)


def test_facet_filters_tags_exact_match_author_not_filtered():
    analysis = qp.QueryAnalysis(
        search_query="x", author="Dr R K Sharma", tags=["biofuels", "solar"]
    )
    conds = qp._facet_filters(analysis)
    by_key = {getattr(c, "key", None): c for c in conds}
    assert by_key["tags"].match.any == ["biofuels", "solar"]
    # author is intentionally NOT a hard filter: exact keyword match on sparse
    # display-name metadata excludes most of the corpus and rarely matches the
    # LLM's extracted form (see _facet_filters). Scoping stays on the catalog path.
    assert "authors" not in by_key


def test_facet_filters_absent_tags_add_nothing():
    conds = qp._facet_filters(qp.QueryAnalysis(search_query="x"))
    keys = {getattr(c, "key", None) for c in conds}
    assert "tags" not in keys


# --------------------------------------------------------------------------- #
# Generation format directives.
# --------------------------------------------------------------------------- #

def test_timeline_format_directive_exists():
    from app.generation.prompts import format_directive

    directive = format_directive("timeline")
    assert "chronological" in directive and "citation" in directive


def test_format_exemplars_attach_only_with_their_directive():
    from app.generation.prompts import format_directive

    assert "Example shape:" in format_directive("table")
    assert "Example shape:" in format_directive("timeline")
    # The default path must stay lean: no directive, no exemplar.
    assert format_directive("default") == ""
    assert format_directive(None) == ""
    assert "Example shape:" not in format_directive("list")


def test_grounded_prompt_carries_worked_example():
    from app.generation.prompts import GROUNDED_SYSTEM_PROMPT

    assert "Example:" in GROUNDED_SYSTEM_PROMPT
    assert GROUNDED_SYSTEM_PROMPT.rstrip().endswith(
        "Answer factually, in as much depth as the context genuinely supports."
    )


# --------------------------------------------------------------------------- #
# ProcessedQuery / analysis schema.
# --------------------------------------------------------------------------- #

def test_query_analysis_structured_slot_defaults():
    a = qp.QueryAnalysis(search_query="x")
    assert a.operation is None
    assert a.bundle is None
    assert a.group_by is None
    assert a.title_contains is None
    assert a.author is None
    assert a.tags == []
    assert a.limit == 10


def test_answer_format_accepts_timeline():
    a = qp.QueryAnalysis(search_query="x", answer_format="timeline")
    assert a.answer_format == "timeline"


def test_process_carries_analysis(monkeypatch):
    understanding = qp.QueryUnderstanding(
        query_rewrite="how many events in 2024",
        intents=[qp.IntentPrediction(label="database", confidence=0.9, rationale="")],
        operation="count",
        bundle="events",
    )

    class _FakeStructured:
        def with_structured_output(self, schema):
            return self

        def invoke(self, messages):
            return understanding

    monkeypatch.setattr(qp, "get_structured_llm", lambda: _FakeStructured())
    pq = qp.process("how many events in 2024?")
    # 'database' derives the legacy structured route; slots reach pq.analysis and
    # the full multi-label result is exposed on pq.understanding.
    assert pq.intent == "structured"
    assert pq.analysis.operation == "count"
    assert pq.analysis.bundle == "events"
    assert pq.understanding.intents[0].label == "database"


def test_process_passthrough_has_no_analysis(monkeypatch):
    def boom():
        raise RuntimeError("llm down")

    monkeypatch.setattr(qp, "get_structured_llm", boom)
    pq = qp.process("hello")
    assert pq.analysis is None
    assert pq.intent == "qa"


# --------------------------------------------------------------------------- #
# A count and a breakdown of the same scope must agree.
#
# They diverged in two places, and both were invisible because every registered
# bundle is currently a website node:
#
#   * `distribution` defaulted `source_type` to "website" while its two sibling
#     functions defaulted to None, so the same author was 35 documents in a
#     breakdown and 46 in a count;
#   * `aggregate_records` hardcoded source_type/entity_type instead of reading
#     them off the entity the way `count_records` does.
#
# The numbers a user is told are the product this system sells, so the invariant
# is asserted directly rather than left to the two call sites to keep in step.
# --------------------------------------------------------------------------- #


def test_distribution_defaults_to_no_source_filter_like_its_siblings():
    """Every other parameter of these functions means "no filter" when unset."""
    import inspect

    from app.catalog import queries

    defaults = {
        fn.__name__: inspect.signature(fn).parameters["source_type"].default
        for fn in (queries.count_documents, queries.list_documents,
                   queries.distribution)
    }
    assert set(defaults.values()) == {None}, defaults


def test_aggregate_records_scopes_from_the_entity_not_a_hardcoded_default(
    monkeypatch,
):
    """A bundle that is not a website node must scope the breakdown to what it
    actually is — otherwise a count and its breakdown answer differently."""
    from app.retrieval.structured import tools
    from app.retrieval.structured.types import RecordFilters

    class _Entity:
        name = "report"
        source_type = "pdf_attachment"
        entity_type = "file"

    seen = {}

    def _fake_distribution(dimension, **kwargs):
        seen.update(kwargs)
        return [("Energy", 3)]

    # A registered bundle name, so `_entity_guard` lets it through, standing in
    # for one whose source kind is not a website node.
    monkeypatch.setattr(tools, "get_entity", lambda name: _Entity())
    monkeypatch.setattr(tools.state, "distribution", _fake_distribution)
    tools.aggregate_records("report", "theme", RecordFilters())

    assert seen["source_type"] == "pdf_attachment"
    assert seen["entity_type"] == "file"


def test_count_and_aggregate_scope_the_same_entity_identically(monkeypatch):
    """The two tools must derive their scope the same way, so a total and a
    breakdown of that total can never disagree."""
    from app.retrieval.structured import tools
    from app.retrieval.structured.types import RecordFilters

    class _Entity:
        name = "report"
        source_type = "pdf"
        entity_type = "file"

    counted, grouped = {}, {}
    monkeypatch.setattr(tools, "get_entity", lambda name: _Entity())
    monkeypatch.setattr(
        tools.state, "count_documents",
        lambda **kw: counted.update(kw) or 7,
    )
    monkeypatch.setattr(
        tools.state, "distribution",
        lambda dimension, **kw: grouped.update(kw) or [("Energy", 7)],
    )
    tools.count_records("report", RecordFilters())
    tools.aggregate_records("report", "theme", RecordFilters())

    for key in ("source_type", "entity_type", "bundle"):
        assert counted[key] == grouped[key], (key, counted[key], grouped[key])
