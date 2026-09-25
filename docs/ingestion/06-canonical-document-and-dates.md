# 06 — The Canonical Document and Date Resolution

**Purpose.** Converge two very different sources — a CMS record and a PDF file —
onto one document shape, with facets routed, themes classified and an effective
date decided and justified.

**Inputs.** A `DrupalRecord`, or an `ExtractionResult` plus the node that carries
it.

**Outputs.** A `CanonicalDocument`, plus (as a side effect) a row in
`documents_date_decision` explaining the date.

**Components.** `app/core/models/document.py`,
`app/ingestion/canonical.py`, `app/ingestion/bundle_dates.py`,
`app/ingestion/source_dates.py`,
`app/ingestion/date_{evidence,rules,llm,resolution,candidates}.py`,
`app/core/editions.py`, `app/catalog/date_decisions.py`,
`app/catalog/theme_taxonomy.py`.

---

## `CanonicalDocument`

The single shape everything downstream reads.

```python
@dataclass
class CanonicalDocument:
    # identity
    document_id: str
    source_type: str                       # "website" | "pdf_attachment"
    title: str | None
    sections: list[CanonicalSection]

    # source references
    source_url: str | None                 # the web page
    file_url: str | None                   # the PDF actually fetched
    pdf_id / pdf_path
    article_uuid / linked_pdf_id / linked_article_uuid

    # facets
    authors / tags / categories: list[str]
    language: str = "en"
    effective_start_date: str | None               # the effective/start date
    date_source: str | None        # created | cms_field | parent_page
                                           #   | document_title | document_copyright
                                           #   | document_text | no_evidence
    start_precision: str | None     # year | month | day
    effective_end_date: str | None            # end of the period, or None
    end_precision: str | None
    doc_version: int = 1
    is_current: bool = True
    content_hash: str = ""
    extra: dict                            # bundle, nid, changed, edition_label

    # catalog-only — never enters a chunk payload
    entity_refs: list[EntityRef]
    file_links: list[FileLink]
    raw_meta: dict
```

`CanonicalSection` is `(text, heading, page_start, page_end, order)`.

Three helpers matter: `is_paginated` (any section has a `page_start` — true for
PDFs, false for website records), `full_text()` (headings and text joined, the
input to the content hash), and `ensure_content_hash()`.

### The catalog-only fields

`entity_refs`, `file_links` and `raw_meta` are persisted to MySQL and **never**
reach a chunk payload. The chunker copies fields into `DocumentMeta` explicitly
(see `_meta_from_canonical`), so adding a field here does not silently start
writing it to every point in the collection. That explicitness is deliberate:
`raw_meta` is by far the largest field, and a payload is replicated once per chunk.

---

## Builders

| Builder | Input | Used by |
| --- | --- | --- |
| `from_drupal_record(record)` | a `DrupalRecord` | the `website` path in the sweep |
| `from_pdf(result, ...)` | an `ExtractionResult` | the `pdf_attachment` path |
| `from_drupal_export(item)` | an ad-hoc dict | `POST /ingest/article`, `indexer --drupal-json` |

`from_drupal_export` has no relationships to read, so its facets come from
metadata name hints alone.

### `from_pdf`

```python
sections = [CanonicalSection(text=page.text,
                             page_start=page.page_number,
                             page_end=page.page_number,
                             order=i)
            for i, page in enumerate(result.pages) if page.text]
```

One section per **non-empty** page, 1-indexed by the extractor. Empty pages are
dropped from the sections list but their page numbers are not renumbered, so page
attribution survives.

### `_drupal_document`

Builds a single-section document from the flattened body (or no sections if the
body is empty), sets `document_id` to the uuid (falling back to a slug of the URL
or `bundle/title`), records `extra = {bundle, nid, changed}`, carries `entity_refs`
and `raw_meta`, and resolves the date via `_effective_dates_for`.

---

## Facet routing

`drupal_facets(metadata, refs)` produces `categories` (themes), `tags` and
`authors`, and is shared by the node document **and its attachment documents** —
an attached PDF inherits its node's facets so theme-scoped retrieval and per-theme
counts reach the attached content too.

```python
THEME_HINTS          = ("theme",)
TAG_HINTS            = ("tag", "keyword")
AUTHOR_HINTS         = ("author",)
CATEGORY_VOCABULARIES = ("themes",)
```

- **`categories`**: the union of metadata fields whose name contains `theme`,
  **plus any `EntityRef` into a `themes` vocabulary whatever the referencing field
  is called**. Vocabulary routing beats field-name guessing, and catches fields
  the name hints miss.
- **`tags`**: the union of fields whose name contains `tag` or `keyword`.
- **`authors`**: the *first* matching `author` field (a list if one is a list,
  otherwise a comma-split string).

### What is deliberately not a theme

Themes match on `"theme"` **alone**. The hints used to also absorb any field named
`category`, `area` or `division`, which put things that are not themes — a
division, a regional area — into a document's themes. Those vocabularies are
dimensions of their own and still reach the catalog through `entity_refs` and
`raw_meta`.

A taxonomy term's `parent` is not folded in by name either: a real parent inside a
theme vocabulary already arrives as a ref, and the parent of a term in some *other*
vocabulary was never a theme. The primary-tag/sub-theme relationship is recorded on
the theme rows themselves.

`app/ingestion/field_audit.py` reports against these same constants — it samples
records from every source and reports, per field, how the extractor partitions it,
which facet the hints route it to (or nothing, i.e. dropped), the observed fill
rate and relationship target types. Run it after any CMS field change:

```bash
python -m app.ingestion.field_audit --sample 50 --out reports/field_audit.json
```

---

## Theme classification

`app/catalog/theme_taxonomy.classify(names)` turns a flat list of theme names into
typed rows, against the static map at `app/theme_structure.json` — or wherever the
`theme_taxonomy_path` setting points, which is the one place the authoritative
file is named.

The file's top level (`Main Themes` / `Other Themes`) is a **grouping bucket, not a
theme**:

- A bucket's children are **primary tags** (`parent` is NULL, `depth` 1).
- Anything below one is a **sub-theme** whose `parent` is its *immediate* parent.
- A bucket name is **never stored** as a theme.
- `theme_group` (`main` / `other`) records which bucket a theme traces back to; a
  sub-theme inherits its primary tag's bucket, however deep it sits. It is tracked
  separately from `theme_type`/`parent` because two primary tags from different
  buckets are both `(primary, NULL)`.

**Depth is not capped.** `parent` names one hop; `theme_path` carries the whole
chain (`Energy > Energy Access > Rural Energy Access`) and `depth` its segment
count. These columns record the hierarchy; they are not read at question time.
A theme filter matches the theme's own name only, so "Energy" does not widen to
documents tagged only "Energy Access".

Matching is case- and whitespace-insensitive using Unicode `\s`, so Drupal's
non-breaking spaces are folded too.

Four guards:

1. **Only the document's own themes get rows.** A parent is recorded as a
   *reference*, never materialised as an extra row, so a post tagged only "Energy
   Access" is not also credited with "Energy".
2. A theme the map does not know is kept, classified **`unknown`** — no parent, no
   group, and its own name as a one-segment path. It is countable and listable
   (under `list_themes(scope="all")`) without the map being edited. `unknown`
   rather than `sub`, because calling it a sub-theme asserts a parent nothing
   supports, and it also hid the theme from the primary-tag listing.
3. Bucket names and blanks are dropped, so a document with no valid theme gets
   **no row at all** — "has no theme" is the absence of rows, never a placeholder.
4. `_NOT_A_THEME = {"false", "true", "none", "null", "nan"}` drops stringified
   booleans. The catalog once held **404 rows whose theme was the literal string
   `"False"`** — a real `False` is falsy and drops out in cleaning, but `"False"`
   does not.

A missing or malformed map is logged, not raised, *per document* — a problem
appearing mid-run costs one document rather than aborting a long ingest. But it is
a **preflight failure**: `require_taxonomy()` runs before the first write in
`pipeline._run`, `ingestion.backfill.backfill_catalog` and
`state.reclassify_theme_rows`, and raises `TaxonomyUnavailable`. Without that
guard, classifying against an empty map does not fail — it succeeds and reports
every theme as unmapped, so the run quietly rewrites the hierarchy of every
document it touches. The trigger can be as small as the data file going missing.

The map is deliberately a static file rather than the crawled Drupal tree:
classification has to stay stable however a vocabulary happens to be nested in the
CMS, and the same map has to apply to the ref-less export/upload paths, which have
no taxonomy to read.

### Who owns theme membership

MySQL, alone. Every count, filter, grouping and aggregation reads
`documents_theme`. Chunk payloads still carry a `categories` list, but nothing
filters on it: the semantic path resolves a theme to document ids via
`catalog.theme_document_ids` and lets Qdrant rank *within* that set
(`understanding.filters._theme_condition`). Payload `categories` used to be the
semantic path's theme filter, which made it a second, independent copy of
membership — written at index time, so it went stale on any rename or
reclassification.

---

## Dates: one decision, propagated

The principle: **nothing invents a date.** A document either carries a date the
source states, or it carries no date and is logged and counted as such.

Which date a Drupal record carries is a property of its **bundle**, not of which
date-like fields happen to be present. `news` takes `field_news_date`,
`completed_projects` takes the project's start, `article` takes its creation
stamp. That mapping is declared once in `app/ingestion/bundle_dates.py` and the
resolution algorithm names no bundle:

```
bundle -> configured field -> extract -> normalise -> effective date
```

| Source type | Date | Can be overridden by |
| --- | --- | --- |
| `website` | the bundle's configured field, else the record's `created` (or `revision_created`) | nothing |
| `pdf_attachment`, **alone on its page** | **its parent page's resolved date** | nothing |
| `pdf_attachment`, **sharing its page** | a date the **file's own name** states; else a date its **own text** states; else its page's date, **but only where the page's bundle states one** | — |

`date_source` on the document and in the catalog records which happened:
`created`, `cms_field`, `parent_page`, `document_title`, `document_copyright`,
`document_text`, or `no_evidence`.

**A file sharing its page may end up with no date at all.** That is the point of
`no_evidence`, not a gap in it. A page holding twelve PDFs is a shelf that
accreted documents over years, so its Drupal creation stamp is a fact about the
shelf — `TERI-Annual-Report-2024-25.pdf` was stored as 2022-02-09, and 69 FCRA
financial statements spanning eight years all shared 2018-04-04. Where such a
file states nothing about itself, no date is the honest record: a date-scoped
query that misses a document is recoverable, one that confidently returns it
under the wrong year is not. Measured on the live corpus, of 1,523 attachments on
multi-PDF pages, 164 take a date from their own name, 156 keep a corroborated
copyright year, 869 keep a date their page's *bundle* states, and 334 are left
undated. The 1,986 attachments alone on their page are untouched.

**What `effective_start_date` now means.** For `news`, `press_release`, `report` and
`research_papers` the bundle's field *is* a stated publication date. For
`completed_projects`, `ongoing_projects` and `events` it is a project start or an
event date — the date the content is *about*, which the site treats as that
item's date. `effective_start_date` is therefore the **effective date**, and
`documents_date_decision.date_type` records which of the two a given row is
(`date`, `range_start`, `range_end`), so the distinction stays visible rather
than being erased. `source_dates.FIELD_ROLES` owns that classification.

---

## The bundle mapping: `bundle_dates.py`

```python
BUNDLE_DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "article":            ("created",),
    "page":               ("created",),
    "feature_articles":   ("created",),
    "policy_brief":       ("created",),
    "videos":             ("created",),
    "infographics":       ("created",),
    "people":             ("created",),
    "services":           ("created",),   # not in the supplied map
    "news":               ("field_news_date",),
    "press_release":      ("field_pressrelease_date",),
    "report":             ("field_report_date",),
    "research_papers":    ("field_rpaper_year",),
    "ongoing_projects":   ("field_ongoing_start_date",),
    "completed_projects": ("field_completed_start_date",
                           "field_completed_end_date"),
    "events":             ("field_event_start_date", "field_event_end_date"),
}
```

**A bundle maps to one or more fields, in order: `(start,)` or `(start, end)`.**
Neither is a special case of the other — the same per-field resolution runs on
each configured field, and the only thing a second field adds is the range
validation. Adding a bundle, or giving an existing bundle an end date, is one
row. No branch in the resolver, and none in the pipeline, names a bundle.

**Precision is declared per field, not per bundle**, and read from
`source_dates.FIELD_ROLES`, which already declares `(role, precision)` for every
one of them. `field_rpaper_year` holds a year whichever bundle points at it, so
saying so twice would be two facts that can disagree.

`ongoing_projects` is single-field on purpose: `field_ongoing_end_date` does not
exist. Drupal answers a filter on it with "the field does not exist" and the
attribute is absent from all 595 published records — which is also the
semantically right answer, since an *ongoing* project has not ended.

`services` is **declared** rather than omitted even though it maps to `created`
and carries no date-like field: a crawled bundle nobody has classified must stay
a meaningful alarm (`reconcile.date_checks.unmapped_bundle_dates`) instead of
firing on the same bundle forever — the same reason `FIELD_ROLES` declares the
fields it refuses. `block_content:basic` is deliberately absent: it is not a node
bundle and has no `created` attribute, so it resolves through the unmapped
default to the `revision_created` stamp `_created_at` already gives it.

Field shapes, verified against the live JSON:API: `field_rpaper_year` is an
**integer** (2012–2019 observed); `field_report_date` carries a `+05:30` offset
and a real clock time and is null on 2 of 8 records; every other field is IST
midnight expressed as `+00:00`. None is multi-valued or a date range on this site.

### Date ranges

For a bundle whose mapping declares an end field, the end runs through the
**same** per-field resolution as the start, and the two are then compared as
**normalised calendar dates** — never as raw values.

| Case | Behaviour | `range_issue` |
| --- | --- | --- |
| No end field configured | `effective_end_date` is NULL | — |
| `start <= end` (including `start == end`) | both kept | — |
| **`start > end`** | end **dropped**, start kept, both raws preserved, review row | `inverted` |
| End field empty | no end — a valid partial range, not a defect | — |
| End field holds a non-date | end dropped, raw preserved, review row | `end_invalid` |
| **End usable, start not** | start falls back to `created`, **end preserved**, review row | `end_without_start` |

Three things this gets right, each measured on the full published corpus:

- **Comparison happens after normalisation.** By raw string 13 events and 5
  completed projects look inverted; after `to_ist_date` only **2 and 2** are. The
  rest differ solely in the time component and land on the same Indian calendar
  day, or carried a `1970-01-01` end that plausibility already removed. Comparing
  raw strings would have sent eleven correct records to a review queue.
- **`start == end` is valid.** 784 of 1,094 published events are a one-day event
  whose two fields hold the same day.
- **Values are never swapped, and an end is never manufactured.** A record whose
  two dates contradict each other is a CMS defect; guessing which one is wrong
  would bury it.

`end_without_start` deliberately does **not** re-check the preserved end against
the fallback `created` stamp. A creation stamp is not a range endpoint — every
completed project's end predates its 2017 import stamp, so comparing them would
flag the whole bundle for a relationship the source never asserted.

### The fallback ladder

| Case | Behaviour | `source` | `rule` |
| --- | --- | --- | --- |
| Bundle maps to `created` | use `created` | `created` | `bundle_created` |
| Field present, valid | use it | `cms_field` | `bundle_date_field` |
| Field holds a bare year in a full-date field | use it, **downgrade precision to `year`** | `cms_field` | `bundle_date_field_year_only` |
| Field absent / empty / null | fall back to `created`, INFO log | `created` | `field_empty` |
| Field unparseable / implausible | fall back to `created`, WARNING log, **review row** | `created` | `field_invalid` |
| Bundle not in the mapping | fall back to `created`, INFO log once per bundle | `created` | `bundle_unmapped` |
| No `created` either | `effective_start_date = None`; the `undated` flag fires | `created` | `no_date` |

Falling back to `created` is justified, not incidental: it is a real date the
source states about the record, it is exactly the historical behaviour, and the
alternative — no date — makes a document **invisible** to every date filter
rather than merely mis-ordered.

The `created` stamp is passed through **verbatim**, never re-normalised to
midnight: its intra-day clock reading is the only thing separating the 646
completed projects that share one import date.

## What each field *is*: `source_dates.py`

This module does not decide which field a document uses; it supplies the
vocabulary and the value-reading primitives (`to_ist_date`, `is_plausible`,
`as_stored_date`, `found_dates`) that the bundle mapping and the audit trail
share.

The problem it answers: this CMS has **thirteen fields with date-like names**, and
what each one *is* — a date the content is stated with, one end of a period, a
sort key, a publisher's name that merely looks temporal — is knowledge no
algorithm can derive from the value.

**It describes the source field, not the application's date model.** The system
stores an `effective_start_date` and, where the bundle declares one, an
`effective_end_date`. It has no publication-date concept, and this taxonomy
deliberately does not use the word: a role answers "what is this Drupal field?",
never "is the resulting date a publication?".

So the meaning of a field is **data, declared once**, in `FIELD_ROLES`:

```python
FIELD_ROLES: dict[str, tuple[FieldRole, Precision]] = {
    # date — the single date the CMS states about this content
    #        (verified against the rendered pages, 30/30 sampled)
    "field_news_date":            ("date", "day"),
    "field_pressrelease_date":    ("date", "day"),
    "field_report_date":          ("date", "day"),
    "field_rpaper_year":          ("date", "year"),
    # range_start / range_end — the period the content covers
    #        (~2,100 values, the most tempting to misread)
    "field_completed_start_date": ("range_start", "day"),
    "field_completed_end_date":   ("range_end",   "day"),
    "field_ongoing_start_date":   ("range_start", "day"),
    "field_event_start_date":     ("range_start", "day"),
    "field_event_end_date":       ("range_end",   "day"),
    # sort_key — orders a listing, describes nothing
    "field_enddate_forlatestfirst": ("sort_key", "day"),
    # not_a_date — venues and publisher names whose field names contain "publish"
    "field_article_published_in": ("not_a_date", "day"),
    "field_rpaper_published_in":  ("not_a_date", "day"),
    "field_rpaper_publisher":     ("not_a_date", "day"),
}
```

Five roles, each earning its place by controlling something:

| Role | What it controls |
| --- | --- |
| `date` | The CMS states the content's date outright; the provenance sentence needs no gloss. |
| `range_start` / `range_end` | The value is one end of a period. Kept distinct because `BUNDLE_DATE_FIELDS` is ordered `(start, end)` — reversing a pair would date every project by its finish, and the split is the only independent check on that. |
| `sort_key` | A real timestamp the site uses to order event listings. It describes nothing, so **no bundle may map to it** — asserted by a test. |
| `not_a_date` | A field whose *name* looks temporal and whose values are not dates. |

Three consequences, each stated in the module for a reason:

1. **`not_a_date` is the default.** An undeclared field cannot become a date, so
   a CMS that grows a new field does not silently start moving dates.
2. **The non-date fields are declared rather than omitted**, so "a date-like field
   nobody has classified" stays a meaningful alarm
   (`reconcile.date_checks.undeclared_source_date_field`) instead of firing on
   the same three fields forever. One `field_rpaper_publisher` value is literally
   `"2021"`, which is bad CMS data and would otherwise parse as a date.
3. **Supporting another site means adding rows, not branching code.** No algorithm
   can know `field_news_date` states a date and `field_enddate_forlatestfirst`
   sorts a listing.

### The other taxonomy, and why it keeps the word

`app.ingestion.date_rules.DateType` — `publication`, `notification`, `effective`,
`upload`, `authoring`, `edition`, `event`, `unknown` — classifies a date the model
found **inside a PDF's text**. That is a different question about a different
thing: the interpreter is literally asked whether a quoted statement is a
publication line or a notification, and the answer is what the gates act on. It
keeps the word because there the word is the point.

The two share the `{state}_date_decision.date_type` column, told apart by
`origin`: a `website` row holds a `FieldRole`, an `attachment`/`inbody` row holds
a `DateType`. The value sets are disjoint (a test asserts it), so a row is never
ambiguous — but no query should group across the two without filtering `origin`
first.

### Timezone: IST, and why it is not optional

```python
IST = timezone(timedelta(hours=5, minutes=30))
```

The CMS stores a date-only field as **IST midnight expressed in UTC**:
`2012-04-17T18:30:00+00:00` is 18 April in Delhi, and 18 April is the date the site
itself displays. Reading the UTC calendar date puts every one of these **a day
early**. `to_ist_date` converts to the IST calendar date.

Then `as_stored_date(value)` writes it back as **midnight UTC**, not midnight IST:
`state._to_datetime` normalises to naive UTC, so `2012-04-18T00:00:00+05:30` would
land in the column as `2012-04-17 18:30` and every consumer would read a date a day
early — precisely the error being corrected. The date has already been resolved
*to* the Indian calendar day; this only has to preserve it.

### Plausibility

`is_plausible(value)` requires `1990 <= year <= current_year + 1`. A zero timestamp
read as a date lands in 1970, and a parse accident can land centuries away. Both
are rejected here rather than downstream, because a date that is merely *stored* is
already acting on ranking. An implausible value from a declared publication field
is discarded with an INFO log.

### The single decision

```python
def resolve(bundle, created, metadata) -> EffectiveDate:
    configured = BUNDLE_DATE_FIELDS.get(bundle)
    if configured is None:            return created, "created"   # bundle_unmapped
    if configured.is_created:         return created, "created"   # bundle_created
    raw = metadata.get(configured.field)
    if raw in (None, "", [], {}):     return created, "created"   # field_empty
    value = to_ist_date(raw)
    if not is_plausible(value):       return created, "created"   # field_invalid
    return as_stored_date(value), "cms_field"                    # bundle_date_field
```

Four callers, one function: the website builder (`canonical._effective_dates_for`),
the attachment path (`attachment.resolve_parent_date`), the evidence adapter
(`date_resolution.build_evidence`) and the backfill
(`scripts.backfill_bundle_dates`). Two copies of this rule would drift, and a
re-ingested document would then get a different date than the backfill gave it.

It returns an `EffectiveDate`, not a bare value: the caller writes the audit row,
and a row derived from a second reading of the metadata could disagree with what
was stored. It carries the value, the source, the precision, the bundle, the field
consulted, that field's raw value, the rule that fired and what
`source_dates.classify` says the field *is*.

**A note on what changed.** The previous, field-keyed rule declined a
year-precision statement whose year the record already sat in — `"2016"` plus a
record created 2016-03-15 kept the real day. The bundle mapping is unconditional:
`field_rpaper_year` is what a research paper is dated by, so it applies whatever
`created` says. The ~228 papers already in the right year lose their intra-year
ordering; that is a deliberate, recorded cost of making the rule uniform.

### Year precision is a marker, not a day

A year-precision value is stored as **1 January as a marker for the year**.
`start_precision="year"` is what keeps that from being read as a January
publication, and it is carried all the way to the chunk payload (`build_payload`
writes it **only** when it is `"year"`, so absent means "a full date" — true of
every point already in the collection, which is why this needed no `PAYLOAD`
version bump).

Anything that renders the day without reading the precision invents a January
publication. Reconciliation's `year_precision_not_january` check asserts the
converse invariant: a year-precision date whose value is *not* 1 January means the
value and its precision disagree about what is known.

### The audit row

`_record_source_date_decision` writes a `documents_date_decision` row for a
`website` document — but **only when the document's bundle maps to a real CMS
date field** (`date_decisions.from_effective_date` returns `None` otherwise). A
row for every document of a `created`-mapped bundle would cost an INSERT and a
commit each to store a fact two columns already carry: `documents.bundle` plus
`date_source='created'` *is* the whole answer for `article` or `page`.

So a row is written when a field was consulted — whether it supplied the date
(`bundle_date_field`), was empty (`bundle_field_empty`, a keep) or held something
that is not a date (`bundle_field_invalid`, which reaches the **review queue**:
the CMS says this content type is dated by that field and the field holds
nonsense, and nobody can fix that from here). `date_type` records what the
*source field* is — `date`, `range_start`, `range_end` — rather than flattening
that away.

Fails open: an unreachable database costs one warning, never a document its
ingestion.

`found_dates(metadata)` returns *every* declared date, publication or not, for the
audit trail — "what did this record actually offer, and why was none of it used" is
only answerable if the rejected candidates were recorded too.

---

## PDF publication-date resolution

Gated by `date_resolution_enabled` (default **on**). With it off, every PDF simply
inherits its page's resolved date — so turning the resolver off degrades to plain
inheritance rather than to a different date.

### The contract

**A file alone on its page carries that page's date.** The parent's
`EffectiveDate` is resolved **once** per attachment build and passed in, so a page
and its attachment cannot diverge — which they previously did by construction,
because this path read `node.created` while the page's own builder applied the
bundle's field. Nothing about the file is read: not its name, not its timestamps.
One PDF on a page is that page's publication.

Being uploaded later, having a later `file.created`, sitting under a later
`/files/YYYY-MM/` path, or carrying a later PDF `CreationDate` or `ModDate` are
all **supporting signals**: they decide whether a document is worth reading
closely, and never set a date. Nor does the ingestion clock.

**A file sharing its page is a document in its own right**, and two rules follow.

*The file's own name decides* (`title_override`). It reads three strings the
crawl already holds, **strongest first**:

| # | Source | Why here |
| --- | --- | --- |
| 1 | `filename` | Belongs to the file. |
| 2 | `pdf_internal_title` | Belongs to the file, but producers leave template junk in it. |
| 3 | `link_text` | Belongs to the *page*, not the file: routinely generic ("Annual Report", "Download") or stale where the file behind the slot was swapped for a newer edition. |

So a clear filename is never overridden by a link label:
`TERI-Annual-Report-2024-25.pdf` behind the text "Annual Report" is the 2024-25
edition, and `Annual_Report_2024.pdf` behind the text "Report 2022" is a 2024
document mislabelled on the page. Link text stays last rather than being dropped,
because it is the only source for a file whose name carries nothing.

The date is read at the precision the string states — `09 March 2026` a day,
`feb21` a month, `2024-25` a year — and the *kind* of statement is recorded
separately, because it says how much weight it carries:

| `title_kind` | Example | Effect |
| --- | --- | --- |
| `full_date` | `15.03.2022` | Settles the file outright: no read, no model call. |
| `edition` | `2024-25` | Settles it outright too — a period the document *covers* has no publication day inside it to find. |
| `month_year` | `feb21`, `2024_September` | Fixes the month; the document's own text may sharpen it *inside* that month. |
| `bare_year` | `Post_2015_…` | Weakest. Never displaces a CMS-stated date or a verified text date. |

`Tender_..._2024_December.pdf` whose first line reads `ISSUE NO. 22 DATED
11-12-2024` is therefore dated to the 11th; the same file with `11-12-2023` in
its body keeps December 2024, because a different year is a disagreement and not
a sharpening.

Two false positives are blocked by name. A year must be **delimited** to count:
`ES2009CE09.pdf` is a project code, and reading 2009 out of it would move 137
files onto a date nothing stated. And a number introduced by a sequence label is
not a day: `Issue 8, Nov 2019` is the eighth issue, published in November 2019.

*Where nothing states a date, the file gets none* (`page_date_is_usable`). The
exception, and it is a large one: where the page's **bundle** states its date in a
configured CMS field, the page is still authoritative for every file on it. An
event's agenda, concept note and slide deck all happened on the day of the event.
Only a page dated by its bare Drupal creation stamp loses the right to lend it.

### The precedence table

The resolver implements this, rather than accumulating special cases. "Strong"
means the page's bundle states its date in a configured CMS field; "weak" means
the page has only its Drupal creation stamp.

| Situation | Result |
| --- | --- |
| Single PDF | Parent effective date |
| Multi PDF + full file date | File date |
| Multi PDF + edition/period | File period |
| Multi PDF + month/year | File month; text may sharpen |
| Multi PDF + bare year + strong CMS parent | Keep CMS date |
| Multi PDF + bare year + weak creation parent | File year may win |
| Multi PDF + verified text date | File text date |
| Multi PDF + no file evidence + strong CMS parent | Parent date |
| Multi PDF + no file evidence + weak creation parent | Undated |
| Resolver failure + strong CMS parent | Parent date |
| Resolver failure + weak multi-PDF parent | Undated |

The last two rows are not a detail. "Fail closed" used to mean one thing here —
keep the page's date — which is right where that date is a claim about the
content and exactly backwards on a shelf: an unrelated crash in a rule that never
ran would hand every file on the page the day somebody typed the node, silently
reintroducing the original bug. `_failed()` asks the *same* predicate the success
path asks, so the two cannot drift.

### An override replaces only what it establishes

An override used to clear the parent's `effective_end_date` unconditionally. That
destroyed real information: a file on a 2020-2025 project named "Annual Report
2024" states something about 2024 and nothing whatever about when the project
ends.

| Evidence | `effective_start_date` | `effective_end_date` |
| --- | --- | --- |
| full date | the day | cleared — a document issued on one day covers no period |
| month + year | the month | cleared, same reason |
| edition / bare year / copyright year | the year | **the parent's end survives** |

With one bound: the result must read forwards. An override landing after the
inherited end drops it rather than storing a backwards range, which
`reconcile.date_checks.inverted_date_range` would correctly report as something
else having written the column.

**An override otherwise needs the document's text to say so** — a copyright year
its own DocInfo corroborates, or a publication statement the LLM interpreter
quotes and every gate accepts.

`PageContext` therefore carries **two** dates: `node_created`, the creation stamp,
which the upload-gap arithmetic reasons about, and `node_start_date`, the
resolved date, which the file inherits. Substituting the latter in the gap
arithmetic would read a completed project's 2004 start as a 13-year upload gap and
route the whole bundle to the model.

Nothing here downloads anything — the caller already holds the PDF bytes — and
Document Intelligence is unreachable, because `date_resolution.py` does not import
the extraction module.

### The evidence ladder — cheapest first

`PdfEvidence` (`date_evidence.py`) gathers, in cost order:

| Tier | Fields | Cost |
| --- | --- | --- |
| 1 | node date, bundle, page URL, `pdf_count` on the page | free (already in the crawl payload) |
| 2 | filename, anchor text, `/files/YYYY-MM/` upload month, edition label, years mentioned | free (string work) |
| 3 | PDF DocInfo `CreationDate` / `ModDate` / title | PyMuPDF only, bytes in hand |
| 4 | first-page text (`HEAD_PAGES=2`, `HEAD_CHARS=2500`) | `page.get_text` only |

Tier 3's DocInfo read (`read_pdf_docinfo`) lives in `app/ingestion/date_candidates.py`,
not in `date_evidence.py` itself — `_read_pdf_signals` imports it. That module is
otherwise a **separate, measurement-only** model (`resolve()`) for a proposed
two-sided correction to attachment dates (a migration-era DocInfo override, a
late-upload `file.created` override); it is not wired into `date_rules.decide` or
`date_llm.interpret` and writes nothing to a document. It backs the shadow tooling
(`scripts/shadow_date_prototype.py`, `scripts/shadow_pdf_sample.py`,
`scripts/shadow_corpus_report.py`, `scripts/audit_annual_report_dates.py`) that
measures what the correction *would* do against the live corpus before any of it
ships.

`PageContext.pdf_count` is the whole point of the model: **the unit of analysis is
a page and its PDFs, not a PDF alone.** One PDF on a page means the file is almost
certainly part of the page's own publication; several means the page may be a shelf
that accreted documents over years.

`edition_label` recognises a fiscal/reporting span — `2024-25`, `2024-2025`,
`2024_25`, `20-21` — and only **consecutive** spans count: "2024-25" is an edition,
"2019-2024" is a range and "Report 2 - 3" is nothing. It produces a **label and
never a date**: an annual report for 2024-25 was not published on any particular
day the label implies. Anchor text is checked first, because it names the edition
for 10/10 annual reports including the one whose filename carries no year at all.
The spelling rule itself lives in `app/core/editions.normalise_edition` so retrieval
applies the identical one.

`month_start("YYYY-MM")` returns the **15th** of that month, not the 1st: the value
is only accurate to a month, and anchoring at the midpoint halves the worst-case
error either way.

### The deterministic pass — `date_rules.decide`

**This function can no longer propose a date change.** It returns
`keep_page_date`, `needs_llm`, or (only when the page has no date at all)
`needs_manual_review`.

That is a deliberate narrowing after manual review. The previous version treated a
late upload as a publication date, which conflates two different facts: when a file
was *put on the server* and when the document was *released*. Drupal's
`file.created`, a `/files/YYYY-MM/` path, a PDF `CreationDate`, an event date, a
notification date and an effective date are all evidence of something — but none
of them is, by itself, a publication date. A **year in the filename** is the one
item that has since moved off this list, and only on a multi-PDF page; see
*The file's own name decides* above.

Upload timing keeps one job: **it decides where it is worth spending money.**

Read `keep` below as "fall back to the page's date" — which on a multi-PDF page
whose own date is only a Drupal creation stamp means `drop_page_date` and no date
at all. Every branch routes through one helper, so the rule that fired and the
value it yields cannot drift apart, and no branch added later can quietly
reintroduce the borrowed date.

```
name states a full date or a reporting
  period                              -> override (title_states_date)
                                         [multi-PDF pages only; nothing below runs]

page has no date at all               -> needs_manual_review (no_page_date)

page's bundle states its date in a
  configured CMS field                -> keep (parent_bundle_date_field, conf 1.0)
                                         [single-PDF pages only;
                                          nothing below is evaluated]

single-PDF page:
  file uploaded > 365d after the page,
  and not a migration import          -> needs_llm  (single_pdf_late_upload_review)
  otherwise                           -> keep       (single_pdf_page, conf 0.9)

multi-PDF page:
  Drupal file date, not migrated:
    gap > 90d                         -> needs_llm  (multi_pdf_late_upload_review)
    otherwise                         -> keep       (multi_pdf_uploaded_with_page, 0.85)
  no file date but a URL month:
    gap > 90d                         -> needs_llm  (multi_pdf_url_month_review)
    otherwise                         -> keep       (multi_pdf_url_month_matches, 0.8)
  migration import:
    no DocInfo and no head text       -> keep       (migration_cohort_no_evidence, 0.5)
    otherwise                         -> needs_llm  (migration_cohort_review)
  in-body, no upload signal:
    any textual evidence at all       -> needs_llm  (multi_pdf_textual_only)
    otherwise                         -> keep       (multi_pdf_no_evidence, 0.5)
```

Two measured facts behind the thresholds:

- **Single-PDF pages default to the page date.** Of the 439 attachments whose file
  arrived days-to-weeks after the node, 76% were authored within 30 days of the
  node and 89% read as "written and posted together". The measured median gap
  between page creation and attachment is 14 days, which is why `SEPARATION_DAYS`
  is 90 and `SINGLE_PDF_LOOK_DAYS` is 365.
- **The 2017–2018 migration cohort is real and must never be read as upload
  timing.** 1,406 of 1,545 pre-cutoff files share **four** timestamps, one of them
  covering 397 files whose nodes span 13.5 years. `in_migration_cohort` treats any
  `file.created` before `2018-06-01` as an import, not an upload. (Equivalent to
  `fid <= 3760` on this site, verified zero overlap with post-cutoff fids.)

Whatever fires, the upload facts are carried on the decision as
`supporting_evidence` so a reviewer can see what triggered the look — but nothing
acts on them.

### Read, then reconsider

```python
decision = decide(evidence)
if decision.action == "needs_llm":
    _read_pdf_signals(evidence, content)   # DocInfo + first-2-pages text
    decision = decide(evidence)            # re-run BEFORE paying for a model call
if decision.action == "needs_llm":
    decision, llm_raw = _interpret(evidence, decision)
```

Reading the document may itself settle the case — an unreadable PDF has nothing to
say — so the deterministic pass runs again before the model is called.

### The interpreter, and its gates

`date_llm.interpret(evidence)` sends **metadata and a short text head, never a
whole PDF**, so cost per call is bounded. The model is **not** asked "when was this
published?" — it is asked what *kind* of date the evidence supports, because the
whole problem is that a corpus is full of dates that look publishable and are not:
reporting periods, event dates, notification dates, effective dates, upload
timestamps and PDF export dates. The prompt carries worked examples on both sides
(newspaper mastheads and issue lines *are* publication; "Notified on 18.05.2023",
"comes into force with effect from 1 April 2024", "Workshop held on 5 March 2025",
"Annual Report 2024-2025" are not).

The verdict is a Pydantic model, then **downgraded** by `safe_action()` unless it
clears **every** gate:

| Gate | Method | Rejects |
| --- | --- | --- |
| Kind is `publication` | — | notification, effective, event, edition, upload, authoring |
| A date was produced | `_sane_date` validator | unparseable, or outside 1990…next year |
| The date appears in the document text | `date_is_in_text` | a date assembled from the *filename* — the corpus has newspaper clippings whose page text is unreadable mojibake and whose "verbatim quote" was a tidied-up copy of `Hindustan-Times-Chandigarh-Monday-December-23-2013.pdf` |
| The **statement** appears in the document text | `statement_is_in_text` | a full masthead reported from a document containing only a browser print header (`12/24/13 The Pioneer`) — date grounding alone proved insufficient |
| The quote is ≥8 characters | `MIN_STATEMENT_CHARS` | fragments |
| The quote carries the year being proposed | `statement_supports_date` | a publisher imprint ("PUBLISHED BY The Energy and Resources Institute") paired with a date taken from the PDF `CreationDate` |
| The quote is not a bare year | `statement_is_year_only` | "© TERI, 2023" → 2023-01-01 invents a January publication |
| The quote gives the day | `statement_supports_the_day` | "Colombo, September 2007" → the 1st invents a day |
| Publication language governs *this* date | `publication_linkage_ok` | "first published in September 2020 and is being updated in 2023" proposing 2023; "January 2023 Final Report" with no publication verb at all |
| `confidence >= 0.9` | `MIN_OVERRIDE_CONFIDENCE` | near-misses |

Anything short of all of them becomes `review` (when the model saw something) or
`keep_page_date` (when it did not). Review is the honest landing place: it puts the
case in front of a person instead of silently changing a date or silently
discarding real evidence.

Three implementation details worth knowing:

- **Grounding is set by the caller, not the model.** `_grounded` and
  `_statement_grounded` are `PrivateAttr`s, deliberately absent from the schema the
  model answers, because a model cannot be trusted to certify its own grounding.
  `interpret()` sets them from `date_is_in_text` / `statement_is_in_text` against
  `evidence.head_text` — the document's own text **only**. The filename and the
  anchor are in the prompt too, and a date lifted from either is exactly what the
  rules forbid.
- **`_squash` forgives extraction artefacts and nothing else.** Statement matching
  compares lowercase alphanumerics with separators removed, which absorbs case,
  whitespace runs, line breaks, hyphenation across a break and punctuation styling.
  It cannot introduce a word, reorder words, expand an abbreviation or supply a
  name, so a quote that only matches after "normalisation" of that kind still
  fails.
- **`publication_linkage_ok` reads the 60 characters before the date** and lets the
  nearest cue win: a disqualifier (`updated`, `revised`, `effective`, `w.e.f`,
  `notified`, `held`, `scheduled`, `accessed`, `reprinted`, …) closer to the date
  than a qualifier (`published`, `issued`, `dated`, `released`, `edition of`, …)
  loses the override. A newspaper masthead (weekday + date) or a press dateline
  (place, comma, date carrying at least a day) is accepted without publication
  wording, because naming a paper with a weekday and date *is* an issue line — but
  still only if no update/effective cue governs it. A bare year is deliberately not
  accepted as a dateline, so "TERI, 2023" is not mistaken for one.

`MIN_OVERRIDE_CONFIDENCE` was raised from 0.85 to **0.9** after manual review: an
override now changes a date that nothing else in the system would have changed, so
it must be near-certain.

`prompt_version()` fingerprints the prompt, the JSON schema and both thresholds, so
a prompt edit is visible in the audit rows.

### The outcome

```python
effective_start_date = (decision.candidate_start_date if decision.action == "propose_override"
                else page_date)
```

**Only an override may move the date.** Every other outcome — including a review —
lands on the page's own date where that date is the file's to borrow, and on **no
date** where it is not. `resolve()` wraps everything in a `try/except` that returns
the page date on any unexpected error: a stale date is recoverable and a wrong one
is not. A model outage returns `None` from `interpret` and produces a
`keep_page_date` decision with `rule="llm_unavailable"`.

The drop is applied in two places that ask the same predicate — in `decide`, so
the audit row says `drop_page_date`, and again in `_outcome`, because the
interpreter can hand back `keep_page_date` or `needs_manual_review` for a file on
exactly such a page and a date the rules refused must not return through the
model's door.

On the document:

- `effective_start_date` — the resolved date, or `None` where none was
  established.
- `date_source` — `"document_title"`, `"document_copyright"` or `"document_text"`
  for the three kinds of override, `"no_evidence"` for a file left undated, else
  `"parent_page"`. `no_evidence` is recorded rather than left NULL so "we looked
  and found nothing" stays distinguishable from "this row predates the resolver",
  which is what `reconcile.date_checks.date_provenance_unrecorded` alarms on.
- `start_precision` — **inherited from the page**. A file hanging off a
  research paper is year-precision too; rendering its 1 January as a day would
  invent a January publication for the file exactly as it would for the page. For
  an override it is the decision's own — whatever the name or the statement
  actually established. `None` exactly when there is no date: a leftover `"day"`
  would read as a claim.
- `effective_end_date` / `end_precision` — **inherited whole**. A file on
  a completed project covers the period the project did. Cleared on an override:
  a statement about a publication gives a day or a year, never a period.
- `extra["edition_label"]` — set when an edition was found. **A reporting period is
  a label, never a date**: "Annual Report 2024-2025" sets this and leaves
  `effective_start_date` alone.
- `title` — `file.description or node.title or file.filename`. The anchor text wins,
  which is what tells editions of a series apart.
- `source_url` — the node's page. `file_url` — the URL the download actually
  succeeded on.

### The decision record

`documents_date_decision`, one row per document, overwritten (a current-state
snapshot, not an audit trail — `ingest_log` already is one). It holds the action,
the rule, the confidence, the date type, the edition label, the quoted evidence,
the raw LLM verdict as JSON, the prompt version, and
`current_start_date` — the page's own date — so a row reads as *"would have been
X, assigned Y"*.

**This table is also the review queue.** A case the resolver could not settle
safely lands here with `action="needs_manual_review"` rather than moving a date.

Kept out of the document itself on purpose: the payload gets the date and the
edition label, while the confidence, the quoted statement and the rule live here.
Fails open — an unreachable database costs one warning.

### Diagram to include: the PDF date decision

A flowchart from "PDF" through the four evidence tiers to `decide()`, with the three
deterministic outcomes; the `needs_llm` branch looping back through "read the PDF
head → decide again" before reaching the interpreter; and the interpreter's output
passing through the ten gates drawn as a funnel, with the count of survivors
labelled `propose_override` and everything else landing in `review` or
`keep_page_date`. Annotate the funnel edges with *what each gate rejects* — that is
where the value is.

---

## The undated-document flag

Back in `_handle`, after chunking and before indexing:

```python
if not doc.effective_start_date:
    flag("undated")
    logger.warning("Indexing %s (%s/%s) with no effective date; it will be "
                   "excluded from date-filtered results.", ...)
```

Not an error — some sources genuinely state no date, and inventing one would be
worse than having none. But an undated document is **invisible** to every
date-range filter rather than merely ranked low, so it must not pass silently.
The count reaches the run's throughput line as `indexed_without_date`, and
reconciliation's `documents_without_date` check reports the standing total.

---

## Validation at this stage

| Check | Where | On failure |
| --- | --- | --- |
| Bundle is in the mapping | `bundle_dates.field_for` | `created` kept; one INFO log per bundle |
| Configured field holds something | `bundle_dates.resolve` | `created` kept; INFO log; audit row |
| Value parses and is 1990…next year | `is_plausible` | `created` kept; WARNING log; **review row** |
| A full-date field did not hold a bare year | `bundle_dates.resolve` | Precision downgraded to `year` |
| End field holds something usable | `bundle_dates._read` | End dropped; review row (`end_invalid`) |
| The range is not backwards | `bundle_dates._with_range` | End dropped, never swapped; review row (`inverted`) |
| A usable end has a usable start | `bundle_dates.resolve` | End preserved; review row (`end_without_start`) |
| Page has a date at all | `date_rules.decide` | `needs_manual_review` |
| Page's own field settles it | `date_rules.decide` | — (short-circuits to `keep`) |
| PDF date is inside the document text | `date_is_in_text` | Downgraded to `review` |
| Quoted statement is inside the document text | `statement_is_in_text` | Downgraded to `review` |
| Quote carries the year / the day / publication language | three `DateInterpretation` methods | Downgraded to `review` |
| Confidence ≥ 0.9 | `safe_action` | Downgraded to `review` |
| Theme name is not a stringified boolean or a bucket | `theme_taxonomy.classify` | Row dropped |
| Document has an effective date at all | `_handle` | `undated` flag + WARNING; still indexed |

## Failure scenarios

| Scenario | Detection | Response | Recovery |
| --- | --- | --- | --- |
| CMS adds a new bundle | `reconcile.date_checks.unmapped_bundle_dates` | `created` kept (the safe direction), reported per sweep | Declare it in `BUNDLE_DATE_FIELDS` |
| CMS adds a new date field | `reconcile.date_checks.undeclared_source_date_field` | Ignored (the safe direction), reported per sweep | Classify it in `FIELD_KINDS`; map it if a bundle should use it |
| A bundle should take a different field | Nothing automatic | — | Edit `BUNDLE_DATE_FIELDS` and run `scripts.backfill_bundle_dates` |
| Configured field holds a non-date | `documents_date_decision.action='needs_manual_review'`, rule `bundle_field_invalid` | `created` kept | Fix the CMS value; a re-crawl heals the row |
| Stated date not applied to a row | `reconcile.date_checks.stated_date_not_applied` | Reported | Re-run `scripts.backfill_bundle_dates`. Note `app.ingestion.backfill` lifts dates out of chunk payloads and can overwrite a resolved value |
| An attachment's date or period differs from its page's | `reconcile.date_checks.attachment_date_adrift` | Reported | Re-run `scripts.backfill_bundle_dates` |
| A stored range runs backwards | `reconcile.date_checks.inverted_date_range` | Reported | `bundle_dates` never stores one, so something else wrote it; re-run `scripts.backfill_bundle_dates` |
| A bundle's two CMS dates contradict each other | `documents_date_decision.range_issue` | Start applied, end dropped, review row | Fix the CMS values; a re-crawl heals the row |
| `date_source` unrecorded | `date_provenance_unrecorded` | Reported | `scripts.backfill_date_provenance`; legacy rows are deliberately left unclaimed rather than blanket-labelled `created` |
| Year precision with a non-January day | `year_precision_not_january` | Reported | Investigate; value and precision disagree |
| LLM unavailable | `interpret` returns `None` | `keep_page_date`, `rule="llm_unavailable"`, confidence 0 | Next re-index re-attempts |
| LLM proposes a filename-derived date | Grounding checks | `review` | A human reads `documents_date_decision` |
| `theme_structure.json` missing or malformed | `require_taxonomy()` preflight | The run refuses to start (`TaxonomyUnavailable`); nothing is written | Restore the file (or fix `THEME_TAXONOMY_PATH`) and re-run |
| Date-decision table unreachable | `except` in both recorders | One warning; ingestion continues | Next re-index |

## Observability

- `documents.date_source` distribution — the single most useful date query:

```sql
SELECT date_source, start_precision, COUNT(*)
FROM documents GROUP BY 1, 2 ORDER BY 3 DESC;
```

- The review queue:

```sql
SELECT action, rule, COUNT(*) FROM documents_date_decision
GROUP BY 1, 2 ORDER BY 3 DESC;

SELECT document_id, candidate_start_date, current_start_date, confidence,
       LEFT(evidence, 120)
FROM documents_date_decision
WHERE action = 'needs_manual_review' ORDER BY updated_at DESC LIMIT 50;
```

- Where each bundle's dates come from, and which carry a period:

```sql
SELECT bundle, date_source, start_precision,
       COUNT(*)                                        AS documents,
       SUM(effective_end_date IS NOT NULL)                AS with_a_period
FROM documents WHERE source_type = 'website' GROUP BY 1, 2, 3 ORDER BY 1;
```

- Ranges the CMS could not state cleanly:

```sql
SELECT range_issue, COUNT(*) FROM documents_date_decision
WHERE range_issue IS NOT NULL GROUP BY 1;
```

- Why one document carries the date it does — the whole chain in one row:

```sql
SELECT bundle, date_source, date_type, rule, current_start_date,
       candidate_start_date, candidate_end_date, range_issue, evidence
FROM documents_date_decision WHERE document_id = ?;
```

- Log lines: `Bundle %r has no configured date field`, `Bundle %r states its date
  in %s, which is empty on this record`, `... whose value %r is not a usable
  date`, `... which holds only the year %r`, `Bundle %r has %s = %r, which is not
  a usable date; the end of the range is dropped`, `Bundle %r has %s (%s) after
  %s (%s)`, `Bundle %r has a usable %s but no usable %s`,
  `Discarding implausible %s value %r on a source record.`,
  `Discarding unparseable model date %r.`, each `safe_action` downgrade at INFO
  naming the gate that failed, and `Indexing %s … with no effective date`.
- `indexed_without_date` on the run's `ingest_throughput` line.
- Migration: `scripts.backfill_bundle_dates` (dry run by default).
  `scripts.backfill_source_dates` is retired and refuses to run.
- Related audit scripts: `scripts.audit_dates`, `scripts.audit_overrides`,
  `scripts.audit_annual_report_dates`,
  `scripts.eval_date_resolution`, `scripts.build_manual_review`.

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `date_resolution_enabled` | `true` | Off means every PDF plainly inherits its page's resolved date — no name read, no drop; decisions are still recorded. |
| `azure_openai_model`, `llm_structured_temperature` | — | The interpreter's model. |

## Hand-off

`ensure_content_hash()` is called by the builders, then `_handle` compares it and —
if a rebuild is needed — hands the document to `chunk_canonical`. See
[07](07-chunking-embedding-indexing.md).

**A date change does not move the content hash**, which is computed from body
text alone. That is why re-dating the corpus is a metadata migration
(`scripts.backfill_bundle_dates`) and not a re-index: an ordinary sweep finds
these documents unchanged and returns before rebuilding them, so ingestion code
alone never re-dates anything already ingested. See
[bundle-date-capture-plan.md](bundle-date-capture-plan.md) §12.

---

Previous: [05 — Extraction and Normalisation](05-extraction-and-normalisation.md) · Next: [07 — Chunking, Embedding and Indexing](07-chunking-embedding-indexing.md)
