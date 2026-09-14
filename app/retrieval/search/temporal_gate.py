"""Temporal scope of a question, and the one filter retrieval can honour today.

Why this exists
---------------
The 86-question benchmark asked "Are there any upcoming TERI training
programmes?" and the system returned six *past* programmes (TERI-DST and
TERI-ITEC cycles from 2013-15) and then refused. Nothing on the retrieval path
distinguished "upcoming" from "ever", and ranking by date alone puts the *most
recent past* event first — close to the opposite of the answer.

What this can and cannot do
---------------------------
Applied as a post-retrieval gate rather than a pre-filter: the candidates come
back as usual, and for an ``UPCOMING`` question the event blocks whose date has
already passed are dropped.

Everything it needs is in the block's own payload. An ``events`` document's
``effective_start_date`` **is** its event start date, because the bundle names
the field it is dated by, and ``effective_end_date`` carries the end of a
multi-day event. This used to read ``field_event_start_date`` out of
``documents.raw_meta`` over a MySQL round trip per query — the last place the
read path knew a Drupal field name. The canonical fields say the same thing,
cost nothing, and mean retrieval no longer depends on how the CMS happens to
spell a bundle's date field.

The gate is deliberately narrow:

* it only ever *removes* blocks, so it cannot invent an answer;
* it only touches bundles whose date is a scheduled occurrence
  (``app.core.corpus.SCHEDULED_BUNDLES``), so a page, a policy brief or a
  project is never affected. Scoping by bundle is what replaced "carries an
  event date": every document has an ``effective_start_date``, so without the
  scope the gate would drop most of the corpus for any future-tense question;
* it respects precision. An event stated as a month is not past until that
  month is, and one stated as a year is not past until the year is — the same
  refusal to read 1 January as a day that the answer layer makes.
* it declines to filter at all when that would empty the context, because
  answering from stale events is bad and answering from nothing is worse — the
  generator is told what it has and can say no upcoming ones are listed.

Modes
-----
``PAST``, ``UPCOMING``, ``CURRENT``, ``POINT_IN_TIME``, ``DATE_RANGE``, ``NONE``.

Two of them gate, four of them rank, and none of them filters by date — that is
still ``app.retrieval.understanding.filters.date_conditions``, which turns the
window the user named into precision-aware overlap conditions before the search
runs. Nothing here duplicates it.

* ``UPCOMING`` and ``PAST`` **gate**: scheduled occurrences on the wrong side of
  today are dropped from the finished context, under the rules above.
* ``CURRENT``, ``PAST``, ``POINT_IN_TIME`` and ``DATE_RANGE`` **rank**, via
  :func:`temporal_fit` — a band the reranker cuts *inside* the relevance band,
  so fitting the asked-for period reorders candidates that are already
  comparably relevant and can never lift one that is not. See
  :mod:`app.retrieval.search.reranker`.
* ``NONE`` does neither. A question with no temporal intent must not acquire
  one, so :attr:`TemporalIntent.ranks` is False and every candidate scores the
  same — one band, no reordering.

All of it is behind ``temporal_intent_enabled`` except the ``UPCOMING`` gate,
which predates the flag and keeps running as it always has.
"""
from __future__ import annotations

import logging
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

from app.core.dates import parse_iso_date

logger = logging.getLogger(__name__)

PAST = "past"
UPCOMING = "upcoming"
CURRENT = "current"
POINT_IN_TIME = "point_in_time"
DATE_RANGE = "date_range"
NONE = "none"

# Word-boundary patterns, most specific first: "as of 2019" is a point in time
# even though it contains no tense, and "since 2019" is a range even though it
# reads as current. Order therefore matters and the first match wins.
_PATTERNS: tuple[tuple[str, str], ...] = (
    (DATE_RANGE, r"\bbetween\s+\d{4}\b|\bfrom\s+\d{4}\b|\bsince\s+\d{4}\b"
                 r"|\b\d{4}\s*(?:-|–|to)\s*\d{4}\b|\bover the (?:last|past)\b"),
    (POINT_IN_TIME, r"\bas of\b|\bat the (?:time|end) of\b|\bin \d{4}\b"),
    (UPCOMING, r"\bupcoming\b|\bforthcoming\b|\bscheduled\b|\bwill (?:be )?(?:take place|happen|run|host)"
               r"|\bnext (?:week|month|year|session|summit|conference|event)\b"
               r"|\bany (?:planned|future)\b|\bplanned\b|\bfuture\b(?!\s+of\b)"),
    (PAST, r"\bpast\b|\bprevious(?:ly)?\b|\bformer\b|\bused to\b|\bhistor(?:y|ical)\b"
           r"|\bearlier\b|\bonce\b|\bcompleted\b"),
    (CURRENT, r"\bcurrent(?:ly)?\b|\bright now\b|\bat present\b|\bpresently\b"
              r"|\bongoing\b|\bunderway\b|\bactive\b|\btoday\b|\blatest\b"),
)


def detect_mode(question: str) -> str:
    """The temporal scope a question asks for. Deterministic; no model call."""
    text = (question or "").lower()
    if not text.strip():
        return NONE
    for mode, pattern in _PATTERNS:
        if re.search(pattern, text):
            return mode
    return NONE


def _parse(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _reference_date(reference: date | None) -> date:
    return reference or datetime.now(timezone.utc).date()


def period_end(payload: Any) -> date | None:
    """The last day this document's period covers, or None if it has no date.

    Read entirely from the canonical fields. ``effective_end_date`` when the
    document states a period; otherwise the end of whatever
    ``effective_start_date`` establishes, which is where precision matters: a
    date stated as "September 2007" covers until the 30th, and one stated as
    "2007" until 31 December. Treating the stored 1st as the whole answer would
    call a month-long event past on its second day.
    """
    end = _as_date(payload.get("effective_end_date"))
    if end is not None:
        return _period_last_day(end, payload.get("end_precision"))
    start = _as_date(payload.get("effective_start_date"))
    if start is None:
        return None
    return _period_last_day(start, payload.get("start_precision"))


def _as_date(value: Any) -> date | None:
    """A stored timestamp as the calendar day it names, or None.

    The columns and the payload hold a full timestamp; every comparison here is
    between calendar days, so the time is dropped rather than compared.
    """
    parsed = parse_iso_date(value)
    return parsed.date() if parsed is not None else None


def _period_last_day(value: date, precision: Any) -> date:
    """The last day of the period ``value`` opens at ``precision``."""
    if precision == "year":
        return date(value.year, 12, 31)
    if precision == "month":
        return date(value.year, value.month, monthrange(value.year, value.month)[1])
    return value


def _is_scheduled(payload: Any) -> bool:
    """Whether "upcoming" means anything for this document's bundle."""
    from app.core.corpus import SCHEDULED_BUNDLES

    return str(payload.get("bundle") or "") in SCHEDULED_BUNDLES


def _is_open_ended(payload: Any) -> bool:
    """Whether this bundle's period runs to the present rather than to a date.

    ``ongoing_projects`` declares only a start, deliberately — stored, that is
    indistinguishable from a single-date document, and reading it as a point
    would call a project running since 2005 "not current". The same reading
    ``filters.date_conditions`` already applies to the lower bound.
    """
    from app.core.corpus import OPEN_ENDED_BUNDLES

    return str(payload.get("bundle") or "") in OPEN_ENDED_BUNDLES


# --------------------------------------------------------------------------- #
# Temporal intent, and how well a document fits it
# --------------------------------------------------------------------------- #

#: Modes that can reorder a ranking. ``UPCOMING`` is absent because it already
#: has a gate and nothing is gained by also scoring it; ``NONE`` is absent
#: because a question with no temporal intent must not acquire one.
_RANKING_MODES: frozenset[str] = frozenset({CURRENT, PAST, POINT_IN_TIME, DATE_RANGE})

# Fit is banded, not weighted, so only the *order* of these matters and the gaps
# only have to exceed the band tolerance. Named rather than spelled inline
# because the reranker's tolerance is calibrated against them.
FIT_MATCH = 1.0     # covers the time the question is about
FIT_PARTIAL = 0.75  # overlaps it, but reaches outside
FIT_UNKNOWN = 0.5   # no usable date — neither favoured nor penalised
FIT_MISS = 0.25     # dated, and not the time asked about


@dataclass(frozen=True)
class TemporalIntent:
    """The time a question is about: the mode, plus the window if one was named.

    ``mode`` is :func:`detect_mode`'s verdict — the lexical classifier that has
    always been in this module. ``date_from``/``date_to`` are not a second
    extraction: they are the *same* bounds query understanding already produced
    and ``filters.date_conditions`` already applied as Qdrant conditions,
    carried here so ranking can ask "how well does this fit?" about the window
    the filter asked "does this overlap at all?" about. Half-open like every
    other date bound on the read path — ``date_to`` is exclusive.
    """

    mode: str = NONE
    date_from: str | None = None
    date_to: str | None = None

    @property
    def ranks(self) -> bool:
        """Whether this intent is allowed to reorder candidates.

        False for ``NONE`` and ``UPCOMING``, which is what makes the ranking
        signal inert on the overwhelming majority of questions rather than a
        freshness thumb on every scale.
        """
        return self.mode in _RANKING_MODES


def temporal_fit(
    payload: Any, intent: TemporalIntent, *, reference: date | None = None
) -> float:
    """How well this document's period matches the time the question is about.

    Returns :data:`FIT_UNKNOWN` — the neutral value — for every case where the
    answer is not known: no temporal intent, an intent with no window, a
    document with no date. That is deliberate and load-bearing. An unknown must
    never be scored as a miss, because the reranker bands these and a miss band
    sits below a match band: penalising an undated passage would quietly demote
    every document whose date ingestion could not recover.

    Reads only fields the payload already carries. ``period_end`` supplies the
    precision-aware end (a document stated as "2007" covers until 31 December),
    so this never asserts a day its source did not state.
    """
    if not intent.ranks:
        return FIT_UNKNOWN
    today = _reference_date(reference)
    start = _as_date(payload.get("effective_start_date"))
    # An open-ended bundle has no end *because it has not ended*; every other
    # document's period closes where its own precision says it does.
    open_ended = _is_open_ended(payload)
    end = None if open_ended else period_end(payload)

    if intent.mode == CURRENT:
        # Defence in depth: `hybrid_search.build_filter` already makes
        # `is_current == True` mandatory, so a superseded chunk cannot reach a
        # ranking. If one ever does, it is not evidence about the present.
        if payload.get("is_current") is False:
            return FIT_MISS
        if start is None:
            return FIT_UNKNOWN
        if start > today:
            return FIT_MISS  # not in force yet
        if end is None:
            return FIT_MATCH  # open-ended and already started: valid now
        return FIT_MATCH if end >= today else FIT_MISS

    if intent.mode == PAST:
        if start is None:
            return FIT_UNKNOWN
        if open_ended:
            return FIT_MISS  # still running, so not something that "was"
        return FIT_MATCH if end is not None and end < today else FIT_MISS

    # POINT_IN_TIME / DATE_RANGE: overlap with the window the user named.
    lo = _as_date(intent.date_from)
    hi = _as_date(intent.date_to)
    if lo is None and hi is None or start is None:
        # The mode fired lexically ("as of", "between ... and ...") but no bounds
        # were extracted. There is nothing to compare against, and guessing a
        # window here would be the second temporal classifier this must not be.
        return FIT_UNKNOWN
    last = end if end is not None else today
    if hi is not None and start >= hi:
        return FIT_MISS
    if lo is not None and last < lo:
        return FIT_MISS
    contained = (lo is None or start >= lo) and (hi is None or last < hi)
    return FIT_MATCH if contained else FIT_PARTIAL


def _gate(
    blocks: Sequence[Any],
    stale: Any,
    *,
    reference: date | None,
    label: str,
    reason: str,
) -> list[Any]:
    """Drop the scheduled-occurrence blocks ``stale`` rejects, or change nothing.

    The shared body of the two gates. Every property the upcoming gate has
    always had is here and is the same for both: only scheduled bundles are
    considered, the list comes back untouched when nothing is stale, and it
    comes back untouched when *everything* is — answering from the wrong
    occurrences is bad, answering from nothing is worse, and the generator can
    say the corpus lists none.
    """
    if not blocks:
        return list(blocks)
    today = _reference_date(reference)

    kept, dropped = [], []
    for block in blocks:
        payload = block.payload or {}
        if _is_scheduled(payload) and stale(payload, today):
            dropped.append(block)
        else:
            kept.append(block)
    if not dropped:
        return list(blocks)
    if not kept:
        logger.info(
            "%s gate would empty the context (%d stale event blocks); "
            "keeping them so the answer can say none are %s.",
            label, len(dropped), label.lower(),
        )
        return list(blocks)
    logger.info("%s gate dropped %d block(s) for events %s.", label, len(dropped), reason)
    for i, block in enumerate(kept, start=1):
        block.n = i
    return kept


def gate_upcoming(
    blocks: Sequence[Any], *, reference: date | None = None
) -> list[Any]:
    """Drop blocks for scheduled occurrences that are already over.

    Returns the list unchanged when there is nothing to gate, when no block is a
    scheduled bundle, or when gating would leave nothing — the caller must
    always get a context it can reason about.
    """
    def over(payload: Any, today: date) -> bool:
        end = period_end(payload)
        return end is not None and end < today

    return _gate(blocks, over, reference=reference,
                 label="Upcoming", reason="already started")


def gate_past(blocks: Sequence[Any], *, reference: date | None = None) -> list[Any]:
    """Drop blocks for scheduled occurrences that have not happened yet.

    The exact mirror of :func:`gate_upcoming`, and it exists for the mirror
    failure: "which workshops has TERI already run?" answered from next
    quarter's calendar is wrong in the same way that "any upcoming programmes?"
    answered from 2013 was. Same narrow scope — only the bundles whose date is a
    scheduled occurrence — and the same refusal to empty a context.

    Precision cuts the other way here, so it reads the period's *start*: an
    occurrence stated as "2026" has not happened yet only while 1 January 2026
    is still ahead, which is what the stored start already says. Nothing is
    inferred for an undated block; it is kept, as before.
    """
    def not_yet(payload: Any, today: date) -> bool:
        start = _as_date(payload.get("effective_start_date"))
        return start is not None and start > today

    return _gate(blocks, not_yet, reference=reference,
                 label="Past", reason="that have not happened yet")
