"""Candidate reranking — relevance decides the ranking, recency decides ties.

Ordering used to be a weighted blend of a normalized semantic score, recency and
authority. A blend gets this backwards in the case that matters: because the
semantic scores are min-max normalized first, it separates candidates most
aggressively exactly when their scores are closest together — when the relevance
difference means least — while a fixed recency weight small enough not to
overrule a genuinely better passage is also too small to break the ties it is
there for.

So candidates are *banded* instead, and ranked on the bands in priority order:

1. **relevance** — scores within ``rerank_relevance_tolerance`` are "similarly
   relevant" and go on to compete on the keys below; a candidate a band lower
   never climbs past one above it, however new or full it is;
2. **temporal fit** — within a relevance band, a passage covering the time the
   question is about leads one that does not (see
   :mod:`app.retrieval.search.temporal_gate`). Inert unless the question named a
   time: with no temporal intent every candidate scores alike and this band is a
   constant, so it cannot reorder anything;
3. **authority** — within a temporal band, a canonical source (an organisation's
   own service or hub page) leads a secondary retelling of the same material;
4. **completeness** — within an authority band, a passage holding
   ``rerank_substance_ratio`` times the text of another says substantially more
   and leads it;
5. **recency** — comparable passages settle on the effective date, newest first.

Note what is *not* here: a freshness term that applies to every query. Recency is
the last key, and temporal fit only speaks when the question named a time — so a
newer document never wins on being newer.

Two editions of the same annual report land in one relevance band, and unless one
is a fragment the newer leads. An older passage that actually answers the
question still outranks a newer one that merely mentions it.

Why authority sits above completeness
-------------------------------------
It used to sit below, and below recency, reading only a ``source_authority``
payload key that nothing ever wrote — so it was a constant that could not
reorder anything. That left completeness, a *length* proxy, as the first
tie-break inside a relevance band, and length is exactly the axis on which a
canonical page loses: the 60-word "Water, soil and sludge testing" service node
carries the authoritative answer, and a 450-token annual-report chunk that
mentions testing in passing outranked it on substance every time.

Measured on the 86-question organisational benchmark: the authoritative page the
reference set names reached retrieval for 42% of questions, and nine questions
retrieved none of it at all. So authority is now *derived* from the metadata the
corpus already carries (:func:`derived_authority`) rather than waiting for an
ingest-time stamp, and it is banded like the others so only a material
difference reorders anything. An explicit ``source_authority`` payload value
still wins, so a corpus that does stamp authority keeps control.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from functools import lru_cache
from typing import Any, NamedTuple, Sequence

from pydantic import BaseModel, Field

from app.config import get_settings
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.search.volatility import is_volatile

logger = logging.getLogger(__name__)

_MAX_LLM_CANDIDATES = 40
_LLM_SNIPPET_CHARS = 600
# Recency/authority for a candidate carrying no signal either way. Mid-scale, so
# an unknown neither leads nor trails its band on a fact we do not have.
_UNKNOWN = 0.5


def _recency_scores(candidates: Sequence[Candidate]) -> list[float]:
    """Publication date per candidate, scaled to [0,1] across the set.

    Only the *order* of these values is read (recency ranks within a band, it is
    no longer weighted into a score), so the scaling exists to place an undated
    candidate at `_UNKNOWN` — mid-set — rather than to make the number comparable
    to anything else."""
    epochs: list[float | None] = []
    for c in candidates:
        raw = c.payload.get("effective_start_date")
        epoch: float | None = None
        if isinstance(raw, str) and raw:
            try:
                epoch = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                epoch = None
        epochs.append(epoch)
    known = [e for e in epochs if e is not None]
    if not known:
        return [_UNKNOWN] * len(candidates)
    lo, hi = min(known), max(known)
    span = hi - lo
    return [
        _UNKNOWN if e is None else (_UNKNOWN if span < 1e-9 else (e - lo) / span)
        for e in epochs
    ]


# How far apart two authority scores must be to count as different kinds of
# source. The scale below is laid out in steps of 0.15, so a tolerance under that
# separates every tier while keeping candidates inside one tier together.
_AUTHORITY_TOLERANCE = 0.10

# Temporal fit is emitted on a coarse, discrete scale (see `temporal_gate`:
# 1.0 match / 0.75 partial / 0.5 unknown / 0.25 miss). A tolerance below the
# smallest of those gaps separates every tier, and — the property that matters —
# collapses to a single band when every candidate scores the same, which is what
# a question with no temporal intent produces.
_TEMPORAL_TOLERANCE = 0.10

# Authority by the bundle a website node belongs to. The ordering is editorial
# provenance, not topic: a page the organisation maintains *as its statement* on
# something outranks a dated announcement about the same thing, which outranks a
# PDF attachment that happens to mention it.
#
# Deliberately not a website/PDF switch. A PDF attachment is the right source for
# plenty of questions (a report's findings, a table), and this must not bury it —
# which is why every tier sits inside one relevance band and only reorders
# candidates the relevance step already called equivalent.
_CANONICAL_BUNDLES = frozenset({
    # Pages and service nodes the organisation maintains as its own description
    # of itself: mission, contact, thematic hubs, centre-of-excellence hubs, the
    # service catalogue. These are short, which is why they lost before.
    "page", "services", "basic",
})
_PRIMARY_BUNDLES = frozenset({
    # The organisation's own published output.
    "report", "policy_brief", "research_papers", "infographics",
})
_PROJECT_BUNDLES = frozenset({"ongoing_projects", "completed_projects"})
_SECONDARY_BUNDLES = frozenset({
    # Dated announcements and third-party coverage. Correct for "what did you
    # launch in March", weak for "what do you offer".
    "news", "press_release", "events", "feature_articles", "article", "videos",
})

_AUTHORITY_CANONICAL = 0.90
_AUTHORITY_PRIMARY = 0.75
_AUTHORITY_PROJECT = 0.60
_AUTHORITY_SECONDARY = 0.45
_AUTHORITY_ATTACHMENT = 0.35


def derived_authority(payload: dict) -> float:
    """Editorial authority in [0,1] inferred from metadata already in the payload.

    Reads ``source_type`` and ``bundle`` only — both are stamped on every chunk at
    ingest, so this needs no new field, no reprojection and no ingest change.

    A note on the attachment tier: ``source_type == "pdf_attachment"`` is scored
    below its own bundle because the attachment is a *derived* artefact of the
    node it hangs off. The clearest case in this corpus is the annual reports,
    where every edition from 2015-16 to 2024-25 hangs off one Drupal node and so
    shares one title and one date; a chunk from deep inside one of them is poor
    evidence for "what does the organisation do", and excellent evidence for a
    figure in that report — which the relevance band, not this, decides.
    """
    if is_graph_facts_payload(payload):
        # Verified relationships, not a retelling of prose. Top of the scale so a
        # facts block is never displaced by a page that merely mentions the same
        # entity.
        return 1.0
    bundle = str(payload.get("bundle") or "").strip().lower()
    source_type = str(payload.get("source_type") or "").strip().lower()

    if source_type == "website":
        if bundle in _CANONICAL_BUNDLES:
            return _AUTHORITY_CANONICAL
        if bundle in _PRIMARY_BUNDLES:
            return _AUTHORITY_PRIMARY
        if bundle in _PROJECT_BUNDLES:
            return _AUTHORITY_PROJECT
        if bundle in _SECONDARY_BUNDLES:
            return _AUTHORITY_SECONDARY
        return _UNKNOWN
    if source_type:
        # Attachments and anything else non-website. Keep the bundle's ordering
        # inside the tier so a policy-brief PDF still leads a news PDF.
        if bundle in _CANONICAL_BUNDLES or bundle in _PRIMARY_BUNDLES:
            return _AUTHORITY_ATTACHMENT + 0.05
        return _AUTHORITY_ATTACHMENT
    return _UNKNOWN


def is_graph_facts_payload(payload: dict) -> bool:
    """Local, import-light check for the graph's verified-relationships block."""
    from app.core.models.context import is_graph_facts

    try:
        return bool(is_graph_facts(payload))
    except Exception:  # pragma: no cover - defence in depth
        return False


def _authority_scores(candidates: Sequence[Candidate]) -> list[float]:
    """Source trustworthiness in [0,1].

    An explicit ``source_authority`` payload value is authoritative and is used
    as given; otherwise it is derived from ``source_type``/``bundle`` by
    :func:`derived_authority`. Before, the absent key meant every candidate
    scored ``_UNKNOWN`` and the key could never reorder anything.
    """
    scores: list[float] = []
    for c in candidates:
        try:
            scores.append(min(1.0, max(0.0, float(c.payload["source_authority"]))))
        except (KeyError, TypeError, ValueError):
            scores.append(derived_authority(c.payload))
    return scores


def _substance_scores(candidates: Sequence[Candidate]) -> list[float]:
    """Log-scaled passage length — the stand-in for "completeness".

    Accuracy cannot be measured at ranking time and neither, strictly, can
    completeness; what is visible is how much a passage actually says, and a
    chunk cut short at a document boundary does carry less of an answer than a
    full one.

    Log scale so the band tolerance reads as a *ratio*: one passage says
    substantially more than another when it holds `rerank_substance_ratio` times
    the text. That claim survives the fact that chunks are already roughly
    uniform in size, where a linear scale would not — min-max normalization would
    inflate the gap between 1,400 and 1,500 characters into a decisive one, which
    is the mistake the relevance blend used to make.

    Measured on the child chunk that matched; the parent expansion happens later,
    in the context builder."""
    return [math.log1p(len(c.text)) for c in candidates]


def _bands(values: Sequence[float], *, tolerance: float) -> list[int]:
    """Band index per value, 0 being the highest band.

    A band starts at its leader and holds every value within `tolerance` of it;
    the first value that falls further than that opens the next band. Grown
    greedily down the sorted order rather than cut into fixed-width buckets, so
    two near-identical values can never land either side of an arbitrary
    boundary — and measured against the *leader* rather than the previous value,
    so a long chain of small steps cannot drift an arbitrarily weak value into
    the top band."""
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
    bands = [0] * len(values)
    band = 0
    leader = values[order[0]]
    for i in order:
        if leader - values[i] > tolerance:
            band += 1
            leader = values[i]
        bands[i] = band
    return bands


class _Scored(NamedTuple):
    """A candidate's ranking signals, before they are cut into bands."""

    candidate: Candidate
    relevance: float   # semantic score plus any table boost; the band is cut from this
    semantic: float    # raw provider score, carried through for the context floors
    substance: float
    recency: float
    authority: float
    # How well the document's period matches the time the question is about.
    # Constant (and therefore inert) whenever there is no temporal intent, which
    # is why the default is the same neutral value every other unknown takes.
    temporal: float = _UNKNOWN


class _Ranked(NamedTuple):
    """A scored candidate placed in the ranking."""

    relevance_band: int   # 0 is the most relevant band
    temporal_band: int    # 0 is the best temporal fit, cut within the relevance band
    recency_band: int     # 0 is the newest; CURRENT questions only, else constant
    authority_band: int   # 0 is the most authoritative, cut within the recency band
    substance_band: int   # 0 is the fullest band, cut within the authority band
    scored: _Scored


def _relevance_tolerance(query: str, settings) -> float:
    """Relevance band width for this query — widened when the topic goes stale.

    Nothing can cross a band however wide it gets, so this only changes how often
    the lower-priority keys are reachable, never whether relevance wins."""
    tolerance = settings.rerank_relevance_tolerance
    if not is_volatile(query):
        return tolerance
    widened = tolerance * settings.rerank_volatile_tolerance_multiplier
    logger.debug("Volatile topic; relevance band widened to %.3f.", widened)
    return widened


def _substance_tolerance(settings) -> float:
    """Completeness band width, as the log of the configured length ratio — see
    `_substance_scores`. A ratio at or below 1 would make every difference in
    length substantial, so it is clamped away."""
    return math.log(max(float(settings.rerank_substance_ratio), 1.0))


def _nested_bands(
    values: Sequence[float], outer: Sequence[Any], *, tolerance: float
) -> list[int]:
    """Band ``values`` separately inside each group of ``outer``.

    Banding across the whole set would let a candidate from a much less relevant
    group place the boundary that splits two similarly relevant ones. Both the
    authority and completeness steps are only ever questions between candidates
    that already tied above them, so both use this.
    """
    bands = [0] * len(values)
    for group in set(outer):
        members = [i for i, b in enumerate(outer) if b == group]
        within = _bands([values[i] for i in members], tolerance=tolerance)
        for i, band in zip(members, within):
            bands[i] = band
    return bands


def _substance_bands(
    scored: Sequence[_Scored], enclosing: Sequence[Any], *, tolerance: float
) -> list[int]:
    """Completeness band per candidate, cut *within* each enclosing band."""
    return _nested_bands(
        [s.substance for s in scored], enclosing, tolerance=tolerance
    )


def _temporal_bands(
    scored: Sequence[_Scored], relevance_bands: Sequence[int]
) -> list[int]:
    """Temporal-fit band per candidate, cut *within* each relevance band.

    Inside, never above: a candidate a relevance band lower cannot climb past
    one above it however perfectly it fits the period, which is the same
    guarantee authority and completeness already have and the reason a newer
    document cannot win on being newer. What this does change is the order of
    candidates the relevance step has already called equivalent — and there,
    covering the year the user asked about is a better reason to lead than being
    a more canonical kind of page, which is why it sits above authority.

    Inert by construction when there is no temporal intent: every candidate then
    scores ``FIT_UNKNOWN``, one band holds all of them, and the key below it
    decides exactly as before.
    """
    return _nested_bands(
        [s.temporal for s in scored], relevance_bands, tolerance=_TEMPORAL_TOLERANCE
    )


#: How much newer one document must be than another to count as better evidence
#: about the present. A year: shorter than the interval over which a post, a
#: policy or a figure typically changes, and long enough that two write-ups of
#: the same season are not separated by it.
_CURRENT_RECENCY_TOLERANCE_DAYS = 365.0
_SECONDS_PER_DAY = 86400.0


def _recency_bands(
    scored: Sequence[_Scored], enclosing: Sequence[Any], *, temporal: Any | None
) -> list[int]:
    """Recency band per candidate — for CURRENT questions only, else constant.

    Why this exists, and why only here. :func:`temporal_fit` answers "does this
    document's period cover the time asked about", which for CURRENT means "is it
    still in force today". That is the right question for an ongoing project or a
    standing page, and the wrong one for the far commoner case of a document that
    *reports* something on a date: a 2020 announcement and a 2023 brief both
    closed their period years ago, so both score ``FIT_MISS`` and the temporal
    band cannot tell them apart. Authority then decided, and a website
    announcement outranks a PDF attachment — which is how "who is the director
    general" came back answered from the older of the two.

    Between two documents that both merely *describe* the present, the newer one
    is the better evidence about it. That is what this band says, and all it says.

    Three properties keep it from becoming "the newest document wins":

    * it is cut *inside* the relevance and temporal-fit bands, so it only ever
      reorders candidates those two have already called equivalent;
    * it only applies when the question is about the present. For every other
      question the band is a constant, the key is inert, and the ordering is
      byte-identical to what it was before this existed — the same guarantee
      :func:`_temporal_bands` has;
    * the tolerance is an *absolute* year. ``_recency_scores`` normalises dates
      across the candidate set, so a fixed tolerance on that value would mean
      two years in one query and nine days in the next. Converting a year into
      the set's own scale keeps the claim the band makes ("materially newer")
      the same claim every time, and collapses to a single band — inert again —
      whenever every candidate was published within about a year of the others.
    """
    from app.retrieval.search.temporal_gate import CURRENT

    if temporal is None or getattr(temporal, "mode", None) != CURRENT:
        return [0] * len(scored)
    span = _recency_span_days(s.candidate for s in scored)
    if span <= 0:
        return [0] * len(scored)
    # `s.recency` is already [0,1] across the set with undated candidates at the
    # neutral midpoint, which is exactly the treatment an unknown needs here too.
    tolerance = _CURRENT_RECENCY_TOLERANCE_DAYS / span
    if tolerance >= 1.0:
        return [0] * len(scored)
    return _nested_bands([s.recency for s in scored], enclosing, tolerance=tolerance)


def _recency_span_days(candidates: Any) -> float:
    """Days between the oldest and newest dated candidate; 0 when undecidable."""
    epochs: list[float] = []
    for c in candidates:
        raw = c.payload.get("effective_start_date")
        if isinstance(raw, str) and raw:
            try:
                epochs.append(
                    datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                )
            except ValueError:
                continue
    if len(epochs) < 2:
        return 0.0
    return (max(epochs) - min(epochs)) / _SECONDS_PER_DAY


def _authority_bands(
    scored: Sequence[_Scored], enclosing: Sequence[Any]
) -> list[int]:
    """Authority band per candidate, cut *within* each enclosing band.

    The enclosing band is (relevance, temporal fit, recency): each key is only
    ever a question between candidates that already tied above it, so authority
    is asked only of candidates that are comparably relevant, comparably
    well-matched to the period, and — on a question about the present — comparably
    recent. Source kind therefore settles which of two equally good passages
    leads; it cannot promote a passage that is years out of date.
    """
    return _nested_bands(
        [s.authority for s in scored], enclosing, tolerance=_AUTHORITY_TOLERANCE
    )


def _sort_key(r: _Ranked) -> tuple[float, ...]:
    """The ranking priority, most significant first: relevance band, then
    temporal-fit band, then authority band, then completeness band, then
    recency, then the fine-grained relevance within the band — a deterministic
    last resort, and by construction a sub-tolerance difference that the band
    already declared immaterial.

    Authority moved above completeness because completeness is a length proxy and
    a canonical page is short: see the module docstring for the measurement that
    prompted it. It stays *below* relevance, so a canonical page that does not
    answer the question still cannot climb over a passage that does.

    Temporal fit sits directly under relevance for the same kind of reason, in
    the other direction: when a question names a time, a passage from that time
    is a better answer than a more canonical passage from a different one — a
    2019 hub page does not answer "what happened in 2023" better than a 2023
    news item. It is still *under* relevance, so this remains a tie-break among
    comparable passages and never "the newest document wins". And with no
    temporal intent every candidate scores alike, so the band is a constant and
    the order is exactly what it was before this key existed.

    The recency band between them applies to questions about the present only
    (see :func:`_recency_bands`), and is the one key that moves *above* authority:
    asked who holds a post now, "this evidence is three years newer" is a better
    reason to lead than "this is a more canonical kind of page". For every other
    question it is a constant and the order below is unchanged.
    """
    return (
        r.relevance_band,
        r.temporal_band,
        r.recency_band,
        r.authority_band,
        r.substance_band,
        -r.scored.recency,
        -r.scored.relevance,
    )


class _Relevance(BaseModel):
    scores: list[float] = Field(description="Relevance 0..1 per candidate, in order.")


def _llm_semantic(query: str, candidates: Sequence[Candidate]) -> list[float] | None:
    from app.core.clients.llm import get_structured_llm

    listing = "\n".join(
        f"[{i}] {c.text[:_LLM_SNIPPET_CHARS]}" for i, c in enumerate(candidates)
    )
    try:
        model = get_structured_llm().with_structured_output(_Relevance)
        result: _Relevance = model.invoke(
            [
                (
                    "system",
                    "Rate how well each numbered passage answers the query, from 0 "
                    "(irrelevant) to 1 (directly answers). Return one score per "
                    "passage, in order.",
                ),
                ("human", f"Query: {query}\n\nPassages:\n{listing}"),
            ]
        )
    except Exception:
        logger.warning("LLM rerank failed; falling back to dense score.", exc_info=True)
        return None
    if len(result.scores) != len(candidates):
        logger.warning("LLM rerank returned %d scores for %d candidates; ignoring.",
                       len(result.scores), len(candidates))
        return None
    return [max(0.0, min(1.0, float(s))) for s in result.scores]


def _cross_encoder_semantic(query: str, candidates: Sequence[Candidate]) -> list[float] | None:
    """Cross-encoder relevance per candidate, squashed to 0..1.

    A cross-encoder emits an unbounded logit — measured on this corpus, roughly
    +4 for a passage that answers the query and -11 for one that does not. Every
    consumer of this number is calibrated in cosine, i.e. 0..1: the relevance
    band width (`rerank_relevance_tolerance`, 0.03), the drop threshold
    (`rerank_score_threshold`) and the corrective loop's trigger
    (`corrective_min_score`). Handing those a logit breaks all three — 0.03 is
    below the gap between any two logits, so every candidate takes its own band
    and the temporal/recency/authority keys stop being reachable, while a
    moderately relevant passage scoring -2 falls under a floor meant to reject
    the weakly related. This is the failure `fusion.rrf` documents for its own
    scale, in the other direction.

    A sigmoid is the model's own calibration rather than an arbitrary rescale:
    these models are trained with BCE on that logit, so sigmoid(logit) is the
    probability the pair is relevant, and it is already the normalisation
    BAAI publish for bge-reranker. That puts it on the same 0..1 footing as a
    cosine and leaves every downstream threshold meaning what it says.
    """
    model_name = get_settings().rerank_model or "BAAI/bge-reranker-v2-m3"
    try:
        encoder = _load_cross_encoder(model_name)
        scores = encoder.predict([(query, c.text) for c in candidates])
        return [_sigmoid(float(s)) for s in scores]
    except Exception:
        logger.warning("cross_encoder rerank unavailable; falling back.", exc_info=True)
        return None


def _sigmoid(x: float) -> float:
    """Logistic squash, written to not overflow on a large-magnitude logit."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


_CROSS_ENCODER_CACHE: dict[str, object] = {}
# Each cached model's own `max_seq_length`, so `rerank_max_seq_length = 0` can
# restore it rather than leaving the last override standing.
_MODEL_MAX_SEQ: dict[str, int] = {}


def _load_cross_encoder(model_name: str):
    if model_name not in _CROSS_ENCODER_CACHE:
        from sentence_transformers import CrossEncoder

        # Loading is seconds and several hundred MB, so the cache is what keeps
        # this off the query path; only the first query pays.
        encoder = CrossEncoder(model_name)
        _CROSS_ENCODER_CACHE[model_name] = encoder
        _MODEL_MAX_SEQ[model_name] = encoder.max_seq_length
    encoder = _CROSS_ENCODER_CACHE[model_name]
    # Assigned on every lookup rather than at construction, and assigned
    # unconditionally. The cache is keyed by model name while `max_seq_length` is
    # mutable state on the cached object, so a load-time-only assignment would
    # pin whatever the setting was for the first query of the process — and
    # skipping the assignment when the setting is 0 would leave the *previous*
    # override in place rather than restoring the model's own default, which is
    # what 0 means. Both were measured as a silently unchanged sequence length.
    settings = get_settings()
    encoder.max_seq_length = (
        settings.rerank_max_seq_length or _MODEL_MAX_SEQ[model_name]
    )
    return encoder


@lru_cache(maxsize=1)
def _cohere_client():
    import os

    import cohere

    return cohere.Client(os.environ.get("COHERE_API_KEY", ""))


def _cohere_semantic(query: str, candidates: Sequence[Candidate]) -> list[float] | None:
    settings = get_settings()
    try:
        # Cached client: constructing one per rerank call rebuilds an HTTP
        # connection pool on every query.
        client = _cohere_client()
        model = settings.rerank_model or "rerank-3.5"
        resp = client.rerank(
            query=query, documents=[c.text for c in candidates], model=model
        )
        scores = [0.0] * len(candidates)
        for r in resp.results:
            scores[r.index] = float(r.relevance_score)
        return scores
    except Exception:
        logger.warning("cohere rerank unavailable; falling back.", exc_info=True)
        return None


def _dense_scores(candidates: Sequence[Candidate]) -> list[float]:
    """The candidates' semantic relevance, on the scale the floors expect.

    Reads ``semantic_score`` rather than ``score``: after ``fusion.rrf`` the
    latter holds a reciprocal-rank value, and using it here propagated that
    scale to every downstream threshold. Falls back to ``score`` for candidates
    built outside the search layer (the graph hydration path, and tests), which
    leave ``semantic_score`` at its default.
    """
    return [c.semantic_score or c.score for c in candidates]


def _semantic_scores(query: str, candidates: Sequence[Candidate], provider: str) -> list[float]:
    dense = _dense_scores(candidates)
    if provider == "llm" and len(candidates) <= _MAX_LLM_CANDIDATES:
        return _llm_semantic(query, candidates) or dense
    if provider == "cross_encoder":
        return _cross_encoder_semantic(query, candidates) or dense
    if provider == "cohere":
        return _cohere_semantic(query, candidates) or dense
    return dense


def _temporal_scores(
    candidates: Sequence[Candidate], temporal: Any | None
) -> list[float]:
    """Temporal fit per candidate, or a flat neutral list.

    Flat — and therefore inert — whenever there is no intent, the intent cannot
    rank (``NONE``/``UPCOMING``), or scoring raises. The last case is the reason
    this is wrapped at all: a temporal signal is an improvement to an ordering
    that already works, and it must never be able to cost a ranking. Same
    posture as ``retriever._gate_temporal``.
    """
    from app.retrieval.search.temporal_gate import FIT_UNKNOWN, temporal_fit

    if temporal is None or not getattr(temporal, "ranks", False):
        return [FIT_UNKNOWN] * len(candidates)
    try:
        return [temporal_fit(c.payload, temporal) for c in candidates]
    except Exception:
        logger.warning("Temporal fit failed; ranking without it.", exc_info=True)
        return [FIT_UNKNOWN] * len(candidates)


def rerank(
    query: str,
    candidates: Sequence[Candidate],
    *,
    top_n: int | None = None,
    table_boost: float = 0.0,
    temporal: Any | None = None,
) -> list[Candidate]:
    """Candidates in ranked order, best first, capped at `top_n`.

    Each returned candidate carries the relevance its band was cut from in
    `score` and the raw provider score in `semantic_score` (the context builder's
    floors read the latter). `score` is not monotone with the returned order:
    inside a band the ranking is by recency, so a newer candidate can lead one
    scoring marginally higher.

    Under the cross_encoder provider only the first `rerank_max_candidates` are
    scored; the rest keep their incoming order behind them."""
    candidates = list(candidates)
    if not candidates:
        return []
    settings = get_settings()
    provider = (settings.reranker_provider or "embedding").lower()

    # A cross-encoder costs one model pass per candidate, so the fused set is
    # capped before it is scored (see `rerank_max_candidates`). The tail is not
    # dropped but held behind every scored candidate, and deliberately not sorted
    # against them: its score is still a cosine while the head's is a normalised
    # cross-encoder relevance, and ranking the two together would let a candidate
    # the first stage put 41st climb over one the reranker judged irrelevant —
    # the scale-mixing this module's other scores are kept apart to avoid.
    tail: list[Candidate] = []
    cap = settings.rerank_max_candidates
    if provider == "cross_encoder" and cap and len(candidates) > cap:
        candidates, tail = candidates[:cap], candidates[cap:]

    semantic = _semantic_scores(query, candidates, provider)
    threshold = settings.rerank_score_threshold
    substance = _substance_scores(candidates)
    recency = _recency_scores(candidates)
    authority = _authority_scores(candidates)
    temporal_fits = _temporal_scores(candidates, temporal)

    kept: list[_Scored] = []
    for cand, sem, sub, rec, auth, fit in zip(
        candidates, semantic, substance, recency, authority, temporal_fits
    ):
        if threshold and sem < threshold:
            continue
        # The boost lifts relevance rather than a final score, so a table-bearing
        # chunk can climb a band when the answer wants a table. Still a nudge and
        # not a filter — and inert when it is smaller than the band tolerance.
        boost = table_boost if table_boost and cand.payload.get("has_table") else 0.0
        kept.append(
            _Scored(
                candidate=cand, relevance=sem + boost, semantic=sem,
                substance=sub, recency=rec, authority=auth, temporal=fit,
            )
        )

    relevance_bands = _bands(
        [s.relevance for s in kept], tolerance=_relevance_tolerance(query, settings)
    )
    temporal_bands = _temporal_bands(kept, relevance_bands)
    recency_bands = _recency_bands(
        kept,
        [(rb, tb) for rb, tb in zip(relevance_bands, temporal_bands)],
        temporal=temporal,
    )
    # Authority is cut inside the bands above for the same reason completeness
    # is cut inside authority: each key is only ever a question between
    # candidates that already tied above it, and banding across the whole set
    # would let a candidate from a worse-fitting group place the boundary.
    authority_bands = _authority_bands(
        kept,
        [(rb, tb, cb) for rb, tb, cb
         in zip(relevance_bands, temporal_bands, recency_bands)],
    )
    # Completeness is cut inside the authority band, not the relevance band: two
    # candidates only compete on length once they are the same *kind* of source,
    # otherwise a long attachment would still set the boundary that splits two
    # canonical pages.
    substance_bands = _substance_bands(
        kept,
        [(rb, tb, cb, ab) for rb, tb, cb, ab
         in zip(relevance_bands, temporal_bands, recency_bands, authority_bands)],
        tolerance=_substance_tolerance(settings),
    )
    ranked = sorted(
        (
            _Ranked(relevance_band=rb, temporal_band=tb, recency_band=cb,
                    authority_band=ab, substance_band=sb, scored=s)
            for rb, tb, cb, ab, sb, s in zip(
                relevance_bands, temporal_bands, recency_bands, authority_bands,
                substance_bands, kept
            )
        ),
        key=_sort_key,
    )
    out = [
        Candidate(
            id=r.scored.candidate.id, score=r.scored.relevance,
            payload=r.scored.candidate.payload, vector=r.scored.candidate.vector,
            semantic_score=r.scored.semantic,
            # Carried, not recomputed: how this candidate was fused stays
            # readable downstream for tracing a ranking.
            fusion_score=r.scored.candidate.fusion_score,
        )
        for r in ranked
    ]
    out.extend(tail)
    return out[:top_n] if top_n else out
