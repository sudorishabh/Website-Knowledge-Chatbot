"""Asking what the user meant — at most once, with options the catalog can back.

Why this exists
---------------
``clarification_needed`` has been a first-class label in the intent taxonomy
since v2 (see :mod:`app.retrieval.understanding.prompts`), and until now it was
computed and then dropped: ``_legacy_intent_and_format`` collapses every terminal
intent onto ``chitchat``, so a question too vague to route reached the small-talk
prompt instead of a question back.

That collapse is only *partly* visible in practice, which is worth stating
because it sets how much this may change. ``_corrected_intent`` rescues a
chitchat verdict whenever the turn reads like an information request, so
"show me performance" already reaches retrieval — it is the turns that read like
neither small talk nor a question ("what about that one?", "show me a table")
that fall through to a greeting. This module handles both by reading the
*understanding*, before the rescue, rather than the route it produced.

What it does not do
-------------------
* **It is not a second classifier.** The trigger is the label the existing
  understanding call already emits. No extra LLM call is made, here or anywhere
  on the clarification path — the question, its options and the two guards that
  can decline to ask it are all deterministic.

* **It does not trust the label on its own.** See :func:`decide`: the verdict
  has to clear a confidence bar of its own, and a turn that names a subject is
  not interrupted just to ask what it is about.
* **It does not use ``is_ambiguous``.** That signal is a near-tie between
  *content intents* (qa vs database), which is a routing question the pipeline
  can answer by retrieving. It is not evidence that the user's meaning is
  unclear, and clarifying on it would interrupt questions that answer fine.
* **It never invents options.** Every option is a canonical name the catalog
  holds, drawn through the machinery that already serves entity-name ambiguity
  (:mod:`app.retrieval.structured.resolve`, :mod:`app.retrieval.structured.entities`).
  When the catalog cannot back at least two, the question is asked without
  options rather than padded to look complete.

State, without a session store
------------------------------
The server holds no conversation state: ``history`` is whatever the client
echoes back (see ``app/api/chat.py``). So the clarification turn *is* the state,
and :data:`MARKER` is what makes it recognisable — a fixed closing line this
module emits and nothing else does.

That line is deliberately ordinary English rather than a hidden token. The UI
escapes HTML before rendering (``ui/script.js``: ``renderMarkdown`` →
``escapeHtml``) and streams raw text into the bubble while tokens arrive, so an
HTML comment or a sentinel string would be *visible* to the reader, first mid-
stream and then permanently. A marker the user can read without being confused
by it is the only kind this transport allows.

The one-round guard falls out of the same reading. :func:`pending` looks at the
immediately preceding assistant turn only: if it is a clarification, this turn is
the answer to it and clarification is not offered again, so the sequence can only
ever be *clarify → answer*. A later, unrelated vague question in the same thread
is still free to clarify, because by then the preceding assistant turn is an
ordinary answer.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import get_settings

logger = logging.getLogger(__name__)

# The closing line of every clarification turn, and the marker `pending` matches
# on. Changing this text breaks state carried by conversations already in
# flight: a client echoing back the old wording stops being recognised, and the
# thread falls back to answering the follow-up on its own — degraded, not
# broken, which is why this is a constant rather than a format string.
MARKER = "Reply with your answer and I will take it from there."

# Options are a *choice*, so one is not a list — a single option is a guess
# wearing a question mark, and the user cannot tell which it is. Four is where a
# terminal chat bubble stops being scannable.
MIN_OPTIONS = 2
MAX_OPTIONS = 4

# The label the understanding taxonomy uses for "too vague to route without
# guessing". Imported as a literal rather than from the Literal type so this
# module stays free of the query_processor import (which would be a cycle).
CLARIFICATION_LABEL = "clarification_needed"


@dataclass(frozen=True)
class Clarification:
    """One question back to the user, with the options that back it.

    ``kind`` names where the options came from — ``theme``, ``content_type``,
    ``author`` or ``""`` when there are none — so a trace can say whether the
    catalog contributed or the question was asked blind.
    """

    question: str
    options: list[str] = field(default_factory=list)
    kind: str = ""

    def render(self) -> str:
        """The assistant turn's text: question, numbered options, marker.

        Numbering matches ``structured.tools._ambiguous_result`` so the two
        clarification paths read identically to a user who meets both.
        """
        parts = [self.question]
        if self.options:
            parts.append(
                "\n".join(f"{i}. {o}" for i, o in enumerate(self.options, start=1))
            )
        parts.append(MARKER)
        return "\n\n".join(parts)


def is_clarification_turn(text: str | None) -> bool:
    """Whether this assistant turn is one this module produced."""
    return bool(text) and text.rstrip().endswith(MARKER)


def _turns(history: Sequence[dict[str, str]] | None) -> list[dict[str, str]]:
    return [t for t in (history or []) if (t.get("content") or "").strip()]


def pending(history: Sequence[dict[str, str]] | None) -> str | None:
    """The question a clarification was asked about, or None if none is open.

    Only the immediately preceding assistant turn is consulted. That is both the
    correct reading — a clarification is answered by the very next turn, not by
    something three exchanges later — and the whole of the one-round guard: a
    caller that merges whenever this returns a value, and clarifies only when it
    returns None, cannot emit two clarifications in a row.
    """
    turns = _turns(history)
    if len(turns) < 2:
        return None
    last = turns[-1]
    if last.get("role") != "assistant" or not is_clarification_turn(last.get("content")):
        return None
    for turn in reversed(turns[:-1]):
        if turn.get("role") == "user":
            return (turn.get("content") or "").strip()
    return None


def merge(original: str, answer: str) -> str:
    """The original question and the user's clarification as one request.

    Deliberately a concatenation rather than a rewrite. The standalone, readable
    form of the merged question ("show me revenue performance") is what the
    understanding call's ``query_rewrite`` already produces for every follow-up
    turn, and it is given both halves here; this is the *floor* under that — the
    text retrieval falls back to when the LLM is unavailable and ``process``
    degrades to passthrough. Making it grammatical would need a model call on a
    path whose entire point is that it does not make one.
    """
    original = (original or "").strip()
    answer = (answer or "").strip()
    if not original:
        return answer
    if not answer:
        return original
    return f"{original.rstrip('.?! ')}: {answer}"


# --------------------------------------------------------------------------- #
# Options, drawn from the catalog
# --------------------------------------------------------------------------- #
# Every import below is function-local, for the reason
# `app.retrieval.understanding.catalog_prompt` documents: importing any submodule
# of `app.retrieval.structured` runs its __init__, which pulls in the planner,
# the tools and the MySQL/Qdrant/LLM clients behind them. Understanding must not
# pay for the query layer to ask a question.


def _entity_options(name: str | None, kind: str) -> list[str]:
    """Canonical catalog names a free-text entity plausibly meant.

    The existing entity-ambiguity path, reused verbatim: `resolve_entity` ranks
    the name against the catalog's own authors/themes/bundles and `plausible`
    keeps only those at or above the ambiguity floor — which is what stops a
    0.38-scoring unrelated name being offered as a choice.
    """
    if not (name or "").strip():
        return []
    try:
        from app.retrieval.structured import resolve

        candidates = resolve.resolve_entity(name, kind, limit=MAX_OPTIONS + 1)
        return [c.canonical_name for c in resolve.plausible(candidates, limit=MAX_OPTIONS)]
    except Exception:
        logger.debug("Entity options unavailable for %r.", name, exc_info=True)
        return []


def _content_type_options(question: str) -> list[str]:
    """Display labels for a collective word that spans several content types.

    ``entities.ambiguous_bundles`` owns the mapping — today only "projects",
    which spans ``completed_projects`` and ``ongoing_projects`` and whose silent
    collapse onto one of them is the bug that registry exists to prevent.
    Filtered by ``is_available`` so a type this catalog holds nothing for is
    never offered: a choice that can only return zero is not a choice.
    """
    try:
        from app.retrieval.structured import entities
    except Exception:
        logger.debug("Content-type options unavailable.", exc_info=True)
        return []
    for word in re.findall(r"[A-Za-z][A-Za-z_-]{2,}", question or ""):
        try:
            spanned = entities.ambiguous_bundles(word)
            available = [b for b in spanned if entities.is_available(b)]
            if len(available) >= MIN_OPTIONS:
                return [entities.entity_label(b, 2).title() for b in available[:MAX_OPTIONS]]
        except Exception:
            logger.debug("Content-type lookup failed for %r.", word, exc_info=True)
            return []
    return []


def _options(question: str, understanding: Any) -> tuple[list[str], str]:
    """The best catalog-backed options for this question, and where they came from.

    Ordered by how specific the evidence is: a name the understanding actually
    extracted beats one inferred from the raw wording, and both beat a collective
    content word. Returns ``([], "")`` when nothing clears the floor — the caller
    then asks without options, which is the honest outcome and not a failure.
    """
    scope = getattr(understanding, "scope", None)
    for value, kind in (
        (getattr(scope, "theme", None), "theme"),
        (getattr(scope, "author", None), "author"),
    ):
        options = _entity_options(value, kind)
        if len(options) >= MIN_OPTIONS:
            return options, kind

    options = _content_type_options(question)
    if len(options) >= MIN_OPTIONS:
        return options, "content_type"

    # Last resort: the question's own wording against the theme vocabulary. A
    # genuinely vague turn ("show me performance") scores below the ambiguity
    # floor against every theme and yields nothing, which is the intent — this
    # only fires when the words really do name several themes the catalog has.
    options = _entity_options(question, "theme")
    if len(options) >= MIN_OPTIONS:
        return options, "theme"
    return [], ""


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #

_QUESTION_WITH_OPTIONS = {
    "theme": "Which theme did you mean?",
    "author": "Which of these did you mean?",
    "content_type": "Which of these did you mean?",
}
_QUESTION_BARE = (
    "I need a little more to go on — could you say what topic or which "
    "documents you are asking about?"
)

# --------------------------------------------------------------------------- #
# "Does the turn name a subject?" — the veto
# --------------------------------------------------------------------------- #
# The understanding prompt defines the label as "too vague or underspecified to
# route without guessing (e.g. a format request naming no subject)", and the
# example is the whole of it: what makes "show me a table" unanswerable is that
# there is nothing to look for. Nothing enforced that definition, so the label
# was trusted on turns that named a subject perfectly well —
# "director generak of teri" drew `clarification_needed` at 0.74 with the
# rationale "Request is vague/typo; no clear question target", and TERI's
# Director General was sitting in the 2022-23 annual report. The user spent a
# turn being asked what topic they meant.
#
# So a turn that names a subject is not clarified *when the catalog cannot back
# the question with options*. The two halves of that matter separately:
#
# * With options, asking is grounded and cheap — "projects" really does span
#   completed and ongoing, and a choice between two names the catalog holds is
#   worth a turn even though "projects" is a subject. That path is untouched.
# * Without options the question degrades to `_QUESTION_BARE`, which asks the
#   user to restate what they already said. That is the case this vetoes, and
#   the case that fired above.
#
# Deliberately structural, like `_looks_like_real_question` in `query_processor`
# and for the same reason: a pure function of the text is exactly as
# deterministic as the text, which is the property worth buying back from a
# classifier that `analysis_votes = 1` lets decide on one sample.

# Words that are a *request* or an answer *shape* rather than a thing to look
# for. Not a copy of `query_processor._STOPWORDS`: this set has to reject the
# format vocabulary ("table", "json", "chart") that the chitchat probe has no
# reason to care about, and it keeps subject nouns the other set drops.
_NOT_A_SUBJECT = frozenset("""
    the a an of to in on for and or with by from as at into over under about
    across per via both each any all some many few much more most other another
    such than then there here
    is are was were be been being am do does did done have has had having
    can could may might must shall should will would
    what which who whom whose when where why how
    this that these those it its they them their thing things stuff
    one ones anything something everything nothing anyone someone
    dunno idk whatever else yes yeah nope
    i we you your our my me us mine ours yours
    show showing give given tell telling send sending share sharing get getting
    find finding fetch fetching provide providing make making need needing
    want wanting like please kindly help
    list lists listed listing enumerate table tables chart charts graph graphs
    diagram diagrams flowchart flowcharts timeline timelines bullet bullets
    point points json csv xml yaml markdown format formatted formatting
    summarize summarise summary summarised summarized overview breakdown
    """.split())

_SUBJECT_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")

# Two, not one. A single content word is a topic hint and not much more —
# "performance", "projects" — and those are exactly the turns where asking is
# the right move and where the catalog most often has options to offer. Two or
# more words naming something ("director general teri") is a subject.
MIN_SUBJECT_WORDS = 2


def names_a_subject(question: str) -> bool:
    """Whether the turn names something to look for.

    Counts *distinct* words so "reports reports" is one subject word, not two.
    """
    words = {
        w.lower() for w in _SUBJECT_WORD.findall(question or "")
    } - _NOT_A_SUBJECT
    return len(words) >= MIN_SUBJECT_WORDS


def decide(understanding: Any, question: str) -> Clarification | None:
    """A question back to the user, or None to answer as usual.

    ``None`` is the answer for every case that is not an explicit
    ``clarification_needed`` verdict, so a clear question is never interrupted —
    which is the property that matters most here, and the one the caller relies
    on to stay byte-compatible when the feature is off.

    The verdict is necessary but no longer sufficient. Two guards stand between
    the label and a question, because the label is one stochastic sample
    (``analysis_votes`` defaults to 1) and acting on it wrongly costs the user a
    whole turn:

    1. the label must clear ``clarification_min_confidence``, a bar of its own
       rather than the one every content label shares;
    2. if the catalog cannot back the question with options, the turn must not
       already name a subject (:func:`names_a_subject`) — there is nothing to
       ask a user who has told us what they are asking about.

    Both are one-directional: they can only decline to clarify, never cause a
    clarification that the label did not ask for.

    The caller is responsible for the one-round guard (see :func:`pending`); this
    function is a pure read of one turn's understanding and holds no state.
    """
    if understanding is None:
        return None
    confidence = max(
        (
            float(getattr(p, "confidence", 0.0) or 0.0)
            for p in getattr(understanding, "intents", []) or []
            if getattr(p, "label", None) == CLARIFICATION_LABEL
        ),
        default=None,
    )
    if confidence is None:
        return None
    floor = float(getattr(get_settings(), "clarification_min_confidence", 0.8))
    if confidence < floor:
        logger.info(
            "Not clarifying: %s at %.2f is below the %.2f bar.",
            CLARIFICATION_LABEL, confidence, floor,
        )
        return None
    options, kind = _options(question, understanding)
    if not options and names_a_subject(question):
        logger.info(
            "Not clarifying: the turn names a subject and the catalog has no "
            "options to offer, so there is nothing to ask that the user has "
            "not already said."
        )
        return None
    text = _QUESTION_WITH_OPTIONS.get(kind, _QUESTION_BARE) if options else _QUESTION_BARE
    return Clarification(question=text, options=options, kind=kind)
