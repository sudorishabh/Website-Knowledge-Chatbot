# 12 — Operations, Configuration and Troubleshooting

**Purpose.** Everything you need to run this pipeline: how to deploy it, how to
configure it, the runbooks for the things you will actually do, a symptom-driven
troubleshooting table, and the definition of "the ingestion completed
successfully".

---

## Deployment

### Processes

```bash
# Ingestion server: sweep scheduler + control plane. One instance only.
uvicorn app.ingest_main:app --port 8001

# Retrieval server: answers questions. Scale horizontally.
uvicorn app.main:app --port 8000
```

**Exactly one ingestion server.** The one-run-at-a-time lock is a
`threading.Lock`, process-local by design. Two ingestion servers against the same
MySQL and Qdrant will double-embed documents and race each other's writes. There is
no distributed lock.

### Backing services

```bash
docker compose up -d        # qdrant + neo4j
```

- **Qdrant** — `qdrant/qdrant:latest`, ports 6333/6334, volume
  `qdrant_storage`.
- **Neo4j** — `neo4j:2026.07.1-community`, ports 7474/7687, volume `neo4j_data`
  declared **external** so adopting compose keeps an existing graph rather than
  starting an empty one. Stop and remove any hand-started container first, or the
  port bindings collide. The image is **pinned**, not `:latest`, because the graph
  schema is written against what Community edition actually supports (it rejects
  `NODE KEY` and existence constraints) and a silent major-version bump would change
  which DDL is legal.
- **MySQL** — not in compose; provide your own. `NEO4J_PASSWORD` must be set in
  `.env` or compose refuses to start.
- **Redis** — optional, for the semantic answer cache. Not used by ingestion except
  that the sweep loop prunes the cache.

### Dependencies worth knowing about

From `requirements.txt`:

| Package | Needed for | If missing |
| --- | --- | --- |
| `PyMuPDF` | page classification and local text — **the backbone of PDF extraction** | PDFs cannot be read at all |
| `azure-ai-documentintelligence` | OCR for scanned pages | Scanned pages produce nothing |
| `camelot-py[base]` | born-digital table extraction | Tables degrade to plain text, with a warning |
| `tiktoken` | accurate chunk token counts | **Falls back to a ~4-chars/token heuristic** — chunk sizes differ, so do not mix the two across a corpus |
| `pymysql` + `cryptography` | the catalog | Ingestion cannot start |
| `qdrant-client` | the vector store | Ingestion cannot start |
| `langchain-openai` + `openai` + `httpx` | embeddings, including the throttle gate | Ingestion cannot start |
| `neo4j` | the knowledge graph | Only matters when `knowledge_enabled` |
| `PyJWT` | control-plane auth | Auth cannot be enabled |
| `opentelemetry-*` | OTLP export | Commented out; tracing falls back to in-process only |

Camelot's `lattice` flavor also needs **Ghostscript** on the host.

### First-run checklist

1. Set `.env` from `.env.example`. At minimum: MySQL connection, `qdrant_url`,
   `azure_openai_embedding_*`, `drupal_jsonapi_base`.
2. Set `azure_openai_embedding_dimensions` to match the model you intend to keep.
   **Changing it later means re-embedding the corpus.**
3. Configure `azure_document_intelligence_*` unless you are content for scanned PDFs
   to produce nothing.
4. Set `jwt_secret` and `ingest_admin_group` (or `ops_admin_group`), or accept that
   any authenticated caller may drive ingestion. Leaving both unset logs a WARNING on
   every admin call.
5. Start the ingestion server and check `GET /ready` returns 200.
6. `ensure_collection()` creates the collection **and all thirteen payload indexes**
   on the first run — nothing needs to be run by hand.
7. Run a bounded first pass:

```bash
python -m app.workers.tasks drupal --bundle news
```

8. Check the tally, then `python -m scripts.verify_corpus`.
9. Set `worker_sweep_interval_seconds` and let the scheduler take over.

### Upgrading an existing deployment

| Situation | Action, once |
| --- | --- |
| Tables still named `ingest_state*` | `python -m scripts.rename_catalog_tables` **before or at deploy**, or new tables are created empty beside the old ones |
| `source_type` rows still say `article` | `python -m scripts.migrate_source_type_website`. Change detection loads both in the meantime, so nothing breaks while you wait |
| `documents_theme` value column still `category` | Automatic, in `migrate_renamed_facets` on `ensure_state_table` |
| Collection predates a payload index | `python -m scripts.create_payload_indexes`, `python -m scripts.create_fulltext_index` — **while no ingestion is running** |
| A pipeline component was bumped | `python -m scripts.reprocess_corpus` |
| Dates need re-deriving | `python -m scripts.backfill_bundle_dates` (dry run first), then `scripts.backfill_date_provenance` |

---

## Configuration reference

Everything is read from environment variables / `.env` via `app/config.py`
(pydantic-settings, `extra="ignore"`). `get_settings()` is `lru_cache`-d, so
**changing a value requires a restart** — except where a tool deliberately mutates the
settings object for the duration of one call (`reprocess._limits`).

### Source

| Setting | Default | Notes |
| --- | --- | --- |
| `drupal_jsonapi_base` | `https://teriin.org/jsonapi` | The site base for URL resolution is derived by splitting on `/jsonapi`. |
| `drupal_request_timeout` | `60` | Used for JSON:API pages **and** PDF downloads. |
| `drupal_page_size` | `50` | Raising it reduces round trips and increases the cost of one failed page. |
| `drupal_max_retries` | `3` | Transport retries for 429/5xx with backoff, honouring `Retry-After`. |
| `drupal_ingest_external_pdfs` | `false` | Download non-TERI in-body PDFs. |
| `drupal_block_min_chars` | `200` | Boilerplate-block threshold (unless the block carries a PDF). |

### PDF extraction

| Setting | Default | Notes |
| --- | --- | --- |
| `extraction_mode` | `hybrid` | `hybrid` / `azure_only` / `local_only`. |
| `pdf_scanned_char_threshold` | `100` | Below this many characters a page routes to OCR. |
| `azure_document_intelligence_endpoint` / `_key` | `""` | Unset disables OCR. |
| `azure_document_intelligence_model` | `prebuilt-read` | `prebuilt-layout` adds tables and Markdown at ~6× the cost. |
| `camelot_flavor` | `lattice` | Needs Ghostscript; `stream` is the automatic second pass per page. |
| `pdf_detect_ruled_grid` | `false` | Extra table tier — noisy on designed PDFs. |
| `pdf_table_min_grid_lines` | `3` | Distinct ruling positions needed on **both** axes. |
| `pdf_detect_borderless_tables` | `false` | Extra table tier. |
| `pdf_borderless_min_aligned_rows` | `4` | |
| `pdf_borderless_min_columns` | `3` | Internal columns, not just a left margin. |
| `pdf_running_header_min_fraction` | `0.5` | `0` disables running-header stripping. |
| `pdf_drop_number_soup` | `true` | Chart axis/data-region stripping. |

### Embedding and vector store

| Setting | Default | Notes |
| --- | --- | --- |
| `azure_openai_embedding_model` | `""` | Deployment name. Part of `embedding_version()`. |
| `azure_openai_embedding_endpoint` / `_key` / `_api_version` | `""` / `""` / `2024-06-01` | Endpoint and api-version are deliberately **not** part of `embedding_version()`. |
| `azure_openai_embedding_dimensions` | `3072` | Validated against the collection. 1536 halves storage and search cost on `text-embedding-3-*` with negligible retrieval loss. Blank for ada-002. |
| `azure_openai_embedding_max_retries` | `8` | Higher than the SDK's 2, to ride out an Azure throttling window. |
| `azure_openai_embedding_max_throttle_seconds` | `60.0` | Ceiling on one throttle pause, and the fallback for a missing `Retry-After`. |
| `qdrant_url` / `qdrant_api_key` / `qdrant_collection` | `http://localhost:6333` / none / `documents` | |

### Run control

| Setting | Default | Notes |
| --- | --- | --- |
| `worker_sweep_interval_seconds` | `3600` | `<= 0` disables the scheduler. First sweep is immediate. |
| `worker_sweep_reconcile` | `false` | **Run the dry run before turning this on.** |
| `ingest_max_docs_per_run` | `0` | Worked-document cap; `0` = unlimited. |
| `ingest_batch_size` | `0` | Throttle every N worked documents. |
| `ingest_batch_pause_seconds` | `0.0` | Pause duration. |
| `ingest_workers` | `1` | **Keep below `mysql_pool_size`.** |
| `ingest_reconcile_max_missing_ratio` | `0.10` | Share of a bundle one run may delete. |
| `ingest_reconcile_min_deletions` | `2` | Absolute allowance below that ratio. |

### Catalog and log

| Setting | Default | Notes |
| --- | --- | --- |
| `mysql_host` / `_port` / `_user` / `_password` / `_database` | `localhost` / `3306` / `""` / — / `""` | |
| `mysql_connect_timeout` | `10` | |
| `mysql_pool_size` | `5` | |
| `mysql_pool_timeout` | `30` | Fail-fast checkout wait. |
| `ingest_state_table` | `documents` | Prefix for every catalog table. Validated by `safe_table`. |
| `ingest_log_table` | `ingest_log` | |
| `ingest_log_enabled` | `true` | |
| `ingest_log_unchanged` | `false` | On = one INSERT+commit per document per sweep. |
| `ingest_log_retention_days` | `90` | **Also how far back `recover_stranded` can see.** `0` disables pruning. |

### Dates and enrichment

| Setting | Default | Notes |
| --- | --- | --- |
| `date_resolution_enabled` | `true` | Off = every PDF inherits its node's date; decisions are still recorded. |
| `enrichment_enabled` | `false` | Launches off — the first pass over an existing corpus costs real money, so it should be a deliberate act. |
| `enrichment_max_attempts` | `3` | Reset by a prompt/model change. |

### Knowledge layer

| Setting | Default | Notes |
| --- | --- | --- |
| `knowledge_enabled` | `false` | Master switch. Off = no Neo4j connection is ever opened. |
| `knowledge_process_after_index` | `false` | Build knowledge on the ingest path. |
| `knowledge_project_per_document` | `true` | Inert while its parent flag is off. |
| `knowledge_stage_budget_seconds` | `30.0` | |
| `knowledge_llm_max_calls_per_document` | `8` | |
| `knowledge_stage_max_attempts` | `3` | |
| `claim_extraction_enabled` | `false` | |
| `graph_project_after_sweep` | `true` | Gated by `knowledge_enabled`. |
| `graph_projection_max_age_seconds` | `86400` | Staleness tolerance. |
| `neo4j_uri` / `_user` / `_password` / `_database` / `_connection_timeout` | `bolt://localhost:7687` / `neo4j` / — / `neo4j` / `10.0` | Community supports exactly one database, named `neo4j`. |

### Security and ops

| Setting | Default | Notes |
| --- | --- | --- |
| `ingest_auth_enabled` | **`true`** | Auth for the whole control plane, independent of `auth_enabled`. |
| `ingest_admin_group` | `""` | Group for mutating routes; falls back to `ops_admin_group`. **Unset = any authenticated caller, logged as a WARNING.** |
| `auth_enabled` | `false` | Retrieval API auth. |
| `jwt_secret` | `""` | Required when either auth switch is on. Unset while required = HTTP 500. |
| `jwt_algorithms` | `HS256` | Comma-separated. |
| `jwt_audience` / `jwt_issuer` | `""` | Verified when set; audience verification is skipped when unset. |
| `jwt_groups_claim` | `groups` | String or list. |
| `ops_detail_enabled` | `false` | Whether `/ready` and `/metrics` bodies are visible without a group. |
| `ops_admin_group` | `""` | Group that may see `/metrics`. Only honoured when `auth_enabled`. |
| `verify_corpus_after_sweep` | `true` | Cross-store reconciliation. |
| `metrics_log_enabled` | `true` | `rag_metrics` log lines. |
| `otel_enabled` | `false` | |
| `otel_service_name` | `agentic-rag` | |
| `otel_exporter_otlp_endpoint` | `""` | Unset = in-process spans only. |

---

## Security and access control

| Surface | Control |
| --- | --- |
| Ingestion control plane | Bearer JWT, on by default (`ingest_auth_enabled`); mutating routes additionally require a group |
| `/metrics`, `/metrics/timings` | Ops-gated; **404** to everyone else, so the endpoints are hidden rather than advertised |
| `/ready` body | Only detailed when `ops_detail_enabled` — error strings and point counts fingerprint the deployment |
| Table names from configuration | `safe_table()` requires alphanumerics-plus-underscore, so a bad setting cannot become an injection vector via f-string interpolation |
| SQL parameters | Every value is bound; only validated identifiers are interpolated |
| Source credentials | **None.** The crawl is anonymous, which is also why unpublished content is indistinguishable from deleted |
| Document-level access control | **None, by design.** The corpus is public: `tenant_id` and `acl` are not written and not indexed. Do not ingest non-public content into this collection |
| Groups | Only ever from a verified token, never from a request body. They widen access to ops endpoints; they do **not** scope retrieval |
| Neo4j | Community has no RBAC, so the read-only boundary is enforced in code (`read_session` vs `write_session`) |
| Secrets | Environment / `.env` only. `embedding_version()` deliberately excludes the key, so **rotating a secret does not re-embed the corpus** |
| PII | Author names and `raw_meta` are stored verbatim from a public CMS. `ingest_log.error_message` can contain URLs and exception text — which is why the read-only log route still requires authentication |

---

## Reliability and scalability

### What scales, and how

| Dimension | Mechanism | Ceiling |
| --- | --- | --- |
| Corpus size | The crawl is a **generator**; per-document work interleaves with crawling | Memory is bounded regardless of corpus size |
| Documents per run | `ingest_max_docs_per_run`, resumable via the high-water mark | A capped run resumes exactly where it stopped |
| Concurrency | `ingest_workers` (one crawler, a pool of document workers) | `mysql_pool_size`; Camelot is serialised |
| Embedding throughput | Batches of 128, plus vector reuse | Azure deployment quota, defended by the throttle gate |
| Cost | Vector reuse, the enrichment cache keyed by content hash, per-page OCR routing | — |
| Ingestion instances | **1** | Process-local lock |

### What does not scale, and what to do instead

- **You cannot add ingestion servers.** To go faster, raise `ingest_workers` (up to
  just under `mysql_pool_size`) and raise `mysql_pool_size` with it.
- **Reconciliation is a full scroll per sweep.** On a very large corpus, turn
  `verify_corpus_after_sweep` off and schedule `scripts.verify_corpus` instead —
  deliberately, not by drift.
- **A permanently failing document holds its bundle's window open**, so every sweep
  scans more of that bundle. Triage `documents_retry`.

### Reliability properties, restated

1. One run at a time.
2. A document is never replaced by nothing.
3. New points before old deletes — searchable throughout, previous version intact on
   failure.
4. Every write idempotent on a deterministic key.
5. Every catalog write on the ingest path fails open.
6. Capped and interrupted runs resume from the high-water mark.
7. Failures leave a marker that widens the next window.
8. Derived stores (Neo4j) cannot fail an ingestion.
9. Deletion requires a live enumeration the code is willing to believe.

---

## Runbooks

### Run a scoped ingestion

```bash
python -m app.workers.tasks drupal --bundle news --bundle report
# or
curl -X POST localhost:8001/ingest/run -H "Authorization: Bearer $T" \
     -d '{"bundles": ["news", "report"]}'
```

### Run a full sweep now

```bash
python -m app.workers.tasks sweep
# or
curl -X POST localhost:8001/reindex -H "Authorization: Bearer $T" -d '{"sweep": true}'
```

409 means a run is already in progress.

### Rebuild one document

```bash
curl -X POST localhost:8001/reindex -H "Authorization: Bearer $T" \
     -d '{"document_id": "<uuid>"}'
```

Returns `{"status": "queued"}` — the **next sweep** does the work. Deletes nothing.
404 means the document is not catalogued.

### Rebuild the corpus after a code change

1. Bump the right component in `app/ingestion/version.py`, in the same commit as the
   change.
2. Deploy.
3. Census first:

```bash
python -m scripts.reprocess_corpus --dry-run
```

4. A cautious first pass, and check the results:

```bash
python -m scripts.reprocess_corpus --limit 200
python -m scripts.verify_corpus
```

5. Then the rest. Safe to interrupt and re-run at any point:

```bash
python -m scripts.reprocess_corpus
```

If a pass reports `stopped_because: "no progress"`, the remaining documents are
*failing* to rebuild rather than waiting their turn — check `ingest_log` and
`documents_retry`.

### Enable delete reconciliation

```bash
# 1. See exactly what it would do. Nothing is deleted, nothing is indexed.
python -m app.ingestion.pipeline --dry-run-reconcile

# 2. Read the report carefully: `documents`, `attachments`, `moved`, `by_bundle`.
# 3. If it is right, enable it on the sweep.
#    WORKER_SWEEP_RECONCILE=true, then restart.
```

If the dry run reports a refusal, **do not raise
`ingest_reconcile_max_missing_ratio` to make it pass.** Find out why the live set
came back short first.

### Recover documents that failed before retry markers existed

```bash
python -m scripts.recover_stranded --dry-run
python -m scripts.recover_stranded
```

Then let the next sweep run. See
[10, Recovery](10-failures-retries-and-recovery.md#recovery-documents-that-failed-before-markers-existed).

### Force a dead attachment to be retried

```sql
DELETE FROM documents_dead_link WHERE document_id = '<file uuid>';
```

Or, at the source: re-upload the file and **save the node** (for a real attachment),
or **edit the link** (for an in-body PDF — the corrected URL yields a different uuid,
which was never marked dead).

### Backfill abstracts

```bash
python -m app.ingestion.enrich_backfill --dry-run --limit 50
python -m app.ingestion.enrich_backfill --limit 500
```

This one spends money. Run it with a limit and watch it.

### Add a new Drupal bundle

1. Add it to `DEFAULT_BUNDLES` in **`app/core/corpus.py`** — not to the extractor.
   The read path reads the same list, so adding it there is what makes the bundle
   describable to the model and countable by the structured planner. (Block types
   and the entity allowlist do live in `drupal_extractor.py`.)
2. Add a chunking preset in `chunking/config.py`, or let it fall back to the article
   preset (which is what every news-like bundle uses).
3. Crawl it scoped first: `python -m app.workers.tasks drupal --bundle <new>`.
4. Audit what its fields did: `python -m app.ingestion.field_audit --bundle <new>`.
5. Check for a date field. If it has one and the bundle should be dated by it, declare it in
   `source_dates.FIELD_ROLES` — otherwise every document in the bundle is dated by
   its CMS creation stamp, and reconciliation's `undeclared_source_date_field` will
   tell you so.

### Change the embedding model or dimension

This re-embeds the corpus. There are two cases:

- **New deployment name, or a changed `dimensions`.** `embedding_version()` changes,
  so vector reuse correctly refuses every stored vector and a reprocess re-embeds
  everything. If the dimension changed, the collection is the wrong shape and
  `ensure_collection` raises `VectorDimensionMismatch` — create a new collection.
- **The same deployment repointed in place at a different model.** This is
  **undetectable**: the name and dimension are unchanged, so reuse happily keeps
  vectors from the old model. **You must clear the collection and re-ingest.**

### Move the collection

Point `qdrant_collection` at a new name and run a full ingestion. `ensure_collection`
creates it with the configured dimension and all thirteen payload indexes. The old
collection is untouched, so this is also how you do a zero-downtime re-embed.

### Take ingestion down for maintenance

Set `WORKER_SWEEP_INTERVAL_SECONDS=0` and restart the ingestion server. The scheduler
logs that it is disabled and the control plane stays available for manual runs. Do
this before any corpus-wide CLI operation, so a CLI in a separate process cannot race
the sweep.

### Run the tests

```bash
pytest                                   # everything (scoped to tests/ by pytest.ini)
pytest tests/ingestion tests/catalog     # the ingestion suite
pytest tests/test_architecture.py        # the layering rules
pytest -m "not llm"                      # skip tests that need model credentials
```

`pytest.ini` pins `testpaths = tests` and excludes `redundant/`, which holds an
archived copy of the whole suite — collecting both makes every module a basename
collision and aborts the run.

The ingestion suite is organised by stage:

| Path | Covers |
| --- | --- |
| `tests/ingestion/change_detection/` | retry floors, reconcile safety, the reconcile dry run, bundle moves, the unpublish policy, dead-link crawling, attachment orphans, reindex recoverability, stranded recovery, corpus reprocessing |
| `tests/ingestion/chunking/` | identity, parents, orphans, overlap and its boundaries, page boundaries, the max-token cap, breadcrumbs, section classification, heading detection, losslessness, payload |
| `tests/ingestion/dates/` | source dates, PDF resolution and its cases, the full resolution pipeline, provenance, coverage, the reconciliation date checks |
| `tests/ingestion/extractors/` | Drupal pagination, in-body PDF URLs and anchor text, attachment download, the hybrid router, text normalisation |
| `tests/ingestion/` (top level) | batch ingest, corpus reconciliation, the empty-extraction guard, enrichment, entity refs, indexer reuse, catalog wiring, pipeline versioning, the searchable-source allowlist, the field audit |
| `tests/catalog/` | schema migration, facet uniqueness, author names, dead links, the enrichment cache, theme rows and queries, the analytical readers, paging |
| `tests/test_architecture.py` | the `app/` layering rules: no runtime import points up the hierarchy, deferred upward imports match an allowlist, no runtime package cycles, every package documents itself |

The `llm` marker means "hits the configured LLM deployment; needs credentials and
network".

---

## Troubleshooting matrix

Start from the symptom.

| Symptom | Most likely cause | How to confirm | Fix |
| --- | --- | --- | --- |
| Nothing is being ingested | Scheduler disabled | No "Starting background sweep" line at startup | Set `worker_sweep_interval_seconds > 0` |
| | MySQL unreachable | `GET /ready` → 503 | Restore MySQL |
| | Every sweep skipped as busy | INFO "Skipping sweep; another ingestion run is in progress" | A wedged run; restart the server |
| A new page is not in answers | Not yet crawled | `SELECT * FROM documents WHERE document_id=...` returns nothing | Wait a sweep, or run scoped |
| | Its bundle is not crawled | Check `DEFAULT_BUNDLES` in `app/core/corpus.py` | Add it |
| | It is a taxonomy term | WARNING "Not crawling taxonomy_term/…" | By design; the term's name already travels on content chunks |
| | It is a boilerplate block | Body under `drupal_block_min_chars` and no PDF | By design |
| An edited page still answers with old text | `unchanged_content` — only the title changed | `ingest_log.status` for that document | Correct; the payload title was refreshed |
| | It failed | `documents_retry` has a row | Read the `error` column |
| A document answers nothing at all | Points missing | Reconciliation `indexed_without_points` | `state.clear_change_markers` + next sweep |
| | Extraction produced nothing | `ingest_log` ERROR "extracted to nothing" | Investigate the PDF; previous version was kept |
| Attachment never appears | 4xx at source | `documents_dead_link` has a row | Re-upload and save the node, or clear the marker |
| | The link is malformed | `ingest_log` `skipped` with a download error | Fix the link; the corrected URL is a new document |
| | Not a PDF | WARNING "Skipping non-PDF document attachment" | Convert it, or add an extractor |
| Scanned PDF answers nothing | Azure DI unconfigured | WARNING "Azure Document Intelligence is not configured" | Configure it |
| | DI call failing | `logger.exception` from `_ocr_pdf` | Check endpoint, key, quota |
| Tables missing from answers | Camelot or Ghostscript missing | WARNING "Camelot is not installed" / a Camelot exception | Install them |
| | PDF forbids extraction | WARNING about permission flags | Prose survives; tables do not |
| PDF text is garbage | PUA / `(cid:N)` text layer | Read the extracted text with `python -m app.ingestion.extractors.pdf_extractor <path> --full` | Re-source the PDF, or run it through `azure_only` |
| A document has the wrong date | Its bundle maps to the wrong field, or to none | Reconciliation `unmapped_bundle_dates`; `documents_date_decision.evidence` | Fix `BUNDLE_DATE_FIELDS`, then `scripts.backfill_bundle_dates` |
| | A PDF override | `documents_date_decision` shows `propose_override` | Read `evidence`; if wrong, it is a gate gap worth reporting |
| | A backfill overwrote it | Reconciliation `stated_date_not_applied` | Re-run `scripts.backfill_bundle_dates` |
| A PDF is dated differently from its page | Something bypassed inheritance | Reconciliation `attachment_date_adrift` | Re-run `scripts.backfill_bundle_dates` |
| A project or event has no end date | Its CMS end field is empty, or the two dates contradict each other | `documents_date_decision.range_issue` | Fix the CMS values; a re-crawl heals the row |
| A document is missing from date-filtered answers | It has no date | `SELECT effective_start_date FROM documents WHERE ...` is NULL; run tally `indexed_without_date` | Check the source exposes a date field |
| A year-only document reads as 1 January | A consumer ignoring `start_precision` | `start_precision='year'` on the row and the payload | Fix the consumer; the marker is correct |
| Documents disappeared en masse | Delete reconciliation ran on a truncated enumeration | ERROR/WARNING history; `ingest_log` `deleted` rows in one run | The guard should have refused. Re-crawl to restore; **lower** the ratio |
| | They were unpublished at source | The site no longer serves them | Republish; they return as `NEW` on the next run |
| Deletes are not happening | `worker_sweep_reconcile` is off | Config | Turn it on **after** a dry run |
| | The guard is refusing | WARNING "Refusing to reconcile deletes" | Investigate the source first |
| Ingestion is very slow | Lots of OCR | `/metrics/timings`, `extraction` share | Expected; consider `local_only` for a bounded backfill |
| | Little vector reuse | `Indexed … 0 reused` | Expected right after a version bump or a mass retitle |
| | Throttling | `events.embedding_http.throttled` | Raise quota, or add `ingest_batch_pause_seconds` |
| | Serialised on Camelot | Many table pages, `ingest_workers > 1` | Unavoidable; the lock prevents a process crash |
| Run stops early every time | Batch budget | `budget_stop=1` in the tally | Raise `ingest_max_docs_per_run`, or accept incremental catch-up |
| `documents_retry` keeps growing | A source or extractor problem affecting many documents | Group the queue by `outcome`, `bundle` and `error` | Fix the cause; delete rows only for genuinely dead documents |
| Every sweep scans a whole bundle | One permanently failing document holds the floor | `SELECT MIN(changed_mark) FROM documents_retry WHERE bundle=...` | Fix or deliberately delete that retry row |
| Keyword/lexical retrieval finds nothing | `chunk_text` index missing | Qdrant payload schema | `python -m scripts.create_fulltext_index` |
| `VectorDimensionMismatch` on startup | Collection shape ≠ configured dimension | The exception message says both numbers | Repoint the setting, or use a new collection |
| Answers mix two embedding models | Deployment repointed in place | **Not detectable** | Clear the collection and re-ingest |
| Duplicate facet counts | Legacy rows from before the unique key | `SELECT document_id, tag, COUNT(*) … HAVING COUNT(*)>1` | `ensure_state_table` migrates; a reindex heals the rows |
| Themes include "False" | Legacy rows | `SELECT * FROM documents_theme WHERE theme IN ('False','True','none')` | `_NOT_A_THEME` prevents new ones; reindex to clear |
| A document is credited with a parent theme it was not tagged with | Legacy rows from before the guard | Compare against `theme_structure.json` | Reindex; only own themes get rows now |
| Ingest aborts with `TaxonomyUnavailable` | The theme map cannot be read | The message names the resolved path | Restore the file, or point `THEME_TAXONOMY_PATH` at it. Do **not** bypass — the run would rewrite every theme's hierarchy to NULL |
| A theme renamed in the CMS still counts under its old name | Drupal term renames don't bump referencing nodes' `changed`, so ingestion never sees it | `SELECT DISTINCT theme FROM documents_theme` | `scripts.rename_theme --apply`, then update the theme map, then `scripts.reclassify_theme_rows` |
| Knowledge tables empty | Layer disabled, or `build_knowledge` never ran | `GET /metrics` → `knowledge` | Enable the flags; run `scripts.build_knowledge` |
| Graph behind the corpus | Projection not running | Reconciliation `graph_projection`, or `freshness()` | Check `graph_project_after_sweep`; `scripts.project_graph --rebuild` |
| Anyone can start a crawl | No admin group | WARNING "No ingest_admin_group (or ops_admin_group) is configured" | Set one |
| `ingest_log` is enormous | Retention off, or `ingest_log_unchanged` on | Table size | Set `ingest_log_retention_days`; turn the unchanged flag off |
| Temp dir filling with PDFs (Windows) | Camelot handles | Temp dir listing | `_remove_temp_pdf` retries after `gc.collect()`; if it persists, watch for an exception around it |

---

## End-to-end completion criteria

"The ingestion completed successfully" is a specific, checkable claim. All of the
following must hold.

### Per document

A document is fully ingested when:

1. Its points are in Qdrant — children with real vectors, parents with zero vectors,
   all carrying the current `pipeline_version`.
2. The previous version's points have been removed by the scoped delete.
3. Its `documents` row is committed with the correct `fingerprint`, `content_hash`,
   `doc_version`, `pipeline_version`, `changed_mark`, `effective_start_date` +
   `date_source` + `start_precision`, `title`, `url`, and a non-NULL
   `indexed_at`.
4. Its facet rows (`author`, `tag`, `theme`) and `attachment` link rows match the
   document, all written in the same transaction as the row.
5. `ingest_log` holds an `indexed` row for it under this run's `run_id`, with the
   chunk count.
6. It has **no** row in `documents_retry`.
7. If it is a `pdf_attachment`, `documents_date_decision` explains its date.
8. If the knowledge layer is on, `documents_knowledge_run` holds a row with
   `status="ok"` for its current `doc_version`.

### Per run

A run completed cleanly when:

1. The tally shows `error = 0` and `skipped = 0`.
2. `budget_stop` is absent, or its presence is expected because you set a cap.
3. The `ingest_throughput` line shows a plausible `documents_per_minute` and
   `enrichment_failures = 0`.
4. `indexed_without_date` is 0, or every non-zero case is a source that genuinely
   states no date.
5. No WARNING about a refused delete reconciliation.
6. `documents_retry` is no deeper than it was before the run.

### Per sweep

A sweep completed cleanly when, in addition:

1. `corpus_reconcile ok=true` — every check is 0 or legitimately skipped.
2. `knowledge_catch_up` reports `failed = 0` (if the layer is on).
3. `graph_projection` reports a version and node counts (if the graph is on).
4. `GET /ready` is 200.
5. The next sweep's high-water marks have advanced, or the corpus genuinely has not
   changed.

### The one-command check

```bash
python -m scripts.verify_corpus
```

Exit **0** means the stores agree. That is the closest thing to a single answer, and
it is the right thing to gate a deployment step on.

---

## Where to look next

- **The code.** Every module in `app/ingestion` carries a docstring explaining *why*
  it is shaped the way it is. Those docstrings are the primary source for this set,
  and they are kept current with the code in a way prose documentation cannot be.
- **The tests.** `tests/ingestion` and `tests/catalog` encode the invariants. Several
  test modules are cited in the code as the record of measured corpus evidence —
  `tests/ingestion/chunking/test_chunk_orphans.py`,
  `test_chunk_page_boundaries.py`, `test_chunk_payload.py`.
- **`app/ingestion/version.py`.** Read it before changing anything in the chunker,
  the payload or the embedded string.

---

Previous: [11 — Observability, Monitoring and Alerting](11-observability-and-monitoring.md) · Back to the [index](README.md)
