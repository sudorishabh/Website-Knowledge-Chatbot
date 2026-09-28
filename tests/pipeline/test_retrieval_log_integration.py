"""The trace, taken through the real retrieval path rather than around it.

``tests/observability/test_retrieval_log.py`` covers the logging package on its
own. This file asserts the thing that matters about it: that the instrumentation
is wired into the pipeline the application actually runs, so turning
``is_retrieval_log`` on explains a real query without anyone having to add a
call. Qdrant and the embedder are stubbed; no network, no LLM.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.pipeline import query_pipeline as pipe
from app.retrieval.context import builder
from app.retrieval.search import hybrid_search
from app.retrieval.understanding import query_processor as qp


class _Point:
    def __init__(self, id_, score, payload):
        self.id = id_
        self.score = score
        self.payload = payload
        self.vector = [0.1, 0.2]


def _point(id_, score, **payload):
    payload.setdefault("chunk_text", f"passage {id_}")
    payload.setdefault("document_id", f"doc-{id_}")
    payload.setdefault("source_type", "pdf")
    payload.setdefault("title", "Annual Report 2024")
    return _Point(id_, score, payload)


class _FakeQdrant:
    """Answers the two calls the read path makes: a dense pull and a parent fetch."""

    def __init__(self):
        self.queries = []

    def collection_exists(self, name):
        return True

    def query_points(self, **kwargs):
        self.queries.append(kwargs)
        return SimpleNamespace(
            points=[_point("a", 0.82), _point("b", 0.61)]
        )

    def retrieve(self, **kwargs):
        return []


@pytest.fixture
def traced(monkeypatch, tmp_path):
    """Retrieval logging on, writing to a temporary tree; stores stubbed."""
    settings = get_settings()
    monkeypatch.setattr(settings, "is_retrieval_log", True, raising=False)
    monkeypatch.setattr(settings, "retrieval_log_dir", str(tmp_path), raising=False)
    # The graph and the semantic cache have their own tests; keeping them out
    # leaves exactly the Qdrant legs to assert on.
    monkeypatch.setattr(settings, "graph_routing_enabled", False, raising=False)
    monkeypatch.setattr(settings, "semantic_cache_enabled", False, raising=False)

    fake = _FakeQdrant()
    monkeypatch.setattr(hybrid_search, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(builder, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(
        hybrid_search, "get_embeddings", lambda: SimpleNamespace(
            embed_query=lambda text: [0.1, 0.2]
        )
    )
    monkeypatch.setattr(
        "app.retrieval.retriever.embed_query", lambda text: [0.1, 0.2]
    )
    monkeypatch.setattr(
        qp, "process",
        lambda question, history=None: qp.ProcessedQuery(
            original=question, search_query="rooftop solar capacity 2024", intent="qa"
        ),
    )
    monkeypatch.setattr(pipe, "process", qp.process)
    return tmp_path, fake


def _trace(root):
    files = [p for p in root.glob("*/*/trace.json")]
    assert len(files) == 1, f"expected one trace, found {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))


def _report(root):
    files = [p for p in root.glob("*/*/report.md")]
    assert len(files) == 1, f"expected one report, found {files}"
    return files[0].read_text(encoding="utf-8")


def test_a_search_query_writes_a_trace_of_the_real_pulls(traced):
    root, fake = traced

    result = pipe.search_blocks("how much rooftop solar was added in 2024?")

    assert result["blocks"], "the stubbed pull should have produced blocks"
    trace = _trace(root)

    # The query as understanding rewrote it — the thing that was actually searched.
    assert trace["question"] == "how much rooftop solar was added in 2024?"
    assert trace["query"]["search_query"] == "rooftop solar capacity 2024"
    assert trace["query"]["intent"] == "qa"
    assert trace["entrypoint"] == "search"

    # One event per real Qdrant call, named by the leg that issued it. The
    # website-preference default splits the base pull in two, and the trace has
    # to be able to tell them apart.
    stages = [e["stage"] for e in trace["events"] if e["retriever"] == "qdrant"]
    assert "website_pull" in stages and "not_website_pull" in stages
    assert len(stages) == len(fake.queries)

    pull = next(e for e in trace["events"] if e["stage"] == "not_website_pull")
    assert pull["operation"] == "vector_search"
    assert pull["request"]["collection"] == get_settings().qdrant_collection
    assert pull["request"]["limit"] == get_settings().retrieval_candidate_k
    # The filter is recorded as one line, so a pull that returned nothing can be
    # explained by what it was asked for without reading forty lines of tree.
    assert pull["request"]["filter"] == (
        "is_parent=false AND is_current=true AND NOT (section_type in "
        "[toc, references, glossary] OR source_type=website)"
    )
    assert pull["result_count"] == 2
    # One line per hit: rank, score, provenance, snippet.
    assert pull["results"][0].startswith(" 1. 0.820  Annual Report 2024")
    assert "passage a" in pull["results"][0]
    assert pull["latency_ms"] >= 0

    # The per-retriever roll-up, and the context that came out the other end.
    # Only Qdrant is asserted: retrieval's own catalog reads (the title leg's
    # gazetteer) are traced too, and whether MySQL is reachable is a property of
    # the machine, not of the trace.
    assert "qdrant" in trace["retrievers"]["invoked"]
    assert trace["retrievers"]["totals"]["qdrant"]["calls"] == len(fake.queries)
    assert trace["context"]["block_count"] == len(result["blocks"])
    assert trace["context"]["blocks"][0]["text"]
    assert trace["outcome"]["answered"] is True
    assert trace["notes"]["candidates"] >= 2
    assert trace["notes"]["legs"]["dual"] is True
    assert trace["errors"] == []


def _wire_stream(monkeypatch, blocks):
    """The streaming entrypoint with generation stubbed, as
    tests/generation/test_faithfulness_claims.py does it."""
    pq = qp.ProcessedQuery(original="q", search_query="q")
    generation = pipe._Generation(pq=pq, blocks=blocks, query_vector=[0.1], top_k=6)
    monkeypatch.setattr(pipe, "_prepare", lambda q, **kw: (None, generation))
    monkeypatch.setattr(
        pipe, "generate_stream",
        lambda q, b, history=None, answer_format=None, plan_directive="", notes=():
            iter(["the answer ", "[1]"]),
    )
    monkeypatch.setattr(pipe, "_persist", lambda gen, result: None)
    return generation


def test_the_streaming_entrypoint_writes_a_trace(traced, monkeypatch):
    root, _ = traced
    from app.core.models.context import ContextBlock

    blocks = [ContextBlock(n=1, text="evidence text", payload={"document_id": "d1"})]
    _wire_stream(monkeypatch, blocks)

    events = [e["type"] for e in pipe.stream_answer("what does the report say?")]
    assert events[-1] == "done"

    trace = _trace(root)
    assert trace["entrypoint"] == "chat.stream"
    assert trace["context"]["block_count"] == 1
    assert trace["context"]["blocks"][0]["text"] == "evidence text"
    assert trace["outcome"]["answered"] is True
    assert trace["outcome"]["used_chunks"] == 1
    assert trace["timings"]["finished_at"]


def test_the_outcome_survives_the_streaming_thread_hop(traced, monkeypatch):
    """The regression this exists for: the outcome is recorded after the first
    token, and the SSE driver resumes the generator in a fresh context where the
    active-trace ContextVar is unset (app/api/chat.py::_sse). Every streamed
    query wrote ``"outcome": {}`` until the trace was passed explicitly — the
    retrieval was real, the verdict on it was silently dropped.

    Driven through ``contextvars.copy_context()`` per event, which is what a
    threadpool hop does to a generator's context.
    """
    import contextvars

    root, _ = traced
    from app.core.models.context import ContextBlock

    _wire_stream(monkeypatch, [ContextBlock(n=1, text="evidence", payload={})])

    stream = pipe.stream_answer("what does the report say?")
    while True:
        # Each event is pulled in its own context copy, as the driver does.
        event = contextvars.copy_context().run(lambda: next(stream, None))
        if event is None:
            break

    trace = _trace(root)
    assert trace["outcome"]["answered"] is True
    assert trace["outcome"]["used_chunks"] == 1
    assert trace["outcome"]["cached"] is False
    assert trace["outcome"]["answer_chars"] > 0
    assert trace["outcome"]["latency_ms"] >= 0


def test_the_context_records_the_prompt_size_it_could_not_keep(traced, monkeypatch):
    """A truncated sample must not read as the whole prompt: the per-block text
    is clipped, but ``prompt_chars`` is the true size of the rendered context the
    model was sent, and ``text_chars`` the true size of each block."""
    root, _ = traced
    from app.core.models.context import ContextBlock

    long_text = "x" * 5000
    _wire_stream(monkeypatch, [ContextBlock(n=1, text=long_text, payload={})])
    monkeypatch.setattr(
        get_settings(), "retrieval_log_max_text_chars", 100, raising=False
    )

    list(pipe.stream_answer("q"))

    context = _trace(root)["context"]
    assert context["blocks"][0]["text_chars"] == 5000          # what the LLM got
    assert len(context["blocks"][0]["text"]) < 200              # what the log kept
    assert context["total_chars"] == 5000
    # The rendered context is longer than the block: it carries the [1] header.
    assert context["prompt_chars"] > 5000


def test_a_disconnected_client_still_leaves_a_trace(traced, monkeypatch):
    """The SSE driver closes the generator on disconnect (see app/api/chat.py);
    the trace is written from the same `finally` the pipeline's own cleanup runs
    in, so an abandoned query is still explainable."""
    root, _ = traced
    from app.core.models.context import ContextBlock

    _wire_stream(monkeypatch, [ContextBlock(n=1, text="evidence", payload={})])

    stream = pipe.stream_answer("what does the report say?")
    next(stream)          # one token, then the client goes away
    stream.close()

    trace = _trace(root)
    assert trace["entrypoint"] == "chat.stream"
    assert trace["context"]["block_count"] == 1
    assert trace["timings"]["finished_at"]


def test_the_same_query_writes_nothing_when_the_flag_is_off(traced, monkeypatch):
    root, _ = traced
    monkeypatch.setattr(get_settings(), "is_retrieval_log", False, raising=False)

    result = pipe.search_blocks("how much rooftop solar was added in 2024?")

    assert result["blocks"]  # the answer is unchanged...
    assert list(root.rglob("*.json")) == []  # ...and nothing was written
