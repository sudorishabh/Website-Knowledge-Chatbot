"""Regression tests: the semantic cache must expire when the corpus changes.

The partition key used to hash only the retrieval-preference settings, so a
cached answer outlived any amount of ingestion — for the full TTL a question
could be answered from text that had been re-indexed or deleted, citing chunk
ids Qdrant no longer held. The invariant enforced here:

    same query + same settings + same corpus revision -> hit
    same query + changed corpus revision              -> miss

The corpus revision is read from the catalog (``MAX(indexed_at)`` + row count),
which moves on every real re-index and on every deletion. MySQL and Qdrant are
stubbed; no network.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.cache import cache_keys, semantic_cache as sc
from app.catalog import queries as catalog


@pytest.fixture(autouse=True)
def _enable_cache(monkeypatch):
    monkeypatch.setattr(sc.get_settings(), "semantic_cache_enabled", True)


# --------------------------------------------------------------------------- #
# A Qdrant stub that actually partitions on `scope`, so a hit/miss is real.
# --------------------------------------------------------------------------- #

class _ScopedStore:
    def __init__(self):
        self.points: list = []
        self.deleted: list = []

    def collection_exists(self, name):
        return True

    def upsert(self, collection_name, points):
        self.points.extend(points)

    @staticmethod
    def _scope_of(query_filter):
        for cond in query_filter.must:
            if getattr(cond, "key", None) == "scope":
                return cond.match.value
        return None

    def query_points(self, **kw):
        scope = self._scope_of(kw["query_filter"])
        now = time.time()
        hits = [
            p for p in self.points
            if p.payload.get("scope") == scope and p.payload.get("expires_at", 0) >= now
        ]
        return SimpleNamespace(points=[SimpleNamespace(payload=p.payload) for p in hits])

    def delete(self, collection_name, points_selector):
        self.deleted.append(points_selector)


def _wire(monkeypatch, store, revision):
    """Point the cache at the stub store and pin the corpus revision."""
    monkeypatch.setattr(sc, "_client", lambda: store)
    monkeypatch.setattr(sc, "_ensure_collection", lambda c, dim: True)
    monkeypatch.setattr(sc, "_maybe_prune", lambda c, name: None)
    monkeypatch.setattr(cache_keys, "corpus_revision", lambda: revision)


VECTOR = [0.1, 0.2]
ANSWER = {"answer": "grounded answer", "citations": [{"n": 1}]}


def _store(monkeypatch, store, revision):
    _wire(monkeypatch, store, revision)
    sc.store(VECTOR, ANSWER, top_k=6)


def _lookup(monkeypatch, store, revision):
    _wire(monkeypatch, store, revision)
    return sc.lookup(VECTOR, top_k=6)


# --------------------------------------------------------------------------- #
# 1-4. Corpus revision drives hit / miss.
# --------------------------------------------------------------------------- #

def test_unchanged_corpus_hits(monkeypatch):
    store = _ScopedStore()
    _store(monkeypatch, store, "2026-08-17T10:00:00|1200")
    assert _lookup(monkeypatch, store, "2026-08-17T10:00:00|1200") == ANSWER


def test_new_ingestion_misses(monkeypatch):
    """A sweep indexes new documents: newer MAX(indexed_at), higher count."""
    store = _ScopedStore()
    _store(monkeypatch, store, "2026-08-17T10:00:00|1200")
    assert _lookup(monkeypatch, store, "2026-08-17T11:30:00|1205") is None


def test_document_update_misses(monkeypatch):
    """A re-index of one document moves MAX(indexed_at); the count is unchanged."""
    store = _ScopedStore()
    _store(monkeypatch, store, "2026-08-17T10:00:00|1200")
    assert _lookup(monkeypatch, store, "2026-08-17T10:45:00|1200") is None


def test_document_deletion_misses(monkeypatch):
    """A deletion drops the row count; MAX(indexed_at) need not move at all."""
    store = _ScopedStore()
    _store(monkeypatch, store, "2026-08-17T10:00:00|1200")
    assert _lookup(monkeypatch, store, "2026-08-17T10:00:00|1199") is None


# --------------------------------------------------------------------------- #
# 5. Settings still invalidate, exactly as before.
# --------------------------------------------------------------------------- #

def test_retrieval_setting_change_still_invalidates(monkeypatch):
    """`website_chunk_floor` used to be the knob turned here. It was one of the
    per-source admission budgets, and it went when website and PDF candidates
    stopped being admitted as separate groups; `website_candidate_k` — the size
    of the website recall pull, which is what the preference still means — is
    the surviving member of the same fingerprint."""
    monkeypatch.setattr(cache_keys, "corpus_revision", lambda: "rev-1")
    settings = cache_keys.get_settings()
    before = cache_keys.semantic_partition(6, "default")

    monkeypatch.setattr(settings, "website_candidate_k", 35)
    assert cache_keys.semantic_partition(6, "default") != before

    monkeypatch.setattr(settings, "website_candidate_k", 20)
    assert cache_keys.semantic_partition(6, "default") == before
    # top_k and answer_format remain part of the partition too.
    assert cache_keys.semantic_partition(8, "default") != before
    assert cache_keys.semantic_partition(6, "table") != before


@pytest.mark.parametrize("field, changed", [
    ("azure_openai_model", lambda current: f"{current}-other"),
    ("llm_temperature_supported", lambda current: not current),
    ("llm_reasoning_effort", lambda current: f"{current or ''}-other"),
])
def test_a_model_change_invalidates(monkeypatch, field, changed):
    """The model writes the answer, so changing it — or how it is called — must
    not leave the previous model's answers servable for the rest of their TTL.
    The model used to be missing from the fingerprint entirely.

    Each case moves the field away from whatever the local .env set, so the
    test holds on any machine."""
    monkeypatch.setattr(cache_keys, "corpus_revision", lambda: "rev-1")
    settings = cache_keys.get_settings()
    before = cache_keys.semantic_partition(6, "default")

    monkeypatch.setattr(settings, field, changed(getattr(settings, field)))
    assert cache_keys.semantic_partition(6, "default") != before


def test_a_blank_effort_is_the_same_as_none(monkeypatch):
    # A blank .env line sends no effort (app.core.clients.llm), so it must not
    # split the cache from an unset one either.
    monkeypatch.setattr(cache_keys, "corpus_revision", lambda: "rev-1")
    settings = cache_keys.get_settings()
    monkeypatch.setattr(settings, "llm_reasoning_effort", None)
    unset = cache_keys.semantic_partition(6, "default")
    monkeypatch.setattr(settings, "llm_reasoning_effort", "  ")
    assert cache_keys.semantic_partition(6, "default") == unset


def test_corpus_revision_changes_the_partition(monkeypatch):
    monkeypatch.setattr(cache_keys, "corpus_revision", lambda: "rev-1")
    first = cache_keys.semantic_partition(6, "default")
    monkeypatch.setattr(cache_keys, "corpus_revision", lambda: "rev-2")
    assert cache_keys.semantic_partition(6, "default") != first


def test_a_behaviour_revision_changes_the_partition(monkeypatch):
    """A ranking or prompt change has no setting to hash. `PIPELINE_REVISION` is
    the hand-bumped stand-in, so an answer generated under the old behaviour is
    not served for the rest of its day-long TTL once the fix is running — which
    is how one fix was reported as not working three times over."""
    monkeypatch.setattr(cache_keys, "corpus_revision", lambda: "rev-1")
    before = cache_keys.semantic_partition(6, "default")
    monkeypatch.setattr(cache_keys, "PIPELINE_REVISION", "the next change")
    assert cache_keys.semantic_partition(6, "default") != before


def test_unknown_revision_disables_the_cache(monkeypatch):
    """A catalog outage must not be answered from a cache we cannot date."""
    store = _ScopedStore()
    _store(monkeypatch, store, "rev-1")

    _wire(monkeypatch, store, None)
    assert sc.semantic_partition(6, "default") is None
    assert sc.lookup(VECTOR, top_k=6) is None
    sc.store(VECTOR, ANSWER, top_k=6)
    assert len(store.points) == 1  # nothing written under an undatable key


# --------------------------------------------------------------------------- #
# 6. TTL and pruning are untouched.
# --------------------------------------------------------------------------- #

def test_expired_entry_is_not_served(monkeypatch):
    store = _ScopedStore()
    _store(monkeypatch, store, "rev-1")
    store.points[0].payload["expires_at"] = time.time() - 1
    assert _lookup(monkeypatch, store, "rev-1") is None


def test_prune_still_deletes_on_expires_at(monkeypatch):
    store = _ScopedStore()
    _wire(monkeypatch, store, "rev-1")
    sc.prune(store, "semantic_cache")
    cond = store.deleted[0].filter.must[0]
    assert cond.key == "expires_at" and cond.range.lt is not None


def test_store_records_the_ttl(monkeypatch):
    store = _ScopedStore()
    _store(monkeypatch, store, "rev-1")
    ttl = sc.get_settings().semantic_cache_ttl
    assert store.points[0].payload["expires_at"] == pytest.approx(
        time.time() + ttl, abs=5
    )


# --------------------------------------------------------------------------- #
# The catalog read behind the revision.
# --------------------------------------------------------------------------- #

class _FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls: list[str] = []

    def execute(self, sql, params=None):
        self.calls.append(" ".join(sql.split()))
        return 1

    def fetchall(self):
        return self.rows.pop(0) if self.rows else []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clear_revision_memo(monkeypatch):
    monkeypatch.setattr(catalog, "_corpus_revision", None)


def test_corpus_revision_reads_max_indexed_at_and_count(monkeypatch):
    from datetime import datetime

    cursor = _FakeCursor([[{"latest": datetime(2026, 8, 17, 10, 0, 0), "documents": 1200}]])
    monkeypatch.setattr(catalog, "mysql_connection", lambda: _FakeConn(cursor))

    revision = catalog.corpus_revision()
    assert revision == "2026-08-17T10:00:00|1200"
    assert "MAX(indexed_at)" in cursor.calls[0] and "COUNT(*)" in cursor.calls[0]


def test_corpus_revision_handles_an_empty_catalog(monkeypatch):
    cursor = _FakeCursor([[{"latest": None, "documents": 0}]])
    monkeypatch.setattr(catalog, "mysql_connection", lambda: _FakeConn(cursor))
    assert catalog.corpus_revision() == "never|0"


def test_corpus_revision_is_unknown_when_mysql_fails(monkeypatch):
    def boom():
        raise RuntimeError("mysql down")

    monkeypatch.setattr(catalog, "mysql_connection", boom)
    assert catalog.corpus_revision() is None


def test_corpus_revision_is_memoized(monkeypatch):
    from datetime import datetime

    cursor = _FakeCursor([[{"latest": datetime(2026, 8, 17, 10, 0, 0), "documents": 1200}]])
    monkeypatch.setattr(catalog, "mysql_connection", lambda: _FakeConn(cursor))

    assert catalog.corpus_revision() == catalog.corpus_revision()
    assert len(cursor.calls) == 1  # one round trip, not one per query
