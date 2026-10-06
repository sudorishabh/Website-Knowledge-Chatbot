"""Re-read the author bylines already in the catalog with the corrected parser.

Ingestion used to split free-text author fields on every comma, so "Sehgal,
Meena" was stored as two authors and "Dhup Saumya and Dhawan Vibha" as one (see
`app.ingestion.bylines`). New ingests are read correctly; this corrects the rows
already written, from the source values `documents.raw_meta` keeps verbatim. A
local reshuffle like ``scripts.backfill_tag_facet``: no network, no Drupal, no
re-chunking or re-embedding.

**Which field.** A document is only corrected when one of its author fields,
read the old way, reproduces its stored authors exactly — that field is provably
where they came from, and only its reading changes. Ingestion's choice of field
depends on the order fields arrived in, which `raw_meta` does not keep (its keys
come back sorted), so picking a field afresh would switch some documents to a
different field altogether. That would be a different change from this one.

**Attachments.** A PDF inherits its parent page's authors. An attachment whose
stored authors equal its parent's old ones gets the parent's correction.

**Where.** The catalog's author rows (`documents_author`) and the ``authors`` list
on every Qdrant point of the document. The knowledge graph is not touched: it
reads authors from the catalog when a document is next processed.

Default is a dry run. ``--apply`` writes, and first saves every document's old
and new authors to an undo file under ``logs/``.

    python -m scripts.backfill_author_bylines
    python -m scripts.backfill_author_bylines --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.ingestion.bylines import authors_from
from app.ingestion.canonical import author_fields

# documents_author.author is VARCHAR(255); a value is compared as stored.
_WIDTH = 255


def old_reading(value: Any) -> list[str]:
    """How ingestion read an author field before the fix: list items as given,
    a string split on every comma. Frozen here on purpose — it is the reading
    the stored rows came from, and must not follow later parser changes."""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [part.strip() for part in str(value or "").split(",")]
    return [item for item in items if item]


@dataclass
class Fix:
    document_id: str
    old: list[str]
    new: list[str]
    #: The raw_meta field the authors came from ("a -> b" when the first held
    #: no name and the second is read instead); None for an attachment.
    field: str | None = None
    #: The page an attachment inherits from; None for the page itself.
    parent: str | None = None


def _stored_form(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v[:_WIDTH] for v in values if v))


def source_field(meta: dict[str, Any], stored: set[str]) -> tuple[str, Any] | None:
    """The author field whose old reading produced exactly ``stored``."""
    for key, value in author_fields(meta):
        old = set(_stored_form(old_reading(value)))
        if old and old == stored:
            return key, value
    return None


def _fallback(meta: dict[str, Any], used: str) -> tuple[str, list[str]] | None:
    """The other author field ingestion now falls back to when the one used
    holds no name (`canonical._authors`) — but only when exactly one other field
    has names, since raw_meta's sorted keys cannot say which would come first."""
    found = {
        key: names
        for key, value in author_fields(meta)
        if key != used and (names := _stored_form(authors_from(value)))
    }
    if len({tuple(names) for names in found.values()}) != 1:
        return None
    return next(iter(found.items()))


def plan(
    documents: Iterable[tuple[str, dict[str, Any]]],
    stored: dict[str, set[str]],
    attachments: dict[str, list[str]],
) -> list[Fix]:
    """Every document whose authors read differently now, pages first."""
    fixes: dict[str, Fix] = {}
    for document_id, meta in documents:
        have = stored.get(document_id) or set()
        found = source_field(meta, have) if have else None
        if found is None:
            continue
        field, value = found
        new = _stored_form(authors_from(value))
        if not new and (fallback := _fallback(meta, field)) is not None:
            other, new = fallback
            field = f"{field} -> {other}"
        if set(new) == have:
            continue
        fixes[document_id] = Fix(document_id, sorted(have), new, field=field)
        for attachment in attachments.get(document_id, ()):
            if attachment not in fixes and stored.get(attachment) == have:
                fixes[attachment] = Fix(attachment, sorted(have), new, parent=document_id)
    return list(fixes.values())


def _load() -> tuple[list[tuple[str, dict]], dict[str, set[str]], dict[str, list[str]]]:
    from app.catalog.db import state_table
    from app.core.clients import mysql_connection

    table = state_table()
    with mysql_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT document_id, raw_meta FROM `{table}` WHERE raw_meta IS NOT NULL")
        documents = []
        for row in cur.fetchall():
            raw = row["raw_meta"]
            meta = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if isinstance(meta, dict):
                documents.append((row["document_id"], meta))
        cur.execute(f"SELECT document_id, author FROM `{table}_author`")
        stored: dict[str, set[str]] = {}
        for row in cur.fetchall():
            stored.setdefault(row["document_id"], set()).add(row["author"])
        cur.execute(f"SELECT document_id, file_uuid FROM `{table}_attachment`")
        attachments: dict[str, list[str]] = {}
        for row in cur.fetchall():
            attachments.setdefault(row["document_id"], []).append(row["file_uuid"])
    return documents, stored, attachments


def _rewrite_points(client: Any, collection: str, fix: Fix) -> int:
    """Set the document's ``authors`` on every point; delete the key when there
    are none, since the payload builder never writes an empty list."""
    from qdrant_client import models as qm

    flt = qm.Filter(must=[qm.FieldCondition(
        key="document_id", match=qm.MatchValue(value=fix.document_id))])
    points = client.count(collection_name=collection, count_filter=flt, exact=True).count
    if not points:
        return 0
    selector = qm.FilterSelector(filter=flt)
    if fix.new:
        client.set_payload(collection_name=collection, payload={"authors": fix.new},
                           points=selector)
    else:
        client.delete_payload(collection_name=collection, keys=["authors"],
                              points=selector)
    return points


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    parser.add_argument("--show", type=int, default=25, help="examples to print")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    documents, stored, attachments = _load()
    fixes = plan(documents, stored, attachments)
    pages = [f for f in fixes if f.parent is None]
    print(f"documents with raw_meta     : {len(documents)}")
    print(f"documents with author rows  : {len(stored)}")
    print(f"pages to correct            : {len(pages)}")
    print(f"attachments to correct      : {len(fixes) - len(pages)}")
    for fix in pages[: args.show]:
        print(f"\n  {fix.document_id}  ({fix.field})")
        print(f"    was: {fix.old}")
        print(f"    now: {fix.new}")
    if not args.apply:
        print("\nDry run: nothing written. Re-run with --apply.")
        return 0

    from app.catalog import state
    from app.config import get_settings
    from app.core.clients import get_qdrant_client

    undo = Path("logs") / f"author_bylines_undo_{datetime.now():%Y%m%d-%H%M%S}.json"
    undo.parent.mkdir(parents=True, exist_ok=True)
    undo.write_text(json.dumps([asdict(f) for f in fixes], ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"\nundo file: {undo}")

    client = get_qdrant_client()
    collection = get_settings().qdrant_collection
    rows = points = 0
    for fix in fixes:
        if state.replace_authors(fix.document_id, fix.new):
            rows += 1
        points += _rewrite_points(client, collection, fix)
    print(f"applied: {rows} documents' author rows rewritten; {points} points updated")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
