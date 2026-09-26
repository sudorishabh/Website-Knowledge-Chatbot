"""Unit tests for ingest-time abstract generation.

Covers the skip rule, single-call vs map-reduce path selection, the window
splitter, the raise-vs-None failure contract, and version-fingerprint
invalidation. Every model call is stubbed through ``_complete``; no network.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.core.models import CanonicalDocument, CanonicalSection
from app.ingestion import enrich
from app.ingestion.chunking.packer import get_encoder


def _doc(body: str, title: str | None = "A Report") -> CanonicalDocument:
    return CanonicalDocument(
        document_id="d1",
        source_type="pdf",
        title=title,
        sections=[CanonicalSection(text=body, order=0)],
    )


def _stub(monkeypatch, reply="An abstract.") -> list[tuple[str, str]]:
    """Record (system, human) for every model call and return a fixed reply."""
    calls: list[tuple[str, str]] = []

    def fake(system: str, human: str) -> str:
        calls.append((system, human))
        return reply

    monkeypatch.setattr(enrich, "_complete", fake)
    return calls


# Comfortably over _MIN_CHARS but well under _SINGLE_CALL_TOKENS.
_MEDIUM = "The programme added 1.2 GW of rooftop capacity in 2023. " * 40


# --------------------------------------------------------------------------- #
# The skip rule — short documents are their own summary.
# --------------------------------------------------------------------------- #

def test_short_documents_are_skipped_without_calling_the_model(monkeypatch):
    calls = _stub(monkeypatch)
    assert enrich.generate_abstract(_doc("A one-line video stub.")) is None
    assert calls == []


def test_an_empty_document_is_skipped(monkeypatch):
    calls = _stub(monkeypatch)
    assert enrich.generate_abstract(_doc("")) is None
    assert calls == []


# --------------------------------------------------------------------------- #
# Path selection.
# --------------------------------------------------------------------------- #

def test_a_medium_document_takes_one_direct_call(monkeypatch):
    calls = _stub(monkeypatch)

    assert enrich.generate_abstract(_doc(_MEDIUM)) == "An abstract."

    assert len(calls) == 1
    system, human = calls[0]
    assert system == enrich._DIRECT_SYSTEM
    assert human.startswith("Title: A Report")


def test_the_title_is_omitted_when_absent(monkeypatch):
    calls = _stub(monkeypatch)
    enrich.generate_abstract(_doc(_MEDIUM, title=None))
    assert not calls[0][1].startswith("Title:")


def test_a_long_document_maps_then_reduces(monkeypatch):
    calls = _stub(monkeypatch)
    body = "\n\n".join("Capacity grew across the region this year. " * 60 for _ in range(40))
    assert get_encoder(enrich._ENCODING).count(body) > enrich._SINGLE_CALL_TOKENS

    assert enrich.generate_abstract(_doc(body)) == "An abstract."

    systems = [s for s, _ in calls]
    assert systems.count(enrich._MAP_SYSTEM) >= 2
    assert systems[-1] == enrich._REDUCE_SYSTEM
    assert "Section notes:" in calls[-1][1]


def test_an_empty_model_reply_yields_no_abstract(monkeypatch):
    _stub(monkeypatch, reply="   ")
    assert enrich.generate_abstract(_doc(_MEDIUM)) is None


# --------------------------------------------------------------------------- #
# Failure contract: raise so the caller can count the attempt.
# --------------------------------------------------------------------------- #

def test_model_failure_propagates(monkeypatch):
    def boom(system, human):
        raise RuntimeError("deployment rate limited")

    monkeypatch.setattr(enrich, "_complete", boom)

    with pytest.raises(RuntimeError, match="rate limited"):
        enrich.generate_abstract(_doc(_MEDIUM))


# --------------------------------------------------------------------------- #
# Window splitting.
# --------------------------------------------------------------------------- #

def test_windows_split_on_paragraph_boundaries_within_budget():
    enc = get_encoder(enrich._ENCODING)
    paras = ["word " * 100 for _ in range(10)]
    windows = enrich._windows("\n\n".join(paras), 250, enc)

    assert len(windows) > 1
    assert all(enc.count(w) <= 250 for w in windows)
    # Nothing is lost and order is preserved.
    assert " ".join(windows).split() == " ".join(paras).split()


def test_an_oversized_paragraph_is_split_rather_than_dropped():
    enc = get_encoder(enrich._ENCODING)
    windows = enrich._windows("word " * 900, 100, enc)

    assert len(windows) > 1
    assert all(enc.count(w) <= 100 for w in windows)


def test_blank_paragraphs_are_dropped():
    enc = get_encoder(enrich._ENCODING)
    assert enrich._windows("alpha\n\n\n\n   \n\nbeta", 1000, enc) == ["alpha\n\nbeta"]


# --------------------------------------------------------------------------- #
# Version fingerprint — a retune must invalidate cached abstracts.
# --------------------------------------------------------------------------- #

def test_version_is_stable_across_calls():
    assert enrich.abstract_version() == enrich.abstract_version()


def test_editing_a_prompt_changes_the_version(monkeypatch):
    before = enrich.abstract_version()
    monkeypatch.setattr(enrich, "_DIRECT_SYSTEM", enrich._DIRECT_SYSTEM + " Be terse.")
    assert enrich.abstract_version() != before


def test_repointing_the_deployment_changes_the_version(monkeypatch):
    before = enrich.abstract_version()
    settings = enrich.get_settings()
    monkeypatch.setattr(
        enrich, "get_settings", lambda: settings.model_copy(update={"azure_openai_model": "other"})
    )
    assert enrich.abstract_version() != before


def test_resizing_the_map_window_changes_the_version(monkeypatch):
    before = enrich.abstract_version()
    monkeypatch.setattr(enrich, "_MAP_WINDOW_TOKENS", enrich._MAP_WINDOW_TOKENS + 1)
    assert enrich.abstract_version() != before


def test_a_model_reply_in_parts_is_read_as_text(monkeypatch):
    # gpt-6-luna replies over the Responses API with a list of parts, not a string.
    class Model:
        def invoke(self, messages):
            return AIMessage(content=[{"type": "text", "text": "  An abstract.  "}])

    monkeypatch.setattr("app.core.clients.llm.get_llm", lambda *a, **kw: Model())
    assert enrich._complete("system", "human") == "An abstract."
