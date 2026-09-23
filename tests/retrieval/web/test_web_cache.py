"""The web caches: expiry, bounds, copy semantics, and graceful degradation.

A cache here is only ever an optimisation, so the properties that matter are
that an entry disappears when its lifetime is up, that a process-local cache
cannot grow without bound, that a caller cannot corrupt what the next caller
reads, and that a Redis failure costs a miss rather than a query.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.retrieval.web import cache
from app.retrieval.web.cache import TTLCache


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl


class _BrokenRedis:
    def get(self, key):
        raise ConnectionError("redis down")

    def setex(self, key, ttl, value):
        raise ConnectionError("redis down")


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(cache, "_now", fake)
    return fake


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(cache, "get_redis", lambda: None)


# --- in-process backend ----------------------------------------------------


def test_a_value_is_returned_until_its_lifetime_is_up(clock, no_redis):
    store = TTLCache("t", ttl=60)
    store.set("q", {"hits": [1, 2]})
    clock.now += 59
    assert store.get("q") == {"hits": [1, 2]}
    clock.now += 2
    assert store.get("q") is None


def test_the_least_recently_used_entry_is_evicted_at_the_bound(clock, no_redis):
    store = TTLCache("t", ttl=60, max_entries=2)
    store.set("a", 1)
    store.set("b", 2)
    assert store.get("a") == 1          # "a" is now the most recently used
    store.set("c", 3)
    assert store.get("b") is None
    assert store.get("a") == 1
    assert store.get("c") == 3


def test_editing_a_returned_value_does_not_change_the_cache(clock, no_redis):
    store = TTLCache("t", ttl=60)
    store.set("doc", {"sections": ["one"]})
    first = store.get("doc")
    first["sections"].append("tampered")
    assert store.get("doc") == {"sections": ["one"]}


def test_a_zero_lifetime_disables_the_cache(clock, no_redis):
    store = TTLCache("t", ttl=0)
    store.set("q", "value")
    assert store.enabled is False
    assert store.get("q") is None


def test_a_value_that_is_not_plain_data_is_skipped_not_raised(clock, no_redis):
    store = TTLCache("t", ttl=60)
    store.set("q", object())
    assert store.get("q") is None


def test_namespaces_do_not_share_entries(clock, no_redis):
    a, b = TTLCache("search", ttl=60), TTLCache("page", ttl=60)
    a.set("same-key", "search value")
    assert b.get("same-key") is None


# --- Redis backend ---------------------------------------------------------


def test_redis_holds_entries_with_the_cache_lifetime(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: redis)
    store = TTLCache("page", ttl=86400)
    store.set("https://www.teriin.org/user/15680", {"title": "Mr Sayanta Ghosh"})
    (key,) = redis.store
    assert key.startswith("web:page:")
    assert "teriin" not in key            # hashed, so any URL is a valid key
    assert redis.ttls[key] == 86400
    assert store.get("https://www.teriin.org/user/15680") == {"title": "Mr Sayanta Ghosh"}


def test_a_redis_failure_falls_back_to_the_process_cache(clock, monkeypatch):
    monkeypatch.setattr(cache, "get_redis", lambda: _BrokenRedis())
    store = TTLCache("search", ttl=60)
    store.set("q", ["hit"])
    assert store.get("q") == ["hit"]


# --- the three named caches -----------------------------------------------


def test_each_named_cache_takes_its_own_lifetime(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_search_cache_ttl", 11)
    monkeypatch.setattr(settings, "web_page_cache_ttl", 22)
    monkeypatch.setattr(settings, "web_robots_cache_ttl", 33)
    for factory in (cache.search_cache, cache.page_cache, cache.robots_cache):
        factory.cache_clear()
    try:
        assert cache.search_cache().ttl == 11
        assert cache.page_cache().ttl == 22
        assert cache.robots_cache().ttl == 33
        assert len({cache.search_cache().namespace, cache.page_cache().namespace,
                    cache.robots_cache().namespace}) == 3
    finally:
        for factory in (cache.search_cache, cache.page_cache, cache.robots_cache):
            factory.cache_clear()
