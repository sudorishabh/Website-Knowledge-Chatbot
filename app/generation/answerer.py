"""Answer generation over retrieved context.

The grounded LLM call (buffered and streaming) plus the chit-chat reply. Faith-
fulness verification and the query pipeline that drives these live in
:mod:`app.generation.faithfulness` and :mod:`app.pipeline` respectively; this
module only turns a question + context blocks into answer text.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator, Sequence

from app.core.clients.llm import get_llm
from app.core.models.context import ContextBlock
from app.generation.prompts import (
    CHITCHAT_SYSTEM_PROMPT,
    REFUSAL,
    SHAPE_REMINDER,
    format_context_blocks,
    graph_facts_rule,
    has_graph_facts,
    format_directive,
    grounded_system_prompt,
    projects_note,
    staff_note,
    supersession_note,
    today_anchor,
)

logger = logging.getLogger(__name__)

#: The human turn. `{notes}` is the per-request notes about this evidence — the
#: supersession note, the people-listings note — each followed by a blank line,
#: or the empty string, so a context that triggers neither renders exactly as it
#: always has. Placed between the context and the question rather than in the
#: system prompt: they are facts about *this* evidence, and the end of the human
#: turn is where the model is looking when it starts to write. For the same
#: reason `{shape}` — `prompts.SHAPE_REMINDER` — follows the question.
_HUMAN_TURN = "Numbered context:\n{context}\n\n{notes}Question: {question}\n\n{shape}"


def _notes(blocks: list[ContextBlock], extra: Sequence[str] = ()) -> str:
    """The block-derived notes, then ``extra``: notes the pipeline knows and the
    blocks cannot say, such as a list it will add after the answer."""
    notes = (supersession_note(blocks), staff_note(blocks), projects_note(blocks), *extra)
    return "".join(f"{note}\n\n" for note in notes if note)

# Prior turns threaded into the answer prompt so the model can resolve follow-up
# references ("it", "that one", the original question) that the standalone query
# rewrite may not fully capture. Counts messages, not exchanges — each exchange
# is a user + assistant pair — so this is ~6 turns of memory; older turns drop
# off to bound prompt growth on long conversations.
HISTORY_MAX_TURNS = 12

# Appended to the grounded system prompt only when prior turns are present: the
# history is for interpreting the question, never a source of facts/citations.
_HISTORY_RULE = (
    "10. Earlier conversation turns appear before the numbered context for "
    "continuity — use them only to interpret the current question (e.g. resolve "
    'references like "it" or "that", or recall what the user asked earlier). '
    "Every fact and every [n] citation in your answer must still come from the "
    "numbered context below, never from an earlier turn."
)


def _history_messages(
    history: Sequence[dict[str, str]] | None, max_turns: int = HISTORY_MAX_TURNS
) -> list[Any]:
    """The recent conversation as LangChain messages for a MessagesPlaceholder.

    Roles collapse to human/ai; blank turns are dropped. Empty list on no
    history (the placeholder then renders to nothing). Returned as message
    objects, not template strings, so any braces in prior turns are never
    re-interpreted as prompt variables.
    """
    if not history:
        return []
    from langchain_core.messages import AIMessage, HumanMessage

    messages: list[Any] = []
    for turn in list(history)[-max_turns:]:
        content = turn.get("content", "")
        if not content:
            continue
        if turn.get("role") == "assistant":
            messages.append(AIMessage(content=content))
        else:
            messages.append(HumanMessage(content=content))
    return messages


def chitchat(question: str, history: list[dict[str, str]] | None) -> str:
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", CHITCHAT_SYSTEM_PROMPT),
            MessagesPlaceholder("history"),
            ("human", "{question}"),
        ]
    )
    chain = prompt | get_llm() | StrOutputParser()
    return chain.invoke(
        {"history": _history_messages(history), "question": question}
    ).strip()


def _build_system(
    answer_format: str | None,
    correction: str | None,
    *,
    has_history: bool = False,
    graph_facts: bool = False,
    plan_directive: str = "",
) -> str:
    """The grounded system prompt for this call.

    The context's composition used to select between two prompts here, because
    a context holding both website and PDF blocks was answered in two labelled
    sections. It no longer does: the blocks arrive as one ranked evidence set
    and get one answer, so there is one prompt and the format directive has one
    structure to describe.

    `graph_facts` says whether one of the blocks is the knowledge graph's
    verified-relationship block, which needs a rule of its own about reading
    validity windows. Both extra rules are numbered from 10 in the order they
    are added, continuing the list the base prompt ends at, so the model is
    never handed a rule 11 with no rule 10.
    """
    system = grounded_system_prompt()
    next_rule = 10
    if has_history:
        system += f"\n{_HISTORY_RULE}"
        next_rule += 1
    if graph_facts:
        system += f"\n{graph_facts_rule(next_rule)}"
    directive = format_directive(answer_format)
    if directive:
        system += f"\n\n{directive}"
    if correction:
        system += f"\n\n{correction}"
    if plan_directive:
        system += plan_directive
    system += today_anchor()
    return system


def generate_answer(
    question: str,
    blocks: list[ContextBlock],
    *,
    history: Sequence[dict[str, str]] | None = None,
    correction: str | None = None,
    answer_format: str | None = None,
    plan_directive: str = "",
    notes: Sequence[str] = (),
) -> str:
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    if not blocks:
        return REFUSAL

    messages = _history_messages(history)
    system = _build_system(
        answer_format,
        correction,
        has_history=bool(messages),
        graph_facts=has_graph_facts(blocks),
        plan_directive=plan_directive,
    )
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system),
            MessagesPlaceholder("history"),
            ("human", _HUMAN_TURN),
        ]
    )
    chain = prompt | get_llm(temperature=0.2) | StrOutputParser()
    return chain.invoke(
        {
            "history": messages,
            "context": format_context_blocks(blocks),
            "notes": _notes(blocks, notes),
            "question": question,
            "shape": SHAPE_REMINDER,
        }
    ).strip()


def generate_stream(
    question: str,
    blocks: list[ContextBlock],
    *,
    history: Sequence[dict[str, str]] | None = None,
    answer_format: str | None = None,
    plan_directive: str = "",
    notes: Sequence[str] = (),
) -> Iterator[str]:
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    messages = _history_messages(history)
    system = _build_system(
        answer_format,
        None,
        has_history=bool(messages),
        graph_facts=has_graph_facts(blocks),
        plan_directive=plan_directive,
    )
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system),
            MessagesPlaceholder("history"),
            ("human", _HUMAN_TURN),
        ]
    )
    chain = prompt | get_llm(temperature=0.2, streaming=True) | StrOutputParser()
    yield from chain.stream(
        {
            "history": messages,
            "context": format_context_blocks(blocks),
            "notes": _notes(blocks, notes),
            "question": question,
            "shape": SHAPE_REMINDER,
        }
    )
