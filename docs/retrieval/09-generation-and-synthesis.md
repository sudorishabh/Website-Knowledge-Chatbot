# 09 — Generation and Answer Synthesis

**Purpose.** Turn a question plus the `ContextBlock`s retrieval selected into
cited prose — and catch, mechanically, the two ways a grounded model still
gets it wrong: a claim the context does not support, and a document dated by
the web page that happens to carry it.

**Inputs.** The search query, a `list[ContextBlock]`, conversation history,
the detected answer format, and (when the question has more than one part) a
plan directive.

**Outputs.** Answer text — buffered or token-streamed — already through
citation-marker validation, an optional one-shot faithfulness correction, a
mandatory publication-date guard, and an unknown-link check.

**Components.** `app/generation/prompts.py`, `answerer.py`, `sections.py`,
`answer_plan.py`, `faithfulness.py`, `redundancy.py`, `date_claims.py`,
`tidy.py`.

---

## Layering: generation never sees a retrieval module

`app/generation` reads `ContextBlock` from `app/core/models/context.py`, not
from any `app.retrieval` module — `ContextBlock`, `GRAPH_FACTS_KIND`,
`is_graph_facts` and `page_span` all live in that neutral core specifically so
retrieval (which builds the block) and generation (which formats and cites it)
can agree on the shape without either importing the other's implementation.
`tests/test_architecture.py` enforces this at the package level: `generation`
sits at layer 6, `retrieval` at layer 5, and an import the wrong way fails the
build.

One exception exists, and it is deliberate and recorded:
`prompts._is_canonical` lazily imports `app.retrieval.search.reranker.
derived_authority` inside a function body to score whether a block is the
organisation's "official page" (§ Rules, below). `ALLOWED_DEFERRED_UPWARD` in
`tests/test_architecture.py` names it explicitly — *"a ranking concept
generation only borrows"* — because a deferred, function-body import creates
no runtime coupling and no import-order constraint, unlike a top-of-file one.
A new upward import anywhere else in `generation` fails the architecture test
until someone records why, the same way.

---

## The grounded prompt: one shape, for every context

`generate_answer` / `generate_stream` use `GROUNDED_SYSTEM_PROMPT`, built once
at import as a pure string constant — assembling it per call would repeat work
on every question for text that never changes.

The answer structure it demands is one answer from all the blocks together,
whatever mix of sources they came from, organised by topic and never by source:
no section labelled by where its material came from, and no sentence about the
material itself ("the context", "the available sources", "an attached report").
Rule text and worked examples both exist because the failure mode is a model
that manufactures a supplementary section and fills it by restating the answer.
It used to say "one continuous answer"; a model read "continuous" as "prose",
and every overview came back as unbroken paragraphs (§ Answer shapes, below).

Naming the page a statement comes from is allowed, and is the one kind of
attribution the answer makes in words: when the answer rests on a block marked
`official page` or `live page`, the opening sentence may say "According to the
organisation's Climate Change page, …".

### What this replaced, and why

There were two prompts, selected by `has_mixed_sources(blocks)`. A context
holding both website and non-website blocks was answered in two wrapped blocks,
`<website_answer>` then `<pdf_answer>` — the second captioned "From our
documents" by the frontend — "always in that order", explicitly "whatever the
relevance scores say". A single-kind context got a second prompt with the
structure stripped out.

Rule 5 of the mixed variant made website content authoritative: when a website
block and a PDF block disagreed, the website statement *was* the answer, never
offered as an equal alternative.

That is the sentence that produced a reported failure. Asked "who is Ajay
Mathur", the model was handed a 2020 web page headed "Statement by Dr Ajay
Mathur, Director General, TERI" and a 2023 PDF saying he "was earlier ... at
TERI as its Director General" — and instructed to take the web page and file the
correction in the aside below it. The reader was left to reconcile two answers
that contradicted each other.

Source kind is now one term of the authority band in the reranker (see
[05](05-ranking-and-temporal-gating.md)) — a tie-break between passages already
judged comparably relevant, comparably well-matched to the period and, on a
question about the present, comparably recent — and nothing more. Rule 5 now
says blocks are weighed on what they say and when they said it, never on what
kind of source they came from, and rule 6 says one answer may cite website
pages, documents and the knowledge graph together.

### Rules 1–9, and what each defends against

The base prompt (`_RULES_HEAD` + `_RULES_SOURCES` + `_RULES_TAIL`) is nine
numbered rules, continued by callers that append more
(history at 10, graph facts after that — see below). Worth reading closely
because each clause exists for a specific observed failure, not as boilerplate:

| Rule | Defends against |
| --- | --- |
| 1 — context only | Outside-knowledge answers |
| 2 — cite every claim, at the end of its sentence or bullet | Unattributable prose. Placement is part of the rule: "after every claim" was applied per phrase, and nine centres listed from one page came back each followed by `[1]`. A list drawn from one block is now cited once, on its opening sentence. Citations stay mandatory because the sources footer shows only the cited blocks (`query_pipeline._cited_blocks`) and the faithfulness check scopes each claim to its citations |
| 3 — the exact refusal string, with five carve-outs | A model that refuses a partial answer the context *does* support, refuses a yes/no it can evidence, refuses a "where do I get X" question because the block names X without narrating a how-to, gives a bare refusal when the context shows something merely *adjacent* to what was asked, or refuses "list the articles where X is mentioned" because it cannot enumerate every article — the first carve-out has it list the retrieved blocks that mention X, one per block with title, date and citation, framed as what the sources include; the kind of document the user named is descriptive, not a filter. Measured 2026-09-18 on six IPCC blocks: as the *last* bullet the same instruction was still refused; as the *first* it produced the list, so its position is pinned by `tests/generation/test_grounded_prompt_rules.py` |
| 4 — no invention | Fabricated sources, URLs, page numbers |
| 5 — website precedence (mixed only) | A PDF version presented as equally true when the website disagrees |
| 6 — one answer, citing any source kind | See above |
| 7 — context is reference material | Prompt injection from inside a passage |
| 8 — no document counts, the "official page" carve-out, and the retrieved-list carve-out | Treating a sample of pages as the whole corpus ("how many reports exist"); the `official page` marker (`CANONICAL_MARKER`, threshold `_CANONICAL_AUTHORITY = 0.85` on `derived_authority`) lets a genuine standing statement — a service catalogue, a themes page — be read as source material rather than over-generalised from |
| 9 — newer-wins, temporal phrasing, and the edition/page-date split | The largest rule by far; see next section |

### Rule 9: dates, precedence, and the edition/page-date split

Four distinct sub-rules live under rule 9, each answering a different
observed failure:

1. **Newer wins**, by the header's "page date" — except when the
   older statement is plainly fuller, or rule 5 (website precedence) already
   settles it.
2. **Time-bound wording is reported as of its source's date, never as of
   now.** A 2023 passage saying "currently" is evidence about 2023; the model
   must write "as of its 2023 report..." rather than copy the present tense
   into an answer that reads as today's fact.
3. **The `official page` marker again**, this time for precedence between
   blocks that merely differ in directness rather than disagree: a 60-word
   service page that states the answer outranks a 400-word announcement that
   alludes to it. Length is explicitly *not* a signal of authority.
4. **The edition/page-date split** — the newest sub-rule and the one with its
   own worked reply format. A block header may carry `edition <period>` (the
   reporting period a document covers) and a `web page date` (when the page
   carrying it went up); for a page holding a whole series — every edition of
   an annual report — that page date belongs to no single document on it.
   *"Annual Report 2024-2025 was published on 9 February 2022"* is named
   explicitly as a false statement assembled out of two true ones, and the
   rule forbids writing it even with a qualifier attached. Asked when such a
   document was published, the model is required to answer in three labelled
   parts:

   ```
   report edition: 2024-25
   page date: 2022-02-09
   report publication date: not stated in the available sources
   ```

   Only the document's own text may fill the third line — never an edition
   label, a PDF `CreationDate`, a cover month-year, an upload time or a URL
   path. This was the read-side mirror of a write-side column that no longer exists
   path (see
   [ingestion 06](../ingestion/06-canonical-document-and-dates.md));
   the field the prompt is told to quote and the field the guard below checks
   are the same one.

Rule 9 is prompted for, but — as measurement showed (§ the publication-date
guard) — prompting alone was not enough, which is why a deterministic check
exists downstream of it.

### The block header: `_source_hint`

Every numbered block is preceded by a header built by `_source_hint`, e.g.
`(website · official page · Mission and Goals · page date 2024-01-15)`.
It is assembled from payload fields, not free text, specifically so the model
cannot be shown a fact the prompt then has no rule to govern:

- `source_type`, and `official page` when `_is_canonical` clears the
  authority threshold.
- `title`, `edition_label` (when set), the page span (via
  `app.core.models.context.page_span` — one definition shared with the
  citation builder, so the header and the citation can never disagree about
  which pages a block stands on), `section_heading`, `contains a table`.
- The date: `page date <date>`. Labelled "page date", never "published",
  because it is the *effective* date of the page the block came from — for an
  event or a project bundle the underlying CMS field is a start date, and for a
  page holding a whole series it belongs to the page rather than to any document
  on it. That labelling is what lets rule 9's answer be assembled without the
  model reaching for the page date as a publication date by default. A
  year-precision `effective_start_date` renders as `"2019 (year only; the day is
  not known)"` rather than a fabricated January day — the same refusal
  `DateInterpretation.statement_is_year_only` makes on the write side (see
  [ingestion 06](../ingestion/06-canonical-document-and-dates.md#the-interpreter-and-its-gates)).
- `link <url>` — last, and only on the organisation's own web pages (a block
  that is `official page` or `live page` and has a `source_url`). It is what
  the "Read more" line a list or an overview ends with is copied from. An
  ordinary article's address is already in the sources footer, and offering
  it here would only invite a link per bullet; a PDF's address never appears.
- The graph's facts block gets its own hint instead — `"knowledge graph ·
  current relationships"` or `"knowledge graph · includes past
  relationships"` — never the generic `(source)` a block with no
  `source_type` would otherwise print.

### Graph facts: a rule appended only when one is present

`has_graph_facts(blocks)` checks `is_graph_facts` (the shared predicate from
`core.models.context`) against every block. When true, `graph_facts_rule(n)`
appends a numbered rule — continuing whatever number history already used —
that exists because the graph block is the one part of the context that
states facts compactly and confidently, with a validity window in
parentheses, which is exactly the shape a model is tempted to paraphrase into
the present tense. Every relationship the graph currently holds has an end
date in the past (see
[ingestion 09](../ingestion/09-knowledge-layer-and-graph.md)), so an untreated
"X is funded by Y" would be wrong for all of them. The rule also forbids
inferring a validity period from a document's publication date, forbids
counting the printed lines when the block already states its own total, and
tells the model the trailing `claim_...` identifier is provenance, not
something to print.

### Format directives

When query understanding detects a desired shape (`list`, `table`, `summary`,
`detailed`, `timeline`), `format_directive` appends a per-format instruction —
plus, for `table` and `timeline`, a short worked exemplar — after the base
prompt. `default`/unknown formats add nothing, deliberately: the base
`_ANSWER_STYLE` guidance already names the shapes, and a directive here can
only narrow the shape further, never loosen it. Each directive is written as
one of those shapes taken further — `list` is the List shape, `detailed` is the
Overview at its fullest, `summary` stays unsectioned — rather than as a second
description of it, because `_SCOPE_NOTE` makes a directive win any conflict
with the style. The old `list` directive ("no preamble", "each bullet leads
with its claim and its citation") contradicted both the list shape and rule 2,
and would have won.

### Answer shapes, formatting and depth

`_ANSWER_STYLE` names five shapes and asks the model to pick the one that fits
the question and the material. Which one applies is the model's reading, never
a list of questions:

| Shape | For | Looks like |
| --- | --- | --- |
| Direct fact | who / when / how many / yes-no — never a question about a subject, even one starting "what is" | The answer in the first sentence, then 2–4 sentences or a few bullets of surrounding detail; no headings |
| List | the members of a set — themes, centres, programmes, people | One opening sentence, then one bullet per item: **name** — the one-line description the context gives it; every item kept, in order |
| Selection | the top, leading or key members of a set the context lists at length (more than about 15) | About 12–15 of them, most senior first, under 2–3 `###` headings by level; a `### By area` index; one closing sentence offering the full listing or one area |
| Overview | "tell me about X", "what is X", "explain X", "how does X work", "why does X matter" — a subject covered from several angles | 2–3 opening sentences, then 3–5 `###` sections, each a different angle the context covers (its kinds or how it works, why it matters, scale and progress, challenges, what the organisation does on it), headed from the material, 2–6 bullets each; draws on every block that adds something; a three-section answer may close with one tying sentence |
| Comparison | several things across several dimensions | A Markdown table |

Four more rules came from the people questions ("TERI top researchers"):

- **A broad question is answered on its named reading.** When a word has no
  measure the context gives ("top", "main", "leading"), the answer takes the
  likely reading, names it in the opening sentence ("If by … you mean …"), and
  draws on every list or page in the context that fits it. The first wording
  ("answer its most likely reading") was read as "pick one set" and dropped a
  whole listing that fitted.
- **"Top" asks for a selection, not the whole set.** Drawing on both listings,
  the next answers printed all 15 fellows and all 35 directors, HR and
  communications posts among them. The selection shape says what to choose by,
  all of it stated in the context: the head of the organisation leads, then
  the heads of areas by the seniority of their titles, with a few fellows
  beside them and never in their place. Only roles that fit the question are
  kept (a researchers question leaves out administration, HR, communications,
  partnerships and business development), and the choice goes one per area
  before a second from any area. Each clause fixed a measured miss. Ranked by
  "distinction", six fellows displaced the Director General. Picked in page
  order, two directors of one area beat an area left out. The head of the
  organisation is always the first bullet and is exempt from the role filter
  by name, because the two rules collided: "researchers leave out
  administration" read the Director General as an administrative post, and a
  live "teri top researchers" answer opened with a senior director. With the
  exemption she led 16 answers in 16. Asking for all, every or the full list
  still gets the List shape.
- **A list of more than about 12 items is grouped** into 2–4 sections by
  something the context states for every item (a title's level, a division,
  the listing it came from). The same question once came back as one flat list
  of 42 names.
- **"Former" is honoured**: a list of current people leaves out anyone the
  context marks as former, past or ended.

Formatting rules ride with them: bold only an item's name and at most one or
two key terms in a paragraph; headings only in an overview and never over a
single point; a description cut off mid-sentence in the context (a live page's
teaser ending "...") is given to its last complete phrase, never finished from
memory; and a list or an overview resting on one page whose header gives a
`link` ends with `Read more: [title](link)`.

They replaced one generic line — "structure anything past a couple of sentences
… **bold** for the points that matter most" — which, measured on 2026-09-25,
produced seven themes run into one sentence with their descriptions dropped,
nine centres listed inline with `[1]` after each, and an overview of four
paragraphs with some forty bold phrases. The model was left to invent a
structure per answer and invented none.

**A question about a subject is never a direct fact.** "What is renewable
energy" and "why is biodiversity important" were read as definitions: a few
lines from two of seven blocks, with 24,000–37,000 characters on the subject
left unused. Measured on 2026-10-04 over twenty general questions, three runs
each on shared blocks, the answers averaged 1,250 characters, 1.6 sections and
two thirds of the blocks cited. The overview now names the angles a subject is
usually covered from, each kept only where the context covers it, and the
reminder tells the opening to say plainly what the subject is even when no
block defines it, never "the sources describe …". That gave 2,150 characters,
3.3 sections and four fifths of the blocks. A blind gpt-5.4 judge preferred
the new answers in 57 of 60 pairs (completeness 3.5 → 4.9, accuracy 4.8 →
4.9) with no more unsupported claims. Over eight controls (direct facts,
lists, the capital of France) it was neutral or better, and the refusal held.

The direct-fact floor ("never a bare clause or a single sentence") stays: an
abstract "be thorough" lost to the model's own pull toward one-line answers.
The reminder repeats it ("never the fact alone", naming the dates, roles,
scope and background to add), because beside "a subject is never one fact"
the one-fact answers shrank to the fact.
So does the anti-padding clause: every added sentence or bullet must rest on a
cited block and say something new, because a shape with sections raises the
temptation to fill them with restatement.

### The worked examples

The model copies the exemplar's shape more than anything described to it —
the single three-sentence paragraph that stood here until 2026-09-25 is the
shape every answer came back in. There are three, all about an invented
organisation ("Org One") so no example can leak a real fact:

1. **An overview** of a programme from a mixed website + PDF context, in
   `###` sections, closing with its `Read more` link — mixed because that is
   the context the model used to split by source.
2. **A list** of centres: one citation on the opening sentence, each item's
   description kept, a truncated teaser cut at its last complete phrase, and
   the one item described from a second block carrying that block's citation.
3. **A role stated at two times** — rule 9's dated-title clause, the one rule a
   model reliably ignores when a block reads like a standing label.

### `today_anchor()`: a fixed "now"

Appended fresh to the system prompt on every call — never baked into the
cached `GROUNDED_SYSTEM_PROMPT` constant, since a long-running process must
not judge "now" against the date it happened to start at. It exists because
rule 9's temporal-phrasing guidance was, measured, inert without it: a
sentence like *"as of 2023, TERI is celebrating its 50th anniversary"* had no
fixed "today" to be judged against, and survived into the answer on some runs
and not others — tracking not the evidence, which never changed, but whether
the model happened to reason its own way to "2023 is in the past" on that
particular call.

---

## The answer call

`answerer.py` is deliberately thin: it assembles the system prompt (base +
history rule + graph-facts rule + format directive + correction + plan
directive + `today_anchor()`), a `MessagesPlaceholder` for history, and one
human turn (`"Numbered context:\n{context}\n\n{notes}Question: {question}\n\n{shape}"`),
then invokes or streams it through `get_llm(temperature=0.2, streaming=...)`.

- **The people-listings note** (`prompts.staff_note`) joins the dates note in
  the `{notes}` slot before the question. It is empty unless two or more
  distinct people listings were read because the question asks for the
  organisation's people (`priority_reason == "staff"`, see
  [13](13-priority-pages.md)); then it names them by block number and says the
  answer draws on all of them, and that the top or leading people are chosen
  across them by seniority and fit, not listing by listing. With both the
  Committee of Directors and the Distinguished Fellows in context and the
  broad-reading rule already in the prompt, three answers in four still named
  only the fellows; with the note, all four drew on both. Its first wording
  ("one group per listing") then had each listing printed whole.
- **The shape reminder** (`prompts.SHAPE_REMINDER`) fills `{shape}`: the
  answer shapes of `_ANSWER_STYLE` compressed to a few lines, after the
  question. The style section sits in the middle of a system prompt of several
  thousand tokens, and on a ten-question live run the small answering model
  ignored it on most answers — the themes still in one sentence, a yes/no
  question in one line. The reminder is the `supersession_note` fix again:
  the end of the human turn is where the model is looking when it starts to
  write. It names shapes, never a question or an organisation, defers to a
  requested format, and hands a publication-date question to rule 9's
  labelled parts by name. It *opens* with rules 1 and 3 — context only, and
  rule 3's exact reply when the context says nothing — because the last
  instruction the model reads outweighs the first: without that line, "what
  is the capital of France" was answered "Paris" in two of three runs on
  identical blocks (one of three with no reminder at all), and with it in none
  of four, while the publication-date question kept its labelled parts in four
  of four.

- **The dates note** (`prompts.supersession_note`) is the first of the `{notes}`, and is
  empty for every context the builder did not flag — which is almost all of
  them, so the human turn is then exactly what it was. When
  `flag_supersession` has marked a pair (see
  [06](06-context-and-citations.md)), it states in one paragraph, by block
  number and date, which block is the most recent account of the subject and
  which are older, and that a role the older ones give in the present tense was
  true as of their own dates. Rule 9 and the header markers say the same thing.
  On the run that still opened with the superseded block, both were in the
  prompt, the block was [6] of six, and the context ran to 30,000 characters;
  this is the same computed fact placed beside the question, where the model is
  looking when it starts to write. No model call and no second classifier —
  every word derives from flags already set — and it names no person or
  organisation.

- **History** (`_history_messages`) collapses roles to human/ai, drops blank
  turns, and keeps the last `HISTORY_MAX_TURNS = 12` messages (~6 exchanges).
  Passed as LangChain message objects rather than interpolated into a
  template string, so curly braces in a prior turn are never re-read as
  prompt variables. `_HISTORY_RULE` (numbered 10, appended only when history
  is non-empty) is explicit that history resolves references ("it", "that
  one") and nothing else — every fact and citation must still come from the
  numbered context.
- **`generate_answer`** returns `REFUSAL` immediately when `blocks` is empty,
  without a model call.
- **`generate_stream`** has no such short-circuit — the caller
  (`query_pipeline`) only reaches it once it already knows blocks exist.
- **`chitchat(question, history)`** is a separate, ungrounded path for small
  talk / meta questions, using `CHITCHAT_SYSTEM_PROMPT` and no context at all.

`format_context_blocks(blocks)` renders the blocks into the human turn's
`{context}` slot, in ranked order, each under its own `[n]` header. It used to
group consecutive same-kind blocks under `— TERI website —` / `— PDF
documents —` headings whenever the context had been segregated by source;
announcing that grouping to the model was half of what made it answer in two
parts, and the graph's facts block — which carries no `source_type` by design —
fell to the "not website" branch and was introduced as PDF content. The headings
went with the segregation. Each block still names its own kind in its own
header, which is what weighing the evidence needs.

A block flagged by `flag_supersession` (see
[06](06-context-and-citations.md)) carries one extra note in that header: either
"a later source below describes this as past" or "dates an earlier statement
above". Rule 9 tells the model what to do with each.

---

## The evidence-coverage plan (`answer_plan.py`)

Solves a specific, measured failure distinct from unfaithfulness: retrieval
can succeed completely and generation can still under-deliver. One logged
case retrieved the correct, authoritative Mission and Goals page, and the
answer stated the mission but silently dropped twelve stated goals and six
values present on the same page — the evidence was there, but the generic
"answer factually, in as much depth as the context genuinely supports"
instruction gave the model no reason to notice it had left two of three
asked-for things on the table.

Two steps, run in parallel with retrieval so the plan adds no serial latency:

1. **`extract_requirements(question)`** — one structured LLM call (same shape
   and cost as the query-understanding call already made per request) that
   lists the distinct, separately-answerable things the question names, as
   short noun phrases. Explicitly told to return exactly one item for a
   single-subject question however long its wording, and never to invent a
   sub-part the wording does not name. Fails open to `[]` on any error, which
   is a no-op everywhere downstream.
2. **`build_plan(requirements, blocks)`** — lexical only, no second model
   call: a requirement counts `supported` when any of its content words
   (stopwords and short tokens filtered) appears anywhere in the
   concatenated block text. Deliberately permissive — a bad match's failure
   mode is "the directive says nothing" (silent, identical to not having a
   plan), never "the directive wrongly tells the model to disclaim something
   the text actually covers." A single requirement short-circuits to
   "supported, no directive": the ordinary case (one subject) is left exactly
   as the base prompt already handles it.

`plan_directive(plan)` renders nothing when there are fewer than two
requirements — the common case — so this feature can only ever *add*
instruction for a genuinely multi-part question; it cannot regress a question
the base prompt already handles. When it does fire, it names what's covered
(push to answer every supported part, not just the first) and what is not
(say plainly the material does not specify it, never invent it, never
generalise in its place) — and, when at least one part is supported, an
explicit instruction not to refuse the whole question over the unsupported
part. `AnswerPlan.evidence_blocks` is retained only for callers that want to
inspect or log the plan; the directive text is derived purely from the
`supported`/`unsupported` lists, never block content, so this stage cannot
introduce a fact generation did not already have.

---

## Post-generation verification: faithfulness

`faithfulness.py` runs **after** the answer has already streamed to the
client at full speed — verification never blocks the first token. It is a
claim-level check, not a holistic grade, because a general-purpose model is
measured to be unreliable as a holistic grader but strong at scoped binary
verdicts:

1. `_extract_claims(answer)` — one structured call, splitting the answer into
   atomic claims, each keeping the `[n]` citations it was written under.
2. `_claim_supported(claim, evidence)` — one binary `supported: bool` verdict
   per claim, run in parallel (`ThreadPoolExecutor(max_workers=4)`) against
   only the blocks the claim actually cited (or, uncited, every block).
   `supported=true` requires the passage to state or directly entail the
   claim, with **numbers, dates and names matching exactly** — the two
   worked examples in `_SUPPORT_SYSTEM` are deliberately close misses
   ("announced" is not "opened").

**Fails open at every stage**: extraction failure, an empty claim list, or a
per-claim exception all resolve to `faithful=True`. Gated by
`faithfulness_check` (default on); when it fires and finds at least one
`supported=False` verdict, `query_pipeline` regenerates **once**, with
`report.correction_note()` appended as a `correction` to the system prompt.
The correction note deliberately does not restate the answer's structure —
it points back at "the answer structure required above," so a rewrite of a
single-source answer is never told to preserve blocks it never had. If the
regeneration itself raises, the streamed (unverified) answer is kept rather
than losing the response entirely. A successful correction is emitted to the
client as a distinct `{"type": "correction", "reason": "faithfulness"}` SSE
event, not a silent replacement — so a client that cares can show that the
answer changed.

Two more checks in this module are deterministic and **observational only** —
neither blocks nor corrects anything:

- `citation_coverage(answer)` — fraction of sentences carrying at least one
  `[n]` marker.
- `numeric_mismatches(answer, blocks)` — numbers appearing in the answer but
  in none of its cited blocks (or, uncited, none of the blocks at all).
  Percent signs and thousands separators are normalised away to keep false
  positives low. A block is its header as well as its text: the model is
  shown each block's title, edition and page date, and rule 9 asks it to
  date its claims by them. Each number has one spelling ("05" and "5", "95.40"
  and "95.4"), an abbreviated year range is read as its years ("2022–23"),
  and a decimal also counts as its whole part (a footnote marker run into a
  year, "2031.6"). The UI shows the flag as "Some figures in this answer
  could not be verified". Reading the text alone, it fired on 55 of 196
  answers measured on 2026-10-04, and all 87 figures it flagged were in the
  context. With these changes it fired on 2 of 364.

`validate_markers(answer, n_blocks)` is unconditional and cheap: it strips any
`[n]` whose `n` falls outside `1..n_blocks` — a model citing a block that
does not exist — and is applied to every answer, correction or not.

---

## Post-generation verification: the publication-date guard

`date_claims.py` exists because rule 9's edition/page-date instruction, on its
own, was not enough: even with the prompt rule *and* the header's separated
date fields in place, **4 of 6 sampled answers** to "When was the 2024-25
annual report published?" still said "published on 9 February 2022" — the
Drupal page's date, not the report's. Runs unconditionally, regardless of the
`faithfulness_check` setting: this is one specific false claim, not a
judgement call, so it is checked rather than merely requested.

Ingestion has since stopped *producing* that date — a PDF sharing its page is
now dated from its own name or left undated (see
[ingestion 06](../ingestion/06-canonical-document-and-dates.md)), so the 2024-25
report carries 2024 rather than its shelf's 2022 stamp. This guard stays: it
catches the model inventing the page date from the header even when the block
never carried it, which is a different failure from the one ingestion fixed.

`verify_date_claims(answer, blocks)` looks only at blocks carrying
`edition_label` (§ page dates, below) and flags two distinct failures per
offending sentence:

- **Conflation** — a sentence whose subject is a document-ish noun (`report`,
  `edition`, `document`, `publication`, `brochure`, `factsheet`, `paper`,
  `brief`, or a `YYYY-YY` span) paired with a `published`/`released`/`issued`
  verb and a date matching one of the context's page dates. Wording that
  correctly attributes the date to the *page* (`_PAGE_SUBJECT`: "the web page
  carrying it was published on...") is explicitly exempted — the check flags
  the false claim, not the true one sitting next to it.
- **Mis-attribution** — the sentence cites `[n]`, but block `n` does not
  itself carry the claimed date. This was the shape of every observed
  failure: the answer cited the FCRA Financials block (dated 2018-04-04)
  while quoting 2022-02-09 sourced from a different block. A citation-blind
  check would have passed it.

A page date is deliberately anchored to `effective_start_date`, never
a document-stated date — the comment in `_block_date` is explicit that the
guard reads the page date only. A document-stated date was modelled once as
"when was this published," and treating it as forbidden would invert the
guard, rewriting *correct* answers and admitting wrong ones.

`_parse_dates` recognises ISO, `D Month YYYY`, `Month D, YYYY` and numeric
(`DD/MM/YYYY`, tried both orderings) forms, so a model paraphrasing the same
date in a different format is still caught.

On a hit, `query_pipeline` regenerates once with
`report.correction_note()` (which names the offending claim and prescribes
the exact three-line labelled answer from rule 9); if the recheck on the
regenerated text is still not clean — or the regeneration call itself raised
— `safe_rewrite(answer, recheck)` performs a **mechanical, non-LLM**
replacement of each offending sentence with the labelled template, using the
first known edition/page-date pair. This is the one place in generation where
correctness is enforced by string substitution rather than another model
call: the guard's job is that the claim cannot reach a reader, not that it is
merely usually absent. Either outcome emits its own `correction` SSE event
(`reason: "date_claim"` or `"date_claim_fallback"`).

---

## Post-generation verification: unknown links

`faithfulness.strip_unknown_links(answer, blocks)` runs last, after both passes
above, because either rewrite can itself introduce a link. It unlinks every
Markdown link — the one form the frontend turns into an anchor — whose address
the context never showed: not a block's `source_url` / `file_url`, and not
written anywhere in a block's text (a live page names the documents it links to
as "Title (https://...)"). The label stays as plain text, so the sentence still
reads. Matching ignores case and a trailing slash or full stop.

It exists because the answer style now asks a list or an overview to end with a
link to its page. Rule 4 already forbids inventing a URL, and the header gives
the real one, but a model asked for a link will sometimes supply a plausible one,
and a plausible link to the wrong page is worse than none.

## Post-generation tidying: list citations (`tidy.py`)

`tidy.tidy_lists(answer)` runs straight after the link check and corrects three
habits the prompt asks against and the small answering model keeps anyway —
measured on identical blocks with every rule above in place:

- a list drawn from one block cites it on the opening sentence *and* on every
  item. When every item carries the same single `[n]`, the citation moves to
  the opening sentence (added there if missing) and leaves the items. A list
  whose items cite different blocks, or several, is left alone. A list under a
  heading has no sentence of its own to carry the citation. When its items are
  named items (`- **Name** — ...`) sharing a single `[n]`, the marker moves to
  the answer's opening instead, which gains it if it lacks it. That is the
  grouped selection, which otherwise carried fifteen `[2]`s below an opening
  that cited `[1][2]`, or nothing at all. An answer that opens with a heading
  keeps the markers. So do an overview's bullets under a heading, which are
  claims. Either way the marker goes before the sentence's closing colon or
  full stop (`listed [2].`);
- an item with no description keeps the dash that would have introduced one
  (`**Name** — [1]`); the dash goes. Only an em or en dash: names carry
  hyphens;
- the `Read more` line carries a citation; it goes, the link being the source.

Nothing here changes what the answer claims, and a clean answer comes back
byte-for-byte unchanged, so it never costs a correction. The opening sentence
always keeps the citation, so the sources footer sees the same blocks. The two
passes share one `correction` event: `reason: "unknown_link"` when a link was
removed, otherwise `"list_citations"`.

Order relative to faithfulness matters: the date guard runs **after** the
faithfulness pass and reads whatever text that pass left behind (`strip_tags`
applied fresh each time) — a faithfulness correction can itself introduce a
publication-date conflation, so checking on the pre-faithfulness text would
miss it.

---

## The retired two-block structure: `sections.py`

Parses a raw answer carrying `<website_answer>`/`<pdf_answer>` tags into an
ordered `list[Section]`, plus `strip_tags`, which is what the verification
passes above actually use — they reason about claims, not presentation, so tags
never reach the LLM-facing checks.

**Nothing asks a model for those tags any more** (see "What this replaced"
above), so a live answer arrives untagged and every function here falls through
to its plain-text branch. It is kept because the semantic cache holds answers
generated under the old contract, and one served from there still arrives
wrapped — unparsed, it would render its tags as literal text. `ui/script.js`
mirrors these rules for the same reason; retire the two together once the cache
has turned over.

Parsing is deliberately **tolerant**: the tags came from a model, not code,
and a stream can be cut mid-tag, so a malformed or missing wrapper degrades to
plain text rather than losing the answer. Behaviour worth knowing:

- **Refusal handling is content-aware.** A block holding nothing but the
  refusal is dropped once *any* other section carries real content (kept in
  full it would sit beside an answer and read as a denial of it); when
  nothing anywhere carries content, the refusal is returned once, as a single
  plain section.
- **A lone PDF block is demoted to plain prose.** With no website block
  beside it, the split has nothing left to set apart — a PDF section with no
  sibling would render as a captioned aside wrapped around the whole reply,
  so `_without_lead` also strips the `**From our documents**` caption line
  that only made sense as a label under a container.
- Website content always precedes PDF content in the returned list,
  regardless of which order the model emitted the tags in, and repeated tags
  of one kind are merged into a single section.

**This module is currently unwired in the read path.** `strip_tags` is used
(by `faithfulness.py` and `date_claims.py`, and directly in
`query_pipeline.py`); `split_sections` itself has no caller in `app/` outside
its own tests (`tests/generation/test_answer_sections.py`) — the module's own
docstring states the intent ("the frontend parses the same sections out of
the answer it renders"), i.e. the frontend is expected to do its own
equivalent parsing of the raw tagged text it receives over SSE, rather than
call this function. Worth confirming against the current frontend
implementation before relying on this doc as a guarantee that both parsers
agree.

---

## PDF redundancy filtering: built, tested, not currently called

`redundancy.py` computes, in pure Python with no embeddings and no model
call, how much of a PDF block's text is already stated by the website block,
and returns the PDF text with the restated parts removed
(`filter_pdf_text`). The grounded prompt already *asks* the model to drop a
PDF block that only restates the website answer (rule under `_MIXED_STRUCTURE`
in `prompts.py`), but that is a judgement call left to the model; this module
was built to decide it deterministically instead.

Design choices worth knowing, since they read as unusual for a text-overlap
filter:

- **Coverage is asymmetric**, not Jaccard: it is the share of a PDF
  sentence's own content words also present in *one* website sentence. A
  symmetric measure would score a short PDF restatement against a long
  website paragraph as barely similar (the paragraph's extra words count
  against it) and fail to filter the repeat.
- **Each PDF sentence is scored against website sentences one at a time**,
  never against their pooled vocabulary — pooling would let a genuinely new
  PDF sentence look "covered" because its words happen to be scattered across
  several unrelated website sentences.
- **Every rule leans toward keeping text when unsure** (`DEFAULT_COVERAGE =
  0.8`; negation words are deliberately kept out of the stopword list, so "X
  supports SSO" and "X does not support SSO" are never folded onto the same
  token set) — dropping a sentence the reader needed is treated as strictly
  worse than leaving a mild repeat on screen.
- List filtering is per-item (`_filter_list`): a partly-redundant list loses
  only the items that repeat, with wrapped continuation lines following their
  item. Prose filtering is all-or-nothing per paragraph, because excising
  sentences from the middle of a paragraph leaves dangling references ("This
  also means...") pointing at text that is no longer there.

**No caller in `app/` invokes `filter_pdf_text`** outside its own tests
(`tests/generation/test_pdf_redundancy.py`); the model-driven instruction in
the prompt is, as of this doc, the only mechanism actually filtering PDF
redundancy on the live path. Treat this module as a tested, ready-to-wire
component rather than an active stage of generation.

---

## Validation at this stage

| Check | Where | On failure |
| --- | --- | --- |
| Citation targets a real block | `validate_markers` | The `[n]` marker is stripped |
| Claim is entailed by its cited evidence | `faithfulness.verify` | One regeneration with a correction note; streamed answer kept if the retry fails |
| A number in the answer appears in cited evidence | `numeric_mismatches` | Reported only — no correction |
| A document is not dated by its page's date | `date_claims.verify_date_claims` | One regeneration, then a mechanical sentence rewrite if the regeneration doesn't clear it |
| A link's address was shown in the context | `faithfulness.strip_unknown_links` | The anchor is removed and its label kept; a `correction` event carries the result |
| A single-source list is cited once | `tidy.tidy_lists` | The repeated `[n]` moves to the opening sentence; an empty description's dash is dropped |
| Requirement extraction succeeds | `extract_requirements` | Fails open to `[]` — no plan directive |
| Context is non-empty | `generate_answer` | Returns `REFUSAL` with no model call |

## Failure scenarios

| Scenario | Detection | Response | Recovery |
| --- | --- | --- | --- |
| LLM call fails during the main answer | Exception propagates | The streaming caller sees the exception (not swallowed here) | Caller-level handling in `query_pipeline` |
| Faithfulness claim extraction fails | `except` in `verify` | `faithful=True` (fails open); no correction attempted | — |
| Faithfulness correction regeneration fails | `except` in `query_pipeline` | Streamed (unverified) answer is kept | Next query |
| Date-claim correction regeneration fails | `except` in `query_pipeline` | Falls back to `safe_rewrite`'s mechanical replacement | — |
| Requirement extraction (`answer_plan`) fails | `except` in `extract_requirements` | Empty plan; no directive added | Next query |
| A model cites a nonexistent block | `validate_markers` | Marker silently stripped | — |
| Model emits a malformed or truncated tag | Tolerant regex in `sections.py` | Degrades to plain text | — |
| Model manufactures a section on a single-source context | Nothing automatic — prompted against, not checked | — | Prompt-level; not covered by a deterministic guard |

## Observability

- `rag.faithfulness` span, `faithful` attribute set from the verdict.
- Log lines: `"Streamed answer flagged unfaithful; correcting once."`,
  `"Answer dated a document by its page; correcting once (%d claim(s))."`,
  `"Replaced %d unsafe publication-date sentence(s) after a failed
  regeneration."`.
- The `correction` SSE event's `reason` distinguishes the guards:
  `faithfulness`, `date_claim` / `date_claim_fallback`, `unknown_link` and
  `list_citations`, so
  a client or a retrieval-log trace can tell which check fired without parsing
  the log.
- Per-query retrieval-log trace (`is_retrieval_log=true`) captures the
  rendered context string handed to the model
  (`retrieval_log.note_context(gen.blocks, rendered=...)`) and whether a plan
  directive was in force (`plan_directive=bool(...)`) — see
  [11](11-observability-and-logging.md).

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `faithfulness_check` | `true` | Whether the claim-level verification pass and its one-shot correction run. |
| `azure_openai_model` / `llm_structured_temperature` | — | The structured calls behind claim extraction, claim support, and requirement extraction. |

The publication-date guard (`date_claims`) has no on/off setting — it runs
unconditionally, on the reasoning stated in its own module docstring: this
failure survived two rounds of prompt-only fixes.

## Hand-off

Once an answer clears both post-generation checks (or exhausts its one
correction each), `query_pipeline` caches it via `app.cache.semantic_cache`
keyed on the corpus revision at that moment — see
[10](10-caching.md) — and streams the final tokens (or correction event) to
the client.

---

Previous: [08 — Knowledge Graph Retrieval](08-knowledge-graph-retrieval.md) · Next: [10 — The Semantic Answer Cache](10-caching.md)
