"""The web fetcher: safe, polite, bounded, and never a crawler.

Each property here is something a fetched page must never be able to talk the
server out of: every URL and every redirect hop checked before it is requested,
robots.txt obeyed (RFC 9309 status rules, Crawl-delay included), requests to a
site spaced out, responses capped by size and type, transient failures retried
a bounded number of times, and concurrent requests for one page sharing one
fetch.

HTTP goes through ``httpx.MockTransport`` as a scripted multi-site server; DNS
answers public for every name; the clock and sleep are fake, so pacing is
asserted exactly and no test waits.
"""
from __future__ import annotations

import threading
import time as real_time

import httpx
import pytest

from app.config import get_settings
from app.retrieval.web import cache, fetch, safety
from app.retrieval.web.fetch import FetchRefused

PAGE = "https://www.teriin.org/user/15680"
HTML_BODY = b"<!doctype html><html><head><title>Mr Sayanta Ghosh</title></head></html>"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Server:
    """Routes by (host, path). A route is a response, an exception to raise, a
    list consumed one per request, or a callable taking the request."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [f"{r.url.host}{r.url.path}" for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.url.host, request.url.path))
        if route is None:
            return httpx.Response(404)
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if callable(route) and not isinstance(route, httpx.Response):
            route = route(request)
        if isinstance(route, Exception):
            raise route
        return route


def _html(body: bytes = HTML_BODY, **headers) -> httpx.Response:
    return httpx.Response(200, content=body,
                          headers={"content-type": "text/html; charset=utf-8", **headers})


def _robots(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/plain"})


ALLOW_ALL = _robots("User-agent: *\nAllow: /\n")


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(fetch, "_now", fake.monotonic)
    monkeypatch.setattr(fetch, "_sleep", fake.sleep)
    return fake


@pytest.fixture(autouse=True)
def isolated(monkeypatch, clock):
    s = get_settings()
    for name, value in {
        "web_respect_robots": True,
        "web_per_host_interval_seconds": 1.0,
        "web_max_host_wait_seconds": 3.0,
        "web_fetch_retries": 1,
        "web_max_redirects": 3,
        "web_fetch_pdfs": True,
        "web_fetch_max_bytes": 1000,
        "web_pdf_max_bytes": 5000,
        "web_primary_domains": "teriin.org",
        "web_blocked_domains": "",
        "web_allow_third_party": True,
        "web_user_agent": "TERI-Knowledge-Assistant/1.0 (+https://www.teriin.org)",
    }.items():
        monkeypatch.setattr(s, name, value)
    monkeypatch.setattr(safety, "_addresses", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(cache, "get_redis", lambda: None)
    robots_store = cache.TTLCache("robots", ttl=3600)
    monkeypatch.setattr(fetch, "robots_cache", lambda: robots_store)
    monkeypatch.setattr(fetch, "_gate", fetch._HostGate())
    monkeypatch.setattr(fetch, "_coalescer", fetch._Coalescer())
    return s


def _serve(monkeypatch, routes: dict) -> _Server:
    server = _Server(routes)
    client = httpx.Client(transport=httpx.MockTransport(server), follow_redirects=False)
    monkeypatch.setattr(fetch, "get_web_http_client", lambda: client)
    return server


def _refusal(url: str = PAGE) -> str:
    with pytest.raises(FetchRefused) as caught:
        fetch.fetch(url)
    return caught.value.reason


# --- the ordinary case -----------------------------------------------------


def test_a_page_is_fetched_after_its_sites_robots_txt(monkeypatch):
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): ALLOW_ALL,
        ("www.teriin.org", "/user/15680"): _html(),
    })
    page = fetch.fetch(PAGE)
    assert page.kind == fetch.HTML
    assert page.content == HTML_BODY
    assert page.final_url == PAGE and page.status == 200 and page.redirects == 0
    assert page.fetched_at.endswith("+00:00")
    assert server.paths() == ["www.teriin.org/robots.txt", "www.teriin.org/user/15680"]
    accept = server.requests[1].headers["accept"]
    assert "text/html" in accept and "application/pdf" in accept


def test_an_unsafe_url_is_refused_before_any_request(monkeypatch):
    server = _serve(monkeypatch, {})
    assert _refusal("http://127.0.0.1/admin") == safety.PRIVATE_ADDRESS
    assert server.requests == []


# --- robots.txt ------------------------------------------------------------


def test_a_page_robots_txt_disallows_is_never_requested(monkeypatch):
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): _robots("User-agent: *\nDisallow: /user/\n"),
        ("www.teriin.org", "/user/15680"): _html(),
    })
    assert _refusal() == fetch.ROBOTS_DISALLOWED
    assert server.paths() == ["www.teriin.org/robots.txt"]


def test_rules_addressed_to_this_agent_are_obeyed(monkeypatch):
    _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): _robots(
            "User-agent: TERI-Knowledge-Assistant\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
        ),
        ("www.teriin.org", "/user/15680"): _html(),
    })
    assert _refusal() == fetch.ROBOTS_DISALLOWED


def test_a_missing_robots_txt_means_no_rules(monkeypatch):
    _serve(monkeypatch, {("www.teriin.org", "/user/15680"): _html()})  # robots -> 404
    assert fetch.fetch(PAGE).status == 200


@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.ConnectError("down")])
def test_an_unreadable_robots_txt_means_leave_the_site_alone(monkeypatch, failure):
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): failure,
        ("www.teriin.org", "/user/15680"): _html(),
    })
    assert _refusal() == fetch.ROBOTS_DISALLOWED
    assert "www.teriin.org/user/15680" not in server.paths()


def test_an_unreadable_robots_txt_is_asked_again_next_time(monkeypatch):
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): [httpx.Response(503), httpx.Response(503),
                                            ALLOW_ALL],
        ("www.teriin.org", "/user/15680"): _html(),
    })
    assert _refusal() == fetch.ROBOTS_DISALLOWED   # 503, retried once, 503 again
    assert fetch.fetch(PAGE).status == 200         # asked again, not shunned
    assert server.paths().count("www.teriin.org/robots.txt") == 3


def test_robots_txt_is_read_once_per_site(monkeypatch):
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): ALLOW_ALL,
        ("www.teriin.org", "/user/15680"): _html(),
        ("www.teriin.org", "/air"): _html(),
    })
    fetch.fetch(PAGE)
    fetch.fetch("https://www.teriin.org/air")
    assert server.paths().count("www.teriin.org/robots.txt") == 1


def test_robots_txt_is_not_read_when_the_setting_is_off(monkeypatch, isolated):
    monkeypatch.setattr(isolated, "web_respect_robots", False)
    server = _serve(monkeypatch, {("www.teriin.org", "/user/15680"): _html()})
    fetch.fetch(PAGE)
    assert server.paths() == ["www.teriin.org/user/15680"]


# --- pacing ----------------------------------------------------------------


def test_requests_to_one_site_are_spaced_by_the_interval(monkeypatch, clock):
    _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): ALLOW_ALL,
        ("www.teriin.org", "/user/15680"): _html(),
    })
    fetch.fetch(PAGE)                   # robots.txt at t0, the page one second later
    assert clock.sleeps == [1.0]


def test_a_sites_crawl_delay_is_honoured(monkeypatch, clock):
    _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): _robots("User-agent: *\nCrawl-delay: 2\n"),
        ("www.teriin.org", "/user/15680"): _html(),
        ("www.teriin.org", "/air"): _html(),
    })
    fetch.fetch(PAGE)
    fetch.fetch("https://www.teriin.org/air")
    assert clock.sleeps == [1.0, 2.0]


def test_a_turn_further_away_than_the_wait_allows_is_skipped(monkeypatch, clock):
    _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): _robots("User-agent: *\nCrawl-delay: 5\n"),
        ("www.teriin.org", "/user/15680"): _html(),
        ("www.teriin.org", "/air"): _html(),
    })
    fetch.fetch(PAGE)
    assert _refusal("https://www.teriin.org/air") == fetch.RATE_LIMITED


def test_different_sites_do_not_wait_on_each_other(monkeypatch, clock, isolated):
    monkeypatch.setattr(isolated, "web_respect_robots", False)
    _serve(monkeypatch, {
        ("www.teriin.org", "/user/15680"): _html(),
        ("example.org", "/report"): _html(),
    })
    fetch.fetch(PAGE)
    fetch.fetch("https://example.org/report")
    assert clock.sleeps == []


# --- redirects -------------------------------------------------------------


def _redirect(location: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


def test_a_relative_redirect_is_followed_to_the_final_address(monkeypatch, isolated):
    monkeypatch.setattr(isolated, "web_respect_robots", False)
    _serve(monkeypatch, {
        ("www.teriin.org", "/user/15680"): _redirect("/profile/sayanta-ghosh"),
        ("www.teriin.org", "/profile/sayanta-ghosh"): _html(),
    })
    page = fetch.fetch(PAGE)
    assert page.final_url == "https://www.teriin.org/profile/sayanta-ghosh"
    assert page.redirects == 1


def test_a_redirect_into_the_private_network_is_refused(monkeypatch, isolated):
    monkeypatch.setattr(isolated, "web_respect_robots", False)
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/user/15680"): _redirect("http://169.254.169.254/latest/meta-data/"),
    })
    assert _refusal() == safety.PRIVATE_ADDRESS
    assert server.paths() == ["www.teriin.org/user/15680"]


def test_a_redirect_is_checked_against_the_destinations_robots_txt(monkeypatch):
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): ALLOW_ALL,
        ("www.teriin.org", "/user/15680"): _redirect("https://other.example/profile"),
        ("other.example", "/robots.txt"): _robots("User-agent: *\nDisallow: /\n"),
        ("other.example", "/profile"): _html(),
    })
    assert _refusal() == fetch.ROBOTS_DISALLOWED
    assert "other.example/profile" not in server.paths()


def test_redirect_chains_are_bounded(monkeypatch, isolated):
    monkeypatch.setattr(isolated, "web_respect_robots", False)
    monkeypatch.setattr(isolated, "web_max_redirects", 2)
    _serve(monkeypatch, {
        ("www.teriin.org", "/user/15680"): _redirect("/a"),
        ("www.teriin.org", "/a"): _redirect("/b"),
        ("www.teriin.org", "/b"): _redirect("/c"),
        ("www.teriin.org", "/c"): _html(),
    })
    assert _refusal() == fetch.TOO_MANY_REDIRECTS


def test_a_redirect_without_a_location_is_refused(monkeypatch, isolated):
    monkeypatch.setattr(isolated, "web_respect_robots", False)
    _serve(monkeypatch, {("www.teriin.org", "/user/15680"): httpx.Response(302)})
    assert _refusal() == fetch.BAD_REDIRECT


# --- failures and retries --------------------------------------------------


@pytest.fixture
def no_robots(monkeypatch, isolated):
    monkeypatch.setattr(isolated, "web_respect_robots", False)


def test_a_transient_failure_is_retried_once(monkeypatch, no_robots):
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/user/15680"): [httpx.Response(503), _html()],
    })
    assert fetch.fetch(PAGE).status == 200
    assert len(server.requests) == 2


def test_a_persistent_server_error_is_refused_with_its_status(monkeypatch, no_robots):
    server = _serve(monkeypatch, {("www.teriin.org", "/user/15680"): httpx.Response(503)})
    assert _refusal() == "http_503"
    assert len(server.requests) == 2


def test_a_not_found_is_refused_without_a_retry(monkeypatch, no_robots):
    server = _serve(monkeypatch, {("www.teriin.org", "/user/15680"): httpx.Response(404)})
    assert _refusal() == "http_404"
    assert len(server.requests) == 1


def test_a_network_failure_is_retried_then_refused(monkeypatch, no_robots):
    server = _serve(monkeypatch, {
        ("www.teriin.org", "/user/15680"): httpx.ConnectTimeout("slow"),
    })
    assert _refusal() == fetch.NETWORK_ERROR
    assert len(server.requests) == 2


# --- size and type ---------------------------------------------------------


def test_a_declared_length_over_the_cap_is_refused(monkeypatch, no_robots):
    _serve(monkeypatch, {("www.teriin.org", "/user/15680"): _html(b"x" * 1500)})
    assert _refusal() == fetch.TOO_LARGE


def test_a_body_that_grows_past_the_cap_while_streaming_is_refused(monkeypatch, no_robots):
    streamed = httpx.Response(200, headers={"content-type": "text/html"},
                              content=iter([b"<html>" + b"x" * 600, b"y" * 600]))
    _serve(monkeypatch, {("www.teriin.org", "/user/15680"): streamed})
    assert _refusal() == fetch.TOO_LARGE


def test_a_pdf_gets_its_own_larger_cap(monkeypatch, no_robots):
    pdf = b"%PDF-1.7\n" + b"0" * 3000               # over the page cap, under the PDF cap
    _serve(monkeypatch, {("www.teriin.org", "/files/report.pdf"): httpx.Response(
        200, content=pdf, headers={"content-type": "application/pdf"})})
    page = fetch.fetch("https://www.teriin.org/files/report.pdf")
    assert page.kind == fetch.PDF and page.content == pdf


def test_a_pdf_served_as_an_opaque_type_is_recognised_by_its_bytes(monkeypatch, no_robots):
    _serve(monkeypatch, {("www.teriin.org", "/download"): httpx.Response(
        200, content=b"%PDF-1.4 body", headers={"content-type": "application/octet-stream"})})
    assert fetch.fetch("https://www.teriin.org/download").kind == fetch.PDF


def test_html_served_without_a_type_is_recognised_by_its_bytes(monkeypatch, no_robots):
    _serve(monkeypatch, {("www.teriin.org", "/user/15680"): httpx.Response(
        200, content=HTML_BODY)})
    assert fetch.fetch(PAGE).kind == fetch.HTML


def test_pdfs_are_refused_when_pdf_fetching_is_off(monkeypatch, no_robots, isolated):
    monkeypatch.setattr(isolated, "web_fetch_pdfs", False)
    _serve(monkeypatch, {
        ("www.teriin.org", "/a.pdf"): httpx.Response(
            200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}),
        ("www.teriin.org", "/b"): httpx.Response(
            200, content=b"%PDF-1.4", headers={"content-type": "application/octet-stream"}),
    })
    assert _refusal("https://www.teriin.org/a.pdf") == fetch.PDF_DISABLED
    assert _refusal("https://www.teriin.org/b") == fetch.PDF_DISABLED


@pytest.mark.parametrize("content_type, body", [
    ("image/png", b"\x89PNG"),
    ("application/zip", b"PK\x03\x04"),
    ("application/octet-stream", b"MZ\x90\x00"),      # opaque, and not a document
])
def test_anything_but_a_document_is_refused(monkeypatch, no_robots, content_type, body):
    _serve(monkeypatch, {("www.teriin.org", "/thing"): httpx.Response(
        200, content=body, headers={"content-type": content_type})})
    assert _refusal("https://www.teriin.org/thing") == fetch.UNSUPPORTED_TYPE


# --- one fetch per page ----------------------------------------------------


def test_concurrent_requests_for_one_page_share_one_fetch(monkeypatch, no_robots):
    entered, release = threading.Event(), threading.Event()

    def slow(request):
        entered.set()
        release.wait(5)
        return _html()

    server = _serve(monkeypatch, {("www.teriin.org", "/user/15680"): slow})
    results: list = []
    first = threading.Thread(target=lambda: results.append(fetch.fetch(PAGE)))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=lambda: results.append(
        fetch.fetch("http://teriin.org/user/15680/")))      # the same page, spelled differently
    second.start()
    real_time.sleep(0.2)                                    # let the second caller queue
    release.set()
    first.join(5)
    second.join(5)
    assert len(results) == 2 and results[0] is results[1]
    assert len(server.requests) == 1


def test_a_refusal_is_recorded_in_the_trace(monkeypatch, tmp_path, isolated):
    from app.observability import retrieval_log
    import json

    monkeypatch.setattr(isolated, "is_retrieval_log", True, raising=False)
    monkeypatch.setattr(isolated, "retrieval_log_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(isolated, "retrieval_log_report", False, raising=False)
    monkeypatch.setattr(isolated, "retrieval_log_summary", False, raising=False)
    _serve(monkeypatch, {
        ("www.teriin.org", "/robots.txt"): _robots("User-agent: *\nDisallow: /\n"),
    })
    with retrieval_log.query_log("who is Sayanta Ghosh", entrypoint="test"):
        with pytest.raises(FetchRefused):
            fetch.fetch(PAGE)
    (trace_file,) = [p for p in tmp_path.rglob("trace.json") if "errors" not in p.parts]
    (event,) = [e for e in json.loads(trace_file.read_text(encoding="utf-8"))["events"]
                if e["stage"] == "web_fetch"]
    assert event["request"]["url"] == PAGE
    assert event["metrics"]["refused"] == fetch.ROBOTS_DISALLOWED
