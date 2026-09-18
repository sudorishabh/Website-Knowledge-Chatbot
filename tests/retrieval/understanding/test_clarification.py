"""Phase A: clarification, and the conversation state it rides on.

Covers the decision (when to ask), the options (what the catalog can back), the
history protocol (marker, merge, one-round guard) and the `process` wiring. No
network: the understanding call is patched at its module attribute, matching the
convention the rest of this directory uses.
"""

from __future__ import annotations

import pytest

from app.retrieval.understanding import clarify
from app.retrieval.understanding import query_processor as qp


def _u(labels, **scope):
    """A QueryUnderstanding with the given (label, confidence) intents."""
    return qp.QueryUnderstanding(
        query_rewrite="rewritten",
        intents=[
            qp.IntentPrediction(label=lbl, confidence=c, rationale="")
            for lbl, c in labels
        ],
        scope=qp.QueryScope(**scope),
    )


def _clarified(question="Which theme did you mean?", options=("A", "B")):
    return clarify.Clarification(
        question=question, options=list(options), kind="theme"
    ).render()


def _history(user="Show me performance.", assistant=None):
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant if assistant is not None else _clarified()},
    ]


# --------------------------------------------------------------------------- #
# The marker: what makes an assistant turn recognisable as a clarification
# --------------------------------------------------------------------------- #

def test_rendered_clarification_ends_with_the_marker():
    text = _clarified()
    assert text.endswith(clarify.MARKER)
    assert clarify.is_clarification_turn(text)


def test_rendered_clarification_numbers_its_options():
    text = clarify.Clarification(
        question="Which of these did you mean?", options=["Climate Change", "Water"]
    ).render()
    assert "1. Climate Change" in text
    assert "2. Water" in text


def test_a_clarification_without_options_still_carries_the_marker():
    text = clarify.Clarification(question="Say more?").render()
    assert clarify.is_clarification_turn(text)
    assert "1." not in text


def test_an_ordinary_answer_is_not_a_clarification_turn():
    assert not clarify.is_clarification_turn("TERI was established in 1974. [1]")
    assert not clarify.is_clarification_turn("")
    assert not clarify.is_clarification_turn(None)


# --------------------------------------------------------------------------- #
# pending(): reading state back out of the client-echoed history
# --------------------------------------------------------------------------- #

def test_pending_finds_the_question_a_clarification_was_asked_about():
    assert clarify.pending(_history()) == "Show me performance."


def test_pending_is_none_without_history():
    assert clarify.pending(None) is None
    assert clarify.pending([]) is None


def test_pending_is_none_when_the_last_turn_is_an_ordinary_answer():
    assert clarify.pending(_history(assistant="Revenue grew 4%. [1]")) is None


def test_pending_is_none_when_the_clarification_is_not_the_latest_turn():
    """A clarification already answered is a closed episode, not an open one."""
    history = _history() + [
        {"role": "user", "content": "Revenue"},
        {"role": "assistant", "content": "Revenue grew 4%. [1]"},
    ]
    assert clarify.pending(history) is None


def test_pending_skips_blank_turns():
    history = [
        {"role": "user", "content": "Show me performance."},
        {"role": "assistant", "content": "   "},
        {"role": "assistant", "content": _clarified()},
    ]
    assert clarify.pending(history) == "Show me performance."


# --------------------------------------------------------------------------- #
# merge()
# --------------------------------------------------------------------------- #

def test_merge_carries_both_halves():
    merged = clarify.merge("Show me performance.", "Revenue")
    assert "performance" in merged
    assert "Revenue" in merged


def test_merge_degrades_to_whichever_half_exists():
    assert clarify.merge("", "Revenue") == "Revenue"
    assert clarify.merge("Show me performance.", "") == "Show me performance."


# --------------------------------------------------------------------------- #
# decide(): when to ask at all
# --------------------------------------------------------------------------- #

def test_a_clear_question_is_never_clarified():
    assert clarify.decide(_u([("qa", 0.95)]), "What was the 2023 revenue?") is None


def test_a_near_tie_between_content_intents_is_not_a_clarification():
    """`is_ambiguous` is a routing signal, not evidence the user was unclear —
    retrieval can settle qa-vs-database, so it must not interrupt."""
    understanding = _u([("qa", 0.55), ("database", 0.50)])
    assert qp._is_ambiguous(understanding.intents)
    assert clarify.decide(understanding, "how many reports on climate") is None


def test_passthrough_understanding_never_clarifies():
    assert clarify.decide(None, "anything") is None


def test_clarification_needed_produces_a_question(monkeypatch):
    monkeypatch.setattr(clarify, "_options", lambda *a, **k: ([], ""))
    result = clarify.decide(_u([("clarification_needed", 0.9)]), "show me a table")
    assert result is not None
    assert result.question
    assert result.options == []


# --------------------------------------------------------------------------- #
# Options: catalog-backed, 2..4, never invented
# --------------------------------------------------------------------------- #

def test_content_type_options_come_from_the_registry(monkeypatch):
    """"projects" spans two bundles; both are offered because both have rows."""
    from app.retrieval.structured import entities

    monkeypatch.setattr(entities, "is_available", lambda name: True)
    options = clarify._content_type_options("show me the projects")
    assert options == ["Completed Projects", "Ongoing Projects"]


def test_content_type_options_drop_a_bundle_the_catalog_cannot_match(monkeypatch):
    """An option that could only ever return zero is not offered — and one
    remaining option is not a choice, so the list collapses to empty."""
    from app.retrieval.structured import entities

    monkeypatch.setattr(
        entities, "is_available", lambda name: name == "completed_projects"
    )
    assert clarify._content_type_options("show me the projects") == []


def test_a_vague_question_naming_nothing_gets_no_options(monkeypatch):
    """The anti-invention property: no catalog evidence, no options."""
    from app.retrieval.structured import resolve

    monkeypatch.setattr(resolve, "resolve_entity", lambda *a, **k: [])
    options, kind = clarify._options("show me performance", _u([("clarification_needed", 0.9)]))
    assert options == []
    assert kind == ""


def test_entity_options_are_capped_and_floored(monkeypatch):
    """`plausible` is the floor (nothing below the ambiguity score is offered)
    and MAX_OPTIONS is the cap."""
    from app.retrieval.structured import resolve

    many = [
        resolve.EntityCandidate(id=f"t{i}", canonical_name=f"Theme {i}", type="theme", score=0.9)
        for i in range(6)
    ]
    monkeypatch.setattr(resolve, "resolve_entity", lambda *a, **k: many)
    options = clarify._entity_options("climate", "theme")
    assert len(options) == clarify.MAX_OPTIONS
    assert options[0] == "Theme 0"


def test_entity_options_survive_a_resolver_failure(monkeypatch):
    """A catalog outage must cost the options, never the clarification."""
    from app.retrieval.structured import resolve

    def boom(*a, **k):
        raise RuntimeError("mysql down")

    monkeypatch.setattr(resolve, "resolve_entity", boom)
    assert clarify._entity_options("climate", "theme") == []


def test_offered_options_are_between_two_and_four(monkeypatch):
    from app.retrieval.structured import entities

    monkeypatch.setattr(entities, "is_available", lambda name: True)
    result = clarify.decide(_u([("clarification_needed", 0.9)]), "show me the projects")
    assert result is not None
    assert clarify.MIN_OPTIONS <= len(result.options) <= clarify.MAX_OPTIONS


# --------------------------------------------------------------------------- #
# process(): the wiring, and the flag
# --------------------------------------------------------------------------- #

@pytest.fixture
def _understanding(monkeypatch):
    """Patch the single-sample understanding call to return a chosen object."""
    holder: dict = {}

    class _Model:
        def with_structured_output(self, _schema):
            return self

        def invoke(self, _messages):
            holder["messages"] = _messages
            return holder["result"]

    monkeypatch.setattr(qp, "get_structured_llm", lambda: _Model())
    monkeypatch.setattr(qp, "_facet_filters", lambda analysis: [])
    monkeypatch.setattr(qp, "_edition_conditions", lambda question: [])
    return holder


def _enable(monkeypatch, on=True):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "clarification_enabled", on, raising=False)


def test_flag_off_keeps_clarification_needed_collapsing_to_chitchat(
    monkeypatch, _understanding
):
    """The pre-existing behaviour, asserted so the flag cannot silently change it."""
    _enable(monkeypatch, False)
    _understanding["result"] = _u([("clarification_needed", 0.9)])
    pq = qp.process("what about that one?")
    assert pq.clarification is None
    assert pq.intent == "chitchat"


def test_flag_on_produces_a_clarification_instead(monkeypatch, _understanding):
    _enable(monkeypatch)
    monkeypatch.setattr(clarify, "_options", lambda *a, **k: ([], ""))
    _understanding["result"] = _u([("clarification_needed", 0.9)])
    pq = qp.process("what about that one?")
    assert pq.clarification is not None
    assert clarify.is_clarification_turn(pq.clarification.render())


def test_flag_on_leaves_a_clear_question_untouched(monkeypatch, _understanding):
    _enable(monkeypatch)
    _understanding["result"] = _u([("qa", 0.95)])
    pq = qp.process("What was the 2023 revenue?")
    assert pq.clarification is None
    assert pq.intent == "qa"


def test_the_next_turn_merges_the_clarification_answer(monkeypatch, _understanding):
    _enable(monkeypatch)
    _understanding["result"] = _u([("qa", 0.9)])
    pq = qp.process("Revenue", _history())

    assert pq.clarified_from == "Show me performance."
    assert pq.clarification is None, "one round only"
    # The understanding call saw both halves, so its rewrite can be standalone.
    sent = str(_understanding["messages"])
    assert "performance" in sent and "Revenue" in sent


def test_the_merged_text_survives_an_understanding_failure(monkeypatch, _understanding):
    """Passthrough is the floor under the LLM rewrite: the merge still happened."""
    _enable(monkeypatch)

    def boom():
        raise RuntimeError("llm down")

    monkeypatch.setattr(qp, "get_structured_llm", boom)
    pq = qp.process("Revenue", _history())
    assert "performance" in pq.search_query
    assert "Revenue" in pq.search_query
    assert pq.clarification is None


def test_one_round_guard_holds_even_if_the_answer_is_still_vague(
    monkeypatch, _understanding
):
    """The loop guard: a second `clarification_needed` verdict, while a
    clarification is open, does not produce a second question."""
    _enable(monkeypatch)
    _understanding["result"] = _u([("clarification_needed", 0.9)])
    pq = qp.process("dunno", _history())
    assert pq.clarification is None
    assert pq.clarified_from == "Show me performance."


def test_a_later_independent_vague_question_may_clarify_again(
    monkeypatch, _understanding
):
    """The guard is per episode, not per thread: once a clarification has been
    answered, a new unclear question is still allowed one question back."""
    _enable(monkeypatch)
    monkeypatch.setattr(clarify, "_options", lambda *a, **k: ([], ""))
    history = _history() + [
        {"role": "user", "content": "Revenue"},
        {"role": "assistant", "content": "Revenue grew 4%. [1]"},
    ]
    _understanding["result"] = _u([("clarification_needed", 0.9)])
    pq = qp.process("show me a table", history)
    assert pq.clarification is not None


# --------------------------------------------------------------------------- #
# The two guards between the label and a question
# --------------------------------------------------------------------------- #
# Reported from production: "director generak of teri" was answered with
# "I need a little more to go on — could you say what topic or which documents
# you are asking about?", and the very next turn, "who is the director general
# of TERI", answered it from the 2022-23 annual report. The trace shows a single
# intent, `clarification_needed` at 0.74, rationale "Request is vague/typo; no
# clear question target" — the model clarified because of a typo, and nothing
# downstream could disagree.


@pytest.fixture
def _floor(monkeypatch):
    """Set the confidence bar, returning a setter so a test can pick its own."""
    from app.config import get_settings

    def _set(value):
        monkeypatch.setattr(
            get_settings(), "clarification_min_confidence", value, raising=False
        )

    return _set


@pytest.fixture
def _no_options(monkeypatch):
    """The case the bug fell into: nothing the catalog can offer."""
    monkeypatch.setattr(clarify, "_options", lambda *a, **k: ([], ""))


# --- Guard 1: a bar of its own ---------------------------------------------- #

def test_the_reported_confidence_is_below_the_default_bar(_no_options):
    """0.74 cleared `intent_confidence_threshold` (0.5) and must not clear this."""
    assert clarify.decide(
        _u([("clarification_needed", 0.74)]), "director generak of teri"
    ) is None


def test_a_confident_verdict_still_clarifies(_floor, _no_options):
    _floor(0.8)
    assert clarify.decide(
        _u([("clarification_needed", 0.9)]), "show me a table"
    ) is not None


def test_the_bar_is_inclusive(_floor, _no_options):
    _floor(0.8)
    assert clarify.decide(
        _u([("clarification_needed", 0.8)]), "show me a table"
    ) is not None


def test_a_bar_of_zero_restores_the_previous_behaviour(_floor, _no_options):
    """The flag's own escape hatch: the guard is configuration, not a rewrite."""
    _floor(0.0)
    assert clarify.decide(
        _u([("clarification_needed", 0.1)]), "what about that one?"
    ) is not None


def test_the_bar_reads_the_clarification_label_not_the_highest_one(
    _floor, _no_options
):
    """A confident *content* label alongside a weak clarification verdict must
    not lend it its confidence."""
    _floor(0.8)
    understanding = _u([("qa", 0.95), ("clarification_needed", 0.6)])
    assert clarify.decide(understanding, "show me a table") is None


# --- Guard 2: a turn that names a subject ----------------------------------- #

@pytest.mark.parametrize("question", [
    "director generak of teri",          # the reported turn
    "director general of TERI",
    "annual report 2023",
    "solar capacity in Gujarat",
    "vibha dhawan biography",
])
def test_a_turn_naming_a_subject_is_not_clarified(question, _floor, _no_options):
    """Even at full confidence: a bare question back would ask the user to
    restate what they have already said."""
    _floor(0.0)
    assert clarify.decide(_u([("clarification_needed", 1.0)]), question) is None


@pytest.mark.parametrize("question", [
    "show me a table",
    "what about that one?",
    "in json please",
    "give me a list",
    "summarize it",
    "dunno",
    "as a chart",
    "",
])
def test_a_turn_naming_no_subject_is_still_clarified(question, _floor, _no_options):
    _floor(0.0)
    assert clarify.decide(_u([("clarification_needed", 1.0)]), question) is not None


def test_options_outrank_the_subject_veto(monkeypatch, _floor):
    """The veto applies only where the question degrades to the bare wording.
    Options the catalog holds are a real choice and worth a turn even when the
    turn names a subject — that path must not be narrowed."""
    _floor(0.0)
    monkeypatch.setattr(
        clarify, "_options", lambda *a, **k: (["Climate Change", "Energy"], "theme")
    )
    result = clarify.decide(
        _u([("clarification_needed", 1.0)]), "climate and energy projects"
    )
    assert clarify.names_a_subject("climate and energy projects")
    assert result is not None and result.options == ["Climate Change", "Energy"]


# --- names_a_subject, on its own -------------------------------------------- #

def test_request_and_format_words_are_not_subjects():
    assert not clarify.names_a_subject("show me the table as a list in json")


def test_one_content_word_is_not_yet_a_subject():
    """A single word is a topic hint, and those are the turns where asking back
    helps most — "projects" is the option-backed case in the tests above."""
    assert not clarify.names_a_subject("performance")
    assert not clarify.names_a_subject("show me the projects")
    assert clarify.names_a_subject("show me the water projects")


def test_repeated_words_count_once():
    assert not clarify.names_a_subject("reports reports reports")


def test_short_tokens_do_not_count():
    assert not clarify.names_a_subject("a b of x y")


def test_the_veto_is_a_pure_function_of_the_text():
    question = "director generak of teri"
    assert clarify.names_a_subject(question) is clarify.names_a_subject(question)


# --- The reported turn, end to end through `process` ------------------------ #

def test_the_reported_turn_reaches_retrieval_instead_of_asking(
    monkeypatch, _understanding, _no_options
):
    """The bug, at the seam that produced it."""
    _enable(monkeypatch)
    _understanding["result"] = _u([("clarification_needed", 0.74)])
    pq = qp.process("director generak of teri")
    assert pq.clarification is None


def test_the_label_is_still_reported_when_it_is_not_acted_on(
    monkeypatch, _understanding, _no_options
):
    """The guards decline to *ask*; they do not hide the verdict, so the
    false-positive rate stays visible on the trace."""
    _enable(monkeypatch)
    _understanding["result"] = _u([("clarification_needed", 0.74)])
    pq = qp.process("director generak of teri")
    assert pq.clarification is None
    assert [p.label for p in pq.understanding.intents] == ["clarification_needed"]
