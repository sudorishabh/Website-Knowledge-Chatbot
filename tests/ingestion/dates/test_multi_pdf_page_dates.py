"""How a PDF sharing its page with others gets a date — and when it gets none.

The reported failure: every file on a shelf page carried the day somebody typed
the node. ``TERI-Annual-Report-2024-25.pdf`` was stored as 2022-02-09, 69 FCRA
financial statements spanning eight years all shared 2018-04-04, and
``newsTRAC-feb21.pdf`` was dated 2018-09-19. Measured across the live corpus, 87
of the 237 multi-PDF attachments whose name states a year carried a date from a
different year.

Two rules answer it, and they are deliberately narrow.

**The file's own name decides — on a multi-PDF page only.** A page holding one
file is that file's page and its date is a claim about the document; the file
inherits it unconditionally and no name is read. A page holding twelve is a
shelf, its date is a claim about the shelf, and the only thing telling one file
on it from another is what it is called.

**Where nothing states a date, there is no date.** Not the shelf's creation
stamp. A date-scoped query that misses a document is recoverable; one that
confidently returns it under the wrong year is not.

The second rule stops where the page's date stops being about the shelf: an
``events`` page states its date in a CMS field, and its agenda, concept note and
slide deck all belong to that day. Only a page dated by its bare Drupal creation
stamp loses it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ingestion.date_resolution import build_evidence, resolve, title_override
from app.ingestion.date_rules import decide, page_date_is_usable

SHELF_DATE = "2022-02-09T00:00:00+00:00"
EVENT_DATE = "2015-08-27T00:00:00+00:00"


def _file(**kwargs):
    return SimpleNamespace(
        uuid=kwargs.pop("uuid", "f1"),
        url=kwargs.pop("url", "https://teriin.org/files/a.pdf"),
        filename=kwargs.pop("filename", "a.pdf"),
        description=kwargs.pop("description", None),
        origin=kwargs.pop("origin", "attachment"),
        created=kwargs.pop("created", None),
        **kwargs,
    )


def _shelf(files=3, **kwargs):
    """An `Annual Reports` / `Announcements` page: `page` bundle, so its date is
    only the day the node was typed."""
    return SimpleNamespace(
        uuid="node-1", title=kwargs.pop("title", "Annual Reports"),
        url="https://teriin.org/annual-reports", created=SHELF_DATE,
        bundle="page", metadata={}, files=[object()] * files, **kwargs,
    )


def _event(files=3, **kwargs):
    """A page whose bundle *states* its date in a configured CMS field."""
    return SimpleNamespace(
        uuid="node-2", title="A launch event", url="https://teriin.org/event",
        created="2015-01-01T00:00:00+00:00", bundle="news",
        metadata={"field_news_date": EVENT_DATE},
        files=[object()] * files, **kwargs,
    )


def _resolve(node, file, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(
            "app.ingestion.date_llm.interpret",
            lambda _e: pytest.fail("no model call is needed for this case"),
        )
    return resolve(build_evidence(document_id="d1", node=node, file=file),
                   content=b"%PDF-")


# --------------------------------------------------------------------------- #
# The reported failure
# --------------------------------------------------------------------------- #

def test_the_annual_report_shelf_dates_each_edition_from_its_own_name(monkeypatch):
    """Ten editions on one page. They are not ten copies of a 2022 document.

    Each is dated to the year its period *ends* in: the 2024-25 report is a 2025
    document, published at the close of the year it covers."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    node = _shelf(files=10)
    got = {
        name: _resolve(node, _file(filename=name)).start_value
        for name in ("TERI-Annual-Report-2024-25.pdf",
                     "TERI-Annual-Report-2019-20.pdf",
                     "TAR_2016-17.pdf")
    }
    assert got == {
        "TERI-Annual-Report-2024-25.pdf": "2025-01-01T00:00:00+00:00",
        "TERI-Annual-Report-2019-20.pdf": "2020-01-01T00:00:00+00:00",
        "TAR_2016-17.pdf": "2017-01-01T00:00:00+00:00",
    }
    assert SHELF_DATE not in got.values()


def test_the_fcra_shelf_stops_sharing_one_stamp(monkeypatch):
    """69 financial statements spanning eight years, all stored as 2018-04-04."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    node = _shelf(files=69, title="FCRA Financials")
    dates = [
        _resolve(node, _file(filename=name)).start_value
        for name in ("Balance_Sheet_22_23.pdf", "Receipts-&-Payments-2024-25.pdf",
                     "Income-and-Expenditure_17-18.pdf")
    ]
    assert dates == ["2023-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00",
                     "2018-01-01T00:00:00+00:00"]


def test_a_newsletter_issue_is_dated_to_its_month(monkeypatch):
    got = _resolve(_shelf(files=12), _file(filename="newsTRAC-feb21.pdf"), monkeypatch)
    assert got.start_value == "2021-02-01T00:00:00+00:00"
    assert got.start_precision == "month", "the name states a month, not a day"
    assert got.canonical_source == "document_title"


def test_the_decision_row_quotes_the_words_that_dated_the_file(monkeypatch):
    """"Why does this PDF have the date 2024?" has to be answerable from the
    stored row, which means the row has to contain the statement itself."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_shelf(files=10),
                   _file(filename="TERI-Annual-Report-2024-25.pdf"))
    assert got.decision.rule == "title_states_date"
    assert got.decision.decided_by == "deterministic"
    assert "'2024-25'" in got.decision.evidence
    assert "filename" in got.decision.evidence
    assert "10 PDFs" in got.decision.evidence
    # The one reading a person could not check from the value alone.
    assert "ends in" in got.decision.evidence


# --------------------------------------------------------------------------- #
# One PDF on a page is that page's document
# --------------------------------------------------------------------------- #

def test_a_lone_pdf_inherits_the_page_date_whatever_its_name_says(monkeypatch):
    """`TEDDY-2015-16-press-release.pdf` really was released on its page's date;
    `2015-16` is the edition TEDDY covers. The rule never looks."""
    got = _resolve(_event(files=1),
                   _file(filename="TEDDY-2015-16-press-release.pdf"), monkeypatch)
    assert got.start_value == EVENT_DATE
    assert got.overridden is False
    assert got.canonical_source == "parent_page"


def test_a_lone_pdf_on_a_shelf_bundle_still_inherits(monkeypatch):
    """The page's date being a creation stamp changes nothing while it is the
    only file: one file on a page is that page's publication."""
    got = _resolve(_shelf(files=1),
                   _file(filename="TERI-Annual-Report-2024-25.pdf"), monkeypatch)
    assert got.start_value == SHELF_DATE
    assert got.overridden is False


def test_the_title_rule_is_not_consulted_for_a_single_pdf_page():
    """Stated structurally as well as behaviourally: the restriction is the
    rule's first line, not an accident of how the caller happens to route."""
    evidence = build_evidence(
        document_id="d1", node=_shelf(files=1),
        file=_file(filename="TERI-Annual-Report-2024-25.pdf"))
    assert title_override(evidence) is None


# --------------------------------------------------------------------------- #
# No name, no date
# --------------------------------------------------------------------------- #

def test_a_shelf_file_that_states_nothing_is_left_undated(monkeypatch):
    got = _resolve(_shelf(files=8), _file(filename="Brochure.pdf"), monkeypatch)
    assert got.start_value is None
    assert got.start_precision is None
    assert got.end_value is None
    assert got.dropped is True
    assert got.canonical_source == "no_evidence"


def test_the_drop_records_why_rather_than_leaving_silence(monkeypatch):
    """"Why has this PDF no date?" and "why did you not just use the page's?"
    are the same question, and the row has to answer both."""
    got = _resolve(_shelf(files=8), _file(filename="Brochure.pdf"), monkeypatch)
    assert got.decision.action == "drop_page_date"
    assert got.decision.candidate_start_date is None
    assert "8 PDFs" in got.decision.evidence
    assert "creation stamp" in got.decision.evidence
    assert "undated" in got.decision.evidence


def test_a_stated_page_date_is_still_inherited_by_every_file_on_it(monkeypatch):
    """The limit of the drop. An event's agenda, concept note and slide deck all
    happened on the day of the event, and the CMS says which day that was."""
    node = _event(files=3)
    dates = {_resolve(node, _file(filename=name), monkeypatch).start_value
             for name in ("Agenda.pdf", "Concept Note.pdf", "Slides.pdf")}
    assert dates == {EVENT_DATE}


@pytest.mark.parametrize("bundle, stated, usable", [
    ("page", False, False),
    ("basic", False, False),
    ("policy_brief", False, False),
    ("events", True, True),
    ("completed_projects", True, True),
    ("research_papers", True, True),
])
def test_only_a_bare_creation_stamp_is_refused_as_a_shelf_date(bundle, stated, usable):
    from app.ingestion.date_evidence import PageContext

    page = PageContext(node_uuid="n", bundle=bundle, pdf_count=5,
                       node_start_date="2020-01-01T00:00:00+00:00",
                       date_source="cms_field" if stated else "created")
    assert page.date_from_bundle_field is stated
    assert page_date_is_usable(page) is usable


def test_a_single_pdf_page_is_always_usable():
    from app.ingestion.date_evidence import PageContext

    page = PageContext(node_uuid="n", bundle="page", pdf_count=1,
                       node_start_date="2020-01-01T00:00:00+00:00")
    assert page_date_is_usable(page) is True


# --------------------------------------------------------------------------- #
# Weighing a name against the document's own text
# --------------------------------------------------------------------------- #

def test_a_bare_year_does_not_displace_a_date_the_cms_states(monkeypatch):
    """`c-2024.pdf` on a research paper whose `field_rpaper_year` says 2016 is
    not a 2024 paper. A four-digit number that happened to sit between two
    delimiters is the weakest reading a name offers."""
    # A bare year does not settle the case, so the file takes the ordinary route
    # and the interpreter is asked. It finds nothing, which is the usual answer.
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    node = SimpleNamespace(
        uuid="n3", title="A paper", url="https://teriin.org/paper",
        created="2016-01-01T00:00:00+00:00", bundle="research_papers",
        metadata={"field_rpaper_year": 2016}, files=[object()] * 3)
    got = _resolve(node, _file(filename="c-2024.pdf"))
    assert got.start_value == "2016-01-01T00:00:00+00:00"
    assert got.overridden is False


def test_a_bare_year_still_dates_a_file_on_a_bare_shelf(monkeypatch):
    """Against a creation stamp shared by eight files, even the weak reading is
    the better one — the alternative is not a better date, it is no date.

    The interpreter is asked first, because a bare year is exactly the case a
    closer reading might improve on. It finds nothing, and the name stands."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_shelf(files=8), _file(filename="IoET_brochure_2026.pdf"))
    assert got.start_value == "2026-01-01T00:00:00+00:00"
    assert got.decision.title_kind == "bare_year"


def test_a_name_that_happens_to_match_the_shelf_stamp_still_fires(monkeypatch):
    """Standing down has to preserve something, or it is just data loss.

    69 FCRA statements share the day their page was typed, 2018-04-04, so one of
    them was always going to be the April-June 2018 one. Treating that as the
    file's name *corroborating* the page — and declining — does not keep
    2018-04-04, because on a shelf the page's date is not the file's to borrow:
    it drops the date entirely and throws away the one thing the file states.
    """
    node = SimpleNamespace(
        uuid="n4", title="FCRA Financials", url="https://teriin.org/fcra",
        created="2018-04-04T00:00:00+00:00", bundle="page", metadata={},
        files=[object()] * 69)
    got = _resolve(node, _file(filename="April18-Jun18_new.pdf"), monkeypatch)
    assert got.start_value == "2018-04-01T00:00:00+00:00"
    assert got.start_precision == "month"
    assert got.canonical_source == "document_title"


def test_a_name_agreeing_with_a_stated_page_date_does_stand_down(monkeypatch):
    """The other side of it. Here declining *does* preserve something — the day
    the CMS states — so the coarser name must not replace it.

    The year in the filename still routes the file to the interpreter, which is
    unchanged behaviour and not what this test is about; it finds nothing."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_event(files=4), _file(filename="Report_August2015.pdf"))
    assert got.start_value == EVENT_DATE, "2015-08-27, the day the CMS states"
    assert got.overridden is False


def test_a_named_period_is_sharpened_by_the_documents_own_text(monkeypatch):
    """The tender bulletin. Its name says December 2024; its first line says the
    11th. The text is the same claim stated more precisely, so it stands."""
    from app.ingestion import date_resolution
    from app.ingestion.date_llm import DateInterpretation

    text = "TENDER BULLETIN ISSUE NO. 22 DATED 11-12-2024 Renewal of contract."
    monkeypatch.setattr(
        date_resolution, "_read_pdf_signals",
        lambda evidence, _c: setattr(evidence, "head_text", text))
    verdict = DateInterpretation(
        candidate_start_date="2024-12-11", date_type="publication",
        publication_statement="ISSUE NO. 22 DATED 11-12-2024",
        confidence=0.95, recommended_action="override")
    verdict.set_grounded(True, True)
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: verdict)

    got = _resolve(_shelf(files=20, title="Announcements"),
                   _file(filename="Tender_No_22_TERI_2024_December.pdf",
                         created="2025-06-01T00:00:00+00:00"))
    assert str(got.start_value)[:10] == "2024-12-11"
    assert got.canonical_source == "document_text"


def test_a_text_verdict_outside_the_named_period_loses_to_the_name(monkeypatch):
    """A disagreement, not a sharpening. The name is what a person wrote about
    this file; the verdict is what a model read out of it."""
    from app.ingestion import date_resolution
    from app.ingestion.date_llm import DateInterpretation

    text = "Published on 1 June 2019 by TERI"
    monkeypatch.setattr(
        date_resolution, "_read_pdf_signals",
        lambda evidence, _c: setattr(evidence, "head_text", text))
    verdict = DateInterpretation(
        candidate_start_date="2019-06-01", date_type="publication",
        publication_statement="Published on 1 June 2019",
        confidence=0.99, recommended_action="override")
    verdict.set_grounded(True, True)
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: verdict)

    got = _resolve(_shelf(files=20),
                   _file(filename="TERI-Annual-Report-2024-25.pdf",
                         created="2025-06-01T00:00:00+00:00"))
    assert got.start_value == "2025-01-01T00:00:00+00:00"
    assert got.canonical_source == "document_title"


# --------------------------------------------------------------------------- #
# The routing is unchanged
# --------------------------------------------------------------------------- #

def test_the_drop_does_not_widen_what_gets_read_or_asked():
    """The new outcome replaces the *value* a fallback produced, not the rule
    that produced it. A file that used to end at `multi_pdf_uploaded_with_page`
    still ends there, and still costs nothing."""
    evidence = build_evidence(
        document_id="d1", node=_shelf(files=15),
        file=_file(created="2022-03-01T00:00:00+00:00"))
    got = decide(evidence)
    assert got.rule == "multi_pdf_uploaded_with_page"
    assert got.action == "drop_page_date"
    assert got.used == ["drupal"]


# --------------------------------------------------------------------------- #
# Which of the file's strings answers, end to end
# --------------------------------------------------------------------------- #

def test_the_filename_outranks_the_drupal_link_label(monkeypatch):
    """A link label describes the page's slot, not the file sitting in it. Ten
    annual reports hang off labels that all read "Annual Report"."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_shelf(files=10),
                   _file(filename="TERI-Annual-Report-2024-25.pdf",
                         description="Annual Report"))
    assert got.start_value == "2025-01-01T00:00:00+00:00"
    assert got.decision.title_source == "filename"


def test_a_stale_link_label_does_not_beat_a_clear_filename(monkeypatch):
    """The sharper case: the label is not merely generic, it names a *different*
    year. A slot labelled "Report 2022" whose file was swapped for the 2024
    edition must follow the file.

    A bare year does not settle the case on its own, so the interpreter is still
    asked; it finds nothing, and the filename's year stands."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_shelf(files=10),
                   _file(filename="Annual_Report_2024.pdf",
                         description="Report 2022"))
    assert got.start_value == "2024-01-01T00:00:00+00:00"
    assert got.decision.title_source == "filename"


def test_the_link_label_still_answers_for_an_unnamed_file(monkeypatch):
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_shelf(files=10),
                   _file(filename="download.pdf",
                         description="Annual Report 2021-2022"))
    assert got.start_value == "2022-01-01T00:00:00+00:00"
    assert got.decision.title_source == "link_text"


# --------------------------------------------------------------------------- #
# Reconciling a named period against the document's own text
# --------------------------------------------------------------------------- #

def _with_text(monkeypatch, text, candidate, statement):
    from app.ingestion import date_resolution
    from app.ingestion.date_llm import DateInterpretation

    monkeypatch.setattr(
        date_resolution, "_read_pdf_signals",
        lambda evidence, _c: setattr(evidence, "head_text", text))
    verdict = DateInterpretation(
        candidate_start_date=candidate, date_type="publication",
        publication_statement=statement, confidence=0.95,
        recommended_action="override")
    verdict.set_grounded(True, True)
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: verdict)


def test_a_body_date_inside_the_named_month_sharpens_it(monkeypatch):
    """December 2024 in the name, 11 December 2024 in the body → the 11th."""
    _with_text(monkeypatch, "TENDER BULLETIN ISSUE NO. 22 DATED 11-12-2024",
               "2024-12-11", "ISSUE NO. 22 DATED 11-12-2024")
    got = _resolve(_shelf(files=20, title="Announcements"),
                   _file(filename="Tender_No_22_TERI_2024_December.pdf",
                         created="2025-06-01T00:00:00+00:00"))
    assert str(got.start_value)[:10] == "2024-12-11"
    assert got.canonical_source == "document_text"
    assert got.decision.title_disposition == "refined"


def test_a_body_date_in_a_different_year_loses_to_the_named_month(monkeypatch):
    """December 2024 in the name, 11 December *2023* in the body. That is a
    disagreement, not a sharpening, so the name stands."""
    _with_text(monkeypatch, "TENDER BULLETIN ISSUE NO. 22 DATED 11-12-2023",
               "2023-12-11", "ISSUE NO. 22 DATED 11-12-2023")
    got = _resolve(_shelf(files=20, title="Announcements"),
                   _file(filename="Tender_No_22_TERI_2024_December.pdf",
                         created="2025-06-01T00:00:00+00:00"))
    assert got.start_value == "2024-12-01T00:00:00+00:00"
    assert got.start_precision == "month"
    assert got.canonical_source == "document_title"
    assert "outside" in got.decision.evidence


# --------------------------------------------------------------------------- #
# An unexpected error must not resurrect the shelf stamp
# --------------------------------------------------------------------------- #

def _explode(monkeypatch):
    from app.ingestion import date_resolution

    def _boom(_evidence):
        raise RuntimeError("boom")

    monkeypatch.setattr(date_resolution, "decide", _boom)


def test_a_resolver_failure_on_a_weak_shelf_leaves_the_file_undated(monkeypatch):
    """The regression that matters most here. "Fail closed" used to mean "keep
    the page date", which on a shelf is the original bug — reintroduced silently
    by a crash in a rule that never ran. A missing date is preferable to a
    confidently wrong one."""
    _explode(monkeypatch)
    got = _resolve(_shelf(files=8), _file(filename="Brochure.pdf"))
    assert got.start_value is None
    assert got.start_precision is None
    assert got.end_value is None
    assert got.dropped is True
    assert got.canonical_source == "no_evidence"
    assert got.decision.rule == "resolver_failed"
    assert "creation stamp" in got.decision.evidence


def test_a_resolver_failure_on_a_stated_page_still_inherits(monkeypatch):
    """The other half. Where the page's date is a claim about the content,
    keeping it on failure is still right — a stale date is recoverable."""
    _explode(monkeypatch)
    got = _resolve(_event(files=4), _file(filename="Agenda.pdf"))
    assert got.start_value == EVENT_DATE
    assert got.dropped is False
    assert got.decision.rule == "resolver_failed"
    assert got.decision.action == "keep_page_date"


def test_a_resolver_failure_on_a_single_pdf_page_still_inherits(monkeypatch):
    """One file on a page is that page's document however badly the run went."""
    _explode(monkeypatch)
    got = _resolve(_shelf(files=1), _file(filename="Brochure.pdf"))
    assert got.start_value == SHELF_DATE


def test_the_failure_path_asks_the_same_question_as_the_success_path():
    """Stated structurally: two predicates would drift, and the drift would be
    invisible until a crash."""
    import inspect

    from app.ingestion import date_resolution

    assert "page_date_is_usable" in inspect.getsource(date_resolution._failed)


# --------------------------------------------------------------------------- #
# An override replaces only what it establishes
# --------------------------------------------------------------------------- #

def _ranged(files=3, **kwargs):
    """A project page covering a period: start 2020, end 2025."""
    return SimpleNamespace(
        uuid="n5", title="A long project", url="https://teriin.org/project",
        created="2019-01-01T00:00:00+00:00", bundle="completed_projects",
        metadata={"field_completed_start_date": "2020-01-01",
                  "field_completed_end_date": "2025-12-31"},
        files=[object()] * files, **kwargs)


def test_an_edition_from_a_name_does_not_destroy_the_inherited_period(monkeypatch):
    """"Progress Report 2022-23" says something about 2022-23 and nothing
    whatever about when the project ends. Clearing the end used to be
    unconditional, which threw away information the file never contradicted."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_ranged(), _file(filename="Progress-Report-2022-23.pdf"))
    assert got.start_value == "2023-01-01T00:00:00+00:00"
    assert got.start_precision == "year"
    assert got.end_value == "2025-12-31T00:00:00+00:00", "the period survives"
    assert got.end_precision == "day"


def test_a_bare_year_on_a_ranged_page_destroys_nothing_at_all(monkeypatch):
    """The weakest name against the strongest parent: both ends survive
    untouched, because a four-digit number in a filename establishes neither."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_ranged(), _file(filename="Annual_Report_2024.pdf"))
    assert got.start_value == "2020-01-01T00:00:00+00:00"
    assert got.end_value == "2025-12-31T00:00:00+00:00"
    assert got.decision.title_disposition == "rejected"


def test_a_point_date_does_clear_the_inherited_period(monkeypatch):
    """The limit. A document issued on one day does not cover its project's five
    years, so there the inherited end goes with the date it belonged to."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_ranged(), _file(filename="Minutes_14-03-2023.pdf"))
    assert got.start_value == "2023-03-14T00:00:00+00:00"
    assert got.start_precision == "day"
    assert got.end_value is None, "a point-dated document covers no period"


def test_an_override_after_the_inherited_end_drops_it_rather_than_inverting(
    monkeypatch,
):
    """A stored range that reads backwards is worse than a missing end:
    `reconcile.date_checks.inverted_date_range` would correctly report it as
    something else having written the column."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_ranged(), _file(filename="Follow-up-Report-2026-27.pdf"))
    assert got.start_value == "2027-01-01T00:00:00+00:00"
    assert got.end_value is None


# --------------------------------------------------------------------------- #
# Provenance: what exact file metadata gave us this date
# --------------------------------------------------------------------------- #

def test_the_decision_names_the_source_kind_and_disposition(monkeypatch):
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_shelf(files=10),
                   _file(filename="TERI-Annual-Report-2024-25.pdf"))
    assert got.decision.title_source == "filename"
    assert got.decision.title_kind == "edition"
    assert got.decision.title_disposition == "replaced"
    assert "'2024-25'" in got.decision.evidence
    assert got.start_value == "2025-01-01T00:00:00+00:00"


def test_a_name_that_merely_agreed_is_still_recorded(monkeypatch):
    """The audit question is "the filename says 2015 — why is this dated
    2015-08-27?", and silence is not an answer."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_event(files=4), _file(filename="Report_August2015.pdf"))
    assert got.start_value == EVENT_DATE
    assert got.decision.title_disposition == "corroborating"
    assert got.decision.title_source == "filename"
    assert "changes nothing" in got.decision.evidence


def test_a_name_that_was_refused_is_still_recorded(monkeypatch):
    """And so is "the filename says 2024 — why is this 2016?"."""
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    node = SimpleNamespace(
        uuid="n3", title="A paper", url="https://teriin.org/paper",
        created="2016-01-01T00:00:00+00:00", bundle="research_papers",
        metadata={"field_rpaper_year": 2016}, files=[object()] * 3)
    got = _resolve(node, _file(filename="c-2024.pdf"))
    assert got.start_value == "2016-01-01T00:00:00+00:00"
    assert got.decision.title_disposition == "rejected"
    assert got.decision.title_kind == "bare_year"
    assert "bare year is not a claim" in got.decision.evidence


def test_the_provenance_reaches_the_decision_row(monkeypatch):
    """The three columns are on the row the catalog writes, not only on the
    in-memory decision."""
    from app.catalog import date_decisions
    from app.ingestion.date_rules import DateDecision

    row = date_decisions.from_decision(
        DateDecision(document_id="d", action="propose_override",
                     candidate_start_date="2024-01-01T00:00:00+00:00",
                     source="document_title", rule="title_states_date",
                     title_source="filename", title_kind="edition",
                     title_disposition="replaced"),
        origin="attachment", bundle="page", node_uuid="n", page_pdf_count=10,
        current_start_date=SHELF_DATE, url=None, filename="x.pdf",
    )
    assert row.title_source == "filename"
    assert row.title_kind == "edition"
    assert row.title_disposition == "replaced"


def test_the_decision_insert_binds_every_column_it_names():
    """One placeholder per column, one value per placeholder. Adding a column
    and forgetting the tuple is a runtime-only failure otherwise, and this write
    fails open — so it would lose the audit trail silently."""
    from contextlib import contextmanager

    from app.catalog import date_decisions

    captured = {}

    class _Cursor:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    @contextmanager
    def _fake():
        yield _Conn()

    original = date_decisions.mysql_connection
    date_decisions.mysql_connection = _fake
    try:
        date_decisions.record(date_decisions.DecisionRow(
            document_id="d", origin="attachment", title_source="filename",
            title_kind="edition", title_disposition="replaced"))
    finally:
        date_decisions.mysql_connection = original

    assert captured["sql"].count("%s") == len(captured["params"])
    assert "title_source" in captured["sql"]
    assert "filename" in captured["params"]


# --------------------------------------------------------------------------- #
# A reporting period names the year it ends in
# --------------------------------------------------------------------------- #

def test_a_multi_pdf_edition_is_dated_to_the_ending_year(monkeypatch):
    monkeypatch.setattr("app.ingestion.date_llm.interpret", lambda _e: None)
    got = _resolve(_shelf(files=10),
                   _file(filename="TERI-Annual-Report-2024-25.pdf"))
    assert got.start_value == "2025-01-01T00:00:00+00:00"
    assert got.start_precision == "year"
    assert got.canonical_source == "document_title"
    assert got.decision.title_kind == "edition"
    assert got.decision.title_source == "filename"
    assert got.decision.title_disposition == "replaced"
    assert "'2024-25'" in got.decision.evidence, "the span is kept verbatim"


def test_a_single_pdf_edition_still_inherits_its_page(monkeypatch):
    """The ending-year change is about how a span is read, not about who may
    read one. A page holding one file is still authoritative for it."""
    got = _resolve(_shelf(files=1),
                   _file(filename="TERI-Annual-Report-2024-25.pdf"), monkeypatch)
    assert got.start_value == SHELF_DATE
    assert got.overridden is False
    assert got.canonical_source == "parent_page"


def test_a_body_date_inside_the_named_year_sharpens_the_edition(monkeypatch):
    """2024-25 resolves to 2025, so March 2025 is inside the period it named."""
    _with_text(monkeypatch, "Released 15 March 2025 by TERI",
               "2025-03-15", "Released 15 March 2025")
    got = _resolve(_shelf(files=20), _file(filename="Report_2024-25.pdf",
                                           created="2025-06-01T00:00:00+00:00"))
    assert str(got.start_value)[:10] == "2025-03-15"
    assert got.canonical_source == "document_text"
    assert got.decision.title_disposition == "refined"


def test_a_body_date_in_the_opening_year_cannot_move_the_edition(monkeypatch):
    """March 2024 is the year the period *opened* in, not the year the name
    resolved to, so it is a contradiction rather than a sharpening."""
    _with_text(monkeypatch, "Released 15 March 2024 by TERI",
               "2024-03-15", "Released 15 March 2024")
    got = _resolve(_shelf(files=20), _file(filename="Report_2024-25.pdf",
                                           created="2025-06-01T00:00:00+00:00"))
    assert got.start_value == "2025-01-01T00:00:00+00:00"
    assert got.start_precision == "year"
    assert got.canonical_source == "document_title"
    assert "outside" in got.decision.evidence
