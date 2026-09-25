"""Reading a priority page: allowlist, documents refused, cache, revalidation,
and the last good copy when the site fails."""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.retrieval.priority import fetch

HOSTS = frozenset({"teriin.org"})
HTML = "<html><body><h1>Climate Change</h1></body></html>"


class _Response:
    def __init__(self, status=200, body=HTML, url="https://teriin.org/climate",
                 ctype="text/html; charset=UTF-8", etag='"v1"'):
        self.status_code = status
        self._body = body.encode("utf-8")
        self.url = url
        self.headers = {"Content-Type": ctype, "ETag": etag, "Last-Modified": "Thu, 24 Sep 2026"}
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, *, headers, timeout, allow_redirects, stream):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    fetch.clear_cache()
    monkeypatch.setattr(get_settings(), "priority_cache_ttl", 300)
    monkeypatch.setattr(get_settings(), "priority_fetch_timeout", 4.0)
    yield
    fetch.clear_cache()


def _use(monkeypatch, session):
    monkeypatch.setattr(fetch, "_get_session", lambda: session)
    return session


def test_a_host_off_the_list_is_never_requested(monkeypatch):
    session = _use(monkeypatch, _Session())
    assert fetch.fetch("https://example.com/climate", allowed_hosts=HOSTS) is None
    assert session.calls == []


@pytest.mark.parametrize("url", [
    "https://teriin.org/sites/default/files/2024-03/Annual_Report.pdf",
    "https://teriin.org/files/Receipts%20and%20Payments.PDF",
    "https://teriin.org/files/brochure.docx",
])
def test_a_document_is_never_downloaded(monkeypatch, url):
    session = _use(monkeypatch, _Session())
    assert fetch.fetch(url, allowed_hosts=HOSTS) is None
    assert session.calls == []


def test_a_response_that_is_not_html_is_dropped_unread(monkeypatch):
    response = _Response(ctype="application/pdf")
    _use(monkeypatch, _Session(response))
    assert fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS) is None
    assert response.closed


def test_a_fresh_copy_is_served_without_a_request(monkeypatch):
    session = _use(monkeypatch, _Session(_Response()))
    clock = _Clock()
    first = fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    clock.t += 299
    second = fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    assert first.html == second.html == HTML
    assert not first.from_cache and second.from_cache
    assert len(session.calls) == 1


def test_an_expired_copy_is_revalidated_and_a_304_keeps_it(monkeypatch):
    session = _use(monkeypatch, _Session(_Response(), _Response(status=304, body="")))
    clock = _Clock()
    fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    clock.t += 301
    again = fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    assert again.html == HTML and not again.stale
    assert session.calls[1]["headers"]["If-None-Match"] == '"v1"'
    assert session.calls[1]["timeout"] == 4.0


def test_a_failure_after_a_good_read_serves_the_last_good_copy(monkeypatch):
    _use(monkeypatch, _Session(_Response(), TimeoutError("slow")))
    clock = _Clock()
    fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    clock.t += 301
    stale = fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    assert stale.html == HTML and stale.stale


def test_a_down_site_is_not_retried_by_every_question(monkeypatch):
    session = _use(monkeypatch, _Session(_Response(), _Response(status=503)))
    clock = _Clock()
    fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    clock.t += 301
    fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    clock.t += 5
    held = fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS, now=clock)
    assert held.stale and len(session.calls) == 2


def test_a_page_never_read_successfully_is_none(monkeypatch):
    _use(monkeypatch, _Session(_Response(status=500)))
    assert fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS) is None


def test_a_redirect_off_the_site_is_refused(monkeypatch):
    _use(monkeypatch, _Session(_Response(url="https://elsewhere.example/climate")))
    assert fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS) is None


def test_a_body_the_validator_rejects_counts_as_a_failure(monkeypatch):
    _use(monkeypatch, _Session(_Response(body="<html>Site under maintenance</html>")))
    page = fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS,
                       validate=lambda html: "<h1>" in html)
    assert page is None


def test_an_oversized_body_is_refused(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    _use(monkeypatch, _Session(_Response()))
    assert fetch.fetch("https://teriin.org/climate", allowed_hosts=HOSTS) is None
