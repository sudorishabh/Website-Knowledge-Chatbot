"""scripts.backfill_author_bylines — re-reading stored bylines, conservatively.

The raw_meta values are copied from the live catalog (2026-10-04)."""
from __future__ import annotations

from types import SimpleNamespace

from scripts import backfill_author_bylines as bab


def _plan(meta, stored, attachments=None, document_id="d1"):
    return bab.plan([(document_id, meta)], stored, attachments or {})


def test_a_split_byline_is_rejoined():
    [fix] = _plan({"field_rpaper_authors": "Sehgal, Meena"}, {"d1": {"Sehgal", "Meena"}})
    assert fix.new == ["Sehgal Meena"]
    assert fix.old == ["Meena", "Sehgal"]
    assert fix.field == "field_rpaper_authors"


def test_a_joined_byline_is_split():
    [fix] = _plan({"field_rpaper_authors": "Dhup Saumya and Dhawan Vibha"},
                  {"d1": {"Dhup Saumya and Dhawan Vibha"}})
    assert fix.new == ["Dhup Saumya", "Dhawan Vibha"]


def test_only_the_field_the_rows_came_from_is_reread():
    """The stored authors came from the people references; the byline beside
    them is not this fix's to adopt."""
    meta = {
        "field_rpaper_author": ["Dr Malini Balakrishnan"],
        "field_rpaper_authors": "Alharthi A I, Balakrishnan M",
    }
    assert _plan(meta, {"d1": {"Dr Malini Balakrishnan"}}) == []


def test_a_document_never_switches_field():
    """raw_meta keys come back sorted, so the field ingestion picked is not
    the first one here — and must not become it."""
    meta = {
        "field_external_authors": ["Ms Madhur Bhargava"],
        "field_policybrief_authors": ["Mr Manjeet Singh"],
    }
    assert _plan(meta, {"d1": {"Mr Manjeet Singh"}}) == []


def test_rows_from_no_known_field_are_left_alone():
    assert _plan({"field_rpaper_authors": "Sehgal, Meena"}, {"d1": {"Someone Else"}}) == []


def test_list_items_are_tidied():
    [fix] = _plan({"field_report_authors_external": ["David Palchak |", "Vinayak Narwade"]},
                  {"d1": {"David Palchak |", "Vinayak Narwade"}})
    assert fix.new == ["David Palchak", "Vinayak Narwade"]


def test_an_address_only_field_leaves_no_authors():
    [fix] = _plan({"field_authors": ["reetas@teri.res.in"]}, {"d1": {"reetas@teri.res.in"}})
    assert fix.new == []


def test_an_address_only_field_gives_way_to_the_byline():
    """As ingestion now does: the references hold only an account email, the
    byline beside them names the people."""
    meta = {"field_rpaper_author": ["reetas@teri.res.in"],
            "field_rpaper_authors": "Sharma Reeta,  Shekar Alpana C"}
    [fix] = _plan(meta, {"d1": {"reetas@teri.res.in"}})
    assert fix.new == ["Sharma Reeta", "Shekar Alpana C"]
    assert fix.field == "field_rpaper_author -> field_rpaper_authors"


def test_no_fallback_when_two_other_fields_disagree():
    meta = {"field_authors": ["x@teri.res.in"],
            "field_external_authors": ["A B"], "field_rpaper_authors": "C D"}
    [fix] = _plan(meta, {"d1": {"x@teri.res.in"}})
    assert fix.new == []


def test_an_attachment_follows_its_page():
    stored = {"d1": {"Sehgal", "Meena"}, "pdf1": {"Sehgal", "Meena"},
              "pdf2": {"Someone Else"}}
    fixes = _plan({"field_rpaper_authors": "Sehgal, Meena"}, stored,
                  attachments={"d1": ["pdf1", "pdf2"]})
    assert [(f.document_id, f.parent, f.new) for f in fixes] == [
        ("d1", None, ["Sehgal Meena"]),
        ("pdf1", "d1", ["Sehgal Meena"]),
    ]


def test_old_reading_is_the_comma_split():
    assert bab.old_reading("Ghosh, S., & Sharma, J. V.") == ["Ghosh", "S.", "& Sharma", "J. V."]
    assert bab.old_reading([" A ", "", "B"]) == ["A", "B"]


# --------------------------------------------------------------------------- #
# The Qdrant rewrite.
# --------------------------------------------------------------------------- #

class _Client:
    def __init__(self, points):
        self.points = points
        self.calls = []

    def count(self, **kw):
        return SimpleNamespace(count=self.points)

    def set_payload(self, **kw):
        self.calls.append(("set", kw["payload"]))

    def delete_payload(self, **kw):
        self.calls.append(("delete", kw["keys"]))


def test_points_get_the_new_authors():
    client = _Client(points=4)
    written = bab._rewrite_points(client, "c", bab.Fix("d1", ["Meena", "Sehgal"], ["Sehgal Meena"]))
    assert written == 4
    assert client.calls == [("set", {"authors": ["Sehgal Meena"]})]


def test_no_authors_removes_the_key():
    client = _Client(points=2)
    bab._rewrite_points(client, "c", bab.Fix("d1", ["x@y.z"], []))
    assert client.calls == [("delete", ["authors"])]


def test_a_document_with_no_points_is_not_written():
    client = _Client(points=0)
    assert bab._rewrite_points(client, "c", bab.Fix("d1", ["A"], ["B"])) == 0
    assert client.calls == []
