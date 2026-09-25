"""The answer structure: one answer, and the reader for the one that came before.

Two things live here, and they no longer describe the same era.

`split_sections` and `strip_tags` parse the retired ``<website_answer>`` /
``<pdf_answer>`` contract. Nothing asks a model for those tags any more, but the
parser is not dead code: answers generated under the old contract are still in
the semantic cache, and one served from there arrives wrapped. Its tests are
unchanged — a malformed wrapper from a truncated stream still has to degrade to
prose rather than lose the answer.

The prompt contract below it is the current one, and it is the opposite shape:
one answer from one ranked evidence set, with source kind carrying no
precedence at all. Also here: the sources footer, which has to agree with the
citations above it. No network.
"""

from __future__ import annotations

import re

import pytest

from app.core.models.context import ContextBlock
from app.generation import answerer
from app.generation.faithfulness import FaithfulnessReport
from app.generation.prompts import (
    GROUNDED_SYSTEM_PROMPT,
    PDF_LEAD,
    PDF_TAG,
    REFUSAL,
    SUPERSEDED_MARKER,
    SUPERSEDES_MARKER,
    WEBSITE_TAG,
    format_directive,
    grounded_system_prompt,
)
from app.generation.sections import (
    PDF,
    PLAIN,
    WEBSITE,
    split_sections,
    strip_tags,
)
from app.pipeline import query_pipeline as pipe
from app.retrieval.understanding import query_processor as qp


def _web(body):
    return f"<{WEBSITE_TAG}>\n{body}\n</{WEBSITE_TAG}>"


def _pdf(body):
    return f"<{PDF_TAG}>\n{PDF_LEAD}\n{body}\n</{PDF_TAG}>"


def _kinds(answer):
    return [s.kind for s in split_sections(answer)]


# --------------------------------------------------------------------------- #
# split_sections — the shapes the prompt asks for.


def test_both_blocks_parse_in_order():
    sections = split_sections(_web("Grew 1.2 GW [1].") + "\n" + _pdf("60% commercial [2]."))
    assert [s.kind for s in sections] == [WEBSITE, PDF]
    assert sections[0].text == "Grew 1.2 GW [1]."
    assert sections[1].text == f"{PDF_LEAD}\n60% commercial [2]."


def test_website_only_yields_one_section():
    assert _kinds(_web("Grew 1.2 GW [1].")) == [WEBSITE]


def test_pdf_only_answer_is_demoted_to_plain_prose():
    # With no website block beside it the PDF block is the answer itself, so it
    # must not render as a captioned supplement wrapped around the whole reply.
    sections = split_sections(_pdf("60% commercial [2]."))
    assert [s.kind for s in sections] == [PLAIN]
    assert sections[0].text == "60% commercial [2]."


def test_demoted_pdf_block_keeps_its_body_intact():
    # Only the lead line goes; a bold run anywhere else is the model's own
    # emphasis and stays.
    sections = split_sections(_pdf("**Rooftop** grew 60% [2].\n\nAnd 1.2 GW [1]."))
    assert sections[0].text == "**Rooftop** grew 60% [2].\n\nAnd 1.2 GW [1]."


def test_demotion_survives_a_missing_or_reworded_lead():
    assert _kinds(f"<{PDF_TAG}>\n60% commercial [2].\n</{PDF_TAG}>") == [PLAIN]
    assert split_sections(
        f"<{PDF_TAG}>\n**From our documents:**\n60% commercial [2].\n</{PDF_TAG}>"
    )[0].text == "60% commercial [2]."


def test_pdf_block_beside_an_empty_website_block_is_still_demoted():
    # An empty website block is a block the model opened and had nothing for;
    # the PDF block is alone whether or not the wrapper is present.
    answer = f"<{WEBSITE_TAG}>\n\n</{WEBSITE_TAG}>" + _pdf("60% commercial [2].")
    assert _kinds(answer) == [PLAIN]


def test_demoted_pdf_block_keeps_its_place_below_a_catalog_prefix():
    answer = "We hold 4 reports.\n\n" + _pdf("60% commercial [2].")
    sections = split_sections(answer)
    assert [s.kind for s in sections] == [PLAIN, PLAIN]
    assert sections[0].text == "We hold 4 reports."
    assert sections[1].text == "60% commercial [2]."


def test_untagged_answer_is_a_single_plain_section():
    sections = split_sections("I don't have information on that.")
    assert [s.kind for s in sections] == [PLAIN]
    assert sections[0].text == "I don't have information on that."


def test_empty_answer_yields_no_sections():
    assert split_sections("") == []
    assert split_sections("   \n\n  ") == []


# --------------------------------------------------------------------------- #
# split_sections — model non-compliance and truncated streams.


def test_pdf_block_emitted_first_is_reordered_behind_website():
    # The prompt forbids this ordering; the parser enforces it regardless.
    assert _kinds(_pdf("60% commercial [2].") + _web("Grew 1.2 GW [1].")) == [
        WEBSITE,
        PDF,
    ]


def test_repeated_blocks_of_one_kind_merge():
    sections = split_sections(_web("First [1].") + _web("Second [2]."))
    assert [s.kind for s in sections] == [WEBSITE]
    assert sections[0].text == "First [1].\n\nSecond [2]."


def test_unterminated_block_runs_to_the_end():
    sections = split_sections(f"<{WEBSITE_TAG}>\nGrew 1.2 GW [1].")
    assert [s.kind for s in sections] == [WEBSITE]
    assert sections[0].text == "Grew 1.2 GW [1]."


def test_empty_block_is_dropped_rather_than_rendered():
    assert _kinds(_web("Grew 1.2 GW [1].") + f"<{PDF_TAG}>\n\n</{PDF_TAG}>") == [WEBSITE]


def test_unpaired_close_tag_never_reaches_the_output():
    sections = split_sections(f"Plain text.</{PDF_TAG}>")
    assert [s.kind for s in sections] == [PLAIN]
    assert sections[0].text == "Plain text."


def test_tag_casing_and_inner_whitespace_are_tolerated():
    answer = f"<{WEBSITE_TAG.upper()} >\nGrew 1.2 GW [1].\n</{WEBSITE_TAG} >"
    assert _kinds(answer) == [WEBSITE]


# --------------------------------------------------------------------------- #
# split_sections — the refusal is a whole answer, never a part of one. A model
# handed a category with nothing useful in it is told to drop that block; when
# it apologizes in the block instead, the apology reads as a denial of the
# answer beside it and keeps the PDF block from standing on its own.


def test_refusal_in_the_website_block_is_dropped_beside_a_real_answer():
    # The observed failure: two off-topic website blocks retrieved, so the
    # model filled the website half with the refusal and answered from the PDFs.
    answer = _web(REFUSAL) + _pdf("60% commercial [2].")
    sections = split_sections(answer)
    assert [s.kind for s in sections] == [PLAIN]
    assert sections[0].text == "60% commercial [2]."


def test_refusal_in_the_pdf_block_is_dropped_beside_a_real_answer():
    sections = split_sections(_web("Grew 1.2 GW [1].") + _pdf(REFUSAL))
    assert [s.kind for s in sections] == [WEBSITE]
    assert sections[0].text == "Grew 1.2 GW [1]."


def test_untagged_refusal_is_dropped_beside_a_real_answer():
    assert _kinds(f"{REFUSAL}\n\n" + _web("Grew 1.2 GW [1].")) == [WEBSITE]


def test_refusal_in_every_block_is_answered_once_and_unwrapped():
    sections = split_sections(_web(REFUSAL) + _pdf(REFUSAL))
    assert [s.kind for s in sections] == [PLAIN]
    assert sections[0].text == REFUSAL


def test_refusal_alone_survives_as_the_answer():
    for answer in (REFUSAL, _web(REFUSAL), _pdf(REFUSAL)):
        assert _kinds(answer) == [PLAIN], answer
        assert split_sections(answer)[0].text == REFUSAL


@pytest.mark.parametrize(
    "variant",
    [
        REFUSAL.rstrip("."),
        f"**{REFUSAL}**",
        f'"{REFUSAL}"',
        REFUSAL.replace("'", "’"),
        f"  {REFUSAL.upper()}  ",
    ],
)
def test_refusal_is_recognized_through_surface_variation(variant):
    assert _kinds(_web(variant) + _pdf("60% commercial [2].")) == [PLAIN]


def test_an_answer_that_merely_reports_a_gap_is_not_a_refusal():
    # Matched by equality, not substring: this one carries content and stays.
    said = f"{REFUSAL} The 2019 report covers Assam only [1]."
    sections = split_sections(_web(said) + _pdf("60% commercial [2]."))
    assert [s.kind for s in sections] == [WEBSITE, PDF]


# --------------------------------------------------------------------------- #
# split_sections — untagged text keeps its position around the blocks.


def test_catalog_prefix_stays_above_the_blocks():
    answer = "We hold 4 reports.\n\n" + _web("Grew 1.2 GW [1].")
    sections = split_sections(answer)
    assert [s.kind for s in sections] == [PLAIN, WEBSITE]
    assert sections[0].text == "We hold 4 reports."


def test_trailing_remark_stays_below_the_blocks():
    sections = split_sections(_web("Grew 1.2 GW [1].") + "\nAsk me for more.")
    assert [s.kind for s in sections] == [WEBSITE, PLAIN]
    assert sections[-1].text == "Ask me for more."


# --------------------------------------------------------------------------- #
# strip_tags — what the faithfulness and numeric checks see.


def test_strip_tags_removes_wrappers_and_keeps_content():
    stripped = strip_tags(_web("Grew 1.2 GW [1].") + "\n" + _pdf("60% commercial [2]."))
    assert WEBSITE_TAG not in stripped
    assert PDF_TAG not in stripped
    assert "Grew 1.2 GW [1]." in stripped
    assert "60% commercial [2]." in stripped


def test_strip_tags_collapses_the_gaps_left_by_removal():
    assert "\n\n\n" not in strip_tags(_web("A [1].") + "\n\n" + _pdf("B [2]."))


def test_strip_tags_leaves_an_untagged_answer_alone():
    assert strip_tags("I don't have information on that.") == (
        "I don't have information on that."
    )


# --------------------------------------------------------------------------- #
# The prompt contract. Asserted structurally rather than by prose match, so
# rewording the rules stays free while the demonstrated shape stays pinned.
#
# Most of what stood here is gone, with the thing it pinned. The prompt came in
# two variants: a mixed context was answered in a <website_answer> block followed
# by a <pdf_answer> block captioned "From our documents", in that order "whatever
# the relevance scores say", and a single-kind context got a second prompt with
# the structure stripped out. The split was categorical rather than evidential.
# Asked who the director general is, the model was handed a 2020 web page and a
# 2023 document correcting it, and rule 5 — "Website sources are authoritative"
# — told it to take the web page and file the correction in the aside below.


def _rule(n: int, until: str) -> str:
    return GROUNDED_SYSTEM_PROMPT[
        GROUNDED_SYSTEM_PROMPT.index(f"\n{n}. ") : GROUNDED_SYSTEM_PROMPT.index(until)
    ]


def _example() -> str:
    return GROUNDED_SYSTEM_PROMPT[GROUNDED_SYSTEM_PROMPT.index("Example:") :]


def test_the_prompt_asks_for_one_answer_organised_by_topic_not_source():
    assert "one answer from all the blocks together" in GROUNDED_SYSTEM_PROMPT
    assert "Organise it by topic, never by source" in GROUNDED_SYSTEM_PROMPT
    # "continuous" read as "prose" and flattened overviews into paragraphs.
    assert "continuous" not in GROUNDED_SYSTEM_PROMPT


def test_the_prompt_never_mentions_the_retired_block_structure():
    for token in (WEBSITE_TAG, PDF_TAG, PDF_LEAD):
        assert token not in GROUNDED_SYSTEM_PROMPT, token


def test_the_prompt_forbids_ranking_evidence_by_source_kind():
    """Rule 5, which used to be the reason the older page won."""
    rule = _rule(5, "\n7. ")
    assert "never on what kind of source" in rule
    assert "does not outrank" in rule
    assert "authoritative" not in rule


def test_citations_may_come_from_any_source_kind_in_one_answer():
    assert "whichever kind of source it came" in _rule(6, "\n7. ")


def test_the_prompt_states_the_structure_before_demonstrating_it():
    assert GROUNDED_SYSTEM_PROMPT.index("Answer structure") < (
        GROUNDED_SYSTEM_PROMPT.index("Example:")
    )


def test_the_worked_examples_parse_as_plain_text():
    # The demonstrated answers are what the model copies, so they have to read
    # as untagged wholes to the same parser the frontend mirrors.
    assert _kinds(_example()) == [PLAIN]


def test_a_worked_example_answers_a_mixed_context_as_one_passage():
    # The case the model used to split. Demonstrating it answered whole is worth
    # more than describing it, so the example context carries both kinds.
    example = _example()
    assert "(website ·" in example and "(pdf ·" in example


def test_a_worked_example_demonstrates_the_dated_title_rule():
    # Rule 9's appositive clause is the one a model most readily ignores: a role
    # beside a name reads as a standing label rather than a dated claim.
    example = _example()
    assert "Director General, Org One" in example
    assert "was previously" in example


def test_format_directives_name_no_wrappers():
    for fmt in ("list", "table", "summary", "detailed", "timeline"):
        directive = format_directive(fmt)
        assert WEBSITE_TAG not in directive, fmt
        assert PDF_TAG not in directive, fmt
        assert "this shape wins" in directive, fmt
    # The default path stays lean: no directive, so no scope note either.
    assert format_directive("default") == ""


def test_grounded_system_prompt_is_the_only_prompt():
    assert grounded_system_prompt() == GROUNDED_SYSTEM_PROMPT


def test_the_prompt_keeps_the_rule_numbering():
    # app.generation.answerer appends the history rule as "10.".
    for n in range(1, 10):
        assert f"\n{n}. " in f"\n{GROUNDED_SYSTEM_PROMPT}", n
    assert "\n10. " not in GROUNDED_SYSTEM_PROMPT


# --------------------------------------------------------------------------- #
# Rule 9: which of two blocks describes the present.


def test_the_conflict_rule_settles_on_the_publication_date():
    # The date reaches the model only through the block header (_source_hint),
    # so the rule has to point at it rather than at the payload field name.
    rule = _rule(9, "\nAnswer structure")
    assert "published" in rule and "later" in rule


def test_the_conflict_rule_forbids_inventing_a_missing_date():
    # Most PDFs carry no effective_start_date, so an undated block is the common
    # case, not the exception — reading one as "the current version" is the
    # failure.
    assert "no date shown" in _rule(9, "\nAnswer structure")


def test_the_conflict_rule_covers_a_title_written_beside_a_name():
    """The reported failure. "Statement by Dr X, Director General, TERI" on a
    2020 page carries no verb and no time word, so the time-bound-wording clause
    above had nothing to catch and the model rendered a three-year-old role as a
    present-tense fact."""
    rule = _rule(9, "\nAnswer structure")
    assert "beside a name" in rule
    assert "past tense" in rule


def test_the_conflict_rule_reads_the_supersession_markers():
    rule = _rule(9, "\nAnswer structure")
    assert SUPERSEDED_MARKER in rule
    assert SUPERSEDES_MARKER in rule


# --------------------------------------------------------------------------- #
# Style, and the depth the examples demonstrate.


def test_prompt_states_the_style_between_the_structure_and_the_example():
    structure = GROUNDED_SYSTEM_PROMPT.index("Answer structure")
    style = GROUNDED_SYSTEM_PROMPT.index("Answer style:")
    assert structure < style < GROUNDED_SYSTEM_PROMPT.index("Example:")


def test_prompt_guards_added_depth_against_padding():
    # Asking a grounded model for fuller answers invites padding; the guard that
    # ties every added sentence back to the context has to survive rewording.
    style = GROUNDED_SYSTEM_PROMPT[GROUNDED_SYSTEM_PROMPT.index("Answer style:") :]
    assert "never from padding" in style


def test_prompt_states_a_length_floor_and_not_only_a_ceiling():
    # "Be thorough" on its own lost to the model's pull toward one-line replies,
    # so the style names a concrete target and rules out the bare-clause answer.
    style = GROUNDED_SYSTEM_PROMPT[GROUNDED_SYSTEM_PROMPT.index("Answer style:") :]
    assert re.search(r"\d+-\d+ sentences", style)
    assert "never a bare clause" in style


def _demonstrated_answers(prompt: str) -> list[str]:
    """Each answer body the worked examples demonstrate.

    Every example is untagged now, so this is the passage after each "Answer:"
    — where it used to have to unwrap block runs first.
    """
    example = prompt[prompt.index("Example:") :]
    return [
        part.split("\n\nExample")[0].strip()
        for part in example.split("Answer:\n")[1:]
    ]


def test_worked_examples_demonstrate_the_depth_the_style_asks_for():
    # The exemplar outweighs the described style for 4o-mini, so a one-sentence
    # demonstration teaches one-sentence answers however thorough the style
    # section above it reads. Cited sentences specifically: the depth being
    # taught has to be the grounded kind. A bullet is a unit as much as a
    # sentence is, so line breaks split too.
    bodies = _demonstrated_answers(GROUNDED_SYSTEM_PROMPT)
    assert bodies
    for body in bodies:
        cited = [
            part
            for part in re.split(r"(?<=[.!?])\s+|\n", body)
            if re.search(r"\[\d+\]", part)
        ]
        assert len(cited) >= 2, body


# --------------------------------------------------------------------------- #
# The context's composition no longer selects anything.
#
# `has_mixed_sources` read the blocks and handed a mixed context the two-block
# prompt; `_build_system` took the verdict as `mixed`. Both are gone, and the
# signature is the first half of the proof — there is nothing left to pass. The
# second half is that no combination of the arguments it still takes can put the
# retired structure back into the prompt.


def test_no_argument_combination_reintroduces_the_block_structure():
    notes = (None, FaithfulnessReport(faithful=False, unsupported=["1.2 GW"])
             .correction_note())
    for fmt in (None, "default", "table", "list", "summary", "detailed", "timeline"):
        for note in notes:
            for history in (False, True):
                system = answerer._build_system(
                    fmt, note, has_history=history, graph_facts=True,
                )
                for token in (WEBSITE_TAG, PDF_TAG, PDF_LEAD):
                    assert token not in system, (fmt, bool(note), history, token)


def test_correction_note_names_no_structure():
    # A retry runs through the same prompt as the draft it replaces, so a note
    # naming blocks would push a rewrite back into a shape nothing else asks for.
    note = FaithfulnessReport(faithful=False, unsupported=["1.2 GW"]).correction_note()
    for token in (WEBSITE_TAG, PDF_TAG, PDF_LEAD, "answer-block"):
        assert token not in note, token


def test_history_rule_continues_the_numbering():
    system = answerer._build_system(None, None, has_history=True)
    assert "\n10. " in system
    assert "\n11. " not in system


# --------------------------------------------------------------------------- #
# The sources footer lists what the answer cited, not everything retrieved.


def _generation():
    blocks = [
        ContextBlock(
            n=1, text="The programme added 1.2 GW in 2023.",
            payload={"source_type": "website", "title": "Rooftop Push"},
        ),
        ContextBlock(
            n=2, text="Commercial installations were 60% of capacity.",
            payload={"source_type": "pdf", "title": "Annual Report"},
        ),
    ]
    return pipe._Generation(
        pq=qp.ProcessedQuery(
            understanding=None, original="q", search_query="q", intent="qa"
        ),
        blocks=blocks, query_vector=[0.0], top_k=6,
    )


def _cited(answer):
    return [c["n"] for c in pipe._assemble(answer, _generation())["citations"]]


def test_footer_lists_both_sources_when_both_blocks_cite():
    assert _cited(_web("Added 1.2 GW [1].") + _pdf("60% commercial [2].")) == [1, 2]


def test_dropped_pdf_block_leaves_no_pdf_chip():
    # The rule that makes the footer agree with the answer: a PDF the model
    # rightly left out must not resurface as a source under it.
    out = pipe._assemble(_web("Added 1.2 GW [1]."), _generation())
    assert [c["type"] for c in out["citations"]] == ["website"]


def test_pdf_only_answer_lists_the_pdf_alone():
    assert _cited(_pdf("60% commercial [2].")) == [2]


def test_uncited_answer_keeps_every_source():
    assert _cited(_web("The programme added capacity.")) == [1, 2]


def test_used_chunks_still_counts_what_retrieval_supplied():
    out = pipe._assemble(_web("Added 1.2 GW [1]."), _generation())
    assert out["used_chunks"] == 2


def test_tags_do_not_register_as_unverified_figures():
    out = pipe._assemble(_web("Added 1.2 GW in 2023 [1]."), _generation())
    assert out["numeric_mismatch"] is False
