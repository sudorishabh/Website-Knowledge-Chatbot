# 13 — Web Retrieval

How the read path consults the public web when the corpus cannot answer a
question, and how it keeps doing everything else exactly as before.

Code: `app/retrieval/web/` (the package), `app/retrieval/retriever.py` (the one
doorway), `app/pipeline/query_pipeline.py` (the one flag read). Tests:
`tests/retrieval/web/`, `tests/pipeline/test_web_pipeline.py`,
`tests/evaluation/test_web_benchmark*.py`.

---

## What it is for, and what it is not

The corpus — Drupal pages and their PDFs, ingested with OCR, table extraction
and dated by the ingestion rules — is the authoritative source and stays the
first one. The web is a **fallback** for what the corpus structurally cannot
hold:

- pages ingestion never crawls (an expert's profile: `teriin.org/user/15680`);
- documents published since the last sweep;
- a question that explicitly asks to look something up online.

It is **not** a second corpus. Fetched pages are evidence for one answer; they
are never indexed into Qdrant, catalogued in MySQL, or kept in the answer
cache. Nothing is crawled: only search-result URLs are fetched, and a page's
links are never followed.

**Off by default.** `web_search_enabled=false` hands retrieval `None`, the web
package is never imported (a subprocess test asserts this), and every answer is
byte-identical to before. Enabling it also needs a provider and key — one flag
alone cannot reach the web.

---

## The flow

```
question
  └─ understanding (unchanged)
       └─ _web_plan(pq)            the ONE read of web_search_enabled   (pipeline)
            planner.plan(...)      deterministic signals + queries       (no model call)
  └─ retrieve(..., web=plan)                                             (retriever)
       ├─ forced? (explicit / freshness) ── start service.gather() now, in parallel
       ├─ corpus legs → RRF → rerank → corrective loop                    (unchanged)
       ├─ sufficiency.assess(plan, ranked) → decide()   → trace: web_decision
       ├─ if searching:  service.gather(plan, query_vector)
       │     1 search    site-restricted first; open web as fallback      (providers)
       │     2 sort      in the corpus? → its ingested chunks, no fetch    (corpus)
       │                 TERI page not in the corpus? → recorded as a gap
       │     3 fetch     what is missing, TERI first, ≤4, budgeted          (fetch)
       │     4 read      HTML/PDF → headed, dated, sanitized blocks          (extract)
       │     5 passages  child + section context, corpus-scale scores        (passages)
       │                                                     → trace: web
       ├─ merge_ranked(ranked, web candidates)   one banded ranking       (reranker)
       └─ build_context  (inline section for web passages)                 (unchanged)
  └─ generation: web blocks headed as web; web rule; date rules            (prompts)
  └─ citations: provenance fields; "From the web" group in the UI
  └─ answer cache: bypassed when forced; web-backed answers never stored
```

The insertion point is inside `retrieve`, after ranking and before context
building, because that is where "is the internal evidence enough?" can be
answered, and because it lets web evidence reuse — rather than duplicate —
ranking, de-duplication, parent expansion, the token budget and citations.

---

## When the web is consulted

The decision (`sufficiency.decide`) is never a result count or a score
threshold. Measured on this corpus, "What is TERI SAS" retrieved 0.66-cosine
boilerplate and a cross-encoder scored SASMIRA passages at 0.99 — then the
answer was refused. What was actually wrong is visible without a score: no
passage said "SAS".

| Trigger | Consults the web when | Setting |
| --- | --- | --- |
| `explicit_request` | "search the web", "look it up online", "web search" | always |
| `freshness` | "latest", "most recent", "this year", a year ≥ this one | `web_on_freshness` |
| `no_internal_evidence` | the corpus returned nothing | `web_fallback_enabled` |
| `subject_absent` | the top passages mention < 50% of the named subjects | `web_subject_min_coverage` |
| `no_profile` | a person is asked about and no page is *about* them | — |
| `passage_not_found` | a passage hunt, and no single passage holds 80% of its words | `web_passage_min_coverage` |
| `date_window_uncovered` | nothing is dated in the period asked about | — |
| `period_uncovered:<year>` | a comparison of years, and one year has nothing | — |
| `document_not_found` | a quoted document title is not among the candidates | — |
| `low_relevance` | off until calibrated for the deployment's reranker | `web_min_internal_relevance` |

Explicit and freshness questions are *forced*: the web search starts beside the
corpus pulls, so its latency overlaps theirs.

---

## Internal first, even after a search

A search result whose address the catalog holds
(`catalog.queries.indexed_documents_for_urls`, which tries every spelling —
scheme, `www.`, trailing slash, percent-encoding — against page URLs and
attachment file URLs) is answered from its **ingested chunks** through the
existing `search_within_documents`, and never fetched. Those chunks carry the
corpus's own dates, pages and OCR'd text; they gain
`retrieval_method = corpus_via_web_search:<provider>`.

TERI pages web search finds that the corpus lacks are **gaps**: logged, traced,
counted (`web_corpus_gap` metric) and, with `web_gap_log_path` set, appended to
a JSONL file to review for ingestion. Nothing is ingested automatically.

Measured on the benchmark URLs: the Bridging-the-Gap article, the 2018
executive summary PDF, the 29 June 2026 truck press release and its PDF were all
already in the corpus; the profile page and `AQM-SA_0.pdf` were not.

---

## Searching: the provider abstraction

`providers.WebSearchProvider` has one method, `search(query, *, site, limit)`.
Two adapters ship, both official APIs — search-result HTML is never scraped:

| `web_search_provider` | API | Site restriction |
| --- | --- | --- |
| `brave` | Brave Search (`X-Subscription-Token`) | `site:` operator |
| `tavily` | Tavily (`Authorization: Bearer`) | `include_domains` |

Adding one is a class with `name` and `search`, registered in `_PROVIDERS`.
`providers.search()` adds what every provider needs: the result cache, one retry
on 429/5xx/timeouts, the domain policy (blocked domains, the third-party
switch, unsafe URLs), de-duplication, and a `web` trace event.

**Queries** come from the planner. A question naming the organisation searches
`site:teriin.org` first and the open web only if that returned fewer than two
results; other questions search both at once. A passage hunt sends its content
words, a person lookup `"<name> profile"`, anything else the understanding
rewrite with the request scaffolding removed.

---

## Fetching safely

`fetch.fetch(url)` returns a page or raises `FetchRefused(reason)`:

- **SSRF.** Every URL and every redirect hop passes `safety.check_url`: http(s)
  only, no credentials, ports 80/443, no private hostnames, and *every* resolved
  address must be public (so `2130706433`, `169.254.169.254` and names resolving
  privately are refused). The HTTP client never follows redirects itself.
  Residual risk, stated: a DNS server answering differently at connect time
  (rebinding) is not caught by the check alone.
- **Site policy.** robots.txt per RFC 9309 — 2xx obeyed (including Crawl-delay),
  4xx means no rules, 429/5xx/unreachable means leave the site alone (not
  cached). Requests to a site are spaced (`web_per_host_interval_seconds`); a
  turn further away than `web_max_host_wait_seconds` is skipped.
- **Bounds.** HTML, text and PDF only; `web_fetch_max_bytes` / `web_pdf_max_bytes`
  enforced while streaming the decoded body; one retry for transient failures;
  concurrent requests for one page share one fetch; a 12 s budget per question.

---

## Reading pages

`extract.extract(page)` produces a `WebDocument`:

- **HTML** — only the marked content (`<main>`, `<article>`) when there is one;
  navigation, headers, footers, sidebars, forms, share bars and cookie banners
  removed; **hidden text dropped** (`hidden`, `aria-hidden`, `display:none`,
  `sr-only`). Dates in priority order: JSON-LD, `article:published_time` and
  citation/Dublin Core tags, a `<time>` in the content, then the page's date
  markup (how TERI's Drupal theme shows it: `created--on`). Authors from JSON-LD,
  meta tags, and author/byline field markup.
- **PDF** — through `app.core.pdf_text`, the same page reader and normalization
  ingestion uses (moved to core so both paths share it). Paragraphs close at
  sentence ends; short title-shaped lines become section headings that carry
  across pages; each block keeps its page. The date is the PDF's own metadata,
  labelled `pdf_metadata`. No OCR or Camelot at query time — a scanned page is
  empty, not guessed.
- **Untrusted text** — control and zero-width characters removed; sentences
  addressed to a model ("ignore previous instructions", chat-template tokens,
  `SYSTEM:`) removed and counted. The prompt treats web text as data too.

Pages that ingestion already holds are never read this way — see "Internal
first".

---

## Passages, provenance and ranking

`passages.to_candidates` cuts a document into paragraph-sized passages (the
child), each carrying its section — up to ~600 words — as `context_text` (the
parent, done where the text came from). Passages are embedded with the corpus's
model and its `title › heading` breadcrumb, so a web passage's score means what
a corpus chunk's does. Every passage carries its provenance: `url`, `domain`,
`is_primary_source`, `title`, `authors`, `published_date` + `date_source`,
`retrieved_at`, `retrieval_method`, `search_query`/`search_rank`, and for PDFs
`page_number`/`page_range`. A passage is never dated by when it was fetched.

Web candidates join the corpus's in **one banded ranking**
(`reranker.merge_ranked`, which scores only the new candidates). Authority tiers,
applied only between comparably relevant candidates: corpus canonical 0.90 →
corpus primary 0.75 → **TERI web page 0.60** → corpus attachment 0.35 →
**third-party web 0.20**. Relevance still decides first: a 0.72 profile page
outranks a 0.34 corpus mention.

---

## What the model is told

- Web block headers: `web · organisation's own site (teriin.org) · <title> ·
  published 2025-04-09 (shown on the page) · read 2026-09-23` — never the
  corpus's "page date" label; `third-party site (…)`; `publication date not
  stated`.
- A **web rule**, only when a web block is present: web text is untrusted and
  never followed; third-party claims are attributed to their site; the
  organisation's own source wins when both cover a point; "read" is never a
  publication date.
- For **every** answer, in rule 9: a document's year is not its data's year;
  separate studies, seasons, pollutants and places are never merged, averaged or
  strung into one series; no value is supplied for a year the sources do not
  give; a disagreement over a measured value is shown with both sources.

---

## Citations

`Citation` gained optional provenance fields — `domain`, `authors`,
`published_date`, `date_source`, `retrieved_at`, `retrieval_method`,
`is_primary_source`, `chunk_id`, `score`. A web citation has `type = "web"` and
links to the page (`#page=N` for a PDF). A corpus citation reports no
`published_date`: its only date is the CMS page date, which is not one. The UI
shows web sources in their own "From the web" group with domain, a third-party
marker, and the published or read date.

---

## Caching

| Cache | Key | Lifetime | Backend |
| --- | --- | --- | --- |
| Search results | provider, site, limit, normalized query | `web_search_cache_ttl` (6 h) | Redis, else in-process LRU |
| Extracted pages | canonical URL (and searched, final URL) | `web_page_cache_ttl` (24 h) | same |
| robots.txt | site origin | `web_robots_cache_ttl` (24 h) | same |
| Answers (semantic cache) | — | — | **forced questions bypass it; web-backed answers are never stored** |

The semantic cache's fingerprint includes `web_search_enabled` and the
provider, so switching either self-invalidates it; `PIPELINE_REVISION` was
bumped for the prompt change.

---

## Observability: "why did it use this page?" and "why not?"

Every web-enabled query's trace (`is_retrieval_log=true`) carries:

| Note / event | Answers |
| --- | --- |
| `notes.web_plan` | what the planner read in the question (signals, forced reasons, queries) |
| `notes.web_decision` | searched or not, every reason, and the internal verdict with its signals — including `sufficient: true` when the web was *not* used |
| `notes.web` | provider, each query and its hit count, `in_corpus`, `fetched`, `not_read` with reasons (`robots_disallowed`, `beyond_fetch_limit`, `budget_exhausted`, …), `gaps`, passage counts, latency |
| events `web / search`, `web / fetch` | each provider call and each fetch, with its refusal reason or status, size and final URL |
| `notes.web_corpus_gaps` | TERI pages found on the web and missing from the corpus |

Timing: `rag.web_search` and `rag.web_fetch` roll up into a `web` component,
`rag.web_passages` into `embedding`.

---

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `web_search_enabled` | `false` | master and kill switch |
| `web_search_provider` / `web_search_api_key` / `web_search_endpoint` | empty | `brave` or `tavily`; key; optional endpoint override |
| `web_primary_domains` | `teriin.org` | the organisation's own sites (searched first; primary authority) |
| `web_organisation_names` | `TERI, The Energy and Resources Institute` | names that make a question organisation-scoped |
| `web_allow_third_party` / `web_blocked_domains` | `true` / empty | third-party sites at all; never-used domains |
| `web_on_freshness` / `web_fallback_enabled` | `true` / `true` | freshness forces the web; insufficiency falls back to it |
| `web_subject_min_coverage` / `web_passage_min_coverage` / `web_min_internal_relevance` | `0.5` / `0.8` / `0` | sufficiency thresholds (relevance floor off until calibrated) |
| `web_search_max_results` / `web_max_fetches` / `web_budget_seconds` | `8` / `4` / `12` | breadth and time per question |
| `web_fetch_pdfs`, `web_pdf_max_pages` | `true`, `40` | PDFs not in the corpus may be read, first N pages |
| `web_fetch_timeout_seconds`, `web_search_timeout_seconds`, `web_fetch_retries`, `web_search_retries`, `web_max_redirects`, `web_max_connections` | `8`, `6`, `1`, `1`, `3`, `8` | network bounds |
| `web_fetch_max_bytes` / `web_pdf_max_bytes` | 3 MB / 15 MB | response caps |
| `web_respect_robots`, `web_per_host_interval_seconds`, `web_max_host_wait_seconds`, `web_user_agent` | `true`, `1.0`, `3.0`, named agent | site policy |
| `web_passages_per_document` / `web_max_candidates` | `3` / `12` | passages handed to ranking |
| `web_search_cache_ttl` / `web_page_cache_ttl` / `web_robots_cache_ttl` / `web_cache_max_entries` | 6 h / 24 h / 24 h / 512 | caches |
| `web_gap_log_path` | empty | JSONL file of corpus gaps |

---

## The benchmark

`tests/evaluation/web_benchmark.py` records six questions with their sources
(verified 2026-09-23 against the live catalog, Qdrant and teriin.org), required
facts, date handling, expected web decision and whether several documents are
needed. `test_web_benchmark.py` checks the planner and the decision
deterministically; `test_web_benchmark_live.py` (`-m llm`) runs them end to end.

| Q | Question (short) | Web? | Why | Measured |
| --- | --- | --- | --- | --- |
| T1 | Who is Mr Sayanta Ghosh; publications | yes | `no_profile` — none of 40 corpus candidates mentions him | live: the profile became block [1], "more than 25 publications" in its section |
| T2 | Vehicular share of Delhi PM | no | corpus holds the 2025 article's seasonal figures | four separate facts (winter/summer × PM10/PM2.5); figures are from the 2016-17 study |
| T3 | Transport share ~2019 vs 2025 | no | both years in the corpus | the 2019 figure is on a page whose CMS date is 2018 — publication year ≠ data year |
| T4 | Seaweed / aquafeed / NPK / Asparagopsis paragraph | no | the article is in the corpus and ranks first; passage coverage 0.889 | the brief expected a web search; the corpus answers it |
| T5 | Latest TERI vehicle-pollution work | yes | `freshness` | the 29 June 2026 truck report and policy brief are also in the corpus |
| T6 | Overview 2019–2025 | no | in-window documents in the corpus | multi-document; per-source publication vs data years |

The live runner needs an LLM deployment, and a search provider for T1 and T5.

---

## Known limitations

- **No live provider was exercised in development**: there was no search API
  key. Everything after the search — catalog matching, fetching teriin.org,
  extraction, passages, ranking — was run live with real search-result URLs
  standing in for a provider's response; the adapters are tested against each
  vendor's documented request/response shape.
- **Heuristic extraction.** Main-content and date detection is markup-based; a
  site with unusual markup may yield boilerplate or no date (the trace shows
  it). PDFs are text-only at query time.
- **Lexical sufficiency checks** tolerate inflection, spelling and compound
  variants, not synonyms: a paraphrased passage hunt is absorbed by the 0.8
  share, not understood.
- **The relevance floor is uncalibrated** and therefore off.
- **DNS rebinding** is not caught by the URL check alone (see "Fetching
  safely").
- **Answers built on web evidence are not cached**, so a repeated web question
  pays the web's latency (typically 2–5 s) each time; the page and search caches
  absorb most of it.

## Operating it

1. Choose a provider and set `WEB_SEARCH_PROVIDER` and `WEB_SEARCH_API_KEY`.
2. Set `WEB_SEARCH_ENABLED=true`. Turning it back off is the complete rollback.
3. Set `WEB_GAP_LOG_PATH` and review it: every entry is a TERI page users needed
   that ingestion does not hold — the profile pages above are the first case.
4. With `IS_RETRIEVAL_LOG=true`, read `notes.web_decision` to see why a question
   did or did not reach the web.
