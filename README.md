# Agentic RAG Chatbot

A FastAPI RAG service over a mixed corpus of **PDFs** and **website/Drupal articles**.
Hybrid retrieval on **Qdrant**, cross-encoder reranking, grounded generation with
**citations built from chunk payloads** (never hallucinated), intent routing to a
structured path answered from the local MySQL catalog, optional **Bearer-JWT auth**
on the public API, Redis response/embedding caches plus a Qdrant-backed semantic
cache, a background ingestion server, and observability. Models are served via
**Azure OpenAI**; orchestration uses LangChain.

Design rationale lives in [`docs/`](docs/). The ingestion (write) path is
documented end to end in [`docs/ingestion/`](docs/ingestion/README.md); the
codebase structure and layering rules are in [`app/README.md`](app/README.md).

## Architecture

Two servers over shared stores. The **write path** keeps a searchable copy of the
site in step with the site; the **read path** answers questions from it.

```
WRITE PATH                                READ PATH
uvicorn app.ingest_main:app               uvicorn app.main:app
(exactly one instance)                    (scale horizontally)
      |                                         |
  workers/     when ingestion runs          api/        HTTP + auth
  ingestion/   crawl -> extract -> date     pipeline/   query orchestration
               -> chunk -> embed -> index   retrieval/  understanding -> search
  knowledge/   claims -> Neo4j                          -> context
      |                                     generation/ answer + verification
      v                                         v
      +------- catalog/ (MySQL) ----------------+
      +------- Qdrant · Azure · Neo4j · Redis --+
                 (via core/clients/)
```

| Package | Responsibility |
| --- | --- |
| `main.py` / `ingest_main.py` / `app_factory.py` | Entry points: retrieval server, ingestion server, shared FastAPI setup. |
| `config.py` | Every setting, one class. A pure leaf — read by everything, imports nothing. |
| `api/` | HTTP surface: `/chat` (SSE), `/search`, `/ingest/*`, `/reindex`, `/health`, `/ready`, `/metrics`. |
| `schemas/` | Request/response models for those routes (the *external* boundary). |
| `pipeline/` | Read-path orchestration. `query_pipeline.py` is a query end to end. |
| `retrieval/` | The read path: `understanding/` -> `search/` -> `context/`, plus `structured/` (catalog answers) and `graph/` (knowledge-graph answers). |
| `generation/` | Context blocks -> grounded, cited prose, with an optional entailment check. |
| `ingestion/` | The write path: change detection, extraction, dating, chunking, indexing, and the operational tooling around them. |
| `knowledge/` | Entity resolution, claims, and the Neo4j projection (a projection of MySQL, never a system of record). |
| `catalog/` | MySQL: the document catalog, crawl cursor, audit log and operational markers. |
| `cache/` | Qdrant-backed semantic answer cache. |
| `core/` | Shared contracts: `clients/` (the only place external services are constructed), `models/` (cross-package types), and vocabulary both paths must agree on. |
| `observability/` | Spans and in-process timing metrics. Imported by every layer, imports none. |

Dependencies point **downward only**, and that is enforced —
`tests/test_architecture.py` fails the build on a runtime import that goes up the
hierarchy, on a new package with no declared layer, and on a package that does
not document itself.

**Full structure guide, including where to add new code and where to look for a
bug: [`app/README.md`](app/README.md).**
The write path is documented end to end in
[`docs/ingestion/`](docs/ingestion/README.md).
## Prerequisites

- Python 3.11+
- Docker (for Qdrant)
- **Azure OpenAI** — a chat deployment and an embedding deployment
- *Optional:* Azure Document Intelligence (OCR for scanned PDFs), MySQL/MariaDB
  (the document catalog — required by the ingestion server), Redis (response and
  embedding caches), Neo4j (the knowledge graph). All degrade gracefully when
  unconfigured, except MySQL on the ingestion server.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on POSIX)
pip install -r requirements.txt
copy .env.example .env          # cp on POSIX
```

Fill in `.env` with your Azure OpenAI credentials and deployment names (see
**Configuration**). Then start Qdrant:

```bash
docker compose up -d
```

## Run

Two independent servers share the codebase and `.env`. Start Qdrant (above), then
run each in its own terminal:

```bash
# Retrieval server — query API (chat / search), read-only
uvicorn app.main:app --reload --port 8000

# Ingestion server — background change-detection sweep + ingest/reindex endpoints
uvicorn app.ingest_main:app --reload --port 8001
```

The ingestion server runs an incremental sweep (PDFs + Drupal → Qdrant) every
`WORKER_SWEEP_INTERVAL_SECONDS` (default 3600; set `0` to disable and ingest only
on demand). The first sweep runs at startup.

- Retrieval Swagger UI: http://127.0.0.1:8000/docs
- Ingestion Swagger UI: http://127.0.0.1:8001/docs
- Qdrant dashboard: http://localhost:6333/dashboard

## Endpoints

| Server | Method | Path | Purpose |
|---|---|---|---|
| Retrieval | `POST` | `/chat` | Ask a question. **Streams** the grounded answer as SSE: `token` events, then a `sources` event with citations, then `done` (or a terminal `error`). |
| Retrieval | `POST` | `/search` | Retrieval only — returns the ranked context blocks, no generation. |
| Ingestion | `POST` | `/ingest/run` | Incremental Drupal ingest (nodes, terms, blocks + their PDFs). |
| Ingestion | `POST` | `/ingest/article` | Index an inline article (`title`/`body`/`url`), or crawl live Drupal `bundles`. |
| Ingestion | `GET` | `/ingest/log` | Recent ingestion audit events. |
| Ingestion | `POST` | `/reindex` | Reset one `document_id` for re-ingest, or run a full incremental `sweep`. |
| Both | `GET` | `/health` | Liveness probe. |
| Both | `GET` | `/ready` | Readiness — 200/503 on Qdrant reachability (body detail only when `OPS_DETAIL_ENABLED`). |
| Both | `GET` | `/metrics` | Config + store snapshot; 404 unless `OPS_DETAIL_ENABLED`. |

When `AUTH_ENABLED` is set, the retrieval endpoints require an
`Authorization: Bearer <JWT>` header. The ingestion control plane is protected
separately and **on by default** (`INGEST_AUTH_ENABLED`); its mutating routes
additionally require membership of `INGEST_ADMIN_GROUP` (falling back to
`OPS_ADMIN_GROUP`). See
[docs/ingestion/03-triggers-and-control-plane.md](docs/ingestion/03-triggers-and-control-plane.md#authentication-and-authorization).

## Usage

Ingest an inline article:

```bash
curl -X POST http://127.0.0.1:8001/ingest/article \
  -H "Content-Type: application/json" \
  -d '{"title":"Solar Mini-Grid Pilot 2025","body":"The pilot connected 1,240 households...","url":"https://example.org/a"}'
```

Ask a question (SSE stream):

```bash
curl -N -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"How many households did the 2025 pilot connect?"}'
```

```
data: {"type": "token", "text": "The pilot connected 1,240 households [1]."}
data: {"type": "sources", "citations": [{"n":1,"type":"article","title":"Solar Mini-Grid Pilot 2025","url":"https://example.org/a"}], "intent": "qa", "used_chunks": 1}
data: {"type": "done"}
```

`/search` returns the same retrieval result without generation — handy for tuning K/N and inspecting scores.

## Configuration

Settings load from the environment / `.env` (see `app/config.py` for the full list
and defaults; `.env.example` for a starting template). The most relevant:

| Variable | Default | Description |
|---|---|---|
| `AZURE_OPENAI_MODEL` / `_API_KEY` / `_ENDPOINT` | — | Standard chat deployment (e.g. `gpt-5-mini`). |
| `AZURE_OPENAI_EMBEDDING_MODEL` / `_KEY` / `_ENDPOINT` | — | Embedding deployment (e.g. `text-embedding-3-large`). |
| `LLM_STRUCTURED_TEMPERATURE` | *(unset → omitted)* | Temperature for deterministic/structured calls (query understanding, routing, rerank, faithfulness). **Leave unset for reasoning models** (gpt-5 / o-series reject any value but the default); set `0` for classic chat models. |
| `LLM_TEMPERATURE_SUPPORTED` | `true` | Whether the chat deployment accepts a temperature at all. **Set `false` for a model that rejects it** (gpt-6-luna answers every call carrying one with a 400): the answer call and the voted query understanding send a temperature whatever `LLM_STRUCTURED_TEMPERATURE` says, so this is the switch that omits it everywhere. |
| `AZURE_DOCUMENT_INTELLIGENCE_*` | — | Optional OCR/layout for scanned PDFs. |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server URL. |
| `QDRANT_COLLECTION` | `documents` | Collection name. |
| `REDIS_URL` | *(empty → disabled)* | Enables the response / embedding caches (the semantic cache lives in Qdrant). |
| `MYSQL_*` | `localhost:3306` | Ingest-state manifest / document catalog (also answers the structured-query path). |
| `AUTH_ENABLED` / `JWT_*` | `false` | Require a Bearer JWT on the public retrieval API. |
| `RETRIEVAL_CANDIDATE_K` | `40` | Wide candidate pool from hybrid search before reranking (K). |
| `RETRIEVAL_TOP_K` | `6` | Reranked context blocks handed to the LLM (N). |
| `RERANKER_PROVIDER` | `embedding` | `embedding` \| `llm` \| `cross_encoder` \| `cohere` \| `none`. |
| `RERANK_SCORE_THRESHOLD` | `0.0` | Drop weak blocks (hallucination guard); `0` disables. |
| `HYBRID_USE_SPARSE` | `false` | Collection is dense-only today; enable once sparse vectors are indexed. |
| `FAITHFULNESS_CHECK` | `false` | Optional post-generation entailment verification. |
| `is_retrieval_log` | `false` | Per-query JSON trace of the whole retrieval pipeline (Qdrant / graph / MySQL requests, results, latencies, failures, final context) under `logs/`. See [docs/retrieval-logging.md](docs/retrieval-logging.md). |
| `CELERY_BROKER_URL` | *(empty → inline)* | Background ingestion; falls back to inline execution when unset. |

## Tests

```bash
python -m pytest -q        # unit suite (chunking, routing, filters, counting) — offline
```

`tests/` mirrors `app/`, so a module's tests sit in the matching directory.
`tests/test_architecture.py` asserts the layering rules described in
[`app/README.md`](app/README.md).

A manual, live-data harness lives in `tools/local_tests/`. It writes only to
isolated `local_test_*` MySQL tables and a `local_test_documents` collection,
never the real catalog:

```bash
python -m tools.local_tests.run_ingestion_test --bundle article --max-docs 3
python -m tools.local_tests.run_ingestion_test --cleanup
```
</content>
</invoke>
