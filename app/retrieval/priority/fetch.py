"""Reading a priority page from the live site, cheaply and safely.

Four properties, each for a failure that would otherwise reach an answer:

* **Allowlisted.** Only hosts that appear in the priority list are fetched, and
  the host is checked again after redirects, so a URL can never come from the
  question and a redirect can never leave the site.
* **Cached for ``priority_cache_ttl``** (the 300 s the site's own
  ``Cache-Control`` states), then **revalidated** with the ETag and
  Last-Modified it sent, so an unchanged page costs a 304 rather than a body.
* **One request per page at a time.** Concurrent questions about the same page
  wait for the fetch already in flight instead of each starting their own.
* **A last good copy survives failures.** A timeout, an error status or an
  unusable body returns the previous copy marked ``stale``; only a page that
  has never been read successfully comes back as ``None``.

Pages only. A PDF or office document is never downloaded — a URL that names
one is refused before any request, and a response that turns out not to be
HTML is dropped before its body is read. Documents reach the answer as links.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Refuse bodies larger than this. The largest page on the list is ~500 KB.
MAX_BYTES = 3 * 1024 * 1024

USER_AGENT = "TERI-Chatbot/1.0 (+https://teriin.org; priority page reader)"

#: File types that are linked from pages but never fetched.
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                       ".csv", ".zip")


def is_document_url(url: str) -> bool:
    """Whether ``url`` names a document file rather than a page."""
    from urllib.parse import unquote, urlsplit

    return unquote(urlsplit(url).path).lower().endswith(DOCUMENT_EXTENSIONS)


@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    html: str
    fetched_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    #: Served from the process cache without a request.
    from_cache: bool = False
    #: The live fetch failed and this is the last good copy.
    stale: bool = False


@dataclass
class _Entry:
    page: FetchedPage
    expires: float


_cache: dict[str, _Entry] = {}
_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()
_session = None


def _lock_for(url: str) -> threading.Lock:
    with _registry_lock:
        return _locks.setdefault(url, threading.Lock())


def _get_session():
    global _session
    if _session is None:
        import requests

        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })
        _session = session
    return _session


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def clear_cache() -> None:
    """Forget every fetched page (tests)."""
    with _registry_lock:
        _cache.clear()
        _locks.clear()


def fetch(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    validate: Callable[[str], bool] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> FetchedPage | None:
    """The page at ``url``, from cache when fresh, else from the site.

    ``validate`` is asked whether a freshly downloaded body is usable (a
    maintenance page answers 200 too); a body it rejects is treated like any
    other failure, so the last good copy stands.
    """
    if _host(url) not in allowed_hosts:
        logger.warning("Refusing to fetch %s: host is not on the priority list.", url)
        return None
    if is_document_url(url):
        logger.info("Not fetching %s: documents are linked, never read.", url)
        return None
    settings = get_settings()
    entry = _cache.get(url)
    if entry is not None and entry.expires > now():
        return replace(entry.page, from_cache=True)

    with _lock_for(url):
        # Whoever held the lock may just have refreshed it.
        entry = _cache.get(url)
        if entry is not None and entry.expires > now():
            return replace(entry.page, from_cache=True)
        previous = entry.page if entry is not None else None
        page = _download(url, previous, allowed_hosts=allowed_hosts,
                         timeout=float(settings.priority_fetch_timeout), validate=validate)
        if page is None:
            if previous is None:
                return None
            # Retry no sooner than a tenth of the TTL, so a down site is not
            # hammered by every question while it is down.
            stale = replace(previous, stale=True, from_cache=False)
            _cache[url] = _Entry(stale, now() + max(5.0, settings.priority_cache_ttl / 10))
            return stale
        _cache[url] = _Entry(page, now() + float(settings.priority_cache_ttl))
        return page


def _download(
    url: str,
    previous: FetchedPage | None,
    *,
    allowed_hosts: frozenset[str],
    timeout: float,
    validate: Callable[[str], bool] | None,
) -> FetchedPage | None:
    headers: dict[str, str] = {}
    if previous is not None and not previous.stale:
        if previous.etag:
            headers["If-None-Match"] = previous.etag
        if previous.last_modified:
            headers["If-Modified-Since"] = previous.last_modified
    started = time.monotonic()
    try:
        response = _get_session().get(url, headers=headers, timeout=timeout,
                                      allow_redirects=True, stream=True)
    except Exception as exc:
        logger.warning("Priority page %s could not be fetched: %s", url, exc)
        return None
    try:
        if _host(response.url) not in allowed_hosts:
            logger.warning("Priority page %s redirected off the site to %s; ignored.",
                           url, response.url)
            return None
        if response.status_code == 304 and previous is not None:
            return replace(previous, fetched_at=datetime.now(timezone.utc),
                           from_cache=False, stale=False)
        if response.status_code != 200:
            logger.warning("Priority page %s answered HTTP %s.", url, response.status_code)
            return None
        ctype = response.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            logger.warning("Priority page %s is not HTML (%s).", url, ctype)
            return None
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                logger.warning("Priority page %s exceeds %d bytes; ignored.", url, MAX_BYTES)
                return None
        html = body.decode(response.encoding or "utf-8", errors="replace")
    finally:
        response.close()
    if validate is not None and not validate(html):
        logger.warning("Priority page %s returned no usable content.", url)
        return None
    logger.info("Fetched priority page %s in %.0f ms.", url,
                (time.monotonic() - started) * 1000)
    return FetchedPage(
        url=url,
        final_url=response.url,
        html=html,
        fetched_at=datetime.now(timezone.utc),
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )
