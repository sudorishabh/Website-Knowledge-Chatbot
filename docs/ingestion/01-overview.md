# 01 — Ingestion Pipeline Overview

## What the pipeline does

It keeps a searchable copy of the TERI website in step with the website.

Concretely: it walks the site's JSON:API, notices what has appeared, changed or
gone since last time, downloads and reads the PDFs those pages carry, breaks the
text into retrievable pieces, embeds them, writes them to a vector store, and
records in a relational catalog exactly what it did and why. It runs on a timer,
without supervision, and it is expected to survive the site being slow, the
model deployment being throttled, a PDF being corrupt, and the process being
killed mid-document.

## Why it exists

The retrieval side of the application answers questions from vectors and
payloads in Qdrant. Nothing else puts content there. So the ingestion pipeline is
the only path by which the corpus can be correct, current or complete, and every
retrieval defect is downstream of a decision made here.

Three properties drove most of the design:

1. **The source is a CMS, not a feed.** There is no change stream. Change has to
   be *inferred* from a `changed` timestamp and from content hashes, and the
   inference has to be conservative in both directions — a missed change leaves a
   stale answer, and a false change re-embeds the corpus at full cost.
2. **Most of the substance is in PDFs.** A page often carries a title, a
   paragraph and a link to a 90-page report. The report is the answer, so PDFs
   are ingested as documents in their own right, with their own extraction,
   dating and lifecycle.
3. **A wrong write is worse than a late write.** Deletion is irreversible, a
   wrong effective date is acted on silently, and an empty extraction that
   replaces a good document is invisible. So the pipeline fails *open* on
   external dependencies (a failure costs a log line and a retry) and fails
   *closed* on anything that would change or remove content on weak evidence.

## The major components

```
                       +--------------------------------------+
   Drupal JSON:API --->|  Change detection (generator)        |
   teriin.org/jsonapi  |  app/ingestion/change_detection      |
                       +----------------+---------------------+
                                        | ChangeRecord stream
                                        v
   PDF bytes over  --->+--------------------------------------+
   HTTPS               |  Per-document handler  _handle       |
                       |  app/ingestion/pipeline.py           |
                       +---+--------+--------+--------+-------+
                           |        |        |        |
               extraction  | dating | chunk  | embed  | persist
                           v        v        v        v
        +--------------------+ +--------+ +------------+ +--------------+
        | PyMuPDF / Azure DI | | date_* | | Azure      | | MySQL        |
        | Camelot            | | rules  | | embeddings | | catalog+log  |
        +--------------------+ +--------+ +-----+------+ +--------------+
                                               |
                                               v
                                         +----------+
                                         |  Qdrant  |
                                         +----------+
                     per document, once indexed: knowledge stage -> Neo4j
                          after the sweep: knowledge catch-up, graph
                                 projection, cross-store reconciliation
```

| Component | Module | Role |
| --- | --- | --- |
| Ingestion server | `app/ingest_main.py` | FastAPI process hosting the sweep scheduler and the control plane. Not ready without MySQL. |
| Sweep scheduler | `app/workers/scheduler.py` | Fires a sweep every `worker_sweep_interval_seconds`; the first run is immediate. |
| Sweep task | `app/workers/tasks.py` | `ingest_drupal` → knowledge catch-up → graph projection → reconciliation. |
| Run coordinator | `app/ingestion/pipeline.py` | Mutual exclusion, the run loop, the budget, parallelism, the per-document handler, retry bookkeeping. |
| Change detection | `app/ingestion/change_detection/` | Yields `NEW`/`CHANGED`/`UNCHANGED`/`DELETED` records; owns the crawl window and delete reconciliation. |
| Source extractor | `app/ingestion/extractors/drupal_extractor.py` | JSON:API paging, relationship resolution, HTML→text, PDF discovery. |
| PDF extraction | `app/ingestion/extractors/{pdf_extractor,pymupdf_local,camelot_tables,text_normalize}.py` | Per-page routing between local text, OCR and table extraction, then normalisation. |
| Date resolution | `app/ingestion/bundle_dates.py`, `date_{evidence,rules,llm,resolution}.py`, `source_dates.py` | Decides `effective_start_date` (and `effective_end_date` for bundles whose content covers a period) from the bundle -> date-field mapping. A page's dates pass to its lone attachment unchanged; where several PDFs share a page, each is dated from its own name (`title_dates.py`) or left undated. |
| Canonical model | `app/core/models/document.py`, `app/ingestion/canonical.py` | The one document shape everything converges on. |
| Chunking | `app/ingestion/chunking/` | Structure-aware parent/child windows, chunk identity, payload. |
| Indexer | `app/ingestion/indexer.py` | Vector reuse, embedding, batched upsert. |
| Vector store gateway | `app/core/clients/vector_store.py` | Collection creation, payload indexes, dimension validation, scoped delete. |
| Catalog | `app/catalog/` | `documents` and its child tables, the audit log, retry markers, dead links, the enrichment cache, date decisions. |
| Knowledge stage | `app/ingestion/knowledge_sync.py` | Runs per document, right after it is indexed (`process_after_index`), so knowledge does not wait for a corpus pass; never fails ingestion. |
| Post-sweep stages | `app/ingestion/{knowledge_sync,graph_sync,reconcile}.py` | Knowledge catch-up (`knowledge_sync.catch_up`, for documents whose per-document stage did not land), graph projection, cross-store verification. |
| Repair tools | `app/ingestion/{recovery,reprocess,backfill,enrich_backfill}.py`, `scripts/` | Bring back stranded documents, rebuild after code changes, backfill. |

## The complete lifecycle

A single sweep, end to end:

1. **Trigger.** The scheduler (or an operator, or a CLI) calls `sweep()`. A
   process-local non-blocking lock is taken; a second concurrent run is refused
   with `IngestBusyError` (HTTP 409).
2. **Preparation.** The catalog tables are ensured, retry markers are read once,
   a `run_id` is minted, and in parallel mode the Qdrant collection, the
   embeddings client and the tokenizer are pre-warmed.
3. **Crawl.** For each configured source (15 node bundles plus
   `block_content:basic`), the incremental window is computed
   (`changed >= high-water mark`, pulled back to the earliest unresolved
   failure), and records are yielded oldest-first — a `website` record for the
   page, then one `pdf_attachment` record per PDF it carries.
4. **Per document.** Each record goes through `_handle`:
   - `DELETED` → remove points, remove the row, collect orphaned attachments.
   - `UNCHANGED` → nothing (optionally logged).
   - otherwise → build the canonical document (download + extract + date),
     compute the content hash, optionally enrich, and then either refresh the
     fingerprint (`unchanged_content`) or chunk → embed → upsert → swap →
     persist (`indexed`). An `indexed` document is handed to the knowledge stage
     immediately, in the same call — the document does not wait for a
     corpus-wide builder to notice it (see
     [09](09-knowledge-layer-and-graph.md)). That call's result is discarded:
     nothing about it can unmake a document that is already indexed.
5. **Accounting.** Every outcome is tallied, throttled against the batch budget,
   and turned into a retry marker (written on failure, cleared on success).
6. **Post-sweep.** Knowledge catch-up for documents whose per-document knowledge
   stage did not land (it errored, was cut short by its budget, or never ran),
   then graph projection, then cross-store reconciliation. None of these can
   fail the sweep.
7. **Housekeeping.** Semantic-cache prune, ingest-log retention prune, sleep.

The run returns a `Counter` of outcomes: `indexed`, `deleted`, `skipped`,
`unchanged`, `unchanged_content`, `error`, `undated`, `budget_stop`, and seven
`enrich_*` counters (`enrich_hit`, `enrich_stored`, `enrich_skipped`,
`enrich_exhausted`, `enrich_failed`, `enrich_aborted`, `enrich_error` —
`enrich_off` is never counted; `_run`'s `note()` filters it out).

### Diagram to include: end-to-end sequence

A swimlane sequence diagram with lanes for **Scheduler**, **Crawl**, **Document
handler**, **Azure (DI + embeddings)**, **Qdrant**, **MySQL**, **Neo4j**. It
should show: the sweep tick; the lock acquisition; one node record and its
attachment record arriving in order; the download and extraction round trip; the
embed call; the upsert *followed by* the scoped delete (this ordering is the
point of the diagram); the single catalog transaction; the log append; and the
three post-sweep steps hanging off the end with dashed "cannot fail the sweep"
edges.

## Batch, not streaming

This is a **batch, poll-based** pipeline. There is no queue, no event stream and
no real-time path. The unit of scheduling is a sweep; the unit of work is a
document. The crawl is a generator, so crawling and per-document work interleave
and memory stays bounded regardless of corpus size — but the pipeline is
deliberately not a streaming system, because the source cannot push, and because
"exactly once" is achieved by making every write idempotent on a deterministic
key rather than by delivery guarantees.

The one out-of-band path is `POST /ingest/article`, which indexes an
operator-supplied document immediately and keeps no change-detection state. It is
an injection hatch, not a second pipeline — see
[03](03-triggers-and-control-plane.md#4-post-ingestarticle).

## Vocabulary

These terms are used precisely throughout the set.

| Term | Meaning |
| --- | --- |
| **Sweep** | One scheduled pass: an ingestion run plus the post-sweep stages. |
| **Run** | One invocation of `ingest_drupal`. Has a `run_id` stamped on every log row. |
| **Source** | An `(entity_type, bundle, incremental)` triple, e.g. `("node", "news", True)`. |
| **Bundle** | A Drupal content type (`news`, `report`, …) or block type (`basic`). |
| **Record** (`ChangeRecord`) | One unit of work: a document id, a status, a fingerprint and a payload. |
| **Document** | A `website` page/block, or a `pdf_attachment`. Each has its own catalog row and its own points. |
| **`document_id`** | Drupal's UUID for a node/block; the file UUID for an attachment; `inbody:<sha1(url)>` for an in-body PDF link. |
| **Fingerprint** | The crawl's change token. The node's `changed` timestamp, or the in-body UUID. |
| **`content_hash`** | SHA-256 of the document's **body text only**. Decides whether to re-index. |
| **`changed_mark`** | The record's `changed` timestamp as a Unix integer. Positions the crawl cursor. |
| **`doc_version`** | Monotonic per-document version, incremented on every real re-index. |
| **`PIPELINE_VERSION`** | Which code produced a document (`c1.i1.p1.e1`). A second, independent change signal. |
| **Parent / child chunk** | A child is an embedded retrieval unit; a parent is the wider window it sits in, stored as a zero vector for context expansion. |
| **Facet** | A multi-valued document attribute stored one row per value: author, tag, theme. |
| **Retry marker** | A row in `documents_retry` recording that a document reached processing and did not come out indexed. |
| **Floor** | The earliest unresolved crawl position for a bundle, used to widen the incremental window. |
| **Fails open** | An external dependency failing costs a log line and, at worst, one document's freshness — never the run. |
| **Fails closed** | Weak or missing evidence leaves the existing state untouched rather than acting on it. |

## Cross-cutting invariants

Everything in the rest of this set upholds these. If you change the pipeline,
these are what you must not break.

1. **One corpus-wide run at a time**, enforced by a process-local lock.
2. **A document is never replaced by nothing.** An empty extraction is an `error`
   that keeps the previous version, not a statement that the document is empty.
3. **New points are written before old ones are deleted.** The document is
   searchable throughout an update, and an interrupted update leaves the previous
   version intact.
4. **Every catalog write on the ingest path fails open.** An unreachable database
   costs one warning.
5. **Every write is idempotent on a deterministic key**, so a retry re-derives
   rather than duplicates.
6. **`content_hash` covers body text and nothing else**, so it is reproducible
   from the source bytes and cannot drift across runs.
7. **Deletion requires a live enumeration the code is willing to believe.**
8. **Nothing invents a date.** An undated document stays undated, is logged and
   is counted.
9. **The knowledge layer and the graph cannot fail an ingestion.** They are
   derived stores, re-derivable from MySQL.

---

Next: [02 — Sources and Data Acquisition](02-sources-and-acquisition.md)
