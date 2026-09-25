"""The answer shapes: what a list, an overview and a direct fact look like.

Measured 2026-09-25 on the live service, before these rules existed: seven
themes came back as one sentence with the one-line description the home page
gives each of them dropped; nine centres of excellence were listed inline with
"[1]" after every name; an overview ran to four paragraphs with some forty bold
phrases and no headings; and answers opened "From the available sources". The
generic "structure anything past a couple of sentences" instruction left the
model to invent a structure per answer, and it invented none.

These pin the contract that replaced it, in the prompt text itself. No network.
"""
from __future__ import annotations

from app.generation.prompts import (
    CANONICAL_MARKER,
    GROUNDED_SYSTEM_PROMPT,
    LIVE_MARKER,
    REFUSAL,
)


def _section(start: str, end: str) -> str:
    return GROUNDED_SYSTEM_PROMPT[
        GROUNDED_SYSTEM_PROMPT.index(start) : GROUNDED_SYSTEM_PROMPT.index(end)
    ]


def _style() -> str:
    return _section("Answer style:", "Example:")


def _structure() -> str:
    return _section("Answer structure (mandatory):", "Answer style:")


def test_the_style_names_each_shape_a_question_takes():
    style = _style()
    for shape in ("- Direct fact —", "- List —", "- Overview —", "- Comparison —"):
        assert shape in style, shape
    assert "Pick the one shape below that fits" in style


def test_a_list_gives_every_item_on_its_own_line_with_its_description():
    style = _style()
    assert "one bullet per item" in style
    assert "one-line description whenever the context gives one" in style
    assert "Keep every item the context lists" in style


def test_an_overview_is_sectioned_under_headings_taken_from_the_material():
    style = _style()
    assert "### Heading" in style
    assert "2-5 sections" in style
    assert "never a fixed template" in style


def test_headings_are_kept_out_of_short_answers():
    style = _style()
    assert "Headings only in an overview" in style
    # A direct fact is the shape most likely to be over-dressed.
    fact = style[style.index("- Direct fact —") : style.index("- List —")]
    assert "No headings" in fact


def test_bold_is_rationed():
    style = _style()
    assert "Bold sparingly" in style
    assert "Never several bold phrases in one sentence" in style
    # The instruction that produced the bold-everything answers.
    assert "for the points that matter most" not in GROUNDED_SYSTEM_PROMPT


def test_a_truncated_description_is_not_completed_from_memory():
    # Priority-page teasers end in "..."; finishing one is outside knowledge.
    assert "never finish it from your own knowledge" in _style()


def test_the_answer_never_talks_about_the_material_itself():
    structure = _structure()
    assert "Never write about the material itself" in structure
    for phrase in ("\"the context\"", "\"the blocks\"", "\"an attached report\""):
        assert phrase in structure, phrase


def test_the_prescribed_wordings_are_exempt_from_the_meta_wording_ban():
    # The refusal and rule 9's labelled date parts both say "available
    # sources"; the ban must not read as overruling them.
    assert "Outside the exact wordings rules 3 and 9 prescribe" in _structure()
    assert REFUSAL in GROUNDED_SYSTEM_PROMPT
    assert "not stated in the available sources" in GROUNDED_SYSTEM_PROMPT


def _rule_2() -> str:
    return _section("\n2. ", "\n3. ")


def test_citations_close_the_sentence_or_bullet_not_each_phrase():
    rule = _rule_2()
    assert "at the end of the sentence or bullet that makes it" in rule
    assert "not after each phrase or name inside it" in rule
    # The wording that produced "[1]" after each of nine names.
    assert "after every claim it supports" not in GROUNDED_SYSTEM_PROMPT


def test_a_list_from_one_block_is_cited_once_on_its_opening_sentence():
    rule = _rule_2()
    assert "cite that block once, on the list's opening sentence" in rule
    assert "an item that comes from a different block carries its own" in rule


def test_citations_stay_mandatory():
    # The sources footer and the faithfulness check both read the markers.
    assert "Cite the block number [n] for every claim" in _rule_2()


def test_attribution_by_page_name_is_licensed_only_by_the_header_markers():
    structure = _structure()
    assert f"\"{CANONICAL_MARKER}\" or \"{LIVE_MARKER}\"" in structure
    assert "attribute it to that page by name" in structure
