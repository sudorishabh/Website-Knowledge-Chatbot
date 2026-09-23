"""The web source marker, and the switches that keep web retrieval off by default.

Every layer that handles a block — ranking, prompt formatting, citations, the
answer cache — has to recognise web evidence by one rule, so it lives in the
core model. And a deployment must never reach the web by accident: the master
switch is off and no provider is configured unless someone sets them.
"""
from __future__ import annotations

from app.config import Settings
from app.core.models.context import WEB_SOURCE_TYPE, is_web, source_kind


def test_a_web_payload_is_recognised_by_its_source_type():
    assert is_web({"source_type": WEB_SOURCE_TYPE})


def test_corpus_payloads_are_not_web():
    for source_type in ("website", "article", "pdf_attachment", None):
        assert not is_web({"source_type": source_type})
    assert not is_web({})


def test_web_is_its_own_source_kind_not_a_website_alias():
    # "website" means an ingested Drupal page; a fetched page must never be
    # folded into it, or it would inherit the corpus's authority and dates.
    assert source_kind({"source_type": WEB_SOURCE_TYPE}) == "web"


def test_web_retrieval_ships_disabled_with_no_provider():
    fields = Settings.model_fields
    assert fields["web_search_enabled"].default is False
    assert fields["web_search_provider"].default == ""
    assert fields["web_search_api_key"].default == ""


def test_the_primary_domain_defaults_to_the_organisations_site():
    assert Settings.model_fields["web_primary_domains"].default == "teriin.org"
