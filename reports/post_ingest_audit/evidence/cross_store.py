# -*- coding: utf-8 -*-
"""READ-ONLY cross-store comparison: MySQL catalog vs Qdrant payload, per document."""
import io, sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.core.clients.database import mysql_connection
from app.core.clients.vector_store import get_qdrant_client
from app.config import get_settings

q = get_qdrant_client()
coll = get_settings().qdrant_collection

# One representative payload per document (first point seen).
payload = {}
offset = None
while True:
    pts, offset = q.scroll(
        collection_name=coll, limit=4000, offset=offset, with_vectors=False,
        with_payload=["document_id", "effective_start_date", "effective_end_date",
                      "start_precision", "end_precision", "title", "bundle",
                      "source_type", "pipeline_version", "is_current"])
    if not pts:
        break
    for p in pts:
        pl = p.payload or {}
        payload.setdefault(pl.get("document_id"), pl)
    if offset is None:
        break
print(f"documents represented in Qdrant: {len(payload):,}")

with mysql_connection() as c, c.cursor() as cur:
    cur.execute("""SELECT document_id, title, bundle, source_type, doc_version,
                          pipeline_version, effective_start_date, effective_end_date,
                          start_precision, end_precision, date_source
                   FROM documents WHERE indexed_at IS NOT NULL""")
    catalog = {r["document_id"]: r for r in cur.fetchall()}
print(f"documents indexed in MySQL     : {len(catalog):,}")


def day(v):
    return str(v)[:10] if v is not None else None


mis = Counter()
examples = {}
for did, row in catalog.items():
    pl = payload.get(did)
    if pl is None:
        mis["absent from Qdrant"] += 1
        continue
    checks = {
        "title": (str(row["title"] or "").strip(), str(pl.get("title") or "").strip()),
        "bundle": (str(row["bundle"]), str(pl.get("bundle"))),
        "source_type": (str(row["source_type"]), str(pl.get("source_type"))),
        "pipeline_version": (str(row["pipeline_version"]), str(pl.get("pipeline_version"))),
        "effective_start_date": (day(row["effective_start_date"]),
                                 day(pl.get("effective_start_date"))),
        "effective_end_date": (day(row["effective_end_date"]),
                               day(pl.get("effective_end_date"))),
        "start_precision": (row["start_precision"], pl.get("start_precision")),
        "end_precision": (row["end_precision"], pl.get("end_precision")),
    }
    for field, (a, b) in checks.items():
        # Precision is written to the payload only when it is year or month;
        # day is represented by its absence, so compare on that convention.
        if field in ("start_precision", "end_precision"):
            a = a if a in ("year", "month") else None
        if a != b:
            mis[field] += 1
            examples.setdefault(field, []).append((did, a, b))

print()
print("=" * 74)
print("MYSQL vs QDRANT MISMATCHES (per document)")
print("=" * 74)
if not mis:
    print("  none")
for field, n in mis.most_common():
    print(f"  {field:24} {n:>7}")
    for did, a, b in (examples.get(field) or [])[:3]:
        print(f"      {did[:44]:44} mysql={a!r} qdrant={b!r}")

print()
print("=" * 74)
print("PRECISION REPRESENTATION")
print("=" * 74)
mysql_prec = Counter(str(r["start_precision"]) for r in catalog.values())
qd_prec = Counter(str((payload.get(d) or {}).get("start_precision")) for d in catalog)
print(f"  MySQL start_precision : {dict(mysql_prec)}")
print(f"  Qdrant start_precision: {dict(qd_prec)}")
print()
print("  (day precision is represented in the payload by absence, so 'None' in")
print("   Qdrant is correct for day; a MySQL 'month' with Qdrant None is not.)")
