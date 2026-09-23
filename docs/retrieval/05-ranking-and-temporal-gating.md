# 05 — Ranking and Temporal Gating

**Purpose.** Take the candidates search fused into one list and decide the order
the LLM actually reads them in — then, for the one class of question where order
is not enough, remove the candidates that are simply wrong.

**Inputs.** A query string and a `Sequence[Candidate]` from search/fusion.

**Outputs.** A ranked `list[Candidate]` (reranking), and — later in the request,
after context is built — a `list[ContextBlock]` with stale event entries removed
(temporal gating).

**Components.** `app/retrieval/search/reranker.py`, `app/retrieval/search/volatility.py`,
`app/retrieval/search/temporal_gate.py`.

---

## Three scores, one candidate

Every `Candidate` (`app/retrieval/search/hybrid_search.py`) carries three scores
that this stage is careful never to conflate:

| Field | What it holds |
| --- | --- |
| `score` | The *current ranking* value — dense similarity out of Qdrant, then the RRF-fused value, then the banded relevance this stage produces. Ordering only. |
| `semantic_score` | The *raw* semantic relevance, on the active scorer's own scale. Set once at search time and carried through fusion untouched, because every configured floor (`corrective_min_score`, `rerank_score_threshold`) is calibrated against it. |
| `fusion_score` | The reciprocal-rank value from `fusion.rrf`, `0.0` when no fusion ran. Carried through reranking unchanged, so a ranking can still be traced back to how it was fused. |

Keeping these apart fixed a real defect: `rrf` used to overwrite `score`, and the
floors read `score` — so turning on the keyword or multi-query leg put every
candidate an order of magnitude below the cosine-scaled floors, emptying the
website group through an admission floor since retired and pinning the
corrective loop open through one that has not. See [04](04-search-and-fusion.md)
for how a
`Candidate` is built and fused; this document starts from the fused list.

---

## Why banding, not a weighted blend

Ranking used to be a weighted blend of a min-max-normalized semantic score,
recency and authority. A blend gets this backwards in the case that matters:
because the semantic scores are normalized first, it separates candidates most
aggressively exactly when their scores are closest together — when the relevance
difference means least — while a recency weight small enough not to overrule a
genuinely better passage is also too small to break the ties it exists for.

So candidates are **banded**: grouped into tiers by "similarly relevant, similarly
authoritative, similarly complete", and only ranked on the next key once two
candidates land in the same tier on every key before it. A candidate a band lower
never climbs past one above it, however new, authoritative or complete it is —
whatever crosses a band boundary is, by construction, a difference the system
considers *material*; whatever stays inside one is noise the lower-priority keys
are free to break ties on.

### The banding algorithm

```python
def _bands(values, *, tolerance):
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=True)
    band = 0
    leader = values[order[0]]
    for i in order:
        if leader - values[i] > tolerance:
            band += 1
            leader = values[i]
        bands[i] = band
    return bands
```

A band starts at its leader and holds every value within `tolerance` of it; the
first value further than that opens the next band, becoming the new leader.
Grown greedily down the sorted order rather than cut into fixed-width buckets, so
two near-identical values can never land on either side of an arbitrary boundary
— and measured against the **leader**, not the previous value, so a long chain of
small steps cannot drift an arbitrarily weak value into the top band.

Authority and completeness bands are cut a second time, *inside* each band
above them (`_nested_bands`): two candidates only compete on completeness once
they are the same kind of source, otherwise a long attachment would set the
boundary that splits two canonical pages apart.

---

## The priority

```python
def _sort_key(r):
    return (r.relevance_band, r.temporal_band, r.recency_band,
            r.authority_band, r.substance_band,
            -r.scored.recency, -r.scored.relevance)
```

| Priority | Signal | Cut within | Tolerance |
| --- | --- | --- | --- |
| 1 | **Relevance** | the whole candidate set | `rerank_relevance_tolerance` (widened for volatile queries — see below) |
| 2 | **Temporal fit** | the relevance band | `_TEMPORAL_TOLERANCE = 0.10` (fixed) |
| 3 | **Recency band** — CURRENT questions only | the temporal band | one year, converted to the set's own scale |
| 4 | **Authority** | the recency band | `_AUTHORITY_TOLERANCE = 0.10` (fixed) |
| 5 | **Completeness** ("substance") | the authority band | `log(rerank_substance_ratio)` |
| 6 | **Recency** | not banded — settles ties directly | — |
| 7 | Exact relevance | final deterministic tiebreak | — |

Two editions of the same annual report land in one relevance band, and unless
one is a fragment the newer leads. An older passage that actually answers the
question still outranks a newer one that merely mentions it — recency is never
the first word.

Keys 2 and 3 are **inert unless the question has the matching temporal intent**:
with none, every candidate scores alike, one band holds all of them, and the
order is exactly what it was before either key existed.

### Why a recency band, above authority, for CURRENT questions

`temporal_fit` asks whether a document's period covers the time asked about,
which for CURRENT means "is it still in force today". That is right for an
ongoing project or a standing page and wrong for the far commoner case of a
document that *reports* something on a date: a 2020 announcement and a 2023
brief both closed their period years ago, so both score `FIT_MISS` and key 2
cannot separate them.

Authority then decided, and a website announcement outranks a PDF attachment.
That is how "who is the director general" came back answered from a 2020 page
headed "Statement by Dr X, Director General, TERI", with the 2023 document
saying he "was earlier ... at TERI as its Director General" ranked below it.

Between two documents that both merely *describe* the present, the newer is the
better evidence about it — a better reason to lead than being a more canonical
kind of page. That, and only that, is what key 3 says. It cannot become "the
newest document wins": it is cut inside keys 1 and 2, it applies to no other
kind of question, and its tolerance is an absolute year, so it collapses to a
single band whenever every candidate was published within about a year of the
others.

### What "open-ended" covers, and what it does not

Under CURRENT, `temporal_fit` gives `FIT_MATCH` to a document whose period has
not closed. The only such documents are pages of the `OPEN_ENDED_BUNDLES`
(`ongoing_projects`): a project declares a start and no end because it has not
ended, and the page describing it is maintained. Every other document's period
closes where its own precision says — a 2020 announcement in 2020, a report
stated as "2019" on 31 December 2019.

An attachment of an ongoing-project page inherits the page's bundle, and until
this was measured `_is_open_ended` read the bundle alone and called the
attachment open-ended too. That made every such PDF — a guideline, a brochure,
a report, dated by its own copyright line — a perfect fit for any question about
the present, one band above every dated document. The corpus holds about 24
attachment chunks under this bundle for every page chunk, so this was not a
corner case. On the reported "who is X", a 2019 building-retrofit guideline led
the context above a 2023 brief on the strength of a foreword signed with the
subject's then title, scoring lowest of the six blocks on relevance, and the
answer gave that title in the present tense. Every dated block behind it stood
in recency order; only the guideline was out of place.

`_is_open_ended` now answers for the *page*: a `pdf_attachment` is a fixed
artefact whatever bundle it carries, and its period closes on its own date. The
project page itself is unchanged and still leads a CURRENT question. The recall
side (`filters.date_conditions`) still treats the bundle as open-ended for date
filters, which is the permissive direction and the right one there.

### Why authority sits above completeness

It used to sit below completeness, and below recency, reading a
`source_authority` payload key that nothing ever wrote — a constant that could
not reorder anything. That left completeness, a length proxy, as the first
tie-break inside a relevance band, and length is exactly the axis a canonical
page loses on: a 60-word "Water, soil and sludge testing" service node carries
the authoritative answer, and a 450-token annual-report chunk that mentions
testing in passing outranked it on substance every time.

Measured on an 86-question organisational benchmark: the authoritative page the
reference set named reached retrieval for 42% of questions, and nine questions
retrieved none of it at all. Authority is now **derived** from metadata the
corpus already carries rather than waiting for an ingest-time stamp, and it is
banded like everything else so only a material difference reorders anything.

---

## Relevance: which scorer, and volatility

`_semantic_scores(query, candidates, provider)` picks the scorer named by
`reranker_provider` (default `"embedding"` — the dense score from search,
unchanged):

| Provider | What it does | Falls back to dense when |
| --- | --- | --- |
| `embedding` (default) | Uses the dense score already on the candidate. | — |
| `llm` | One structured-output call rating every passage 0–1, only when the set is `<= _MAX_LLM_CANDIDATES` (40). | Above the cap, the call raises, or the model returns the wrong number of scores. |
| `cross_encoder` | A local `sentence-transformers` `CrossEncoder` (`BAAI/bge-reranker-v2-m3` by default), cached per model name. Scores the first `rerank_max_candidates` (40) only — one model pass per candidate, so its cost is linear where every other provider's is flat. | The library or model is unavailable. |
| `cohere` | Cohere's hosted rerank endpoint (`rerank-3.5` by default), client cached with `lru_cache`. | The API call fails. |

Every non-default provider fails open to the dense score with a warning — a
reranker outage degrades ranking quality, never breaks the request.

A cross-encoder is the one provider that does not natively return a 0..1 score:
it emits an unbounded logit (measured on this corpus, ~+4 for a passage that
answers the query and ~-11 for one that does not), so
`_cross_encoder_semantic` squashes it through a sigmoid. That is the model's own
calibration — these models are trained with BCE on that logit — and it is what
keeps `semantic_score` on the cosine footing every consumer is tuned for: the
relevance band width (`rerank_relevance_tolerance`, 0.03), the drop threshold
(`rerank_score_threshold`) and the corrective loop's `corrective_min_score`
(0.2). Passing the raw logit through
would break all three at once, in the same way `fusion.rrf` would if it wrote its
reciprocal-rank value over the semantic score. Pinned by
`tests/retrieval/search/test_fusion_score_integrity.py`.

Candidates past the cap are not dropped: they keep their incoming (fused) order
behind every scored candidate, and are deliberately not ranked against them —
their score is still a cosine while the head's is a normalised cross-encoder
relevance, and the two are not calibrated to each other. With
`retrieval_top_k` at 6 and the cap at 40, a candidate the first stage ranked
below 40th has never reached an answer.

### Relevance band width, and volatile topics

```python
def _relevance_tolerance(query, settings):
    tolerance = settings.rerank_relevance_tolerance
    if not is_volatile(query):
        return tolerance
    return tolerance * settings.rerank_volatile_tolerance_multiplier
```

`is_volatile` (`volatility.py`) is a lexical check against the **rewritten**
query (pronouns already resolved by understanding), not an LLM call: a wrong
call here only costs a marginally wider or narrower band, which does not justify
a model call's latency, cost and variance on every search.

It matches two independent things:

- **Volatile topics** — subjects whose answer has a shelf life: software/API
  surfaces (`api`, `release`, `deprecated`, …), money (`pricing`, `subsidy`,
  `budget`, …), rules (`policy`, `regulation`, `deadline`, …), and things that
  are news by definition (`announcement`, `launch`, `press release`, …).
- **Recency cues** — `latest`, `current`, `as of`, `so far`, … — regardless of
  topic.

The lexicon leans inclusive on purpose: over-matching widens a band a little,
and relevance still decides across bands, whereas a miss silently leaves two
editions of the same document ordered by a hair of cosine noise. On a policy- or
regulation-heavy corpus most queries will read as volatile, and that is the
intended reading, not a misfire — `rerank_relevance_tolerance` is the knob to
reach for if the resulting bands feel too wide.

Widening only ever changes how often authority/completeness/recency get to
*run* — nothing can cross a band, however wide it gets, so relevance still wins
whenever a genuine gap exists.

### The table boost

```python
boost = table_boost if table_boost and cand.payload.get("has_table") else 0.0
relevance = sem + boost
```

`table_boost` is `rerank_table_boost` (default `0.15`) when the caller asked for
`answer_format == "table"`, otherwise `0.0`. It lifts **relevance itself**
rather than a final score, so a table-bearing chunk can climb into a better band
when the answer wants a table — but it is still a nudge, not a filter: smaller
than the band tolerance and it changes nothing.

### The score threshold

`rerank_score_threshold` (default `0.0`, i.e. off) drops any candidate whose
semantic score falls below it, before banding. This is a hard cut, unlike
everything else in this stage.

---

## Authority: `derived_authority`

An explicit `source_authority` payload value is used as given; otherwise
`derived_authority(payload)` infers a tier from `source_type`/`bundle` — both
stamped on every chunk at ingest, so no ingest change or reprojection is needed
to use it.

| Tier | Value | Bundles (website) |
| --- | --- | --- |
| Verified graph facts | `1.00` | (`kind == "graph_facts"` — never displaced by a page merely mentioning the same entity) |
| Canonical | `0.90` | `page`, `services`, `basic` — the organisation's own description of itself |
| Primary | `0.75` | `report`, `policy_brief`, `research_papers`, `infographics` |
| Project | `0.60` | `ongoing_projects`, `completed_projects` |
| Secondary | `0.45` | `news`, `press_release`, `events`, `feature_articles`, `article`, `videos` |
| Unknown | `0.50` | anything else, or no `source_type` at all |
| Attachment | `0.35` (`0.40` if the bundle is canonical/primary) | any non-website `source_type` — a PDF is scored below its own bundle because it is a *derived* artefact of the node it hangs off |

Deliberately not a website/PDF switch — a PDF attachment is the right source for
plenty of questions (a report's findings, a table), and a blanket demotion would
bury it. Every tier sits inside one relevance band and only ever reorders
candidates relevance already called equivalent; authority cannot promote an
off-topic canonical page over a passage that actually answers the question. Cut
with a fixed `_AUTHORITY_TOLERANCE = 0.10` — under the 0.15 spacing between
tiers, so each tier separates cleanly while candidates inside one stay together.

---

## Completeness: the substance score

```python
def _substance_scores(candidates):
    return [math.log1p(len(c.text)) for c in candidates]
```

A log-scaled stand-in for "says more" — accuracy and completeness cannot be
measured at ranking time, but how much a passage actually says is visible, and a
chunk cut short at a document boundary does carry less of an answer than a full
one. Measured on the **matched child chunk**; parent expansion happens later, in
context building ([06](06-context-and-citations.md)).

Log-scaled so the band tolerance reads as a *ratio*: one passage says
substantially more than another when it holds `rerank_substance_ratio` (default
`1.5`) times the text — `log(1.5)` is the tolerance passed to `_bands`. A linear
scale would inflate the gap between, say, 1,400 and 1,500 characters into a
decisive one even though chunks are already roughly uniform in size — the same
mistake the old relevance blend made.

---

## Where this sits in the request

In `retriever.py`, in order:

1. **Search** (with an optional facet-relaxed retry) produces `candidates`.
2. **`rerank(search_query, candidates, table_boost=...)`** — this document, span
   `rag.rerank`.
3. **Corrective requery** (optional, `corrective_loop_enabled`): if the top
   result's `semantic_score` is below `corrective_min_score` (default `0.2`),
   the query is reformulated once and re-searched — see
   [04](04-search-and-fusion.md).
4. **Context is built** from the ranked list ([06](06-context-and-citations.md)).
5. Attachment supplementation and the graph merge run, if applicable.
6. **Temporal gating** runs last of all, on the finished `ContextBlock` list —
   below.

---

## Temporal gating: the "upcoming" problem

### Why it exists

An 86-question benchmark asked *"Are there any upcoming TERI training
programmes?"* and the system returned six **past** programmes from 2013–15, then
refused to answer further. Nothing on the retrieval path distinguished
"upcoming" from "ever", and nothing consulted an event's own start date — the
only date on a chunk is `effective_start_date`, when the *page* was published, which for
an "upcoming" question is close to ranking by the opposite of the answer.

### What it can and cannot do

`field_event_start_date` lives in MySQL `documents.raw_meta`, not in the Qdrant
payload, so it cannot pre-filter the vector search without re-ingesting the
corpus. It is applied instead as a **post-retrieval gate**, over the finished
context blocks — one indexed MySQL read per query (`state.event_start_dates`)
over the candidate document ids.

The gate is deliberately narrow:

- it only ever **removes** blocks — it can never invent an answer;
- it only touches documents that actually carry an event start date, so a page,
  a policy brief or a project is never affected;
- it **declines to filter at all when that would empty the context** — answering
  from stale events is bad, answering from nothing is worse. The generator is
  told what it has and can say no upcoming events are listed.

### Detecting the question's temporal mode

```python
PAST, UPCOMING, CURRENT, POINT_IN_TIME, DATE_RANGE, NONE
```

`detect_mode(question)` is a deterministic regex match, most-specific pattern
first (order matters — the first match wins):

| Mode | Example cue | Checked before |
| --- | --- | --- |
| `DATE_RANGE` | "between 2020 and 2023", "since 2019" | everything — "since 2019" would otherwise read as `CURRENT` |
| `POINT_IN_TIME` | "as of 2019", "at the time of" | `UPCOMING`/`PAST`/`CURRENT` — "in 2019" contains no tense word but is not "now" |
| `UPCOMING` | "upcoming", "scheduled", "will host", "next summit" | `PAST`/`CURRENT` |
| `PAST` | "previous", "historical", "used to" | `CURRENT` |
| `CURRENT` | "currently", "ongoing", "latest" | — |

Only `UPCOMING` currently changes retrieval. The rest are classified so the
distinction is explicit and testable, and so publication-date questions keep
using `effective_start_date` and relationship-history questions keep using claim
validity, exactly as before — this module does not touch either.

### The gate itself

```python
def gate_upcoming(blocks, *, reference=None):
    ...
    if not kept:
        return list(blocks)     # would empty the context — keep everything
    return kept                 # renumbered 1..N
```

Blocks whose `document_id` resolves to an event with a start date **before**
today are dropped; everything else — including every non-event block — is kept
untouched. If dropping would leave nothing, nothing is dropped, and an INFO log
records how many stale blocks were kept anyway.

Called from `retriever._gate_temporal`, wrapped in a bare `try`/`except` that
returns the ungated blocks on any failure: *"A temporal filter must never cost
an answer."* It runs **after** the graph merge — the last step before the
context is handed to generation — specifically so it can gate whatever the graph
merge contributed too, not just the vector-search blocks.

---

## Failure scenarios

| Scenario | Detection | Response | Recovery |
| --- | --- | --- | --- |
| Reranker provider unavailable (LLM/cross-encoder/Cohere) | `except Exception` around the provider call | WARNING logged, falls back to the dense score | Ranking quality only; the request still returns |
| LLM rerank returns the wrong number of scores | Length check in `_llm_semantic` | WARNING, falls back to dense | Same |
| Every candidate below `rerank_score_threshold` | `kept` list ends up empty | `rerank` returns `[]` | Caller (`retriever.py`) treats an empty ranked list as "nothing from the corpus" |
| `event_start_dates` MySQL read fails | `except` in `temporal_gate.event_start_dates` | WARNING, returns `{}` — gate becomes a no-op | Next query |
| Temporal gate raises for any other reason | `except` in `retriever._gate_temporal` | WARNING, ungated blocks returned | Next query |
| Gating would remove every block | Explicit check in `gate_upcoming` | Nothing is dropped; INFO logged with the count | None needed — this is the intended behaviour |

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `reranker_provider` | `embedding` | Which scorer produces relevance: `embedding`, `llm`, `cross_encoder`, `cohere`. |
| `rerank_model` | `""` | Model name for `cross_encoder`/`cohere`; each has its own built-in default. |
| `rerank_max_candidates` | `40` | Candidates the `cross_encoder` provider scores; the rest hold their order behind them. `0` disables the cap. |
| `rerank_max_seq_length` | `0` | Tokens per query+passage pair the cross-encoder sees. `0` leaves the model default (512). Latency is close to linear in this. |
| `rerank_score_threshold` | `0.0` | Hard floor on semantic score before banding; `0` disables it. |
| `rerank_relevance_tolerance` | `0.03` | Relevance band width, on the 0..1 scale every provider is normalised to. |
| `rerank_volatile_tolerance_multiplier` | `2.0` | Multiplier applied to the tolerance when `is_volatile(query)`. |
| `rerank_substance_ratio` | `1.5` | The length ratio that counts as "substantially more" for completeness banding. |
| `rerank_table_boost` | `0.15` | Relevance boost for `has_table` chunks when `answer_format == "table"`. |
| `corrective_loop_enabled` | `false` | Whether a weak top result triggers one reformulate-and-retry. |
| `corrective_min_score` | `0.2` | The `semantic_score` floor that triggers it. |

## Hand-off

The ranked candidates go to context building — [06](06-context-and-citations.md)
— which decides what actually reaches the LLM, expands parents and flags
conflicts. Temporal gating runs after that, directly on the blocks context
building produced.

---

Previous: [04 — Search and Fusion](04-search-and-fusion.md) · Next: [06 — Context and Citations](06-context-and-citations.md)
