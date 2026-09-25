"""Unit tests for scalar vote merging and the process() wiring.

Covers _vote (majority + tie behavior) and process(): exploratory-temperature
samples when analysis_votes > 1, dropped erroring votes, all-failed passthrough,
and the single-call path at votes=1. The multi-label merge, confidence, and
legacy derivation live in test_intent_understanding.py. LLM is stubbed; no
network.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.retrieval.understanding import query_processor as qp


def _u(labels, **kw):
    """Build a QueryUnderstanding (v2) from (label, confidence) pairs + attrs."""
    kw.setdefault("query_rewrite", "q")
    intents = [
        qp.IntentPrediction(label=lbl, confidence=c, rationale="") for lbl, c in labels
    ]
    return qp.QueryUnderstanding(intents=intents, **kw)


# --------------------------------------------------------------------------- #
# Vote math.
# --------------------------------------------------------------------------- #

def test_vote_majority_wins():
    assert qp._vote(["count", "count", "list"]) == "count"
    assert qp._vote([None, None, "count"]) is None  # None is a real majority
    assert qp._vote([["a"], ["a"], ["b"]]) == ["a"]  # list slots (tags)


def test_vote_ties_take_first_non_null():
    assert qp._vote(["count", "list", None]) == "count"
    assert qp._vote([None, "list", "count"]) == "list"
    assert qp._vote([None, None]) is None


# --------------------------------------------------------------------------- #
# process() wiring.
# --------------------------------------------------------------------------- #

class _FakeModel:
    """Pops one prepared response per invoke (list.pop is atomic, so the
    concurrent vote threads each get their own item)."""

    def __init__(self, feed):
        self._feed = list(feed)

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        item = self._feed.pop()
        if isinstance(item, Exception):
            raise item
        return item


def _settings(monkeypatch, votes):
    monkeypatch.setattr(qp, "get_settings", lambda: SimpleNamespace(analysis_votes=votes))


def test_process_merges_three_votes(monkeypatch):
    model = _FakeModel([
        _u([("database", 0.9)], operation="count"),
        _u([("database", 0.9)], operation="count"),
        _u([("qa", 0.8)], operation=None),
    ])
    temps: list = []

    def fake_get_llm(temperature=None, streaming=False):
        temps.append(temperature)
        return model

    monkeypatch.setattr(qp, "get_llm", fake_get_llm)
    _settings(monkeypatch, 3)

    pq = qp.process("how many news items?")

    assert temps == [0.7, 0.7, 0.7]  # exploratory temperature per sample
    assert pq.intent == "structured"  # database -> legacy structured route
    assert pq.analysis.operation == "count"


def test_process_drops_erroring_votes(monkeypatch):
    model = _FakeModel([
        RuntimeError("one vote down"),
        _u([("database", 0.9)], operation="count"),
        _u([("database", 0.9)], operation="count"),
    ])
    monkeypatch.setattr(qp, "get_llm", lambda **kw: model)
    _settings(monkeypatch, 3)

    pq = qp.process("how many news items?")
    assert pq.intent == "structured"


def test_process_all_votes_failing_is_passthrough(monkeypatch):
    model = _FakeModel([RuntimeError("down")] * 3)
    monkeypatch.setattr(qp, "get_llm", lambda **kw: model)
    _settings(monkeypatch, 3)

    pq = qp.process("how many news items?")
    assert pq.intent == "qa" and pq.analysis is None
    assert pq.search_query == "how many news items?"


def test_single_vote_keeps_pinned_llm(monkeypatch):
    def no_exploratory(**kw):
        raise AssertionError("voted path must not run at votes=1")

    monkeypatch.setattr(qp, "get_llm", no_exploratory)
    monkeypatch.setattr(
        qp, "get_structured_llm", lambda: _FakeModel([_u([("chitchat", 0.9)])])
    )
    _settings(monkeypatch, 1)

    assert qp.process("hi").intent == "chitchat"


# --------------------------------------------------------------------------- #
# Merge completeness. _merge_understanding rebuilds QueryUnderstanding field by
# field, so a slot it forgets is silently reset to its default rather than
# failing.
# --------------------------------------------------------------------------- #

_MERGE_EXEMPT = frozenset({
    "query_rewrite",  # taken from a matching sample, not voted
    "intents",        # agreement-resolved, not voted
    "scope",          # merged field by field into a QueryScope
})


def test_merge_votes_every_understanding_slot():
    """Guards the whole class of bug: add a field to QueryUnderstanding and this
    fails until _merge_understanding votes on it too."""
    import inspect

    source = inspect.getsource(qp._merge_understanding)
    missing = [
        name for name in qp.QueryUnderstanding.model_fields
        if name not in _MERGE_EXEMPT and f"s.{name}" not in source
    ]
    assert not missing, f"_merge_understanding drops: {missing}"
