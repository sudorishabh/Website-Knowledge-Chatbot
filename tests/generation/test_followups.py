"""Contextual follow-up questions: the call, the screen, the ranking, the task.

What matters here is not that the model's questions read well — a prompt is
judged by eye and by the live eval (``scripts/eval_followups.py``) — but that
the rules around it hold: a question whose answer is not in our own passages, a
rewording of what was asked, or a near duplicate never reaches the visitor; the
answer is never held up; and every failure means "no follow-ups", never a broken
turn.

LLM and embeddings are stubbed; no network.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.core.models.context import ContextBlock
from app.generation import followups as fu
from app.generation.prompts import REFUSAL

QUESTION = "What does TERI do on air pollution?"


def _blocks():
    return [
        ContextBlock(n=1, text="TERI measures air quality across Indian cities with "
                               "a network of monitoring stations.",
                     payload={"title": "Air quality", "section_heading": "Overview"}),
        ContextBlock(n=2, text="A source apportionment study for Delhi found "
                               "vehicles and dust were the largest sources.",
                     payload={"title": "Delhi source apportionment"}),
        ContextBlock(n=3, text="TERI's clean cooking programme reaches rural homes "
                               "with improved cookstoves and cleaner fuels.",
                     payload={"title": "Clean cooking"}),
    ]


class _FakeLLM:
    def __init__(self, questions=None, raises=None):
        self._questions = questions or []
        self._raises = raises
        self.calls = 0
        self.messages = None

    def with_structured_output(self, _schema):
        return self

    def invoke(self, messages):
        self.calls += 1
        self.messages = messages
        if self._raises:
            raise self._raises
        return SimpleNamespace(questions=self._questions)


def _suggest(question, passage, evidence="", scope=""):
    return SimpleNamespace(question=question, passage=passage, evidence=evidence,
                           scope=scope)


#: Words copied from `_blocks()` passages 2 and 1, as the model is asked to
#: copy them.
DELHI = "found vehicles and dust were the largest sources"
AIR_QUALITY = "TERI measures air quality across Indian cities with a network"


def _install(monkeypatch, llm):
    seen: dict = {}

    def factory(**kw):
        seen.update(kw)
        return llm

    monkeypatch.setattr(fu, "get_llm", factory)
    return seen


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #

def test_the_call_sees_the_question_the_answer_so_far_and_the_passages(monkeypatch):
    llm = _FakeLLM([_suggest("What did the Delhi source apportionment study find?", 2,
                             evidence=DELHI, scope="specific")])
    _install(monkeypatch, llm)
    out = fu.generate_followups(
        QUESTION, _blocks(),
        answer_so_far="TERI monitors air quality [1].",
        search_query="TERI air pollution work",
        history=[{"role": "user", "content": "Who is TERI's Director General?"},
                 {"role": "assistant", "content": "Dr Vibha Dhawan."}],
        n=4,
    )
    assert out == [fu.Candidate("What did the Delhi source apportionment study find?", 2,
                                evidence=DELHI)]
    assert llm.calls == 1
    system, human = llm.messages[0][1], llm.messages[1][1]
    assert "language of the visitor's question" in system
    assert "stand on its own" in system
    assert "copy into 'evidence'" in system
    assert "Exactly one should be broad" in system
    assert f"Visitor's question: {QUESTION}" in human
    assert "(Understood as: TERI air pollution work)" in human
    assert "TERI monitors air quality [1]." in human
    assert "[2] Delhi source apportionment\nA source apportionment study" in human
    assert "- Who is TERI's Director General?" in human
    assert "Suggest 4 follow-up questions." in human


def test_its_own_reasoning_effort_reaches_the_client(monkeypatch):
    seen = _install(monkeypatch, _FakeLLM([]))
    fu.generate_followups(QUESTION, _blocks(), answer_so_far="x", reasoning_effort="none")
    assert seen["reasoning_effort"] == "none"
    seen.clear()
    fu.generate_followups(QUESTION, _blocks(), answer_so_far="x", reasoning_effort="")
    # Empty inherits the global setting, which `get_llm` reads for None.
    assert seen["reasoning_effort"] is None


def test_a_failed_call_is_tried_once_more_then_no_followups(monkeypatch):
    llm = _FakeLLM(raises=RuntimeError("400"))
    _install(monkeypatch, llm)
    assert fu.generate_followups(QUESTION, _blocks(), answer_so_far="x") == []
    assert llm.calls == 2


def test_a_malformed_reply_is_no_followups(monkeypatch):
    llm = _FakeLLM()
    llm.invoke = lambda messages: SimpleNamespace(questions="not a list")
    _install(monkeypatch, llm)
    assert fu.generate_followups(QUESTION, _blocks(), answer_so_far="x") == []


def test_an_empty_reply_gets_one_more_try(monkeypatch):
    # Measured live: at reasoning effort "none" the model sometimes answers an
    # ordinary context with {"questions": []}; a second try usually proposes.
    replies = [SimpleNamespace(questions=[]),
               SimpleNamespace(questions=[_suggest("What did the Delhi study find?", 2,
                                                   evidence=DELHI)])]
    llm = _FakeLLM()
    llm.invoke = lambda messages: replies.pop(0)
    _install(monkeypatch, llm)
    out = fu.generate_followups(QUESTION, _blocks(), answer_so_far="x")
    assert [c.question for c in out] == ["What did the Delhi study find?"]
    assert replies == []


def test_the_schema_asks_for_the_evidence_before_the_question():
    # The model writes fields in schema order: the question must be written to
    # fit words already chosen, not the other way round.
    fields = list(fu._Suggestion.model_fields)
    assert fields.index("passage") < fields.index("evidence") < fields.index("question")


def test_a_non_numeric_passage_reads_as_none(monkeypatch):
    _install(monkeypatch, _FakeLLM([_suggest("What is clean cooking at TERI?", "three")]))
    assert fu.generate_followups(QUESTION, _blocks(), answer_so_far="x") == [
        fu.Candidate("What is clean cooking at TERI?", 0)
    ]


def test_no_passages_means_no_call(monkeypatch):
    llm = _FakeLLM([_suggest("Anything?", 1)])
    _install(monkeypatch, llm)
    assert fu.generate_followups(QUESTION, [], answer_so_far="x") == []
    assert llm.calls == 0


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #

def _screen(candidates, **kw):
    """Screen (question, passage) pairs whose evidence is their passage's own
    text, so each test exercises the one rule it is about."""
    kw.setdefault("question", QUESTION)
    kw.setdefault("blocks", _blocks())
    texts = {b.n: b.text for b in kw["blocks"]}
    return fu.screen([fu.Candidate(q, p, evidence=texts.get(p, "")) for q, p in candidates],
                     **kw)


def test_the_screen_keeps_well_formed_new_questions():
    kept, dropped = _screen([
        ("What did the Delhi source apportionment study find?", 2),
        ("How does TERI's clean cooking programme reach rural homes?", 3),
    ])
    assert [c.question for c in kept] == [
        "What did the Delhi source apportionment study find?",
        "How does TERI's clean cooking programme reach rural homes?",
    ]
    assert not dropped


def test_shape_rules_drop_statements_and_paragraphs():
    kept, dropped = _screen([
        ("Tell me about the Delhi study", 2),
        ("What " + "very " * 40 + "long question is this?", 2),
    ])
    assert kept == [] and dropped["shape"] == 2


def test_a_question_without_a_real_passage_is_dropped():
    kept, dropped = _screen([("What did the Mumbai study find?", 9),
                             ("What did the Pune study find?", 0)])
    assert kept == [] and dropped["passage"] == 2


# --------------------------------------------------------------------------- #
# The evidence: a question is shown only on its passage's own words
# --------------------------------------------------------------------------- #

def test_a_quote_from_the_passage_is_grounded():
    passage = _blocks()[1].text
    assert fu.grounded(DELHI, passage)
    # Copying edits: case, punctuation, a closed-up ellipsis.
    assert fu.grounded("A source-apportionment study for Delhi found vehicles, and "
                       "dust\u2026 were the largest sources.", passage)


def test_words_the_passage_does_not_contain_are_not_evidence():
    passage = _blocks()[1].text
    # A paraphrase, and the classic overreach: a detail the passage never gives.
    assert not fu.grounded("Delhi's pollution mostly comes from cars and road dust",
                           passage)
    assert not fu.grounded("The study was funded by the World Bank in 2019", passage)


def test_a_name_or_a_phrase_is_not_evidence():
    passage = _blocks()[1].text
    assert not fu.grounded("Delhi", passage)
    assert not fu.grounded("source apportionment study", passage)
    assert not fu.grounded("", passage)


def test_a_sentence_quoted_up_to_where_the_excerpt_was_cut_is_not_evidence():
    # The call sees only the passage's opening. A quote running into the cut is
    # a sentence whose end the model never saw: the answer is in the missing
    # part ("68 per cent stated that temperatures ...").
    opening = "Nearly 90 per cent of respondents in Pune feel that climate change is "
    padding = "occurring across the city and its surrounding districts. " * 8
    passage = (opening + padding + "68 per cent stated that temperatures had risen "
               "sharply over the past decade.")
    shown, cut = fu._excerpt(passage)
    assert cut and len(shown) <= fu._SNIPPET_CHARS
    up_to_the_cut = " ".join(shown.split()[-10:])
    assert not fu.grounded(up_to_the_cut, passage)
    # A whole sentence inside the excerpt still counts.
    assert fu.grounded(opening + "occurring across the city", passage)


def test_the_excerpt_is_cut_at_a_word_and_marked():
    long_text = "Solar irrigation pumps reduce diesel use for farmers. " * 20
    digest = fu.passage_digest([ContextBlock(n=1, text=long_text, payload={})])
    body = digest.split("\n", 1)[0]
    assert body.endswith(" …")
    last_word = body[:-2].split()[-1]
    assert last_word in long_text.split()


#: Rows of two directory pages, as the website stores them: a name on one line,
#: the title on the next. The suggestions under "list down all the management
#: people…" stood on these: "What is TERI's work on green shipping?" on the
#: Rashmi row, "What is R Mukundan's role at Tata Chemicals Limited?" on his.
COMMITTEE = ("Committee of Directors\nDr Vibha Dhawan\nDirector General\nMr Sanjay Seth\n"
             "Executive Director, Strategy & Partnerships\nMs Suruchi Bhadwal\n"
             "Director, Climate Change and Air Quality\nMr R R Rashmi\n"
             "Distinguished Fellow, Green Shipping\nMr A K Saxena\n"
             "Senior Director, Electricity and Renewables")
COUNCIL = ("Governing Council\nMr Nitin Desai\nChairman - Governing Council\n"
           "Dr Vibha Dhawan\nEx-officio Member - Governing Council\nMr R Mukundan\n"
           "Member - Governing Council\nManaging Director & CEO\nTata Chemicals Limited\n"
           "Mr Mahendra Singhi\nMember - Governing Council\nDirector & Strategic Advisor\n"
           "Dalmia Cement (Bharat) Limited")
#: Sentences wrapped in a narrow column (a regional centre's brochure) and in a
#: report's page width — real passages whose quotes made answered suggestions.
NARROW = ("Our Mission\nTo lead sustainable\nmanagement and\nconservation of marine,\n"
          "coastal, and inland\nresources through\nmultidisciplinary\nresearch, capacity\n"
          "building, and consulting.")
WRAPPED = ("Development' as its tag line, the TERI\nUniversity's academic programmes are\n"
           "focussed around the challenges of providing for a\nrising global population "
           "with a limited and degraded\nnatural resource base.")


def test_the_excerpt_keeps_a_list_on_its_lines():
    digest = fu.passage_digest([ContextBlock(n=1, text=COMMITTEE + "\n\n\n  ",
                                             payload={})])
    assert "Mr R R Rashmi\nDistinguished Fellow, Green Shipping" in digest
    assert "\n\n" not in digest and not digest.endswith(" ")


def test_a_list_row_is_not_evidence():
    # Each passes the word check — eight words or more, all the passage's own.
    rashmi = "Mr R R Rashmi Distinguished Fellow, Green Shipping"
    mukundan = ("Mr R Mukundan Member - Governing Council Managing Director & CEO "
                "Tata Chemicals Limited")
    assert fu.grounded(rashmi, COMMITTEE) and fu.grounded(mukundan, COUNCIL)
    assert fu.list_row(rashmi, COMMITTEE)
    assert fu.list_row(mukundan, COUNCIL)
    assert fu.list_row("Ms Suruchi Bhadwal Director, Climate Change and Air Quality",
                       COMMITTEE)


def test_a_wrapped_sentence_is_not_a_list_row():
    assert not fu.list_row("To lead sustainable management and conservation of marine, "
                           "coastal, and inland resources through multidisciplinary "
                           "research, capacity building, and consulting.", NARROW)
    # Two seams start with a capital ("TERI / University's"), one does not.
    assert not fu.list_row("the TERI University's academic programmes are focussed "
                           "around the challenges of providing for a rising global "
                           "population", WRAPPED)


def test_a_heading_over_its_sentence_is_not_a_list_row():
    passage = ("Founders\nMr Darbari S Seth was a unique individual whose vision and "
               "drive earned him the title of 'the man with the Midas touch'.")
    assert not fu.list_row("Founders Mr Darbari S Seth was a unique individual whose "
                           "vision and drive", passage)


def test_a_sentence_wrapped_at_acronyms_is_not_a_list_row():
    # Every seam opens with a capital — "(PM2.5)", "CRB" — but it ends a sentence.
    passage = ("The study aimed to understand and quantify the effect of poor air quality\n"
               "(PM2.5) and the respiratory health of the population exposed to PM during\n"
               "CRB period.")
    assert not fu.list_row("The study aimed to understand and quantify the effect of poor "
                           "air quality (PM2.5) and the respiratory health of the "
                           "population exposed to PM during CRB period.", passage)


def test_sentences_on_lines_of_their_own_are_not_a_list_row():
    passage = ("TERI set up the Centre of Excellence on green ports in 2023.\n"
               "It advises ports on electrification, green fuels and pilot projects.")
    assert not fu.list_row("TERI set up the Centre of Excellence on green ports in 2023. "
                           "It advises ports on electrification", passage)


def test_a_quote_from_one_line_is_not_a_list_row():
    assert not fu.list_row("Senior Director, Electricity and Renewables", COMMITTEE)
    assert not fu.list_row(DELHI, _blocks()[1].text)


def test_a_script_without_capitals_is_never_a_list_row():
    passage = "टेरी ने वायु गुणवत्ता की निगरानी के लिए\nशहरों में एक नेटवर्क स्थापित किया है"
    assert not fu.list_row("टेरी ने वायु गुणवत्ता की निगरानी के लिए शहरों में एक नेटवर्क",
                           passage)


def test_the_screen_drops_a_question_on_a_list_row():
    blocks = [ContextBlock(n=1, text=COMMITTEE, payload={}),
              ContextBlock(n=2, text=WRAPPED, payload={})]
    kept, dropped = fu.screen([
        fu.Candidate("What is TERI's work on green shipping?", 1,
                     evidence="Mr R R Rashmi Distinguished Fellow, Green Shipping"),
        fu.Candidate("What challenges shape TERI University's academic programmes?", 2,
                     evidence="the TERI University's academic programmes are focussed "
                              "around the challenges of providing for a rising global "
                              "population"),
    ], question="list down all the management people", blocks=blocks)
    assert [c.question for c in kept] == [
        "What challenges shape TERI University's academic programmes?"]
    assert dropped == {"list_entry": 1}


def test_the_screen_drops_a_question_without_its_answer_in_the_passage():
    candidates = [
        fu.Candidate("What did the Delhi source apportionment study find?", 2,
                     evidence=DELHI),
        # The model's own words, not the passage's.
        fu.Candidate("Who funded the Delhi source apportionment study?", 2,
                     evidence="The study was funded by the World Bank in 2019"),
        # Real words, but from passage 1, while the question names passage 2.
        fu.Candidate("How does TERI measure air quality in cities?", 2,
                     evidence=AIR_QUALITY),
        fu.Candidate("What does TERI's clean cooking programme do?", 3, evidence=""),
    ]
    kept, dropped = fu.screen(candidates, question=QUESTION, blocks=_blocks())
    assert [c.question for c in kept] == [
        "What did the Delhi source apportionment study find?"]
    assert dropped["unsupported"] == 3


def test_the_question_again_is_dropped_lexically():
    kept, dropped = _screen([("What does TERI do on air pollution?", 1),
                             ("What does TERI do on pollution?", 1)])
    assert kept == [] and dropped["repeat"] == 2


def test_an_earlier_question_is_not_suggested_again():
    kept, dropped = _screen(
        [("Who is TERI's Director General?", 1)],
        history=[{"role": "user", "content": "Who is TERI's Director General?"}],
    )
    assert kept == [] and dropped["repeat"] == 1


def test_a_synonym_swap_is_dropped_by_cosine(monkeypatch):
    vectors = {
        "What is TERI's work on air quality?": [0.999, 0.044],
        "What did the Delhi source apportionment study find?": [0.1, 0.995],
    }
    monkeypatch.setattr(fu, "_embed", lambda texts: [vectors[t] for t in texts])
    kept, dropped = _screen(
        [(q, 2) for q in vectors], query_vector=[1.0, 0.0], threshold=0.92,
    )
    assert [c.question for c in kept] == ["What did the Delhi source apportionment study find?"]
    assert dropped["repeat"] == 1


def test_near_duplicates_of_each_other_keep_only_the_first(monkeypatch):
    vectors = {
        "What did the Delhi source apportionment study find?": [0.0, 1.0],
        "What were the Delhi source apportionment study's findings?": [0.0, 0.999],
    }
    monkeypatch.setattr(fu, "_embed", lambda texts: [vectors[t] for t in texts])
    kept, dropped = _screen([(q, 2) for q in vectors], query_vector=[1.0, 0.0])
    assert len(kept) == 1 and dropped["near_duplicate"] == 1


def test_an_embedding_failure_keeps_the_candidates(monkeypatch):
    def boom(texts):
        raise RuntimeError("embeddings down")

    monkeypatch.setattr(fu, "_embed", boom)
    kept, _ = _screen([("What did the Delhi source apportionment study find?", 2)],
                      query_vector=[1.0, 0.0])
    assert len(kept) == 1


def test_no_query_vector_means_no_embedding_call(monkeypatch):
    def boom(texts):
        raise AssertionError("must not embed")

    monkeypatch.setattr(fu, "_embed", boom)
    kept, _ = _screen([("What did the Delhi source apportionment study find?", 2)])
    assert len(kept) == 1


# --------------------------------------------------------------------------- #
# The ranking, against the final answer
# --------------------------------------------------------------------------- #

def test_uncited_passages_lead_and_each_passage_counts_once():
    candidates = [
        fu.Candidate("A on cited one?", 1),
        fu.Candidate("B on cited one?", 1),
        fu.Candidate("C on uncited two?", 2),
        fu.Candidate("D on uncited three?", 3),
    ]
    assert fu.rank(candidates, "Air quality is measured [1].", 3) == [
        "C on uncited two?", "D on uncited three?", "A on cited one?",
    ]


def test_a_list_answer_keeps_only_passages_it_did_not_cite():
    candidates = [fu.Candidate("On listed one?", 1), fu.Candidate("On listed two?", 2),
                  fu.Candidate("On other three?", 3)]
    assert fu.unlisted(candidates, "Rows [1] and more rows [2].") == [candidates[2]]
    assert fu.unlisted(candidates, "No markers at all.") == candidates


def test_a_second_question_on_one_passage_fills_a_short_list():
    candidates = [fu.Candidate("A?", 1), fu.Candidate("B?", 1)]
    assert fu.rank(candidates, "Answer [1].", 3) == ["A?", "B?"]


def test_rank_caps_at_n():
    candidates = [fu.Candidate(f"Q{i}?", i) for i in range(1, 6)]
    assert len(fu.rank(candidates, "", 3)) == 3
    assert fu.rank(candidates, "", 0) == []


# --------------------------------------------------------------------------- #
# The task: when it launches, what it waits for
# --------------------------------------------------------------------------- #

def _task(monkeypatch, *, draft=None, release=None, **kw):
    calls: list = []

    def fake_draft(question, blocks, *, answer_so_far, **_):
        calls.append(answer_so_far)
        if release is not None:
            release.wait(5)
        return draft or fu.Draft(candidates=[fu.Candidate("What did the Delhi study find?", 2)],
                                 proposed=1)

    monkeypatch.setattr(fu, "draft_followups", fake_draft)
    kw.setdefault("launch_chars", 20)
    task = fu.FollowupTask(question=QUESTION, blocks=_blocks(), **kw)
    return task, calls


def test_it_launches_at_the_first_line_break_past_the_mark(monkeypatch):
    task, calls = _task(monkeypatch)
    task.feed("TERI measures air ")
    task.feed("quality.\n")           # 27 chars, a line break, but the
    assert task.launched              # mark is 20: launches here
    task.feed("More detail.\n")       # and never again
    task.collect("TERI measures air quality.\nMore detail.")
    task.close()
    assert calls == ["TERI measures air quality.\n"]


def test_it_does_not_launch_before_the_mark_or_mid_line(monkeypatch):
    task, calls = _task(monkeypatch, launch_chars=100)
    task.feed("Short.\n")
    task.feed("x" * 200)              # past the mark but mid-line
    assert not task.launched
    task.close()


def test_a_short_answer_launches_when_it_ends(monkeypatch):
    task, calls = _task(monkeypatch, launch_chars=1000)
    task.feed("TERI measures air quality [1].")
    task.finish("TERI measures air quality [1].")
    questions, info = task.collect("TERI measures air quality [1].")
    task.close()
    assert calls == ["TERI measures air quality [1]."]
    assert questions == ["What did the Delhi study find?"]
    assert info["launched_at_chars"] == len("TERI measures air quality [1].")


def test_a_catalog_prefix_is_part_of_what_the_call_sees(monkeypatch):
    task, calls = _task(monkeypatch, prefix="There are 12 reports.", launch_chars=10)
    task.feed("Air quality.\n")
    task.collect("There are 12 reports.\n\nAir quality.")   # waits for the thread
    task.close()
    assert calls == ["There are 12 reports.\n\nAir quality.\n"]


def test_a_refusal_never_launches_and_gets_none(monkeypatch):
    task, calls = _task(monkeypatch, launch_chars=1000)
    task.feed(REFUSAL)
    task.finish(REFUSAL)
    questions, info = task.collect(REFUSAL)
    task.close()
    assert calls == [] and questions == [] and info["skipped"] == "refusal"


def test_a_late_call_is_abandoned_without_holding_the_turn(monkeypatch):
    release = threading.Event()
    task, _ = _task(monkeypatch, release=release, wait_seconds=0.05)
    task.finish("TERI measures air quality [1].")
    questions, info = task.collect("TERI measures air quality [1].")
    release.set()
    task.close()
    assert questions == [] and info["timed_out"] is True


def test_collect_ranks_against_the_final_answer(monkeypatch):
    draft = fu.Draft(candidates=[fu.Candidate("On one?", 1, evidence="words in one"),
                                 fu.Candidate("On three?", 3, evidence="words in three")],
                     proposed=4, dropped={"repeat": 2}, ms=12.0)
    task, _ = _task(monkeypatch, draft=draft, count=3)
    task.finish("Answer citing one [1].")
    questions, info = task.collect("Answer citing one [1].")
    task.close()
    assert questions == ["On three?", "On one?"]
    assert info["proposed"] == 4 and info["dropped"] == {"repeat": 2} and info["ms"] == 12.0
    # The trace keeps what each shown question stood on.
    assert info["evidence"] == [{"block": 3, "evidence": "words in three"},
                                {"block": 1, "evidence": "words in one"}]


@pytest.mark.parametrize("answer_format", sorted(fu.LIST_FORMATS))
def test_a_list_answer_drops_questions_on_the_passages_it_listed(monkeypatch,
                                                                 answer_format):
    draft = fu.Draft(candidates=[fu.Candidate("On one?", 1, evidence="words in one"),
                                 fu.Candidate("On three?", 3, evidence="words in three")],
                     proposed=5, dropped={"list_entry": 3}, ms=12.0)
    task, _ = _task(monkeypatch, draft=draft, count=3, answer_format=answer_format)
    task.finish("Mr A — Director [1].")
    questions, info = task.collect("Mr A — Director [1].\nMs B — Director [1].")
    task.close()
    assert questions == ["On three?"]
    assert info["list_answer"] is True
    assert info["dropped"] == {"list_entry": 3, "listed": 1}
    assert info["evidence"] == [{"block": 3, "evidence": "words in three"}]
    assert draft.dropped == {"list_entry": 3}          # the draft is not mutated


def test_a_prose_answer_still_shows_questions_on_cited_passages(monkeypatch):
    draft = fu.Draft(candidates=[fu.Candidate("On one?", 1)], proposed=1)
    task, _ = _task(monkeypatch, draft=draft, answer_format="default")
    task.finish("Prose [1].")
    questions, info = task.collect("Prose [1].")
    task.close()
    assert questions == ["On one?"] and "list_answer" not in info


def test_a_list_answer_is_told_to_the_call(monkeypatch):
    seen: dict = {}

    def fake_draft(question, blocks, *, answer_so_far, list_answer=False, **_):
        seen["list_answer"] = list_answer
        return fu.Draft()

    monkeypatch.setattr(fu, "draft_followups", fake_draft)
    for answer_format, expected in (("list", True), ("default", False)):
        task = fu.FollowupTask(question=QUESTION, blocks=_blocks(),
                               answer_format=answer_format)
        task.finish("An answer [1].")
        task.collect("An answer [1].")
        task.close()
        assert seen["list_answer"] is expected


def test_the_list_note_is_in_the_prompt_only_for_a_list_answer(monkeypatch):
    llm = _FakeLLM([])
    _install(monkeypatch, llm)
    fu.generate_followups(QUESTION, _blocks(), answer_so_far="x", list_answer=True)
    assert "This answer is a list." in llm.messages[1][1]
    fu.generate_followups(QUESTION, _blocks(), answer_so_far="x")
    assert "This answer is a list." not in llm.messages[1][1]


def test_a_failed_task_is_no_followups(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("thread blew up")

    monkeypatch.setattr(fu, "draft_followups", boom)
    task = fu.FollowupTask(question=QUESTION, blocks=_blocks())
    task.finish("An answer [1].")
    questions, info = task.collect("An answer [1].")
    task.close()
    assert questions == [] and info["skipped"] == "error"


def test_draft_proposes_more_than_it_shows_and_screens_them(monkeypatch):
    llm = _FakeLLM([
        _suggest("What does TERI do on air pollution?", 1,             # the question again
                 evidence=AIR_QUALITY),
        _suggest("What did the Delhi source apportionment study find?", 2,
                 evidence=DELHI),
        _suggest("What did the Mars study find?", 7, evidence=DELHI),  # no such passage
        _suggest("Who funded the Delhi study?", 2,                     # not in passage 2
                 evidence="The study was funded by the World Bank"),
    ])
    _install(monkeypatch, llm)
    seen: dict = {}
    real = fu.generate_followups

    def spy(*a, **k):
        seen["n"] = k["n"]
        return real(*a, **k)

    monkeypatch.setattr(fu, "generate_followups", spy)
    draft = fu.draft_followups(QUESTION, _blocks(), answer_so_far="x", n=5)
    assert seen["n"] == 5
    assert [c.question for c in draft.candidates] == [
        "What did the Delhi source apportionment study find?"
    ]
    assert draft.proposed == 4
    assert draft.dropped == {"repeat": 1, "passage": 1, "unsupported": 1}


@pytest.mark.parametrize("count,expected", [(3, 5), (2, 4)])
def test_the_task_asks_for_two_more_than_it_shows(monkeypatch, count, expected):
    seen: dict = {}

    def fake_draft(question, blocks, *, answer_so_far, n, **_):
        seen["n"] = n
        return fu.Draft()

    monkeypatch.setattr(fu, "draft_followups", fake_draft)
    task = fu.FollowupTask(question=QUESTION, blocks=_blocks(), count=count)
    task.finish("An answer [1].")
    task.collect("An answer [1].")
    task.close()
    assert seen["n"] == expected
