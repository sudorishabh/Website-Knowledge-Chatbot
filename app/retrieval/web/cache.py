"""Time-limited caches for what web retrieval has already paid for.

Three of them, because three things are expensive to repeat and each goes stale
at its own rate: a search provider's results for a query, the extracted content
of a fetched page, and a site's robots.txt rules. Lifetimes come from settings
(`web_search_cache_ttl`, `web_page_cache_ttl`, `web_robots_cache_ttl`); 0
disables a cache.

Redis holds the entries when ``redis_url`` is configured, so every API process
shares them and a restart keeps them. Without Redis each process keeps its own,
bounded by `web_cache_max_entries` and evicted least-recently-used.

Values are stored as JSON on both backends. That keeps them behaviourally
identical — in particular, what ``get`` returns is always a fresh copy, so a
caller editing a cached document cannot change what the next caller reads — and
it limits entries to plain data, which is all any of these three need.

A cache is an optimisation and nothing more: every failure degrades to a miss,
and nothing here is ever written to the corpus.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Any

from app.config import get_settings
from app.core.clients import get_redis

logger = logging.getLogger(__name__)

__all__ = ["TTLCache", "page_cache", "robots_cache", "search_cache"]

# The clock entries expire by. A seam for tests.
_now = time.monotonic


class TTLCache:
    """A namespaced key → JSON-value cache whose entries expire after ``ttl`` seconds."""

    def __init__(self, namespace: str, ttl: int, *, max_entries: int = 512) -> None:
        self.namespace = namespace
        self.ttl = max(0, int(ttl))
        self.max_entries = max(1, int(max_entries))
        self._local: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def _key(self, key: str) -> str:
        # Hashed so an arbitrary URL or query is always a valid, bounded key.
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"web:{self.namespace}:{digest}"

    def get(self, key: str) -> Any | None:
        """The cached value, or None on a miss, an expired entry or any error."""
        if not self.enabled:
            return None
        stored = self._get_raw(self._key(key))
        if stored is None:
            return None
        try:
            return json.loads(stored)
        except ValueError:
            return None

    def set(self, key: str, value: Any) -> None:
        """Store ``value``. Silently skipped when it is not JSON-serializable."""
        if not self.enabled:
            return
        try:
            stored = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.debug("Value for %s cache is not JSON; not cached.", self.namespace)
            return
        self._set_raw(self._key(key), stored)

    # -- backends ------------------------------------------------------------

    def _get_raw(self, key: str) -> str | None:
        redis = get_redis()
        if redis is not None:
            try:
                return redis.get(key)
            except Exception:
                logger.warning("Redis read for the %s cache failed; using the "
                               "in-process cache.", self.namespace, exc_info=True)
        with self._lock:
            entry = self._local.get(key)
            if entry is None:
                return None
            expires_at, stored = entry
            if expires_at <= _now():
                del self._local[key]
                return None
            self._local.move_to_end(key)
            return stored

    def _set_raw(self, key: str, stored: str) -> None:
        redis = get_redis()
        if redis is not None:
            try:
                redis.setex(key, self.ttl, stored)
                return
            except Exception:
                logger.warning("Redis write for the %s cache failed; using the "
                               "in-process cache.", self.namespace, exc_info=True)
        with self._lock:
            self._local[key] = (_now() + self.ttl, stored)
            self._local.move_to_end(key)
            while len(self._local) > self.max_entries:
                self._local.popitem(last=False)


def _cache(namespace: str, ttl_setting: str) -> TTLCache:
    settings = get_settings()
    return TTLCache(
        namespace,
        getattr(settings, ttl_setting, 0),
        max_entries=getattr(settings, "web_cache_max_entries", 512),
    )


@lru_cache(maxsize=1)
def search_cache() -> TTLCache:
    """Provider results per (provider, query, restriction)."""
    return _cache("search", "web_search_cache_ttl")


@lru_cache(maxsize=1)
def page_cache() -> TTLCache:
    """Extracted page content per canonical URL — never the raw bytes."""
    return _cache("page", "web_page_cache_ttl")


@lru_cache(maxsize=1)
def robots_cache() -> TTLCache:
    """robots.txt text per site."""
    return _cache("robots", "web_robots_cache_ttl")
