"""The grounded prompt lists the sources that mention a subject instead of refusing.

Measured 2026-09-18: with six blocks about the IPCC in context, "list down all
the articles where IPCC mentioned" was answered with the refusal string. The
model read the question as an enumeration of *articles* it could not complete,
and none of the blocks was labelled an article. Rule 3 now opens with a
carve-out for exactly that request, and rule 8 notes that listing the blocks
in hand is not a count.

Position is load-bearing and pinned here: the same instruction as the *last*
of rule 3's bullets was still refused on the same six blocks; as the *first*
bullet it produced the list.
"""
from __future__ import annotations

from app.generation import prompts

CARVE = "mention, discuss or cover X"


def test_rule_3_answers_a_documents_that_mention_x_request_from_the_blocks():
    text = prompts.GROUNDED_SYSTEM_PROMPT
    assert "List / which documents (articles, reports, news, pages, papers)" in text
    assert "is answered from the blocks in hand, never refused" in text
    assert "led by the block's title and date from its header with its citation" in text


def test_the_list_is_framed_as_the_retrieved_sources_not_the_corpus():
    text = prompts.GROUNDED_SYSTEM_PROMPT
    assert "covers the sources retrieved here, not everything that exists" in text
    assert "is neither a count nor generalising from a sample" in text


def test_the_kind_of_document_named_is_descriptive_not_a_filter():
    text = prompts.GROUNDED_SYSTEM_PROMPT
    assert "The kind of document the user named is descriptive, not a filter" in text
    assert "belongs in the list when the user said" in text


def test_the_carve_out_is_the_first_bullet_under_rule_3():
    text = prompts.GROUNDED_SYSTEM_PROMPT
    head = text.index("3. If the context does not contain the answer")
    carve = text.index(CARVE)
    next_bullet = text.index("\"Does not contain the answer\" means")
    rule_4 = text.index("4. Do not invent sources")
    assert head < carve < next_bullet < rule_4
