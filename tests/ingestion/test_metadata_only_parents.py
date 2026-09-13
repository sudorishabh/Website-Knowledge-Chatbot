"""A Drupal page with attachments but no body may be catalogued. Narrowly.

The gate this qualifies exists for a real failure: an extraction that produces
nothing must never replace a document that had content — a blanked body at
source, an extractor regression, an unreadable PDF text layer. That behaviour is
unchanged and most of this file is about proving it.

The exception is for one shape the gate was never aimed at. A
``completed_projects`` page is a title, a start and end date and a link to the
project's executive summary; the document *is* the PDF and the page is its
record card. Refusing it left 85 PDFs with no parent, hanging off 83 pages that
are perfectly healthy in Drupal.

Nothing is fabricated to make this work: no chunk, no placeholder prose, no
vector. The row says ``content_state='metadata_only'`` and carries the link.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.models import CanonicalDocument
from app.ingestion import pipeline
from app.ingestion.change_detection.base import ChangeRecord, ChangeStatus
from app.ingestion.pipeline import (
    METADATA_ONLY,
    METADATA_ONLY_BUNDLES,
    _metadata_only_is_warranted,
)


def _file(uuid="f1", url="https://teriin.org/files/a.pdf", filename="a.pdf"):
    return SimpleNamespace(uuid=uuid, url=url, filename=filename,
                           origin="attachment", description=None, created=None)


def _doc(**kwargs) -> CanonicalDocument:
    files = kwargs.pop("file_links", [_file()])
    doc = CanonicalDocument(
        document_id=kwargs.pop("document_id", "node-1"),
        source_type="website",
        title=kwargs.pop("title", "A completed project"),
        source_url=kwargs.pop("source_url", "https://teriin.org/project/x"),
        effective_start_date=kwargs.pop("effective_start_date",
                                        "2003-05-12T00:00:00+00:00"),
        **kwargs,
    )
    doc.file_links = files
    return doc


def _record(**kwargs) -> ChangeRecord:
    return ChangeRecord(
        status=kwargs.pop("status", ChangeStatus.NEW),
        document_id=kwargs.pop("document_id", "node-1"),
        source_type=kwargs.pop("source_type", "website"),
        source_key=kwargs.pop("source_key", "https://teriin.org/project/x"),
        fingerprint="2018-01-01T00:00:00+00:00",
        bundle=kwargs.pop("bundle", "completed_projects"),
        prior=kwargs.pop("prior", None),
        payload=kwargs.pop("payload", None),
        entity_type="node",
    )


# --------------------------------------------------------------------------- #
# The predicate
# --------------------------------------------------------------------------- #

def test_an_empty_project_page_with_an_attachment_qualifies():
    assert _metadata_only_is_warranted(_record(), _doc()) is True


def test_an_empty_page_with_no_attachment_does_not_qualify():
    """The whole point of the exception is giving an attachment a parent. With
    nothing attached this is the broken extraction the gate exists for."""
    assert _metadata_only_is_warranted(_record(), _doc(file_links=[])) is False


def test_several_attachments_qualify_the_same_way():
    doc = _doc(file_links=[_file("f1"), _file("f2", filename="b.pdf")])
    assert _metadata_only_is_warranted(_record(), doc) is True


@pytest.mark.parametrize("bundle", ["events", "news", "page", "research_papers",
                                    "report", None, ""])
def test_no_other_bundle_qualifies_yet(bundle):
    """21 further pages hit the same gate and none carries an attachment, so the
    exception stays at one content type until someone looks at the others."""
    assert _metadata_only_is_warranted(_record(bundle=bundle), _doc()) is False


def test_the_scope_is_exactly_one_bundle():
    assert METADATA_ONLY_BUNDLES == frozenset({"completed_projects"})


@pytest.mark.parametrize("missing", ["title", "source_url", "document_id"])
def test_identity_is_required(missing):
    """Without a title or a URL the row is a placeholder, not a document."""
    if missing == "document_id":
        record, doc = _record(document_id=""), _doc()
    else:
        record, doc = _record(), _doc(**{missing: None})
    assert _metadata_only_is_warranted(record, doc) is False


def test_a_blank_title_is_not_a_title():
    assert _metadata_only_is_warranted(_record(), _doc(title="   ")) is False


def test_an_attachment_that_extracts_to_nothing_never_qualifies():
    """A PDF with no text layer is a failed extraction however many conditions
    the rest of it happens to meet."""
    record = _record(source_type="pdf_attachment", bundle="completed_projects")
    assert _metadata_only_is_warranted(record, _doc()) is False


# --------------------------------------------------------------------------- #
# The gate's original purpose is untouched
# --------------------------------------------------------------------------- #

def test_a_page_that_previously_had_content_still_errors():
    """The failure the gate was built for: a document with real content whose
    body is blanked at source. It must keep its old version, not become a
    metadata-only stub."""
    from app.catalog.models import StateRecord

    prior = StateRecord(document_id="node-1", source_type="website",
                        source_key="k", fingerprint="f", content_hash="abc123",
                        content_state=None)
    assert _metadata_only_is_warranted(_record(prior=prior), _doc()) is False


def test_a_page_that_was_already_metadata_only_still_qualifies():
    """Idempotence at the predicate level: re-crawling a recovered page must
    reach the same answer, not error on the second pass."""
    from app.catalog.models import StateRecord

    prior = StateRecord(document_id="node-1", source_type="website",
                        source_key="k", fingerprint="f",
                        content_state=METADATA_ONLY)
    assert _metadata_only_is_warranted(_record(prior=prior), _doc()) is True


def _chunk(text: str):
    from app.ingestion.chunking.models import Chunk, DocumentMeta

    return Chunk(chunk_id="c1", text=text, is_parent=False,
                 meta=DocumentMeta(document_id="node-1", source_type="website",
                                   title="A completed project"))


def test_the_empty_predicate_itself_is_unchanged():
    assert pipeline._extraction_is_empty([]) is True
    assert pipeline._extraction_is_empty([_chunk("   ")]) is True
    assert pipeline._extraction_is_empty([_chunk("real text")]) is False


# --------------------------------------------------------------------------- #
# What the gate does with a qualifying page
# --------------------------------------------------------------------------- #

class _Recorder:
    def __init__(self):
        self.saved = []
        self.indexed = []
        self.deleted = []
        self.logged = []


@pytest.fixture
def world(monkeypatch):
    """Drive `_handle` with every store stubbed, and record what it asked for."""
    rec = _Recorder()

    monkeypatch.setattr(pipeline, "_save_state",
                        lambda record, doc, content_hash, version, *, indexed,
                        content_state=None: rec.saved.append(
                            (record.document_id, indexed, content_state, version)))
    monkeypatch.setattr(pipeline, "_linked_attachments", lambda record: [])
    monkeypatch.setattr(pipeline, "_delete_orphaned_attachments",
                        lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "index_chunks",
                        lambda chunks: rec.indexed.append(len(chunks)) or len(chunks))
    monkeypatch.setattr(pipeline, "delete_document",
                        lambda *a, **k: rec.deleted.append(a))
    monkeypatch.setattr(pipeline, "_log",
                        lambda run_id, record, status, **k:
                        rec.logged.append(status))
    monkeypatch.setattr(pipeline, "_record_source_date_decision",
                        lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_enrich", lambda doc, h: "skipped")
    monkeypatch.setattr(pipeline.knowledge_sync, "enabled", lambda: False)
    return rec


def _handle(record, doc, chunks):
    def build(_record):
        return doc

    import app.ingestion.pipeline as p

    return p._handle(record, build), chunks


def test_a_qualifying_page_is_catalogued_without_being_indexed(world, monkeypatch):
    doc = _doc()
    monkeypatch.setattr(pipeline, "chunk_canonical", lambda d: [])
    outcome = pipeline._handle(_record(), lambda _r: doc)

    assert outcome == "metadata_only"
    assert world.saved == [("node-1", False, METADATA_ONLY, 1)]
    assert world.indexed == [], "no chunks may be indexed"
    assert world.deleted == [], "nothing to swap out"
    assert world.logged == ["metadata_only"]


def test_no_synthetic_chunk_is_created_for_it(world, monkeypatch):
    """The row exists because the source describes the document, not because a
    placeholder was manufactured to get past the gate."""
    produced: list = []

    def _chunk(d):
        produced.append(d)
        return []

    monkeypatch.setattr(pipeline, "chunk_canonical", _chunk)
    pipeline._handle(_record(), lambda _r: _doc())

    assert produced, "the real chunker still ran"
    assert world.indexed == [], "and produced nothing to index"


def test_an_empty_page_with_no_attachment_still_errors(world, monkeypatch):
    monkeypatch.setattr(pipeline, "chunk_canonical", lambda d: [])
    outcome = pipeline._handle(_record(), lambda _r: _doc(file_links=[]))

    assert outcome == "error"
    assert world.saved == [], "nothing may be catalogued"
    assert world.logged == ["error"]


def test_a_page_with_real_content_takes_the_ordinary_path(world, monkeypatch):
    chunks = [_chunk("Real body text.")]
    monkeypatch.setattr(pipeline, "chunk_canonical", lambda d: chunks)
    outcome = pipeline._handle(_record(), lambda _r: _doc())

    assert outcome == "indexed"
    assert world.indexed == [1]
    assert world.saved == [("node-1", True, None, 1)]


def test_the_outcome_counts_as_settled():
    """A document that reached processing must land in one of the two sets, or
    the crawl's retry cursor loses track of it."""
    assert "metadata_only" in pipeline._RESOLVED_OUTCOMES
    assert "metadata_only" not in pipeline._UNRESOLVED_OUTCOMES
