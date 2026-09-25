# 03 — Query Understanding

**Purpose.** Turn one conversational turn into what to retrieve and how to
scope it: an intent, a standalone search query, a facet filter, and — for two
narrow, high-value question shapes — a set of specific document ids resolved
before search ever runs.

**Inputs.** The latest user turn, plus recent conversation history.

**Outputs.** A `ProcessedQuery`: the intent (`qa` / `structured` /
`scoped_summary` / `chitchat`), a pronoun-resolved `search_query`, an
`answer_format` hint, and a list of Qdrant filter conditions.

**Components.** `app/retrieval/understanding/query_processor.py` (entry point
and contracts), `prompts.py` (the classifier prompt), `filters.py` (facets →
Qdrant conditions), `relational.py` (the predicate vocabulary),
`approved_aliases.py` (query-time entity recognition), `annual_report_editions.py`
(edition scoping), `catalog_prompt.py` (shared bundle/coverage text).

This stage touches nothing in Qdrant or Neo4j. It reads MySQL (the alias table,
the bundle inventory, the annual-report series) and calls the LLM once (or
several times, see below). Everything after it — search, ranking, structured
answers, graph — reads what this stage decided and does not re-decide it.

---

## Two contracts, one boundary

`QueryAnalysis` is the **downstream contract**: a flat set of fields — intent,
search query, facet scope, structured-query slots — that every later stage
(search, structured, generation) has always consumed. `QueryUnderstanding` is
the **classifier's actual output**: a multi-label taxonomy where a turn can
carry several intents at once (`qa` *and* `structured_output`, `database` *and*
`comparison`), each with its own confidence and rationale.

The classifier answers in the second shape; `_to_legacy_analysis` collapses it
onto the first. Nothing downstream of `process()` has to know the taxonomy
exists — that boundary is deliberate, so a v2 label can be added without
touching every consumer, and so the single-label routing the rest of the
pipeline was built against keeps working unchanged.

```python
class QueryUnderstanding(BaseModel):
    query_rewrite: str
    intents: list[IntentPrediction]      # multi-label, each with confidence + rationale
    output_format: OutputFormat          # prose | list | table | csv | json | markdown | diagram | timeline
    scope: QueryScope                    # source_type, target, theme, author, tags, dates, language
    operation, group_by, bundle, title_contains, limit  # database-only slots
```

`QueryScope.date_to` is a computed `@property`, not a field the model fills:
the model is asked for the **inclusive** end date (`date_to_inclusive`) because
copying a date out of a question is reliable and incrementing one is not, and
the exclusive bound every downstream query actually needs is derived from it
(`app.core.dates.exclusive_end`) rather than asked for twice. `IsoDate` (not
`str`) is used for every date field because the model routinely trails JSON
punctuation into free-text values (`"2022-01-01},"`), which otherwise reaches
SQL as a silently dropped bound and the answer text as a visible artefact.

---

## The intent taxonomy

Nine labels, in three groups:

| Group | Labels | Rule |
| --- | --- | --- |
| Content | `qa`, `database`, `summarization`, `comparison` | Combine freely; co-equal, ranked by confidence |
| Format modifier | `structured_output` | Rides alongside a content intent; never stands alone |
| Terminal | `chitchat`, `clarification_needed`, `out_of_scope`, `safety_policy` | **Exclusive** — if any applies, return only it |

The boundary questions the prompt spells out explicitly, because they are the
ones a model guesses wrong by default:

- **A quantity a report states is `qa`; a fact about the catalog is
  `database`.** "How many MW does the report cite" is content; "how many
  reports are there" is not.
- **`summarize/overview/TL;DR` is `summarization`; a specific question — even
  across many documents — is `qa`.**
- **A shape request says nothing about the source.** "In a table" is
  `structured_output` and pairs with whichever content intent the subject
  implies; a table that lives *inside* a document ("the report's emissions
  table") is `qa`, not `structured_output`.
- **A greeting wrapping a real request is not `chitchat`** — the request is
  classified, the greeting is just noise in front of it.

### Resolving a multi-label call to a route

`_resolve_intents` applies three rules in order, over per-label confidences
that have already cleared `intent_confidence_threshold` (default 0.5):

1. **Terminal exclusivity.** If any terminal label survives the threshold, the
   highest-priority one wins alone: `safety_policy > out_of_scope >
   clarification_needed > chitchat`.
2. **A content intent is guaranteed.** If no content label survives the
   threshold, the highest-confidence content label *below* it is promoted
   rather than left empty — and if none exists at all, `qa` is manufactured at
   confidence 0.5. A turn with no terminal label always resolves to something
   that retrieves.
3. **`structured_output` rides along, never alone.** It is appended to the
   result only when a content intent is already present.

`_primary_intent` is the single-label view the rest of the pipeline actually
routes on: the highest-priority terminal label if any survived, else the
highest-confidence content label, else `qa`.

### Confidence is hybrid, not just the model's opinion

`_label_confidences` reports, per label: with one sample, the model's own
`confidence` field; with several samples (`analysis_votes > 1`), the **fraction
of samples that predicted it** — agreement, not self-reported certainty. This
is why voting changes more than robustness: a label a model asserts at 0.95 on
every one of five samples and a label it asserts at 0.95 on one of five arrive
as 0.95 and 0.2 respectively once voted, which is a materially different
signal than either alone.

`_is_ambiguous` flags a genuine multi-way call — the top two *content* labels
within 0.2 of each other — as a debug/clarification signal on `ProcessedQuery`;
it does not change the route.

### Sample voting and merge

```python
votes = max(1, int(settings.analysis_votes))
```

With `votes > 1`, `_voted_understanding` runs that many structured-output
calls concurrently at temperature 0.7 (`ThreadPoolExecutor`), drops any that
raised, and merges the survivors:

- **Intents** vote by agreement (above).
- **Scalar attributes** (`output_format`, `theme`, `bundle`, dates, …) are
  majority-voted by `_vote`: ties take the first non-null value in the order
  samples were drawn, which is a real but rarely-hit tie-break rather than a
  deterministic re-sort.
- **`query_rewrite`** is taken from whichever sample's own intent set matches
  the *merged* primary intent, not necessarily the first sample — a rewrite
  worded for the intent that actually won the vote reads better than one
  worded for an intent that lost it.

`_merge_understanding` rebuilds `QueryUnderstanding` field by field rather than
copying one sample and patching it. The comment on the assembly is explicit
about why: a slot added to the schema and forgotten here does not raise, it
silently resets to the schema default for every merged call — so the rebuild
is deliberately exhaustive, and a new slot must be added to it by hand.

**All of this is atomic per call.** If every vote errors, or the single call
(`votes == 1`) raises, `process()` returns a **passthrough** `ProcessedQuery`:
`intent="qa"`, `search_query=question` unchanged, no filters, `analysis=None`.
Understanding failing is a degraded qa search, never a broken turn.

---

## Legacy derivation: v2 → the routing the pipeline consumes

`_legacy_intent_and_format` collapses the resolved intent set onto the four
routes `app.pipeline.query_pipeline` actually branches on:

| Primary v2 intent | Route | Note |
| --- | --- | --- |
| `chitchat`, `clarification_needed`, `safety_policy` | `chitchat` | Answered from a canned string; never reaches retrieval |
| `database` | `structured` | See structured-answer routing (doc 07) |
| `summarization`, scope = single document or `title_contains` set | `qa`, format `summary` | One named document keeps the old qa+summary behaviour |
| `summarization`, otherwise | `scoped_summary` | A set or the whole corpus — see `app.pipeline.summarize` |
| everything else (`qa`, `comparison`, a lone modifier) | `qa` | |

**`out_of_scope` is deliberately routed to `qa`, not `chitchat`.** The
classifier is one stochastic sample and frequently mislabels an in-corpus
question — a pasted document title, a domain topic phrased unusually — as
out-of-scope. Routing it through retrieval lets the corpus be the arbiter: a
genuinely off-topic query retrieves nothing usable and the grounding prompt's
standard refusal fires anyway, while a misjudged one gets answered instead of
silently deflected.

---

## The chitchat safety net

A `chitchat` draw is the one label with no way back downstream — `_prepare`
answers it from a canned string and never reaches retrieval. That makes a
false-positive chitchat classification the single most expensive mistake this
stage can make, and it is not rare: measured over five identical runs, *"Who
led the Eco-city Project - Phase I?"* came back `chitchat` twice of five, and
*"Which documents were published between 2005 and 2010?"* twice of five. Both
are ordinary questions.

`_corrected_intent` runs only on the `chitchat` branch and rescues a turn by
either of two independent, deterministic probes (combined with OR):

1. **`_names_entity_and_relationship`** — the question names a known entity
   *and* an approved relationship cue (`relational.read_relational` plus
   `approved_aliases.get_index().match`). Deliberately weaker than routing: it
   asks recognition and vocabulary, not the resolver or the graph planner, so
   intent classification never reaches into graph retrieval — keeping the
   one-doorway rule (`retriever.py` only) that `tests/test_graph_retrieval.py`
   enforces.
2. **`_looks_like_real_question`** — purely lexical: an interrogative or
   content-requesting shape, at least one real content word, and no match
   against the canonical social/meta phrase list (checked last, and wins
   regardless of the other two — "how are you?" has a WH-word and passes the
   shape test, but is a greeting). This probe exists because probe 1 alone
   left three benchmark questions unrescued — none of them names a resolvable
   entity, but none of them is small talk either.

A **counting question** (`how many X`, `number of X`, `count of X`) is routed
straight to `structured`, not `qa` — no prose answer to a "how many" claim is
as trustworthy as a database count. Every other rescue lands on `qa`.

The override is one-directional by construction: a real greeting resolves
neither probe and passes through untouched, which is what the test suite
pins.

---

## Type words and tags: filters only when the question is about the catalog

The glossary the classifier reads (`catalog_prompt.BUNDLE_MEANINGS`) tells it
that a type word the user says — "articles", "reports", "news", "projects" —
names that bundle. That is right for a question *about the catalog* ("how many
articles in 2023" counts the `article` bundle) and wrong for a question about
what documents *say*. Measured on 2026-09-18: *"list down all the articles
where IPCC mentioned"* came back `database 0.78, bundle=article, tags=[IPCC]`.
The catalog holds no text, so the plan matched nothing; the fall-through to
semantic search then kept `tags in [IPCC]` on every leg, which held 2
documents (both 2011–12 feature articles) while the 22 documents with IPCC in
their title carry tags such as "Climate change" and "CoP28". The user meant
"articles" as people say it — "things you have written".

Two deterministic guards in `process()`, both built on
`content_scope.py` and run straight after `_corrected_intent`, hold the rule
the prompt states (`catalog_prompt.CONTENT_QUESTION_RULE`), because with one
analysis vote a prompt can only move which questions the model gets wrong:

1. **`_widen_content_question`** — when the wording asks for documents by
   what they say (`content_scope.conditions_on_text`: *mentioned, discuss,
   refer to, reference, talk about, contain, appear*), the type word is
   descriptive. `bundle` is cleared — *whichever* bundle it was — and a
   `structured` route becomes `qa`. The bundle is cleared on every route, not
   only `structured`, because the catalog fallback and the scoped summary plan
   from the same slots. `list_themes` and `distribution` are left alone ("what
   topics are discussed?" is about the vocabulary). Topic words ("about X",
   "on X") are deliberately *not* predicates: the structured layer accounts
   for them through `topic.residual_topic`, and "news about COP28" is often
   meant literally.
2. **`_drop_implicit_tags`** — a tag the model extracted survives only when
   the question names the facet (`content_scope.names_tag_facet`: *tag,
   tags, tagged, tagging*). Otherwise it is dropped and the word is matched as
   content by the keyword and content-term legs, which take their terms from
   the question text. Decided once here because every consumer — the Qdrant
   facet filter, the planner's tag join, the catalog fallback and the
   semantic-cache fingerprint — reads the same `analysis`.

The catalog-shaped reading is untouched: "how many articles were published in
2023?" still counts 461 articles, and "how many posts are tagged 'policy'?"
still filters by tag. Tests: `tests/retrieval/understanding/test_content_scope.py`.

---

## Facets → Qdrant filter (`filters.py`)

`_facet_filters(analysis)` builds the `FieldCondition` list:

| Analysis field | Condition | Note |
| --- | --- | --- |
| `theme` | `categories` MatchAny, `{theme, theme.title(), theme.strip()}` | Payloads store whatever casing the CMS supplied; there is no MySQL term table to translate a name into ids here |
| `author` | *(none — deliberately not filtered)* | See below |
| `tags` | `tags` MatchAny | Only reaches here when the question names the tag facet ("tagged X"); `_drop_implicit_tags` clears a subject word the model put in the slot — see above |
| `source_type == "pdf"` | `source_type` MatchAny `["pdf", "pdf_attachment"]` | "PDFs" includes attachments |
| `source_type in ("website", "article")` | `source_type` MatchAny `["website", "article"]` | `article` kept for points indexed before the bundle rename |
| `language` | `language` MatchValue | |
| `date_from` / `date_to` | `effective_start_date` `DatetimeRange`, UTC-aware | Only when the scope is *not* relationship time — see below |

**Why `author` is never a hard filter on the qa path.** The stored `authors`
payload field is a keyword index — exact match, no substring — populated on
only ~20% of chunks, holding full display names ("Ms Meena Sehgal"). The
understanding LLM extracts a loose form ("Sharma", "TERI") that almost never
equals the stored value. As an AND condition that excludes the ~80% of the
corpus with no author facet at all and then usually misses the rest anyway —
turning a strong semantic match into a false refusal. Author scoping is
applied only on the structured/catalog path, which `LIKE`-matches a real facet
table (see doc 07). The qa path relies on semantic search, where an author's
name in the title or body text already surfaces relevant content.

### Relationship time vs. publication time

`_is_relationship_time` decides whether a date range in the question bounds a
*relationship* ("what did the Department of Biotechnology fund between 2005
and 2010") rather than *when documents were published* ("reports published
between 2005 and 2010"). Applying the wrong reading is not a near miss:
`effective_start_date` holds no value before 2010 on this corpus at all, so scoping a
funding question by it selects almost nothing — and it also blocks graph
routing, which is the one path that *can* answer a validity question by
interval overlap (`retrieval/graph/scope.py`), by handing it a scope no
template expresses. Measured: every year-range relational question in the
benchmark reached `scope_unsupported` under the old reading.

The test is narrow and deterministic: the question names an approved
predicate (`relational.read_relational(...).is_relational`) **and** the
sentence contains no publication-language cue (`published`, `issued`,
`dated`, `report(s)/document(s)/paper(s) from|in|of|between`). Either
condition failing keeps the existing document-date reading exactly as it was
— which is why "which documents were published between 2005 and 2010" is
untouched.

### The date-filter retry contract

`filters.date_conditions(filters)` extracts just the `effective_start_date` condition
out of a mixed filter list, by attribute (`getattr(c, "key", None)`) rather
than type, since `_theme_condition` returns a nested `Filter`, not a bare
`FieldCondition`. This is what lets `retriever.retrieve` (doc 04) drop
everything *except* the date scope on a total-miss retry: theme, author-shaped
guesses and source-type are the understanding LLM's guesses at how the corpus
happens to be labelled, so discarding them recovers from a bad guess — but a
date range is what the user actually asked for, and widening it answers about
a period they did not ask about. That distinction is why the date condition
survives the retry and nothing else does.

---

## Relational questions: the predicate vocabulary (`relational.py`)

`PREDICATE_CUES` is a closed map — `FUNDED_BY`, `LED_BY`, `PARTNER_OF`,
`WORKS_AT`, `MEMBER_OF`, `PARENT_OF`, `HAS_ROLE` — from predicate name to the
phrases this corpus actually uses for it ("sponsor", "grant", "financed" for
`FUNDED_BY`; "principal investigator", "spearheaded" for `LED_BY`). Declaring
cues here rather than in a route table is the point: an approved predicate
becomes askable by describing how people phrase it, not by adding a branch to
a router.

This module is intentionally at the *understanding* layer, not inside
`retrieval/graph/`, even though only graph retrieval ever traverses these
predicates. Three consumers need the vocabulary — the graph router (to plan a
hop), the facet builder above (to distinguish relationship time from
publication time), and the chitchat safety net (to know a relational question
is not small talk) — and understanding owning it is what keeps the dependency
one-directional: `graph/` imports this module, and the reverse would pull
graph retrieval into the general path, which the isolation tests forbid.

`PARENT_OF`'s cues are deliberately narrower than the other predicates: bare
organizational nouns like "department" or "division" were tried and removed,
because this corpus is full of organization *names* that contain them — every
question about the Department of Biotechnology itself started reading as a
question about its internal structure. Every surviving cue is a relational
phrase ("a unit of", "owned by"), not a noun that could be the entity's own
name.

`read_relational(question)` returns a `RelationalIntent`: the predicates
named, most-named-first (by character offset, not by a set — a two-hop
question like "who leads projects funded by X" names `LED_BY` before
`FUNDED_BY`, and the offsets are what let the graph router build the chain
outward from the anchor), plus `inverse_hint` for the one predicate
(`PARENT_OF`) whose domain and range share a type. Only cues whose predicate
name is in `app.knowledge.claims.predicates`' live vocabulary are honoured, so
a cue left behind for a retired predicate cannot resurrect it.

---

## Query-time entity recognition (`approved_aliases.py`)

The gazetteer used everywhere else (`app.knowledge.gazetteer`) is built from
raw CMS metadata and has to be conservative — its inputs really do include
"Steel", "Summary" and "Download" as project titles — so it requires long
surfaces and case-sensitive matching for short ones. That is right for
scanning prose, and too blunt for a *question*: benchmarking found five whole
classes of ordinary phrasing that produced zero recognised mentions at all
("What did ADB fund?", "Who led WEO 2007?", "Who worked on HI-AWARE?").

This module reads a cleaner source instead — `documents_entity_alias`, the
**reviewed** alias table for seeded entities — and is query-only by
construction: ingestion writes claims, so widening what ingestion links would
change what gets asserted, but widening what a *question* may look up only
changes what can be found.

### Four admission guards (building the index)

An alias row is indexed only if **all** hold:

1. `autolink = 1` and `is_ambiguous = 0` — review said this surface may link.
2. The owning entity is `active` and `claim_eligible`.
3. The normalized form maps to exactly **one** entity across the whole alias
   table, non-autolink rows included — a recorded ambiguity anywhere vetoes
   the surface (this is what keeps `MPCB` — both a Haryana and a Maharashtra
   board — unresolved).
4. An `acronym`-type row must be acronym-*shaped* (`^[A-Z][A-Z0-9&.\-]{1,7}$`)
   and its letters must be derivable from the initials of the name it claims
   to abbreviate (`acronym_matches_name`). This guard earns its place: the
   glossary extractor produced `MOEFCC -> Central Pollution Control Board` —
   wrong, and flagged non-ambiguous, so the review flags alone would have
   admitted it. It costs three legitimate syllabic acronyms (TRIFED, HAREDA,
   POSOCO), which stay unresolved rather than risk a wrong identity.

A second acronym source, `derived_owners`, computes initials directly from
every active organization's own `canonical_name` and admits the result only
when exactly one organization produces that acronym and the alias table has
not separately flagged it ambiguous — not new data, just the deterministic
inverse of guard 4.

### Admissibility (matching one occurrence)

Passing the index only says an alias is *safe to link somewhere*;
`_admissible(alias, text)` decides whether *this* span in *this* question is
really using it as a name:

| Alias shape | Rule |
| --- | --- |
| Acronym | Case is the whole signal — `oil` is not "Oil India Limited", `OIL` is |
| Code | No rule — a project code (`2012MC03`) is distinctive by construction |
| Person, ≥2 tokens | Admitted — a person's name is not a common noun |
| Org/project, > 3 tokens | Admitted — long enough to be distinctive alone |
| Org/project, 1 token | Needs a digit or an internal capital (`Water4Crops`, `HI-AWARE`) |
| Org/project, 2–3 tokens | Needs a capital letter anywhere in the matched text |

`CASE_SENSITIVE_MAX_TOKENS = 3` matches `gazetteer._CASE_SENSITIVE_MAX_TOKENS`
deliberately — the same tension applies in both places: "Water Resources" is a
real TERI division and an ordinary noun phrase, and only capitalisation tells
them apart.

`ApprovedAliasIndex.match` finds longest-first, non-overlapping spans (a
12-token cap on the n-gram sweep, `MAX_ALIAS_TOKENS`), then `lookup_mentions`
turns each match into a `knowledge.types.Mention` carrying the **entity's
canonical name** as its surface — not the string the user typed — so the
resolver's exact-name tier finds the entity by the name the store actually
knows it by, while the mention's offsets still point at the original text
(what keeps entity masking in the graph router honest). The mention then
passes through the **unchanged** resolver, with every existing veto still
applying — a provisional person still declines, an ambiguous surface still
declines. This module only widens which strings get to *ask*.

The index is process-wide, TTL-cached at 300s (`INDEX_TTL_SECONDS`, matching
`graph.policy.INDEX_TTL_SECONDS`) so a re-seed is picked up without a restart
and a query never pays a MySQL round trip.

---

## Annual-report edition resolution (`annual_report_editions.py`)

"Give me the latest annual report" cannot be answered by ranking, for a
structural reason: every edition is an in-body attachment on the *same*
Drupal page, so all ten share that page's `effective_start_date` (2022-02-09) and a
breadcrumb that names the page, not the edition. Relevance cannot separate
them (near-identical text), and recency cannot either (a ten-way tie on the
same date) — the observed failure was page 148 of the 2020-21 edition winning
by a hair of cosine noise. See the write-side edition mechanics in
[ingestion doc 06](../ingestion/06-canonical-document-and-dates.md#pdf-publication-date-resolution).

`resolve(question)` runs entirely before search, is lexical and deterministic
(no LLM call — this has to be reproducible and free on every question that
merely mentions the series), and returns `None` — leaving retrieval untouched
— unless the question actually names the series (`\bannual\s+reports?\b`).

### Precedence, once the series is named

1. **A whole-series cue** (`all`, `list`, `how many`, `trend`, `older`,
   `compare`, …) → unfiltered. Narrowing "how many annual reports are there"
   to one edition would answer a different question.
2. **A specific edition is pointed at** — a canonical span (`2024-25`), a
   span shape that matches no held edition (`2019-2024` is a period,
   `2031-32` is out of range), or a bare year *only when adjacent to the
   series name* (`annual report 2018`, never a year elsewhere in the
   sentence, which would misread a report's own discussion of 2018 as a
   request for the 2018 edition). A pointed-at edition the series does not
   hold resolves to `None` — **never** falls through to the newest — because
   answering "the 2012-13 report" out of the 2024-25 edition is worse than
   answering unfiltered.
3. **The earliest** (`earliest`, `oldest`, `first`) → the minimum edition.
4. **Otherwise, default to the newest edition.** This is the one place the
   module resolves something the user did not explicitly ask for, and it is
   framed as a correction, not a guess: leaving "the annual report" unscoped
   does not search the editions even-handedly, it lets whichever chunk scores
   a hair higher decide. `kind` distinguishes `latest` (the user said so) from
   `default_latest` (they didn't) purely for the log line.

The series itself is discovered by grouping catalogued attachments whose
title starts `Annual Report%` by the Drupal page they hang off
(`_read_series_rows`, reading the catalog directly rather than
`documents_date_decision`, which is documented as never being read back into
retrieval). The page holding the *most* editions wins, and only outright — a
tie between two pages means two plausible series and resolves to nothing,
because there is no way to tell which one "the latest" means. Cached 300s.

`conditions_for` scopes by **`document_id`**, not by edition label, so the
filter cannot be defeated by a label spelled `2023-2024` in one place and
`2023-24` in another — the ids came from the catalog already resolved. The
parent page is deliberately excluded from the scope: it links every edition,
so admitting it re-introduces exactly the ambiguity the filter exists to
remove. Because this returns a facet condition (matched against
`document_id`), a miss falls back to the retry-without-facets path in
`retriever.retrieve` (doc 04) exactly like any other facet miss — it can
never *starve* retrieval, only fail to narrow it.

---

## The corpus vocabulary shared with three prompts (`catalog_prompt.py`)

This module holds the bundle glossary, the coverage directives and the
few-shot bank as plain text — deliberately with **no import of
`app.retrieval.structured`**, even though two of its three consumers
(`understanding/prompts.py`, `structured/answerer.py`, `structured/planner.py`)
live one layer below it in `structured`. Importing that package runs its
`__init__`, which constructs the MySQL/Qdrant/LLM-backed tools and planner —
paying for the whole structured-query stack just to obtain prompt text was the
wrong trade, and would additionally create an import cycle
(`structured.__init__ -> answerer -> this module`) that only stays acyclic
while this module imports nothing from `structured`.

**`BUNDLE_GLOSSARY`** exists because a bare list of bundle names lets the model
guess the collective reading of an everyday word: "articles" reads as a
generic word for "any record" in most CMSs, but here `article` is a real
bundle holding 459 of 2,135 rows, and "total number of articles" answered
`2135` before this was written down explicitly. The same trap sits under
"reports" and "papers". Each described bundle is checked against the live
registry (`tests/test_shared_prompt.py`), so the glossary cannot silently
drift from `app.core.corpus.DEFAULT_BUNDLES`.

**Two directives are computed per request, not baked into the prompt text**,
because they describe *this deployment's actual data*, not the configured
vocabulary:

- `catalog_inventory_directive()` — which configured bundles the catalog
  actually holds rows for. Without it, a type with zero rows is still
  advertised as valid, the model confidently sets it, and the answer is a flat
  zero that reads like a fact about the corpus rather than about a vocabulary
  gap.
- `catalog_coverage_directive()` — the real `effective_start_date` span the catalog
  covers. Without it, "what changed this year" against an archive whose newest
  document is from 2024 gets a confident zero about a period the catalog never
  reached. It also settles what a bare "the latest" means: left alone the
  model turns it into a guessed date bound that *excludes* the very documents
  that would answer the question, so the directive says explicitly to leave
  both dates null for "latest/newest/most recent" and let ranking's
  recency band (doc 05) do the job instead.

Both return `""` on any read failure (no database, a MySQL blip), so an outage
degrades to the configured-vocabulary prompt rather than asserting the catalog
is empty. Both are meant to sit *before* `current_date_directive()` in the
assembled prompt — text that changes only when the corpus changes stays ahead
of text that changes every day, which matters for prompt-prefix caching.

---

## Validation performed at this stage

| Check | Where | On failure |
| --- | --- | --- |
| Structured-output call succeeds | `process()` | Passthrough `ProcessedQuery` (plain `qa`, unfiltered) |
| At least one vote survives | `_voted_understanding` / `process()` | Passthrough |
| Merged intent set is non-empty on the content side | `_resolve_intents` | A content intent is manufactured (`qa`, confidence 0.5) rather than left empty |
| A chitchat draw is really chitchat | `_corrected_intent` | Rescued to `qa` or `structured` on either probe |
| A type word is a filter the user chose | `_widen_content_question` | On a question about what documents say: `bundle` cleared, `structured` → `qa` |
| A tag is a filter the user chose | `_drop_implicit_tags` | Tags dropped unless the question names the facet |
| Approved-alias acronym is acronym-shaped and derivable | `ApprovedAliasIndex.build` | Row excluded from the index |
| Alias normalized form is unambiguous corpus-wide | `ApprovedAliasIndex.build` | Row excluded from the index (guard 3) |
| Edition span names a series-held edition | `annual_report_editions._requested` | Resolves to `None`; retrieval stays unfiltered |
| Two pages both look like the annual-report series | `annual_report_editions._series` | Resolves to `{}`; nothing scoped |
| Catalog inventory/coverage readable | `catalog_prompt.*_directive` | Returns `""`; prompt falls back to the static glossary |

## Failure scenarios

| Scenario | Detection | Response | Recovery |
| --- | --- | --- | --- |
| LLM call raises or times out | `except` in `process()` / `_voted_understanding` | Passthrough query; that vote (or the whole call) is dropped | Next turn retries independently |
| All `analysis_votes` samples fail | `process()` | Passthrough | — |
| Model mislabels a real question as `chitchat` | Measured to recur (2/5 draws on some questions) | `_corrected_intent`'s two probes | Tune `intent_confidence_threshold`, or extend the probes' cue vocabulary |
| Relational probe / alias lookup itself raises | `except Exception` in `_names_entity_and_relationship`, `lookup_mentions` | Logged (`debug`/`warning`), treated as "no evidence" | A probe must never break understanding — this is enforced by the try/except, not by correctness |
| MySQL unreachable for the alias index | `ApprovedAliasIndex.load` raises | Propagates to the caller's try/except (recognition, not understanding, degrades) | Next call after the connection recovers |
| Annual-report series unreadable | `except` in `_series` | Logged warning; resolves to `{}` → unfiltered | Next call after the connection recovers |
| A CMS field the classifier was never told about drives a wrong bundle guess | Not detected here | Wrong or null bundle in `QueryAnalysis` | Extend `BUNDLE_MEANINGS` / `catalog_inventory_directive` |
| Model turns a type word or the subject into a filter on a content question | Measured: "articles where IPCC mentioned" → `bundle=article`, `tags=[IPCC]` | `_widen_content_question` / `_drop_implicit_tags` on the wording | Extend `content_scope`'s predicate or `CONTENT_QUESTION_RULE` |

## Observability

- `logger.info("intent: %s -> route=%s%s", ...)` on every call — the full
  per-label confidence list, the resolved route, and an `(ambiguous)` suffix
  when the top two content intents are within margin.
- `logger.info("annual-report edition: %s", resolution.describe())` — kind,
  resolved edition(s), document count, and the full series for context.
- `logger.warning("Understanding vote failed; dropping it.", exc_info=True)`
  per failed sample under voting.
- `logger.info("Approved-alias index: %d alias(es), %d derived acronym(s).")`
  on each index rebuild.
- `logger.info("Overriding a chitchat classification: ...")` whenever the
  safety net fires, naming which probe rescued the turn.

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `analysis_votes` | `1` | Concurrent understanding samples per query; `>1` switches confidence from self-reported to agreement-based |
| `intent_confidence_threshold` | `0.5` | Minimum per-label confidence to survive into intent resolution |
| `INDEX_TTL_SECONDS` (approved aliases) | `300` | Alias-index rebuild interval |
| `_CACHE_TTL_SECONDS` (annual-report series) | `300` | Series-lookup cache interval |

## Hand-off

`ProcessedQuery.filters` (facet conditions plus any edition conditions) and
`search_query` go to `retriever.retrieve`, which runs the search legs and
retries without facets on a total miss except for the date condition
(`filters.date_conditions`). See [04 — Search and Fusion](04-search-and-fusion.md).
The `structured` route instead hands the whole `QueryAnalysis` to
`app.retrieval.structured.planner` (doc 07). The `scoped_summary` route hands
it to `app.pipeline.summarize`.

---

Next: [04 — Search and Fusion](04-search-and-fusion.md)
