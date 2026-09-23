# Post-ingestion audit — production corpus

**Audited:** 2026-09-13 · **Ingestion run:** 2026-09-11 05:24 → 16:01 UTC (10h 37m)
**Method:** read-only inspection of live MySQL, Qdrant and Neo4j. No code changed,
no production data modified, nothing re-ingested, nothing deleted.

---

## 1. Executive summary

**Verdict: YELLOW — the document corpus is sound and usable for retrieval; the
knowledge graph is empty and must be built before any graph query is served.**

The ingestion itself completed correctly and to a high standard. 12,140 documents
are catalogued, 12,057 are indexed, and **MySQL and Qdrant agree on every field
for all 12,057 documents except one precision marker on 62 of them.** Dates,
titles, bundles, versions and end-dates reconcile exactly. Chunking is clean.
Retrieval date filtering works, including period overlap and year precision.

Three things are not right, and only the first is serious:

1. **The knowledge graph was never built.** The entity store is empty
   (`documents_entity` = 0), so all 140,774 resolution decisions came back
   UNRESOLVED, zero claims were produced, and Neo4j holds 8 Predicate nodes and
   **zero relationships**. The post-ingestion steps `scripts.seed_entities
   --rebuild` and `scripts.build_knowledge` were not run.
2. **The corpus is one payload version behind the code.** 12,057 documents sit at
   `c1.i1.p2.e1`; the code now declares `c1.i1.p3.e1`. Two date fixes landed
   *after* the ingestion finished.
3. **62 month-precision documents lost their precision marker in Qdrant**, caused
   by a bug in `scripts/backfill_bundle_dates.py` — not by the ingestion pipeline.

Nothing found suggests the ingested document data is untrustworthy.

---

## 2. Corpus completeness

Every source document seen is accounted for:

| | count |
|---|---|
| distinct documents in `ingest_log` | **12,240** |
| documents in the catalog | **12,140** |
| — indexed (have points) | 12,057 |
| — metadata-only (catalogued, no points) | 83 |
| not catalogued — build/download returned nothing (`skipped`) | 65 |
| not catalogued — extraction produced 0 chunks (`error`) | 35 |

`12,057 + 83 + 65 + 35 = 12,240` — exact reconciliation, no unexplained losses.

**Pages vs PDFs:** 8,631 website · 3,509 pdf_attachment.

**By bundle:** events 2,122 · feature_articles 1,946 · completed_projects 1,908 ·
news 1,667 · press_release 1,033 · ongoing_projects 844 · research_papers 651 ·
article 559 · policy_brief 522 · page 455 · videos 224 · basic 105 ·
infographics 49 · services 30 · report 17 · people 8.

**Other states:** 0 documents missing a crawl position · 61 dead links ·
100 rows in the retry queue (65 skipped, 35 error) · 0 knowledge runs
retry-eligible · 83 documents with no knowledge-run row (the metadata-only set).

The 83 metadata-only documents are all `completed_projects` that previously
failed with *"extraction produced no indexable content"*. Commit `f7069c3`
("catalog an empty Drupal page that carries attachments") now catalogues them
deliberately. **Expected behaviour**, not a defect.

---

## 3. Pipeline / version state

| | |
|---|---|
| current code version | **`c1.i1.p3.e1`** |
| documents at `c1.i1.p2.e1` | **12,057** |
| documents at `c1.i1.p3.e1` | 83 |
| **stale vs current code** | **12,057** |
| points carrying `c1.i1.p2.e1` | 153,370 (100%) |
| duplicate live versions | **0** |
| catalog ↔ Qdrant version disagreement | **0** |

The catalog and the payloads agree perfectly — the corpus is *internally*
consistent at p2. It is only *externally* stale, because `PAYLOAD` was bumped
2 → 3 after the run by two commits that change how an attached PDF is dated:

> *"on a page holding several PDFs the file's own name states its date, and where
> nothing states one and the page is dated only by its Drupal creation stamp the
> file is left undated. Measured on the live corpus, 164 attachments take a date
> from their name and 334 lose one they should never have had."*

A partial backfill has already applied much of this (602 catalog rows updated
after 2026-09-12; 321 documents now `no_evidence`, 174 `document_title`), which
is why the corpus already reflects p3 *values* while still carrying the p2
*stamp*. **This is a version-bookkeeping mismatch, not data corruption** — but it
means a future `reprocess_corpus` will consider all 12,057 stale and rebuild them.

---

## 4. Date quality

Dates are the strongest area of the audit.

| check | result |
|---|---|
| end before start | **0** |
| year precision not on 1 January | **0** |
| month precision not on the 1st | **0** |
| precision without a date | **0** |
| NULL `date_source` | **0** |
| off-vocabulary `date_source` | **0** |
| end dates on bundles that declare none | **5** |

**`date_source` distribution:** cms_field 5,849 · parent_page 2,855 · created
2,782 · no_evidence 321 · document_title 174 · document_copyright 159.
All within the declared vocabulary
(`created, cms_field, parent_page, document_text, document_copyright,
document_title, no_evidence`).

**Precision:** day 10,873 · year 884 · month 62 · none 321.
The 321 without a date are all `pdf_attachment` with `date_source='no_evidence'`
— the deliberate p3 outcome of refusing to inherit a bare creation stamp.
MySQL and Qdrant agree exactly on which 321 those are.

**End dates:** 3,937 total — events 2,100, completed_projects 1,832, plus **5
strays** on `page`(2), `article`(2), `ongoing_projects`(1). All five are
`pdf_attachment` inheriting `parent_page`. One is questionable —
`article 2020-02-05 → 2021-05-30`, a 15-month span on an article PDF.
*Harmless legacy state, low impact, 5 of 12,140.*

**Date decisions recorded:** 9,402 rows (website 5,892 · attachment 3,022 ·
inbody 488), so provenance is auditable per document.

---

## 5. Multi-PDF quality

| | count |
|---|---|
| pages carrying attachments | 2,510 |
| single-PDF pages | 2,073 |
| **multi-PDF pages** | **437** |
| attachment links | 3,726 |
| largest page | **72 PDFs** |

**Distribution:** 2 PDFs ×277 · 3 ×59 · 4 ×31 · 5 ×17 · 6 ×11 · 7 ×9 · 8 ×5 ·
9 ×3 · 10 ×6 · 11 ×3 · 12 ×2 · … up to 72.

**PDF date rules applied:** `parent_bundle_date_field` 1,364 ·
`single_pdf_page` 618 · `multi_pdf_uploaded_with_page` 513 ·
`multi_pdf_no_evidence` 485 · `title_states_date` 174 ·
`copyright_statement_corroborated` 159 · `llm_interpreted` 83 ·
`migration_cohort_no_evidence` 62 · `multi_pdf_url_month_matches` 52.

**Decided by:** deterministic 3,427 (97.6%) · model 83 (2.4%).

Of 421 multi-PDF pages with dated files, **83 carry different dates per PDF** and
338 share one. The shared ones are correct, not lazy inheritance — for example a
page of 18 PDFs titled *"Chapter 5 – Industry Sector"*, *"Chapter 11 – Mining
Sector"*, *"Executive Summary"* is one report, and its chapters legitimately share
its date. 22 pages have ≥6 PDFs all sharing a date; every one inspected is a
chaptered report.

Genuine per-PDF differentiation is visible and evidence-backed:

```
page 04107bb4…  2020-01-01 year document_copyright  TERI Alumni Association
                2022-01-01 year document_copyright  TERI Alumni Association
                (none)     day  no_evidence         TERI Alumni Association

page 12736819…  2022-11-10 day  document_title      Background Note
                2022-12-02 day  parent_page         Circular Economy: Eco Design
```

**No suspicious pattern found.** Only 83 of 3,510 attachment decisions used the
model, and 41 decisions are flagged `needs_manual_review` rather than guessed.

---

## 6. Chunking quality

| | value |
|---|---|
| child chunks | 125,641 |
| documents with children | 12,057 |
| chunks/document | p50 1 · p90 14 · p95 44 · p99 176 · max 2,221 |
| chunk size (chars) | p50 1,427 · p90 2,277 · p95 2,444 · p99 2,789 · max 8,821 |
| empty chunks | **0** |
| chunks > 20k chars | **0** |
| chunks < 40 chars | 24 |
| duplicate text within one document | 258 (0.2%) |

`section_type` classification is active: references 5,771 · toc 1,297 · glossary 11.

No truncation, no runaway chunks, no empty text. The 258 intra-document duplicates
are almost certainly repeated PDF headers/footers — negligible.

---

## 7. Qdrant audit

| | value |
|---|---|
| collection | `documents` (3072-dim, Cosine) |
| points | **153,370** |
| parent / child | 27,729 / 125,641 |
| distinct documents | **12,057** |
| duplicate chunk ids | **0** |
| dangling `parent_chunk_id` | **0** |
| points with empty text | **0** |
| boolean `"True"` category leakage | **0** |
| empty facet lists | **0** |
| payload indexes | **17 declared, 17 present, 0 stray** |
| `pipeline_version` on points | `c1.i1.p2.e1` ×153,370 (uniform) |
| `embed_model` | `text-embedding-3-large:3072` on all 125,641 children |

**Required-field coverage:** `document_id`, `chunk_id`, `is_parent`,
`is_current`, `source_type`, `bundle`, `title`, `pipeline_version` — **0 missing**.
`effective_start_date` absent on 13,497 points (321 documents), which matches
MySQL exactly and is the intended `no_evidence` outcome.

6,496 documents have child points but no parent point. This is the chunker's
contract, not an orphan: 86% of them have a single chunk, and a parent exists only
to group multiple sections.

`date_source` is **not** a payload field by design — provenance is a catalog
concern, not a retrieval filter. Its absence from all points is correct.

---

## 8. MySQL audit

| table | rows |
|---|---|
| documents | 12,140 |
| documents_attachment | 3,726 |
| documents_author | 4,696 |
| documents_tag | 22,910 |
| documents_theme | 11,106 |
| documents_date_decision | 9,402 |
| documents_entity_mention | 140,774 |
| documents_entity_resolution_decision | 140,774 |
| documents_entity_extraction | 100,933 |
| documents_knowledge_run | 12,057 |
| ingest_log | 12,445 |
| **documents_entity / _alias / _identifier** | **0 / 0 / 0** |
| **documents_assertion / _rejection / _link** | **0 / 0 / 0** |

**Integrity:** 0 invalid `doc_version` · 0 duplicate live versions · 0 orphan
knowledge runs · 0 orphan attachment links · **33 date-decision rows referencing a
document that no longer exists** (minor, harmless).

**Legacy columns are gone** — no `published_at*` column remains on `documents`.
The date migration completed cleanly.

Date-decision rows are being written and read correctly (9,402 rows with rule,
action, evidence, confidence and `decided_by` all populated).

---

## 9. Neo4j audit — **empty**

| | count |
|---|---|
| total nodes | **8** (all `Predicate`) |
| total relationships | **0** |
| Document / Person / Organization / Project / Claim nodes | **0** |
| AUTHORED · PARTNER_OF · FUNDED_BY · LED_BY · HAS_ROLE · WORKS_AT · MEMBER_OF | **0 each** |

Schema and indexes are correct and ONLINE, including
`document_effective_start_date` on the canonical field and `claim_*`,
`entity_*`, `alias_*`. **No stale index** — the obsolete `document_published`
is gone.

The graph is structurally ready and contains no data.

---

## 10. Knowledge / LLM audit

| | value |
|---|---|
| knowledge runs | 12,057 — **all `ok`** |
| partial / failed / retryable | **0 / 0 / 0** |
| documents with no run row | 83 (metadata-only) |
| mentions extracted | 140,774 |
| entities auto-resolved | **0** |
| entities unresolved | **140,774 (100%)** |
| claims built / staged / rejected | **0 / 0 / 0** |
| LLM calls | **0** |
| model failures | 0 |
| call-limit / time-budget / per-document stops | 0 / 0 / 0 |
| runs carrying errors | **0** (`errors = '[]'` on all 12,057) |

**Root cause:** `documents_entity` is empty. `CmsClaimContext` and the resolver
both build from the entity index; with no entities, every mention resolves
UNRESOLVED, no chunk becomes claim-eligible, the LLM extractor is never invoked,
and no CMS claim finds a subject.

**Are there silent successes?** No. Every run genuinely had nothing to do and
reported it honestly with an empty error list. Mention extraction and resolution
both ran at full corpus scale (140,774 each), so the machinery works — it was
starved of its input.

**Is useful information lost?** Yes, but recoverably and without re-ingestion:
the raw material (140,774 mentions, 4,696 author rows, the CMS partner/sponsor/PI
fields) is all in MySQL. Running the two global passes will build the graph.

---

## 11. Graph coverage vs projection

| relationship | projected pre-ingestion | actual |
|---|---|---|
| FUNDED_BY (CMS) | ~956 | **0** |
| LED_BY (CMS) | ~416 | **0** |
| AUTHORED (CMS) | 802 surviving validation | **0** |
| PARTNER_OF (CMS) | 59 | **0** |
| HAS_ROLE / WORKS_AT / MEMBER_OF (LLM) | ~280 | **0** |
| documents with ≥1 relationship | ~1,100 | **0** |

Nothing unexpected was created and no entity was invented — the safety properties
hold trivially, because nothing was created at all.

---

## 12. Cross-store consistency

Full comparison of all **12,057** indexed documents, MySQL ↔ Qdrant:

| field | mismatches |
|---|---|
| document identity (present in both) | **0** |
| title | **0** |
| bundle | **0** |
| source_type | **0** |
| pipeline_version | **0** |
| effective_start_date | **0** |
| effective_end_date | **0** |
| end_precision | **0** |
| **start_precision** | **62** |

0 documents in Qdrant but not the catalog; 0 in the catalog but not Qdrant.

This is an exceptionally clean result — **one** mismatched field out of nine
across 12,057 documents.

---

## 13. Retrieval validation (live stores)

| query | documents |
|---|---|
| from 2022 | 1,257 |
| from 2023 | 1,388 |
| after March 2023 (open upper bound) | 3,664 |
| before 2015 (open lower bound) | 3,431 |
| June 2022 only | 718 |
| single day 2024-09-05 | 912 |
| 2019 – 2021 | 2,489 |

**Period overlap works:** completed_projects overlapping 2022 → 32; events → 139;
ongoing_projects → 618 (open-ended, correctly matching every year from their
start). 8 completed_projects start before 2022 and end during it — reachable only
through end-date overlap, and the filter finds them.

**Year precision is respected.** A 2022 year-precision document matches a *June
2022* query (correct — the source said only "2022"), matches a 2022 query, and
does **not** match 2023.

**Month precision is not.** A month-precision document does **not** match a
mid-month query, because its payload lost the marker — see Critical finding 3.

**The read path names no Drupal field.** `field_event_start_date`,
`field_news_date`, `field_report_date`, `published_at` and `raw_meta` all appear
**zero** times in `filters.py` and `temporal_gate.py` code.

---

## 14. Legacy / stale state

**A — harmless historical/legacy:**
- 33 date-decision rows for deleted documents
- 5 end dates on bundles that declare none (all `parent_page` inheritance)
- 61 dead-link rows, 100 retry rows (operational records)
- 20 `e2e_audit_*` MySQL tables (audit harness, isolated)

**B — still actively read, and correct:**
- All 17 Qdrant payload indexes
- All Neo4j constraints/indexes (schema ready, data absent)
- `documents_entity_extraction` (100,933 rows) — the mention cache, valid

**C — can cause incorrect retrieval:**
- **62 documents whose month precision is missing from Qdrant** (see below)

**Removed and confirmed gone:** legacy `published_at*` columns, the obsolete
`document_published` Neo4j index, boolean `"True"` category leakage.

---

## 15. Critical findings

### C1 — The knowledge graph was never built
**Count:** entire graph — 0 claims, 0 relationships, 0 entities.
**Impact:** every graph-backed query returns nothing. Any feature depending on
FUNDED_BY, LED_BY, AUTHORED, PARTNER_OF or person/organization traversal is dead.
**Evidence:** `documents_entity`=0; 140,774/140,774 resolution decisions
UNRESOLVED; Neo4j = 8 Predicate nodes, 0 relationships.
**Classification:** **real defect — a missed post-ingestion step**, not a code
fault. The ingestion correctly ran the per-document knowledge stage; it had no
entity index to resolve against because `seed_entities` was not run after the
tables were truncated.
**Remedy (not applied):** `python -m scripts.seed_entities --rebuild` then
`python -m scripts.build_knowledge`. No re-ingestion needed.

### C2 — Corpus is one payload version behind the code
**Count:** 12,057 of 12,140 documents.
**Impact:** the corpus is internally consistent, but any `reprocess_corpus` will
treat all 12,057 as stale and rebuild them. The p3 date rules have already been
partially applied by backfill, so catalog *values* are ahead of the version
*stamp*.
**Evidence:** catalog `c1.i1.p2.e1` ×12,057; `PIPELINE_VERSION = c1.i1.p3.e1`;
602 rows updated after 2026-09-12.
**Classification:** **expected consequence of changing code after ingesting** —
needs a decision, not a fix.

### C3 — 62 month-precision documents lost their marker in Qdrant
**Count:** 62 documents (all `date_source='document_title'`, updated
2026-09-13 00:26:59 by the backfill).
**Impact:** retrieval treats them as day-precision, so a document dated "June
2012" matches only 1 June rather than the whole month. Verified live: a
month-precision document does not match a mid-month query.
**Evidence:** `scripts/backfill_bundle_dates.py:703`
```python
"start_precision": ("year" if move.start_precision == "year" else None),
```
versus `app/ingestion/chunking/payload.py`, which writes both year **and** month
and whose comment states month *"used to be dropped here, which was the one place
a month-precision date silently became a day."*
**Classification:** **real defect in the backfill script** — it reintroduces, for
documents it touches, exactly the bug the ingestion path fixed. The ingestion
pipeline is correct.

---

## 16. Minor findings

| finding | count | classification |
|---|---|---|
| End dates on bundles that declare none | 5 | harmless legacy; one 15-month span on an article PDF is questionable |
| Date-decision rows for deleted documents | 33 | harmless orphan |
| Intra-document duplicate chunk text | 258 (0.2%) | expected — PDF headers/footers |
| Chunks under 40 characters | 24 | negligible |
| Documents that never reached the catalog | 100 | source-data issue — downloads/extractions returned nothing |
| `hybrid_use_sparse` setting read by no code | 1 | dead configuration |

---

## 17. Recommendations

1. **Build the knowledge layer.** `seed_entities --rebuild` then
   `build_knowledge`. This is the single change that moves the system from
   "documents only" to "documents + graph". Nothing needs re-ingesting.
2. **Fix `backfill_bundle_dates.py:703 & 705`** to write `month` as well as
   `year`, matching `payload.py`, then re-run it for the 62 affected documents.
   Small, contained, and it prevents the same loss on any future backfill.
3. **Decide on the version gap.** Either accept p2 and bump nothing, or run
   `reprocess_corpus` to bring all 12,057 to p3. Vectors are reused on unchanged
   keys, so a reprocess re-embeds nothing — but it is another ~10-hour run.

I am deliberately not recommending anything about chunking, Qdrant payloads,
cross-store consistency, date provenance, multi-PDF handling or the retrieval
read path: **all of these are working correctly** and need no attention.

---

## 18. Overall readiness

### VERDICT: **YELLOW**

**Ready now:** document retrieval. The corpus is complete, internally consistent
across MySQL and Qdrant, correctly dated, cleanly chunked, and its date filtering
— including period overlap, open-ended projects and year precision — behaves
correctly against the live store.

**Not ready:** knowledge-graph queries. The graph is empty.

### Top 3 to fix before relying on this corpus

1. **Run `seed_entities --rebuild` + `build_knowledge`** — without it the graph
   half of the product does not exist. *(blocking for graph queries; ~1–2 hours,
   no re-ingestion)*
2. **Fix the month-precision write in `backfill_bundle_dates.py` and re-apply to
   the 62 documents** — currently causes measurably wrong retrieval for those
   documents. *(small, contained)*
3. **Resolve the p2/p3 version gap deliberately** — harmless today, but it makes
   every future staleness check report the whole corpus as out of date.

Nothing found requires re-ingesting the corpus.
