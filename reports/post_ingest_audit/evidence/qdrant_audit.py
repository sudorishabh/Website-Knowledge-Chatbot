# -*- coding: utf-8 -*-
"""READ-ONLY Qdrant audit: payload integrity, parent/child shape, cross-store."""
import io, sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.config import get_settings
from app.core.clients.vector_store import PAYLOAD_INDEXES, get_qdrant_client
from app.core.clients.database import mysql_connection
from app.ingestion.version import PIPELINE_VERSION

s = get_settings()
q = get_qdrant_client()
coll = s.qdrant_collection
info = q.get_collection(coll)
schema = info.payload_schema or {}

print("=" * 74)
print("QDRANT COLLECTION")
print("=" * 74)
print(f"  name           : {coll}")
print(f"  points         : {info.points_count:,}")
print(f"  vector size    : {info.config.params.vectors.size}")
print(f"  distance       : {info.config.params.vectors.distance}")
print(f"  indexed fields : {len(schema)}")
declared = set(PAYLOAD_INDEXES)
stored = set(schema)
print(f"  declared but missing : {sorted(declared - stored) or 'none'}")
print(f"  stored but undeclared: {sorted(stored - declared) or 'none'}")
print()
print(f"  {'field':26} {'type':10} {'points':>10}  declared?")
for name in sorted(schema):
    v = schema[name]
    print(f"  {name:26} {str(v.data_type):10} {v.points:>10}  "
          f"{'yes' if name in declared else 'NO -- stray'}")

# --- full scroll, payload integrity ---------------------------------------
print()
print("=" * 74)
print("PAYLOAD INTEGRITY (full scan)")
print("=" * 74)
REQUIRED = ["document_id", "chunk_id", "is_parent", "is_current", "source_type",
            "bundle", "effective_start_date", "title", "pipeline_version"]
counts = Counter()
missing = Counter()
bad_cat = Counter()
versions = Counter()
models = Counter()
precisions = Counter()
sources = Counter()
doc_ids = set()
chunk_ids = Counter()
parents_by_doc = Counter()
children_by_doc = Counter()
parent_refs = Counter()
empty_facets = Counter()
no_text = 0
giant = 0
seen = 0

offset = None
while True:
    pts, offset = q.scroll(collection_name=coll, limit=4000, offset=offset,
                           with_payload=True, with_vectors=False)
    if not pts:
        break
    for p in pts:
        seen += 1
        pl = p.payload or {}
        did = pl.get("document_id")
        doc_ids.add(did)
        chunk_ids[pl.get("chunk_id")] += 1
        for f in REQUIRED:
            if pl.get(f) is None:
                missing[f] += 1
        if pl.get("is_parent"):
            counts["parent"] += 1
            parents_by_doc[did] += 1
        else:
            counts["child"] += 1
            children_by_doc[did] += 1
            if pl.get("parent_chunk_id"):
                parent_refs[pl["parent_chunk_id"]] += 1
        versions[str(pl.get("pipeline_version"))] += 1
        models[str(pl.get("embed_model"))] += 1
        precisions[f"{pl.get('start_precision')}/{pl.get('end_precision')}"] += 1
        sources[str(pl.get("date_source"))] += 1
        for cat in (pl.get("categories") or []):
            if str(cat).strip().lower() in ("true", "false"):
                bad_cat[str(cat)] += 1
        for facet in ("categories", "tags", "authors"):
            if pl.get(facet) == []:
                empty_facets[facet] += 1
        text = pl.get("chunk_text") or ""
        if not text.strip():
            no_text += 1
        if len(text) > 20000:
            giant += 1
    if offset is None:
        break

print(f"  points scanned            : {seen:,}")
print(f"  parent points             : {counts['parent']:,}")
print(f"  child points              : {counts['child']:,}")
print(f"  distinct document_ids     : {len(doc_ids):,}")
print(f"  duplicate chunk_id values : {sum(1 for v in chunk_ids.values() if v > 1)}")
print(f"  points with empty text    : {no_text}")
print(f"  points >20k chars         : {giant}")
print(f"  boolean True/False category leakage: {sum(bad_cat.values())}")
print()
print("  missing required payload fields:")
for f in REQUIRED:
    print(f"    {f:26} {missing.get(f, 0):>8}")
print()
print(f"  pipeline_version on points : {dict(versions)}")
print(f"  embed_model                : {dict(models)}")
print(f"  empty facet lists          : {dict(empty_facets)}")
print()
print(f"  start/end precision (top 8): {dict(precisions.most_common(8))}")
print(f"  date_source (top 8)        : {dict(sources.most_common(8))}")

# --- orphan analysis -------------------------------------------------------
print()
print("=" * 74)
print("ORPHANS AND CROSS-STORE")
print("=" * 74)
parent_ids = {cid for cid, n in chunk_ids.items()}
dangling = [pid for pid in parent_refs if pid not in parent_ids]
print(f"  children whose parent_chunk_id is absent: {len(dangling)}")
docs_with_children_no_parent = [d for d in children_by_doc
                                if d not in parents_by_doc]
print(f"  documents with children but no parent   : {len(docs_with_children_no_parent)}")

with mysql_connection() as c, c.cursor() as cur:
    cur.execute("SELECT document_id FROM documents WHERE indexed_at IS NOT NULL")
    catalog = {r["document_id"] for r in cur.fetchall()}
print(f"  catalog documents (indexed)             : {len(catalog):,}")
print(f"  in Qdrant but NOT in catalog            : {len(doc_ids - catalog):,}")
print(f"  in catalog but NOT in Qdrant            : {len(catalog - doc_ids):,}")
