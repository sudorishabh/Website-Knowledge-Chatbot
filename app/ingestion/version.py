"""What the ingestion pipeline was, when it produced a document.

A document is re-indexed when its *content* changes. Nothing watched the *code*,
so a corpus was pinned to whatever the pipeline did on the day each document was
first seen: four chunker correctness fixes, a chunk-id scheme change and a
payload cleanup all landed after the corpus was built and none of them ever
reached it, because `content_hash` covers body text and body text had not
changed. Roughly 99% of stored chunks came from a chunker with known, fixed
defects, and no mechanism existed to re-apply them.

This module is that missing signal. The version is stamped into the catalog row
and into every point's payload, and a document whose stored version differs from
:data:`PIPELINE_VERSION` is rebuilt on its next crawl even when its content is
byte-identical.

Bumping
-------
Bump the component whose behaviour changed, in the same commit as the change.
The test is simple: **would identical input now produce different output?**

* :data:`CHUNKING` — segmentation, packing, overlap, heading handling, section
  classification. Different chunks for the same text.
* :data:`CHUNK_IDENTITY` — how a chunk id is derived. Different ids for the same
  chunks, which also means stored vectors can no longer be found for reuse.
* :data:`PAYLOAD` — the fields written to each point. A reader expecting a field
  that old points lack, or points carrying a field the code no longer writes.
* :data:`EMBED_INPUT` — the exact string handed to the embedder (breadcrumb,
  overlap carry). Different vectors for the same chunk.

Do **not** bump for a refactor, a log line, a comment or a performance change
that leaves the output identical: every bump costs a full corpus reprocess, so a
bump that changes nothing is a bill with no benefit.

Components rather than one number so a reader can see *what* changed, and so the
reconciliation report can say "these points predate the payload change" rather
than only "these points are old".
"""
from __future__ import annotations

CHUNKING = 1
CHUNK_IDENTITY = 1
#: 2 — the publication-date vocabulary was removed. `published_at`,
#: `published_until`, `published_at_precision`, `published_until_precision` and
#: `document_published_at` became `effective_start_date`, `effective_end_date`,
#: `start_precision` and `end_precision` (the last was dropped: nothing ever
#: wrote it). Payload *keys* changed, which is exactly what this component is
#: for, so it is bumped rather than relying on the migration alone — a
#: deployment that skipped `scripts.backfill_bundle_dates` would otherwise serve
#: points whose date keys no reader consults, silently and without a signal.
#:
#: The bump is cheap: `_reusable_vectors` keys on chunk id + `embed_hash` +
#: `embed_model`, none of which this touches, so a re-indexed document reuses
#: every stored vector and nothing is re-embedded.
#:
#: 3 — an attached PDF's `effective_start_date` is now decided differently, so
#: the same crawl produces a different payload. Two rules changed
#: (`app.ingestion.date_resolution`): on a page holding several PDFs the file's
#: own name states its date, and where nothing states one and the page is dated
#: only by its Drupal creation stamp the file is left undated. Measured on the
#: live corpus, 164 attachments take a date from their name and 334 lose one
#: they should never have had.
#:
#: This is a *value* change, where the four components otherwise describe
#: structural ones — but it is the component that governs the payload and the
#: module's own test settles it: identical input now produces different output,
#: and without the bump a document whose body text has not changed keeps the
#: wrong date forever, which is the exact failure this module exists to prevent.
#: Vectors are reused on the same keys as before, so the reprocess re-embeds
#: nothing.
PAYLOAD = 3
EMBED_INPUT = 1

#: The version stamped on everything this pipeline writes. Short by design: it
#: lives on a VARCHAR(32) column and on every point payload in the collection.
PIPELINE_VERSION = f"c{CHUNKING}.i{CHUNK_IDENTITY}.p{PAYLOAD}.e{EMBED_INPUT}"

__all__ = [
    "CHUNKING",
    "CHUNK_IDENTITY",
    "PAYLOAD",
    "EMBED_INPUT",
    "PIPELINE_VERSION",
]
