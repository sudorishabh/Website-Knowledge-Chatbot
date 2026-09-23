"""The web HTTP client's safety-relevant configuration.

The fetcher checks every URL before requesting it, which only works if the
client never requests a URL on its own — so it must not follow redirects. It
must also identify itself by the configured agent, since that is what robots.txt
rules and site operators key on.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.clients import web


@pytest.fixture
def fresh_client():
    web.get_web_http_client.cache_clear()
    yield
    client = web.get_web_http_client()
    client.close()
    web.get_web_http_client.cache_clear()


def test_the_client_never_follows_a_redirect_on_its_own(fresh_client):
    assert web.get_web_http_client().follow_redirects is False


def test_requests_carry_the_configured_user_agent(fresh_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "web_user_agent", "TestAgent/2.0 (+https://example.org)")
    client = web.get_web_http_client()
    assert client.headers["User-Agent"] == "TestAgent/2.0 (+https://example.org)"


def test_reading_gets_the_fetch_timeout_and_connecting_a_shorter_one(fresh_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "web_fetch_timeout_seconds", 9.0)
    timeout = web.get_web_http_client().timeout
    assert timeout.read == 9.0
    assert timeout.connect == 4.0


def test_a_short_fetch_timeout_also_caps_connecting(fresh_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "web_fetch_timeout_seconds", 2.0)
    assert web.get_web_http_client().timeout.connect == 2.0


def test_the_client_is_built_once_and_shared(fresh_client):
    assert web.get_web_http_client() is web.get_web_http_client()
