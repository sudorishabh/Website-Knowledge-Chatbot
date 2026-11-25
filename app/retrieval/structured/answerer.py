"""Structured (database-intent) answer adapter.

Answers catalog questions by delegating to the Database Planner + tools
(`app.retrieval.structured`). This module holds only:

- the LLM parse fallback (`parse_structured` / `StructuredQuery`) for when no
  usable analysis was supplied;
- `answer_structured`, the thin adapter the query pipeline calls.

The catalog operations, filters, entity handling, rendering, and the lookup->QA
chaining (`resolve_lookup_chain`) live in `app.retrieval.structured` (see
docs/database-tool-registry.md). The name is source-agnostic on purpose: this
adapter has no Drupal-specific logic — only the underlying bundle list does.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Literal, Sequence

from pydantic import BaseModel

from app.config import get_settings
from app.core.dates import IsoDate, current_date_directive, exclusive_end
from app.retrieval.understanding.catalog_prompt import (
    BUNDLE_GLOSSARY,
    BUNDLE_LIST,
    COLLECTIVE_WORD_WARNING,
    VOCABULARY,
    catalog_coverage_directive,
    catalog_inventory_directive,
)
from app.retrieval.structured import topic
from app.retrieval.structured.types import ToolResult

if TYPE_CHECKING:
    from app.retrieval.understanding.query_processor import QueryAnalysis

logger = logging.getLogger(__name__)

CountOf = Literal[
    "records", "theme", "content_type", "author", "year"
]
Operation = Literal["lookup", "list", "count", "distribution", "list_themes"]
GroupBy = Literal["theme", "content_type", "author", "year"]

_PARSE_SYSTEM = (
    "Extract structured-query parameters from the user's request about a content "
    "repository of news, articles, reports, projects, events and research papers.\n"
    # Glossary before the vocabulary block: the latter refers back to the
    # everyday words listed "above" for each type.
    + BUNDLE_GLOSSARY + "\n"
    + VOCABULARY + "\n"
    "- operation: 'count' for how-many/aggregate; 'distribution' for a breakdown "
    "per group ('how many per theme', 'spread across content types'); 'lookup' "
    "for a single specific item; 'list' for browse/enumerate; 'list_themes' ONLY "
    "to enumerate the theme vocabulary itself ('what themes are there?', 'how "
    "many themes are there?'). A how-many question about anything other than "
    "themes — articles, publications, authors, records — is 'count', never "
    "'list_themes'.\n"
    "- bundle: the specific content type when the user names one, one of: "
    + BUNDLE_LIST + " — or the user's own word where the content-type glossary "
    "above says to pass it through. " + COLLECTIVE_WORD_WARNING + "\n"
    "- theme: the thematic area / topic / theme name if the request is scoped "
    "to one (e.g. 'under the Climate theme', 'in the Energy area'); else null.\n"
    "- group_by: for 'distribution' only — the dimension to break down by: "
    "'theme', 'content_type', 'author', or 'year'; else null.\n"
    "- title_contains: a title keyword if the user names/quotes a title; else null.\n"
    "- author: an author/person name if specified; else null.\n"
    "- year: a four-digit year if a specific year is referenced; else null.\n"
    "- date_from / date_to_inclusive: the FIRST and LAST ISO dates (YYYY-MM-DD) "
    "to include for any date or period mentioned. Copy the dates the request "
    "names; never add or subtract a day. For a single day both are that same day; "
    "for a month or year span it end to end (2024 -> 2024-01-01 / 2024-12-31); "
    "for 'since'/'after' set only date_from; for 'before'/'until X' set only "
    "date_to_inclusive, to the day before X; else both null.\n"
    "- count_of: what a 'count' counts. Read it off the noun straight after "
    "'how many': that noun IS the thing being counted.\n"
    "    'how many articles / publications / reports / records' -> 'records' "
    "(the default — these are documents)\n"
    "    'how many AUTHORS are there' -> 'author'\n"
    "    'how many AUTHORS work on Energy' -> 'author', theme='Energy'\n"
    "    'how many THEMES does Meena Sehgal publish in' -> 'theme', "
    "author='Meena Sehgal'\n"
    "    'how many PEOPLE wrote these' -> 'author'\n"
    "  The filters say what to count *over*; count_of says what to count. Only "
    "for operation='count' — a 'which/what X...' question wants a breakdown, so "
    "'which authors have published the most' is operation='distribution' with "
    "group_by='author', not a count.\n"
    "- secondary_group_by: a SECOND grouping dimension for 'distribution', used "
    "only when BOTH dimensions vary across the answer — 'which authors write "
    "about which themes' is group_by='author' plus secondary_group_by='theme'. "
    "A dimension the user pins to one named value is a FILTER, not a dimension: "
    "'what themes does Meena Sehgal publish in' is group_by='theme' with "
    "author='Meena Sehgal' and secondary_group_by null. Null for any ordinary "
    "per-X breakdown.\n"
    "- limit: how many items to return for list/lookup (default 10)."
)


class StructuredQuery(BaseModel):
    operation: Operation = "list"
    bundle: str | None = None
    theme: str | None = None
    group_by: GroupBy | None = None
    secondary_group_by: GroupBy | None = None
    count_of: CountOf = "records"
    title_contains: str | None = None
    author: str | None = None
    year: int | None = None
    date_from: IsoDate = None
    date_to_inclusive: IsoDate = None
    limit: int = 10

    @property
    def date_to(self) -> str | None:
        """Exclusive upper bound derived from the inclusive end the LLM supplies
        (see `QueryScope.date_to`); `planner._tool_call` reads it duck-typed."""
        return exclusive_end(self.date_to_inclusive)


def parse_structured(
    question: str, history: Sequence[dict[str, str]] | None = None
) -> StructuredQuery | None:
    """LLM fallback parse of the structured slots, used when no usable analysis
    was supplied. None on failure."""
    from app.core.clients.llm import get_structured_llm

    convo = ""
    if history:
        convo = "\n".join(f"{t.get('role')}: {t.get('content')}" for t in list(history)[-4:])
    try:
        model = get_structured_llm().with_structured_output(StructuredQuery)
        return model.invoke(
            [
                (
                    "system",
                    _PARSE_SYSTEM
                    + catalog_inventory_directive()
                    + catalog_coverage_directive()
                    + current_date_directive(),
                ),
                ("human", f"Conversation:\n{convo}\n\nRequest: {question}"),
            ]
        )
    except Exception:
        logger.warning("Structured-query parse failed.", exc_info=True)
        return None


# Collective words that mean "everything published", not one content type. The
# classifier inconsistently collapses these onto the research_papers bundle,
# under-counting a person's output (10 papers instead of the 21 papers+articles).
_GENERIC_SCOPE = re.compile(r"\b(publications?|works|writings|output|everything)\b", re.I)


def _spans_all_content(question: str, bundle: str | None) -> bool:
    """True when a generic collective term (not a type the user actually named)
    is what set ``bundle`` — the count/list must then span all content types.

    Detected structurally, so it stays robust to the classifier's nondeterminism:
    a generic term is present AND none of the resolved bundle's own label words
    appear in the question. 'how many publications from X' -> clear the bundle;
    'how many research paper publications' -> keep it (the type was named)."""
    if not bundle or not _GENERIC_SCOPE.search(question):
        return False
    from app.retrieval.structured.entities import entity_label

    label_words = set(re.findall(r"[a-z]+", f"{bundle} {entity_label(bundle, 2)}".lower()))
    question_words = set(re.findall(r"[a-z]+", question.lower()))
    return not (label_words & question_words)


# "Article" names one content type on the site and is everyday English for
# anything a person writes. Asked of a person, the everyday reading is the one
# meant: measured 2026-10-04, "how many articles are there of Vidha dhawan" was
# answered "There is 1 article" — the one item in the Article category — while
# the same author has 31 feature articles, 7 research papers and 2 policy briefs.
# The category reading stays when the wording points at it: the category or
# section by name, "only articles", a quoted "Articles", or another content type
# named beside it ("articles and research papers" sets the types apart).
_ARTICLE_WORD = re.compile(r"\barticles?\b", re.I)
_ARTICLE_CATEGORY = re.compile(
    r"\barticles?\s+(?:category|section|type|content\s+type|tab|page)\b"
    r"|\b(?:category|section|content\s+type|type|tab)\s+(?:of\s+|called\s+|named\s+)?"
    r"[\"'‘“]?articles?\b"
    r"|\b(?:only|just)\s+(?:the\s+)?articles?\b|\barticles?\s+only\b"
    r"|[\"‘“]articles?[\"’”]",
    re.I,
)
_OTHER_TYPE_WORD = re.compile(
    r"\b(?:feature[ds]?|papers?|briefs?|reports?|news|press|releases?|events?|"
    r"videos?|infographics?|projects?|blogs?)\b",
    re.I,
)


def _reads_article_as_writing(question: str, slots: Any) -> bool:
    """Whether a person's "articles" means everything they published rather
    than the site's Article category (see `_ARTICLE_WORD`)."""
    from app.retrieval.structured.entities import normalize_entity

    if not getattr(slots, "author", None):
        return False
    if normalize_entity(getattr(slots, "bundle", None)) != "article":
        return False
    if not _ARTICLE_WORD.search(question):
        return False
    return not (_ARTICLE_CATEGORY.search(question) or _OTHER_TYPE_WORD.search(question))


def _widen_articles(calls: Sequence[Any]) -> None:
    """Point a person's planned "articles" calls at every content type, and let
    a count or list say how many are in the Article category itself.

    Done to the plan rather than to the slots: planned with the bundle still
    set, "articles" is read as the type word it is. Cleared first, it was left
    over as subject matter and every list was narrowed to titles containing
    "articles" — none of the 41 (measured 2026-10-04)."""
    from app.retrieval.structured.entities import normalize_entity

    logger.info("Reading a person's 'articles' as all of their publications.")
    for call in calls:
        if normalize_entity(getattr(call, "entity", None)) != "article":
            continue
        call.entity = None
        if call.tool in ("count_records", "list_records"):
            call.named_type = "article"


# The noun after "how many" names what is counted. The unified analysis has no
# guidance for `count_of` and leaves it at "records": measured 2026-10-04, "how
# many authors are there?" and "how many themes are there?" both arrived that
# way, and the second was answered "There are 5524 items". Only nouns that name
# a catalog facet unambiguously are read here; "people" or "years" can mean
# something the catalog does not record, so they are left to the classifier.
_QUALIFIER = r"(?:different\s+|distinct\s+|unique\s+|main\s+|other\s+)*"
_COUNTED_NOUNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"\bhow\s+many\s+{_QUALIFIER}(?:authors?|writers?|contributors?)\b", re.I),
     "author"),
    (re.compile(rf"\bhow\s+many\s+{_QUALIFIER}(?:themes?|thematic\s+areas?)\b", re.I),
     "theme"),
    (re.compile(rf"\bhow\s+many\s+{_QUALIFIER}(?:content\s+types?|(?:types|kinds)\s+of\s+content)\b",
                re.I),
     "content_type"),
)


def _counted_noun(question: str) -> str | None:
    """The facet a "how many <noun>" question counts, when the noun names one."""
    for pattern, count_of in _COUNTED_NOUNS:
        if pattern.search(question):
            return count_of
    return None


# Slots that narrow a count to part of the catalog.
_SCOPE_SLOTS = ("bundle", "theme", "tags", "author", "title_contains",
                "date_from", "date_to", "year")


def _recount(slots: Any, counted: str) -> None:
    """Point a count at the facet its noun names.

    An unscoped theme count is the theme listing's question ("How many themes
    are there?" -> list_themes, as the catalog prompt's own example has it): a
    distinct count over the theme facet includes sub-theme rows and answered 26
    where the listing and the home page both say 7. Scoped ("how many themes
    does Meena Sehgal publish in") it stays a distinct count."""
    if counted == "theme" and not any(getattr(slots, s, None) for s in _SCOPE_SLOTS):
        slots.operation = "list_themes"
        return
    slots.count_of = counted


# One calendar year stated as the period ("in 2030", "during 2019"). The year
# alone is not enough — "the 2030 Agenda" names a subject, not a period.
_IN_YEAR = re.compile(r"\b(?:in|during)\s+(?:the\s+year\s+)?((?:19|20)\d{2})\b", re.I)
_ANY_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _stated_year(question: str, slots: Any) -> int | None:
    """A year the question states as its period but the slots dropped.

    The catalog-coverage directive tells the classifier to leave the dates null
    for a period the catalog does not reach, which suits passage retrieval and
    answers a count wrongly: "how many events were held in 2030?" came back as
    "There are 1082 events", the all-time total. Restoring the year makes the
    count an honest zero instead. Only one distinct year, stated with "in" or
    "during", and not part of a title the question names."""
    if any(getattr(slots, name, None) for name in ("date_from", "date_to", "year")):
        return None
    if len(set(_ANY_YEAR.findall(question))) != 1:
        return None
    match = _IN_YEAR.search(question)
    if match is None:
        return None
    if match.group(1) in (getattr(slots, "title_contains", None) or ""):
        return None
    return int(match.group(1))


def _restore_year(slots: Any, year: int) -> None:
    # The parse fallback carries a `year` shorthand (its `date_to` is derived);
    # the unified analysis carries the bounds themselves, `date_to` exclusive.
    if "year" in getattr(type(slots), "model_fields", {}):
        slots.year = year
    else:
        slots.date_from, slots.date_to = f"{year:04d}-01-01", f"{year + 1:04d}-01-01"


# Words that ask about authorship itself, which the catalog records exactly.
_AUTHORSHIP_WORDS = re.compile(
    r"\b(?:authors?|authored|wrote|written|writers?|writing|publish(?:ed|es|ing)?|"
    r"contributors?|contributed)\b",
    re.I,
)


def _asks_about_authorship(question: str, slots: Any) -> bool:
    """Whether a person question is answered by the catalog's authorship facet.

    "Which authors have published the most on climate change?" is a breakdown by
    author, and "how many authors are there?" a count of author names — both
    are exactly what the catalog stores, and declining them as person questions
    left the first refused and the second answered from an unrelated book.
    "Which researchers work on X" asks about work, not authorship, and is still
    declined (see `answer_structured`)."""
    if not _AUTHORSHIP_WORDS.search(question):
        return False
    operation = getattr(slots, "operation", None)
    if operation == "count":
        return getattr(slots, "count_of", None) == "author"
    if operation == "distribution":
        return "author" in (getattr(slots, "group_by", None),
                            getattr(slots, "secondary_group_by", None))
    return False


# error_kind values that mean "the filter was understood but could not be
# honored" — the answer is the result's `rendered` message, not a cue to guess
# via semantic search. Every other ok=False (unknown entity, no matching
# records, a query failure) keeps today's fall-through behavior.
#
# These two come out of fuzzy name matching, so they are terminal only when
# `entity_resolution_enabled` is on: that flag's job is to hold the matching
# behaviour back until it has been evaluated.
_TERMINAL_ERROR_KINDS = frozenset({"unresolved", "ambiguous"})

# Terminal whatever that flag says: nothing fuzzy produced them, so there is no
# matching quality to evaluate first. An ambiguous content type is a word that
# names more than one bundle ("projects"), where every alternative to asking is a
# wrong answer — one type's total reported as all, or the whole corpus counted as
# projects. Falling through to semantic search instead answers a counting
# question from prose.
_ALWAYS_TERMINAL_ERROR_KINDS = frozenset({"ambiguous_entity"})


def _terminal_result(results: list[ToolResult], *, strict: bool) -> ToolResult | None:
    """The first failed result whose error is terminal, if any. `strict` mirrors
    `entity_resolution_enabled`, widening what counts as terminal to include the
    fuzzy-matching outcomes."""
    kinds = (
        _TERMINAL_ERROR_KINDS | _ALWAYS_TERMINAL_ERROR_KINDS
        if strict
        else _ALWAYS_TERMINAL_ERROR_KINDS
    )
    for result in results:
        if not result.ok and result.error_kind in kinds:
            return result
    return None


def _gate_detail(calls: Sequence[Any], *, allowed: bool) -> None:
    """Detail only for a plan with one real question in it. A `resolve_entity`
    step only names who or what the other call filters on, so it does not
    count; two counts side by side are a comparison, and each part following
    itself with spreads and recent items would bury it."""
    substantive = [c for c in calls if getattr(c, "tool", None) != "resolve_entity"]
    for call in calls:
        call.detail = allowed and len(substantive) == 1


def _compose(results: list[ToolResult]) -> dict[str, Any]:
    """Merge the successful tool results into one structured answer: stack the
    rendered sections and renumber citations sequentially across them. A single
    result (the v1 deterministic plan) round-trips unchanged."""
    bodies: list[str] = []
    citations: list[dict[str, Any]] = []
    used_chunks = 0
    # A resolution step only names what the next call filters on, and that call
    # already states the canonical name in its own sentence — so "'rishab negi'
    # resolves to Rishabh Negi (author)." is plumbing, unless it is all there is.
    answered = any(r.tool != "resolve_entity" and r.rendered for r in results)
    for result in results:
        if result.tool == "resolve_entity" and answered:
            continue
        if result.rendered:
            bodies.append(result.rendered)
        for citation in result.citations:
            citations.append({**citation, "n": len(citations) + 1})
        used_chunks += len(result.data.get("records", []))
    return {
        "answer": "\n\n".join(bodies),
        "citations": citations,
        "intent": "structured",
        "used_chunks": used_chunks,
        "conflict": False,
        "cached": False,
    }


# Facets that make a catalog listing relevant to what was *asked about*. A bundle
# or a date alone does not: "the 10 most recent reports" answers no question about
# a subject, and offering it in place of a refusal implies a relevance the rows do
# not have. Both still apply as additional filters when the analysis carries them.
_SUBJECT_FACETS = ("theme", "tags", "author", "title_contains")


def catalog_fallback(
    question: str, *, analysis: QueryAnalysis | None
) -> dict[str, Any] | None:
    """Catalog entries matching a content question's scope, for when semantic
    retrieval found nothing to ground an answer.

    The catalog indexes titles and facets, so it can still place a document the
    vector store could not surface — a subject whose chunks all fell below the
    rerank threshold, say. The listing is deterministic, so it states what exists
    without claiming to answer the question (the caller supplies that framing).

    Returns None whenever there is nothing worth offering, leaving the caller to
    refuse as before:

    * no analysis — the passthrough fallback carries no facets to scope by;
    * no subject facet (see `_SUBJECT_FACETS`);
    * no matching rows.

    Never parses. A qa analysis has no `operation`, so `answer_structured` would
    spend an LLM call re-deriving slots these facets already hold — on a path that
    has already failed once and is about to refuse.
    """
    from app.retrieval.structured import planner

    if analysis is None:
        return None
    if not any(getattr(analysis, facet, None) for facet in _SUBJECT_FACETS):
        return None
    # A listing whatever the classifier's operation: a count or a distribution
    # answers nothing for a question that wanted content.
    db_plan = planner.plan(
        analysis.model_copy(update={"operation": "list"}),
        output_format=analysis.answer_format,
        question=question,
    )
    ok = [result for result in planner.execute(db_plan, question=question) if result.ok]
    return _compose(ok) if ok else None


def answer_structured(
    question: str,
    history: Sequence[dict[str, str]] | None = None,
    *,
    analysis: QueryAnalysis | None = None,
    detail: bool = True,
) -> dict[str, Any] | None:
    """Answer a catalog (database-intent) query via the Database Planner + tools.

    ``detail`` lets a single-call answer follow its headline with what the same
    scope shows (see `app.retrieval.structured.detail`). A combined answer passes
    False — its prose says the rest — and a multi-call plan never gets it, so a
    comparison stays a set of headlines rather than several stacked reports.

    The unified analysis already extracted the structured slots — reuse it and let
    the planner pick the tool; parse only when no usable analysis came. Returns
    None (fall through to semantic search) on an unusable plan or a guarded/empty
    tool result — unless every result failed and one is terminal, in which case
    its `rendered` message is the answer (see `_terminal_result`).
    `entity_resolution_enabled` widens what counts as terminal to the
    fuzzy-matching outcomes; an ambiguous content type is terminal either way.
    The flag gates only this fall-through change; it does not disable the
    catalog tools themselves.
    """
    from app.retrieval.structured import planner

    slots = (
        analysis
        if (analysis is not None and analysis.operation)
        else parse_structured(question, history)
    )
    if slots is None:
        return None
    # The year first: it scopes the count, which decides how a theme count is read.
    year = _stated_year(question, slots)
    if year is not None:
        logger.info("Restoring the year %s the question states as its period.", year)
        _restore_year(slots, year)
    if getattr(slots, "operation", None) == "count" and getattr(
        slots, "count_of", "records"
    ) in (None, "records"):
        counted = _counted_noun(question)
        if counted is not None:
            _recount(slots, counted)
    # A question about people is not a question about documents. The catalog
    # stores authorship, which is a different claim from "works on" — so unless
    # the question named the person to look up, listing documents at it produces
    # a confident non-answer. Measured: "Which researchers work on AI and
    # sustainability?" returned an opinion piece on education, a memorial lecture
    # and a solar-industry news item, and named nobody, while semantic retrieval
    # had the two AI papers whose recorded authors are the answer. Declining
    # hands it to the layer that can use them.
    if (topic.enabled()
            and topic.wants_person(question)
            and not getattr(slots, "author", None)
            and not _asks_about_authorship(question, slots)):
        logger.info(
            "Declining the structured path for a person question; the catalog "
            "lists documents, not people."
        )
        return None
    # A theme listing enumerates the vocabulary; a question that names one theme
    # is about that theme. Measured 2026-09-25: "tell me about climate change
    # theme" came back as the list of all seven themes — the classifier read
    # "theme" as a request for the vocabulary while extracting
    # theme="climate change" in the same breath, and the listing ignores its
    # theme. Declining hands it to passage retrieval, where the theme's own live
    # page answers it; the same question worded "thematic" had already gone that
    # way and was answered from that page.
    if (getattr(slots, "operation", None) == "list_themes"
            and getattr(slots, "theme", None)):
        logger.info(
            "Declining a theme listing for a question about one theme (%r).",
            slots.theme,
        )
        return None
    # A generic "publications / works" ask must count across every content type;
    # drop a bundle the classifier inferred from that collective word so the total
    # is not silently narrowed to one type (see _spans_all_content).
    if _spans_all_content(question, getattr(slots, "bundle", None)):
        slots.bundle = None
    widen_articles = _reads_article_as_writing(question, slots)
    output_format = analysis.answer_format if analysis is not None else "default"
    db_plan = None
    if get_settings().database_multi_call_enabled:
        db_plan = planner.plan_multi(question, output_format=output_format)
    if db_plan is None:  # disabled, or the LLM planner produced nothing usable
        db_plan = planner.plan(
            slots, output_format=output_format, question=question
        )
    if widen_articles:
        _widen_articles(getattr(db_plan, "calls", None) or [])
    _gate_detail(getattr(db_plan, "calls", None) or [], allowed=detail)
    results = planner.execute(db_plan, question=question)
    ok = [r for r in results if r.ok]
    if not ok:
        terminal = _terminal_result(
            results, strict=get_settings().entity_resolution_enabled
        )
        return _compose([terminal]) if terminal is not None else None
    # Marks an answer the catalog gave, as opposed to a question it asked back,
    # so the pipeline can tell a count it must keep from a clarification the
    # graph may still answer (see `query_pipeline._prepare`).
    return {**_compose(ok), "catalog_answered": True}
