# 10 — The Semantic Answer Cache

**Purpose.** Answer a near-duplicate question from a prior result instead of
paying for retrieval, ranking and generation again — without ever serving an
answer the corpus, or the retrieval settings, have since moved past.

**Inputs.** The query embedding, the result width (`top_k`), the detected
answer format, and a compact facet fingerprint of the processed query.

**Outputs.** A cached result dict on a hit, or nothing (the caller proceeds to
ordinary retrieval) on a miss — and, as a side effect of every store, an
opportunistic prune of expired entries.

**Components.** `app/cache/semantic_cache.py`, `app/cache/cache_keys.py`.
Reads `app.catalog.queries.corpus_revision()` — the read-side counterpart of
the write-path table documented in
[ingestion 08](../ingestion/08-persistence-and-catalog.md#what-ingestion-does-not-hand-off).

---

## Where it sits

`query_pipeline._answer_front_matter` (the shared entry for both the
buffered and streaming answer paths) runs the cache lookup **after** query
understanding and query embedding, but **before** structured/scoped-summary
short-circuits have a chance to fall through to it — a chit-chat intent, a
pure structured answer, or a scoped summary never reach the cache at all,
because none of them is the kind of answer this cache stores:

```
question
  -> process(question, history)              query understanding
  -> chitchat? / structured? / scoped_summary?   return early, cache never consulted
  -> embed_query(search_query)
  -> semantic_cache.lookup(...)               <-- here
       hit  -> return the stored result, done
       miss -> continue to ordinary retrieval, generation, ...
  -> (after generation, verification, and both correction passes)
  -> semantic_cache.store(...)
```

So only grounded QA answers — the ones that actually ran retrieval and
generation — are cached; nothing here caches a catalog count, a graph answer,
or small talk.

---

## What makes two questions "the same" here

A cache hit requires **two** things to agree, not one:

1. **Vector proximity.** `lookup` runs a Qdrant nearest-neighbour search on
   the query embedding, restricted to points in the same **partition**
   (`scope`, a `MatchValue` filter — see below) and not yet expired, gated by
   `semantic_cache_threshold` (cosine, default **0.995**). That threshold is
   deliberately tight: at an earlier, looser 0.97 a subtly different question
   — another year, another theme — could return the wrong cached answer, and
   correctness was judged to matter more here than hit rate.
2. **An exact facet-fingerprint match**, checked as a Python equality
   *after* the vector search returns its single best candidate
   (`facet_fingerprint`). This is deliberately not folded into the vector
   search itself: it is a compact dict of `source_type`, `language`,
   `theme`, `author`, `date_from`/`date_to` and sorted `tags`, all
   lower-cased and stripped, with `None` values dropped. A cached answer
   built under one filter set must never be served to a query under a
   different one, however close the two questions' embeddings are — a legacy
   entry stored before this fingerprint existed simply fails the equality
   check and ages out normally via its `expires_at`.

Both checks are necessary: proximity alone would risk serving an answer
scoped to the wrong theme or date range; the fingerprint alone (with no
vector search) would require an exact rewrite of the question.

When a question draws on a live priority page, the fingerprint also carries
`priority`: each page's URL with the start of its content hash. The answer is
then reused only while the page says the same thing, and an ordinary question's
fingerprint is unchanged. See [13 — Priority Pages](13-priority-pages.md#caching).

---

## The partition key: `semantic_partition`

`scope` — the Qdrant payload field both `lookup` and `store` filter on — is a
SHA-256 hash (`cache_keys._sha`) of four things, computed fresh on every call
rather than cached:

| Input | Purpose |
| --- | --- |
| `_pref_fingerprint()` | Hash of `PIPELINE_REVISION`, the retrieval-preference knobs (`prefer_website_enabled`, `website_candidate_k`, `retrieval_top_k`, `retrieval_candidate_k`, `context_token_budget`) and the chat model with the two settings that shape its calls (`azure_openai_model`, `llm_temperature_supported`, `llm_reasoning_effort`) — so switching models retires the previous model's answers instead of serving them for the rest of their TTL |
| `top_k` | The requested result width |
| `answer_format` | list/table/summary/detailed/timeline/default |
| `corpus_revision()` | The indexed corpus's current state — see below |

**Caller identity is deliberately absent.** The corpus is public and every
caller retrieves over all of it, so two callers asking the same question are
owed the same answer; partitioning by identity would only fragment the cache
into more, smaller partitions for no correctness gain.

Toggling `prefer_website_enabled` or tuning any of its knobs changes the
partition, so an old-mode answer is never served to a new-mode query — the
alternative would be old answers surviving until TTL and polluting a
before/after tuning comparison.

### `corpus_revision()`: self-invalidation without a callback

`app.catalog.queries.corpus_revision()` returns `"<MAX(indexed_at) or
'never'>|<COUNT(*)>"` over the `documents` table, in-process TTL-cached for
`_CORPUS_REVISION_TTL_SECONDS = 30` — read on *every* cached query, so it is
trusted for far less time than the other catalog reads on this path (the
bundle-inventory cache elsewhere uses ten minutes): this value gates whether
a cached answer may be served at all, so 30 seconds is the ceiling on how
long a just-reindexed or just-deleted document can still be served from a
stale cache entry, chosen because it sits comfortably below the LLM latency
this cache exists to avoid.

Two independent signals, and both matter:

- `MAX(indexed_at)` moves only when a document is actually **re-chunked and
  re-indexed** — `state.upsert` `COALESCE`s the column
  (see [ingestion 08](../ingestion/08-persistence-and-catalog.md#coalesce-vs-values-field-by-field)),
  so a fingerprint-only `unchanged_content` refresh deliberately does **not**
  move it, and correctly does not invalidate anything: nothing retrievable
  changed.
- `COUNT(*)` moves whenever a document is added or deleted — a case
  `indexed_at` alone would miss.

Together they change for every ingestion event that could alter what
retrieval returns, and for nothing else.

**`None` means *unknown*, and both `lookup` and `store` treat that as "do not
touch the cache"** — `semantic_partition` returns `None` when the revision
query fails, and both callers bail out rather than falling back to a partial
key. This is stated as the one deliberately unsafe direction to avoid: fail
open here (pin the partition to a placeholder) and a MySQL outage would let
ingestion keep changing the corpus underneath a cache that stopped noticing.
Bypassing the cache — answering fresh every time — is the safe failure mode,
even though it costs the full retrieval-plus-generation latency on every
query for the outage's duration.

This is the read-side half of the mechanism the write path exposes in
[ingestion 08, "What ingestion does not hand off"](../ingestion/08-persistence-and-catalog.md#what-ingestion-does-not-hand-off):
ingestion pushes no invalidation and knows nothing about this cache; the cache
independently reads the same aggregate on every lookup and store, so a cached
answer simply becomes unreachable — its partition key no longer matches —
the moment a sweep actually changes what's indexed.

---

## Lookup and store, mechanically

Both `lookup` and `store` are backed by a dedicated Qdrant collection
(`semantic_cache_collection`, default `"semantic_cache"`), created lazily on
first `store` with a payload index on `scope` (keyword) and `expires_at`
(float) — `lookup` never creates the collection, so a cache that has never
been written to simply reports "not found" rather than creating an empty
collection on the read path.

- **`lookup`** filters on `scope == <partition>` **and** `expires_at >=
  now()` in the same Qdrant query, `limit=1`, so an expired point is
  invisible to a reader even before `prune` has run.
- **`store`** writes one point keyed by a fresh UUID, payload
  `{result, scope, facets, expires_at: now() + semantic_cache_ttl}`, then
  calls `_maybe_prune`.
- **Pruning** is opportunistic and count-based, not time-based:
  `_maybe_prune` increments a module-level counter on every store and runs a
  `Filter(expires_at < now())` delete every `semantic_cache_prune_every`
  stores (default 200; `0` disables the opportunistic path entirely, relying
  on lookup-time filtering plus whatever scheduled prune a deployment runs
  separately). Qdrant has no native TTL, which is the entire reason
  `expires_at` and this pruning exist — the alternative would be a
  collection that only grows.

## Fails open, always

Every operation in `semantic_cache.py` catches its own exceptions and
degrades to "cache disabled for this call" rather than failing the query:
`_client()` catches a Qdrant construction failure and returns `None`;
`lookup` catches a search failure and returns `None`; `store` catches an
upsert failure and returns having done nothing; `prune` catches a delete
failure and logs a warning. None of these can turn a cache problem into a
user-facing error — the worst case is always "this query costs full
retrieval and generation," never "this query fails."

## A note on Redis: provisioned, not used here

`app/config.py` defines `redis_url` immediately above the semantic-cache
settings, and `app/core/clients/cache.py::get_redis()` constructs a client
from it — but the only caller in `app/` is `api/health.py`'s optional
`/ready`/`/metrics` Redis probe (behind `ops_detail_enabled`; see
[11](11-observability-and-logging.md)). Nothing on the read path
described in this document reads or writes Redis: the semantic answer cache
is entirely Qdrant-backed, and an unset `redis_url` has no effect on it.

---

## Validation at this stage

| Check | Where | On failure |
| --- | --- | --- |
| Corpus revision is known | `semantic_partition` | Returns `None`; caller skips the cache entirely |
| Stored facet fingerprint matches the query's | `lookup`, post-filter | Treated as a miss, even on a vector match |
| Query vector / cache disabled | `lookup`, `store` | No-op, returns `None` / returns |
| Qdrant reachable | every operation's own `try`/`except` | Logged warning; cache disabled for that call |

## Failure scenarios

| Scenario | Detection | Response | Recovery |
| --- | --- | --- | --- |
| MySQL (corpus_revision) unreachable | `except` in `corpus_revision` | `None`; `semantic_partition` returns `None`; cache bypassed | Next query, once MySQL is back |
| Qdrant unreachable | `except` in `_client` | Cache disabled for that call; query answered fresh | Next query |
| Semantic-cache collection missing on lookup | `collection_exists` check | Treated as a miss; collection is **not** created by `lookup` | Created lazily on the next `store` |
| A near-duplicate question under a different facet scope | `facet_fingerprint` post-filter | Miss, even though the embeddings matched | — |
| Retrieval-preference settings tuned | `_pref_fingerprint` changes | Old entries become unaddressable (different partition); no stale cross-mode serving | Old points age out via `expires_at` |
| Chat model switched, or its temperature / reasoning-effort settings changed | `_pref_fingerprint` changes | The previous model's answers become unaddressable | Old points age out via `expires_at` |
| Ranking, context or prompt behaviour changed in code | `PIPELINE_REVISION` in `cache_keys.py`, bumped by hand as part of the change | Old entries become unaddressable; the fix is visible on the next question rather than after the TTL. A code change has no setting to hash, so without the bump the pre-fix answer is served for up to 24 hours and the fix looks like it did nothing — which is how one ranking fix was reported as not working three times | Old points age out via `expires_at` |
| A sweep re-indexes or adds/deletes documents | `corpus_revision()` changes | Every previously-cached partition for that revision becomes unaddressable | New answers repopulate the cache under the new revision |
| Cache grows unbounded | Qdrant has no native TTL | `_maybe_prune` every `semantic_cache_prune_every` stores; `expires_at` filtered at lookup regardless | Run `prune()` manually, or via a scheduled job, if opportunistic pruning is disabled |

## Observability

- `rag.semantic_cache` span (lookup) and `rag.semantic_cache_store` span
  (store) in the query pipeline; the lookup span's `hit` attribute records
  whether a result was returned.
- Per-query retrieval-log trace (`is_retrieval_log=true`) records the lookup
  as a `qdrant_call("vector_search", stage="semantic_cache_lookup", ...)`
  event, including the computed `scope` and the facet fingerprint used — see
  [11](11-observability-and-logging.md).
- Log lines: `"Qdrant unavailable; semantic cache disabled."`,
  `"Could not ensure semantic cache collection."`,
  `"Semantic cache lookup failed."`, `"Semantic cache store failed."`,
  `"Semantic cache prune failed."`.

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `semantic_cache_enabled` | `true` | The one switch; both `lookup` and `store` no-op when off. |
| `semantic_cache_threshold` | `0.995` | Cosine similarity floor for a hit. Deliberately tight — see above. |
| `semantic_cache_collection` | `"semantic_cache"` | The dedicated Qdrant collection name. |
| `semantic_cache_ttl` | `86400` (24h) | Seconds until a stored entry's `expires_at` passes, absent an earlier corpus-revision or fingerprint change making it unreachable sooner. |
| `semantic_cache_prune_every` | `200` | Stores between opportunistic prune sweeps; `0` disables the opportunistic path. |

## Hand-off

A cache hit returns the stored result dict directly to the same code path a
fresh answer would reach — the caller cannot tell the difference except by
the `rag.semantic_cache` span's `hit` attribute, which is the point: caching
is an optimisation over the answer pipeline in [09](09-generation-and-synthesis.md),
never a second answer contract for the client to understand.

---

Previous: [09 — Generation and Answer Synthesis](09-generation-and-synthesis.md) · Next: [11 — Observability and Retrieval Logging](11-observability-and-logging.md)
