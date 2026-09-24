# Retrieval and Generation Documentation

This is the complete, start-to-finish description of how a question becomes a
cited answer: the HTTP entry points, query understanding, candidate search and
fusion, ranking and temporal gating, context assembly, the two alternative
answer routes (structured catalog answers and knowledge-graph answers),
answer generation and verification, the semantic answer cache, and the
observability wired through all of it.

Everything here is written from the code in `app/pipeline`, `app/api`,
`app/retrieval`, `app/generation`, `app/cache` and the read-path parts of
`app/observability`. Where a behaviour has a non-obvious reason, the reason is
given — several of the surprising choices in this pipeline exist because a
simpler version was measured against real questions and made things worse in a
specific, documented way (parent-node false-positive conflicts, the "upcoming
events" temporal bug, authority losing to raw relevance, a 4-in-6 date-claim
failure rate).

**For the codebase as a whole** — how the read path relates to the write path,
the layering rules, and where new code belongs — see
[`app/README.md`](../../app/README.md). **For the write path** — how content
gets from the source site into the stores this pipeline reads — see
[`docs/ingestion/`](../ingestion/README.md). This set is the read path in
depth.

## Before you read further

Two facts are easy to get wrong from the code's own docstrings and worth
stating up front:

- **The knowledge-graph route is not "off by default."** Every docstring in
  `app/retrieval/graph/` and `app/retrieval/README.md` describes the package
  as isolated because `graph_retrieval_enabled` is `false` by default. That
  flag is never read by any gating code — it is vestigial. The switch that
  actually controls whether a production question reaches Neo4j is
  `graph_routing_enabled`, which defaults to **`true`**. On an unconfigured
  deployment, graph routing runs on every query today. See
  [08, "What isolated actually means today"](08-knowledge-graph-retrieval.md#read-this-section-first-what-isolated-actually-means-today).
- **Two generation-side modules are built and tested but not wired in.**
  `app/generation/sections.py::split_sections` and
  `app/generation/redundancy.py::filter_pdf_text` have no caller anywhere in
  `app/` outside their own module and tests. The only live mechanism
  filtering PDF redundancy today is a prompt instruction, not code. See
  [09, "PDF redundancy filtering: built, tested, not currently called"](09-generation-and-synthesis.md#pdf-redundancy-filtering-built-tested-not-currently-called).

## Read in this order

| Doc | What it covers |
| --- | --- |
| [01 — Read Path Overview](01-overview.md) | What the read path does, why it exists, the components, the whole `query_pipeline.py` lifecycle in one page, and the vocabulary the rest of the set uses. |
| [02 — Triggers and the API Surface](02-triggers-and-api.md) | The two ways a question enters the system (`/chat` SSE, `/search`), auth, streaming and mid-stream failure, and backpressure on the read path. |
| [03 — Query Understanding](03-query-understanding.md) | `QueryAnalysis`/`QueryUnderstanding`, the intent taxonomy, facet-filter translation, relational questions, approved aliases, and annual-report edition resolution. |
| [04 — Search and Fusion](04-search-and-fusion.md) | `Candidate`, the hybrid search primitive, reciprocal-rank fusion, recall-expansion legs (multi-query, keyword, corrective requery), the title-anchored leg. |
| [05 — Ranking and Temporal Gating](05-ranking-and-temporal-gating.md) | The authority/recency/substance bands, why banding instead of a weighted blend, volatility, and the "upcoming events" temporal gate. |
| [06 — Context and Citations](06-context-and-citations.md) | `ContextBlock` admission: parent expansion, dedup, conflict flagging, token budget, and how citations describe those blocks back to the user. |
| [07 — Structured Answers](07-structured-answers.md) | Questions the catalog answers exactly — counts and lists — the entity registry, scope resolution, and the terminal-vs-fall-through decision. |
| [08 — Knowledge Graph Retrieval](08-knowledge-graph-retrieval.md) | Verified relationships from Neo4j, the template registry's safety guarantees, routing, and the merge into ordinary context. |
| [09 — Generation and Synthesis](09-generation-and-synthesis.md) | The grounding prompt, the answer call, faithfulness checking, the publication-date guard, and answer sectioning. |
| [10 — Caching](10-caching.md) | The semantic answer cache: what makes two questions "the same," the corpus-revision partition key, and fail-open behaviour. |
| [11 — Observability and Logging](11-observability-and-logging.md) | Spans and timing metrics, the per-query retrieval trace, and read-path health/readiness. |
| [12 — Operations and Troubleshooting](12-operations-and-troubleshooting.md) | Deployment, the full configuration reference, security, scalability, runbooks, and a troubleshooting matrix. |

## Topic map

Where each cross-cutting concern is covered, for readers arriving with a
specific question rather than reading front to back.

| Topic | Primary | Also |
| --- | --- | --- |
| How a question enters the system | [02](02-triggers-and-api.md#the-two-ways-a-request-enters) | [01](01-overview.md#the-complete-lifecycle) |
| Authentication and authorization | [02](02-triggers-and-api.md#authentication-and-authorization) | [12](12-operations-and-troubleshooting.md#security-and-access-control) |
| Streaming and mid-stream failure | [02](02-triggers-and-api.md#1-post-chat--streamed) | [11](11-observability-and-logging.md#surviving-the-sse-stream) |
| Intent detection and query analysis | [03](03-query-understanding.md#the-intent-taxonomy) | [01](01-overview.md#vocabulary) |
| Facets and filters | [03](03-query-understanding.md#facets--qdrant-filter-filterspy) | [04](04-search-and-fusion.md) |
| Entity recognition in a question | [03](03-query-understanding.md#query-time-entity-recognition-approved_aliasespy) | [07](07-structured-answers.md#entities-the-queryable-registry), [08](08-knowledge-graph-retrieval.md#routing-which-questions-are-graph-shaped) |
| Candidate fetch and vector search | [04](04-search-and-fusion.md#the-primitive-candidate-and-search) | |
| Fusion across retrieval legs | [04](04-search-and-fusion.md#fusing-rankings-reciprocal-rank-fusion-fusionpy) | |
| Recall expansion (multi-query, keyword, corrective) | [04](04-search-and-fusion.md#recall-expansion-legs-strategiespy) | |
| Ranking and score bands | [05](05-ranking-and-temporal-gating.md#why-banding-not-a-weighted-blend) | |
| Temporal questions and the recency gate | [05](05-ranking-and-temporal-gating.md#temporal-gating-the-upcoming-problem) | |
| What the LLM actually sees | [06](06-context-and-citations.md#admission-dedup-budget-ordering) | [09](09-generation-and-synthesis.md#the-grounded-prompt-two-shapes-chosen-by-what-arrived) |
| Conflicting sources | [06](06-context-and-citations.md#conflict-flagging) | |
| Citations and page numbers | [06](06-context-and-citations.md#citations-describing-blocks-back-to-the-user) | |
| Counts and list questions | [07](07-structured-answers.md) | |
| Relational / knowledge-graph questions | [08](08-knowledge-graph-retrieval.md) | |
| Answer generation and prompting | [09](09-generation-and-synthesis.md#the-grounded-prompt-two-shapes-chosen-by-what-arrived) | |
| Faithfulness / hallucination checking | [09](09-generation-and-synthesis.md#post-generation-verification-faithfulness) | |
| Date claims in generated answers | [09](09-generation-and-synthesis.md#post-generation-verification-the-publication-date-guard) | [06 (annual editions on the write side)](../ingestion/06-canonical-document-and-dates.md) |
| Caching and cache invalidation | [10](10-caching.md#the-partition-key-semantic_partition) | [08, write-side view](../ingestion/08-persistence-and-catalog.md#what-ingestion-does-not-hand-off) |
| Logging, spans, timing metrics | [11](11-observability-and-logging.md#spans-and-timing-metrics) | |
| Per-query retrieval trace | [11](11-observability-and-logging.md#the-retrieval-trace-and-how-it-hooks-in) | [docs/retrieval-logging.md](../retrieval-logging.md) |
| Health and readiness | [11](11-observability-and-logging.md#health-and-readiness) | [12, ingestion contrast](../ingestion/11-observability-and-monitoring.md#ready-on-the-ingestion-server) |
| Deployment and scaling | [12](12-operations-and-troubleshooting.md#deployment) | [01](01-overview.md) |
| Configuration reference | [12](12-operations-and-troubleshooting.md#configuration-reference) | every doc's own config table |
| Security and access control | [12](12-operations-and-troubleshooting.md#security-and-access-control) | [02](02-triggers-and-api.md) |
| Troubleshooting a wrong or missing answer | [12](12-operations-and-troubleshooting.md#troubleshooting-matrix) | [`app/README.md`, "Where a bug lives"](../../app/README.md#where-a-bug-lives) |

## Where the code is

Each document maps to the modules it describes. The package-level layering is
enforced by `tests/test_architecture.py`, and the knowledge-graph isolation
claims (see "Before you read further" above) by
`tests/retrieval/graph/test_graph_retrieval.py`.

| Doc | Modules |
| --- | --- |
| 01 | `app/pipeline/query_pipeline.py` · `app/pipeline/summarize.py` |
| 02 | `app/api/chat.py` · `app/api/search.py` · `app/api/auth.py` · `app/api/health.py` · `app/main.py` · `app/app_factory.py` · `app/schemas/query.py` |
| 03 | `app/retrieval/understanding/{query_processor,prompts,filters,relational,approved_aliases,annual_report_editions,catalog_prompt}.py` |
| 04 | `app/retrieval/search/{hybrid_search,fusion,strategies,scoped_retrieval,title_leg}.py` |
| 05 | `app/retrieval/search/{reranker,volatility,temporal_gate}.py` |
| 06 | `app/retrieval/context/{builder,citations}.py` · `app/core/models/context.py` |
| 07 | `app/retrieval/structured/{planner,tools,entities,resolve,filters,theme_scope,topic,answerer,types}.py` |
| 08 | `app/retrieval/graph/{router,templates,plans,policy,pipeline,traverse,hydrate,facts,scope,shadow,intent}.py` |
| 09 | `app/generation/{prompts,answerer,sections,answer_plan,faithfulness,redundancy,date_claims}.py` |
| 10 | `app/cache/{semantic_cache,cache_keys}.py` |
| 11 | `app/observability/{tracing,metrics}.py` · `app/observability/retrieval_log/*` |
| 12 | `app/config.py` · `docker-compose.yml` · `app/retrieval/retriever.py` (the orchestrator that ties search, ranking, context and graph together — see doc 04 for its search-assembly role and docs 05–06 for its context-merge and gating role) |

`app/retrieval/retriever.py` does not have a single dedicated doc — it is the
glue between stages, so its logic is described where each piece of that logic
takes effect: candidate assembly in [04](04-search-and-fusion.md), the
temporal-gate call and graph merge in
[05](05-ranking-and-temporal-gating.md#where-this-sits-in-the-request) and
[06](06-context-and-citations.md), and graph routing in
[08](08-knowledge-graph-retrieval.md).

## If you are here for one thing

- **"Is it working?"** → [11](11-observability-and-logging.md), then `GET /metrics/timings`.
- **"The answer is wrong or missing content."** → [12, Troubleshooting](12-operations-and-troubleshooting.md#troubleshooting-matrix), then [04](04-search-and-fusion.md) (was it fetched?) and [06](06-context-and-citations.md) (was it admitted?).
- **"The citation or page number is wrong."** → [06, Citations](06-context-and-citations.md#citations-describing-blocks-back-to-the-user).
- **"A count or list looks wrong."** → [07](07-structured-answers.md).
- **"The graph gave a relational answer I didn't expect."** → [08](08-knowledge-graph-retrieval.md) — and read "Before you read further" above first.
- **"The answer looks stale."** → [10](10-caching.md#the-partition-key-semantic_partition).
- **"I want the exact trace of one query."** → [`docs/retrieval-logging.md`](../retrieval-logging.md).
