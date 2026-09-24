"""In-process stage timing metrics: which pipeline stage takes how much time.

Every ``tracing.span()`` reports its elapsed time here, keyed by span name
("rag.search", "ingest.embed", ...). ``snapshot()`` returns per-stage
aggregates (count / total / avg / p50 / p95 / max) served by
``GET /metrics/timings``, and ``collect_into()`` gathers a per-request
breakdown that ends up on the ``rag_metrics`` log line.

The registry is per-process — each uvicorn worker keeps its own numbers and
they reset on restart. Parent spans (e.g. "rag.stream_answer") include the
time of the child stages they wrap, so stage totals overlap by design.
"""

from __future__ import annotations

import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

_WINDOW = 512  # recent samples kept per stage; percentiles cover this window

# Which dependency a stage's time is spent on, so the read-outs can answer
# "how much time did Qdrant / the LLM / everything else take" directly.
# Unmapped stages (context build, chunking, ...) count as "other"; the parent
# spans are excluded from component totals — they wrap the stages below and
# would double-count.
_PARENTS = {"rag.stream_answer"}
_COMPONENTS = {
    "rag.search": "qdrant",
    "rag.semantic_cache": "qdrant",
    "rag.semantic_cache_store": "qdrant",
    "ingest.upsert": "qdrant",
    "rag.generate": "llm",
    "rag.query_understanding": "llm",
    "rag.faithfulness": "llm",
    "rag.embed_query": "embedding",
    "ingest.embed": "embedding",
    "rag.rerank": "rerank",
    "ingest.extract": "extraction",
}


def component_of(stage: str) -> str | None:
    """The component a stage belongs to; None for parent (total) spans."""
    if stage in _PARENTS:
        return None
    return _COMPONENTS.get(stage, "other")


def component_totals(stages: dict[str, float]) -> dict[str, float]:
    """Fold a per-stage breakdown into per-component time (ms)."""
    totals: dict[str, float] = {}
    for name, ms in stages.items():
        component = component_of(name)
        if component is None:
            continue
        totals[component] = round(totals.get(component, 0.0) + ms, 1)
    return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))


class _StageStats:
    __slots__ = ("count", "total_ms", "max_ms", "samples")

    def __init__(self) -> None:
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.samples: deque[float] = deque(maxlen=_WINDOW)

    def add(self, elapsed_ms: float) -> None:
        self.count += 1
        self.total_ms += elapsed_ms
        if elapsed_ms > self.max_ms:
            self.max_ms = elapsed_ms
        self.samples.append(elapsed_ms)


_lock = threading.Lock()
_stages: dict[str, _StageStats] = {}
_since = datetime.now(timezone.utc)

# Outcome counters, keyed family -> outcome -> count. Stage timings answer "how
# long"; these answer "how often, and how did it end" — which is the question a
# fallback path raises. A route that silently fell back looks identical to one
# that was never attempted unless the two are counted apart.
_events: dict[str, dict[str, int]] = {}

_breakdown: ContextVar[dict[str, float] | None] = ContextVar(
    "stage_breakdown", default=None
)


def record_stage(name: str, elapsed_ms: float) -> None:
    with _lock:
        stats = _stages.get(name)
        if stats is None:
            stats = _stages[name] = _StageStats()
        stats.add(elapsed_ms)
    per_request = _breakdown.get()
    if per_request is not None:
        per_request[name] = per_request.get(name, 0.0) + elapsed_ms


def record_event(family: str, outcome: str) -> None:
    """Count one outcome in a family, e.g. ``("graph_routing", "zero_result")``."""
    with _lock:
        bucket = _events.get(family)
        if bucket is None:
            bucket = _events[family] = {}
        bucket[outcome] = bucket.get(outcome, 0) + 1


def events() -> dict[str, dict[str, int]]:
    """Outcome counters, with a total and a share per family."""
    with _lock:
        copied = {family: dict(counts) for family, counts in _events.items()}
    out: dict[str, dict[str, Any]] = {}
    for family, counts in sorted(copied.items()):
        total = sum(counts.values())
        out[family] = {
            "total": total,
            "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "share_pct": {
                name: round(100.0 * count / total, 1)
                for name, count in sorted(counts.items(), key=lambda kv: -kv[1])
            } if total else {},
        }
    return out


@contextmanager
def collect_into(breakdown: dict[str, float]) -> Iterator[None]:
    """Additionally route this context's span timings into ``breakdown``.

    The caller owns the dict, so the collected numbers survive even when the
    context doesn't: the chat SSE stream advances the RAG generator with one
    threadpool hop per event (see app.api.chat._sse), so spans after the first
    yield run in fresh context copies. Those still hit the global registry —
    only the per-request dict misses them — and the reset below may then see
    a token from another context, which is harmless because that context copy
    is discarded anyway.
    """
    token = _breakdown.set(breakdown)
    try:
        yield
    finally:
        try:
            _breakdown.reset(token)
        except ValueError:  # generator resumed in a different context
            pass


def _percentile(ordered: list[float], q: float) -> float:
    return ordered[max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))]


def snapshot() -> dict[str, Any]:
    with _lock:
        copied = {
            name: (s.count, s.total_ms, s.max_ms, list(s.samples))
            for name, s in _stages.items()
        }
    stages = []
    component_time: dict[str, float] = {}
    component_calls: dict[str, int] = {}
    for name, (count, total, max_ms, samples) in sorted(
        copied.items(), key=lambda item: item[1][1], reverse=True
    ):
        ordered = sorted(samples)
        component = component_of(name)
        stages.append(
            {
                "stage": name,
                "component": component or "total",
                "count": count,
                "total_ms": round(total, 1),
                "avg_ms": round(total / count, 1),
                "p50_ms": round(_percentile(ordered, 0.50), 1),
                "p95_ms": round(_percentile(ordered, 0.95), 1),
                "max_ms": round(max_ms, 1),
            }
        )
        if component is not None:
            component_time[component] = component_time.get(component, 0.0) + total
            component_calls[component] = component_calls.get(component, 0) + count

    attributed = sum(component_time.values())
    components = [
        {
            "component": name,
            "total_ms": round(total, 1),
            "calls": component_calls[name],
            "share_pct": round(100.0 * total / attributed, 1) if attributed else 0.0,
        }
        for name, total in sorted(
            component_time.items(), key=lambda item: item[1], reverse=True
        )
    ]
    return {
        "since": _since.isoformat(),
        "window": _WINDOW,
        "components": components,
        "stages": stages,
        "events": events(),
    }


def reset() -> None:
    global _since
    with _lock:
        _stages.clear()
        _events.clear()
        _since = datetime.now(timezone.utc)
