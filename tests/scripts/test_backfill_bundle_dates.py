"""The migration that re-dates what is already ingested.

Ingestion code alone fixes nothing historical: a sweep that finds a document
unchanged returns before the document is rebuilt, so an existing row keeps the
date the old rule gave it. These tests cover the parts that decide *what* moves
and *what must not* — the database round-trips are exercised by running it.

The properties that matter:

* it re-derives from the metadata, never from the current value, so running it
  twice cannot compound;
* an attachment takes its page's new date, and a file dated from its own text
  does not;
* the pre-flight refuses a run that would write something the mapping does not
  sanction.
"""

from __future__ import annotations

import pytest

from app.ingestion.bundle_dates import resolve_effective_dates
from scripts.backfill_bundle_dates import (
    Move,
    attachment_moves,
    preflight,
    report,
)

CREATED = "2018-01-11T06:29:59+00:00"


def _move(**kwargs) -> Move:
    base = dict(
        document_id="d1", source_type="website", bundle="news",
        url="https://teriin.org/news/x", old_start=CREATED,
        new_start="2015-08-27T00:00:00+00:00", start_precision="day",
        source="cms_field", rule="bundle_date_field", field="field_news_date",
    )
    base.update(kwargs)
    return Move(**base)


# --------------------------------------------------------------------------- #
# The move
# --------------------------------------------------------------------------- #

def test_a_move_reports_how_far_the_stored_date_was_out():
    assert _move().days > 0, "the stored date was later than the truth"
    assert _move(old_start="2010-01-01T00:00:00+00:00").days < 0


def test_a_move_is_immutable():
    """It is reported, pre-flighted and then written; a mutable one could be
    reviewed as one thing and applied as another."""
    with pytest.raises(Exception):
        _move().new_start = "2001-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------------- #

def test_a_clean_set_of_moves_passes():
    assert preflight([_move()], expect=-1) == []


def test_a_well_formed_range_passes():
    assert preflight([_move(field="field_completed_start_date",
                            new_start="2020-01-01T00:00:00+00:00",
                            new_end="2022-12-31T00:00:00+00:00")], expect=-1) == []


def test_a_range_stored_end_before_start_is_refused():
    """`bundle_dates` drops an inverted end rather than resolving it, so this
    can only fire if something downstream reassembled one."""
    problems = preflight([_move(new_start="2022-12-31T00:00:00+00:00",
                                new_end="2020-01-01T00:00:00+00:00")], expect=-1)
    assert any("end-before-start" in p for p in problems)


def test_an_end_value_with_no_timezone_is_refused():
    problems = preflight([_move(new_end="2022-12-31T00:00:00")], expect=-1)
    assert any("UTC" in p for p in problems)


def test_an_end_field_is_a_mapped_field():
    """Both halves of a range come from the mapping, so neither can be a field
    nothing declared."""
    assert preflight([_move(field="field_event_end_date")], expect=-1) == []


def test_a_drifted_corpus_stops_the_run():
    """The reviewed dry run is the authority. A different set must not be
    rewritten silently because the corpus changed since."""
    problems = preflight([_move()], expect=99)
    assert problems and "expected 99" in problems[0]


def test_a_field_outside_the_mapping_is_refused():
    """The mapping is what sanctions a write. A value from anywhere else means
    something re-implemented the rule."""
    problems = preflight([_move(field="field_something_else")], expect=-1)
    assert any("not in the bundle mapping" in p for p in problems)


def test_an_unexpected_provenance_is_refused():
    problems = preflight([_move(source="guesswork")], expect=-1)
    assert any("unexpected provenance" in p for p in problems)


def test_a_year_precision_value_that_is_not_january_is_refused():
    """A year-precision date is 1 January standing in for a year. Any other day
    means the value and its precision disagree about what is known."""
    problems = preflight(
        [_move(start_precision="year", new_start="2016-06-01T00:00:00+00:00")], expect=-1)
    assert any("1 January" in p for p in problems)


def test_a_real_year_precision_value_passes():
    assert preflight([_move(start_precision="year", field="field_rpaper_year",
                            new_start="2016-01-01T00:00:00+00:00")],
                     expect=-1) == []


def test_a_value_with_no_timezone_is_refused():
    """`state._to_datetime` normalises to naive UTC; an offset-less value would
    shift the calendar date every consumer reads."""
    problems = preflight([_move(new_start="2015-08-27T00:00:00")], expect=-1)
    assert any("timezone" in p for p in problems)


def test_an_attachment_move_passes_the_same_gate():
    assert preflight([_move(source_type="pdf_attachment", source="parent_page",
                            rule="inherited_from_parent", parent_id="p1")],
                     expect=-1) == []


# --------------------------------------------------------------------------- #
# Attachment inheritance
# --------------------------------------------------------------------------- #

class _Cursor:
    """A cursor that answers the two queries under test.

    `fetchone` returns None so `schema._column_exists` reports every legacy
    `published_*` column as absent — i.e. this models a **fully migrated**
    database, which is the state these tests are about. The half-migrated
    read path (COALESCE over the legacy column) is exercised against the real
    schema, not here.

    The second query counts PDFs per page. It is answered by counting the
    fixture rows rather than by a separate fixture, so a test that declares two
    attachments on one page gets a page that holds two — the two facts cannot
    drift apart and say different things about the same page.
    """

    def __init__(self, rows):
        self._rows = rows
        self._last = ""

    def execute(self, sql=None, *_a, **_k):
        self._last = sql or ""
        return None

    def fetchall(self):
        if "COUNT(DISTINCT file_uuid)" in self._last:
            counts: dict[str, set] = {}
            for row in self._rows:
                counts.setdefault(row["parent_id"], set()).add(row["document_id"])
            return [{"document_id": page, "n": len(files)}
                    for page, files in counts.items()]
        return self._rows

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.fixture
def rows(monkeypatch):
    """Patch the one query `attachment_moves` makes."""
    captured: list = []

    def _install(value):
        captured.clear()
        captured.extend(value)

    import scripts.backfill_bundle_dates as script

    monkeypatch.setattr(script, "attachment_moves",
                        script.attachment_moves)  # keep the real function
    monkeypatch.setattr("app.catalog.db.state_table", lambda: "documents")
    monkeypatch.setattr("app.core.clients.mysql_connection",
                        lambda *a, **k: _Conn(captured))
    return _install


def _paper_page():
    return {"p1": resolve_effective_dates("research_papers", CREATED, {"field_rpaper_year": 2016})}


def _row(**kwargs):
    base = {"parent_id": "p1", "document_id": "f1",
            "effective_start_date": CREATED, "start_precision": "day",
            "effective_end_date": None, "end_precision": None,
            "date_source": "parent_page",
            "url": "https://teriin.org/a.pdf", "bundle": "research_papers",
            # What the file is called, and what the page calls it. Both are
            # read now: on a multi-PDF page the naming is what tells one file
            # from another.
            "title": None, "filename": "a.pdf", "origin": "attachment",
            "parent_title": "A paper"}
    base.update(kwargs)
    return base


def _project_page(start="2020-01-01T00:00:00+00:00",
                  end="2022-12-31T00:00:00+00:00"):
    """A completed_projects page whose bundle declares both ends."""
    metadata = {"field_completed_start_date": start}
    if end is not None:
        metadata["field_completed_end_date"] = end
    return {"p1": resolve_effective_dates("completed_projects", CREATED, metadata)}


def test_an_attachment_takes_its_pages_new_date(rows):
    rows([_row()])
    moves = attachment_moves(_paper_page())
    assert len(moves) == 1
    assert moves[0].new_start == "2016-01-01T00:00:00+00:00"
    assert moves[0].start_precision == "year"
    assert moves[0].source == "parent_page"
    assert moves[0].parent_id == "p1"


def test_an_attachment_already_on_its_pages_date_does_not_move(rows):
    rows([_row(effective_start_date="2016-01-01T00:00:00+00:00",
               start_precision="year")])
    assert attachment_moves(_paper_page()) == []


def test_a_precision_change_alone_is_still_a_move(rows):
    """The value is right and the marker is not: a reader would render 1 January
    as a real day."""
    rows([_row(effective_start_date="2016-01-01T00:00:00+00:00",
               start_precision="day")])
    assert len(attachment_moves(_paper_page())) == 1


def test_a_file_dated_from_its_own_text_is_left_alone(rows):
    """The one override the design grants, and it outranks inheritance."""
    rows([_row(date_source="document_text")])
    assert attachment_moves(_paper_page()) == []


def test_an_attachment_whose_page_is_not_in_the_catalog_is_skipped(rows):
    rows([_row(parent_id="missing")])
    assert attachment_moves(_paper_page()) == []


def test_an_attachment_reachable_from_two_pages_moves_once(rows):
    """84 files hang off more than one page. Ordered by (file, page) so the same
    parent wins on every run."""
    rows([_row(parent_id="p1"), _row(parent_id="p2")])
    resolutions = _paper_page()
    resolutions["p2"] = resolve_effective_dates("article", "2020-05-05T00:00:00+00:00", {})
    moves = attachment_moves(resolutions)
    assert len(moves) == 1 and moves[0].parent_id == "p1"


def test_every_file_on_one_page_takes_the_same_date(rows):
    rows([_row(document_id=f"f{i}") for i in range(5)])
    moves = attachment_moves(_paper_page())
    assert len(moves) == 5
    assert {m.new_start for m in moves} == {"2016-01-01T00:00:00+00:00"}


def test_a_page_that_resolved_to_nothing_moves_none_of_its_files(rows):
    rows([_row()])
    assert attachment_moves({"p1": resolve_effective_dates("article", None, {})}) == []


# --------------------------------------------------------------------------- #
# Ranges through the backfill
# --------------------------------------------------------------------------- #

def test_an_attachment_inherits_both_ends_of_its_pages_range(rows):
    """A file on a completed project covers the period the project did."""
    rows([_row(bundle="completed_projects")])
    moves = attachment_moves(_project_page())
    assert len(moves) == 1
    assert moves[0].new_start.startswith("2020-01-01")
    assert moves[0].new_end.startswith("2022-12-31")
    assert moves[0].range_added


def test_every_file_on_a_range_page_inherits_the_same_period(rows):
    rows([_row(document_id=f"f{i}", bundle="completed_projects") for i in range(4)])
    moves = attachment_moves(_project_page())
    assert len(moves) == 4
    assert {m.new_end for m in moves} == {"2022-12-31T00:00:00+00:00"}


def test_an_attachment_already_carrying_the_period_does_not_move(rows):
    rows([_row(bundle="completed_projects",
               effective_start_date="2020-01-01T00:00:00+00:00",
               effective_end_date="2022-12-31T00:00:00+00:00")])
    assert attachment_moves(_project_page()) == []


def test_an_end_date_appearing_alone_is_still_a_move(rows):
    """The start is already right and the end is not stored yet — which is the
    shape of every completed project on the day this ships."""
    rows([_row(bundle="completed_projects",
               effective_start_date="2020-01-01T00:00:00+00:00")])
    moves = attachment_moves(_project_page())
    assert len(moves) == 1
    assert not moves[0].start_changed
    assert moves[0].end_changed


def test_an_end_the_source_no_longer_states_is_cleared(rows):
    """A stored end whose CMS value was removed has to go, or a deleted date
    would be immortal."""
    rows([_row(bundle="completed_projects",
               effective_start_date="2020-01-01T00:00:00+00:00",
               effective_end_date="2022-12-31T00:00:00+00:00")])
    moves = attachment_moves(_project_page(end=None))
    assert len(moves) == 1
    assert moves[0].new_end is None


def test_an_inverted_range_propagates_the_start_and_not_the_end(rows):
    rows([_row(bundle="completed_projects")])
    moves = attachment_moves(_project_page(start="2022-12-31T00:00:00+00:00",
                                           end="2020-01-01T00:00:00+00:00"))
    assert len(moves) == 1
    assert moves[0].new_start.startswith("2022-12-31")
    assert moves[0].new_end is None
    assert moves[0].range_issue == "inverted"


# --------------------------------------------------------------------------- #
# Reading a half-migrated database
#
# `ensure_state_table()` is additive: it creates the new columns empty. On a
# database where it has run and `copy_legacy_date_columns()` has not, the new
# columns are NULL for every row while the legacy ones still hold the data.
# Reading the new columns there made every page look like it already agreed with
# itself, so the dry run reported only attachment moves — 0 pages out of 8,507 —
# and `--apply` would have written attachment dates inherited from page
# resolutions while leaving every page untouched.
# --------------------------------------------------------------------------- #

def test_the_read_path_falls_back_to_the_legacy_column_while_it_exists():
    """One expression has to be correct in all three migration states: before
    the copy, after the copy, and after the drop."""
    from scripts.backfill_bundle_dates import _readable

    class _Has:
        def execute(self, *a, **k): return None
        def fetchone(self): return {"1": 1}

    class _HasNot:
        def execute(self, *a, **k): return None
        def fetchone(self): return None

    before = _readable(_Has(), "documents", "effective_start_date", "published_at")
    after = _readable(_HasNot(), "documents", "effective_start_date", "published_at")
    assert before == "COALESCE(`effective_start_date`, `published_at`)"
    assert after == "`effective_start_date`"


def test_the_read_path_qualifies_columns_when_the_query_joins():
    """`attachment_moves` joins the table to itself under aliases; an unqualified
    COALESCE there is an ambiguous-column error at runtime, not a test failure."""
    from scripts.backfill_bundle_dates import _readable

    class _Has:
        def execute(self, *a, **k): return None
        def fetchone(self): return {"1": 1}

    got = _readable(_Has(), "documents", "effective_start_date", "published_at", "d")
    assert got == "COALESCE(d.`effective_start_date`, d.`published_at`)"


def test_a_page_is_only_unrecoverable_when_the_answer_needs_the_lost_stamp():
    """A `news` page whose `field_news_date` is intact resolves from the CMS
    field and never consults `created`, so a lost creation stamp must not drop
    it. The first version skipped 2,770 pages it could have dated exactly."""
    import inspect

    from scripts import backfill_bundle_dates as script

    source = inspect.getsource(script.page_moves)
    assert 'resolved.source != "cms_field"' in source, (
        "unrecoverable must be judged from the resolution, not from whether a "
        "stamp happened to be recoverable"
    )


# --------------------------------------------------------------------------- #
# Reading MySQL, and rewriting Qdrant
# --------------------------------------------------------------------------- #

def test_a_stamp_read_from_mysql_carries_its_utc_offset():
    """The column is a DATETIME and `state._to_datetime` normalises to naive UTC
    on the way in, so the driver hands back a naive value. Passing it through
    verbatim produced `2017-12-28T08:58:09`, which the pre-flight refused — an
    offset-less value read in another zone shifts the calendar date, the exact
    class of bug this migration exists to correct."""
    from datetime import datetime, timezone

    from scripts.backfill_bundle_dates import _iso

    assert _iso(datetime(2017, 12, 28, 8, 58, 9)) == "2017-12-28T08:58:09+00:00"
    # An already-aware value is left exactly as it is.
    aware = datetime(2017, 12, 28, 8, 58, 9, tzinfo=timezone.utc)
    assert _iso(aware) == aware.isoformat()
    assert _iso("2020-01-01T00:00:00+00:00") == "2020-01-01T00:00:00+00:00"
    assert _iso(None) is None


class _FakeQdrant:
    """Just enough of the client for the key migration."""

    def __init__(self, points):
        self.points = points          # [(id, payload)]
        self.set_calls: list[dict] = []
        self.deleted: list[list[str]] = []

    def collection_exists(self, *_a, **_k):
        return True

    def scroll(self, *, with_payload, **_k):
        from types import SimpleNamespace

        got = [SimpleNamespace(id=i, payload={k: v for k, v in p.items()
                                              if k in with_payload})
               for i, p in self.points]
        return got, None

    def set_payload(self, *, payload, **_k):
        self.set_calls.append(payload)

    def delete_payload(self, *, keys, **_k):
        self.deleted.append(list(keys))


def _run_key_migration(monkeypatch, points):
    import scripts.backfill_bundle_dates as script

    fake = _FakeQdrant(points)
    monkeypatch.setattr("app.core.clients.get_qdrant_client", lambda: fake)
    monkeypatch.setattr("app.config.get_settings",
                        lambda: type("S", (), {"qdrant_collection": "c"})())
    script.migrate_payload_keys()
    return fake


def test_the_key_migration_never_overwrites_a_corrected_value(monkeypatch):
    """The bug that would have silently reverted Qdrant on all 5,152 moved
    documents. `apply()` writes the corrected date under the new key and leaves
    the legacy key on the point; a migration that read only the legacy name
    carried the stale value straight back over the correction."""
    fake = _run_key_migration(monkeypatch, [
        ("p1", {"published_at": "2018-01-11T06:29:59+00:00",       # stale
                "effective_start_date": "2004-06-29T00:00:00+00:00"}),  # corrected
    ])
    assert fake.set_calls == [], "nothing should have been written over"
    assert fake.deleted == [["published_at"]], "the legacy key is still dropped"


def test_the_key_migration_carries_a_point_nothing_corrected(monkeypatch):
    """A point outside the move set has no new key, so the legacy value is the
    only value there is and must be carried."""
    fake = _run_key_migration(monkeypatch, [
        ("p2", {"published_at": "2018-01-11T06:29:59+00:00"}),
    ])
    assert fake.set_calls == [
        {"effective_start_date": "2018-01-11T06:29:59+00:00"}]
    assert fake.deleted == [["published_at"]]


def test_the_key_migration_is_a_no_op_on_an_already_migrated_point(monkeypatch):
    fake = _run_key_migration(monkeypatch, [
        ("p3", {"effective_start_date": "2004-06-29T00:00:00+00:00"}),
    ])
    assert fake.set_calls == [] and fake.deleted == []


def test_the_key_migration_drops_the_never_populated_column(monkeypatch):
    """`document_published_at` maps to "" — removed, not renamed."""
    fake = _run_key_migration(monkeypatch, [
        ("p4", {"document_published_at": "2020-01-01T00:00:00+00:00"}),
    ])
    assert fake.set_calls == []
    assert fake.deleted == [["document_published_at"]]


# --------------------------------------------------------------------------- #
# The report is what a reviewer approves
# --------------------------------------------------------------------------- #

def test_the_report_survives_an_empty_run(capsys):
    report([], [])
    assert "0" in capsys.readouterr().out


def test_the_report_counts_the_range_populations_separately(capsys):
    """A start moving and an end appearing are different risks, and a reviewer
    signs them off separately rather than seeing one total."""
    report([
        _move(document_id="a", old_start=CREATED,
              new_start="2020-01-01T00:00:00+00:00"),                # start moved
        _move(document_id="b", old_start="2020-01-01T00:00:00+00:00",
              new_start="2020-01-01T00:00:00+00:00",
              new_end="2022-12-31T00:00:00+00:00",
              bundle="completed_projects"),                          # range added
        _move(document_id="c", bundle="completed_projects",
              new_start="2020-01-01T00:00:00+00:00",
              old_start="2020-01-01T00:00:00+00:00",
              range_issue="inverted"),                               # invalid
        _move(document_id="d", source="created", rule="field_empty"),
    ], [])
    printed = capsys.readouterr().out
    assert "start date changed" in printed
    assert "end date changed" in printed
    assert "range newly added" in printed
    assert "bundle has an end field, none stated" in printed
    assert "invalid range (start applied, end dropped)" in printed
    assert "falling back to the created stamp" in printed
    assert "inverted" in printed


def test_the_report_separates_pages_from_attachments(capsys):
    report([_move(),
            _move(document_id="f1", source_type="pdf_attachment",
                  source="parent_page", rule="inherited_from_parent")], [])
    printed = capsys.readouterr().out
    assert "1 pages, 1 attachments" in printed


def test_the_report_names_documents_it_could_not_re_derive(capsys):
    """Skipped, not guessed: without the original creation stamp the fallback
    rungs have nothing to fall back to."""
    report([], ["lost-1", "lost-2"])
    printed = capsys.readouterr().out
    assert "not recoverable" in printed
    assert "lost-1" in printed


def test_an_undated_document_does_not_break_the_report(capsys):
    report([_move(old_start="")], [])
    assert "attachments" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The backfill applies the resolver's rules, it does not compete with them
# --------------------------------------------------------------------------- #

def _shelf_page():
    """A `page` bundle: its date is only the day the node was typed."""
    return {"p1": resolve_effective_dates("page", "2022-02-09T00:00:00+00:00", {})}


def test_a_shelf_file_is_dated_from_its_name_not_from_the_page(rows):
    """The bug this script must not reintroduce. Two annual reports on one shelf
    page: inheritance would stamp both 2022-02-09, which is what ingestion has
    just stopped doing."""
    rows([_row(document_id="f1", filename="TERI-Annual-Report-2024-25.pdf",
               bundle="page"),
          _row(document_id="f2", filename="TAR_2016-17.pdf", bundle="page")])
    moves = {m.document_id: m for m in attachment_moves(_shelf_page())}
    # The year each period *ends* in: the 2024-25 report is a 2025 document.
    assert moves["f1"].new_start == "2025-01-01T00:00:00+00:00"
    assert moves["f2"].new_start == "2017-01-01T00:00:00+00:00"
    assert {m.source for m in moves.values()} == {"document_title"}


def test_a_shelf_file_that_names_nothing_is_cleared_not_inherited(rows):
    """The other half, and the more dangerous one: a sweep that wrote the page's
    stamp here would undo the fix for 300 documents in one run."""
    rows([_row(document_id="f1", filename="Brochure.pdf", bundle="page"),
          _row(document_id="f2", filename="Flyer.pdf", bundle="page")])
    moves = {m.document_id: m for m in attachment_moves(_shelf_page())}
    assert moves["f1"].new_start is None
    assert moves["f1"].start_cleared is True
    assert moves["f1"].source == "no_evidence"
    assert moves["f1"].start_precision is None


def test_a_lone_file_on_a_shelf_page_still_inherits(rows):
    """One PDF on a page is that page's document, here exactly as in ingestion —
    and its name is not read even though it states a year."""
    rows([_row(document_id="f1", filename="TERI-Annual-Report-2024-25.pdf",
               bundle="page")])
    moves = attachment_moves(_shelf_page())
    assert len(moves) == 1
    assert moves[0].new_start == "2022-02-09T00:00:00+00:00"
    assert moves[0].source == "parent_page"


def test_a_page_title_reused_as_the_files_label_is_not_read_as_the_files_name(rows):
    """`canonical.from_pdf` falls back to the node's title when a file has no
    description, so a file's stored title is often the *shelf's* name. Reading it
    as link text would date every file on 'Annual Reports 2024-25' to 2024."""
    rows([_row(document_id="f1", filename="Brochure.pdf", bundle="page",
               title="Annual Reports 2024-25", parent_title="Annual Reports 2024-25"),
          _row(document_id="f2", filename="Flyer.pdf", bundle="page",
               title="Annual Reports 2024-25", parent_title="Annual Reports 2024-25")])
    moves = {m.document_id: m for m in attachment_moves(_shelf_page())}
    assert moves["f1"].new_start is None
    assert moves["f1"].source == "no_evidence"


def test_the_backfill_and_the_resolver_agree_on_the_same_file(rows):
    """The requirement stated as a test: one set of rules, two callers. If the
    script ever grew its own copy, this is what would catch it."""
    from types import SimpleNamespace

    from app.ingestion.date_resolution import build_evidence, resolve

    filename = "TERI-Annual-Report-2024-25.pdf"
    rows([_row(document_id="f1", filename=filename, bundle="page"),
          _row(document_id="f2", filename="TAR_2016-17.pdf", bundle="page")])
    by_script = attachment_moves(_shelf_page())[0]

    node = SimpleNamespace(uuid="p1", title="Annual Reports",
                           url="https://teriin.org/x",
                           created="2022-02-09T00:00:00+00:00", bundle="page",
                           metadata={}, files=[object(), object()])
    file = SimpleNamespace(uuid="f1", url="https://teriin.org/a.pdf",
                           filename=filename, description=None,
                           origin="attachment", created=None)
    by_ingestion = resolve(
        build_evidence(document_id="f1", node=node, file=file), content=None)

    assert by_script.new_start == by_ingestion.start_value
    assert by_script.start_precision == by_ingestion.start_precision
    assert by_script.source == by_ingestion.canonical_source


def test_a_name_on_a_ranged_page_keeps_the_period_it_says_nothing_about(rows):
    """The same range rule as the resolver: a year-precision name establishes no
    end, so the project's end is left alone."""
    rows([_row(document_id="f1", filename="Progress-Report-2021-22.pdf",
               bundle="completed_projects", parent_title="A project"),
          _row(document_id="f2", filename="Other.pdf",
               bundle="completed_projects", parent_title="A project")])
    moves = {m.document_id: m for m in attachment_moves(_project_page())}
    assert moves["f1"].new_start == "2022-01-01T00:00:00+00:00"
    assert moves["f1"].new_end == "2022-12-31T00:00:00+00:00"


def test_a_point_date_on_a_ranged_page_clears_the_period(rows):
    rows([_row(document_id="f1", filename="Minutes_14-03-2021.pdf",
               bundle="completed_projects", parent_title="A project"),
          _row(document_id="f2", filename="Other.pdf",
               bundle="completed_projects", parent_title="A project")])
    moves = {m.document_id: m for m in attachment_moves(_project_page())}
    assert moves["f1"].new_start == "2021-03-14T00:00:00+00:00"
    assert moves["f1"].new_end is None


# --------------------------------------------------------------------------- #
# Re-derived, not preserved
# --------------------------------------------------------------------------- #

def test_a_title_derived_row_is_re_resolved_not_skipped(rows):
    """The rule can move. A row already reading `document_title` is not evidence
    that today's rule would still produce that date — when the edition reading
    changed from the opening year of a span to the closing one, 55 documents
    needed exactly this path, and treating the source as "already decided" hid
    every one of them behind a dry run reporting two."""
    rows([_row(document_id="f1", filename="TERI-Annual-Report-2024-25.pdf",
               bundle="page", date_source="document_title",
               effective_start_date=None),
          _row(document_id="f2", filename="Other.pdf", bundle="page")])
    moves = {m.document_id: m for m in attachment_moves(_shelf_page())}
    assert "f1" in moves
    assert moves["f1"].new_start == "2025-01-01T00:00:00+00:00"


@pytest.mark.parametrize("source", ["document_text", "document_copyright"])
def test_a_text_derived_row_is_left_alone(rows, source):
    """These were read out of the PDF's bytes and this script has no bytes.
    Recomputing them from the naming would replace better evidence with worse."""
    rows([_row(document_id="f1", filename="TERI-Annual-Report-2024-25.pdf",
               bundle="page", date_source=source),
          _row(document_id="f2", filename="Other.pdf", bundle="page")])
    moves = {m.document_id: m for m in attachment_moves(_shelf_page())}
    assert "f1" not in moves


def test_an_already_undated_row_is_not_re_proposed_every_run(rows):
    """Idempotence. A NULL precision means `day` only where there is a date for
    it to describe; reading one in anyway made every undated row look different
    from an undated target forever, and a second dry run proposed 321 no-op
    writes."""
    rows([_row(document_id="f1", filename="Brochure.pdf", bundle="page",
               date_source="no_evidence", effective_start_date=None,
               start_precision=None),
          _row(document_id="f2", filename="Flyer.pdf", bundle="page",
               date_source="no_evidence", effective_start_date=None,
               start_precision=None)])
    assert attachment_moves(_shelf_page()) == []


def test_an_undated_row_that_should_now_have_a_date_still_moves(rows):
    """The other direction: re-deriving has to be able to *add* a date, or the
    idempotence guard would freeze the corpus."""
    rows([_row(document_id="f1", filename="TERI-Annual-Report-2024-25.pdf",
               bundle="page", date_source="no_evidence",
               effective_start_date=None, start_precision=None),
          _row(document_id="f2", filename="Flyer.pdf", bundle="page",
               date_source="no_evidence", effective_start_date=None,
               start_precision=None)])
    moves = {m.document_id: m for m in attachment_moves(_shelf_page())}
    assert moves["f1"].new_start == "2025-01-01T00:00:00+00:00"
    assert "f2" not in moves


def test_the_two_source_sets_do_not_overlap():
    """A source is either recomputed or preserved. Being in both would make the
    order of two `if`s decide the corpus."""
    from scripts.backfill_bundle_dates import (
        RECOMPUTED_SOURCES,
        TEXT_DERIVED_SOURCES,
    )

    assert RECOMPUTED_SOURCES & TEXT_DERIVED_SOURCES == frozenset()
