"""Unit tests for claim-level production faithfulness.

Covers the deterministic numeric-mismatch check, the decomposed verify()
(evidence selection, rate composition, fail-open), and the streaming
correction event: ordering before sources/done, corrected answer persisted,
and no event when the draft verifies clean. All LLM calls stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.models.context import ContextBlock
from app.generation import faithfulness as fa
from app.pipeline import query_pipeline as pipe
from app.retrieval.understanding.query_processor import ProcessedQuery


def _block(n, text, **payload):
    payload.setdefault("source_type", "website")
    return ContextBlock(n=n, text=text, payload=payload)


# --------------------------------------------------------------------------- #
# numeric_mismatches — deterministic regex check.
# --------------------------------------------------------------------------- #

def test_numeric_match_and_mismatch():
    blocks = [_block(1, "Capacity reached 1,234 MW in 2023.")]
    assert fa.numeric_mismatches("It reached 1234 MW in 2023 [1].", blocks) == []
    assert fa.numeric_mismatches("It reached 999 MW [1].", blocks) == ["999"]


def test_numeric_markers_are_not_claims():
    blocks = [_block(1, "Capacity data without numbers.")]
    # [1] is a citation marker, not a numeric claim.
    assert fa.numeric_mismatches("See the capacity data [1].", blocks) == []


def test_numeric_checks_only_cited_blocks():
    blocks = [_block(1, "The 2023 figure was 40%."), _block(2, "In 2024 it hit 55%.")]
    # 55 lives in block 2, but only block 1 is cited -> mismatch.
    assert fa.numeric_mismatches("Growth hit 55% [1].", blocks) == ["55"]
    # No citations at all -> every block counts as evidence.
    assert fa.numeric_mismatches("Growth hit 55%.", blocks) == []


def test_a_figure_from_the_block_header_is_sourced():
    """Measured 2026-10-04: 77 of 87 flagged figures were dates and titles the
    model was shown in a header and told to date its claims by — "the 2022–23
    annual report also identifies her as Director-General"."""
    blocks = [_block(1, "From the Director General's desk.", title="Annual Report 2022-2023",
                     effective_start_date="2023-01-01")]
    assert fa.numeric_mismatches("The 2022–23 annual report names her [1].", blocks) == []
    assert fa.numeric_mismatches("A 2023 report names her [1].", blocks) == []
    assert fa.numeric_mismatches("A 2019 report names her [1].", blocks) == ["2019"]


def test_a_figure_written_another_way_is_the_same_figure():
    blocks = [_block(1, "During 2004–5, 95.40% was collected; 165 million tonnes by 2031.6 "
                        "The next survey is due 2023-09-21.")]
    assert fa.numeric_mismatches("For 2004–05, 95.4% was collected [1].", blocks) == []
    # "2031.6" is the year with a footnote marker run into it.
    assert fa.numeric_mismatches("165 million tonnes by 2031 [1].", blocks) == []
    # A date's month is not the end of a year range.
    assert fa.numeric_mismatches("In 2009 it began [1].", blocks) == ["2009"]


def test_an_abbreviated_year_range_is_read_as_its_years():
    assert fa._numbers("2022–23 and 2004-5 and 1999-00") == {"2022", "2023", "2004", "2005",
                                                             "1999", "2000"}
    assert fa._numbers("1,234 on 2023-09-21") == {"1234", "2023", "9", "21"}


def test_numeric_empty_cases():
    assert fa.numeric_mismatches("No figures here [1].", [_block(1, "text")]) == []
    assert fa.numeric_mismatches("42 things", []) == []


# --------------------------------------------------------------------------- #
# verify() — decomposed composition.
# --------------------------------------------------------------------------- #

def test_verify_flags_unsupported_claims(monkeypatch):
    claims = [fa._Claim(text="good", citations=[1]), fa._Claim(text="bad", citations=[1])]
    monkeypatch.setattr(fa, "_extract_claims", lambda a: claims)
    monkeypatch.setattr(fa, "_claim_supported", lambda text, ev: text == "good")

    report = fa.verify("answer", [_block(1, "evidence")])
    assert report.faithful is False
    assert report.unsupported == ["bad"]


def test_verify_selects_cited_evidence(monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        fa, "_extract_claims",
        lambda a: [fa._Claim(text="cited", citations=[2]),
                   fa._Claim(text="uncited", citations=[])],
    )
    monkeypatch.setattr(
        fa, "_claim_supported", lambda text, ev: seen.append((text, ev)) or True
    )

    report = fa.verify("answer", [_block(1, "one"), _block(2, "two")])
    assert report.faithful is True
    assert dict(seen) == {"cited": "two", "uncited": "one\n\ntwo"}


def test_verify_fails_open(monkeypatch):
    def boom(answer):
        raise RuntimeError("llm down")

    monkeypatch.setattr(fa, "_extract_claims", boom)
    assert fa.verify("answer", [_block(1, "b")]).faithful is True

    monkeypatch.setattr(fa, "_extract_claims", lambda a: [])
    assert fa.verify("answer", [_block(1, "b")]).faithful is True

    # Per-claim check errors (None) skip the claim rather than flag it.
    monkeypatch.setattr(fa, "_extract_claims", lambda a: [fa._Claim(text="c")])
    monkeypatch.setattr(fa, "_claim_supported", lambda text, ev: None)
    assert fa.verify("answer", [_block(1, "b")]).faithful is True

    assert fa.verify("", [_block(1, "b")]).faithful is True
    assert fa.verify("answer", []).faithful is True


# --------------------------------------------------------------------------- #
# Streaming correction event.
# --------------------------------------------------------------------------- #

def _gen(blocks):
    pq = ProcessedQuery(original="q", search_query="q")
    return pipe._Generation(pq=pq, blocks=blocks, query_vector=[0.1], top_k=6)


def _wire_stream(monkeypatch, *, check_on, faithful, persisted):
    blocks = [_block(1, "evidence text")]
    monkeypatch.setattr(pipe, "_prepare", lambda q, **kw: (None, _gen(blocks)))
    monkeypatch.setattr(
        pipe, "generate_stream",
        lambda q, b, history=None, answer_format=None, plan_directive="":
            iter(["draft ", "answer [1]"]),
    )
    monkeypatch.setattr(
        pipe, "get_settings", lambda: SimpleNamespace(faithfulness_check=check_on)
    )
    monkeypatch.setattr(
        fa, "verify",
        lambda a, b: fa.FaithfulnessReport(faithful=faithful,
                                           unsupported=[] if faithful else ["claim"]),
    )
    monkeypatch.setattr(
        pipe, "generate_answer",
        lambda q, b, history=None, correction=None, answer_format=None,
               plan_directive="": "corrected answer [1]",
    )
    monkeypatch.setattr(pipe, "_persist", lambda gen, result: persisted.update(result))


def test_stream_emits_correction_before_sources_and_persists_it(monkeypatch):
    persisted: dict = {}
    _wire_stream(monkeypatch, check_on=True, faithful=False, persisted=persisted)

    events = list(pipe.stream_answer("q"))
    types = [e["type"] for e in events]
    assert types == ["token", "token", "correction", "sources", "done"]
    correction = events[2]
    assert correction["text"] == "corrected answer [1]"
    assert correction["reason"] == "faithfulness"
    assert persisted["answer"] == "corrected answer [1]"


def test_stream_clean_answer_has_no_correction(monkeypatch):
    persisted: dict = {}
    _wire_stream(monkeypatch, check_on=True, faithful=True, persisted=persisted)

    events = list(pipe.stream_answer("q"))
    assert [e["type"] for e in events] == ["token", "token", "sources", "done"]
    assert persisted["answer"] == "draft answer [1]"


def test_stream_check_off_never_verifies(monkeypatch):
    persisted: dict = {}
    _wire_stream(monkeypatch, check_on=False, faithful=True, persisted=persisted)

    def no_verify(a, b):
        raise AssertionError("verify must not run when the check is off")

    monkeypatch.setattr(fa, "verify", no_verify)
    events = list(pipe.stream_answer("q"))
    assert [e["type"] for e in events] == ["token", "token", "sources", "done"]


def test_assemble_sets_numeric_mismatch_flag():
    gen = _gen([_block(1, "the total was 40% in 2023")])
    assert pipe._assemble("It was 40% in 2023 [1].", gen)["numeric_mismatch"] is False
    assert pipe._assemble("It was 90% in 2023 [1].", gen)["numeric_mismatch"] is True
