"""The chat client never sends a temperature to a deployment that rejects one.

Measured 2026-09-25 on gpt-6-luna: every call carrying `temperature` came back
400 "Unsupported parameter: 'temperature' is not supported with this model" —
the answer call (0.2), the voted query understanding (0.7) and every
structured call with `LLM_STRUCTURED_TEMPERATURE=0` — so no question could be
answered. The clients are built, never called; no network.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.clients import llm


@pytest.fixture
def settings(monkeypatch):
    def use(**over):
        values = dict(
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_api_key="test-key",
            azure_openai_api_version="2024-06-01",
            azure_openai_model="some-deployment",
            llm_structured_temperature=0.0,
            llm_temperature_supported=True,
        )
        values.update(over)
        monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(**values))
        llm.get_llm.cache_clear()

    yield use
    llm.get_llm.cache_clear()


def test_a_supported_temperature_is_sent(settings):
    settings()
    assert llm.get_llm(temperature=0.2).temperature == 0.2


def test_an_unsupported_temperature_is_never_sent(settings):
    settings(llm_temperature_supported=False)
    assert llm.get_llm(temperature=0.2).temperature is None
    assert llm.get_llm(temperature=0.7, streaming=True).temperature is None


def test_the_structured_temperature_is_dropped_too(settings):
    settings(llm_temperature_supported=False, llm_structured_temperature=0.0)
    assert llm.get_structured_llm().temperature is None


def test_the_default_is_to_send_it():
    # Every deployment this app ran on before gpt-6-luna takes a temperature.
    from app.config import Settings

    assert Settings.model_fields["llm_temperature_supported"].default is True
