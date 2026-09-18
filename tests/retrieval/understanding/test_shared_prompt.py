"""Unit tests for the shared DB-slot prompt text (app.retrieval.structured.prompt)
and its use by the three prompts that previously hand-duplicated it: the intent
classifier, the slot-extraction fallback, and the v2 tool-calling planner.
No LLM, no network — these check the composed prompt strings only.
"""

from __future__ import annotations

import pytest

from app.ingestion.extractors.drupal_extractor import DEFAULT_BUNDLES
from app.retrieval.understanding import catalog_prompt as prompt
from app.retrieval.structured import answerer
from app.retrieval.structured import planner
from app.retrieval.understanding import prompts as understanding_prompts


# --------------------------------------------------------------------------- #
# The blocks themselves.
# --------------------------------------------------------------------------- #

def test_bundle_list_matches_default_bundles():
    assert prompt.BUNDLE_LIST == ", ".join(DEFAULT_BUNDLES)


def test_vocabulary_names_synonyms_and_the_bundle_word():
    assert "items" in prompt.VOCABULARY and "entries" in prompt.VOCABULARY
    assert "bundle" in prompt.VOCABULARY


def test_vocabulary_does_not_call_articles_a_generic_word():
    """`article` is a real bundle (459 of 2,135 rows). Listing "articles" among
    the words that name no specific type made "total number of articles" answer
    with the whole-corpus total instead of the article count."""
    generic, _, naming = prompt.VOCABULARY.partition("A bundle's own name")
    assert "articles" not in generic
    assert "articles" in naming  # named on the "does name a type" side instead


# --------------------------------------------------------------------------- #
# BUNDLE_GLOSSARY — the per-type meanings the bare BUNDLE_LIST left implicit.
# --------------------------------------------------------------------------- #

def test_every_described_bundle_is_a_real_bundle():
    """Drift guard: a glossary entry for a bundle that no longer exists would
    teach the model to set a content type the registry rejects — which counts as
    zero, not as everything."""
    for name, _ in prompt.BUNDLE_MEANINGS:
        assert name in DEFAULT_BUNDLES, name


def test_glossary_describes_every_bundle_users_ask_about_by_name():
    described = {name for name, _ in prompt.BUNDLE_MEANINGS}
    assert described == {
        "article", "feature_articles", "news", "events", "press_release",
        "research_papers", "policy_brief", "report", "completed_projects",
        "ongoing_projects",
    }


def test_glossary_still_advertises_the_undescribed_bundles_as_valid():
    """The types nobody asks for by name stay selectable — describing a subset
    must not silently narrow the set of legal values."""
    for bundle in DEFAULT_BUNDLES:
        assert bundle in prompt.BUNDLE_GLOSSARY, bundle


def test_glossary_claims_plain_articles_for_the_article_bundle():
    assert "- article: " in prompt.BUNDLE_GLOSSARY
    assert "not a generic word" in prompt.BUNDLE_GLOSSARY
    # feature_articles is fenced off so it cannot absorb a plain "articles"
    assert "\"feature\"" in prompt.BUNDLE_GLOSSARY


def test_glossary_passes_an_ambiguous_project_word_through():
    """Bare "projects" spans completed_projects and ongoing_projects. The model
    must neither pick one (reported 0 ongoing while 918 completed existed) nor
    drop the type (counted articles and papers as projects) — it passes the word
    through so tools._entity_guard can ask which was meant."""
    assert "Pass the user's own word through" in prompt.BUNDLE_GLOSSARY
    assert "the query layer will ask which they meant" in prompt.BUNDLE_GLOSSARY
    assert "Do not pick one" in prompt.BUNDLE_GLOSSARY
    assert "do not leave the type off" in prompt.BUNDLE_GLOSSARY


def test_the_pass_through_word_is_one_the_guard_actually_recognizes():
    """The prompt tells the model to send "projects" verbatim; if the registry
    stopped treating that word as ambiguous the instruction would produce an
    unknown entity that quietly falls through instead of asking."""
    from app.retrieval.structured.entities import ambiguous_bundles

    assert "\"projects\"" in prompt.BUNDLE_GLOSSARY
    assert ambiguous_bundles("projects")


def test_collective_word_warning_names_publications():
    assert "publications" in prompt.COLLECTIVE_WORD_WARNING
    assert "research_papers" in prompt.COLLECTIVE_WORD_WARNING


def test_resolve_first_tells_the_planner_not_to_pre_resolve():
    """Resolution happens in the filter path, so a separate pre-resolve call
    would be dead weight — its result cannot reach a sibling call."""
    assert "resolved for you" in prompt.RESOLVE_FIRST
    assert "resolve_entity" in prompt.RESOLVE_FIRST  # still named, for the ask-about case
    assert "cannot reach another call" in prompt.RESOLVE_FIRST


def test_behavior_covers_ambiguity_and_no_fabrication():
    assert "Ambiguity" in prompt.BEHAVIOR
    assert "No fabrication" in prompt.BEHAVIOR
    assert "Main themes" in prompt.BEHAVIOR


def test_few_shots_cover_every_worked_example():
    for marker in (
        "Rishabh Negi", "How many events are there", "How many themes",
        "Climate Change", "content_type", "source links", "tagged 'policy'",
        "clarification question", "no author matching",
    ):
        assert marker in prompt.FEW_SHOTS, marker


def test_few_shots_pass_names_through_rather_than_pre_resolving():
    """Each filtered example is a single call carrying the raw name — the old
    two-call "resolve then query" shape could not work (parallel execution)."""
    assert "rishab negi" in prompt.FEW_SHOTS          # misspelling passed straight in
    assert "do not pre-resolve names yourself" in prompt.FEW_SHOTS
    # resolve_entity appears once, for the "is there an author called X" case only
    assert prompt.FEW_SHOTS.count("resolve_entity") == 1


# --------------------------------------------------------------------------- #
# planner._PLANNER_SYSTEM — the only prompt that should gain resolve_entity.
# --------------------------------------------------------------------------- #

def test_planner_system_advertises_resolve_entity_tool():
    assert "- resolve_entity:" in planner._PLANNER_SYSTEM


def test_planner_system_includes_every_shared_block():
    for block in (
        prompt.BUNDLE_LIST, prompt.BUNDLE_GLOSSARY, prompt.VOCABULARY,
        prompt.RESOLVE_FIRST, prompt.OPERATIONS, prompt.BEHAVIOR,
        prompt.COLLECTIVE_WORD_WARNING, prompt.FEW_SHOTS,
    ):
        assert block in planner._PLANNER_SYSTEM


# --------------------------------------------------------------------------- #
# answerer._PARSE_SYSTEM and understanding._SYSTEM — reuse the vocabulary/
# bundle-list blocks only; neither does multi-step tool calling, so neither
# gains resolve_entity or the few-shots.
# --------------------------------------------------------------------------- #

def test_parse_system_reuses_shared_bundle_and_vocabulary_blocks():
    assert prompt.BUNDLE_LIST in answerer._PARSE_SYSTEM
    assert prompt.COLLECTIVE_WORD_WARNING in answerer._PARSE_SYSTEM
    assert prompt.VOCABULARY in answerer._PARSE_SYSTEM
    assert prompt.BUNDLE_GLOSSARY in answerer._PARSE_SYSTEM
    assert "resolve_entity" not in answerer._PARSE_SYSTEM


def test_understanding_system_reuses_shared_bundle_block():
    assert prompt.BUNDLE_LIST in understanding_prompts.UNDERSTANDING_SYSTEM
    assert prompt.COLLECTIVE_WORD_WARNING in understanding_prompts.UNDERSTANDING_SYSTEM
    assert prompt.BUNDLE_GLOSSARY in understanding_prompts.UNDERSTANDING_SYSTEM
    assert "resolve_entity" not in understanding_prompts.UNDERSTANDING_SYSTEM


@pytest.mark.parametrize(
    "system",
    [
        pytest.param(lambda: answerer._PARSE_SYSTEM, id="parse"),
        pytest.param(lambda: planner._PLANNER_SYSTEM, id="planner"),
    ],
)
def test_glossary_precedes_the_vocabulary_block_that_cites_it(system):
    """VOCABULARY points at "the everyday words listed for it above" — ordering
    is load-bearing, and appending either block in the wrong place leaves a
    dangling reference."""
    text = system()
    assert text.index(prompt.BUNDLE_GLOSSARY) < text.index(prompt.VOCABULARY)


def test_bundle_list_is_not_duplicated_by_hand_in_any_consumer():
    """Regression guard for the drift this module exists to prevent: none of
    the three consumers should hand-build ", ".join(DEFAULT_BUNDLES) anymore."""
    import inspect

    for module in (answerer, planner, understanding_prompts):
        source = inspect.getsource(module)
        assert "DEFAULT_BUNDLES" not in source


def test_shared_prompt_does_not_depend_on_the_structured_package():
    """It is imported by the intent classifier, which wants prompt *text* only.
    Importing from app.retrieval.structured would run that package's __init__ —
    dragging in the tools, planner and MySQL/Qdrant/LLM clients — and would make
    the structured.__init__ -> answerer -> prompt chain a real cycle.

    Checks the import statements, not the source text: the docstring names those
    modules deliberately, as its list of consumers."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(prompt))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert not [m for m in imported if m.startswith("app.retrieval.structured")]


# --------------------------------------------------------------------------- #
# catalog_inventory_directive — what this deployment actually holds.
# --------------------------------------------------------------------------- #

def test_inventory_directive_names_present_and_absent_types(monkeypatch):
    """DEFAULT_BUNDLES is what ingestion attempts, not what it found. A type with
    no rows was still advertised, so the model set it and the query answered a
    flat zero that read like a fact about the corpus."""
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: ("article", "page")
    )
    text = prompt.catalog_inventory_directive()
    assert "currently holds only: article, page" in text
    assert "report" in text and "news" in text
    assert "leave the content type null" in text


def test_inventory_directive_is_silent_when_the_catalog_is_unreachable(monkeypatch):
    """Empty means "cannot tell". Claiming the catalog holds nothing would stop
    the model setting any content type at all."""
    monkeypatch.setattr("app.catalog.queries.available_bundles", lambda **kw: ())
    assert prompt.catalog_inventory_directive() == ""


def test_inventory_directive_is_silent_when_everything_is_present(monkeypatch):
    """Nothing to warn about — don't spend prompt tokens restating the glossary."""
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: DEFAULT_BUNDLES
    )
    assert prompt.catalog_inventory_directive() == ""


def test_inventory_directive_ignores_bundles_outside_the_registry(monkeypatch):
    """A stray bundle in the data is not a content type the model may set."""
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles",
        lambda **kw: ("article", "page", "some_unregistered_thing"),
    )
    assert "some_unregistered_thing" not in prompt.catalog_inventory_directive()


# --------------------------------------------------------------------------- #
# catalog_coverage_directive — the period this deployment actually spans.
# --------------------------------------------------------------------------- #

def _range(monkeypatch, value):
    monkeypatch.setattr("app.catalog.queries.effective_date_range", lambda **kw: value)


def test_coverage_directive_names_the_real_span(monkeypatch):
    """Today's date reads the user correctly and describes the corpus badly: an
    archive ending in 2024 answers "this year" with a zero that reads as a fact
    about the world."""
    _range(monkeypatch, ("2011-03-04", "2024-11-30"))
    text = prompt.catalog_coverage_directive()
    assert "between 2011-03-04 and 2024-11-30" in text
    assert "nothing newer than 2024-11-30" in text
    assert "scope to the part it does reach" in text


def test_coverage_directive_forbids_turning_latest_into_a_date(monkeypatch):
    """A guessed bound excludes, and the documents that answer the question are
    the first to go — ranking already prefers the newest of them."""
    _range(monkeypatch, ("2011-03-04", "2024-11-30"))
    text = prompt.catalog_coverage_directive()
    assert "leave BOTH dates null" in text
    assert "A period the user names themselves" in text


def test_coverage_directive_overrides_the_date_block_that_follows(monkeypatch):
    """It is appended before `current_date_directive`, so it has to say which of
    the two wins."""
    _range(monkeypatch, ("2011-03-04", "2024-11-30"))
    assert "override the relative-date guidance" in prompt.catalog_coverage_directive()


def test_coverage_directive_is_silent_when_the_range_is_unknown(monkeypatch):
    """A MySQL blip must fall back to today's-date reasoning, not announce that
    the catalog covers nothing."""
    for unknown in ((None, None), (None, "2024-11-30"), ("2011-03-04", None)):
        _range(monkeypatch, unknown)
        assert prompt.catalog_coverage_directive() == ""


def test_classifier_prompt_import_stays_client_free():
    """The measurable form of the above: importing the classifier's prompt text
    must not transitively load the DB / vector / LLM clients."""
    import subprocess
    import sys

    code = (
        "import sys;"
        "import app.retrieval.understanding.prompts;"
        "bad=[m for m in sys.modules if m.startswith('app.') and ("
        "'core.clients' in m or 'retrieval.structured' in m or 'catalog.queries' in m)];"
        "print(','.join(sorted(bad)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"classifier prompt import pulled in {out}"


# --------------------------------------------------------------------------- #
# CONTENT_QUESTION_RULE — the boundary of the literal reading.
# --------------------------------------------------------------------------- #

def test_the_glossary_scopes_the_literal_reading_to_catalog_questions():
    """"articles" means the `article` bundle in "how many articles in 2023"; in
    "articles where IPCC is mentioned" it is a word for "things you wrote", and
    the catalog cannot answer the question whatever type is set."""
    assert "In a question about the catalog" in prompt.BUNDLE_GLOSSARY
    assert "in a catalog question means THIS type" in prompt.BUNDLE_GLOSSARY
    assert prompt.CONTENT_QUESTION_RULE in prompt.BUNDLE_GLOSSARY


def test_the_content_rule_says_what_to_do_and_that_it_is_general():
    assert "what they SAY" in prompt.CONTENT_QUESTION_RULE
    assert "leave the content type null" in prompt.CONTENT_QUESTION_RULE
    assert "do not turn the subject into a tag" in prompt.CONTENT_QUESTION_RULE
    assert "every type here, not only articles" in prompt.CONTENT_QUESTION_RULE


def test_the_content_rule_reaches_every_prompt_that_fills_the_bundle_slot():
    """Riding inside BUNDLE_GLOSSARY, so the classifier, the parse fallback and
    the planner cannot drift apart on it."""
    for system in (
        understanding_prompts.UNDERSTANDING_SYSTEM,
        answerer._PARSE_SYSTEM,
        planner._PLANNER_SYSTEM,
    ):
        assert prompt.CONTENT_QUESTION_RULE in system


def test_the_classifier_separates_type_words_from_content():
    text = understanding_prompts.UNDERSTANDING_SYSTEM
    assert "- Type word vs content:" in text
    assert "the catalog holds no text" in text
    assert "'list the articles where IPCC is mentioned' -> [qa" in text
    assert "'reports that discuss green hydrogen' -> [qa]" in text
    # the contrast is kept beside it so the literal reading is not unlearned
    assert "'latest 5 reports under Climate Change' -> [database]" in text


def test_the_classifier_reserves_tags_for_the_named_facet():
    text = understanding_prompts.UNDERSTANDING_SYSTEM
    assert "tags (only when the user names the facet" in text
    assert "is NOT a tag" in text
