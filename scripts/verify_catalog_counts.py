"""Check every catalog reader against independently written SQL.

The catalog readers build their SQL by composing joins and clauses, which is
what makes arbitrary filter combinations possible — and what makes a fan-out bug
easy to miss: a document with two authors and three themes joins to six rows,
and a `COUNT(*)` where `COUNT(DISTINCT ...)` was meant reports six documents.
Unit tests assert on the *emitted* SQL; this asserts on the *numbers*, against
statements written independently for each case.

Lives in scripts/ rather than tests/ because it needs the live catalog, which is
the repo's convention (the fake-cursor tests are in tests/test_theme_queries.py;
the real statements run here and in app/local_tests).

    python -m scripts.verify_catalog_counts

Exits non-zero on any disagreement.
"""
from datetime import datetime

from app.catalog import queries as q
from app.core.clients import mysql_connection


def sql(statement, params=()):
    with mysql_connection() as c, c.cursor() as cur:
        cur.execute(statement, params)
        rows = cur.fetchall()
    return rows


def one(statement, params=()):
    rows = sql(statement, params)
    return list(rows[0].values())[0] if rows else 0


CHECKS = []


def check(name, got, want):
    CHECKS.append((name, got, want, got == want))


# --- fan-out: a document with several authors / several themes must count once
check(
    "documents with >1 author exist",
    one("SELECT COUNT(*) FROM (SELECT document_id FROM documents_author "
        "GROUP BY document_id HAVING COUNT(*) > 1) x") > 0,
    True,
)
check(
    "documents with >1 theme exist",
    one("SELECT COUNT(*) FROM (SELECT document_id FROM documents_theme "
        "GROUP BY document_id HAVING COUNT(*) > 1) x") > 0,
    True,
)

# --- 1. plain document count
check(
    "count articles",
    q.count_documents(bundle="article"),
    one("SELECT COUNT(*) FROM documents WHERE bundle='article'"),
)

# --- 2. author filter (LIKE) with fan-out -> DISTINCT documents
check(
    "count articles by author",
    q.count_documents(bundle="article", author="TERI Web Desk"),
    one("SELECT COUNT(DISTINCT s.document_id) FROM documents s "
        "JOIN documents_author a ON a.document_id=s.document_id "
        "WHERE s.bundle='article' AND a.author LIKE %s", ("%TERI Web Desk%",)),
)

# --- 3. theme filter matches the theme name exactly
check(
    "count articles under Energy",
    q.count_documents(bundle="article", theme="Energy"),
    one("SELECT COUNT(DISTINCT s.document_id) FROM documents s "
        "JOIN documents_theme t ON t.document_id=s.document_id "
        "WHERE s.bundle='article' AND t.theme='Energy'"),
)

# --- 4. author + theme (two facet joins, must not multiply)
check(
    "count articles by author under Energy",
    q.count_documents(bundle="article", author="TERI Web Desk", theme="Energy"),
    one("SELECT COUNT(DISTINCT s.document_id) FROM documents s "
        "JOIN documents_author a ON a.document_id=s.document_id "
        "JOIN documents_theme  t ON t.document_id=s.document_id "
        "WHERE s.bundle='article' AND a.author LIKE %s "
        "AND t.theme='Energy'", ("%TERI Web Desk%",)),
)

# --- 5. date range, half-open
check(
    "count articles in 2024",
    q.count_documents(bundle="article",
                      effective_from=datetime(2024, 1, 1),
                      effective_to=datetime(2025, 1, 1)),
    one("SELECT COUNT(*) FROM documents WHERE bundle='article' "
        "AND effective_start_date >= '2024-01-01' AND effective_start_date < '2025-01-01'"),
)

# --- 6. theme_group as a document scope, by equality
check(
    "count documents carrying a main theme",
    q.count_documents(theme_group="main"),
    one("SELECT COUNT(DISTINCT s.document_id) FROM documents s "
        "JOIN documents_theme g ON g.document_id=s.document_id "
        "WHERE g.theme_group='main'"),
)
check(
    "count documents carrying an other theme",
    q.count_documents(theme_group="other"),
    one("SELECT COUNT(DISTINCT s.document_id) FROM documents s "
        "JOIN documents_theme g ON g.document_id=s.document_id "
        "WHERE g.theme_group='other'"),
)

# --- 7. distinct facet counts
check(
    "distinct authors overall",
    q.count_distinct_values("author"),
    one("SELECT COUNT(DISTINCT COALESCE(a.author_norm, a.author)) FROM documents s "
        "JOIN documents_author a ON a.document_id=s.document_id"),
)
check(
    "distinct authors under Energy",
    q.count_distinct_values("author", theme="Energy"),
    one("SELECT COUNT(DISTINCT COALESCE(a.author_norm, a.author)) FROM documents s "
        "JOIN documents_theme  t ON t.document_id=s.document_id "
        "JOIN documents_author a ON a.document_id=s.document_id "
        "WHERE (t.theme='Energy' OR t.parent='Energy')"),
)
check(
    "distinct themes for one author",
    q.count_distinct_values("theme", author="TERI Web Desk"),
    one("SELECT COUNT(DISTINCT t.theme) FROM documents s "
        "JOIN documents_author a ON a.document_id=s.document_id "
        "JOIN documents_theme  t ON t.document_id=s.document_id "
        "WHERE a.author LIKE %s AND t.theme<>'' "
        "AND t.theme NOT IN ('False','True')", ("%TERI Web Desk%",)),
)
check(
    "distinct main themes for one author",
    q.count_distinct_values("theme", author="TERI Web Desk", theme_group="main"),
    one("SELECT COUNT(DISTINCT t.theme) FROM documents s "
        "JOIN documents_author a ON a.document_id=s.document_id "
        "JOIN documents_theme  g ON g.document_id=s.document_id "
        "JOIN documents_theme  t ON t.document_id=s.document_id "
        "WHERE a.author LIKE %s AND g.theme_group='main' AND t.theme<>'' "
        "AND t.theme NOT IN ('False','True')", ("%TERI Web Desk%",)),
)

# --- 8. single-dimension distribution
rows = dict((k, n) for k, n in q.distribution("theme", bundle="article"))
want = {r["k"]: r["n"] for r in sql(
    "SELECT gt.theme AS k, COUNT(DISTINCT s.document_id) AS n FROM documents s "
    "JOIN documents_theme gt ON gt.document_id=s.document_id "
    "WHERE s.bundle='article' AND gt.theme<>'' "
    "AND gt.theme NOT IN ('False','True') GROUP BY k ORDER BY n DESC LIMIT 20")}
check("distribution by theme (top 20)", rows, want)

# --- 9. theme_group restricts the grouped themes, not just the documents
main_rows = dict((k, n) for k, n in q.distribution(
    "theme", bundle="article", theme_group="main", limit=100))
main_want = {r["k"]: r["n"] for r in sql(
    "SELECT gt.theme AS k, COUNT(DISTINCT s.document_id) AS n FROM documents s "
    "JOIN documents_theme gt ON gt.document_id=s.document_id "
    "WHERE s.bundle='article' AND gt.theme<>'' "
    "AND gt.theme NOT IN ('False','True') AND gt.theme_group='main' "
    "GROUP BY k ORDER BY n DESC LIMIT 100")}
check("distribution by main theme", main_rows, main_want)

# --- 10. cross distribution
cross = {(a, b): n for a, b, n in q.cross_distribution(
    "author", "theme", bundle="article", limit=500)}
cross_want = {(r["a"], r["b"]): r["n"] for r in sql(
    "SELECT MIN(ga.author) AS a, gb.theme AS b, COUNT(DISTINCT s.document_id) AS n "
    "FROM documents s "
    "JOIN documents_author ga ON ga.document_id=s.document_id "
    "JOIN documents_theme  gb ON gb.document_id=s.document_id "
    "WHERE s.bundle='article' AND gb.theme<>'' "
    "AND gb.theme NOT IN ('False','True') "
    "GROUP BY COALESCE(ga.author_norm, ga.author), gb.theme "
    "ORDER BY n DESC, a ASC, b ASC LIMIT 500")}
check("cross_distribution author x theme", cross, cross_want)

# --- 11. everything at once
check(
    "author + theme + year + bundle",
    q.count_documents(bundle="article", author="TERI Web Desk", theme="Environment",
                      effective_from=datetime(2020, 1, 1),
                      effective_to=datetime(2021, 1, 1)),
    one("SELECT COUNT(DISTINCT s.document_id) FROM documents s "
        "JOIN documents_author a ON a.document_id=s.document_id "
        "JOIN documents_theme  t ON t.document_id=s.document_id "
        "WHERE s.bundle='article' AND a.author LIKE %s "
        "AND (t.theme='Environment' OR t.parent='Environment') "
        "AND s.effective_start_date >= '2020-01-01' AND s.effective_start_date < '2021-01-01'",
        ("%TERI Web Desk%",)),
)

# --- 12. theme_group as the *counted* dimension, not a document scope
check(
    "distinct main themes",
    q.count_distinct_values("theme", theme_group="main"),
    one("SELECT COUNT(DISTINCT theme) FROM documents_theme "
        "WHERE theme_group='main' AND theme<>'' AND theme NOT IN ('False','True')"),
)
check(
    "distinct other themes",
    q.count_distinct_values("theme", theme_group="other"),
    one("SELECT COUNT(DISTINCT theme) FROM documents_theme "
        "WHERE theme_group='other' AND theme<>'' AND theme NOT IN ('False','True')"),
)

# --- 13. cross distribution restricted to main themes, either order
cross_main = {(a, b): n for a, b, n in q.cross_distribution(
    "author", "theme", bundle="article", theme_group="main", limit=500)}
cross_main_want = {(r["a"], r["b"]): r["n"] for r in sql(
    "SELECT MIN(ga.author) AS a, gb.theme AS b, COUNT(DISTINCT s.document_id) AS n "
    "FROM documents s "
    "JOIN documents_author ga ON ga.document_id=s.document_id "
    "JOIN documents_theme  gb ON gb.document_id=s.document_id "
    "WHERE s.bundle='article' AND gb.theme<>'' "
    "AND gb.theme NOT IN ('False','True') AND gb.theme_group='main' "
    "GROUP BY COALESCE(ga.author_norm, ga.author), gb.theme "
    "ORDER BY n DESC, a ASC, b ASC LIMIT 500")}
check("cross_distribution restricted to main themes", cross_main, cross_main_want)

check(
    "cross_distribution transposes cleanly",
    {(b, a): n for a, b, n in q.cross_distribution(
        "author", "theme", bundle="article", limit=500)},
    {(a, b): n for a, b, n in q.cross_distribution(
        "theme", "author", bundle="article", limit=500)},
)

# --- 14. the derived author column agrees with the module that writes it
from app.catalog.author_names import normalize as _norm

_mismatch = [
    r for r in sql("SELECT DISTINCT author, author_norm FROM documents_author")
    if (_norm(r["author"]) or None) != r["author_norm"]
]
check("author_norm matches author_names.normalize", _mismatch, [])
check(
    "raw author values still present",
    one("SELECT COUNT(*) FROM documents_author WHERE author IS NULL OR author=''"),
    0,
)

import sys

width = max(len(n) for n, *_ in CHECKS)
failed = 0
for name, got, want, ok in CHECKS:
    if ok:
        shown = got if not isinstance(got, dict) else f"{len(got)} groups"
        print(f"  OK   {name:<{width}}  {shown}")
    else:
        failed += 1
        print(f"  FAIL {name:<{width}}  got={got!r} want={want!r}")
print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} agree with independent SQL")
