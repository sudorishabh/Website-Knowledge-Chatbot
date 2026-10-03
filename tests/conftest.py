"""Suite-wide guards.

Deliberately tiny. This repository had no ``conftest.py`` and did not need one:
tests patch the real modules' attributes, which keeps the call sites under test
real and the wiring visible in each file. Nothing here changes that.

What it does own is one thing no individual test can: the per-document
knowledge stage now hangs off ``app.ingestion.pipeline._handle``, so *every*
test that drives an ingestion — ``test_bundle_moves``, ``test_batch_ingest``,
``test_empty_extraction`` and others — would run it as a side effect on any
machine whose ``.env`` sets ``KNOWLEDGE_PROCESS_AFTER_INDEX=true``. That is not
hypothetical: it happened, it reached the real MySQL and Qdrant, and it left
knowledge-run rows for fixture documents like ``doc-1`` and ``mover``.

Those tests still passed — the stage is fail-open, which is the point — so the
only symptom was a slower suite quietly doing real work against real stores.
A guard that fails loudly is not available here; a guard that makes the default
explicit is.

So the flag is forced off for every test, and a test that wants the stage turns
it on for itself (see ``tests/knowledge/test_knowledge_hook.py``, which patches
``get_settings`` outright). The suite's behaviour then does not depend on the
developer's ``.env``, which is the property that was missing.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _retrieval_logging_off_by_default(monkeypatch):
    """Keep per-query retrieval traces out of unrelated tests.

    Same reasoning as the knowledge-stage guard below: with ``is_retrieval_log``
    set in a developer's ``.env``, every test that drives the pipeline would
    write a trace file into the real ``logs/`` directory. A test that wants the
    trace turns it on for itself (see
    ``tests/observability/test_retrieval_log.py``).
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "is_retrieval_log", False, raising=False)


@pytest.fixture(autouse=True)
def _priority_pages_off_by_default(monkeypatch):
    """Keep live page reads out of unrelated tests.

    The feature ships on, and with it on every test that drives the pipeline
    would read teriin.org. A test of the feature turns it on for itself and
    stubs the fetch.
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "priority_pages_enabled", False, raising=False)


@pytest.fixture(autouse=True)
def _catalog_answer_detail_off_by_default(monkeypatch):
    """Keep the catalog answers' detail queries out of unrelated tests.

    The feature ships on, and with it on every test that drives a catalog tool
    would issue the follow-up SELECTs its stubs never anticipated — reaching
    the developer's MySQL, or failing open and testing nothing. A test of the
    feature turns it on for itself and stubs the queries.
    """
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "catalog_answer_detail_enabled", False, raising=False
    )


@pytest.fixture(autouse=True)
def _knowledge_stage_off_by_default(monkeypatch):
    """Keep the ingest-path knowledge stage out of unrelated ingestion tests.

    Set on the cached ``Settings`` instance rather than the environment,
    because ``get_settings`` is memoized and every caller shares that object.
    ``raising=False`` so the fixture survives the attribute being renamed
    without turning one rename into a suite-wide error.
    """
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "knowledge_process_after_index", False, raising=False
    )


@pytest.fixture(autouse=True)
def _fresh_claim_call_budget():
    """Give every test its own run-level claim-extraction allowance.

    The budget registry is deliberately process-global — that is what makes one
    ceiling span every document of an ingestion run. The cost is that tests
    driving ``process_document`` without a run id all share the ``__unscoped__``
    entry, so the allowance would drain across the session and whichever test
    happened to run after the 200th call would fail for reasons of its own.
    """
    from app.knowledge.claims import run_budget

    run_budget.reset()
    yield
    run_budget.reset()
