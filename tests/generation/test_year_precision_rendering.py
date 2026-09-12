"""A year the source stated must never be shown to the model as a day.

389 research papers state only a year — ``field_rpaper_year = 2016``. The column
is a DATETIME, so the value has to be *some* day, and it is 1 January **as a
marker**. Nothing about that marker is a claim, and the only place that can keep
it from becoming one is the layer that renders a date for the model: show
``2016-01-01`` and the answer says "published on 1 January 2016", which nobody
asserted.

The same holds one step finer. A statement establishing a month but no day
("Published in September 2007") yields 2007-09-01 at ``month`` precision, where
the 1st is the marker. Both are produced by
``DateInterpretation.supported_precision`` on the PDF path, which stores the
first day of the period it can establish and nothing finer — so the layer that
renders a date has to stop at the same boundary.
"""

from __future__ import annotations

import pytest

from app.generation.prompts import _source_hint
from app.pipeline.summarize import _effective_date_label

YEAR_ONLY = "2016-01-01T00:00:00+00:00"
FULL_DATE = "2019-08-27T06:31:43+00:00"


# --------------------------------------------------------------------------- #
# The block header the model reads
# --------------------------------------------------------------------------- #

def test_a_year_precision_date_is_shown_as_a_year():
    header = _source_hint({
        "source_type": "website", "title": "Some paper",
        "effective_start_date": YEAR_ONLY, "start_precision": "year",
    })
    assert "2016" in header
    assert "2016-01-01" not in header
    assert "01-01" not in header


def test_the_header_says_the_day_is_not_known():
    """Not merely omitting the day — saying so. A bare "2016" invites the model
    to supply a plausible month."""
    header = _source_hint({
        "source_type": "website", "title": "Some paper",
        "effective_start_date": YEAR_ONLY, "start_precision": "year",
    })
    assert "year only" in header
    assert "day is not known" in header


def test_a_full_date_is_unchanged():
    """Everything that is not year precision must render byte-identically to
    before, which is 12,000-odd documents."""
    header = _source_hint({
        "source_type": "website", "title": "TERI launches report",
        "effective_start_date": "2024-05-01T00:00:00",
    })
    assert header == "website · TERI launches report · page date 2024-05-01T00:00:00"


def test_an_absent_precision_reads_as_a_full_date():
    """Existing points predate the payload field. Absent means "a full date",
    which is true of all of them — and is why this needed no version bump."""
    header = _source_hint({
        "source_type": "website", "title": "x", "effective_start_date": FULL_DATE,
    })
    assert FULL_DATE in header


@pytest.mark.parametrize("precision", [None, "", "day", "unknown"])
def test_a_day_or_unknown_precision_renders_the_full_date(precision):
    """Only a precision that names a *period* changes the rendering. Anything
    else, including an unrecognised value, is treated as a full date — the safe
    direction, because it shows what is stored rather than hiding part of it."""
    payload = {"source_type": "website", "title": "x", "effective_start_date": FULL_DATE}
    if precision is not None:
        payload["start_precision"] = precision
    assert FULL_DATE in _source_hint(payload)


def test_a_month_precision_date_is_shown_as_a_month():
    """Month precision is real now: the resolver can establish a month without a
    day (`DateInterpretation.supported_precision`), and the day it stores is the
    1st as a marker. Rendering the full value would assert that 1st."""
    header = _source_hint({
        "source_type": "website", "title": "A report",
        "effective_start_date": "2007-09-01T00:00:00+00:00",
        "start_precision": "month",
    })
    assert "2007-09" in header
    assert "month only" in header
    assert "day is not known" in header
    assert "2007-09-01" not in header


def test_a_year_precision_pdf_still_keeps_its_edition_and_document_date_apart():
    """The annual-report guard must survive: three labelled facts, none merged."""
    header = _source_hint({
        "source_type": "pdf_attachment", "title": "Annual Report 2024-2025",
        "edition_label": "2024-25", "effective_start_date": YEAR_ONLY,
        "start_precision": "year", "page_number": 3,
    })
    assert "edition 2024-25" in header
    assert "year only" in header
    assert "2016-01-01" not in header


# --------------------------------------------------------------------------- #
# The summariser's per-document line
# --------------------------------------------------------------------------- #

def test_the_summary_line_shows_a_bare_year():
    assert _effective_date_label(YEAR_ONLY, "year") == "2016"


def test_the_summary_line_shows_a_full_date_otherwise():
    assert _effective_date_label(FULL_DATE, None) == "2019-08-27"
    assert _effective_date_label(FULL_DATE, "day") == "2019-08-27"


def test_the_summary_line_survives_a_missing_date():
    assert _effective_date_label(None, None) == ""
    assert _effective_date_label("", "year") == ""


def test_the_summary_line_shows_a_bare_month():
    assert _effective_date_label("2007-09-01T00:00:00+00:00", "month") == "2007-09"


def test_the_summary_line_never_leaks_january_first():
    """The specific string that would make the model report an invented day."""
    assert "01-01" not in _effective_date_label(YEAR_ONLY, "year")


# --------------------------------------------------------------------------- #
# It has to reach the payload to be readable at all
# --------------------------------------------------------------------------- #

def test_year_precision_reaches_the_chunk_payload():
    from app.ingestion.chunking.models import Chunk, DocumentMeta
    from app.ingestion.chunking.payload import build_payload

    meta = DocumentMeta(document_id="d", source_type="website",
                        effective_start_date=YEAR_ONLY, start_precision="year")
    payload = build_payload(Chunk(chunk_id="c", text="x", is_parent=False, meta=meta))
    assert payload["start_precision"] == "year"


def test_a_full_date_writes_no_precision_key():
    """Absent rather than "day": that is what keeps old points valid and this
    off the PAYLOAD version, which would cost a full corpus reprocess."""
    from app.ingestion.chunking.models import Chunk, DocumentMeta
    from app.ingestion.chunking.payload import build_payload

    meta = DocumentMeta(document_id="d", source_type="website",
                        effective_start_date=FULL_DATE, start_precision="day")
    payload = build_payload(Chunk(chunk_id="c", text="x", is_parent=False, meta=meta))
    assert "start_precision" not in payload


@pytest.mark.parametrize("precision", ["day", "unknown", None])
def test_only_a_period_precision_reaches_a_payload(precision):
    """Filtered in `build_payload` rather than at a caller, so it holds however
    the meta was built — a document constructed by any other route cannot add
    the key and quietly make old points look stale.

    ``day`` and anything unrecognised write nothing: absent has always meant "a
    full date" for every point in the collection.
    """
    from app.ingestion.chunking.models import Chunk, DocumentMeta
    from app.ingestion.chunking.payload import build_payload

    meta = DocumentMeta(document_id="d", source_type="website",
                        effective_start_date=FULL_DATE, start_precision=precision)
    payload = build_payload(Chunk(chunk_id="c", text="x", is_parent=False, meta=meta))
    assert "start_precision" not in payload


@pytest.mark.parametrize("precision", ["year", "month"])
def test_a_period_precision_reaches_the_chunk_payload(precision):
    """The payload is the only copy retrieval and the answer layer read, so a
    precision that does not reach it does not exist. ``month`` was dropped here
    until this landed, which was the one place a month-precision date silently
    became a day."""
    from app.ingestion.chunking.models import Chunk, DocumentMeta
    from app.ingestion.chunking.payload import build_payload

    meta = DocumentMeta(document_id="d", source_type="website",
                        effective_start_date="2007-09-01T00:00:00+00:00",
                        start_precision=precision,
                        effective_end_date="2008-09-01T00:00:00+00:00",
                        end_precision=precision)
    payload = build_payload(Chunk(chunk_id="c", text="x", is_parent=False, meta=meta))
    assert payload["start_precision"] == precision
    assert payload["end_precision"] == precision


def test_the_precision_marker_still_needs_no_bump_of_its_own():
    """Adding `start_precision` did not require a payload bump: absent means "a
    full date", which was true of every point already in the collection. That
    reasoning is unchanged, and this test exists to keep it separate from the
    reasons that *did* justify one.

    Each bump so far, and why:

    * **2** — the publication-date keys were *renamed*, so old points carry keys
      no reader consults.
    * **3** — an attached PDF's `effective_start_date` is now *decided*
      differently: on a page holding several PDFs the file's own name states its
      date, and where nothing states one and the page is dated only by its Drupal
      creation stamp the file is left undated. A value change rather than a
      structural one, which stretches this component — but it is the component
      that governs the payload, and without a bump a document whose body text has
      not changed keeps the wrong date forever. `reprocess.py` selects on
      `PIPELINE_VERSION` and nothing else, so this is the only lever there is.

    Neither is "a new optional key", which is still not a reason.
    """
    from app.ingestion.version import PAYLOAD

    assert PAYLOAD == 3, (
        "PAYLOAD moved again; check the reason is a key readers actually miss or "
        "a value the same input would no longer produce — not merely a new "
        "optional key."
    )
