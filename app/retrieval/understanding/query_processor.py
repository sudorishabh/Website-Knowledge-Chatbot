from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.clients.llm import get_llm, get_structured_llm
from app.core.dates import IsoDate, current_date_directive, exclusive_end
from app.retrieval.search.temporal_gate import TemporalIntent, detect_mode
from app.retrieval.understanding import clarify
from app.retrieval.understanding.catalog_prompt import (
    catalog_coverage_directive,
    catalog_inventory_directive,
)
from app.retrieval.understanding.filters import (
    _facet_filters,
    _parse_bound,
    _theme_condition,
)
from app.retrieval.understanding.prompts import UNDERSTANDING_SYSTEM as _UNDERSTANDING_SYSTEM

logger = logging.getLogger(__name__)

Intent = Literal["qa", "structured", "scoped_summary", "chitchat"]
# How the user wants the answer shaped. Detected from the turn; used downstream
# to steer generation (and table-aware retrieval). 'default' = let the model
# choose the natural shape.
AnswerFormat = Literal["default", "list", "table", "summary", "detailed", "timeline"]
Operation = Literal["count", "list", "lookup", "distribution", "list_themes"]
GroupBy = Literal["theme", "content_type", "author", "year"]
# What a count counts: the documents, or the distinct values of one facet.
# Mirrors `app.retrieval.structured.types.CountOf`, defined here for the same
# reason `GroupBy` is — this module is the upstream contract and does not import
# from the structured package.
CountOf = Literal["records", "theme", "content_type", "author", "year"]


class QueryAnalysis(BaseModel):
    intent: Intent = "qa"
    search_query: str = Field(description="Standalone, pronoun-resolved query.")
    answer_format: AnswerFormat = "default"
    # shared facet scope (used by both the structured and qa paths)
    source_type: str | None = None
    theme: str | None = None
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    date_from: IsoDate = None
    date_to: IsoDate = None
    language: str | None = None
    # structured-only slots (null/defaults on the qa path)
    operation: Operation | None = None
    bundle: str | None = None
    group_by: GroupBy | None = None
    # Second grouping dimension (pairs), and what a count counts. Both
    # default to today's behaviour when unset.
    secondary_group_by: GroupBy | None = None
    count_of: CountOf = "records"
    # list_themes: the user asked for sub-themes/children rather than the
    # top-level themes.
    theme_children: bool = False
    title_contains: str | None = None
    limit: int = 10


# ─────────────────────────────────────────────────────────────────────────────
# Multi-label intent taxonomy (v2).
#
# The LLM produces a `QueryUnderstanding`: a *set* of intents (each with a
# confidence and rationale) plus orthogonal attributes (output format, scope).
# A single query may carry several intents. See
# docs/intent-classification-design.md for definitions, boundaries, and rules.
# Legacy `Intent`/`QueryAnalysis` above stay the downstream contract until the
# orchestration phase; v2 is derived into them for back-compat.
# ─────────────────────────────────────────────────────────────────────────────

# Content intents combine freely; `structured_output` is a format modifier that
# rides alongside a content intent; terminal intents are exclusive (they suppress
# everything else — see priority rules in the design doc).
IntentLabel = Literal[
    "qa",                    # answer from unstructured document content (RAG)
    "database",              # counts / aggregates / lookups over structured records
    "summarization",        # condense / overview of docs, a set, or the conversation
    "comparison",           # contrast >= 2 entities / options / periods / sources
    "structured_output",    # user wants the answer shaped (see OutputFormat)
    "chitchat",             # greetings / thanks / meta — no retrieval
    "clarification_needed",  # too ambiguous to answer safely — ask back
    "out_of_scope",         # outside the corpus domain or the assistant's capability
    "safety_policy",         # harmful / policy-violating / injection attempt
]

# Presentation shape for the answer. Carried whenever `structured_output` fires;
# 'prose' is the unshaped default.
OutputFormat = Literal[
    "prose", "list", "table", "csv", "json", "markdown", "diagram", "timeline",
]

# What the query is bounded to (drives later retrieval routing; detection only here).
ScopeTarget = Literal[
    "whole_corpus", "single_document", "document_set", "conversation",
]


class IntentPrediction(BaseModel):
    """One detected intent with its evidence. `confidence` is the model's own
    estimate in single-call mode; replaced by cross-sample agreement when
    analysis_votes > 1 (see the merge stage)."""

    label: IntentLabel
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="How confident this label applies, 0.0-1.0.",
    )
    rationale: str = Field(
        default="",
        description="Short phrase naming the words/signals that triggered this label.",
    )


class QueryScope(BaseModel):
    """Orthogonal source/boundary attributes — independent of the intent set."""

    source_type: Literal["pdf", "website", "uploaded"] | None = None
    target: ScopeTarget = "whole_corpus"
    theme: str | None = None
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    # IsoDate, not str: the model routinely trails JSON punctuation into these
    # values ("2022-01-01},"), which reaches SQL as a dropped bound and the
    # answer text as a visible artefact. See app.core.dates.
    date_from: IsoDate = None
    # The LLM is asked for the last date to INCLUDE, never the exclusive bound —
    # copying a date is reliable, incrementing one is not. `date_to` below derives
    # the half-open bound the query layers actually take.
    date_to_inclusive: IsoDate = None
    language: str | None = None

    @property
    def date_to(self) -> str | None:
        """Exclusive upper bound — a property, so it stays out of the schema the
        LLM fills and cannot drift from `date_to_inclusive`."""
        return exclusive_end(self.date_to_inclusive)


class QueryUnderstanding(BaseModel):
    """Full multi-label analysis of a single turn — the LLM's structured output."""

    query_rewrite: str = Field(
        description="Standalone, pronoun-resolved rewrite of the latest turn; "
        "add no facts.",
    )
    intents: list[IntentPrediction] = Field(
        default_factory=list,
        description="Every applicable intent label for the query (multi-label).",
    )
    output_format: OutputFormat = Field(
        default="prose",
        description="Desired answer shape; 'prose' when the user did not ask for one.",
    )
    scope: QueryScope = Field(default_factory=QueryScope)
    # database-only slots (null/defaults unless the `database` intent applies)
    operation: Operation | None = None
    group_by: GroupBy | None = None
    bundle: str | None = None
    title_contains: str | None = None
    theme_children: bool = Field(
        default=False,
        description="For list_themes: true when the user asked for sub-themes / "
        "children rather than the top-level themes.",
    )
    limit: int = 10


@dataclass
class ProcessedQuery:
    original: str
    search_query: str
    intent: Intent = "qa"
    answer_format: AnswerFormat = "default"
    source_type: str | None = None
    language: str | None = None
    filters: list[Any] = field(default_factory=list)
    # Full analysis for downstream consumers (structured route); None when the
    # analysis call failed and we fell back to passthrough.
    analysis: QueryAnalysis | None = None
    # Full multi-label understanding (v2) for exposure/debugging; None on the
    # passthrough fallback. Downstream still routes on `intent`/`analysis`.
    understanding: QueryUnderstanding | None = None
    # A question to ask the user instead of answering this turn, or None to
    # answer as usual. Only ever set when `clarification_enabled` is on, the
    # turn was labelled `clarification_needed`, and no clarification is already
    # open — so the default is None on every path that exists today.
    clarification: clarify.Clarification | None = None
    # The earlier question this turn was merged with, when it is the answer to a
    # clarification; None on an ordinary turn. Recorded for the trace, and as the
    # marker that says why `search_query` mentions something the user did not
    # type in this turn.
    clarified_from: str | None = None
    # The time this question is about: the mode `temporal_gate.detect_mode`
    # classifies, plus the window understanding already extracted. Always
    # computed (a regex over a string the analysis produced anyway) so the trace
    # can show it; only *acted on* when `temporal_intent_enabled` is set, which
    # the pipeline decides — see `app.pipeline.query_pipeline._prepare`.
    temporal_intent: "TemporalIntent" = field(default_factory=lambda: TemporalIntent())

    @property
    def is_ambiguous(self) -> bool:
        """Near-tie between the top content intents — a debug/clarification
        signal. False on the passthrough fallback (no understanding)."""
        return _is_ambiguous(self.understanding.intents) if self.understanding else False


def _format_history(history: Sequence[dict[str, str]] | None, max_turns: int = 12) -> str:
    if not history:
        return "(no prior conversation)"
    recent = list(history)[-max_turns:]
    return "\n".join(f"{t.get('role', 'user')}: {t.get('content', '')}" for t in recent)


def _vote(values: Sequence[Any]) -> Any:
    """Majority value across samples; ties take the first non-null value in vote
    order. Used to merge the scalar attributes of understanding samples."""
    keyed = [tuple(v) if isinstance(v, list) else v for v in values]
    counts = Counter(keyed)
    top = max(counts.values())
    leaders = {k for k, n in counts.items() if n == top}
    if len(leaders) == 1:
        winner = next(iter(leaders))
    else:
        winner = next((k for k in keyed if k in leaders and k is not None), None)
    return list(winner) if isinstance(winner, tuple) else winner


# ── Multi-label merge + hybrid confidence (v2) ───────────────────────────────
# Terminal intents are exclusive: the highest-priority one present wins alone.
# Content intents are co-equal, ranked by confidence. `structured_output` is a
# modifier that only rides alongside a content intent. See
# docs/intent-classification-design.md (sections 6-8).
_TERMINAL_PRIORITY: tuple[IntentLabel, ...] = (
    "safety_policy", "out_of_scope", "clarification_needed", "chitchat",
)
_CONTENT_INTENTS: frozenset[str] = frozenset(
    {"qa", "database", "summarization", "comparison"}
)


def _label_confidences(
    samples: Sequence[QueryUnderstanding],
) -> dict[str, tuple[float, str]]:
    """Per-label confidence + a representative rationale (the hybrid scheme):
    with several samples, confidence is the agreement fraction (vote share);
    with a single sample it is the model's own reported confidence."""
    n = len(samples)
    grouped: dict[str, list[float]] = {}
    rationale: dict[str, str] = {}
    for sample in samples:
        for pred in sample.intents:
            grouped.setdefault(pred.label, []).append(pred.confidence)
            rationale.setdefault(pred.label, pred.rationale)
    out: dict[str, tuple[float, str]] = {}
    for label, confs in grouped.items():
        conf = confs[0] if n <= 1 else len(confs) / n
        out[label] = (round(float(conf), 3), rationale.get(label, ""))
    return out


def _resolve_intents(
    confidences: dict[str, tuple[float, str]], *, threshold: float
) -> list[IntentPrediction]:
    """Apply the taxonomy rules to per-label confidences: terminal exclusivity +
    priority, the threshold gate, a guaranteed content intent, and the
    structured_output-never-alone modifier rule."""
    kept = {label: cr for label, cr in confidences.items() if cr[0] >= threshold}

    # Terminal intents are exclusive — the highest-priority one present wins.
    for term in _TERMINAL_PRIORITY:
        if term in kept:
            conf, why = kept[term]
            return [IntentPrediction(label=term, confidence=conf, rationale=why)]

    # Content path: guarantee at least one content intent survives.
    content = {lbl: cr for lbl, cr in kept.items() if lbl in _CONTENT_INTENTS}
    if not content:
        pool = {lbl: cr for lbl, cr in confidences.items() if lbl in _CONTENT_INTENTS}
        if pool:
            top = max(pool, key=lambda lbl: pool[lbl][0])
            content = {top: pool[top]}
        else:
            content = {"qa": (0.5, "fallback: no content intent detected")}

    ordered = sorted(content, key=lambda lbl: content[lbl][0], reverse=True)
    result = [
        IntentPrediction(label=lbl, confidence=content[lbl][0], rationale=content[lbl][1])
        for lbl in ordered
    ]
    # structured_output rides along only when a content intent is present.
    if "structured_output" in kept:
        conf, why = kept["structured_output"]
        result.append(
            IntentPrediction(label="structured_output", confidence=conf, rationale=why)
        )
    return result


def _primary_intent(intents: Sequence[IntentPrediction]) -> IntentLabel:
    """Single-label view for back-compat: highest-priority terminal, else the
    highest-confidence content intent, else qa."""
    labels = {p.label for p in intents}
    for term in _TERMINAL_PRIORITY:
        if term in labels:
            return term
    content = [p for p in intents if p.label in _CONTENT_INTENTS]
    if content:
        return max(content, key=lambda p: p.confidence).label
    return "qa"


def _is_ambiguous(intents: Sequence[IntentPrediction], *, margin: float = 0.2) -> bool:
    """True when the top two content intents are within `margin` of each other —
    a genuine multi-way call worth surfacing (and a clarification signal)."""
    content = sorted(
        (p for p in intents if p.label in _CONTENT_INTENTS),
        key=lambda p: p.confidence, reverse=True,
    )
    if len(content) < 2:
        return False
    return (content[0].confidence - content[1].confidence) < margin


def _merge_understanding(
    samples: Sequence[QueryUnderstanding], *, threshold: float
) -> QueryUnderstanding:
    """Merge N understanding samples into one: agreement-voted intents (see
    _label_confidences / _resolve_intents), majority-voted attributes, and a
    query_rewrite drawn from a sample that matches the merged primary intent.
    Assumes at least one sample."""
    confidences = _label_confidences(samples)
    intents = _resolve_intents(confidences, threshold=threshold)

    def vote(getter: Any) -> Any:
        return _vote([getter(s) for s in samples])

    scope = QueryScope(
        source_type=vote(lambda s: s.scope.source_type),
        target=vote(lambda s: s.scope.target) or "whole_corpus",
        theme=vote(lambda s: s.scope.theme),
        author=vote(lambda s: s.scope.author),
        tags=vote(lambda s: s.scope.tags) or [],
        date_from=vote(lambda s: s.scope.date_from),
        date_to_inclusive=vote(lambda s: s.scope.date_to_inclusive),
        language=vote(lambda s: s.scope.language),
    )
    primary = _primary_intent(intents)
    rewrite = next(
        (s.query_rewrite for s in samples
         if s.query_rewrite.strip() and any(p.label == primary for p in s.intents)),
        samples[0].query_rewrite,
    )
    return QueryUnderstanding(
        query_rewrite=rewrite,
        intents=intents,
        output_format=vote(lambda s: s.output_format) or "prose",
        scope=scope,
        operation=vote(lambda s: s.operation),
        group_by=vote(lambda s: s.group_by),
        bundle=vote(lambda s: s.bundle),
        title_contains=vote(lambda s: s.title_contains),
        # Any slot added to QueryUnderstanding must be voted here too: this
        # rebuilds the object field by field, so an omission silently resets the
        # slot to its default instead of failing.
        theme_children=bool(vote(lambda s: s.theme_children)),
        limit=vote(lambda s: s.limit) or 10,
    )


def _understanding_messages(
    question: str, history: Sequence[dict[str, str]] | None
) -> list[tuple[str, str]]:
    return [
        (
            "system",
            _UNDERSTANDING_SYSTEM
            + catalog_inventory_directive()
            + catalog_coverage_directive()
            + current_date_directive(),
        ),
        (
            "human",
            f"Conversation so far:\n{_format_history(history)}\n\n"
            f"Latest user turn:\n{question}",
        ),
    ]


def _voted_understanding(
    question: str, history: Sequence[dict[str, str]] | None, votes: int
) -> list[QueryUnderstanding]:
    """N concurrent understanding samples at exploratory temperature. Errored
    samples are dropped; the caller merges the survivors."""
    from concurrent.futures import ThreadPoolExecutor

    messages = _understanding_messages(question, history)

    def sample(_: int) -> QueryUnderstanding | None:
        try:
            model = get_llm(temperature=0.7).with_structured_output(QueryUnderstanding)
            return model.invoke(messages)
        except Exception:
            logger.warning("Understanding vote failed; dropping it.", exc_info=True)
            return None

    with ThreadPoolExecutor(max_workers=votes) as pool:
        return [r for r in pool.map(sample, range(votes)) if r is not None]


# v2 output_format -> legacy AnswerFormat. csv/json/markdown/diagram have no
# legacy equivalent yet, so they degrade to 'default' (the structured_output
# intent still records the exact shape on `understanding` for later use).
_FORMAT_TO_LEGACY: dict[str, AnswerFormat] = {
    "prose": "default", "list": "list", "table": "table", "timeline": "timeline",
    "csv": "default", "json": "default", "markdown": "default", "diagram": "default",
}


def _legacy_intent_and_format(u: QueryUnderstanding) -> tuple[Intent, AnswerFormat]:
    """Collapse the multi-label understanding onto the single-label route the
    current pipeline consumes.

    `out_of_scope` is deliberately routed to `qa`, not chitchat: the classifier
    is a single stochastic sample that frequently mislabels an in-corpus question
    (a pasted document title, a domain topic) as out-of-scope, and a blind
    deflection then hides content the store actually contains. Routing it through
    retrieval lets the corpus be the arbiter — a genuinely off-topic query
    retrieves nothing usable and the grounding prompt returns the standard
    refusal, while a misjudged one gets answered. `chitchat` /
    `clarification_needed` / `safety_policy` stay on the non-retrieving path."""
    primary = _primary_intent(u.intents)
    fmt: AnswerFormat = _FORMAT_TO_LEGACY.get(u.output_format, "default")
    if primary in ("chitchat", "clarification_needed", "safety_policy"):
        return "chitchat", fmt
    if primary == "database":
        return "structured", fmt
    if primary == "summarization":
        # One named document keeps the old qa+summary behavior; a set or the whole
        # corpus uses the scoped-summary path.
        if u.scope.target == "single_document" or u.title_contains:
            return "qa", (fmt if fmt != "default" else "summary")
        return "scoped_summary", fmt
    return "qa", fmt  # qa, comparison, or a lone modifier


def _to_legacy_analysis(question: str, u: QueryUnderstanding) -> QueryAnalysis:
    """Derive the downstream QueryAnalysis contract from the v2 understanding."""
    intent, answer_format = _legacy_intent_and_format(u)
    source_type = (
        u.scope.source_type if u.scope.source_type in ("pdf", "website") else None
    )
    return QueryAnalysis(
        intent=intent,
        search_query=(u.query_rewrite or question).strip() or question,
        answer_format=answer_format,
        source_type=source_type,
        theme=u.scope.theme,
        author=u.scope.author,
        tags=list(u.scope.tags or []),
        date_from=u.scope.date_from,
        date_to=u.scope.date_to,
        language=u.scope.language,
        operation=u.operation,
        bundle=u.bundle,
        group_by=u.group_by,
        theme_children=u.theme_children,
        title_contains=u.title_contains,
        limit=u.limit,
    )


def _names_entity_and_relationship(question: str) -> bool:
    """Whether the question names a known entity *and* an approved relationship.

    Deliberately weaker than routing: it asks recognition and the cue vocabulary,
    not the resolver or the planner. Knowing a question is *not small talk* needs
    far less evidence than answering it, and this way intent classification does
    not reach into graph retrieval — the one-doorway rule
    (tests/test_graph_retrieval.py) keeps that to `retriever.py`, and it is worth
    keeping.

    Both halves are required. A greeting names neither. "Thanks for the funding
    update" names a cue but no entity, and stays chitchat.
    """
    try:
        from app.retrieval.understanding.approved_aliases import get_index
        from app.retrieval.understanding.relational import read_relational

        if not read_relational(question).is_relational:
            return False
        return bool(get_index().match(question))
    except Exception:  # pragma: no cover - a probe must not break understanding
        logger.debug("Relational-shape probe failed.", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# A second, independent way to recognise "this is not small talk": lexical
# structure rather than entity resolution. `_names_entity_and_relationship`
# only rescues a question that names a *known* entity and an *approved*
# predicate — a narrow, high-precision net that Q079
# ("What technologies are available for waste valorization?") falls straight
# through, since it names neither. Measured over three repeats of one build,
# Q079 drew qa/chitchat/chitchat, Q091 drew chitchat/qa/chitchat, and Q077 drew
# qa/chitchat/qa: none of the three names a resolvable entity, but all three
# are plainly information requests by their wording alone.
#
# The two probes are combined with OR in `_corrected_intent`, so between them a
# question is rescued if it is either "about a known thing" (relational) or
# "shaped like a question" (this one) — whichever evidence is available.
# --------------------------------------------------------------------------- #

# Canonical small talk and meta-questions about the assistant itself — the
# actual definition of chitchat (see the understanding prompt). Matched
# anywhere in the text, not just at the start, so a leading greeting in front
# of a real request ("hi, how many news items are there?") does not itself
# count as evidence *against* a real request — it only withholds evidence *for*
# one when nothing else in the turn does either.
_SOCIAL_OR_META = re.compile(
    r"^\s*(hi|hello|hey|hiya|greetings|good\s*(morning|afternoon|evening))\b"
    r"|\b(thanks?|thank\s*you|thx|cheers|much\s*appreciated)\b"
    r"|\b(bye|goodbye|see\s*you|take\s*care)\b"
    r"|\bhow\s*are\s*you\b|\bwhat'?s\s*up\b"
    r"|\bwho\s*are\s*you\b|\bwhat\s*can\s*you\s*do\b"
    r"|\bare\s*you\s*(a\s*)?(bot|ai|human|real)\b"
    r"|\bwhat\s*is\s*your\s*name\b|\bhow\s*do\s*you\s*work\b",
    re.IGNORECASE,
)
# The interrogative shapes an information request actually takes: a WH-word or
# question-forming auxiliary, or an imperative that asks for content ("list
# the...", "tell me about..."), or a literal question mark.
_WH_OR_AUX = re.compile(
    r"\b(what|which|who|whom|whose|when|where|why|how|"
    r"is|are|was|were|do|does|did|can|could|will|would|should|has|have|had)\b",
    re.IGNORECASE,
)
_IMPERATIVE_LEAD = re.compile(
    r"^\s*(tell|describe|explain|list|show|give|provide|summarize|summarise|"
    r"outline|name|identify|compare|elaborate)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z][a-z'-]{2,}")
_STOPWORDS = frozenset(
    """
    the a an of to in on for and or with by from as at is are was were be been
    being this that these those it its their our we us you your they them he
    she his her which who whom whose what when where why how not no also more
    most other another such than then there here into over under about across
    during within without between among per via both each any all some many few
    much less least own same so too very can could may might must shall should
    will would do does did done have has had having tell describe explain list
    show give provide summarize summarise outline name identify compare
    elaborate
    """.split()
)


def _looks_like_real_question(question: str) -> bool:
    """Lexical evidence that the turn is an information request, not chitchat.

    Deliberately structural rather than semantic: no entity resolution, no
    corpus lookup, just the shape of the sentence. That makes it a pure
    function of the text, so it is exactly as deterministic across repeated
    calls as the question itself — the property this guard exists to buy back
    from a stochastic classifier.

    Requires (a) an interrogative or content-requesting shape, (b) at least one
    real content word so a bare "what's up?" cannot pass on structure alone,
    and (c) no match against the canonical social/meta phrases, which is
    checked last and wins regardless of the other two — "how are you?" has
    both a WH-word and a content-free "you", but it is a greeting and must
    never be rescued from chitchat.
    """
    text = (question or "").strip()
    if not text or _SOCIAL_OR_META.search(text):
        return False
    shaped = "?" in text or _WH_OR_AUX.search(text) or _IMPERATIVE_LEAD.match(text)
    if not shaped:
        return False
    return any(
        len(w) >= 4 and w not in _STOPWORDS for w in _WORD.findall(text.lower())
    )


# Counting phrases are the one case where the *route*, not just "is this
# chitchat", is decidable from wording alone: "how many X are there" has no
# reliable qa answer (prose does not carry a trustworthy count), and the
# few-shot bank already pins "how many research papers were published in
# 2024?" to [database]. Kept to this one unambiguous shape rather than the
# brief's broader "list / which projects" suggestion — that one was tried
# against this benchmark and made Q096 worse (a training-programmes question,
# lexically "what programmes...", answered better as prose than as a bundle
# listing), which is the concrete reason a wider rule is not safe here.
_COUNTING = re.compile(r"\bhow\s+many\b|\bnumber\s+of\b|\bcount\s+of\b", re.IGNORECASE)


def _corrected_intent(question: str, intent: Intent) -> Intent:
    """``chitchat``, unless the question is demonstrably a real question.

    ``analysis_votes`` defaults to 1, so a single stochastic sample decides the
    route, and ``chitchat`` is the one label with no way back: `_prepare` answers
    it from a canned string and never reaches retrieval. Measured over five
    identical runs, "Who led the Eco-city Project- Phase I?" came back chitchat
    twice and qa three times, and "Which documents were published between 2005
    and 2010?" chitchat twice of five. Both are ordinary questions the corpus can
    answer; on the chitchat draws the user got "I'm here to help…".

    Two independent probes feed the override, combined with OR: naming a known
    entity and an approved relationship (`_names_entity_and_relationship`), or
    simply reading as an information request by its wording
    (`_looks_like_real_question`). Either is sufficient; the second exists
    because the first alone left Q079/Q091/Q077-shaped questions unrescued —
    none of them names a resolvable entity, but none of them is small talk.

    A counting question ("how many X are there") is routed to ``structured``
    directly rather than ``qa``, because no prose answer to a "how many" claim
    is trustworthy the way a database count is. Everything else the guard
    rescues lands on ``qa``, matching the pre-existing behaviour.

    The override is deliberately one-directional: a greeting resolves neither
    probe and is untouched, which is the property the test suite pins.

    The probes cost a cached-index lookup and a few regex scans, and run only
    on the chitchat branch.
    """
    if intent != "chitchat":
        return intent
    if _COUNTING.search(question or ""):
        logger.info("Overriding a chitchat classification: counting question.")
        return "structured"
    if not (_names_entity_and_relationship(question) or _looks_like_real_question(question)):
        return intent
    logger.info("Overriding a chitchat classification: the question is relational.")
    return "qa"


def _edition_conditions(question: str) -> list[Any]:
    """Annual-report edition conditions for this question, or nothing.

    "Latest annual report" cannot be answered by ranking: the newest edition is
    absent from the unfiltered candidate set, so it is resolved here and applied
    as a filter before retrieval. Returns [] for every question that does not
    name an edition, which leaves retrieval byte-identical to before -
    including for annual-report *content* questions that name no edition.

    Failures are contained: the resolver logs and returns None if the series
    cannot be read, and this adds no condition.
    """
    try:
        from app.retrieval.understanding.annual_report_editions import conditions_for, resolve

        resolution = resolve(question)
    except Exception:
        logger.warning("Annual-report edition resolution failed; retrieval "
                       "proceeds unfiltered.", exc_info=True)
        return []
    if resolution is None:
        return []
    logger.info("annual-report edition: %s", resolution.describe())
    return conditions_for(resolution)


def process(question: str, history: Sequence[dict[str, str]] | None = None) -> ProcessedQuery:
    settings = get_settings()
    clarifying = bool(getattr(settings, "clarification_enabled", False))
    # An open clarification makes this turn the *answer* to the previous
    # question rather than a question of its own, so the two are merged before
    # anything reads them. `answered` doubles as the one-round guard: it is
    # non-None exactly when a clarification is already open, and the decision
    # below is skipped in that case, so the sequence can only be clarify→answer.
    #
    # With the flag off this is None and `effective` is `question` itself, which
    # is what keeps every path below byte-identical to before.
    answered = clarify.pending(history) if clarifying else None
    effective = clarify.merge(answered, question) if answered else question
    passthrough = ProcessedQuery(
        original=question, search_query=effective, intent="qa",
        clarified_from=answered,
        temporal_intent=TemporalIntent(mode=detect_mode(effective)),
    )
    votes = max(1, int(settings.analysis_votes))
    threshold = float(getattr(settings, "intent_confidence_threshold", 0.5))
    try:
        if votes > 1:
            samples = _voted_understanding(effective, history, votes)
        else:
            model = get_structured_llm().with_structured_output(QueryUnderstanding)
            samples = [model.invoke(_understanding_messages(effective, history))]
    except Exception:
        logger.warning("Query analysis failed; using passthrough.", exc_info=True)
        return passthrough

    samples = [s for s in samples if s is not None]
    if not samples:
        logger.warning("All analysis votes failed; using passthrough.")
        return passthrough

    understanding = _merge_understanding(samples, threshold=threshold)
    analysis = _to_legacy_analysis(effective, understanding)
    # A chitchat draw on a real question is unrecoverable downstream, so it is
    # checked against the corpus here rather than trusted. See `_corrected_intent`.
    analysis.intent = _corrected_intent(effective, analysis.intent)
    # Read from the understanding, not from `analysis.intent`: by this line the
    # terminal label has already been collapsed onto chitchat and possibly
    # rescued back to qa, and neither still says the user was unclear.
    clarification = (
        clarify.decide(understanding, effective)
        if clarifying and answered is None
        else None
    )
    logger.info(
        "intent: %s -> route=%s%s%s",
        [f"{p.label}:{p.confidence}" for p in understanding.intents],
        analysis.intent,
        " (ambiguous)" if _is_ambiguous(understanding.intents) else "",
        " (clarifying)" if clarification is not None else "",
    )
    return ProcessedQuery(
        original=question,
        search_query=analysis.search_query,
        intent=analysis.intent,
        answer_format=analysis.answer_format,
        source_type=analysis.source_type,
        language=analysis.language,
        filters=_facet_filters(analysis) + _edition_conditions(effective),
        analysis=analysis,
        understanding=understanding,
        clarification=clarification,
        clarified_from=answered,
        # Detected on `analysis.search_query` — the exact string `retrieve` is
        # handed and the one `_gate_temporal` has always detected on itself.
        # Same input, same classifier, so the promoted mode is by construction
        # the mode the gate would have computed, and the UPCOMING path cannot
        # change. The window is the one understanding already extracted; it is
        # not re-derived here.
        temporal_intent=TemporalIntent(
            mode=detect_mode(analysis.search_query),
            date_from=analysis.date_from,
            date_to=analysis.date_to,
        ),
    )
