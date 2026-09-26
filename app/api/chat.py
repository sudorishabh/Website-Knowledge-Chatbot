"""SSE chat endpoint.

Event contract (each ``data:`` line is one JSON object, keyed by ``type``):
  token      — one answer fragment; concatenate in order.
  correction — full replacement answer text (a post-generation check flagged
               the streamed draft; ``reason`` says which: ``faithfulness``,
               ``date_claim``, ``date_claim_fallback``, ``unknown_link`` or
               ``list_citations``).
               Discard prior tokens.
  sources    — citations + answer metadata; follows the final answer text.
  done       — normal end of stream.
  error      — the stream failed mid-response; the answer is incomplete.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import AsyncIterator, Iterator

import anyio
import anyio.to_thread
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.auth import Principal, require_principal
from app.config import get_settings
from app.pipeline.query_pipeline import stream_answer
from app.schemas.query import QueryRequest

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)

_END = object()  # sentinel: the sync event stream is exhausted


@lru_cache(maxsize=1)
def _chat_limiter() -> anyio.CapacityLimiter:
    return anyio.CapacityLimiter(get_settings().chat_stream_max_concurrency)


def _next_event(events: Iterator[dict]) -> object:
    # StopIteration must not escape into async code (PEP 479); map it to _END.
    try:
        return next(events)
    except StopIteration:
        return _END


async def _sse(events: Iterator[dict]) -> AsyncIterator[str]:
    """Drive the blocking RAG event stream from the event loop, borrowing a
    worker thread per event from a chat-only capacity limiter.

    The pipeline blocks inside ``next()`` (retrieval, then a network wait per
    LLM token), so a sync iterator here would pin one of the ~40 shared
    request-threadpool threads per active chat for the whole generation —
    enough concurrent chats starve auth dependencies, probes and every other
    sync offload. The dedicated limiter isolates chat load; extra chats queue
    against it instead of the shared pool.
    """
    limiter = _chat_limiter()
    try:
        while True:
            event = await anyio.to_thread.run_sync(_next_event, events, limiter=limiter)
            if event is _END:
                break
            yield f"data: {json.dumps(event)}\n\n"
    except Exception:
        # The 200 + headers are already on the wire, so an HTTP error is no
        # longer possible; a terminal SSE event is the only way to tell the
        # client the answer was cut short (a bare disconnect renders as a
        # complete answer). Generic on purpose — details stay in the log.
        logger.exception("Chat stream failed mid-response.")
        yield 'data: {"type": "error"}\n\n'
    finally:
        # Runs on normal completion and on client disconnect: close the sync
        # generator so the pipeline's finally blocks (spans, cache writes in
        # flight) execute and generation work stops.
        await anyio.to_thread.run_sync(events.close, limiter=limiter)


@router.post("/chat")
async def chat(
    request: QueryRequest,
    # Kept for its side effect: this is the authentication gate, rejecting a
    # missing or invalid token when auth_enabled. The identity it returns no
    # longer scopes retrieval — the corpus is public and every caller sees it
    # whole — so nothing downstream reads it.
    _principal: Principal = Depends(require_principal),
) -> StreamingResponse:
    events = stream_answer(
        request.question,
        history=[turn.model_dump() for turn in request.history],
        top_k=request.top_k,
    )
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
