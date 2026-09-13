# -*- coding: utf-8 -*-
"""READ-ONLY retrieval validation against the live collection.

Uses the production filter builder (`date_scope_filter`) and counts matches by
scrolling. No writes, no embedding calls, no code changes.
"""
import io, sys
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.config import get_settings
from app.core.clients.vector_store import get_qdrant_client
from app.core.clients.database import mysql_connection
from app.retrieval.understanding.filters import date_scope_filter

q = get_qdrant_client()
coll = get_settings().qdrant_collection
UTC = timezone.utc


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def count(lo, hi, extra=None):
    """Documents (not points) matching a date scope."""
    f = date_scope_filter(at(lo) if lo else None, at(hi) if hi else None)
    if extra is not None:
        from qdrant_client.models import Filter
        f = Filter(must=[f, extra]) if f else extra
    seen, offset = set(), None
    while True:
        pts, offset = q.scroll(collection_name=coll, limit=4000, offset=offset,
                               scroll_filter=f, with_payload=["document_id"],
                               with_vectors=False)
        if not pts:
            break
        for p in pts:
            seen.add((p.payload or {}).get("document_id"))
        if offset is None:
            break
    return len(seen)


print("=" * 76)
print("DATE-FILTER BEHAVIOUR ON THE LIVE CORPUS")
print("=" * 76)
cases = [
    ("documents from 2022",                 "2022-01-01", "2023-01-01"),
    ("documents from 2023",                 "2023-01-01", "2024-01-01"),
    ("after March 2023 (open upper bound)",  "2023-03-01", None),
    ("before 2015 (open lower bound)",       None,        "2015-01-01"),
    ("June 2022 only",                       "2022-06-01", "2022-07-01"),
    ("a single day: 2024-09-05",             "2024-09-05", "2024-09-06"),
    ("2019 through 2021",                    "2019-01-01", "2022-01-01"),
]
for label, lo, hi in cases:
    print(f"  {label:38} {count(lo, hi):>7,} documents")

print()
print("=" * 76)
print("PRECISION IS RESPECTED")
print("=" * 76)
# A year-precision document stored as 2022-01-01 must match a June-2022 query.
with mysql_connection() as c, c.cursor() as cur:
    cur.execute("""SELECT document_id, effective_start_date FROM documents
                   WHERE start_precision='year'
                     AND YEAR(effective_start_date)=2022 LIMIT 1""")
    year_doc = cur.fetchone()
    cur.execute("""SELECT document_id, effective_start_date FROM documents
                   WHERE start_precision='month' LIMIT 1""")
    month_doc = cur.fetchone()
from qdrant_client.models import FieldCondition, MatchValue

if year_doc:
    cond = FieldCondition(key="document_id",
                          match=MatchValue(value=year_doc["document_id"]))
    print(f"  year-precision doc {year_doc['document_id'][:20]} "
          f"({str(year_doc['effective_start_date'])[:10]})")
    print(f"    matches June 2022 query : {count('2022-06-01','2022-07-01',cond) > 0}"
          "   <- must be True (source said only '2022')")
    print(f"    matches 2022 query      : {count('2022-01-01','2023-01-01',cond) > 0}")
    print(f"    matches 2023 query      : {count('2023-01-01','2024-01-01',cond) > 0}"
          "  <- must be False")
if month_doc:
    d = str(month_doc["effective_start_date"])[:10]
    y, m = d[:4], d[5:7]
    nxt = f"{int(y)+1}-01-01" if m == "12" else f"{y}-{int(m)+1:02d}-01"
    cond = FieldCondition(key="document_id",
                          match=MatchValue(value=month_doc["document_id"]))
    mid = f"{y}-{m}-15"
    print(f"\n  month-precision doc {month_doc['document_id'][:20]} ({d})")
    print(f"    matches mid-month ({mid}) : "
          f"{count(mid, f'{y}-{m}-16', cond) > 0}"
          "   <- SHOULD be True; payload lost the month marker")

print()
print("=" * 76)
print("PERIOD OVERLAP (completed_projects / events)")
print("=" * 76)
bundle = lambda b: FieldCondition(key="bundle", match=MatchValue(value=b))
for b in ("completed_projects", "events", "ongoing_projects"):
    print(f"  {b:20} overlapping 2022: {count('2022-01-01','2023-01-01', bundle(b)):>6,}")

with mysql_connection() as c, c.cursor() as cur:
    cur.execute("""SELECT COUNT(*) n FROM documents
                   WHERE bundle='completed_projects'
                     AND effective_start_date < '2022-01-01'
                     AND effective_end_date >= '2022-01-01'""")
    spanning = cur.fetchone()["n"]
print(f"\n  MySQL: completed_projects that START before 2022 but END during/after it")
print(f"         (only reachable through end-date overlap): {spanning:,}")

print()
print("=" * 76)
print("READ PATH USES CANONICAL FIELDS ONLY")
print("=" * 76)
import inspect
from app.retrieval.understanding import filters as F
from app.retrieval.search import temporal_gate as TG
src = inspect.getsource(F) + inspect.getsource(TG).replace(TG.__doc__ or "", "")
for name in ("field_event_start_date", "field_news_date", "field_report_date",
             "published_at", "raw_meta"):
    print(f"  {name:26} appears in read path: {name in src}")
