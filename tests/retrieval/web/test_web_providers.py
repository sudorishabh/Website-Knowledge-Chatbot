"""Web search providers and the one ``search`` entry point around them.

The vendor is a setting, so these tests pin the contract every adapter meets —
the request each vendor's API expects, and results parsed into ``SearchHit`` —
and what ``search`` adds around any adapter: selection by configuration, the
cache, a bounded retry, the domain policy, de-duplication, and degrading every
failure to "no results".

HTTP goes through ``httpx.MockTransport``: real request objects and real
response parsing, no network, no API key.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import get_settings
from app.retrieval.web import cache, providers, safety
from app.retrieval.web.providers import (
    BraveSearchProvider,
    SearchHit,
    TavilySearchProvider,
)


@pytest.fixture
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "web_search_provider", "brave")
    monkeypatch.setattr(s, "web_search_api_key", "test-key")
    monkeypatch.setattr(s, "web_search_endpoint", "")
    monkeypatch.setattr(s, "web_search_max_results", 8)
    monkeypatch.setattr(s, "web_search_retries", 1)
    monkeypatch.setattr(s, "web_primary_domains", "teriin.org")
    monkeypatch.setattr(s, "web_blocked_domains", "")
    monkeypatch.setattr(s, "web_allow_third_party", True)
    monkeypatch.setattr(providers, "_RETRY_PAUSE_SECONDS", 0.0)
    return s


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(cache, "get_redis", lambda: None)
    store = cache.TTLCache("search", ttl=3600)
    monkeypatch.setattr(providers, "search_cache", lambda: store)
    return store


class _Server:
    """A scripted search API: records every request, answers from a queue."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def _serve(monkeypatch, server: _Server) -> None:
    client = httpx.Client(transport=httpx.MockTransport(server))
    monkeypatch.setattr(providers, "get_web_http_client", lambda: client)


def _brave(*results: dict) -> httpx.Response:
    return httpx.Response(200, json={"web": {"results": list(results)}})


PROFILE = {
    "title": "Mr <strong>Sayanta Ghosh</strong> | TERI",
    "url": "https://www.teriin.org/user/15680",
    "description": "Associate Fellow &amp; GIS specialist. More than 25 publications.",
    "page_age": "2025-03-04T10:22:00",
}


# --- Brave -----------------------------------------------------------------


def test_brave_is_asked_with_the_token_and_the_site_operator(settings, monkeypatch):
    server = _Server(_brave(PROFILE))
    _serve(monkeypatch, server)
    BraveSearchProvider("test-key").search("Sayanta Ghosh", site="teriin.org", limit=5)
    (request,) = server.requests
    assert request.url.host == "api.search.brave.com"
    assert request.headers["X-Subscription-Token"] == "test-key"
    assert request.url.params["q"] == "Sayanta Ghosh site:teriin.org"
    assert request.url.params["count"] == "5"


def test_brave_results_are_parsed_into_plain_text_hits(settings, monkeypatch):
    _serve(monkeypatch, _Server(_brave(PROFILE)))
    (hit,) = BraveSearchProvider("k").search("Sayanta Ghosh", site=None, limit=5)
    assert hit.url == "https://www.teriin.org/user/15680"
    assert hit.title == "Mr Sayanta Ghosh | TERI"
    assert hit.snippet == "Associate Fellow & GIS specialist. More than 25 publications."
    assert hit.published == "2025-03-04"
    assert (hit.provider, hit.rank, hit.query) == ("brave", 1, "Sayanta Ghosh")


def test_brave_without_web_results_is_an_empty_list(settings, monkeypatch):
    _serve(monkeypatch, _Server(httpx.Response(200, json={"query": {}})))
    assert BraveSearchProvider("k").search("nothing", site=None, limit=5) == []


# --- Tavily ----------------------------------------------------------------


def test_tavily_is_asked_with_a_bearer_token_and_include_domains(settings, monkeypatch):
    server = _Server(httpx.Response(200, json={"results": []}))
    _serve(monkeypatch, server)
    TavilySearchProvider("tvly-key").search("truck emissions", site="teriin.org", limit=4)
    (request,) = server.requests
    assert request.method == "POST"
    assert request.url.host == "api.tavily.com"
    assert request.headers["Authorization"] == "Bearer tvly-key"
    body = json.loads(request.content)
    assert body["query"] == "truck emissions"
    assert body["include_domains"] == ["teriin.org"]
    assert body["max_results"] == 4


def test_tavily_results_and_their_rfc_dates_are_parsed(settings, monkeypatch):
    _serve(monkeypatch, _Server(httpx.Response(200, json={"results": [{
        "title": "A-PAG, IIT Delhi, and TERI Release Landmark Report",
        "url": "https://www.teriin.org/press-release/pag-iit-delhi-and-teri-release",
        "content": "Interstate trucks behind nearly a quarter of transport pollution.",
        "published_date": "Mon, 29 Jun 2026 09:00:00 GMT",
    }]})))
    (hit,) = TavilySearchProvider("k").search("trucks", site=None, limit=4)
    assert hit.published == "2026-06-29"
    assert hit.provider == "tavily"


def test_an_endpoint_override_is_used_instead_of_the_public_api(settings, monkeypatch):
    server = _Server(httpx.Response(200, json={"results": []}))
    _serve(monkeypatch, server)
    TavilySearchProvider("k", "https://search-proxy.example.org/search").search(
        "q", site=None, limit=1
    )
    assert server.requests[0].url.host == "search-proxy.example.org"


# --- choosing a provider ---------------------------------------------------


@pytest.mark.parametrize("name, expected", [("brave", BraveSearchProvider),
                                            ("Tavily", TavilySearchProvider)])
def test_the_provider_is_chosen_by_setting(settings, monkeypatch, name, expected):
    monkeypatch.setattr(settings, "web_search_provider", name)
    assert isinstance(providers.get_provider(), expected)


@pytest.mark.parametrize("name, key", [("", "k"), ("bing-scraper", "k"), ("brave", "")])
def test_no_name_an_unknown_name_or_no_key_means_no_provider(settings, monkeypatch, name, key):
    monkeypatch.setattr(settings, "web_search_provider", name)
    monkeypatch.setattr(settings, "web_search_api_key", key)
    assert providers.get_provider() is None


def test_search_without_a_provider_makes_no_request(settings, fresh_cache, monkeypatch):
    monkeypatch.setattr(settings, "web_search_provider", "")
    server = _Server(_brave(PROFILE))
    _serve(monkeypatch, server)
    assert providers.search("anything") == []
    assert server.requests == []


# --- what search() adds ----------------------------------------------------


def test_a_repeated_search_is_answered_from_the_cache(settings, fresh_cache, monkeypatch):
    server = _Server(_brave(PROFILE))
    _serve(monkeypatch, server)
    first = providers.search("Sayanta Ghosh", site="teriin.org")
    second = providers.search("  sayanta   GHOSH ", site="teriin.org")
    assert first == second
    assert len(server.requests) == 1


def test_a_different_site_restriction_is_a_different_search(settings, fresh_cache, monkeypatch):
    server = _Server(_brave(PROFILE))
    _serve(monkeypatch, server)
    providers.search("Sayanta Ghosh", site="teriin.org")
    providers.search("Sayanta Ghosh", site=None)
    assert len(server.requests) == 2


def test_a_transient_failure_is_retried_once(settings, fresh_cache, monkeypatch):
    server = _Server(httpx.Response(503), _brave(PROFILE))
    _serve(monkeypatch, server)
    hits = providers.search("Sayanta Ghosh")
    assert [h.url for h in hits] == [PROFILE["url"]]
    assert len(server.requests) == 2


def test_a_persistent_failure_degrades_to_no_results(settings, fresh_cache, monkeypatch):
    server = _Server(httpx.Response(503))
    _serve(monkeypatch, server)
    assert providers.search("Sayanta Ghosh") == []
    assert len(server.requests) == 2          # one try, one retry, then give up


def test_a_timeout_degrades_to_no_results(settings, fresh_cache, monkeypatch):
    _serve(monkeypatch, _Server(httpx.ReadTimeout("slow")))
    assert providers.search("Sayanta Ghosh") == []


def test_a_client_error_is_not_retried(settings, fresh_cache, monkeypatch):
    server = _Server(httpx.Response(401))
    _serve(monkeypatch, server)
    assert providers.search("Sayanta Ghosh") == []
    assert len(server.requests) == 1


def test_a_failed_search_is_not_cached(settings, fresh_cache, monkeypatch):
    _serve(monkeypatch, _Server(httpx.Response(500)))
    providers.search("Sayanta Ghosh")
    server = _Server(_brave(PROFILE))
    _serve(monkeypatch, server)
    assert providers.search("Sayanta Ghosh")
    assert len(server.requests) == 1


def test_results_the_policy_refuses_are_dropped(settings, fresh_cache, monkeypatch):
    monkeypatch.setattr(settings, "web_blocked_domains", "spam.example")
    _serve(monkeypatch, _Server(_brave(
        PROFILE,
        {"title": "spam", "url": "https://www.spam.example/teri"},
        {"title": "local", "url": "http://localhost/teri"},
        {"title": "file", "url": "file:///etc/passwd"},
    )))
    assert [h.url for h in providers.search("Sayanta Ghosh")] == [PROFILE["url"]]


def test_third_party_results_are_dropped_when_disallowed(settings, fresh_cache, monkeypatch):
    monkeypatch.setattr(settings, "web_allow_third_party", False)
    _serve(monkeypatch, _Server(_brave(
        {"title": "news", "url": "https://example-news.com/teri-study"}, PROFILE,
    )))
    assert [h.url for h in providers.search("Sayanta Ghosh")] == [PROFILE["url"]]


def test_the_same_page_listed_twice_is_kept_once_at_its_best_rank(settings, fresh_cache, monkeypatch):
    _serve(monkeypatch, _Server(_brave(
        PROFILE,
        {"title": "again", "url": "http://teriin.org/user/15680/?utm_source=feed"},
    )))
    hits = providers.search("Sayanta Ghosh")
    assert len(hits) == 1 and hits[0].rank == 1


def test_search_never_resolves_dns_itself(settings, fresh_cache, monkeypatch):
    def fail(*_):
        raise AssertionError("result filtering must not touch DNS")

    monkeypatch.setattr(safety, "_addresses", fail)
    _serve(monkeypatch, _Server(_brave(PROFILE)))
    assert providers.search("Sayanta Ghosh")


def test_a_search_is_traced_with_what_was_asked_kept_and_dropped(
    settings, fresh_cache, monkeypatch, tmp_path
):
    from app.observability import retrieval_log

    monkeypatch.setattr(settings, "is_retrieval_log", True, raising=False)
    monkeypatch.setattr(settings, "retrieval_log_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "retrieval_log_report", False, raising=False)
    monkeypatch.setattr(settings, "retrieval_log_summary", False, raising=False)
    _serve(monkeypatch, _Server(_brave(PROFILE, {"title": "x", "url": "http://localhost/"})))

    with retrieval_log.query_log("who is Sayanta Ghosh", entrypoint="test"):
        providers.search("Sayanta Ghosh", site="teriin.org")

    (trace_file,) = [p for p in tmp_path.rglob("trace.json") if "errors" not in p.parts]
    text = trace_file.read_text(encoding="utf-8")
    trace = json.loads(text)
    (event,) = [e for e in trace["events"] if e["retriever"] == "web"]
    assert event["stage"] == "web_search"
    assert event["request"]["provider"] == "brave"
    assert event["request"]["site"] == "teriin.org"
    assert event["result_count"] == 1
    assert event["metrics"]["cached"] is False
    assert event["metrics"]["dropped"] == {"http://localhost/": "private_host"}
    assert "test-key" not in text              # the API key never reaches a trace


def test_a_hit_survives_a_cache_round_trip():
    hit = SearchHit(url="https://www.teriin.org/air", title="Air", snippet="s",
                    provider="brave", query="q", rank=2, site="teriin.org",
                    published="2026-06-29")
    assert SearchHit.from_dict(json.loads(json.dumps(hit.to_dict()))) == hit
