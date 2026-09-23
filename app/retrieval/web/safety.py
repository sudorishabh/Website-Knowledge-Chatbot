"""Whether a URL may be fetched, and how two URLs are recognised as one.

Every URL this package fetches came from somewhere it does not control — a
search provider's result list, a redirect a server chose to send — so each one
is checked here before any request is made, and the fetcher checks again at
every redirect hop.

The check is aimed at server-side request forgery: a result or a redirect that
points the server at itself or its network (``localhost``, ``10.0.0.5``, the
cloud metadata address ``169.254.169.254``). Hostnames are *resolved* and every
address they resolve to must be public, because the dangerous forms are rarely
literal — ``2130706433`` and ``0x7f.1`` are both 127.0.0.1, and a public-looking
name can resolve to a private address.

One residual risk is stated rather than hidden: the name is resolved here and
again by the HTTP client when it connects, so a DNS server that answers
differently the second time (rebinding) is not caught by this check alone. The
fetcher's own limits — no credentials, no non-web ports, bounded size, no
crawling — are what bound the damage in that case.

The domain policy lives here too, since "may this be fetched" and "is this the
organisation's own site" are the same question asked of the same host.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from app.config import get_settings

__all__ = [
    "DomainPolicy",
    "UnsafeURL",
    "check_url",
    "domain_of",
    "matches_domain",
    "policy",
    "url_key",
]

_SCHEMES = frozenset({"http", "https"})
# Web ports only. A search result has no business pointing at a database port,
# and allowing arbitrary ports is how a fetcher becomes a port scanner.
_PORTS = frozenset({80, 443})
# Suffixes that name a private network whatever they happen to resolve to.
_PRIVATE_SUFFIXES = (
    ".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp",
)
# Query parameters that identify a click, not a document. Dropped from the
# comparison key so the same page reached from two campaigns is one page.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid"})

# Why a URL was refused. Stable strings, because they are what the retrieval
# trace records when someone asks why a result was not used.
SCHEME = "scheme"
CREDENTIALS = "credentials"
NO_HOST = "no_host"
PORT = "port"
PRIVATE_HOST = "private_host"
PRIVATE_ADDRESS = "private_address"
UNRESOLVABLE = "unresolvable"
BLOCKED_DOMAIN = "blocked_domain"
THIRD_PARTY = "third_party_disallowed"


class UnsafeURL(ValueError):
    """A URL this package must not fetch, and the reason code why."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason


def _normal_host(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


def domain_of(url: str) -> str:
    """The URL's host, lowercased and without a leading ``www.``."""
    return _normal_host(urlsplit(url).hostname).removeprefix("www.")


def matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    """Whether ``host`` is one of ``domains`` or a subdomain of one.

    Suffix matching on a label boundary, so ``www.teriin.org`` matches
    ``teriin.org`` while ``evilteriin.org`` and ``teriin.org.evil.com`` do not.
    """
    host = _normal_host(host).removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in domains)


def _domains(raw: str) -> tuple[str, ...]:
    """A comma-separated setting as bare domains. Tolerates what an operator is
    likely to paste — a scheme, a ``www.``, a trailing slash, capitals."""
    out: list[str] = []
    for item in (raw or "").replace(";", ",").split(","):
        item = item.strip().lower()
        if not item:
            continue
        host = urlsplit(item if "://" in item else f"//{item}").hostname or ""
        host = _normal_host(host).removeprefix("www.")
        if host and host not in out:
            out.append(host)
    return tuple(out)


@dataclass(frozen=True)
class DomainPolicy:
    """Which sites count as the organisation's own, and which may not be used."""

    primary: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    allow_third_party: bool = True

    def is_primary(self, url: str) -> bool:
        return matches_domain(domain_of(url), self.primary)

    def is_blocked(self, url: str) -> bool:
        return matches_domain(domain_of(url), self.blocked)

    def refusal(self, url: str) -> str | None:
        """Why the policy refuses this URL, or None when it admits it. A blocked
        domain is refused even if it is also listed as primary."""
        if self.is_blocked(url):
            return BLOCKED_DOMAIN
        if not self.allow_third_party and not self.is_primary(url):
            return THIRD_PARTY
        return None


def policy() -> DomainPolicy:
    """The domain policy the settings describe."""
    settings = get_settings()
    return DomainPolicy(
        primary=_domains(getattr(settings, "web_primary_domains", "")),
        blocked=_domains(getattr(settings, "web_blocked_domains", "")),
        allow_third_party=bool(getattr(settings, "web_allow_third_party", True)),
    )


def _addresses(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to. A seam for tests: it is the one
    place this module touches the network."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _is_public(address: str) -> bool:
    """Whether an address is globally routable unicast.

    ``is_global`` already excludes private, loopback, link-local (the metadata
    address), carrier-grade NAT and reserved ranges. An IPv4 address wrapped in
    IPv6 (``::ffff:127.0.0.1``) is judged as the IPv4 address it carries.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(
    url: str,
    *,
    domain_policy: DomainPolicy | None = None,
    resolve: bool = True,
) -> str:
    """``url`` unchanged when it may be fetched; raises :class:`UnsafeURL` otherwise.

    Cheap checks run first and the DNS lookup last, so a URL that fails on its
    shape never costs a network round trip. ``resolve=False`` skips the lookup
    for callers that only need the shape and policy verdict (ranking a result
    list, say) and will check again before fetching.
    """
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise UnsafeURL(raw, PORT) from None
    if parts.scheme.lower() not in _SCHEMES:
        raise UnsafeURL(raw, SCHEME)
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL(raw, CREDENTIALS)
    host = _normal_host(parts.hostname)
    if not host:
        raise UnsafeURL(raw, NO_HOST)
    if port is not None and port not in _PORTS:
        raise UnsafeURL(raw, PORT)

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public(host):
            raise UnsafeURL(raw, PRIVATE_ADDRESS)
    elif "." not in host or host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        # A single-label name only resolves on a private network — and it is
        # also how a decimal address such as 2130706433 arrives.
        raise UnsafeURL(raw, PRIVATE_HOST)

    refusal = (domain_policy or policy()).refusal(raw)
    if refusal:
        raise UnsafeURL(raw, refusal)

    if resolve and literal is None:
        default_port = 443 if parts.scheme.lower() == "https" else 80
        try:
            addresses = _addresses(host, port or default_port)
        except OSError:
            raise UnsafeURL(raw, UNRESOLVABLE) from None
        if not addresses:
            raise UnsafeURL(raw, UNRESOLVABLE)
        # Every address, not any: a name with one public and one private record
        # would otherwise let the client pick the private one.
        if not all(_is_public(a) for a in addresses):
            raise UnsafeURL(raw, PRIVATE_ADDRESS)
    return raw


def url_key(url: str) -> str:
    """The identity two URLs are compared by: same key, same page.

    Scheme, ``www.``, default ports, fragments, trailing slashes, percent
    escapes, tracking parameters and query-parameter order are all ignored, since
    none of them changes which document is served. What remains is host, path
    and the meaningful query.
    """
    parts = urlsplit((url or "").strip())
    host = _normal_host(parts.hostname).removeprefix("www.")
    path = unquote(parts.path or "").rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
        and not k.lower().startswith(_TRACKING_PREFIXES)
    )
    key = f"{host}{path}"
    return f"{key}?{urlencode(query)}" if query else key
