"""Fetching one search-result URL, politely and safely.

:func:`fetch` returns a :class:`FetchedPage` or raises :class:`FetchRefused`
with a stable reason code (which is what the trace records when someone asks
why a result was not used). Everything it does is bounded:

* **Safety.** Every URL — the result itself and every redirect hop — passes
  :func:`app.retrieval.web.safety.check_url` before it is requested. The HTTP
  client never follows a redirect on its own; this module does, one checked hop
  at a time, up to ``web_max_redirects``.
* **Site policy.** robots.txt is read per site and obeyed, including
  Crawl-delay (RFC 9309: a 4xx robots.txt means no rules, an unreachable one
  means stay away). A per-site interval spaces requests, and a request that
  would have to wait longer than ``web_max_host_wait_seconds`` for its turn is
  skipped rather than allowed to stall the answer.
* **Size and type.** Only HTML, plain text and (when ``web_fetch_pdfs``) PDF are
  read, each up to its own byte cap. An oversized response is refused, not
  truncated.
* **Transient failure.** A timeout, 429 or 5xx is retried ``web_fetch_retries``
  times, each retry taking its own turn at the site.
* **Duplication.** Concurrent requests for the same page share one fetch.

What it deliberately never does is follow a link inside a page. Only the URLs a
search returned are ever fetched — there is no crawling.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import get_settings
from app.core.clients import get_web_http_client
from app.observability import retrieval_log
from app.retrieval.web.cache import robots_cache
from app.retrieval.web.providers import TRACE_RETRIEVER
from app.retrieval.web.safety import UnsafeURL, check_url, url_key

logger = logging.getLogger(__name__)

__all__ = ["FetchRefused", "FetchedPage", "fetch"]

HTML = "html"
PDF = "pdf"
TEXT = "text"
# Declared by nothing trustworthy; the bytes decide.
_UNKNOWN = "unknown"

# Why a fetch was refused, beyond the safety module's own reason codes.
ROBOTS_DISALLOWED = "robots_disallowed"
RATE_LIMITED = "rate_limited"
TOO_MANY_REDIRECTS = "too_many_redirects"
BAD_REDIRECT = "bad_redirect"
UNSUPPORTED_TYPE = "unsupported_type"
PDF_DISABLED = "pdf_disabled"
TOO_LARGE = "too_large"
NETWORK_ERROR = "network_error"

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_PAUSE_SECONDS = 0.5
# RFC 9309 asks crawlers to read at least 500 KiB of a robots.txt; nothing
# legitimate is larger.
_ROBOTS_MAX_BYTES = 512_000
_ACCEPT = "text/html,application/xhtml+xml,application/pdf;q=0.9,text/plain;q=0.8"

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_PDF_TYPES = frozenset({"application/pdf", "application/x-pdf"})
# Types that say nothing about the body; the bytes decide.
_OPAQUE_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})

# The clock and sleep the rate limiter and retries use. Seams for tests.
_now = time.monotonic
_sleep = time.sleep


class FetchRefused(Exception):
    """A URL that was not fetched, and the reason code why."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason


@dataclass(frozen=True)
class FetchedPage:
    """One fetched document: the bytes, and where and when they came from."""

    url: str          # as requested
    final_url: str    # after redirects — the document's actual address
    kind: str         # html | pdf | text
    content: bytes
    content_type: str
    status: int
    fetched_at: str   # ISO 8601 UTC: when this copy was read, never a publication date
    redirects: int = 0


# --- per-site pacing ----------------------------------------------------------


class _HostGate:
    """Spaces requests to one site ``interval`` seconds apart.

    Reserves a slot rather than sleeping under the lock, so requests to
    different sites never wait on each other, and refuses outright when the slot
    would be further away than ``max_wait`` — a request that must queue behind a
    long Crawl-delay is not worth holding an answer for.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str, interval: float, max_wait: float) -> None:
        with self._lock:
            now = _now()
            slot = max(now, self._next.get(host, now))
            delay = slot - now
            if delay > max_wait:
                raise FetchRefused(host, RATE_LIMITED)
            self._next[host] = slot + max(0.0, interval)
        if delay > 0:
            _sleep(delay)


_gate = _HostGate()


# --- one request ------------------------------------------------------------


@dataclass
class _Response:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes = b""
    kind: str | None = None
    content_type: str = ""


def _media_type(headers: httpx.Headers) -> str:
    return (headers.get("content-type") or "").split(";", 1)[0].strip().lower()


def _declared_kind(media_type: str, url: str) -> str | None:
    """The kind the headers (or, for an opaque type, the path) promise."""
    if media_type in _HTML_TYPES:
        return HTML
    if media_type in _PDF_TYPES:
        return PDF
    if media_type == "text/plain":
        return TEXT
    if media_type in _OPAQUE_TYPES:
        return PDF if urlsplit(url).path.lower().endswith(".pdf") else _UNKNOWN
    return None


def _sniffed_kind(body: bytes, declared: str) -> str:
    """The kind the bytes show, which overrides a declaration they contradict:
    a PDF served as octet-stream is still a PDF, and so is an HTML page served
    with no type at all an HTML page."""
    if body.startswith(b"%PDF-"):
        return PDF
    if declared == _UNKNOWN:
        head = body[:512].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            return HTML
    return declared


def _read(response: httpx.Response, cap: int) -> bytes:
    """The decoded body, refused the moment it passes ``cap``.

    Counted on the decoded bytes, so a small compressed response that inflates
    into gigabytes is stopped at the cap like any other.
    """
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise FetchRefused(str(response.url), TOO_LARGE)
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > cap:
            raise FetchRefused(str(response.url), TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


def _download(url: str, *, interval: float, max_bytes: int | None = None) -> _Response:
    """One URL, no redirects followed: the status, and the body when it is
    worth reading. Retries transient failures, each retry taking its own turn
    at the site."""
    settings = get_settings()
    client = get_web_http_client()
    host = urlsplit(url).hostname or ""
    attempts = 1 + max(0, int(settings.web_fetch_retries))
    for attempt in range(1, attempts + 1):
        _gate.wait(host, interval, float(settings.web_max_host_wait_seconds))
        try:
            with client.stream("GET", url, headers={"Accept": _ACCEPT}) as response:
                status = response.status_code
                if status in _RETRY_STATUSES and attempt < attempts:
                    _sleep(_RETRY_PAUSE_SECONDS * attempt)
                    continue
                result = _Response(url=url, status=status, headers=response.headers)
                if not 200 <= status < 300:
                    return result
                return _with_body(result, response, max_bytes)
        except httpx.TransportError:
            if attempt == attempts:
                raise FetchRefused(url, NETWORK_ERROR) from None
            _sleep(_RETRY_PAUSE_SECONDS * attempt)
    raise FetchRefused(url, NETWORK_ERROR)  # pragma: no cover - loop always returns


def _with_body(result: _Response, response: httpx.Response, max_bytes: int | None) -> _Response:
    """Read the body of a successful response, deciding its kind and cap."""
    settings = get_settings()
    media_type = _media_type(response.headers)
    result.content_type = media_type
    if max_bytes is not None:            # robots.txt: read as text, whatever it says
        result.body = _read(response, max_bytes)
        result.kind = TEXT
        return result
    declared = _declared_kind(media_type, result.url)
    if declared is None:
        raise FetchRefused(result.url, UNSUPPORTED_TYPE)
    if declared == PDF and not settings.web_fetch_pdfs:
        raise FetchRefused(result.url, PDF_DISABLED)
    cap = (int(settings.web_pdf_max_bytes) if declared in (PDF, _UNKNOWN)
           else int(settings.web_fetch_max_bytes))
    body = _read(response, cap)
    kind = _sniffed_kind(body, declared)
    if kind == _UNKNOWN:
        raise FetchRefused(result.url, UNSUPPORTED_TYPE)
    if kind == PDF and not settings.web_fetch_pdfs:
        raise FetchRefused(result.url, PDF_DISABLED)
    if kind != PDF and len(body) > int(settings.web_fetch_max_bytes):
        # Read under the PDF cap because it was undeclared, but it is a page.
        raise FetchRefused(result.url, TOO_LARGE)
    result.body, result.kind = body, kind
    return result


def _follow(
    url: str,
    *,
    permit: Callable[[str], float],
    max_bytes: int | None = None,
) -> tuple[_Response, int]:
    """Request ``url``, following redirects one permitted hop at a time.

    ``permit`` checks a URL and returns the pacing interval for its site; it is
    called for the URL and for every hop, so a redirect cannot reach anything
    the URL itself could not.
    """
    limit = max(0, int(get_settings().web_max_redirects))
    current = url
    for hops in range(limit + 1):
        response = _download(current, interval=permit(current), max_bytes=max_bytes)
        if response.status not in _REDIRECTS:
            return response, hops
        location = response.headers.get("location")
        if not location:
            raise FetchRefused(current, BAD_REDIRECT)
        current = urljoin(current, location)
    raise FetchRefused(url, TOO_MANY_REDIRECTS)


# --- robots.txt -------------------------------------------------------------


@dataclass(frozen=True)
class _Robots:
    """A site's robots.txt verdict: allow everything, nothing, or per the rules."""

    mode: str                  # "rules" | "allow_all" | "disallow_all"
    text: str = ""

    def parser(self) -> urllib.robotparser.RobotFileParser:
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(self.text.splitlines())
        return parser

    def allows(self, url: str, agent: str) -> bool:
        if self.mode != "rules":
            return self.mode == "allow_all"
        return self.parser().can_fetch(agent, url)

    def crawl_delay(self, agent: str) -> float:
        if self.mode != "rules":
            return 0.0
        try:
            return float(self.parser().crawl_delay(agent) or 0.0)
        except (TypeError, ValueError):
            return 0.0


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{(parts.netloc or '').lower()}"


def _robots(url: str) -> _Robots:
    """The robots.txt verdict for ``url``'s site, cached per site.

    RFC 9309 status handling: a 2xx is parsed; a 4xx other than 429 means the
    site publishes no rules, so everything is allowed; a 429, a 5xx or a network
    failure means the rules could not be read, so the site is left alone. That
    last verdict is not cached — the site is asked again next time rather than
    shunned for a day over one bad minute.

    Raises :class:`FetchRefused` (``rate_limited``) when the site's turn is too
    far away even to ask, so the page is reported as rate-limited rather than as
    disallowed by rules nobody read.
    """
    origin = _origin(url)
    cache = robots_cache()
    cached = cache.get(origin)
    if isinstance(cached, dict) and cached.get("mode"):
        return _Robots(mode=cached["mode"], text=cached.get("text") or "")

    settings = get_settings()

    def permit(target: str) -> float:
        _check(target)
        return float(settings.web_per_host_interval_seconds)

    try:
        response, _ = _follow(f"{origin}/robots.txt", permit=permit,
                              max_bytes=_ROBOTS_MAX_BYTES)
    except FetchRefused as exc:
        if exc.reason == RATE_LIMITED:
            raise FetchRefused(url, RATE_LIMITED) from None
        if exc.reason == NETWORK_ERROR:
            return _Robots(mode="disallow_all")
        # A robots.txt that redirects somewhere unsafe, or loops, is treated as
        # unavailable — RFC 9309's reading of a robots.txt that cannot be
        # followed — which means no rules.
        verdict = _Robots(mode="allow_all")
    else:
        if 200 <= response.status < 300:
            verdict = _Robots(mode="rules",
                              text=response.body.decode("utf-8", errors="replace"))
        elif 400 <= response.status < 500 and response.status != 429:
            verdict = _Robots(mode="allow_all")
        else:
            return _Robots(mode="disallow_all")
    cache.set(origin, {"mode": verdict.mode, "text": verdict.text})
    return verdict


# --- fetching a page --------------------------------------------------------


def _check(url: str) -> None:
    try:
        check_url(url)
    except UnsafeURL as exc:
        raise FetchRefused(url, exc.reason) from None


def _permit_page(url: str) -> float:
    """Whether a page URL may be requested, and how far apart to space requests
    to its site: the configured interval, or the site's Crawl-delay if longer."""
    settings = get_settings()
    _check(url)
    interval = float(settings.web_per_host_interval_seconds)
    if settings.web_respect_robots:
        agent = settings.web_user_agent
        robots = _robots(url)
        if not robots.allows(url, agent):
            raise FetchRefused(url, ROBOTS_DISALLOWED)
        interval = max(interval, robots.crawl_delay(agent))
    return interval


def _fetch(url: str) -> FetchedPage:
    response, redirects = _follow(url, permit=_permit_page)
    if not 200 <= response.status < 300:
        raise FetchRefused(url, f"http_{response.status}")
    return FetchedPage(
        url=url,
        final_url=response.url,
        kind=response.kind or HTML,
        content=response.body,
        content_type=response.content_type,
        status=response.status,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        redirects=redirects,
    )


class _Coalescer:
    """Runs one fetch per key at a time; concurrent callers share its outcome."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[str, Future] = {}

    def run(self, key: str, work: Callable[[], Any]) -> Any:
        with self._lock:
            future = self._inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._inflight[key] = future
        if not owner:
            return future.result()
        try:
            result = work()
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(result)
            return result
        finally:
            with self._lock:
                self._inflight.pop(key, None)


_coalescer = _Coalescer()


def fetch(url: str) -> FetchedPage:
    """Fetch one search-result URL; raises :class:`FetchRefused` when it may
    not or could not be fetched."""

    def traced() -> FetchedPage:
        with retrieval_log.retriever_call(
            TRACE_RETRIEVER, "fetch", stage="web_fetch", request={"url": url}
        ) as call:
            try:
                page = _fetch(url)
            except FetchRefused as exc:
                call.note(refused=exc.reason)
                raise
            call.note(
                status=page.status, kind=page.kind, bytes=len(page.content),
                final_url=page.final_url, redirects=page.redirects,
            )
            return page

    return _coalescer.run(url_key(url), traced)
