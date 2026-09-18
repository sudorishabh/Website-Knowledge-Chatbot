"""When a content-type word describes rather than filters.

The problem
-----------
The query-understanding model fills the catalog slots, and the glossary it
reads (`catalog_prompt.BUNDLE_MEANINGS`) tells it that a type word the user
says — "articles", "reports", "news", "papers", "projects" — names that
bundle. That is right for a question *about the catalog*: "how many articles
in 2023" counts the `article` bundle, and reading the word collectively once
answered 2,135 for 461.

It is wrong for a question about what documents *say*. "List the articles
where IPCC is mentioned" was planned as ``bundle=article``; the catalog cannot
look inside a document, so the plan matched nothing, and the fall-through to
semantic search still carried the type word into the title leg and refused.
Measured: 22 documents carry IPCC in their title alone — events, news, press
releases — and not one is an `article`. The user meant "articles" the way
people say it, as a word for "things you have written".

The rule
--------
A type word is a filter only in a catalog-shaped question. Once the question
conditions on the text — mentions, discusses, refers to, contains — the type
word is descriptive, the question is a content question, and it spans every
content type. This holds for every bundle, not one: the trigger is the shape
of the question, and what gets widened is whichever type the model set.

Decided from the wording rather than by the model, for the same reason
`query_processor._corrected_intent` is: with one analysis vote a prompt can
only move which questions the model gets wrong, while a rule on the question's
own words holds every time.

Deliberately narrow. Topic words ("about X", "on X", "regarding X") are not
predicates: the structured layer already accounts for them through
`app.retrieval.structured.topic`, and a type word beside a topic ("news about
COP28") is often meant literally. Only a verb about the text itself widens.
"""
from __future__ import annotations

import re

# Verbs that make a question about what the documents say, in the forms users
# write them: "where IPCC is mentioned", "reports that discuss hydrogen", "news
# referring to COP28", "papers containing the word adaptation", "where AR6
# appears". Instruction verbs the user aims at the assistant ("cite your
# sources", "quote the figure") are left out on purpose — they say nothing
# about the documents wanted.
_CONTENT_PREDICATE = re.compile(
    r"\b(?:"
    r"mention(?:s|ed|ing)?"
    r"|discuss(?:es|ed|ing)?"
    r"|referenc(?:e|es|ed|ing)"
    r"|refer(?:s|red|ring)?\s+to"
    r"|talk(?:s|ed|ing)?\s+about"
    r"|contain(?:s|ed|ing)?"
    r"|appear(?:s|ed|ing)?"
    r")\b",
    re.IGNORECASE,
)


def conditions_on_text(question: str) -> bool:
    """Whether the question asks for documents by what they say.

    True for "list the articles where IPCC is mentioned" and "which reports
    discuss hydrogen"; false for "how many articles in 2023", "list all news
    since March" and "articles about IPCC" (a topic, not a predicate).
    """
    return bool(_CONTENT_PREDICATE.search(question or ""))
