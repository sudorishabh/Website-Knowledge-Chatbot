"""Web search behind one interface, so the vendor is a setting and not a design.

A provider does one thing: turn a query (optionally restricted to one site) into
a ranked list of :class:`SearchHit`. Fetching and reading the pages is not its
job — that is :mod:`.fetch` and :mod:`.extract`, the same for every provider —
so switching vendor changes which API is called and nothing downstream.

Two adapters ship, both official search APIs (search-result pages are never
scraped): Brave Search and Tavily. ``web_search_provider`` picks one; an empty or
unknown name, or a missing key, means no provider and web search is skipped.

:func:`search` is the only entry point callers use. Around the provider call it
adds what every provider needs and none should implement: the result cache, a
bounded retry, the domain policy, de-duplication, and the retrieval trace. Every
failure degrades to an empty list — the question is then answered from the
corpus alone, exactly as it would be with web retrieval off.
"""
from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from app.config import get_settings
from app.core.clients import get_web_http_client
from app.core.dates import stated_day
from app.observability import retrieval_log
from app.retrieval.web.cache import search_cache
from app.retrieval.web.safety import UnsafeURL, check_url, policy, url_key

logger = logging.getLogger(__name__)

__all__ = [
    "BraveSearchProvider",
    "SearchHit",
    "TavilySearchProvider",
    "WebSearchProvider",
    "get_provider",
    "search",
]

# The trace's name for this store, beside "qdrant", "graph" and "mysql".
TRACE_RETRIEVER = "web"

# Status codes worth one more attempt: throttling and server-side trouble. A 4xx
# otherwise means the request itself is wrong, and repeating it cannot help.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_PAUSE_SECONDS = 0.5


@dataclass(frozen=True)
class SearchHit:
    """One search result, as the provider ranked it.

    ``published`` is the date the *provider* reports for the page (ISO
    ``YYYY-MM-DD``), when it reports one. It is a hint, not a fact about the
    document: providers estimate it, and the page's own metadata, read at
    extraction time, is preferred over it.
    """

    url: str
    title: str
    snippet: str
    provider: str
    query: str
    rank: int
    site: str | None = None
    published: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchHit":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})


class WebSearchProvider(Protocol):
    """What a search vendor adapter implements."""

    name: str

    def search(self, query: str, *, site: str | None, limit: int) -> list[SearchHit]:
        """Ranked results for ``query``, restricted to ``site`` when given.
        Raises on failure; :func:`search` decides what a failure costs."""
        ...


_TAG = re.compile(r"<[^>]+>")


def _plain(text: Any) -> str:
    """Provider text with highlight markup and entities removed."""
    return " ".join(html.unescape(_TAG.sub("", str(text or ""))).split())


def _send(request: Any) -> httpx.Response:
    """Send with the configured timeout and a bounded retry on transient failure."""
    settings = get_settings()
    client = get_web_http_client()
    timeout = float(settings.web_search_timeout_seconds)
    attempts = 1 + max(0, int(settings.web_search_retries))
    for attempt in range(1, attempts + 1):
        try:
            response = request(client, timeout)
        except httpx.TransportError:
            if attempt == attempts:
                raise
        else:
            if response.status_code not in _RETRY_STATUSES or attempt == attempts:
                response.raise_for_status()
                return response
        time.sleep(_RETRY_PAUSE_SECONDS * attempt)
    raise RuntimeError("unreachable")  # pragma: no cover


class BraveSearchProvider:
    """Brave Search API (web results). Site restriction via the ``site:`` operator."""

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, endpoint: str = "") -> None:
        self.api_key = api_key
        self.endpoint = endpoint or self.endpoint

    def search(self, query: str, *, site: str | None, limit: int) -> list[SearchHit]:
        q = f"{query} site:{site}" if site else query
        response = _send(
            lambda client, timeout: client.get(
                self.endpoint,
                params={"q": q, "count": min(limit, 20), "text_decorations": "false"},
                headers={"X-Subscription-Token": self.api_key,
                         "Accept": "application/json"},
                timeout=timeout,
            )
        )
        results = ((response.json() or {}).get("web") or {}).get("results") or []
        return [
            SearchHit(
                url=str(item.get("url") or ""),
                title=_plain(item.get("title")),
                snippet=_plain(item.get("description")),
                provider=self.name, query=query, rank=rank, site=site,
                published=stated_day(item.get("page_age")),
            )
            for rank, item in enumerate(results[:limit], start=1)
            if item.get("url")
        ]


class TavilySearchProvider:
    """Tavily Search API. Site restriction via ``include_domains``."""

    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str, endpoint: str = "") -> None:
        self.api_key = api_key
        self.endpoint = endpoint or self.endpoint

    def search(self, query: str, *, site: str | None, limit: int) -> list[SearchHit]:
        body: dict[str, Any] = {
            "query": query,
            "max_results": min(limit, 20),
            "search_depth": "basic",
        }
        if site:
            body["include_domains"] = [site]
        response = _send(
            lambda client, timeout: client.post(
                self.endpoint,
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=timeout,
            )
        )
        results = (response.json() or {}).get("results") or []
        return [
            SearchHit(
                url=str(item.get("url") or ""),
                title=_plain(item.get("title")),
                snippet=_plain(item.get("content")),
                provider=self.name, query=query, rank=rank, site=site,
                published=stated_day(item.get("published_date")),
            )
            for rank, item in enumerate(results[:limit], start=1)
            if item.get("url")
        ]


_PROVIDERS: dict[str, type] = {
    BraveSearchProvider.name: BraveSearchProvider,
    TavilySearchProvider.name: TavilySearchProvider,
}


def get_provider() -> WebSearchProvider | None:
    """The configured provider, or None when there is none to use."""
    settings = get_settings()
    name = str(getattr(settings, "web_search_provider", "") or "").strip().lower()
    if not name:
        return None
    adapter = _PROVIDERS.get(name)
    if adapter is None:
        logger.warning("Unknown web_search_provider %r; web search is off.", name)
        return None
    api_key = str(getattr(settings, "web_search_api_key", "") or "").strip()
    if not api_key:
        logger.warning("web_search_provider %r has no API key; web search is off.", name)
        return None
    return adapter(api_key, str(getattr(settings, "web_search_endpoint", "") or "").strip())


def _admissible(hits: list[SearchHit]) -> tuple[list[SearchHit], dict[str, str]]:
    """Hits the domain policy admits, first sighting of each page only, and the
    reason each dropped one was dropped (url -> reason) for the trace.

    Checked without DNS: this decides which results are worth considering, and
    the fetcher resolves and checks again before anything is requested.
    """
    domain_policy = policy()
    kept: list[SearchHit] = []
    dropped: dict[str, str] = {}
    seen: set[str] = set()
    for hit in hits:
        try:
            check_url(hit.url, domain_policy=domain_policy, resolve=False)
        except UnsafeURL as exc:
            dropped[hit.url] = exc.reason
            continue
        key = url_key(hit.url)
        if key in seen:
            dropped[hit.url] = "duplicate"
            continue
        seen.add(key)
        kept.append(hit)
    return kept, dropped


def search(query: str, *, site: str | None = None, limit: int | None = None) -> list[SearchHit]:
    """Ranked, policy-admitted, de-duplicated results for ``query``; ``[]`` when
    no provider is configured or the provider fails."""
    query = " ".join((query or "").split())
    if not query:
        return []
    provider = get_provider()
    if provider is None:
        return []
    limit = max(1, int(limit or get_settings().web_search_max_results))
    cache = search_cache()
    cache_key = f"{provider.name}|{site or ''}|{limit}|{query.lower()}"

    with retrieval_log.retriever_call(
        TRACE_RETRIEVER,
        "search",
        stage="web_search",
        request={"provider": provider.name, "query": query, "site": site, "limit": limit},
    ) as call:
        cached = cache.get(cache_key)
        if cached is not None:
            hits = [SearchHit.from_dict(item) for item in cached]
            call.note(cached=True)
        else:
            try:
                hits = provider.search(query, site=site, limit=limit)
            except Exception as exc:
                call.fail(exc)
                logger.warning("Web search via %s failed; answering from the corpus.",
                               provider.name, exc_info=True)
                return []
            cache.set(cache_key, [hit.to_dict() for hit in hits])
            call.note(cached=False)
        kept, dropped = _admissible(hits)
        if dropped:
            call.note(dropped=dropped)
        call.row_results([hit.to_dict() for hit in kept])
    return kept
