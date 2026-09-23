"""Catalog domain models: the ingest-state record and its link/log types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AttachmentLink:
    """A node's link to an attached PDF (its own document, keyed by file_uuid)."""

    file_uuid: str
    origin: str  # "attachment" | "inbody"
    url: str | None = None
    filename: str | None = None


@dataclass
class StateRecord:

    document_id: str
    source_type: str
    source_key: str
    fingerprint: str
    content_hash: str = ""
    doc_version: int = 1
    # Which ingestion pipeline produced the indexed content (see
    # app.ingestion.version). A row whose version differs from the running
    # pipeline's is rebuilt on its next crawl even when its content is
    # unchanged — content hashes cannot see a code change. None on a row written
    # before the column existed, which is treated as "not the current version".
    pipeline_version: str | None = None
    bundle: str | None = None
    # JSON:API entity type ("node", "taxonomy_term", "block_content") for
    # Drupal records; None for attachment documents.
    # Content counts filter on it so facet terms don't count as documents.
    entity_type: str | None = None
    changed_mark: int | None = None
    indexed_at: str | None = None
    effective_start_date: str | None = None
    #: Where ``effective_start_date`` came from: ``created`` | ``cms_field`` |
    #: ``parent_page`` | ``document_text`` | ``document_copyright``. None means
    #: not recorded — which is
    #: every row written before the column existed, and is deliberately not read
    #: as ``created``.
    date_source: str | None = None
    #: How precise ``effective_start_date`` is: ``year`` | ``month`` | ``day``. None
    #: means not recorded. A ``year`` value stored as 1 January is a marker for
    #: the year, never a claim about the month.
    start_precision: str | None = None
    #: End of the period the content covers, for a bundle that declares an end
    #: field. None for a single-date document; never derived from
    #: ``effective_start_date``, which stays the effective date everything ranks on.
    effective_end_date: str | None = None
    end_precision: str | None = None
    # Display fields so structured list/lookup queries can be answered from the
    # catalog (no live site fetch). url is the document's public page/file URL.
    title: str | None = None
    url: str | None = None
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    # Free-text keyword tags (documents_tag). Separate from `categories`
    # (themes): tags are a flat, long-tail vocabulary with no hierarchy.
    tags: list[str] = field(default_factory=list)
    #: Why this document has no chunks, when it has none. ``None`` is the
    #: ordinary case: the document has indexed content. ``metadata_only`` says
    #: the *source* provides identity and attachments but no body — a Drupal
    #: project page that is a title, two date fields and a PDF link — and that
    #: this is a complete representation of it rather than a failed extraction.
    #:
    #: It exists so that nothing downstream may assume "a row exists, therefore
    #: it has chunks". Deliberately a column of its own rather than inferred
    #: from an empty ``content_hash``: the two have been equal by accident
    #: before, and an invariant that depends on an accident is not an invariant.
    content_state: str | None = None
    # Attachment links and the lossless source metadata (JSON column).
    attachments: list[AttachmentLink] = field(default_factory=list)
    raw_meta: dict[str, Any] | None = None


@dataclass
class LogEntry:
    """One ingestion event: where a file/record came from and what happened."""

    document_id: str
    source_type: str
    status: str
    run_id: str | None = None
    source_path: str | None = None
    source_url: str | None = None
    bundle: str | None = None
    tags: str | None = None
    title: str | None = None
    doc_version: int | None = None
    chunks_indexed: int | None = None
    fingerprint: str | None = None
    content_hash: str | None = None
    error_message: str | None = None
