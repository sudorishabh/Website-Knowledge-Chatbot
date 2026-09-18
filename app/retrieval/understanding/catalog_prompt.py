"""Shared text for the three prompts that describe the document catalog's
database slots to an LLM: the intent classifier
(`app.retrieval.understanding.prompts`), the slot-extraction fallback
(`app.retrieval.structured.answerer`), and the v2 tool-calling planner
(`app.retrieval.structured.planner`). Kept in one place so the bundle list, the
vocabulary mapping, and the collective-word warning don't drift across three
independently-edited strings, each previously hand-duplicating its own version.

Each block is a self-contained sentence/paragraph meant to be appended as its
own line wherever it applies — not designed to be spliced mid-sentence. See
docs/database-retrieval-redesign.md §10.

Deliberately sits here rather than inside `app.retrieval.structured`: importing
anything from that package runs its `__init__`, which pulls in the tools, the
planner and the MySQL/Qdrant/LLM clients behind them. The intent classifier only
wants prompt *text*, so paying for the whole query layer to get it — and
creating a `structured.__init__ -> answerer -> prompt` cycle that only stays
unbroken while this module has no `structured` imports — was the wrong trade.
`app/retrieval/` has no `__init__.py`, so this module costs only its own import.
"""

from __future__ import annotations

from app.core.corpus import DEFAULT_BUNDLES

BUNDLE_LIST = ", ".join(DEFAULT_BUNDLES)

# What each content type holds and the words users say for it. This exists
# because a bare list of bundle names left the model guessing which everyday
# word maps to which type, and it guessed the collective reading: "articles" is
# a generic word for "records" in most CMSs, but here `article` is a real bundle
# (459 of 2,135 rows), so "total number of articles" answered 2135. The same
# trap sits under "reports", "papers" and "briefs", so every type users name
# gets described rather than only the one that broke.
#
# The literal reading has a boundary of its own, stated in
# CONTENT_QUESTION_RULE below: it holds for questions about the catalog. A
# question about what documents *say* ("articles where IPCC is mentioned") uses
# the type word descriptively, and the catalog cannot answer it whatever type is
# set. `query_processor._widen_content_question` enforces the same rule on the
# wording, so the prompt is the first line and not the only one.
#
# Keyed by bundle so the glossary cannot drift from the source registry —
# `tests/test_shared_prompt.py` asserts every key is a real bundle and that the
# undescribed remainder is still advertised as valid.
BUNDLE_MEANINGS: tuple[tuple[str, str], ...] = (
    ("article", "standalone articles and blog posts. Plain \"article(s)\" in a "
                "catalog question means THIS type — it is not a generic word for a "
                "record here"),
    ("feature_articles", "long-form feature pieces — only when the user says "
                         "\"feature\" or \"featured\""),
    ("news", "news items, announcements, press coverage, news stories"),
    ("events", "events, conferences, workshops, seminars, webinars"),
    ("press_release", "press releases and media releases"),
    ("research_papers", "research papers, journal papers, studies. \"paper(s)\" "
                        "means this type"),
    ("policy_brief", "policy briefs and briefing papers"),
    ("report", "reports. \"report(s)\" means THIS type, not publications at large"),
    ("completed_projects", "projects that have finished — \"completed\", "
                           "\"finished\", \"past\" projects"),
    ("ongoing_projects", "projects still running — \"ongoing\", \"current\", "
                         "\"active\" projects"),
)

_DESCRIBED = tuple(name for name, _ in BUNDLE_MEANINGS)
_OTHER_BUNDLES = tuple(b for b in DEFAULT_BUNDLES if b not in _DESCRIBED)

# The boundary of the literal reading. A type word filters the catalog; it
# cannot filter what the catalog does not hold, and the catalog holds no text.
CONTENT_QUESTION_RULE = (
    "A type word is a filter only in a question ABOUT THE CATALOG — a count, or "
    "a list by date, author or theme. When the question asks for documents by "
    "what they SAY (\"articles where IPCC is mentioned\", \"reports that discuss "
    "hydrogen\", \"news referring to COP28\"), the type word is descriptive: "
    "leave the content type null so the question spans every type, and do not "
    "turn the subject into a tag. This holds for every type here, not only "
    "articles."
)

BUNDLE_GLOSSARY = (
    "Content types, with the everyday words users use for each. In a question "
    "about the catalog, when the user's word appears here, set the content type "
    "to that bundle — do not fall back to \"no specific type\" just because the "
    "word could also be read collectively:\n"
    + "\n".join(f"- {name}: {meaning}." for name, meaning in BUNDLE_MEANINGS)
    + "\nAlso valid, rarely asked about by name: " + ", ".join(_OTHER_BUNDLES) + ".\n"
    "\"Projects\" with no completed/ongoing cue spans two of these types. Pass "
    "the user's own word through as the content type (\"projects\") — the query "
    "layer will ask which they meant. Do not pick one of the two, and do not "
    "leave the type off: picking reports one type's total as if it were every "
    "project, and omitting it counts articles and papers as projects.\n"
    + CONTENT_QUESTION_RULE
)

VOCABULARY = (
    "Vocabulary: \"items\", \"pieces\", \"entries\", \"posts\", and \"records\" "
    "mean a catalog record in general — none of them names a specific content "
    "type. A bundle's own name does name one, and so do the everyday words listed "
    "for it above (\"articles\", \"reports\", \"papers\", \"events\", \"press "
    "releases\"); users never say the word \"bundle\" itself."
)

COLLECTIVE_WORD_WARNING = (
    "A generic collective word (\"publications\", \"works\", \"output\", "
    "\"everything\") means every content type, not one — leave the content type "
    "null for these rather than collapsing onto a single bundle (do not map "
    "\"publications\" to research_papers)."
)

# resolve_entity is not advertised for tags — see
# docs/database-retrieval-redesign.md §3/§4.1: a dev-DB sample found ~237
# freeform tag terms over ~224 tagged documents, the shape of long-tail CMS
# tagging rather than a curated vocabulary fuzzy matching could usefully rank.
RESOLVE_FIRST = (
    "Names are resolved for you: pass an author or theme through exactly as the "
    "user wrote it (\"rishab negi\", \"climate\", \"env theme\") — the query layer "
    "matches it to the catalog's canonical name before filtering, and reports "
    "back which entity it used. Do NOT add a separate resolve_entity call just "
    "to look a name up first; its result cannot reach another call in the same "
    "plan. Call resolve_entity only when the user is explicitly asking which "
    "entities match a name (\"is there an author called Negi?\")."
)

OPERATIONS = (
    "count_records answers \"how many\" of one thing. aggregate_records answers "
    "\"how many per X\" or \"which X does Y appear in\" — always ONE call, never "
    "one call per value. list_records is for when the user wants to see rows or "
    "metadata, not just a number."
)

BEHAVIOR = (
    "Themes: a specific theme is a normal filter; \"how many themes are there?\" "
    "means list_themes, which lists Main themes first and Other themes in a "
    "separate section.\n"
    "Ambiguity: if resolve_entity returns more than one close match, ask the "
    "user to pick — never silently choose the top candidate.\n"
    "No fabrication: if a filter does not resolve to anything, say so explicitly "
    "(e.g. \"no author matching 'X' found\") rather than treating it as zero.\n"
    "Always name the resolved entities in the final answer, so the user can "
    "catch a wrong match."
)

def catalog_inventory_directive() -> str:
    """Prompt block naming the content types this deployment actually holds.

    The blocks above describe every *configured* bundle, which is what ingestion
    tries to fetch — not what it found. A type the catalog has no rows for is
    still advertised, so the model confidently sets it and the query answers a
    flat zero that reads like a fact about the corpus rather than about the
    vocabulary. Naming the real inventory stops the model choosing a type that
    cannot match.

    Returns "" when the inventory cannot be determined (no database, a MySQL
    blip) so the prompt falls back to the configured list rather than claiming
    the catalog is empty.

    Reads the catalog directly rather than through
    `app.retrieval.structured.entities`: importing any submodule of that package
    runs its `__init__`, which is the dependency this module exists to avoid (see
    the module docstring). The import is function-local so module import stays
    client-free, and calling per request means a new ingest needs no restart."""
    from app.catalog.queries import available_bundles

    present = tuple(b for b in available_bundles() if b in DEFAULT_BUNDLES)
    if not present:
        return ""
    absent = tuple(b for b in DEFAULT_BUNDLES if b not in present)
    if not absent:
        return ""
    return (
        "\n\n## Content types actually present\n"
        f"This catalog currently holds only: {', '.join(present)}.\n"
        f"These are configured but have NO records: {', '.join(absent)}.\n"
        "If the user asks for one of the empty types (reports, papers, news, "
        "events...), leave the content type null so the query spans what does "
        "exist — do not set a type that cannot match, and do not substitute a "
        "different one. Filters like theme, author and date still apply."
    )


def catalog_coverage_directive() -> str:
    """Prompt block naming the period this deployment's documents actually span.

    The date prompts anchor relative expressions to today, which reads the user
    correctly and describes the corpus badly: an archive whose newest document is
    from 2024 answers "what changed this year" with a confident zero, and a zero
    about a period the catalog never covered reads as a fact about the world.
    Naming the real span lets the model scope to something that can match.

    It also settles what a bare "the latest" means. Left to itself the model
    turns it into a date bound, and a guessed bound *excludes* — the documents
    that answer the question are the first to go. Ranking already prefers the
    newest of several comparable documents (see
    :mod:`app.retrieval.search.reranker`), so the correct extraction is no date at all.

    Returns "" when the range cannot be determined (no database, a MySQL blip),
    so an outage falls back to today's-date reasoning rather than claiming the
    catalog covers nothing. Same posture, and same per-request call, as
    :func:`catalog_inventory_directive`.

    Meant to be appended *before* ``current_date_directive`` — its own text says
    it overrides what follows — so the blocks that change only with the corpus
    stay in the cacheable prefix ahead of the one that changes daily."""
    from app.catalog.queries import effective_date_range

    oldest, newest = effective_date_range()
    if not oldest or not newest:
        return ""
    return (
        "\n\n## What the catalog covers\n"
        f"Every document in it was published between {oldest} and {newest}; there "
        f"is nothing newer than {newest}. Both points below override the "
        "relative-date guidance that follows.\n"
        f"- A bound past {newest} matches nothing. When the user asks about a "
        "period the catalog does not reach — including \"this year\" once the "
        "year has run past that date — scope to the part it does reach, or leave "
        "the dates null when none of it is covered.\n"
        "- \"The latest\", \"the most recent\" and \"the newest\" name no period "
        "on their own: leave BOTH dates null for them. Ranking already prefers "
        "the newest of the documents that answer the question, whereas a date "
        "guessed from the two above would exclude them.\n"
        "A period the user names themselves (\"in 2023\", \"since March\") is "
        "still theirs and still applies."
    )


FEW_SHOTS = (
    "Examples (the tool calls, not the answer). Names go in as written — the "
    "query layer canonicalizes them:\n"
    "1. \"How many posts are there from Rishabh Negi?\" -> "
    "count_records(filters={author=\"Rishabh Negi\"}).\n"
    "2. \"How many events are there?\" -> count_records(entity=\"events\").\n"
    "3. \"How many themes are there?\" -> list_themes().\n"
    "4. \"How many events are under Climate Change?\" -> "
    "count_records(entity=\"events\", filters={theme=\"Climate Change\"}).\n"
    "5. \"How many posts from rishab negi under env theme?\" -> "
    "count_records(filters={author=\"rishab negi\", theme=\"env theme\"}) — one "
    "call; the misspelling and the abbreviation are resolved for you.\n"
    "6. \"Which bundles does Rishabh Negi post in?\" -> "
    "aggregate_records(group_by=\"content_type\", filters={author=\"Rishabh Negi\"}).\n"
    "7. \"Latest 5 reports under Climate Change with their source links\" -> "
    "list_records(entity=\"report\", limit=5, filters={theme=\"Climate Change\"}).\n"
    "8. \"How many posts are tagged 'policy'?\" -> "
    "count_records(filters={tag=\"policy\"}) — tags are matched exactly.\n"
    "9. \"Is there an author called Negi?\" -> resolve_entity(\"Negi\", \"author\") "
    "— the question IS about which entities match, so resolving is the answer.\n"
    "The query layer handles the rest: a name matching several entities returns a "
    "clarification question instead of a count, and a name matching nothing "
    "returns \"no author matching 'X' found\" rather than a misleading zero. Plan "
    "the query you want; do not pre-resolve names yourself."
)
