"""Regression tests pinned to the specific benchmark questions this phase
targeted (organization_121_chatbot_fix2_results.json), one per brief item.

Each test exercises the actual mechanism responsible for that question's
behaviour — the intent guard, the answer plan, or the prompt text — with the
question's own wording, so a future change that reopens one of these can be
caught here without re-running the live benchmark. Where the underlying
mechanism already has full generic coverage elsewhere (test_intent_determinism,
test_answer_plan, test_generation_temporal_and_priority), this file adds only
the named, question-anchored case — it does not re-derive the general rule.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.generation import answer_plan as ap
from app.generation import answerer, prompts
from app.retrieval.understanding import query_processor as qp


def _block(n, text):
    return SimpleNamespace(n=n, text=text)


# --------------------------------------------------------------------------- #
# 1. Q001 — mission/goals/values completeness, no invented vision.
# --------------------------------------------------------------------------- #

def test_q001_mission_and_vision_flags_the_unstated_vision():
    """Retrieval succeeded (this is the real Mission and Goals page content,
    trimmed); the fix is that generation is told which of the two asked-for
    parts the page actually states."""
    evidence = _block(1,
        "TERI's mission is to usher in transitions to a cleaner and "
        "sustainable future through the conservation and efficient use of "
        "energy and other resources, and innovative ways of minimizing and "
        "reusing waste. TERI pursues this mission through twelve goals "
        "covering clean energy access, water conservation, sustainable "
        "cities and clean air, guided by the values of Collaboration, "
        "Integrity, Resilience, Nurture, Innovation and Inclusive.")
    plan = ap.build_plan(["mission", "vision"], [evidence])
    assert plan.supported == ["mission"]
    assert plan.unsupported == ["vision"]
    directive = ap.plan_directive(plan)
    assert "invent" in directive.lower()


def test_q001_a_goal_or_value_named_in_the_question_would_be_credited():
    """The mechanism decomposes what the QUESTION names, not what the source
    happens to enumerate — so "goals" and "values" are only ever checked when
    the question itself asks for them. Confirmed here so the boundary is
    explicit rather than assumed."""
    evidence = _block(1, "TERI's values are Collaboration, Integrity, Resilience.")
    plan = ap.build_plan(["mission", "values"], [evidence])
    assert "values" in plan.supported


# --------------------------------------------------------------------------- #
# 2. Q002 — historical narrative, no stale "As of 2023" present tense.
# --------------------------------------------------------------------------- #

def test_q002_the_generation_prompt_carries_a_real_current_date():
    """The prior failure: rule 9 already said to phrase dated wording
    historically, but nothing in the prompt stated what "now" was, so a
    passage saying "As of 2023, TERI is celebrating its 50th anniversary" had
    no fixed point to be measured against."""
    from app.core.dates import today_utc

    system = answerer._build_system(None, None)
    assert today_utc().isoformat() in system
    assert "as of that source's date" in system  # rule 9's pre-existing guidance


# --------------------------------------------------------------------------- #
# 3. Q079 — stable intent classification (waste valorization).
# --------------------------------------------------------------------------- #

def test_q079_no_longer_flips_to_chitchat():
    """Measured over three repeats of one build: qa/chitchat/chitchat. The
    lexical guard is a pure function of this exact text, so every call agrees."""
    q = "What technologies are available for waste valorization?"
    assert {qp._corrected_intent(q, "chitchat") for _ in range(10)} == {"qa"}


# --------------------------------------------------------------------------- #
# 4/5. Q091 / Q093 — answer when authoritative evidence is present.
# --------------------------------------------------------------------------- #

def test_q091_air_quality_evidence_supports_a_direct_answer():
    """Q091's block 1 was, verbatim, the Air Quality Research service node.
    The plan mechanism must recognise a single clear ask as fully supported
    and add no hedge that could invite an unnecessary refusal."""
    evidence = _block(1,
        "TERI provides air quality monitoring and testing through its "
        "NABL-accredited laboratory, covering emissions assessment and "
        "source apportionment.")
    plan = ap.build_plan(["air quality testing"], [evidence])
    assert plan.supported == ["air quality testing"]
    assert ap.plan_directive(plan) == ""  # single requirement: no directive needed


def test_q093_a_partly_covered_yes_no_question_names_what_is_missing():
    """Q093 asks two things at once (soil testing, environmental analysis); the
    corpus in some runs supports only the second. The plan must say so rather
    than let generation collapse the whole answer to a refusal."""
    evidence = _block(1,
        "TERI's NABL-accredited laboratory provides environmental analysis "
        "including emissions assessment and monitoring.")
    plan = ap.build_plan(["soil testing", "environmental analysis"], [evidence])
    assert "environmental analysis" in plan.supported
    assert "soil testing" in plan.unsupported
    directive = ap.plan_directive(plan)
    assert "do not refuse the whole question" in directive.lower()


# --------------------------------------------------------------------------- #
# 6. Q097 — do not refuse when the retrieved evidence supports a safe negative.
# --------------------------------------------------------------------------- #

def test_q097_adjacent_evidence_rule_covers_the_upcoming_shape():
    """Q097 asks for upcoming training programmes; the corpus may show only
    events of a different type or period. Rule 3 now names this shape
    explicitly as a supported negative rather than grounds for a bare refusal.
    This does not touch temporal retrieval — it only tells generation what to
    do with whatever was retrieved."""
    prompt = prompts.grounded_system_prompt()
    assert "different time" in prompt
    assert "different specific type" in prompt
    assert "supported negative answer" in prompt


# --------------------------------------------------------------------------- #
# 7. Q099 — certificate question, single clear ask.
# --------------------------------------------------------------------------- #

def test_q099_certificate_question_is_a_single_requirement():
    """A single-part factual question should never gain a directive — the
    base prompt's existing "answer factually" instruction is sufficient, and
    adding text here would be pure noise on the common case."""
    plan = ap.build_plan(["certificates"],
                         [_block(1, "TERI awards a certificate at the end of "
                                    "an internship term.")])
    assert ap.plan_directive(plan) == ""


# --------------------------------------------------------------------------- #
# 8. Q111 — stable answer/refusal behaviour ("where can I download...").
# --------------------------------------------------------------------------- #

def test_q111_the_where_can_i_download_rule_is_present():
    """Q111's context held the Annual Reports page directly (three of five
    blocks) and the model still refused on some runs. Rule 3 now says
    explicitly that being the right source page IS the answer, even without
    prose narrating download steps."""
    prompt = prompts.grounded_system_prompt()
    assert "name it, cite it, and give its url" in prompt.lower()
    assert "lacking a how-to sentence" in prompt


def test_q111_the_refusal_path_itself_is_unchanged():
    """The fix is entirely about what to do when the source page IS present;
    it must not touch the behaviour when there is truly no context at all."""
    assert answerer.generate_answer("Where can I download TERI's annual reports?", []) \
        == prompts.REFUSAL
