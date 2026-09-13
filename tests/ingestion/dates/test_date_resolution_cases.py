"""Case-by-case checks of the production resolver against real corpus shapes.

Each test drives :func:`app.ingestion.date_resolution.resolve` — the canonical
entry point — with the PDF text that document actually contains. Only the model
call is stubbed, and the stub's verdict is passed through the *production*
grounding functions and ``safe_action`` gates, so everything that decides whether
a date moves is the real implementation.

The statements and dates below are taken from
``reports/phase0/override_audit.csv`` and the Phase 0 audits. Nothing here
special-cases a filename: the fixtures are inputs, and the rules under test are
general.

The default fixture page is an ``Announcements`` shelf — a ``page``-bundle node
holding three files, dated only by its Drupal creation stamp. That matters for
what a *refused* override now means here: the file does not fall back to the
shelf's stamp, it ends up with **no date**, because a stamp shared by three
documents is a fact about the page (see
:func:`app.ingestion.date_rules.page_date_is_usable`). Each test's subject is
unchanged — that the proposed date did not become the document's.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ingestion import date_resolution
from app.ingestion.date_llm import (
    DateInterpretation,
    date_is_in_text,
    statement_is_in_text,
)
from app.ingestion.date_resolution import build_evidence, resolve

PAGE_DATE = "2018-04-11T00:00:00+00:00"
LATER_UPLOAD = "2024-06-01T00:00:00+00:00"


def _node(created=PAGE_DATE, files=3, **kwargs):
    return SimpleNamespace(
        uuid="node-1", title=kwargs.pop("title", "Announcements"),
        url="https://teriin.org/announcements", created=created,
        bundle="page", files=[object()] * files, **kwargs,
    )


def _file(**kwargs):
    return SimpleNamespace(
        uuid="f1", url=kwargs.pop("url", "https://teriin.org/files/a.pdf"),
        filename=kwargs.pop("filename", "a.pdf"),
        description=kwargs.pop("description", None),
        origin=kwargs.pop("origin", "inbody"),
        created=kwargs.pop("created", None),
        **kwargs,
    )


def _verdict(**kwargs) -> DateInterpretation:
    kwargs.setdefault("confidence", 0.95)
    kwargs.setdefault("recommended_action", "override")
    kwargs.setdefault("date_type", "publication")
    return DateInterpretation(**kwargs)


def _resolve(
    monkeypatch,
    *,
    pdf_text: str = "",
    verdict: DateInterpretation | None = None,
    node=None,
    file=None,
    pdf_created: str | None = None,
):
    """Resolve one PDF, stubbing only the model call.

    The verdict is grounded with the production checks, so a statement the PDF
    does not contain fails here exactly as it would in a real run.
    """
    def _fill(evidence, _content):
        evidence.head_text = pdf_text
        evidence.pdf_created = pdf_created

    monkeypatch.setattr(date_resolution, "_read_pdf_signals", _fill)

    def _interpret(evidence):
        if verdict is None:
            return None
        verdict.set_grounded(
            date_is_in_text(verdict.candidate_start_date, evidence.head_text),
            statement_is_in_text(verdict.publication_statement, evidence.head_text),
        )
        return verdict

    monkeypatch.setattr("app.ingestion.date_llm.interpret", _interpret)
    evidence = build_evidence(
        document_id="d1",
        node=node or _node(),
        file=file or _file(created=LATER_UPLOAD, origin="attachment"),
    )
    return resolve(evidence, content=b"%PDF-")


# --------------------------------------------------------------------------- #
# The six approved examples
# --------------------------------------------------------------------------- #

APPROVED = [
    pytest.param(
        "20250331_pr_3851.pdf", "2025-03-31",
        "New Delhi, 31 March 2025",
        "TERI Press Release New Delhi, 31 March 2025 Findings of the study were "
        "released today at India Habitat Centre.",
        id="press-release-dateline",
    ),
    pytest.param(
        "Post_2015_bulletin_and_TEDDY_launch.pdf", "2014-07-09",
        "New Delhi, July 9, 2014: The bulletin on the post-2015 Development "
        "Agenda was presented at The Energy and Resources Institute (TERI).",
        "New Delhi, July 9, 2014: The bulletin on the post-2015 Development "
        "Agenda was presented at The Energy and Resources Institute (TERI). "
        "TEDDY was launched at the same event.",
        id="press-release-long-dateline",
    ),
    pytest.param(
        "Tender_No_22_Project_TERI_2024_December.pdf", "2024-12-11",
        "ISSUE NO. 22 DATED 11-12-2024",
        "TENDER BULLETIN ISSUE NO. 22 DATED 11-12-2024 Renewal of annual "
        "maintenance contract, The Energy and Resources Institute.",
        id="official-issue-line",
    ),
    pytest.param(
        "Tender_NAM_Project_TERI_2023_August.pdf", "2023-08-10",
        "ISSUE NO. 01 DATED 10-08-2023",
        "TENDER BULLETIN ISSUE NO. 01 DATED 10-08-2023 Supply and installation "
        "of monitoring equipment.",
        id="official-issue-line-second",
    ),
    pytest.param(
        "1.-MoR-circular-date-15.03.2022.pdf", "2022-03-15",
        "dt.15.03.2022",
        "Government of India Ministry of Railways Circular No. 2022/TG-IV "
        "dt.15.03.2022 Sub: revised guidelines.",
        id="circular-dated",
    ),
    pytest.param(
        "India-News-Calling-Chandigarh-Monday-December-23-2013.pdf", "2013-12-23",
        "Chandigarh,23.12.13:",
        "India News Calling Chandigarh,23.12.13: A conference on promoting "
        "rooftop solar photovoltaic systems was held in the city.",
        id="newspaper-short-dateline",
    ),
]


@pytest.mark.parametrize("filename, expected, statement, pdf_text", APPROVED)
def test_the_approved_examples_still_resolve(
    monkeypatch, filename, expected, statement, pdf_text
):
    """The six overrides signed off in Phase 1 must still be produced."""
    got = _resolve(
        monkeypatch, pdf_text=pdf_text,
        verdict=_verdict(candidate_start_date=expected, publication_statement=statement),
        file=_file(filename=filename, created=LATER_UPLOAD, origin="attachment"),
    )
    assert got.overridden is True, f"{filename} should override"
    # Compared on the day: the two override paths spell the same value
    # differently — the interpreter hands back a bare `YYYY-MM-DD`, the
    # deterministic rules a stored midnight-UTC timestamp — and which of them
    # answers is not what this test is about. Two of these six now cost no model
    # call at all, because the filename states the same day the body does.
    assert str(got.start_value)[:10] == expected


# --------------------------------------------------------------------------- #
# Newspaper dateline vs reconstructed masthead
# --------------------------------------------------------------------------- #

def test_a_masthead_present_in_the_pdf_overrides(monkeypatch):
    got = _resolve(
        monkeypatch,
        pdf_text="CHANDIGARHTRIBUNE CHANDIGARH | TUESDAY | 24 | DECEMBER 2013 5 NEWS",
        verdict=_verdict(candidate_start_date="2013-12-24",
                         publication_statement="CHANDIGARH | TUESDAY | 24 | DECEMBER 2013"),
    )
    assert got.start_value == "2013-12-24"


def test_a_masthead_reconstructed_from_the_filename_is_rejected(monkeypatch):
    """The Pioneer false positive: the date is in the print header, the
    masthead the model reported is not in the document at all."""
    got = _resolve(
        monkeypatch,
        pdf_text="12/24/13 The Pioneer www.dailypioneer.com/print.php?storydetail",
        verdict=_verdict(candidate_start_date="2013-12-24",
                         publication_statement="The Pioneer, Tuesday, December 24, 2013"),
        file=_file(filename="The-Pioneer-Chandigarh-Tuesday-December-24-2013.pdf",
                   created=LATER_UPLOAD, origin="attachment"),
    )
    # The *model's* reconstruction is still refused — the masthead it quoted is
    # not in the document. What dates the file now is the filename read directly,
    # as a name, at the precision it actually states; the two must not be
    # confused, so the rule and the source name which one answered.
    assert got.decision.rule == "title_states_date"
    assert got.canonical_source == "document_title"
    assert got.start_value == "2013-12-24T00:00:00+00:00"
    assert got.llm_raw is None, "a name that answers costs no model call"


# --------------------------------------------------------------------------- #
# Date kinds that must never become a publication date
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "date_type, statement, candidate, pdf_text",
    [
        ("notification", "Notified on 18.05.2023", "2023-05-18",
         "National Electricity Policy. Notified on 18.05.2023 by the Ministry."),
        ("effective", "with effect from 1 April 2024", "2024-04-01",
         "Tariff Order. This shall come into force with effect from 1 April 2024."),
        ("event", "Workshop held on 5 March 2025", "2025-03-05",
         "Agenda. Workshop held on 5 March 2025 at India Habitat Centre."),
        ("authoring", "created 12 August 2019", "2019-08-12",
         "Report. Document properties: created 12 August 2019 by the author."),
        ("upload", "uploaded 22 August 2024", "2024-08-22",
         "Handout. File uploaded 22 August 2024 to the project page."),
    ],
)
def test_other_date_kinds_never_move_the_date(
    monkeypatch, date_type, statement, candidate, pdf_text
):
    got = _resolve(
        monkeypatch, pdf_text=pdf_text,
        verdict=_verdict(candidate_start_date=candidate, date_type=date_type,
                         publication_statement=statement),
    )
    assert got.overridden is False
    assert got.start_value is None, "refused, and a shelf stamp is not a fallback"
    assert got.decision.date_type == date_type


def test_an_update_year_does_not_move_the_date(monkeypatch):
    """"first published in September 2020 and updated in 2023" proposing 2023."""
    text = ("This factsheet has first been published in September 2020 and is "
            "being updated in 2023 as part of the alliance's work.")
    got = _resolve(
        monkeypatch, pdf_text=text, pdf_created="2023-11-11T00:00:00+00:00",
        verdict=_verdict(
            candidate_start_date="2023-11-11",
            publication_statement="first been published in September 2020 and is "
                                  "being updated in 2023"),
    )
    assert got.overridden is False
    assert got.start_value is None


def test_a_citation_year_does_not_move_the_date(monkeypatch):
    text = ("Suggested citation The Energy and Resources Institute (TERI). 2023. "
            "Needs Assessment for Transformative Climate Action. New Delhi: TERI")
    got = _resolve(
        monkeypatch, pdf_text=text,
        verdict=_verdict(
            candidate_start_date="2023-01-01",
            publication_statement="The Energy and Resources Institute (TERI). 2023. "
                                  "Needs Assessment for Transformative Climate Action"),
    )
    assert got.overridden is False
    assert got.start_value is None


def test_a_month_only_statement_does_not_invent_a_day(monkeypatch):
    got = _resolve(
        monkeypatch,
        pdf_text="Marine Litter in the South Asian Seas Region. Colombo, September 2007",
        verdict=_verdict(candidate_start_date="2007-09-01",
                         publication_statement="Colombo, September 2007"),
    )
    assert got.overridden is False
    assert got.start_value is None


def test_a_cover_month_without_publication_wording_does_not_move_the_date(monkeypatch):
    got = _resolve(
        monkeypatch,
        pdf_text="CSIR-NEERI, Nagpur January 2023 Final Report Emission Inventory",
        verdict=_verdict(candidate_start_date="2023-01-01",
                         publication_statement="January 2023 Final Report"),
    )
    assert got.overridden is False
    assert got.start_value is None


# --------------------------------------------------------------------------- #
# Annual reports: an edition label, never a date
# --------------------------------------------------------------------------- #

def test_an_annual_report_is_dated_from_its_own_name(monkeypatch):
    """The reported failure, end to end. Ten annual reports hang off one
    `Annual Reports` page whose node was typed on 2022-02-09, and every edition
    was carrying that day — the 2024-25 report included. The file's own name says
    which edition it is, so it is dated to **2025** — the year the 2024-25
    period ends in, which is when an edition covering it is published — at year
    precision, 1 January standing in for the year.

    The edition label is still produced and is still not a date; the two now
    simply agree about the year rather than the label being the only true thing
    on the record."""
    got = _resolve(
        monkeypatch,
        pdf_text="ANNUAL REPORT 2024/25 Vision Creating Innovative Solutions",
        verdict=_verdict(candidate_start_date=None, date_type="edition",
                         edition_label="2024-2025",
                         recommended_action="keep_page_date"),
        node=_node(created="2022-02-09T06:59:06+00:00", files=10,
                   title="Annual Reports"),
        file=_file(filename="TERI-Annual-Report-2024-25.pdf"),
    )
    assert got.start_value == "2025-01-01T00:00:00+00:00"
    assert got.start_precision == "year"
    assert got.edition_label == "2024-25", "canonical spelling, per app.core.editions"
    assert got.overridden is True
    assert got.canonical_source == "document_title"
    assert got.decision.rule == "title_states_date"


def test_an_edition_in_the_filename_is_labelled_without_reading_the_pdf():
    """A single-PDF page settles deterministically, and still yields the label."""
    evidence = build_evidence(
        document_id="d1", node=_node(files=1),
        file=_file(filename="TAR_2015-16.pdf"),
    )
    got = resolve(evidence, content=b"%PDF-")
    assert got.start_value == PAGE_DATE
    assert got.edition_label == "2015-16"


# --------------------------------------------------------------------------- #
# Signals that are never enough on their own — settled without a model call
# --------------------------------------------------------------------------- #

def _no_llm(monkeypatch):
    monkeypatch.setattr(
        "app.ingestion.date_llm.interpret",
        lambda _e: pytest.fail("this case must be settled without a model call"),
    )


def test_a_single_pdf_page_keeps_its_date(monkeypatch):
    _no_llm(monkeypatch)
    evidence = build_evidence(document_id="d1", node=_node(files=1), file=_file())
    got = resolve(evidence, content=b"%PDF-")
    assert got.start_value == PAGE_DATE


def test_a_filename_year_alone_does_not_move_the_date(monkeypatch):
    _no_llm(monkeypatch)
    evidence = build_evidence(
        document_id="d1", node=_node(files=1),
        file=_file(filename="2014BL18-es-women-empow.pdf"),
    )
    got = resolve(evidence, content=b"%PDF-")
    assert got.start_value == PAGE_DATE


def test_a_pdf_creation_date_alone_does_not_move_the_date(monkeypatch):
    """Several PDFs uploaded with the page; DocInfo is years older. A PDF
    creation timestamp has never been allowed to date a document, and the file
    ends up undated rather than borrowing the shelf's stamp."""
    _no_llm(monkeypatch)
    monkeypatch.setattr(
        date_resolution, "_read_pdf_signals",
        lambda evidence, _c: setattr(evidence, "pdf_created",
                                     "2015-02-04T00:00:00+00:00"),
    )
    evidence = build_evidence(
        document_id="d1", node=_node(created="2019-03-18T00:00:00+00:00", files=6),
        file=_file(created="2019-03-20T00:00:00+00:00", origin="attachment"),
    )
    got = resolve(evidence, content=b"%PDF-")
    assert got.overridden is False
    assert got.start_value is None


def test_several_pdfs_uploaded_together_are_still_not_dated_from_the_page(monkeypatch):
    """Fifteen files that arrived within a month of the page. Arriving together
    says they were uploaded together, not that they were written together — and
    the page's creation stamp is one date for fifteen documents."""
    _no_llm(monkeypatch)
    evidence = build_evidence(
        document_id="d1", node=_node(created="2018-09-27T00:00:00+00:00", files=15),
        file=_file(created="2018-10-23T00:00:00+00:00", origin="attachment"),
    )
    got = resolve(evidence, content=b"%PDF-")
    assert got.start_value is None
    assert got.canonical_source == "no_evidence"


def test_a_migration_era_file_date_is_never_treated_as_an_upload(monkeypatch):
    _no_llm(monkeypatch)
    evidence = build_evidence(
        document_id="d1", node=_node(created="2012-06-23T00:00:00+00:00", files=1),
        file=_file(created="2018-05-01T00:00:00+00:00", origin="attachment"),
    )
    got = resolve(evidence, content=b"%PDF-")
    assert got.start_value == "2012-06-23T00:00:00+00:00"


def test_an_unreadable_pdf_on_a_routed_page_cannot_be_dated(monkeypatch):
    """Real bytes PyMuPDF cannot parse: no text, so nothing can be grounded —
    and on a shelf page there is nothing to fall back to either."""
    got = _resolve(
        monkeypatch, pdf_text="",
        verdict=_verdict(candidate_start_date="2024-06-01",
                         publication_statement="Published on 1 June 2024"),
    )
    assert got.overridden is False
    assert got.start_value is None
