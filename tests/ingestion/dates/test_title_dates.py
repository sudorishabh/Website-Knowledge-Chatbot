"""The date a file states in its own name — :mod:`app.ingestion.title_dates`.

Every fixture below is a real filename or link text from the live corpus. The
reading is pinned to the day/month/year the string actually establishes, because
the precision is the claim: "2024-25" says a year and inventing 1 January as a
*day* from it would be the same error the resolver exists to prevent.

The rejections matter more than the acceptances. A parser that finds a year in
everything is worse than no parser: it would have moved 137 completed-project
files onto scheme codes that merely start with four digits.
"""

from __future__ import annotations

import pytest

from app.ingestion.title_dates import read_title_date, title_date


def _read(text):
    found = read_title_date(text)
    return None if found is None else (found.normalized_value.isoformat(),
                                       found.precision, found.title_kind)


# --------------------------------------------------------------------------- #
# What a name can establish
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, expected", [
    # A full date, in each spelling the corpus uses.
    ("Summary for Policymakers 09 March 2026.pdf", ("2026-03-09", "day", "full_date")),
    ("1.-MoR-circular-date-15.03.2022.pdf", ("2022-03-15", "day", "full_date")),
    ("Policy Brief_Minor Forest Produce_10-02-2018.pdf",
     ("2018-02-10", "day", "full_date")),
    ("Net Zero Report _24-5-2024.pdf", ("2024-05-24", "day", "full_date")),
    ("1691667813ITC MSK Report 14 April 2023.pdf", ("2023-04-14", "day", "full_date")),
    # A month and a year, in both orders and both spellings of the year.
    ("Tender_No_20_CG_Global_Project_TERI_2024_September_Tender.pdf",
     ("2024-09-01", "month", "month_year")),
    ("Dr Shiv Kumar Dube IJSRP November 2018.pdf", ("2018-11-01", "month", "month_year")),
    ("newsTRAC-feb21.pdf", ("2021-02-01", "month", "month_year")),
    ("newsTRAC-nov19-hindi.pdf", ("2019-11-01", "month", "month_year")),
    ("2005SF32-nhpc-report-final-may-07.pdf", ("2007-05-01", "month", "month_year")),
    # A quarter opens at the first month it names.
    ("April-June2020_SB_NL.pdf", ("2020-04-01", "month", "month_year")),
    # A reporting period: a year, and only a year.
    ("TERI-Annual-Report-2024-25.pdf", ("2024-01-01", "year", "edition")),
    ("Auditor-Report-2024-25.pdf", ("2024-01-01", "year", "edition")),
    ("Balance_Sheet_22_23.pdf", ("2022-01-01", "year", "edition")),
    ("Air-Quality-Status-Report-of-Maharashtra-2022-23.pdf",
     ("2022-01-01", "year", "edition")),
    # A bare year, correctly labelled as the weak reading it is.
    ("IoET_brochure_2026.pdf", ("2026-01-01", "year", "bare_year")),
    ("Presentation_IGES_ISAP_2017.pdf", ("2017-01-01", "year", "bare_year")),
])
def test_a_name_is_read_at_the_precision_it_states(text, expected):
    assert _read(text) == expected


# --------------------------------------------------------------------------- #
# What a name does not establish
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    # Scheme codes. "ES", the scheme year, the scheme — four digits glued to
    # letters on both sides. This is the rejection that pays for the module.
    "ES2009CE09.pdf",
    "ES1999CR61.pdf",
    "ES2004SF32.pdf",
    "2013MC02 Air Quality Report.pdf",
    # An upload-id prefix Drupal generates.
    "1694685266Uttreja et al - The Lancet.pdf",
    # No date of any kind.
    "Agenda.pdf",
    "clean-fuel-for-cooking-solutions-dp-new.pdf",
    "Flyer WEBINAR 23 September.pdf",     # a day and a month, but no year
    # Out of range for this corpus: a scanner with a dead clock, or an id.
    "report-1970.pdf",
    "doc-2099.pdf",
    "",
    None,
])
def test_a_name_that_states_no_date_yields_none(text):
    assert read_title_date(text) is None


def test_a_year_glued_to_a_token_is_not_a_year():
    """The single rule that separates a date from an identifier, stated once."""
    assert read_title_date("ES2009CE09.pdf") is None
    assert read_title_date("ES-2009-CE09.pdf") is not None


def test_a_two_digit_year_needs_to_be_joined_to_its_month():
    """"nov19" is November 2019. "Nov 19" is far more likely the 19th, and a
    month with no year establishes nothing, so it is refused rather than guessed."""
    assert _read("newsTRAC-nov19.pdf") == ("2019-11-01", "month", "month_year")
    assert read_title_date("Workshop Nov 19.pdf") is None


def test_the_most_precise_reading_of_a_string_wins():
    """A string can offer several. "Tender ... 2024 ... 11-12-2024" must be read
    as the day it names, not as the year that also appears in it."""
    text = "Tender_No_22_Project_TERI_2024_December_dated_11-12-2024.pdf"
    assert _read(text) == ("2024-12-11", "day", "full_date")


def test_an_edition_span_beats_the_bare_year_inside_it():
    """"2016-17" is one statement about a period, not two loose years — and the
    difference is what the caller weighs it by."""
    assert _read("TAR_2016-17.pdf") == ("2016-01-01", "year", "edition")


def test_a_file_extension_is_never_read_as_part_of_a_date():
    assert read_title_date("report.2018.pdf").normalized_value.isoformat() == "2018-01-01"


# --------------------------------------------------------------------------- #
# Which of the file's strings answers, and in what order
# --------------------------------------------------------------------------- #

def test_the_source_order_is_declared_not_inlined():
    """The order *is* the rule, so it is stated once, as data."""
    from app.ingestion.title_dates import TITLE_SOURCES

    assert TITLE_SOURCES == ("filename", "pdf_internal_title", "link_text")


def test_the_filename_beats_a_generic_link_label():
    """A Drupal link label describes the *page's* slot, not the file in it. Ten
    editions sit behind labels reading "Annual Report"."""
    found = title_date(link_text="Annual Report",
                       filename="TERI-Annual-Report-2024-25.pdf")
    assert found.normalized_value.isoformat() == "2024-01-01"
    assert found.title_source == "filename"


def test_the_filename_beats_a_stale_link_label_that_names_a_date():
    """The sharper case: the label is not merely generic, it is *wrong*. A slot
    labelled "Report 2022" whose file was swapped for the 2024 edition is the
    shape this order exists for — the label belongs to the page, the name
    belongs to the file."""
    found = title_date(link_text="Report 2022", filename="Annual_Report_2024.pdf")
    assert found.normalized_value.isoformat() == "2024-01-01"
    assert found.title_source == "filename"


def test_the_internal_title_is_a_source_of_its_own_between_the_two():
    """Stronger than a link label because it belongs to the file; weaker than
    the filename because producers leave template junk in it."""
    found = title_date(link_text="Download", filename="file.pdf",
                       pdf_title="Emission Inventory January 2023")
    assert found.title_source == "pdf_internal_title"
    assert found.precision == "month"


def test_the_internal_title_beats_the_link_label():
    found = title_date(link_text="Quarterly update 2016",
                       filename="download.pdf",
                       pdf_title="Air Quality Report March 2019")
    assert found.title_source == "pdf_internal_title"
    assert found.normalized_value.isoformat() == "2019-03-01"


def test_link_text_still_answers_when_the_file_names_nothing():
    """Phase 0 found the label names the edition for 10/10 annual reports,
    including one whose filename has no year at all. Last, not unused."""
    found = title_date(link_text="Annual Report 2021-2022",
                       filename="TERI_Annual_Report_upload.pdf")
    assert found.normalized_value.isoformat() == "2021-01-01"
    assert found.title_source == "link_text"


def test_a_source_that_states_no_date_is_skipped_not_treated_as_an_answer():
    found = title_date(link_text="Auditor's Report",
                       filename="Auditor-Report-2024-25.pdf")
    assert found.normalized_value.isoformat() == "2024-01-01"
    assert found.title_source == "filename"


def test_nothing_named_anywhere_is_none():
    assert title_date(link_text="Download", filename="a.pdf",
                      pdf_title="Microsoft Word - Document1") is None


# --------------------------------------------------------------------------- #
# A sequence number is not a day
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, expected", [
    ("Issue 8, Nov 2019 (Hindi)", ("2019-11-01", "month", "month_year")),
    ("Issue 12, Feb 2021", ("2021-02-01", "month", "month_year")),
    ("Volume 3, March 2020", ("2020-03-01", "month", "month_year")),
    ("Chapter 5 January 2015", ("2015-01-01", "month", "month_year")),
])
def test_a_labelled_number_is_not_read_as_a_day(text, expected):
    """"Issue 8, Nov 2019" is the eighth issue, published in November 2019.
    Reading it as 8 November invents a day the newsletter never claimed — and
    these are link labels, which is the source most likely to be phrased this
    way. The month/year reading still gets its turn."""
    assert _read(text) == expected


def test_the_label_guard_does_not_swallow_a_real_day():
    """It must not become a blanket refusal of numbers near words."""
    assert _read("No. 22 dated 11-12-2024") == ("2024-12-11", "day", "full_date")
    assert _read("Press release 09 March 2026") == ("2026-03-09", "day", "full_date")


# --------------------------------------------------------------------------- #
# A year the document looks *towards* is not the year it was written
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text", [
    # The named case. "Post-2015" is the development agenda; the bulletin
    # announcing it was released on 9 July 2014.
    "Post_2015_bulletin and_TEDDY_launch.pdf",
    "Post 2030_Agenda.pdf",
    "Post 2030_Concept_Note.pdf",
    "Event_Summary_Agenda_2030_and_Beyond.pdf",
    "Vision 2030 brochure.pdf",
    "Towards-2047.pdf",
    "Emissions by 2030.pdf",
])
def test_a_policy_horizon_is_not_read_as_a_date(text):
    """Refused outright rather than downgraded, because there is no weaker true
    reading underneath: the number is not a date at all. Measured on the live
    corpus, this was the last remaining way a shelf file took a wrong year."""
    assert read_title_date(text) is None


def test_the_horizon_guard_only_looks_at_the_adjacent_word():
    """It must not refuse every year that follows one of these words somewhere.
    "Post Harvest Losses 2015" is a 2015 document about post-harvest losses."""
    assert _read("Post Harvest Losses 2015.pdf") == ("2015-01-01", "year", "bare_year")


@pytest.mark.parametrize("text, expected", [
    # A horizon in the name must not suppress a real date elsewhere in it.
    ("Agenda 2030 progress report 14 April 2023.pdf",
     ("2023-04-14", "day", "full_date")),
    ("Vision 2030 update March 2021.pdf", ("2021-03-01", "month", "month_year")),
    ("Post 2030 Annual Report 2019-20.pdf", ("2019-01-01", "year", "edition")),
    # Two bare years, the first a horizon: the second still answers.
    ("Beyond 2030 brochure 2024.pdf", ("2024-01-01", "year", "bare_year")),
])
def test_a_horizon_does_not_suppress_a_real_date_in_the_same_name(text, expected):
    assert _read(text) == expected
