"""Phase 3: the Database Planner — slot mapping and plan execution/routing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.retrieval.structured import planner
from app.retrieval.structured.types import DatabasePlan, ToolCall, ToolResult


def _slots(**kw):
    base = dict(
        operation=None, bundle=None, theme=None, author=None, title_contains=None,
        group_by=None, date_from=None, date_to=None, limit=10,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "operation, tool",
    [
        ("count", "count_records"),
        ("distribution", "aggregate_records"),
        ("lookup", "lookup_record"),
        ("list", "list_records"),
        (None, "list_records"),   # default
    ],
)
def test_plan_maps_operation_to_tool(operation, tool):
    call = planner.plan(_slots(operation=operation, bundle="news")).calls[0]
    assert call.tool == tool
    assert call.entity == "news"


def test_plan_maps_list_themes_operation():
    call = planner.plan(_slots(operation="list_themes", limit=50)).calls[0]
    assert call.tool == "list_themes"
    assert call.entity is None  # vocabulary-wide, not scoped to a bundle


def test_plan_list_themes_ignores_the_content_row_limit():
    """A vocabulary enumeration must cover the whole vocabulary — inheriting the
    content-row limit (default 10) would report a truncated theme count as if it
    were the total."""
    for row_limit in (10, 50, None):
        call = planner.plan(_slots(operation="list_themes", limit=row_limit)).calls[0]
        assert call.limit == planner.THEME_VOCABULARY_LIMIT


def test_execute_lists_top_level_themes_even_when_a_theme_is_named(monkeypatch):
    """A named theme does not turn the listing into a sub-theme listing: the
    tool is only ever asked for the theme vocabulary."""
    seen = {}

    def fake_list_themes(*, scope, limit, output_format):
        seen.update(scope=scope)
        return ToolResult(tool="list_themes")

    monkeypatch.setattr(planner, "list_themes", fake_list_themes)
    planner.execute(planner.plan(_slots(operation="list_themes", theme="Energy")))
    # No question was supplied, so the listing stays on the safe side.
    assert seen["scope"] == "main"


def test_plan_multi_list_themes_ignores_the_llm_row_limit():
    planned = planner._PlannedCall(tool="list_themes")  # LLM leaves limit at 10
    assert planned.limit == 10
    assert planner._to_tool_call(planned, "default").limit == planner.THEME_VOCABULARY_LIMIT


def test_plan_multi_other_tools_keep_their_row_limit():
    planned = planner._PlannedCall(tool="list_records", limit=5)
    assert planner._to_tool_call(planned, "default").limit == 5


def test_plan_carries_filters_and_format():
    call = planner.plan(
        _slots(operation="count", theme="Climate", author="Sharma",
               date_from="2024-01-01", date_to="2025-01-01"),
        output_format="table",
    ).calls[0]
    assert call.output_format == "table"
    assert call.filters.theme == "Climate"
    assert call.filters.author == "Sharma"
    assert call.filters.date_from == "2024-01-01"


def test_plan_maps_first_tag_to_filter():
    """`tags` (plural, from the shared query-understanding extraction) maps to
    the single `RecordFilters.tag` slot the catalog tools support."""
    call = planner.plan(
        _slots(operation="count", tags=["policy", "climate"])
    ).calls[0]
    assert call.filters.tag == "policy"


def test_plan_no_tags_leaves_filter_unset():
    call = planner.plan(_slots(operation="count", tags=[])).calls[0]
    assert call.filters.tag is None
    call = planner.plan(_slots(operation="count")).calls[0]  # no tags attr at all
    assert call.filters.tag is None


def test_plan_expands_year_shorthand():
    # parse_structured may set only `year`; the planner expands it to a range.
    call = planner.plan(_slots(operation="list", year=2023)).calls[0]
    assert (call.filters.date_from, call.filters.date_to) == ("2023-01-01", "2024-01-01")


def test_plan_explicit_dates_win_over_year():
    call = planner.plan(
        _slots(operation="count", year=2023, date_from="2020-06-01", date_to="2020-07-01")
    ).calls[0]
    assert (call.filters.date_from, call.filters.date_to) == ("2020-06-01", "2020-07-01")


def test_execute_routes_to_tool(monkeypatch):
    monkeypatch.setattr(
        planner, "count_records",
        lambda entity, filters, *, question=None, count_of="records", detail=False: ToolResult(
            tool="count_records", entity=entity, data={"count": 7}
        ),
    )
    results = planner.execute(DatabasePlan(calls=[ToolCall(tool="count_records", entity="news")]))
    assert len(results) == 1
    assert results[0].tool == "count_records" and results[0].data == {"count": 7}


def test_execute_routes_to_list_themes(monkeypatch):
    monkeypatch.setattr(
        planner, "list_themes",
        lambda *, scope, limit, output_format: ToolResult(
            tool="list_themes", data={"themes": ["Climate"]}
        ),
    )
    results = planner.execute(DatabasePlan(calls=[ToolCall(tool="list_themes")]))
    assert len(results) == 1
    assert results[0].tool == "list_themes" and results[0].data == {"themes": ["Climate"]}


def test_execute_forwards_offset_and_fields_to_list_records(monkeypatch):
    """Both exist on list_records/list_documents; without this forwarding they
    are unreachable from any plan — dead parameter surface."""
    seen = {}

    def fake_list(entity, filters, *, sort, limit, offset, output_format, fields,
                  detail=False):
        seen.update(limit=limit, offset=offset, fields=fields)
        return ToolResult(tool="list_records", entity=entity)

    monkeypatch.setattr(planner, "list_records", fake_list)
    planner.execute(DatabasePlan(calls=[
        ToolCall(tool="list_records", entity="report", limit=5, offset=10,
                 fields=["title", "url"]),
    ]))
    assert seen == {"limit": 5, "offset": 10, "fields": ["title", "url"]}


def test_plan_multi_carries_fields_but_not_offset(monkeypatch):
    """`fields` is LLM-settable (users ask for specific metadata); `offset` is
    not — there is no "next page" state for the model to reason about, and a
    hallucinated offset silently hides rows."""
    assert "offset" not in planner._PlannedCall.model_fields
    planned = planner._PlannedCall(tool="list_records", fields=["title"])
    call = planner._to_tool_call(planned, "default")
    assert call.fields == ["title"]
    assert call.offset == 0


def test_plan_multi_empty_fields_list_means_all_fields():
    planned = planner._PlannedCall(tool="list_records", fields=[])
    assert planner._to_tool_call(planned, "default").fields is None


def test_execute_routes_to_resolve_entity(monkeypatch):
    monkeypatch.setattr(
        planner, "resolve_entity",
        lambda query, resolve_type: ToolResult(
            tool="resolve_entity", ok=True,
            data={"resolved": {"id": "Rishabh Negi", "canonical_name": "Rishabh Negi",
                               "type": "author", "score": 1.0}},
        ),
    )
    results = planner.execute(
        DatabasePlan(calls=[ToolCall(tool="resolve_entity", query="rishabh negi",
                                     resolve_type="author")])
    )
    assert len(results) == 1
    assert results[0].tool == "resolve_entity" and results[0].ok is True


def test_execute_runs_multiple_calls(monkeypatch):
    monkeypatch.setattr(
        planner, "count_records",
        lambda entity, filters, *, question=None, count_of="records", detail=False: ToolResult(
            tool="count_records", entity=entity
        ),
    )
    monkeypatch.setattr(
        planner, "aggregate_records",
        lambda entity, group_by, filters, secondary_group_by=None,
        aggregation="count", output_format="default", detail=False:
            ToolResult(tool="aggregate_records", entity=entity),
    )
    plan_obj = DatabasePlan(calls=[
        ToolCall(tool="count_records", entity="a"),
        ToolCall(tool="aggregate_records", entity="b", group_by="theme"),
    ])
    results = planner.execute(plan_obj)
    assert {r.tool for r in results} == {"count_records", "aggregate_records"}


def test_execute_passes_question_to_lookup(monkeypatch):
    seen = {}

    def fake_lookup(entity, title, filters, *, limit, output_format, question,
                    detail=False):
        seen["question"] = question
        seen["title"] = title
        return ToolResult(tool="lookup_record", entity=entity)

    monkeypatch.setattr(planner, "lookup_record", fake_lookup)
    planner.execute(
        DatabasePlan(calls=[ToolCall(tool="lookup_record", entity="report", title="T")]),
        question="what does it say?",
    )
    assert seen == {"question": "what does it say?", "title": "T"}


def test_execute_passes_question_to_count(monkeypatch):
    """count_records needs the question to tell a title-scoped zero the user asked
    for from one the classifier guessed (tools._title_guess_zero)."""
    seen = {}

    def fake_count(entity, filters, *, question=None, count_of="records", detail=False):
        seen["question"] = question
        return ToolResult(tool="count_records", entity=entity)

    monkeypatch.setattr(planner, "count_records", fake_count)
    planner.execute(
        DatabasePlan(calls=[ToolCall(tool="count_records", entity="report")]),
        question="how many reports about solar?",
    )
    assert seen == {"question": "how many reports about solar?"}


def test_execute_empty_plan():
    assert planner.execute(DatabasePlan(calls=[])) == []


# --------------------------------------------------------------------------- #
# plan_multi — the v2 LLM planner (LLM stubbed; no network).
# --------------------------------------------------------------------------- #

class _FakePlannerLLM:
    def __init__(self, plan):
        self._plan = plan

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        return self._plan


def test_plan_multi_builds_calls_from_llm(monkeypatch):
    fake = planner._MultiPlan(
        calls=[
            planner._PlannedCall(tool="count_records", entity="report",
                                 date_from="2023-01-01",
                                 date_to_inclusive="2023-12-31"),
            planner._PlannedCall(tool="count_records", entity="report",
                                 date_from="2024-01-01",
                                 date_to_inclusive="2024-12-31"),
        ],
        rationale="2023 vs 2024",
    )
    monkeypatch.setattr(
        "app.core.clients.llm.get_structured_llm", lambda: _FakePlannerLLM(fake)
    )
    plan_obj = planner.plan_multi("reports in 2023 vs 2024", output_format="table")

    assert [c.tool for c in plan_obj.calls] == ["count_records", "count_records"]
    assert plan_obj.calls[0].filters.date_from == "2023-01-01"
    assert plan_obj.calls[1].filters.date_to == "2025-01-01"
    assert all(c.output_format == "table" for c in plan_obj.calls)


def test_plan_multi_maps_resolve_entity_call(monkeypatch):
    """_PlannedCall.tool reuses the shared ToolName Literal, so resolve_entity
    is a valid LLM-planned tool without a second, separately-maintained list."""
    fake = planner._MultiPlan(
        calls=[
            planner._PlannedCall(tool="resolve_entity", query="rishab negi",
                                 resolve_type="author"),
            planner._PlannedCall(tool="count_records", entity="news"),
        ]
    )
    monkeypatch.setattr(
        "app.core.clients.llm.get_structured_llm", lambda: _FakePlannerLLM(fake)
    )
    plan_obj = planner.plan_multi("posts by rishab negi")

    assert [c.tool for c in plan_obj.calls] == ["resolve_entity", "count_records"]
    assert plan_obj.calls[0].query == "rishab negi"
    assert plan_obj.calls[0].resolve_type == "author"


def test_plan_multi_caps_calls(monkeypatch):
    fake = planner._MultiPlan(
        calls=[planner._PlannedCall(tool="list_records") for _ in range(10)]
    )
    monkeypatch.setattr(
        "app.core.clients.llm.get_structured_llm", lambda: _FakePlannerLLM(fake)
    )
    assert len(planner.plan_multi("q").calls) == planner._MAX_CALLS


def test_plan_multi_empty_is_none(monkeypatch):
    monkeypatch.setattr(
        "app.core.clients.llm.get_structured_llm",
        lambda: _FakePlannerLLM(planner._MultiPlan(calls=[])),
    )
    assert planner.plan_multi("q") is None


def test_plan_multi_falls_back_on_error(monkeypatch):
    def boom():
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.core.clients.llm.get_structured_llm", boom)
    assert planner.plan_multi("q") is None


# --------------------------------------------------------------------------- #
# The four count/aggregate shapes.
#
# The operation vocabulary stays at five; what changes is the *subject* of a
# count and the *arity* of a grouping. Unset slots reproduce the old behaviour
# exactly, so every existing plan is unaffected:
#
#   1. document count          count       count_of unset          -> count_documents
#   2. distinct entity count   count       count_of=author|theme|… -> count_distinct_values
#   3. single-dim distribution distribution group_by=X             -> distribution
#   4. two-dim distribution    distribution group_by=X, secondary=Y -> cross_distribution
# --------------------------------------------------------------------------- #


def test_case1_document_count_is_the_default():
    call = planner.plan(_slots(operation="count", bundle="article")).calls[0]
    assert call.tool == "count_records"
    assert call.count_of == "records"


def test_case2_distinct_entity_count_carries_the_facet():
    call = planner.plan(
        _slots(operation="count", bundle="article", count_of="author", theme="Energy")
    ).calls[0]
    assert call.tool == "count_records"
    assert call.count_of == "author"
    assert call.filters.theme == "Energy"


def test_case3_single_dimension_distribution_has_no_second():
    call = planner.plan(
        _slots(operation="distribution", bundle="article", group_by="theme")
    ).calls[0]
    assert call.tool == "aggregate_records"
    assert call.group_by == "theme" and call.secondary_group_by is None


def test_case4_two_dimension_distribution_carries_both():
    call = planner.plan(
        _slots(operation="distribution", bundle="article",
               group_by="author", secondary_group_by="theme")
    ).calls[0]
    assert call.tool == "aggregate_records"
    assert (call.group_by, call.secondary_group_by) == ("author", "theme")


def test_slots_without_the_new_fields_still_plan():
    """`_slots` here has no count_of/secondary_group_by attributes at all — the
    duck-typed read must fall back rather than raise, because the parse fallback
    and the unified analysis are different classes."""
    call = planner.plan(_slots(operation="count")).calls[0]
    assert call.count_of == "records"
    call = planner.plan(_slots(operation="distribution", group_by="year")).calls[0]
    assert call.secondary_group_by is None


def test_execute_forwards_count_of_to_the_tool(monkeypatch):
    seen = {}

    def fake_count(entity, filters, *, question=None, count_of="records", detail=False):
        seen["count_of"] = count_of
        return ToolResult(tool="count_records", entity=entity)

    monkeypatch.setattr(planner, "count_records", fake_count)
    planner.execute(planner.plan(_slots(operation="count", count_of="author")))
    assert seen["count_of"] == "author"


def test_execute_forwards_both_dimensions_to_the_tool(monkeypatch):
    seen = {}

    def fake_aggregate(entity, group_by, filters, *, secondary_group_by=None,
                       aggregation="count", output_format="default", detail=False):
        seen.update(group_by=group_by, secondary_group_by=secondary_group_by)
        return ToolResult(tool="aggregate_records", entity=entity)

    monkeypatch.setattr(planner, "aggregate_records", fake_aggregate)
    planner.execute(
        planner.plan(
            _slots(operation="distribution", group_by="author",
                   secondary_group_by="theme")
        )
    )
    assert seen == {"group_by": "author", "secondary_group_by": "theme"}


def test_the_llm_planner_can_set_both_new_fields():
    planned = planner._PlannedCall(
        tool="aggregate_records", group_by="author", secondary_group_by="theme"
    )
    call = planner._to_tool_call(planned, "default")
    assert (call.group_by, call.secondary_group_by) == ("author", "theme")

    planned = planner._PlannedCall(tool="count_records", count_of="theme")
    assert planner._to_tool_call(planned, "default").count_of == "theme"


def test_the_new_shapes_keep_the_theme_group_rule():
    """Step 2's main-vs-other restriction must survive the new operations: a
    generic distinct count or pair breakdown is still about the main structure."""
    # No theme dimension and no mention of themes: no restriction. Applying
    # one would drop every author whose documents carry no theme.
    generic = planner.plan(
        _slots(operation="count", count_of="author"),
        question="How many authors are there?",
    ).calls[0]
    assert generic.filters.theme_group is None

    paired = planner.plan(
        _slots(operation="distribution", group_by="author",
               secondary_group_by="theme"),
        question="Which authors write about which themes?",
    ).calls[0]
    assert paired.filters.theme_group == "main"

    explicit = planner.plan(
        _slots(operation="count", count_of="author"),
        question="How many authors work on the other themes?",
    ).calls[0]
    assert explicit.filters.theme_group == "other"
