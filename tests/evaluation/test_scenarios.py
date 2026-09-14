"""Phase E: the 32 evaluation scenarios, run through the harness.

Each test drives one query end to end under a chosen feature configuration and
asserts about the :class:`EvalRecord` — what the layer decided and what evidence
survived — rather than about an internal call. Numbering follows the Phase E
brief so a scenario can be found from the report.

Deterministic throughout: the three model calls are fixture data, the corpus is
hand-built. See ``harness.py`` for what that does and does not let us claim.
"""

from __future__ import annotations

import pytest

from app.retrieval.search import temporal_gate as tg
from app.retrieval.understanding import clarify
from tests.evaluation.harness import (
    BASELINE, CLARIFIED, DECOMPOSED, FULL_STACK, PERSPECTIVE, TEMPORAL_ONLY,
    Features, Harness, doc,
)


def _h(monkeypatch, features=BASELINE):
    return Harness(monkeypatch, features)


def _answered(history_question, clarification_text):
    return [
        {"role": "user", "content": history_question},
        {"role": "assistant", "content": clarification_text},
    ]


# ═══════════════════════════════════════════════════════════════════════════ #
# Clarification (1-6)
# ═══════════════════════════════════════════════════════════════════════════ #

def test_01_clear_question_is_answered_directly(monkeypatch):
    record = (
        _h(monkeypatch, CLARIFIED)
        .understanding(rewrite="TERI revenue in 2023", intents=[("qa", 0.95)])
        .corpus([doc("a", relevance=0.9)])
        .run("What was TERI's revenue in 2023?")
    )
    assert not record.clarified
    assert record.evidence_ids == ["a"]


def test_02_a_genuinely_ambiguous_question_clarifies(monkeypatch):
    record = (
        _h(monkeypatch, CLARIFIED)
        .understanding(rewrite="show me a table",
                       intents=[("clarification_needed", 0.9)])
        .corpus([doc("a", relevance=0.9)])
        .run("show me a table")
    )
    assert record.clarified
    assert record.clarification_question
    assert record.evidence_ids == [], "a clarification turn retrieves nothing"


def test_03_catalog_backed_options_are_offered(monkeypatch):
    """Options come from the catalog's own registry — "projects" spans two
    content types — and there are between two and four of them."""
    record = (
        _h(monkeypatch, CLARIFIED)
        .understanding(rewrite="show me the projects",
                       intents=[("clarification_needed", 0.9)])
        .corpus([])
        .run("show me the projects")
    )
    assert record.clarified
    assert clarify.MIN_OPTIONS <= len(record.options) <= clarify.MAX_OPTIONS
    assert record.options == ["Completed Projects", "Ongoing Projects"]


def test_04_the_reply_merges_with_the_original_request(monkeypatch):
    text = clarify.Clarification(
        question="Which of these did you mean?",
        options=["Completed Projects", "Ongoing Projects"],
    ).render()
    record = (
        _h(monkeypatch, CLARIFIED)
        .understanding(rewrite="TERI ongoing projects", intents=[("qa", 0.9)])
        .corpus([doc("a", relevance=0.9)])
        .run("Ongoing Projects", _answered("show me the projects", text))
    )
    assert record.clarified_from == "show me the projects"
    assert not record.clarified


def test_05_a_second_clarification_is_prevented(monkeypatch):
    """Even when the reply is still unclear, the loop guard holds."""
    text = clarify.Clarification(question="Which did you mean?").render()
    record = (
        _h(monkeypatch, CLARIFIED)
        .understanding(rewrite="dunno", intents=[("clarification_needed", 0.9)])
        .corpus([doc("a", relevance=0.9)])
        .run("dunno", _answered("show me performance", text))
    )
    assert not record.clarified
    assert record.clarified_from == "show me performance"


def test_06_clarification_off_preserves_the_original_behaviour(monkeypatch):
    record = (
        _h(monkeypatch, BASELINE)
        .understanding(rewrite="show me a table",
                       intents=[("clarification_needed", 0.9)])
        .corpus([doc("a", relevance=0.9)])
        .run("show me a table")
    )
    assert not record.clarified
    assert record.evidence_ids == ["a"]


# ═══════════════════════════════════════════════════════════════════════════ #
# Temporal (7-12)
# ═══════════════════════════════════════════════════════════════════════════ #

_STALE = doc("stale", relevance=0.80, bundle="news",
             start="2016-01-01", end="2016-02-01")
_VALID = doc("valid", relevance=0.80, bundle="ongoing_projects", start="2016-01-01")


def test_07_a_current_question_favours_currently_valid_evidence(monkeypatch):
    record = (
        _h(monkeypatch, TEMPORAL_ONLY)
        .understanding(rewrite="what is the current status of the programme")
        .corpus([_STALE, _VALID])
        .run("what is the current status of the programme?")
    )
    assert record.temporal_mode == tg.CURRENT
    assert record.leads() == "valid"


def test_08_a_historical_question_favours_the_closed_period(monkeypatch):
    record = (
        _h(monkeypatch, TEMPORAL_ONLY)
        .understanding(rewrite="what was the previous status of the programme")
        .corpus([_VALID, _STALE])
        .run("what was the previous status?")
    )
    assert record.temporal_mode == tg.PAST
    assert record.leads() == "stale"
    assert record.recalled("valid"), "history reorders evidence, never drops it"


def test_09_point_in_time_carries_the_window_understanding_extracted(monkeypatch):
    """The window is the one `date_conditions` already applies — Phase B scores
    fit against it, it does not re-derive it."""
    record = (
        _h(monkeypatch, TEMPORAL_ONLY)
        .understanding(rewrite="what was the status as of 2019",
                       date_from="2019-01-01", date_to_inclusive="2019-12-31")
        .corpus([doc("a", relevance=0.9, start="2019-06-01")])
        .run("what was the status as of 2019?")
    )
    assert record.temporal_mode == tg.POINT_IN_TIME
    assert record.temporal_window == ("2019-01-01", "2020-01-01")


def test_10_a_date_range_prefers_evidence_inside_the_window(monkeypatch):
    inside = doc("inside", relevance=0.80, start="2023-04-01", end="2023-06-01")
    straddling = doc("straddling", relevance=0.80, start="2020-01-01", end="2025-01-01")
    record = (
        _h(monkeypatch, TEMPORAL_ONLY)
        .understanding(rewrite="what changed between 2023 and 2024",
                       date_from="2023-01-01", date_to_inclusive="2023-12-31")
        .corpus([straddling, inside])
        .run("what changed between 2023 and 2024?")
    )
    assert record.temporal_mode == tg.DATE_RANGE
    assert record.leads() == "inside"
    assert record.recalled("straddling")


def test_11_newer_but_weaker_evidence_still_loses(monkeypatch):
    record = (
        _h(monkeypatch, TEMPORAL_ONLY)
        .understanding(rewrite="what is the current status of the programme")
        .corpus([
            doc("new-weak", relevance=0.35, bundle="ongoing_projects",
                start="2026-01-01"),
            doc("old-strong", relevance=0.92, start="2011-01-01"),
        ])
        .run("what is the current status of the programme?")
    )
    assert record.leads() == "old-strong"


def test_12_no_temporal_intent_creates_no_freshness_bias(monkeypatch):
    """Equivalence, not an assertion about one pair: with no temporal wording,
    the ordering is the baseline's."""
    corpus = [
        doc("older", relevance=0.80, start="2011-01-01"),
        doc("newer", relevance=0.80, start="2026-01-01"),
    ]
    with_flag = (
        _h(monkeypatch, TEMPORAL_ONLY)
        .understanding(rewrite="what does the programme cover")
        .corpus(corpus).run("what does the programme cover?")
    )
    assert with_flag.temporal_mode == tg.NONE

    baseline = (
        _h(monkeypatch, BASELINE)
        .understanding(rewrite="what does the programme cover")
        .corpus(corpus).run("what does the programme cover?")
    )
    assert with_flag.evidence_ids == baseline.evidence_ids


# ═══════════════════════════════════════════════════════════════════════════ #
# Decomposition / routing (13-17)
# ═══════════════════════════════════════════════════════════════════════════ #

def test_13_a_multi_part_question_produces_retrievable_parts(monkeypatch):
    record = (
        _h(monkeypatch, DECOMPOSED)
        .understanding(rewrite="TERI services and certifications")
        .requirements("services", "certifications")
        .corpus([doc("base", relevance=0.9)])
        .run("what services and certifications does TERI offer?")
    )
    assert len(record.subqueries) == 2
    assert all("TERI" in s for s in record.subqueries), "parts keep their subject"


def test_14_parts_can_route_to_different_mechanisms(monkeypatch):
    """The semantic leg always; the graph only for a part naming a relationship.
    MySQL stays routed for the whole question (see the Phase C limitation)."""
    record = (
        _h(monkeypatch, DECOMPOSED)
        .understanding(rewrite="TERI SDG 7 projects and their funders")
        .requirements("projects listed", "funded by")
        .corpus([doc("base", relevance=0.9)])
        .run("which TERI SDG 7 projects are there and who funded them?")
    )
    routes = record.routes
    assert all("semantic" in r for r in routes.values())
    assert any("graph" in r for r in routes.values()), "a relational part exists"


def test_15_a_simple_question_keeps_the_single_query_path(monkeypatch):
    record = (
        _h(monkeypatch, DECOMPOSED)
        .understanding(rewrite="TERI mission")
        .requirements("mission")
        .corpus([doc("base", relevance=0.9)])
        .run("what is TERI's mission?")
    )
    assert record.subqueries == []
    assert record.evidence_ids == ["base"]


def test_16_a_mixed_question_produces_complementary_evidence(monkeypatch):
    """Each part reaches a passage the base pull alone does not."""
    record = (
        _h(monkeypatch, DECOMPOSED)
        .understanding(rewrite="TERI services and certifications")
        .requirements("services", "certifications")
        .corpus(
            [doc("base", relevance=0.90)],
            per_subquery={
                "TERI services": [doc("services-doc", relevance=0.88)],
                "TERI certifications": [doc("certs-doc", relevance=0.88)],
            },
        )
        .run("what services and certifications does TERI offer?")
    )
    assert record.recalled("base", "services-doc", "certs-doc")


def test_17_evidence_two_parts_both_reach_is_not_duplicated(monkeypatch):
    shared = doc("shared", relevance=0.88)
    record = (
        _h(monkeypatch, DECOMPOSED)
        .understanding(rewrite="TERI services and certifications")
        .requirements("services", "certifications")
        .corpus(
            [doc("base", relevance=0.90)],
            per_subquery={
                "TERI services": [shared],
                "TERI certifications": [shared],
            },
        )
        .run("what services and certifications does TERI offer?")
    )
    assert record.evidence_ids.count("shared") == 1
    assert len(record.evidence_ids) == len(set(record.evidence_ids))


# ═══════════════════════════════════════════════════════════════════════════ #
# Perspectives (18-24)
# ═══════════════════════════════════════════════════════════════════════════ #

_ONLY_BASE = doc("A", relevance=0.90)
_ONLY_PERSPECTIVE = doc("B", relevance=0.88)


def test_18_19_20_a_perspective_recovers_evidence_the_query_misses(monkeypatch):
    """Scenarios 18-20 together, because they are one measurement: the base
    query alone misses B (18), the perspective reaches it (19), and RRF keeps it
    in the final evidence (20)."""
    baseline = (
        _h(monkeypatch, BASELINE)
        .understanding(rewrite="what issues affected the rollout programme")
        .corpus([_ONLY_BASE],
                per_perspective={"programme delays and blockers": [_ONLY_PERSPECTIVE]})
        .run("what issues affected the rollout programme?")
    )
    assert baseline.evidence_ids == ["A"], "18: the original query alone misses B"

    improved = (
        _h(monkeypatch, PERSPECTIVE)
        .understanding(rewrite="what issues affected the rollout programme")
        .generated_perspectives("programme delays and blockers")
        .corpus([_ONLY_BASE],
                per_perspective={"programme delays and blockers": [_ONLY_PERSPECTIVE]})
        .run("what issues affected the rollout programme?")
    )
    assert improved.perspectives == ["programme delays and blockers"]  # 19
    assert improved.recalled("A", "B"), "20: RRF kept the extra evidence"


def test_21_literal_paraphrases_are_rejected(monkeypatch):
    """The lexical half of the guard, which is what this harness exercises: the
    candidate contributes no content word the original did not already have.

    The *semantic* half — a synonym swap, which word overlap cannot see — needs
    hand-placed embeddings and is tested in
    `tests/retrieval/search/test_perspectives.py`; the harness's query vectors
    are orthogonal by construction (see `_query_vector`)."""
    record = (
        _h(monkeypatch, PERSPECTIVE)
        .understanding(rewrite="what issues affected the rollout programme")
        .generated_perspectives("what issues did the rollout programme have")
        .corpus([_ONLY_BASE])
        .run("what issues affected the rollout programme?")
    )
    assert record.perspectives == []


def test_22_redundant_perspectives_are_rejected(monkeypatch):
    """Two proposals that reduce to the same text are one leg, not two."""
    record = (
        _h(monkeypatch, PERSPECTIVE)
        .understanding(rewrite="what issues affected the rollout programme")
        .generated_perspectives("programme delays", "programme delays")
        .corpus([_ONLY_BASE])
        .run("what issues affected the rollout programme?")
    )
    assert record.perspectives == ["programme delays"]


def test_23_generation_failure_falls_back_to_the_original(monkeypatch):
    record = (
        _h(monkeypatch, PERSPECTIVE)
        .understanding(rewrite="what issues affected the rollout programme")
        .generated_perspectives()  # nothing generated
        .corpus([_ONLY_BASE])
        .run("what issues affected the rollout programme?")
    )
    assert record.perspectives == []
    assert record.evidence_ids == ["A"]


def test_24_perspectives_off_preserves_the_baseline(monkeypatch):
    record = (
        _h(monkeypatch, BASELINE)
        .understanding(rewrite="what issues affected the rollout programme")
        .generated_perspectives("programme delays and blockers")
        .corpus([_ONLY_BASE],
                per_perspective={"programme delays and blockers": [_ONLY_PERSPECTIVE]})
        .run("what issues affected the rollout programme?")
    )
    assert record.perspectives == []
    assert record.evidence_ids == ["A"]


# ═══════════════════════════════════════════════════════════════════════════ #
# Combined behaviour (25-32)
# ═══════════════════════════════════════════════════════════════════════════ #

def test_25_clarification_then_a_temporal_query(monkeypatch):
    text = clarify.Clarification(question="Which did you mean?").render()
    record = (
        _h(monkeypatch, FULL_STACK)
        .understanding(rewrite="what is the current status of the ongoing projects")
        .requirements("current status")
        .corpus([_STALE, _VALID])
        .run("Ongoing Projects", _answered("show me the projects", text))
    )
    assert record.clarified_from == "show me the projects"
    assert record.temporal_mode == tg.CURRENT
    assert record.leads() == "valid"


def test_26_clarification_then_multi_query_retrieval(monkeypatch):
    text = clarify.Clarification(question="Which did you mean?").render()
    record = (
        _h(monkeypatch, FULL_STACK)
        .understanding(rewrite="what issues affected the rollout programme")
        .generated_perspectives("programme delays and blockers")
        .corpus([_ONLY_BASE],
                per_perspective={"programme delays and blockers": [_ONLY_PERSPECTIVE]})
        .run("the rollout one", _answered("what issues are there?", text))
    )
    assert record.clarified_from == "what issues are there?"
    assert record.recalled("A", "B")


def test_27_a_decomposed_query_is_still_ranked_temporally(monkeypatch):
    record = (
        _h(monkeypatch, FULL_STACK)
        .understanding(rewrite="what is the current status and funding of the programme")
        .requirements("current status", "funding")
        .corpus([_STALE, _VALID])
        .run("what is the current status and funding of the programme?")
    )
    assert len(record.subqueries) == 2
    assert record.temporal_mode == tg.CURRENT
    assert record.leads() == "valid"


def test_28_a_decomposed_query_also_gets_perspectives(monkeypatch):
    record = (
        _h(monkeypatch, FULL_STACK)
        .understanding(rewrite="what issues and costs affected the rollout programme")
        .requirements("issues raised", "costs incurred")
        .generated_perspectives("programme delays and blockers")
        .corpus(
            [_ONLY_BASE],
            per_perspective={"programme delays and blockers": [_ONLY_PERSPECTIVE]},
            per_subquery={"programme issues raised": [doc("C", relevance=0.85)]},
        )
        .run("what issues and costs affected the rollout programme?")
    )
    assert record.subqueries, "decomposition still ran"
    assert record.perspectives == ["programme delays and blockers"]
    assert record.recalled("A", "B"), "both legs contributed"


def test_29_multi_source_plus_temporal(monkeypatch):
    """Parts route independently and the temporal tie-break still applies to the
    fused result."""
    record = (
        _h(monkeypatch, FULL_STACK)
        .understanding(rewrite="what is the current status and funding of the programme")
        .requirements("current status", "funded by")
        .corpus(
            [_STALE],
            # The planner's own text for the first part — anchors are already in
            # the requirement, so it stands alone.
            per_subquery={"current status": [_VALID]},
        )
        .run("what is the current status and funding of the programme?")
    )
    assert any("graph" in r for r in record.routes.values())
    assert record.recalled("valid", "stale")
    assert record.leads() == "valid"


def test_30_a_query_asking_for_both_history_and_the_present(monkeypatch):
    """The lexical classifier resolves one mode per query — "historical" wins
    here by pattern order. What matters for the report is that both pieces of
    evidence survive; see the known limitation."""
    record = (
        _h(monkeypatch, FULL_STACK)
        .understanding(rewrite="what was the historical status and the current status")
        .requirements("historical status", "current status")
        .corpus([_STALE, _VALID])
        .run("what was the historical status and what is it now?")
    )
    assert record.temporal_mode in (tg.PAST, tg.CURRENT)
    assert record.recalled("stale", "valid"), (
        "a mixed question must keep both sides of the evidence"
    )


def test_31_old_relevant_vs_new_irrelevant_on_a_current_question(monkeypatch):
    """The headline trade-off, through the full stack rather than the reranker
    alone."""
    record = (
        _h(monkeypatch, FULL_STACK)
        .understanding(rewrite="what is the current status of the programme")
        .requirements("current status")
        .corpus([
            doc("new-irrelevant", relevance=0.30, bundle="ongoing_projects",
                start="2026-01-01"),
            doc("old-relevant", relevance=0.93, start="2011-01-01"),
        ])
        .run("what is the current status of the programme?")
    )
    assert record.leads() == "old-relevant"


def test_32_overlapping_perspectives_produce_no_duplicate_evidence(monkeypatch):
    shared = doc("shared", relevance=0.87)
    record = (
        _h(monkeypatch, PERSPECTIVE)
        .understanding(rewrite="what issues affected the rollout programme")
        .generated_perspectives("programme delays", "programme complaints")
        .corpus(
            [_ONLY_BASE],
            per_perspective={
                "programme delays": [shared, _ONLY_PERSPECTIVE],
                "programme complaints": [shared],
            },
        )
        .run("what issues affected the rollout programme?")
    )
    assert len(record.perspectives) == 2
    assert record.evidence_ids.count("shared") == 1
    assert len(record.evidence_ids) == len(set(record.evidence_ids))
