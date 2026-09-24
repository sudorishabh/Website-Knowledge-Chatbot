# `app/` — codebase structure

This is the map. It answers: what is each package for, what may import what, and
where does new code go.

The layering here is **enforced**, not aspirational — `tests/test_architecture.py`
asserts it and fails the build if a runtime import points the wrong way.

---

## The two paths

Almost everything in this codebase belongs to one of two pipelines. Knowing which
one you are in answers most "where does this go?" questions immediately.

```
WRITE PATH (ingestion)                    READ PATH (query)
the live site -> the stores               a question -> a cited answer

ingest_main.py  (server)                  main.py  (server)
      |                                         |
workers/        when to run                api/          HTTP surface
      |                                         |
ingestion/      crawl, extract, date,     pipeline/     orchestration
                chunk, embed, index             |
      |                                   retrieval/    understanding -> search
knowledge/      claims, graph projection                 -> context
      |                                         |
      |                                   generation/   answer + verification
      v                                         v
      +---------  catalog/ (MySQL)  ------------+
      +---------  core/clients (Qdrant, Azure, Neo4j, Redis) ---+
```

They meet in exactly two places: the **stores** (`catalog/`, and Qdrant via
`core/clients/`), and the **shared vocabulary** in `core/`. Neither path imports
the other — `retrieval/`, `pipeline/` and `generation/` contain no import of
`app.ingestion`, and that is asserted.

---

## Dependency hierarchy

A package may import its own layer or any layer **below** it. Never above.

| Layer | Packages | Responsibility |
| --- | --- | --- |
| 9 | `main.py` · `ingest_main.py` · `app_factory.py` | **Entry points.** Compose the app. Nothing imports these. |
| 8 | `api/` | **HTTP surface.** Routing, auth, request/response only. No business logic. |
| 7 | `pipeline/` · `workers/` | **Orchestration.** `pipeline` runs a query end to end; `workers` decides when ingestion runs. |
| 6 | `generation/` | **Answer synthesis.** Context blocks in, cited prose out. |
| 5 | `ingestion/` · `retrieval/` | **The two domains.** The write path and the read path. |
| 4 | `knowledge/` | **Derived knowledge.** Entities, claims, graph projection. |
| 3 | `catalog/` · `cache/` | **Persistence.** MySQL document catalog; semantic answer cache. |
| 2 | `core/` · `schemas/` | **Shared contracts.** Infrastructure clients, cross-package models, corpus vocabulary; HTTP wire models. |
| 1 | `observability/` | **Instrumentation.** Imported by every layer, imports none of them. |
| 0 | `config.py` | **Settings.** A pure leaf: imports nothing from the app, read by everything. |

### Reading the graph

- **`config.py` is why the package graph looks cyclic and is not.** Every layer
  reads it; it reads nothing. Treat it as a leaf, and the graph is a clean DAG
  with zero cycles.
- **Deferred imports are a real distinction.** A handful of upward imports exist
  inside function bodies or under `TYPE_CHECKING`. Those create no runtime
  coupling and no import-order constraint, and each one is listed with its reason
  in `ALLOWED_DEFERRED_UPWARD` in `tests/test_architecture.py`. A *new* one fails
  the test until someone records why.
- **`retrieval/graph/` must not load on the default path.** Every reference to it
  from production retrieval is inside a function, behind a flag that is off. Two
  tests in `tests/retrieval/graph/test_graph_retrieval.py` assert this
  structurally.

---

## The packages

### Entry points and configuration

| Path | What it is |
| --- | --- |
| `main.py` | Retrieval server: `/chat`, `/search`, `/health`, `/ready`, `/metrics`. |
| `ingest_main.py` | Ingestion server: the sweep scheduler plus `/ingest/*` and `/reindex`. Declares MySQL required for readiness. |
| `app_factory.py` | Shared FastAPI construction (logging, CORS, observability init). |
| `config.py` | Every setting, one `Settings` class, `lru_cache`d. Changing a value needs a restart. |

Run them separately:

```bash
uvicorn app.main:app        --port 8000   # read path, scale horizontally
uvicorn app.ingest_main:app --port 8001   # write path, exactly one instance
```

**One ingestion server only** — the one-run-at-a-time lock is a
`threading.Lock`, process-local by design. There is no distributed lock.

### `api/` — HTTP surface

`auth.py` (bearer JWT, principal, the ingest-admin group), `chat.py` (SSE),
`search.py`, `ingest.py` (control plane), `health.py` (`/health`, `/ready`,
`/metrics`, `/metrics/timings`).

Routers validate, delegate, and shape a response. Business logic lives one layer
down.

### `pipeline/` — read-path orchestration

`query_pipeline.py` is the read path end to end: cache lookup → understanding →
routing → search → rerank → context → generation → citations → metrics.
**Start here to understand how a query is answered.**
`summarize.py` is scoped summarisation ("summarise everything about X").

### `retrieval/` — the read path

Four stages plus two alternative answer routes. See
[`retrieval/README.md`](retrieval/README.md).

### `generation/` — answer synthesis

`prompts.py` (grounding prompt, context formatting), `answerer.py`,
`sections.py`, `answer_plan.py`, `faithfulness.py` (post-generation entailment),
`redundancy.py`, `date_claims.py`.

Reads `ContextBlock` from `core/models/context.py` — **never** from a retrieval
module.

### `ingestion/` — the write path

Fully documented in [`docs/ingestion/`](../docs/ingestion/README.md). Structure:

| Path | Stage |
| --- | --- |
| `change_detection/` | What changed since last run; the crawl window; delete reconciliation. |
| `extractors/` | Drupal JSON:API, PDF download, per-page extraction routing, text normalisation. |
| `date_*.py`, `source_dates.py` | What date a document carries, and the evidence for it. |
| `canonical.py` | The one document shape everything converges on. |
| `chunking/` | Parent/child windows, chunk identity, payload. |
| `indexer.py`, `version.py` | Vector reuse, embedding, batched upsert; the pipeline-version stamp. |
| `pipeline.py` | The run coordinator and the per-document handler. **The heart of the write path.** |
| `reconcile.py`, `reprocess.py`, `recovery.py`, `backfill.py`, `enrich*.py`, `field_audit.py` | Operational tooling, each with a CLI. |
| `knowledge_sync.py`, `graph_sync.py` | The post-index hooks into `knowledge/`. Cannot fail an ingestion. |

### `knowledge/` — derived knowledge

Entity resolution (`seed`, `resolver`, `gazetteer`, `candidates`, `normalize`,
`pi_promotion`), claims (`claims/`), graph projection (`graph/`), and
`document_pipeline.py` — the per-document stage ingestion calls.

Neo4j is a **projection of MySQL**, never a system of record, so an outage
degrades this layer and loses nothing.

### `catalog/` — MySQL persistence

The system of record for ingestion: the crawl cursor, change detection,
provenance, exact counts, the audit log.

| Module group | Purpose |
| --- | --- |
| `schema.py`, `db.py`, `models.py` | DDL + idempotent migrations; identifier safety; row types. |
| `state.py`, `queries.py` | The document row and its facets (write); analytical reads. |
| `log.py`, `retries.py`, `dead_links.py` | The audit trail and operational markers. |
| `enrichment.py`, `date_decisions.py`, `theme_taxonomy.py`, `author_names.py` | Caches, date provenance, classification. |
| `entities.py`, `mentions.py`, `assertions.py`, `predicate_candidates.py`, `knowledge_runs.py` | Knowledge-layer stores. |

Table reference: [`docs/ingestion/08-persistence-and-catalog.md`](../docs/ingestion/08-persistence-and-catalog.md).

### `core/` — shared contracts

| Path | What |
| --- | --- |
| `clients/` | The only place external services are constructed: `vector_store` (Qdrant), `database` (MySQL pool), `embeddings` (Azure + throttle gate), `llm`, `graph` (Neo4j), `cache` (Redis). All `lru_cache`d. |
| `models/` | Cross-package data contracts: `CanonicalDocument`, `ContextBlock`, plus payload interpretation helpers. |
| `corpus.py` | The bundle vocabulary both paths must agree on. |
| `dates.py`, `editions.py` | Date parsing and edition-label spelling, shared so ingestion and retrieval cannot drift. |

**`core/` is where a shared piece goes.** If two packages need the same rule and
one currently reaches into the other for it, that rule belongs here —
`core/corpus.py` and `core/editions.py` both exist for exactly that reason, and
each says so in its docstring.

### `cache/`, `observability/`, `schemas/`, `workers/`

Small and single-purpose; see each package's `__init__.py`.

`observability/` holds two things worth naming: `tracing.py`/`metrics.py` (the
`span()` context manager and the in-process stage registry behind
`GET /metrics/timings`) and `retrieval_log/`, the per-query retrieval trace
(`is_retrieval_log=true` → one JSON file per query under `logs/`; see
[docs/retrieval-logging.md](../docs/retrieval-logging.md)). Both live at layer 1
for the same reason: they are called from the client gateways, retrieval, the
catalog and the pipeline alike, so they may import none of them — the trace
renders a `Candidate` or a `ContextBlock` by duck-typing instead.

---

## Where new code goes

| You are adding… | Put it in | Also do |
| --- | --- | --- |
| A new HTTP endpoint | `api/<router>.py` + a model in `schemas/` | Keep logic out of the router |
| A new query-understanding signal | `retrieval/understanding/` | Extend `QueryAnalysis` in `query_processor.py` |
| A new way to fetch candidates | `retrieval/search/` | It takes and returns `Candidate` |
| A new retriever / store on the read path | wherever it belongs | Wrap its call in `retrieval_log.retriever_call("<name>", …)` so the trace covers it |
| A new ranking signal | `retrieval/search/reranker.py` | Band it, so only a material difference reorders |
| A change to what the LLM sees | `retrieval/context/builder.py` (selection) or `generation/prompts.py` (formatting) | |
| A new source of documents | `ingestion/extractors/` | Emit a `CanonicalDocument` |
| A change to chunking or payload | `ingestion/chunking/` | **Bump `ingestion/version.py`** and run `scripts/reprocess_corpus.py` |
| A new table or column | `catalog/schema.py` (DDL + migration) + a module for its reads/writes | Migrations must be idempotent |
| A new external service | `core/clients/` | Nothing else may construct a client |
| A rule two packages share | `core/` | See `core/corpus.py` for the pattern |
| A background job | `workers/tasks.py` | Called by the scheduler, the API and the CLI alike |
| A one-off operation | `scripts/` (repo root) | Give it `--dry-run` and a clear exit code |
| A dev-only harness | `tools/` (repo root) | Nothing in `app/` may import it |

### Where a bug lives

| Symptom | Look at |
| --- | --- |
| Wrong or missing answer content | `retrieval/search/` (was it fetched?) then `retrieval/context/builder.py` (was it admitted?) |
| Right documents, bad prose | `generation/prompts.py`, `generation/answerer.py` |
| Wrong citation or page number | `retrieval/context/citations.py`, `core/models/context.py::page_span` |
| A document missing from the corpus | `ingestion/change_detection/`, then `catalog` tables `documents_retry` / `documents_dead_link` |
| Wrong publication date | `ingestion/source_dates.py` (CMS) or `ingestion/date_rules.py` + `date_llm.py` (PDF); evidence in `documents_date_decision` |
| Wrong count or list | `retrieval/structured/` and `catalog/queries.py` |
| Stores disagree | `ingestion/reconcile.py`, or run `python -m scripts.verify_corpus` |

---

## Conventions

- **Lazy imports are deliberate.** ~338 imports sit inside function bodies, for
  two reasons: breaking would-be cycles, and keeping cold-start and the default
  path light (Neo4j, Camelot and Azure SDKs must not load when unused). Do not
  "tidy" them to the top of the file.
- **Fail open on external dependencies, fail closed on content changes.** A
  catalog write that fails costs a log line; an extraction that produces nothing
  must not replace a good document.
- **`docs/ingestion/` is the write path's reference.** Twelve documents, kept in
  step with the code.
- **Tests mirror this tree.** `tests/<package>/...` matches `app/<package>/...`.
  A new module's tests go in the mirrored directory.
