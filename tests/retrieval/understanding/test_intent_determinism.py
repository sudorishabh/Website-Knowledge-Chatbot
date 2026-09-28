"""Unit tests for the deterministic chitchat-correction guard
(``app.retrieval.understanding.query_processor._corrected_intent`` and its probes).

Regression cover for a measured failure: identical questions drew different
intents on different calls because the query-analysis LLM samples at a
non-zero temperature (no seed, temperature unset). Q079
("What technologies are available for waste valorization?") drew
qa/chitchat/chitchat across three repeats of one build; Q091 drew
chitchat/qa/chitchat; Q077 drew qa/chitchat/qa. None of the three names a
resolvable entity, so the pre-existing relational-override
(``_names_entity_and_relationship``) never rescued them.

The fix is a second, independent, purely lexical probe
(``_looks_like_real_question``) plus a narrow counting-question override,
combined with the existing one via OR. Both are pure functions of the question
text — no LLM, no corpus lookup — so they are exactly as deterministic as the
input, which is the property this whole module exists to buy back.

No LLM, no network: everything here is string matching.
"""
from __future__ import annotations

import pytest

from app.retrieval.understanding import query_processor as qp

# --------------------------------------------------------------------------- #
# 1. The three measured flapping cases, pinned by literal text.
# --------------------------------------------------------------------------- #

_FLAPPED_TO_QA = [
    "What technologies are available for waste valorization?",   # Q079
    "Can TERI conduct air quality testing and monitoring?",       # Q091
    "How can industries improve resource efficiency through circular practices?",  # Q077
    "Where can I download TERI's annual reports",                 # Q111
    "Does TERI offer soil testing and environmental analysis?",   # Q093
]


@pytest.mark.parametrize("question", _FLAPPED_TO_QA)
def test_measured_flapping_questions_are_no_longer_chitchat(question):
    assert qp._corrected_intent(question, "chitchat") == "qa"


@pytest.mark.parametrize("question", _FLAPPED_TO_QA)
def test_identical_high_confidence_questions_get_the_same_intent(question):
    """The whole point: a pure function of the text can't flap. Calling it
    repeatedly must return the same answer every time, unlike the LLM sample it
    is correcting."""
    results = {qp._corrected_intent(question, "chitchat") for _ in range(20)}
    assert results == {"qa"}


# --------------------------------------------------------------------------- #
# 2. Chitchat is not incorrectly suppressed.
# --------------------------------------------------------------------------- #

_GENUINE_CHITCHAT = [
    "hi there, thanks for the help!",   # the one pinned few-shot example
    "Thanks for the funding update",
    "hello",
    "hi",
    "bye",
    "goodbye",
    "how are you?",
    "how are you doing today?",
    "what's up?",
    "who are you?",
    "what can you do?",
    "are you a bot?",
    "are you human?",
    "what is your name?",
    "how do you work?",
]


@pytest.mark.parametrize("question", _GENUINE_CHITCHAT)
def test_genuine_chitchat_is_not_rescued(question):
    assert qp._corrected_intent(question, "chitchat") == "chitchat"


def test_a_bare_non_question_is_not_rescued():
    """No wh-word, no auxiliary, no question mark, no imperative lead: there is
    no structural evidence this is an information request."""
    assert qp._corrected_intent("something", "chitchat") == "chitchat"
    assert qp._looks_like_real_question("something") is False


def test_an_empty_or_blank_question_is_not_rescued():
    for text in ("", "   ", None):
        assert qp._looks_like_real_question(text) is False


# --------------------------------------------------------------------------- #
# 3. Counting questions route to structured, not qa.
# --------------------------------------------------------------------------- #

_COUNTING_QUESTIONS = [
    "How many research papers were published in 2024?",
    "How many publications are there from Dr Suneel Pandey?",
    "What is the number of news items this year?",
    "What is the count of ongoing projects?",
]


@pytest.mark.parametrize("question", _COUNTING_QUESTIONS)
def test_counting_questions_are_routed_to_structured_not_qa(question):
    """No prose answer to "how many X" is trustworthy the way a database count
    is — the few-shot bank already pins this shape to [database]. A chitchat
    draw on one of these must not be corrected to the *wrong* safe intent."""
    assert qp._corrected_intent(question, "chitchat") == "structured"


def test_a_non_counting_question_does_not_get_the_structured_override():
    """"which projects" / "what programmes" style phrasing is deliberately left
    to the LLM classifier — a wider deterministic rule here was tried against
    the benchmark and made a real question (Q096, training programmes) worse
    by routing it to a bundle listing instead of prose."""
    assert qp._corrected_intent(
        "What training programmes does TERI offer?", "chitchat"
    ) == "qa"


# --------------------------------------------------------------------------- #
# 4. Structured/list questions are not routed to qa when their structure is
#    obvious — i.e. the guard must not downgrade a question that already
#    reads as a counting question just because it also has other qa-ish words.
# --------------------------------------------------------------------------- #

def test_a_counting_question_with_extra_wording_still_goes_structured():
    q = "Could you tell me how many policy briefs TERI has published recently?"
    assert qp._corrected_intent(q, "chitchat") == "structured"


# --------------------------------------------------------------------------- #
# 5. The override stays one-directional: it only ever reads a chitchat draw.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("intent", ["qa", "structured", "scoped_summary"])
def test_the_override_never_touches_a_non_chitchat_intent(intent):
    assert qp._corrected_intent(
        "What technologies are available for waste valorization?", intent
    ) == intent


# --------------------------------------------------------------------------- #
# 6. The two probes still combine with OR — a relational match alone remains
#    sufficient, independent of the new lexical probe.
# --------------------------------------------------------------------------- #

def test_a_relational_match_alone_still_rescues(monkeypatch):
    import app.retrieval.understanding.approved_aliases as aa
    import app.retrieval.understanding.relational as rel

    class _Intent:
        is_relational = True

    class _Index:
        def match(self, question):
            return [("x",)]

    monkeypatch.setattr(rel, "read_relational", lambda q: _Intent())
    monkeypatch.setattr(aa, "get_index", lambda: _Index())
    # "something" has no lexical shape at all, so only the relational probe
    # can be rescuing it here.
    assert qp._corrected_intent("something", "chitchat") == "qa"


def test_the_lexical_probe_never_raises_on_odd_input():
    for text in (None, "", "?" * 50, "𝔘𝔫𝔦𝔠𝔬𝔡𝔢 𝔱𝔢𝔰𝔱?", "a" * 5000):
        qp._looks_like_real_question(text)  # must not raise


# --------------------------------------------------------------------------- #
# 7. A bare noun phrase naming the organisation's people or one of its pages.
#    Measured 2026-09-25: "TERI top researchers" drew clarification_needed at
#    0.91, which collapses onto chitchat, and the user was asked what they
#    meant instead of being answered. It has no question shape and names no
#    gazetteer entity, so neither probe above could see it.
# --------------------------------------------------------------------------- #

_BARE_SUBJECTS = [
    "TERI top researchers",        # the reported turn — asks for people
    "yes leading researchers",     # its follow-up, before the history rewrite
    "TERI climate change team",
    "air quality experts",
    "green shipping",              # a listed page's own name
    "centres of excellence",       # a curated page phrase
    "Teri thematic area",          # the home page's theme-list phrase
]


@pytest.mark.parametrize("question", _BARE_SUBJECTS)
def test_a_bare_phrase_naming_people_or_a_page_is_rescued(question):
    assert qp._looks_like_real_question(question) is False  # the gap this closes
    assert qp._corrected_intent(question, "chitchat") == "qa"


@pytest.mark.parametrize("question", ["okay cool", "nice work", "good job", "great, got it"])
def test_content_words_alone_are_not_enough(question):
    # Two content words and no question shape: small talk, not a subject.
    assert qp._corrected_intent(question, "chitchat") == "chitchat"


@pytest.mark.parametrize("question", ["who are you?", "thanks team", "hi, you are the experts"])
def test_a_person_word_in_small_talk_is_not_rescued(question):
    # The social/meta check runs first and wins, as in the lexical probe.
    assert qp._names_an_organisation_subject(question) is False


def _people_index(monkeypatch, *, enabled):
    """The people index, stubbed to one name; returns how often it was read."""
    from app.config import get_settings
    from app.retrieval.priority import evidence
    from app.retrieval.priority.people import Person

    reads: list = []

    def listed_people(registry):
        reads.append(registry)
        return [Person("Dr Prasoon Singh", "https://teriin.org/profile/prasoon-singh",
                       "Climate Change Theme")]

    monkeypatch.setattr(get_settings(), "priority_pages_enabled", enabled)
    monkeypatch.setattr(evidence, "listed_people", listed_people)
    return reads


def test_a_named_person_on_the_people_pages_is_a_real_request(monkeypatch):
    """Measured 2026-09-28: "Prasoon Singh work" was answered as small talk."""
    _people_index(monkeypatch, enabled=True)
    assert qp._corrected_intent("Prasoon Singh work", "chitchat") == "qa"
    # Small talk names nobody, whatever the index holds.
    assert qp._corrected_intent("nice work", "chitchat") == "chitchat"


def test_without_priority_pages_no_name_is_read(monkeypatch):
    reads = _people_index(monkeypatch, enabled=False)
    assert qp._corrected_intent("Prasoon Singh work", "chitchat") == "chitchat"
    assert reads == []


def test_the_subject_probe_never_raises_on_odd_input():
    for text in (None, "", "?" * 50, "a" * 5000):
        qp._names_an_organisation_subject(text)  # must not raise
