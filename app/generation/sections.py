"""A reader for the retired two-block answer structure.

Answers used to come back wrapped in ``<website_answer>`` / ``<pdf_answer>``
tags, so the frontend could style website-sourced content as the answer and
PDF-sourced content as a captioned supplement below it. Nothing asks a model for
that shape any more: website, PDF and graph evidence is ranked as one set and
answered once, so a live answer arrives untagged and every function here falls
through to its plain-text branch.

It is kept because the semantic cache holds answers generated under the old
contract, and one served from there still arrives wrapped — unparsed, it would
render its tags as literal text. The frontend keeps a mirror of these rules for
the same reason (``ui/script.js``); retire the two together once the cache has
turned over.

Parsing stays tolerant. The tags came from a model, not from code, and a stream
can be cut mid-tag — so a malformed or absent wrapper degrades to plain text
rather than losing the answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.generation.prompts import PDF_LABEL, PDF_TAG, REFUSAL, WEBSITE_TAG

WEBSITE = "website"
PDF = "pdf"
# Text outside any block: a refusal, a chit-chat reply, a scoped summary, or the
# deterministic catalog prefix that rides above a combined answer.
PLAIN = "plain"

# A block body: to its matching close tag, or to the end of a truncated answer.
_BLOCK = re.compile(
    rf"<({WEBSITE_TAG}|{PDF_TAG})\s*>(.*?)(?:</\1\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# Stray wrappers left over after the blocks are consumed (an unpaired close tag,
# a nested repeat) are presentation debris — never shown, never verified.
_ANY_TAG = re.compile(rf"</?(?:{WEBSITE_TAG}|{PDF_TAG})\s*>", re.IGNORECASE)
# The bold lead opening a PDF block. Read only when the block is demoted: the
# label it carries is a caption for a supplement, and there is nothing to
# supplement. A trailing colon lands inside or outside the bold depending on the
# model, so both are matched.
_PDF_LEAD_LINE = re.compile(
    rf"^\s*\*\*\s*{re.escape(PDF_LABEL)}\s*:?\s*\*\*\s*:?\s*(?:\r?\n|$)",
    re.IGNORECASE,
)


# Emphasis and quote marks a model may wrap the refusal in, plus the smart
# apostrophe it may spell it with — surface variation, not different text.
_TRIM_CHARS = "*_\"' "
_SMART_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


@dataclass(frozen=True)
class Section:
    kind: str  # WEBSITE, PDF, or PLAIN
    text: str


def _clean(text: str) -> str:
    """Tag-free text with the blank-line runs left behind by removal collapsed."""
    return re.sub(r"\n{3,}", "\n\n", _ANY_TAG.sub("", text)).strip()


def _without_lead(text: str) -> str:
    """The block body without the caption line a PDF block opens with. The
    caption is presentation: the frontend emits its own when the block keeps its
    container, and nothing should carry it once the block loses one."""
    return _PDF_LEAD_LINE.sub("", text).strip()


def _normalize(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text.translate(_SMART_QUOTES)).strip()
    return collapsed.strip(_TRIM_CHARS).rstrip(".").strip().casefold()


_REFUSAL_NORM = _normalize(REFUSAL)


def _is_refusal(text: str) -> bool:
    """True when the text is the refusal and nothing besides.

    The PDF lead is a caption rather than content, so a block holding the
    caption and then the refusal is still only a refusal. Deliberately an
    equality test and not a substring one: an answer that merely says what it
    could not find still carries content, and must survive.
    """
    return _normalize(_without_lead(text)) == _REFUSAL_NORM


def strip_tags(answer: str) -> str:
    """The answer without its block wrappers, for the faithfulness and numeric
    checks — tag text is presentation, not a claim to verify."""
    return _clean(answer)


def split_sections(answer: str) -> list[Section]:
    """Parse an answer into the sections to render, in display order.

    Website content always precedes PDF content, whichever order the model
    emitted the blocks in, and repeated blocks of one kind merge into a single
    section. Untagged text keeps its position relative to the blocks, so the
    catalog prefix stays on top and trailing remarks stay at the bottom.
    Sections that clean up to nothing are dropped, so an empty block the model
    emitted against instructions never reaches the frontend as a bare
    container.

    The refusal is a whole answer, never a part of one: a block holding nothing
    but the refusal is dropped when any other section carries content, and when
    none does the refusal is returned once as plain text. A model with nothing
    to say for one category is supposed to omit that block; when it apologizes
    in the block instead, the apology otherwise reads as a denial of the answer
    sitting right beside it.

    A PDF block with no website block beside it is then demoted to plain prose:
    the split exists to set a supplement apart from the answer it supplements,
    and with nothing above it the block *is* the answer. Left as a PDF section
    it would render as a captioned aside wrapped around the whole reply.
    """
    leading: list[str] = []
    trailing: list[str] = []
    grouped: dict[str, list[str]] = {WEBSITE: [], PDF: []}
    cursor = 0
    seen_block = False

    for match in _BLOCK.finditer(answer):
        (trailing if seen_block else leading).append(answer[cursor : match.start()])
        kind = WEBSITE if match.group(1).lower() == WEBSITE_TAG else PDF
        grouped[kind].append(match.group(2))
        cursor = match.end()
        seen_block = True
    (trailing if seen_block else leading).append(answer[cursor:])

    leading_text = _clean("\n\n".join(leading))
    trailing_text = _clean("\n\n".join(trailing))
    website_text = _clean("\n\n".join(grouped[WEBSITE]))
    pdf_text = _clean("\n\n".join(grouped[PDF]))

    parts = (leading_text, website_text, pdf_text, trailing_text)
    if any(text and not _is_refusal(text) for text in parts):
        # Something real was found, so every refusal beside it is a block the
        # model filled rather than dropped. Left in, it contradicts the content
        # next to it and counts as a website answer the PDF block must defer to.
        leading_text, website_text, pdf_text, trailing_text = (
            "" if _is_refusal(text) else text for text in parts
        )
    else:
        # Refusals and blanks only: the refusal is the whole answer, said once
        # and unwrapped, whichever block the model happened to put it in.
        refused = _without_lead(next((text for text in parts if text), ""))
        return [Section(PLAIN, refused)] if refused else []

    pdf_kind = PDF
    if not website_text:
        # Standing alone, the PDF block is the answer: demote it, and drop the
        # lead-in that only read as a label under the caption it no longer gets.
        pdf_kind = PLAIN
        pdf_text = _without_lead(pdf_text)

    sections = []
    for kind, text in (
        (PLAIN, leading_text),
        (WEBSITE, website_text),
        (pdf_kind, pdf_text),
        (PLAIN, trailing_text),
    ):
        if text:
            sections.append(Section(kind, text))
    return sections
