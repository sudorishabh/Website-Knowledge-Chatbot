"""Evaluation harness for the Query Intelligence Layer (Phases A-D).

What this is for
----------------
The four phases each ship behind their own flag, and each was tested in
isolation. What no phase could test is the question this harness exists to
answer: *does the stack actually change what evidence reaches the answer, and
does it change it for the better?*

So the unit here is not "did the function return the right value" but **one
query run end to end through a chosen feature configuration**, with everything
the layer decided recorded in an :class:`EvalRecord`. A scenario then asserts
about the record — the normalized query, the temporal intent, the parts, the
perspectives, the candidate ids that came back, the evidence ids that survived —
rather than about an internal call.

Deliberately deterministic
--------------------------
No LLM, no Qdrant, no MySQL, no network. The three model calls the layer makes
(query understanding, requirement extraction, perspective generation) are
supplied as fixture data per scenario, and the corpus is a list of hand-built
candidates. That is a real limitation and it is stated in the report: this
harness measures *the machinery around the model*, not the model's judgement.
It cannot tell you whether the LLM proposes good perspectives; it can tell you
that a good one is retrieved, fused and kept, and that a bad one is rejected.

What it does not do
-------------------
It does not generate an answer. Generation is downstream of every decision here
and adding it would put an LLM in the deterministic suite for no extra signal —
the evidence that reaches the prompt is what these phases change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Sequence

from app.pipeline import query_pipeline as pipe
from app.retrieval import retriever, subqueries as subq
from app.retrieval.search import strategies, temporal_gate as tg
from app.retrieval.search.hybrid_search import Candidate
from app.retrieval.understanding import clarify
from app.retrieval.understanding import query_processor as qp

# --------------------------------------------------------------------------- #
# The feature matrix — the repository's own flag names
# --------------------------------------------------------------------------- #

CLARIFICATION = "clarification_enabled"
TEMPORAL = "temporal_intent_enabled"
DECOMPOSITION = "subquery_planning_enabled"
PERSPECTIVES = "multi_query_enabled"

FLAGS: tuple[str, ...] = (CLARIFICATION, TEMPORAL, DECOMPOSITION, PERSPECTIVES)


@dataclass(frozen=True)
class Features:
    """One row of the feature matrix. Every flag defaults off — the baseline."""

    clarification: bool = False
    temporal: bool = False
    decomposition: bool = False
    perspectives: bool = False
    label: str = "baseline"

    def as_settings(self) -> dict[str, bool]:
        return {
            CLARIFICATION: self.clarification,
            TEMPORAL: self.temporal,
            DECOMPOSITION: self.decomposition,
            PERSPECTIVES: self.perspectives,
        }


BASELINE = Features(label="baseline")
CLARIFIED = Features(clarification=True, label="clarified")
TEMPORAL_ONLY = Features(temporal=True, label="temporal")
DECOMPOSED = Features(decomposition=True, label="decomposed")
PERSPECTIVE = Features(perspectives=True, label="perspective")
FULL_STACK = Features(True, True, True, True, label="full stack")

#: The compact matrix the evaluation report is written against.
MATRIX: tuple[Features, ...] = (
    BASELINE, CLARIFIED, TEMPORAL_ONLY, DECOMPOSED, PERSPECTIVE, FULL_STACK,
)


# --------------------------------------------------------------------------- #
# What one run records
# --------------------------------------------------------------------------- #

@dataclass
class EvalRecord:
    """Everything the Query Intelligence Layer decided about one query.

    Reading order matches the pipeline: what was asked, what it was turned into,
    what was planned, what was pulled, what survived.
    """

    original: str
    features: str = "baseline"

    # Understanding / clarification (Phase A)
    normalized: str = ""
    clarified: bool = False
    clarification_question: str = ""
    options: list[str] = field(default_factory=list)
    clarified_from: str | None = None

    # Temporal (Phase B)
    temporal_mode: str = tg.NONE
    temporal_window: tuple[str | None, str | None] = (None, None)

    # Decomposition (Phase C)
    requirements: list[str] = field(default_factory=list)
    subqueries: list[str] = field(default_factory=list)
    routes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # Perspectives (Phase D)
    perspectives: list[str] = field(default_factory=list)

    # Retrieval
    candidate_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def leads(self) -> str | None:
        """The top piece of evidence, or None when nothing was retrieved."""
        return self.evidence_ids[0] if self.evidence_ids else None

    def recalled(self, *ids: str) -> bool:
        """Whether every named passage reached the final evidence."""
        return set(ids) <= set(self.evidence_ids)


# --------------------------------------------------------------------------- #
# Building a corpus
# --------------------------------------------------------------------------- #

def doc(
    doc_id: str,
    *,
    relevance: float,
    start: str | None = None,
    end: str | None = None,
    bundle: str = "page",
    source_type: str = "website",
    is_current: bool = True,
    text: str = "a passage",
    **payload: Any,
) -> Candidate:
    """One retrievable passage, with the payload fields ranking actually reads.

    `relevance` is the semantic score, set explicitly so a scenario states the
    relevance relationship it is testing instead of inheriting one from an
    embedding model.
    """
    full = {
        "source_type": source_type, "bundle": bundle, "is_current": is_current,
        "chunk_id": doc_id, "chunk_text": text, **payload,
    }
    if start:
        full["effective_start_date"] = start
    if end:
        full["effective_end_date"] = end
    return Candidate(
        id=doc_id, score=relevance, payload=full, vector=_one_hot(doc_id),
        semantic_score=relevance,
    )


#: Wide enough that two distinct ids in a scenario do not collide, and one-hot so
#: that when they do not, they are exactly orthogonal.
_DIMS = 512


def _stable_index(text: str) -> int:
    """A deterministic index for ``text`` — not ``hash()``, which is salted per
    process and would make a fixture's vectors differ between runs."""
    acc = 0
    for ch in text:
        acc = (acc * 131 + ord(ch)) % _DIMS
    return acc


def _one_hot(text: str) -> list[float]:
    """A vector that is identical to itself and orthogonal to everything else.

    The context builder de-duplicates admitted blocks by cosine, so the fixture
    vectors decide which passages it treats as the same text. An angle-based
    scheme put "base" and "services-doc" 2 degrees apart — cosine 0.9994, above
    the dedup threshold — and silently dropped one of them, which read as a
    recall failure that was really a fixture artefact. One-hot removes the
    question: distinct ids are orthogonal, so only genuine repeats dedup.
    """
    vector = [0.0] * _DIMS
    vector[_stable_index(text)] = 1.0
    return vector


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #

_RETRIEVE_SETTINGS = dict(
    retrieval_top_k=6, retrieval_candidate_k=40, prefer_website_enabled=False,
    website_candidate_k=20, multi_query_paraphrases=2,
    multi_query_distinct_threshold=0.92, keyword_leg_enabled=False,
    corrective_loop_enabled=False, corrective_min_score=0.2,
    graph_routing_enabled=False, graph_shadow_enabled=False,
    rerank_table_boost=0.15, reranker_provider="embedding",
    rerank_score_threshold=0.0, rerank_relevance_tolerance=0.03,
    rerank_volatile_tolerance_multiplier=2.0, rerank_substance_ratio=1.5,
    rerank_max_candidates=40, rerank_max_seq_length=0,
    subquery_max=3,
    # Read by the context builder.
    context_token_budget=9000, dedup_cosine_threshold=0.999,
    website_max_slots=6, website_chunk_floor=0.0,
    pdf_max_slots=6, pdf_high_confidence_floor=0.0,
)


class Harness:
    """Drives one query through a chosen feature configuration.

    Constructed per test with a ``monkeypatch``. The three model calls are
    fixture data; the corpus is whatever ``base`` (and the optional per-leg
    corpora) say. Everything between them is the real code — the real
    `process`, the real planner, the real RRF, the real reranker, the real
    context builder.
    """

    def __init__(self, monkeypatch, features: Features = BASELINE):
        self.monkeypatch = monkeypatch
        self.features = features
        self.settings = SimpleNamespace(
            **_RETRIEVE_SETTINGS, **features.as_settings()
        )
        self._apply_flags()

    def _apply_flags(self) -> None:
        from app.config import get_settings

        live = get_settings()
        for flag, value in self.features.as_settings().items():
            self.monkeypatch.setattr(live, flag, value, raising=False)
        # `retrieve` and the reranker read settings through their own modules.
        from app.retrieval.context import builder as context_builder
        from app.retrieval.search import reranker

        for module in (retriever, reranker, context_builder):
            self.monkeypatch.setattr(
                module, "get_settings", lambda s=self.settings: s
            )

    # -- understanding -------------------------------------------------- #

    def understanding(
        self,
        *,
        rewrite: str,
        intents: Sequence[tuple[str, float]] = (("qa", 0.9),),
        **scope: Any,
    ) -> "Harness":
        """Fix what query understanding returns for this run."""
        result = qp.QueryUnderstanding(
            query_rewrite=rewrite,
            intents=[
                qp.IntentPrediction(label=lbl, confidence=c) for lbl, c in intents
            ],
            scope=qp.QueryScope(**scope),
        )

        class _Model:
            def with_structured_output(self, _schema):
                return self

            def invoke(self, _messages):
                return result

        self.monkeypatch.setattr(qp, "get_structured_llm", lambda: _Model())
        self.monkeypatch.setattr(qp, "_facet_filters", lambda a: [])
        self.monkeypatch.setattr(qp, "_edition_conditions", lambda q: [])
        return self

    def requirements(self, *items: str) -> "Harness":
        """Fix what `answer_plan.extract_requirements` returns."""
        self._requirements = list(items)
        return self

    def generated_perspectives(self, *items: str) -> "Harness":
        """Fix what the perspective generator proposes (before the guards)."""
        self._generated = list(items)
        return self

    # -- corpus ----------------------------------------------------------- #

    def corpus(
        self,
        base: Sequence[Candidate],
        *,
        per_perspective: dict[str, Sequence[Candidate]] | None = None,
        per_subquery: dict[str, Sequence[Candidate]] | None = None,
    ) -> "Harness":
        """What each retrieval leg returns.

        ``base`` is the original query's own pull — the leg that always runs.
        The other two map a derived query's text to what it alone can reach,
        which is how a recall scenario states "only this leg finds B".
        """
        self._base = list(base)
        self._per_perspective = dict(per_perspective or {})
        self._per_subquery = dict(per_subquery or {})
        return self

    # -- run -------------------------------------------------------------- #

    def run(self, question: str, history: list[dict[str, str]] | None = None) -> EvalRecord:
        mp = self.monkeypatch
        record = EvalRecord(original=question, features=self.features.label)

        pq = qp.process(question, history)
        record.normalized = pq.search_query
        record.clarified_from = pq.clarified_from
        record.temporal_mode = pq.temporal_intent.mode
        record.temporal_window = (
            pq.temporal_intent.date_from, pq.temporal_intent.date_to
        )
        if pq.clarification is not None:
            record.clarified = True
            record.clarification_question = pq.clarification.question
            record.options = list(pq.clarification.options)
            # A clarification turn answers nothing and retrieves nothing; that
            # *is* the behaviour under test, so the record stops here.
            return record

        # Phase C: the plan, built the way the pipeline builds it.
        record.requirements = list(getattr(self, "_requirements", []))
        plan = pipe._subquery_plan(pq, _Ready(record.requirements))
        record.subqueries = [s.text for s in plan]
        record.routes = {s.text: s.routes for s in plan}

        # Phase D: the guards run for real; only generation is fixture data.
        generated = list(getattr(self, "_generated", []))
        captured: list[str] = []

        def fake_perspectives(query, n, **kw):
            kept = strategies._distinct(
                query, generated, n=n, query_vector=kw.get("query_vector")
            )
            captured.extend(p.text for p in kept)
            return kept

        mp.setattr(retriever, "perspectives", fake_perspectives)
        mp.setattr(
            retriever, "perspective_search",
            lambda q, **kw: list(self._per_perspective.get(q, [])),
        )
        mp.setattr(
            retriever, "subquery_search",
            lambda q, **kw: list(self._per_subquery.get(q, [])),
        )
        mp.setattr(retriever, "search", lambda *a, **k: list(self._base))
        mp.setattr(retriever, "dual_search", lambda *a, **k: list(self._base))
        mp.setattr(retriever, "title_search", lambda *a, **k: [])
        mp.setattr(retriever, "_observe_in_shadow", lambda *a, **k: None)
        mp.setattr(strategies, "embed_query", _query_vector)

        captured_candidates: list[Candidate] = []
        real_rerank = retriever.rerank

        def watching_rerank(query, candidates, **kw):
            captured_candidates.extend(candidates)
            return real_rerank(query, candidates, **kw)

        mp.setattr(retriever, "rerank", watching_rerank)

        blocks = retriever.retrieve(
            pq.search_query,
            n=6,
            query_vector=_query_vector(pq.search_query),
            filters=pq.filters,
            answer_format=pq.answer_format,
            source_type=pq.source_type,
            capabilities={p.label for p in (pq.understanding.intents if pq.understanding else [])},
            temporal=pipe._temporal_intent(pq),
            subqueries=plan,
        )

        record.perspectives = captured
        record.candidate_ids = list(dict.fromkeys(c.id for c in captured_candidates))
        record.evidence_ids = [
            b.payload.get("chunk_id") or b.payload.get("document_id") or str(b.n)
            for b in blocks
        ]
        record.scores = {
            (b.payload.get("chunk_id") or str(b.n)): round(b.score, 4) for b in blocks
        }
        return record


class _Ready:
    """A already-resolved stand-in for the requirements future."""

    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


def _query_vector(text: str) -> list[float]:
    """A stable embedding for a query string.

    One-hot for the same reason the corpus vectors are: two different queries
    must not look identical to Phase D's semantic distinctness guard. A shared
    constant vector made every perspective score cosine 1.0 against the original
    and be rejected — the guard working correctly on a fixture that was lying to
    it. With distinct directions the *lexical* guard is what decides here, which
    keeps these scenarios about routing and fusion; the semantic guard has its
    own tests with hand-placed vectors in
    `tests/retrieval/search/test_perspectives.py`.
    """
    return _one_hot(text.strip().lower())
