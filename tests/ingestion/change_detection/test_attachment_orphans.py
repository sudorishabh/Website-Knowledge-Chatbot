"""An attachment outlives losing one parent, but not its last.

A parent stops claiming an attachment in two ways: the page is deleted, or the
page is edited and no longer references the PDF. Both are covered here — the
second matters just as much, because the page survives and nothing about it
looks like a deletion.


A PDF is a document in its own right, and one PDF is often reachable from
several pages — 84 of them are, up to eight parents each. Deleting a page must
therefore end only that page's claim on its attachments, never the attachments
themselves; they go when the last claim does. `documents_attachment` records
every claim, and the deleted parent's rows cascade away with it, so an id left
with no rows has no parent left.

Without this an attachment survives every page that ever referenced it and stays
searchable indefinitely: nothing else deletes one, because the crawl can only
reach an attachment through a parent it no longer has.

The catalog below is a stand-in for the two tables involved, with the same
cascade the foreign key provides. No MySQL, no Qdrant.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ingestion import pipeline
from app.ingestion.change_detection import ChangeRecord, ChangeStatus


class _Catalog:
    """`documents` and `documents_attachment`, and the FK cascade between them."""

    def __init__(self) -> None:
        self.documents: dict[str, str] = {}          # document_id -> source_type
        self.links: set[tuple[str, str]] = set()     # (file_uuid, parent document_id)
        self.deleted_points: list[str] = []

    # --- fixture setup ---------------------------------------------------- #

    def add_page(self, page: str, *attachments: str) -> None:
        self.documents[page] = "website"
        for attachment in attachments:
            self.documents[attachment] = "pdf_attachment"
            self.links.add((attachment, page))

    def add_link_only(self, file_uuid: str, page: str) -> None:
        """A file that was linked but never successfully ingested — a link row
        with no document of its own. 91 of these exist today."""
        self.links.add((file_uuid, page))

    def rewrite_links(self, parent: str, file_uuids) -> None:
        """What `state.upsert` does to the link table: replace this parent's rows
        wholesale, leaving no record of what it used to reference."""
        self.documents[parent] = "website"
        self.links = {(f, p) for f, p in self.links if p != parent}
        for file_uuid in file_uuids:
            self.documents.setdefault(file_uuid, "pdf_attachment")
            self.links.add((file_uuid, parent))

    # --- the catalog API the pipeline calls -------------------------------- #

    def attachment_ids_for(self, document_id: str) -> list[str]:
        return sorted(f for f, parent in self.links if parent == document_id)

    def orphaned_attachments(self, file_uuids) -> list[str]:
        still_linked = {f for f, _ in self.links}
        return [
            f for f in dict.fromkeys(file_uuids)
            if f not in still_linked and self.documents.get(f) == "pdf_attachment"
        ]

    def delete(self, ids) -> None:
        for document_id in ids:
            self.documents.pop(document_id, None)
            # ON DELETE CASCADE: a parent takes its own link rows with it.
            self.links = {(f, p) for f, p in self.links if p != document_id}

    def delete_points(self, document_id, keep_ids=None) -> None:
        self.deleted_points.append(document_id)


@pytest.fixture
def catalog(monkeypatch):
    site = _Catalog()
    logged: list[tuple[str, str]] = []

    monkeypatch.setattr(pipeline, "delete_document", site.delete_points)
    monkeypatch.setattr(pipeline.state, "delete", site.delete)
    monkeypatch.setattr(pipeline.state, "attachment_ids_for", site.attachment_ids_for)
    monkeypatch.setattr(pipeline.state, "orphaned_attachments", site.orphaned_attachments)
    monkeypatch.setattr(
        pipeline, "_log",
        lambda run_id, record, status, **kw: logged.append((record.document_id, status)),
    )
    # The document write, reduced to its effect on the link table — and applied
    # where the real one applies it, so `_persist` still has to read the old
    # links before this runs to know what the update let go of.
    monkeypatch.setattr(
        pipeline, "_save_state",
        lambda record, doc, content_hash, version, indexed=True, **_k: site.rewrite_links(
            record.document_id, [link.uuid for link in doc.file_links]
        ),
    )
    site.logged = logged
    return site


def _delete(page: str) -> str:
    record = ChangeRecord(
        status=ChangeStatus.DELETED, document_id=page, source_type="website",
        source_key=f"https://teriin.org/{page}", bundle="report", entity_type="node",
    )
    return pipeline._handle(record, build_doc=lambda r: None)


# --------------------------------------------------------------------------- #
# The last parent takes the attachment with it.
# --------------------------------------------------------------------------- #

def test_an_attachment_with_one_parent_goes_with_it(catalog):
    catalog.add_page("page-a", "pdf-1")

    assert _delete("page-a") == "deleted"

    assert "pdf-1" not in catalog.documents, "the attachment's catalog row must go"
    assert catalog.deleted_points == ["page-a", "pdf-1"], "and its vectors, after the parent's"


def test_deleting_the_attachment_removes_its_qdrant_points(catalog):
    catalog.add_page("page-a", "pdf-1")
    _delete("page-a")

    assert "pdf-1" in catalog.deleted_points


def test_every_orphaned_attachment_on_a_page_is_removed(catalog):
    catalog.add_page("page-a", "pdf-1", "pdf-2", "pdf-3")

    _delete("page-a")

    assert catalog.documents == {}
    assert catalog.deleted_points == ["page-a", "pdf-1", "pdf-2", "pdf-3"]


# --------------------------------------------------------------------------- #
# A shared attachment survives losing one parent.
# --------------------------------------------------------------------------- #

def test_a_shared_attachment_survives_losing_one_parent(catalog):
    catalog.add_page("page-a", "pdf-1")
    catalog.add_page("page-b", "pdf-1")

    _delete("page-a")

    assert catalog.documents.get("pdf-1") == "pdf_attachment"
    assert catalog.links == {("pdf-1", "page-b")}
    assert catalog.deleted_points == ["page-a"], "the attachment keeps its vectors"


def test_the_shared_attachment_goes_when_its_last_parent_does(catalog):
    catalog.add_page("page-a", "pdf-1")
    catalog.add_page("page-b", "pdf-1")

    _delete("page-a")
    assert "pdf-1" in catalog.documents, "still held by page-b"

    _delete("page-b")

    assert "pdf-1" not in catalog.documents
    assert catalog.deleted_points == ["page-a", "page-b", "pdf-1"]


def test_one_parent_of_three_leaves_the_attachment_alone(catalog):
    for page in ("page-a", "page-b", "page-c"):
        catalog.add_page(page, "pdf-1")

    _delete("page-b")

    assert catalog.documents.get("pdf-1") == "pdf_attachment"
    assert catalog.links == {("pdf-1", "page-a"), ("pdf-1", "page-c")}
    assert "pdf-1" not in catalog.deleted_points


def test_a_page_keeps_its_own_attachment_while_releasing_a_shared_one(catalog):
    """The mixed case: one attachment is exclusive to the deleted page, the
    other is not. Only the exclusive one may go."""
    catalog.add_page("page-a", "pdf-own", "pdf-shared")
    catalog.add_page("page-b", "pdf-shared")

    _delete("page-a")

    assert "pdf-own" not in catalog.documents
    assert catalog.documents.get("pdf-shared") == "pdf_attachment"
    assert catalog.deleted_points == ["page-a", "pdf-own"]


# --------------------------------------------------------------------------- #
# Everything else about deletion stays as it was.
# --------------------------------------------------------------------------- #

def test_a_page_with_no_attachments_deletes_exactly_as_before(catalog):
    catalog.add_page("page-a")

    assert _delete("page-a") == "deleted"

    assert catalog.deleted_points == ["page-a"]
    assert catalog.documents == {}
    assert catalog.logged == [("page-a", "deleted")]


def test_a_linked_file_that_was_never_ingested_costs_no_delete(catalog):
    """A link row with no document behind it — 91 of those exist. There is
    nothing to delete, so nothing should be attempted."""
    catalog.add_page("page-a")
    catalog.add_link_only("pdf-never-ingested", "page-a")

    _delete("page-a")

    assert catalog.deleted_points == ["page-a"]


def test_each_orphan_is_recorded_in_the_audit_log(catalog):
    catalog.add_page("page-a", "pdf-1", "pdf-2")

    _delete("page-a")

    assert catalog.logged == [
        ("page-a", "deleted"), ("pdf-1", "deleted"), ("pdf-2", "deleted")
    ]


def test_a_failed_orphan_check_leaves_the_attachments_alone(catalog, monkeypatch):
    """Fails open: the parent delete already succeeded, and an attachment that
    survives is the behaviour that predates this cleanup."""
    catalog.add_page("page-a", "pdf-1")

    def boom(file_uuids):
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(pipeline.state, "orphaned_attachments", boom)

    assert _delete("page-a") == "deleted"
    assert catalog.deleted_points == ["page-a"]
    assert "pdf-1" in catalog.documents


# --------------------------------------------------------------------------- #
# The page survives; the attachment is simply no longer on it.
#
# `state.upsert` replaces a document's link rows wholesale, so an edit that drops
# a PDF leaves no trace of what was dropped. `_persist` reads the links first and
# re-examines whatever the new version no longer claims — after the write has
# committed, since the orphan query runs on its own connection and would
# otherwise still see the link it is asking about.
# --------------------------------------------------------------------------- #

def _update(catalog, page: str, *attachments: str, prior: bool = True) -> None:
    """Re-ingest `page` so that it now links exactly `attachments`."""
    record = ChangeRecord(
        status=ChangeStatus.CHANGED if prior else ChangeStatus.NEW,
        document_id=page, source_type="website",
        source_key=f"https://teriin.org/{page}", bundle="report", entity_type="node",
        prior=SimpleNamespace(doc_version=1, content_hash="old", title=page) if prior else None,
    )
    doc = SimpleNamespace(
        document_id=page,
        file_links=[SimpleNamespace(uuid=a) for a in attachments],
    )
    pipeline._persist(record, doc, "hash", 2, indexed=True, run_id="run-1")


def test_dropping_the_only_pdf_deletes_it(catalog):
    catalog.add_page("page-a", "pdf-a")

    _update(catalog, "page-a")                       # the PDF is taken off the page

    assert "pdf-a" not in catalog.documents
    assert catalog.deleted_points == ["pdf-a"]
    assert "page-a" in catalog.documents, "the page itself survives the edit"


def test_dropping_one_of_two_deletes_only_that_one(catalog):
    catalog.add_page("page-a", "pdf-a", "pdf-b")

    _update(catalog, "page-a", "pdf-b")              # A removed, B kept

    assert "pdf-a" not in catalog.documents
    assert catalog.documents.get("pdf-b") == "pdf_attachment"
    assert catalog.deleted_points == ["pdf-a"]


def test_dropping_the_other_one_deletes_the_other(catalog):
    """The mirror image, so neither result can be an artefact of ordering."""
    catalog.add_page("page-a", "pdf-a", "pdf-b")

    _update(catalog, "page-a", "pdf-a")              # B removed, A kept

    assert "pdf-b" not in catalog.documents
    assert catalog.documents.get("pdf-a") == "pdf_attachment"
    assert catalog.deleted_points == ["pdf-b"]


def test_a_pdf_dropped_by_one_page_survives_on_another(catalog):
    catalog.add_page("page-a", "pdf-shared")
    catalog.add_page("page-b", "pdf-shared")

    _update(catalog, "page-a")

    assert catalog.documents.get("pdf-shared") == "pdf_attachment"
    assert catalog.links == {("pdf-shared", "page-b")}
    assert catalog.deleted_points == []


def test_the_shared_pdf_goes_when_the_second_page_drops_it_too(catalog):
    catalog.add_page("page-a", "pdf-shared")
    catalog.add_page("page-b", "pdf-shared")

    _update(catalog, "page-a")
    _update(catalog, "page-b")

    assert "pdf-shared" not in catalog.documents
    assert catalog.deleted_points == ["pdf-shared"]


def test_adding_a_pdf_deletes_nothing(catalog):
    """A new attachment is not in the previous link set, so it is never a
    candidate — the check only ever looks at what was let go."""
    catalog.add_page("page-a", "pdf-a")

    _update(catalog, "page-a", "pdf-a", "pdf-new")

    assert catalog.deleted_points == []
    assert catalog.documents.get("pdf-a") == "pdf_attachment"


def test_unchanged_links_delete_nothing(catalog):
    catalog.add_page("page-a", "pdf-a", "pdf-b")

    _update(catalog, "page-a", "pdf-a", "pdf-b")

    assert catalog.deleted_points == []
    assert catalog.links == {("pdf-a", "page-a"), ("pdf-b", "page-a")}


def test_a_first_ingestion_reads_no_links(catalog, monkeypatch):
    """A document with no catalog row can have no link rows — they are foreign
    keyed to it — so a first ingestion must not pay for the lookup."""
    reads: list[str] = []
    monkeypatch.setattr(
        pipeline.state, "attachment_ids_for",
        lambda doc_id: reads.append(doc_id) or catalog.attachment_ids_for(doc_id),
    )

    _update(catalog, "page-new", "pdf-a", prior=False)

    assert reads == [], "no prior means nothing to release"
    assert catalog.deleted_points == []


def test_a_failed_link_read_leaves_the_update_intact(catalog, monkeypatch):
    """The document write must land either way; only the cleanup is skipped."""
    catalog.add_page("page-a", "pdf-a")
    saved: list[str] = []
    monkeypatch.setattr(
        pipeline, "_save_state",
        lambda record, *a, **k: saved.append(record.document_id),
    )

    def boom(document_id):
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(pipeline.state, "attachment_ids_for", boom)

    _update(catalog, "page-a")

    assert saved == ["page-a"], "the document update still happened"
    assert catalog.documents.get("pdf-a") == "pdf_attachment", "the PDF is left alone"
    assert catalog.deleted_points == []
