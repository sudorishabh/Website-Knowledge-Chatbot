"""Ingest-time document enrichment: the per-document abstract.

An abstract generated here replaces the stand-in
:func:`app.retrieval.search.scoped_retrieval.lead_parents` uses at query time — a
document's *first section*, which for a long report is its cover page or table
of contents. Generating it at ingest sees the whole document and is paid once
per ``doc_version`` rather than on every query that touches the document (see
docs/ingestion-improvements-roadmap.md, item 7).

Sizing is adaptive: a document that fits in one call gets one call, which covers
most Drupal content and short PDFs. Longer documents are summarized in two
stages — notes per window, then one reduce — so the map/reduce that
:mod:`app.pipeline.summarize` currently runs per query happens here instead.

Failure contract: this module **raises** when the model call fails, so the
caller can count the attempt (:func:`app.catalog.enrichment.record_failure`),
and returns ``None`` only for documents deliberately skipped — those must not be
retried. Keeping ingestion running in the face of a failed call is the caller's
job, matching how every other external dependency here degrades. The one raise
the caller must *not* count is the shutdown race — see :func:`is_shutdown_error`.
"""
from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings
from app.core.models import CanonicalDocument
from app.ingestion.chunking.packer import Encoder, get_encoder

logger = logging.getLogger(__name__)

__all__ = ["abstract_version", "generate_abstract", "is_shutdown_error"]

# Long enough to carry a document's main threads, short enough that a full
# scope of thirty fits in one reduce call downstream.
_TARGET_WORDS = 200

# Bodies shorter than this are their own best summary — a `people` record, a
# video stub, an infographic caption. Summarizing them buys a paraphrase and a
# hallucination surface for no gain.
_MIN_CHARS = 600

# Above this the document is summarized in two stages instead of one call.
_SINGLE_CALL_TOKENS = 12_000

# Window size for the map stage, and how many windows run at once.
_MAP_WINDOW_TOKENS = 6_000
_MAP_WORKERS = 4

_ENCODING = "cl100k_base"

_DIRECT_SYSTEM = (
    f"Write a factual abstract of the document below, in at most {_TARGET_WORDS} "
    "words. Use ONLY the document's own text: no outside knowledge, no "
    "speculation, numbers and dates copied exactly. Say what the document is "
    "about, its main findings or claims, and the scope it covers (sector, "
    "region, period) wherever the text states them. Write plain prose — no "
    "headings, no bullets, and no preamble restating that this is a document."
)

_MAP_SYSTEM = (
    "You are summarizing ONE PART of a longer document for a later aggregation "
    "step. Produce 3-5 short factual bullets, strictly from the text given: no "
    "outside knowledge, numbers copied exactly. Do not speculate about parts of "
    "the document you cannot see, and do not describe the excerpt itself."
)

_REDUCE_SYSTEM = (
    f"Write a factual abstract of a single document, in at most {_TARGET_WORDS} "
    "words, from the section notes below. The notes are in document order and "
    "are the only source available: no outside knowledge, numbers copied "
    "exactly. Write plain prose, not a list."
)


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def abstract_version() -> str:
    """Fingerprint of everything that determines what an abstract says.

    Stored beside each cached abstract; a mismatch reads as a cache miss, so
    editing a prompt or repointing the deployment re-enriches transparently
    instead of serving output the current code would not produce. The prompts
    themselves are hashed, which is what makes that automatic. Mirrors
    ``app.cache.cache_keys._pref_fingerprint``.
    """
    return _fingerprint(
        _DIRECT_SYSTEM,
        _MAP_SYSTEM,
        _REDUCE_SYSTEM,
        str(_SINGLE_CALL_TOKENS),
        str(_MAP_WINDOW_TOKENS),
        get_settings().azure_openai_model,
    )


def _complete(system: str, human: str) -> str:
    """One chat completion at the pinned parsing temperature.

    The single seam every model call goes through, so callers and tests have one
    thing to stub.
    """
    from app.core.clients.llm import get_llm, reply_text

    model = get_llm(temperature=get_settings().llm_structured_temperature)
    response = model.invoke([("system", system), ("human", human)])
    return reply_text(response).strip()


def _windows(text: str, budget: int, enc: Encoder) -> list[str]:
    """Split ``text`` into ~``budget``-token windows on paragraph boundaries.

    Deliberately independent of the chunker: enrichment runs while the canonical
    document is being built, before chunking, and must not depend on
    parent/child sizing or on a document version that does not exist yet.
    """
    out: list[str] = []
    current: list[str] = []
    spent = 0
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        cost = enc.count(para)
        if cost > budget:  # one oversized block — split it on its own
            if current:
                out.append("\n\n".join(current))
                current, spent = [], 0
            out.extend(enc.split_to_token_limit(para, budget))
            continue
        if current and spent + cost > budget:
            out.append("\n\n".join(current))
            current, spent = [], 0
        current.append(para)
        spent += cost
    if current:
        out.append("\n\n".join(current))
    return out


def is_shutdown_error(exc: BaseException) -> bool:
    """True when ``exc`` is the map stage losing its pool to a dying process.

    :func:`_section_notes` opens a pool per document, and ``ThreadPoolExecutor``
    refuses to schedule once the interpreter is shutting down — which is what an
    ingest worker thread hits when the main thread has already exited (a Ctrl-C
    during a long extraction, say). The model was never asked and there is
    nothing wrong with the document, so the caller must not spend one of its
    attempts on it.
    """
    return isinstance(exc, RuntimeError) and "interpreter shutdown" in str(exc)


def _section_notes(windows: list[str]) -> str:
    with ThreadPoolExecutor(max_workers=_MAP_WORKERS) as pool:
        notes = list(pool.map(lambda w: _complete(_MAP_SYSTEM, w), windows))
    return "\n".join(note for note in notes if note)


def generate_abstract(doc: CanonicalDocument) -> str | None:
    """A factual abstract of ``doc``, or None when there is nothing to summarize.

    Raises whatever the model client raises; see the module docstring for why
    that is not caught here.
    """
    text = doc.full_text().strip()
    if len(text) < _MIN_CHARS:
        logger.debug(
            "Skipping abstract for %s: %d chars of body text.", doc.document_id, len(text)
        )
        return None

    enc = get_encoder(_ENCODING)
    header = f"Title: {doc.title}\n\n" if doc.title else ""

    if enc.count(text) <= _SINGLE_CALL_TOKENS:
        return _complete(_DIRECT_SYSTEM, f"{header}{text}").strip() or None

    notes = _section_notes(_windows(text, _MAP_WINDOW_TOKENS, enc))
    if not notes:
        return None
    return _complete(_REDUCE_SYSTEM, f"{header}Section notes:\n{notes}").strip() or None
