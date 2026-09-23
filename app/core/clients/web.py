"""The HTTP client web retrieval reaches the public web through.

One client for search-provider calls and page fetches alike, constructed here
because this layer is the only place the app builds a client for an external
service. Built lazily and memoized: with web retrieval off it is never created.

Two settings on it are safety properties rather than tuning:

* ``follow_redirects=False``. A redirect is a new URL chosen by the server, and
  every URL is checked by ``app.retrieval.web.safety.check_url`` before it is
  requested — so the fetcher follows redirects itself, one checked hop at a
  time. An auto-following client would let a public page bounce the request to
  ``http://169.254.169.254/`` unchecked.
* A bounded connection pool, so a burst of questions cannot open an unbounded
  number of connections to the sites being read.
"""
from __future__ import annotations

from functools import lru_cache

import httpx

from app.config import get_settings

__all__ = ["get_web_http_client"]


@lru_cache(maxsize=1)
def get_web_http_client() -> httpx.Client:
    settings = get_settings()
    timeout = float(settings.web_fetch_timeout_seconds)
    connections = max(1, int(settings.web_max_connections))
    return httpx.Client(
        follow_redirects=False,
        # Connecting gets a shorter allowance than reading: a host that cannot
        # accept a connection in a few seconds will not serve a page in time.
        timeout=httpx.Timeout(timeout, connect=min(timeout, 4.0)),
        limits=httpx.Limits(
            max_connections=connections,
            max_keepalive_connections=connections,
        ),
        headers={
            "User-Agent": settings.web_user_agent,
            "Accept-Language": "en",
        },
    )
