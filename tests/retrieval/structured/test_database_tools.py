"""Phase 2: the catalog tools. state.* and the name vocabularies are stubbed; no DB."""

from __future__ import annotations

import pytest

from app.catalog.models import StateRecord
from app.ingestion.extractors.drupal_extractor import DEFAULT_BUNDLES
from app.retrieval.structured import tools
from app.retrieval.structured.types import RecordFilters


def _rec(document_id="d1", title="A", url="http://a", effective_start_date="2024-05-01T00:00:00",
         bundle="news"):
    return StateRecord(
        document_id=document_id, source_type="website", source_key="k",
        fingerprint="f", title=title, url=url, effective_start_date=effective_start_date, bundle=bundle,
    )


def _theme_vocab(*names, group="main"):
    """Vocabulary rows. Pass ("Name", "other") tuples to vary the group."""
    rows = []
    for entry in names:
        name, grp = entry if isinstance(entry, tuple) else (entry, group)
        rows.append({"theme": name, "theme_type": "primary", "parent": None,
                     "theme_group": grp, "documents": 3})
    return rows


@pytest.fixture(autouse=True)
def _offline_catalog(monkeypatch):
    """Themes/tags/authors come from the catalog by name; stub them empty so no
    test reaches MySQL, and let individual cases override.

    The bundle inventory is stubbed to the full configured list so these tests
    describe tool logic rather than whichever content types the developer's local
    ingest happens to hold; `test_absent_bundle_*` overrides it."""
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    monkeypatch.setattr("app.catalog.queries.find_tag", lambda name: None)
    monkeypatch.setattr("app.catalog.queries.distinct_authors", lambda **kw: [])
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: DEFAULT_BUNDLES
    )
    from app.retrieval.structured import resolve

    resolve.reload_authors()
    yield
    resolve.reload_authors()


@pytest.fixture
def resolve_theme_ok(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Climate", "Climate Change", "Environment"),
    )


@pytest.fixture
def resolve_tag_ok(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.find_tag",
                        lambda name: "policy" if name.lower() == "policy" else None)


# --------------------------------------------------------------------------- #
# _scope_phrase / _applied_filters — the shared "state the interpretation"
# helpers used by count_records / list_records / aggregate_records.
# --------------------------------------------------------------------------- #

def test_scope_phrase_empty_when_nothing_set():
    assert tools._scope_phrase(RecordFilters()) == ""


def test_scope_phrase_names_author_theme_tag_and_period_in_order():
    filters = RecordFilters(
        author="Rishabh Negi", theme="Climate Change", tag="policy",
        date_from="2024-01-01", date_to="2025-01-01",
    )
    assert tools._scope_phrase(filters) == (
        " by Rishabh Negi on 'Climate Change' tagged 'policy' in 2024"
    )


def test_scope_phrase_author_only():
    assert tools._scope_phrase(RecordFilters(author="Rishabh Negi")) == " by Rishabh Negi"


def test_scope_phrase_names_the_title_filter():
    assert tools._scope_phrase(RecordFilters(title_contains="Solar")) == (
        " with 'Solar' in the title"
    )


def test_scope_phrase_covers_every_filter_that_applied_filters_echoes():
    """The prose and the structured echo must name the same filter set — a
    filter stated in one but not the other is either an unexplained number or
    an unexplained key. Dates are compared as a period rather than verbatim: a
    whole calendar year deliberately reads "in 2024", and the prose names the
    last day the range covers rather than the exclusive bound."""
    filters = RecordFilters(
        author="Rishabh Negi", theme="Climate Change", tag="policy",
        title_contains="Solar", date_from="2024-03-15", date_to="2024-06-01",
    )
    phrase = tools._scope_phrase(filters)
    applied = tools._applied_filters(None, filters)
    assert set(applied) == {
        "author", "theme", "tag", "title_contains", "date_from", "date_to",
    }
    for key, value in applied.items():
        expected = "2024-05-31" if key == "date_to" else value
        assert expected in phrase, f"{key} echoed in data but absent from the prose"


def test_scope_phrase_names_the_last_day_covered_not_the_exclusive_bound():
    """Echoing the raw bound claimed a day the query excludes."""
    filters = RecordFilters(date_from="2020-01-01", date_to="2022-01-01")
    assert tools._scope_phrase(filters) == " between 2020-01-01 and 2021-12-31"


def test_scope_phrase_collapses_a_single_day():
    filters = RecordFilters(date_from="2024-03-15", date_to="2024-03-16")
    assert tools._scope_phrase(filters) == " on 2024-03-15"


def test_scope_phrase_states_a_collapsed_calendar_year():
    filters = RecordFilters(date_from="2024-01-01", date_to="2025-01-01")
    assert tools._scope_phrase(filters) == " in 2024"
    assert set(tools._applied_filters(None, filters)) == {"date_from", "date_to"}


def test_applied_filters_omits_unset_values():
    assert tools._applied_filters("news", RecordFilters()) == {"entity": "news"}
    assert tools._applied_filters(None, RecordFilters()) == {}


def test_applied_filters_includes_every_set_value():
    filters = RecordFilters(
        author="Rishabh Negi", theme="Climate Change", tag="policy",
        date_from="2024-01-01", date_to="2025-01-01",
    )
    assert tools._applied_filters("events", filters) == {
        "entity": "events", "author": "Rishabh Negi", "theme": "Climate Change",
        "tag": "policy", "date_from": "2024-01-01", "date_to": "2025-01-01",
    }


# --------------------------------------------------------------------------- #
# count_records
# --------------------------------------------------------------------------- #

def test_count_records_renders_and_passes_scope(monkeypatch, resolve_theme_ok):
    seen = {}

    def fake_count(**kw):
        seen.update(kw)
        return 3

    monkeypatch.setattr("app.catalog.queries.count_documents", fake_count)
    r = tools.count_records(
        "news", RecordFilters(theme="Climate", date_from="2024-01-01", date_to="2025-01-01")
    )
    assert r.ok
    assert r.data == {
        "count": 3,
        "applied": {"entity": "news", "theme": "Climate",
                   "date_from": "2024-01-01", "date_to": "2025-01-01"},
    }
    assert r.rendered == "There are 3 news items on 'Climate' in 2024 matching your query."
    assert seen["bundle"] == "news"
    assert seen["source_type"] == "website" and seen["entity_type"] == "node"
    assert seen["theme"] == "Climate"


def test_count_records_unknown_entity_is_not_ok(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 0)
    r = tools.count_records("tenders", RecordFilters())
    assert r.ok is False and "unknown entity" in (r.error or "")


def test_count_records_unknown_entity_stays_non_terminal(monkeypatch):
    """An unrecognized type must keep falling through to semantic search — only
    a genuinely ambiguous one is terminal."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 0)
    assert tools.count_records("tenders", RecordFilters()).error_kind is None


# --------------------------------------------------------------------------- #
# Ambiguous content type — a word naming several bundles asks instead of picking.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "tool, call",
    [
        ("count_records", lambda: tools.count_records("projects", RecordFilters())),
        ("list_records", lambda: tools.list_records("projects", RecordFilters())),
        ("aggregate_records",
         lambda: tools.aggregate_records("projects", "theme", RecordFilters())),
        ("lookup_record",
         lambda: tools.lookup_record("projects", "anything", RecordFilters())),
    ],
)
def test_ambiguous_content_type_asks_which_instead_of_querying(tool, call, monkeypatch):
    """"projects" spans completed_projects and ongoing_projects. Picking one
    reported "0 ongoing projects" while 918 completed ones existed; dropping the
    type instead counted articles and papers as projects."""
    def boom(**kw):  # the guard must fire before any query runs
        raise AssertionError("queried despite an ambiguous content type")

    for name in ("count_documents", "list_documents", "distribution"):
        monkeypatch.setattr(f"app.catalog.queries.{name}", boom)

    r = call()
    assert r.ok is False
    assert r.error_kind == "ambiguous_entity"  # terminal even with the flag off
    assert "matches more than one content type" in r.rendered
    assert "completed projects" in r.rendered and "ongoing projects" in r.rendered
    assert r.rendered.endswith("Which did you mean?")


def test_ambiguous_content_type_offers_readable_labels():
    """The options are what a user would type back, not raw bundle keys."""
    r = tools.count_records("projects", RecordFilters())
    assert "1. completed projects" in r.rendered
    assert "completed_projects" not in r.rendered


def test_naming_one_project_type_is_not_ambiguous(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 918)
    r = tools.count_records("completed_projects", RecordFilters())
    assert r.ok and r.error_kind is None
    assert r.rendered == "There are 918 completed projects matching your query."


def test_lookup_record_preserves_a_terminal_error_kind(monkeypatch):
    """lookup_record delegates to list_records; dropping error_kind on the way
    back turned a clarification into a plain no-answer that fell through to
    semantic search — a guess at the very question it had asked about."""
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [])
    r = tools.lookup_record("projects", "anything", RecordFilters())
    assert r.error_kind == "ambiguous_entity"


def test_count_records_unresolved_theme_still_counts_via_name_fallback(monkeypatch):
    """A theme name the vocabulary could not place still filters — matching being
    unsure is not proof of absence, so the query runs and may well find rows."""
    seen = {}

    def fake_count(**kw):
        seen.update(kw)
        return 99

    monkeypatch.setattr("app.catalog.queries.count_documents", fake_count)
    r = tools.count_records("news", RecordFilters(theme="Environment"))
    assert r.ok is True
    assert seen["theme"] == "Environment"  # filtered by name
    assert r.data["count"] == 99


def test_count_records_unresolved_theme_with_no_rows_is_a_terminal_miss(monkeypatch):
    """Only when the fallback also finds nothing are "unknown theme" and
    "genuinely no documents" indistinguishable — then the miss is the honest
    answer, not a bare 0."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 0)
    r = tools.count_records("news", RecordFilters(theme="Quantum Beekeeping"))
    assert r.ok is False and r.error_kind == "unresolved"
    assert r.rendered == "No theme matching 'Quantum Beekeeping' found."


def test_count_records_resolved_theme_with_no_rows_is_an_honest_zero(monkeypatch):
    """A theme that DID resolve and matched nothing is a real zero — never
    relabelled as a miss."""
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Climate Change"),
    )
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 0)
    r = tools.count_records("news", RecordFilters(theme="Climate Change"))
    assert r.ok is True and r.data["count"] == 0
    assert r.rendered == "There are no news items on 'Climate Change' matching your query."


@pytest.fixture
def only_articles(monkeypatch):
    """A catalog that ingested articles and pages but none of the other
    configured content types — the shape that produced "0 reports"."""
    monkeypatch.setattr(
        "app.catalog.queries.available_bundles", lambda **kw: ("article", "page")
    )


def test_absent_bundle_falls_through_instead_of_counting_zero(monkeypatch, only_articles):
    """`report` is configured but has no rows here. Answering "0 reports" states
    a fact about the corpus when the truth is about the vocabulary, so the tool
    declines and lets semantic search answer."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 0)
    r = tools.count_records("report", RecordFilters())
    assert r.ok is False
    assert "no 'report' content in this catalog" in r.error
    # Not terminal: error_kind is what stops the fall-through to semantic search.
    assert r.error_kind is None


def test_present_bundle_with_no_matches_is_still_an_honest_zero(monkeypatch, only_articles):
    """The bundle exists, so zero is a real answer about the filters."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 0)
    r = tools.count_records("article", RecordFilters(date_from="2019-01-01"))
    assert r.ok is True and r.data["count"] == 0


def test_absent_bundle_that_still_matched_rows_is_reported_normally(
    monkeypatch, only_articles
):
    """The inventory is consulted only when the result is empty — a stale
    inventory must never suppress rows the query actually found."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 3)
    r = tools.count_records("report", RecordFilters())
    assert r.ok is True and r.data["count"] == 3


def test_unknown_inventory_keeps_the_previous_behaviour(monkeypatch):
    """An unreachable catalog returns no inventory; that must read as "cannot
    tell", not as "every content type is empty"."""
    monkeypatch.setattr("app.catalog.queries.available_bundles", lambda **kw: ())
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 0)
    r = tools.count_records("report", RecordFilters())
    assert r.ok is True and r.data["count"] == 0


def test_count_records_passes_tag_scope(monkeypatch, resolve_tag_ok):
    seen = {}

    def fake_count(**kw):
        seen.update(kw)
        return 4

    monkeypatch.setattr("app.catalog.queries.count_documents", fake_count)
    r = tools.count_records("news", RecordFilters(tag="policy"))
    assert r.ok
    assert r.data == {"count": 4, "applied": {"entity": "news", "tag": "policy"}}
    assert r.rendered == "There are 4 news items tagged 'policy' matching your query."
    assert seen["tag"] == "policy"


def test_count_records_unmatched_tag_with_no_rows_is_a_terminal_miss(monkeypatch):
    """Tags are matched exactly, so an unmatched name finds nothing — and an
    empty result on an unmatched name is the honest miss."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 0)
    r = tools.count_records("news", RecordFilters(tag="nonexistent"))
    assert r.ok is False and r.error_kind == "unresolved"
    assert r.rendered == "No tag matching 'nonexistent' found."


def test_count_records_singular_verb(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 1)
    r = tools.count_records("report", RecordFilters())
    assert r.rendered == "There is 1 report matching your query."


def test_count_records_bare_total(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 123)
    r = tools.count_records(None, RecordFilters())
    assert r.rendered == "There are 123 items matching your query."


def test_count_records_date_range_render(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 2)
    r = tools.count_records(
        None, RecordFilters(date_from="2024-03-15", date_to="2024-04-01")
    )
    assert r.rendered == (
        "There are 2 items between 2024-03-15 and 2024-03-31 matching your query."
    )


def test_count_records_applies_the_title_filter(monkeypatch):
    """count_documents did not accept title_contains at all, so a title-filtered
    count silently returned the whole corpus while list_records filtered
    correctly — the two disagreed on the same query."""
    seen = {}
    monkeypatch.setattr(
        "app.catalog.queries.count_documents", lambda **kw: seen.update(kw) or 3
    )
    r = tools.count_records("report", RecordFilters(title_contains="Solar"))
    assert seen["title_contains"] == "Solar"
    assert r.rendered == "There are 3 reports with 'Solar' in the title matching your query."


def test_zero_under_a_guessed_title_falls_through(monkeypatch):
    """`title_contains` only matches the title column, so a zero under a subject
    the intent layer put there says nothing about the body text. Reporting it as 0
    would tell the user the corpus is silent on their topic; fall through to
    semantic search instead."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 0)
    r = tools.count_records(
        "report", RecordFilters(title_contains="quantum teleportation"),
        question="how many reports about quantum teleportation?",
    )
    assert not r.ok
    assert r.error_kind is None  # falls through; not a terminal answer
    assert not r.rendered


def test_zero_is_honest_when_the_question_is_about_titles(monkeypatch):
    """Asked about titles, a title-scoped zero is exactly the answer wanted —
    prose from semantic search would be worse."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 0)
    for question in (
        "how many reports are titled 'Solar'?",
        'how many reports are called "Solar"?',
    ):
        r = tools.count_records(
            "report", RecordFilters(title_contains="Solar"), question=question
        )
        assert r.ok, question
        assert r.rendered == (
            "There are no reports with 'Solar' in the title matching your query."
        )


def test_zero_without_a_title_filter_stays_an_honest_zero(monkeypatch):
    """The catalog is authoritative for a date- or bundle-scoped count: 0 there is
    a fact about the corpus, and falling through would answer a counting question
    from prose."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 0)
    r = tools.count_records(
        "report", RecordFilters(date_from="2023-01-01", date_to="2024-01-01"),
        question="how many reports in 2023?",
    )
    assert r.ok
    assert r.rendered == "There are no reports in 2023 matching your query."


def test_nonzero_title_count_is_unaffected_by_the_guard(monkeypatch):
    """The guard reads only the empty case; a real count answers as before even
    for a guessed title."""
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **kw: 3)
    r = tools.count_records(
        "report", RecordFilters(title_contains="Solar"),
        question="how many reports about solar?",
    )
    assert r.ok
    assert r.rendered == "There are 3 reports with 'Solar' in the title matching your query."


def test_count_and_list_pass_the_same_filter_set(monkeypatch):
    """Regression guard: whatever narrows a listing must narrow its count too."""
    counted, listed = {}, {}
    monkeypatch.setattr(
        "app.catalog.queries.count_documents", lambda **kw: counted.update(kw) or 1
    )
    monkeypatch.setattr(
        "app.catalog.queries.list_documents", lambda **kw: listed.update(kw) or [_rec()]
    )
    filters = RecordFilters(author="Sharma", title_contains="Solar",
                            date_from="2024-01-01", date_to="2025-01-01")
    tools.count_records("report", filters)
    tools.list_records("report", filters)
    # list_documents additionally takes paging keys; every filter must match.
    for key, value in counted.items():
        assert listed[key] == value, f"{key} differs between count and list"


def test_count_records_names_author_in_rendered_answer(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 21)
    r = tools.count_records(None, RecordFilters(author="Dr Suneel Pandey"))
    assert r.rendered == "There are 21 items by Dr Suneel Pandey matching your query."
    assert r.data["applied"] == {"author": "Dr Suneel Pandey"}


def test_count_records_combines_author_theme_tag_and_period(monkeypatch, resolve_theme_ok):
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary",
                        lambda name, vocabulary=None: [{"term_uuid": "u1", "name": name}])
    monkeypatch.setattr("app.catalog.queries.count_documents", lambda **k: 3)
    r = tools.count_records(
        "news",
        RecordFilters(author="Rishabh Negi", theme="Climate Change", tag="policy",
                     date_from="2024-01-01", date_to="2025-01-01"),
    )
    assert r.rendered == (
        "There are 3 news items by Rishabh Negi on 'Climate Change' tagged 'policy' "
        "in 2024 matching your query."
    )
    assert r.data["applied"] == {
        "entity": "news", "author": "Rishabh Negi", "theme": "Climate Change",
        "tag": "policy", "date_from": "2024-01-01", "date_to": "2025-01-01",
    }


def test_count_records_db_error_is_not_ok(monkeypatch):
    def boom(**k):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.catalog.queries.count_documents", boom)
    assert tools.count_records("news", RecordFilters()).ok is False


# --------------------------------------------------------------------------- #
# list_records
# --------------------------------------------------------------------------- #

def test_list_records_plain(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.list_records("news", RecordFilters())
    assert r.ok
    assert r.data["records"][0]["document_id"] == "d1"
    assert r.citations[0]["title"] == "A"
    assert r.rendered == "Found 1 news item:\n- [A](http://a) — 1 May 2024"
    assert r.data["applied"] == {"entity": "news"}


def test_list_records_applied_names_author(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.list_records("news", RecordFilters(author="Rishabh Negi"))
    assert r.data["applied"] == {"entity": "news", "author": "Rishabh Negi"}
    # the record list itself is the evidence; the prose names the same scope
    assert r.rendered == "Found 1 news item by Rishabh Negi:\n- [A](http://a) — 1 May 2024"


def test_list_records_table(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.list_records("news", RecordFilters(), output_format="table")
    assert "| Title | Published | Type |" in r.rendered


def test_list_records_empty_is_not_ok(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [])
    r = tools.list_records("news", RecordFilters())
    assert r.ok is False


def test_list_records_passes_tag_scope(monkeypatch, resolve_tag_ok):
    seen = {}

    def fake_list(**kw):
        seen.update(kw)
        return [_rec()]

    monkeypatch.setattr("app.catalog.queries.list_documents", fake_list)
    r = tools.list_records("news", RecordFilters(tag="policy"))
    assert r.ok
    assert seen["tag"] == "policy"


def test_list_records_unmatched_tag_with_no_rows_is_a_terminal_miss(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [])
    r = tools.list_records("news", RecordFilters(tag="nonexistent"))
    assert r.ok is False and r.error_kind == "unresolved"


def test_list_records_passes_offset(monkeypatch):
    seen = {}

    def fake_list(**kw):
        seen.update(kw)
        return [_rec()]

    monkeypatch.setattr("app.catalog.queries.list_documents", fake_list)
    tools.list_records("news", RecordFilters(), limit=5, offset=15)
    assert seen["limit"] == 5 and seen["offset"] == 15


def test_list_records_projects_requested_fields(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.list_records("news", RecordFilters(), fields=["title", "url"])
    assert r.data["records"] == [{"title": "A", "url": "http://a"}]
    # rendered stays the normal human-readable answer, unaffected by fields
    assert r.rendered == "Found 1 news item:\n- [A](http://a) — 1 May 2024"


def test_list_records_unknown_fields_do_not_empty_the_records(monkeypatch):
    """An LLM-supplied field name that does not exist should cost the caller a
    few extra keys, not silently blank every record."""
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.list_records("news", RecordFilters(), fields=["nope", "alsonope"])
    assert r.data["records"][0]["title"] == "A"  # full record, not {}


def test_list_records_mixed_known_and_unknown_fields_keeps_the_known_ones(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.list_records("news", RecordFilters(), fields=["title", "nope"])
    assert r.data["records"] == [{"title": "A"}]


def test_list_records_without_fields_keeps_full_metadata(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.list_records("news", RecordFilters())
    assert set(r.data["records"][0]) == {
        "document_id", "title", "url", "effective_start_date", "bundle",
    }


def test_list_records_timeline_groups_by_year(monkeypatch):
    recs = [
        _rec("d1", "Old", "http://old", "2023-11-02T00:00:00"),
        _rec("d2", "New", "http://new", "2024-05-20T00:00:00"),
        _rec("d3", "NoDate", None, None),
    ]
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: recs)
    r = tools.list_records("news", RecordFilters(), output_format="timeline")
    a = r.rendered
    assert a.index("2024:") < a.index("2023:") < a.index("Undated:")
    assert "- 20 May 2024: [New](http://new)" in a
    assert "- n.d.: NoDate" in a
    # citations follow the rendered (newest-first) order
    assert [c["title"] for c in r.citations] == ["New", "Old", "NoDate"]


# --------------------------------------------------------------------------- #
# lookup_record
# --------------------------------------------------------------------------- #

def test_lookup_record_chains_on_content_question(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.lookup_record(
        "report", "Solar Report", RecordFilters(),
        question="what does the Solar Report say about capacity?",
    )
    assert r.data["chain_document_id"] == "d1"
    assert r.ok  # also renders the list
    # inherited from list_records's data via the {**result.data, ...} spread
    assert r.data["applied"] == {"entity": "report", "title_contains": "Solar Report"}


def test_lookup_record_no_chain_on_browse(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.list_documents", lambda **k: [_rec()])
    r = tools.lookup_record(
        "report", "Solar Report", RecordFilters(), question="show the Solar Report",
    )
    assert r.data["chain_document_id"] is None


def test_lookup_record_no_chain_when_multiple_match(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.list_documents",
        lambda **k: [_rec("d1"), _rec("d2")],
    )
    r = tools.lookup_record(
        "report", "Report", RecordFilters(), question="what does the report say?",
    )
    assert r.data["chain_document_id"] is None  # ambiguous -> no chain


# --------------------------------------------------------------------------- #
# aggregate_records
# --------------------------------------------------------------------------- #

def test_list_themes_renders_vocabulary(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [
            {"theme": "Climate Change", "theme_type": "primary", "parent": None, "theme_group": "main", "documents": 3},
            {"theme": "Energy", "theme_type": "primary", "parent": None, "theme_group": "main", "documents": 3},
        ],
    )
    r = tools.list_themes()
    assert r.ok
    assert r.data["themes"] == ["Climate Change", "Energy"]
    assert "Climate Change" in r.rendered and "Energy" in r.rendered


def test_list_themes_reports_the_whole_vocabulary_not_a_page(monkeypatch):
    """The brief's "how many themes are there?" — the rendered total is a factual
    claim, so it must never be a truncated page of the vocabulary."""
    vocabulary = _theme_vocab(*[f"Theme {i}" for i in range(30)])
    seen = {}

    def fake_vocabulary(**kw):
        seen.update(kw)
        return vocabulary[: kw.get("limit", tools.THEME_VOCABULARY_LIMIT)]

    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", fake_vocabulary)
    r = tools.list_themes()
    assert seen["limit"] == tools.THEME_VOCABULARY_LIMIT
    assert len(r.data["themes"]) == 30
    assert r.rendered.startswith("The collection covers 30 main themes:")



def test_list_themes_empty_falls_through(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: [])
    r = tools.list_themes()
    assert r.ok is False and r.tool == "list_themes"


def _mixed_vocab():
    """Two primary themes (one per group) plus sub-themes hanging off them."""
    return [
        {"theme": "Energy", "theme_type": "primary", "parent": None,
         "theme_group": "main", "documents": 227},
        {"theme": "Energy Access", "theme_type": "sub", "parent": "Energy",
         "theme_group": "main", "documents": 13},
        {"theme": "Energy Efficiency", "theme_type": "sub", "parent": "Energy",
         "theme_group": "main", "documents": 13},
        {"theme": "Green Shipping", "theme_type": "primary", "parent": None,
         "theme_group": "other", "documents": 2},
    ]


def test_list_themes_excludes_sub_themes_from_the_default_listing(monkeypatch):
    """"How many themes are there?" counts top-level themes. Including
    sub-themes both overstates the total and flattens the hierarchy."""
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: _mixed_vocab())
    r = tools.list_themes()
    assert r.data["themes"] == ["Energy"]
    assert r.data["main_themes"] == ["Energy"]
    assert r.data["other_themes"] == []
    assert r.rendered.startswith("The collection covers 1 main theme:")
    assert "Energy Access" not in r.rendered
    assert "Green Shipping" not in r.rendered


def test_list_themes_never_lists_sub_theme_rows_even_as_a_table(monkeypatch):
    """Every shape lists themes only — no sub-theme column, no nesting."""
    monkeypatch.setattr("app.catalog.queries.theme_vocabulary", lambda **kw: _mixed_vocab())
    r = tools.list_themes(scope="all", output_format="table")
    assert r.data["themes"] == ["Energy", "Green Shipping"]
    assert "sub_themes" not in r.data and "by_parent" not in r.data
    assert "sub-theme" not in r.rendered and "Energy Access" not in r.rendered


def test_list_themes_no_primary_themes_falls_through(monkeypatch):
    """A vocabulary of only sub-themes cannot answer "what themes are there"."""
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: [{"theme": "Air", "theme_type": "sub", "parent": "Environment",
                       "theme_group": "main", "documents": 1}],
    )
    assert tools.list_themes().ok is False


def test_list_themes_splits_main_and_other(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Energy", ("Green Shipping", "other")),
    )
    r = tools.list_themes(scope="all")
    assert r.data["main_themes"] == ["Energy"]
    assert r.data["other_themes"] == ["Green Shipping"]
    assert r.rendered.index("Main themes:") < r.rendered.index("Other themes:")
    assert "- Energy" in r.rendered and "- Green Shipping" in r.rendered


def test_a_generic_listing_never_exposes_other_themes(monkeypatch):
    """The requirement in one test: the default listing is the curated
    thematic structure, not an inventory of every term the CMS holds."""
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Energy", ("Green Shipping", "other")),
    )
    r = tools.list_themes()
    assert r.data["themes"] == ["Energy"]
    assert "Green Shipping" not in r.rendered
    assert "Other themes" not in r.rendered


def test_an_explicit_other_request_returns_only_other_themes(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Energy", ("Green Shipping", "other")),
    )
    r = tools.list_themes(scope="other")
    assert r.data["themes"] == ["Green Shipping"]
    assert r.data["main_themes"] == []
    assert "Energy" not in r.rendered
    assert r.rendered.startswith("The collection covers 1 other theme:")


def test_asking_for_other_themes_when_there_are_none_falls_through(monkeypatch):
    """Better to fall through to semantic search than render a heading over
    an empty list."""
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary", lambda **kw: _theme_vocab("Energy"),
    )
    r = tools.list_themes(scope="other")
    assert r.ok is False and r.error == "no other themes found"


def test_an_unknown_scope_falls_back_to_main(monkeypatch):
    """Fail safe: an unrecognised scope must not widen the answer."""
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Energy", ("Green Shipping", "other")),
    )
    assert tools.list_themes(scope="everything").data["themes"] == ["Energy"]


def test_an_ungrouped_theme_is_not_presented_as_an_other_theme(monkeypatch):
    """`theme_group` is NULL for a theme discovered in Drupal that the theme
    map does not define. Filing it under Other would present a term nobody
    curated as part of a curated structure. Testing for inequality against
    the main group is how that happens, since NULL is not equal to it.

    It is not dropped: `scope="all"` reports it, labelled for what it is.
    """
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Energy", ("Quantum Beekeeping", None)),
    )
    assert "Quantum Beekeeping" not in tools.list_themes().rendered
    assert tools.list_themes(scope="other").ok is False

    everything = tools.list_themes(scope="all")
    assert everything.data["other_themes"] == []
    assert "Unclassified themes:\n- Quantum Beekeeping" in everything.rendered



def test_list_themes_table_format_has_two_labelled_sections(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.theme_vocabulary",
        lambda **kw: _theme_vocab("Energy", ("Green Shipping", "other")),
    )
    r = tools.list_themes(scope="all", output_format="table")
    assert "**Main themes**" in r.rendered and "**Other themes**" in r.rendered
    assert "| Energy |" in r.rendered and "| Green Shipping |" in r.rendered



def test_aggregate_records_table_and_dimension(monkeypatch):
    seen = {}

    def fake_dist(group_by, **kw):
        seen["dimension"] = group_by
        return [("Climate", 5), ("Energy", 3)]

    monkeypatch.setattr("app.catalog.queries.distribution", fake_dist)
    r = tools.aggregate_records(None, "theme", RecordFilters(), output_format="table")
    assert r.ok
    assert r.data["groups"] == [["Climate", 5], ["Energy", 3]]
    assert seen["dimension"] == "theme"  # theme -> theme facet
    assert "| theme | count |" in r.rendered


def test_aggregate_records_plain(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.queries.distribution", lambda group_by, **k: [("Climate", 5)]
    )
    r = tools.aggregate_records(None, "theme", RecordFilters())
    assert r.rendered == "Distribution of items by theme:\n- Climate: 5"


def test_aggregate_records_passes_tag_scope(monkeypatch, resolve_tag_ok):
    seen = {}

    def fake_dist(group_by, **kw):
        seen.update(kw)
        return [("Climate", 5)]

    monkeypatch.setattr("app.catalog.queries.distribution", fake_dist)
    r = tools.aggregate_records(None, "theme", RecordFilters(tag="policy"))
    assert r.ok
    assert seen["tag"] == "policy"


def test_aggregate_records_unmatched_tag_with_no_rows_is_a_terminal_miss(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.distribution", lambda group_by, **k: [])
    r = tools.aggregate_records(None, "theme", RecordFilters(tag="nonexistent"))
    assert r.ok is False and r.error_kind == "unresolved"


def test_aggregate_records_content_type_maps_to_bundle(monkeypatch):
    seen = {}

    def fake_dist(group_by, **kw):
        seen["dimension"] = group_by
        return [("news", 10)]

    monkeypatch.setattr("app.catalog.queries.distribution", fake_dist)
    tools.aggregate_records(None, "content_type", RecordFilters())
    assert seen["dimension"] == "bundle"


def test_aggregate_records_empty_is_not_ok(monkeypatch):
    monkeypatch.setattr("app.catalog.queries.distribution", lambda group_by, **k: [])
    r = tools.aggregate_records("news", "year", RecordFilters())
    assert r.ok is False


# --------------------------------------------------------------------------- #
# resolve_entity — the tools.py wrapper around app.retrieval.structured.resolve.
# Candidate ranking/banding itself is covered by test_entity_resolution.py and
# test_entity_resolution_scoring.py; these tests cover the ToolResult shaping.
# --------------------------------------------------------------------------- #

def _cand(name, type_="author", score=1.0, id_=None):
    from app.retrieval.structured.resolve import EntityCandidate

    return EntityCandidate(id=id_ or name, canonical_name=name, type=type_, score=score)


def test_resolve_entity_accept_returns_resolved_candidate(monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.structured.resolve.resolve_entity",
        lambda query, type=None, **kw: [_cand("Rishabh Negi", score=0.957),
                                         _cand("Rishab Nigam", score=0.783)],
    )
    r = tools.resolve_entity("rishab negi", type="author")
    assert r.ok is True
    assert r.error_kind is None
    assert r.data["resolved"] == {
        "id": "Rishabh Negi", "canonical_name": "Rishabh Negi",
        "type": "author", "score": 0.957,
    }
    assert len(r.data["candidates"]) == 2
    assert r.rendered == "'rishab negi' resolves to Rishabh Negi (author)."


def test_resolve_entity_ambiguous_lists_candidates_and_asks(monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.structured.resolve.resolve_entity",
        lambda query, type=None, **kw: [_cand("Rishabh Negi", score=0.75),
                                         _cand("Rishab Nigam", score=0.75)],
    )
    r = tools.resolve_entity("rishab", type="author")
    assert r.ok is False
    assert r.error_kind == "ambiguous"
    assert "resolved" not in r.data
    assert r.rendered == (
        "'rishab' matches more than one author:\n"
        "1. Rishabh Negi\n"
        "2. Rishab Nigam\n"
        "Which did you mean?"
    )


def test_resolve_entity_ambiguous_caps_display_at_three(monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.structured.resolve.resolve_entity",
        lambda query, type=None, **kw: [
            _cand("A", score=0.75), _cand("B", score=0.74),
            _cand("C", score=0.73), _cand("D", score=0.72),
        ],
    )
    r = tools.resolve_entity("x", type="author")
    assert r.rendered.count("\n") == 4  # 3 numbered options + the closing question
    assert "D" not in r.rendered
    assert len(r.data["candidates"]) == 4  # the full ranking is still in data


def test_resolve_entity_no_candidates_is_terminal_miss(monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.structured.resolve.resolve_entity", lambda query, type=None, **kw: []
    )
    r = tools.resolve_entity("zzznonexistent", type="theme")
    assert r.ok is False
    assert r.error_kind == "unresolved"
    assert r.rendered == "No theme matching 'zzznonexistent' found."


def test_resolve_entity_low_score_is_terminal_miss(monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.structured.resolve.resolve_entity",
        lambda query, type=None, **kw: [_cand("Rishabh Negi", score=0.31)],
    )
    r = tools.resolve_entity("zzznonexistent", type="author")
    assert r.ok is False and r.error_kind == "unresolved"


def test_resolve_entity_unadvertised_type_is_reported_not_raised(monkeypatch):
    def boom(query, type=None, **kw):
        raise ValueError("bad type")

    monkeypatch.setattr("app.retrieval.structured.resolve.resolve_entity", boom)
    r = tools.resolve_entity("policy", type="tag")
    assert r.ok is False
    assert "bad type" in r.error


def test_resolve_entity_query_failure_is_not_ok(monkeypatch):
    def boom(query, type=None, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.retrieval.structured.resolve.resolve_entity", boom)
    r = tools.resolve_entity("rishabh", type="author")
    assert r.ok is False and r.error == "query failed"


def test_resolve_entity_no_type_labels_as_generic_entity(monkeypatch):
    monkeypatch.setattr(
        "app.retrieval.structured.resolve.resolve_entity", lambda query, type=None, **kw: []
    )
    r = tools.resolve_entity("zzznonexistent")
    assert r.rendered == "No entity matching 'zzznonexistent' found."


@pytest.mark.parametrize("query", [None, "", "   "])
def test_resolve_entity_missing_query_falls_through_without_rendering_it(query):
    """`ToolCall.query` defaults to None, so a planned call that omits it lands
    here — it must not render the missing value into the answer ("No entity
    matching 'None' found."). A planner that forgot the query is a bug with
    nothing useful to tell the user, so this falls through rather than being
    terminal."""
    r = tools.resolve_entity(query, "author")
    assert r.ok is False
    assert r.rendered == ""
    assert r.error_kind is None  # fall through, not a terminal answer
    assert r.error == "no query to resolve"
