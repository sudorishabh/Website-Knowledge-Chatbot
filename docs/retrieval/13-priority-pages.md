# 13 — Priority Pages

**Purpose.** Answer from the organisation's own pages as the live site shows
them, whenever a question concerns one of them — the home page (for the list of
themes), theme pages, regional centres, the people pages and profiles, and the
institutional pages — and leave every other question to the corpus exactly as
before.

**Inputs.** The question, its `ProcessedQuery` (the rewritten search query and
the resolved theme facet), the query vector the pipeline already computed, and
the page list `data/priority_crawl_pages.json`.

**Outputs.** A `PriorityEvidence` per question: the pages it needs and why,
the sections read from them as `ContextBlock`s, and the content fingerprint the
answer cache keys on.

**Components.** `app/retrieval/priority/` (`registry`, `fetch`, `extract`,
`people`, `match`, `evidence`, `projects`), two touch points in
`app/pipeline/query_pipeline.py`, one argument on `retriever.retrieve`, and the
`live page` header and rule in `app/generation/prompts.py`. A person's latest
publications come from `app/retrieval/structured/authored.py`.

---

## Why these pages are read live

The Drupal crawl ([ingestion 02](../ingestion/02-sources-and-acquisition.md))
reads entities through JSON:API. Most of these pages are not entities it can
read:

| Pages | What they are in Drupal | In the corpus? |
| --- | --- | --- |
| The home page (`/`) | the front page, its sections hand-built in one body field | no |
| 44 theme and regional-centre pages (`/climate`, `/goa`, …) | taxonomy terms rendered through Views (`theme_tabs`, `theme_experts`, `theme_projects`) | no — taxonomy terms are refused by design |
| `/services`, `/people/*` (4) | Views routes | no |
| `/profile/<slug>`, `/governing-council/<slug>` | user entities | no |
| 11 institutional pages (`/policy`, `/announcements`, `/fcra-financials`, …) | ordinary `page` nodes | yes — and their stored copy is now ignored |

Surveyed on 2026-09-24: every page answered in 0.05–0.94 s, server-rendered
(no content loaded by script), with `Cache-Control: max-age=300` and an ETag.
Before this, "tell me about climate change theme" was answered from a 2022
explainer and two learning-module PDFs — the theme page that states what the
organisation does on climate change was not in the corpus at all.

---

## The flow

```
question
  -> understanding (unchanged)
  -> chitchat / series lookups (unchanged, terminal)
  -> explicit_targets()            people, page names, groups, theme facet,  (rag.priority_targets)
                                   theme listing
       overrides the catalog route only for a person, an institutional or
       people page, the regional-centres group, or the home page when
       understanding read a theme listing (operation list_themes)
  -> structured / scoped_summary (unchanged unless overridden)
  -> embed_query (unchanged, one vector)
  -> gather()                      + description similarity; read pages;     (rag.priority_pages)
                                   choose sections
  -> semantic cache lookup         key = facets + priority page content hashes
  -> retrieve(..., priority=evidence)
       rerank (unchanged)
       drop_stored_copies()        website chunks of listed pages go; their  (rag.priority_stored_copies)
                                   PDFs stay; a listed page ranked in the top
                                   n is read live instead
       build context, attachments, graph merge (unchanged)
       merge()                     priority blocks lead; corpus keeps ≥2 slots (rag.priority_merge)
  -> generation                    header "official page · live page, read <date>
                                   · ... · link <url>"
  -> semantic cache store          key recomputed after retrieval
```

With `priority_pages_enabled=false` none of this runs: the package is never
imported, `retrieve` is called exactly as before, and cache keys are unchanged.
A question about one edition of a series ("the 2023-24 annual report") skips
priority pages; the edition's own PDF answers it.

---

## Which pages a question needs (`match.py`)

Strongest first; none costs a model call.

| Reason | Fires when | Opening section admitted |
| --- | --- | --- |
| `person` | a name on the people listings appears in the question → that person's profile | yes |
| `name` | a page's own multi-word name ("climate change", "green shipping"), a theme's topic followed by "theme" or "thematic" ("the water theme"), or a curated phrase ("director general", "tender", "founder", "fcra"); the home page by a phrase asking for the list of themes ("TERI's thematic areas", "themes area TERI works on"), or by any question understanding read as a theme listing | yes |
| `group` | "regional centres", … → one block built from the list itself; nothing fetched | — |
| `staff` | the question asks for the organisation's people as a group ("leading researchers", "air quality experts", "the climate team") and names no one → the two people listings whose descriptions sit closest to the question. Skipped when a person or a listing is already named, and held back when the question asks for a theme's or centre's team as it stands (see **Teams** below). Narrower than the catalog's person test: a bare "who" or "author" asks about one byline, which the corpus answers | yes |
| `theme` | understanding resolved a theme facet that is a page on the list | yes |
| `similar` | the query vector is ≥ `priority_match_threshold` (0.48) to one page's description and ≥ `priority_match_margin` (0.06) ahead of the next | yes |
| `surfaced` | retrieval ranked a listed page's stored copy within the top `n` | no — sections by score only |

A single common word never names a page on its own ("water", "policy",
"energy" name topics far more often than pages); those pages are reached
through "water theme", the theme facet and the description match. Descriptions
are embedded with the organisation's name removed — every description mentions
it and most questions do too, and left in it pulled "TERI's work on air
pollution" to the Policy page instead of Air — and a theme by its topic without
the file's "Theme", which otherwise pulled "what are TERI's thematic areas" to
the Environment page at 0.56.

**Themes.** The file lists the themes flat, one "<theme> Theme" page each, and
the list of themes is the home page's to give: its "Thematic Areas" section,
the seven areas with a line on each. So a question asking for the list reads
the home page, and one naming a theme reads that theme's page instead — the
home page gives way whenever a theme is named ("the climate change thematic
area"). A theme listing adds no page by description match, since it is about
no one theme. The overview phrases are deliberately tight: a page that matches
leads the context, and "which themes have the most publications" is a catalog
count — the home page overrules the catalog only for a `list_themes` reading.
When understanding reads a theme listing *and* names a theme, the home page
gives way as above and the catalog declines the listing too (see
[07](07-structured-answers.md#the-tools)), so the named theme's page answers.

**Calibration** (2026-09-24, 20 questions): most on-topic questions scored
0.49–0.75 against the right page, every off-list question stayed below 0.46,
and the on-topic questions under the bar ("mission of TERI", "latest tenders")
are caught by a curated name. The trace records the top three scores for every
question (`notes.priority_pages.similar_top`); recalibrate from those.

**People** (`people.py`): the three listings are read first (cached like any
page). A name matches when every significant part appears as a whole word —
honorifics ignored, single initials optional; a surname alone only after an
honorific and only when unique ("Dr Mathur" matches nobody: there are two).
Someone on two listings is one person, and the committee listing's profile is
used. The Team sections below name more people (68 on 2026-09-28, 29 of them on
no listing), each linked to whatever page the site gives them: five of those
links are not `/profile/<slug>`, and a guessed `/profile/` address is a 404. They
are matched after the listings, so the listing's profile wins for someone on
both. The Team sections span 45 pages, which cost 2.4 s cold to read, so they
are read off the question path (`evidence.team_roster`): a stale roster is
re-read in a background thread and the question uses the last one. The first
question after a start knows only the listings. A question naming nobody on any
of them goes to the corpus, as before. Understanding's small-talk override
reads the same names, so a bare "Prasoon Singh work" is a question, not small
talk.

**Teams**: 36 of the 38 theme pages (not Environment or the World Sustainable
Development Summit), and the Mukteshwar centre page, end with a
Team section — the page's own list of its people, each with a post (surveyed
2026-09-28; the largest, Climate Change, is 8 people in 562 characters). A
question that asks for people (a `staff` word, or "member(s)") and is about a
theme or centre page gets that page's Team section as its opening block
instead of its introduction. Before this, "climate change theme team" read the
Climate Change page and both staff listings, the three openings filled the
three blocks, and the team never reached the answer.

What else it reads depends on the wording:

| Question | Blocks |
| --- | --- |
| a team as it stands — "the climate change team", "members of the water theme" | the Team section; the staff listings are held back, since they would pad the team with people who are not on it, and read after all when the page shows no team |
| a choice among people — "top climate change researchers", "who leads the climate team", "climate change experts" | the Team section, then the two staff listings |
| people with no theme or centre named | the two staff listings, as before |

A choice is told from a team by its words: "top", "leading", "senior", "key",
"head", "chief", "main" and the like (`match._RANKING_WORDS`). The Team block
carries `team_of` (the page's title), and the answer's people note
(`prompts.staff_note`) names it: asked for the team, the answer names every
one of its people with their post; asked for the top people, it starts from
the team, adds from the listings only those whose post names that area, and
orders them by seniority.

**A person's work**: a question that names someone with a profile and asks
about their work (a word such as "work", "projects", "publications",
"research", "papers" or "wrote"; `match.asks_for_work`) gets three things. "Who
is X" gets the profile alone.

| Part | Source | Where it goes |
| --- | --- | --- |
| what they work on | the profile, a biography; nothing on it lists projects or publications | the first block, as for any named person |
| projects they had a role in | project passages (`completed_projects`, `ongoing_projects`) that name them as principal investigator, co-investigator, team member or contact (`projects.person_projects`), one block per project, the latest 4 | after the profile, before the corpus, on their own slots |
| their latest publications | the catalog: the 5 newest website pages recording them as an author, and how many in all (`structured.authored`) | a section after the answer, written by the pipeline and not the model |

No record links a person to a project: a project page is a title, two dates and
a PDF, and the knowledge graph links people to organisations only. The project
text does name them, so a mention counts only with a role heading shortly
before the name and no sentence end between. A reviewer, a moderator, a radio
guest or anyone an acknowledgement thanks did not do the project.

The catalog records authors as each document printed them, so one person is
often several strings: Manish Kumar Shrivastava was 18 documents under his full
name and five more as "Shrivastava Manish Kumar", "Mr Manish Shrivastava" and
"Shrivastava M K". A variant counts as theirs only when it fits them and no
other author sharing their surname, so "Datta A", which fits Alekhya and Arindam
Datta, is neither's. Only website pages are listed, as in every catalog
listing: an attached PDF carries its page's title and authors and would list
each publication twice.

The profile overrules the catalog route, as before. Before this, "Suruchi
Bhadwal work" was answered from her profile and listed none of her 22
publications, while "Prasoon Singh work" found no profile and got his
publications alone. The model is told the list follows and not to refer to it
(`prompts.publications_note`); it never writes the list, so titles, dates and
links are the catalog's. A catalog or search failure costs that part only.

---

## What is read, and what becomes a block

**Fetching** (`fetch.py`): only hosts on the list, checked again after
redirects; each page cached for `priority_cache_ttl` (300 s), then revalidated
with its ETag (an unchanged page costs a 304); one request per page at a time;
a failure serves the last good copy marked stale. **Documents are never
downloaded**: a URL ending in `.pdf`, `.docx`, `.xlsx`, … is refused before any
request, and a response that is not HTML is dropped unread.

**Extraction** (`extract.py`): one cut for every page — from the
`region-content` div (`region-home-sections` on the home page, which has no
`region-content`) to the footer regions — then sections opened by the page's
`h1` and its classed section titles (`block-title`, `section-heading`). A page
with no `h1` — only the home page — also opens a section at the first heading
of each `<section>` element, begins at its first section ("Thematic Areas",
past the banner carousel of headlines), and is titled by its document title.
Long sections are cut at line boundaries to ≤ 2,400 characters, each piece led
by its heading; a people listing is cut at 4,800, so it stays one block (the
Committee of Directors listing was 2,353 characters on 2026-09-28). Linked documents stay in the text as `label (URL)` — a PDF
behind "Read more" is labelled with its item's title — so the answer can hand
the link over.

**Selection** (`evidence.py`): the opening section of every page the question
is about (for a people question, a theme or centre page's Team section, as
above), then any other section scoring ≥ `priority_section_floor` (0.40)
against the question, best first, up to `priority_max_blocks` (3) from at most
`priority_max_pages` (3) pages. Section vectors are embedded once per distinct
text. A block carries `source_type="website"`, `source_authority=1.0` (so the
prompt calls it an *official page*), the live URL, `fetched_at`,
`content_hash` and `stale`.

**Merge** (`PriorityEvidence.merge`): the blocks lead the context. With
`priority_own_slots` on (the default since 2026-09-25) they come on top of the
corpus's `retrieval_top_k` slots and `context_token_budget`. A question that
reads live pages keeps every passage retrieval would have given it without
them. Off, they share the slots and the corpus keeps at least two
(`CORPUS_MIN_SLOTS`). That is how "TERI top researchers" came to be answered
from three live pages and three short news items: a 1,900-token context
against a 9,000-token budget.

Measured on 2026-09-25 with 24 questions (6 people, 5 theme, 6 page, 7 that
read no live page), both settings, two runs each, all through the full
pipeline on gpt-6-luna:

| Live-page questions (34 answers each) | Shared | Own slots |
| --- | --- | --- |
| Corpus passages | 3.5 | 5.9 |
| Context tokens | 3,618 | 5,577 |
| Answer words | 173 | 192 |
| Corpus passages cited | 1.3 | 1.9 |
| First token / total | 11.5 s / 14.6 s | 10.5 s / 13.6 s |

The questions that read no live page were unaffected, as they must be. A blind
pairwise judge (gpt-5.4, each answer shown with its own context, order
shuffled) scored the 33 pairs whose contexts differed. It found own slots more
complete (better in 12 pairs, worse in 6), less concise (7 vs 14) and about
level on accuracy (7 vs 10). Its overall pick was even: 14 own, 15 shared,
4 ties. None of the differences is significant (sign test, p ≥ 0.19). The
questions that read no live page set the noise floor. The setting cannot
change them, yet their retrieval varied between runs and the judge still moved
one accuracy score from 5 to 2. So the switch buys fuller answers at no
latency cost, not better answers. Most of the extra unsupported claims the
judge listed were loose paraphrase of the added passages, and a few were
peripheral items (a 2022 side event in a climate-theme overview).

---

## Conflicts with the corpus

The prompt's rule 9 normally prefers the block with the later page date. A
live block has no page date — only when it was read — so rule 9 carries a
sub-rule for it: for what the page states about the organisation itself (who
holds which post, who sits on a council or team, what a theme, centre or
programme covers, what it currently lists) the live page is the current
position and wins over any block that disagrees, whatever that block's date.
Its read date is never a publication date. A document it links was not read:
the answer gives its title and link, and nothing about its contents the page
does not say.

A figure a theme page merely summarises from a report is not page-owned; the
report, when it is in the context, remains the better source.

---

## Caching

The answer cache key gains `priority`: each contributing page's URL and the
first 16 hex digits of its content hash. An answer built on a page is reused
only while the page says the same thing; with no page involved the key is
exactly what it was. The key is recomputed after retrieval, because a
`surfaced` page may have joined; such an answer is stored under a key the next
lookup of the same question will not build, so it is not reused — correct, at
the cost of a cache hit.

`PIPELINE_REVISION` was bumped to `2026-09-24.1` with this feature, to
`2026-09-25.2` when the list of themes moved to the home page, to
`2026-09-28.1` when people questions about a theme moved to its Team section,
and to `2026-09-28.2` when a question for a person's work gained their projects
and publications.

---

## Latency

Measured on the live server: warm (page in the 5-minute cache, vectors
embedded), the whole feature costs ~40 ms per question (`rag.priority_targets`
13.6 ms + `rag.priority_pages` 26.4 ms). The first question after a restart
paid ~3.7 s, almost all of it embedding the 59 descriptions (60 since the home
page joined) and the page's
sections once; a page read itself took 83 ms.

The people index (2026-09-28) reads the three listings on the question path, as
before, and the 45 theme and centre pages for their Team sections in the
background. With every page extracted once per distinct HTML
(`evidence._extract`), the whole index costs 1–13 ms warm, against ~150 ms when
each read extracted afresh. A work question adds one vector-store scroll for
the projects and two catalog queries for the publications, which run beside
retrieval.

---

## Failure scenarios

| Scenario | Response |
| --- | --- |
| Site down or slow for a page | last good copy, marked stale in the header; never read before → no block, corpus only |
| Page answers 200 with no content (maintenance) | treated as a failure (< 80 characters extracted) |
| Redirect off the site | refused |
| Embedding service down | similarity trigger skipped; opening sections still admitted |
| Page list missing or malformed | empty registry, feature does nothing, one error log line |
| Theme redesign breaks extraction | as "no content" above; check `notes.priority_pages.reads[].sections` |
| Anything unexpected in `gather` | logged, no priority blocks, answer from the corpus |

---

## Operations

- **Adding or changing a page**: edit `data/priority_crawl_pages.json`
  (tracked in git) and restart. `page_url` and `site_url` are both read.
- **The theme names follow the same file.** The file no longer holds a
  hierarchy; `app/theme_structure.json` keeps the Main/Other tree that
  ingestion classifies against, and
  `tests/catalog/test_theme_map_matches_priority_pages.py` fails if the two
  name different themes. After changing the map, re-apply it to stored rows:
  `python -m scripts.reclassify_theme_rows --dry-run`, then without the flag.
- **Seeing a decision**: every trace holds `notes.priority_pages` — targets and
  reasons, similarity top three, each read (cache hit, stale, ms, sections,
  documents, hash), the blocks chosen, and how many stored copies were dropped.

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `priority_pages_enabled` | `true` | The whole feature. Off = corpus-only answers, byte-identical to before. |
| `priority_pages_path` | `""` | The page list; empty means `data/priority_crawl_pages.json`. |
| `priority_fetch_timeout` | `4.0` | Seconds per page request. |
| `priority_cache_ttl` | `300` | Seconds a page is reused before revalidation. |
| `priority_max_pages` | `3` | Pages read per question. |
| `priority_max_blocks` | `3` | Priority blocks per question. |
| `priority_own_slots` | `true` | The priority blocks come on top of the corpus's `retrieval_top_k` slots and `context_token_budget`. Off = they share them and the corpus keeps at least two slots. |
| `priority_match_threshold` | `0.48` | Description-similarity floor. |
| `priority_match_margin` | `0.06` | Lead the best page needs over the next. |
| `priority_section_floor` | `0.40` | Score a non-opening section needs. |

---

Previous: [12 — Operations and Troubleshooting](12-operations-and-troubleshooting.md) · Back to the [index](README.md)
