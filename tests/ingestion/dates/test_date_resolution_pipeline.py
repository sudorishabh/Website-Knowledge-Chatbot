"""Tests for the canonical resolver: :func:`app.ingestion.date_resolution.resolve`.

These cover the orchestration — routing, the re-check after reading the PDF, the
mapping of a model verdict onto a date, and the fail-closed guarantee. The rules
themselves are covered by ``tests/test_date_resolution.py``.

The property every test here defends: ``effective_start_date`` moves only on an
override, and everything else lands on the page's own date.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ingestion import date_resolution
from app.ingestion.date_llm import DateInterpretation
from app.ingestion.date_resolution import build_evidence
from app.ingestion.date_resolution import resolve as resolve_pdf_date

NODE_DATE = "2020-01-10T00:00:00+00:00"


def _node(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=kwargs.pop("uuid", "node-1"),
        title=kwargs.pop("title", "A page"),
        url=kwargs.pop("url", "https://teriin.org/page"),
        created=kwargs.pop("created", NODE_DATE),
        bundle=kwargs.pop("bundle", "page"),
        files=kwargs.pop("files", []),
        **kwargs,
    )


def _file(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=kwargs.pop("uuid", "f1"),
        url=kwargs.pop("url", "https://teriin.org/a.pdf"),
        filename=kwargs.pop("filename", "a.pdf"),
        description=kwargs.pop("description", None),
        origin=kwargs.pop("origin", "attachment"),
        created=kwargs.pop("created", None),
        **kwargs,
    )


def _dated_node(**kwargs) -> SimpleNamespace:
    """A page whose bundle *states* its date, resolving to ``NODE_DATE``.

    Used wherever a test's subject is the interpreter rather than the shelf rule.
    A file may only fall back to its page's date when that date is a claim about
    the content — see :func:`app.ingestion.date_rules.page_date_is_usable` — so a
    test that asserts "the page's date stands" has to be run against a page whose
    date actually stands for something. ``page``-bundle nodes, whose date is only
    a Drupal creation stamp, are used below for the cases where that is the point.
    """
    kwargs.setdefault("bundle", "news")
    kwargs.setdefault("metadata", {"field_news_date": NODE_DATE})
    return _node(**kwargs)


def _evidence(node=None, file=None, count=None):
    return build_evidence(document_id="d1", node=node or _node(),
                          file=file or _file(), page_pdf_count=count)


# --------------------------------------------------------------------------- #
# Evidence adaptation
# --------------------------------------------------------------------------- #

def test_page_pdf_count_defaults_to_the_nodes_file_count():
    node = _node(files=[_file(), _file(uuid="f2"), _file(uuid="f3")])
    assert _evidence(node=node).page.pdf_count == 3


def test_a_node_with_no_files_still_counts_as_one_pdf():
    assert _evidence(node=_node(files=[])).page.pdf_count == 1


def test_the_file_description_is_carried_as_anchor_text():
    evidence = _evidence(file=_file(description="Annual Report 2021-2022"))
    assert evidence.edition == "2021-22"


# --------------------------------------------------------------------------- #
# The default: the page's date
# --------------------------------------------------------------------------- #

def test_a_single_pdf_page_keeps_the_page_date_without_reading_the_pdf(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("the PDF must not be read for a settled case")

    monkeypatch.setattr(date_resolution, "_read_pdf_signals", _boom)
    got = resolve_pdf_date(_evidence(), content=b"%PDF-")
    assert got.start_value == NODE_DATE
    assert got.overridden is False
    assert got.decision.rule == "single_pdf_page"
    assert "llm" not in got.used


def test_a_late_upload_does_not_move_the_date(monkeypatch):
    """Routed for a look. Even if the model claims a publication date, an empty
    document cannot ground it, so the page date stands.

    The routed path deliberately still asks when the text is unreadable — the
    validated run made 315 calls against 122 fetch failures — because grounding,
    not routing, is what prevents the override.
    """
    monkeypatch.setattr(date_resolution, "_read_pdf_signals", lambda *_a, **_k: None)
    verdict = DateInterpretation(
        candidate_start_date="2024-06-01", date_type="publication",
        publication_statement="Published on 1 June 2024",
        confidence=0.99, recommended_action="override")
    verdict.set_grounded(False, False)          # nothing readable to ground against
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: verdict)
    node = _dated_node(files=[_file(), _file(uuid="f2")])
    file = _file(created="2024-06-01T00:00:00+00:00")
    got = resolve_pdf_date(_evidence(node=node, file=file), content=b"%PDF-")
    assert got.start_value == NODE_DATE
    assert got.overridden is False
    assert got.needs_review is True


# --------------------------------------------------------------------------- #
# Overrides: only a surviving verdict moves the date
# --------------------------------------------------------------------------- #

def _route_to_llm(monkeypatch, head_text: str, verdict: DateInterpretation | None):
    """Force the routed path with a given PDF text and model verdict."""
    def _fill(evidence, _content):
        evidence.head_text = head_text
        evidence.pdf_created = "2024-01-01T00:00:00+00:00"

    monkeypatch.setattr(date_resolution, "_read_pdf_signals", _fill)
    if verdict is not None:
        verdict.set_grounded(
            *_grounding(verdict, head_text)
        )
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: verdict)


def _grounding(verdict: DateInterpretation, text: str) -> tuple[bool, bool]:
    """Both grounding checks, at the precision the statement establishes — the
    same pair :func:`app.ingestion.date_llm.interpret` applies."""
    from app.ingestion.date_llm import period_is_in_text, statement_is_in_text

    return (period_is_in_text(verdict.candidate_start_date, text,
                              verdict.supported_precision() or "day"),
            statement_is_in_text(verdict.publication_statement, text))


def _routed_evidence():
    """A multi-PDF page whose file date diverges — the routed shape.

    The page states its own date (`_dated_node`), so the fallback these tests
    assert is a real one: the subject here is which verdicts survive the gates,
    not whether a creation stamp may be borrowed.
    """
    node = _dated_node(files=[_file(), _file(uuid="f2"), _file(uuid="f3")])
    return _evidence(node=node, file=_file(created="2024-06-01T00:00:00+00:00"))


def test_a_grounded_publication_statement_moves_the_date(monkeypatch):
    text = "Press Release New Delhi, 31 March 2025 TERI announces findings"
    _route_to_llm(monkeypatch, text, DateInterpretation(
        candidate_start_date="2025-03-31", date_type="publication",
        publication_statement="New Delhi, 31 March 2025",
        confidence=0.95, recommended_action="override"))
    got = resolve_pdf_date(_routed_evidence(), content=b"%PDF-")
    assert got.start_value == "2025-03-31"
    assert got.overridden is True
    assert got.decision.source == "llm_publication"
    assert "llm" in got.used


def test_a_reconstructed_statement_does_not_move_the_date(monkeypatch):
    """The Pioneer failure: the date is in the text, the statement is not."""
    _route_to_llm(monkeypatch, "12/24/13 The Pioneer", DateInterpretation(
        candidate_start_date="2013-12-24", date_type="publication",
        publication_statement="The Pioneer, Tuesday, December 24, 2013",
        confidence=0.95, recommended_action="override"))
    got = resolve_pdf_date(_routed_evidence(), content=b"%PDF-")
    assert got.start_value == NODE_DATE
    assert got.overridden is False
    assert got.needs_review is True


def test_a_notification_verdict_does_not_move_the_date(monkeypatch):
    _route_to_llm(monkeypatch, "Notified on 18.05.2023 by the Ministry.",
                  DateInterpretation(
                      candidate_start_date="2023-05-18", date_type="notification",
                      publication_statement="Notified on 18.05.2023",
                      confidence=0.99, recommended_action="override"))
    got = resolve_pdf_date(_routed_evidence(), content=b"%PDF-")
    assert got.start_value == NODE_DATE
    assert got.decision.date_type == "notification"


def test_an_edition_verdict_yields_a_label_and_keeps_the_date(monkeypatch):
    _route_to_llm(monkeypatch, "ANNUAL REPORT 2024/25 Vision", DateInterpretation(
        candidate_start_date=None, date_type="edition", edition_label="2024-2025",
        confidence=0.99, recommended_action="keep_page_date"))
    got = resolve_pdf_date(_routed_evidence(), content=b"%PDF-")
    assert got.start_value == NODE_DATE
    assert got.edition_label == "2024-2025"
    assert got.overridden is False


def test_a_model_outage_keeps_the_page_date(monkeypatch):
    _route_to_llm(monkeypatch, "some readable text 2024", None)
    got = resolve_pdf_date(_routed_evidence(), content=b"%PDF-")
    assert got.start_value == NODE_DATE
    assert got.decision.rule == "llm_unavailable"


def test_the_raw_verdict_is_returned_for_the_audit_trail(monkeypatch):
    text = "Published on 12 September 2024 by TERI"
    _route_to_llm(monkeypatch, text, DateInterpretation(
        candidate_start_date="2024-09-12", date_type="publication",
        publication_statement="Published on 12 September 2024",
        confidence=0.96, recommended_action="override"))
    got = resolve_pdf_date(_routed_evidence(), content=b"%PDF-")
    assert got.llm_raw and got.llm_raw["candidate_start_date"] == "2024-09-12"


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #

def test_an_unreadable_pdf_cannot_produce_an_override(monkeypatch):
    """Real bytes that PyMuPDF cannot parse: head text stays empty, so grounding
    fails and no date can move — even against a maximally confident verdict.

    The stub stands in for the model so the unit suite stays offline; the
    grounding it is checked against is the production one.
    """
    seen: list[str] = []

    def _stub(evidence):
        from app.ingestion.date_llm import date_is_in_text, statement_is_in_text

        seen.append(evidence.head_text)
        verdict = DateInterpretation(
            candidate_start_date="2024-06-01", date_type="publication",
            publication_statement="Published on 1 June 2024",
            confidence=0.99, recommended_action="override")
        verdict.set_grounded(
            date_is_in_text(verdict.candidate_start_date, evidence.head_text),
            statement_is_in_text(verdict.publication_statement, evidence.head_text),
        )
        return verdict

    monkeypatch.setattr("app.ingestion.date_llm.interpret", _stub)
    got = resolve_pdf_date(_routed_evidence(), content=b"not a pdf")
    assert seen == [""], "the model must be shown no text for an unreadable PDF"
    assert got.start_value == NODE_DATE
    assert got.overridden is False


def test_missing_content_keeps_the_page_date(monkeypatch):
    """No bytes at all — the routed case still asks, and still cannot ground."""
    verdict = DateInterpretation(
        candidate_start_date="2024-06-01", date_type="publication",
        publication_statement="Published on 1 June 2024",
        confidence=0.99, recommended_action="override")
    verdict.set_grounded(False, False)
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: verdict)
    got = resolve_pdf_date(_routed_evidence(), content=None)
    assert got.start_value == NODE_DATE
    assert got.overridden is False


def test_an_unexpected_error_keeps_the_page_date(monkeypatch):
    def _explode(_evidence):
        raise RuntimeError("boom")

    monkeypatch.setattr(date_resolution, "decide", _explode)
    got = resolve_pdf_date(_evidence(), content=b"%PDF-")
    assert got.start_value == NODE_DATE
    assert got.overridden is False


def test_a_node_without_a_date_never_invents_one():
    got = resolve_pdf_date(_evidence(node=_node(created=None)), content=b"%PDF-")
    assert got.start_value is None
    assert got.overridden is False


# --------------------------------------------------------------------------- #
# Inheritance: the page's *resolved* date, not its creation stamp
# --------------------------------------------------------------------------- #

def test_the_page_date_a_pdf_inherits_is_the_bundles_resolved_date():
    """The page's own builder applies `field_rpaper_year`; before this change the
    attachment path read `node.created` instead, so a paper's page and its PDF
    carried different dates."""
    node = _node(bundle="research_papers", files=[_file()],
                 metadata={"field_rpaper_year": 2016})
    got = resolve_pdf_date(_evidence(node=node), content=b"%PDF-")
    assert got.start_value == "2016-01-01T00:00:00+00:00"
    assert got.start_precision == "year"


def test_a_stated_bundle_date_settles_a_single_pdf_without_reading_it(monkeypatch):
    """The page says what date this content type carries and holds one file, so
    there is nothing for a file-level reading to improve on — and nothing is paid
    for one.

    Previously this held for a page with three files too. It no longer does, and
    the test below is that change: an authoritative date for a *page* says
    nothing about when each *file* on a shelf came out.
    """
    def _boom(*_a, **_k):
        raise AssertionError("the PDF must not be read")

    monkeypatch.setattr(date_resolution, "_read_pdf_signals", _boom)
    monkeypatch.setattr("app.ingestion.date_llm.interpret",
                        lambda _e: pytest.fail("the model must not be called"))
    node = _node(bundle="news", metadata={"field_news_date": "2015-08-26T18:30:00+00:00"},
                 files=[_file()])
    got = resolve_pdf_date(_evidence(node=node, file=_file()), content=b"%PDF-")
    assert got.start_value == "2015-08-27T00:00:00+00:00"
    assert got.decision.rule == "parent_bundle_date_field"
    assert "llm" not in got.used


def test_a_stated_bundle_date_no_longer_answers_for_every_pdf_on_a_shelf(monkeypatch):
    """Requirement 3. The page's configured field still gives the *fallback*, but
    each file on a multi-PDF page is read before that fallback is accepted.

    The files here arrived with the page, so the routing keeps the page date —
    what changed is that the document was consulted first and the record says so.
    """
    monkeypatch.setattr("app.ingestion.date_llm.interpret",
                        lambda _e: pytest.fail("the model must not be called"))
    node = _node(bundle="news", metadata={"field_news_date": "2015-08-26T18:30:00+00:00"},
                 created=NODE_DATE,
                 files=[_file(created=NODE_DATE), _file(uuid="f2", created=NODE_DATE),
                        _file(uuid="f3", created=NODE_DATE)])
    got = resolve_pdf_date(_evidence(node=node, file=_file(created=NODE_DATE)),
                           content=_book_pdf(copyright_line="", creation="D:20240101000000+00'00'"))

    assert got.start_value == "2015-08-27T00:00:00+00:00", "the page date is the fallback"
    assert got.decision.rule == "multi_pdf_uploaded_with_page"
    assert got.decision.rule != "parent_bundle_date_field", "Case 0 must not fire"
    assert "pdf_text" in got.used, "the file's own bytes were read"
    assert "read for a date of its own" in got.decision.evidence
    assert "llm" not in got.used


def test_two_pdfs_on_one_page_can_get_different_dates(monkeypatch):
    """Requirement 3, end to end through the real PyMuPDF readers.

    One shelf page, two books, two different dates, neither of them the page's.
    This is the property the old policy made impossible.
    """
    monkeypatch.setattr("app.ingestion.date_llm.interpret",
                        lambda _e: pytest.fail("the model must not be called"))
    node = _node(created="2025-09-30T04:28:20+00:00",
                 files=[_file(uuid="a", origin="inbody", filename="book-one.pdf",
                              description="TERI Bookstore"),
                        _file(uuid="b", origin="inbody", filename="book-two.pdf",
                              description="TERI Bookstore")])

    first = resolve_pdf_date(
        _evidence(node=node, file=node.files[0]),
        _book_pdf(copyright_line="© TERI Alumni Association 2020",
                  creation="D:20200921175150+00'00'"))
    second = resolve_pdf_date(
        _evidence(node=node, file=node.files[1]),
        _book_pdf(copyright_line="© TERI Alumni Association 2022",
                  creation="D:20220302104252+00'00'"))

    assert first.start_value == "2020-01-01T00:00:00+00:00"
    assert second.start_value == "2022-01-01T00:00:00+00:00"
    assert first.start_value != second.start_value, "two documents, two dates"
    assert (first.start_precision, second.start_precision) == ("year", "year")
    assert first.decision.rule == "copyright_statement_corroborated"
    assert second.decision.rule == "copyright_statement_corroborated"
    assert first.decision.decided_by == "deterministic"


def test_one_dated_and_one_undated_pdf_on_the_same_page(monkeypatch):
    """The mixed shelf: the file that states a date gets it, and the file that
    states nothing gets **no date** — the page's 2025 stamp is a fact about the
    page, and two files that shared it were published five years apart. What is
    recorded either way is the reason, not silence."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret",
                        lambda _e: pytest.fail("the model must not be called"))
    page_date = "2025-09-30T04:28:20+00:00"
    node = _node(created=page_date,
                 files=[_file(uuid="a", origin="inbody", filename="dated.pdf",
                              description="TERI Bookstore"),
                        _file(uuid="b", origin="inbody", filename="undated.pdf",
                              description="TERI Bookstore")])

    dated = resolve_pdf_date(
        _evidence(node=node, file=node.files[0]),
        _book_pdf(copyright_line="© TERI Alumni Association 2020",
                  creation="D:20200921175150+00'00'"))
    undated = resolve_pdf_date(
        _evidence(node=node, file=node.files[1]),
        _book_pdf(copyright_line="", creation="D:20200921175150+00'00'"))

    assert dated.start_value == "2020-01-01T00:00:00+00:00"
    assert dated.start_precision == "year"
    assert dated.overridden is True

    assert undated.start_value is None, "no verifiable date of its own"
    assert undated.start_precision is None, "and so no precision to claim"
    assert undated.overridden is False
    assert undated.dropped is True
    assert undated.canonical_source == "no_evidence"
    assert undated.decision.rule == "multi_pdf_no_evidence"
    assert "read for a date" in undated.decision.evidence
    assert "undated" in undated.decision.evidence


def test_the_multi_pdf_fallback_keeps_the_pages_provenance_and_precision(monkeypatch):
    """Provenance and precision propagation for the fallback case: a file that
    states nothing carries `parent_page`, and a year-precision page hands over
    year precision rather than a manufactured day."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret",
                        lambda _e: pytest.fail("the model must not be called"))
    paper = _node(bundle="research_papers", metadata={"field_rpaper_year": 2016},
                  files=[_file(origin="inbody", filename="a.pdf"),
                         _file(uuid="f2", origin="inbody", filename="b.pdf")])
    got = resolve_pdf_date(
        _evidence(node=paper, file=paper.files[0]),
        _book_pdf(copyright_line="", creation="D:20240101000000+00'00'"))

    assert got.start_value == "2016-01-01T00:00:00+00:00"
    assert got.start_precision == "year", "inherited, not flattened to a day"
    assert got.overridden is False


def test_a_year_only_verdict_matching_the_pages_year_keeps_the_page_date(monkeypatch):
    """Replacing a stated day with the same year at year precision makes the
    record vaguer without making it truer, so the page date stands. The same
    guard `copyright_override` applies. A verdict naming a *different* year is
    the case worth acting on, and the test below is that."""
    page_year = NODE_DATE[:4]
    verdict = DateInterpretation(
        candidate_start_date=f"{page_year}-01-01", date_type="publication",
        publication_statement=f"First published in {page_year} by TERI",
        confidence=0.95, recommended_action="override")
    _route_to_llm(monkeypatch, f"First published in {page_year} by TERI", verdict)

    got = resolve_pdf_date(_routed_evidence(), content=b"%PDF-")

    assert got.overridden is False
    assert got.start_value == NODE_DATE, "the page's day is kept"
    assert got.start_precision == "day"
    assert got.decision.action == "keep_page_date"


def test_a_year_only_verdict_naming_another_year_moves_the_date(monkeypatch):
    verdict = DateInterpretation(
        candidate_start_date="2015-01-01", date_type="publication",
        publication_statement="First published in 2015 by TERI",
        confidence=0.95, recommended_action="override")
    _route_to_llm(monkeypatch, "A report. First published in 2015 by TERI.", verdict)

    got = resolve_pdf_date(_routed_evidence(), content=b"%PDF-")

    assert got.overridden is True
    assert got.start_value == "2015-01-01"
    assert got.start_precision == "year", "a year, not a January day"
    assert got.end_value is None


def test_a_read_survives_the_re_decide_in_the_evidence_tiers(monkeypatch):
    """`decide` builds a fresh decision when it is re-run after the file is read,
    so without carrying the tiers forward a document the interpreter was shown
    reports as never opened. Measured on 7 of 49 replayed multi-PDF links."""
    verdict = DateInterpretation(
        candidate_start_date=None, date_type="unknown",
        publication_statement=None, confidence=0.4,
        recommended_action="keep_page_date")
    verdict.set_grounded(False, False)
    _fill_signals(monkeypatch, pdf_created="2024-01-01T00:00:00+00:00",
                  front_text="A document with no copyright line")
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: verdict)

    node = _node(created="2020-01-10T00:00:00+00:00",
                 files=[_file(created="2024-06-01T00:00:00+00:00"),
                        _file(uuid="f2", created="2024-06-01T00:00:00+00:00")])
    got = resolve_pdf_date(
        _evidence(node=node, file=_file(created="2024-06-01T00:00:00+00:00")),
        content=b"%PDF-")

    assert "pdf_text" in got.used, "the read must not vanish from the tiers"
    assert "llm" in got.used
    assert got.overridden is False


def test_a_late_upload_on_a_stated_page_may_now_reach_the_interpreter(monkeypatch):
    """A consequence worth pinning: a file that arrived long after a multi-PDF
    page is routed as it always was, and Case 0 no longer intercepts it. The
    model answering nothing must still leave the page's date in place."""
    asked = {"n": 0}

    def _none(_evidence):
        asked["n"] += 1
        return None

    monkeypatch.setattr("app.ingestion.date_llm.interpret", _none)
    node = _node(bundle="news", metadata={"field_news_date": "2015-08-26T18:30:00+00:00"},
                 files=[_file(created="2024-06-01T00:00:00+00:00"),
                        _file(uuid="f2", created="2024-06-01T00:00:00+00:00")])
    got = resolve_pdf_date(
        _evidence(node=node, file=_file(created="2024-06-01T00:00:00+00:00")),
        content=b"not a pdf")

    assert asked["n"] == 1, "routed, where Case 0 used to answer"
    assert got.start_value == "2015-08-27T00:00:00+00:00"
    assert got.overridden is False
    assert got.decision.rule == "llm_unavailable"


# --------------------------------------------------------------------------- #
# The one deterministic override: a corroborated copyright statement
# --------------------------------------------------------------------------- #
#
# The audited case: two 240-page books linked in the body of the "TERI Alumni
# Association" page (created 2025-09-30, bundle `page`, three in-body PDFs, no
# file entities). Page 4 of each reads "Ⓒ TERI Alumni Association 2020" / "2022"
# and their DocInfo creation dates are 2020-09-21 and 2022-03-02. Both carried
# 2025-09-30 because the deterministic pass ended `multi_pdf_no_evidence`
# without opening the file.

BOOK_COPYRIGHT = "Ⓒ \ue064\ue055\ue062\ue059 Alumni Association 2020"


def _shelf_page_evidence():
    """An in-body PDF on a created-dated page holding three files: the shape
    `decide` settles as `multi_pdf_no_evidence`."""
    node = _node(created="2025-09-30T04:28:20+00:00",
                 files=[_file(), _file(uuid="f2"), _file(uuid="f3")])
    file = _file(origin="inbody", url="https://teriin.org/sites/default/files/files/Book.pdf",
                 filename="Book-on-Dr-RK-Pachauri.pdf", description="TERI Bookstore")
    return _evidence(node=node, file=file)


def _fill_signals(monkeypatch, *, pdf_created, front_text):
    def _fill(evidence, _content):
        evidence.pdf_created = pdf_created
        evidence.front_text = front_text
        evidence.head_text = "Dr R K Pachauri The Visionary Institution Builder"

    monkeypatch.setattr(date_resolution, "_read_pdf_signals", _fill)

    def _no_model(_evidence):
        raise AssertionError("the model must not be called on this path")

    monkeypatch.setattr("app.ingestion.date_llm.interpret", _no_model)


def test_the_shelf_page_shape_is_the_no_evidence_case():
    from app.ingestion.date_rules import decide

    assert decide(_shelf_page_evidence()).rule == "multi_pdf_no_evidence"


def test_a_copyright_year_corroborated_by_docinfo_overrides_at_year_precision(monkeypatch):
    _fill_signals(monkeypatch, pdf_created="2020-09-21T17:51:50+00:00", front_text=BOOK_COPYRIGHT)
    got = resolve_pdf_date(_shelf_page_evidence(), content=b"%PDF-")

    assert got.overridden is True
    assert got.start_value == "2020-01-01T00:00:00+00:00"
    assert got.start_precision == "year"
    assert got.end_value is None
    assert got.decision.rule == "copyright_statement_corroborated"
    assert got.decision.decided_by == "deterministic"
    assert "2020" in got.decision.evidence and "Alumni Association" in got.decision.evidence
    assert "llm" not in got.used and "pdf_text" in got.used


def test_a_copyright_year_the_docinfo_does_not_corroborate_leaves_it_undated(monkeypatch):
    _fill_signals(monkeypatch, pdf_created="2024-03-02T10:42:52+00:00", front_text=BOOK_COPYRIGHT)
    got = resolve_pdf_date(_shelf_page_evidence(), content=b"%PDF-")

    assert got.overridden is False
    assert got.start_value is None, "an uncorroborated year is not a date, and "                                    "neither is the shelf's creation stamp"
    assert got.decision.rule == "multi_pdf_no_evidence"
    # The reason has to be on the persisted field: `evidence` is the column an
    # auditor reads, and `supporting_evidence` is not stored.
    assert "read for a date" in got.decision.evidence
    assert "undated" in got.decision.evidence
    assert "llm" not in got.used


def test_a_docinfo_date_alone_still_never_sets_a_date(monkeypatch):
    """The audited newsletter: DocInfo 2026-09-01, no copyright statement. A PDF
    creation timestamp has never been allowed to date a document and still is
    not — the file is left undated rather than dated from its own metadata."""
    _fill_signals(monkeypatch, pdf_created="2026-09-01T06:30:49+00:00",
                  front_text="TERI ALUMNI ASSOCIATION INAUGURAL ISSUE, SEPTEMBER 2026 CONTENTS Editorial")
    got = resolve_pdf_date(_shelf_page_evidence(), content=b"%PDF-")

    assert got.overridden is False
    assert got.start_value is None
    assert got.decision.rule == "multi_pdf_no_evidence"
    # Reading the file must not have widened the routing to the model.
    assert got.decision.action == "drop_page_date" and "llm" not in got.used


def test_a_copyright_year_equal_to_the_pages_year_proposes_nothing(monkeypatch):
    """The rule declines, because agreeing with the page buys nothing. On a shelf
    page that leaves the file undated — declining to override is not the same as
    endorsing the stamp."""
    _fill_signals(monkeypatch, pdf_created="2025-02-01T00:00:00+00:00",
                  front_text="© TERI Alumni Association 2025")
    got = resolve_pdf_date(_shelf_page_evidence(), content=b"%PDF-")
    assert got.overridden is False and got.start_value is None


def test_an_implausible_copyright_year_is_refused(monkeypatch):
    _fill_signals(monkeypatch, pdf_created="1985-01-01T00:00:00+00:00",
                  front_text="© Somebody 1985")
    got = resolve_pdf_date(_shelf_page_evidence(), content=b"%PDF-")
    assert got.overridden is False


def test_the_copyright_rule_needs_the_bytes(monkeypatch):
    """No content, nothing to read: the decision is exactly what it was."""
    _fill_signals(monkeypatch, pdf_created="2020-09-21T17:51:50+00:00", front_text=BOOK_COPYRIGHT)
    got = resolve_pdf_date(_shelf_page_evidence(), content=None)
    assert got.overridden is False and got.decision.rule == "multi_pdf_no_evidence"
    assert got.used == ["drupal"]


def test_a_single_pdf_is_never_read(monkeypatch):
    """A page holding one PDF settles before the file is opened, whether its date
    comes from the bundle's configured field or from its creation stamp. This is
    the single-PDF guarantee, and the multi-PDF change must not touch it."""
    def _boom(*_a, **_k):
        raise AssertionError("the PDF must not be read for a single-PDF page")

    monkeypatch.setattr(date_resolution, "_read_pdf_signals", _boom)

    created_dated = resolve_pdf_date(_evidence(), content=b"%PDF-")
    assert created_dated.decision.rule == "single_pdf_page"
    assert created_dated.start_value == NODE_DATE

    stated = _node(bundle="news", metadata={"field_news_date": "2024-05-01T00:00:00+00:00"},
                   files=[_file()])
    field_dated = resolve_pdf_date(_evidence(node=stated, file=_file()), content=b"%PDF-")
    assert field_dated.decision.rule == "parent_bundle_date_field"
    assert field_dated.start_value == "2024-05-01T00:00:00+00:00"
    assert field_dated.decision.confidence == 1.0


def _book_pdf(*, copyright_line: str, creation: str) -> bytes:
    """A four-page PDF shaped like the audited books: title page, blank,
    title/editors, copyright page — with a DocInfo creation date."""
    import fitz

    doc = fitz.open()
    for text in ("Dr R K Pachauri The Visionary Institution Builder", "",
                 "Dr R K Pachauri\nThe Visionary Institution Builder\nEditors", copyright_line):
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text, fontsize=11)
    doc.set_metadata({"creationDate": creation, "modDate": creation})
    return doc.tobytes()


def test_the_audited_book_shape_overrides_from_real_bytes(monkeypatch):
    """End to end through the real PyMuPDF readers: page 4 copyright line plus a
    matching DocInfo year moves a shelf-page book from the page's 2025 to its
    own 2020, at year precision, without a model call."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret",
                        lambda _e: (_ for _ in ()).throw(AssertionError("no model call")))
    content = _book_pdf(copyright_line="© TERI Alumni Association 2020",
                        creation="D:20200921175150+00'00'")
    got = resolve_pdf_date(_shelf_page_evidence(), content=content)
    assert got.overridden is True
    assert got.start_value == "2020-01-01T00:00:00+00:00" and got.start_precision == "year"

    # Same bytes with a DocInfo year that disagrees: nothing is corroborated, so
    # nothing is proposed — and a shelf stamp is not a fallback.
    content = _book_pdf(copyright_line="© TERI Alumni Association 2020",
                        creation="D:20240302104252+00'00'")
    got = resolve_pdf_date(_shelf_page_evidence(), content=content)
    assert got.overridden is False and got.start_value is None


# --------------------------------------------------------------------------- #
# The production path: build_attachment_doc must carry the decision through
# --------------------------------------------------------------------------- #

class _FakePage:
    page_number = 1
    text = "PDF body text."


class _FakePdfResult:
    source = "a.pdf"
    pages = [_FakePage()]


def _build_doc(monkeypatch, *, node, file, resolved):
    """Run build_attachment_doc with the resolver stubbed and no I/O."""
    from app.ingestion.extractors import attachment, pdf_extractor

    monkeypatch.setattr(attachment, "fetch_attachment",
                        lambda s, url, t: (b"%PDF-", url))
    monkeypatch.setattr(pdf_extractor, "extract_pdf",
                        lambda content, name: _FakePdfResult())
    recorded: list = []
    monkeypatch.setattr("app.catalog.date_decisions.ensure_table", lambda: None)
    monkeypatch.setattr("app.catalog.date_decisions.record", recorded.append)
    monkeypatch.setattr(attachment, "_resolve_date",
                        lambda record, n, f, content, parent: resolved)
    record = SimpleNamespace(document_id="f1", source_type="pdf_attachment",
                             payload=(node, file), fingerprint="fp")
    return attachment.build_attachment_doc(record, session=None), recorded


def test_an_override_reaches_the_document(monkeypatch):
    from app.ingestion.date_resolution import ResolvedDate
    from app.ingestion.date_rules import DateDecision

    resolved = ResolvedDate(
        start_value="2025-03-31",
        decision=DateDecision(document_id="f1", action="propose_override",
                              candidate_start_date="2025-03-31", date_type="publication",
                              source="llm_publication", confidence=0.95,
                              rule="llm_interpreted", decided_by="llm"),
    )
    node = _node(metadata={}, refs=[], files=[_file()])
    doc, recorded = _build_doc(monkeypatch, node=node, file=_file(), resolved=resolved)
    assert doc.effective_start_date == "2025-03-31"
    assert len(recorded) == 1 and recorded[0].action == "propose_override"


def test_a_title_override_reaches_the_document_with_its_own_provenance(monkeypatch):
    from app.ingestion.date_resolution import ResolvedDate
    from app.ingestion.date_rules import DateDecision

    resolved = ResolvedDate(
        start_value="2024-01-01T00:00:00+00:00", start_precision="year",
        decision=DateDecision(
            document_id="f1", action="propose_override",
            candidate_start_date="2024-01-01T00:00:00+00:00",
            candidate_precision="year", date_type="publication",
            source="document_title", confidence=0.95, rule="title_states_date",
            title_kind="edition", decided_by="deterministic"),
    )
    node = _node(metadata={}, refs=[], files=[_file(), _file(uuid="f2")])
    doc, recorded = _build_doc(monkeypatch, node=node,
                               file=_file(filename="TERI-Annual-Report-2024-25.pdf"),
                               resolved=resolved)
    assert doc.effective_start_date == "2024-01-01T00:00:00+00:00"
    assert doc.start_precision == "year"
    assert doc.date_source == "document_title"
    assert doc.effective_end_date is None
    # The parent's resolution is still on the record: what an override displaced
    # is the interesting fact about it.
    assert doc.date_evidence.rule == "title_states_date"
    assert len(recorded) == 1


def test_a_dropped_date_reaches_the_document_as_no_date(monkeypatch):
    """The undated outcome has to survive the document builder intact — a
    leftover precision or an inherited end would each be a claim the resolver
    explicitly declined to make."""
    from app.ingestion.date_resolution import ResolvedDate
    from app.ingestion.date_rules import DateDecision

    resolved = ResolvedDate(
        start_value=None, start_precision=None, end_value=None, end_precision=None,
        decision=DateDecision(
            document_id="f1", action="drop_page_date", candidate_start_date=None,
            source="node_effective_date", confidence=0.0,
            rule="multi_pdf_no_evidence", decided_by="deterministic",
            evidence="One of 8 PDFs ... left undated."),
    )
    node = _node(metadata={}, refs=[], files=[_file(), _file(uuid="f2")])
    doc, recorded = _build_doc(monkeypatch, node=node, file=_file(), resolved=resolved)
    assert doc.effective_start_date is None
    assert doc.start_precision is None
    assert doc.effective_end_date is None
    assert doc.end_precision is None
    assert doc.date_source == "no_evidence", "not NULL: 'we looked' is a finding"
    assert doc.date_evidence.start_value is None
    assert len(recorded) == 1 and recorded[0].action == "drop_page_date"


def test_a_year_precision_override_reaches_the_document_as_a_year(monkeypatch):
    """The copyright rule's override must not be flattened to a day on the way
    to the document: `start_precision` travels with the value, so the payload
    marks 1 January as a year and nothing renders a January publication."""
    from app.ingestion.date_resolution import ResolvedDate
    from app.ingestion.date_rules import DateDecision

    resolved = ResolvedDate(
        start_value="2020-01-01T00:00:00+00:00", start_precision="year",
        decision=DateDecision(document_id="f1", action="propose_override",
                              candidate_start_date="2020-01-01T00:00:00+00:00",
                              candidate_precision="year", date_type="publication",
                              source="document_copyright", confidence=0.9,
                              rule="copyright_statement_corroborated",
                              decided_by="deterministic"),
    )
    node = _node(metadata={}, refs=[], files=[_file(), _file(uuid="f2")])
    doc, recorded = _build_doc(monkeypatch, node=node, file=_file(origin="inbody"), resolved=resolved)
    assert doc.effective_start_date == "2020-01-01T00:00:00+00:00"
    assert doc.start_precision == "year"
    assert doc.date_source == "document_copyright", (
        "a copyright year is its own provenance, not a quoted publication statement"
    )
    assert doc.effective_end_date is None
    assert doc.date_evidence.start_precision == "year"
    assert recorded[0].rule == "copyright_statement_corroborated"
    assert recorded[0].decided_by == "deterministic"


def test_an_edition_label_lands_in_extra_without_moving_the_date(monkeypatch):
    from app.ingestion.date_resolution import ResolvedDate
    from app.ingestion.date_rules import DateDecision

    resolved = ResolvedDate(
        start_value=NODE_DATE, edition_label="2024-2025",
        decision=DateDecision(document_id="f1", action="keep_page_date",
                              candidate_start_date=NODE_DATE, date_type="edition",
                              edition_label="2024-2025", rule="llm_interpreted",
                              decided_by="llm"),
    )
    node = _node(metadata={}, refs=[], files=[_file()])
    doc, _ = _build_doc(monkeypatch, node=node, file=_file(), resolved=resolved)
    assert doc.effective_start_date == NODE_DATE
    assert doc.extra["edition_label"] == "2024-2025"
    assert doc.extra["bundle"] == "page"


def test_no_edition_label_means_no_key_in_extra(monkeypatch):
    from app.ingestion.date_resolution import ResolvedDate

    resolved = ResolvedDate(start_value=NODE_DATE)
    node = _node(metadata={}, refs=[], files=[_file()])
    doc, _ = _build_doc(monkeypatch, node=node, file=_file(), resolved=resolved)
    assert "edition_label" not in doc.extra


def test_the_feature_flag_falls_back_to_the_node_date(monkeypatch):
    """With resolution off, a PDF inherits its node's date, as it did before."""
    from app.config import get_settings
    from app.ingestion.extractors import attachment

    settings = get_settings()
    monkeypatch.setattr(settings, "date_resolution_enabled", False)
    monkeypatch.setattr("app.ingestion.date_resolution.resolve",
                        lambda *_a, **_k: pytest.fail("resolver must not run"))
    node = _node(files=[_file()])
    got = attachment._resolve_date(
        SimpleNamespace(document_id="f1"), node, _file(), b"%PDF-",
        attachment.resolve_parent_date(node))
    assert got.start_value == NODE_DATE
    assert got.decision is None
