"""Does the internal evidence already answer the question?

:func:`assess` looks at the reranked internal candidates the way a reader would
before reaching for another source: do they mention what the question names;
for a person, is there a page *about* them; for a passage hunt, does one passage
contain the words; for a period, is anything from it here; for a comparison,
is each side covered. Each failed check is a named reason, and the measured
values behind every check are kept as signals — so the trace can answer both
"why did it search the web?" and "why did it not?".

Why not a relevance threshold
-----------------------------
Measured on this corpus, a relevance score cannot make this call alone. "What is
TERI SAS" retrieved passages at 0.66 cosine that were the organisation's
boilerplate self-description, and the cross-encoder scored passages about
SASMIRA — a different body — at 0.99; the answer was then refused. A floor
tuned to reject that would reject good answers elsewhere. What was actually
wrong is visible without a score: none of the passages said "SAS". So the
checks here are about what the evidence *contains*; the optional relevance floor
(`web_min_internal_relevance`) is off until it has been calibrated for the
deployment's reranker.

:func:`decide` then combines the verdict with the plan's forced reasons — an
explicit request, a freshness question — into the one yes/no the service acts
on, with every reason that contributed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import get_settings
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.web.planner import FRESHNESS, WebPlan

__all__ = ["Sufficiency", "WebDecision", "assess", "decide"]

NO_INTERNAL_EVIDENCE = "no_internal_evidence"
LOW_RELEVANCE = "low_relevance"
SUBJECT_ABSENT = "subject_absent"
NO_PROFILE = "no_profile"
PASSAGE_NOT_FOUND = "passage_not_found"
DATE_WINDOW_UNCOVERED = "date_window_uncovered"
PERIOD_UNCOVERED = "period_uncovered"
DOCUMENT_NOT_FOUND = "document_not_found"

# How many of the reranked candidates are read: the ones that can reach the
# context, with some depth beyond it.
_MIN_CONSIDERED = 8

_HONORIFIC = re.compile(r"^(?:mr|mrs|ms|dr|prof|professor|shri|smt|sri)\.?\s+", re.IGNORECASE)
_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ied", "ed",
             "es", "s")
_PEOPLE_BUNDLES = frozenset({"people"})


@dataclass(frozen=True)
class Sufficiency:
    """The verdict on the internal evidence: sufficient, or the reasons not."""

    sufficient: bool
    reasons: tuple[str, ...] = ()
    signals: dict[str, Any] = field(default_factory=dict)

    def to_trace(self) -> dict[str, Any]:
        return {"sufficient": self.sufficient, "reasons": list(self.reasons),
                "signals": self.signals}


@dataclass(frozen=True)
class WebDecision:
    """Whether to consult the web for this question, and every reason why."""

    search: bool
    reasons: tuple[str, ...] = ()
    sufficiency: Sufficiency | None = None

    def to_trace(self) -> dict[str, Any]:
        record: dict[str, Any] = {"search": self.search, "reasons": list(self.reasons)}
        if self.sufficiency is not None:
            record["internal"] = self.sufficiency.to_trace()
        return record


# --- matching ---------------------------------------------------------------


def _fold(text: str) -> str:
    """Lowercase, with British and American spellings made equal
    ("fertiliser" / "fertilizer", "organisation" / "organization")."""
    return (text or "").lower().replace("iz", "is").replace("yz", "ys")


def _stem(word: str) -> str:
    word = _fold(word)
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


# Stems at least this long are also looked for with spaces and hyphens removed,
# so a compound matches however it is written ("aquafeed" / "aqua feeds" /
# "aqua-feed"). Short stems are not: joined up, "air" is inside "chair".
_COMPOUND_MIN = 6


def _has_term(term: str, text: str) -> bool:
    """Whether ``text`` (already folded) contains ``term`` in any inflection —
    "replacing" is found in "can replace" — or, for a long term, in any
    compound spelling; for a hyphenated term, every part of it."""
    parts = [p for p in re.split(r"[-\s]+", term) if p]
    if not parts:
        return False
    squashed: str | None = None
    for part in parts:
        stem = _stem(part)
        if re.search(rf"\b{re.escape(stem)}", text):
            continue
        if len(stem) >= _COMPOUND_MIN:
            squashed = squashed if squashed is not None else re.sub(r"[\s\-]+", "", text)
            if stem in squashed:
                continue
        return False
    return True


def _has_phrase(phrase: str, text: str) -> bool:
    """Whether ``text`` (already folded) contains ``phrase`` word for word, with
    any whitespace between the words and nothing else."""
    words = _fold(phrase).split()
    if not words:
        return False
    pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
    return re.search(pattern, text) is not None


def _name(entity: str) -> str:
    return _HONORIFIC.sub("", " ".join(entity.split())).strip()


def _text_of(candidate: Candidate) -> str:
    payload = candidate.payload
    parts = (payload.get("title"), payload.get("section_heading"), candidate.text)
    return _fold(" ".join(str(p) for p in parts if p))


def _year_of(candidate: Candidate) -> int | None:
    raw = str(candidate.payload.get("effective_start_date") or "")
    return int(raw[:4]) if raw[:4].isdigit() else None


# --- the checks -------------------------------------------------------------


def _subjects(plan: WebPlan, texts: list[str], signals: dict) -> str | None:
    subjects = list(dict.fromkeys(_name(e) for e in plan.entities if _name(e)))
    if plan.person:
        # The person has a check of their own; counting their name here too
        # would let a document merely authored by them satisfy this one.
        person_words = {w.lower() for w in plan.person.split()}
        subjects = [s for s in subjects if not {w.lower() for w in s.split()} <= person_words]
    if not subjects:
        return None
    found = {s: any(_has_phrase(s, t) for t in texts) for s in subjects}
    coverage = sum(found.values()) / len(found)
    signals["subjects"] = found
    signals["subject_coverage"] = round(coverage, 3)
    minimum = float(get_settings().web_subject_min_coverage)
    return SUBJECT_ABSENT if coverage < minimum else None


def _profile(plan: WebPlan, considered: list[Candidate], signals: dict) -> str | None:
    """A named person needs a page about them: one whose title names them, or
    the organisation's people pages. A document they merely wrote is not one."""
    if not plan.person:
        return None
    for candidate in considered:
        title = _fold(str(candidate.payload.get("title") or ""))
        bundle = str(candidate.payload.get("bundle") or "").lower()
        if _has_phrase(plan.person, title) or (
            bundle in _PEOPLE_BUNDLES and _has_phrase(plan.person, _text_of(candidate))
        ):
            signals["profile"] = candidate.id
            return None
    signals["profile"] = None
    return NO_PROFILE


def _passage(plan: WebPlan, considered: list[Candidate], texts: list[str],
             signals: dict) -> str | None:
    """A passage hunt needs one passage holding the words — all of any quoted
    phrase, and most of the rest — not several passages holding a few each."""
    if not plan.find_passage or not (plan.terms or plan.phrases):
        return None
    best, best_id = 0.0, None
    for candidate, text in zip(considered, texts):
        if plan.phrases and not all(_has_phrase(p, text) for p in plan.phrases):
            continue
        coverage = (sum(_has_term(t, text) for t in plan.terms) / len(plan.terms)
                    if plan.terms else 1.0)
        if coverage > best:
            best, best_id = coverage, candidate.id
    signals["passage_coverage"] = round(best, 3)
    signals["passage"] = best_id
    minimum = float(get_settings().web_passage_min_coverage)
    return PASSAGE_NOT_FOUND if best < minimum else None


def _window(plan: WebPlan, considered: list[Candidate], signals: dict) -> str | None:
    """A question about a period needs evidence dated inside it."""
    if not (plan.date_from or plan.date_to):
        return None
    lo, hi = plan.date_from or "0000", plan.date_to or "9999"
    inside = [c.id for c in considered
              if lo <= str(c.payload.get("effective_start_date") or "")[:10] < hi]
    signals["in_window"] = len(inside)
    return None if inside else DATE_WINDOW_UNCOVERED


def _periods(plan: WebPlan, considered: list[Candidate], texts: list[str],
             signals: dict) -> list[str]:
    """A comparison of named years needs something from each year: a document
    dated then, or one that talks about it."""
    if not (plan.comparison and len(plan.years) >= 2) or plan.date_from or plan.date_to:
        return []
    covered: dict[str, bool] = {}
    for year in plan.years:
        covered[str(year)] = any(
            _year_of(c) == year or re.search(rf"\b{year}\b", t)
            for c, t in zip(considered, texts)
        )
    signals["periods"] = covered
    return [f"{PERIOD_UNCOVERED}:{y}" for y, ok in covered.items() if not ok]


def _document(plan: WebPlan, considered: list[Candidate], signals: dict) -> str | None:
    """A request for a named document needs that document: a candidate whose
    title holds the name the user quoted."""
    if not (plan.document_request and plan.phrases):
        return None
    titles = [_fold(str(c.payload.get("title") or "")) for c in considered]
    found = any(all(_has_phrase(p, t) for p in plan.phrases) for t in titles)
    signals["document_found"] = found
    return None if found else DOCUMENT_NOT_FOUND


def assess(plan: WebPlan, ranked: Sequence[Candidate], *, top_n: int | None = None
           ) -> Sufficiency:
    """The verdict on the reranked internal candidates for this plan."""
    settings = get_settings()
    n = top_n or max(_MIN_CONSIDERED, int(settings.retrieval_top_k))
    considered = list(ranked)[:n]
    if not considered:
        return Sufficiency(False, (NO_INTERNAL_EVIDENCE,), {"considered": 0})

    texts = [_text_of(c) for c in considered]
    top = max(c.semantic_score or c.score for c in considered)
    dates = sorted(str(c.payload.get("effective_start_date"))[:10]
                   for c in considered if c.payload.get("effective_start_date"))
    signals: dict[str, Any] = {
        "considered": len(considered),
        "top_relevance": round(float(top), 4),
        "newest_internal": dates[-1] if dates else None,
    }
    reasons: list[str] = []
    floor = float(settings.web_min_internal_relevance)
    if floor and top < floor:
        reasons.append(LOW_RELEVANCE)
    for reason in (
        _subjects(plan, texts, signals),
        _profile(plan, considered, signals),
        _passage(plan, considered, texts, signals),
        _window(plan, considered, signals),
        _document(plan, considered, signals),
    ):
        if reason:
            reasons.append(reason)
    reasons.extend(_periods(plan, considered, texts, signals))
    return Sufficiency(not reasons, tuple(reasons), signals)


def decide(plan: WebPlan, sufficiency: Sufficiency | None) -> WebDecision:
    """Whether to consult the web.

    An explicit request always does. Freshness does when ``web_on_freshness``.
    Otherwise the web is consulted only when ``web_fallback_enabled`` and the
    internal evidence was judged insufficient — and when it was judged
    sufficient, the decision records that, which is the answer to "why did it
    not search the web?".
    """
    settings = get_settings()
    reasons = [r for r in plan.forced_reasons
               if r != FRESHNESS or settings.web_on_freshness]
    if sufficiency is not None and not sufficiency.sufficient and settings.web_fallback_enabled:
        reasons.extend(sufficiency.reasons)
    return WebDecision(search=bool(reasons), reasons=tuple(reasons), sufficiency=sufficiency)
