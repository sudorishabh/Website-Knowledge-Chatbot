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
    format_directive,
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


def _demonstrated(label: str) -> str:
    """The answer body of the worked example headed ``label``."""
    example = GROUNDED_SYSTEM_PROMPT[GROUNDED_SYSTEM_PROMPT.index(label) :]
    return example.split("Answer:\n", 1)[1].split("\n\nExample", 1)[0]


def test_the_first_example_demonstrates_an_overview_in_headed_sections():
    # The model copies the exemplar's shape; the one-paragraph exemplar that
    # stood here is the shape every answer came back in.
    body = _demonstrated("Example:\n")
    assert body.count("\n### ") >= 2
    assert body.startswith("According to Org One's")
    assert "\n- " in body


def test_the_list_example_keeps_descriptions_and_cites_its_list_once():
    body = _demonstrated("Example (a list):")
    items = [line for line in body.splitlines() if line.startswith("- ")]
    assert len(items) == 3
    assert all(line.startswith("- **") and " — " in line for line in items)
    # One citation on the opening sentence covers the whole list; only the
    # item described from a different block carries its own.
    assert body.splitlines()[0].rstrip().endswith("[1]:")
    assert [line for line in items if "[" in line] == [items[1]]
    # The truncated teaser is cut at its last complete phrase, not finished.
    assert "..." not in body


def test_the_examples_are_invented():
    example = GROUNDED_SYSTEM_PROMPT[GROUNDED_SYSTEM_PROMPT.index("Example:") :]
    assert "Org One" in example
    assert "TERI" not in example


def test_the_list_directive_follows_the_list_shape_and_rule_2():
    directive = format_directive("list")
    assert "the List shape above" in directive
    assert "one opening sentence" in directive
    assert "once on the opening sentence when every item comes from one block" in directive
    # The directive wins a conflict with the style, so the old wording would win.
    assert "no preamble" not in directive
    assert "leads with its claim and its citation" not in directive


def test_the_detailed_directive_is_the_overview_at_its_fullest():
    directive = format_directive("detailed")
    assert "the Overview shape above" in directive
    assert "### sections" in directive


def test_a_summary_stays_unsectioned():
    assert "no headings" in format_directive("summary")


def test_page_layout_is_not_narrated():
    # Measured: "(listed under "New in Green Shipping")" after seven bullets.
    structure = _structure()
    assert "Nor say where on a page something appears" in structure
    assert "give the item, not its position" in structure


def test_an_item_is_named_once_and_the_opening_is_not_restated():
    style = _style()
    assert "Name each item once, even when several blocks list it" in style
    assert "never restate the opening as a section" in style


def test_the_shape_reminder_follows_the_question(monkeypatch):
    """The style sits mid-way through a long system prompt, and the small model
    ignored it on most of a live run; the reminder goes where it is looking."""
    from langchain_core.runnables import RunnableLambda

    from app.core.models.context import ContextBlock
    from app.generation import answerer
    from app.generation.prompts import SHAPE_REMINDER

    seen: dict = {}

    def capture(prompt_value):
        seen["human"] = prompt_value.to_messages()[-1].content
        return "an answer"

    monkeypatch.setattr(answerer, "get_llm", lambda **_: RunnableLambda(capture))
    answerer.generate_answer(
        "a question", [ContextBlock(n=1, text="x", payload={"source_type": "website"})]
    )
    assert seen["human"].endswith(f"Question: a question\n\n{SHAPE_REMINDER}")


def test_the_shape_reminder_defers_to_the_wording_rules():
    from app.generation.prompts import SHAPE_REMINDER

    assert "or the shape requested above, if one was" in SHAPE_REMINDER
    assert "rule 9's labelled parts" in SHAPE_REMINDER
    # Naming the refusal here turned a publication-date question into one.
    assert "refusal" not in SHAPE_REMINDER
    assert "TERI" not in SHAPE_REMINDER


def test_the_shape_reminder_opens_with_grounding():
    """Measured on identical blocks: with the reminder last and no word about
    grounding in it, "what is the capital of France" was answered "Paris" from
    the model's own knowledge in two of three runs (one of three on main).
    Leading with rule 1 and rule 3 took it to none of four."""
    from app.generation.prompts import SHAPE_REMINDER

    assert SHAPE_REMINDER.startswith(
        "Every statement comes only from the numbered context above, never from "
        "your own knowledge"
    )
    assert "the whole answer is rule 3's exact reply" in SHAPE_REMINDER


def test_a_publication_date_question_is_kept_out_of_the_direct_fact_shape():
    """Measured on identical blocks: "answer first" led one run to "The Annual
    Report 2024-25 was published in 2025" — the page date, as rule 9 forbids.
    The shape and the reminder both hand the question back to rule 9."""
    from app.generation.prompts import SHAPE_REMINDER

    assert "report edition; page publication date; report publication date" in SHAPE_REMINDER
    assert "never a page date given as the day the document was published" in SHAPE_REMINDER
    fact = _style()[_style().index("- Direct fact —") : _style().index("- List —")]
    assert "rule 9's labelled parts are that answer" in fact


def test_an_undescribed_item_is_its_name_alone():
    # Measured: nine centres each followed by "(no description provided in the
    # available sources)" once the reminder asked for a description per item.
    from app.generation.prompts import SHAPE_REMINDER

    assert "or the name alone when it gives none" in SHAPE_REMINDER
    assert "never add a note that its description is missing" in _style()


def test_a_list_opening_does_not_name_the_items():
    # Measured: all seven themes in the opening sentence, then again as bullets.
    from app.generation.prompts import SHAPE_REMINDER

    assert "not naming the items" in SHAPE_REMINDER
    assert "without naming the items" in _style()


def test_sections_are_headed_by_what_they_hold_not_the_page_section():
    from app.generation.prompts import SHAPE_REMINDER

    assert "never by the page section they came from" in SHAPE_REMINDER
    assert "never the name of the page section they appeared in" in _style()


def test_a_broad_question_is_answered_on_its_named_reading():
    """Measured: "TERI top researchers" with the listings in hand opened "The
    sources do not rank TERI researchers as 'top'" instead of answering."""
    from app.generation.prompts import SHAPE_REMINDER

    structure = _structure()
    assert "answer its most likely reading and name that reading" in structure
    assert "Never ask back instead" in structure
    assert "never open by saying the sources do not define the word" in structure
    assert "A broad question gets its most likely reading" in SHAPE_REMINDER
    # Measured: "most likely reading" alone was read as "pick one set" and
    # dropped a whole listing that fitted.
    assert "never just the first set that fits" in structure
    assert "answered from every list or page in the context that fits" in SHAPE_REMINDER


def test_a_long_list_is_grouped_by_what_the_context_states():
    """Measured: the same question came back as one flat list of 42 people."""
    from app.generation.prompts import SHAPE_REMINDER

    style = _style()
    assert "more than about 12 items is split into 2-4 groups" in style
    assert "something the context states for every item" in style
    assert "Headings only in an overview or a grouped long list" in style
    assert "more than about 12 items grouped under ### headings" in SHAPE_REMINDER


def _listing(n, title, reason="staff"):
    from app.core.models.context import PRIORITY_PAGE_KIND, ContextBlock

    return ContextBlock(n=n, text=f"{title}\nDr A\nDirector", payload={
        "kind": PRIORITY_PAGE_KIND, "source_type": "website", "title": title,
        "priority_reason": reason,
    })


def test_several_people_listings_are_named_beside_the_question():
    """Measured: with both listings in context, three answers in four named
    only the Distinguished Fellows and dropped the research directors."""
    from app.generation.prompts import staff_note

    note = staff_note([_listing(1, "Distinguished Fellows"),
                       _listing(2, "Committee of Directors")])
    assert "[1] Distinguished Fellows, [2] Committee of Directors" in note
    assert "draws on each of them" in note
    assert "rather than choosing one listing as the answer" in note


def test_the_note_is_absent_unless_two_listings_were_read_for_people():
    from app.generation.prompts import staff_note

    assert staff_note([_listing(1, "Distinguished Fellows")]) == ""
    # A listing named outright ("the governing council") is not this case.
    assert staff_note([_listing(1, "A", reason="name"), _listing(2, "B", reason="name")]) == ""
    # One listing split across two blocks is still one listing.
    assert staff_note([_listing(1, "Committee of Directors"),
                       _listing(2, "Committee of Directors")]) == ""


def test_the_staff_reason_matches_retrieval():
    from app.generation.prompts import STAFF_REASON
    from app.retrieval.priority.match import STAFF

    assert STAFF_REASON == STAFF


def test_the_note_reaches_the_human_turn_before_the_question(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    from app.generation import answerer

    seen: dict = {}

    def capture(prompt_value):
        seen["human"] = prompt_value.to_messages()[-1].content
        return "an answer"

    monkeypatch.setattr(answerer, "get_llm", lambda **_: RunnableLambda(capture))
    answerer.generate_answer("TERI top researchers", [
        _listing(1, "Distinguished Fellows"), _listing(2, "Committee of Directors")])
    human = seen["human"]
    assert human.index("People listings in this context") < human.index("Question: TERI top")


def test_a_list_of_current_people_leaves_out_the_former_ones():
    from app.generation.prompts import SHAPE_REMINDER

    assert "leave out anyone or anything the context marks as former" in _style()
    assert "no one the context marks as former" in SHAPE_REMINDER


def test_attribution_by_page_name_is_licensed_only_by_the_header_markers():
    structure = _structure()
    assert f"\"{CANONICAL_MARKER}\" or \"{LIVE_MARKER}\"" in structure
    assert "attribute it to that page by name" in structure
