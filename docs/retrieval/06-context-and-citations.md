# 06 — Context and Citations

**Purpose.** Decide which candidate text the LLM actually reads, in what order,
with what page attribution — and afterward, describe those exact same blocks
back to the user as citations.

**Inputs.** A ranked `Sequence[Candidate]` (from [05](05-ranking-and-temporal-gating.md)).

**Outputs.** A `list[ContextBlock]` for generation, and a `list[Citation]` for
the API response.

**Components.** `app/retrieval/context/builder.py`, `app/retrieval/context/citations.py`,
`app/core/models/context.py`.

---

## `ContextBlock`: the shared contract

```python
@dataclass
class ContextBlock:
    n: int
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    conflict: bool = False
    also_available: list[dict[str, Any]] = field(default_factory=list)
```

Defined in `app/core/models/context.py` — the neutral core layer — rather than
in `retrieval/`, for exactly one reason: **`generation/` must never import a
retrieval implementation module.** Retrieval produces the ordered list via
`build_context`; generation formats and cites it; both agree on the shape
through `core/models/context.py` alone. Three helpers live alongside the
dataclass for the same reason — each is read by more than one layer and a copy
in either would be a copy that can drift:

| Helper | Answers | Read by |
| --- | --- | --- |
| `is_graph_facts(payload)` / `GRAPH_FACTS_KIND` | Is this the graph's verified-relationships block (not a document)? | The prompt builder (labels it), the citation builder (describes it specially), the reranker (scores it `1.0`) |
| `source_kind(payload)` / `WEBSITE_SOURCE_TYPES` | This payload's source type, with the legacy `article` alias folded into `website` | The context builder (conflict check), the citation builder |
| `page_span(payload)` | The `(first, last)` page the payload's text covers, or `(None, None)` | The prompt header and the citation — so the two can never disagree about which pages a block stands on |

`page_span` treats `page_range` as authoritative when present (the context
builder rewrites it to describe the text it actually admitted) and reads a lone
`page_number` as a one-page span. Nothing is inferred when neither is set — an
unpaginated source has no page, and inventing one is worse than showing none.

---

## Parent expansion: which text wins

A candidate is a **child** chunk, but the block it becomes may carry its
**parent's** wider text instead. `_admissible_text(cand, parents)` decides, in
order:

1. **The parent's text**, if there is a parent and its text is substantive.
2. **The child's own text**, if *it* is substantive — the orphan case (no
   parent — see [07, when a parent is emitted](../ingestion/07-chunking-embedding-indexing.md#when-a-parent-is-emitted))
   and the excluded-parent case, where the child is the largest admissible
   passage available.
3. **Nothing** — the candidate contributes no context at all.

"Substantive" means the text is not one of the non-searchable section types
(`toc`, `references`, `glossary` — `hybrid_search._NON_SEARCHABLE_SECTIONS`,
imported rather than duplicated so the search-time filter and this check can
never name different sections). This re-check exists because search excludes
those sections going *in*, but parent expansion then replaces the matched text
wholesale — a body child sitting inside a bibliography window would otherwise
carry the whole bibliography straight past a filter that had already excluded
it. An excluded child under a *substantive* parent still expands normally: the
classifier reads content, not headings, so a citation-dense run inside a
findings section is a fragment of that section, and the section is what the
block should carry.

### Provenance follows the text, not the chunk

```python
def _block_payload(child, parent):
    payload = dict(child)
    if parent is None:
        return payload
    for field_name in ("page_number", "page_range", "overlap_page_range"):
        payload.pop(field_name, None)
    span = parent.get("page_range")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        payload["page_range"] = list(span)
        payload["page_number"] = span[0]
    return payload
```

Identity stays the child's — the chunk that matched is the chunk a citation
resolves to — but the *page* has to follow whichever text is being shown.
Citing the child's single page for a passage that actually spans the whole
parent window claims a narrower source than the evidence supports: the reader
opens page 7 for a statement that may live on page 9. A parent with no
`page_range` at all (an unpaginated source) leaves the block with **no page**,
rather than keeping the child's — stretching one page number over text it does
not describe is never the honest option.

---

## Admission: dedup, budget, ordering

`_admit` walks candidates in ranked order and, for each, either adds a block or
skips it — mutating `blocks`, `block_vectors` and `seen_parents` as it goes so a
later call (attachment supplementation) can continue from where an earlier one
left off:

1. **Stop conditions** — `limit` total blocks reached, or `max_add` added by
   this call.
2. **Floor** — if set, skip candidates below `semantic_score`. Nothing sets it
   today; it is the surviving parameter of the per-source admission floors
   described under "One ranked pass" below.
3. **Parent-level dedup** — `seen_parents` is keyed by `parent_id or id`. A
   second child from a parent already represented contributes nothing new.
4. **Near-duplicate dedup** — cosine similarity against every already-kept
   block's vector; `>= dedup_cosine_threshold` (default `0.92`) counts as the
   same content. The duplicate is dropped from the blocks list, but if it is
   *linked* to the block it duplicates (see below), its payload is recorded in
   that block's `also_available` — so a citation can still name the alternate
   format.
5. **Text resolution** — `_admissible_text`, above. An admissible-but-blank
   text (whitespace only) is skipped.
6. **Token budget** — `_count_tokens` (tiktoken `cl100k_base`, or a
   ~4-chars-per-token heuristic offline) against `context_token_budget`
   (default `9000`). The very first block is always admitted even if it alone
   exceeds the budget — the check is `if blocks and spent + cost > budget`, so
   an empty context is never handed back purely on token size.

### One ranked pass

`_admit` is called **once**, over every candidate, in the reranker's order.
Source kind gets no budget, no floor and no reserved position: it is already
represented in that order as one term of the authority band, which sits below
relevance, temporal fit and — on a question about the present — recency. A newer
PDF can therefore lead an older web page.

It used to be called three times against the same mutable state:

| Call | Pool | `max_add` | Floor |
| --- | --- | --- | --- |
| 1 | Website candidates | `website_max_slots` (`2`) | `website_chunk_floor` (`0.30`) |
| 2 | Non-website candidates | `pdf_max_slots` (`2`) | none |
| 3 | Non-website candidates (again) | `1` | `pdf_high_confidence_floor` (`0.5`) |

Website first, unconditionally-admitted PDFs second, one extra high-confidence
PDF slot last, and nothing past that — and the emitted order was website-first
whatever the ranking said. Two consequences retired it. The ordering was
categorical rather than evidential, so a 2020 announcement led the 2023 brief
that corrected it (the "who is the director general" failure — see
[05](05-ranking-and-temporal-gating.md)); and the graph's facts block carries no
`source_type` by design, so it fell into the "not website" pool and competed
with PDF attachments for those two slots.

One thing was genuinely lost with it: a website candidate used to win any
near-duplicate tie against a PDF describing the same thing, so the PDF landed in
the website block's `also_available` rather than taking a slot. The
higher-ranked candidate now wins that tie instead — which is the same preference
expressed through the ranking rather than over it.

### Ordering: ranked, and numbered from 1

`_numbered` numbers the blocks in the order the reranker returned them. Block
`[2]` is the second-best piece of evidence, and the ordering degrades
monotonically from there.

It used to be `_order_for_attention`:

```python
def _order_for_attention(blocks):
    if len(blocks) <= 2:
        return blocks
    head, tail = blocks[0::2], blocks[1::2]
    return head + tail[::-1]
```

For three or more blocks this interleaved — odd-ranked blocks kept their order
at the front, even-ranked ones were appended **reversed**, so `[1,2,3,4,5]` came
out `[1,3,5,4,2]`. The two best blocks landed at the two ends of the context and
the weaker ones were pushed toward the middle, matching the "lost in the middle"
effect. Nothing in the repo ever claimed more than a resemblance; this document
previously described it as "inferred from the code's shape, not a claim backed
by an in-repo comment".

Two things retired it:

- **It only ever ran on the minority path.** `prefer_website_enabled` is on in
  production, so an ordinary question took the segregated build, which emitted
  its own website-first order and never called this. Unifying that build put
  every question through a reordering production had not been using.
- **The ranked order now carries meaning.** For a question about the present,
  rank encodes how recent the evidence is (see
  [05](05-ranking-and-temporal-gating.md)); scrambling it discards the signal
  the ranking exists to express. Measured on the reported query: the block
  ranked **2nd** — a 2023 brief saying the subject "was earlier ... at TERI as
  its Director General" — was moved to display position **6**, below a 2020 page
  ranked 6th that called him TERI's Director General in the present tense. The
  answer led with the 2020 page.

The strongest block is still first. The difference is that the second strongest
is now second.

---

## Conflict flagging

```python
def _conflicting(a, b):
    if _same_document(a, b): return False
    if _same_source_two_formats(a, b): return False
    return _linked(a, b)
```

A conflict is a disagreement **between sources**, so it requires two distinct
documents — `_same_document` checks a union of `document_id`/`pdf_id`/
`article_uuid`, since `_admit` already deduplicates by *parent*, and an overlap
here most often means two sections of the same document reached the context
through two different children.

Two documents that are the **same content in two formats** — a website node and
its own attached PDF, linked by `linked_pdf_id`/`linked_article_uuid` and
matching kinds under `source_kind` — are explicitly not a conflict either.

What remains — genuinely linked but not the same document and not the
same-content-two-formats case — is flagged, on both blocks (`_flag_conflicts`
compares every pair once). The flag reaches the API response and the
generation prompt's "prefer the later page date" rule, so it is
load-bearing, not cosmetic.

**Sharing a parent node is deliberately not treated as a conflict signal.**
Editions of one publication do arrive that way — separate attachment documents
under a single Drupal node — but so does every catalogue page, and on this
corpus the latter dominates: the largest such nodes carry 69 financial
statements, 68 announcements, 43 brochures under one node. The two shapes are
indistinguishable from the payload alone (same node, same title, same
`effective_start_date`), and an earlier version that treated shared-parent as a
conflict flagged roughly a quarter of all answers, mostly wrongly. Telling them
apart needs a content signal and a threshold measured against a labelled set,
not a guess — so today, the honest reading of "two files under one page" is
that they are two files under one page, not a contradiction.

---

## Citations: describing blocks back to the user

`build_citations(blocks)` maps `_citation_from_block` over the finished list.
Both the primary citation and every `also_available` alternate are built from
the **same** function, `_source_from_payload` — a payload cannot describe
itself two ways depending on which slot it lands in:

```python
def _source_from_payload(payload):
    start, end = page_span(payload)
    return CitationSource(
        type=_source_type(payload), title=payload.get("title"),
        url=_primary_url(payload), page=start, page_end=end,
        section=payload.get("section_heading"), edition=payload.get("edition_label"),
    )
```

### The URL

`_primary_url`: a website node links to its own page (`source_url`) — never to
an attachment it happens to carry, or a PDF citation would render under "Web
pages". A PDF links to the attachment URL it was downloaded from, anchored at
the evidence's first page via `_with_page` (`#page=N`), so opening the link
lands where the quoted passage begins rather than somewhere inside a
multi-hundred-page document.

### The type vocabulary

`_source_type` returns exactly one of ingestion's own two values — `website` or
`pdf_attachment` (the pre-rename `article` alias folds into `website`). There is
deliberately no second, citation-side vocabulary to translate into: keeping one
name space is what stopped the same PDF coming back as `pdf_attachment` in one
slot of a response and `pdf` in another.

### The graph's citation is a special case

```python
def _graph_citation(block):
    ...
    return Citation(n=block.n, type=GRAPH_CITATION_TYPE,
                     title=f"Knowledge graph — verified relationships ({detail})",
                     url=None, document_id=None)
```

Described separately from `_source_from_payload` because that function
describes *documents*, and the graph's facts block is not one. Passed through
the ordinary path, the block's deliberately-absent `source_type` fell through
to `_source_type`'s `or "pdf_attachment"` default — so a set of verified
relationships was cited as an untitled, unopenable PDF attachment, and the
frontend (which renders a citation chip as `title || document_id || type`)
showed users the literal string "pdf_attachment" under a "PDFs" heading. The
fix gives the block its own type (`GRAPH_CITATION_TYPE = "knowledge_graph"`), no
URL — there is nothing to open; the block's provenance is the claim ids on its
own lines plus the evidence blocks cited separately beneath it — and a title
that names the record count and whether it reflects current or historical
relationships.

---

## What is out of scope here

`retriever.py` calls `build_context` more than once per request when
`answer_format == "detailed"` (attachment supplementation) and merges in the
graph's blocks afterward when the graph route contributed anything — both are
orchestration decisions made in `retriever.py`, not in `builder.py` itself, and
are not detailed in this document. Temporal gating, which runs on the finished
block list produced here, is covered in
[05](05-ranking-and-temporal-gating.md#temporal-gating-the-upcoming-problem).

---

## Failure scenarios

| Scenario | Detection | Response | Recovery |
| --- | --- | --- | --- |
| Parent fetch (`client.retrieve`) fails | `except Exception` in `_fetch_parents` | Logged exception, empty parent map returned | Every candidate falls back to its own child text |
| A candidate's parent id names a point that no longer exists | Missing from `_fetch_parents`'s result dict | `parents.get(...)` returns `None` → child text used | None needed — same as no parent |
| A candidate carries no vector | `_admit`'s cosine check is skipped (`if cand.vector and kvec`) | The candidate is never deduplicated against, only ever a dedup target | — |
| Token budget too small for even one block | First block's cost exceeds `token_budget` | Admitted anyway (the `if blocks and ...` guard exempts the first) | Context may exceed the nominal budget by one block's worth |
| tiktoken unavailable | `_encoder()` catches the import failure | Falls back to the 4-chars/token heuristic | Token counts are approximate; budget behaviour is unaffected in kind |

## Configuration

| Setting | Default | Effect |
| --- | --- | --- |
| `retrieval_top_k` | `6` | `limit` — maximum blocks in a context. |
| `context_token_budget` | `9000` | Token ceiling across all admitted block text. |
| `dedup_cosine_threshold` | `0.92` | Cosine similarity above which two blocks count as the same content. |

## Supersession

After the conflict flags, `flag_supersession` compares blocks pairwise, subject
to four conditions — all required:

1. the question is **CURRENT** (for any other question the older statement may
   be exactly what was asked for);
2. both blocks mention everything the question named. The subject comes from
   `strategies.extract_key_terms`, the same helper the keyword leg uses;
3. they are at least **180 days** apart;
4. the newer one puts a role or affiliation in the past ("was earlier at",
   "until 2021", "former", "stepped down") **within 400 characters of a mention
   of the subject**.

The older block is marked `superseded`, the newer `supersedes`. Both markers
reach the prompt in the block header, and rule 9 tells the model to date the
older block's claims rather than state them as current fact.

Conditions 2 and 4 are precision, and they were learned the hard way. The first
version compared the blocks to *each other* — two or more shared capitalised
words — and searched the whole of the newer block for past framing. Replayed
against the reported query it marked a 2019 building-retrofit PDF as superseded
by a 2021 announcement about a person it never mentions. Blocks run to thousands
of characters and this corpus is all one organisation, so pairwise name overlap
is nearly free and an unanchored "formerly" turns up almost every time.

It is advisory, never a filter: the older block stays in the context and stays
citable, because it is still the evidence for what was true at its own date.

Deliberately conservative, and deliberately modest about what it claims. It does
not read *which* role, or *whose* — establishing that the past-framed role in
the newer block is the same role the older one asserts needs relation
extraction, and regular expressions would produce a confident wrong answer more
often than a useful one. The authoritative version of this comparison is the
claim graph's `valid_from` / `valid_until` ladder
([08](08-knowledge-graph-retrieval.md)); this is the document-evidence fallback
for the common case where the graph holds no claim.

## Hand-off

`ContextBlock`s go two places: to generation, formatted into the prompt and
cited in the final prose (`app/generation/` — see
[09 — Generation and Answer Synthesis](09-generation-and-synthesis.md)),
and to `build_citations` here, whose `Citation`/`CitationSource` objects
(`app/schemas/query.py`) ride alongside the answer in the API response.

---

Previous: [05 — Ranking and Temporal Gating](05-ranking-and-temporal-gating.md) · Next: [07 — Structured Answers](07-structured-answers.md)
