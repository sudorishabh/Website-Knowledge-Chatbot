# 07 — Structured (Catalog) Answers

**Purpose.** Answer questions the MySQL catalog can settle exactly —
counts, listings, breakdowns, theme vocabulary, a specific document by title —
without a vector search, and to refuse rather than guess the moment the
question asks something the catalog's closed facet set cannot honestly
express.

**Inputs.** A question, plus either the unified query-understanding
`QueryAnalysis` (the common case) or a fresh LLM parse when none was supplied.

**Outputs.** A rendered answer plus structured `data` and `citations` — either
terminal (the catalog's own words are the answer) or `None`, meaning "fall
through to semantic search."

**Components.** `app/retrieval/structured/{planner,tools,entities,resolve,
filters,theme_scope,topic,types,answerer}.py` — `answerer.py` is the thin
adapter (`answer_structured`, `catalog_fallback`) that `app/pipeline/
query_pipeline.py` actually calls; everything else in the list is what it
delegates to. Reads `app/catalog/queries.py` for every SQL statement these
tools issue; see [docs/ingestion/08](../ingestion/08-persistence-and-catalog.md)
for the table shapes being queried.

---

## Why this path exists, and why it is careful rather than eager

"How many policy briefs are there?" is a `COUNT(DISTINCT document_id)`, not a
vector search — semantic retrieval would rank the ten most recent policy
briefs highly and never actually count them. So this path exists to answer
catalog-shaped questions exactly. But the catalog's facet set is small and
closed (content type, theme, tag, author, title substring, date), and a
question's real subject is open vocabulary. When the two do not line up, an
eager structured path fails in two measured, specific ways:

1. **A topic gets snapped onto the nearest taxonomy theme.** "publications on
   *Sustainable Development Goals*" resolved to the theme "Resources &
   Sustainable Development"; "reports on *climate change adaptation*" resolved
   to "Climate Change". Both themes are real and far broader than the
   question, so the filter was not absent — it was wrong, and the answer came
   back as the newest rows of a large bucket (an opinion piece on education,
   a children's science congress, a BioE3 video, for the SDG question).
2. **Whatever the facets cannot express simply vanishes.** "Which researchers
   work on AI and sustainability?" carried no facet at all, so the plan
   degraded to a bare `list_records` over everything, ordered by recency —
   and named nobody, because a document catalog stores *authors*, which is a
   different claim from "works on."

Both collapse to the same signature: **the list head** — the newest N rows of
whatever bucket survived, nearly identical for any two questions landing in
the same bucket. `app.retrieval.structured.topic` and `theme_scope` exist
specifically to catch this before it reaches the user; see below. The
governing rule, stated once and applied everywhere in this package: **prefer
"no trustworthy structured answer" to "a plausible but wrong list."**

---

## Entities: the queryable registry

An "entity" in this package is a content bundle (`news`, `research_papers`,
`policy_brief`, ...) — every row is `source_type='website', entity_type='node'`
in the catalog. There are no per-entity tables and no per-entity tools: a
bundle is a query *parameter*, so registering a new content type is a data
change in `app/retrieval/structured/entities.py`, not new code.

- **`normalize_entity`** maps free text ("event", "press release", "person")
  onto a canonical bundle via a synonym table, then plural/singular tolerance.
  An unrecognized type returns its cleaned key rather than `None` — callers
  gate on `is_known`, so an unrecognized entity counts as *nothing matches*,
  never as *everything matches*.
- **`is_known`** vs **`is_available`** are deliberately different questions.
  `is_known` says the bundle is configured at all; `is_available` says *this*
  catalog actually holds rows for it. The distinction exists because a
  known-but-empty bundle used to produce confident false zeroes: filtering on
  `bundle='report'` against a catalog with no reports answered "0 reports" as
  if the query had genuinely counted them, when the honest answer is "this
  deployment doesn't have that content type" — which falls through to
  semantic search instead.
- **`_AMBIGUOUS_BUNDLE_WORDS`** is a short, curated list of words that name
  *several* bundles at once — today just `"projects"` → `(completed_projects,
  ongoing_projects)`. Before this existed, "projects" silently resolved to one
  project type and answered "0 ongoing projects" while 918 completed ones
  existed. The catalog tools take one bundle per call, so the honest move for
  a genuinely ambiguous word is to ask, never to guess (see the ambiguity
  rule in `resolve.py` below) — and this is checked *before* any query runs,
  independent of the fuzzy-matching flag, because it is a curated list rather
  than a similarity score.

---

## Resolution: fuzzy name matching with an explicit ambiguity band

`resolve.py` maps loose, synonym-heavy free text ("rishab negi", "env theme")
onto the catalog's own author, bundle and theme names. It is plain
normalization plus `difflib`, scored in Python over each type's small
candidate set (16 bundles, ~200 themes, low hundreds of authors) — a
deliberate no-new-dependency choice, since the sets are small enough that a
search index would be overhead.

`score(query, candidate)` takes the max of four measures — a whole-string
ratio, a word-order-insensitive ratio (`"negi rishabh"` ties `"Rishabh
Negi"`), a single-token prefix/abbreviation match (`"env"` → `"Environment"`,
discounted by how much of a multi-word candidate that one token represents,
so it does not outscore an exact match), and a length-aware substring boost.
Filler words a user adds around the entity itself ("env **theme**", "policy
**brief**") are stripped from the query before scoring, never from the
candidate side.

**Three bands, not two: `ACCEPT` / `AMBIGUOUS` / `MISS`.**
`classify_band(top_score, runner_up_score)` accepts when the top score is
near-exact (≥0.90) *or* clearly dominant over the runner-up (≥0.60 and a
≥0.30 margin — "no real competition"); anything scoring ≥0.60 without that
dominance is `AMBIGUOUS`, and everything below is a `MISS`. The tuning target
was two worked examples: "climate" must accept to "Climate Change" (dominant,
even if not near-exact), while "rishab" must **not** silently accept
"Rishabh Negi" over "Rishab Nigam" (a genuine tie) — it must ask. This is the
concrete form of the ambiguity rule stated throughout this package: a
moderate match with a real competitor is a question for the user, not a
guess.

Tags are matched by **exact name only** (`_resolve_tag_name`), never
fuzzily — a long-tail freeform vocabulary (thousands of near-duplicate
entries like "Solid waste" / "Urban waste" / "Waste management") would flag an
ambiguity on almost every query if scored the same way authors and themes are.

---

## The Scope Resolver: canonicalizing filters on the way to SQL

`filters.resolve_filters(RecordFilters) -> ResolvedScope` is where
author/theme/tag canonicalization actually happens — deliberately **not** a
separate planner step, because a plan's tool calls execute in parallel with no
data flow between them (a `resolve_entity` call could never hand a result to a
sibling `count_records`). Resolving here means every tool benefits from
canonicalization regardless of how a plan is shaped.

Two outcomes worth knowing about specifically:

- **`entity_resolution_enabled` gates what happens to an imperfect match, not
  whether matching runs.** Matching against the catalog's stored names is
  unconditional now that every facet (author, theme, tag) has its own table to
  match against. With the flag off, an ambiguous name quietly takes the best
  candidate and a miss is not reported — the pre-existing behaviour of
  answering with whatever a filter finds. With it on, a genuine `AMBIGUOUS`
  match becomes a clarification question (one at a time — author first, since
  a person's name is the most common source of a real near-tie) rather than a
  silent pick.
- **A theme substitution can be *unfaithful*, and that is checked
  separately.** `topic.faithful_theme(requested, resolved)` demands the
  resolved name carry **every** word the question asked for — directional
  containment, not similarity. "climate" → "Climate Change" is faithful
  (naming part of a theme is fine); "climate change **adaptation**" →
  "Climate Change" is not (dropping the word "adaptation" widens the answer
  to everything the word existed to exclude — measured: a request for
  adaptation reports returned a COP28 decarbonisation report and a NAPCC white
  paper). An unfaithful substitution is dropped (`theme_widened` records what
  it would have been, for diagnostics only), and the caller falls through to
  constraining by the topic's own words instead — see below.

---

## The topic residual: what facets cannot express, kept rather than dropped

`topic.py` computes, for a `list`/`lookup` question, the words left over after
every facet that genuinely covers the question's vocabulary is accounted for
(`residual_topic`). "What policy briefs has TERI recently published?" is
*entirely* content type and recency — once `policy_brief` and the collective
words ("publications", "documents", ...) are consumed, nothing is left, and
the ten most recent policy briefs are the right answer with no further
constraint. But when subject-matter words survive that accounting, they
become an explicit `topic_terms` constraint on the rows
(`RecordFilters.topic_terms`) rather than being silently dropped — a title
must contain at least one, and rows matching more of them rank higher.

Two supporting mechanisms:

- **`_ubiquitous`** drops terms too common across the corpus's own title
  catalogue to name a subject — computed live against `state.website_titles()`
  rather than configured, so the organisation's own name (in 11.9% of this
  corpus's titles) is dropped without this module ever being told what the
  organisation is called. Fails open toward a *larger* residual on a lookup
  failure, which errs toward declining rather than guessing.
- **`wants_person`** detects a question asking for people rather than
  documents ("which researchers work on...", "who wrote..."). The structured
  path declines these outright (in `answer_structured`, before any plan runs)
  unless the question names a specific person to look up — a document catalog
  records *authorship*, a different claim from "works on", and listing
  documents at a person-question produces a confident non-answer (measured:
  an opinion piece, a memorial lecture, a solar-industry news item, naming
  nobody, for "which researchers work on AI and sustainability?" — semantic
  retrieval had the two actual AI papers whose recorded authors are the
  answer).

---

## Theme scope: Main, Other, and never volunteering Other by accident

`theme_scope.py` decides, deterministically and independently of the LLM
classifier, whether a theme question wants the curated **Main** structure, the
peripheral **Other** vocabulary, or **All** of it. It is deliberately its own
module rather than a field the classifier sets, because the one guarantee
that matters — *a generic theme question never exposes Other themes* — must
not sit behind a model's paraphrase-sensitive judgement. `SCOPE_MAIN` is the
default for anything unrecognised; the two failure modes are asymmetric
(answering generically with only the main areas is correct, volunteering the
peripheral ones is the leak this module exists to prevent), so breadth is
only ever granted by an explicit marker ("other", "outside the main areas",
"all themes").

A second, independent job — **whether a question is about themes at all**
(`mentions_themes`) — used to be conflated with the scope decision, and that
conflation applied a Main-theme restriction to *every* count: "how many
authors are there?" quietly excluded any author whose documents carry no main
theme (955 → 876), and a plain document count lost 2,620 untagged documents.
`planner._theme_group_for` now only applies a theme-group restriction when
the question is themed *and* names no specific theme itself — a named Other
theme (e.g. "Green Shipping") must stay countable even though the generic
default excludes Other.

---

## The Database Planner: v1 deterministic, v2 optional LLM multi-call

**v1 (`planner.plan`)** is the default: a single deterministic mapping from
the slots query understanding already extracted (or a fresh
`parse_structured` LLM call when none was supplied) onto exactly one
`ToolCall`. It reads `operation` (`count`/`list`/`lookup`/`distribution`/
`list_themes`) and dispatches accordingly, applying the topic-residual and
theme-scope logic above along the way.

**v2 (`planner.plan_multi`, gated by `database_multi_call_enabled`)**
decomposes a genuinely compound question into up to `_MAX_CALLS` (4) tool
calls — a comparison across periods or themes, a count paired with a list, or
a request that needs a name resolved before the real query
(`resolve_entity`, as its own planned step). It returns `None` on any failure
or an empty plan, and the caller falls back to v1 automatically. Independent
calls in either version execute in parallel via a small `ThreadPoolExecutor`
(`planner.execute`).

A detail worth knowing if extending the planner: `ToolCall.offset` exists on
the type but is **deliberately never settable by the v2 LLM planner** — paging
needs a notion of "the next page" this pipeline has no conversation state
for, and a hallucinated offset would silently hide rows rather than fail
visibly. It stays available for a programmatic caller that is genuinely
paging.

---

## The tools

Each tool in `tools.py` wraps one `app.catalog.queries` read and renders a
uniform `ToolResult` (`ok`, `data`, `citations`, `rendered`, `error`,
`error_kind`). Every tool shares the same guard sequence: `_entity_guard`
(unknown or ambiguous content-type word), `_scope_guard` (an ambiguous
author/theme/tag), then the query itself, then `_empty_result_miss` (was an
empty result a genuine zero, or a name/bundle that never resolved to anything
real?).

| Tool | Answers | Notable behaviour |
| --- | --- | --- |
| `count_records` | "how many X" | `count_of` changes *what* is counted (documents vs. a distinct facet value — "264 authors work on Energy" is a different claim from "264 articles"); an unrecognised `count_of` is refused rather than silently defaulting, because a wrong noun on a right number is a confident wrong answer |
| `list_records` | browse/enumerate | Leads with "Here are the N most recent of TOTAL" whenever the page is full and the topic constraint is active, using the identical filters that produced the rows, so the two numbers can never disagree ("N of TOTAL …, closest matches first" when a topic ranks the rows). Items are linked titles with dates at the stored precision (`rendering.py`), and name their content type when the list spans several |
| `lookup_record` | one specific document by title | Also resolves a `chain_document_id` (`_resolve_chain`) when the title uniquely matches one catalog document *and* the question asks about content ("what does X say") rather than browsing — letting the pipeline chain straight into full-document QA |
| `aggregate_records` | breakdown per theme/content type/author/year | `secondary_group_by` makes the key the **pair** of dimensions ("which authors write about which themes"), not a repeated single breakdown; ignored when it names the same dimension as the primary (a pair of one thing is the single-dimension question). Content types read as their labels ("feature articles"), years come back in full and in order, and a breakdown cut at 20 groups (50 pairs) says so |
| `list_themes` | the theme vocabulary itself | Top-level themes only, split into Main and Other by `theme_scope`; rows stored as sub-themes are never listed, and a named theme does not narrow the listing — so a `list_themes` reading that carries a theme never reaches it: `answer_structured` declines, and the question ("tell me about climate change theme") is answered from passages and the theme's own page |
| `resolve_entity` | "what does X refer to" | The only tool that wraps `resolve.py` rather than a catalog read; renders `ACCEPT`/`AMBIGUOUS`/`MISS` as a resolved name, a "which did you mean?" clarification, or an explicit no-match respectively |

### Author counts are counts of *names*, not people

`_SOURCE_LABEL_DIMENSIONS = {"author"}` and the deliberately non-plural noun
`"distinct author name"` (not "author") exist because Drupal stores authors
as free text with no id, email or stable reference anywhere in the payload —
the knowledge graph's PERSON entities for authors are all provisional. So the
catalog can count distinct **labels in the source**, and cannot count
**people**: two people both written "Arun Kumar" are one name here, and one
person written "Datta Debajit" in one place and "Debajit Datta" in another is
two. Every rendered count under this dimension says "... recorded in the
source data" rather than asserting an identity resolution that has not been
done.

### What an answer says beyond its headline

A count used to be one sentence: "There are 68 articles on 'Climate Change'
matching your query." Right, and nothing else. `detail.py` follows each
headline with what the same scope shows, read over **exactly the filters the
headline used**, so the extra lines cannot disagree with the number above them:

| Answer | Follows the headline with |
| --- | --- |
| A count of documents | The years they date from and the peak year; the content-type mix when the count spans types, or for one author's count of one type their total across types; the three most recent items, linked and cited; follow-ups that vary ("list them", "break them down by year") |
| An honest zero | The nearest scope that is not empty: across all dates (and how recent the newest is) when a period emptied it, else across all content types |
| A distinct count | The values themselves — all of them when few, else the five largest (for authors, the names that appear most often) |
| A breakdown | The scope's total in the lead, the year span and peak or the leading group, and a note when a document can sit in several groups |
| A list | Bylines on each item (only the co-authors in a list by one author); one document is shown as a card (type, date, authors, top-level themes) with an offer to answer from it; a cut list offers the dimensions still open |
| The theme listing | How many items each theme holds, counted as a theme count counts them |

No model call, and every read is fail-open: a query that fails costs its own
section and nothing else. Detail is given to a plan with **one** real question
in it (a `resolve_entity` step does not count) — two counts side by side are a
comparison and stay headlines — and never to the catalog section of a combined
answer, whose grounded prose is the detail. `catalog_answer_detail_enabled`
turns it off; the headline then stands alone as before.

The follow-ups offered are ones the catalog route was checked to answer from
the conversation ("list them", "break them down by year", and "what does it
say?" after a card, which chains into document QA).

### A person's "articles"

"Article" is one content type on the site and everyday English for anything a
person writes. Asked of a person, the everyday reading is the one meant: "how
many articles are there of Vibha Dhawan" used to count the 1 item in the
Article category and miss 31 feature articles, 7 research papers and 2 policy
briefs. `answerer._reads_article_as_writing` decides; `_widen_articles` then
clears the type from the *planned* calls, not the slots, so the planner still
reads "articles" as the type word rather than leaving it over as a title word.
The answer covers both readings — the total across types, and how many of
those are in the Article category:

> There are 41 publications by Dr Vibha Dhawan matching your query.
>
> That spans every type of publication, not only the site's Article category,
> which holds 1 of them. ...

The category reading stays when the wording points at it ("the article
category", "only articles", a quoted "Articles") or names another type beside
it ("articles and research papers"). Without a person, "articles" stays the
Article category. Every document that carries authors is a publication
(feature article, research paper, article, policy brief, report), so one
author's documents of every type are called publications rather than items.

---

## Ambiguity is always terminal, never a silent guess

Two failure kinds out of the tools are treated as **terminal** — the tool's
own `rendered` clarification or refusal *is* the answer, not a cue to fall
through to semantic search:

- `"unresolved"` / `"ambiguous"` — from fuzzy name matching, terminal **only**
  while `entity_resolution_enabled` is on (that flag's whole job is holding
  matching-quality changes back from user-visible behaviour until evaluated).
- `"ambiguous_entity"` — a content-type word naming several bundles
  (`"projects"`) — terminal **unconditionally**, because this is a curated
  list decided independently of similarity scoring, and every alternative to
  asking is a wrong answer (one type's total reported as if it covered both,
  or the whole corpus miscounted as one type).

Everything else that returns `ok=False` — an unknown entity, a genuinely empty
result, a query exception — keeps the original fall-through behaviour: the
caller (`answer_structured`) returns `None` and the pipeline tries semantic
search next.

### The lookup / count "guessed title" carve-out

`_title_guess_zero` catches one specific way a confident zero used to be
wrong: `title_contains` is `title LIKE '%…%'` over a single column, and the
intent layer fills that slot from whatever subject a question names when
nothing else fits — so "how many reports about quantum teleportation" arrives
as a *title substring* even though the corpus's answer, if any, would live in
body text the tool never searches. Reporting `0` there states the corpus is
silent on a topic when only its titles are. The carve-out is narrow: it does
**not** apply when the question is explicitly about titles themselves
("reports **titled** X", a quoted phrase) — there, a zero is the honest
answer and prose from semantic search would be worse.

---

## The catalog fallback: a listing when semantic search found nothing

`catalog_fallback(question, analysis)` is a *different* entry point from
`answer_structured`, used by the pipeline only after semantic retrieval has
already failed to ground an answer. It forces the plan's operation to `list`
regardless of what the classifier originally said (a count or a breakdown
answers nothing for a question that wanted content), and requires at least
one subject facet already present on the analysis (`theme`, `tags`, `author`,
`title_contains`) — a bundle or a date range alone is not "about" anything, so
offering a bare recency-ordered list in place of an honest refusal would imply
a relevance the rows do not have. It never re-parses the question (a qa
analysis carries no `operation`, and this path has already failed once — an
extra LLM call here would be spent on a query about to be declined anyway).
Returns `None` when there is nothing worth offering, leaving the caller's
existing refusal in place.

---

## Failure scenarios

| Scenario | Detection | Response | Recovery |
| --- | --- | --- | --- |
| Unknown content-type word | `_entity_guard` / `is_known` | `ok=False`, no `error_kind` — falls through | — |
| Content-type word names several bundles | `ambiguous_bundles` | Terminal clarification (`ambiguous_entity`) | User picks one |
| Bundle known but empty in this catalog | `is_available` | `ok=False`, falls through (never a confident zero) | — |
| Author/theme/tag name matches nothing plausible | `resolve.classify_band` = `MISS` | `entity_resolution_enabled` on: terminal "no X matching" / off: filters on the name as typed | — |
| Author/theme/tag name matches two candidates too closely | `classify_band` = `AMBIGUOUS` | `entity_resolution_enabled` on: terminal clarification / off: takes the top candidate | User picks one |
| Theme resolves to something broader than asked | `topic.faithful_theme` fails | Theme dropped; falls back to a topic-term constraint | — |
| Question's subject has no covering facet | `topic.residual_topic` non-empty, `topic.enabled()` | Rows constrained by the residual words; ranked by match count | — |
| Question asks about people, not documents | `topic.wants_person`, no `author` set | Structured path declines outright (`None`) — unless it asks about authorship itself ("which authors published the most", "how many authors"), which is a breakdown or count the catalog records | Semantic retrieval answers instead |
| "How many authors / themes / content types" arrives as a document count | `answerer._counted_noun` | `count_of` set from the noun; an unscoped theme count becomes the theme listing (a distinct count includes sub-theme rows) | — |
| A period the question states ("in 2030") arrives without dates | `answerer._stated_year` | The year is restored, so the count is an honest zero rather than the all-time total | — |
| A named person's profile page would take a count or list of their publications | `query_pipeline._asks_for_authorship` | The page gives way to the catalog for a count or breakdown with an author, or a list or lookup naming a kind of writing ("list articles by X"); "what does X work on" stays the page's | — |
| A person's "articles" read as the Article category alone | `answerer._reads_article_as_writing` | Counted and listed across every type, with the Article category's share stated | Ask for "the article category" for the narrow count |
| `count_of` / `group_by` names an unsupported dimension | `_dimension_or_reject` / a `_GROUP_DIMENSIONS` miss | Refused with an explicit error, never a silent default | Fix the caller/plan |
| Title substring was guessed from the question's subject, count is zero | `_title_guess_zero` | Falls through instead of reporting a corpus-wide zero | Semantic retrieval answers instead |
| Catalog query raises | `except Exception` around every `state.*` call | `ok=False, error="query failed"`, logged | Falls through |
| Multi-call planner fails or returns nothing | `plan_multi` exception or empty | `None`, falls back to v1 `plan` | — |
| Every planned call fails, none terminal | `_terminal_result` finds nothing | `answer_structured` returns `None` | Semantic retrieval is tried next |

## Observability

- Warnings on every guarded failure name the tool and the value that failed
  to resolve (`logger.warning("count_records query failed.", exc_info=True)`,
  etc.), so a query-failure spike is diagnosable from the app log alone.
- `_applied_filters` / `data["applied"]` on every successful `ToolResult`
  states exactly which filters were in effect after canonicalization — a
  structured, machine-checkable counterpart to the human-readable
  `_scope_phrase`, useful for verifying an answer's interpretation
  programmatically rather than only by reading its prose.
- `theme_widened` on `ResolvedScope` is diagnostic-only — it is never surfaced
  to the user, but is available to anything inspecting why a theme was
  dropped from a query.

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `database_multi_call_enabled` | `false` | Opt into the v2 LLM multi-call planner; v1 is otherwise always used. |
| `entity_resolution_enabled` | `false` | Whether an ambiguous/missing name match becomes a terminal clarification (`true`) or is absorbed silently as before (`false`). Matching itself always runs. |
| `structured_topic_constraint_enabled` | `true` | The topic-residual constraint (`topic.py`) that keeps a list from degenerating into "the newest N rows of a large bucket." |
| `catalog_answer_detail_enabled` | `true` | Whether a catalog answer follows its headline with detail (`detail.py`). Off in the test suite unless a test turns it on (`tests/conftest.py`). |

## Hand-off

A successful `ToolResult` is composed by `_compose` into the same
`{"answer", "citations", "intent": "structured", ...}` shape the pipeline
expects from any answer path, and `resolve_lookup_chain` may hand a
`document_id` onward into ordinary content QA. See
`app/pipeline/query_pipeline.py` for where this fits among the pipeline's
other answer routes, and
[08 — Knowledge Graph Retrieval](08-knowledge-graph-retrieval.md) for the
other alternative route this package's caller has to choose between.

---

Cross-references: [docs/ingestion/08 — Persistence and the Catalog](../ingestion/08-persistence-and-catalog.md)
for the table shapes `app.catalog.queries` reads. Several modules in this
package (`planner.py`, `tools.py`, `resolve.py`) point their own docstrings at
`docs/database-tool-registry.md` and `docs/database-retrieval-redesign.md` for
design rationale — neither file exists in this repository today, so treat
those as historical references to design discussions that happened elsewhere,
not as documents to go find.

---

Previous: [06 — Context and Citations](06-context-and-citations.md) · Next: [08 — Knowledge Graph Retrieval](08-knowledge-graph-retrieval.md)
