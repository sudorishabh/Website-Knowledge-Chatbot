# 04 — Search and Fusion

**Purpose.** Turn a search query (plus its facet filters) into an ordered set
of candidate chunks: one or more Qdrant pulls, each optional recall-expansion
leg run in parallel, and reciprocal-rank fusion to merge them into a single
ranking before reranking ever runs.

**Inputs.** `search_query`, the facet filter list from query understanding
(doc 03), a query embedding, and a handful of settings-gated switches
deciding which legs run at all.

**Outputs.** A `list[Candidate]`, fused and ordered, ready for
`app.retrieval.search.reranker.rerank` (doc 05).

**Components.** `app/retrieval/search/hybrid_search.py` (the primitive),
`fusion.py` (RRF), `strategies.py` (recall-expansion legs), `title_leg.py`
(title-anchored retrieval), `scoped_retrieval.py` (id-scoped reads). Orchestrated
by `app/retrieval/retriever.py::retrieve`.

This is deliberately **one package rather than "fetch" and "rank" split
apart**: `hybrid_search.Candidate` is the type every module here passes
around, and splitting them would put that type on one side of a boundary and
half its users on the other — a cycle by construction.

---

## The primitive: `Candidate` and `search()`

Every leg in this document — the base pull, the keyword leg, the title leg,
the corrective requery — is a call to `hybrid_search.search()`, which does
exactly one thing: embed (or accept a precomputed vector), apply the
mandatory filter, run one `query_points`, and return `Candidate`s.

### Three scores that must not be conflated

```python
@dataclass
class Candidate:
    id: str
    score: float           # current ranking value — whatever stage produced it
    semantic_score: float  # raw semantic relevance, fixed at search time
    fusion_score: float    # RRF value, ~0.016-0.033 scale; 0.0 when no fusion ran
    payload: dict
    vector: list[float]
```

- **`score`** is *ordering only* — it means "dense cosine" right after
  `search()`, "fused RRF value" after `fusion.rrf`, and "banded relevance"
  after `reranker.rerank` (doc 05). Never compare it to a configured
  threshold.
- **`semantic_score`** is the value every configured floor is actually
  calibrated against (`rerank_score_threshold`,
  `corrective_min_score`, `rerank_score_threshold`). Set once, in `_to_candidate`,
  the moment a candidate is born from a real Qdrant hit, and preserved through
  every later stage.
- **`fusion_score`** is the RRF value alone, kept apart because it lives on a
  completely different scale from cosine similarity.

This three-way split exists to fix a real defect: `rrf` used to overwrite
`score` and the floors read `score`, so enabling the keyword or multi-query leg
put every fused candidate an order of magnitude below the cosine-scaled
floors, emptying the website group through an admission floor since retired.
`tests/retrieval/search/test_fusion_score_integrity.py`
pins the separation.

### The mandatory filter

`build_filter()` is applied by `search()` on **every** call, with no way to
turn it off from a leg — only `exclude_non_searchable` is a caller option, and
even that defaults on:

| Condition | Why |
| --- | --- |
| `is_parent == False` | Parents hold no vector of their own; only children are searchable |
| `is_current == True` | Superseded versions are not the answer |
| `section_type not in (toc, references, glossary)` *(default)* | These extract cleanly but pollute retrieval — a table of contents ranks on the same words as its subject |

There is no tenant or ACL leg here at all — the corpus is public and every
caller may see all of it (see [ingestion doc 08](../ingestion/08-persistence-and-catalog.md#what-ingestion-does-not-hand-off)
for the write-side statement of the same fact).

`exclude_non_searchable=False` exists for exactly one caller,
`scoped_retrieval.lead_parents`: a fetch that must return *something* to
represent a document — even its table of contents — is a different need than
finding the best thing to search, and only that caller turns the exclusion
off.

### Collection existence is checked once, not per query

`_verified_collections` is a process-wide set. A missing collection is a
bootstrap/deployment error, not a per-query concern, so the check runs once and
every steady-state search costs exactly one `query_points` call — no extra
round trip to ask Qdrant whether the collection exists first.

---

## Fusing rankings: reciprocal-rank fusion (`fusion.py`)

```python
score = Σ 1/(k + rank)   # k = 60
```

`rrf` merges any number of independently-ranked lists by **id**, using only
each list's rank — never its raw score. That is what lets it fuse lists whose
raw scores are not comparable at all: dense cosine similarity from the base
pull and a full-text match score from the keyword leg sit on unrelated scales,
but "3rd in this list" and "3rd in that list" combine cleanly.

The candidate object itself — payload, vector — is kept from its **first
sighting** across the input rankings; only the score is recomputed. Ties break
on id, for determinism. `semantic_score` is deliberately left untouched by
this function (see above) — only `fusion_score` and, through it, `score` move.

---

## The base pull: plain, or website-biased dual

`retriever.retrieve` chooses between two shapes for the primary pull:

```python
dual = prefer_website_enabled and not source_type and answer_format != "table"
```

**`dual_search`** runs two pulls sharing one query vector — one filtered to
`source_type == website` (`website_candidate_k` results), one filtered to
*not* website (`retrieval_candidate_k` results) — and returns their
concatenation (not yet fused; that happens later against the recall-expansion
legs too). The reason is corpus composition: PDFs numerically dominate the
collection, so an unweighted pull under-represents website pages even when
they are the better answer.

This is a **recall** guarantee and nothing more. Both halves land in one list,
are ranked together, and are admitted together: source kind survives only as one
term of the authority band (see [05](05-ranking-and-temporal-gating.md)), which
sits below relevance, temporal fit and — on a question about the present —
recency. It used to mean more, and [06](06-context-and-citations.md) records
what. See the archived `redundant/docs/website-preference-retrieval.md` for the
measurement behind it.

Dual is skipped whenever the caller already pinned a `source_type` (honouring
an explicit filter with one pull, since the "not website" half would
contradict it) or the answer format is `table` (tables live in PDFs; forcing a
website-biased pull would fight the format instead of serving it).

---

## Recall-expansion legs (`strategies.py`)

Each of these is an *optional*, independently-gated extra pull, fused back in
by RRF — never a replacement for the base pull, and never able to make the
result set smaller than the base pull alone would produce.

### Multi-query paraphrasing

Gated by all of:

```python
multi_query_enabled and content_search and not source_type and not filters
and len(search_query.split()) >= 5
```

`content_search` means the query understanding capability set is empty (the
degraded passthrough, treated as qa) or intersects `{qa, comparison}` — a
`database` lookup gets nothing from paraphrasing. No `source_type` and no
`filters` because an already-narrowed pull has less to gain from wording
diversity. The five-word floor exists because short factoids are already
unambiguous; paraphrasing them mostly produces noise.

`paraphrases(query, n)` asks the LLM (temperature 0.7, for diversity — not the
pinned parsing temperature used elsewhere) for `n` alternative phrasings that
could retrieve passages a literal match misses, filters out anything that
collapses back to the original query case-insensitively, and returns `[]` on
any failure. Each surviving paraphrase gets its own `paraphrase_search` pull
(own cached embedding, own `multi_query_leg` trace stage), run concurrently
with everything else in the `ThreadPoolExecutor` in `retriever.retrieve`.

### The keyword leg: precise terms, then content terms

Two independent term extractors feed **two separate** keyword pulls, because
merging them into one OR-ed set would be the wrong fix for the reason each
exists:

**`extract_key_terms`** — precise, pattern-first: quoted phrases, capitalised
bigrams, alphanumeric tokens (`PM2.5`, `CO2`), acronyms (optionally with a
qualifying number: `SDG 7`, `COP26`), years. Falls back to lowercase content
words (3-char floor, not 4 — "air" and "gas" name whole domains here) **only**
when none of the precise patterns matched at all, because a query like "life
cycle analysis of transport modes" names its subject exactly without
capitalising any of it, and skipping the leg there was measured as the single
biggest hole in the lexical path. A shorter term that is merely the prefix of
another kept term is dropped (`PM2` when `PM2.5` is already kept) — the
patterns overlap by design and the shorter form is never more selective.

**`extract_content_terms`** — unconditionally the lowercase-content-word list,
no pattern-precision pass at all. It exists because the organisation's own
acronym matches nearly every question asked of it: measured across the
86-question benchmark, every organisational question extracted exactly
`['TERI']` from `extract_key_terms`, which then — OR-ed — matches nearly the
whole corpus and contributes nothing the dense pull did not already have.
Pulling the topical content words ("initiatives", "centres", "excellence") as
their *own* ranking, fused separately, lets that small on-subject set surface
through RRF instead of being diluted inside a ubiquitous-term match. The
caller (`retriever.retrieve`) skips this leg outright when its term set is a
subset of the precise leg's — no point running the same filter twice.

`keyword_search(terms, ...)` builds `Filter(should=[MatchText(term) for term
in terms])` — **OR**, not AND. A single multi-word `MatchText` is an AND
across its words, which was measured brittle for exactly the case the leg
exists for: "Emission Inventorisation for Faridabad Town" matched nothing,
because no single chunk held all four words. OR degrades gracefully toward the
dense pull instead of collapsing to zero, and the ranking *within* the matched
set is still ordinary dense similarity — this leg only restricts the
candidate pool, it does not introduce its own scoring. It fails open to `[]`
— notably while the `chunk_text` full-text index does not yet exist
(`scripts/create_fulltext_index.py`).

### One-shot corrective requery

`corrective_requery` is called from `retriever.retrieve` **after** the first
rerank, only when the top result's `semantic_score` is below
`corrective_min_score` (doc 05 covers reranking itself; this is the recall
side of that gate). `corrective_query` asks the LLM for a single reformulation
"targeting the missing information" — shown the top 3 candidates' text as
context for what is missing — and returns `None` on failure or an echo of the
original query. If it produces something new, one extra `search()` pull runs,
is RRF-fused with the existing ranking, and reranked once more. **Strictly one
iteration**: there is no loop here, and any failure (LLM error, a fused set
identical to what was already there) simply keeps the original ranking rather
than retrying again.

---

## The title-anchored leg (`title_leg.py`)

Dense retrieval ranks by how a *passage* reads, and an organisation's own
canonical pages are frequently its worst-reading passages: a "Centres of
Excellence" hub page whose text is nine repetitions of a link label and "Read
More" embeds nowhere near a question that names the concept in prose. Measured
on the 86-question benchmark: nine questions retrieved none of their
authoritative sources this way, and the hub page was outside the top 40
candidates entirely. Reranking cannot fix this (it only reorders what was
retrieved) and the keyword leg cannot either (its terms are OR-ed, so the
organisation's own name — present in 23% of chunks — dilutes any real match
back into the corpus).

What survives is the **title**: short, curated, stored in MySQL, matchable by
word rather than by embedding. `title_candidates(question)` resolves
*documents* by title-word overlap, and `title_search` then pulls their chunks
from Qdrant by id (`scoped_retrieval.search_within_documents`) and hands them
back as one more ranking for RRF — it never replaces the base pull, only adds
a signal RRF is free to discount.

It takes none of the question's facets — a title match is meant to reach past
a wrong facet guess — except its period (`date_scope`, from
`filters.date_conditions`), which every leg keeps. Without it, "WSDS events in
2026" matched a 2021 curtain raiser by title, the only candidate of the pull,
and that one hit kept the facet-miss retry below from firing.

### Why two words, and computed rarity rather than a stopword list

A bare word-overlap match would let the organisation's own name in every
title win, or let "research" — 1.5% of titles, but a whole genre, not a page
— beat a genuinely rare title word. Four guards, the first three computed
per-query against the live title table rather than configured once:

1. **≥2 matched title words**, or exactly one — but only when that one word is
   long (`_DISTINCTIVE_MIN_LEN = 6`), the title itself is short (≤6 words), and
   the word is *rare* across the whole title catalogue (≤1% of titles,
   `_MAX_SINGLE_HIT_SHARE`, floored at `_MIN_DF_CEILING = 25` titles so a small
   catalogue does not make every word look ubiquitous by percentage alone).
2. **Selective terms** (`_selective_terms`) — query terms occurring in ≤10% of
   titles (`_MAX_TITLE_SHARE`) are the only ones allowed to count at all;
   dropping the rest is what stops "which title mentions the organisation
   most" from being the effective ranking.
3. **Word-level matching, not substring** — "vision" inside "Visionary"
   previously outranked "Mission and Goals" for a mission/vision question.
4. **A word that names a kind of document never carries a match alone**
   (`_names_a_kind`, applied in `_rare_terms`), however rare it is in the
   titles. "articles" is in one title of 8,631 — the "Articles & Publications"
   index — so by frequency it identified that page exactly, and *"list the
   articles where IPCC is mentioned"* retrieved the index's link labels in
   place of anything about IPCC; "reports" alone matched 37 short titles
   carrying "Report", "projects" 39, "events" 8. The vocabulary
   (`_kind_words`) is not a list kept here: it is the bundle names, display
   labels and collective words of every type the glossary describes, through
   the same `topic.bundle_words` the structured layer uses, imported lazily
   and failing open to the frequency-only rule. Two-word agreement is
   unaffected — "annual reports", "articles publications" and "papers
   discussing Article 6" still resolve their pages.

Singular/plural is the one inflection crossed deliberately (`centres` ↔
`centre`, `reports` ↔ `report`), because pages are named one way and asked
about the other.

Restricted to **website nodes only** — an attachment inherits its parent
node's title, so matching titles across attachments would return the same
document many times. The title table itself is cached 300s
(`_TITLE_TTL_SECONDS`), since titles change only when the CMS does and the leg
must not scan the whole title table (thousands of rows) on every question.

---

## Id-scoped reads (`scoped_retrieval.py`)

Inverts the usual flow for callers where **MySQL already knows the id set**
and Qdrant only needs to rank or fetch content inside it: the annual-report
edition scope (doc 03), the title leg above, attachment supplementation (doc
06), and scoped summarisation.

**`search_within_documents(query_vector, document_ids, limit)`** is a dense
search with one extra `MatchAny(document_id)` condition — everything else
about it is ordinary `hybrid_search.search()`. `_MAX_IDS = 150` caps the id set
before it reaches Qdrant regardless of what the caller passed, as a hard
safety limit independent of whatever cap the caller applied upstream.

**`lead_parents(document_ids)`** answers a different question — "the single
best block to represent this document", for summarisation — by finding each
document's earliest *usable* child chunk and hopping to its
`parent_chunk_id`. "Usable" matters because the mandatory filter excludes
toc/references/glossary chunks, so a report whose first chunk is its table of
contents used to match nothing under `chunk_index == 0` and silently vanish
from the caller's scope. Three escalating strategies fix that while keeping
the common case at one point per document:

| Strategy | Scans | `exclude_non_searchable` |
| --- | --- | --- |
| 1 | `chunk_index == 0` | on |
| 2 | `chunk_index` 1..4 | on |
| 3 | `chunk_index == 0` | **off** |

Only documents still missing a lead after a strategy runs go on to the next
one, so a normal document (front matter of one chunk or none) costs exactly
one scroll. Strategy 3 exists for a document that is *entirely* front
matter — at that point returning the opening chunk regardless is better than
representing the document with nothing. Single-child documents (no
`parent_chunk_id` on their lead) fall back to the child payload itself.

---

## How `retriever.retrieve` assembles all of this

The base pull, and every gated leg that is active, run **concurrently** in one
`ThreadPoolExecutor(max_workers=4)`:

```
base_search (dual or plain)     ┐
keyword_search(precise terms)   │  all submitted together;
keyword_search(content terms)   │  paraphrase generation + its searches
title_search                    │  overlap the base pull's wall-clock
paraphrase_search × N           ┘
```

Once every future resolves, everything except the base pull is RRF-fused
against it (`rrf([base] + rankings)`); if no optional leg ran at all, the base
pull's own order stands untouched — fusion is skipped rather than run
pointlessly on a single list.

### The facet-miss retry

Covered from the filter side in [03 — Query Understanding](03-query-understanding.md#the-date-filter-retry-contract);
the search-side half is here. If the fused candidate set comes back **empty**
and facets were applied, `retrieve` retries once, keeping only the date
condition (`filters.date_conditions`) and dropping theme/tags/source_type/etc:

```python
kept = date_conditions(filters)
if not candidates and filters and len(kept) < len(filters):
    candidates = _base_search(kept or None, use_dual=relaxed_dual)
```

This is precision-preserving by construction — it only fires on a **total**
miss, so a non-empty facet-scoped result is left exactly as it was. It never
runs at all when every filter present was already a date condition (retrying
the same query that just came back empty would waste a round trip for
nothing). The relaxation is recorded on the `rag.search_relaxed` span and in
the retrieval log, never surfaced in the answer text — the widening is
silent, which is exactly why the date condition is the one thing not dropped
even here.

A theme no document carries never reaches this retry: `_theme_condition`
drops it up front, since the theme vocabulary is the names documents carry and
a name none carries is a word the model put in the slot ("WSDS"). It used to
become a filter matching nothing, which emptied every filtered leg.

---

## Validation performed at this stage

| Check | Where | On failure |
| --- | --- | --- |
| Collection exists | `_collection_ready` | `[]`, warning logged |
| Every candidate satisfies the mandatory filter | `build_filter`, applied server-side by Qdrant | Excluded from the result at the source |
| A paraphrase differs from the original query | `paraphrases` | Dropped |
| A corrective reformulation differs from the original and adds a new id | `corrective_requery` | Falls back to the original ranking |
| Keyword/content terms actually narrow the pull | `extract_content_terms` vs `extract_key_terms` overlap check (caller) | Content-term leg skipped when redundant |
| Title match clears the two-guard bar | `_score` | Document not returned as a title candidate |
| Id set bounded before reaching Qdrant | `_MAX_IDS` in `scoped_retrieval` | Truncated to 150 |
| Fused set is non-empty before reranking (facet case) | `retrieve`'s post-search check | Retry without non-date facets |

## Failure scenarios

| Scenario | Detection | Response | Recovery |
| --- | --- | --- | --- |
| Qdrant collection missing | `client.collection_exists` false | `[]`, no exception | Run `scripts/create_payload_indexes.py` / re-index |
| Paraphrase / corrective LLM call fails | `except` in `strategies.py` | `[]` / `None`; that leg contributes nothing | Next query retries independently |
| `chunk_text` full-text index absent | `except` in `keyword_search` | `[]`, debug log; dense-only | `scripts/create_fulltext_index.py` |
| Title table unreadable | `except` in `title_candidates` | `[]`, warning; leg skipped | Next call after DB recovers |
| Facet filters match nothing | Empty candidate set post-fusion | One retry, date scope kept | If still empty, the refusal path downstream is correct |
| Attachment / scoped lookup fails | `except` around `scoped_retrieval` calls | Original blocks/candidates kept | — |
| Parent retrieve fails in `lead_parents` | `except` around the batched `retrieve` call | Falls back to child payloads | — |

## Observability

- Spans: `rag.embed_query`, `rag.search` (with `candidates` count and the
  per-leg boolean map), `rag.multi_query` (paraphrase count),
  `rag.keyword_leg` / `rag.content_term_leg` / `rag.title_leg` (hit counts),
  `rag.search_relaxed` (facet-retry outcome), `rag.corrective`
  (score before/after, whether it improved).
- `retrieval_log.note(search_query=..., candidates=..., legs={...})` — the one
  place that records which legs actually ran and what the fused set came to,
  since no single Qdrant event can state that.
- `logger.info("Facet filters matched no chunks; retried without facets%s (%d candidates).")`
- `logger.info("corrective loop: top score %.4f -> %.4f (%s)")`.
- Every `search()` call is traced individually via
  `retrieval_log.qdrant_call("vector_search", stage=trace_stage, ...)` — the
  `trace_stage` argument (`website_pull`, `not_website_pull`, `multi_query_leg`,
  `keyword_leg`, `content_term_leg`, `title_leg`, `corrective_pull`,
  `scoped_pull`, `attachment_pull`, `lead_child_scroll`) is what lets a
  retrieval-log trace (`docs/retrieval-logging.md`) tell the legs apart; it has
  no effect on retrieval itself.

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `retrieval_candidate_k` | — | Per-leg candidate pull size (base, keyword, content-term, title, multi-query, corrective) |
| `website_candidate_k` | — | Website-half pull size under dual search |
| `prefer_website_enabled` | — | Enables the website-biased dual pull |
| `multi_query_enabled` | — | Enables paraphrase-based recall expansion |
| `multi_query_paraphrases` | — | Paraphrases requested per query |
| `keyword_leg_enabled` | — | Enables both the precise and content-term keyword legs |
| `corrective_loop_enabled` | — | Enables the one-shot corrective requery |
| `corrective_min_score` | — | `semantic_score` floor below which the corrective loop fires (checked against the post-rerank top result) |
| `retrieval_top_k` | — | Final context block count (`n` in `retrieve`) |

## Hand-off

The fused candidate list goes to `app.retrieval.search.reranker.rerank`, which
applies authority/recency/substance bands and (for `answer_format="table"`)
the table boost — see 05 — Ranking and Temporal Gating. From there,
`context.builder.build_context` (doc 06) decides which reranked candidates are
actually admitted into the LLM's context.

---

Previous: [03 — Query Understanding](03-query-understanding.md) · Next: [05 — Ranking and Temporal Gating](05-ranking-and-temporal-gating.md)
