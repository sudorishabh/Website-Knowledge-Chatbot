# `app/retrieval/` — the read path

The largest package in the codebase (~12k lines). It is organised as **four
sequential stages** plus **two alternative answer routes**, and the import
direction between them is one-way.

```
                       question
                          |
              +-----------v-----------+
              |     understanding/    |   what is being asked, and how to filter
              +-----------+-----------+
                          |  QueryAnalysis
      +-------------------+-------------------+
      |                   |                   |
+-----v-----+      +------v------+     +------v------+
| structured|      |   search/   |     |   graph/    |
| (catalog  |      | fetch + rank|     | (knowledge  |
|  answers) |      +------+------+     |   graph)    |
+-----------+             |            +------+------+
                   +------v------+            |
                   |  context/   |<-----------+
                   | blocks +    |
                   | citations   |
                   +------+------+
                          |
                    generation/
```

`retriever.py` at the top level is the orchestrator: it runs
understanding → search → context, and consults `graph/` behind a flag.
`structured/` and `graph/` are chosen by the *caller*
(`app/pipeline/query_pipeline.py`), not by `retriever.py`.

---

## The stages

### `understanding/` — question → what to retrieve

| Module | Responsibility |
| --- | --- |
| `query_processor.py` | The entry point and the data contracts (`QueryAnalysis`, `QueryUnderstanding`): the LLM call, sample voting/merge, legacy derivation. |
| `prompts.py` | The understanding prompt text, split out to keep control flow readable. |
| `filters.py` | `QueryAnalysis` → a Qdrant facet filter. |
| `relational.py` | Relational and comparative question shapes. |
| `approved_aliases.py` | Vetted surface forms for entities the corpus names inconsistently. |
| `annual_report_editions.py` | Which edition of a recurring series a question means. |
| `catalog_prompt.py` | The corpus description (bundles, themes) injected into understanding and structured planning. |

Nothing here touches Qdrant.

### `search/` — candidate fetch and ordering

Deliberately **one** package rather than separate "fetch" and "rank" ones:
`hybrid_search.Candidate` is the type every module here passes around, so
splitting them would put that type on one side of a boundary and half its users
on the other — a cycle by construction.

Read in this order:

| Module | Responsibility |
| --- | --- |
| `hybrid_search.py` | **The primitive.** `Candidate`, `build_filter`, `search`. Everything else sits on it. |
| `fusion.py` | Reciprocal-rank fusion across legs. |
| `strategies.py` | Recall expansion: website-biased dual pull, keyword full-text leg, multi-query paraphrasing, one-shot corrective requery. |
| `scoped_retrieval.py` | Search restricted to named documents. |
| `title_leg.py` | The title-anchored leg — for pages whose text is a list of link labels no embedding matches. |
| `reranker.py` | Reordering, and the authority / recency / substance bands. |
| `volatility.py` | Whether a question's answer decays with time (read by the recency band). |
| `temporal_gate.py` | Drops candidates a temporal question excludes. |

### `context/` — candidates → what the LLM sees

| Module | Responsibility |
| --- | --- |
| `builder.py` | Which candidate text is admitted, in what order, with what page attribution. Parent expansion, dedup, conflict flagging, token budget. |
| `citations.py` | Describes those same blocks back to the user. |

`ContextBlock` itself lives in `app/core/models/context.py`, not here, so
`generation/` never has to import a retrieval implementation module.

---

## The alternative routes

### `structured/` — questions the catalog answers exactly

"How many policy briefs are there?" is a `COUNT(DISTINCT document_id)`, not a
vector search. This subpackage plans and executes those against MySQL.

`planner.py` (what to ask), `tools.py` (the operations), `entities.py` (the
queryable registry, one per bundle), `resolve.py` / `filters.py` /
`theme_scope.py` / `topic.py` (turning names into ids and scopes),
`answerer.py` (terminal answer, or fall through to ordinary retrieval),
`types.py`.

### `graph/` — questions the knowledge graph answers

Verified relationships with provenance. **Isolated by construction**: nothing on
the default path imports this package, every reference from production retrieval
is inside a function behind a flag that is off, and two tests in
`tests/retrieval/graph/test_graph_retrieval.py` assert that importing production
retrieval does not *load* it.

Neo4j returns identifiers and structure, never source text; the hop back to text
is `chunk_id` in Qdrant.

`router.py` (does a template fit?), `templates.py` (the closed, parameterised
query set), `plans.py`, `policy.py`, `pipeline.py`, `traverse.py`,
`hydrate.py` (ids → text), `facts.py`, `scope.py`, `shadow.py` (measure without
answering), `intent.py`.

---

## Import rules

- `understanding/` → `search/` → `context/`. One way.
- `retriever.py`, `structured/` and `graph/` sit above all three.
- Nothing here imports `app.ingestion`, `app.pipeline` or `app.generation`.
- Shared vocabulary comes from `app.core` (`corpus.py`, `editions.py`,
  `models/context.py`) — not from a write-path module.

`tests/test_architecture.py` enforces the package-level rules; the graph
isolation tests enforce the rest.

---

## Where to start reading

1. `app/pipeline/query_pipeline.py` — the whole query, top to bottom.
2. `retriever.py` — how the legs are combined and merged.
3. `search/hybrid_search.py` — what a `Candidate` is.
4. `context/builder.py` — how candidates become the prompt.
