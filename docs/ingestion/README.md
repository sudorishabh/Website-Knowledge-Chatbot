# Ingestion Pipeline Documentation

This is the complete, start-to-finish description of how content gets from the
TERI Drupal website into the stores that answer questions: **Qdrant** (chunk
vectors and payloads), **MySQL** (the document catalog, audit log and operational
state) and, optionally, **Neo4j** (the knowledge graph).

Everything here is written from the code in `app/ingestion`, `app/catalog`,
`app/core/clients`, `app/knowledge`, `app/workers` and `app/api`. Where a
behaviour has a non-obvious reason, the reason is given — most of the surprising
choices in this pipeline exist because the simpler version was tried and broke
something specific.

**For the codebase as a whole** — how the write path relates to the read path,
the layering rules, and where new code belongs — see
[`app/README.md`](../../app/README.md). This set is the write path in depth.

## Read in this order

| Doc | What it covers |
| --- | --- |
| [01 — Overview](01-overview.md) | What the pipeline does, why it exists, the components, the whole lifecycle in one page, and the vocabulary the rest of the set uses. |
| [02 — Sources and Data Acquisition](02-sources-and-acquisition.md) | The Drupal JSON:API, how a bundle is walked, how a record is turned into text, and how attached and in-body PDFs are discovered. |
| [03 — Triggers, Transport and the Control Plane](03-triggers-and-control-plane.md) | The five ways a run starts, the HTTP control plane, authentication and authorization, mutual exclusion, and how work is transported and throttled. |
| [04 — Change Detection and Versioning](04-change-detection-and-versioning.md) | Fingerprints, content hashes, pipeline versions, the incremental crawl window, retry floors, and delete reconciliation. |
| [05 — Extraction and Normalisation](05-extraction-and-normalisation.md) | HTML flattening, the hybrid PDF router (PyMuPDF / Azure Document Intelligence / Camelot), and page-text normalisation. |
| [06 — The Canonical Document and Date Resolution](06-canonical-document-and-dates.md) | `CanonicalDocument`, facet routing, theme hierarchy, the bundle → date-field mapping (one field, or a start/end pair), how a page's dates reach its lone attachment, and how a PDF sharing its page is dated from its own name or left undated. |
| [07 — Chunking, Embedding and Indexing](07-chunking-embedding-indexing.md) | Parent/child chunking, chunk identity, payload construction, vector reuse, and the safe index-then-delete swap. |
| [08 — Persistence and the Catalog](08-persistence-and-catalog.md) | Every MySQL table, the single-transaction write, facet replacement, attachment links and orphan collection. |
| [09 — The Knowledge Layer and Graph](09-knowledge-layer-and-graph.md) | The optional post-index knowledge stage, the catch-up sweep, and graph projection. |
| [10 — Failures, Retries and Recovery](10-failures-retries-and-recovery.md) | Every failure mode, what the system does about it, and the recovery tools. |
| [11 — Observability, Monitoring and Alerting](11-observability-and-monitoring.md) | Logs, run tallies, spans, timing metrics, cross-store reconciliation and what is worth alerting on. |
| [12 — Operations, Configuration and Troubleshooting](12-operations-and-troubleshooting.md) | Runbooks, the full configuration reference, deployment notes, a troubleshooting matrix and the end-to-end completion criteria. |

Alongside the numbered set:

| Doc | What it covers |
| --- | --- |
| [Bundle-specific date capture — implementation plan](bundle-date-capture-plan.md) | The current-state analysis, the validated bundle → date-field mapping, the discrepancies found against the live CMS and how each was resolved, and the migration. Revision 2 covers date ranges. Written before each change and kept as the record of why it is shaped this way. |

## Topic map

Where each cross-cutting concern is covered, for readers arriving with a specific
question rather than reading front to back.

| Topic | Primary | Also |
| --- | --- | --- |
| Source systems and input data | [02](02-sources-and-acquisition.md#the-source-system) | [01](01-overview.md#why-it-exists) |
| Extraction and collection | [02](02-sources-and-acquisition.md#walking-a-bundle) | [05](05-extraction-and-normalisation.md) |
| Ingestion interfaces (HTTP, CLI, timer) | [03](03-triggers-and-control-plane.md#the-five-ways-a-run-starts) | |
| Transport | [02](02-sources-and-acquisition.md#transport-the-http-session) | [03](03-triggers-and-control-plane.md#backpressure-and-rate-limiting) |
| Authentication and authorization | [03](03-triggers-and-control-plane.md#authentication-and-authorization) | [12](12-operations-and-troubleshooting.md#security-and-access-control) |
| Validation and schema checks | every doc's *Validation* section | [08](08-persistence-and-catalog.md#schema-management) |
| Parsing and transformation | [05](05-extraction-and-normalisation.md) | [06](06-canonical-document-and-dates.md) |
| Cleansing and normalisation | [05](05-extraction-and-normalisation.md#page-text-normalisation) | |
| Enrichment | [08](08-persistence-and-catalog.md#the-enrichment-cache) | [09](09-knowledge-layer-and-graph.md) |
| Deduplication | [04](04-change-detection-and-versioning.md#deduplication-every-mechanism-in-one-place) | |
| Data quality checks | [11](11-observability-and-monitoring.md#cross-store-reconciliation) | |
| Error handling and failure scenarios | [10](10-failures-retries-and-recovery.md) | every doc's *Failure scenarios* table |
| Retry mechanisms | [10](10-failures-retries-and-recovery.md#retry-markers-in-operation) | [04](04-change-detection-and-versioning.md#retry-markers) |
| Queues, buffers, async processing | [03](03-triggers-and-control-plane.md#sequential-vs-parallel) — there is no queue, and why | [01](01-overview.md#batch-not-streaming) |
| Batch vs streaming | [01](01-overview.md#batch-not-streaming) | |
| Processing stages and dependencies | [01](01-overview.md#the-complete-lifecycle) | docs 04–09 in order |
| Storage and persistence | [08](08-persistence-and-catalog.md) | [07](07-chunking-embedding-indexing.md#the-collection) |
| Metadata and lineage | [04](04-change-detection-and-versioning.md#pipeline-version-the-second-change-signal) | [06](06-canonical-document-and-dates.md#the-decision-record), [08](08-persistence-and-catalog.md#ingest_log) |
| Monitoring, logging, observability | [11](11-observability-and-monitoring.md) | |
| Alerting | [11](11-observability-and-monitoring.md#what-to-alert-on) | |
| Idempotency and replay | [04](04-change-detection-and-versioning.md#idempotency-and-replay) | [10](10-failures-retries-and-recovery.md#replay-and-reprocessing-which-tool-for-which-problem) |
| Ordering and out-of-order events | [04](04-change-detection-and-versioning.md#ordering-and-out-of-order-events) | |
| Partial failures and recovery | [10](10-failures-retries-and-recovery.md) | |
| Backpressure and performance | [03](03-triggers-and-control-plane.md#backpressure-and-rate-limiting) | [12](12-operations-and-troubleshooting.md#reliability-and-scalability) |
| Security and access controls | [12](12-operations-and-troubleshooting.md#security-and-access-control) | [03](03-triggers-and-control-plane.md#authentication-and-authorization) |
| Scalability and reliability | [12](12-operations-and-troubleshooting.md#reliability-and-scalability) | [01](01-overview.md#cross-cutting-invariants) |
| Operational procedures | [12](12-operations-and-troubleshooting.md#runbooks) | |
| Troubleshooting | [12](12-operations-and-troubleshooting.md#troubleshooting-matrix) | [11](11-observability-and-monitoring.md#diagnostic-recipes) |
| Deployment and configuration | [12](12-operations-and-troubleshooting.md#deployment) | [12, config reference](12-operations-and-troubleshooting.md#configuration-reference) |
| Downstream handoff and consumption | [08](08-persistence-and-catalog.md#downstream-handoff-and-consumption) | [07](07-chunking-embedding-indexing.md#stage-4--the-payload) |
| End-to-end completion criteria | [12](12-operations-and-troubleshooting.md#end-to-end-completion-criteria) | |

## Where the code is

Each document to the modules it describes. The layout is enforced by
`tests/test_architecture.py`, so these paths do not silently drift.

| Doc | Modules |
| --- | --- |
| 02 | `app/ingestion/extractors/drupal_extractor.py` · `extractors/attachment.py` · `app/core/corpus.py` (the bundle vocabulary) |
| 03 | `app/ingest_main.py` · `app/workers/{scheduler,tasks}.py` · `app/api/{ingest,auth,health}.py` · `app/ingestion/pipeline.py` (`_exclusive`, `_run`) |
| 04 | `app/ingestion/change_detection/{base,drupal}.py` · `app/ingestion/version.py` · `app/catalog/retries.py` |
| 05 | `app/ingestion/extractors/{pdf_extractor,pymupdf_local,camelot_tables,text_normalize}.py` |
| 06 | `app/core/models/document.py` · `app/ingestion/canonical.py` · `app/ingestion/bundle_dates.py` · `app/ingestion/source_dates.py` · `app/ingestion/date_{evidence,rules,llm,resolution}.py` · `app/core/editions.py` · `app/catalog/theme_taxonomy.py` · `app/ingestion/date_candidates.py` (measurement-only DocInfo/shadow-correction helper, not wired into the live decision) |
| 07 | `app/ingestion/chunking/*` · `app/ingestion/indexer.py` · `app/core/clients/vector_store.py` |
| 08 | `app/catalog/*` (schema, state, log, retries, dead_links, enrichment, date_decisions) · `app/core/clients/database.py` |
| 09 | `app/ingestion/{knowledge_sync,graph_sync}.py` · `app/knowledge/document_pipeline.py` · `app/knowledge/document_loader.py` · `app/catalog/knowledge_runs.py` |
| 10 | `app/ingestion/{recovery,reprocess,backfill,enrich_backfill}.py` · `app/catalog/{retries,dead_links}.py` · `scripts/{recover_stranded,reprocess_corpus}.py` |
| 11 | `app/observability/{tracing,metrics}.py` · `app/ingestion/reconcile.py` · `app/catalog/log.py` · `app/api/health.py` · `scripts/verify_corpus.py` |
| 12 | `app/config.py` · `docker-compose.yml` · `pytest.ini` · `scripts/*` · `tools/local_tests/` |

Two paths in this table moved during a codebase reorganisation and are worth
noting because older notes may still name the old ones:

- `DEFAULT_BUNDLES` is in **`app/core/corpus.py`**, not in the Drupal extractor.
  It is shared vocabulary: the read path needs the identical list, and used to
  import it from a write-path extractor. `drupal_extractor` re-exports it.
- The manual harness is **`tools/local_tests/`**, not `app/local_tests/` — it is
  developer tooling and nothing in `app/` imports it.

## If you are here for one thing

- **"Is it working?"** → [11](11-observability-and-monitoring.md), then `GET /metrics`.
- **"A document is missing/wrong."** → [12, Troubleshooting](12-operations-and-troubleshooting.md#troubleshooting-matrix).
- **"I changed the chunker."** → [04, Pipeline versions](04-change-detection-and-versioning.md#pipeline-version-the-second-change-signal) and [12, Reprocess the corpus](12-operations-and-troubleshooting.md#rebuild-the-corpus-after-a-code-change).
- **"Why does this PDF have that date?"** → [06, PDF date resolution](06-canonical-document-and-dates.md#pdf-publication-date-resolution).
- **"Deletes look wrong."** → [04, Delete reconciliation](04-change-detection-and-versioning.md#delete-reconciliation).
