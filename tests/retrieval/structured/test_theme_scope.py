"""Which theme groups a question is asking for.

The guarantee under test is asymmetric: a generic theme question must never
surface Other themes, while missing an explicit request for them is a lesser
failure that the user can correct by asking again. So the default is Main, and
every case that is not clearly a request for more resolves there.
"""
from __future__ import annotations

import pytest

from app.retrieval.structured import planner, theme_scope
from app.retrieval.structured.theme_scope import SCOPE_ALL, SCOPE_MAIN, SCOPE_OTHER


# --------------------------------------------------------------------------- #
# Generic questions get the main thematic structure
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question",
    [
        "How many themes are there?",
        "What are the thematic areas?",
        "What major thematic areas are you working on?",
        "What are your main themes?",
        "What topics do you cover?",
        "List your themes",
        "What themes do you work on?",
        "What are the primary focus areas?",
        "Tell me about your work areas",
    ],
)
def test_a_generic_theme_question_asks_for_main(question):
    assert theme_scope.detect(question) == SCOPE_MAIN


@pytest.mark.parametrize("question", ["", "   ", None])
def test_no_question_defaults_to_main(question):
    assert theme_scope.detect(question) == SCOPE_MAIN


def test_an_unrecognised_question_defaults_to_main():
    """The default is the whole safety property: anything not clearly asking
    for more gets the curated structure."""
    assert theme_scope.detect("Qwerty asdf zxcv?") == SCOPE_MAIN


def test_all_the_main_themes_is_still_a_main_question():
    """The totality asked for is the main structure, not the vocabulary around
    it — "all" alone must not widen the answer."""
    assert theme_scope.detect("List all the main themes") == SCOPE_MAIN
    assert theme_scope.detect("Show me every one of your core themes") == SCOPE_MAIN


# --------------------------------------------------------------------------- #
# Explicit requests for the other themes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question",
    [
        "What other themes are available?",
        "Show me the other themes.",
        "What additional themes does Drupal contain?",
        "Any minor themes?",
        "Any remaining themes?",
        "What are the non-main themes?",
        "Are there secondary themes?",
    ],
)
def test_an_explicit_request_asks_for_other(question):
    assert theme_scope.detect(question) == SCOPE_OTHER


@pytest.mark.parametrize(
    "question",
    [
        "Are there any themes outside the main thematic areas?",
        "What themes exist besides the main ones?",
        "Show themes other than the main ones",
        "Which themes fall beyond the main areas?",
        "Anything apart from the main themes?",
    ],
)
def test_excluding_the_main_group_asks_for_other_not_both(question):
    """These phrases *contain* the word "main" while meaning the opposite of it.

    Reading that "main" as the question naming both sides would answer "what
    else is there?" with the main themes the user was explicitly excluding —
    which is why the exclusion is consumed before the main marker is tested.
    """
    assert theme_scope.detect(question) == SCOPE_OTHER


def test_another_does_not_read_as_other():
    """`\\bother\\b` must not fire inside "another"."""
    assert theme_scope.detect("Tell me another thing about themes") == SCOPE_MAIN


# --------------------------------------------------------------------------- #
# Asking for both
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question",
    [
        "List all themes, main and other",
        "main and other themes please",
        "Show both the main and additional themes",
    ],
)
def test_naming_both_sides_asks_for_all(question):
    assert theme_scope.detect(question) == SCOPE_ALL


@pytest.mark.parametrize(
    "question",
    [
        "Show me the complete list of themes",
        "Give me every theme you have",
        "List all themes",
    ],
)
def test_asking_for_the_whole_vocabulary_asks_for_all(question):
    assert theme_scope.detect(question) == SCOPE_ALL


# --------------------------------------------------------------------------- #
# The planner carries the decision onto the tool call
# --------------------------------------------------------------------------- #


class _Slots:
    operation = "list_themes"
    theme = None
    theme_children = False
    bundle = author = title_contains = group_by = None
    date_from = date_to = year = tags = None
    limit = 10


@pytest.mark.parametrize(
    "question,expected",
    [
        ("What are your themes?", SCOPE_MAIN),
        ("What other themes are there?", SCOPE_OTHER),
        ("List all themes, main and other", SCOPE_ALL),
    ],
)
def test_the_plan_records_the_scope(question, expected):
    call = planner.plan(_Slots(), question=question).calls[0]
    assert call.tool == "list_themes"
    assert call.theme_scope == expected


def test_a_plan_built_without_a_question_is_main_scoped():
    """A caller that has only slots gets the safe side rather than everything."""
    assert planner.plan(_Slots()).calls[0].theme_scope == SCOPE_MAIN


def test_the_scope_reaches_the_tool(monkeypatch):
    """End to end through `execute`: the decision must arrive at `list_themes`,
    not stop at the plan."""
    seen = {}

    def _fake_list_themes(**kwargs):
        seen.update(kwargs)
        from app.retrieval.structured.types import ToolResult

        return ToolResult(tool="list_themes", ok=True, data={}, rendered="x")

    monkeypatch.setattr(planner, "list_themes", _fake_list_themes)
    plan = planner.plan(_Slots(), question="What other themes are there?")
    planner.execute(plan, question="What other themes are there?")
    assert seen["scope"] == SCOPE_OTHER


# --------------------------------------------------------------------------- #
# The prompt backstop, and the line it must not cross
# --------------------------------------------------------------------------- #


def test_the_grounded_prompt_forbids_assembling_a_theme_list_from_context():
    """The catalog path renders theme answers deterministically, with no model
    involved. The semantic path has no such guarantee: a question that does not
    route to `list_themes` reaches generation with a sample of pages, and the
    themes those pages happen to mention are not the thematic structure.

    This is the same argument rule 8 makes about document totals, applied to the
    other thing a sample cannot support a claim about.
    """
    from app.generation.prompts import (
        GROUNDED_SYSTEM_PROMPT,
    )

    for prompt in (GROUNDED_SYSTEM_PROMPT,):
        assert "thematic areas" in prompt
        assert "sample of pages" in prompt
        # The restriction is on generalising, not on mentioning a theme at all.
        assert "belongs to is fine" in prompt


def test_theme_resolution_still_reaches_other_themes(monkeypatch):
    """The separation the whole design rests on.

    Page data is not the thematic structure: a user asking for content under an
    Other theme must still find it. Only the *listing* is narrowed to Main —
    resolving a theme the user named by name spans every group, or the pages
    behind Other themes become unreachable.
    """
    from app.retrieval.structured import resolve

    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [
            {"theme": "Energy", "theme_type": "primary", "parent": None,
             "theme_group": "main", "documents": 10},
            {"theme": "Green Shipping", "theme_type": "primary", "parent": None,
             "theme_group": "other", "documents": 2},
        ],
    )
    names = {c.canonical_name for c in resolve._theme_candidates("Green Shipping")}
    assert "Green Shipping" in names, "an Other theme must stay resolvable"
    assert "Energy" in names


# --------------------------------------------------------------------------- #
# The same rule, applied to counts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How many articles are there?", None),
        ("How many articles under each main theme?", "main"),
        ("How many articles are under the other themes?", "other"),
        ("How many articles across all themes?", None),
    ],
)
def test_counts_carry_the_theme_group_the_question_asked_for(question, expected):
    call = planner.plan(_CountSlots(), question=question).calls[0]
    assert call.filters.theme_group == expected


def test_naming_a_theme_lifts_the_group_restriction():
    """"How many articles under Green Shipping?" must answer, even though Green
    Shipping is an Other theme. The restriction shapes questions that name no
    theme; a named one is always countable."""
    from app.retrieval.structured.filters import resolve_filters

    slots = _CountSlots()
    slots.theme = "Green Shipping"
    call = planner.plan(slots, question="How many articles under Green Shipping?").calls[0]
    assert resolve_filters(call.filters).theme_group is None


class _CountSlots:
    operation = "count"
    bundle = "article"
    theme = None
    theme_children = False
    author = title_contains = group_by = None
    date_from = date_to = year = tags = None
    limit = 10
