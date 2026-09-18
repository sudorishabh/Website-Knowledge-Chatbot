# 12 — Operations, Configuration and Troubleshooting

**Purpose.** Everything you need to run the read path: how to deploy it, every
setting that shapes a query, a symptom-driven troubleshooting table, and the
definition of "this query was answered correctly."

This is the read path's half of the operations picture. The write path's is
[docs/ingestion/12](../ingestion/12-operations-and-troubleshooting.md) — the two
servers share one `app/config.py` and one pair of backing services, so several
settings and runbooks below are identical to that document's and are linked
rather than repeated.

---

## Deployment

### Processes

```bash
# Retrieval server: /chat, /search, /health, /ready, /metrics. Scale horizontally.
uvicorn app.main:app --port 8000

# Ingestion server: separate process, separate concerns. See docs/ingestion/12.
uvicorn app.ingest_main:app --port 8001
```

**The read server has none of the ingestion server's single-instance
constraint.** It holds no process-local lock and no crawl cursor; every request
is independent, so it scales the ordinary way — more `uvicorn` workers, more
replicas behind a load balancer. The one thing to size deliberately per
instance is `chat_stream_max_concurrency` (default `64`): the `/chat` pipeline
is blocking (LLM, Qdrant and Redis clients all make synchronous calls), so each
active SSE stream occupies a worker thread for most of its life. It has its own
capacity limiter, separate from the shared request threadpool (~40 threads) that
auth dependencies and health probes use, specifically so a burst of long chat
generations cannot starve those. Requests past the limit queue rather than fail.

### Backing services

```bash
docker compose up -d        # qdrant + neo4j — see docs/ingestion/12, Backing services
```

The read path needs exactly the services ingestion needs, plus one it can use
optionally that ingestion barely touches:

- **Qdrant** — required. Dense retrieval, the semantic answer cache
  (`semantic_cache_collection`), and (when `graph_retrieval_enabled`) the
  hop back from graph node ids to chunk text all read it.
- **MySQL** — needed for the structured/catalog answer route
  (`retrieval/structured/`) and for author/theme corroboration in the context
  builder. Not required for readiness on this server (see
  [11, `/ready` on the read server](11-observability-and-logging.md#ready-on-the-read-server))
  — its absence degrades a feature rather than failing the probe.
- **Neo4j** — only read when `knowledge_enabled` and `graph_retrieval_enabled`
  are both on. See [08, Knowledge Graph Retrieval](08-knowledge-graph-retrieval.md).
- **Redis** — optional, backs the semantic answer cache's TTL bookkeeping. See
  [10, Caching](10-caching.md).

### Dependencies worth knowing about

Beyond what ingestion already lists (`requirements.txt`):

| Package | Needed for | If missing |
| --- | --- | --- |
| `qdrant-client` | every retrieval leg | The server cannot search at all |
| `langchain-openai` + `openai` + `httpx` | query understanding, generation, faithfulness | Cannot answer any query |
| `PyJWT` | `/chat` and `/search` auth | Auth cannot be enabled |
| `neo4j` | graph retrieval | Only matters when `graph_retrieval_enabled` |
| `sentence-transformers` | the `cross_encoder` reranker provider | Falls back to the `embedding` provider if absent — see `reranker_provider` below |
| `cohere` (commented out by default) | the `cohere` reranker provider | Falls back to the `embedding` provider if unset — see `reranker_provider` below |
| `opentelemetry-*` | OTLP export | Commented out; tracing falls back to in-process only |

### First-run checklist

1. Everything in [docs/ingestion/12's first-run checklist](../ingestion/12-operations-and-troubleshooting.md#first-run-checklist)
   must already be done — there is nothing to retrieve until a corpus exists.
2. Set `azure_openai_model` (generation) distinctly from the embedding
   deployment; the two are separate settings and a swapped value is a common
   first-run mistake.
3. Decide `reranker_provider` (`embedding` is the default and needs nothing
   extra; `cross_encoder` and `cohere` need their own model/credentials).
   `cross_encoder` downloads its model on first use, so the first query after a
   deploy pays the load; the default `BAAI/bge-reranker-v2-m3` is ~2.3GB
   resident and will fail to load on a host without the headroom (falling back
   to `embedding` with a warning, so the symptom is silently unchanged ranking
   rather than an error). `cross-encoder/ms-marco-MiniLM-L-6-v2` is ~90MB and
   English-only.
4. Decide whether `auth_enabled` should be `true` for `/chat` and `/search` —
   off by default, so an unconfigured deployment is publicly answerable.
5. Leave every feature flag (`multi_query_enabled`, `corrective_loop_enabled`,
   `keyword_leg_enabled`, `database_multi_call_enabled`,
   `entity_resolution_enabled`, `graph_retrieval_enabled`) at its shipped
   default until an eval has shown a win — every one of them says so in its own
   docstring in `app/config.py`.
6. Start the retrieval server and check `GET /ready` returns 200.
7. Send one `/search` request and check the response has `citations`.
8. If `is_retrieval_log` matters to you, turn it on and read one `report.md` —
   see [docs/retrieval-logging.md](../retrieval-logging.md).

---

## Configuration reference

All from `app/config.py` (one shared `Settings` class with ingestion's; see
[docs/ingestion/12](../ingestion/12-operations-and-troubleshooting.md#configuration-reference)
for the write-path groups — Source, PDF extraction, Knowledge layer, and most of
Security and ops are the same settings and are not repeated here).

### Candidate fetch and ranking

| Setting | Default | Notes |
| --- | --- | --- |
| `retrieval_top_k` | `6` | Passages returned/used after ranking. |
| `retrieval_candidate_k` | `40` | Candidates fetched before ranking. |
| `hybrid_use_sparse` | `false` | Reserved for true sparse vectors (ingest-time writes); not the keyword leg below. |
| `keyword_leg_enabled` | `false` | Full-text leg over `chunk_text` (needs `scripts.create_fulltext_index`); fails open to dense-only if the index is absent. |
| `multi_query_enabled` | `false` | LLM paraphrase fan-out, RRF-fused with the base pull. |
| `multi_query_paraphrases` | `2` | |
| `corrective_loop_enabled` | `false` | One-shot reformulate-and-requery when the top score is low. |
| `corrective_min_score` | `0.2` | Trigger threshold. |
| `reranker_provider` | `embedding` | `embedding` / `cross_encoder` / `cohere`. |
| `rerank_model` | `""` | Provider-specific model name. |
| `rerank_max_candidates` | `40` | Candidates the `cross_encoder` provider scores; bounds its linear cost. |
| `rerank_max_seq_length` | `0` | Cross-encoder tokens per pair; `0` keeps the model default (512). |
| `rerank_score_threshold` | `0.0` | Floor below which a candidate is dropped outright. |
| `rerank_relevance_tolerance` | `0.03` | Ranking-band width; see [05](05-ranking-and-temporal-gating.md). |
| `rerank_volatile_tolerance_multiplier` | `2.0` | Band widening for time-sensitive topics. |
| `rerank_substance_ratio` | `1.5` | Completeness-tier threshold. |
| `rerank_table_boost` | `0.15` | Additive score boost for table-shaped answers to table-shaped questions. |
| `dedup_cosine_threshold` | `0.92` | Near-duplicate collapse threshold in context building. |

### Website preference and PDF budget

| Setting | Default | Notes |
| --- | --- | --- |
| `prefer_website_enabled` | `true` | Dual pull (website / not-website), merged with a concise website lead. |
| `website_candidate_k` | `20` | Website-only candidates pulled alongside the larger not-website pull. |

### Query understanding and structured answers

| Setting | Default | Notes |
| --- | --- | --- |
| `analysis_votes` | `1` | Self-consistency sampling for query understanding; `1` = single pinned-temperature call. |
| `intent_confidence_threshold` | `0.5` | Minimum per-label confidence to keep a multi-label intent. |
| `database_multi_call_enabled` | `false` | LLM-decomposed multi-tool structured planning (v2); any failure falls back to v1. |
| `entity_resolution_enabled` | `false` | Fuzzy theme/author/bundle name resolution; makes an unresolved filter a terminal answer instead of falling through to semantic search. |
| `structured_topic_constraint_enabled` | `true` | Constrain a structured list by the question's own topic rather than the nearest facet bucket. |

### Context and generation

| Setting | Default | Notes |
| --- | --- | --- |
| `context_token_budget` | `9000` | Max tokens of retrieved context sent to the LLM (~5 diverse parent-chunk passages). |
| `faithfulness_check` | `false` | Post-generation entailment verification; one regeneration on failure. |
| `azure_openai_model` | `""` | Generation deployment — distinct from the embedding deployment. |
| `llm_structured_temperature` | *(unset)* | Overrides temperature for structured/deterministic LLM calls when set. |

### Graph retrieval

| Setting | Default | Notes |
| --- | --- | --- |
| `graph_retrieval_enabled` | `false` | Master switch; the graph package is not imported on the request path while this is off. |
| `graph_routing_enabled` | `true` | The kill switch *within* graph retrieval — see [08](08-knowledge-graph-retrieval.md). |
| `graph_routing_classes` | *(unset → all)* | Comma-separated allow-list; a rollout/isolation lever, not the definition of what the graph knows. |
| `graph_routing_budget_seconds` | `3.0` | Wall-clock budget for one graph attempt before falling back. |
| `graph_shadow_enabled` | `false` | Run graph retrieval beside production for comparison, without touching the answer. |
| `graph_shadow_log_path` | *(unset)* | JSONL destination for shadow observations; unset logs to the application log only. |

### Caching, streaming and ops

| Setting | Default | Notes |
| --- | --- | --- |
| `semantic_cache_*` | see [10](10-caching.md#configuration) | Full reference lives with the caching doc. |
| `chat_stream_max_concurrency` | `64` | `/chat`'s dedicated capacity limiter. |
| `auth_enabled` | `false` | `/chat` and `/search` auth. |
| `cors_allow_origins` | `*` | |
| `ops_detail_enabled`, `ops_admin_group`, `otel_*`, `metrics_log_enabled` | see [docs/ingestion/12](../ingestion/12-operations-and-troubleshooting.md#configuration-reference) | Shared settings, same effect on both servers. |
| `is_retrieval_log`, `RETRIEVAL_LOG_*` | see [docs/retrieval-logging.md](../retrieval-logging.md) | Full reference lives with that document. |

---

## Security and access control

The read path shares its auth machinery with ingestion (`app/api/auth.py`,
bearer JWT, `jwt_*` settings) but a different scoping rule:

| Surface | Control |
| --- | --- |
| `/chat`, `/search` | Bearer JWT when `auth_enabled`; **groups do not scope retrieval** — the corpus is public, so an authenticated caller and an anonymous one (`groups: ["public"]`) see the same content. Auth here is about *who may call the API at all*, not what they may see. |
| `/metrics`, `/metrics/timings` | Ops-gated; 404 to everyone else |
| Document-level access control | **None, by design** — same as ingestion: no `tenant_id`, no `acl`. Do not point this stack at a corpus with non-public content. |
| The retrieval trace | Requires no auth to enable (it's a deployment setting, not a request-time one), but its files can contain full passage text and query text — treat `retrieval_log_dir` with the same care as any other log directory holding user questions |

---

## Reliability and scalability

### What scales, and how

| Dimension | Mechanism | Ceiling |
| --- | --- | --- |
| Request throughput | Stateless workers behind a load balancer | Backing-service capacity (Qdrant, the LLM deployment) |
| Concurrent `/chat` streams per instance | `chat_stream_max_concurrency` | Worker thread pool behind it |
| LLM cost/latency | `retrieval_candidate_k` / `retrieval_top_k` sizing, `context_token_budget` | Deployment quota — same throttle gate ingestion's embedding calls use, shared at the client level (`app/core/clients/embeddings.py`) |
| Cache offload | `semantic_cache_enabled` | Hit rate depends on question repetition; see [10](10-caching.md) |

### What does not scale the way you might expect

- **Feature flags are additive cost, not additive latency budget.** Turning on
  `multi_query_enabled`, `keyword_leg_enabled` and `corrective_loop_enabled`
  together multiplies the number of Qdrant round trips a single query makes;
  each was evaluated and shipped OFF individually for a reason stated in its
  own `app/config.py` docstring.
- **The graph has its own budget, not a shared one.** `graph_routing_budget_seconds`
  bounds one graph attempt; it does not borrow time from or lend time to the
  rest of the request, so a slow graph query still falls back within its own
  budget rather than blowing the whole request's latency.
- **The semantic cache is not a substitute for indexing capacity.** It absorbs
  repeated questions; it does nothing for the long tail, which is most of real
  traffic. Do not use cache hit rate as a proxy for "is retrieval fast enough."

---

## Runbooks

### Run the servers

```bash
uvicorn app.main:app --port 8000 --reload      # dev
uvicorn app.main:app --port 8000 --workers 4   # prod-shaped, single host
```

### Send a test query

```bash
curl -X POST localhost:8000/search -H "Content-Type: application/json" \
     -d '{"question": "What does TERI do on carbon capture?"}'

curl -N -X POST localhost:8000/chat -H "Content-Type: application/json" \
     -d '{"question": "What does TERI do on carbon capture?"}'   # SSE stream
```

`/search` is the non-streaming form (`app/api/search.py` → `search_blocks`);
`/chat` streams tokens over SSE (`app/api/chat.py`). Both share the same
pipeline from `rag.query_understanding` onward.

### Debug one answer end to end

```bash
IS_RETRIEVAL_LOG=true uvicorn app.main:app --port 8000
# reproduce the question, then read the newest folder under logs/<today>/
```

See [docs/retrieval-logging.md](../retrieval-logging.md) for reading the trace.

### Check what the read path is spending time on

```bash
curl localhost:8000/metrics/timings   # needs ops visibility — see 11
```

### Flip a retrieval feature flag and validate before keeping it on

1. Flip exactly one flag (e.g. `MULTI_QUERY_ENABLED=true`) in a non-production
   environment.
2. Run `scripts/eval_retrieval.py` or `scripts/benchmark_chat.py` against a
   fixed question set — every flag in `app/config.py` says "launches OFF; flip
   after eval" for exactly this reason.
3. Compare latency (`/metrics/timings`) and quality (the eval script's own
   report) against the baseline before deploying the change.

### Clear a bad cached answer

```bash
python -c "from app.cache import semantic_cache; semantic_cache.clear()"
```

Or wait for `semantic_cache_ttl` to expire it, or for the next ingestion sweep
to move `corpus_revision()` past it — see [10, Caching](10-caching.md).

### Rebuild the full-text index for the keyword leg

```bash
python -m scripts.create_fulltext_index
```

Same script ingestion's operations doc lists — the keyword leg
(`keyword_leg_enabled`) fails open to dense-only until this has run once.

### Run the tests

```bash
pytest tests/retrieval tests/pipeline tests/generation tests/cache
pytest -m "not llm"     # skip tests that need model credentials
```

| Path | Covers |
| --- | --- |
| `tests/retrieval/understanding/` | query analysis, filters, relational shapes, approved aliases, edition resolution |
| `tests/retrieval/search/` | hybrid search, fusion, strategies, reranker bands, temporal gate, title leg |
| `tests/retrieval/context/` | context building, citations |
| `tests/retrieval/structured/` | catalog planning, entity resolution, structured answers |
| `tests/retrieval/graph/` | graph routing, templates, **and the isolation tests** that assert production retrieval never imports `retrieval/graph/` on the default path |
| `tests/pipeline/` | `query_pipeline.py` end to end, `summarize.py` |
| `tests/generation/` | prompts, answer synthesis, faithfulness, redundancy |
| `tests/cache/` | semantic cache keying and invalidation |

`pytest.ini` scopes bare `pytest` to `tests/` — see
[docs/ingestion/12](../ingestion/12-operations-and-troubleshooting.md#run-the-tests)
for why `redundant/` must stay excluded.

---

## Troubleshooting matrix

Expands on `app/README.md`'s "Where a bug lives" table. Start from the symptom.

| Symptom | Most likely cause | How to confirm | Fix |
| --- | --- | --- | --- |
| `GET /ready` returns 503 | Qdrant unreachable | The response body when `ops_detail_enabled` | Restore Qdrant; MySQL alone never causes this on the read server |
| Wrong or missing answer content | The right document was never fetched | Retrieval log's search legs, or `/search`'s raw candidates | Check `retrieval_candidate_k`, the relevant recall leg's flag, and whether the document is even indexed (ingestion issue, not this path) |
| | The document was fetched but not admitted to context | Retrieval log's context section vs. its search section | `context/builder.py` — token budget, dedup threshold, or a conflict flag suppressing it |
| Right documents, bad prose | Generation, not retrieval | Compare the retrieval log's `context` block against the answer | `generation/prompts.py`, `generation/answerer.py` |
| Wrong citation or page number | A payload/citation mapping bug, not a ranking one | `retrieval/context/citations.py`; `core/models/context.py::page_span` | Usually a re-index fixes stale payload; a code bug needs `citations.py` itself |
| Wrong count or list from a "how many" question | Structured planner picked the wrong scope, or fell through when it shouldn't have | `retrieval/structured/` trace, or `catalog/queries.py` directly | `structured/theme_scope.py` / `topic.py` for scope; `entity_resolution_enabled` if a name should have resolved |
| "List the X where Y is mentioned" refuses, or cites an index page such as "Articles & Publications" | The type word or the subject became a filter: `analysis.bundle` set, `tags in [...]` on every search leg, or the title leg matched the type word alone | The trace's `query.analysis` (`bundle`, `tags`) and the `title_leg` event's `document_id in [...]` | `understanding/content_scope.py` (the predicates), `CONTENT_QUESTION_RULE` in `catalog_prompt.py`, `title_leg._kind_words`; see [03](03-query-understanding.md#type-words-and-tags-filters-only-when-the-question-is-about-the-catalog) |
| Graph answers something implausible, or nothing | Routing missed, or the template's parameters don't match intent | `events.graph_routing` share; the graph's own trace entries | `graph/router.py`, `graph/templates.py`; check `graph_routing_classes` isn't unnecessarily narrowed |
| Cache serving a stale answer | `corpus_revision()` hasn't advanced, the question is a near-paraphrase above `semantic_cache_threshold`, or a ranking/prompt change shipped without bumping `PIPELINE_REVISION` | Compare `documents.indexed_at` against the cached answer's timestamp; the trace of a hit says `cached: true` and writes no retrieval events | See [10, Caching](10-caching.md#the-partition-key-semantic_partition). For an ingestion question this is usually correct behaviour, not a bug. For "I fixed the ranking and the answer did not change", bump `PIPELINE_REVISION` in `cache_keys.py` — the same question re-asked matches its own cached answer at cosine 1.0 |
| Query is much slower than usual | A feature flag combination, an LLM deployment slowdown, or throttling | `/metrics/timings` component breakdown | `llm` dominant → check the deployment; `qdrant` dominant → check `retrieval_candidate_k` and how many legs are enabled |
| Streamed (`/chat`) `rag_metrics` line missing `stages` | The SSE thread-hop compensation regressed | Compare a `/search` request's `rag_metrics` line (same question) — it should have `stages`, `/chat`'s should too | See [11, Surviving the SSE stream](11-observability-and-logging.md#surviving-the-sse-stream) |
| `/metrics` or `/metrics/timings` returns 404 for an operator | Not ops-visible | `ops_detail_enabled` off and no matching `ops_admin_group` membership | Set one, or use a private deployment with `ops_detail_enabled=true` |
| A feature flag makes things worse after flipping it | It was flipped without an eval | Diff behavior before/after with `scripts/eval_retrieval.py` | Flip it back; every flag in `app/config.py` documents "flip after eval" for this reason |
| Retrieval trace files not appearing | `is_retrieval_log` off, or a write failure | `logs/` directory; a `WARNING` about "Could not write a retrieval trace" | Enable the flag; check `retrieval_log_dir` permissions |

---

## End-to-end completion criteria

"This query was answered correctly" is a specific, checkable claim, in the same
spirit as ingestion's per-document/per-run/per-sweep criteria
([docs/ingestion/12](../ingestion/12-operations-and-troubleshooting.md#end-to-end-completion-criteria)).

### Per query

A query is fully and correctly answered when:

1. Query understanding produced a `QueryAnalysis` with a resolvable `intent`
   (or a deliberate terminal one — chitchat, out-of-scope).
2. Either the structured/catalog route answered exactly, or the search stage
   returned at least one candidate that reranking placed above
   `rerank_score_threshold`.
3. The context builder admitted at least one block, unless the honest answer is
   "nothing in the corpus addresses this."
4. Generation produced prose grounded in those blocks; if `faithfulness_check`
   is on, the check passed (or the one allowed regeneration passed).
5. Every citation in the response resolves to a real `chunk_id` that was in the
   admitted context — not a hallucinated or stale reference.
6. `rag_metrics` logged the request with `answered: True` (or a deliberate
   `False` for an honest refusal, not a swallowed error).
7. If `is_retrieval_log` is on, the trace's `outcome` is populated (not `{}`)
   and its `context` section is non-empty when an answer was generated.

### Fleet-level, over a window

The read path is healthy when, in addition:

1. `GET /ready` has been 200 throughout the window.
2. `events.graph_routing`'s `fallback` share is stable, not climbing (climbing
   means the graph or its budget is degrading, not that traffic changed shape).
3. The `cached: True` share in `rag_metrics` is consistent with question-
   repetition patterns for this deployment — a sudden drop after a large
   ingestion sweep is expected (see [10](10-caching.md)); a sustained drop with
   no sweep is not.
4. No sustained rise in `p95_ms` for `rag.generate` or `rag.search` without a
   corresponding, explained cause (deployment change, quota, a newly flipped
   feature flag).

---

## Where to look next

- **The code.** Every module in `app/retrieval`, `app/generation` and
  `app/pipeline` carries a docstring explaining *why* it is shaped the way it
  is — the same convention ingestion's docs rely on.
- **The tests.** `tests/retrieval/graph/test_graph_retrieval.py` in particular
  encodes an architectural invariant (graph isolation), not just behavior.
- **`app/README.md`.** The "Where new code goes" and "Where a bug lives" tables
  are the fast paths into this codebase; this document expands the second one
  for the read path specifically.
- **[docs/retrieval-logging.md](../retrieval-logging.md).** The deepest tool
  for understanding one specific query.

---

Previous: [11 — Observability and Logging](11-observability-and-logging.md) · Back to the [index](README.md)
