"""Follow-up questions through the stream: when they are sent, and when not.

The contract under test is the SSE sequence. Off, every path streams exactly
what it always has. On, a generated answer streams ``token… sources followups
done`` — follow-ups after ``sources`` (which is when the widget renders the
answer) and before ``done`` (after which it stops reading) — and a turn whose
follow-ups fail, run late or would follow a refusal still ends with ``done``.

``_prepare`` and generation are stubbed, and so is the follow-up call itself
(``draft_followups``); its rules are tested in
``tests/generation/test_followups.py``. No network.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from app.generation import followups as fu
from app.generation.prompts import REFUSAL
from app.pipeline import query_pipeline as pipe
from app.retrieval.context.builder import ContextBlock
from app.retrieval.understanding import query_processor as qp

QUESTIONS = ["What did the Delhi source apportionment study find?",
             "How does TERI's clean cooking programme work?"]


def _gen(db_prefix="", answer_format="default"):
    blocks = [
        ContextBlock(n=1, text="TERI measures air quality.", payload={"title": "Air"}),
        ContextBlock(n=2, text="A Delhi study found vehicles and dust.",
                     payload={"title": "Delhi study"}),
    ]
    pq = qp.ProcessedQuery(original="q", search_query="TERI air pollution", intent="qa",
                           answer_format=answer_format)
    return pipe._Generation(pq=pq, blocks=blocks, query_vector=[1.0, 0.0], top_k=6,
                            db_prefix=db_prefix)


def _settings(on=True, **kw):
    base = dict(faithfulness_check=False, followups_enabled=on, followups_count=3,
                followups_launch_chars=10, followups_wait_seconds=2.0,
                followups_reasoning_effort="", multi_query_distinct_threshold=0.92)
    base.update(kw)
    return SimpleNamespace(**base)


def _drive(monkeypatch, tokens, *, settings, draft=None, release=None, gen=None):
    """Stream one answer; return (events, what the call saw, what was stored,
    the stage dict `_record` got)."""
    seen: list[str] = []
    stored: dict = {}
    recorded: dict = {}

    def fake_draft(question, blocks, *, answer_so_far, **_):
        seen.append(answer_so_far)
        if release is not None:
            release.wait(5)
        return draft if draft is not None else fu.Draft(
            candidates=[fu.Candidate(QUESTIONS[0], 2), fu.Candidate(QUESTIONS[1], 1)],
            proposed=2, ms=5.0)

    monkeypatch.setattr(fu, "draft_followups", fake_draft)
    monkeypatch.setattr(pipe, "_prepare", lambda q, **kw: (None, gen or _gen()))
    monkeypatch.setattr(pipe, "generate_stream", lambda *a, **k: iter(tokens))
    monkeypatch.setattr(pipe, "get_settings", lambda: settings)
    monkeypatch.setattr(pipe, "_persist", lambda gen, result: stored.update(result))
    monkeypatch.setattr(pipe, "_record",
                        lambda s, result, stages, trace: recorded.update(stages))
    events = list(pipe.stream_answer("What does TERI do on air pollution?"))
    return events, seen, stored, recorded


def _types(events):
    return [e["type"] for e in events]


# --------------------------------------------------------------------------- #
# Off: nothing changes
# --------------------------------------------------------------------------- #

def test_off_streams_exactly_what_it_always_has(monkeypatch):
    events, seen, stored, _ = _drive(
        monkeypatch, ["TERI measures air quality [1].\n", "More [2]."],
        settings=_settings(on=False),
    )
    assert _types(events) == ["token", "token", "sources", "done"]
    assert seen == [] and "followups" not in stored


def test_off_needs_no_followup_settings_at_all(monkeypatch):
    """A deployment whose settings predate the feature still streams."""
    events, seen, _, _ = _drive(monkeypatch, ["Answer [1]."],
                                settings=SimpleNamespace(faithfulness_check=False))
    assert _types(events) == ["token", "sources", "done"] and seen == []


# --------------------------------------------------------------------------- #
# On: after sources, before done
# --------------------------------------------------------------------------- #

def test_on_sends_followups_between_sources_and_done(monkeypatch):
    events, _, stored, recorded = _drive(
        monkeypatch, ["TERI measures air quality [1].\n", "More detail."],
        settings=_settings(),
    )
    assert _types(events) == ["token", "token", "sources", "followups", "done"]
    sent = events[3]["questions"]
    # Passage 2 was not cited, so its question leads.
    assert sent == QUESTIONS
    assert stored["followups"] == QUESTIONS        # cached with the answer
    assert recorded["rag.followups"] == 5.0        # timed despite the yield


def test_a_list_answer_shows_only_questions_on_passages_it_did_not_list(monkeypatch):
    events, _, stored, _ = _drive(
        monkeypatch, ["Air quality monitors [1].\n", "More rows [1]."],
        settings=_settings(), gen=_gen(answer_format="list"),
    )
    assert _types(events) == ["token", "token", "sources", "followups", "done"]
    # QUESTIONS[1] stands on passage 1, which the list answer cited.
    assert events[3]["questions"] == [QUESTIONS[0]]
    assert stored["followups"] == [QUESTIONS[0]]


def test_the_call_sees_the_answer_so_far_not_the_whole_answer(monkeypatch):
    _, seen, _, _ = _drive(
        monkeypatch,
        ["TERI measures air quality [1].\n", "A second paragraph.\n", "A third."],
        settings=_settings(followups_launch_chars=10),
    )
    assert seen == ["TERI measures air quality [1].\n"]


def test_a_short_answer_launches_when_it_ends(monkeypatch):
    _, seen, _, _ = _drive(monkeypatch, ["TERI measures air quality [1]."],
                           settings=_settings(followups_launch_chars=10_000))
    assert seen == ["TERI measures air quality [1]."]


def test_a_combined_answer_shows_the_call_its_catalog_section(monkeypatch):
    _, seen, _, _ = _drive(monkeypatch, ["Air quality [1].\n"],
                           settings=_settings(), gen=_gen(db_prefix="There are 12 reports."))
    assert seen == ["There are 12 reports.\n\nAir quality [1].\n"]


def test_nothing_survived_means_no_followups_event(monkeypatch):
    events, _, stored, _ = _drive(monkeypatch, ["Answer [1]."], settings=_settings(),
                                  draft=fu.Draft(proposed=3, dropped={"repeat": 3}))
    assert _types(events) == ["token", "sources", "done"]
    assert "followups" not in stored


def test_a_late_call_still_ends_the_turn(monkeypatch):
    release = threading.Event()
    try:
        events, _, stored, _ = _drive(monkeypatch, ["Answer [1]."], release=release,
                                      settings=_settings(followups_wait_seconds=0.05))
    finally:
        release.set()
    assert _types(events) == ["token", "sources", "done"]
    assert "followups" not in stored


def test_a_refusal_gets_no_followups_and_no_call(monkeypatch):
    events, seen, _, _ = _drive(monkeypatch, [REFUSAL],
                                settings=_settings(followups_launch_chars=10_000))
    assert _types(events) == ["token", "sources", "done"]
    assert seen == []


def test_a_client_that_leaves_mid_answer_closes_the_task(monkeypatch):
    closed: list = []
    real_close = fu.FollowupTask.close

    def spy(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(fu.FollowupTask, "close", spy)
    monkeypatch.setattr(fu, "draft_followups", lambda *a, **k: fu.Draft())
    monkeypatch.setattr(pipe, "_prepare", lambda q, **kw: (None, _gen()))
    monkeypatch.setattr(pipe, "generate_stream",
                        lambda *a, **k: iter(["Opening line [1].\n", "More."]))
    monkeypatch.setattr(pipe, "get_settings", lambda: _settings())
    stream = pipe.stream_answer("q")
    assert next(stream)["type"] == "token"
    stream.close()                                   # the client went away
    assert closed == [True]


# --------------------------------------------------------------------------- #
# Ready-made results: cache hits and catalog answers
# --------------------------------------------------------------------------- #

def test_a_cached_answer_replays_its_followups():
    events = list(pipe._stream_result(
        {"answer": "Cached answer [1].", "followups": QUESTIONS}
    ))
    assert _types(events) == ["token", "sources", "followups", "done"]
    assert events[2]["questions"] == QUESTIONS


def test_a_result_without_followups_streams_as_before():
    events = list(pipe._stream_result({"answer": "Cached answer [1]."}))
    assert _types(events) == ["token", "sources", "done"]


def test_a_clarification_carries_no_followups():
    events = list(pipe._stream_result({
        "answer": "Which theme?", "intent": "clarification",
        "clarification": {"question": "Which theme?", "options": ["Energy", "Water"],
                          "kind": "theme"},
    }))
    assert _types(events) == ["token", "sources", "done"]
