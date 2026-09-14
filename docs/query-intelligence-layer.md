# Query Intelligence Layer — Phase 0: discovery and baseline

Branch: `feature/query-intelligence-layer` (cut from `main` @ `0d61baf`, clean tree).

This document records what the read path already does, what it does **not** do,
and the smallest integration points for the new layer. It is written before any
code changes so the later diffs can be read against it.

---

## 1. The headline finding

**Most of the target architecture already exists.** This codebase is not a naive
RAG pipeline: it already has multi-label query understanding, three-way source
routing, band-based reranking with authority and recency, a temporal-scope
classifier, document conflict flagging, and a full claim-level
supersession/validity model in the knowledge layer.

The work is therefore **not** to build a Query Intelligence Layer from scratch.
It is to:

1. **connect signals that are computed and then discarded** (the
   `clarification_needed` intent, five of six temporal modes, claim-level
   supersession), and
2. **add the three genuinely missing capabilities** (clarification state across
   turns, sub-query execution, temporal-fit ranking).

Building a parallel layer would duplicate — and eventually contradict — machinery
that is already measured and tested. Every phase below extends an existing seam.

---

## 2. The read path as it stands

```
POST /chat  (app/api/chat.py)            SSE: token | correction | sources | done
  └─ stream_answer                       (app/pipeline/query_pipeline.py)
       └─ _prepare
            ├─ process()                 (retrieval/understanding/query_processor.py)
            │    • N-sample voted LLM call -> QueryUnderstanding (multi-label)
            │    • merged -> QueryAnalysis (single-label, legacy contract)
            │    • _corrected_intent: lexical rescue of chitchat misfires
            │    • filters = facet filters + annual-report edition conditions
            ├─ chitchat / structured / scoped_summary short-circuits
            ├─ embed_query -> semantic_cache.lookup
            ├─ parallel: extract_requirements | _db_section | retrieve
            │    └─ retrieve()           (retrieval/retriever.py)
            │         ├─ graph leg       (retrieval/graph/policy.attempt)
            │         ├─ base pull       (plain or website-biased dual)
            │         ├─ recall legs     keyword | content-term | title | multi-query
            │         ├─ rrf fusion      (search/fusion.py)
            │         ├─ rerank          (search/reranker.py)
            │         ├─ corrective loop (one shot, flagged off)
            │         ├─ build_context   (context/builder.py: dedup, parent
            │         │                    expansion, conflict flagging, budget)
            │         ├─ graph merge
            │         └─ _gate_temporal  (UPCOMING only)
            └─ build_plan -> plan_directive
       └─ generate_stream -> faithfulness verify -> date-claim guard -> citations
```

### Conversation state

There is **none on the server**. `QueryRequest.history` is a client-supplied
`list[ChatTurn]`, passed down to `process()` (for pronoun resolution) and to
generation. There is no session store, no turn id, no server-side memory.

This is the single most important constraint on Phase 1: clarification state has
to be carried somewhere, and the existing model offers only the history the
client echoes back.

---

## 3. Goal-by-goal: what exists, what is missing

| # | Goal | Status | Where |
| --- | --- | --- | --- |
| 1 | Answer clear questions directly | **Done** | `process()` never clarifies on the qa path |
| 2 | Targeted clarification | **Detected, then discarded** | `clarification_needed` is a real intent label, but `_legacy_intent_and_format` collapses it onto `chitchat`, which answers with generic small talk |
| 3 | Data-driven options | **Done for one case only** | `structured/filters.AmbiguousFilter` + `resolve.plausible()` + `tools._ambiguous_result` produce a numbered, catalog-derived clarification for ambiguous author/theme/tag/bundle names. Nothing equivalent for vague *content* questions |
| 4 | Multi-turn clarification state | **Missing** | no session model (§2) |
| 5 | Decomposition | **Partial** | `generation/answer_plan.extract_requirements` already splits a question into separately-answerable requirements, and `build_plan` checks each against retrieved text. But the requirements never become *sub-queries* — one retrieval serves them all |
| 6 | Multi-query retrieval | **Present but wrong shape, and off** | `strategies.paraphrases` generates literal paraphrases (the spec explicitly rules these out); gated by `multi_query_enabled=False`, ≥5 words, no filters, content intent |
| 7 | Source routing | **Done at query level** | structured→MySQL, graph→Neo4j, search→Qdrant, chosen in `_prepare` + `retriever.retrieve`. Missing only *per-sub-query* routing |
| 8 | Temporal intent | **Classified, mostly unused** | `search/temporal_gate.detect_mode` returns PAST / UPCOMING / CURRENT / POINT_IN_TIME / DATE_RANGE / NONE. Only `UPCOMING` changes anything; its own docstring says so |
| 9 | Relevance + temporal + authority + validity ranking | **Partial** | `reranker.py` bands on relevance → authority → substance → recency. Authority is *derived* from `source_type`/`bundle`. Temporal **fit** (does this document's validity window cover the asked-for time?) is not a signal |
| 10 | Never blindly prefer newest | **Done — preserve** | The band design is explicitly built for this; the module docstring documents the measurement behind it. Must not be regressed |
| 11 | Competing versions / conflicts | **Two half-systems** | `context/builder._flag_conflicts` flags document-level disagreement; `knowledge/claims/conflicts.py` has a full mechanical ladder (functional predicates, overlap, basis rank, `supersedes`/`contradicts` links, `disputed`). The claim ladder never reaches document ranking |
| 12 | Avoid stale info on current-state questions | **Partial** | `volatility.is_volatile` widens the relevance band so recency can break more ties. `claims.is_current_state_eligible` exists graph-side |
| 13 | Preserve history | **Done — preserve** | `conflicts.py`: "A conflict changes *status*, never existence." |
| 14 | Loop prevention | **Missing** | nothing tracks how many times we have asked |
| 15 | Preserve ingestion | **Constraint** | see §4 |

---

## 4. Files deliberately NOT touched

Per the preservation rule, the following are treated as intentional and are
out of scope for every phase. The new layer reads their outputs; it never
changes them.

| Area | Paths | Why it stays |
| --- | --- | --- |
| Ingestion pipeline | `app/ingestion/**` (all 40 modules) | source-specific parsing, Drupal field mappings, chunking config, payload construction, date resolution rules |
| Knowledge build | `app/knowledge/**` | claim extraction, gazetteer, graph projection, author/PI promotion, the conflict ladder itself |
| Catalog schema | `app/catalog/schema.py`, `app/catalog/entities.py` | fixed MySQL table/column mappings |
| Corpus vocabulary | `app/core/corpus.py`, `app/core/editions.py` | bundle names, scheduled-bundle set, edition conventions |
| Qdrant payload keys | any writer of `effective_start_date`, `bundle`, `source_type`, `parent_chunk_id` | the read path consumes these names; it must not rename them |

**No new ingest-time metadata is required by any phase.** Every temporal and
authority signal the plan uses (`effective_start_date`, `effective_end_date`,
`bundle`, `source_type`, and claim `valid_from`/`valid_until`/`status`) is
already written today. This is checked per phase.

---

## 5. Integration points (smallest safe seams)

| Phase | Seam | Nature of the change |
| --- | --- | --- |
| 1 | `query_processor._legacy_intent_and_format` + `_prepare` | stop collapsing `clarification_needed` onto chitchat; return a clarification result instead of an answer |
| 1 | `ProcessedQuery` | additive fields (`clarification`, `temporal_intent`); dataclass with defaults, so every existing construction site keeps working |
| 1 | conversation state | **derived from `history`**, not a new store — see §6 |
| 2 | `structured/resolve.plausible`, `catalog/queries`, `understanding/catalog_prompt` | reuse the existing catalog-derived option machinery for content questions |
| 3 | `answer_plan.extract_requirements` | already computed; promote requirements to routed sub-queries instead of re-decomposing |
| 3 | `retriever.retrieve` multi-query leg | replace paraphrase generation with retrieval *perspectives*; keep RRF and the existing gates |
| 4 | `retriever._gate_temporal`, `reranker._sort_key` | widen the gate beyond UPCOMING; add a temporal-fit band |
| 5 | `context/builder._flag_conflicts`, `retrieval/graph/templates` | surface claim status/supersession into block payloads |
| 6 | `reranker.rerank` | one band order that carries all signals |
| 7 | `tests/retrieval/`, `tests/pipeline/` | regression suite |

### Layering

The new modules live inside `app/retrieval/` (layer 5) and are integrated from
`app/pipeline/` (layer 7). **No new top-level package**, so `LAYERS` in
`tests/test_architecture.py` needs no entry and no new deferred-upward exception
is created. This is deliberate: adding a top-level package would require an
architecture-test change on day one.

---

## 6. Conversation state without a session store

The clarification loop needs to remember: the original query, the question
asked, the user's answer, and how many rounds have been spent. The server has no
session. Three options were considered:

1. **New server-side session store** (Redis/MySQL) — rejected: it introduces
   state, expiry and identity concerns into a deployment that is deliberately
   stateless, for one feature.
2. **New request/response fields** — a `clarification` object on `QueryRequest`
   echoed by the client. Clean, but requires a client change before the feature
   works at all.
3. **Derive state from `history`** — the assistant's clarification turn is
   *already* in the history the client echoes back. Marking it machine-readably
   makes the history itself the state carrier.

**Chosen: (3), with (2) as an additive optional enhancement.** A clarification
turn is emitted with a marker; on the next turn, if the previous assistant turn
was a clarification, the current user turn is read as the answer to it, the two
are merged into one normalized query, and **clarification is not offered again
for that thread** — which is also the loop guard. Zero client changes required;
richer clients may still consume the structured `clarification` SSE event.

---

## 7. Baseline

- Branch cut from a clean tree; no uncommitted work was at risk.
- Test suite: recorded in the Phase 0 commit message.
- Flag convention observed: every substantial read-path feature in this repo
  ships behind a setting that defaults **off** (`multi_query_enabled`,
  `keyword_leg_enabled`, `corrective_loop_enabled`, `graph_retrieval_enabled`,
  `entity_resolution_enabled`). The new layer follows the same convention.

---

# Re-scoped plan (supersedes §5's phase table)

Phase 0 established that the seven-phase plan was written against a system
simpler than this one. Three of its phases are substantially already built, and
one of them cannot be built further without either changing ingestion or
guessing. What follows replaces it.

## 8. Corrected status — the things that surprised us

Four capabilities the original plan lists as work are already in production:

| Capability | Where | Consequence for the plan |
| --- | --- | --- |
| **Superseded versions never retrieved** | `search/hybrid_search.build_filter` makes `is_current == True` a *mandatory* condition on every search | Goal 12 is done at the document-version level. Nothing to add |
| **Point-in-time / date-range filtering** | `understanding/filters.date_conditions` — precision-aware interval overlap: closed periods end at `effective_end_date`, open-ended bundles run to the present, points end at their own stated precision | Goals 8's `point_in_time` and `date_range` already work whenever understanding extracts the dates |
| **Historical preservation** | `knowledge/claims/conflicts.py` ("a conflict changes *status*, never existence"); `graph/templates._overlap` imposes no minimum date | Goal 13 done. A 1996-1999 relationship is as retrievable as last year's |
| **Version visible to the model** | `doc_version` reaches the prompt via `generation/prompts.py:590` | Goal 9's version signal is already surfaced |

And the payload already carries every field the remaining work needs —
`effective_start_date`, `effective_end_date`, `start_precision`, `end_precision`,
`is_current`, `doc_version`, `bundle`, `source_type`. **No ingestion change is
required by any phase below.**

## 9. The real gaps

| Gap | Goals | Nature |
| --- | --- | --- |
| **A. Clarification is computed, then thrown away** | 2, 3, 4, 14 | `clarification_needed` is a first-class intent label that `_legacy_intent_and_format` collapses onto `chitchat`, so an ambiguous question gets small talk. No state carrier, no loop guard. Data-driven options machinery already exists but serves only ambiguous entity *names* | 
| **B. Temporal intent is classified, then thrown away** | 8, 9, 10, 12 | `temporal_gate.detect_mode` returns six modes; `retriever._gate_temporal` acts on one. `current`/`latest`/`historical` never influence ranking |
| **C. Sub-queries are never separately retrieved or routed** | 5, 7 | `answer_plan.extract_requirements` already splits the question into separately-answerable parts, and they already run in parallel with retrieval — but one retrieval serves all of them, and none is routed by part |
| **D. Multi-query generates paraphrases, not perspectives** | 6 | `strategies.paraphrases` produces the literal rewordings the spec rules out; off by default |
| **E. No evaluation harness for any of this** | 7 (phase) | The 86-question benchmark is referenced throughout the code's docstrings but there is no regression suite for query-intelligence behaviour |

## 10. Phases

Five phases, ordered by value and by how self-contained they are. Each ships
behind its own setting defaulting **off**, per §7.

| # | Phase | Gap | Touches |
| --- | --- | --- | --- |
| **A** | **Clarification + conversation state** | A | `understanding/clarify.py` (new), `query_processor` (stop discarding the label; additive `ProcessedQuery` fields), `pipeline/query_pipeline` (emit a clarification result), `schemas/query` (additive) |
| **B** | **Temporal intent wiring** | B | `understanding/query_processor` (promote `temporal_intent` to the normalized query), `retriever._gate_temporal` (widen past UPCOMING), `search/reranker` (a temporal-fit band, cut *inside* the relevance band) |
| **C** | **Sub-query planning + per-part routing** | C | `retrieval/planning/` (new subpackage), reusing `extract_requirements`; routes parts across the three existing legs and de-duplicates |
| **D** | **Retrieval perspectives** | D | `search/strategies.paraphrases` → perspective generation; RRF, gating and every other leg unchanged |
| **E** | **Evaluation harness** | E | `tests/retrieval/`, `tests/pipeline/` — the fourteen scenarios the brief names, including malformed LLM output and store failures |

### What is deliberately NOT a phase

The original **Phase 5 (conflict / version / supersession)** is dropped as a
code phase. Document-version supersession is already enforced as a hard filter
(§8), and claim-level supersession already has a complete mechanical ladder in
`knowledge/claims/conflicts.py`.

What remains is *document-level competing-fact detection*, and
`context/builder._conflicting` documents why it stops where it does: two
attachments under one Drupal node are indistinguishable from two editions of one
publication, because they share a node, a title and an `effective_start_date`.
The largest such nodes carry 69 financial statements, 68 announcements and 43
brochures. Treating that relationship as disagreement previously flagged about a
quarter of all answers, mostly wrongly.

Separating the two shapes needs **a content signal and a threshold measured
against a labelled set** — or a `supersedes` relation written at ingest time.
Both are out of bounds: the first is measurement, not code; the second changes
ingestion. **This is reported as a stop condition rather than implemented.**

### Ranking invariant to protect

`reranker.py` ranks relevance band → authority band → substance band → recency.
That ordering is the thing that already satisfies goal 10, and it was arrived at
by measurement documented in the module. Phase B adds temporal fit *inside* the
relevance band, never above it — so a newer document still cannot outrank a more
relevant older one. Phase E pins this with a test.

---

# Phase A — clarification + conversation state (implemented)

Setting: `clarification_enabled` (default **false**). With it false, every path
below is byte-identical to before: `clarify.pending` is never called, the merged
question *is* the question, and `ProcessedQuery.clarification` stays `None`, so
the pipeline's guard is unreachable.

## What changed

| File | Change |
| --- | --- |
| `app/retrieval/understanding/clarify.py` | **New.** The decision, the options, the marker, the merge, the guard |
| `app/retrieval/understanding/query_processor.py` | `ProcessedQuery` gains `clarification` and `clarified_from` (both defaulted); `process` merges an open clarification before understanding and decides afterwards |
| `app/pipeline/query_pipeline.py` | `_clarification_result`; a guard in `_prepare` ahead of the chitchat branch; an additive `clarification` key on the `sources` SSE event |
| `app/config.py` | `clarification_enabled: bool = False` |

## The protocol

```
turn 1   user: "show me the projects"
         understanding -> [clarification_needed]
         answer text:  Which of these did you mean?
                       1. Completed Projects
                       2. Ongoing Projects
                       Reply with your answer and I will take it from there.   <- MARKER

turn 2   client echoes that turn back inside `history`
         user: "Ongoing Projects"
         pending(history) -> "show me the projects"          (marker matched)
         merge(...)       -> "show me the projects: Ongoing Projects"
         clarification decision skipped  -> one round, always
         ... existing understanding -> routing -> retrieval -> generation
```

## Why the marker is a readable sentence

The UI escapes HTML before rendering (`ui/script.js`: `renderMarkdown` →
`escapeHtml`) and writes raw text into the bubble while tokens stream. An HTML
comment or a sentinel token would therefore be **visible** — first mid-stream,
then permanently. A marker the reader can understand is the only kind this
transport allows.

The cost is that changing `MARKER`'s wording breaks state for conversations
already in flight. Those degrade rather than fail: the follow-up is answered on
its own instead of being merged.

## Why `is_ambiguous` is not a trigger

`_is_ambiguous` is a near-tie between *content intents* (qa vs database). That is
a routing question retrieval can settle, not evidence the user was unclear.
Clarifying on it would interrupt questions that answer perfectly well, so the
only trigger is the explicit `clarification_needed` label. Pinned by
`test_a_near_tie_between_content_intents_is_not_a_clarification`.

## One correction to §3

The Phase 0 table said an ambiguous question "gets small talk". That is true only
for turns that read like neither small talk nor a question ("what about that
one?", "show me a table"). `_corrected_intent` already rescues a chitchat verdict
whenever the wording reads as an information request, so "show me performance"
reached retrieval before this phase. Phase A reads the *understanding* rather
than the route it produced, so it covers both shapes.

---

# Phase B — temporal intent wiring (implemented)

Setting: `temporal_intent_enabled` (default **false**). Read in exactly one
place — `query_pipeline._temporal_intent` — which hands `retrieve` either the
question's `TemporalIntent` or `None`. With `None`, ranking and the pre-existing
`UPCOMING` gate are byte-identical to Phase A.

## What changed

| File | Change |
| --- | --- |
| `app/retrieval/search/temporal_gate.py` | `TemporalIntent`, `temporal_fit`, `gate_past`; `gate_upcoming` refactored onto a shared body |
| `app/retrieval/search/reranker.py` | A temporal-fit band, cut inside the relevance band and above authority |
| `app/retrieval/retriever.py` | `retrieve(temporal=...)`; `_gate_temporal` selects a gate by mode |
| `app/retrieval/search/strategies.py` | `corrective_requery` threads `temporal` into its rerank |
| `app/retrieval/understanding/query_processor.py` | `ProcessedQuery.temporal_intent`, promoted in `process` |
| `app/pipeline/query_pipeline.py` | `_temporal_intent` — the single flag read |
| `app/config.py` | `temporal_intent_enabled: bool = False` |

## The three layers, and what each is allowed to do

| Layer | Mechanism | Modes |
| --- | --- | --- |
| **Filter** (unchanged) | `filters.date_conditions` → precision-aware Qdrant overlap conditions, applied before the search | any question whose window understanding extracted |
| **Gate** | `temporal_gate.gate_upcoming` / `gate_past`, removal-only, scheduled bundles only, never empties the context | `UPCOMING`, `PAST` |
| **Rank** | `temporal_fit` → a band cut *inside* the relevance band | `CURRENT`, `PAST`, `POINT_IN_TIME`, `DATE_RANGE` |

Nothing in Phase B filters by date. Point-in-time and date-range questions keep
using `date_conditions` exactly as before; the ranking signal scores *degree of
fit* among the candidates that filter already admitted, which is the part it
could not express.

## The ranking invariant

```
relevance → temporal fit → authority → substance → recency
```

Temporal fit is **inside** the relevance band. A candidate a relevance band
lower cannot climb past one above it however perfectly it fits the period, so a
newer-but-less-relevant document still loses — pinned by
`test_a_newer_but_less_relevant_document_still_loses` and, exhaustively across
all five modes, by `test_temporal_fit_never_overrules_relevance_across_bands`.

It sits *above* authority because when a question names a time, a passage from
that time answers better than a more canonical passage from a different one: a
2019 hub page does not answer "what happened in 2023" better than a 2023 news
item does.

## Why "current" is not "newest"

`temporal_fit` under `CURRENT` scores **validity**, not publication date:

* an `ongoing_projects` node started in 2005 and still open → `FIT_MATCH`
* a news item published last January, whose period closed the same day → `FIT_MISS`

That is the opposite ordering to a freshness score, and it is the requirement.
Open-endedness is read from `OPEN_ENDED_BUNDLES`, the same reading
`date_conditions` already applies to its lower bound.

## Unknown is never a miss

`FIT_UNKNOWN` (0.5) sits between match and miss, and every case where the answer
is not known returns it: no temporal intent, an intent whose window was never
extracted, a document with no date, malformed metadata, or an exception while
scoring. Scoring an unknown as a miss would quietly demote every document whose
date ingestion could not recover.

## Known limitations

* **`doc_version` is not a ranking signal.** It reaches the prompt already, and
  `is_current == True` is a mandatory pre-filter in `build_filter`, so every
  candidate that reaches ranking is the current version and the field carries no
  ordering information. `is_current` *is* read by `temporal_fit`, as defence in
  depth, for the case where a superseded chunk reaches ranking anyway.
* **Latest vs current are the same mode.** `detect_mode` maps "latest" into
  `CURRENT` by its existing lexicon. The distinction the brief draws — "current"
  = valid now, "latest" = most recent applicable — is served by the band order
  rather than by two modes: fit decides validity, and recency (already the last
  key) settles the remainder. Splitting them would mean changing `_PATTERNS`,
  which is the classifier Phase B was told to reuse rather than replace.
* **A window is only as good as the extraction.** When understanding returns no
  dates, a `POINT_IN_TIME`/`DATE_RANGE` question scores neutral everywhere and
  ranking is unchanged. Guessing a window here would be the second temporal
  classifier this phase must not add.
