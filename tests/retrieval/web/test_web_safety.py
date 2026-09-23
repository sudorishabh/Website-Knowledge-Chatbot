"""URL safety for web retrieval: what may be fetched, and when two URLs are one.

Every URL the web layer fetches comes from a search provider or a redirect —
input it does not control. These tests pin the server-side request forgery
guard (the server must never be pointed at itself or its network), the domain
policy, and the comparison key used to de-duplicate results.

DNS is replaced through the module's ``_addresses`` seam, so no test touches the
network.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.retrieval.web import safety
from app.retrieval.web.safety import DomainPolicy, UnsafeURL, check_url, url_key

TERI = DomainPolicy(primary=("teriin.org",))


@pytest.fixture
def public_dns(monkeypatch):
    """Every hostname resolves to one public address."""
    monkeypatch.setattr(safety, "_addresses", lambda host, port: ["93.184.216.34"])


def _reason(url: str, **kwargs) -> str:
    with pytest.raises(UnsafeURL) as caught:
        check_url(url, domain_policy=kwargs.pop("domain_policy", TERI), **kwargs)
    return caught.value.reason


# --- shape -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://teriin.org/a.pdf", "javascript:alert(1)",
            "gopher://teriin.org/", "teriin.org/no-scheme"],
)
def test_only_http_and_https_are_fetched(url):
    assert _reason(url, resolve=False) == safety.SCHEME


def test_credentials_in_a_url_are_refused():
    assert _reason("https://user:secret@teriin.org/", resolve=False) == safety.CREDENTIALS


@pytest.mark.parametrize("url", ["https://teriin.org:8080/", "http://teriin.org:6379/",
                                 "https://teriin.org:99999/"])
def test_non_web_ports_are_refused(url):
    assert _reason(url, resolve=False) == safety.PORT


def test_explicit_web_ports_are_allowed(public_dns):
    assert check_url("https://teriin.org:443/air", domain_policy=TERI)
    assert check_url("http://teriin.org:80/air", domain_policy=TERI)


# --- server-side request forgery ------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://intranet/",               # single-label name
        "http://2130706433/",             # 127.0.0.1 written as a decimal
        "http://printer.local/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_private_hostnames_are_refused_without_a_lookup(url, monkeypatch):
    def fail(*_):
        raise AssertionError("a private name must be refused before DNS")

    monkeypatch.setattr(safety, "_addresses", fail)
    assert _reason(url, domain_policy=DomainPolicy()) == safety.PRIVATE_HOST


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.1.1/",
        "http://172.16.4.2/", "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
    ],
)
def test_private_address_literals_are_refused(url):
    assert _reason(url, domain_policy=DomainPolicy()) == safety.PRIVATE_ADDRESS


def test_a_public_name_that_resolves_privately_is_refused(monkeypatch):
    monkeypatch.setattr(safety, "_addresses", lambda host, port: ["10.1.2.3"])
    assert _reason("https://teriin.org/air") == safety.PRIVATE_ADDRESS


def test_every_resolved_address_must_be_public(monkeypatch):
    # One public record is not enough: the client could connect to the other.
    monkeypatch.setattr(
        safety, "_addresses", lambda host, port: ["93.184.216.34", "127.0.0.1"]
    )
    assert _reason("https://teriin.org/air") == safety.PRIVATE_ADDRESS


def test_an_unresolvable_host_is_refused(monkeypatch):
    def nxdomain(*_):
        raise OSError("name or service not known")

    monkeypatch.setattr(safety, "_addresses", nxdomain)
    assert _reason("https://no-such-host.teriin.org/") == safety.UNRESOLVABLE


def test_a_public_url_is_returned_unchanged(public_dns):
    url = "https://www.teriin.org/user/15680"
    assert check_url(url, domain_policy=TERI) == url


def test_resolve_false_checks_shape_and_policy_only(monkeypatch):
    def fail(*_):
        raise AssertionError("resolve=False must not look anything up")

    monkeypatch.setattr(safety, "_addresses", fail)
    assert check_url("https://www.teriin.org/air", domain_policy=TERI, resolve=False)


# --- domain policy ---------------------------------------------------------


def test_subdomains_match_on_a_label_boundary_only():
    domains = ("teriin.org",)
    assert safety.matches_domain("teriin.org", domains)
    assert safety.matches_domain("www.teriin.org", domains)
    assert safety.matches_domain("static.teriin.org", domains)
    assert not safety.matches_domain("evilteriin.org", domains)
    assert not safety.matches_domain("teriin.org.evil.com", domains)


def test_a_blocked_domain_and_its_subdomains_are_refused():
    policy = DomainPolicy(primary=("teriin.org",), blocked=("spam.example",))
    assert _reason("https://cdn.spam.example/x", domain_policy=policy,
                   resolve=False) == safety.BLOCKED_DOMAIN


def test_blocking_wins_over_being_primary():
    policy = DomainPolicy(primary=("teriin.org",), blocked=("teriin.org",))
    assert _reason("https://teriin.org/", domain_policy=policy,
                   resolve=False) == safety.BLOCKED_DOMAIN


def test_third_party_sites_are_refused_when_disallowed():
    policy = DomainPolicy(primary=("teriin.org",), allow_third_party=False)
    assert _reason("https://example.org/report", domain_policy=policy,
                   resolve=False) == safety.THIRD_PARTY
    assert check_url("https://www.teriin.org/air", domain_policy=policy, resolve=False)


def test_the_policy_is_read_from_settings_as_an_operator_would_write_it(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_primary_domains",
                        "https://www.TERIIN.org/, teri.res.in ; teriin.org")
    monkeypatch.setattr(settings, "web_blocked_domains", "spam.example")
    monkeypatch.setattr(settings, "web_allow_third_party", False)
    policy = safety.policy()
    assert policy.primary == ("teriin.org", "teri.res.in")
    assert policy.blocked == ("spam.example",)
    assert policy.allow_third_party is False
    assert policy.is_primary("https://www.teriin.org/air")
    assert not policy.is_primary("https://example.org/")


# --- comparison key --------------------------------------------------------


def test_presentation_differences_do_not_make_a_different_page():
    variants = [
        "https://www.teriin.org/news/trucks-delhi",
        "http://teriin.org/news/trucks-delhi/",
        "https://teriin.org/news/trucks-delhi#section-2",
        "https://TERIIN.org/news/trucks-delhi?utm_source=x&utm_medium=y",
        "https://teriin.org/news/trucks-delhi?fbclid=abc",
    ]
    assert len({url_key(v) for v in variants}) == 1


def test_percent_escapes_and_parameter_order_are_ignored():
    assert url_key("https://teriin.org/files/Baseline%20Report.pdf") == url_key(
        "https://teriin.org/files/Baseline Report.pdf"
    )
    assert url_key("https://teriin.org/news?page=2&theme=51") == url_key(
        "https://teriin.org/news?theme=51&page=2"
    )


def test_a_meaningful_difference_is_a_different_page():
    assert url_key("https://teriin.org/news?page=1") != url_key(
        "https://teriin.org/news?page=2"
    )
    assert url_key("https://teriin.org/air") != url_key("https://teriin.org/water")
    assert url_key("https://teriin.org/air") != url_key("https://example.org/air")
