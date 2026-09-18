from __future__ import annotations

import hashlib

from app.catalog.queries import corpus_revision
from app.config import get_settings


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


#: The read path's behaviour revision. Bumped by hand — the date of the change
#: is the convention — when a code change alters what the *same* question should
#: be answered with: how candidates are ranked, how the context is composed,
#: what the prompt asks for. Settings changes self-invalidate through
#: `_pref_fingerprint`; a code change has no knob to hash, so without this an
#: answer generated under the old behaviour is served for the rest of its TTL
#: (24 hours by default) and a ranking fix looks, from the chat window, like it
#: did nothing. That is how the fix for "who is X" answering from a 2020 page
#: was reported as not working three times: each retest matched the cached
#: answer at cosine 1.0.
PIPELINE_REVISION = "2026-09-18"


def _pref_fingerprint() -> str:
    """Hash of the retrieval-preference settings, plus `PIPELINE_REVISION`, so
    that toggling the feature, tuning its knobs, or shipping a behaviour change
    self-invalidates the semantic cache (otherwise old-mode answers would be
    served until TTL and pollute before/after comparisons)."""
    s = get_settings()
    return _sha(
        PIPELINE_REVISION,
        str(s.prefer_website_enabled),
        str(s.website_candidate_k),
        str(s.retrieval_top_k),
        str(s.retrieval_candidate_k),
        str(s.context_token_budget),
    )


def semantic_partition(top_k: int, answer_format: str) -> str | None:
    """Partition key for the semantic cache, or None when it must not be used.

    Four things decide whether a stored answer is still the answer: the
    retrieval-preference fingerprint, the result width, the answer format, and
    **the state of the corpus it was grounded in**. The last one used to be
    missing, so an answer survived any amount of ingestion and could be served
    for the whole TTL quoting text that had since been re-indexed or deleted.

    ``None`` when the corpus revision is unknown: an answer that cannot be dated
    against the corpus cannot be shown to be fresh, and bypassing the cache is
    the only safe reading of that. Callers skip the cache rather than fall back
    to a partial key.

    Caller identity is deliberately absent. The corpus is public and every
    caller retrieves over all of it, so two callers asking the same question
    are owed the same answer — partitioning by identity would only fragment
    the cache."""
    revision = corpus_revision()
    if revision is None:
        return None
    return _sha(_pref_fingerprint(), str(top_k), answer_format, revision)
