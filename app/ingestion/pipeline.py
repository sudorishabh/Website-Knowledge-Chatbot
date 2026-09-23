from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from app.catalog import enrichment
from app.catalog import retries
from app.catalog import state
from app.catalog import theme_taxonomy
from app.catalog import log as ingest_log
from app.catalog.models import AttachmentLink, StateRecord
from app.config import get_settings
from app.core.models import CanonicalDocument
from app.ingestion import change_detection as cd
from app.ingestion import knowledge_sync
from app.ingestion.change_detection import ChangeRecord, ChangeStatus
from app.ingestion.chunking import Chunk, chunk_canonical
from app.ingestion.enrich import abstract_version, generate_abstract, is_shutdown_error
from app.ingestion.indexer import index_chunks
from app.ingestion.version import PIPELINE_VERSION
from app.core.clients import delete_document, refresh_document_title
from app.observability.tracing import span

logger = logging.getLogger(__name__)

DocBuilder = Callable[[ChangeRecord], "CanonicalDocument | None"]

# One corpus-wide ingestion run (sweep / PDF scan / Drupal crawl) at a time.
# Concurrent runs double-embed documents and race each other's delete/upsert
# and documents-table writes. Process-local by design: the ingestion server is a
# single private instance (celery mode serializes via its queue instead).
_run_lock = threading.Lock()


class IngestBusyError(RuntimeError):
    """Another ingestion run is already in progress in this process."""


@contextmanager
def _exclusive(what: str) -> Iterator[None]:
    if not _run_lock.acquire(blocking=False):
        raise IngestBusyError(f"Another ingestion run is in progress; {what} rejected.")
    try:
        yield
    finally:
        _run_lock.release()


def _save_state(
    record: ChangeRecord,
    doc: CanonicalDocument,
    content_hash: str,
    version: int,
    *,
    indexed: bool,
    content_state: str | None = None,
) -> None:
    state.upsert(
        StateRecord(
            document_id=record.document_id,
            source_type=record.source_type,
            source_key=record.source_key,
            fingerprint=record.fingerprint,
            content_hash=content_hash,
            doc_version=version,
            # Only a write that actually re-chunked the document may claim the
            # current pipeline version. A fingerprint refresh leaves the stored
            # one alone (the upsert COALESCEs None), so a document that has not
            # been rebuilt keeps reading as stale until it is.
            # A metadata-only document is *settled*, not half-done: there will
            # never be chunks for it, so leaving it unstamped would make the
            # corpus reprocessor rebuild it forever to reach the same answer.
            pipeline_version=(PIPELINE_VERSION
                              if indexed or content_state == METADATA_ONLY
                              else None),
            # `indexed_at` deliberately stays NULL — it means "has points in the
            # collection", and this document has none by design.
            content_state=content_state,
            bundle=record.bundle,
            entity_type=record.entity_type,
            changed_mark=record.changed_mark,
            effective_start_date=doc.effective_start_date,
            date_source=doc.date_source,
            start_precision=doc.start_precision,
            # The end of the period, for the bundles whose mapping declares one
            # (completed_projects, events) and the files attached to them. The
            # resolver leaves it None everywhere else, and `state.upsert` writes
            # it with VALUES() so a cleared CMS end date is cleared here too.
            effective_end_date=doc.effective_end_date,
            end_precision=doc.end_precision,
            title=doc.title,
            url=doc.source_url,
            authors=list(doc.authors),
            categories=list(doc.categories),
            tags=list(doc.tags),
            attachments=[
                AttachmentLink(
                    file_uuid=f.uuid, origin=f.origin, url=f.url, filename=f.filename
                )
                for f in doc.file_links
            ],
            raw_meta=doc.raw_meta or None,
        ),
        mark_indexed=indexed,
    )


def _linked_attachments(record: ChangeRecord) -> list[str]:
    """What this document linked to before it is rewritten.

    ``state.upsert`` replaces a document's link rows wholesale, so once it has
    run there is no record of what the document used to reference — this has to
    be read first or not at all.

    Skipped for a document the catalog has never seen: link rows are foreign-keyed
    to the document, so one that has no row can have no links, and a first
    ingestion should not pay for a lookup that can only come back empty.
    """
    if record.prior is None:
        return []
    try:
        return state.attachment_ids_for(record.document_id)
    except Exception:
        logger.warning(
            "Could not read %s's attachment links; any it drops in this update "
            "will be left in place.", record.document_id, exc_info=True,
        )
        return []


def _persist(
    record: ChangeRecord,
    doc: CanonicalDocument,
    content_hash: str,
    version: int,
    *,
    indexed: bool,
    run_id: str | None = None,
    content_state: str | None = None,
) -> None:
    """Persist the content record and the facet rows derived from it.

    The document row is the primary fact — and the FK target every facet row
    hangs off — so it is written first, with its theme/author/tag/attachment rows
    following inside the same transaction.

    A page that drops a PDF is the other way an attachment loses its last parent,
    alongside the page being deleted outright: the link row simply stops being
    written, and nothing else in the pipeline would ever notice. So the links
    this document is about to replace are read first, and whichever of them it no
    longer claims are re-examined once the write has landed.

    The ordering is the whole trick. ``orphaned_attachments`` asks the catalog on
    its own connection, so it can only see committed rows — run before the write
    it would still find the old link and conclude the attachment is spoken for.
    """
    previously_linked = _linked_attachments(record)
    _save_state(record, doc, content_hash, version, indexed=indexed,
                content_state=content_state)

    still_claimed = {link.uuid for link in doc.file_links}
    released = [f for f in previously_linked if f not in still_claimed]
    _delete_orphaned_attachments(released, record, run_id)


def _log(
    run_id: str | None,
    record: ChangeRecord,
    status: str,
    *,
    doc: CanonicalDocument | None = None,
    version: int | None = None,
    chunks: int | None = None,
    error: str | None = None,
) -> None:
    prior_hash = record.prior.content_hash if record.prior else None
    ingest_log.record(
        ingest_log.LogEntry(
            run_id=run_id,
            document_id=record.document_id,
            source_type=record.source_type,
            status=status,
            source_url=record.source_key,
            bundle=record.bundle,
            tags=", ".join(doc.tags) if doc and doc.tags else None,
            title=doc.title if doc else None,
            doc_version=version,
            chunks_indexed=chunks,
            fingerprint=record.fingerprint or None,
            content_hash=(doc.content_hash if doc else prior_hash) or None,
            error_message=error,
        )
    )


def _enrich_once(doc: CanonicalDocument, content_hash: str, max_attempts: int) -> str:
    version = abstract_version()
    cached = enrichment.get(content_hash, version=version)
    if cached is not None and cached.abstract:
        return "hit"
    if cached is not None and cached.attempts >= max_attempts:
        return "exhausted"

    try:
        abstract = generate_abstract(doc)
    except Exception as exc:
        if is_shutdown_error(exc):
            # The process is going down mid-document, so no call was made and
            # no attempt is owed. Counting it would spend the budget on a
            # Ctrl-C and could leave a big document permanently abstract-less.
            logger.info("Abstract generation abandoned for %s: %s", doc.document_id, exc)
            return "aborted"
        # A model failure is worth remembering: without a counter, a document
        # that always fails is retried at full cost on every sweep forever.
        logger.warning("Abstract generation failed for %s.", doc.document_id, exc_info=True)
        enrichment.record_failure(content_hash, version=version, error=str(exc))
        return "failed"

    if abstract is None:
        return "skipped"  # too short to be worth summarizing; never retried
    enrichment.put(content_hash, version=version, abstract=abstract)
    return "stored"


def _enrich(doc: CanonicalDocument, content_hash: str) -> str:
    """Ensure this content has a cached abstract; report what happened.

    Fails open in every direction, like the rest of the pipeline's external
    dependencies: a rate-limited deployment or an unreachable catalog leaves the
    document without an abstract rather than stopping the sweep.
    """
    settings = get_settings()
    if not settings.enrichment_enabled:
        return "off"
    try:
        return _enrich_once(doc, content_hash, settings.enrichment_max_attempts)
    except Exception as exc:
        if is_shutdown_error(exc):
            logger.info("Enrichment abandoned for %s: %s", doc.document_id, exc)
            return "aborted"
        logger.warning(
            "Enrichment could not run for %s; continuing without an abstract.",
            doc.document_id, exc_info=True,
        )
        return "error"


def _delete_orphaned_attachments(
    linked: Sequence[str], parent: ChangeRecord, run_id: str | None
) -> list[str]:
    """Remove the attachments that just lost their last parent.

    A PDF is its own document, and one PDF is often reachable from several pages
    — 84 of them are. Deleting a page must therefore not delete its attachments,
    only end that page's claim on them; the attachment goes when the last claim
    does. `documents_attachment` holds every claim, and the deleted parent's rows
    have already cascaded away by the time this runs, so an id with no rows left
    has no parent left.

    Without this an attachment outlives every page that referenced it and stays
    searchable forever: nothing else in the pipeline ever deletes one, because
    the crawl only reaches an attachment through a parent it no longer has.

    Fails open. An attachment that survives a failure here is the behaviour that
    predates this function, and is worth far less than the parent delete that
    already succeeded.
    """
    if not linked:
        return []
    try:
        orphans = state.orphaned_attachments(linked)
    except Exception:
        logger.warning(
            "Could not check whether %s's attachments still have a parent; "
            "leaving them in place.", parent.document_id, exc_info=True,
        )
        return []

    for orphan in orphans:
        try:
            delete_document(orphan)
            state.delete([orphan])
        except Exception:
            logger.warning("Could not delete orphaned attachment %s.", orphan, exc_info=True)
            continue
        _log(
            run_id,
            ChangeRecord(
                status=ChangeStatus.DELETED,
                document_id=orphan,
                source_type="pdf_attachment",
                source_key=parent.source_key,
                bundle=parent.bundle,
            ),
            "deleted",
        )

    kept = len(linked) - len(orphans)
    logger.info(
        "Deleted %d attachment(s) orphaned by %s; %d still linked elsewhere.",
        len(orphans), parent.document_id, kept,
    )
    return orphans


def _extraction_is_empty(chunks: Sequence[Chunk]) -> bool:
    """Whether this document produced nothing worth indexing.

    Not merely ``not chunks``: a chunk carrying only whitespace is the same
    outcome reached by a different route — a body of non-breaking spaces, a PDF
    whose text layer yields blank lines — and indexing it would replace real
    content with an empty point just as surely.
    """
    return not any(chunk.text.strip() for chunk in chunks)


#: ``content_state`` for a document the source describes fully without a body.
METADATA_ONLY = "metadata_only"

#: The bundles whose pages may be catalogued without body text.
#:
#: Deliberately one entry. A ``completed_projects`` page is, by design, a title,
#: a start and end date and a link to the project's executive summary — the
#: document *is* the PDF, and the page is its record card. 96 of them extract to
#: nothing, and 83 of those have a PDF we already hold and cannot connect to
#: anything, because the page they hang off was never catalogued.
#:
#: The other 21 pages that extract to nothing (13 ``events``, 4 ``news``, 2
#: ``page``, 2 ``research_papers``) carry no attachments at all, so admitting
#: them would buy nothing and would widen an exception to bundles whose empty
#: body has not been shown to be intentional. They stay out until someone looks.
METADATA_ONLY_BUNDLES: frozenset[str] = frozenset({"completed_projects"})


def _metadata_only_is_warranted(
    record: ChangeRecord, doc: CanonicalDocument
) -> bool:
    """May this empty extraction be catalogued as a metadata-only document?

    Every condition has to hold, and each one is doing work:

    * **A Drupal page.** An attachment or a local PDF that extracts to nothing
      is a failed download or a scanned text layer, never a design.
    * **A bundle whose pages are known to be record cards** — see
      :data:`METADATA_ONLY_BUNDLES`.
    * **At least one attachment.** This is what makes the page worth having: it
      is the parent that lets a PDF we already hold be reachable. A page with an
      empty body *and* nothing attached is exactly the broken extraction the
      gate exists for, and still fails.
    * **Identity.** A uuid, a title and a URL. Without them the row is a
      placeholder rather than a document, and
      ``reconcile.date_checks.metadata_only_without_identity`` would be right to
      complain about it.
    * **Nothing indexable to lose.** The gate's real purpose is that a document
      which *had* content must never be replaced by an empty one — a blanked
      body at source, an extractor regression. So this applies only where there
      is no prior version, or where the prior version was itself metadata-only.
      A page that used to have prose and now extracts to nothing still errors,
      still keeps its old version, and still comes back on the retry.

    Returns False for anything else, which leaves the original behaviour exactly
    as it was.
    """
    if record.source_type != "website":
        return False
    if (record.bundle or "") not in METADATA_ONLY_BUNDLES:
        return False
    if not getattr(doc, "file_links", None):
        return False
    if not (record.document_id and (doc.title or "").strip() and doc.source_url):
        return False
    prior = record.prior
    return prior is None or prior.content_state == METADATA_ONLY


def _record_source_date_decision(record: ChangeRecord, doc: CanonicalDocument) -> None:
    """Record why a *website* document carries the date it does.

    Attachments record their own decision inside ``build_attachment_doc``, which
    is where the evidence lives; this is the same audit trail for the other
    source type, so one table and one review queue answer "why does this
    document carry this date?" for the whole corpus.

    Only written when the document's bundle maps to a real CMS date field — see
    ``date_decisions.from_effective_date``. Fails open like every other catalog
    write on this path: an unreachable database costs one warning, never a
    document its ingestion.
    """
    if record.source_type != "website":
        return
    from app.catalog import date_decisions

    try:
        row = date_decisions.from_effective_date(
            document_id=record.document_id,
            url=doc.source_url,
            created=getattr(record.payload, "created", None),
            # The resolution the builder actually applied, carried on the
            # document rather than re-derived here: a second reading of the
            # metadata could disagree with the value that was stored.
            resolved=doc.date_evidence,
            title=doc.title,
        )
        if row is None:
            return
        date_decisions.ensure_table()
        date_decisions.record(row)
    except Exception:
        logger.warning(
            "Could not record the date decision for %s; ingestion continues.",
            record.document_id, exc_info=True,
        )


def _handle(
    record: ChangeRecord,
    build_doc: DocBuilder,
    run_id: str | None = None,
    note: Callable[[str], None] | None = None,
    fail: Callable[[str], None] | None = None,
    flag: Callable[[str], None] | None = None,
) -> str:
    """Process one change record and report the outcome.

    ``fail`` receives the reason for an unresolved outcome, so the retry marker
    can say *why* a document is unresolved rather than only that it is. ``flag``
    receives run-level observations that are not outcomes — a document indexed
    without an effective date, say, which is neither a success worth hiding nor
    a failure worth retrying.
    """
    prior_version = record.prior.doc_version if record.prior else None

    def failed(reason: str) -> str:
        if fail is not None:
            fail(reason)
        return reason

    if record.status is ChangeStatus.DELETED:
        # Captured before the delete: the document's link rows cascade away with
        # it, and they are what says which attachments to re-examine afterwards.
        linked = state.attachment_ids_for(record.document_id)
        delete_document(record.document_id)
        state.delete([record.document_id])
        logger.info("Deleted %s (%s)", record.document_id, record.source_key)
        _log(run_id, record, "deleted", version=prior_version)
        _delete_orphaned_attachments(linked, record, run_id)
        return "deleted"

    if record.status is ChangeStatus.UNCHANGED:
        if get_settings().ingest_log_unchanged:
            _log(run_id, record, "unchanged", version=prior_version)
        return "unchanged"

    logger.info(
        "Ingesting %s %s (%s)", record.source_type, record.document_id, record.source_key
    )
    with span("ingest.extract", source_type=record.source_type):
        doc = build_doc(record)
    if doc is None:
        _log(run_id, record, "skipped", error=failed(
            "the document could not be built (download or extraction returned nothing)"
        ))
        return "skipped"

    _record_source_date_decision(record, doc)

    content_hash = doc.ensure_content_hash()
    # Before the content-changed branch, so an unchanged-content document that
    # predates enrichment still picks up an abstract as it is re-crawled. The
    # cache is keyed by this hash, so a hit costs one indexed lookup.
    enriched = _enrich(doc, content_hash)
    if note is not None:
        note(enriched)

    if not cd.needs_rebuild(record, content_hash):
        version = prior_version or 1
        _persist(record, doc, content_hash, version, indexed=False, run_id=run_id)
        # The hash covers body text only, so a title-only edit lands here rather
        # than re-indexing. The catalog took the new title above; carry it to the
        # chunk payloads too (one call, no re-embed) so citations don't display
        # the old one until the body happens to change.
        if record.prior is not None and record.prior.title != doc.title:
            refresh_document_title(record.document_id, doc.title)
        logger.info("Unchanged content for %s; fingerprint refreshed.", record.document_id)
        _log(run_id, record, "unchanged_content", doc=doc, version=version)
        return "unchanged_content"

    if not cd.content_changed(record, content_hash):
        # Same text, different pipeline. Worth a line of its own: during a corpus
        # reprocess this is every document, and "why is it re-embedding unchanged
        # content?" should be answerable from the log.
        logger.info(
            "Rebuilding %s: content unchanged but pipeline version moved %s -> %s.",
            record.document_id,
            (record.prior.pipeline_version if record.prior else None) or "unstamped",
            PIPELINE_VERSION,
        )

    version = cd.next_version(record)
    doc.doc_version = version
    # Index the new version FIRST, then delete everything else for the doc.
    # Chunk ids are version-scoped (uuid5 of doc|version|suffix), so the new
    # points never collide with the old ones: the old version stays searchable
    # until the swap, and a mid-index failure leaves it fully intact.
    with span("ingest.chunk"):
        new_chunks = chunk_canonical(doc)

    # The swap's precondition: there is something to swap *in*. An empty
    # extraction is a failure of this run, not a statement that the document is
    # now empty — a blanked body at source, an unreadable PDF text layer, an
    # extractor regression. Nothing below this point runs, so the previous
    # version keeps its vectors, its catalog row and its indexed_at, and the
    # retry marker written from the outcome brings the document back next run.
    if _extraction_is_empty(new_chunks):
        # The one sanctioned exception: a source that describes the document
        # fully without a body, and whose attachments would otherwise have no
        # parent. Nothing is fabricated to get here — no chunk, no vector, no
        # invented prose. The row exists so the PDF hanging off it can.
        if _metadata_only_is_warranted(record, doc):
            _persist(record, doc, content_hash, prior_version or version,
                     indexed=False, run_id=run_id, content_state=METADATA_ONLY)
            logger.info(
                "%s (%s/%s) has no body but carries %d attachment(s); "
                "catalogued as metadata-only.",
                record.document_id, record.source_type, record.bundle,
                len(doc.file_links),
            )
            _log(run_id, record, "metadata_only", doc=doc,
                 version=prior_version or version, chunks=0)
            return "metadata_only"

        reason = failed(
            f"extraction produced no indexable content ({len(new_chunks)} chunks); "
            f"keeping version {prior_version or 0}"
        )
        logger.error(
            "%s (%s) extracted to nothing; keeping the previous version rather "
            "than replacing it with an empty one.",
            record.document_id, record.source_key,
        )
        _log(run_id, record, "error", doc=doc, version=prior_version, chunks=0,
             error=reason)
        return "error"

    if not doc.effective_start_date:
        # Not an error — some sources genuinely state no date, and inventing one
        # would be worse than having none. But an undated document is *invisible*
        # to every date-range filter rather than merely ranked low, so it must not
        # pass silently.
        if flag is not None:
            flag("undated")
        logger.warning(
            "Indexing %s (%s/%s) with no effective date; it will be excluded "
            "from date-filtered results.",
            record.document_id, record.source_type, record.bundle,
        )

    chunks = index_chunks(new_chunks)
    delete_document(record.document_id, keep_ids=[c.chunk_id for c in new_chunks])
    _persist(record, doc, content_hash, version, indexed=True, run_id=run_id)
    logger.info(
        "%s %s -> v%d", record.status.value, record.document_id, version
    )
    _log(run_id, record, "indexed", doc=doc, version=version, chunks=chunks)

    # The document is now fully indexed: its points are in Qdrant, the previous
    # version has been swapped out, the catalog row and its facets are committed
    # and the log says so. Only then may the knowledge layer look at it.
    #
    # The result is deliberately discarded. This outcome is already decided, and
    # `knowledge_sync` returns rather than raises in every direction, so there is
    # no path by which building knowledge can unmake an indexed document.
    #
    # The `enabled()` guard is not an optimisation. Argument evaluation happens
    # at the call site, outside anything `knowledge_sync` can catch, so with the
    # feature off this is the difference between "inert" and "inert unless one
    # of these attributes is missing". `raw_meta` is read defensively for the
    # same reason: nothing about assembling this call may cost a document that
    # is already indexed.
    if knowledge_sync.enabled():
        knowledge_sync.process_after_index(
            document_id=record.document_id,
            doc_version=version,
            chunks=new_chunks,
            source_type=record.source_type,
            bundle=record.bundle,
            content_hash=content_hash,
            raw_meta=getattr(doc, "raw_meta", None),
            authors=tuple(getattr(doc, "authors", ()) or ()),
            run_id=run_id,
        )
    return "indexed"


# Outcomes that leave a document unindexed, and the ones that settle it. A
# document that reached processing must end in one set or the other, or the
# crawl cursor loses track of it (see app.catalog.retries). "unchanged" is in
# neither: it never reached a build, and a document that is unchanged already
# has the catalog row that positions the cursor.
_UNRESOLVED_OUTCOMES = frozenset({"error", "skipped"})
_RESOLVED_OUTCOMES = frozenset(
    # "metadata_only" is settled, not pending: the source has been read in
    # full and there is nothing further to extract, so the crawl cursor must
    # stop treating it as work outstanding.
    {"indexed", "unchanged_content", "deleted", "metadata_only"}
)


def _track_retry(
    record: ChangeRecord,
    outcome: str,
    pending: frozenset[str],
    error: str | None = None,
) -> None:
    """Keep the crawl's retry floor in step with what this document did.

    ``pending`` is the unresolved set as it stood at the start of the run, so a
    document that was never failing costs no write at all — the common case is
    every document in a healthy sweep.

    ``error`` is why the document is unresolved. Without it the retry queue is a
    list of ids that says nothing about whether they are one broken host, one
    bad extractor or ninety separate problems — which is the difference between
    a queue an operator can triage and one they can only stare at.

    Fails open, like every other catalog write on this path: an unreachable
    database costs one warning and the behaviour that predates the floor.
    """
    try:
        if outcome in _UNRESOLVED_OUTCOMES:
            retries.record(
                record.document_id,
                source_type=record.source_type,
                bundle=record.bundle,
                changed_mark=record.changed_mark,
                outcome=outcome,
                error=error,
            )
        elif outcome in _RESOLVED_OUTCOMES and record.document_id in pending:
            retries.clear([record.document_id])
    except Exception:
        logger.warning(
            "Could not update the retry marker for %s; the crawl cursor may "
            "skip it.", record.document_id, exc_info=True,
        )


# Outcomes that consumed real work (downloads, extraction, embedding). Only
# these count against the batch budget — unchanged scans are free and must
# never exhaust it, or a caught-up capped run would stall before reaching the
# documents that actually changed.
_WORKED_OUTCOMES = frozenset({"indexed", "deleted", "skipped", "error"})


def _pending_retries() -> frozenset[str]:
    """Documents that came into this run already carrying a retry marker.

    Read once, so a healthy sweep never issues a delete per document: a document
    that is not in here has nothing to clear.
    """
    try:
        retries.ensure_table()
        return frozenset(retries.load())
    except Exception:
        logger.exception(
            "Could not read retry markers; failures this run will still be recorded."
        )
        return frozenset()


def _prewarm_clients(settings) -> None:
    """Build the process-wide cached clients once, before any worker needs one.

    ``functools.lru_cache`` does not hold its lock across the wrapped call, so
    two workers that miss at the same time both construct a client and one is
    silently discarded — with its connection pool unclosed. Warming on this
    thread makes every worker a cache hit, which is only worth doing on the
    parallel path: the sequential loop cannot race itself.

    ``get_mysql_pool`` deliberately isn't here — ``state.ensure_table()`` above
    already warmed it on this thread, and it is the one whose double
    construction would actually cost something (two pools, twice the
    connections).

    Best-effort in every direction, like the collection pre-create beside it: a
    client that cannot be built now is built by whichever worker needs it first,
    which is exactly the behaviour that predates this function.
    """
    from app.core.clients import get_embeddings
    from app.ingestion.chunking.packer import get_encoder

    def warm(what: str, build: Callable[[], object]) -> None:
        try:
            build()
        except Exception:
            logger.debug("Could not pre-warm %s; a worker will build it.", what, exc_info=True)

    warm("the embeddings client", get_embeddings)
    # tiktoken downloads its BPE table on a cold cache; four threads racing that
    # is four downloads.
    warm("the tokenizer", lambda: get_encoder("cl100k_base"))
    if getattr(settings, "enrichment_enabled", False):
        from app.core.clients.llm import get_llm

        warm(
            "the LLM client",
            lambda: get_llm(temperature=getattr(settings, "llm_structured_temperature", None)),
        )


def _run(records: Iterator[ChangeRecord], build_doc: DocBuilder) -> Counter:
    # Preflight, before a single document is written. Every document this run
    # touches has its theme rows rewritten from the theme map, and classifying
    # against an unreadable map does not fail — it succeeds and writes the wrong
    # answer, dropping the parent, group and path of every theme it sees. That
    # is a silent corpus-wide rewrite triggered by something as small as the
    # data file going missing (it has), so the run refuses to start instead.
    #
    # Deliberately here rather than in `theme_taxonomy.classify`: per-document
    # classification stays tolerant, so a problem appearing mid-run costs one
    # document rather than aborting a long ingest. Refusing up front costs
    # nothing and prevents the rewrite.
    theme_taxonomy.require_taxonomy()
    state.ensure_table()
    try:
        ingest_log.ensure_table()
    except Exception:
        logger.exception("Could not ensure ingest_log table; events will be skipped.")
    settings = get_settings()
    if settings.enrichment_enabled:
        try:
            enrichment.ensure_table()
        except Exception:
            logger.exception(
                "Could not ensure the enrichment table; abstracts will be skipped."
            )
    pending_retries = _pending_retries()
    max_docs = settings.ingest_max_docs_per_run
    batch_size = settings.ingest_batch_size
    pause = settings.ingest_batch_pause_seconds
    workers = max(1, settings.ingest_workers)

    run_id = uuid.uuid4().hex
    tally: Counter = Counter()
    worked = 0
    started = time.perf_counter()

    # `note` runs on worker threads and `account` on the main loop, so every
    # write to the shared Counter takes this. The two touch disjoint keys, so
    # CPython would get away without it — but "disjoint" is an invariant no
    # caller is told about, and the lock is uncontended either way.
    tally_lock = threading.Lock()

    def note(outcome: str) -> None:
        """Record an enrichment outcome. Hit rate has to be visible: this
        cache's failure mode is silently re-paying for every document."""
        if outcome == "off":
            return
        with tally_lock:
            tally[f"enrich_{outcome}"] += 1

    def flag(observation: str) -> None:
        """Count something worth knowing about a run that is not an outcome."""
        with tally_lock:
            tally[observation] += 1

    def report_throughput() -> None:
        """One run-level line, in the terms that comparing worker counts needs.

        Deliberately not per-document latency: the ``ingest.*`` spans measure
        that already, and it gets *worse* under concurrency even as the run gets
        faster — workers contend, so each document takes longer while more of
        them finish per minute. Throughput is the number that moves in the
        direction the setting is meant to move it.

        ``documents_processed`` is the budget's notion of work (``_WORKED_OUTCOMES``),
        so unchanged scans — which cost nothing and would otherwise inflate the
        rate — are excluded, and two runs over different-sized changed sets stay
        comparable.
        """
        elapsed = time.perf_counter() - started
        per_minute = (worked / elapsed * 60.0) if elapsed > 0 else 0.0
        logger.info(
            "ingest_throughput workers=%d elapsed_seconds=%.1f "
            "documents_processed=%d documents_per_minute=%.1f errors=%d "
            "enrichment_failures=%d indexed_without_date=%d",
            workers,
            elapsed,
            worked,
            per_minute,
            tally["error"],
            tally["enrich_failed"] + tally["enrich_error"],
            tally["undated"],
        )

    def handle(record: ChangeRecord) -> str:
        # Why this document ended unresolved, captured wherever it was decided:
        # `_handle` reports the reasons it can name, and the except below reports
        # the ones it cannot. Both end up on the retry row.
        reason: str | None = None

        def fail(message: str) -> None:
            nonlocal reason
            reason = message

        try:
            outcome = _handle(record, build_doc, run_id, note=note, fail=fail, flag=flag)
        except Exception as exc:
            logger.exception("Failed handling %s; skipping.", record.document_id)
            _log(run_id, record, "error", error=str(exc))
            outcome, reason = "error", str(exc)
        # Here rather than inside _handle: this is the only place that sees
        # every outcome, the raised ones included.
        _track_retry(record, outcome, pending_retries, error=reason)
        return outcome

    def account(outcome: str) -> None:
        nonlocal worked
        with tally_lock:
            tally[outcome] += 1
        # Outside the lock: `note` must not wait on a batch pause, and `worked`
        # is only ever touched here, on the main loop.
        if outcome in _WORKED_OUTCOMES:
            worked += 1
            if pause > 0 and batch_size > 0 and worked % batch_size == 0:
                time.sleep(pause)

    def budget_reached(record: ChangeRecord, pending: int) -> bool:
        # Stop only at a document boundary: a node's attachment records follow
        # it immediately and must land in the same run, or the node's state
        # row would hide them from the next crawl. In-flight documents count
        # pessimistically so the cap can never overshoot.
        if not max_docs or record.source_type == "pdf_attachment":
            return False
        if worked + pending < max_docs:
            return False
        logger.info(
            "Batch budget of %d documents reached; stopping cleanly "
            "(the next run resumes from the high-water mark).", max_docs,
        )
        tally["budget_stop"] = 1
        return True

    if workers == 1:
        for record in records:
            if budget_reached(record, pending=0):
                break
            account(handle(record))
        report_throughput()
        return tally

    # Parallel mode: the crawler stays single-threaded (per-run dedup and
    # node-before-attachment ordering live there); a bounded pool works the
    # heavy per-document I/O (download, extract, embed, index). Documents are
    # independent across MySQL (pooled connections, per-doc transactions) and
    # Qdrant (per-doc points); the one-run-at-a-time lock still applies.
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from app.core.clients import ensure_collection

    try:
        # Pre-create the collection and payload indexes once, so first-run
        # workers don't race the create call.
        ensure_collection()
    except Exception:
        logger.exception("Could not pre-create the collection; workers will retry.")
    _prewarm_clients(settings)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ingest") as pool:
        in_flight: set = set()
        for record in records:
            done = {f for f in in_flight if f.done()}
            in_flight -= done
            for future in done:
                account(future.result())
            if budget_reached(record, pending=len(in_flight)):
                break
            while len(in_flight) >= workers * 2:
                finished, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in finished:
                    account(future.result())
            in_flight.add(pool.submit(handle, record))
        if in_flight:
            finished, _ = wait(in_flight)
            for future in finished:
                account(future.result())
    report_throughput()
    return tally


def _build_drupal_doc(record: ChangeRecord) -> CanonicalDocument | None:
    from app.ingestion.canonical import from_drupal_record

    return from_drupal_record(record.payload)


def _build_drupal_or_attachment(
    record: ChangeRecord, session: "requests.Session"
) -> CanonicalDocument | None:
    if record.source_type == "pdf_attachment":
        from app.ingestion.extractors.attachment import build_attachment_doc

        return build_attachment_doc(record, session)
    return _build_drupal_doc(record)


def ingest_drupal(
    bundles: Iterable[str] | None = None,
    *,
    published_only: bool = True,
    reconcile_deletes: bool = False,
    extra_floors: "Mapping[str, int] | None" = None,
) -> Counter:
    from functools import partial

    from app.ingestion.extractors.drupal_extractor import _build_session

    with _exclusive("Drupal ingestion"):
        logger.info("Drupal ingestion started (bundles=%s, reconcile=%s)", bundles or "default", reconcile_deletes)
        records = cd.detect_drupal_changes(
            bundles,
            published_only=published_only,
            reconcile_deletes=reconcile_deletes,
            extra_floors=extra_floors,
        )
        # One session for the whole run: attachment downloads reuse its connection
        # pool rather than opening a new one per PDF.
        session = _build_session(get_settings().drupal_max_retries)
        try:
            tally = _run(records, partial(_build_drupal_or_attachment, session=session))
        finally:
            session.close()
        logger.info("Drupal ingestion finished: %s", dict(tally))
        return tally


def reconcile_dry_run(
    bundles: Iterable[str] | None = None, *, published_only: bool = True
) -> dict:
    """Report what a reconciling sweep would delete, without deleting anything.

    Runs the real thing as far as the decision and stops there: the same crawl,
    the same enumeration, the same completeness guard, the same per-candidate
    bundle-move confirmation, and the same query that decides which attachments
    an eviction would orphan. What it does not do is hand any record to
    ``_handle``, which is where every write lives — so no document is indexed, no
    row is written or deleted, no vector is touched, no retry marker moves, and
    the high-water mark, derived from rows that do not change, stays put.

    A bundle move needs one adjustment to be recognised here. The real run
    re-indexes the moved document under its new bundle, and the confirmation step
    then reads that back; a dry run indexes nothing, so the catalog still files
    it under the bundle it left. The crawl has already yielded it live under the
    new bundle by the end of the run, though, so the record of the run is enough
    to tell a move from a disappearance.

    Takes the one-run-at-a-time lock: this walks the whole site, and doing that
    alongside a real sweep helps nobody.
    """
    with _exclusive("Reconcile dry run"):
        logger.info(
            "DRY RUN starting (bundles=%s). Nothing will be deleted.",
            bundles or "default",
        )
        live_bundle: dict[str, str] = {}
        candidates: list[ChangeRecord] = []
        for record in cd.detect_drupal_changes(
            bundles, published_only=published_only, reconcile_deletes=True
        ):
            if record.status is ChangeStatus.DELETED:
                candidates.append(record)
            elif record.bundle is not None:
                live_bundle[record.document_id] = record.bundle

        moved = [
            c for c in candidates
            if live_bundle.get(c.document_id) not in (None, c.bundle)
        ]
        moved_ids = {c.document_id for c in moved}
        deleting = [c for c in candidates if c.document_id not in moved_ids]

        parents = [c.document_id for c in deleting]
        linked: list[str] = []
        for parent in parents:
            try:
                linked.extend(state.attachment_ids_for(parent))
            except Exception:
                logger.warning(
                    "Could not read %s's attachments; its PDFs are missing from "
                    "this report.", parent, exc_info=True,
                )
        try:
            orphaned = state.orphaned_attachments(linked, ignoring_parents=parents)
        except Exception:
            logger.warning("Could not resolve attachment orphans.", exc_info=True)
            orphaned = []

        by_bundle = Counter(c.bundle for c in deleting)
        report = {
            "dry_run": True,
            "documents": [
                {"document_id": c.document_id, "bundle": c.bundle,
                 "source_key": c.source_key}
                for c in deleting
            ],
            "attachments": orphaned,
            "moved": [
                {"document_id": c.document_id, "from_bundle": c.bundle,
                 "to_bundle": live_bundle[c.document_id]}
                for c in moved
            ],
            "by_bundle": dict(by_bundle),
            "linked_attachments_surviving": len(set(linked)) - len(orphaned),
        }

        logger.warning(
            "DRY RUN complete — nothing was deleted. Would remove %d document(s) "
            "and %d attachment(s). %d candidate(s) moved bundle and were spared; "
            "%d linked attachment(s) would survive on another parent. Per bundle: %s",
            len(deleting), len(orphaned), len(moved),
            report["linked_attachments_surviving"], dict(by_bundle) or "{}",
        )
        for entry in report["documents"]:
            logger.info(
                "DRY RUN would delete %s (%s) %s",
                entry["document_id"], entry["bundle"], entry["source_key"],
            )
        for attachment in orphaned:
            logger.info("DRY RUN would delete attachment %s (last parent going)", attachment)
        for entry in report["moved"]:
            logger.info(
                "DRY RUN sparing %s: moved %s -> %s",
                entry["document_id"], entry["from_bundle"], entry["to_bundle"],
            )
        return report


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="Incremental ingest of Drupal content into Qdrant.")
    parser.add_argument("--bundle", action="append", default=[], help="Limit Drupal crawl to bundle(s).")
    parser.add_argument("--reconcile", action="store_true", help="Also reconcile Drupal deletes/unpublishes.")
    parser.add_argument("--include-unpublished", action="store_true", help="Include unpublished Drupal nodes.")
    parser.add_argument(
        "--dry-run-reconcile", action="store_true",
        help="Report what reconciliation would delete, and delete nothing. Ingests nothing either.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.dry_run_reconcile:
        report = reconcile_dry_run(
            args.bundle or None, published_only=not args.include_unpublished
        )
        print(
            f"DRY RUN (nothing deleted): {len(report['documents'])} document(s), "
            f"{len(report['attachments'])} attachment(s), "
            f"{len(report['moved'])} spared as bundle moves. "
            f"Per bundle: {report['by_bundle']}"
        )
        return 0

    tally = ingest_drupal(
        args.bundle or None,
        published_only=not args.include_unpublished,
        reconcile_deletes=args.reconcile,
    )
    print(f"Drupal: {dict(tally)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
