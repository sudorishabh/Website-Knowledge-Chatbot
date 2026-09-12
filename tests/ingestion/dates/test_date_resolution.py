"""Unit tests for evidence-based PDF date resolution (shadow prototype).

The contract these pin down, after manual review:

- **No deterministic rule may propose a date change.** ``decide`` returns
  ``keep_page_date`` or ``needs_llm`` only.
- **An upload date is not a publication date** — not from Drupal's
  ``file.created``, not from a ``/files/YYYY-MM/`` path, not from a PDF
  ``CreationDate``, not from a filename year.
- **Only a quoted, high-confidence publication statement can override.**

Fixtures are real corpus rows.
"""

from __future__ import annotations

import pytest

from app.ingestion.date_evidence import (
    PageContext,
    PdfEvidence,
    edition_label,
    path_month,
    years_in,
)
from app.ingestion.date_llm import (
    DateInterpretation,
    date_is_in_text,
    statement_is_in_text,
)
from app.ingestion.date_rules import decide, in_migration_cohort


def _ev(**kwargs) -> PdfEvidence:
    page = PageContext(
        node_uuid="n1",
        node_title=kwargs.pop("node_title", ""),
        node_created=kwargs.pop("node_created", "2020-01-01T00:00:00+00:00"),
        bundle=kwargs.pop("bundle", "page"),
        pdf_count=kwargs.pop("pdf_count", 1),
    )
    kwargs.setdefault("document_id", "d1")
    kwargs.setdefault("origin", "attachment")
    return PdfEvidence(page=page, **kwargs)


def _verdict(**kwargs) -> DateInterpretation:
    kwargs.setdefault("confidence", 0.95)
    kwargs.setdefault("recommended_action", "override")
    return DateInterpretation(**kwargs)


# --------------------------------------------------------------------------- #
# The overarching guarantee
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "evidence",
    [
        _ev(pdf_count=1, file_created="2024-01-01T00:00:00+00:00"),
        _ev(pdf_count=8, file_created="2024-01-01T00:00:00+00:00"),
        _ev(pdf_count=8, origin="inbody", file_created=None,
            url="https://teriin.org/sites/default/files/2025-04/x.pdf"),
        _ev(pdf_count=8, pdf_created="2012-01-01T00:00:00+00:00"),
        _ev(pdf_count=1, filename="report-2014.pdf"),
    ],
)
def test_no_deterministic_rule_can_propose_a_date_change(evidence):
    assert decide(evidence).action in ("keep_page_date", "drop_page_date",
                                       "needs_llm", "needs_manual_review")


# --------------------------------------------------------------------------- #
# The copyright statement finder (deterministic, text only)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text, expected",
    [
        # PyMuPDF's rendering of the audited books' page 4: circled-C symbol and
        # the rights holder in a private-use font.
        ("Ⓒ \ue064\ue055\ue062\ue059 Alumni Association 2020 \ue064 Alumni Association",
         ("Ⓒ \ue064\ue055\ue062\ue059 Alumni Association 2020", 2020)),
        ("© TERI, 2023. All rights reserved.", ("© TERI, 2023", 2023)),
        ("(c) 2019 The Energy and Resources Institute", ("(c) 2019", 2019)),
        ("Some title © Copyright 2026 Discussion Paper", ("© Copyright 2026", 2026)),
    ],
)
def test_a_copyright_statement_is_found_with_its_year(text, expected):
    from app.ingestion.date_evidence import copyright_statement

    assert copyright_statement(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Suggested citation: TERI 2023. Report title.",       # a citation, not a copyright
        "Dr R K Pachauri: 20 August 1940 – 13 February 2020",  # a biography timeline
        "Copyright reserved by the publisher",                 # no year within reach
    ],
)
def test_no_copyright_statement_is_invented(text):
    from app.ingestion.date_evidence import copyright_statement

    assert copyright_statement(text) is None


# --------------------------------------------------------------------------- #
# Required case 1 — single PDF, same upload date
# --------------------------------------------------------------------------- #

def test_single_pdf_uploaded_with_its_page_keeps_the_page_date():
    got = decide(_ev(pdf_count=1,
                     node_created="2018-10-17T00:00:00+00:00",
                     file_created="2018-10-17T00:00:00+00:00",
                     pdf_created="2018-10-17T00:00:00+00:00"))
    assert got.action == "keep_page_date"
    assert got.rule == "single_pdf_page"


# --------------------------------------------------------------------------- #
# Required case 2 — single PDF, late upload
# --------------------------------------------------------------------------- #

def test_a_late_upload_on_a_single_pdf_page_is_reviewed_not_overridden():
    """Previously this auto-overrode to the upload date. It must not."""
    got = decide(_ev(pdf_count=1,
                     node_created="2019-05-09T00:00:00+00:00",
                     file_created="2020-11-09T00:00:00+00:00"))
    assert got.action == "needs_llm"
    assert got.rule == "single_pdf_late_upload_review"
    assert got.candidate_start_date == "2019-05-09T00:00:00+00:00"  # page date retained
    assert "550 days" in got.supporting_evidence


# --------------------------------------------------------------------------- #
# Required case 3 — several PDFs uploaded together
# --------------------------------------------------------------------------- #

def test_pdfs_uploaded_alongside_their_multi_pdf_page_are_not_dated_from_it():
    """Arriving with the page still means the page has nothing to say about
    *which* of its fifteen files this is. The rule is unchanged — the upload gap
    is small, so nothing is routed — but the outcome on a page dated only by its
    creation stamp is no date rather than a shared one."""
    got = decide(_ev(pdf_count=15,
                     node_created="2018-09-27T00:00:00+00:00",
                     file_created="2018-10-23T00:00:00+00:00"))
    assert got.action == "drop_page_date"
    assert got.rule == "multi_pdf_uploaded_with_page"
    assert got.candidate_start_date is None


# --------------------------------------------------------------------------- #
# Required case 4 — several PDFs uploaded at different times
# --------------------------------------------------------------------------- #

def test_a_late_upload_on_a_multi_pdf_page_is_reviewed_not_overridden():
    """Ceramic_Report.pdf: 6 PDFs, this one uploaded 3.5 years after the page.

    Upload timing routes it for a look; it cannot set the date by itself.
    """
    got = decide(_ev(pdf_count=6,
                     node_created="2018-02-05T00:00:00+00:00",
                     file_created="2021-08-11T00:00:00+00:00",
                     filename="Ceramic_Report .pdf"))
    assert got.action == "needs_llm"
    assert got.rule == "multi_pdf_late_upload_review"
    assert got.candidate_start_date == "2018-02-05T00:00:00+00:00"


def test_several_pdfs_alone_do_not_give_each_one_its_own_date():
    """Twenty files sharing a page is not evidence that any of them has a date;
    it is evidence that the page's date is not any of theirs."""
    got = decide(_ev(pdf_count=20, file_created="2020-01-10T00:00:00+00:00",
                     node_created="2020-01-01T00:00:00+00:00"))
    assert got.action == "drop_page_date"
    assert got.candidate_start_date is None


# --------------------------------------------------------------------------- #
# Required case 5 — PDF CreationDate only
# --------------------------------------------------------------------------- #

def test_a_pdf_creation_date_alone_never_moves_the_page_date():
    """electricity-pricing.pdf: node=file=2019-03-18, DocInfo 2017-11-21."""
    got = decide(_ev(pdf_count=1,
                     node_created="2019-03-18T00:00:00+00:00",
                     file_created="2019-03-18T00:00:00+00:00",
                     pdf_created="2017-11-21T00:00:00+00:00",
                     filename="electricity-pricing.pdf"))
    assert got.action == "keep_page_date"


def test_an_authoring_verdict_cannot_become_an_override():
    assert _verdict(candidate_start_date="2019-08-01", date_type="authoring",
                    publication_statement="created 2019").safe_action() == "keep_page_date"


# --------------------------------------------------------------------------- #
# Required case 6 — filename containing a year
# --------------------------------------------------------------------------- #

def test_a_year_in_the_filename_never_moves_the_page_date():
    got = decide(_ev(pdf_count=1, filename="2014BL18-es-women-empow.pdf",
                     node_created="2019-05-09T00:00:00+00:00",
                     file_created="2019-05-20T00:00:00+00:00"))
    assert got.action == "keep_page_date"


# --------------------------------------------------------------------------- #
# Required case 7 — annual report / reporting period
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Annual Report 2024-2025", "2024-25"),
        ("TERI_Annual_Report_2022_23.pdf", "2022-23"),
        ("Annual-Report-20-21.pdf", "2020-21"),
        ("TAR_2015-16.pdf", "2015-16"),
    ],
)
def test_reporting_periods_are_read_as_editions(text, expected):
    assert edition_label(text) == expected


@pytest.mark.parametrize("text", ["Report 2019-2024", "Session 2 - 3", "x.pdf", "", None])
def test_non_editions_are_not_invented(text):
    assert edition_label(text) is None


def test_the_annual_report_edition_survives_without_becoming_a_date():
    got = decide(_ev(origin="inbody", pdf_count=10, file_created=None,
                     node_created="2022-02-09T06:59:06+00:00",
                     filename="TERI_Annual_Report_upload.pdf",
                     anchor="Annual Report 2021-2022",
                     pdf_created="2022-12-09T00:00:00+00:00"))
    assert got.action == "needs_llm"
    assert got.edition_label == "2021-22"
    assert got.candidate_start_date == "2022-02-09T06:59:06+00:00"


def test_an_edition_verdict_never_becomes_an_override():
    verdict = _verdict(candidate_start_date="2024-01-01", date_type="edition",
                       edition_label="2024-2025",
                       publication_statement="Annual Report 2024-2025")
    assert verdict.safe_action() == "keep_page_date"


# --------------------------------------------------------------------------- #
# Required case 8 — explicit publication date in the PDF text
# --------------------------------------------------------------------------- #

def test_a_quoted_publication_statement_overrides():
    verdict = _verdict(candidate_start_date="2024-09-12", date_type="publication",
                       publication_statement="Published on 12 September 2024",
                       confidence=0.96)
    assert verdict.safe_action() == "override"


def test_a_publication_claim_without_a_quote_is_downgraded_to_review():
    """The guard against a confident paraphrase becoming a date."""
    verdict = _verdict(candidate_start_date="2024-09-12", date_type="publication",
                       publication_statement=None, confidence=0.99)
    assert verdict.safe_action() == "review"


def test_a_fragment_does_not_count_as_a_quotation():
    verdict = _verdict(candidate_start_date="2024-09-12", date_type="publication",
                       publication_statement="2024", confidence=0.99)
    assert verdict.safe_action() == "review"


# --------------------------------------------------------------------------- #
# The quote must carry the date it is used to justify
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "statement",
    [
        "PUBLISHED BY The Energy and Resources Institute (TERI)",
        "Published by The Energy and Resources Institute",
        "PUBLISHED BY AUTHORITY",
    ],
)
def test_a_publisher_imprint_without_a_date_cannot_override(statement):
    """The v3 regression: "Published by ..." reads as publication evidence while
    saying nothing about when, and the model paired it with the CreationDate."""
    verdict = _verdict(candidate_start_date="2023-01-01", date_type="publication",
                       publication_statement=statement, confidence=0.9)
    assert verdict.statement_supports_date() is False
    assert verdict.safe_action() == "review"


def test_a_quote_naming_a_different_year_cannot_override():
    verdict = _verdict(candidate_start_date="2014-04-01", date_type="publication",
                       publication_statement="Published in New Delhi, March 2019")
    assert verdict.safe_action() == "review"


def test_a_bare_year_does_not_become_the_first_of_january():
    verdict = _verdict(candidate_start_date="2023-01-01", date_type="publication",
                       publication_statement="© The Energy and Resources Institute, 2023")
    assert verdict.statement_is_year_only() is True
    assert verdict.safe_action() == "review"


@pytest.mark.parametrize(
    "statement, date",
    [
        ("Hindustan Times, Chandigarh, Monday, December 23, 2013", "2013-12-23"),
        ("ISSUE NO. 22 DATED 11-12-2024", "2024-12-11"),
        ("New Delhi, 31 March 2025", "2025-03-31"),
        ("New Delhi, July 9, 2014: The bulletin was presented", "2014-07-09"),
        ("Date of Publication: 15.03.2019", "2019-03-15"),
        ("Published on 12 September 2024", "2024-09-12"),
        ("dt.15.03.2022", "2022-03-15"),
        ("Chandigarh,23.12.13:", "2013-12-23"),  # two-digit year short form
        ("New Delhi, 12 August 2021, Published by Authority", "2021-08-12"),
    ],
)
def test_a_quote_that_carries_the_date_still_overrides(statement, date):
    verdict = _verdict(candidate_start_date=date, date_type="publication",
                       publication_statement=statement, confidence=0.95)
    assert verdict.statement_supports_date() is True
    assert verdict.safe_action() == "override"


# --------------------------------------------------------------------------- #
# The date must be governed by publication, not by an update / cover / citation
# --------------------------------------------------------------------------- #

def test_an_update_year_is_not_the_publication_date():
    """FS08_Informal-sector_updated-Nov-2023.pdf.

    "first been published in September 2020 and is being updated in 2023" —
    the publication verb governs 2020; 2023 is the update. The proposed
    2023-11-11 was the PDF CreationDate wearing the update's year.
    """
    verdict = _verdict(
        candidate_start_date="2023-11-11", date_type="publication",
        publication_statement=(
            "This factsheet has first been published in September 2020 and is "
            "being updated in 2023 as part of the efforts of PREVENT Waste Alliance."
        ),
    )
    assert verdict.publication_linkage_ok() is False
    assert verdict.safe_action() == "review"


def test_a_cover_date_without_publication_wording_is_not_enough():
    """EI_Nashik_August2023.pdf: "January 2023 Final Report" is a cover date."""
    verdict = _verdict(
        candidate_start_date="2023-01-01", date_type="publication",
        publication_statement="January 2023 Final Report Emission Inventory of Nashik District",
    )
    assert verdict.publication_linkage_ok() is False
    assert verdict.safe_action() == "review"


def test_a_suggested_citation_year_is_not_a_publication_date():
    """Report_Needs_Assessment_TERI_Updated.pdf.

    Also the regression test for month detection: "Decision Making" contains
    the substring "dec", which previously made this read as month precision.
    """
    verdict = _verdict(
        candidate_start_date="2023-01-01", date_type="publication",
        publication_statement=(
            "The Energy and Resources Institute (TERI). 2023. Needs Assessment for "
            "Transformative Climate Action Using Participatory Data Driven Decision "
            "Making Platforms- TCAP. New Delhi: TERI"
        ),
    )
    assert verdict.statement_is_year_only() is True
    assert verdict.safe_action() == "review"


def test_month_names_are_matched_on_word_boundaries():
    year_only = _verdict(candidate_start_date="2023-01-01", date_type="publication",
                         publication_statement="Decision Making Platforms, TERI 2023")
    assert year_only.statement_is_year_only() is True
    real_month = _verdict(candidate_start_date="2023-03-15", date_type="publication",
                          publication_statement="Published March 15, 2023")
    assert real_month.statement_is_year_only() is False


def test_a_month_only_quote_cannot_invent_a_day():
    verdict = _verdict(candidate_start_date="2007-09-01", date_type="publication",
                       publication_statement="Colombo, September 2007")
    assert verdict.statement_supports_the_day() is False
    assert verdict.safe_action() == "review"


@pytest.mark.parametrize(
    "statement, date",
    [
        ("Notified on 18.05.2023", "2023-05-18"),
        ("shall come into force with effect from 1 April 2024", "2024-04-01"),
        ("Workshop held on 5 March 2025", "2025-03-05"),
        ("revised on 12 June 2022", "2022-06-12"),
        ("accessed on 3 February 2021", "2021-02-03"),
    ],
)
def test_a_non_publication_cue_beside_the_date_blocks_an_override(statement, date):
    verdict = _verdict(candidate_start_date=date, date_type="publication",
                       publication_statement=statement)
    assert verdict.publication_linkage_ok() is False
    assert verdict.safe_action() == "review"


def test_a_bare_place_and_year_is_not_a_dateline():
    """'TERI, 2023' must not be read as a dateline."""
    verdict = _verdict(candidate_start_date="2023-01-01", date_type="publication",
                       publication_statement="New Delhi: TERI, 2023")
    assert verdict.safe_action() == "review"


# --------------------------------------------------------------------------- #
# The quote has to be grounded in the document, not in the filename
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "date, text, grounded, label",
    [
        ("2013-12-24",
         "CHANDIGARHTRIBUNE CHANDIGARH | TUESDAY | 24 | DECEMBER 2013 5 NEWS",
         True, "real masthead, differently punctuated"),
        ("2013-12-23",
         "Tribune News Service Chandigarh, December 22 Twenty years after 2013",
         False, "document's dateline is the 22nd, not the 23rd"),
        ("2013-12-23", '\x17\x18"\x0c402 garbled', False, "unreadable text layer"),
        ("2024-12-11", "TENDER BULLETIN ISSUE NO. 22 DATED 11-12-2024", True, "numeric"),
        ("2025-03-31", "Press Release New Delhi, 31 March 2025 TERI", True, "dateline"),
        ("2023-11-11", "first published in September 2020 and updated in 2023",
         False, "day absent from the text"),
    ],
)
def test_a_date_must_appear_in_the_document_text(date, text, grounded, label):
    assert date_is_in_text(date, text) is grounded, label


def test_an_ungrounded_override_is_downgraded_to_review():
    """The newspaper-clipping failure: the model tidied the FILENAME into a
    masthead quote for a PDF whose page text is unreadable."""
    verdict = _verdict(
        candidate_start_date="2013-12-23", date_type="publication",
        publication_statement="Hindustan Times, Chandigarh, Monday, December 23, 2013",
    )
    assert verdict.safe_action() == "override"      # passes every textual gate
    verdict.set_grounded(False)                     # but is not in the document
    assert verdict.safe_action() == "review"


# --------------------------------------------------------------------------- #
# Statement grounding: the quoted phrase itself must be in the document
# --------------------------------------------------------------------------- #

def _grounded_verdict(date: str, statement: str, pdf_text: str) -> DateInterpretation:
    """A verdict with both grounding checks applied, as :func:`interpret` does.

    Grounding is checked at the precision the statement establishes, which is
    what production does: demanding a day of a document that states only a year
    refuses the year instead of grounding it.
    """
    from app.ingestion.date_llm import period_is_in_text

    verdict = _verdict(candidate_start_date=date, date_type="publication",
                       publication_statement=statement, confidence=0.95)
    verdict.set_grounded(
        period_is_in_text(date, pdf_text, verdict.supported_precision() or "day"),
        statement_is_in_text(statement, pdf_text),
    )
    return verdict


def test_1_the_pioneer_print_header_cannot_become_a_masthead():
    """The full-corpus false positive.

    The PDF carries a browser print header only. The date is present, so date
    grounding passes — but the masthead the model reported is not in the
    document, and the words came from the filename.
    """
    verdict = _grounded_verdict(
        "2013-12-24", "The Pioneer, Tuesday, December 24, 2013", "12/24/13 The Pioneer")
    assert verdict.evidence_grounded is True        # the date IS in the text
    assert verdict.statement_grounded is False      # the statement is NOT
    assert verdict.safe_action() == "review"


def test_2_a_real_newspaper_masthead_still_overrides():
    verdict = _grounded_verdict(
        "2013-12-24", "CHANDIGARH | TUESDAY | 24 | DECEMBER 2013",
        "CHANDIGARHTRIBUNE CHANDIGARH | TUESDAY | 24 | DECEMBER 2013 5 NEWS")
    assert verdict.statement_grounded is True
    assert verdict.safe_action() == "override"


def test_3_a_real_press_dateline_still_overrides():
    verdict = _grounded_verdict(
        "2025-03-31", "New Delhi, 31 March 2025",
        "Press Release New Delhi, 31 March 2025 TERI announces findings")
    assert verdict.statement_grounded is True
    assert verdict.safe_action() == "override"


def test_4_a_statement_laundered_from_the_filename_is_rejected():
    verdict = _grounded_verdict(
        "2013-12-23", "Hindustan Times, Chandigarh, Monday, December 23, 2013",
        "story text with no date at all")
    assert verdict.evidence_grounded is False
    assert verdict.statement_grounded is False
    assert verdict.safe_action() == "review"


def test_5_a_month_only_statement_cannot_invent_a_day():
    verdict = _grounded_verdict(
        "2007-09-01", "Colombo, September 2007",
        "Marine Litter in the South Asian Seas Region. Colombo, September 2007")
    assert verdict.statement_grounded is True        # the phrase IS present
    assert verdict.safe_action() == "review"         # but the day is invented


def test_6_an_update_year_is_still_rejected_even_when_quoted_verbatim():
    text = "First published in September 2020 and updated in 2023 by the alliance."
    verdict = _grounded_verdict(
        "2023-11-11", "First published in September 2020 and updated in 2023", text)
    assert verdict.statement_grounded is True
    assert verdict.safe_action() == "review"


def test_7_a_citation_year_is_still_rejected_even_when_quoted_verbatim():
    text = "Suggested citation TERI. 2023. Needs Assessment for Climate Action."
    verdict = _grounded_verdict("2023-01-01", "TERI. 2023. Needs Assessment", text)
    assert verdict.statement_grounded is True
    assert verdict.safe_action() == "review"


def test_8_a_notification_is_never_an_automatic_publication_override():
    text = "The Central Government hereby notifies. Notified on 18.05.2023."
    verdict = _grounded_verdict("2023-05-18", "Notified on 18.05.2023", text)
    assert verdict.safe_action() == "review"
    # And when the model labels it correctly, it keeps the page date outright.
    labelled = _verdict(candidate_start_date="2023-05-18", date_type="notification",
                        publication_statement="Notified on 18.05.2023")
    assert labelled.safe_action() == "keep_page_date"


@pytest.mark.parametrize(
    "statement, text, present",
    [
        ("New Delhi, 31 March 2025", "…New  Delhi,\n31 March 2025…", True),
        ("publication date 15.03.2019", "Publi-\ncation Date: 15.03.2019", True),
        ("The Pioneer, Tuesday, December 24", "12/24/13 The Pioneer", False),
        ("Chandigarh Tribune, Monday, December 23, 2013",
         "CHANDIGARH | MONDAY | 23 DECEMBER 2013", False),   # words reordered/added
        ("", "anything", False),
        ("Published on 12 September 2024", "", False),
    ],
)
def test_statement_matching_forgives_layout_but_not_invention(statement, text, present):
    assert statement_is_in_text(statement, text) is present


def test_an_ungrounded_statement_never_becomes_keep_page_date():
    """Requirement 7: a real date that cannot be grounded stays visible."""
    verdict = _grounded_verdict(
        "2013-12-24", "The Pioneer, Tuesday, December 24, 2013", "12/24/13 The Pioneer")
    assert verdict.safe_action() != "keep_page_date"


def test_unreadable_pdf_text_cannot_produce_an_override():
    verdict = _grounded_verdict("2013-12-24", "Some masthead, December 24, 2013", "")
    assert verdict.safe_action() == "review"


def test_grounding_is_not_something_the_model_can_assert():
    """Grounding is a private attribute, so it is absent from the schema the
    model answers and a model-supplied value is simply ignored."""
    assert "evidence_grounded" not in DateInterpretation.model_json_schema()["properties"]
    verdict = DateInterpretation.model_validate(
        {"candidate_start_date": "2024-09-12", "date_type": "publication",
         "publication_statement": "Published on 12 September 2024",
         "confidence": 0.95, "recommended_action": "override",
         "evidence_grounded": False}
    )
    assert verdict.evidence_grounded is True


# --------------------------------------------------------------------------- #
# Required case 9 — newspaper issue date
# --------------------------------------------------------------------------- #

def test_a_newspaper_issue_date_is_a_publication_date():
    verdict = _verdict(
        candidate_start_date="2013-12-23", date_type="publication",
        publication_statement="Hindustan Times, Chandigarh, Monday, December 23, 2013",
        confidence=0.95,
    )
    assert verdict.safe_action() == "override"


# --------------------------------------------------------------------------- #
# Required case 10 — notification / effective dates must NOT publish
# --------------------------------------------------------------------------- #

def test_a_notification_date_is_not_a_publication_date():
    """NEP_2022_32_FINAL_GAZETTE: 'notified on 18.05.2023'. Previously overrode."""
    verdict = _verdict(candidate_start_date="2023-05-18", date_type="notification",
                       publication_statement="notified on 18.05.2023", confidence=0.99)
    assert verdict.safe_action() == "keep_page_date"


def test_an_effective_date_is_not_a_publication_date():
    verdict = _verdict(candidate_start_date="2024-04-01", date_type="effective",
                       publication_statement="with effect from 1 April 2024",
                       confidence=0.99)
    assert verdict.safe_action() == "keep_page_date"


def test_an_event_date_is_not_a_publication_date():
    verdict = _verdict(candidate_start_date="2025-03-05", date_type="event",
                       publication_statement="Agenda, 05.03.2025", confidence=0.99)
    assert verdict.safe_action() == "keep_page_date"


def test_an_upload_verdict_is_not_a_publication_date():
    verdict = _verdict(candidate_start_date="2024-08-22", date_type="upload",
                       publication_statement="uploaded 22 August 2024", confidence=0.99)
    assert verdict.safe_action() == "keep_page_date"


# --------------------------------------------------------------------------- #
# Required case 11 — ambiguous LLM result becomes review
# --------------------------------------------------------------------------- #

def test_an_ambiguous_verdict_becomes_review():
    verdict = DateInterpretation(candidate_start_date="2021-03-01", date_type="publication",
                                 publication_statement="possibly March 2021",
                                 confidence=0.55, recommended_action="review")
    assert verdict.safe_action() == "review"


def test_a_low_confidence_publication_claim_becomes_review():
    verdict = _verdict(candidate_start_date="2024-09-12", date_type="publication",
                       publication_statement="Published September 2024", confidence=0.7)
    assert verdict.safe_action() == "review"


@pytest.mark.parametrize("bad", ["not-a-date", "0000-00-00", "1200-01-01", "2999-01-01"])
def test_unusable_model_dates_are_discarded(bad):
    verdict = _verdict(candidate_start_date=bad, date_type="publication",
                       publication_statement="Published sometime")
    assert verdict.candidate_start_date is None
    assert verdict.safe_action() == "keep_page_date"


def test_confidence_is_bounded():
    with pytest.raises(ValueError):
        DateInterpretation(confidence=1.5)


# --------------------------------------------------------------------------- #
# Supporting signals and migration safety
# --------------------------------------------------------------------------- #

def test_anchor_text_supplies_the_edition_when_the_filename_has_no_year():
    assert _ev(filename="TERI_Annual_Report_upload.pdf",
               anchor="Annual Report 2021-2022").edition == "2021-22"


def test_upload_month_is_read_from_a_managed_path():
    assert path_month("https://teriin.org/sites/default/files/2021-08/x.pdf") == "2021-08"
    assert path_month("https://teriin.org/files/TERI-Annual-Report-2024-25.pdf") is None


def test_years_are_collected_across_fields():
    assert years_in("a2019b", None, "2024-25") == {2019, 2024}


def test_migration_cohort_boundary():
    assert in_migration_cohort("2017-12-19T06:59:00+00:00") is True
    assert in_migration_cohort("2018-05-01T00:00:00+00:00") is True
    assert in_migration_cohort("2018-06-02T00:00:00+00:00") is False
    assert in_migration_cohort(None) is False


def test_a_migration_import_is_never_read_as_a_late_upload():
    got = decide(_ev(pdf_count=1,
                     node_created="2012-06-23T00:00:00+00:00",
                     file_created="2018-05-01T00:00:00+00:00"))
    assert got.action == "keep_page_date"
    assert "migration import" in got.supporting_evidence


def test_a_migration_cohort_file_with_readable_evidence_is_reviewed():
    got = decide(_ev(pdf_count=6,
                     node_created="2018-02-05T00:00:00+00:00",
                     file_created="2018-02-05T00:00:00+00:00",
                     pdf_created="2015-11-23T00:00:00+00:00"))
    assert got.action == "needs_llm"
    assert got.rule == "migration_cohort_review"


def test_an_in_body_pdf_with_a_much_later_upload_path_is_reviewed():
    got = decide(_ev(origin="inbody", pdf_count=34, file_created=None,
                     node_created="2018-03-10T00:00:00+00:00",
                     url="https://teriin.org/sites/default/files/2025-04/x.pdf"))
    assert got.action == "needs_llm"
    assert got.rule == "multi_pdf_url_month_review"


def test_a_multi_pdf_page_with_no_evidence_yields_no_date():
    got = decide(_ev(origin="inbody", pdf_count=70, file_created=None,
                     url="https://teriin.org/sites/default/files/files/x.pdf"))
    assert got.action == "drop_page_date"
    assert got.rule == "multi_pdf_no_evidence"
    assert "undated" in got.evidence


def test_the_evidence_bundle_never_carries_pdf_bytes():
    bundle = _ev(filename="x.pdf", head_text="cover page text").evidence_dict()
    assert bundle["first_page_text"] == "cover page text"
    assert all(not isinstance(v, (bytes, bytearray)) for v in bundle.values())


# --------------------------------------------------------------------------- #
# Precision follows the evidence: a year-only statement gives a year
# --------------------------------------------------------------------------- #
#
# Until this landed, a statement establishing only a year was downgraded to
# review — so the one date most reports actually state about themselves could
# never become a date, and the document kept its *page's* date instead: a
# different year, asserted with false day precision. Recording the year at year
# precision is weaker and truer. Every other gate still applies; the tests at the
# end of this block are the ones that must keep refusing.

from app.ingestion.date_llm import period_is_in_text


@pytest.mark.parametrize(
    "statement, candidate, expected_precision, expected_date",
    [
        ("First published in 2022 by TERI", "2022-01-01", "year", "2022-01-01"),
        ("Published in September 2007", "2007-09-01", "month", "2007-09-01"),
        ("Published on 12 September 2024", "2024-09-12", "day", "2024-09-12"),
    ],
)
def test_the_precision_matches_what_the_statement_establishes(
    statement, candidate, expected_precision, expected_date
):
    verdict = _grounded_verdict(candidate, statement, f"A report. {statement}. New Delhi.")
    assert verdict.supported_precision() == expected_precision
    assert verdict.normalized_start_date() == expected_date
    assert verdict.safe_action() == "override"


def test_a_year_only_statement_no_longer_becomes_review():
    text = "Energy Yearbook. First published in 2022 by TERI. New Delhi."
    verdict = _grounded_verdict("2022-01-01", "First published in 2022 by TERI", text)
    assert verdict.statement_is_year_only() is True
    assert verdict.safe_action() == "override", "the year is evidence, not a near miss"
    assert verdict.supported_precision() == "year"


def test_a_month_or_day_the_statement_cannot_support_is_discarded_not_stored():
    """The model may propose more than its own quote establishes. The surplus is
    dropped rather than kept: '2022' says nothing about June."""
    text = "Energy Yearbook. First published in 2022 by TERI. New Delhi."
    verdict = _grounded_verdict("2022-06-15", "First published in 2022 by TERI", text)
    assert verdict.supported_precision() == "year"
    assert verdict.normalized_start_date() == "2022-01-01"
    assert verdict.safe_action() == "override"

    month = _grounded_verdict("2007-09-22", "Published in September 2007",
                              "Published in September 2007 in Colombo")
    assert month.supported_precision() == "month"
    assert month.normalized_start_date() == "2007-09-01"


@pytest.mark.parametrize(
    "statement, candidate, text, why",
    [
        ("© The Energy and Resources Institute, 2023", "2023-01-01",
         "© The Energy and Resources Institute, 2023",
         "a copyright line carries no publication verb"),
        ("updated in 2023", "2023-01-01", "updated in 2023",
         "an update cue governs the year"),
        ("Suggested citation: TERI 2023.", "2023-01-01", "Suggested citation: TERI 2023.",
         "a citation year is not a publication date"),
        ("First published in 2022 by TERI", "2022-01-01",
         "a document that never names the year", "the year is not in the text"),
        ("First published in 2022 by TERI", "2022-01-01", "unrelated text entirely",
         "the statement is not in the text"),
    ],
)
def test_the_year_only_path_keeps_every_other_safeguard(statement, candidate, text, why):
    verdict = _grounded_verdict(candidate, statement, text)
    assert verdict.safe_action() != "override", why


def test_a_low_confidence_year_is_still_refused():
    text = "First published in 2022 by TERI"
    verdict = _verdict(candidate_start_date="2022-01-01", date_type="publication",
                       publication_statement=text, confidence=0.5)
    verdict.set_grounded(True, True)
    assert verdict.safe_action() == "review"


# --------------------------------------------------------------------------- #
# Grounding is checked at the precision being asserted
# --------------------------------------------------------------------------- #

def test_grounding_at_year_precision_needs_the_year_in_the_text():
    assert period_is_in_text("2022-01-01", "published in 2022 by TERI", "year") is True
    assert period_is_in_text("2022-01-01", "published in 2021 by TERI", "year") is False
    # A four-digit run that is not the year must not ground it.
    assert period_is_in_text("2022-01-01", "item 12022 of the catalogue", "year") is False


def test_grounding_at_month_precision_needs_the_month_and_the_year_together():
    assert period_is_in_text("2007-09-01", "published in September 2007", "month") is True
    # A two-part numeric month ("09/2007") is not read: the numeric-date pattern
    # needs three components. Month grounding relies on the month *name*, which
    # is how documents that state a month actually write it.
    assert period_is_in_text("2007-09-01", "issue 09/2007 of the bulletin", "month") is False
    assert period_is_in_text("2007-09-01", "published 01/09/2007", "month") is True
    assert period_is_in_text("2007-09-01", "September 1998 report", "month") is False
    # Proximity, not parsing: the month name and the year have to sit within one
    # 60-character window, which is the same tolerance `date_is_in_text` uses for
    # a day. It is deliberately loose — a masthead breaks a date across lines —
    # and it is not the only guard: the quoted statement must itself be present
    # and must carry the year.
    assert period_is_in_text(
        "2007-09-01", "September 1998, and separately 2007", "month") is True


def test_grounding_at_day_precision_is_unchanged():
    """The filename-assembled-date guard still demands all three components."""
    assert period_is_in_text("2013-12-23", "Chandigarh, Monday, December 23, 2013", "day") is True
    assert period_is_in_text("2013-12-23", "December 2013", "day") is False
